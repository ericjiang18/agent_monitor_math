"""Load operator configuration and manage each user's provider settings."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_monitor import DATA_DIR, ROOT
from agent_monitor import kimi_k3

ENV_PATH = Path(os.environ.get("AGENT_MONITOR_ENV_PATH") or ROOT / ".env").expanduser().resolve()
# The UI overlay is host state, so it lives with the rest of the data directory
# (never in the checkout when AGENT_MONITOR_DATA_DIR points elsewhere).
SYSTEM_SETTINGS_PATH = DATA_DIR / "system_settings.json"


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
            "gpt-6.1-sol",
            "gpt-6-sol",
            "gpt-6-luna",
            "chat-latest",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
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
        # Most capable first, as for OpenAI; the first two are Max (API)'s
        # advisors (cgm_max.API_ADVISOR_MODELS).
        "models": [
            "claude-fable-5-1", "claude-opus-5-5", "claude-fable-5", "claude-opus-4-8", "claude-sonnet-5",
            "claude-haiku-4-5",
        ],
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

    return ["kimi", *sponsored_kimi_client.supported_engines()]


_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
GUEST_PROVIDER_IDS = frozenset({"kimi", "openai"})
GUEST_ENV_KEYS = frozenset({"OPENAI_API_KEY", "KIMI_API_KEY", "AGENT_MONITOR_MODEL"})
GUEST_DEFAULT_MODEL = "kimi-k3"


OPERATOR_KIMI_BASE_VAR = "AGENT_MONITOR_KIMI_BASE_URL"


def _https_base(value: object) -> str | None:
    """Return a credential-free HTTPS API base, or ``None``."""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return None
    parsed = urlsplit(raw)
    try:
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return raw


def operator_kimi_base_url() -> str:
    """Endpoint for non-sponsored (own-key or hosted-key) Kimi engine runs.

    Only the operator chooses it: ``AGENT_MONITOR_KIMI_BASE_URL`` from the
    server environment or the operator's server env file.  It is not an
    account setting, so a user-settable ``KIMI_API_BASE`` can never redirect
    a key (SSRF guard).  Anything but a credential-free HTTPS URL falls back
    to ``kimi_k3.DEFAULT_BASE_URL``.
    """
    configured = os.environ.get(OPERATOR_KIMI_BASE_VAR)
    if not str(configured or "").strip():
        configured = _read_env_file().get(OPERATOR_KIMI_BASE_VAR)
    return _https_base(configured) or kimi_k3.DEFAULT_BASE_URL


def _account_base_vars() -> tuple[str, ...]:
    names = [str(spec["base_var"]) for spec in PROVIDERS if spec.get("base_var")]
    return tuple(dict.fromkeys([*names, "AGENT_MONITOR_BASE_URL", "KIMI_BASE_URL"]))


def restrict_account_base_urls(
    env: dict[str, str], user: dict[str, Any] | None
) -> dict[str, str]:
    """SSRF guard: only the operator chooses where server-side calls go.

    For the operator (no account, or a signed-in admin) ``env`` is returned
    unchanged.  For every other account, a provider base URL survives only when
    it is exactly the operator's own server value; anything an account stored
    is dropped so the provider's built-in endpoint is used, and Kimi uses the
    operator's Kimi base (as the ``kimi`` engine does).  Mutates and returns
    ``env``; a sponsored credential applied afterwards still wins.
    """
    if user is None or (bool(user.get("is_admin")) and not user.get("guest")):
        return env
    from agent_monitor import account_policy

    if account_policy.has_full_access(user):
        # It already runs local tools on this host, so the endpoint it chose stands.
        return env
    server = _read_env_file()
    for name in _account_base_vars():
        value = str(env.get(name) or "").strip().rstrip("/")
        if not value:
            continue
        operator_value = str(
            os.environ.get(name) or server.get(name) or ""
        ).strip().rstrip("/")
        if value != operator_value:
            env.pop(name, None)
    env[kimi_k3.BASE_VAR] = operator_kimi_base_url()
    return env


def _member_base_url_clears(user_id: int, updates: dict[str, str]) -> list[str]:
    """The save-time side of ``restrict_account_base_urls`` for a member.

    A member's runs send its keys only to a provider's standard endpoint or
    the operator's own, so a member saves only one of those (a Settings form
    sends the standard endpoints it shows): another base is refused, not
    stored and silently ignored.  Returns the member's stored bases that runs
    ignore (saved before this rule), to be removed.
    """
    from agent_monitor.auth import get_user_env

    server = _read_env_file()
    standard = {
        str(spec["base_var"]): {_default_verify_base(spec), str(spec.get("default_base") or "")}
        for spec in PROVIDERS
        if spec.get("base_var")
    }

    def usable(name: str, value: object) -> bool:
        endpoints = {*standard.get(name, ()), os.environ.get(name) or server.get(name) or ""}
        clean = str(value or "").strip().rstrip("/")
        return not clean or clean in {str(item).strip().rstrip("/") for item in endpoints}

    if any(not usable(name, updates[name]) for name in _account_base_vars() if name in updates):
        raise PermissionError(
            "Custom API base URLs are set by the site operator; "
            "your runs use the provider's standard endpoint"
        )
    stored = get_user_env(user_id)
    return [
        name
        for name in _account_base_vars()
        if name not in updates and not usable(name, stored.get(name))
    ]


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

    # 2. Overlay dynamic UI settings from DATA_DIR/system_settings.json
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


def _settings_env(user: dict[str, Any] | None) -> dict[str, str]:
    """The environment Settings reads for ``user``: its resolved one
    (:func:`resolved_user_env`), or the server's without an account."""
    if user is not None:
        return resolved_user_env(user)
    known = _known_env_keys()
    env = {**_read_env_file(), **{k: v for k, v in os.environ.items() if k in known}}
    env.update(_read_env_file())
    return env


