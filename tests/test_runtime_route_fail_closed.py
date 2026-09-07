from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs, lean_verify, proof_graph, settings


_PLAIN = {
    "id": "plain",
    "label": "Plain",
    "available": True,
    "auth_modes": ["api_key", "codex_subscription", "claude_subscription"],
}


class RuntimeRouteFailClosedTests(unittest.TestCase):
    def tearDown(self) -> None:
        with jobs._LOCK:
            jobs._JOBS.clear()

    def test_claude_runtime_catalog_excludes_configured_api_models(self) -> None:
        env = {
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
            "AGENT_MONITOR_MODEL": "kimi-k3",
            "KIMI_API_KEY": "configured-kimi",
            "ANTHROPIC_API_KEY": "configured-anthropic",
        }
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch("agent_monitor.codex_login.account_login_ready", return_value=True),
            patch("agent_monitor.codex_login.available_models", return_value=["gpt-account"]),
            patch("agent_monitor.claude_login.account_login_ready", return_value=True),
        ):
            result = settings.get_settings({"id": 17, "is_admin": False})

        self.assertEqual(result["account_runtime"], "claude")
        self.assertEqual(result["model_presets"], result["claude_models"])
        self.assertEqual(result["active_models"], result["claude_models"])
        self.assertEqual(result["default_model"], "sonnet")
        self.assertIn("kimi-k3", result["models_by_runtime"]["api"])
        self.assertNotIn("kimi-k3", result["model_presets"])

    def test_disconnected_claude_runtime_is_not_reported_as_api(self) -> None:
        env = {
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
            "OPENAI_API_KEY": "configured-but-dormant",
        }
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch("agent_monitor.codex_login.account_login_ready", return_value=False),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
        ):
            result = settings.get_settings({"id": 17, "is_admin": False})

        self.assertEqual(result["auth_mode"], "claude_subscription")
        self.assertFalse(result["runtime_ready"])
        self.assertEqual(result["active_models"], [])
        self.assertFalse(result["api_providers_active"])

    def test_subscription_environment_scrubs_all_provider_routes(self) -> None:
        polluted = {name: f"secret-{index}" for index, name in enumerate(jobs._PROVIDER_ROUTE_ENV_NAMES)}
        polluted["PATH"] = "/bin"
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("agent_monitor.claude_login.account_home", return_value=Path(tmp)),
            patch("agent_monitor.claude_login.account_login_ready", return_value=True),
        ):
            configured = jobs._configure_auth_route(
                polluted, route="claude_subscription", user_id=17
            )

        for name in jobs._PROVIDER_ROUTE_ENV_NAMES:
            self.assertNotIn(name, configured)
        self.assertEqual(configured["AGENT_MONITOR_AUTH_MODE"], "claude_subscription")

    def test_stale_kimi_model_cannot_override_claude_runtime(self) -> None:
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_PLAIN)]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(
                jobs,
                "_user_extra_env",
                return_value={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
                    "KIMI_API_KEY": "configured-but-dormant",
                },
            ),
            patch.object(jobs, "_configure_auth_route") as configure,
            patch.object(jobs.threading, "Thread") as thread,
        ):
            with self.assertRaisesRegex(ValueError, "Kimi K3.*cannot use"):
                jobs.start_job(
                    engine="plain",
                    problem_text="Prove one equals one.",
                    model="kimi-k3",
                    user={"id": 17},
                )
        configure.assert_not_called()
        thread.assert_not_called()

    def test_stale_api_subagent_model_is_rejected_under_claude(self) -> None:
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_PLAIN)]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(
                jobs,
                "_user_extra_env",
                return_value={"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"},
            ),
            patch.object(jobs, "_configure_auth_route") as configure,
            patch.object(jobs.threading, "Thread") as thread,
        ):
            with self.assertRaisesRegex(ValueError, "Unsupported Claude subagent model"):
                jobs.start_job(
                    engine="plain",
                    problem_text="Prove one equals one.",
                    model="sonnet",
                    use_subagents=True,
                    subagent_model="kimi-k3",
                    user={"id": 17},
                )
        configure.assert_not_called()
        thread.assert_not_called()

    def test_stale_api_main_model_is_rejected_under_codex(self) -> None:
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_PLAIN)]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(
                jobs,
                "_user_extra_env",
                return_value={"AGENT_MONITOR_ACCOUNT_RUNTIME": "codex"},
            ),
            patch("agent_monitor.codex_login.available_models", return_value=["gpt-account"]),
            patch.object(jobs, "_configure_auth_route") as configure,
            patch.object(jobs.threading, "Thread") as thread,
        ):
            with self.assertRaisesRegex(ValueError, "Unsupported Codex account model"):
                jobs.start_job(
                    engine="plain",
                    problem_text="Prove one equals one.",
                    model="claude-api-model",
                    user={"id": 17},
                )
        configure.assert_not_called()
        thread.assert_not_called()

    def test_incompatible_engine_does_not_fall_back_to_api(self) -> None:
        api_only = {
            "id": "ucla",
            "label": "UCLA",
            "available": True,
            "auth_modes": ["api_key"],
        }
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"ucla"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[api_only]),
            patch.object(
                jobs,
                "_user_extra_env",
                return_value={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
                    "OPENAI_API_KEY": "configured-but-dormant",
                },
            ),
            patch.object(jobs, "_configure_auth_route") as configure,
        ):
            with self.assertRaisesRegex(ValueError, "selected Claude Code subscription"):
                jobs.start_job(
                    engine="ucla",
                    problem_text="Prove one equals one.",
                    user={"id": 17},
                )
        configure.assert_not_called()

    def test_stale_explicit_route_conflicting_with_settings_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts with the selected claude"):
            jobs._selected_auth_route(
                requested="api_key",
                engine="plain",
                model="sonnet",
                auth_modes=set(_PLAIN["auth_modes"]),
                user={"id": 17},
                env={"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"},
            )

    def test_stale_explicit_route_conflicts_with_legacy_claude_flag(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts with the selected claude"):
            jobs._selected_auth_route(
                requested="api_key",
                engine="plain",
                model="sonnet",
                auth_modes=set(_PLAIN["auth_modes"]),
                user={"id": 17},
                env={
                    "AGENT_MONITOR_USE_CLAUDE": "1",
                    "AGENT_MONITOR_USE_CODEX": "0",
                },
            )

    def test_recorded_codex_dag_never_calls_direct_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "problem.txt").write_text("Prove one equals one.", encoding="utf-8")
            (workspace / "proof.md").write_text("Assume equality. Therefore one equals one.", encoding="utf-8")
            graph_json = '{"title":"demo","nodes":[{"id":"n1","kind":"conclusion","label":"done","statement":"1=1","citations":[]}],"edges":[]}'
            with (
                patch.object(settings, "resolved_user_env", return_value={"OPENAI_API_KEY": "must-not-run"}),
                patch.object(proof_graph, "_run_codex_prompt", return_value=graph_json) as codex,
                patch.object(proof_graph, "_call_llm") as direct_api,
            ):
                result = proof_graph.generate(
                    workspace=workspace,
                    run_record={"auth_route": "codex_subscription", "owner_id": 17},
                    user={"id": 17},
                    model=None,
                    allow_codex_fallback=False,
                )

        self.assertEqual(result["model"], "codex")
        codex.assert_called_once()
        direct_api.assert_not_called()

    def test_recorded_codex_lean_chat_never_calls_direct_api(self) -> None:
        env = {"AGENT_MONITOR_CODEX_SUBSCRIPTION": "1", "CODEX_HOME": "/private/codex"}
        with (
            patch("agent_monitor.proof_graph._run_codex_prompt_env", return_value='{"lean":"theorem main : True := by trivial","uses_mathlib":false}') as codex,
            patch.object(lean_verify, "_call_llm") as direct_api,
        ):
            selected, content = lean_verify._chat(env, None, "formalize", timeout=5)

        self.assertEqual(selected, "codex")
        self.assertIn("theorem main", content)
        codex.assert_called_once()
        direct_api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
