from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor.cli_events import CLIEventParser
from agent_monitor.runners import claude_backend


def _events(*, error: bool = False) -> tuple[dict, ...]:
    return (
        {"type": "system", "subtype": "init", "model": "claude-sonnet"},
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet",
                "content": [{"type": "text", "text": "Completed the proof."}],
                "usage": {"input_tokens": 11, "output_tokens": 7},
            },
        },
        {
            "type": "result",
            "subtype": "error_max_turns" if error else "success",
            "is_error": error,
            "result": "turn cap" if error else "Completed the proof.",
            "num_turns": 1,
            "total_cost_usd": 0.02,
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            },
        },
    )


class ClaudeCodeRuntimeTests(unittest.TestCase):
    def test_direct_runner_script_imports_package_backend(self) -> None:
        runner = (
            Path(__file__).resolve().parents[1]
            / "agent_monitor"
            / "runners"
            / "claude_runner.py"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [sys.executable, str(runner)],
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("empty proof prompt", completed.stderr)
        self.assertNotIn("ImportError", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_oauth_environment_scrubs_api_and_cloud_routes(self) -> None:
        with tempfile.TemporaryDirectory() as config:
            source = {
                "PATH": "/bin",
                "HOME": "/tmp/home",
                "CLAUDE_CONFIG_DIR": config,
                "ANTHROPIC_API_KEY": "api-secret",
                "ANTHROPIC_AUTH_TOKEN": "auth-secret",
                "ANTHROPIC_BASE_URL": "https://gateway.invalid",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "CLAUDE_CODE_USE_VERTEX": "1",
                "CLAUDE_CODE_USE_FOUNDRY": "1",
                "AWS_ACCESS_KEY_ID": "aws-secret",
                "GOOGLE_APPLICATION_CREDENTIALS": "/secret.json",
                "AZURE_CLIENT_SECRET": "azure-secret",
            }
            with patch(
                "agent_monitor.engines_registry.tool_search_path",
                return_value="/approved/node:/approved/claude",
            ):
                env = claude_backend._claude_environment(source)
        self.assertEqual(
            env["PATH"], "/bin:/approved/node:/approved/claude:/usr/bin"
        )
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(Path(config).resolve()))
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "AWS_ACCESS_KEY_ID",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AZURE_CLIENT_SECRET",
        ):
            self.assertNotIn(name, env)

    def test_binary_resolution_uses_explicit_and_registry_approved_paths(self) -> None:
        source = {"PATH": "/route/bin"}
        with (
            patch(
                "agent_monitor.engines_registry.tool_search_path",
                return_value="/approved/node:/approved/claude",
            ),
            patch.object(
                claude_backend.shutil,
                "which",
                return_value="/approved/claude/claude",
            ) as which,
        ):
            binary = claude_backend._claude_binary(source)

        self.assertEqual(binary, "/approved/claude/claude")
        self.assertEqual(
            which.call_args.kwargs["path"],
            "/route/bin:/approved/node:/approved/claude:/bin:/usr/bin",
        )

    def test_command_is_noninteractive_ephemeral_and_not_bare(self) -> None:
        self.assertEqual(
            claude_backend.RESPONSE_ONLY_TOOL_ISOLATION_VERSION,
            1,
        )
        with patch.object(claude_backend, "_claude_binary", return_value="/bin/claude"):
            command = claude_backend._build_command(
                prompt="prove it",
                system="be rigorous",
                model="opus",
                max_turns=23,
                effort="xhigh",
                enable_tools=False,
            )
        self.assertEqual(command[:3], ["/bin/claude", "-p", "prove it"])
        self.assertIn("stream-json", command)
        self.assertIn("--verbose", command)
        self.assertIn("--safe-mode", command)
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--bare", command)
        self.assertEqual(command[command.index("--model") + 1], "opus")
        self.assertEqual(command[command.index("--max-turns") + 1], "23")
        self.assertEqual(command[command.index("--effort") + 1], "xhigh")
        self.assertEqual(command[command.index("--tools") + 1], "")

    def test_haiku_command_is_exact_and_omits_unsupported_effort(self) -> None:
        with patch.object(claude_backend, "_claude_binary", return_value="/bin/claude"):
            command = claude_backend._build_command(
                prompt="prove it",
                system="be rigorous",
                model="claude-haiku-4-5",
                max_turns=7,
                effort="max",
                enable_tools=False,
            )
        self.assertEqual(
            command[command.index("--model") + 1], "claude-haiku-4-5"
        )
        self.assertNotIn("--effort", command)

    def test_default_effort_is_a_real_provider_default(self) -> None:
        self.assertEqual(claude_backend._effort(None, {}), "default")
        with patch.object(claude_backend, "_claude_binary", return_value="/bin/claude"):
            command = claude_backend._build_command(
                prompt="prove it",
                system="be rigorous",
                model="sonnet",
                max_turns=7,
                effort="default",
                enable_tools=False,
            )
        self.assertNotIn("--effort", command)

    def test_exact_haiku_rejects_silent_model_substitution(self) -> None:
        accepted_model = "claude-haiku-4-5-20251001"
        accepted = claude_backend.ClaudeResult(
            "ok",
            {},
            accepted_model,
            None,
            ({"type": "assistant", "message": {"model": accepted_model}},),
            {},
        )
        claude_backend._require_requested_model(accepted, "claude-haiku-4-5")
        substituted_model = "claude-sonnet-5"
        substituted = claude_backend.ClaudeResult(
            "wrong",
            {},
            substituted_model,
            None,
            ({"type": "assistant", "message": {"model": substituted_model}},),
            {},
        )
        with self.assertRaisesRegex(claude_backend.ClaudeBackendError, "model mismatch"):
            claude_backend._require_requested_model(substituted, "claude-haiku-4-5")

    def test_terminal_result_parses_usage_and_rejects_error(self) -> None:
        success = claude_backend._terminal_result(
            claude_backend._ProcessResult(0, "", _events())
        )
        self.assertEqual(success.text, "Completed the proof.")
        self.assertEqual(success.model, "claude-sonnet")
        self.assertEqual(success.usage["cache_read_tokens"], 3)
        self.assertEqual(success.cost_usd, 0.02)
        with self.assertRaisesRegex(claude_backend.ClaudeBackendError, "error_max_turns"):
            claude_backend._terminal_result(
                claude_backend._ProcessResult(0, "", _events(error=True))
            )

    def test_response_helper_requires_explicit_subscription(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                claude_backend.ClaudeBackendError,
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION",
            ):
                claude_backend.claude_exec("system", "user")

    def test_response_helper_rejects_exact_haiku_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as config:
            source_env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "CLAUDE_CONFIG_DIR": config,
                "PATH": os.environ.get("PATH", ""),
            }
            with (
                patch.object(
                    claude_backend,
                    "_run_stream",
                    return_value=claude_backend._ProcessResult(0, "", _events()),
                ),
                patch.object(claude_backend, "_claude_binary", return_value="/bin/claude"),
                self.assertRaisesRegex(claude_backend.ClaudeBackendError, "model mismatch"),
            ):
                claude_backend.claude_exec(
                    "system", "user", model="claude-haiku-4-5", source_env=source_env
                )

    def test_response_helper_uses_explicit_source_without_mutating_global_env(self) -> None:
        with tempfile.TemporaryDirectory() as config:
            source_env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "CLAUDE_CONFIG_DIR": config,
                "AGENT_MONITOR_CLAUDE_MODEL": "fable",
                "AGENT_MONITOR_CLAUDE_MAX_TURNS": "9",
                "AGENT_MONITOR_CLAUDE_EFFORT": "high",
                "PATH": os.environ.get("PATH", ""),
            }
            original = dict(os.environ)
            with patch.object(
                claude_backend,
                "_run_stream",
                return_value=claude_backend._ProcessResult(0, "", _events()),
            ), patch.object(
                claude_backend, "_claude_binary", return_value="/bin/claude"
            ):
                result = claude_backend.claude_exec(
                    "system", "user", source_env=source_env
                )
        self.assertEqual(result.text, "Completed the proof.")
        self.assertEqual(dict(os.environ), original)

    def test_proof_requires_fresh_substantive_root_file_and_normalizes_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as config:
            workspace = Path(td)
            stale = workspace / "proof.md"
            stale.write_text("S" * 500, encoding="utf-8")
            captured: dict[str, list[str]] = {}

            def complete(command, **kwargs):
                captured["command"] = command
                captured["timeout"] = kwargs["timeout"]
                stale.write_text(
                    "# Checked proof\n\n" + "A rigorous derivation. " * 28
                    + "\n\nProvingConsole outcome: Solved.\n",
                    encoding="utf-8",
                )
                return claude_backend._ProcessResult(0, "", _events())

            env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "CLAUDE_CONFIG_DIR": config,
                "PATH": os.environ.get("PATH", ""),
            }
            with patch.dict(os.environ, env, clear=True), patch.object(
                claude_backend, "_run_stream", side_effect=complete
            ), patch.object(
                claude_backend, "_claude_binary", return_value="/bin/claude"
            ):
                result = claude_backend.claude_proof(
                    "End with ProvingConsole outcome: <label>.", workspace
                )
            self.assertEqual(result.artifact, stale)
            self.assertEqual(captured["timeout"], 3300)
            rendered = stale.read_text(encoding="utf-8")
            system_index = captured["command"].index("--system-prompt") + 1
            system_prompt = captured["command"][system_index]
            self.assertIn(str(workspace.resolve()), system_prompt)
            self.assertIn(str(stale.resolve()), system_prompt)
            self.assertIn("/tmp/proof.md", system_prompt)
            self.assertEqual(rendered.count("ProvingConsole outcome:"), 1)
            self.assertTrue(rendered.endswith("ProvingConsole outcome: Solved\n"))

    def test_lean_mode_requires_fresh_root_proof_lean_without_outcome_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as config:
            workspace = Path(td)
            lean = workspace / "Proof.lean"
            lean.write_text("-- stale\n" + " " * 100, encoding="utf-8")

            def complete(*args, **kwargs):
                lean.write_text(
                    "theorem one_plus_one : 1 + 1 = 2 := by norm_num\n"
                    + "-- independently compiled formal checkpoint\n" * 3,
                    encoding="utf-8",
                )
                return claude_backend._ProcessResult(0, "", _events())

            source_env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_LEAN_MODE": "1",
                "CLAUDE_CONFIG_DIR": config,
                "PATH": os.environ.get("PATH", ""),
            }
            with patch.object(
                claude_backend, "_run_stream", side_effect=complete
            ), patch.object(
                claude_backend, "_claude_binary", return_value="/bin/claude"
            ):
                result = claude_backend.claude_proof(
                    "End with ProvingConsole outcome: <label>.",
                    workspace,
                    source_env=source_env,
                )
            lean_text = lean.read_text(encoding="utf-8")
        self.assertEqual(result.artifact.name, "Proof.lean")
        self.assertNotIn("ProvingConsole outcome:", lean_text)

    def test_lean_mode_accepts_generic_formal_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as config:
            workspace = Path(td)
            lean = workspace / "Proof.lean"
            lean.write_text("-- stale\n" + " " * 100, encoding="utf-8")

            def complete(*args, **kwargs):
                lean.write_text(
                    "theorem generic_flag : True := by trivial\n"
                    + "-- independently compiled formal checkpoint\n" * 3,
                    encoding="utf-8",
                )
                return claude_backend._ProcessResult(0, "", _events())

            source_env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_FORMAL_LEAN_MODE": "1",
                "CLAUDE_CONFIG_DIR": config,
                "PATH": os.environ.get("PATH", ""),
            }
            with patch.object(
                claude_backend, "_run_stream", side_effect=complete
            ), patch.object(
                claude_backend, "_claude_binary", return_value="/bin/claude"
            ):
                result = claude_backend.claude_proof(
                    "End with ProvingConsole outcome: <label>.",
                    workspace,
                    source_env=source_env,
                )
            lean_text = lean.read_text(encoding="utf-8")

        self.assertEqual(result.artifact.name, "Proof.lean")
        self.assertNotIn("ProvingConsole outcome:", lean_text)


    def test_proof_rejects_unchanged_nested_short_and_linked_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            stale = workspace / "proof.md"
            stale.write_text("S" * 500, encoding="utf-8")
            before = claude_backend._proof_signatures(workspace)
            with self.assertRaisesRegex(claude_backend.ClaudeBackendError, "fresh substantive"):
                claude_backend._fresh_proof(workspace, before)
            stale.write_text("S" * 500, encoding="utf-8")
            with self.assertRaisesRegex(claude_backend.ClaudeBackendError, "fresh substantive"):
                claude_backend._fresh_proof(workspace, before)
            stale.write_text("short", encoding="utf-8")
            with self.assertRaisesRegex(claude_backend.ClaudeBackendError, "fresh substantive"):
                claude_backend._fresh_proof(workspace, before)
            stale.unlink()
            target = workspace / "target.md"
            target.write_text("L" * 500, encoding="utf-8")
            stale.symlink_to(target)
            with self.assertRaisesRegex(claude_backend.ClaudeBackendError, "fresh substantive"):
                claude_backend._fresh_proof(workspace, before)

    def test_native_claude_protocol_builds_monitor_turns(self) -> None:
        parser = CLIEventParser("claude")
        for event in _events():
            parser.feed(json.dumps(event) + "\n")
        self.assertEqual(parser.usage["input_tokens"], 11)
        self.assertEqual(parser.usage["output_tokens"], 7)
        self.assertEqual(parser.usage["model"], "claude-sonnet")
        self.assertEqual(parser.final_message(), "Completed the proof.")
        self.assertTrue(parser.agent_nodes("claude-test", prompt="prove"))

    def test_codex_projection_preserves_claude_text_and_usage_once(self) -> None:
        projected: list[dict] = []
        for event in _events():
            claude_backend.forward_as_codex_event(event, projected.append)
        parser = CLIEventParser("codex")
        for event in projected:
            parser.feed(json.dumps(event) + "\n")
        self.assertEqual(parser.final_message(), "Completed the proof.")
        self.assertEqual(parser.usage["input_tokens"], 11)
        self.assertEqual(parser.usage["output_tokens"], 7)


if __name__ == "__main__":
    unittest.main()
