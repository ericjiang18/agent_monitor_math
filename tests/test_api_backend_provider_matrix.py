from __future__ import annotations

import unittest
import traceback
from typing import Any
from unittest.mock import patch

from agent_monitor import settings
from agent_monitor.runners import api_backend


_OWNER_ENV = {
    "OPENAI_API_KEY": "owner-openai-key",
    "OPENAI_BASE_URL": "https://owner.openai.invalid/v1/",
    "KIMI_API_KEY": "owner-kimi-key",
    "KIMI_API_BASE": "https://owner.kimi.invalid/v1/",
    "ANTHROPIC_API_KEY": "owner-anthropic-key",
    "ANTHROPIC_BASE_URL": "https://owner.anthropic.invalid/",
    "GEMINI_API_KEY": "owner-google-key",
    "GEMINI_API_BASE": "https://owner.google.invalid/",
    "OPENROUTER_API_KEY": "owner-openrouter-key",
    "OPENROUTER_BASE_URL": "https://owner.openrouter.invalid/api/v1/",
    "DEEPSEEK_API_KEY": "owner-deepseek-key",
    "DEEPSEEK_API_BASE": "https://owner.deepseek.invalid/",
    "GROQ_API_KEY": "owner-groq-key",
    "GROQ_API_BASE": "https://owner.groq.invalid/openai/v1/",
    "TOGETHERAI_API_KEY": "owner-together-key",
    "TOGETHERAI_API_BASE": "https://owner.together.invalid/v1/",
    "XAI_API_KEY": "owner-xai-key",
    "XAI_API_BASE": "https://owner.xai.invalid/v1/",
    "AGENT_MONITOR_API_MAX_TOKENS": "4096",
}


_CASES = (
    {
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "chosen": "gpt-5.6-sol",
        "url": "https://owner.openai.invalid/v1/responses",
        "key": "owner-openai-key",
    },
    {
        "provider": "kimi",
        "model": "kimi-k3",
        "chosen": "kimi-k3",
        "url": "https://owner.kimi.invalid/v1/chat/completions",
        "key": "owner-kimi-key",
    },
    {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "chosen": "claude-sonnet-5",
        "url": "https://owner.anthropic.invalid/v1/messages",
        "key": "owner-anthropic-key",
    },
    {
        "provider": "google",
        "model": "gemini-3.6-flash",
        "chosen": "gemini-3.6-flash",
        "url": (
            "https://owner.google.invalid/v1beta/models/"
            "gemini-3.6-flash:generateContent?key=owner-google-key"
        ),
        "key": "owner-google-key",
    },
    {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "chosen": "deepseek-v4-flash",
        "url": "https://owner.deepseek.invalid/v1/chat/completions",
        "key": "owner-deepseek-key",
    },
    {
        "provider": "xai",
        "model": "grok-4.5",
        "chosen": "grok-4.5",
        "url": "https://owner.xai.invalid/v1/chat/completions",
        "key": "owner-xai-key",
    },
    {
        "provider": "openrouter",
        "model": "openrouter:vendor/proof-model",
        "chosen": "vendor/proof-model",
        "url": "https://owner.openrouter.invalid/api/v1/chat/completions",
        "key": "owner-openrouter-key",
    },
    {
        "provider": "groq",
        "model": "groq:meta-llama/proof-model",
        "chosen": "meta-llama/proof-model",
        "url": "https://owner.groq.invalid/openai/v1/chat/completions",
        "key": "owner-groq-key",
    },
    {
        "provider": "together",
        "model": "together:meta-llama/proof-model",
        "chosen": "meta-llama/proof-model",
        "url": "https://owner.together.invalid/v1/chat/completions",
        "key": "owner-together-key",
    },
)


def _response(provider: str) -> dict[str, Any]:
    if provider == "openai":
        return {
            "output": [{"content": [{"type": "output_text", "text": "Proof text"}]}],
            "usage": {"input_tokens": 11, "output_tokens": 5},
        }
    if provider == "anthropic":
        return {
            "content": [{"type": "text", "text": "Proof text"}],
            "usage": {"input_tokens": 11, "output_tokens": 5},
        }
    if provider == "google":
        return {
            "candidates": [{"content": {"parts": [{"text": "Proof text"}]}}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 5},
        }
    content: object = "Proof text"
    if provider == "groq":
        content = [
            {"type": "text", "text": "Proof"},
            {"type": "text", "text": "text"},
        ]
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5},
    }


