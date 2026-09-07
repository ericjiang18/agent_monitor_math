from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent_monitor import engines_registry, jobs
from agent_monitor.runners import openhands_subscription_runner as runner


class ClaudeOpenHandsCompatibilityTests(unittest.TestCase):
    def test_openhands_stays_out_of_linked_claude_matrix(self) -> None:
        self.assertNotIn(
            "openhands",
            engines_registry.CLAUDE_SUBSCRIPTION_ENGINES,
        )
        self.assertNotIn("openhands", jobs._CLAUDE_SUBSCRIPTION_ENGINES)
        self.assertNotIn(
            "claude_subscription",
            engines_registry.ENGINE_AUTH_MODES["openhands"],
        )


class ClaudeOpenHandsRunnerFailClosedTests(unittest.TestCase):
    def _run_main(self, environment: dict[str, str]) -> tuple[int, list[dict]]:
        output = io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                sys,
                "argv",
                ["openhands_subscription_runner.py", "prove a tiny theorem"],
            ),
            patch.object(runner, "_which") as find_binary,
            patch.object(runner, "_ensure_sdk_python") as ensure_sdk,
            patch.object(runner, "_run_kimi_native_api") as kimi,
            patch.object(runner, "_run_acp") as codex_acp,
            patch.object(runner, "_run_codex_exec") as codex_fallback,
            patch.object(runner.subprocess, "call") as native_api,
            redirect_stdout(output),
        ):
            returncode = runner.main()

        find_binary.assert_not_called()
        ensure_sdk.assert_not_called()
        kimi.assert_not_called()
        codex_acp.assert_not_called()
        codex_fallback.assert_not_called()
        native_api.assert_not_called()
        events = [
            json.loads(line)
            for line in output.getvalue().splitlines()
            if line.strip()
        ]
        return returncode, events

    def test_linked_claude_never_falls_through_to_api_or_another_runtime(self) -> None:
        returncode, events = self._run_main(
            {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_MODEL": "claude-haiku-4-5",
                "CLAUDE_CONFIG_DIR": "/private/account",
                "ANTHROPIC_API_KEY": "poison-anthropic-key",
                "OPENAI_API_KEY": "poison-openai-key",
                "KIMI_API_KEY": "poison-kimi-key",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_ACCESS_KEY_ID": "poison-cloud-credential",
            }
        )

        self.assertEqual(returncode, 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "item.completed")
        item = events[0]["item"]
        self.assertEqual(item["type"], "error")
        self.assertIn("pinned Claude ACP runtime", item["message"])
        self.assertIn("exact-model attestation", item["message"])

    def test_conflicting_subscription_flags_are_rejected_before_launch(self) -> None:
        returncode, events = self._run_main(
            {
                "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            }
        )

        self.assertEqual(returncode, 1)
        self.assertEqual(events[0]["item"]["type"], "error")
        self.assertIn(
            "conflicting Codex and Claude account routes",
            events[0]["item"]["message"],
        )


if __name__ == "__main__":
    unittest.main()
