from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_monitor.research_audit import (
    reconciliation_complete,
    validate_workspace_reconciliation,
    workspace_reconciliation_status,
)


def _valid_output() -> dict:
    return {
        "verdict": "accept",
        "summary": "The exact frozen final candidate was checked.",
        "findings": [],
        "coverage_notes": {
            "reviewed_regions": ["complete candidate"],
            "unreviewed_or_difficult_regions": [],
            "external_checks_not_performed": [],
        },
    }


def _install_contract(workspace: Path) -> Path:
    audit = workspace / "audit" / "sample"
    audit.mkdir(parents=True)
    proof = workspace / "proof.md"
    proof.write_text("Exact audited final candidate.\n", encoding="utf-8")
    digest = hashlib.sha256(proof.read_bytes()).hexdigest()
    (audit / "final-candidate.md").write_bytes(proof.read_bytes())
    for name in ("global.json", "decomposed.json", "verifier-output.json"):
        (audit / name).write_text(json.dumps(_valid_output()), encoding="utf-8")
    (audit / "merge-map.json").write_text(
        json.dumps(
            {
                "schema_version": "ensemble-paper-audit-app.merge-map.v1",
                "findings": [],
                "counts": {
                    "global_only": 0,
                    "decomposed_only": 0,
                    "both": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    (audit / "run.json").write_text(
        json.dumps(
            {
                "candidate_sha256": digest,
                "independence": "fresh_context_serial_passes",
                "degraded_independence": False,
                "repair_history": [],
                "reconciliation": {
                    "schema_version": "research-proof-audit.reconciliation.v1",
                    "status": "audited_unchanged",
                    "final_artifact": {"path": "proof.md", "sha256": digest},
                    "audited_candidate": {
                        "path": "final-candidate.md",
                        "sha256": digest,
                    },
                    "reruns": {
                        "global": "global.json",
                        "decomposed": "decomposed.json",
                        "merged": "verifier-output.json",
                    },
                    "superseded_findings": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return audit


class ResearchAuditRuntimeTests(unittest.TestCase):
    def test_exact_contract_passes_and_post_audit_edit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            audit = _install_contract(workspace)
            certificate = validate_workspace_reconciliation(workspace, audit)
            self.assertTrue(certificate["valid"], certificate)
            self.assertEqual(certificate["returncode"], 0)
            self.assertEqual(certificate["independence_status"], "independent")
            self.assertEqual(certificate["final_audit_verdict"], "accept")
            self.assertEqual(certificate["open_finding_count"], 0)
            self.assertTrue(certificate["audit_accepts_final"])
            self.assertTrue(reconciliation_complete(workspace, audit))
            monitor_status = workspace_reconciliation_status(workspace)
            self.assertEqual(monitor_status["status"], "validated")
            self.assertTrue(monitor_status["audit_accepts_final"])

            (workspace / "proof.md").write_text(
                "Edited after the reviewers finished.\n", encoding="utf-8"
            )
            stale = validate_workspace_reconciliation(workspace, audit)
            self.assertFalse(stale["valid"])
            self.assertEqual(stale["returncode"], 1)
            self.assertFalse(reconciliation_complete(workspace, audit))
            self.assertEqual(
                workspace_reconciliation_status(workspace)["status"],
                "unreconciled",
            )

    def test_valid_contract_discloses_degraded_independence_in_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            audit = _install_contract(workspace)
            manifest_path = audit / "run.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("independence")
            manifest["degraded_independence"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            certificate = validate_workspace_reconciliation(workspace, audit)
            self.assertTrue(certificate["valid"], certificate)
            self.assertEqual(certificate["independence_status"], "degraded")
            status = workspace_reconciliation_status(workspace)
            self.assertTrue(status["valid"])
            self.assertEqual(status["status"], "validated_degraded_independence")

    def test_symbolic_linked_audit_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (workspace / "audit").mkdir()
            linked = workspace / "audit" / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            result = validate_workspace_reconciliation(workspace, linked)
            self.assertFalse(result["valid"])
            self.assertIn("regular directory", result["error"])

    def test_merge_provenance_is_required_and_counts_are_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            audit = _install_contract(workspace)
            merge_map = audit / "merge-map.json"
            merge_map.unlink()
            missing = validate_workspace_reconciliation(workspace, audit)
            self.assertFalse(missing["valid"])
            self.assertIn(
                "merge-map.json is missing, unsafe, empty, or oversized",
                missing["errors"],
            )

            merge_map.write_text(
                json.dumps(
                    {
                        "schema_version": "ensemble-paper-audit-app.merge-map.v1",
                        "findings": [],
                        "counts": {
                            "global_only": 1,
                            "decomposed_only": 0,
                            "both": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            inconsistent = validate_workspace_reconciliation(workspace, audit)
            self.assertFalse(inconsistent["valid"])
            self.assertIn(
                "merge-map counts do not match the source classifications",
                inconsistent["errors"],
            )

    def test_symbolic_linked_manifest_and_oversized_manifest_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            audit = _install_contract(workspace)
            manifest = audit / "run.json"
            outside = Path(tmp) / "outside.json"
            outside.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
            manifest.unlink()
            manifest.symlink_to(outside)
            linked = validate_workspace_reconciliation(workspace, audit)
            self.assertFalse(linked["valid"])
            self.assertIn("run.json is missing, unsafe, empty, or oversized", linked["errors"])

            manifest.unlink()
            manifest.write_bytes(b"{" + b" " * 2_000_000 + b"}")
            oversized = validate_workspace_reconciliation(workspace, audit)
            self.assertFalse(oversized["valid"])
            self.assertIn(
                "run.json is missing, unsafe, empty, or oversized",
                oversized["errors"],
            )

    def test_high_stakes_outcome_requires_valid_exact_final_refuter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            audit = _install_contract(workspace)
            proof = workspace / "proof.md"
            proof.write_text(
                "Claimed solution.\n\nProvingConsole outcome: Solved\n",
                encoding="utf-8",
            )
            digest = hashlib.sha256(proof.read_bytes()).hexdigest()
            (audit / "final-candidate.md").write_bytes(proof.read_bytes())
            manifest = json.loads(
                (audit / "run.json").read_text(encoding="utf-8")
            )
            manifest["candidate_sha256"] = digest
            manifest["reconciliation"]["final_artifact"]["sha256"] = digest
            manifest["reconciliation"]["audited_candidate"]["sha256"] = digest
            (audit / "run.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            missing = validate_workspace_reconciliation(workspace, audit)
            self.assertFalse(missing["valid"])
            self.assertIn(
                "a high-stakes final outcome requires dependent_reruns.refuter",
                missing["errors"],
            )

            (audit / "refuter.json").write_text(
                json.dumps(_valid_output()), encoding="utf-8"
            )
            manifest["reconciliation"]["dependent_reruns"] = {
                "refuter": "refuter.json"
            }
            (audit / "run.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            self.assertTrue(
                validate_workspace_reconciliation(workspace, audit)["valid"]
            )


if __name__ == "__main__":
    unittest.main()