def _api_provider_states(
    user: dict[str, Any] | None, env: dict[str, str]
) -> tuple[list[dict[str, Any]], bool, bool]:
    """``user``'s API provider cards as Settings shows them, from ``env``
    (:func:`_settings_env`), with whether the sponsored Kimi K3 service and
    the hosted guest key serve the account: ``(providers, sponsored_kimi,
    hosted_guest_available)``.  Read from the environment and the sponsored
    service's health alone, never a linked account."""
    sponsored_kimi = (
        user is not None
        and sponsored_kimi_available()
        and (not user.get("guest") or hosted_guest_kimi_available())
    )
    provider_catalog = (
        [p for p in PROVIDERS if p["id"] in GUEST_PROVIDER_IDS]
        if user and user.get("guest")
        else PROVIDERS
    )
    providers = [_provider_state(p, env) for p in provider_catalog]
    hosted_guest_available = bool(user and user.get("guest") and hosted_guest_kimi_available())
    if user and user.get("guest"):
        providers.sort(key=lambda provider: provider["id"] != "kimi")
        for provider in providers:
            # Anonymous visitors never configure an endpoint; the upstream
            # provider URL (a code default or the operator's) stays private.
            provider["base_url"] = ""
            provider["default_base"] = ""
            if provider["id"] == "kimi":
                provider["hosted_available"] = hosted_guest_available
                if hosted_guest_available and not provider["api_key_set"]:
                    provider["api_key_set"] = True
                    provider["api_key_masked"] = None
    for provider in providers:
        if provider["id"] == "kimi":
            provider["sponsored_available"] = sponsored_kimi
            provider["using_sponsored"] = sponsored_kimi and not bool(env.get("KIMI_API_KEY"))
    return providers, sponsored_kimi, hosted_guest_available


def _api_model_list(
    user: dict[str, Any] | None, providers: list[dict[str, Any]], *, sponsored_kimi: bool
) -> list[str]:
    """The API models of these provider cards (:func:`_api_provider_states`),
    as Settings and the console's menus list them: each model of a provider
    whose key is set, in the catalog's order, with Kimi K3 first where the
    sponsored service serves the account, and before all for a guest."""
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
    return api_models


