"""Load operator configuration and manage each user's provider settings."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent_monitor import ROOT
from agent_monitor import kimi_k3

ENV_PATH = Path(os.environ.get("AGENT_MONITOR_ENV_PATH") or ROOT / ".env").expanduser().resolve()
SYSTEM_SETTINGS_PATH = ROOT / "data" / "system_settings.json"


def _server_settings_are_operator_managed() -> bool:
    """Return true when the server environment is an immutable credential."""
    explicit = os.environ.get(
        "AGENT_MONITOR_SERVER_SETTINGS_READ_ONLY", ""
    ).strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if not credential_dir:
        return False
    try:
        ENV_PATH.relative_to(Path(credential_dir).expanduser().resolve())
    except (OSError, ValueError):
        return False
    return True

# Provider catalog — mirrors common LiteLLM env vars.
PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "label": "OpenAI",
        "key_var": "OPENAI_API_KEY",
        "base_var": "OPENAI_BASE_URL",
        "default_base": "https://api.openai.com/v1",
        "models": [
            "gpt-6-astra",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.6",
            "chat-latest",
            "gpt-5.5-2026-04-23",
            "gpt-5.2-2025-12-11",
            "gpt-5-mini-2025-08-07",
        ],
        "docs": "https://developers.openai.com/api/docs/models",
    },
    {
        "id": "kimi",
        "label": "Kimi K3 API",
        "key_var": kimi_k3.KEY_VAR,
        "base_var": kimi_k3.BASE_VAR,
        "default_base": kimi_k3.DEFAULT_BASE_URL,
        "models": [kimi_k3.MODEL_ID],
        "settings": [
            {
                "var": kimi_k3.REASONING_VAR,
                "label": "Reasoning effort",
                "type": "select",
                "choices": list(kimi_k3.ALLOWED_REASONING_EFFORTS),
                "default": kimi_k3.DEFAULT_REASONING_EFFORT,
            }
        ],
        "compatibility_note": (
            "OpenAI-compatible Chat Completions; output_config and "
            "context_management are omitted"
        ),
        "docs": None,
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "key_var": "ANTHROPIC_API_KEY",
        "base_var": "ANTHROPIC_BASE_URL",
        "default_base": "https://api.anthropic.com",
        "models": ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"],
        "docs": "https://docs.litellm.ai/docs/providers/anthropic",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "key_var": "OPENROUTER_API_KEY",
        "base_var": "OPENROUTER_BASE_URL",
        "default_base": "https://openrouter.ai/api/v1",
        "models": [],
        "docs": "https://docs.litellm.ai/docs/providers/openrouter",
    },
    {
        "id": "google",
        "label": "Google Gemini",
        "key_var": "GEMINI_API_KEY",
        "alt_key_vars": ["GOOGLE_API_KEY"],
        "base_var": "GEMINI_API_BASE",
        "default_base": "https://generativelanguage.googleapis.com",
        "models": ["gemini-3.6-flash", "gemini-3.1-pro-preview"],
        "docs": "https://docs.litellm.ai/docs/providers/gemini",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "key_var": "DEEPSEEK_API_KEY",
        "base_var": "DEEPSEEK_API_BASE",
        "default_base": "https://api.deepseek.com",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "docs": "https://docs.litellm.ai/docs/providers/deepseek",
    },
    {
        "id": "groq",
        "label": "Groq",
        "key_var": "GROQ_API_KEY",
        "base_var": "GROQ_API_BASE",
        "default_base": "https://api.groq.com/openai/v1",
        "models": [],
        "docs": "https://docs.litellm.ai/docs/providers/groq",
    },
    {
        "id": "together",
        "label": "Together AI",
        "key_var": "TOGETHERAI_API_KEY",
        "base_var": "TOGETHERAI_API_BASE",
        "default_base": "https://api.together.xyz/v1",
        "models": [],
        "docs": "https://docs.litellm.ai/docs/providers/together_ai",
    },
    {
        "id": "xai",
        "label": "xAI",
        "key_var": "XAI_API_KEY",
        "base_var": "XAI_API_BASE",
        "default_base": "https://api.x.ai/v1",
        "models": ["grok-4.5"],
        "docs": "https://docs.litellm.ai/docs/providers/xai",
    },
]

GENERAL_VARS = {
    "AGENT_MONITOR_MODEL": {"label": "Default model", "type": "model"},
    "AGENT_MONITOR_ENGINE": {"label": "Default harness", "type": "engine"},
    "HERMES_MODEL": {"label": "Hermes model override", "type": "model"},
    "AGENT_MONITOR_BASE_URL": {"label": "Custom API base (optional)", "type": "url"},
    "AGENT_MONITOR_MAX_ITERATIONS": {"label": "Max agent iterations", "type": "number"},
    "AGENT_MONITOR_ACCOUNT_RUNTIME": {"label": "Account runtime", "type": "account_runtime"},
    "AGENT_MONITOR_USE_CODEX": {"label": "Use Codex subscription", "type": "boolean"},
    "AGENT_MONITOR_USE_CLAUDE": {"label": "Use Claude subscription", "type": "boolean"},
}

_DEFAULT_ENGINE_MIGRATIONS = {"ucla": "math_harness"}


def _preferred_default_engine(engine: str) -> str:
    return _DEFAULT_ENGINE_MIGRATIONS.get(engine, engine)


_SECRET_RE = re.compile(r"^sk-[A-Za-z0-9._-]{8,}$|^[A-Za-z0-9._-]{20,}$")


def sponsored_kimi_available() -> bool:
    """Return whether the isolated loopback Kimi proxy is healthy."""
    from agent_monitor import sponsored_kimi_client

    return sponsored_kimi_client.configured()


def sponsored_kimi_engines() -> list[str]:
    from agent_monitor import sponsored_kimi_client

    return list(sponsored_kimi_client.supported_engines())


_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
GUEST_PROVIDER_IDS = frozenset({"kimi", "openai"})
GUEST_ENV_KEYS = frozenset({"OPENAI_API_KEY", "KIMI_API_KEY", "AGENT_MONITOR_MODEL"})
GUEST_DEFAULT_MODEL = "kimi-k3"


def hosted_guest_kimi_key() -> str:
    """The only operator credential available to the bounded guest harness."""
    if os.environ.get("AGENT_MONITOR_GUEST_HOSTED_KIMI", "1").lower() in {"0", "false", "off", "no"}:
        return ""
    values = _read_env_file()
    return str(os.environ.get("AGENT_MONITOR_GUEST_KIMI_API_KEY")
               or values.get("AGENT_MONITOR_GUEST_KIMI_API_KEY")
               or os.environ.get("KIMI_API_KEY") or values.get("KIMI_API_KEY") or "").strip()


def hosted_guest_kimi_available() -> bool:
    if os.environ.get("AGENT_MONITOR_GUEST_HOSTED_KIMI", "1").lower() in {"0", "false", "off", "no"}:
        return False
    from agent_monitor import sponsored_kimi_client
    return sponsored_kimi_client.configured() or bool(hosted_guest_kimi_key())


def validate_guest_api_key(value: object) -> str:
    """Return a bounded provider key suitable for a public guest session."""
    key = str(value or "").strip()
    if not 12 <= len(key) <= 512 or not _SECRET_RE.fullmatch(key):
        raise ValueError("Enter a valid API key")
    return key


def validate_guest_model(value: object) -> str:
    """Return a bounded model identifier with no markup or shell syntax."""
    model = str(value or "").strip()
    if not _MODEL_ID_RE.fullmatch(model):
        raise ValueError("Guest model identifiers contain unsupported characters")
    return model


def _known_env_keys() -> set[str]:
    keys: set[str] = set(GENERAL_VARS.keys())
    for spec in PROVIDERS:
        keys.add(spec["key_var"])
        if spec.get("base_var"):
            keys.add(spec["base_var"])
        for alt in spec.get("alt_key_vars") or []:
            keys.add(alt)
        for option in spec.get("settings") or []:
            if option.get("var"):
                keys.add(str(option["var"]))
    return keys


def resolved_user_env(user: dict[str, Any] | None) -> dict[str, str]:
    """Return only the provider configuration this account is allowed to use.

    Per-user values (users.db) always win. The admin account additionally
    falls back to the server .env, so the operator's keys keep working;
    regular users only ever see their own keys. In public mode ordinary
    accounts also inherit the server .env so anyone can start a run, but
    anonymous guests never do: they are served by the dedicated hosted guest
    credential (see ``hosted_guest_kimi_key``) or by a key they supply.
    """
    out: dict[str, str] = {}
    is_guest = bool(user and user.get("guest"))
    inherit_server = (user is None or user.get("is_admin")) and not is_guest
    if not inherit_server and not is_guest:
        from agent_monitor.auth import public_mode

        inherit_server = public_mode()
    if inherit_server:
        file_env = _read_env_file()
        for key in _known_env_keys():
            value = file_env.get(key) or os.environ.get(key) or ""
            if value:
                out[key] = value
    if user is not None:
        from agent_monitor.auth import get_user_env

        user_values = get_user_env(int(user["id"]))
        if user.get("guest"):
            # Guest runs use only remote API calls. In particular,
            # never honor a stored custom base URL: that would turn a public
            # session into an internal-network request primitive.
            user_values = {
                key: value
                for key, value in user_values.items()
                if key in GUEST_ENV_KEYS
            }
            for key_var in ("OPENAI_API_KEY", "KIMI_API_KEY"):
                try:
                    if key_var in user_values:
                        user_values[key_var] = validate_guest_api_key(user_values[key_var])
                except ValueError:
                    user_values.pop(key_var, None)
            try:
                if "AGENT_MONITOR_MODEL" in user_values:
                    user_values["AGENT_MONITOR_MODEL"] = validate_guest_model(
                        user_values["AGENT_MONITOR_MODEL"]
                    )
            except ValueError:
                user_values.pop("AGENT_MONITOR_MODEL", None)
        for key, value in user_values.items():
            if value:
                out[key] = value
            else:
                out.pop(key, None)
    return out


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "••••"
    return value[:4] + "••••" + value[-4:]


def _read_env_file() -> dict[str, str]:
    out: dict[str, str] = {}

    # 1. Read base .env file
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip()

    # 2. Overlay dynamic UI settings from data/system_settings.json
    if SYSTEM_SETTINGS_PATH.exists():
        try:
            with open(SYSTEM_SETTINGS_PATH, "r", encoding="utf-8") as f:
                out.update(json.load(f))
        except Exception:
            pass

    return out


def _write_env_file(values: dict[str, str], *, clear: list[str] | None = None) -> None:
    if _server_settings_are_operator_managed():
        raise PermissionError(
            "Server-level settings are operator-managed in encrypted credential mode"
        )
    existing_lines: list[str] = []
    if ENV_PATH.exists():
        existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    current: dict[str, str] = {}
    if SYSTEM_SETTINGS_PATH.exists():
        try:
            with open(SYSTEM_SETTINGS_PATH, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                current = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            current = {}

    known_keys = set(values.keys()) | set(clear or [])
    for spec in PROVIDERS:
        known_keys.add(spec["key_var"])
        if spec.get("base_var"):
            known_keys.add(spec["base_var"])
        for alt in spec.get("alt_key_vars") or []:
            known_keys.add(alt)
        for option in spec.get("settings") or []:
            if option.get("var"):
                known_keys.add(str(option["var"]))
    known_keys.update(GENERAL_VARS.keys())

    kept: list[str] = []
    seen: set[str] = set()
    for line in existing_lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            kept.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in known_keys:
            if key not in seen:
                seen.add(key)
            continue
        kept.append(line)

    for key in clear or []:
        values.pop(key, None)
        current.pop(key, None)

    new_pairs: list[tuple[str, str]] = []
    for key, val in values.items():
        if val is None or val == "":
            continue
        new_pairs.append((key, val))

    if new_pairs or (clear or []):
        if kept and kept[-1].strip():
            kept.append("")
        kept.append("# Updated via Proving Console settings")
        for key, val in sorted(new_pairs, key=lambda x: x[0]):
            kept.append(f"{key}={val}")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Provider credentials must never inherit a permissive process umask. Do
    # this before writing so an existing loose file is closed to other users
    # before new secrets are persisted.
    ENV_PATH.touch(mode=0o600, exist_ok=True)
    ENV_PATH.chmod(0o600)
    ENV_PATH.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")

    # _read_env_file overlays this mirror on top of .env, so a stale copy would
    # shadow what was just written. Update it in the same call.
    current.update({k: v for k, v in values.items() if v not in (None, "")})
    SYSTEM_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYSTEM_SETTINGS_PATH.touch(mode=0o600, exist_ok=True)
    SYSTEM_SETTINGS_PATH.chmod(0o600)
    with open(SYSTEM_SETTINGS_PATH, "w", encoding="utf-8") as handle:
        json.dump(current, handle, indent=2)

    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=True)
    except ImportError:
        for key, val in values.items():
            if val:
                os.environ[key] = val
        for key in clear or []:
            os.environ.pop(key, None)


def _provider_state(spec: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    key = env.get(spec["key_var"]) or ""
    for alt in spec.get("alt_key_vars") or []:
        if env.get(alt):
            key = env[alt]
            break
    base = env.get(spec.get("base_var") or "", "") or spec.get("default_base")
    options = []
    for option in spec.get("settings") or []:
        item = dict(option)
        var = str(item.get("var") or "")
        item["value"] = env.get(var) or item.get("default") or ""
        options.append(item)
    return {
        "id": spec["id"],
        "label": spec["label"],
        "key_var": spec["key_var"],
        "base_var": spec.get("base_var"),
        "default_base": spec.get("default_base"),
        "models": spec.get("models") or [],
        "docs": spec.get("docs"),
        "api_key_set": bool(key),
        "api_key_masked": _mask(key),
        "base_url": base,
        "settings": options,
        "compatibility_note": spec.get("compatibility_note"),
    }


def _flag_enabled(value: str | None, *, default: bool = True) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in {"0", "false", "off", "no"}


def account_runtime(
    user: dict[str, Any] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Mutually exclusive account routing: API, Codex, or Claude Code."""
    if user is not None and user.get("guest"):
        # An anonymous guest has no Codex or Claude Code connection to consume,
        # so the API route is the only one it can ever take.
        return "api"
    effective = env if env is not None else resolved_user_env(user)
    explicit = str(effective.get("AGENT_MONITOR_ACCOUNT_RUNTIME") or "").strip().lower()
    if explicit in {"api", "codex", "claude"}:
        return explicit

    if _flag_enabled(effective.get("AGENT_MONITOR_USE_CLAUDE"), default=False):
        return "claude"
    if _flag_enabled(effective.get("AGENT_MONITOR_USE_CODEX"), default=True):
        return "codex"
    return "api"


