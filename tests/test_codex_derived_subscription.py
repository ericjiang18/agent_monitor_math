from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import lean_verify, proof_graph
from agent_monitor.runners import codex_backend
from agent_monitor.runners.codex_backend import CodexBackendError, CodexResult


_GRAPH_JSON = json.dumps(
    {
        "title": "Identity",
        "nodes": [
            {
                "id": "n1",
                "kind": "conclusion",
                "label": "identity",
                "statement": "1 = 1",
                "citations": ["self-derived"],
            }
        ],
        "edges": [],
    }
)


class CodexDerivedSubscriptionTests(unittest.TestCase):
    def test_response_helper_supplies_service_path_when_route_env_omits_it(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION + "\n",
                    stderr="",
                )
            if command[1:] == ["debug", "models", "--bundled"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"models": [{"slug": "gpt-5.6-sol"}]}),
                    stderr="",
                )
            captured["env"] = dict(kwargs["env"])  # type: ignore[arg-type]
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(_GRAPH_JSON, encoding="utf-8")
            terminal = json.dumps({"type": "turn.completed", "usage": {}})
            return subprocess.CompletedProcess(command, 0, stdout=terminal, stderr="")

        source = {
            "CODEX_HOME": "/accounts/codex/user_17",
            "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
        }
        service_path = "/srv/node/bin:/srv/codex/bin:/usr/bin:/bin"
        approved_path = "/approved/node/bin:/approved/codex/bin"
        registry_path = service_path + os.pathsep + approved_path
        with (
            patch.dict(os.environ, {"PATH": service_path}, clear=True),
            patch("agent_monitor.engines_registry.tool_search_path", return_value=registry_path),
            patch(
                "agent_monitor.codex_login.account_login_ready", return_value=True
            ),
            patch.object(
                codex_backend.shutil,
                "which",
                side_effect=lambda name, path=None: (
                    sys.executable if name == "codex" else None
                ),
            ) as which,
            patch.object(codex_backend, "_run_codex_process", side_effect=fake_run),
        ):
            codex_backend.codex_subscription_exec(
                "Return JSON only.",
                "Build the proof graph.",
                source_env=source,
            )

        expected_path = registry_path
        self.assertEqual(captured["env"]["PATH"], expected_path)
        self.assertEqual(which.call_args.kwargs["path"], expected_path)
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", captured["env"])

    def test_response_helper_uses_exact_source_home_and_scrubs_provider_keys(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION + "\n",
                    stderr="",
                )
            if command[1:] == ["debug", "models", "--bundled"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"models": [{"slug": "gpt-5.6-sol"}]}),
                    stderr="",
                )
            captured["command"] = list(command)
            captured["env"] = dict(kwargs["env"])  # type: ignore[arg-type]
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(_GRAPH_JSON, encoding="utf-8")
            terminal = json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            )
            return subprocess.CompletedProcess(command, 0, stdout=terminal, stderr="")

        source = {
            "PATH": "/usr/bin:/bin",
            "CODEX_HOME": "/accounts/codex/user_17",
            "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
            "OPENAI_API_KEY": "dormant-openai",
            "OPENAI_BASE_URL": "https://metered.invalid/v1",
            "ANTHROPIC_API_KEY": "dormant-anthropic",
            "KIMI_API_KEY": "dormant-kimi",
            "KIMI_API_BASE": "https://kimi.invalid/v1",
            "LLM_API_KEY": "dormant-generic",
        }
        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_HOME": "/wrong/global/home",
                    "OPENAI_API_KEY": "wrong-global-key",
                },
                clear=True,
            ),
            patch(
                "agent_monitor.codex_login.account_login_ready", return_value=True
            ) as login_ready,
            patch.object(codex_backend.shutil, "which", return_value=sys.executable),
            patch.object(codex_backend, "_run_codex_process", side_effect=fake_run),
        ):
            result = codex_backend.codex_subscription_exec(
                "Return JSON only.",
                "Build the proof graph.",
                source_env=source,
                model="gpt-5.6-sol",
            )

        child_env = captured["env"]
        self.assertIsInstance(child_env, dict)
        self.assertEqual(child_env["CODEX_HOME"], "/accounts/codex/user_17")
        for name in (
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "ANTHROPIC_API_KEY",
            "KIMI_API_KEY",
            "KIMI_API_BASE",
            "LLM_API_KEY",
        ):
            self.assertNotIn(name, child_env)
        command = captured["command"]
        self.assertIn("gpt-5.6-sol", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertNotIn("danger-full-access", command)
        self.assertEqual(result.usage, {"input_tokens": 10, "output_tokens": 5})
        login_ready.assert_called_once_with(Path("/accounts/codex/user_17"))

    def test_response_helper_requires_explicit_subscription_marker(self) -> None:
        with patch.object(codex_backend, "codex_exec") as raw:
            with self.assertRaisesRegex(
                CodexBackendError, "AGENT_MONITOR_CODEX_SUBSCRIPTION=1"
            ):
                codex_backend.codex_subscription_exec(
                    "system",
                    "user",
                    source_env={
                        "CODEX_HOME": "/accounts/codex/user_17",
                        "OPENAI_API_KEY": "must-not-fallback",
                    },
                )
        raw.assert_not_called()

    def test_response_helper_rejects_api_model_before_process_start(self) -> None:
        with patch.object(codex_backend, "codex_exec") as raw:
            with self.assertRaisesRegex(CodexBackendError, "Kimi is an API model"):
                codex_backend.codex_subscription_exec(
                    "system",
                    "user",
                    source_env={
                        "CODEX_HOME": "/accounts/codex/user_17",
                        "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                        "KIMI_API_KEY": "must-not-run",
                    },
                    model="kimi-k3",
                )
        raw.assert_not_called()

    def test_response_helper_rejects_api_key_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "metered-key"}), encoding="utf-8"
            )
            with patch.object(codex_backend, "codex_exec") as raw:
                with self.assertRaisesRegex(
                    CodexBackendError, "connected Codex subscription login"
                ):
                    codex_backend.codex_subscription_exec(
                        "system",
                        "user",
                        source_env={
                            "CODEX_HOME": str(home),
                            "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                        },
                    )
        raw.assert_not_called()

    def test_recorded_codex_dag_failure_never_falls_back_to_direct_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text(
                "Assume equality. Therefore one equals one.", encoding="utf-8"
            )
            with (
                patch.object(
                    proof_graph,
                    "_run_codex_prompt",
                    side_effect=ValueError("subscription unavailable"),
                ),
                patch.object(proof_graph, "_call_llm") as direct_api,
            ):
                with self.assertRaisesRegex(ValueError, "subscription unavailable"):
                    proof_graph.generate(
                        workspace=workspace,
                        run_record={
                            "auth_route": "codex_subscription",
                            "owner_id": 17,
                            "model": "gpt-5.6-sol",
                        },
                        user={"id": 17},
                        allow_codex_fallback=False,
                    )
        direct_api.assert_not_called()

    def test_recorded_codex_lean_failure_never_falls_back_to_direct_api(self) -> None:
        env = {
            "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
            "CODEX_HOME": "/accounts/codex/user_17",
            "OPENAI_API_KEY": "must-not-run",
        }
        with (
            patch(
                "agent_monitor.proof_graph._run_codex_prompt_env",
                side_effect=ValueError("subscription unavailable"),
            ) as codex,
            patch.object(lean_verify, "_call_llm") as direct_api,
        ):
            with self.assertRaisesRegex(ValueError, "subscription unavailable"):
                lean_verify._chat(env, "gpt-5.6-sol", "formalize", timeout=5)

        self.assertEqual(codex.call_args.kwargs["model"], "gpt-5.6-sol")
        direct_api.assert_not_called()

    def test_dag_helper_delegates_only_to_subscription_backend(self) -> None:
        source = {
            "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
            "CODEX_HOME": "/accounts/codex/user_17",
            "OPENAI_API_KEY": "must-be-dormant",
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with patch.object(
                codex_backend,
                "codex_subscription_exec",
                return_value=CodexResult(_GRAPH_JSON, {}),
            ) as subscription:
                content = proof_graph._run_codex_prompt_env(
                    source,
                    workspace,
                    "graph prompt",
                    model="gpt-5.6-sol",
                )

        self.assertEqual(content, _GRAPH_JSON)
        self.assertIs(subscription.call_args.kwargs["source_env"], source)
        self.assertEqual(subscription.call_args.kwargs["model"], "gpt-5.6-sol")
        self.assertFalse(subscription.call_args.kwargs.get("enable_tools", False))

    def test_dag_route_builds_environment_for_exact_run_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / "user_17"
            account_home.mkdir()
            (account_home / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": "test-oauth-token"}}),
                encoding="utf-8",
            )
            with (
                patch(
                    "agent_monitor.settings.resolved_user_env",
                    return_value={
                        "OPENAI_API_KEY": "dormant-openai",
                        "ANTHROPIC_API_KEY": "dormant-anthropic",
                        "KIMI_API_KEY": "dormant-kimi",
                    },
                ),
                patch(
                    "agent_monitor.codex_login.account_home",
                    return_value=account_home,
                ) as account,
                patch.object(
                    proof_graph,
                    "_run_codex_prompt_env",
                    return_value=_GRAPH_JSON,
                ) as response,
            ):
                content = proof_graph._run_codex_prompt(
                    {"id": 17},
                    root,
                    "graph prompt",
                    model="gpt-5.6-sol",
                    owner_id=17,
                )

        self.assertEqual(content, _GRAPH_JSON)
        account.assert_called_once_with(17)
        source = response.call_args.args[0]
        self.assertEqual(source["CODEX_HOME"], str(account_home))
        self.assertEqual(source["AGENT_MONITOR_CODEX_SUBSCRIPTION"], "1")
        self.assertEqual(source["AGENT_MONITOR_AUTH_MODE"], "chatgpt_subscription")
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "KIMI_API_KEY"):
            self.assertNotIn(name, source)
        self.assertEqual(response.call_args.kwargs["model"], "gpt-5.6-sol")

    def test_dag_route_rejects_mismatched_session_and_run_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("agent_monitor.settings.resolved_user_env") as settings_env,
                patch.object(proof_graph, "_run_codex_prompt_env") as response,
            ):
                with self.assertRaisesRegex(ValueError, "run owner"):
                    proof_graph._run_codex_prompt(
                        {"id": 18},
                        Path(tmp),
                        "graph prompt",
                        owner_id=17,
                    )
        settings_env.assert_not_called()
        response.assert_not_called()


if __name__ == "__main__":
    unittest.main()