class ApiBackendProviderMatrixTests(unittest.TestCase):
    def assert_response_only(self, value: object) -> None:
        forbidden = {
            "function_call",
            "functions",
            "parallel_tool_calls",
            "tool_choice",
            "tool_config",
            "tools",
        }
        if isinstance(value, dict):
            self.assertTrue(forbidden.isdisjoint(value), value)
            for child in value.values():
                self.assert_response_only(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_response_only(child)

    def test_every_settings_provider_uses_its_own_key_and_no_tools(self) -> None:
        self.assertEqual(
            {case["provider"] for case in _CASES},
            {provider["id"] for provider in settings.PROVIDERS},
        )

        for case in _CASES:
            provider = str(case["provider"])
            with self.subTest(provider=provider):
                captured: dict[str, object] = {}

                def fake_http(url, **kwargs):
                    captured.update(url=url, **kwargs)
                    return 200, _response(provider)

                with (
                    patch.dict(api_backend.os.environ, _OWNER_ENV, clear=True),
                    patch("agent_monitor.settings._http_json", side_effect=fake_http),
                ):
                    result = api_backend.api_chat(
                        "System proof contract",
                        "User theorem",
                        model=str(case["model"]),
                    )

                self.assertEqual(captured["url"], case["url"])
                headers = captured["headers"]
                if provider == "anthropic":
                    self.assertEqual(headers["x-api-key"], case["key"])
                    self.assertEqual(headers["anthropic-version"], "2023-06-01")
                elif provider == "google":
                    self.assertEqual(headers, {})
                else:
                    self.assertEqual(
                        headers, {"Authorization": f"Bearer {case['key']}"}
                    )

                payload = captured["payload"]
                self.assert_response_only(payload)
                if provider != "google":
                    self.assertEqual(payload.get("model"), case["chosen"])
                if provider == "openai":
                    self.assertEqual(payload["instructions"], "System proof contract")
                    self.assertEqual(payload["input"], "User theorem")
                    self.assertEqual(payload["max_output_tokens"], 4096)
                elif provider == "anthropic":
                    self.assertEqual(payload["system"], "System proof contract")
                    self.assertEqual(payload["messages"][0]["content"], "User theorem")
                    self.assertEqual(payload["max_tokens"], 4096)
                elif provider == "google":
                    self.assertEqual(
                        payload["systemInstruction"]["parts"][0]["text"],
                        "System proof contract",
                    )
                    self.assertEqual(
                        payload["contents"][0]["parts"][0]["text"], "User theorem"
                    )
                    self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 4096)
                else:
                    self.assertEqual(
                        [message["role"] for message in payload["messages"]],
                        ["system", "user"],
                    )
                    self.assertEqual(payload["max_tokens"], 4096)

                self.assertEqual(result.text, "Proof\ntext" if provider == "groq" else "Proof text")
                self.assertEqual(result.provider, provider)
                self.assertEqual(result.model, case["chosen"])
                self.assertEqual(result.usage["input_tokens"], 11)
                self.assertEqual(result.usage["output_tokens"], 5)

    def test_explicit_provider_never_falls_through_to_another_users_route(self) -> None:
        cases = (
            ("openai:gpt-owner", ("OPENAI_API_KEY",), "OPENAI_API_KEY"),
            ("kimi:kimi-k3", ("KIMI_API_KEY",), "KIMI_API_KEY"),
            ("anthropic:claude-owner", ("ANTHROPIC_API_KEY",), "ANTHROPIC_API_KEY"),
            (
                "google:gemini-owner",
                ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                "GEMINI_API_KEY or GOOGLE_API_KEY",
            ),
            ("openrouter:vendor/model", ("OPENROUTER_API_KEY",), "OPENROUTER_API_KEY"),
            ("deepseek:owner-model", ("DEEPSEEK_API_KEY",), "DEEPSEEK_API_KEY"),
            ("groq:owner-model", ("GROQ_API_KEY",), "GROQ_API_KEY"),
            ("together:owner-model", ("TOGETHERAI_API_KEY",), "TOGETHERAI_API_KEY"),
            ("xai:owner-model", ("XAI_API_KEY",), "XAI_API_KEY"),
        )
        for model, removed, expected in cases:
            with self.subTest(model=model):
                env = dict(_OWNER_ENV)
                for name in removed:
                    env.pop(name, None)
                with (
                    patch.dict(api_backend.os.environ, env, clear=True),
                    patch("agent_monitor.settings._http_json") as request,
                    self.assertRaisesRegex(api_backend.APIBackendError, expected),
                ):
                    api_backend.api_chat("system", "problem", model=model)
                request.assert_not_called()

    def test_named_model_families_never_fall_through_to_openrouter(self) -> None:
        cases = (
            ("gpt-owner", "OPENAI_API_KEY"),
            ("kimi-owner", "KIMI_API_KEY"),
            ("claude-owner", "ANTHROPIC_API_KEY"),
            ("gemini-owner", "GEMINI_API_KEY or GOOGLE_API_KEY"),
            ("deepseek-owner", "DEEPSEEK_API_KEY"),
            ("grok-owner", "XAI_API_KEY"),
        )
        removed_by_model = {
            "gpt-owner": ("OPENAI_API_KEY",),
            "kimi-owner": ("KIMI_API_KEY",),
            "claude-owner": ("ANTHROPIC_API_KEY",),
            "gemini-owner": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "deepseek-owner": ("DEEPSEEK_API_KEY",),
            "grok-owner": ("XAI_API_KEY",),
        }
        for model, expected in cases:
            with self.subTest(model=model):
                env = dict(_OWNER_ENV)
                for name in removed_by_model[model]:
                    env.pop(name, None)
                with (
                    patch.dict(api_backend.os.environ, env, clear=True),
                    patch("agent_monitor.settings._http_json") as request,
                    self.assertRaisesRegex(api_backend.APIBackendError, expected),
                ):
                    api_backend.api_chat("system", "problem", model=model)
                request.assert_not_called()

    def test_openrouter_slash_id_is_preserved_and_colon_pins_direct_provider(self) -> None:
        from agent_monitor.proof_graph import _resolve_llm_provider

        direct = _resolve_llm_provider(
            _OWNER_ENV, "anthropic:claude-owner"
        )
        self.assertEqual(
            direct,
            (
                "anthropic",
                "https://owner.anthropic.invalid",
                "owner-anthropic-key",
                "claude-owner",
            ),
        )
        routed = _resolve_llm_provider(
            {
                "ANTHROPIC_API_KEY": "anthropic-owner",
                "OPENROUTER_API_KEY": "router-owner",
            },
            "anthropic/claude-owner",
        )
        self.assertEqual(
            routed,
            (
                "openrouter",
                "https://openrouter.ai/api/v1",
                "router-owner",
                "anthropic/claude-owner",
            ),
        )

    def test_regular_user_environment_excludes_operator_provider_keys(self) -> None:
        owner = {
            "OPENAI_API_KEY": "account-77-openai",
            "GROQ_API_KEY": "account-77-groq",
        }
        operator = {
            "OPENAI_API_KEY": "operator-openai",
            "GROQ_API_KEY": "operator-groq",
        }
        with (
            patch.dict(settings.os.environ, operator, clear=True),
            patch("agent_monitor.auth.public_mode", return_value=False),
            patch("agent_monitor.auth.get_user_env", return_value=owner),
            patch.object(settings, "_read_env_file", return_value=operator),
        ):
            resolved = settings.resolved_user_env({"id": 77, "is_admin": False})

        self.assertEqual(resolved, owner)
        self.assertNotIn("operator-openai", resolved.values())
        self.assertNotIn("operator-groq", resolved.values())

    def test_gemini_query_key_is_redacted_from_provider_errors(self) -> None:
        secret = "owner-google-secret-query-key"

        def rejected(url, **_kwargs):
            return 403, {
                "error": {
                    "message": f"request {url} rejected for credential {secret}"
                }
            }

        with (
            patch.dict(
                api_backend.os.environ,
                {
                    "GOOGLE_API_KEY": secret,
                    "GEMINI_API_BASE": "https://owner.google.invalid",
                },
                clear=True,
            ),
            patch("agent_monitor.settings._http_json", side_effect=rejected),
            self.assertRaises(api_backend.APIBackendError) as raised,
        ):
            api_backend.api_chat("system", "problem", model="gemini-owner")

        rendered = repr(raised.exception)
        self.assertNotIn(secret, rendered)
        self.assertIn("[redacted]", rendered)

    def test_formal_dag_gemini_error_also_redacts_query_key(self) -> None:
        from agent_monitor import proof_graph

        secret = "formal-google-secret-query-key"

        def rejected(url, **_kwargs):
            return 403, {"error": {"message": f"request {url} used {secret}"}}

        with (
            patch("agent_monitor.settings._http_json", side_effect=rejected),
            self.assertRaises(ValueError) as raised,
        ):
            proof_graph._call_llm(
                {
                    "GOOGLE_API_KEY": secret,
                    "GEMINI_API_BASE": "https://owner.google.invalid",
                },
                "gemini-owner",
                "proof graph prompt",
            )

        rendered = repr(raised.exception)
        self.assertNotIn(secret, rendered)
        self.assertIn("[redacted]", rendered)

    def test_gemini_transport_traceback_suppresses_query_key(self) -> None:
        secret = "transport-google-secret-query-key"

        def exploded(url, **_kwargs):
            raise RuntimeError(f"transport rejected {url}")

        with (
            patch.dict(
                api_backend.os.environ,
                {
                    "GEMINI_API_KEY": secret,
                    "GEMINI_API_BASE": "https://owner.google.invalid",
                },
                clear=True,
            ),
            patch("agent_monitor.settings._http_json", side_effect=exploded),
        ):
            try:
                api_backend.api_chat("system", "problem", model="gemini-owner")
            except api_backend.APIBackendError as exc:
                rendered = "".join(traceback.format_exception(exc))
                self.assertTrue(exc.__suppress_context__)
            else:
                self.fail("transport exception was not projected as APIBackendError")

        self.assertNotIn(secret, rendered)
        self.assertIn("[redacted]", rendered)


if __name__ == "__main__":
    unittest.main()
