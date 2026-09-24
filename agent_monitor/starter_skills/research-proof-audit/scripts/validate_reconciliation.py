#!/usr/bin/env python3
"""Fail closed unless the final proof is the exact candidate audited by current passes.

Usage: python3 validate_reconciliation.py AUDIT_RUN_DIR
"""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import sys
from pathlib import Path
from typing import Any


SCHEMA = "research-proof-audit.reconciliation.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 5_000_000
MAX_MANIFEST_BYTES = 2_000_000
MAX_PROOF_BYTES = 10_000_000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_file(
    root: Path,
    relative: object,
    *,
    max_bytes: int = MAX_JSON_BYTES,
) -> Path | None:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None
    unresolved = root / relative
    cursor = root
    for part in Path(relative).parts:
        if part in {"", ".", ".."}:
            return None
        cursor /= part
        if cursor.is_symlink():
            return None
    try:
        path = unresolved.resolve(strict=True)
    except OSError:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if (
        not path.is_relative_to(root.resolve())
        or not path.is_file()
        or size < 1
        or size > max_bytes
    ):
        return None
    return path


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _finding_ids(document: dict[str, Any]) -> set[str]:
    findings = document.get("findings")
    if not isinstance(findings, list):
        return set()
    return {
        finding["finding_id"]
        for finding in findings
        if isinstance(finding, dict)
        and isinstance(finding.get("finding_id"), str)
        and finding["finding_id"]
    }


def _merge_map_errors(
    document: dict[str, Any],
    *,
    global_document: dict[str, Any],
    decomposed_document: dict[str, Any],
    merged_document: dict[str, Any],
) -> list[str]:
    """Validate standard merge provenance against the exact current pass files."""
    errors: list[str] = []
    if set(document) != {"schema_version", "findings", "counts"}:
        errors.append("merge-map.json must contain only schema_version, findings, and counts")
    if document.get("schema_version") != "ensemble-paper-audit-app.merge-map.v1":
        errors.append("merge-map.json has an unsupported schema_version")
    findings = document.get("findings")
    counts = document.get("counts")
    if not isinstance(findings, list) or not isinstance(counts, dict):
        errors.append("merge-map.json must contain findings and counts objects")
        return errors
    count_keys = {"global_only", "decomposed_only", "both"}
    if set(counts) != count_keys or not all(
        type(counts.get(key)) is int and counts[key] >= 0 for key in count_keys
    ):
        errors.append("merge-map.json counts must be the three nonnegative integer categories")
        return errors

    source_ids = {
        "global": _finding_ids(global_document),
        "decomposed": _finding_ids(decomposed_document),
    }
    merged_ids = _finding_ids(merged_document)
    map_ids: set[str] = set()
    observed_counts = {key: 0 for key in count_keys}
    allowed_passes = {
        "global",
        "decomposed",
        "display_sweep",
        "boundary_clause_sweep",
        "refuter",
        "citation",
    }
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"finding_id", "sources"}:
            errors.append("each merge-map finding must contain only finding_id and sources")
            continue
        finding_id = finding.get("finding_id")
        sources = finding.get("sources")
        if not isinstance(finding_id, str) or not finding_id or finding_id in map_ids:
            errors.append("merge-map finding IDs must be unique nonempty strings")
            continue
        map_ids.add(finding_id)
        if not isinstance(sources, list) or not sources:
            errors.append(f"merge-map finding {finding_id} must have nonempty sources")
            continue
        source_pairs: set[tuple[str, str]] = set()
        core_passes: set[str] = set()
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"pass", "finding_id"}:
                errors.append(f"merge-map finding {finding_id} has a malformed source")
                continue
            pass_name = source.get("pass")
            source_id = source.get("finding_id")
            pair = (pass_name, source_id)
            if (
                not isinstance(pass_name, str)
                or pass_name not in allowed_passes
                or not isinstance(source_id, str)
                or not source_id
                or pair in source_pairs
            ):
                errors.append(f"merge-map finding {finding_id} has an invalid or duplicate source")
                continue
            source_pairs.add(pair)
            if pass_name in source_ids:
                core_passes.add(pass_name)
                if source_id not in source_ids[pass_name]:
                    errors.append(
                        f"merge-map source {pass_name}:{source_id} is absent from the current rerun"
                    )
        if core_passes == {"global", "decomposed"}:
            observed_counts["both"] += 1
        elif core_passes == {"global"}:
            observed_counts["global_only"] += 1
        elif core_passes == {"decomposed"}:
            observed_counts["decomposed_only"] += 1
        else:
            errors.append(f"merge-map finding {finding_id} has no global/decomposed provenance")
    if map_ids != merged_ids:
        errors.append("merge-map finding IDs do not exactly match verifier-output.json")
    if counts != observed_counts:
        errors.append("merge-map counts do not match the source classifications")
    return errors


