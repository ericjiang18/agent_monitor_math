from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import engines_registry, jobs, lean_verify
from agent_monitor.runners import openclaw_claude_cli_bridge as bridge
from agent_monitor.runners import openclaw_runner


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


class ClaudeOpenClawRegistryTests(unittest.TestCase):
    def test_only_genuine_adapters_are_exposed_in_claude_matrix(self) -> None:
        compatible = {
            "claude", "improof", "math_harness", "openclaw", "deepagents", "plain", "metaharness"
        }
        self.assertEqual(
            set(engines_registry.CLAUDE_SUBSCRIPTION_ENGINES), compatible
        )
        self.assertEqual(set(jobs._CLAUDE_SUBSCRIPTION_ENGINES), compatible)
        for engine in ("improof", "openclaw", "deepagents"):
            self.assertIn(
                "claude_subscription",
                engines_registry.ENGINE_AUTH_MODES[engine],
            )
        for unsupported in (
            "hermes", "ucla", "codex", "openclaude", "openhands",
            "deepseek_harness", "danus",
        ):
            self.assertNotIn(
                unsupported, engines_registry.CLAUDE_SUBSCRIPTION_ENGINES
            )


class ClaudeOpenClawBridgeTests(unittest.TestCase):
    def test_bridge_restores_private_home_and_scrubs_billing_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = _private_dir(root / "account")
            binary = _executable(root / "claude")
            source = {
                bridge.CONFIG_HOME_ENV: str(account),
                bridge.PROCESS_HOME_ENV: str(account),
                bridge.BINARY_ENV: str(binary),
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "must-not-survive",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_ACCESS_KEY_ID": "must-not-survive",
                "GOOGLE_APPLICATION_CREDENTIALS": "/other/account.json",
                "AZURE_CLIENT_SECRET": "must-not-survive",
                "OPENAI_API_KEY": "must-not-survive",
                "CODEX_HOME": "/other/codex",
                "KIMI_API_KEY": "must-not-survive",
                "OPENCLAW_GATEWAY_TOKEN": "must-not-survive",
            }
            selected, env = bridge.build_exec_environment(source)

        self.assertEqual(selected, binary.resolve())
        self.assertEqual(env["HOME"], str(account.resolve()))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(account.resolve()))
        self.assertEqual(env["PATH"], "/usr/bin")
        for name in (
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_ACCESS_KEY_ID",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AZURE_CLIENT_SECRET",
            "OPENAI_API_KEY",
            "CODEX_HOME",
            "KIMI_API_KEY",
            "OPENCLAW_GATEWAY_TOKEN",
            bridge.CONFIG_HOME_ENV,
            bridge.PROCESS_HOME_ENV,
            bridge.BINARY_ENV,
        ):
            self.assertNotIn(name, env)

    def test_bridge_rejects_cross_account_home_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _private_dir(root / "first")
            second = _private_dir(root / "second")
            binary = _executable(root / "claude")
            with self.assertRaisesRegex(
                bridge.BridgeConfigurationError, "same account directory"
            ):
                bridge.build_exec_environment(
                    {
                        bridge.CONFIG_HOME_ENV: str(first),
                        bridge.PROCESS_HOME_ENV: str(second),
                        bridge.BINARY_ENV: str(binary),
                    }
                )

    def test_bridge_rejects_account_home_visible_to_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = _private_dir(root / "account")
            account.chmod(0o750)
            binary = _executable(root / "claude")
            with self.assertRaisesRegex(
                bridge.BridgeConfigurationError, "group or others"
            ):
                bridge.build_exec_environment(
                    {
                        bridge.CONFIG_HOME_ENV: str(account),
                        bridge.PROCESS_HOME_ENV: str(account),
                        bridge.BINARY_ENV: str(binary),
                    }
                )


    def test_production_zero_token_assistant_diagnostic_marks_reauth(self) -> None:
        diagnostic = {
            "type": "assistant",
            "message": {
                "model": "claude-haiku-4-5",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Failed to authenticate. API Error: 401 OAuth access "
                            "token has expired. Re-authenticate to continue."
                        ),
                    }
                ],
            },
        }
        terminal = {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": diagnostic["message"]["content"][0]["text"],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(bridge.claude_login, "mark_reauth_required") as mark,
            patch.object(bridge.claude_login, "clear_reauth_required") as clear,
        ):
            account = Path(tmp)
            attestor = bridge._TurnAttestor(account)
            attestor.observe_stdout(json.dumps(diagnostic) + "\n")
            attestor.observe_stdout(json.dumps(terminal) + "\n")

        mark.assert_called_once_with(account, reason="oauth_refresh_failed")
        clear.assert_not_called()

    def test_real_model_activity_prevents_false_reauth_marker(self) -> None:
        assistant = {
            "type": "assistant",
            "message": {
                "model": "claude-haiku-4-5-20251001",
                "usage": {"output_tokens": 1},
                "content": [{"type": "text", "text": "A model response."}],
            },
        }
        terminal = {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "401 OAuth access token has expired.",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(bridge.claude_login, "mark_reauth_required") as mark,
            patch.object(bridge.claude_login, "clear_reauth_required") as clear,
        ):
            attestor = bridge._TurnAttestor(Path(tmp))
            attestor.observe_stdout(json.dumps(assistant) + "\n")
            attestor.observe_stdout(json.dumps(terminal) + "\n")

        mark.assert_not_called()
        clear.assert_not_called()

    def test_only_attested_positive_usage_success_clears_marker(self) -> None:
        success = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Completed.",
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "modelUsage": {
                "claude-haiku-4-5-20251001": {
                    "canonicalModel": "claude-haiku-4-5-20251001"
                }
            },
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(bridge.claude_login, "mark_reauth_required") as mark,
            patch.object(bridge.claude_login, "clear_reauth_required") as clear,
        ):
            account = Path(tmp)
            attestor = bridge._TurnAttestor(account)
            attestor.observe_stdout(json.dumps(success) + "\n")

        clear.assert_called_once_with(account)
        mark.assert_not_called()

    def test_marker_preflight_blocks_openclaw_before_process_start(self) -> None:
        with (
            patch.object(
                bridge,
                "build_exec_environment",
                return_value=(
                    Path("/bin/false"),
                    {"CLAUDE_CONFIG_DIR": "/private/account"},
                ),
            ),
            patch.object(
                bridge.claude_login, "account_login_ready", return_value=False
            ) as ready,
            patch.object(bridge, "_run_official") as run,
            patch.object(bridge.sys, "stderr", io.StringIO()) as stderr,
        ):
            rc = bridge.main()

        self.assertEqual(rc, 77)
        self.assertIn("reauthentication", stderr.getvalue())
        ready.assert_called_once_with(Path("/private/account"))
        run.assert_not_called()