def account_api_models(user: dict[str, Any] | None, *, env: dict[str, str] | None = None) -> list[str]:
    """``get_settings(user)["api_models"]``, without the rest of Settings:
    from the account's provider keys and the sponsored service's health
    alone, so no linked Claude Code or Codex account is probed (Claude
    Code's ``claude auth status`` contacts Anthropic).  ``env``: the
    account's resolved environment (:func:`resolved_user_env`), when the
    caller has it."""
    providers, sponsored_kimi, _hosted = _api_provider_states(
        user, _settings_env(user) if env is None else env
    )
    return _api_model_list(user, providers, sponsored_kimi=sponsored_kimi)


def _easy_route_setting(user: dict[str, Any] | None) -> dict[str, Any]:
    from agent_monitor import easy_route

    # Guests too (their starts: Auto, mathematics only, easy requests on Plain).
    enabled = bool(user) and easy_route.enabled()
    return {"enabled": enabled, "model": easy_route.model() if enabled else ""}


def get_settings(user: dict[str, Any] | None = None) -> dict[str, Any]:
    env = _settings_env(user)
    providers, sponsored_kimi, hosted_guest_available = _api_provider_states(user, env)
    configured = [p["id"] for p in providers if p["api_key_set"]]
    kimi_user_key_set = bool(env.get("KIMI_API_KEY"))

    default_model = (
        env.get("AGENT_MONITOR_MODEL")
        or env.get("HERMES_MODEL")
        or (GUEST_DEFAULT_MODEL if user and user.get("guest") else kimi_k3.MODEL_ID if sponsored_kimi else "gpt-6-astra")
    )
    max_iter = int(env.get("AGENT_MONITOR_MAX_ITERATIONS") or "40")
    saved_default_engine = str(env.get("AGENT_MONITOR_ENGINE") or "").strip()
    default_engine = "kimi" if user and user.get("guest") else _preferred_default_engine(saved_default_engine)

    from agent_monitor import account_policy

    # A restricted member cannot connect or use a subscription, so its runtime
    # is always the API route and its (never linked) account homes are not
    # probed.  No member, full access or not, sees the server's keys.
    member = account_policy.is_member(user)
    restricted = account_policy.is_restricted(user)
    general_env = env
    if member:
        # Public mode lends a member the server's provider keys and endpoints
        # (resolved_user_env).  Like a guest, it sees none of them: only a key
        # (masked) or endpoint it saved itself.
        from agent_monitor.auth import get_user_env

        own = get_user_env(int(user["id"]))
        # Saving Settings before this redaction copied the operator's endpoints
        # into the member's own rows; such a copy is still the operator's.
        server = _read_env_file()
        operator_bases = {
            str(value).strip().rstrip("/")
            for name in _account_base_vars()
            for value in (os.environ.get(name), server.get(name))
            if str(value or "").strip()
        }

        def own_base(name: str) -> bool:
            value = str(own.get(name) or "").strip().rstrip("/")
            return bool(value) and value not in operator_bases

        for provider in providers:
            spec = next(item for item in PROVIDERS if item["id"] == provider["id"])
            provider["api_key_masked"] = _provider_state(spec, own)["api_key_masked"]
            base_var = spec.get("base_var")
            if base_var and env.get(base_var) and not own_base(base_var):
                provider["base_url"] = ""
        if not own_base("AGENT_MONITOR_BASE_URL"):
            general_env = {**env, "AGENT_MONITOR_BASE_URL": ""}
    runtime = "api" if restricted else account_runtime(user, env=env)
    use_codex = runtime == "codex"
    use_claude = runtime == "claude"
    codex_connected = False
    codex_reauth_required = False
    codex_models: list[str] = []
    if user is not None and not user.get("guest") and not restricted:
        try:
            from agent_monitor import codex_login

            home = codex_login.account_home(int(user["id"]))
            codex_connected = codex_login.account_login_ready(home)
            if codex_connected:
                codex_models = codex_login.available_models(int(user["id"]))
            else:
                codex_reauth_required = codex_login.account_reauth_required(home)
        except (KeyError, OSError, ValueError):
            codex_models = []

    # A sign-in a member linked before subscriptions became administrator-only
    # is never used, but it stays on the server until removed.  Look for its
    # credential file only: no CLI probe, and no account home is created.
    stored_logins: dict[str, bool] = {}
    if restricted:
        try:
            from agent_monitor import claude_login, codex_login

            stored_logins = {
                "claude": (claude_login.account_home(int(user["id"])) / ".credentials.json").is_file(),
                "codex": (codex_login.account_home(int(user["id"])) / "auth.json").is_file(),
            }
        except (KeyError, OSError, ValueError):
            stored_logins = {}

    claude_connected = False
    claude_reauth_required = False
    claude_models: list[str] = []
    if user is not None and not user.get("guest") and not restricted:
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

    # Ansatz CGM Max needs both linked accounts, and Max (API) both API keys;
    # the console offers each then.
    cgm_max_plan: dict[str, Any] = {"available": False, "reason": ""}
    cgm_max_api_plan: dict[str, Any] = {"available": False, "reason": ""}
    if user is not None and not user.get("guest"):
        from agent_monitor import cgm_max

        cgm_max_plan = cgm_max.plan_for(user)
        cgm_max_api_plan = cgm_max.plan_for_api(user)

    api_models = _api_model_list(user, providers, sponsored_kimi=sponsored_kimi)
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
        "hosted_guest_available": hosted_guest_available,
        "default_model": selected_default,
        "saved_default_model": default_model,
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
        "codex_reauth_required": codex_reauth_required,
        "claude_enabled": use_claude,
        "claude_connected": claude_connected,
        "claude_reauth_required": claude_reauth_required,
        "cgm_max": cgm_max_plan,
        "cgm_max_api": cgm_max_api_plan,
        # Whether an Ansatz CGM run's turns are rated by the external judge
        # (easy_route), so the launcher can say so; never its key.
        "easy_route": _easy_route_setting(user),
        "subscription_policy": account_policy.subscription_policy(user),
        "subscription_notes": account_policy.subscription_notes() if restricted else {},
        "subscription_stored": stored_logins,
        "auth_mode": auth_mode,
        "general": {
            k: {
                "label": meta["label"],
                "value": (
                    default_engine
                    if k == "AGENT_MONITOR_ENGINE"
                    else general_env.get(k) or ""
                ),
                "type": meta["type"],
            }
            for k, meta in GENERAL_VARS.items()
        },
    }