def codex_enabled(
    user: dict[str, Any] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """True when Codex is the one selected account runtime."""
    return account_runtime(user, env=env) == "codex"


def claude_enabled(
    user: dict[str, Any] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """True when Claude Code is the one selected account runtime."""
    return account_runtime(user, env=env) == "claude"


def get_settings(user: dict[str, Any] | None = None) -> dict[str, Any]:
    if user is not None:
        env = resolved_user_env(user)
    else:
        known = _known_env_keys()
        env = {**_read_env_file(), **{k: v for k, v in os.environ.items() if k in known}}
        env.update(_read_env_file())

    sponsored_kimi = (
        user is not None
        and not bool(user.get("guest"))
        and sponsored_kimi_available()
    )
    provider_catalog = (
        [p for p in PROVIDERS if p["id"] in GUEST_PROVIDER_IDS]
        if user and user.get("guest")
        else PROVIDERS
    )
    providers = [_provider_state(p, env) for p in provider_catalog]
    hosted_guest_available = bool(
        user and user.get("guest") and hosted_guest_kimi_available()
    )
    if user and user.get("guest"):
        providers.sort(key=lambda provider: provider["id"] != "kimi")
    for provider in providers:
        if provider["id"] == "kimi":
            provider["sponsored_available"] = sponsored_kimi
            provider["using_sponsored"] = sponsored_kimi and not provider["api_key_set"]
            provider["hosted_available"] = hosted_guest_available
            if hosted_guest_available and not provider["api_key_set"]:
                provider["api_key_set"] = True
                # Do not disclose even a masked operator credential.
                provider["api_key_masked"] = None
    configured = [p["id"] for p in providers if p["api_key_set"]]
    kimi_user_key_set = any(
        provider["id"] == "kimi" and provider["api_key_set"] for provider in providers
    )

    default_model = (
        env.get("AGENT_MONITOR_MODEL")
        or env.get("HERMES_MODEL")
        or (
            GUEST_DEFAULT_MODEL
            if user and user.get("guest")
            else kimi_k3.MODEL_ID if sponsored_kimi else "gpt-5.6-sol"
        )
    )
    max_iter = int(env.get("AGENT_MONITOR_MAX_ITERATIONS") or "40")
    saved_default_engine = str(env.get("AGENT_MONITOR_ENGINE") or "").strip()
    default_engine = _preferred_default_engine(saved_default_engine)

    runtime = account_runtime(user, env=env)
    use_codex = runtime == "codex"
    use_claude = runtime == "claude"
    codex_connected = False
    codex_models: list[str] = []
    if user is not None:
        try:
            from agent_monitor import codex_login

            home = codex_login.account_home(int(user["id"]))
            codex_connected = codex_login.account_login_ready(home)
            if codex_connected:
                codex_models = codex_login.available_models(int(user["id"]))
        except (KeyError, OSError, ValueError):
            codex_models = []

    claude_connected = False
    claude_reauth_required = False
    claude_models: list[str] = []
    if user is not None:
        try:
            from agent_monitor import claude_login

            home = claude_login.account_home(int(user["id"]))
            claude_connected = claude_login.account_login_ready(home)
            if claude_connected:
                claude_models = list(claude_login.DEFAULT_CLAUDE_MODELS)
            else:
                claude_reauth_required = claude_login.account_reauth_required(home)
        except (KeyError, OSError, ValueError):
            claude_models = []

    api_models: list[str] = []
    visible_providers = [p for p in providers if p["api_key_set"]]
    for provider in visible_providers:
        for model_id in provider["models"]:
            if model_id not in api_models:
                api_models.append(model_id)
    if sponsored_kimi and kimi_k3.MODEL_ID not in api_models:
        api_models.insert(0, kimi_k3.MODEL_ID)
    if user is not None and user.get("guest") and GUEST_DEFAULT_MODEL not in api_models:
        # A guest always has the tool-free Kimi harness available, whether it is
        # served by the hosted credential or by a key the visitor supplies.
        api_models.insert(0, GUEST_DEFAULT_MODEL)
    # A model selector is also a billing selector. Never mix dormant API
    # models into an active subscription catalog (or vice versa): a stale
    # ``kimi-*``/Anthropic model left in a dropdown can otherwise redirect a
    # run away from the account runtime the user just selected.
    if use_claude:
        runtime_models = claude_models
    elif use_codex:
        runtime_models = codex_models
    else:
        runtime_models = api_models
    model_presets = list(dict.fromkeys(runtime_models))
    selected_default = (
        default_model
        if default_model in model_presets
        else (model_presets[0] if model_presets else "")
    )
    # Report the selected billing route even when its login is temporarily
    # unavailable. Connectivity is exposed separately; labeling a selected
    # but disconnected subscription as ``api_key`` is misleading and can
    # encourage clients to offer an unsafe fallback.
    auth_mode = {
        "api": "api_key",
        "codex": "codex_subscription",
        "claude": "claude_subscription",
    }[runtime]
    from agent_monitor.run_tuning import capabilities as run_tuning_capabilities

    run_tuning_by_runtime = {
        "api": {
            model_id: run_tuning_capabilities(
                model_id,
                "sponsored_kimi"
                if model_id == kimi_k3.MODEL_ID and sponsored_kimi and not kimi_user_key_set
                else "api_key",
            )
            for model_id in api_models
        },
        "codex": {
            model_id: run_tuning_capabilities(model_id, "codex_subscription")
            for model_id in codex_models
        },
        "claude": {
            model_id: run_tuning_capabilities(model_id, "claude_subscription")
            for model_id in claude_models
        },
    }

    return {
        "env_path": "users.db" if user is not None else str(ENV_PATH),
        "providers": providers,
        "configured_providers": configured,
        "default_model": selected_default,
        "saved_default_model": default_model,
        "hosted_guest_available": hosted_guest_available,
        "max_iterations": max_iter,
        "model_presets": model_presets,
        "runtime_models": model_presets,
        "active_models": model_presets,
        "models_by_runtime": {
            "api": api_models,
            "codex": codex_models,
            "claude": claude_models,
        },
        "run_tuning_by_runtime": run_tuning_by_runtime,
        "runtime_ready": (
            claude_connected
            if use_claude
            else (codex_connected if use_codex else bool(configured or sponsored_kimi))
        ),
        "api_providers_active": runtime == "api",
        "codex_models": codex_models,
        "claude_models": claude_models,
        "api_models": api_models,
        "default_engine": default_engine,
        "saved_default_engine": saved_default_engine,
        "sponsored_kimi_available": sponsored_kimi,
        "sponsored_kimi_engines": sponsored_kimi_engines() if sponsored_kimi else [],
        "kimi_user_key_set": kimi_user_key_set,
        "sponsored_models": [kimi_k3.MODEL_ID] if sponsored_kimi else [],
        "account_runtime": runtime,
        "codex_enabled": use_codex,
        "codex_connected": codex_connected,
        "claude_enabled": use_claude,
        "claude_connected": claude_connected,
        "claude_reauth_required": claude_reauth_required,
        "auth_mode": auth_mode,
        "general": {
            k: {
                "label": meta["label"],
                "value": (
                    default_engine
                    if k == "AGENT_MONITOR_ENGINE"
                    else env.get(k) or ""
                ),
                "type": meta["type"],
            }
            for k, meta in GENERAL_VARS.items()
        },
    }


def save_settings(body: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    updates: dict[str, str] = {}
    clear: list[str] = list(body.get("clear") or [])

    runtime: str | None = None
    if "account_runtime" in body:
        requested = str(body.get("account_runtime") or "").strip().lower()
        if requested not in {"api", "codex", "claude"}:
            raise ValueError("account_runtime must be api, codex, or claude")
        runtime = requested
    elif "claude_enabled" in body:
        use_codex = bool(body.get("codex_enabled"))
        use_claude = bool(body.get("claude_enabled"))
        if use_codex and use_claude:
            raise ValueError("Codex and Claude account runtimes cannot both be enabled")
        runtime = "claude" if use_claude else ("codex" if use_codex else "api")
    elif "codex_enabled" in body:
        # Legacy clients sent only this boolean. Persist the canonical runtime
        # too; otherwise an existing AGENT_MONITOR_ACCOUNT_RUNTIME value wins
        # forever and the switch appears to save while changing nothing.
        runtime = "codex" if bool(body["codex_enabled"]) else "api"
    if runtime is not None:
        updates["AGENT_MONITOR_ACCOUNT_RUNTIME"] = runtime
        updates["AGENT_MONITOR_USE_CODEX"] = "1" if runtime == "codex" else "0"
        updates["AGENT_MONITOR_USE_CLAUDE"] = "1" if runtime == "claude" else "0"

    for key, val in (body.get("updates") or {}).items():
        if not isinstance(key, str):
            continue
        if key == "AGENT_MONITOR_ENGINE":
            val = _preferred_default_engine(str(val or "").strip())
        if key in {
            "AGENT_MONITOR_ACCOUNT_RUNTIME",
            "AGENT_MONITOR_USE_CODEX",
            "AGENT_MONITOR_USE_CLAUDE",
        }:
            continue
        if val is None or val == "":
            clear.append(key)
        else:
            updates[key] = str(val).strip()

    # Empty key fields mean "keep existing" when masked placeholder sent
    for key, val in list(updates.items()):
        if val in {"••••", "****"} or "••••" in val:
            updates.pop(key)

    if user is not None:
        from agent_monitor.auth import set_user_env

        allowed = _known_env_keys()
        if user.get("guest"):
            requested = set(updates) | set(clear)
            disallowed = sorted(requested - GUEST_ENV_KEYS)
            if disallowed:
                raise PermissionError(
                    "Guest sessions may only save a Kimi or OpenAI API key and model"
                )
            allowed &= GUEST_ENV_KEYS
            for key_var in ("OPENAI_API_KEY", "KIMI_API_KEY"):
                if key_var in updates:
                    updates[key_var] = validate_guest_api_key(updates[key_var])
            if "AGENT_MONITOR_MODEL" in updates:
                updates["AGENT_MONITOR_MODEL"] = validate_guest_model(
                    updates["AGENT_MONITOR_MODEL"]
                )
        set_user_env(
            int(user["id"]),
            {k: v for k, v in updates.items() if k in allowed},
            clear=[k for k in clear if k in allowed],
        )
        return {"ok": True, "settings": get_settings(user)}

    if _server_settings_are_operator_managed():
        raise PermissionError(
            "Server-level settings are operator-managed in encrypted credential mode"
        )
    current = _read_env_file()
    merged = {**current, **updates}
    _write_env_file(merged, clear=clear)
    return {"ok": True, "settings": get_settings()}


def _http_json(url: str, *, headers: dict[str, str], method: str = "GET", payload: dict | None = None, timeout: int = 20) -> tuple[int, dict | str]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def verify_provider(
    provider_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    options: dict[str, Any] | None = None,
    user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = next((p for p in PROVIDERS if p["id"] == provider_id), None)
    if not spec:
        raise ValueError(f"Unknown provider: {provider_id}")
    is_guest = bool(user and user.get("guest"))
    if is_guest and provider_id not in GUEST_PROVIDER_IDS:
        raise ValueError("That provider requires a signed-in account")

    env = resolved_user_env(user) if user is not None else _read_env_file()
    key = (api_key or "").strip() or env.get(spec["key_var"]) or ""
    for alt in spec.get("alt_key_vars") or []:
        if not key and env.get(alt):
            key = env[alt]
    if not key:
        if is_guest and provider_id == "kimi" and hosted_guest_kimi_available():
            return {"ok": True, "provider": "kimi", "hosted": True,
                    "message": "Hosted Kimi is configured for your guest runs"}
        return {"ok": False, "provider": provider_id, "error": "API key not set"}
    if is_guest:
        key = validate_guest_api_key(key)

    if is_guest:
        # Canonical provider origins only. Ignore both request input and any
        # legacy stored route so redirects cannot begin at an attacker host.
        base = str(spec.get("default_base") or "")
    else:
        base = (
            (base_url or "").strip()
            or env.get(spec.get("base_var") or "")
            or spec.get("default_base")
            or ""
        )

    if provider_id == "kimi":
        effort = kimi_k3.reasoning_effort(
            env,
            str((options or {}).get(kimi_k3.REASONING_VAR) or ""),
        )
        code, body = _http_json(
            f"{base.rstrip('/')}/chat/completions",
            method="POST",
            headers={"Authorization": f"Bearer {key}"},
            payload=kimi_k3.chat_payload(
                user="Reply with exactly: KIMI_OK",
                effort=effort,
                max_tokens=128,
            ),
        )
        if code == 200 and isinstance(body, dict) and kimi_k3.response_text(body).strip():
            return {
                "ok": True,
                "provider": provider_id,
                "message": f"Kimi K3 Chat Completions succeeded ({effort} reasoning)",
                "model": kimi_k3.MODEL_ID,
            }
        if isinstance(body, dict):
            error = body.get("error")
            err = error.get("message") if isinstance(error, dict) else str(error or body.get("message") or "")
        else:
            err = str(body)
        return {"ok": False, "provider": provider_id, "error": err or f"HTTP {code}"}

    if provider_id == "openai":
        code, body = _http_json(
            f"{base.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        if code == 200:
            count = len(body.get("data") or []) if isinstance(body, dict) else 0
            return {"ok": True, "provider": provider_id, "message": f"{spec['label']} key valid ({count} models)"}
        err = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        return {"ok": False, "provider": provider_id, "error": err or f"HTTP {code}"}

    if provider_id == "anthropic":
        code, body = _http_json(
            f"{base.rstrip('/')}/v1/messages",
            method="POST",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
            payload={
                "model": "claude-haiku-4-5",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        if code == 200:
            return {
                "ok": True,
                "provider": provider_id,
                "message": "Anthropic Messages API call succeeded",
            }
        err = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        return {
            "ok": False,
            "provider": provider_id,
            "error": err or f"Anthropic Messages API returned HTTP {code}",
        }

    if provider_id == "openrouter":
        code, body = _http_json(
            f"{base.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        if code == 200:
            return {"ok": True, "provider": provider_id, "message": "OpenRouter key valid"}
        err = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        return {"ok": False, "provider": provider_id, "error": err or f"HTTP {code}"}

    if provider_id == "google":
        code, body = _http_json(
            f"{base.rstrip('/')}/v1beta/models?key={key}",
            headers={},
        )
        if code == 200:
            return {"ok": True, "provider": provider_id, "message": "Gemini key valid"}
        err = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        return {"ok": False, "provider": provider_id, "error": err or f"HTTP {code}"}

    # OpenAI-compatible providers
    if provider_id in {"deepseek", "groq", "together", "xai"}:
        code, body = _http_json(
            f"{base.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        if code == 200:
            return {"ok": True, "provider": provider_id, "message": f"{spec['label']} key valid"}
        err = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        return {"ok": False, "provider": provider_id, "error": err or f"HTTP {code}"}

    return {"ok": False, "provider": provider_id, "error": "Verification not implemented"}
