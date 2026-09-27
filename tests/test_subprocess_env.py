"""Verify child processes receive only the intended environment and provider route."""

from __future__ import annotations

import pytest

from agent_monitor.subprocess_env import (
    child_process_env,
    scrub_control_plane_env,
    scrub_provider_env,
    project_provider_env,
)


def test_child_environment_removes_control_plane_secrets_after_overlay() -> None:
    env = child_process_env(
        {
            "PATH": "/safe/bin",
            "ANTHROPIC_API_KEY": "ambient-provider-key",
            "FUTURE_VENDOR_API_KEY": "unknown-ambient-key",
            "SMTP_PASSWORD": "server-mail-secret",
            "AGENT_MONITOR_SECRET_KEY": "server-app-secret",
            "CREDENTIALS_DIRECTORY": "/run/credentials/service",
        },
        {
            "OPENAI_API_KEY": "selected-provider-key",
            "SMTP_PASSWORD": "overlay-must-not-win",
            "AGENT_MONITOR_BASE_PATH": "/private-prefix",
            "KIMI_API_KEY": "selected-kimi-key",
        },
    )

    assert env == {
        "PATH": "/safe/bin",
        "OPENAI_API_KEY": "selected-provider-key",
        "KIMI_API_KEY": "selected-kimi-key",
    }


def test_provider_scrubber_removes_known_and_future_provider_names() -> None:
    env = {
        "PATH": "/safe/bin",
        "OPENAI_API_KEY": "openai-secret",
        "FUTURE_VENDOR_API_KEYS": "future-secret",
        "EXPERIMENT_AUTH_TOKEN": "auth-secret",
        "HTTPS_PROXY": "http://proxy.example",
    }
    assert scrub_provider_env(env) is env
    assert env == {
        "PATH": "/safe/bin",
        "HTTPS_PROXY": "http://proxy.example",
    }


def test_provider_projection_keeps_only_selected_main_and_delegate_routes() -> None:
    env = {
        "PATH": "/safe/bin",
        "OPENAI_API_KEY": "openai-key",
        "OPENAI_BASE_URL": "https://openai.example/v1",
        "ANTHROPIC_API_KEY": "anthropic-key",
        "KIMI_API_KEY": "kimi-key",
        "KIMI_API_BASE": "https://kimi.example/v1",
    }
    project_provider_env(env, ("gpt-test", "claude-test"))
    assert env == {
        "PATH": "/safe/bin",
        "OPENAI_API_KEY": "openai-key",
        "OPENAI_BASE_URL": "https://openai.example/v1",
        "ANTHROPIC_API_KEY": "anthropic-key",
    }


def test_openai_style_model_uses_only_single_openrouter_route_when_configured() -> None:
    env = {
        "OPENROUTER_API_KEY": "openrouter-key",
        "OPENROUTER_BASE_URL": "https://openrouter.example/v1",
        "KIMI_API_KEY": "",
    }
    project_provider_env(env, ("gpt-compatible-model",))
    assert env == {
        "OPENROUTER_API_KEY": "openrouter-key",
        "OPENROUTER_BASE_URL": "https://openrouter.example/v1",
    }


@pytest.mark.parametrize(
    ("model", "expected_key"),
    [
        ("openrouter:vendor/model", "OPENROUTER_API_KEY"),
        ("deepseek:reasoner", "DEEPSEEK_API_KEY"),
        ("groq:llama", "GROQ_API_KEY"),
        ("together:qwen", "TOGETHERAI_API_KEY"),
        ("xai:grok", "XAI_API_KEY"),
        ("anthropic:sonnet", "ANTHROPIC_API_KEY"),
        ("gemini:pro", "GEMINI_API_KEY"),
        ("openai:gpt", "OPENAI_API_KEY"),
    ],
)
def test_colon_qualified_model_keeps_only_its_provider(
    model: str, expected_key: str
) -> None:
    keys = {
        "OPENROUTER_API_KEY": "openrouter-key",
        "DEEPSEEK_API_KEY": "deepseek-key",
        "GROQ_API_KEY": "groq-key",
        "TOGETHERAI_API_KEY": "together-key",
        "XAI_API_KEY": "xai-key",
        "ANTHROPIC_API_KEY": "anthropic-key",
        "GEMINI_API_KEY": "gemini-key",
        "OPENAI_API_KEY": "openai-key",
    }
    project_provider_env(keys, (model,))
    assert {key for key in keys if key.endswith("_API_KEY")} == {expected_key}


def test_scrubber_mutates_existing_mapping_and_is_idempotent() -> None:
    env = {
        "DOTENV_PATH": "/secret/.env",
        "GOOGLE_OAUTH_CLIENT_SECRET": "oauth-secret",
        "SMTP_SERVER": "mail.example",
        "AGENT_MONITOR_ENGINE": "improof",
    }
    assert scrub_control_plane_env(env) is env
    assert scrub_control_plane_env(env) is env
    assert env == {"AGENT_MONITOR_ENGINE": "improof"}
