"""Offline cross-harness regression matrix for linked Claude Code accounts."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import claude_login, engines_registry, jobs
from agent_monitor.runners import claude_backend
from agent_monitor.runners import improof_claude_worker
from agent_monitor.runners import openclaw_claude_cli_bridge


CLAUDE_MODEL = "claude-haiku-4-5"
COMPATIBLE_ENGINES = (
    "claude",
    "plain",
    "metaharness",
    "improof",
    "math_harness",
    "deepagents",
    "openclaw",
)
UNSUPPORTED_ENGINES = (
    "hermes",
    "codex",
    "openclaude",
    "openhands",
    "deepseek_harness",
    "danus",
)
RUNNER_ROUTE_KEYS = (
    "AGENT_MONITOR_CODEX_SUBSCRIPTION",
    "AGENT_MONITOR_CODEX_MODEL",
    "CODEX_HOME",
    "AGENT_MONITOR_OPENAI_MODEL",
    "PLAIN_MODEL",
    "METAHARNESS_MODEL",
    "DEEPAGENTS_MODEL",
    "AGENT_MONITOR_OPENCLAUDE_MODEL",
    "AGENT_MONITOR_OPENCLAW_MODEL",
    "AGENT_MONITOR_OPENHANDS_MODEL",
)
POISONED_PROVIDER_ENV = {
    "ANTHROPIC_API_KEY": "poison-anthropic",
    "ANTHROPIC_AUTH_TOKEN": "poison-anthropic-token",
    "ANTHROPIC_BASE_URL": "https://poison.invalid",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
    "AWS_ACCESS_KEY_ID": "poison-aws",
    "AWS_SECRET_ACCESS_KEY": "poison-aws-secret",
    "GOOGLE_APPLICATION_CREDENTIALS": "/poison/google.json",
    "AZURE_CLIENT_SECRET": "poison-azure",
    "OPENAI_API_KEY": "poison-openai",
    "CODEX_API_KEY": "poison-codex",
    "CODEX_HOME": "/poison/codex",
    "KIMI_API_KEY": "poison-kimi",
    "OPENROUTER_API_KEY": "poison-openrouter",
}


class ClaudeCompatibilityMatrixTests(unittest.TestCase):
    def test_registry_jobs_and_auth_modes_expose_exact_genuine_matrix(self) -> None:
        expected = set(COMPATIBLE_ENGINES)
        self.assertEqual(set(engines_registry.CLAUDE_SUBSCRIPTION_ENGINES), expected)
        self.assertEqual(set(jobs._CLAUDE_SUBSCRIPTION_ENGINES), expected)

        for engine in COMPATIBLE_ENGINES:
            with self.subTest(engine=engine):
                self.assertIn(
                    "claude_subscription",
                    engines_registry.ENGINE_AUTH_MODES[engine],
                )

        for engine in UNSUPPORTED_ENGINES:
            with self.subTest(engine=engine):
                self.assertNotIn(engine, engines_registry.CLAUDE_SUBSCRIPTION_ENGINES)
                self.assertNotIn(engine, jobs._CLAUDE_SUBSCRIPTION_ENGINES)
                self.assertNotIn(
                    "claude_subscription",
                    engines_registry.ENGINE_AUTH_MODES[engine],
                )

        # Multi-provider harnesses intentionally keep an open provider catalog.
        # Claude validation is route-specific in jobs.start_job and therefore
        # cannot restrict valid Codex or API model selections.
        for engine in set(COMPATIBLE_ENGINES) - {"claude"}:
            with self.subTest(multi_provider_engine=engine):
                self.assertEqual(engines_registry.supported_models(engine), [])

    def test_exact_haiku_selection_propagates_without_api_or_codex_model_routes(self) -> None:
        selected_route = {
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "AGENT_MONITOR_AUTH_MODE": "claude_subscription",
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
            "AGENT_MONITOR_USE_CLAUDE": "1",
            "AGENT_MONITOR_USE_CODEX": "0",
        }
        for engine in COMPATIBLE_ENGINES:
            with self.subTest(engine=engine):
                env = dict(selected_route)
                jobs._apply_selected_model_env(
                    env,
                    engine=engine,
                    model=CLAUDE_MODEL,
                )
                self.assertEqual(env["AGENT_MONITOR_SELECTED_MODEL"], CLAUDE_MODEL)
                self.assertEqual(env["AGENT_MONITOR_CLAUDE_MODEL"], CLAUDE_MODEL)
                for name in RUNNER_ROUTE_KEYS:
                    self.assertNotIn(name, env)

    def test_configuring_claude_route_removes_conflicting_codex_account_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_home = Path(tmp)
            polluted = {
                "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CODEX_MODEL": "gpt-5.6-sol",
                "CODEX_HOME": "/poison/codex",
                "ANTHROPIC_API_KEY": "poison-anthropic",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_ACCESS_KEY_ID": "poison-aws",
            }
            with (
                patch.object(claude_login, "account_home", return_value=account_home),
                patch.object(claude_login, "account_login_ready", return_value=True),
            ):
                configured = jobs._configure_auth_route(
                    polluted,
                    route="claude_subscription",
                    user_id=17,
                )

        self.assertEqual(configured["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"], "1")
        self.assertEqual(configured["AGENT_MONITOR_AUTH_MODE"], "claude_subscription")
        self.assertEqual(configured["CLAUDE_CONFIG_DIR"], str(account_home))
        for name in (
            "AGENT_MONITOR_CODEX_SUBSCRIPTION",
            "AGENT_MONITOR_CODEX_MODEL",
            "CODEX_HOME",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_ACCESS_KEY_ID",
        ):
            self.assertNotIn(name, configured)

    def test_unsupported_explicit_route_fails_before_account_or_runner_setup(self) -> None:
        for engine in UNSUPPORTED_ENGINES:
            engine_info = {
                "id": engine,
                "label": engine,
                "available": True,
                "auth_modes": engines_registry.ENGINE_AUTH_MODES[engine],
            }
            with (
                self.subTest(engine=engine),
                patch(
                    "agent_monitor.engines_registry.list_engines",
                    return_value=[engine_info],
                ),
                patch.object(jobs, "_configure_auth_route") as configure,
                patch.object(jobs.threading, "Thread") as thread,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "does not support claude subscription",
                ):
                    jobs.start_job(
                        engine=engine,
                        problem_text="Prove one equals one.",
                        model=CLAUDE_MODEL,
                        auth_route="claude_subscription",
                    )
                configure.assert_not_called()
                thread.assert_not_called()

    def test_unknown_claude_models_fail_before_account_or_runner_setup(self) -> None:
        malicious_models = (
            "claude-unknown",
            "claude-haiku-4-5; touch /tmp/not-allowed",
            "anthropic/claude-haiku-4-5",
        )
        for engine in COMPATIBLE_ENGINES:
            engine_info = {
                "id": engine,
                "label": engine,
                "available": True,
                "auth_modes": engines_registry.ENGINE_AUTH_MODES[engine],
            }
            for malicious in malicious_models:
                with (
                    self.subTest(engine=engine, model=malicious),
                    patch(
                        "agent_monitor.engines_registry.list_engines",
                        return_value=[engine_info],
                    ),
                    patch.object(jobs, "_configure_auth_route") as configure,
                    patch.object(jobs.threading, "Thread") as thread,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "Unsupported Claude account model",
                    ):
                        jobs.start_job(
                            engine=engine,
                            problem_text="Prove one equals one.",
                            model=malicious,
                            auth_route="claude_subscription",
                        )
                    configure.assert_not_called()
                    thread.assert_not_called()


class ClaudeNoFallbackMatrixTests(unittest.TestCase):
    def test_shared_response_runtime_scrubs_every_poisoned_api_and_cloud_route(self) -> None:
        # Direct Claude, Plain, Meta-Harness, and DeepAgents all delegate their
        # Claude turns to this shared official-CLI environment boundary.
        with tempfile.TemporaryDirectory() as tmp:
            source = {
                "PATH": "/bin",
                "HOME": tmp,
                "CLAUDE_CONFIG_DIR": tmp,
                **POISONED_PROVIDER_ENV,
            }
            selected = claude_backend._claude_environment(source)

        self.assertEqual(selected["CLAUDE_CONFIG_DIR"], str(Path(tmp).resolve()))
        for name in POISONED_PROVIDER_ENV:
            self.assertNotIn(name, selected)

    def test_improof_worker_allowlist_scrubs_every_poisoned_provider_route(self) -> None:
        source = {
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "AGENT_MONITOR_CLAUDE_MODEL": CLAUDE_MODEL,
            "CLAUDE_CONFIG_DIR": "/private/account",
            "PATH": "/bin",
            **POISONED_PROVIDER_ENV,
        }
        selected = improof_claude_worker._source_environment(source)

        self.assertEqual(selected["AGENT_MONITOR_CLAUDE_MODEL"], CLAUDE_MODEL)
        self.assertEqual(selected["CLAUDE_CONFIG_DIR"], "/private/account")
        for name in POISONED_PROVIDER_ENV:
            self.assertNotIn(name, selected)

    def test_openclaw_bridge_scrubs_every_poisoned_provider_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = root / "account"
            account.mkdir(mode=0o700)
            account.chmod(0o700)
            binary = root / "claude"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o700)
            source = {
                openclaw_claude_cli_bridge.CONFIG_HOME_ENV: str(account),
                openclaw_claude_cli_bridge.PROCESS_HOME_ENV: str(account),
                openclaw_claude_cli_bridge.BINARY_ENV: str(binary),
                "PATH": "/bin",
                **POISONED_PROVIDER_ENV,
            }
            selected_binary, selected = (
                openclaw_claude_cli_bridge.build_exec_environment(source)
            )

        self.assertEqual(selected_binary, binary.resolve())
        self.assertEqual(selected["CLAUDE_CONFIG_DIR"], str(account.resolve()))
        for name in POISONED_PROVIDER_ENV:
            self.assertNotIn(name, selected)


if __name__ == "__main__":
    unittest.main()
