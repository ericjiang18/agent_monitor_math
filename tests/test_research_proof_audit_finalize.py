from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evals.finalize_research_proof_audit import (
    _merge_map_matches_reruns,
    affirmative_degraded_audit,
    build_snapshot,
)


FINALIZER = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "finalize_research_proof_audit.py"
)


def valid_verifier() -> dict:
    return {
        "verdict": "accept",
        "summary": "The declared scope was checked.",
        "findings": [],
        "coverage_notes": {
            "reviewed_regions": ["complete candidate"],
            "unreviewed_or_difficult_regions": [],
            "external_checks_not_performed": ["none requested"],
        },
    }


def write_valid_audit_contract(
    audit: Path,
    *,
    include_run: bool = True,
    repaired: bool = False,
) -> None:
    workspace = audit.parents[1]
    proof = next(workspace / name for name in ("proof.md", "proof.tex") if (workspace / name).is_file())
    proof_bytes = proof.read_bytes()
    proof_sha = hashlib.sha256(proof_bytes).hexdigest()
    candidate_name = f"final-candidate{proof.suffix}"
    (audit / candidate_name).write_bytes(proof_bytes)
    global_name = "global-rerun.json" if repaired else "global.json"
    decomposed_name = "decomposed-rerun.json" if repaired else "decomposed.json"
    for name in (global_name, decomposed_name, "verifier-output.json"):
        (audit / name).write_text(
            json.dumps(valid_verifier()),
            encoding="utf-8",
        )
    (audit / "merge-map.json").write_text(
        json.dumps(
            {
                "schema_version": "ensemble-paper-audit-app.merge-map.v1",
                "findings": [],
                "counts": {"global_only": 0, "decomposed_only": 0, "both": 0},
            }
        ),
        encoding="utf-8",
    )
    if include_run:
        reconciliation = {
            "schema_version": "research-proof-audit.reconciliation.v1",
            "status": "repaired_revalidated" if repaired else "audited_unchanged",
            "final_artifact": {"path": proof.name, "sha256": proof_sha},
            "audited_candidate": {"path": candidate_name, "sha256": proof_sha},
            "reruns": {
                "global": global_name,
                "decomposed": decomposed_name,
                "merged": "verifier-output.json",
            },
            "superseded_findings": ["F1"] if repaired else [],
        }
        (audit / "run.json").write_text(
            json.dumps(
                {
                    "run_id": "sample",
                    "independence": "isolated",
                    "degraded_independence": False,
                    "candidate_sha256": ("0" * 64) if repaired else proof_sha,
                    "repair_history": ([{"finding_id": "F1"}] if repaired else []),
                    "reconciliation": reconciliation,
                }
            ),
            encoding="utf-8",
        )


