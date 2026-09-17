from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor.engines_registry import (
    CLAUDE_SUBSCRIPTION_ENGINES,
    CLI_ENGINES,
    ENGINE_AUTH_MODES,
    build_cli_command,
    list_engines,
)


class ClaudeRegistryRuntimeTests(unittest.TestCase):
    def test_claude_is_a_dedicated_subscription_only_engine(self) -> None:
        spec = CLI_ENGINES["claude"]
        self.assertEqual(spec["parser_style"], "claude")
        self.assertEqual(
            spec["supported_models"],
            ["sonnet", "opus", "fable", "claude-haiku-4-5"],
        )
        self.assertEqual(ENGINE_AUTH_MODES["claude"], ["claude_subscription"])
        self.assertIn("claude", CLAUDE_SUBSCRIPTION_ENGINES)

        with patch(
            "agent_monitor.engines_registry.which_tool", return_value="/bin/claude"
        ), tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            command = build_cli_command(
                "claude",
                prompt="Prove 1 + 1 = 2.",
                workspace=workspace,
                problem_file=workspace / "problem.txt",
            )
            by_id = {entry["id"]: entry for entry in list_engines()}
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command[0], sys.executable)
        self.assertIn("claude_runner.py", command[2])
        self.assertEqual(by_id["claude"]["auth_modes"], ["claude_subscription"])
        self.assertTrue(by_id["claude"]["claude_subscription_supported"])
        self.assertFalse(by_id["claude"]["subscription_supported"])


if __name__ == "__main__":
    unittest.main()