class ClaudeOpenClawConfigurationTests(unittest.TestCase):
    def test_config_pins_exact_haiku_native_runtime_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = _private_dir(root / "account")
            workspace = _private_dir(root / "workspace")
            state = account / "openclaw" / "runs" / "one"
            binary = _executable(root / "claude")
            (account / ".credentials.json").write_text(
                "never-copy-this-secret", encoding="utf-8"
            )
            with patch(
                "agent_monitor.runners.openclaw_runner.secrets.token_urlsafe",
                return_value="local-token",
            ):
                config_path, model_ref = openclaw_runner._write_claude_subscription_config(
                    state,
                    workspace,
                    model_id="claude-haiku-4-5",
                    account_home=account,
                    claude_binary=binary,
                )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config_mode = config_path.stat().st_mode & 0o777
            state_mode = state.stat().st_mode & 0o777
            account_path = str(account.resolve())
            binary_path = str(binary.resolve())

        defaults = config["agents"]["defaults"]
        backend = defaults["cliBackends"]["claude-cli"]
        self.assertEqual(model_ref, "anthropic/claude-haiku-4-5")
        self.assertEqual(
            defaults["model"],
            {"primary": model_ref, "fallbacks": []},
        )
        self.assertEqual(
            defaults["models"],
            {model_ref: {"agentRuntime": {"id": "claude-cli"}}},
        )
        self.assertEqual(
            backend["command"],
            str(
                Path(openclaw_runner.__file__)
                .with_name("openclaw_claude_cli_bridge.py")
                .resolve()
            ),
        )
        self.assertEqual(backend["env"][bridge.CONFIG_HOME_ENV], account_path)
        self.assertEqual(backend["env"][bridge.BINARY_ENV], binary_path)
        self.assertIn("ANTHROPIC_API_KEY", backend["clearEnv"])
        self.assertIn("OPENAI_API_KEY", backend["clearEnv"])
        self.assertEqual(config["tools"]["exec"]["mode"], "allowlist")
        self.assertNotIn("never-copy-this-secret", json.dumps(config))
        self.assertEqual(config_mode, 0o600)
        self.assertEqual(state_mode, 0o700)