def validate(audit_dir: Path) -> dict[str, Any]:
    workspace = Path.cwd().resolve()
    if audit_dir.is_absolute() or any(part in {"", ".", ".."} for part in audit_dir.parts):
        raise ValueError("AUDIT_RUN_DIR must be a workspace-relative path without traversal")
    unresolved_dir = Path.cwd() / audit_dir
    cursor = Path.cwd()
    for part in audit_dir.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("AUDIT_RUN_DIR must not traverse symbolic links")
    try:
        root = unresolved_dir.resolve(strict=True)
    except OSError as exc:
        raise ValueError("AUDIT_RUN_DIR does not exist") from exc
    if not root.is_relative_to(workspace) or not root.is_dir():
        raise ValueError("AUDIT_RUN_DIR must stay inside the workspace")

    errors: list[str] = []
    observed_files: dict[str, tuple[Path, str]] = {}
    manifest_path = safe_file(root, "run.json", max_bytes=MAX_MANIFEST_BYTES)
    if manifest_path is None:
        return {
            "status": "unreconciled",
            "valid": False,
            "errors": ["run.json is missing, unsafe, empty, or oversized"],
        }
    manifest = load_json(manifest_path)
    observed_files["run"] = (manifest_path, sha256_file(manifest_path))
    independence = manifest.get("independence")
    degraded_independence = manifest.get("degraded_independence")
    if degraded_independence is False and isinstance(independence, str) and independence:
        independence_status = "independent"
    elif degraded_independence is True:
        independence_status = "degraded"
    else:
        independence_status = "unreported"
    reconciliation = manifest.get("reconciliation")
    if not isinstance(reconciliation, dict):
        return {"status": "unreconciled", "valid": False, "errors": ["run.json is missing reconciliation"]}
    if reconciliation.get("schema_version") != SCHEMA:
        errors.append("unsupported reconciliation schema_version")
    status = reconciliation.get("status")
    if status not in {"audited_unchanged", "repaired_revalidated"}:
        errors.append("invalid reconciliation status")

    final = reconciliation.get("final_artifact")
    final_sha: str | None = None
    final_high_stakes = False
    if not isinstance(final, dict):
        errors.append("final_artifact is missing")
    else:
        final_path = safe_file(
            workspace, final.get("path"), max_bytes=MAX_PROOF_BYTES
        )
        claimed = final.get("sha256")
        if final_path is None or final_path.parent != workspace:
            errors.append("final_artifact.path must identify a root workspace file")
        elif not isinstance(claimed, str) or not SHA256_RE.fullmatch(claimed):
            errors.append("final_artifact.sha256 is malformed")
        else:
            final_sha = sha256_file(final_path)
            observed_files["final"] = (final_path, final_sha)
            try:
                final_text = final_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                final_high_stakes = bool(
                    re.search(
                        r"(?im)^\s*ProvingConsole outcome:\s*"
                        r"(?:Solved|Counterexample)\s*$",
                        final_text,
                    )
                )
            except OSError:
                errors.append("final_artifact could not be read for outcome validation")
            if final_sha != claimed:
                errors.append("final_artifact.sha256 does not match the current proof")

    candidate = reconciliation.get("audited_candidate")
    audited_sha: str | None = None
    if not isinstance(candidate, dict):
        errors.append("audited_candidate is missing")
    else:
        candidate_path = safe_file(
            root, candidate.get("path"), max_bytes=MAX_PROOF_BYTES
        )
        claimed = candidate.get("sha256")
        if candidate_path is None:
            errors.append("audited_candidate.path is unsafe or unavailable")
        elif not isinstance(claimed, str) or not SHA256_RE.fullmatch(claimed):
            errors.append("audited_candidate.sha256 is malformed")
        else:
            audited_sha = sha256_file(candidate_path)
            observed_files["candidate"] = (candidate_path, audited_sha)
            if audited_sha != claimed:
                errors.append("audited_candidate.sha256 does not match its file")
            if final_sha is not None and audited_sha != final_sha:
                errors.append("the audited candidate is not byte-identical to the final proof")

    initial_sha = manifest.get("candidate_sha256")
    if not isinstance(initial_sha, str) or not SHA256_RE.fullmatch(initial_sha):
        errors.append("candidate_sha256 is missing or malformed")
    elif status == "audited_unchanged" and final_sha is not None and initial_sha != final_sha:
        errors.append("audited_unchanged does not match the initial frozen candidate")

    validator_path = Path(__file__).with_name("validate_output.py")
    validator = runpy.run_path(str(validator_path)).get("validate_document")
    if not callable(validator):
        raise ValueError("locked output validator is unavailable")
    reruns = reconciliation.get("reruns")
    if not isinstance(reruns, dict):
        errors.append("reruns must map global, decomposed, and merged files")
        reruns = {}
    checked: dict[str, str] = {}
    rerun_documents: dict[str, dict[str, Any]] = {}
    for role in ("global", "decomposed", "merged"):
        path = safe_file(root, reruns.get(role))
        if path is None:
            errors.append(f"reruns.{role} is unsafe or unavailable")
            continue
        normalized = path.name.lower().replace("_", "-")
        if role == "global" and not normalized.startswith("global"):
            errors.append("reruns.global is not a global pass file")
        if role == "decomposed" and not normalized.startswith("decomposed"):
            errors.append("reruns.decomposed is not a decomposed pass file")
        if role == "merged" and path.name != "verifier-output.json":
            errors.append("reruns.merged must be verifier-output.json")
        if status == "repaired_revalidated" and role in {"global", "decomposed"} and "rerun" not in normalized:
            errors.append(f"repaired_revalidated requires an explicit {role} rerun file")
        document = load_json(path)
        result = validator(document)
        if not result.get("valid"):
            errors.append(f"reruns.{role} failed the locked output validator")
        checked[role] = path.name
        rerun_documents[role] = document
        observed_files[role] = (path, sha256_file(path))

    merge_map_path = safe_file(root, "merge-map.json")
    if merge_map_path is None:
        errors.append("merge-map.json is missing, unsafe, empty, or oversized")
    elif all(role in rerun_documents for role in ("global", "decomposed", "merged")):
        merge_map = load_json(merge_map_path)
        errors.extend(
            _merge_map_errors(
                merge_map,
                global_document=rerun_documents["global"],
                decomposed_document=rerun_documents["decomposed"],
                merged_document=rerun_documents["merged"],
            )
        )
        observed_files["merge_map"] = (
            merge_map_path,
            sha256_file(merge_map_path),
        )

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
        refuter_path = safe_file(root, refuter_relative)
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
            result = validator(load_json(refuter_path))
            if not result.get("valid"):
                errors.append(
                    "dependent_reruns.refuter failed the locked output validator"
                )
            checked["refuter"] = refuter_path.name
            observed_files["refuter"] = (
                refuter_path,
                sha256_file(refuter_path),
            )

    superseded = reconciliation.get("superseded_findings")
    if not isinstance(superseded, list) or not all(isinstance(item, str) and item for item in superseded):
        errors.append("superseded_findings must be a list of nonempty finding IDs")
    if status == "repaired_revalidated":
        history = manifest.get("repair_history") or manifest.get("repairs")
        if not isinstance(history, (list, dict)) or not history:
            errors.append("repaired_revalidated requires nonempty repair history")

    for label, (path, digest) in observed_files.items():
        try:
            if sha256_file(path) != digest:
                errors.append(f"{label} changed while reconciliation was being validated")
        except OSError:
            errors.append(f"{label} disappeared while reconciliation was being validated")

    merged_document = rerun_documents.get("merged")
    final_audit_verdict = (
        merged_document.get("verdict")
        if isinstance(merged_document, dict)
        and isinstance(merged_document.get("verdict"), str)
        else None
    )
    merged_findings = (
        merged_document.get("findings")
        if isinstance(merged_document, dict)
        and isinstance(merged_document.get("findings"), list)
        else None
    )
    open_finding_count = (
        len(merged_findings) if isinstance(merged_findings, list) else None
    )
    result = {
        "status": "validated" if not errors else "unreconciled",
        "valid": not errors,
        "errors": errors,
        "reconciliation_status": status,
        "independence_status": independence_status,
        "final_sha256": final_sha,
        "audited_candidate_sha256": audited_sha,
        "final_audit_verdict": final_audit_verdict,
        "open_finding_count": open_finding_count,
        "audit_accepts_final": bool(
            not errors
            and final_audit_verdict == "accept"
            and open_finding_count == 0
        ),
        "validated_pass_files": checked,
        "validated_artifact_sha256": {
            label: digest for label, (_path, digest) in sorted(observed_files.items())
        },
    }
    result["certificate_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] in {"-h", "--help"}:
        print(__doc__.strip())
        return 0 if len(sys.argv) == 2 else 2
    try:
        result = validate(Path(sys.argv[1]))
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "valid": False, "error": str(exc)}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
