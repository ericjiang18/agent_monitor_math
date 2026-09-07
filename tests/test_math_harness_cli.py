from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_monitor import cli, jobs


class MathHarnessCliTests(unittest.TestCase):
    def test_new_run_parser_replaces_ucla_with_math_harness(self) -> None:
        with (
            patch.object(cli, "_bootstrap"),
            patch.object(cli, "cmd_run") as dispatch,
        ):
            cli.main(["run", "math_harness", "fixture.txt"])
            self.assertEqual(dispatch.call_args.args[0].engine, "math_harness")

        with patch.object(cli, "_bootstrap"), self.assertRaises(SystemExit) as stopped:
            cli.main(["run", "ucla", "fixture.txt"])
        self.assertEqual(stopped.exception.code, 2)

    def test_math_harness_cli_uses_ownerless_api_job_and_waits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problem = Path(tmp) / "problem.txt"
            problem.write_text("Prove the CLI migration fixture.", encoding="utf-8")
            args = argparse.Namespace(
                engine="math_harness",
                problem=str(problem),
                problem_id="cli-fixture",
                model="gpt-test",
                max_iterations=3,
            )
            terminal = {
                "job_id": "job-1",
                "run_id": "math_harness_cli-fixture_job-1",
                "status": "finished",
            }
            prior = os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE")
            output = io.StringIO()
            with (
                patch.object(
                    jobs,
                    "start_job",
                    return_value={"job_id": "job-1", "run_id": terminal["run_id"]},
                ) as start,
                patch.object(jobs, "get_job", return_value=terminal) as get,
                patch("sys.stdout", output),
            ):
                cli.cmd_run(args)

        start.assert_called_once_with(
            engine="math_harness",
            problem_id="cli-fixture",
            problem_text="Prove the CLI migration fixture.",
            model="gpt-test",
            max_iterations=3,
            user=None,
            use_subagents=False,
            auth_route="api_key",
        )
        get.assert_called_once_with("job-1")
        self.assertEqual(json.loads(output.getvalue()), terminal)
        self.assertEqual(os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE"), prior)


if __name__ == "__main__":
    unittest.main()
