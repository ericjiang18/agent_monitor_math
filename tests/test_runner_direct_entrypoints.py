from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_RUNNERS_ROOT = _REPOSITORY_ROOT / "agent_monitor" / "runners"


class DirectRunnerEntrypointTests(unittest.TestCase):
    """Registry-style file execution must not depend on cwd or PYTHONPATH."""

    def _run(
        self,
        script: str,
        *,
        clear_path: bool = False,
        clear_bridge: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        if clear_path:
            # openclaw_runner reaches its deterministic missing-binary boundary
            # before it can create state or launch any harness/model process.
            env["PATH"] = ""
        if clear_bridge:
            for name in (
                "PROVINGCONSOLE_CLAUDE_CONFIG_HOME",
                "PROVINGCONSOLE_CLAUDE_PROCESS_HOME",
                "PROVINGCONSOLE_CLAUDE_BINARY",
            ):
                env.pop(name, None)
        with tempfile.TemporaryDirectory() as workspace:
            self.assertFalse(Path(workspace).is_relative_to(_REPOSITORY_ROOT))
            return subprocess.run(
                [sys.executable, "-I", str(_RUNNERS_ROOT / script)],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

    def _assert_clean_boundary(
        self,
        completed: subprocess.CompletedProcess[str],
        *,
        returncode: int,
        marker: str,
    ) -> None:
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, returncode, output)
        self.assertIn(marker, output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("ImportError", output)
        self.assertNotIn("ModuleNotFoundError", output)

    def test_response_runners_reach_empty_prompt_without_model_call(self) -> None:
        for script in (
            "plain_runner.py",
            "metaharness_runner.py",
            "deepagents_runner.py",
        ):
            with self.subTest(script=script):
                self._assert_clean_boundary(
                    self._run(script),
                    returncode=2,
                    marker="empty prompt",
                )

    def test_direct_claude_runner_reaches_empty_prompt_without_model_call(self) -> None:
        self._assert_clean_boundary(
            self._run("claude_runner.py"),
            returncode=2,
            marker="empty proof prompt",
        )

    def test_openclaw_runner_imports_trusted_bridge_from_external_cwd(self) -> None:
        self._assert_clean_boundary(
            self._run("openclaw_runner.py", clear_path=True),
            returncode=127,
            marker="openclaw is not installed",
        )

    def test_openclaw_bridge_reaches_missing_config_boundary_without_exec(self) -> None:
        self._assert_clean_boundary(
            self._run(
                "openclaw_claude_cli_bridge.py",
                clear_bridge=True,
            ),
            returncode=78,
            marker="PROVINGCONSOLE_CLAUDE_CONFIG_HOME is required",
        )


if __name__ == "__main__":
    unittest.main()