class ClaudeOpenClawModelAttestationTests(unittest.TestCase):
    @staticmethod
    def _session_file(state: Path, key: str) -> Path:
        path = state / "agents" / "main" / "sessions" / "session.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".trajectory.jsonl").write_text(
            json.dumps({"sessionKey": key}) + "\n", encoding="utf-8"
        )
        return path

    def test_attestation_accepts_exact_haiku_release_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            key = "agent:main:run"
            path = self._session_file(state, key)
            path.write_text(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "provider": "claude-cli",
                            "model": "claude-haiku-4-5-20251001",
                            "usage": {
                                "modelUsage": [
                                    {
                                        "provider": "anthropic",
                                        "model": "claude-haiku-4-5-20251001",
                                    }
                                ]
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            records = openclaw_runner._attest_claude_session_model(
                state,
                {},
                session_key=key,
                requested="claude-haiku-4-5",
            )

        self.assertEqual(
            records,
            (
                ("claude-cli", "claude-haiku-4-5-20251001"),
                ("anthropic", "claude-haiku-4-5-20251001"),
            ),
        )

    def test_attestation_uses_current_session_index_and_normalized_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            requested_key = "agent:main:Run-One"
            session_id = "895ca7cb-4524-4242-b228-0e41fe5c3e06"
            path = (
                state / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "provider": "claude-cli",
                            "model": "claude-haiku-4-5",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (path.parent / "sessions.json").write_text(
                json.dumps(
                    {
                        requested_key.casefold(): {
                            "sessionId": session_id,
                            "sessionFile": str(path),
                        }
                    }
                ),
                encoding="utf-8",
            )

            records = openclaw_runner._attest_claude_session_model(
                state,
                {},
                session_key=requested_key,
                requested="claude-haiku-4-5",
            )

        self.assertEqual(records, (("claude-cli", "claude-haiku-4-5"),))

    def test_attestation_ignores_old_session_bytes_and_rejects_new_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            key = "agent:main:run"
            path = self._session_file(state, key)
            old = {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "provider": "anthropic",
                    "model": "claude-opus-4-1",
                },
            }
            path.write_text(json.dumps(old) + "\n", encoding="utf-8")
            offsets = {path: path.stat().st_size}
            good = {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5-20251001",
                },
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(good) + "\n")
            openclaw_runner._attest_claude_session_model(
                state,
                offsets,
                session_key=key,
                requested="claude-haiku-4-5",
            )

            new_offsets = {path: path.stat().st_size}
            fallback = {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-5-20250929",
                },
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(fallback) + "\n")
            with self.assertRaisesRegex(ValueError, "model mismatch"):
                openclaw_runner._attest_claude_session_model(
                    state,
                    new_offsets,
                    session_key=key,
                    requested="claude-haiku-4-5",
                )


