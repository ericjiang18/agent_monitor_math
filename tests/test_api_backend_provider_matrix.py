from __future__ import annotations

import json
import unittest
import traceback
from typing import Any
from unittest.mock import patch

from agent_monitor import kimi_k3, settings
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
        # The SDK's base URL: it adds /v1/messages itself.
        "url": "https://owner.anthropic.invalid",
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


def _anthropic_reply() -> object:
    """What the Anthropic SDK call (``api_backend._anthropic_message``) returns."""
    from anthropic.types.beta import BetaMessage

    return BetaMessage.model_validate(
        {
            "id": "msg_matrix",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
            **_response("anthropic"),
        }
    )


def _anthropic_status_error(code: int, body: object) -> Exception:
    """The error the Anthropic SDK raises for an HTTP error reply."""
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://owner.anthropic.invalid/v1/messages")
    response = httpx2.Response(code, json=body, request=request)
    return anthropic.APIStatusError(f"Error code: {code}", response=response, body=body)


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

                def fake_anthropic(options, request):
                    captured.update(options=dict(options), payload=dict(request))
                    return _anthropic_reply()

                with (
                    patch.dict(api_backend.os.environ, _OWNER_ENV, clear=True),
                    patch("agent_monitor.settings._http_json", side_effect=fake_http),
                    patch.object(api_backend, "_anthropic_message", side_effect=fake_anthropic),
                ):
                    result = api_backend.api_chat(
                        "System proof contract",
                        "User theorem",
                        model=str(case["model"]),
                    )

                if provider == "anthropic":
                    # The official SDK sends the key as X-Api-Key (see
                    # test_api_backend_anthropic for the wire format).
                    self.assertEqual(
                        captured["options"],
                        # A custom base URL (a gateway) gets plain requests.
                        {"api_key": case["key"], "base_url": case["url"], "timeout": 240, "stream": False},
                    )
                else:
                    self.assertEqual(captured["url"], case["url"])
                    headers = captured["headers"]
                    if provider == "google":
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

    def test_cached_and_reasoning_tokens_reach_a_cgm_runs_usage(self) -> None:
        from agent_monitor import usage_ledger
        from agent_monitor.cli_events import CLIEventParser
        from agent_monitor.runners import math_harness_runner

        replies = {
            "openai": ("gpt-5.6-sol", {
                "output": [{"content": [{"type": "output_text", "text": "Proof text"}]}],
                "usage": {
                    "input_tokens": 2000,
                    "output_tokens": 300,
                    "input_tokens_details": {"cached_tokens": 1536},
                    "output_tokens_details": {"reasoning_tokens": 200},
                },
            }),
            "xai": ("grok-4.5", {
                "choices": [{"message": {"role": "assistant", "content": "Proof text"}}],
                "usage": {
                    "prompt_tokens": 2000,
                    "completion_tokens": 300,
                    "prompt_tokens_details": {"cached_tokens": 1536},
                    "completion_tokens_details": {"reasoning_tokens": 200},
                },
            }),
        }
        for provider, (model, reply) in replies.items():
            with self.subTest(provider=provider):
                with (
                    patch.dict(api_backend.os.environ, _OWNER_ENV, clear=True),
                    patch("agent_monitor.settings._http_json", return_value=(200, reply)),
                ):
                    result = api_backend.api_chat("System", "User", model=model)
                # Lean and graph calls book the same reply with these counts.
                derived = usage_ledger.http_usage(provider, reply)
                self.assertEqual(
                    (result.usage["cached_input_tokens"], result.usage["reasoning_output_tokens"]),
                    (derived["cached_input_tokens"], derived["reasoning_tokens"]),
                )
                # A CGM run books it from its event stream: cached input is
                # priced at the cache rate, and reasoning is recorded.
                events: list[dict] = []
                math_harness_runner._emit_stage_usage(
                    "worker-1", math_harness_runner._clean_usage(result.usage), out=events.append
                )
                parser = CLIEventParser("codex", source_engine="math_harness")
                parser.feed("".join(json.dumps(event) + "\n" for event in events))
                self.assertEqual(
                    {name: parser.usage[name] for name in ("input_tokens", "cache_read_tokens", "reasoning_tokens")},
                    {"input_tokens": 2000, "cache_read_tokens": 1536, "reasoning_tokens": 200},
                )

    def test_kimi_reasoning_tokens_reach_a_cgm_runs_usage(self) -> None:
        # Kimi usage names reasoning ``reasoning_tokens`` (kimi_k3.response_usage),
        # not ``reasoning_output_tokens``; a Kimi CGM run books it all the same.
        from agent_monitor.cli_events import CLIEventParser
        from agent_monitor.runners import math_harness_runner

        reply = {
            "model": "kimi-k3",
            "choices": [
                {"message": {"role": "assistant", "content": "Proof text"}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": 2000,
                "completion_tokens": 300,
                "prompt_tokens_details": {"cached_tokens": 1536},
                "completion_tokens_details": {"reasoning_tokens": 200},
            },
        }
        with (
            patch.dict(api_backend.os.environ, _OWNER_ENV, clear=True),
            patch("agent_monitor.settings._http_json", return_value=(200, reply)),
        ):
            result = api_backend.api_chat("System", "User", model="kimi-k3")
        self.assertEqual(result.usage.get("reasoning_tokens"), 200)
        events: list[dict] = []
        math_harness_runner._emit_stage_usage(
            "worker-1", math_harness_runner._clean_usage(result.usage), out=events.append
        )
        parser = CLIEventParser("codex", source_engine="kimi")
        parser.feed("".join(json.dumps(event) + "\n" for event in events))
        self.assertEqual(
            {
                name: parser.usage[name]
                for name in ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens")
            },
            {"input_tokens": 2000, "output_tokens": 300, "cache_read_tokens": 1536, "reasoning_tokens": 200},
        )

    def test_a_turns_reasoning_is_booked_once_under_either_name(self) -> None:
        from agent_monitor.cli_events import CLIEventParser

        cases = {
            "codex": ({"reasoning_output_tokens": 200}, 200),
            "kimi": ({"reasoning_tokens": 200}, 200),
            "both": ({"reasoning_output_tokens": 200, "reasoning_tokens": 200}, 200),
            # One name is read (usage_ledger.cli_usage's order), never both added.
            "both, unequal": ({"reasoning_output_tokens": 150, "reasoning_tokens": 200}, 200),
            "kimi zero": ({"reasoning_output_tokens": 150, "reasoning_tokens": 0}, 150),
            "codex zero": ({"reasoning_output_tokens": 0, "reasoning_tokens": 200}, 200),
            "neither": ({}, 0),
        }
        for name, (reasoning, expected) in cases.items():
            with self.subTest(name):
                parser = CLIEventParser("codex", source_engine="kimi")
                event = {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 300, **reasoning},
                }
                parser.feed(json.dumps(event) + "\n")
                # Reasoning is a part of the output, never added to it.
                self.assertEqual(
                    (parser.usage["output_tokens"], parser.usage["reasoning_tokens"]),
                    (300, expected),
                )


