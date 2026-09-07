from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_monitor import jobs


class KimiTerminalOutcomeContractTests(unittest.TestCase):
    @staticmethod
    def _draft() -> str:
        return (
            "# Proof\n\nA substantive self-contained mathematical argument. " * 4
            + "\n\n## References\n\n[1] self-derived — the argument above.\n"
        )

    def test_finished_sponsored_run_is_normalized_before_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text(self._draft() + "\n**Outcome: Solved**\n")
            run = {
                "status": "finished",
                "credential_source": "sponsored_kimi",
                "agents": [],
                "totals": {},
            }

            jobs._attach_analysis(run, workspace)

            rendered = proof.read_text()
            self.assertEqual(rendered.count("ProvingConsole outcome:"), 1)
            self.assertEqual(rendered.count("## References"), 1)
            self.assertTrue(
                rendered.rstrip().endswith(
                    "ProvingConsole outcome: Partial Progress"
                )
            )

    def test_valid_kimi_footer_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            original = self._draft() + "\nProvingConsole outcome: Solved\n"
            proof.write_text(original)
            run = {"status": "finished", "model": "kimi-k3"}

            self.assertFalse(jobs._normalize_terminal_kimi_outcome(run, workspace))
            self.assertEqual(proof.read_text(), original)

    def test_running_kimi_draft_is_not_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            original = self._draft()
            proof.write_text(original)

            self.assertFalse(
                jobs._normalize_terminal_kimi_outcome(
                    {"status": "running", "model": "kimi-k3"}, workspace
                )
            )
            self.assertEqual(proof.read_text(), original)

    def test_non_kimi_finished_run_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            original = self._draft()
            proof.write_text(original)

            self.assertFalse(
                jobs._normalize_terminal_kimi_outcome(
                    {"status": "finished", "model": "gpt-test"}, workspace
                )
            )
            self.assertEqual(proof.read_text(), original)

    def test_math_harness_kimi_artifact_is_not_rewritten_after_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            original = self._draft()
            proof.write_text(original)

            self.assertFalse(
                jobs._normalize_terminal_kimi_outcome(
                    {
                        "status": "finished",
                        "engine": "math_harness",
                        "model": "kimi-k3",
                    },
                    workspace,
                )
            )
            self.assertEqual(proof.read_text(), original)

    def test_symlinked_proof_is_not_read_or_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            workspace = Path(tmp)
            target = Path(outside) / "secret.md"
            original = self._draft()
            target.write_text(original)
            (workspace / "proof.md").symlink_to(target)

            self.assertFalse(
                jobs._normalize_terminal_kimi_outcome(
                    {"status": "finished", "model": "kimi-k3"}, workspace
                )
            )
            self.assertTrue((workspace / "proof.md").is_symlink())
            self.assertEqual(target.read_text(), original)


if __name__ == "__main__":
    unittest.main()