def save_settings(body: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    updates: dict[str, str] = {}
    clear: list[str] = [
        key for key in (body.get("clear") or []) if isinstance(key, str)
    ]

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
    if runtime is not None and runtime != "api":
        from agent_monitor import account_policy

        if account_policy.is_restricted(user):
            # Subscription sign-in is administrator-only, so a member's runs
            # always bill an API key; keep the stored runtime consistent.
            runtime = "api"
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
        from agent_monitor import account_policy
        from agent_monitor.auth import set_user_env

        allowed = _known_env_keys()
        if account_policy.is_restricted(user):
            clear += _member_base_url_clears(int(user["id"]), updates)
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface a 3xx as its status instead of following it.

    urllib would otherwise forward the Authorization header (a provider key or
    sponsored token) to whatever host a redirect names.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_HTTP_OPENER = urllib.request.build_opener(_NoRedirect())


def _http_json(url: str, *, headers: dict[str, str], method: str = "GET", payload: dict | None = None, timeout: int = 20) -> tuple[int, dict | str]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _HTTP_OPENER.open(req, timeout=timeout) as resp:
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


_SAFE_ERROR_TEXT_RE = re.compile(r"[^\x20-\x7e]+")


def _default_verify_base(spec: dict[str, Any]) -> str:
    if spec["id"] == "kimi":
        return operator_kimi_base_url()
    return str(spec.get("default_base") or "")


def _verify_failure(
    spec: dict[str, Any], code: int, body: object, *, echo_provider_message: bool
) -> dict[str, Any]:
    """A bounded verification error that never reflects a raw upstream body.

    Only a short printable ``error.message`` from a JSON reply of the
    provider's own built-in endpoint may be shown; responses from any custom
    base (or non-JSON bodies) are summarized by HTTP status alone, so the
    check cannot be used to read internal URLs.
    """
    label = str(spec.get("label") or spec["id"])
    message = ""
    if echo_provider_message and isinstance(body, dict):
        error = body.get("error")
        raw = error.get("message") if isinstance(error, dict) else body.get("message")
        if isinstance(raw, str):
            message = " ".join(_SAFE_ERROR_TEXT_RE.sub(" ", raw).split())[:200]
    if not message:
        if code == 0:
            message = f"Could not reach {label}"
        elif code in {401, 403}:
            message = f"{label} rejected this API key (HTTP {code})"
        elif code == 429:
            message = f"{label} is rate-limiting this key (HTTP 429)"
        elif 300 <= code < 400:
            message = f"{label} answered with a redirect (HTTP {code}); redirects are not followed"
        else:
            message = f"{label} verification failed (HTTP {code})"
    return {"ok": False, "provider": spec["id"], "error": message}


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
    # The server-level check (no user) and the admin account are the operator;
    # with an account given full access, they may verify a custom endpoint
    # (the key always comes from the account's own settings); every other
    # account may verify keys only against built-in endpoints.
    from agent_monitor import account_policy

    operator = (
        user is None
        or (bool(user.get("is_admin")) and not is_guest)
        or account_policy.has_full_access(user)
    )

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

    default_base = _default_verify_base(spec)
    if is_guest:
        # Begin every anonymous verification at the canonical provider origin;
        # ignore request input and any stale custom route stored in the DB.
        base = default_base
    elif not operator:
        # SSRF guard: a signed-in non-admin account cannot make the server
        # fetch an arbitrary URL, whether it is sent now or stored earlier.
        # The operator's own endpoint is no custom base: this account's runs
        # keep it (restrict_account_base_urls), a save keeps a copy stored
        # before endpoints were redacted, and the account has no field to
        # clear it.  The key is still checked at the built-in endpoint.
        base_var = str(spec.get("base_var") or "")
        requested = (base_url or "").strip().rstrip("/")
        stored = str(env.get(base_var) or "").strip().rstrip("/")
        operator_base = os.environ.get(base_var) or _read_env_file().get(base_var) or ""
        allowed = {
            default_base.rstrip("/"),
            str(spec.get("default_base") or "").rstrip("/"),
            str(operator_base).strip().rstrip("/"),
        }
        if any(value and value not in allowed for value in (requested, stored)):
            return {
                "ok": False,
                "provider": provider_id,
                "error": (
                    "Custom API base URLs can be verified only by the operator; "
                    "clear the custom base to verify this key"
                ),
            }
        base = default_base
    else:
        base = (
            (base_url or "").strip()
            or env.get(spec.get("base_var") or "")
            or default_base
            or ""
        )
    echo_provider_message = base.rstrip("/") == default_base.rstrip("/")

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
        if code == 200:
            return {
                "ok": False,
                "provider": provider_id,
                "error": "Kimi K3 returned no text for the verification prompt",
            }
        return _verify_failure(
            spec, code, body, echo_provider_message=echo_provider_message
        )

    if provider_id == "openai":
        code, body = _http_json(
            f"{base.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        if code == 200:
            count = len(body.get("data") or []) if isinstance(body, dict) else 0
            return {"ok": True, "provider": provider_id, "message": f"{spec['label']} key valid ({count} models)"}
        return _verify_failure(
            spec, code, body, echo_provider_message=echo_provider_message
        )

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
        return _verify_failure(
            spec, code, body, echo_provider_message=echo_provider_message
        )

    if provider_id == "openrouter":
        code, body = _http_json(
            f"{base.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        if code == 200:
            return {"ok": True, "provider": provider_id, "message": "OpenRouter key valid"}
        return _verify_failure(
            spec, code, body, echo_provider_message=echo_provider_message
        )

    if provider_id == "google":
        code, body = _http_json(
            f"{base.rstrip('/')}/v1beta/models?key={key}",
            headers={},
        )
        if code == 200:
            return {"ok": True, "provider": provider_id, "message": "Gemini key valid"}
        return _verify_failure(
            spec, code, body, echo_provider_message=echo_provider_message
        )

    # OpenAI-compatible providers
    if provider_id in {"deepseek", "groq", "together", "xai"}:
        code, body = _http_json(
            f"{base.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        if code == 200:
            return {"ok": True, "provider": provider_id, "message": f"{spec['label']} key valid"}
        return _verify_failure(
            spec, code, body, echo_provider_message=echo_provider_message
        )

    return {"ok": False, "provider": provider_id, "error": "Verification not implemented"}
