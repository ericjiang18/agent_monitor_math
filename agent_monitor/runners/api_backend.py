"""Provider-aware one-shot API backend for non-agentic proof runners."""
from __future__ import annotations

import math
import os
import re
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote


_OPENAI_COMPATIBLE_PROVIDERS = frozenset(
    {"openrouter", "deepseek", "groq", "together", "xai"}
)
# Per-call reasoning effort Anthropic (output_config.effort) and OpenAI
# (reasoning.effort) accept; Kimi has its own, smaller set (kimi_k3).
_API_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
# Models whose refusals the Claude API can re-run server-side on the model
# Anthropic recommends for the refusal's category (beta; Claude API only).
_ANTHROPIC_FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
_ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Only a request the API refused before processing it (429 rate limited,
# 529 overloaded) is sent again, at most this often: anything else may have
# been processed, and a second attempt would be billed again.
_ANTHROPIC_RETRY_STATUSES = frozenset({429, 529})
_ANTHROPIC_RETRIES = 2
# The wait before each retry when the refusal names none (seconds).
_ANTHROPIC_RETRY_WAITS = (2.0, 4.0)
# A retry is sent only if at least this long remains for it after the wait.
_ANTHROPIC_MIN_ATTEMPT_SECONDS = 5.0
# Each attempt's connection must open within this long (seconds).
_ANTHROPIC_CONNECT_SECONDS = 10.0
# The output limit of a call that sets none (AGENT_MONITOR_API_MAX_TOKENS).
# An OpenAI call at extra-high or maximum effort reasons past the usual
# limit (reasoning counts against it) and then has no room left to answer,
# so it gets the limit Max (API) uses.
_DEFAULT_MAX_TOKENS = 16384
_HIGH_EFFORT_MAX_TOKENS = 64_000
_HIGH_EFFORTS = frozenset({"xhigh", "max"})
# An unpaired UTF-16 surrogate (a reply's JSON escape for half of an emoji)
# cannot be written as UTF-8; it becomes U+FFFD.
_LONE_SURROGATES = re.compile("[\ud800-\udfff]")
# A call's total timeout when the caller sets none; such long reasoning also
# needs the time to write its 64,000 tokens (Max (API) allows 1800 s too).
_DEFAULT_TIMEOUT = 240
HIGH_EFFORT_TIMEOUT = 1800


class APIBackendError(RuntimeError):
    """Raised when a direct provider call cannot produce a response.

    ``failure`` carries the content-free facts of a failed sponsored Kimi call
    (``kimi_k3.CallFailure``; ``None`` otherwise).  ``usage`` totals the 2xx
    replies that came back before the call failed (an empty completion is
    still billed), and ``usage_by_model`` splits it by model as
    ``APIResult.usage_by_model`` does.  ``kind`` classifies an own key's
    refusal that every later call would repeat, as ``CodexBackendError.kind``
    does: ``"auth"`` (the provider rejects the key), ``"usage_limit"`` (the
    account is out of credit or quota; a rate limit is not one), ``"model"``
    (the key cannot use the model); ``None`` otherwise, and always for
    sponsored Kimi.  ``refusal`` marks a reply the model declined
    (Anthropic's stop reason ``refusal``): its partial text is never used,
    so a caller can treat it as it treats an empty reply.  ``cut_off_at`` is
    the output limit (tokens) an empty reply reached, as ``APIResult``'s.
    """

    def __init__(
        self,
        message: str,
        *,
        failure: object | None = None,
        usage: dict[str, int] | None = None,
        kind: str | None = None,
        usage_by_model: tuple[tuple[str, dict[str, int]], ...] = (),
        refusal: bool = False,
        cut_off_at: int | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = failure
        self.usage = dict(usage or {})
        self.kind = kind
        self.usage_by_model = tuple(usage_by_model)
        self.refusal = refusal
        self.cut_off_at = cut_off_at


@dataclass(frozen=True)
class APIResult:
    text: str
    usage: dict[str, int]
    model: str
    provider: str
    # The provider response, never the requested/routing model.
    observed_model: str | None = None
    # A call more than one model billed (an Anthropic refusal fallback):
    # each model's share of ``usage``, in the order they answered, so each
    # is priced at its own rates.  Empty when one model billed the call.
    usage_by_model: tuple[tuple[str, dict[str, int]], ...] = ()
    # The output limit (tokens) the reply reached before it finished, when
    # the provider says it stopped there (OpenAI ``incomplete_details.reason``
    # ``max_output_tokens``, Anthropic's stop reason ``max_tokens``, a Chat
    # Completions ``finish_reason`` ``length``, Gemini's ``finishReason``
    # ``MAX_TOKENS``); None otherwise.
    cut_off_at: int | None = None


def _openai_effort(source: Mapping[str, str], model: str, effort: str | None) -> str | None:
    """The reasoning effort an OpenAI call of ``model`` runs at: the call's
    own, else the run profile's (run_tuning.openai_payload_options)."""
    from agent_monitor.run_tuning import openai_payload_options

    return effort or (openai_payload_options(source, model).get("reasoning") or {}).get("effort")


def openai_high_effort(source: Mapping[str, str], model: str) -> bool:
    """Whether a call of ``model`` is an OpenAI call at extra-high or maximum
    effort, from the run profile: it then gets 64,000 output tokens, and its
    callers allow it ``HIGH_EFFORT_TIMEOUT``.  False when ``model`` resolves
    to another provider or to none."""
    from agent_monitor.proof_graph import _resolve_llm_provider

    try:
        provider, _base, _key, chosen = _resolve_llm_provider(dict(source), model)
    except (TypeError, ValueError):
        return False
    return provider == "openai" and _openai_effort(source, chosen, None) in _HIGH_EFFORTS


def _secret_forms(secret: str) -> list[str]:
    """The ways ``secret`` can appear in an error: as it is, repr-escaped
    (an HTTP library quoting an illegal header value), and, for a key with
    a line break or another control character, each part of 8 or more
    characters.  Longest first."""
    forms = {secret, repr(secret)[1:-1], repr(secret.encode("utf-8", "replace"))[2:-1]}
    forms.update(part for part in re.split(r"[\s\x00-\x1f\x7f]+", secret) if len(part) >= 8)
    return sorted((form for form in forms if form), key=len, reverse=True)


def _safe_error(detail: object, secret: str) -> str:
    text = str(detail or "unknown error")
    if secret:
        for form in _secret_forms(secret):
            text = text.replace(form, "[redacted]")
    return text[-1600:]


_FAILURE_STATUSES = {401: "auth", 403: "auth", 402: "usage_limit", 404: "model"}
# Some providers answer a bad key or an empty balance with HTTP 400 or 429
# (Gemini, xAI and Anthropic; OpenAI's and Kimi's quota errors): only the
# reply tells them from a passing failure.
_FAILURE_MARKERS = (
    ("auth", ("api_key_invalid", "incorrect api key")),
    ("usage_limit", (
        "insufficient_quota", "exceeded_current_quota", "credit balance is too low", "no credits remaining",
    )),
    ("model", ("model_not_found",)),
)


# A 403 that refuses this request's input, not the key: OpenRouter's
# moderation ("Your chosen model requires moderation and your input was
# flagged", with metadata.flagged_input).  Later calls may well pass.
_REQUEST_REFUSAL_MARKERS = ("requires moderation", "flagged_input")


def _failure_kind(code: int, body: object) -> str | None:
    """Classify an own key's refusal (see ``APIBackendError.kind``)."""
    text = str(body).lower()
    if code == 403 and any(marker in text for marker in _REQUEST_REFUSAL_MARKERS):
        return None
    if code in _FAILURE_STATUSES:
        return _FAILURE_STATUSES[code]
    return next((kind for kind, markers in _FAILURE_MARKERS if any(marker in text for marker in markers)), None)


def _kimi_usage(body: Mapping[str, object]) -> dict[str, int]:
    from agent_monitor import kimi_k3

    try:
        return kimi_k3.response_usage(body)
    except (TypeError, ValueError):
        return {}


def _usage_detail(usage: Mapping[str, object], details: str, name: str) -> int:
    """One count from an OpenAI-style usage ``*_details`` object (0 if absent)."""
    value = usage.get(details)
    return int(value.get(name) or 0) if isinstance(value, Mapping) else 0


def _total_usage(parts: list[dict[str, int]]) -> dict[str, int]:
    """Token counts summed over every billed reply of one call."""
    total: dict[str, int] = {}
    for usage in parts:
        for name, value in usage.items():
            total[name] = total.get(name, 0) + int(value)
    return total


def _stopped_at_output_limit(provider: str, body: Mapping[str, Any]) -> bool:
    """Whether the provider says a reply stopped at its output limit before
    it finished: OpenAI's Responses API with ``incomplete_details.reason``
    ``max_output_tokens`` (status ``incomplete``), a Chat Completions reply
    (Kimi, OpenRouter, DeepSeek, Groq, Together, xAI) whose first choice has
    ``finish_reason`` ``length``, a Gemini ``generateContent`` reply whose
    first candidate has ``finishReason`` ``MAX_TOKENS``.  Content-free; False
    for any other shape."""
    if provider == "openai":
        incomplete = body.get("incomplete_details")
        return isinstance(incomplete, Mapping) and incomplete.get("reason") == "max_output_tokens"
    if provider == "kimi" or provider in _OPENAI_COMPATIBLE_PROVIDERS:
        choices = body.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        return isinstance(choice, Mapping) and choice.get("finish_reason") == "length"
    if provider == "google":
        candidates = body.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else None
        return isinstance(candidate, Mapping) and candidate.get("finishReason") == "MAX_TOKENS"
    return False


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


def _part_value(part: Any, name: str) -> Any:
    """A field of an Anthropic usage part: a typed SDK object, or a plain
    dict (a GA ``Message`` keeps beta-only fields such as ``iterations`` as
    raw dicts, so a gateway's or a non-beta reply carries them that way)."""
    return part.get(name) if isinstance(part, Mapping) else getattr(part, name, None)


def _anthropic_part_usage(parts: list[Any]) -> dict[str, int]:
    """Token counts of Anthropic usage ``parts`` (the top level, or attempts
    listed in ``iterations``), cache reads and writes inside the input."""

    def total(name: str) -> int:
        return sum(int(_part_value(part, name) or 0) for part in parts)

    read = total("cache_read_input_tokens")
    written = total("cache_creation_input_tokens")
    return {
        "input_tokens": total("input_tokens") + read + written,
        "output_tokens": total("output_tokens"),
        "cached_input_tokens": read,
        "cache_write_tokens": written,
    }


def _anthropic_thinking(usage: Any) -> int:
    details = _part_value(usage, "output_tokens_details")
    return int((_part_value(details, "thinking_tokens") if details is not None else 0) or 0)


def _anthropic_usage(usage: Any) -> dict[str, int]:
    """Anthropic usage in the Codex semantics of the CGM event stream.

    Cache reads and writes count inside ``input_tokens`` (Anthropic reports
    them apart) and thinking inside ``output_tokens``.  After a refusal
    fallback, ``iterations`` lists every attempt Anthropic reports (a declined
    attempt is billed only if it declined mid-reply or in a billed category,
    so this can exceed the bill); the top level covers only the last one, and
    so does the thinking count.
    """
    parts = list(getattr(usage, "iterations", None) or ()) or [usage]
    return {**_anthropic_part_usage(parts), "reasoning_output_tokens": _anthropic_thinking(usage)}


def _anthropic_usage_by_model(usage: Any, requested: str) -> tuple[tuple[str, dict[str, int]], ...]:
    """Each model's share of a refusal-fallback call's usage, in the order the
    models answered; empty when one model billed the whole call.

    Each attempt in ``iterations`` names its model (one that names none is
    the requested model's).  The thinking count covers only the attempt that
    answered, so it is the last model's.
    """
    groups: list[tuple[str, list[Any]]] = []
    for part in getattr(usage, "iterations", None) or ():
        named = _part_value(part, "model")
        model = named.strip() if isinstance(named, str) and named.strip() else requested
        if groups and groups[-1][0] == model:
            groups[-1][1].append(part)
        else:
            groups.append((model, [part]))
    if len(groups) < 2:
        return ()
    last = len(groups) - 1
    return tuple(
        (
            model,
            {
                **_anthropic_part_usage(parts),
                "reasoning_output_tokens": _anthropic_thinking(usage) if index == last else 0,
            },
        )
        for index, (model, parts) in enumerate(groups)
    )


def _wake(response: Any) -> None:
    """Watchdog: end a read blocked on ``response``'s connection.

    The socket is shut down, never closed: closing a descriptor another thread
    is reading could hand its number to a new connection.
    """
    try:
        network = response.extensions["network_stream"]
        network.get_extra_info("socket").shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001 - the call is over, or has no socket
        pass


def _watchdog(response: Any, deadline: float) -> threading.Timer:
    """Start a timer that ends ``response``'s reads at ``deadline``.  A read
    timeout bounds only each wait for data, so a server that keeps sending
    (pings while the model thinks, keep-alive bytes) needs this."""
    timer = threading.Timer(max(0.0, deadline - time.monotonic()), _wake, (response,))
    timer.daemon = True
    timer.start()
    return timer


def _attempt_timeout(deadline: float) -> Any:
    """One attempt's SDK timeout: what is left of the call, and at most
    ``_ANTHROPIC_CONNECT_SECONDS`` of it to connect."""
    import anthropic

    left = max(0.001, deadline - time.monotonic())
    return anthropic.Timeout(left, connect=min(_ANTHROPIC_CONNECT_SECONDS, left))


def _snapshot(stream: Any) -> Any | None:
    """The message ``stream`` has built so far; ``None`` before its first event."""
    try:
        return stream.current_message_snapshot
    except AssertionError:
        return None


def _anthropic_streamed(client: Any, request: Mapping[str, Any], deadline: float) -> Any | None:
    """One streamed attempt.  Returns the message once its stream has ended
    (whole, or cut short without a stop reason, which ``_anthropic_chat``
    reports), or ``None`` when the deadline came first."""
    # Only the refusal-fallback beta needs the beta endpoint.
    messages = client.beta.messages if "betas" in request else client.messages
    with messages.stream(**request, timeout=_attempt_timeout(deadline)) as stream:
        # Thinking can stream nothing but pings (which the SDK does not yield)
        # for many minutes, so a timer, not only this loop, ends the call at
        # its deadline.
        watchdog = _watchdog(stream.response, deadline)
        ended = False
        try:
            for _event in stream:
                if time.monotonic() >= deadline:
                    break
            else:
                ended = True
        except Exception:
            message = _snapshot(stream)
            finished = message is not None and message.stop_reason is not None
            if not finished and time.monotonic() < deadline:
                raise
        finally:
            watchdog.cancel()
        message = _snapshot(stream)
    if ended:
        if message is None:
            # A body without events: not a Messages stream.
            raise APIBackendError(f"Anthropic's reply for {request['model']} carried no message")
        return message
    # A reply whose final event arrived is whole, even if that was at the
    # deadline or the read after it failed.
    return message if message is not None and message.stop_reason is not None else None


def _anthropic_retry_wait(exc: Any, retries: int) -> float | None:
    """Seconds to wait before sending a refused request again; ``None`` when
    it is not sent again (see ``_ANTHROPIC_RETRY_STATUSES``)."""
    if retries >= _ANTHROPIC_RETRIES or exc.status_code not in _ANTHROPIC_RETRY_STATUSES:
        return None
    headers = exc.response.headers
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        try:
            wait = float(headers.get(name) or "") * scale
        except ValueError:
            continue
        if math.isfinite(wait) and wait >= 0:
            return wait
    return _ANTHROPIC_RETRY_WAITS[retries]


def _anthropic_message(options: Mapping[str, Any], request: Mapping[str, Any]) -> Any:
    """Send one Messages request through the SDK; return the final message.

    The one place ``api_chat`` reaches the Anthropic API (tests patch it).
    ``options``: the client's ``api_key`` and ``base_url``; ``timeout``, the
    seconds the whole call may take (every attempt and wait); and
    ``stream``: stream the reply (the Claude API), or make plain requests (a
    custom base URL, which need not stream).

    Each attempt must connect within ``_ANTHROPIC_CONNECT_SECONDS`` and
    receive its response headers within what is left of the call (each read
    is bounded by it); from then on a watchdog ends the reply at the
    deadline, however it is still arriving.  A request refused before
    processing (429, 529) is sent again after the wait its refusal names, at
    most twice, and only while time remains (``_anthropic_retry_wait``); the
    SDK itself never retries.
    """
    import anthropic

    seconds = float(options["timeout"])
    deadline = time.monotonic() + seconds
    attempt = _anthropic_streamed if options["stream"] else _anthropic_plain
    # The key travels in X-Api-Key, so a redirect is never followed.
    http_client = anthropic.DefaultHttpxClient(follow_redirects=False)
    with anthropic.Anthropic(
        api_key=options["api_key"],
        base_url=options["base_url"],
        max_retries=0,
        timeout=seconds,
        http_client=http_client,
    ) as client:
        retries = 0
        while True:
            try:
                message = attempt(client, request, deadline)
            except anthropic.APIStatusError as exc:
                wait = _anthropic_retry_wait(exc, retries)
                if wait is None or time.monotonic() + wait + _ANTHROPIC_MIN_ATTEMPT_SECONDS > deadline:
                    raise
                retries += 1
                time.sleep(wait)
                continue
            if message is not None:
                return message
            break
    raise APIBackendError(
        f"Anthropic model call timed out for {request['model']} after {seconds:g} s"
    )


def _anthropic_plain(client: Any, request: Mapping[str, Any], deadline: float) -> Any | None:
    """One plain (unstreamed) attempt, as a custom base URL gets: a gateway
    need not stream.  Returns the message, or ``None`` when the deadline came
    first (a watchdog ends a body that is still arriving then)."""
    with client.messages.with_streaming_response.create(
        **request, timeout=_attempt_timeout(deadline)
    ) as response:
        watchdog = _watchdog(response.http_response, deadline)
        try:
            return response.parse()
        except Exception:
            if time.monotonic() < deadline:
                raise
            return None
        finally:
            watchdog.cancel()


def _anthropic_chat(
    system: str,
    user: str,
    *,
    model: str,
    base: str,
    key: str,
    max_tokens: int,
    effort: str | None,
    timeout: int,
) -> APIResult:
    """One Anthropic Messages call through the official SDK.

    The Claude API streams the reply.  A custom base URL (a gateway) gets
    plain requests, as before the SDK, since it need not stream, and never
    the refusal-fallback beta.
    """
    try:
        import anthropic

        major = int(str(anthropic.__version__).split(".")[0])
    except (ImportError, AttributeError, ValueError):
        major = 0
    if major < 1:
        # An engine's own environment (setup.sh installs the pinned SDK for
        # DeepAgents; a venv made before this SDK holds an older one).
        raise APIBackendError(
            "The Anthropic SDK in this runner's Python environment is missing or older "
            "than 1.0 (pyproject.toml pins anthropic): re-run ./setup.sh, or install the "
            "pinned version there, to call Claude models"
        )

    from agent_monitor.proof_graph import _api_error

    # The SDK adds /v1 to the base itself.
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    claude_api = root.lower() == _ANTHROPIC_DEFAULT_BASE
    # No thinking parameter (current models think adaptively by default, and
    # some reject an explicit setting) and no sampling parameters.
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if effort is not None:
        request["output_config"] = {"effort": effort}
    if model in _ANTHROPIC_FALLBACK_MODELS and claude_api:
        request["betas"] = [_ANTHROPIC_FALLBACK_BETA]
        request["fallbacks"] = "default"
    try:
        message = _anthropic_message(
            {"api_key": key, "base_url": root, "timeout": timeout, "stream": claude_api},
            request,
        )
    except APIBackendError:
        raise
    # SDK errors keep the request (with its key header): none is chained.
    except anthropic.APITimeoutError as exc:
        if type(exc.__cause__).__name__ == "ConnectTimeout":
            # Connecting gets at most _ANTHROPIC_CONNECT_SECONDS of the call.
            raise APIBackendError(
                f"Anthropic model call failed for {model}: could not connect (connection timed out)"
            ) from None
        raise APIBackendError(
            f"Anthropic model call timed out for {model} after {timeout} s"
        ) from None
    except anthropic.APIStatusError as exc:
        body = exc.body if exc.body is not None else exc.message
        # An error event inside a stream arrives with the stream's HTTP 200.
        where = f"HTTP {exc.status_code}" if exc.status_code != 200 else "stream error"
        detail = (
            "redirects are not followed"
            if 300 <= exc.status_code < 400
            else _safe_error(_api_error(body), key)
        )
        raise APIBackendError(
            f"Anthropic model call failed for {model} ({where}): {detail}",
            kind=_failure_kind(exc.status_code, body),
        ) from None
    except Exception as exc:  # APIConnectionError, or anything unexpected
        detail = _safe_error(exc.__cause__ or exc, key) or type(exc).__name__
        raise APIBackendError(
            f"Anthropic model transport failed for {model}: {detail}"
        ) from None

    try:
        usage = _anthropic_usage(message.usage)
        by_model = _anthropic_usage_by_model(message.usage, model)
        stop_reason = message.stop_reason
        if stop_reason == "refusal":
            # Even after a refusal fallback, so a partial answer is never
            # parsed.  stop_details only says why; it can be missing.
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "refusal"
            raise APIBackendError(
                f"Anthropic declined the request for {model} ({category})",
                usage=usage,
                usage_by_model=by_model,
                refusal=True,
            )
        if stop_reason is None:
            if getattr(message, "type", None) != "message":
                # A gateway's 200 in another shape (an OpenAI-style reply, an
                # error object): the SDK does not validate it.
                raise APIBackendError(
                    f"The reply for {model} is not an Anthropic Messages response"
                )
            raise APIBackendError(
                f"Anthropic's reply for {model} ended before it was complete",
                usage=usage,
                usage_by_model=by_model,
            )
        # Thinking and fallback blocks are skipped.  After a mid-reply
        # fallback the next model continues the text, so blocks are joined
        # as they are.
        text = "".join(
            str(block.text or "") for block in message.content if block.type == "text"
        )
        observed = message.model.strip() if isinstance(message.model, str) else ""
    except (AttributeError, TypeError, ValueError) as exc:
        raise APIBackendError(
            f"Anthropic returned an unexpected response shape for {model}"
        ) from exc
    text = _LONE_SURROGATES.sub("\ufffd", text)  # e.g. half of a split emoji
    cut_off_at = max_tokens if stop_reason == "max_tokens" else None
    if not text.strip():
        raise APIBackendError(
            f"Anthropic returned an empty response for {model} "
            f"(stop_reason={stop_reason}, output_tokens={usage['output_tokens']})",
            usage=usage,
            usage_by_model=by_model,
            cut_off_at=cut_off_at,
        )
    # observed_model is the model that answered, which a fallback changes.
    return APIResult(
        text=text.strip(),
        usage=usage,
        model=model,
        provider="anthropic",
        observed_model=observed or None,
        usage_by_model=by_model,
        cut_off_at=cut_off_at,
    )


def api_chat(
    system: str,
    user: str,
    *,
    model: str,
    timeout: int | None = None,
    effort: str | None = None,
    max_tokens: int | None = None,
    env: Mapping[str, str] | None = None,
    retry_allowance: object | None = None,
) -> APIResult:
    """Call the provider implied by ``model`` using the user's resolved env.

    Every per-call knob can be passed explicitly, so concurrent callers never
    need to mutate the process environment:

    * ``env`` -- the settings mapping to resolve the provider, credentials and
      limits from (default: ``os.environ``, read at call time).  It is only
      read, never modified.
    * ``effort`` -- reasoning effort for this call.  Anthropic and OpenAI:
      ``low``/``medium``/``high``/``xhigh``/``max``, sent as Anthropic's
      ``output_config.effort`` or OpenAI's ``reasoning.effort`` (over the run
      profile's).  Every other provider: ``low``/``high``/``max``, Kimi's
      values, which Kimi sends over ``KIMI_REASONING_EFFORT`` and the others
      ignore (a Kimi model can be served by OpenRouter, Groq or Together).
      Without it every provider keeps its default or validated run profile.
    * ``max_tokens`` -- requested output limit for this call instead of
      ``AGENT_MONITOR_API_MAX_TOKENS`` (same 1024..128000 clamp).  Without
      either it is 16,384, or 64,000 for an OpenAI call at extra-high or
      maximum effort, the call's own or the run profile's (reasoning counts
      against it).  The job's
      ``AGENT_MONITOR_MAX_OUTPUT_TOKENS`` ceiling still applies and can only
      lower it.
    * ``timeout`` -- the call's total seconds (default 240,
      ``HIGH_EFFORT_TIMEOUT`` for an OpenAI call at extra-high or maximum
      effort, or ``kimi_k3.SPONSORED_TIMEOUT_SECONDS`` for a server-sponsored
      Kimi call, as for every other sponsored caller).  A server-sponsored Kimi call
      sends it as ``X-Ansatze-Timeout`` so the broker and gateway answer
      first, and retries only replies ``kimi_k3.sponsored_retryable``
      accepts, within this deadline.  An Anthropic call's attempts, waits
      and replies are held to this deadline (``_anthropic_message`` says
      exactly how).
    * ``retry_allowance`` -- a ``kimi_k3.RetryAllowance`` shared by every
      call of one run, capping that run's sponsored transport retries.
    """
    from agent_monitor.proof_graph import (
        _api_error,
        _openai_response_text,
        _resolve_llm_provider,
        key_defect,
        key_defect_message,
        provider_label,
    )
    from agent_monitor.settings import _http_json

    source: Mapping[str, str] = env if env is not None else os.environ
    try:
        provider, base, key, chosen = _resolve_llm_provider(dict(source), model)
    except (TypeError, ValueError) as exc:
        raise APIBackendError(str(exc)) from exc
    if provider == "kimi":
        from agent_monitor import kimi_k3

        if not kimi_k3.names_kimi_k3(chosen):
            # This Kimi call asks for Kimi K3 whatever it names (chat_payload):
            # one named for another Kimi model (kimi-k2.5) would run as Kimi
            # K3 under that name.  Never sent; every call would repeat it.
            # Only the engines that run Kimi K3 alone call Kimi here (Plain,
            # the CGM engines, Meta-Harness, Analyze, Readability, IMProof's
            # audit); a coding-agent harness's audit names its model with an
            # OpenAI-compatible prefix (``openai:kimi-k2.5``) and is sent as
            # named.
            raise APIBackendError(f"{kimi_k3.OTHER_KIMI_CALL_ERROR}, not {chosen}",
                                  kind=None if kimi_k3.is_sponsored(source) else "model")
    if key_defect(key):
        # An HTTP library would refuse the header (or URL) and quote it in its
        # error; every call with this key fails the same way.
        raise APIBackendError(key_defect_message(provider), kind="auth")

    if effort is not None:
        effort = str(effort).strip().lower()
        if provider in {"anthropic", "openai"}:
            if effort not in _API_EFFORTS:
                raise APIBackendError(
                    "reasoning effort must be one of low, medium, high, xhigh or max"
                )
        else:
            from agent_monitor import kimi_k3

            if effort not in kimi_k3.ALLOWED_REASONING_EFFORTS:
                raise APIBackendError("reasoning effort must be one of low, high or max")

    if max_tokens is not None and (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
    ):
        raise APIBackendError("max_tokens must be an integer")
    high_effort = provider == "openai" and _openai_effort(source, chosen, effort) in _HIGH_EFFORTS
    default_max = _HIGH_EFFORT_MAX_TOKENS if high_effort else _DEFAULT_MAX_TOKENS
    from agent_monitor import kimi_k3

    sponsored = provider == "kimi" and kimi_k3.is_sponsored(source)
    if timeout is None:
        # A sponsored call waits as long as the broker may take: the broker
        # and the gateway end a slow one first, with a typed timeout.
        timeout = (
            kimi_k3.SPONSORED_TIMEOUT_SECONDS if sponsored
            else HIGH_EFFORT_TIMEOUT if high_effort else _DEFAULT_TIMEOUT
        )
    try:
        requested = (
            max_tokens
            if max_tokens is not None
            else int(source.get("AGENT_MONITOR_API_MAX_TOKENS") or default_max)
        )
        max_output = max(1024, min(requested, 128000))
    except ValueError as exc:
        raise APIBackendError("AGENT_MONITOR_API_MAX_TOKENS must be an integer") from exc

    # A bounded run (currently anonymous guest sessions) carries a hard output
    # ceiling. It only ever lowers the limit above, and it is not negotiable by
    # the caller's own AGENT_MONITOR_API_MAX_TOKENS.
    ceiling_raw = str(source.get("AGENT_MONITOR_MAX_OUTPUT_TOKENS") or "").strip()
    if ceiling_raw:
        try:
            ceiling = int(ceiling_raw)
        except ValueError as exc:
            raise APIBackendError(
                "AGENT_MONITOR_MAX_OUTPUT_TOKENS must be an integer"
            ) from exc
        if ceiling > 0:
            max_output = min(max_output, ceiling)

    if provider == "anthropic":
        return _anthropic_chat(
            system,
            user,
            model=chosen,
            base=base,
            key=key,
            max_tokens=max_output,
            effort=effort,
            timeout=timeout,
        )

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

        payload.update(openai_payload_options(source, chosen))
        if effort is not None:
            # The call's own effort wins over the run profile's.
            payload["reasoning"] = {**(payload.get("reasoning") or {}), "effort": effort}
    elif provider == "kimi":
        from agent_monitor import kimi_k3

        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = kimi_k3.chat_payload(
            system=system,
            user=user,
            effort=effort,
            env=source,
            max_tokens=max_output,
        )
    elif provider == "google":
        api_base = base if base.endswith("/v1beta") else f"{base}/v1beta"
        url = f"{api_base}/models/{quote(chosen)}:generateContent?key={key}"
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

    # Every 2xx Kimi reply is billed, including an empty completion that a
    # retry replaced, so the call's usage is the total of all of them.
    billed: list[dict[str, int]] = []

    def request(seconds: float | None = None) -> tuple[int, object]:
        limit = timeout if seconds is None else seconds
        call_headers = headers
        if sponsored:
            call_headers = {
                **headers,
                kimi_k3.TIMEOUT_HEADER: kimi_k3.timeout_header_value(limit),
            }
        code, body = _http_json(
            url,
            method="POST",
            headers=call_headers,
            payload=payload,
            timeout=limit,
        )
        if provider == "kimi" and 200 <= code < 300 and isinstance(body, dict):
            billed.append(_kimi_usage(body))
        return code, body

    failure = None
    try:
        if sponsored:
            # Server-sponsored traffic is admitted and billed per logical model
            # call, whether anonymous or registered.  Only a failure that
            # certainly never reached processing is retried (see
            # kimi_k3.sponsored_retryable); an ambiguous timeout, 5xx or empty
            # response is never replayed, since the upstream may already have
            # processed it.
            code, body, failure = kimi_k3.sponsored_request(
                request,
                timeout=timeout,
                allowance=retry_allowance,  # type: ignore[arg-type]
            )
        elif provider == "kimi":
            # User-key runs retain the existing bounded reliability retries.
            code, body = kimi_k3.request_with_retry(request)
        else:
            code, body = request()
    except Exception as exc:
        # Gemini authenticates in its query string.  Unexpected transport
        # exceptions can include the full URL, so suppress the original cause
        # after projecting a key-redacted diagnostic.
        raise APIBackendError(
            f"{provider_label(provider)} model transport failed for {chosen}: "
            f"{_safe_error(exc, key)}",
            usage=_total_usage(billed),
        ) from None
    if failure is not None:
        raise APIBackendError(
            kimi_k3.failure_message(failure)
            or (
                f"{provider_label(provider)} model call failed for {chosen} (HTTP {code}): "
                f"{_safe_error(_api_error(body), key)}"
            ),
            failure=failure,
            usage=_total_usage(billed),
        )
    if code != 200 or not isinstance(body, dict):
        detail = _api_error(body)
        raise APIBackendError(
            f"{provider_label(provider)} model call failed for {chosen} (HTTP {code}): "
            f"{_safe_error(detail, key)}",
            failure=kimi_k3.call_failure(code, body) if sponsored else None,
            usage=_total_usage(billed),
            kind=None if sponsored else _failure_kind(code, body),
        )

    try:
        if provider == "openai":
            text = _openai_response_text(body)
            raw_usage = body.get("usage") or {}
            # Codex semantics, as the CGM event stream expects: the cached and
            # reasoning parts are inside the input and output totals.
            usage = {
                "input_tokens": int(raw_usage.get("input_tokens") or 0),
                "output_tokens": int(raw_usage.get("output_tokens") or 0),
                "cached_input_tokens": _usage_detail(raw_usage, "input_tokens_details", "cached_tokens"),
                "reasoning_output_tokens": _usage_detail(
                    raw_usage, "output_tokens_details", "reasoning_tokens"
                ),
            }
        elif provider == "kimi":
            text = kimi_k3.response_text(body)
            usage = _total_usage(billed)
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
                "cached_input_tokens": _usage_detail(raw_usage, "prompt_tokens_details", "cached_tokens"),
                "reasoning_output_tokens": _usage_detail(
                    raw_usage, "completion_tokens_details", "reasoning_tokens"
                ),
            }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise APIBackendError(
            f"{provider_label(provider)} returned an unexpected response shape for {chosen}"
        ) from exc
    text = _LONE_SURROGATES.sub("\ufffd", text)  # e.g. half of a split emoji
    cut_off_at = max_output if _stopped_at_output_limit(provider, body) else None
    if not text.strip():
        detail = ""
        if provider == "kimi":
            # finish_reason and token counts only, for diagnosing budget
            # exhaustion; never response or prompt text.
            summarize = getattr(kimi_k3, "empty_completion_summary", None)
            detail = f" ({summarize(body)})" if summarize is not None else ""
        elif provider == "openai":
            # Why the reply ended, and its counts; never response text.
            status = str(body.get("status") or "")
            incomplete = body.get("incomplete_details")
            reason = str(incomplete.get("reason") or "") if isinstance(incomplete, Mapping) else ""
            if reason == "max_output_tokens":
                detail = (
                    f" (it stopped at its {max_output:,}-token output limit; "
                    f"reasoning used {int(usage.get('reasoning_output_tokens') or 0):,} tokens)"
                )
            elif status and status != "completed":
                detail = f" (status {status}" + (f": {reason}" if reason else "") + ")"
        raise APIBackendError(
            f"{provider_label(provider)} returned an empty response for {chosen}{detail}",
            usage=usage,
            cut_off_at=cut_off_at,
        )
    return APIResult(
        text=text.strip(), usage=usage, model=chosen, provider=provider,
        observed_model=(body["model"].strip() or None) if isinstance(body.get("model"), str) else None,
        cut_off_at=cut_off_at,
    )
