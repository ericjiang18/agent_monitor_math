from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor.runners import openclaw_runner


class KimiOpenClawFormalApiTests(unittest.TestCase):
    def test_api_formal_mode_rejects_stale_exit_zero_and_skips_informal_tail(self) -> None:
        class Runtime:
            def __enter__(self):
                return "flashflame/kimi-k3"

            def __exit__(self, *_args):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            workspace.chmod(0o700)
            (workspace / "Proof.lean").write_text(
                "/- substantive existing proof -/\ntheorem existing : True := by\n  trivial\n",
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "AGENT_MONITOR_FORMAL_LEAN_MODE": "1",
                        "AGENT_MONITOR_OPENCLAW_MODEL": "kimi-k3",
                    },
                    clear=True,
                ),
                patch.object(
                    openclaw_runner.sys,
                    "argv",
                    ["openclaw_runner.py", "formalize this", "api-formal"],
                ),
                patch.object(openclaw_runner.Path, "cwd", return_value=workspace),
                patch.object(openclaw_runner.shutil, "which", return_value="/mock/openclaw"),
                patch.object(
                    openclaw_runner,
                    "_openclaw_api_model_runtime",
                    return_value=Runtime(),
                ),
                patch.object(openclaw_runner.subprocess, "call", return_value=0) as call,
                patch.object(openclaw_runner, "_emit_formal_lean_artifact_status") as emitted,
                patch.object(openclaw_runner, "_normalize_requested_outcome") as normalize,
                patch.object(openclaw_runner, "_run_supplemental_audit") as audit,
            ):
                returncode = openclaw_runner.main()

        self.assertEqual(returncode, 65)
        prompt = call.call_args.args[0][-1]
        self.assertIn("FORMAL LEAN FRESHNESS CONTRACT", prompt)
        emitted.assert_called_once()
        self.assertFalse(emitted.call_args.args[0])
        normalize.assert_not_called()
        audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
