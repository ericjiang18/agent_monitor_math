"""Provider-aware one-shot API backend for non-agentic proof runners."""
from __future__ import annotations

import os
from dataclasses import dataclass


_OPENAI_COMPATIBLE_PROVIDERS = frozenset(
    {"openrouter", "deepseek", "groq", "together", "xai"}
)


class APIBackendError(RuntimeError):
    """Raised when a direct provider call cannot produce a response."""


@dataclass(frozen=True)
class APIResult:
    text: str
    usage: dict[str, int]
    model: str
    provider: str


def _safe_error(detail: object, secret: str) -> str:
    text = str(detail or "unknown error")
    if secret:
        text = text.replace(secret, "[redacted]")
    return text[-1600:]


def _chat_response_text(body: dict[str, object]) -> str:
    """Extract text from OpenAI-compatible Chat Completions responses."""
    choices = body.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("text") is not None:
            parts.append(str(block["text"]))
    return "\n".join(parts)


def api_chat(
    system: str,
    user: str,
    *,
    model: str,
    timeout: int = 240,
) -> APIResult:
    """Call the provider implied by ``model`` using the user's resolved env."""
    from agent_monitor.proof_graph import (
        _api_error,
        _openai_response_text,
        _resolve_llm_provider,
    )
    from agent_monitor.settings import _http_json

    try:
        provider, base, key, chosen = _resolve_llm_provider(
            dict(os.environ), model
        )
    except (TypeError, ValueError) as exc:
        raise APIBackendError(str(exc)) from exc

    try:
        max_output = max(
            1024,
            min(int(os.environ.get("AGENT_MONITOR_API_MAX_TOKENS") or 16384), 128000),
        )
    except ValueError as exc:
        raise APIBackendError("AGENT_MONITOR_API_MAX_TOKENS must be an integer") from exc

    # A bounded run (currently anonymous guest sessions) carries a hard output
    # ceiling. It only ever lowers the limit above, and it is not negotiable by
    # the caller's own AGENT_MONITOR_API_MAX_TOKENS.
    ceiling_raw = str(os.environ.get("AGENT_MONITOR_MAX_OUTPUT_TOKENS") or "").strip()
    if ceiling_raw:
        try:
            ceiling = int(ceiling_raw)
        except ValueError as exc:
            raise APIBackendError(
                "AGENT_MONITOR_MAX_OUTPUT_TOKENS must be an integer"
            ) from exc
        if ceiling > 0:
            max_output = min(max_output, ceiling)

    if provider == "openai":
        url = f"{base}/responses"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": chosen,
            "instructions": system,
            "input": user,
            "max_output_tokens": max_output,
            "store": False,
        }
        from agent_monitor.run_tuning import openai_payload_options

        payload.update(openai_payload_options(os.environ, chosen))
    elif provider == "kimi":
        from agent_monitor import kimi_k3

        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = kimi_k3.chat_payload(
            system=system,
            user=user,
            env=os.environ,
            max_tokens=max_output,
        )
    elif provider == "anthropic":
        api_base = base if base.endswith("/v1") else f"{base}/v1"
        url = f"{api_base}/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": chosen,
            "system": system,
            "max_tokens": max_output,
            "messages": [{"role": "user", "content": user}],
        }
    elif provider == "google":
        api_base = base if base.endswith("/v1beta") else f"{base}/v1beta"
        url = f"{api_base}/models/{chosen}:generateContent?key={key}"
        headers = {}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [
                {"role": "user", "parts": [{"text": user}]},
            ],
            "generationConfig": {"maxOutputTokens": max_output},
        }
    elif provider in _OPENAI_COMPATIBLE_PROVIDERS:
        api_base = base if base.endswith("/v1") else f"{base}/v1"
        url = f"{api_base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": chosen,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_output,
        }
    else:
        raise APIBackendError(
            f"Direct {provider} calls are not yet supported by this harness; "
            "choose a provider shown in Settings"
        )

    def request() -> tuple[int, object]:
        return _http_json(
            url,
            method="POST",
            headers=headers,
            payload=payload,
            timeout=timeout,
        )

    try:
        if provider == "kimi":
            from agent_monitor import kimi_k3

            # Server-sponsored traffic is admitted and billed per logical model
            # call, whether anonymous or registered. Never replay an ambiguous
            # timeout/5xx/empty response: the upstream may already have processed
            # it. User-key runs retain the existing bounded reliability retries.
            attempts = (
                1
                if os.environ.get("AGENT_MONITOR_PUBLIC_KIMI", "").strip() == "1"
                or os.environ.get("AGENT_MONITOR_SPONSORED_KIMI", "").strip()
                == "1"
                else kimi_k3.DEFAULT_RETRY_ATTEMPTS
            )
            code, body = kimi_k3.request_with_retry(request, attempts=attempts)
        else:
            code, body = request()
    except Exception as exc:
        # Gemini authenticates in its query string.  Unexpected transport
        # exceptions can include the full URL, so suppress the original cause
        # after projecting a key-redacted diagnostic.
        raise APIBackendError(
            f"{provider.title()} model transport failed for {chosen}: "
            f"{_safe_error(exc, key)}"
        ) from None
    if code != 200 or not isinstance(body, dict):
        detail = _api_error(body)
        raise APIBackendError(
            f"{provider.title()} model call failed for {chosen} (HTTP {code}): "
            f"{_safe_error(detail, key)}"
        )

    try:
        if provider == "openai":
            text = _openai_response_text(body)
            raw_usage = body.get("usage") or {}
            usage = {
                "input_tokens": int(raw_usage.get("input_tokens") or 0),
                "output_tokens": int(raw_usage.get("output_tokens") or 0),
            }
        elif provider == "kimi":
            from agent_monitor import kimi_k3

            text = kimi_k3.response_text(body)
            usage = kimi_k3.response_usage(body)
        elif provider == "anthropic":
            text = "\n".join(
                str(block.get("text") or "")
                for block in body.get("content") or []
                if isinstance(block, dict) and block.get("type") == "text"
            )
            raw_usage = body.get("usage") or {}
            usage = {
                "input_tokens": int(raw_usage.get("input_tokens") or 0),
                "output_tokens": int(raw_usage.get("output_tokens") or 0),
            }
        elif provider == "google":
            text = "\n".join(
                str(part.get("text") or "")
                for candidate in body.get("candidates") or []
                if isinstance(candidate, dict)
                for part in ((candidate.get("content") or {}).get("parts") or [])
                if isinstance(part, dict) and part.get("text")
            )
            raw_usage = body.get("usageMetadata") or {}
            usage = {
                "input_tokens": int(raw_usage.get("promptTokenCount") or 0),
                "output_tokens": int(raw_usage.get("candidatesTokenCount") or 0),
            }
        else:
            text = _chat_response_text(body)
            raw_usage = body.get("usage") or {}
            if not isinstance(raw_usage, dict):
                raw_usage = {}
            usage = {
                "input_tokens": int(raw_usage.get("prompt_tokens") or 0),
                "output_tokens": int(raw_usage.get("completion_tokens") or 0),
            }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise APIBackendError(
            f"{provider.title()} returned an unexpected response shape for {chosen}"
        ) from exc
    if not text.strip():
        raise APIBackendError(f"{provider.title()} returned an empty response for {chosen}")
    return APIResult(
        text=text.strip(), usage=usage, model=chosen, provider=provider
    )