class ApiBackendPerCallParameterTests(unittest.TestCase):
    """Per-call knobs are explicit arguments, so concurrent callers never
    communicate through (or mutate) the process environment."""

    def _kimi_env(self, **extra: str) -> dict[str, str]:
        return {
            "KIMI_API_KEY": "run-kimi-key",
            "KIMI_API_BASE": "https://run.kimi.invalid/v1",
            **extra,
        }

    def test_explicit_effort_and_max_tokens_override_the_environment(self) -> None:
        captured: dict[str, Any] = {}

        def fake_http(url, **kwargs):
            captured.update(url=url, **kwargs)
            return 200, _response("kimi")

        env = self._kimi_env(
            KIMI_REASONING_EFFORT="max", AGENT_MONITOR_API_MAX_TOKENS="2048"
        )
        with (
            patch.dict(api_backend.os.environ, env, clear=True),
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
        ):
            before = dict(api_backend.os.environ)
            api_backend.api_chat(
                "system", "user", model="kimi-k3", effort="low", max_tokens=9000
            )
            self.assertEqual(dict(api_backend.os.environ), before)
        self.assertEqual(captured["payload"]["reasoning_effort"], "low")
        self.assertEqual(captured["payload"]["max_tokens"], 9000)

    def test_job_output_ceiling_still_caps_an_explicit_max_tokens(self) -> None:
        captured: dict[str, Any] = {}

        def fake_http(url, **kwargs):
            captured.update(kwargs)
            return 200, _response("kimi")

        env = self._kimi_env(AGENT_MONITOR_MAX_OUTPUT_TOKENS="4096")
        with (
            patch.dict(api_backend.os.environ, env, clear=True),
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
        ):
            api_backend.api_chat("system", "user", model="kimi-k3", max_tokens=60000)
        self.assertEqual(captured["payload"]["max_tokens"], 4096)

    def test_explicit_env_mapping_replaces_the_process_environment(self) -> None:
        captured: dict[str, Any] = {}

        def fake_http(url, **kwargs):
            captured.update(url=url, **kwargs)
            return 200, _response("kimi")

        run_env = self._kimi_env(KIMI_REASONING_EFFORT="high")
        with (
            patch.dict(
                api_backend.os.environ,
                {"KIMI_API_KEY": "process-key", "KIMI_API_BASE": "https://process.invalid/v1"},
                clear=True,
            ),
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
        ):
            api_backend.api_chat("system", "user", model="kimi-k3", env=run_env)
        self.assertEqual(captured["url"], "https://run.kimi.invalid/v1/chat/completions")
        self.assertEqual(captured["headers"], {"Authorization": "Bearer run-kimi-key"})
        self.assertEqual(captured["payload"]["reasoning_effort"], "high")

    def test_sponsored_retry_policy_reads_the_explicit_env(self) -> None:
        attempts: list[dict[str, str]] = []
        waits: list[float] = []

        def failing_http(url, **kwargs):
            attempts.append(dict(kwargs["headers"]))
            waits.append(kwargs["timeout"])
            return 503, {"error": {"message": "busy"}}

        with (
            patch.dict(
                api_backend.os.environ, {"AGENT_MONITOR_SPONSORED_KIMI": "1"}, clear=True
            ),
            patch("agent_monitor.settings._http_json", side_effect=failing_http),
            patch("agent_monitor.kimi_k3.time.sleep"),
        ):
            with self.assertRaises(api_backend.APIBackendError):
                api_backend.api_chat(
                    "system",
                    "user",
                    model="kimi-k3",
                    env=self._kimi_env(AGENT_MONITOR_SPONSORED_KIMI="1"),
                )
            # An ambiguous (untyped) sponsored failure is never replayed, and
            # the broker is told the call's whole timeout: without one set,
            # as long as the broker may take, as for every sponsored caller.
            self.assertEqual(len(attempts), 1, "sponsored calls are never replayed")
            self.assertEqual(attempts[0]["X-Ansatze-Timeout"], str(kimi_k3.SPONSORED_TIMEOUT_SECONDS))
            self.assertEqual(waits, [kimi_k3.SPONSORED_TIMEOUT_SECONDS])

            # The explicit env, not the process environment, decides: a
            # user-key run keeps its bounded retries and sends no header.
            attempts.clear()
            waits.clear()
            with self.assertRaises(api_backend.APIBackendError):
                api_backend.api_chat("system", "user", model="kimi-k3", env=self._kimi_env())
            self.assertEqual(len(attempts), 3)
            self.assertNotIn("X-Ansatze-Timeout", attempts[0])
            self.assertEqual(waits[0], api_backend._DEFAULT_TIMEOUT)

            # A timeout the caller sets is kept, on either route.
            attempts.clear()
            with self.assertRaises(api_backend.APIBackendError):
                api_backend.api_chat("system", "user", model="kimi-k3", timeout=100,
                                     env=self._kimi_env(AGENT_MONITOR_SPONSORED_KIMI="1"))
            self.assertEqual(attempts[0]["X-Ansatze-Timeout"], "100")

    def test_invalid_effort_or_max_tokens_fail_before_any_request(self) -> None:
        with (
            patch.dict(api_backend.os.environ, self._kimi_env(), clear=True),
            patch("agent_monitor.settings._http_json") as http,
        ):
            with self.assertRaisesRegex(api_backend.APIBackendError, "reasoning effort"):
                api_backend.api_chat("s", "u", model="kimi-k3", effort="xhigh")
            with self.assertRaisesRegex(api_backend.APIBackendError, "max_tokens"):
                api_backend.api_chat("s", "u", model="kimi-k3", max_tokens="9000")  # type: ignore[arg-type]
        http.assert_not_called()

    def test_concurrent_calls_keep_their_own_parameters(self) -> None:
        import threading

        barrier = threading.Barrier(4)
        seen: list[tuple[str, int, str]] = []
        lock = threading.Lock()

        def fake_http(url, **kwargs):
            # Every call is in flight at once before any returns.
            barrier.wait(timeout=10)
            payload = kwargs["payload"]
            with lock:
                seen.append(
                    (payload["reasoning_effort"], payload["max_tokens"], payload["messages"][-1]["content"])
                )
            return 200, _response("kimi")

        jobs = [("low", 2048), ("high", 4096), ("max", 8192), ("low", 16384)]
        errors: list[BaseException] = []

        def call(effort: str, limit: int) -> None:
            try:
                api_backend.api_chat(
                    "system", f"{effort}:{limit}", model="kimi-k3", effort=effort, max_tokens=limit
                )
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        with (
            patch.dict(api_backend.os.environ, self._kimi_env(KIMI_REASONING_EFFORT="max"), clear=True),
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
        ):
            before = dict(api_backend.os.environ)
            threads = [threading.Thread(target=call, args=job) for job in jobs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            self.assertEqual(dict(api_backend.os.environ), before)
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(seen),
            sorted((effort, limit, f"{effort}:{limit}") for effort, limit in jobs),
        )