class ClaudeOpenClawRoutingTests(unittest.TestCase):
    def test_formal_harness_preserves_claude_route_for_openclaw(self) -> None:
        route_env = {
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "CLAUDE_CONFIG_DIR": "/private/account",
            "HOME": "/private/account",
        }
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch("agent_monitor.settings.account_runtime", return_value="claude"),
            patch(
                "agent_monitor.jobs._configure_auth_route",
                return_value=route_env.copy(),
            ) as configure,
            patch("agent_monitor.lean_verify.toolchain_status", return_value={}),
        ):
            env = lean_verify._harness_env(
                "openclaw",
                {"id": 7},
                model="claude-haiku-4-5",
            )

        configure.assert_called_once()
        self.assertEqual(
            configure.call_args.kwargs,
            {"route": "claude_subscription", "user_id": 7},
        )
        self.assertIsInstance(configure.call_args.args[0], dict)
        self.assertEqual(env["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"], "1")
        self.assertEqual(env["AGENT_MONITOR_CLAUDE_MODEL"], "claude-haiku-4-5")
        self.assertEqual(env["AGENT_MONITOR_CLAUDE_LEAN_MODE"], "1")

    def test_supplemental_audit_selects_claude_subscription_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text(
                "candidate " * 20, encoding="utf-8"
            )
            with (
                patch.object(
                    openclaw_runner,
                    "_validated_audit_contract_exists",
                    return_value=False,
                ),
                patch(
                    "agent_monitor.runners.codex_backend.subscription_enabled",
                    return_value=False,
                ),
                patch(
                    "agent_monitor.runners.claude_backend.subscription_enabled",
                    return_value=True,
                ),
                patch(
                    "agent_monitor.runners.deepseek_harness_runner.run_codex_audit",
                    return_value="audited candidate",
                ) as run_audit,
                patch("builtins.print"),
            ):
                changed = openclaw_runner._run_supplemental_audit(
                    workspace,
                    "CANDIDATE AUDIT GATE:",
                    "claude-haiku-4-5",
                )

        self.assertTrue(changed)
        self.assertIs(
            run_audit.call_args.kwargs["call"],
            openclaw_runner._claude_audit_call,
        )
        self.assertEqual(
            run_audit.call_args.kwargs["model"], "claude-haiku-4-5"
        )
    def test_main_uses_native_haiku_route_and_attests_new_session(self) -> None:
        captured: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = _private_dir(root / "account")
            workspace = _private_dir(root / "workspace")
            claude_binary = _executable(root / "claude")
            key = "agent:main:run-one"

            def which(name: str) -> str | None:
                if name == "openclaw":
                    return "/mock/openclaw"
                if name == "claude":
                    return str(claude_binary)
                return None

            def watchdog(
                argv: list[str],
                state_dir: Path,
                *,
                grace_s: float,
                session_key: str | None,
            ) -> int:
                captured["argv"] = list(argv)
                captured["config"] = json.loads(
                    (state_dir / "openclaw.json").read_text(encoding="utf-8")
                )
                session = (
                    state_dir / "agents" / "main" / "sessions" / "session.jsonl"
                )
                session.parent.mkdir(parents=True, exist_ok=True)
                session.write_text(
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "assistant",
                                "provider": "claude-cli",
                                "model": "claude-haiku-4-5-20251001",
                                "stopReason": "stop",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "A substantive terminal proof candidate.",
                                    }
                                ],
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self.assertEqual(session_key, key)
                self.assertGreaterEqual(grace_s, 2.0)
                return 0

            selected_env = {
                "HOME": str(account),
                "CLAUDE_CONFIG_DIR": str(account),
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_MODEL": "claude-haiku-4-5",
                "ANTHROPIC_API_KEY": "must-be-scrubbed",
                "OPENAI_API_KEY": "must-be-scrubbed",
                "AWS_ACCESS_KEY_ID": "must-be-scrubbed",
            }
            with (
                patch.dict(os.environ, selected_env, clear=True),
                patch.object(
                    openclaw_runner.sys,
                    "argv",
                    ["openclaw_runner.py", "prove something", "run-one"],
                ),
                patch.object(openclaw_runner.Path, "cwd", return_value=workspace),
                patch.object(openclaw_runner.shutil, "which", side_effect=which),
                patch.object(
                    openclaw_runner, "_bind_workspace_tool_python", return_value=[]
                ),
                patch.object(
                    openclaw_runner, "_run_with_terminal_watchdog", side_effect=watchdog
                ),
                patch.object(openclaw_runner, "_session_key", return_value=key),
                patch.object(openclaw_runner, "_persist_terminal_candidate"),
                patch.object(
                    openclaw_runner, "_recover_checkpointed_proof", return_value=False
                ),
                patch.object(openclaw_runner, "_normalize_requested_outcome"),
                patch.object(openclaw_runner, "_run_supplemental_audit") as audit,
                patch("builtins.print"),
            ):
                returncode = openclaw_runner.main()
                self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
                self.assertNotIn("OPENAI_API_KEY", os.environ)
                self.assertNotIn("AWS_ACCESS_KEY_ID", os.environ)

        self.assertEqual(returncode, 0)
        argv = captured["argv"]
        assert isinstance(argv, list)
        model_index = argv.index("--model")
        self.assertEqual(argv[model_index + 1], "anthropic/claude-haiku-4-5")
        config = captured["config"]
        assert isinstance(config, dict)
        defaults = config["agents"]["defaults"]
        self.assertEqual(
            defaults["model"],
            {
                "primary": "anthropic/claude-haiku-4-5",
                "fallbacks": [],
            },
        )
        self.assertEqual(
            defaults["models"]["anthropic/claude-haiku-4-5"]["agentRuntime"],
            {"id": "claude-cli"},
        )
        audit.assert_called_once_with(
            workspace, "prove something", "claude-haiku-4-5"
        )

    def test_main_rejects_conflicting_account_routes_before_launch(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                },
                clear=True,
            ),
            patch.object(
                openclaw_runner.sys,
                "argv",
                ["openclaw_runner.py", "prove something", "run-one"],
            ),
            patch.object(
                openclaw_runner.shutil, "which", return_value="/mock/openclaw"
            ),
            patch.object(
                openclaw_runner, "_run_with_terminal_watchdog"
            ) as watchdog,
            patch("builtins.print"),
        ):
            returncode = openclaw_runner.main()

        self.assertEqual(returncode, 78)
        watchdog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
