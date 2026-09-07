#!/usr/bin/env python3
"""Recompute evidence and write a fail-closed 24-hour evaluation snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.research_proof_audit_24h import (
    atomic_json,
    audit_pass_coverage,
    extract_outcomes,
    now_iso,
)
from agent_monitor.proof_tools import audit_output_validator


TOOL_FAILURE_RE = re.compile(
    r"(?:no module named|failed before evaluating|tool unavailable|gatewaycredentialsrequired|gateway closed)",
    re.IGNORECASE,
)
HIGH_STAKES_OUTCOMES = {"Solved", "Counterexample"}
DEGRADED_AUDIT_RE = re.compile(
    r"(?im)(?:^|[.!?]\s+|\\(?:emph|textbf|textit)\{|[*_`]{1,3})"
    r"(?:this\s+is\s+(?:an?\s+)?)?"
    r"degraded\s+audit\s*(?::|[.\u2014-]|$)"
)
DEGRADED_NEGATION_RE = re.compile(
    r"(?i)\b(?:no|not|never|without)(?:\s+(?:a|an|the|such))*\s*$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _usage_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate only numeric telemetry that the runtime actually reported."""
    fields = ("input_tokens", "output_tokens", "cost_usd", "latency_s")
    result: dict[str, Any] = {
        "runs": len(runs),
        "runs_with_token_usage": sum(
            isinstance(run.get("input_tokens"), (int, float))
            or isinstance(run.get("output_tokens"), (int, float))
            for run in runs
        ),
        "runs_with_cost": sum(
            isinstance(run.get("cost_usd"), (int, float)) for run in runs
        ),
    }
    for field in fields:
        values = [
            value
            for run in runs
            if isinstance((value := run.get(field)), (int, float))
            and not isinstance(value, bool)
        ]
        result[field] = sum(values) if values else None
    return result