class ApiBackendFailureKindTests(unittest.TestCase):
    """An own key's refusal that every later call would repeat says so
    (``APIBackendError.kind``); one that may pass on a retry does not."""

    def _kind(self, model: str, code: int, body: object, **env: str) -> str | None:
        def anthropic_reply(*_args: object) -> None:
            # The Anthropic SDK raises an HTTP error reply as an exception.
            raise _anthropic_status_error(code, body)

        with (
            patch.dict(api_backend.os.environ, {**_OWNER_ENV, **env}, clear=True),
            patch("agent_monitor.settings._http_json", return_value=(code, body)),
            patch.object(api_backend, "_anthropic_message", side_effect=anthropic_reply),
            patch("agent_monitor.kimi_k3.time.sleep"),
            self.assertRaises(api_backend.APIBackendError) as raised,
        ):
            api_backend.api_chat("system", "problem", model=model)
        return raised.exception.kind

    def test_refusals_every_later_call_would_repeat(self) -> None:
        cases = (
            ("gpt-5.6-sol", 401, {"error": {"message": "Incorrect API key provided: sk-x.", "code": "invalid_api_key"}},
             "auth"),
            ("claude-sonnet-5", 403, {"type": "error", "error": {"type": "permission_error", "message": "denied"}},
             "auth"),
            ("gemini-3.6-flash", 400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                                 "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}},
             "auth"),
            ("deepseek-v4-flash", 402, {"error": {"message": "Insufficient Balance", "type": "unknown_error"}},
             "usage_limit"),
            ("gpt-5.6-sol", 429, {"error": {"message": "You exceeded your current quota, please check your plan.",
                                            "type": "insufficient_quota", "code": "insufficient_quota"}},
             "usage_limit"),
            ("claude-sonnet-5", 400, {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API."}}, "usage_limit"),
            ("gpt-5.6-sol", 404, {"error": {"message": "The model does not exist.", "code": "model_not_found"}}, "model"),
        )
        for model, code, body, kind in cases:
            with self.subTest(model=model, code=code):
                self.assertEqual(self._kind(model, code, body), kind)

    def test_failures_that_may_pass_have_no_kind(self) -> None:
        cases = (
            ("gpt-5.6-sol", 429, {"error": {"message": "Rate limit reached for requests", "code": "rate_limit_exceeded"}}),
            ("gpt-5.6-sol", 500, {"error": {"message": "The server had an error"}}),
            ("deepseek-v4-flash", 503, "Service Unavailable"),
            ("gpt-5.6-sol", 400, {"error": {"message": "This model's maximum context length is exceeded"}}),
            ("gpt-5.6-sol", 0, "timed out"),
        )
        for model, code, body in cases:
            with self.subTest(model=model, code=code):
                self.assertIsNone(self._kind(model, code, body))

    def test_a_sponsored_refusal_keeps_its_own_failure(self) -> None:
        with (
            patch.dict(api_backend.os.environ, {"AGENT_MONITOR_SPONSORED_KIMI": "1"}, clear=True),
            patch("agent_monitor.settings._http_json", return_value=(401, {"error": {"type": "unauthorized"}})),
            self.assertRaises(api_backend.APIBackendError) as raised,
        ):
            api_backend.api_chat("system", "problem", model="kimi-k3",
                                 env={"AGENT_MONITOR_SPONSORED_KIMI": "1", "KIMI_API_KEY": "broker-token",
                                      "KIMI_API_BASE": "https://run.kimi.invalid/v1"})
        self.assertIsNone(raised.exception.kind)
        self.assertEqual(raised.exception.failure.kind, "unauthorized")


if __name__ == "__main__":
    unittest.main()
