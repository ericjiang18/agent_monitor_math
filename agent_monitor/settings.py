"""Settings: LiteLLM-style provider keys stored in Agent_Monitor/.env."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent_monitor import ROOT

ENV_PATH = ROOT / ".env"

# Provider catalog — mirrors common LiteLLM env vars.
PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "label": "OpenAI",
        "key_var": "OPENAI_API_KEY",
        "base_var": "OPENAI_BASE_URL",
        "default_base": "https://api.openai.com/v1",
        "models": ["gpt-4.1", "gpt-4o", "gpt-4o-mini", "o3-mini", "gpt-5.6-sol"],
        "docs": "https://docs.litellm.ai/docs/providers/openai",
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "key_var": "ANTHROPIC_API_KEY",
        "base_var": "ANTHROPIC_BASE_URL",
        "default_base": "https://api.anthropic.com",
        "models": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"],
        "docs": "https://docs.litellm.ai/docs/providers/anthropic",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "key_var": "OPENROUTER_API_KEY",
        "base_var": "OPENROUTER_BASE_URL",
        "default_base": "https://openrouter.ai/api/v1",
        "models": ["openai/gpt-4o", "anthropic/claude-sonnet-4", "google/gemini-2.5-pro"],
        "docs": "https://docs.litellm.ai/docs/providers/openrouter",
    },
    {
        "id": "google",
        "label": "Google Gemini",
        "key_var": "GEMINI_API_KEY",
        "alt_key_vars": ["GOOGLE_API_KEY"],
        "base_var": "GEMINI_API_BASE",
        "default_base": "https://generativelanguage.googleapis.com",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
        "docs": "https://docs.litellm.ai/docs/providers/gemini",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "key_var": "DEEPSEEK_API_KEY",
        "base_var": "DEEPSEEK_API_BASE",
        "default_base": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "docs": "https://docs.litellm.ai/docs/providers/deepseek",
    },
    {
        "id": "groq",
        "label": "Groq",
        "key_var": "GROQ_API_KEY",
        "base_var": "GROQ_API_BASE",
        "default_base": "https://api.groq.com/openai/v1",
        "models": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
        "docs": "https://docs.litellm.ai/docs/providers/groq",
    },
    {
        "id": "together",
        "label": "Together AI",
        "key_var": "TOGETHERAI_API_KEY",
        "base_var": "TOGETHERAI_API_BASE",
        "default_base": "https://api.together.xyz/v1",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"],
        "docs": "https://docs.litellm.ai/docs/providers/together_ai",
    },
    {
        "id": "xai",
        "label": "xAI",
        "key_var": "XAI_API_KEY",
        "base_var": "XAI_API_BASE",
        "default_base": "https://api.x.ai/v1",
        "models": ["grok-3", "grok-3-mini"],
        "docs": "https://docs.litellm.ai/docs/providers/xai",
    },
]

GENERAL_VARS = {
    "AGENT_MONITOR_MODEL": {"label": "Default model", "type": "model"},
    "HERMES_MODEL": {"label": "Hermes model override", "type": "model"},
    "AGENT_MONITOR_BASE_URL": {"label": "Custom API base (optional)", "type": "url"},
    "AGENT_MONITOR_MAX_ITERATIONS": {"label": "Max agent iterations", "type": "number"},
}

_SECRET_RE = re.compile(r"^sk-[A-Za-z0-9._-]{8,}$|^[A-Za-z0-9._-]{20,}$")


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "••••"
    return value[:4] + "••••" + value[-4:]


def _read_env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def _write_env_file(values: dict[str, str], *, clear: list[str] | None = None) -> None:
    existing_lines: list[str] = []
    if ENV_PATH.exists():
        existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    known_keys = set(values.keys()) | set(clear or [])
    for spec in PROVIDERS:
        known_keys.add(spec["key_var"])
        if spec.get("base_var"):
            known_keys.add(spec["base_var"])
        for alt in spec.get("alt_key_vars") or []:
            known_keys.add(alt)
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
    ENV_PATH.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")

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


def get_settings() -> dict[str, Any]:
    env = {**_read_env_file(), **{k: v for k, v in os.environ.items() if k.endswith("_API_KEY") or k in GENERAL_VARS}}
    file_env = _read_env_file()
    env.update(file_env)

    providers = [_provider_state(p, env) for p in PROVIDERS]
    configured = [p["id"] for p in providers if p["api_key_set"]]

    default_model = (
        env.get("AGENT_MONITOR_MODEL")
        or env.get("HERMES_MODEL")
        or "gpt-5.6-sol"
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
        "env_path": str(ENV_PATH),
        "providers": providers,
        "configured_providers": configured,
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


def save_settings(body: dict[str, Any]) -> dict[str, Any]:
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
) -> dict[str, Any]:
    spec = next((p for p in PROVIDERS if p["id"] == provider_id), None)
    if not spec:
        raise ValueError(f"Unknown provider: {provider_id}")

    env = _read_env_file()
    key = (api_key or "").strip() or env.get(spec["key_var"]) or ""
    for alt in spec.get("alt_key_vars") or []:
        if not key and env.get(alt):
            key = env[alt]
    if not key:
        return {"ok": False, "provider": provider_id, "error": "API key not set"}

    base = (base_url or "").strip() or env.get(spec.get("base_var") or "") or spec.get("default_base") or ""

    if provider_id == "openai":
        code, body = _http_json(
            f"{base.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        if code == 200:
            count = len(body.get("data") or []) if isinstance(body, dict) else 0
            return {"ok": True, "provider": provider_id, "message": f"OpenAI key valid ({count} models)"}
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
                "model": "claude-sonnet-4-20250514",
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
