"""Kimi K3 OpenAI-compatible Chat Completions adapter."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Mapping

MODEL_ID = "kimi-k3"
KEY_VAR = "KIMI_API_KEY"
BASE_VAR = "KIMI_API_BASE"
REASONING_VAR = "KIMI_REASONING_EFFORT"
DEFAULT_BASE_URL = "https://2qg3r7w8aefiukbds.flashflame.ai/v1"
ALLOWED_REASONING_EFFORTS = ("low", "high", "max")
DEFAULT_REASONING_EFFORT = "max"
DEFAULT_RETRY_ATTEMPTS = 3
_RETRY_DELAYS_S = (0.25, 0.75)

# LiteLLM may add these OpenAI Responses fields. Kimi K3's endpoint is Chat
# Completions compatible, so the native adapter never includes them.
DROPPED_OPENAI_PARAMS = ("output_config", "context_management")


def reasoning_effort(env: Mapping[str, str] | None = None, value: str | None = None) -> str:
    requested = str(value or (env or {}).get(REASONING_VAR) or DEFAULT_REASONING_EFFORT).strip().lower()
    return requested if requested in ALLOWED_REASONING_EFFORTS else DEFAULT_REASONING_EFFORT


def chat_payload(
    *,
    user: str,
    system: str | None = None,
    effort: str | None = None,
    env: Mapping[str, str] | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    if system and system.strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload: dict[str, Any] = {
        "model": MODEL_ID,
        "reasoning_effort": reasoning_effort(env, effort),
        "messages": messages,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max(1, int(max_tokens))
    return payload


def response_text(body: Mapping[str, Any]) -> str:
    try:
        content = body["choices"][0]["message"]["content"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("text")
        )
    return ""


def response_usage(body: Mapping[str, Any]) -> dict[str, int]:
    usage = body.get("usage") or {}
    if not isinstance(usage, Mapping):
        usage = {}
    completion_details = usage.get("completion_tokens_details") or {}
    if not isinstance(completion_details, Mapping):
        completion_details = {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    if not isinstance(prompt_details, Mapping):
        prompt_details = {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
        "cache_read_tokens": int(prompt_details.get("cached_tokens") or usage.get("cached_tokens") or 0),
    }


def retryable_response(code: int, body: object) -> bool:
    """Return whether a Kimi Chat response is safe to retry.

    Authentication and other permanent 4xx responses deliberately fail on the
    first attempt.  A successful HTTP response with no assistant text is
    transient in practice at the Kimi-compatible gateway and is retried too.
    """
    if code == 0 or code in {408, 429} or 500 <= code <= 599:
        return True
    if code != 200:
        return False
    return not isinstance(body, Mapping) or not response_text(body).strip()


def request_with_retry(
    request: Callable[[], tuple[int, object]],
    *,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
) -> tuple[int, object]:
    """Run one idempotent Kimi Chat request with bounded deterministic backoff.

    The callback owns all request details, so this helper never receives,
    stores, or logs an API key.  Attempts are capped at three even if a caller
    supplies a larger value.
    """
    total = max(1, min(int(attempts), DEFAULT_RETRY_ATTEMPTS))
    pause = sleep or time.sleep
    code: int = 0
    body: object = ""
    for index in range(total):
        code, body = request()
        if not retryable_response(code, body) or index + 1 >= total:
            break
        pause(_RETRY_DELAYS_S[min(index, len(_RETRY_DELAYS_S) - 1)])
    return code, body
