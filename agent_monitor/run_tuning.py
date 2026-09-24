"""Model-aware, run-scoped reasoning and speed controls.

The UI treats these values as part of a run's immutable routing contract.  This
module is deliberately provider-credential free so validation can happen
before a workspace, thread, or model request is created.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

REASONING_ENV = "AGENT_MONITOR_RUN_REASONING_EFFORT"
SPEED_ENV = "AGENT_MONITOR_RUN_SPEED_MODE"

DEFAULT_REASONING = "default"
DEFAULT_SPEED = "standard"

_OPENAI_REASONING = ("default", "low", "medium", "high", "xhigh", "max")
_CLAUDE_REASONING = ("default", "low", "medium", "high", "xhigh", "max")
_KIMI_REASONING = ("default", "low", "high", "max")
_SPEEDS = ("standard", "fast")

_OPENAI_TUNING_ENGINES = frozenset(
    {"codex", "deepagents", "hermes", "math_harness", "metaharness", "plain"}
)
_KIMI_TUNING_ENGINES = frozenset(
    {
        "codex", "deepagents", "deepseek_harness", "math_harness",
        "metaharness", "openclaw", "plain",
    }
)
_CLAUDE_TUNING_ENGINES = frozenset(
    {"claude", "deepagents", "math_harness", "metaharness", "plain"}
)

_REASONING_LABELS = {
    "default": "Default",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Ultra",
}
_SPEED_LABELS = {"standard": "Standard", "fast": "Fast"}


def _bare_model(model: str | None) -> str:
    value = str(model or "").strip().lower()
    for prefix in (
        "openai:", "openai/", "anthropic:", "anthropic/", "kimi:", "kimi/"
    ):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def capabilities(
    model: str | None,
    auth_route: str | None,
    engine: str | None = None,
) -> dict[str, Any]:
    """Return only controls that the selected model route actually supports."""
    selected = _bare_model(model)
    route = str(auth_route or "").strip().lower()
    reasoning: tuple[str, ...] = (DEFAULT_REASONING,)
    speeds: tuple[str, ...] = (DEFAULT_SPEED,)
    supported_engines: frozenset[str] = frozenset()

    if selected.startswith("kimi-") and route == "api_key":
        reasoning = _KIMI_REASONING
        supported_engines = _KIMI_TUNING_ENGINES
    elif route == "claude_subscription" and (
        selected.startswith("claude-") or selected in {"sonnet", "opus", "fable"}
    ):
        # Claude Code currently omits --effort for Haiku 4.5.
        if "haiku" not in selected:
            reasoning = _CLAUDE_REASONING
            supported_engines = _CLAUDE_TUNING_ENGINES
    elif selected.startswith("gpt-5.6") and route in {
        "api_key",
        "codex_subscription",
    }:
        reasoning = _OPENAI_REASONING
        speeds = _SPEEDS
        supported_engines = _OPENAI_TUNING_ENGINES

    selected_engine = str(engine or "").strip().lower()
    if selected_engine and selected_engine not in supported_engines:
        reasoning = (DEFAULT_REASONING,)
        speeds = (DEFAULT_SPEED,)

    return {
        "reasoning": [
            {"id": item, "label": _REASONING_LABELS[item]} for item in reasoning
        ],
        "speed": [{"id": item, "label": _SPEED_LABELS[item]} for item in speeds],
        "default_reasoning_effort": DEFAULT_REASONING,
        "default_speed_mode": DEFAULT_SPEED,
        "supported_engines": sorted(supported_engines),
    }


def normalize(
    model: str | None,
    auth_route: str | None,
    *,
    reasoning_effort: object = None,
    speed_mode: object = None,
    engine: str | None = None,
) -> dict[str, str]:
    """Validate an explicit UI selection without allowing provider fallthrough."""
    requested_reasoning = str(reasoning_effort or DEFAULT_REASONING).strip().lower()
    requested_speed = str(speed_mode or DEFAULT_SPEED).strip().lower()
    available = capabilities(model, auth_route, engine)
    reasoning_ids = {item["id"] for item in available["reasoning"]}
    speed_ids = {item["id"] for item in available["speed"]}
    if requested_reasoning not in reasoning_ids:
        raise ValueError(
            f"{model or 'This model'} does not support reasoning effort "
            f"{requested_reasoning!r} on {auth_route or 'this route'}"
        )
    if requested_speed not in speed_ids:
        raise ValueError(
            f"{model or 'This model'} does not support speed mode "
            f"{requested_speed!r} on {auth_route or 'this route'}"
        )
    return {
        "reasoning_effort": requested_reasoning,
        "speed_mode": requested_speed,
    }


def apply_environment(
    env: MutableMapping[str, str],
    *,
    model: str | None,
    auth_route: str | None,
    reasoning_effort: object = None,
    speed_mode: object = None,
) -> dict[str, str]:
    """Validate and project a run profile into supported runner variables."""
    tuning = normalize(
        model,
        auth_route,
        reasoning_effort=reasoning_effort,
        speed_mode=speed_mode,
    )
    env[REASONING_ENV] = tuning["reasoning_effort"]
    env[SPEED_ENV] = tuning["speed_mode"]
    selected = _bare_model(model)
    effort = tuning["reasoning_effort"]
    if effort != DEFAULT_REASONING:
        if selected.startswith("kimi-"):
            env["KIMI_REASONING_EFFORT"] = effort
        if auth_route == "claude_subscription":
            env["AGENT_MONITOR_CLAUDE_EFFORT"] = effort
        if auth_route == "codex_subscription":
            env["OPENHANDS_CODEX_REASONING_EFFORT"] = effort
    return tuning


def from_record(env: MutableMapping[str, str], record: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Apply a persisted profile; legacy records without one remain unchanged."""
    data = record or {}
    if "reasoning_effort" not in data and "speed_mode" not in data:
        return None
    return apply_environment(
        env,
        model=str(data.get("model") or "") or None,
        auth_route=str(data.get("auth_route") or "") or None,
        reasoning_effort=data.get("reasoning_effort"),
        speed_mode=data.get("speed_mode"),
    )


