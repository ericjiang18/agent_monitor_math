from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import auto_pipeline, lean_verify, proof_graph


_GRAPH_JSON = json.dumps(
    {
        "title": "Test",
        "nodes": [
            {
                "id": "n1",
                "kind": "conclusion",
                "label": "Conclusion",
                "statement": "The claim follows.",
                "citations": ["self-derived"],
            }
        ],
        "edges": [],
    }
)


class ClaudeLeanDagTests(unittest.TestCase):
    def test_lean_environment_honors_recorded_claude_route_and_model(self) -> None:
        configured = {
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "CLAUDE_CONFIG_DIR": "/tmp/claude-user",
        }
        with (
            patch.dict(
                os.environ,
                {proof_graph.CLAUDE_DERIVED_TIMEOUT_ENV: "750"},
                clear=False,
            ),
            patch("agent_monitor.settings.resolved_user_env", return_value={"OPENAI_API_KEY": "x"}),
            patch("agent_monitor.settings.account_runtime", return_value="api"),
            patch("agent_monitor.jobs._configure_auth_route", return_value=configured) as route,
        ):
            env = lean_verify._llm_environment(
                {"id": 7},
                {"auth_route": "claude_subscription", "model": "claude-haiku-4-5"},
            )

        self.assertEqual(env["AGENT_MONITOR_CLAUDE_MODEL"], "claude-haiku-4-5")
        self.assertEqual(env[proof_graph.CLAUDE_DERIVED_TIMEOUT_ENV], "750")
        self.assertEqual(route.call_args.kwargs["route"], "claude_subscription")
        self.assertEqual(route.call_args.kwargs["user_id"], 7)

    def test_lean_chat_uses_native_claude_without_direct_api_fallback(self) -> None:
        response = SimpleNamespace(text='{"lean":"theorem main : True := by trivial"}', model="claude-sonnet")
        with (
            patch(
                "agent_monitor.runners.claude_backend.claude_exec",
                return_value=response,
            ) as claude,
            patch.object(lean_verify, "_call_llm") as direct_api,
        ):
            model, content = lean_verify._chat(
                {
                    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                    "CLAUDE_CONFIG_DIR": "/tmp/claude-user",
                },
                "sonnet",
                "formalize",
            )

        self.assertEqual(model, "claude-sonnet")
        self.assertIn("theorem main", content)
        self.assertEqual(claude.call_args.kwargs["model"], "sonnet")
        self.assertEqual(claude.call_args.kwargs["timeout"], 600.0)
        direct_api.assert_not_called()

    def test_claude_derived_timeout_override_is_bounded(self) -> None:
        response = SimpleNamespace(text="{}", model="claude-fable")
        env = {
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "CLAUDE_CONFIG_DIR": "/tmp/claude-user",
            proof_graph.CLAUDE_DERIVED_TIMEOUT_ENV: "7200",
        }
        with patch(
            "agent_monitor.runners.claude_backend.claude_exec",
            return_value=response,
        ) as claude:
            lean_verify._chat(env, "fable", "formalize")

        self.assertEqual(
            claude.call_args.kwargs["timeout"],
            proof_graph.MAX_CLAUDE_DERIVED_TIMEOUT,
        )
        self.assertEqual(
            proof_graph._claude_derived_timeout(
                {proof_graph.CLAUDE_DERIVED_TIMEOUT_ENV: "1"}
            ),
            proof_graph.MIN_CLAUDE_DERIVED_TIMEOUT,
        )

    def test_invalid_claude_derived_timeout_fails_before_model_call(self) -> None:
        env = {
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "CLAUDE_CONFIG_DIR": "/tmp/claude-user",
            proof_graph.CLAUDE_DERIVED_TIMEOUT_ENV: "not-a-timeout",
        }
        with (
            patch(
                "agent_monitor.runners.claude_backend.claude_exec"
            ) as claude,
            patch.object(lean_verify, "_call_llm") as direct_api,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "AGENT_MONITOR_CLAUDE_DERIVED_TIMEOUT must be a finite number",
            ):
                lean_verify._chat(env, "fable", "formalize")

        claude.assert_not_called()
        direct_api.assert_not_called()

    def test_dag_claude_helper_uses_derived_timeout_override(self) -> None:
        response = SimpleNamespace(text=_GRAPH_JSON, model="claude-fable")

        def configure(env, *, route, user_id):
            return {
                **env,
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "CLAUDE_CONFIG_DIR": "/tmp/claude-user",
            }

        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch(
                "agent_monitor.jobs._configure_auth_route",
                side_effect=configure,
            ),
            patch(
                "agent_monitor.runners.claude_backend.claude_exec",
                return_value=response,
            ) as claude,
        ):
            model, content = proof_graph._run_claude_prompt(
                {"id": 7},
                "build the Formal DAG",
                model="fable",
                source_env={
                    proof_graph.CLAUDE_DERIVED_TIMEOUT_ENV: "825",
                },
            )

        self.assertEqual((model, content), ("claude-fable", _GRAPH_JSON))
        self.assertEqual(claude.call_args.kwargs["timeout"], 825.0)

    def test_invalid_dag_timeout_fails_before_claude_exec(self) -> None:
        def configure(env, *, route, user_id):
            return {
                **env,
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "CLAUDE_CONFIG_DIR": "/tmp/claude-user",
            }

        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch(
                "agent_monitor.jobs._configure_auth_route",
                side_effect=configure,
            ),
            patch(
                "agent_monitor.runners.claude_backend.claude_exec"
            ) as claude,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "AGENT_MONITOR_CLAUDE_DERIVED_TIMEOUT must be a finite number",
            ):
                proof_graph._run_claude_prompt(
                    {"id": 7},
                    "build the Formal DAG",
                    model="fable",
                    source_env={
                        proof_graph.CLAUDE_DERIVED_TIMEOUT_ENV: "NaN",
                    },
                )

        claude.assert_not_called()

    def test_lean_api_and_codex_default_timeouts_are_unchanged(self) -> None:
        with patch.object(
            lean_verify,
            "_call_llm",
            return_value=("api-model", "api response", "openai"),
        ) as direct_api:
            lean_verify._chat({"OPENAI_API_KEY": "test"}, "api-model", "formalize")
        self.assertEqual(direct_api.call_args.kwargs["timeout"], 240.0)

        with patch.object(
            proof_graph,
            "_run_codex_prompt_env",
            return_value="codex response",
        ) as codex:
            lean_verify._chat(
                {
                    "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                    "CODEX_HOME": "/tmp/codex-user",
                },
                "gpt-5.6-sol",
                "formalize",
            )
        self.assertEqual(codex.call_args.kwargs["timeout"], 240.0)

    def test_claude_formal_harness_is_available_for_selected_connected_account(self) -> None:
        env = {"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"}
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value=env),
            patch("agent_monitor.claude_login.account_login_ready", return_value=True),
            patch("agent_monitor.codex_login.account_login_ready") as codex_ready,
            patch("agent_monitor.engines_registry._cli_available", return_value=(True, "cmd")),
        ):
            engines = lean_verify.harness_engines({"id": 7})

        claude = next(item for item in engines if item["id"] == "claude")
        self.assertTrue(claude["available"])
        self.assertEqual(claude["auth_detail"], "Claude Code subscription")
        self.assertEqual(engines[0]["id"], "claude")
        codex_ready.assert_not_called()

    def test_claude_formal_harness_environment_requires_lean_artifact(self) -> None:
        def configure(env, *, route, user_id):
            return {
                **env,
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_AUTH_MODE": route,
                "CLAUDE_CONFIG_DIR": "/tmp/claude-user",
            }

        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch("agent_monitor.jobs._configure_auth_route", side_effect=configure),
            patch.object(
                lean_verify,
                "toolchain_status",
                return_value={"lean": "/bin/lean", "lake": None, "elan": None},
            ),
        ):
            env = lean_verify._harness_env(
                "claude",
                {"id": 7},
                model="fable",
                auth_route="claude_subscription",
            )

        self.assertEqual(env["AGENT_MONITOR_CLAUDE_LEAN_MODE"], "1")
        self.assertEqual(env["AGENT_MONITOR_CLAUDE_MODEL"], "fable")
        self.assertEqual(env["AGENT_MONITOR_AUTH_MODE"], "claude_subscription")

    def test_automatic_dag_uses_recorded_claude_route(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                proof_graph,
                "_run_claude_prompt",
                return_value=("claude-haiku-4-5-20251001", _GRAPH_JSON),
            ) as claude,
            patch.object(proof_graph, "_call_llm") as direct_api,
        ):
            workspace = Path(tmp)
            (workspace / "proof.md").write_text("# Proof\nThe claim follows.", encoding="utf-8")
            result = proof_graph.generate(
                workspace=workspace,
                run_record={
                    "auth_route": "claude_subscription",
                    "model": "claude-haiku-4-5",
                },
                user={"id": 7},
                model=None,
                allow_codex_fallback=False,
            )

        self.assertEqual(result["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(
            claude.call_args.kwargs["model"], "claude-haiku-4-5"
        )
        self.assertNotIn("timeout", claude.call_args.kwargs)
        direct_api.assert_not_called()

    def test_recorded_api_route_never_switches_dag_to_claude(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("agent_monitor.settings.resolved_user_env", return_value={"OPENAI_API_KEY": "x"}),
            patch("agent_monitor.settings.claude_enabled", return_value=True),
            patch.object(
                proof_graph,
                "_call_llm",
                return_value=("api-model", _GRAPH_JSON, "openai"),
            ) as direct_api,
            patch.object(proof_graph, "_run_claude_prompt") as claude,
        ):
            workspace = Path(tmp)
            (workspace / "proof.md").write_text("# Proof\nThe claim follows.", encoding="utf-8")
            result = proof_graph.generate(
                workspace=workspace,
                run_record={"auth_route": "api_key"},
                user={"id": 7},
                model=None,
                allow_codex_fallback=True,
            )

        self.assertEqual(result["model"], "api-model")
        self.assertEqual(direct_api.call_args.kwargs["timeout"], 180)
        claude.assert_not_called()

    def test_automatic_pipeline_preserves_exact_claude_subscription_aliases(self) -> None:
        for alias in ("sonnet", "opus", "fable"):
            self.assertEqual(auto_pipeline._api_model(alias), alias)
        self.assertEqual(auto_pipeline._api_model("claude-sonnet-4-6"), "claude-sonnet-4-6")


if __name__ == "__main__":
    unittest.main()
