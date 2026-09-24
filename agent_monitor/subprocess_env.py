"""Build child-process environments without console control-plane secrets."""
from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping


# These values authenticate or configure the long-lived web service itself.
# Engine subprocesses never need them. Provider API keys are deliberately not
# included here: the job router separately selects the provider credentials a
# specific run is allowed to receive.
_CONTROL_PLANE_ENV_NAMES = frozenset(
    {
        "AGENT_MONITOR_BASE_PATH",
        "AGENT_MONITOR_DATA_ENCRYPTION_KEY",
        "AGENT_MONITOR_DATA_KEY_FILE",
        "AGENT_MONITOR_ENV_PATH",
        "AGENT_MONITOR_REQUIRE_DATA_KEY",
        "AGENT_MONITOR_SECRET_KEY",
        "AGENT_MONITOR_SERVER_SETTINGS_READ_ONLY",
        "CREDENTIALS_DIRECTORY",
        "DOTENV_PATH",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "OAUTH_CLIENT_SECRET",
    }
)

PROVIDER_ROUTE_ENV_NAMES = frozenset(
    {
        "AGENT_MONITOR_BASE_URL",
        "AGENT_MONITOR_CLAUDE_MODEL",
        "AGENT_MONITOR_CLAUDE_SUBSCRIPTION",
        "AGENT_MONITOR_CODEX_MODEL",
        "AGENT_MONITOR_CODEX_SUBSCRIPTION",
        "AGENT_MONITOR_MODEL",
        "AGENT_MONITOR_OPENAI_MODEL",
        "AGENT_MONITOR_OPENCLAUDE_MODEL",
        "AGENT_MONITOR_OPENCLAW_MODEL",
        "AGENT_MONITOR_OPENHANDS_MODEL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CONFIG_DIR",
        "CODEX_API_KEY",
        "CODEX_HOME",
        "DEEPAGENTS_MODEL",
        "DEEPSEEK_API_BASE",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_BASE",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_BASE",
        "GROQ_API_KEY",
        "HERMES_MODEL",
        "KIMI_API_BASE",
        "KIMI_API_KEY",
        "KIMI_BASE_URL",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "OPENAI_API_BASE",
        "OPENAI_API_KEY",
        "OPENAI_API_KEYS",
        "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "PLAIN_MODEL",
        "TOGETHERAI_API_BASE",
        "TOGETHERAI_API_KEY",
        "XAI_API_BASE",
        "XAI_API_KEY",
    }
)

_PROVIDER_GROUPS: dict[str, frozenset[str]] = {
    "openai": frozenset(
        {"OPENAI_API_KEY", "OPENAI_API_KEYS", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"}
    ),
    "anthropic": frozenset(
        {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"}
    ),
    "kimi": frozenset({"KIMI_API_KEY", "KIMI_API_BASE", "KIMI_BASE_URL"}),
    "deepseek": frozenset({"DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE"}),
    "openrouter": frozenset({"OPENROUTER_API_KEY", "OPENROUTER_BASE_URL"}),
    "gemini": frozenset({"GEMINI_API_KEY", "GEMINI_API_BASE", "GOOGLE_API_KEY"}),
    "groq": frozenset({"GROQ_API_KEY", "GROQ_API_BASE"}),
    "together": frozenset({"TOGETHERAI_API_KEY", "TOGETHERAI_API_BASE"}),
    "xai": frozenset({"XAI_API_KEY", "XAI_API_BASE"}),
    "generic": frozenset({"LLM_API_KEY", "LLM_BASE_URL"}),
}

_PROVIDER_KEYS: dict[str, frozenset[str]] = {
    name: frozenset(
        key
        for key in names
        if key.endswith("_API_KEY")
        or key.endswith("_API_KEYS")
        or key.endswith("_AUTH_TOKEN")
    )
    for name, names in _PROVIDER_GROUPS.items()
}


def scrub_control_plane_env(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Remove server-only secrets and pointers from a child environment."""
    for name in _CONTROL_PLANE_ENV_NAMES:
        env.pop(name, None)
    for name in tuple(env):
        if name.upper().startswith("SMTP_"):
            env.pop(name, None)
    return env


def scrub_provider_env(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Remove inherited model routes; authorized overlays are added afterward."""
    for name in tuple(env):
        upper = name.upper()
        if (
            upper in PROVIDER_ROUTE_ENV_NAMES
            or upper.endswith("_API_KEY")
            or upper.endswith("_API_KEYS")
            or upper.endswith("_AUTH_TOKEN")
        ):
            env.pop(name, None)
    return env


def _provider_for_model(model: str | None) -> str | None:
    value = str(model or "").strip().lower()
    if not value:
        return None
    if value.startswith(("kimi-", "moonshot/", "moonshot-")):
        return "kimi"
    if value.startswith(("claude-", "anthropic:")):
        return "anthropic"
    if value.startswith(("gemini-", "gemini:", "google/", "google:")):
        return "gemini"
    if value.startswith(("deepseek-", "deepseek/", "deepseek:")):
        return "deepseek"
    if value.startswith(("openrouter/", "openrouter:")):
        return "openrouter"
    if value.startswith(("groq/", "groq:")):
        return "groq"
    if value.startswith(("together/", "together:")):
        return "together"
    if value.startswith(("grok-", "xai/", "xai:")):
        return "xai"
    if value.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4", "codex-", "openai:")):
        return "openai"
    return None


def project_provider_env(
    env: MutableMapping[str, str], models: tuple[str | None, ...]
) -> MutableMapping[str, str]:
    """Keep only provider routes needed by the selected main/delegate models."""
    original = dict(env)
    configured = {
        provider
        for provider, key_names in _PROVIDER_KEYS.items()
        if any(str(original.get(name) or "").strip() for name in key_names)
    }
    selected: set[str] = set()
    for model in models:
        provider = _provider_for_model(model)
        if provider is None:
            continue
        if provider == "kimi" or provider in configured:
            selected.add(provider)
        elif len(configured) == 1:
            # A provider such as OpenRouter can serve OpenAI-style model IDs.
            selected.update(configured)
        else:
            selected.add(provider)
    if not selected and len(configured) == 1:
        selected.update(configured)

    scrub_provider_env(env)
    allowed = set().union(*(_PROVIDER_GROUPS[name] for name in selected)) if selected else set()
    for name in allowed:
        if name in original:
            env[name] = original[name]
    return env


def child_process_env(
    source: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy, overlay, then scrub an environment for a subprocess."""
    env = dict(os.environ if source is None else source)
    scrub_control_plane_env(env)
    scrub_provider_env(env)
    if extra:
        env.update({str(key): str(value) for key, value in extra.items() if value})
    scrub_control_plane_env(env)
    return env