class ResearchProofAuditFinalizerTests(unittest.TestCase):
    def test_valid_contract_with_degraded_independence_is_not_full_ensemble(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            run_id = "degraded_independence"
            workspace = data / "runs" / "workspaces" / run_id
            audit = workspace / "audit" / "sample"
            audit.mkdir(parents=True)
            (workspace / "proof.md").write_text(
                "Checked.\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            write_valid_audit_contract(audit)
            manifest_path = audit / "run.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("independence")
            manifest["degraded_independence"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (data / "runs" / f"{run_id}.json").write_text(
                json.dumps({"status": "finished", "workspace": str(workspace)}),
                encoding="utf-8",
            )
            state = root / "state.json"
            state.write_text(
                json.dumps({"runs": [{"run_id": run_id, "mode": "audit"}]}),
                encoding="utf-8",
            )
            snapshot = build_snapshot([state], data)

        run = snapshot["runs"][0]
        self.assertEqual(
            run["audit_compliance"], "validated_degraded_independence"
        )
        self.assertTrue(run["structured_audit_validation"]["contract_validated"])
        self.assertFalse(run["structured_audit_validation"]["complete_validated"])

    def test_merge_map_sources_must_exist_in_exact_reruns(self) -> None:
        merge_map = {
            "findings": [
                {
                    "finding_id": "F1",
                    "sources": [
                        {"pass": "global", "finding_id": "G-missing"},
                        {"pass": "decomposed", "finding_id": "D1"},
                    ],
                }
            ]
        }
        self.assertFalse(
            _merge_map_matches_reruns(
                merge_map,
                global_document={"findings": [{"finding_id": "G1"}]},
                decomposed_document={"findings": [{"finding_id": "D1"}]},
                merged_document={"findings": [{"finding_id": "F1"}]},
            )
        )

    def test_degraded_audit_detection_requires_affirmative_disclosure(self) -> None:
        self.assertTrue(
            affirmative_degraded_audit(
                "Therefore, `degraded audit: files unavailable`."
            )
        )
        self.assertFalse(
            affirmative_degraded_audit(
                "There is no `degraded audit:` gap in the file-backed contract."
            )
        )
        self.assertFalse(
            affirmative_degraded_audit(
                "This is not a `degraded audit:` all files validated."
            )
        )

    def test_direct_cli_resolves_project_package_outside_repo_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(FINALIZER), "--help"],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--state", result.stdout)

    def test_recomputes_tex_outcome_and_core_audit_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            run_id = "improof_sample"
            workspace = data / "runs" / "workspaces" / run_id
            audit = workspace / "audit" / "sample"
            audit.mkdir(parents=True)
            (workspace / "proof.tex").write_text(
                "Result.\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            write_valid_audit_contract(audit)
            runs_dir = data / "runs"
            (runs_dir / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "status": "finished",
                        "workspace": str(workspace),
                        "live": {"tool_calls": 12},
                        "totals": {
                            "latency_s": 4.5,
                            "cost_usd": 1.25,
                            "input_tokens": 1200,
                            "output_tokens": 80,
                        },
                    }
                ),
                encoding="utf-8",
            )
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "runs": [
                            {
                                "run_id": run_id,
                                "engine": "improof",
                                "target": "erdos944",
                                "mode": "audit",
                                "status": "finished",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            snapshot = build_snapshot([state], data)
        run = snapshot["runs"][0]
        self.assertEqual(run["artifact"]["path"], "proof.tex")
        self.assertEqual(run["outcomes"], ["Partial Progress"])
        self.assertEqual(run["audit_compliance"], "complete_validated")
        self.assertTrue(
            run["structured_audit_validation"]["complete_validated"]
        )
        self.assertEqual(run["tool_calls"], 12)
        self.assertEqual(run["input_tokens"], 1200)
        self.assertEqual(run["output_tokens"], 80)
        self.assertEqual(
            snapshot["summary"]["usage"],
            {
                "runs": 1,
                "runs_with_token_usage": 1,
                "runs_with_cost": 1,
                "input_tokens": 1200,
                "output_tokens": 80,
                "cost_usd": 1.25,
                "latency_s": 4.5,
            },
        )
        self.assertEqual(
            snapshot["by_engine"]["improof"]["usage"]["cost_usd"], 1.25
        )

    def test_explicit_workspace_in_state_includes_direct_runner_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            workspace = root / "direct-improof-regression"
            audit = workspace / "audit" / "sample"
            audit.mkdir(parents=True)
            (workspace / "proof.md").write_text(
                "# Direct regression\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            write_valid_audit_contract(audit)
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "runs": [
                            {
                                "run_id": "direct-improof-regression",
                                "engine": "improof",
                                "target": "erdos944_order11",
                                "mode": "audit",
                                "status": "finished",
                                "workspace": str(workspace),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            snapshot = build_snapshot([state], data)

        run = snapshot["runs"][0]
        self.assertEqual(run["workspace"], str(workspace))
        self.assertEqual(run["outcomes"], ["Partial Progress"])
        self.assertEqual(run["audit_compliance"], "complete_validated")

    def test_core_filenames_without_run_manifest_are_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            run_id = "missing_manifest"
            workspace = data / "runs" / "workspaces" / run_id
            audit = workspace / "audit" / "sample"
            audit.mkdir(parents=True)
            (workspace / "proof.md").write_text(
                "ProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            write_valid_audit_contract(audit, include_run=False)
            (data / "runs" / f"{run_id}.json").write_text(
                json.dumps({"status": "finished", "workspace": str(workspace)}),
                encoding="utf-8",
            )
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "runs": [
                            {
                                "run_id": run_id,
                                "engine": "openclaude",
                                "target": "erdos944",
                                "mode": "audit",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            snapshot = build_snapshot([state], data)
        run = snapshot["runs"][0]
        self.assertEqual(run["audit_compliance"], "invalid_structured")
        self.assertFalse(
            run["structured_audit_validation"]["complete_validated"]
        )
        self.assertFalse(
            run["structured_audit_validation"]["provenance"][0][
                "run_manifest_present"
            ]
        )

    def test_final_proof_change_without_reaudit_is_unreconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            run_id = "stale_final"
            workspace = data / "runs" / "workspaces" / run_id
            audit = workspace / "audit" / "sample"
            audit.mkdir(parents=True)
            proof = workspace / "proof.md"
            proof.write_text(
                "Initial.\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            write_valid_audit_contract(audit)
            proof.write_text(
                "Repaired after audit.\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            (data / "runs" / f"{run_id}.json").write_text(
                json.dumps({"status": "finished", "workspace": str(workspace)}),
                encoding="utf-8",
            )
            state = root / "state.json"
            state.write_text(
                json.dumps({"runs": [{"run_id": run_id, "mode": "audit"}]}),
                encoding="utf-8",
            )
            snapshot = build_snapshot([state], data)

        run = snapshot["runs"][0]
        self.assertEqual(run["audit_compliance"], "unreconciled")
        reconciliation = run["structured_audit_validation"]["provenance"][0]["reconciliation"]
        self.assertFalse(reconciliation["valid"])
        self.assertTrue(
            any("current proof" in error or "byte-identical" in error for error in reconciliation["errors"])
        )
        self.assertTrue(any(alert["kind"] == "audit_unreconciled" for alert in snapshot["alerts"]))

    def test_repaired_candidate_requires_and_accepts_explicit_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            run_id = "repaired_revalidated"
            workspace = data / "runs" / "workspaces" / run_id
            audit = workspace / "audit" / "sample"
            audit.mkdir(parents=True)
            (workspace / "proof.md").write_text(
                "Repaired.\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            write_valid_audit_contract(audit, repaired=True)
            (data / "runs" / f"{run_id}.json").write_text(
                json.dumps({"status": "finished", "workspace": str(workspace)}),
                encoding="utf-8",
            )
            state = root / "state.json"
            state.write_text(
                json.dumps({"runs": [{"run_id": run_id, "mode": "audit"}]}),
                encoding="utf-8",
            )
            snapshot = build_snapshot([state], data)

        run = snapshot["runs"][0]
        self.assertEqual(run["audit_compliance"], "complete_validated")
        reconciliation = run["structured_audit_validation"]["provenance"][0]["reconciliation"]
        self.assertTrue(reconciliation["valid"])
        self.assertEqual(reconciliation["status"], "repaired_revalidated")

    def test_high_stakes_reconciliation_requires_refuter_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            run_id = "high_stakes_without_refuter"
            workspace = data / "runs" / "workspaces" / run_id
            audit = workspace / "audit" / "sample"
            audit.mkdir(parents=True)
            (workspace / "proof.md").write_text(
                "Claimed solution.\n\nProvingConsole outcome: Solved\n",
                encoding="utf-8",
            )
            write_valid_audit_contract(audit)
            (data / "runs" / f"{run_id}.json").write_text(
                json.dumps({"status": "finished", "workspace": str(workspace)}),
                encoding="utf-8",
            )
            state = root / "state.json"
            state.write_text(
                json.dumps({"runs": [{"run_id": run_id, "mode": "audit"}]}),
                encoding="utf-8",
            )
            snapshot = build_snapshot([state], data)

        run = snapshot["runs"][0]
        self.assertEqual(run["audit_compliance"], "unreconciled")
        errors = run["structured_audit_validation"]["provenance"][0][
            "reconciliation"
        ]["errors"]
        self.assertIn(
            "a high-stakes final outcome requires dependent_reruns.refuter",
            errors,
        )

    def test_symlinked_audited_candidate_is_unreconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            run_id = "symlink_candidate"
            workspace = data / "runs" / "workspaces" / run_id
            audit = workspace / "audit" / "sample"
            audit.mkdir(parents=True)
            (workspace / "proof.md").write_text(
                "Candidate.\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            write_valid_audit_contract(audit)
            candidate = audit / "final-candidate.md"
            candidate.unlink()
            candidate.symlink_to(workspace / "proof.md")
            (data / "runs" / f"{run_id}.json").write_text(
                json.dumps({"status": "finished", "workspace": str(workspace)}),
                encoding="utf-8",
            )
            state = root / "state.json"
            state.write_text(
                json.dumps({"runs": [{"run_id": run_id, "mode": "audit"}]}),
                encoding="utf-8",
            )
            snapshot = build_snapshot([state], data)

        run = snapshot["runs"][0]
        self.assertEqual(run["audit_compliance"], "unreconciled")
        errors = run["structured_audit_validation"]["provenance"][0]["reconciliation"]["errors"]
        self.assertIn("audited_candidate.path is unsafe or unavailable", errors)

    def test_baseline_is_not_penalized_for_missing_audit_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            run_id = "plain_control"
            workspace = data / "runs" / "workspaces" / run_id
            workspace.mkdir(parents=True)
            (workspace / "proof.md").write_text(
                "# Result\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            (data / "runs" / f"{run_id}.json").write_text(
                json.dumps({"status": "finished", "workspace": str(workspace)}),
                encoding="utf-8",
            )
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "runs": [
                            {
                                "run_id": run_id,
                                "engine": "plain",
                                "target": "erdos944",
                                "mode": "baseline",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            snapshot = build_snapshot([state], data)
        self.assertEqual(
            snapshot["runs"][0]["audit_compliance"], "control_not_requested"
        )
        self.assertFalse(snapshot["alerts"])

    def test_baseline_with_audit_artifacts_is_marked_contaminated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            run_id = "contaminated_control"
            workspace = data / "runs" / "workspaces" / run_id
            audit = workspace / "audit" / "unexpected"
            audit.mkdir(parents=True)
            (audit / "global.json").write_text(
                json.dumps(valid_verifier()), encoding="utf-8"
            )
            (workspace / "proof.md").write_text(
                "ProvingConsole outcome: Partial Progress\n", encoding="utf-8"
            )
            (data / "runs" / f"{run_id}.json").write_text(
                json.dumps({"status": "finished", "workspace": str(workspace)}),
                encoding="utf-8",
            )
            treatment_id = "paired_treatment"
            treatment_workspace = data / "runs" / "workspaces" / treatment_id
            treatment_workspace.mkdir(parents=True)
            (treatment_workspace / "proof.md").write_text(
                "ProvingConsole outcome: Known/Open Status\n", encoding="utf-8"
            )
            (data / "runs" / f"{treatment_id}.json").write_text(
                json.dumps(
                    {"status": "finished", "workspace": str(treatment_workspace)}
                ),
                encoding="utf-8",
            )
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "runs": [
                            {
                                "run_id": run_id,
                                "engine": "deepseek_harness",
                                "target": "erdos413",
                                "mode": "baseline",
                                "status": "finished",
                            },
                            {
                                "run_id": treatment_id,
                                "engine": "deepseek_harness",
                                "target": "erdos413",
                                "mode": "audit",
                                "status": "finished",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            snapshot = build_snapshot([state], data)
        run = snapshot["runs"][0]
        self.assertEqual(run["audit_compliance"], "control_contaminated")
        self.assertTrue(
            any(alert["kind"] == "control_contaminated" for alert in snapshot["alerts"])
        )
        matched = snapshot["matched_groups"][0]
        self.assertEqual(matched["baseline_outcomes"], {})
        self.assertEqual(
            matched["baseline_all_outcomes"], {"Partial Progress": 1}
        )
        self.assertEqual(matched["baseline_contaminated"], 1)

    def test_affirmative_degraded_audit_disclosure_is_classified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            runs = []
            for suffix, disclosure in (
                ("colon", "degraded audit: independent files unavailable"),
                ("sentence", "Audit files were unavailable. This is a degraded audit."),
                ("latex", r"\emph{degraded audit:} independent files unavailable"),
                ("inline_code", "Therefore, `degraded audit: files unavailable`."),
                ("negated", "This is not a degraded audit."),
            ):
                run_id = f"meta_{suffix}"
                workspace = data / "runs" / "workspaces" / run_id
                workspace.mkdir(parents=True)
                (workspace / "proof.md").write_text(
                    f"{disclosure}\n\nProvingConsole outcome: Partial Progress\n",
                    encoding="utf-8",
                )
                (data / "runs" / f"{run_id}.json").write_text(
                    json.dumps({"status": "finished", "workspace": str(workspace)}),
                    encoding="utf-8",
                )
                runs.append(
                    {
                        "run_id": run_id,
                        "engine": "metaharness",
                        "target": "erdos944",
                        "mode": "audit",
                    }
                )
            state = root / "state.json"
            state.write_text(json.dumps({"runs": runs}), encoding="utf-8")
            snapshot = build_snapshot([state], data)

        by_id = {run["run_id"]: run for run in snapshot["runs"]}
        self.assertEqual(by_id["meta_colon"]["audit_compliance"], "degraded_disclosed")
        self.assertEqual(by_id["meta_sentence"]["audit_compliance"], "degraded_disclosed")
        self.assertEqual(by_id["meta_latex"]["audit_compliance"], "degraded_disclosed")
        self.assertEqual(
            by_id["meta_inline_code"]["audit_compliance"], "degraded_disclosed"
        )
        self.assertEqual(by_id["meta_negated"]["audit_compliance"], "incomplete")


if __name__ == "__main__":
    unittest.main()