def openai_payload_options(env: Mapping[str, str], model: str | None) -> dict[str, Any]:
    """Translate a validated profile to Responses API request fields."""
    effort = str(env.get(REASONING_ENV) or "").strip().lower()
    speed = str(env.get(SPEED_ENV) or "").strip().lower()
    caps = capabilities(model, "api_key")
    reasoning_ids = {item["id"] for item in caps["reasoning"]}
    speed_ids = {item["id"] for item in caps["speed"]}
    out: dict[str, Any] = {}
    if effort and effort != DEFAULT_REASONING and effort in reasoning_ids:
        out["reasoning"] = {"effort": effort}
    if speed in speed_ids and len(speed_ids) > 1:
        out["service_tier"] = "fast" if speed == "fast" else "default"
    return out


def codex_config_args(env: Mapping[str, str], model: str | None) -> list[str]:
    """Translate a validated profile to exact Codex CLI config overrides."""
    if _bare_model(model).startswith("kimi-"):
        return []
    effort = str(env.get(REASONING_ENV) or "").strip().lower()
    speed = str(env.get(SPEED_ENV) or "").strip().lower()
    caps = capabilities(model, "codex_subscription")
    reasoning_ids = {item["id"] for item in caps["reasoning"]}
    speed_ids = {item["id"] for item in caps["speed"]}
    args: list[str] = []
    if effort and effort != DEFAULT_REASONING and effort in reasoning_ids:
        args.extend(["-c", f'model_reasoning_effort="{effort}"'])
    if speed in speed_ids and len(speed_ids) > 1:
        tier = "fast" if speed == "fast" else "default"
        args.extend(["-c", f'service_tier="{tier}"'])
    return args
