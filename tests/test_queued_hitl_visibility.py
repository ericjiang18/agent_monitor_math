from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs


class QueuedHitlVisibilityTests(unittest.TestCase):
    def test_active_harness_cannot_see_feedback_queued_for_next_turn(self) -> None:
        run_id = "plain_active_feedback"
        record = {
            "run_id": run_id,
            "engine": "plain",
            "problem_id": "visibility",
            "problem_text": "Prove the original claim.",
            "problems": [
                {"text": "Prove the original claim.", "source": "initial"}
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            workspace = runs / "workspaces" / run_id
            workspace.mkdir(parents=True)
            original = "Prove the original claim.\n"
            (workspace / "problem.txt").write_text(original, encoding="utf-8")

            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(
                    jobs,
                    "_run_is_active",
                    return_value={"job_id": "active-job"},
                ),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_write_run") as write_run,
                patch.object(jobs, "_append_chat", return_value={"role": "user"}),
                patch.object(jobs, "list_chat", return_value=[]),
            ):
                result = jobs.send_human_message(
                    run_id,
                    "Use a different lemma on the next turn.",
                )

            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["job_id"], "active-job")
            self.assertEqual(
                (workspace / "problem.txt").read_text(encoding="utf-8"),
                original,
            )
            self.assertFalse((workspace / "problems.json").exists())
            self.assertEqual(
                record["problems"][-1]["text"],
                "Use a different lemma on the next turn.",
            )
            write_run.assert_called_once_with(record)
            self.assertEqual(
                jobs._drain_human_feedback(workspace),
                ["Use a different lemma on the next turn."],
            )


if __name__ == "__main__":
    unittest.main()
