from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor.runners import improof


def _proof_document() -> str:
    return (
        "\\documentclass{article}\n\\begin{document}\n"
        "\\section*{Proof}\n"
        + (
            "Assume the hypotheses. We show the asserted conclusion by a "
            "direct argument; therefore every required case follows. "
        )
        * 6
        + "\n\\end{document}\n"
    )


class KimiIMProofRunnerTests(unittest.TestCase):
    def test_kimi_selection_is_exact(self) -> None:
        self.assertTrue(
            improof._kimi_model_selected(
                ["--model", "*=models/kimi/k3"],
                None,
            )
        )
        self.assertTrue(
            improof._kimi_model_selected(
                None,
                {"AGENT_MONITOR_SELECTED_MODEL": "kimi-k3"},
            )
        )
        self.assertFalse(
            improof._kimi_model_selected(
                ["--model", "*=models/openai/gpt-54-mini"],
                {"AGENT_MONITOR_SELECTED_MODEL": "kimi-k2.6"},
            )
        )

    def test_kimi_routes_to_response_workflow_and_publishes_artifact(self) -> None:
        captured: dict[str, object] = {}

        def fake_stream(command, **kwargs):
            captured["command"] = command
            run_id = command[command.index("--run-id") + 1]
            output_root = Path(command[command.index("--output") + 1])
            native = output_root / run_id
            solution = native / "solutions" / "p.tex"
            solution.parent.mkdir(parents=True, exist_ok=True)
            solution.write_text(_proof_document(), encoding="utf-8")
            (native / "run-metadata.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "outputs": {
                            "status": "finished",
                            "compiled": True,
                            "solution_tex": str(solution),
                        },
                    }
                ),
                encoding="utf-8",
            )
            return "ok", 0, False

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            problem = root / "problem.txt"
            problem.write_text("Prove that 1 + 1 = 2.", encoding="utf-8")
            with (
                patch(
                    "agent_monitor.runners._stream.stream_subprocess",
                    side_effect=fake_stream,
                ),
                patch.object(improof, "_research_packet") as research_packet,
            ):
                result = improof.run_problem(
                    problem,
                    problem_id="p",
                    output_dir=root,
                    extra_args=["--model", "*=models/kimi/k3"],
                    research_model="kimi-k3",
                )

            command = captured["command"]
            self.assertEqual(
                command[command.index("--workflow") + 1],
                "kimi_author_critic",
            )
            self.assertEqual(result["status"], "finished")
            self.assertEqual(result["returncode"], 0)
            self.assertTrue((root / "proof.tex").is_file())
            research_packet.assert_not_called()

    def test_kimi_internal_error_cannot_be_reported_finished(self) -> None:
        def fake_stream(command, **_kwargs):
            run_id = command[command.index("--run-id") + 1]
            output_root = Path(command[command.index("--output") + 1])
            native = output_root / run_id
            (native / "run-metadata.json").write_text(
                json.dumps(
                    {
                        "status": "error",
                        "error": "provider failed",
                        "outputs": {"error": "provider failed"},
                    }
                ),
                encoding="utf-8",
            )
            return "subprocess returned zero incorrectly", 0, False

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            problem = root / "problem.txt"
            problem.write_text("Prove that 1 + 1 = 2.", encoding="utf-8")
            with patch(
                "agent_monitor.runners._stream.stream_subprocess",
                side_effect=fake_stream,
            ):
                result = improof.run_problem(
                    problem,
                    problem_id="p",
                    output_dir=root,
                    extra_args=["--model", "*=models/kimi/k3"],
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["returncode"], 1)
            self.assertIn("provider failed", result["error"])
            self.assertFalse((root / "proof.tex").exists())


if __name__ == "__main__":
    unittest.main()
