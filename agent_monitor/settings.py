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
        "label": "Kimi / Moonshot",
        "key_var": "KIMI_API_KEY",
        "base_var": "KIMI_BASE_URL",
        "default_base": "https://api.moonshot.ai/v1",
        "models": ["kimi-k3", "kimi-k2.6", "kimi-k2.7-code"],
        "docs": "https://platform.kimi.ai/docs/models",
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
        "models": [],
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
    "HERMES_MODEL": {"label": "Hermes model override", "type": "model"},
    "AGENT_MONITOR_BASE_URL": {"label": "Custom API base (optional)", "type": "url"},
    "AGENT_MONITOR_MAX_ITERATIONS": {"label": "Max agent iterations", "type": "number"},
    "AGENT_MONITOR_USE_CODEX": {"label": "Use Codex subscription", "type": "boolean"},
}

_SECRET_RE = re.compile(r"^sk-[A-Za-z0-9._-]{8,}$|^[A-Za-z0-9._-]{20,}$")
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
    return keys


def resolved_user_env(user: dict[str, Any] | None) -> dict[str, str]:
    """Return only the provider configuration this account is allowed to use.

    Per-user values always win. Only the operator/admin account inherits server
    credentials; regular and guest accounts must provide their own keys.
    """
    out: dict[str, str] = {}
    if user is None or (user.get("is_admin") and not user.get("guest")):
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


def codex_enabled(
    user: dict[str, Any] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """Return whether this account selected its private Codex connection."""
    effective = env if env is not None else resolved_user_env(user)
    raw = str(effective.get("AGENT_MONITOR_USE_CODEX") or "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


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
    current: dict[str, str] = {}
    if SYSTEM_SETTINGS_PATH.exists():
        try:
            with open(SYSTEM_SETTINGS_PATH, "r", encoding="utf-8") as f:
                current = json.load(f)
        except Exception:
            pass

    current.update(values)

    for key in clear or []:
        current.pop(key, None)

    # Ensure data directory exists and save safely
    SYSTEM_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SYSTEM_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)

    # Update active process environment in memory
    for key, val in values.items():
        if val:
            os.environ[key] = str(val)
    for key in clear or []:
        os.environ.pop(key, None)

def _provider_state(spec: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    key = env.get(spec["key_var"]) or ""
    for alt in spec.get("alt_key_vars") or []:
        if env.get(alt):
            key = env[alt]
            break
    base = env.get(spec.get("base_var") or "", "") or spec.get("default_base")
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
    }


def get_settings(user: dict[str, Any] | None = None) -> dict[str, Any]:
    if user is not None:
        env = resolved_user_env(user)
    else:
        env = {**_read_env_file(), **{k: v for k, v in os.environ.items() if k.endswith("_API_KEY") or k in GENERAL_VARS}}
        env.update(_read_env_file())

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
            if provider["id"] == "kimi":
                provider["hosted_available"] = hosted_guest_available
                if hosted_guest_available and not provider["api_key_set"]:
                    provider["api_key_set"] = True
                    # Do not disclose even a masked operator credential.
                    provider["api_key_masked"] = None
    configured = [p["id"] for p in providers if p["api_key_set"]]

    default_model = (
        env.get("AGENT_MONITOR_MODEL")
        or env.get("HERMES_MODEL")
        or (GUEST_DEFAULT_MODEL if user and user.get("guest") else "gpt-6-astra")
    )
    max_iter = int(env.get("AGENT_MONITOR_MAX_ITERATIONS") or "40")

    all_models: list[str] = []
    for p in providers:
        for m in p["models"]:
            if m not in all_models:
                all_models.append(m)
    if default_model not in all_models:
        all_models.insert(0, default_model)

    return {
        "env_path": "users.db" if user is not None else str(ENV_PATH),
        "providers": providers,
        "configured_providers": configured,
        "hosted_guest_available": hosted_guest_available,
        "default_model": default_model,
        "max_iterations": max_iter,
        "model_presets": all_models,
        "general": {
            k: {
                "label": meta["label"],
                "value": env.get(k) or "",
                "type": meta["type"],
            }
            for k, meta in GENERAL_VARS.items()
        },
    }


def save_settings(body: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    updates: dict[str, str] = {}
    clear: list[str] = list(body.get("clear") or [])

    for key, val in (body.get("updates") or {}).items():
        if not isinstance(key, str):
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

    if provider_id in {"openai", "kimi"}:
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
        if code in (200, 400):  # 400 may mean model id mismatch but auth ok
            if isinstance(body, dict) and body.get("type") == "error":
                msg = body.get("error", {}).get("message", "")
                if "authentication" in msg.lower() or "api key" in msg.lower():
                    return {"ok": False, "provider": provider_id, "error": msg}
            return {"ok": True, "provider": provider_id, "message": "Anthropic key accepted"}
        err = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        return {"ok": False, "provider": provider_id, "error": err or f"HTTP {code}"}

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
