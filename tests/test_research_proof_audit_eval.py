from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evals.research_proof_audit_24h import (
    append_audit_suffix,
    audit_pass_coverage,
    atomic_json,
    collect_artifact_evidence,
    extract_outcomes,
    load_targets,
    make_schedule,
    persisted_terminal_run,
    sidecars_settled,
)


ROOT = Path(__file__).parents[1]
TARGETS = ROOT / "evals" / "research_audit_targets.json"


class ResearchProofAuditEvaluatorTests(unittest.TestCase):
    def test_target_contract_and_schedule_prioritize_each_harness(self) -> None:
        targets, suffixes = load_targets(TARGETS)
        self.assertEqual({item["id"] for item in targets}, {"erdos944", "erdos413", "erdos196", "erdos886"})
        self.assertIn("research-proof-audit", suffixes["audit"])
        engines = ["codex", "deepagents", "plain"]
        schedule = make_schedule(engines, targets, seed=7)
        self.assertEqual([item["engine"] for item in schedule[:3]], engines)
        self.assertTrue(all(item["mode"] == "audit" for item in schedule[:3]))
        pairs = {(item["engine"], item["target"], item["mode"]) for item in schedule[3:]}
        self.assertIn(("codex", "erdos944", "audit"), pairs)
        self.assertIn(("codex", "erdos944", "baseline"), pairs)

    def test_schedule_can_prioritize_a_baseline_regression(self) -> None:
        targets, _suffixes = load_targets(TARGETS)
        schedule = make_schedule(
            ["deepagents"], targets, seed=7, first_mode="baseline"
        )
        self.assertEqual(
            schedule[0],
            {"engine": "deepagents", "target": "erdos944", "mode": "baseline"},
        )

    def test_schedule_rejects_unknown_first_mode(self) -> None:
        targets, _suffixes = load_targets(TARGETS)
        with self.assertRaisesRegex(ValueError, "first_mode"):
            make_schedule(["codex"], targets, seed=7, first_mode="unknown")

    def test_audit_prompt_delta_does_not_contaminate_baseline(self) -> None:
        original = {"audit": "AUDIT", "baseline": "CONTROL"}
        composed = append_audit_suffix(original, "  candidate delta  ")
        self.assertEqual(composed["audit"], "AUDIT\n\ncandidate delta")
        self.assertEqual(composed["baseline"], "CONTROL")
        self.assertEqual(original, {"audit": "AUDIT", "baseline": "CONTROL"})

    def test_blank_audit_prompt_delta_is_noop(self) -> None:
        original = {"audit": "AUDIT", "baseline": "CONTROL"}
        self.assertEqual(append_audit_suffix(original, " \n "), original)

    def test_authoritative_pipeline_prevents_early_collection(self) -> None:
        empty_views = {
            "lean": {"status": "none"},
            "dag_informal": {"status": "none"},
            "dag_formal": {"status": "none"},
            "auto_pipeline": {"status": "running"},
        }
        self.assertFalse(sidecars_settled(empty_views))
        empty_views["auto_pipeline"]["status"] = "partial"
        self.assertTrue(sidecars_settled(empty_views))

    def test_outcome_parser_ignores_negated_and_instructional_mentions(self) -> None:
        proof = """This does not solve the original problem.
Allowed labels are Solved, Counterexample, Known/Open Status, or Partial Progress.
**ProvingConsole outcome: Partial Progress**
"""
        self.assertEqual(extract_outcomes(proof), ["Partial Progress"])

    def test_outcome_parser_accepts_explicit_heading_and_tex_wrapper(self) -> None:
        markdown = """## ProvingConsole outcome

Partial Progress
"""
        latex = (
            r"\boxed{\text{\bfseries ProvingConsole outcome: Partial Progress}}"
        )
        self.assertEqual(extract_outcomes(markdown), ["Partial Progress"])
        self.assertEqual(extract_outcomes(latex), ["Partial Progress"])

    def test_atomic_json_leaves_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            atomic_json(target, {"ok": True})
            self.assertTrue(target.is_file())
            self.assertFalse(target.with_suffix(".json.tmp").exists())

    def test_persisted_terminal_run_recovers_only_explicit_terminal_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            runs = data_dir / "runs"
            runs.mkdir()
            (runs / "finished-run.json").write_text(
                json.dumps({"status": "failed", "error": "runner exited"}),
                encoding="utf-8",
            )
            recovered = persisted_terminal_run(data_dir, "finished-run")
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["error"], "runner exited")

            (runs / "active-run.json").write_text(
                json.dumps({"status": "running"}), encoding="utf-8"
            )
            self.assertIsNone(persisted_terminal_run(data_dir, "active-run"))
            self.assertIsNone(persisted_terminal_run(data_dir, "../finished-run"))

    def test_persisted_terminal_run_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            runs = data_dir / "runs"
            runs.mkdir()
            outside = data_dir / "outside.json"
            outside.write_text(json.dumps({"status": "finished"}), encoding="utf-8")
            (runs / "linked.json").symlink_to(outside)
            self.assertIsNone(persisted_terminal_run(data_dir, "linked"))

    def test_evidence_collector_reads_improof_tex_outcome(self) -> None:
        class Client:
            def get(self, path: str):
                if path.endswith("/files"):
                    return {"files": [{"path": "proof.tex"}]}
                self.assert_path = path
                return {
                    "content": (
                        "\\section*{Result}\n"
                        "ProvingConsole outcome: Partial Progress\n"
                    )
                }

        client = Client()
        evidence = collect_artifact_evidence(client, "improof-run")
        self.assertEqual(evidence["proof_artifact"]["path"], "proof.tex")
        self.assertIsNone(evidence["proof_md"])
        self.assertEqual(evidence["outcomes"], ["Partial Progress"])
        self.assertIn("proof.tex", client.assert_path)

    def test_audit_coverage_requires_global_decomposed_and_merge(self) -> None:
        incomplete = audit_pass_coverage(
            ["audit/run/global.json", "audit/run/run.json", "audit/run/certificate.json"]
        )
        self.assertEqual(incomplete["passes"], ["global"])
        self.assertFalse(incomplete["core_complete"])
        complete = audit_pass_coverage(
            [
                "audit/run/global.json",
                "audit/run/decomposed.json",
                "audit/run/verifier-output.json",
                "audit/run/citation.json",
                "audit/run/citation-fetch-log.json",
            ]
        )
        self.assertTrue(complete["core_complete"])
        self.assertEqual(
            complete["passes"], ["citation", "decomposed", "global", "merged"]
        )
        self.assertNotIn(
            "audit/run/citation-fetch-log.json",
            complete["files"]["citation"],
        )

    def test_audit_coverage_accepts_explicit_reruns_but_not_logs(self) -> None:
        coverage = audit_pass_coverage(
            [
                "audit/run/global-rerun.json",
                "audit/run/decomposed-rerun.json",
                "audit/run/boundary-rerun.json",
                "audit/run/citation-rerun.json",
                "audit/run/refuter-rerun.json",
                "audit/run/verifier-output.json",
                "audit/run/global-fetch-log.json",
                "audit/run/citation-fetch-log.json",
            ]
        )
        self.assertTrue(coverage["core_complete"])
        self.assertEqual(
            coverage["passes"],
            ["boundary", "citation", "decomposed", "global", "merged", "refuter"],
        )
        self.assertNotIn(
            "audit/run/global-fetch-log.json", coverage["files"]["global"]
        )
        self.assertNotIn(
            "audit/run/citation-fetch-log.json", coverage["files"]["citation"]
        )


if __name__ == "__main__":
    unittest.main()
