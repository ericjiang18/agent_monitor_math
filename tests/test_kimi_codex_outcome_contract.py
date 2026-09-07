from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor.runners import kimi_codex_runner
from agent_monitor.runners.codex_backend import CodexResult


class KimiCodexOutcomeContractTests(unittest.TestCase):
    def _run_with_proof(self, proof: str) -> tuple[int, str]:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                def write_proof(*_args: object, **_kwargs: object) -> CodexResult:
                    Path("proof.md").write_text(proof, encoding="utf-8")
                    return CodexResult("done", {})

                with (
                    patch.dict(
                        os.environ,
                        {"AGENT_MONITOR_SELECTED_MODEL": "kimi-k3"},
                        clear=False,
                    ),
                    patch.object(
                        os.sys,
                        "argv",
                        ["kimi_codex_runner.py", "prove it"],
                    ),
                    patch.object(
                        kimi_codex_runner,
                        "codex_exec",
                        side_effect=write_proof,
                    ),
                ):
                    status = kimi_codex_runner.main()
                return status, Path("proof.md").read_text(encoding="utf-8")
            finally:
                os.chdir(previous)

    def test_decorative_outcome_does_not_promote_missing_contract(self) -> None:
        status, rendered = self._run_with_proof(
            "# Proof\n\nA fully written substantive argument. " * 4
            + "\n\n**Outcome: Solved**\n"
        )

        self.assertEqual(status, 0)
        self.assertEqual(rendered.count("ProvingConsole outcome:"), 1)
        self.assertTrue(
            rendered.rstrip().endswith("ProvingConsole outcome: Partial Progress")
        )

    def test_exact_canonical_outcome_is_preserved_once(self) -> None:
        status, rendered = self._run_with_proof(
            "# Proof\n\nA fully written substantive argument. " * 4
            + "\n\nProvingConsole outcome: Solved\n"
        )

        self.assertEqual(status, 0)
        self.assertEqual(rendered.count("ProvingConsole outcome:"), 1)
        self.assertTrue(rendered.rstrip().endswith("ProvingConsole outcome: Solved"))

    def test_formal_artifact_is_not_rewritten_as_markdown(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                source = "theorem two_add_two : 2 + 2 = 4 := by decide\n"

                def write_lean(*_args: object, **_kwargs: object) -> CodexResult:
                    Path("Proof.lean").write_text(source, encoding="utf-8")
                    return CodexResult("done", {})

                with (
                    patch.dict(
                        os.environ,
                        {
                            "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
                            "AGENT_MONITOR_KIMI_CODEX_MODE": "lean",
                        },
                        clear=False,
                    ),
                    patch.object(
                        os.sys,
                        "argv",
                        ["kimi_codex_runner.py", "formalize it"],
                    ),
                    patch.object(
                        kimi_codex_runner,
                        "codex_exec",
                        side_effect=write_lean,
                    ),
                ):
                    self.assertEqual(kimi_codex_runner.main(), 0)
                self.assertEqual(Path("Proof.lean").read_text(encoding="utf-8"), source)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