def affirmative_degraded_audit(content: str) -> bool:
    """Accept explicit degraded disclosures, but reject nearby negations."""
    for match in DEGRADED_AUDIT_RE.finditer(content):
        prefix = content[max(0, match.start() - 64):match.start()]
        normalized = re.sub(r"[`*_{}\\]+", " ", prefix)
        if DEGRADED_NEGATION_RE.search(normalized):
            continue
        return True
    return False


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_file(root: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None
    unresolved = root / relative
    if unresolved.is_symlink():
        return None
    candidate = unresolved.resolve()
    if (
        not candidate.is_relative_to(root.resolve())
        or not candidate.is_file()
        or candidate.is_symlink()
    ):
        return None
    return candidate


def _nonempty_repair_history(run_manifest: dict[str, Any]) -> bool:
    for key in ("repair_history", "repairs"):
        value = run_manifest.get(key)
        if isinstance(value, (list, dict)) and bool(value):
            return True
    return False


def _validate_reconciliation(
    workspace: Path,
    audit_dir: Path,
    merged_path: Path,
    run_manifest: dict[str, Any],
    proof: Path | None,
    validations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Bind the final proof to the exact candidate reviewed by current pass files."""
    errors: list[str] = []
    reconciliation = run_manifest.get("reconciliation")
    if not isinstance(reconciliation, dict):
        return {
            "valid": False,
            "status": None,
            "errors": ["run.json is missing the reconciliation object"],
        }
    if reconciliation.get("schema_version") != "research-proof-audit.reconciliation.v1":
        errors.append("unsupported reconciliation schema_version")

    status = reconciliation.get("status")
    if status not in {"audited_unchanged", "repaired_revalidated"}:
        errors.append("status must be audited_unchanged or repaired_revalidated")

    final_artifact = reconciliation.get("final_artifact")
    final_sha: str | None = None
    final_high_stakes = False
    if proof is None or not isinstance(final_artifact, dict):
        errors.append("final_artifact is missing or no root proof artifact exists")
    else:
        final_path = _safe_file(workspace, final_artifact.get("path"))
        claimed_sha = final_artifact.get("sha256")
        if final_path is None or final_path.resolve() != proof.resolve():
            errors.append("final_artifact.path does not identify the root proof artifact")
        elif not isinstance(claimed_sha, str) or not SHA256_RE.fullmatch(claimed_sha):
            errors.append("final_artifact.sha256 is not a lowercase SHA-256 digest")
        else:
            final_sha = _sha256_file(final_path)
            try:
                final_high_stakes = any(
                    item in HIGH_STAKES_OUTCOMES
                    for item in extract_outcomes(
                        final_path.read_text(encoding="utf-8", errors="replace")
                    )
                )
            except OSError:
                errors.append(
                    "final_artifact could not be read for outcome validation"
                )
            if claimed_sha != final_sha:
                errors.append("final_artifact.sha256 does not match the current proof")

    audited_candidate = reconciliation.get("audited_candidate")
    audited_sha: str | None = None
    if not isinstance(audited_candidate, dict):
        errors.append("audited_candidate is missing")
    else:
        candidate_path = _safe_file(audit_dir, audited_candidate.get("path"))
        claimed_sha = audited_candidate.get("sha256")
        if candidate_path is None:
            errors.append("audited_candidate.path is unsafe or unavailable")
        elif not isinstance(claimed_sha, str) or not SHA256_RE.fullmatch(claimed_sha):
            errors.append("audited_candidate.sha256 is not a lowercase SHA-256 digest")
        else:
            audited_sha = _sha256_file(candidate_path)
            if claimed_sha != audited_sha:
                errors.append("audited_candidate.sha256 does not match its file")
            if final_sha is not None and audited_sha != final_sha:
                errors.append("the audited candidate is not byte-identical to the final proof")

    initial_sha = run_manifest.get("candidate_sha256")
    if not isinstance(initial_sha, str) or not SHA256_RE.fullmatch(initial_sha):
        errors.append("run.json candidate_sha256 is missing or malformed")
    elif status == "audited_unchanged" and final_sha is not None and initial_sha != final_sha:
        errors.append("audited_unchanged does not match the initial frozen candidate")

    reruns = reconciliation.get("reruns")
    if not isinstance(reruns, dict):
        errors.append("reruns must map global, decomposed, and merged files")
        reruns = {}
    resolved_reruns: dict[str, str] = {}
    for role in ("global", "decomposed", "merged"):
        relative = reruns.get(role)
        path = _safe_file(audit_dir, relative)
        if path is None:
            errors.append(f"reruns.{role} is unsafe or unavailable")
            continue
        normalized = path.name.lower().replace("_", "-")
        if role == "global" and not normalized.startswith("global"):
            errors.append("reruns.global is not a global pass file")
        if role == "decomposed" and not normalized.startswith("decomposed"):
            errors.append("reruns.decomposed is not a decomposed pass file")
        if role == "merged" and path.resolve() != merged_path.resolve():
            errors.append("reruns.merged is not the current merged verifier output")
        if status == "repaired_revalidated" and role in {"global", "decomposed"}:
            if "rerun" not in normalized:
                errors.append(f"repaired_revalidated requires an explicit {role} rerun file")
        workspace_relative = str(path.relative_to(workspace))
        resolved_reruns[role] = workspace_relative
        if not validations.get(workspace_relative, {}).get("valid"):
            errors.append(f"reruns.{role} did not pass the locked output validator")

    dependent = reconciliation.get("dependent_reruns")
    if dependent is not None and not isinstance(dependent, dict):
        errors.append("dependent_reruns must be an object when present")
        dependent = {}
    if not isinstance(dependent, dict):
        dependent = {}
    refuter_relative = dependent.get("refuter")
    if final_high_stakes and not isinstance(refuter_relative, str):
        errors.append("a high-stakes final outcome requires dependent_reruns.refuter")
    if isinstance(refuter_relative, str):
        refuter_path = _safe_file(audit_dir, refuter_relative)
        if refuter_path is None:
            errors.append("dependent_reruns.refuter is unsafe or unavailable")
        else:
            normalized = refuter_path.name.lower().replace("_", "-")
            if not normalized.startswith("refuter"):
                errors.append("dependent_reruns.refuter is not a refuter pass file")
            if status == "repaired_revalidated" and "rerun" not in normalized:
                errors.append(
                    "repaired_revalidated requires an explicit refuter rerun file"
                )
            workspace_relative = str(refuter_path.relative_to(workspace))
            resolved_reruns["refuter"] = workspace_relative
            if not validations.get(workspace_relative, {}).get("valid"):
                errors.append(
                    "dependent_reruns.refuter did not pass the locked output validator"
                )

    superseded = reconciliation.get("superseded_findings")
    if not isinstance(superseded, list) or not all(
        isinstance(item, str) and item for item in superseded
    ):
        errors.append("superseded_findings must be a list of nonempty finding IDs")
    if status == "repaired_revalidated":
        if not _nonempty_repair_history(run_manifest):
            errors.append("repaired_revalidated requires nonempty repair history")

    return {
        "valid": not errors,
        "status": status if isinstance(status, str) else None,
        "errors": errors,
        "final_sha256": final_sha,
        "audited_candidate_sha256": audited_sha,
        "rerun_files": resolved_reruns,
    }


def _relative_files(root: Path, folder: str) -> list[str]:
    base = root / folder
    if not base.is_dir():
        return []
    return sorted(
        str(path.relative_to(root))
        for path in base.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.resolve().is_relative_to(root.resolve())
    )


def _valid_merge_map(document: dict[str, Any]) -> bool:
    if set(document) != {"schema_version", "findings", "counts"}:
        return False
    if document.get("schema_version") != "ensemble-paper-audit-app.merge-map.v1":
        return False
    findings = document.get("findings")
    counts = document.get("counts")
    if not isinstance(findings, list) or not isinstance(counts, dict):
        return False
    count_keys = {"global_only", "decomposed_only", "both"}
    if set(counts) != count_keys or not all(
        type(counts.get(key)) is int and counts[key] >= 0 for key in count_keys
    ):
        return False
    allowed_passes = {
        "global",
        "decomposed",
        "display_sweep",
        "boundary_clause_sweep",
        "refuter",
        "citation",
    }
    finding_ids: set[str] = set()
    observed_counts = {key: 0 for key in count_keys}
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or set(finding) != {"finding_id", "sources"}
            or not isinstance(finding.get("finding_id"), str)
            or not finding["finding_id"]
            or finding["finding_id"] in finding_ids
            or not isinstance(finding.get("sources"), list)
            or not finding["sources"]
        ):
            return False
        finding_ids.add(finding["finding_id"])
        source_pairs: set[tuple[str, str]] = set()
        core_passes: set[str] = set()
        for source in finding["sources"]:
            if (
                not isinstance(source, dict)
                or set(source) != {"pass", "finding_id"}
                or not isinstance(source.get("pass"), str)
                or source["pass"] not in allowed_passes
                or not isinstance(source.get("finding_id"), str)
                or not source["finding_id"]
                or (source["pass"], source["finding_id"]) in source_pairs
            ):
                return False
            source_pairs.add((source["pass"], source["finding_id"]))
            if source["pass"] in {"global", "decomposed"}:
                core_passes.add(source["pass"])
        if core_passes == {"global", "decomposed"}:
            observed_counts["both"] += 1
        elif core_passes == {"global"}:
            observed_counts["global_only"] += 1
        elif core_passes == {"decomposed"}:
            observed_counts["decomposed_only"] += 1
        else:
            return False
    return counts == observed_counts


def _merge_map_matches_reruns(
    document: dict[str, Any],
    *,
    global_document: dict[str, Any],
    decomposed_document: dict[str, Any],
    merged_document: dict[str, Any],
) -> bool:
    """Bind merge provenance IDs to the exact rerun documents being accepted."""
    def ids(value: dict[str, Any]) -> set[str]:
        findings = value.get("findings")
        if not isinstance(findings, list):
            return set()
        return {
            finding["finding_id"]
            for finding in findings
            if isinstance(finding, dict)
            and isinstance(finding.get("finding_id"), str)
            and finding["finding_id"]
        }

    source_ids = {
        "global": ids(global_document),
        "decomposed": ids(decomposed_document),
    }
    map_ids: set[str] = set()
    for finding in document.get("findings") or []:
        if not isinstance(finding, dict):
            return False
        finding_id = finding.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id:
            return False
        map_ids.add(finding_id)
        for source in finding.get("sources") or []:
            if not isinstance(source, dict):
                return False
            pass_name = source.get("pass")
            source_id = source.get("finding_id")
            if pass_name in source_ids and source_id not in source_ids[pass_name]:
                return False
    return map_ids == ids(merged_document)


def validate_structured_audit(
    workspace: Path, coverage: dict[str, Any], proof: Path | None
) -> dict[str, Any]:
    """Validate every recognized pass plus merged provenance/run metadata."""
    files_by_role = coverage.get("files") or {}
    validations: dict[str, dict[str, Any]] = {}
    for role, relative_paths in files_by_role.items():
        for relative in relative_paths:
            path = (workspace / relative).resolve()
            if (
                not path.is_relative_to(workspace.resolve())
                or not path.is_file()
                or path.is_symlink()
            ):
                validations[relative] = {
                    "valid": False,
                    "phase": "path",
                    "errors": ["unsafe or unavailable audit artifact"],
                }
                continue
            document = _read_json(path)
            try:
                result = audit_output_validator({"document": document})
            except (OSError, ValueError) as exc:
                result = {
                    "valid": False,
                    "phase": "validator",
                    "errors": [str(exc)],
                }
            result.pop("certificate_sha256", None)
            result.pop("tool", None)
            if len(result.get("errors") or []) > 20:
                result["errors"] = result["errors"][:20] + ["... truncated"]
            validations[relative] = result

    merged_paths = list(files_by_role.get("merged") or [])
    provenance: list[dict[str, Any]] = []
    for relative in merged_paths:
        parent = (workspace / relative).parent
        merge_map_path = parent / "merge-map.json"
        run_path = parent / "run.json"
        merge_map = _read_json(merge_map_path)
        run_manifest = _read_json(run_path)
        reconciliation = _validate_reconciliation(
            workspace,
            parent,
            (workspace / relative).resolve(),
            run_manifest,
            proof,
            validations,
        )
        merge_map_valid = _valid_merge_map(merge_map)
        rerun_files = reconciliation.get("rerun_files") or {}
        if merge_map_valid and all(
            isinstance(rerun_files.get(role), str)
            for role in ("global", "decomposed", "merged")
        ):
            merge_map_valid = _merge_map_matches_reruns(
                merge_map,
                global_document=_read_json(workspace / rerun_files["global"]),
                decomposed_document=_read_json(
                    workspace / rerun_files["decomposed"]
                ),
                merged_document=_read_json(workspace / rerun_files["merged"]),
            )
        current_merged = (
            _read_json(workspace / rerun_files["merged"])
            if isinstance(rerun_files.get("merged"), str)
            else {}
        )
        final_audit_verdict = current_merged.get("verdict")
        current_findings = current_merged.get("findings")
        open_finding_count = (
            len(current_findings) if isinstance(current_findings, list) else None
        )
        independence = run_manifest.get("independence")
        degraded_independence = run_manifest.get("degraded_independence")
        if (
            degraded_independence is False
            and isinstance(independence, str)
            and independence
        ):
            independence_status = "independent"
        elif degraded_independence is True:
            independence_status = "degraded"
        else:
            independence_status = "unreported"
        provenance.append(
            {
                "directory": str(parent.relative_to(workspace)),
                "merge_map_present": merge_map_path.is_file()
                and not merge_map_path.is_symlink(),
                "merge_map_valid": merge_map_valid,
                "run_manifest_present": run_path.is_file()
                and not run_path.is_symlink(),
                "run_manifest_valid": bool(run_manifest),
                "independence_status": independence_status,
                "reconciliation": reconciliation,
                "final_audit_verdict": (
                    final_audit_verdict
                    if isinstance(final_audit_verdict, str)
                    else None
                ),
                "open_finding_count": open_finding_count,
                "audit_accepts_final": bool(
                    reconciliation.get("valid")
                    and final_audit_verdict == "accept"
                    and open_finding_count == 0
                ),
            }
        )

    core_paths = [
        relative
        for role in ("global", "decomposed", "merged")
        for relative in files_by_role.get(role) or []
    ]
    all_recognized_valid = bool(validations) and all(
        bool(result.get("valid")) for result in validations.values()
    )
    core_roles_valid = all(
        files_by_role.get(role)
        and all(
            validations.get(relative, {}).get("valid")
            for relative in files_by_role[role]
        )
        for role in ("global", "decomposed", "merged")
    )
    provenance_valid = bool(provenance) and all(
        item["merge_map_valid"]
        and item["run_manifest_valid"]
        and item["reconciliation"]["valid"]
        for item in provenance
    )
    contract_validated = bool(
        coverage.get("core_complete")
        and core_paths
        and core_roles_valid
        and all_recognized_valid
        and provenance_valid
    )
    independence_valid = bool(provenance) and all(
        item["independence_status"] == "independent" for item in provenance
    )
    final_audit_accepts_candidate = contract_validated and all(
        item["audit_accepts_final"] for item in provenance
    )
    return {
        "complete_validated": contract_validated and independence_valid,
        "contract_validated": contract_validated,
        "degraded_independence": contract_validated and not independence_valid,
        "final_audit_accepts_candidate": final_audit_accepts_candidate,
        "open_finding_count": sum(
            item["open_finding_count"] or 0 for item in provenance
        ),
        "final_audit_verdicts": [
            item["final_audit_verdict"] for item in provenance
        ],
        "pass_validations": validations,
        "provenance": provenance,
    }


def recompute_run(record: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    run_id = str(record.get("run_id") or "")
    run_doc = _read_json(data_dir / "runs" / f"{run_id}.json") if run_id else {}
    workspace_value = run_doc.get("workspace") or record.get("workspace") or (
        str(data_dir / "runs" / "workspaces" / run_id) if run_id else ""
    )
    workspace = Path(str(workspace_value)) if workspace_value else Path()
    proof = next(
        (
            candidate
            for name in ("proof.md", "proof.tex")
            if (candidate := workspace / name).is_file()
            and not candidate.is_symlink()
            and candidate.resolve().is_relative_to(workspace.resolve())
        ),
        None,
    )
    content = ""
    artifact: dict[str, Any] | None = None
    if proof is not None:
        try:
            content = proof.read_text(encoding="utf-8", errors="replace")
            artifact = {
                "path": str(proof.relative_to(workspace)),
                "bytes": len(content.encode("utf-8")),
                "sha256": _sha256_file(proof),
            }
        except OSError:
            artifact = None

    audit_files = _relative_files(workspace, "audit") if workspace_value else []
    coverage = audit_pass_coverage(audit_files)
    structured = validate_structured_audit(workspace, coverage, proof) if workspace_value else {
        "complete_validated": False,
        "pass_validations": {},
        "provenance": [],
    }
    lowered = content.lower()
    degraded_disclosed = affirmative_degraded_audit(content)
    mode = str(record.get("mode") or "unknown")
    if mode == "baseline":
        # A control that produced treatment-specific ensemble artifacts is not
        # a valid baseline even when its final prose never mentions the skill.
        compliance = "control_contaminated" if audit_files else "control_not_requested"
    elif structured["complete_validated"]:
        compliance = "complete_validated"
    elif structured.get("degraded_independence"):
        compliance = "validated_degraded_independence"
    elif coverage["core_complete"] and any(
        item.get("run_manifest_valid")
        and not item.get("reconciliation", {}).get("valid")
        for item in structured.get("provenance") or []
    ):
        compliance = "unreconciled"
    elif coverage["core_complete"]:
        compliance = "invalid_structured"
    elif degraded_disclosed:
        compliance = "degraded_disclosed"
    elif audit_files or "audit" in lowered:
        compliance = "incomplete"
    else:
        compliance = "not_executed"

    failures = [
        line.strip()
        for line in content.splitlines()
        if TOOL_FAILURE_RE.search(line)
    ][:20]
    outcomes = extract_outcomes(content)
    live = run_doc.get("live") if isinstance(run_doc.get("live"), dict) else {}
    totals = run_doc.get("totals") if isinstance(run_doc.get("totals"), dict) else {}
    status = str(run_doc.get("status") or record.get("status") or "unknown")
    error = run_doc.get("error") or record.get("error")
    return {
        "run_id": run_id,
        "job_id": record.get("job_id"),
        "engine": record.get("engine"),
        "target": record.get("target"),
        "mode": mode,
        "status": status,
        "error": error,
        "submitted_at": record.get("submitted_at"),
        "collected_at": record.get("collected_at"),
        "workspace": str(workspace) if workspace_value else None,
        "artifact": artifact,
        "outcomes": outcomes,
        "outcome_contract_valid": len(outcomes) == 1,
        "manual_review_required": any(item in HIGH_STAKES_OUTCOMES for item in outcomes),
        "skill_package_materialized": (
            workspace / "_agent" / "skills" / "research-proof-audit" / "SKILL.md"
        ).is_file() if workspace_value else False,
        "audit_files": audit_files,
        "audit_pass_coverage": coverage,
        "structured_audit_validation": structured,
        "audit_compliance": compliance,
        "degraded_disclosed": degraded_disclosed,
        "tool_failure_mentions": failures,
        "tool_calls": live.get("tool_calls"),
        "input_tokens": totals.get("input_tokens"),
        "output_tokens": totals.get("output_tokens"),
        "latency_s": totals.get("latency_s"),
        "cost_usd": totals.get("cost_usd"),
        "sidecars": record.get("sidecars"),
    }


def build_snapshot(state_paths: list[Path], data_dir: Path) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    seen: set[str] = set()
    for state_path in state_paths:
        state = _read_json(state_path)
        states.append(
            {
                "path": str(state_path),
                "started_at": state.get("started_at"),
                "deadline_at": state.get("deadline_at"),
                "stopped_at": state.get("stopped_at"),
                "config": state.get("config"),
            }
        )
        for record in state.get("runs") or []:
            run_id = str(record.get("run_id") or "")
            identity = run_id or json.dumps(record, sort_keys=True)
            if identity in seen:
                continue
            seen.add(identity)
            item = recompute_run(record, data_dir)
            item["source_state"] = str(state_path)
            runs.append(item)

    status_counts = Counter(str(run["status"]) for run in runs)
    outcome_counts = Counter(
        outcome for run in runs for outcome in (run.get("outcomes") or [])
    )
    compliance_counts = Counter(str(run["audit_compliance"]) for run in runs)
    by_engine: dict[str, dict[str, Any]] = {}
    for engine in sorted({str(run.get("engine") or "unknown") for run in runs}):
        selected = [run for run in runs if str(run.get("engine") or "unknown") == engine]
        by_engine[engine] = {
            "runs": len(selected),
            "statuses": dict(Counter(str(run["status"]) for run in selected)),
            "outcomes": dict(Counter(o for run in selected for o in run["outcomes"])),
            "audit_compliance": dict(Counter(str(run["audit_compliance"]) for run in selected)),
            "usage": _usage_summary(selected),
        }

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        groups[(str(run.get("engine")), str(run.get("target")))].append(run)
    matched = []
    for (engine, target), selected in sorted(groups.items()):
        audit = [run for run in selected if run["mode"] == "audit"]
        baseline = [run for run in selected if run["mode"] == "baseline"]
        clean_baseline = [
            run
            for run in baseline
            if run["audit_compliance"] == "control_not_requested"
        ]
        if audit and baseline:
            matched.append(
                {
                    "engine": engine,
                    "target": target,
                    "audit_runs": len(audit),
                    "baseline_runs": len(baseline),
                    "audit_finished": sum(run["status"] == "finished" for run in audit),
                    "baseline_finished": sum(run["status"] == "finished" for run in baseline),
                    "baseline_uncontaminated": sum(
                        run["audit_compliance"] == "control_not_requested"
                        for run in baseline
                    ),
                    "baseline_contaminated": sum(
                        run["audit_compliance"] == "control_contaminated"
                        for run in baseline
                    ),
                    "audit_complete_validated": sum(
                        run["audit_compliance"] == "complete_validated"
                        for run in audit
                    ),
                    "audit_outcomes": dict(
                        Counter(outcome for run in audit for outcome in run["outcomes"])
                    ),
                    "baseline_outcomes": dict(
                        Counter(
                            outcome
                            for run in clean_baseline
                            for outcome in run["outcomes"]
                        )
                    ),
                    "baseline_all_outcomes": dict(
                        Counter(outcome for run in baseline for outcome in run["outcomes"])
                    ),
                    "audit_usage": _usage_summary(audit),
                    "baseline_usage": _usage_summary(clean_baseline),
                }
            )

    alerts = []
    for run in runs:
        if run["status"] == "failed":
            alerts.append({"run_id": run["run_id"], "kind": "engine_failed", "detail": run["error"]})
        if run["audit_compliance"] == "control_contaminated":
            alerts.append(
                {
                    "run_id": run["run_id"],
                    "kind": "control_contaminated",
                    "detail": run["audit_files"],
                }
            )
        if not run["outcome_contract_valid"] and run["status"] in {"finished", "failed"}:
            alerts.append({"run_id": run["run_id"], "kind": "outcome_contract", "detail": run["outcomes"]})
        if run["mode"] == "audit" and run["audit_compliance"] in {
            "incomplete",
            "invalid_structured",
            "unreconciled",
            "not_executed",
        }:
            alert_kind = (
                "audit_unreconciled"
                if run["audit_compliance"] == "unreconciled"
                else "audit_incomplete"
            )
            detail = (
                run["structured_audit_validation"]["provenance"]
                if alert_kind == "audit_unreconciled"
                else run["audit_pass_coverage"]
            )
            alerts.append({"run_id": run["run_id"], "kind": alert_kind, "detail": detail})
        if run["manual_review_required"]:
            alerts.append({"run_id": run["run_id"], "kind": "high_stakes_outcome", "detail": run["outcomes"]})

    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "states": states,
        "summary": {
            "runs": len(runs),
            "statuses": dict(status_counts),
            "outcomes": dict(outcome_counts),
            "audit_compliance": dict(compliance_counts),
            "manual_review_queue": sum(run["manual_review_required"] for run in runs),
            "usage": _usage_summary(runs),
        },
        "by_engine": by_engine,
        "matched_groups": matched,
        "alerts": alerts,
        "runs": runs,
    }


def write_markdown(path: Path, snapshot: dict[str, Any]) -> None:
    summary = snapshot["summary"]
    lines = [
        "# Research proof audit evaluation snapshot",
        "",
        f"Generated: {snapshot['generated_at']}",
        f"Runs: {summary['runs']}",
        f"Statuses: `{json.dumps(summary['statuses'], sort_keys=True)}`",
        f"Outcomes: `{json.dumps(summary['outcomes'], sort_keys=True)}`",
        f"Audit compliance: `{json.dumps(summary['audit_compliance'], sort_keys=True)}`",
        f"High-stakes manual-review queue: {summary['manual_review_queue']}",
        f"Usage: `{json.dumps(summary['usage'], sort_keys=True)}`",
        "",
        "## Engines",
        "",
        "| Engine | Runs | Statuses | Outcomes | Audit compliance | Usage |",
        "|---|---:|---|---|---|---|",
    ]
    for engine, data in snapshot["by_engine"].items():
        lines.append(
            f"| {engine} | {data['runs']} | `{json.dumps(data['statuses'], sort_keys=True)}` "
            f"| `{json.dumps(data['outcomes'], sort_keys=True)}` "
            f"| `{json.dumps(data['audit_compliance'], sort_keys=True)}` "
            f"| `{json.dumps(data['usage'], sort_keys=True)}` |"
        )
    lines.extend(["", "## Alerts", ""])
    if snapshot["alerts"]:
        for alert in snapshot["alerts"]:
            lines.append(
                f"- `{alert['kind']}` — `{alert['run_id']}` — "
                f"`{json.dumps(alert['detail'], sort_keys=True)}`"
            )
    else:
        lines.append("- None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", action="append", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("/home/repo/agent_monitor_math/data"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    snapshot = build_snapshot(args.state, args.data_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(args.out, snapshot)
    write_markdown(args.out.with_suffix(".md"), snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
