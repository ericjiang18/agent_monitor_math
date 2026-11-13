"""Kimi K3 OpenAI-compatible Chat Completions adapter."""
from __future__ import annotations

import http.client
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
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

# Server-sponsored calls go through the loopback broker, which waits for the
# credential gateway at most the gateway's total deadline
# (PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS, default 600 s) plus 15 s, so a client
# waiting this long always receives the broker's own answer.
SPONSORED_TIMEOUT_SECONDS = 630
# The client's own remaining total timeout for one call, in seconds.  The
# broker and the gateway may only shorten their deadlines to it.
TIMEOUT_HEADER = "X-Ansatze-Timeout"
SPONSORED_ATTEMPTS = 3
# Transport retries one CGM or Analyze run may spend across all of its calls:
# twice a sponsored run's 12 recorded calls.  Every one resends a call refused
# before processing (no tokens used); a Kimi provider that refuses the first
# request after each completed call needs about one per call.
SPONSORED_RUN_RETRIES = 24
_SPONSORED_RETRY_DELAYS_S = (5.0, 15.0)
_RETRY_AFTER_BOUNDS_S = (2, 30)
# A retry is skipped when it would leave less of the call's deadline.
_SPONSORED_MIN_REMAINING_S = 30.0
# An upstream 429/503 this fast was refused before any processing.
_FAST_REJECTION_MS = 2000
_NOT_FORWARDED_RETRYABLE = frozenset(
    {"gateway_busy", "gateway_unavailable", "upstream_unavailable"}
)
# Failures every later sponsored call of the same run would repeat.
_PERMANENT_KINDS = frozenset(
    {"sponsor_quota", "gateway_quota", "unauthorized", "forbidden", "bad_request"}
)
# A site quota that frees later than this is reported with its wait.
_LONG_QUOTA_WAIT_S = 300
_SPONSORED_ENV_FLAGS = (
    "AGENT_MONITOR_PUBLIC_KIMI",
    "AGENT_MONITOR_SPONSORED_KIMI",
    "AGENT_MONITOR_GUEST_DERIVED",
)

# LiteLLM may add these OpenAI Responses fields. Kimi K3's endpoint is Chat
# Completions compatible, so the native adapter never includes them.
DROPPED_OPENAI_PARAMS = ("output_config", "context_management")


def names_kimi_k3(model: object) -> bool:
    """Whether ``model`` names Kimi K3: ``kimi-k3`` in any case or spacing,
    alone or after the ``kimi:`` provider prefix of a custom model
    (``kimi:kimi-k3``, which the API runners resolve to Kimi K3 too)."""
    name = str(model or "").strip().lower()
    provider, colon, rest = name.partition(":")
    if colon and provider.strip() == "kimi":
        name = rest.strip()
    return name == MODEL_ID


def is_other_kimi_model(model: object) -> bool:
    """Whether ``model`` names a Kimi model other than Kimi K3 (``kimi-k2.5``
    or ``kimi:kimi-k2.5`` typed as a custom model).  :func:`chat_payload`
    (every Kimi call of ``api_backend.api_chat``) and the sponsored service
    ask for Kimi K3 whatever Kimi model is named, so a run whose Kimi calls
    go there would be recorded, shown and billed under a name it never ran
    on: the engines of ``engines_registry.KIMI_K3_ONLY_ENGINE_IDS`` refuse
    one.  A coding-agent harness sends the name it is given to an account's
    own Kimi endpoint."""
    name = str(model or "").strip().lower()
    return name.startswith("kimi") and not names_kimi_k3(name)


# ``api_backend.api_chat``'s refusal of a Kimi call named for another Kimi
# model (``chat_payload`` would ask for Kimi K3 under that name).
OTHER_KIMI_CALL_ERROR = f"Kimi calls here ask for Kimi K3 whatever Kimi model is named; choose {MODEL_ID}"


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
    before_retry: Callable[[int, object], None] | None = None,
) -> tuple[int, object]:
    """Run one idempotent Kimi Chat request with bounded deterministic backoff.

    The callback owns all request details, so this helper never receives,
    stores, or logs an API key.  Attempts are capped at three even if a caller
    supplies a larger value.  ``before_retry(code, body)`` runs before each
    retry, so a caller can adapt its own payload (for example with
    ``lower_effort_after_empty``).
    """
    total = max(1, min(int(attempts), DEFAULT_RETRY_ATTEMPTS))
    pause = sleep or time.sleep
    code: int = 0
    body: object = ""
    for index in range(total):
        code, body = request()
        if not retryable_response(code, body) or index + 1 >= total:
            break
        if before_retry is not None:
            before_retry(code, body)
        pause(_RETRY_DELAYS_S[min(index, len(_RETRY_DELAYS_S) - 1)])
    return code, body


# ---------------------------------------------------------------------------
# Server-sponsored calls (loopback broker -> credential gateway -> provider)
# ---------------------------------------------------------------------------


def is_sponsored(env: Mapping[str, str]) -> bool:
    """Whether Kimi calls made with ``env`` are server-sponsored."""
    return any(str(env.get(name) or "").strip() == "1" for name in _SPONSORED_ENV_FLAGS)


def timeout_header_value(seconds: float) -> str:
    """``X-Ansatze-Timeout`` for ``seconds`` left: whole seconds, at least 1.

    Rounding down only ever shortens the broker's and gateway's deadlines,
    so they still answer before the client gives up.
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 1.0
    if not math.isfinite(value):
        value = 1.0
    return str(max(1, int(value)))


def _plain_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def typed_error(body: object) -> Mapping[str, Any] | None:
    """The typed ``error`` object of a broker or gateway reply, else ``None``.

    A reply from an older broker (no ``type``) or any other body returns
    ``None``, so it is never treated as safe to retry.
    """
    if not isinstance(body, Mapping):
        return None
    error = body.get("error")
    if not isinstance(error, Mapping):
        return None
    kind = error.get("type")
    return error if isinstance(kind, str) and kind.strip() else None


def sponsored_retryable(code: int, body: object) -> bool:
    """Whether a failed sponsored call certainly never reached processing.

    True only when nothing was forwarded or the provider refused the call at
    once: ``code`` 0 (the loopback broker could not be reached or sent to), a
    typed error marked ``forwarded: false`` because the gateway was busy or
    unavailable, the upstream could not be reached, or the site's hourly
    quota frees within 30 s, or an upstream 429/503 answered within 2 s.
    Everything else -- any 2xx including an empty completion, a timeout, a
    sponsor quota, an authorization failure, any untyped body -- may have
    been processed (and billed) and is never retried.
    """
    if code == 0:
        return True
    if 200 <= code < 300:
        return False
    error = typed_error(body)
    if error is None:
        return False
    kind = str(error.get("type")).strip()
    if error.get("forwarded") is False:
        if kind in _NOT_FORWARDED_RETRYABLE:
            return True
        if kind == "gateway_quota":
            retry_after = _plain_int(error.get("retry_after"))
            return retry_after is not None and retry_after <= 30
    if kind == "upstream_rejected":
        elapsed_ms = _plain_int(error.get("upstream_elapsed_ms"))
        return (
            _plain_int(error.get("upstream_status")) in {429, 503}
            and elapsed_ms is not None
            and 0 <= elapsed_ms <= _FAST_REJECTION_MS
        )
    return False


def sponsored_retry_delay(attempt: int, body: object) -> float:
    """Seconds to wait after failed attempt ``attempt`` (1-based).

    Deterministic, without jitter: 5 s before the second attempt and 15 s
    before the third, or the reply's ``retry_after`` (clamped to 2..30 s)
    when that is longer.  A shorter hint is not followed: Kimi's provider has
    hinted 2 s or less while it kept refusing for several seconds after each
    completed call, so following the hint spent all three attempts of a call
    within about 5 s.
    """
    index = max(0, min(int(attempt) - 1, len(_SPONSORED_RETRY_DELAYS_S) - 1))
    delay = _SPONSORED_RETRY_DELAYS_S[index]
    error = typed_error(body)
    retry_after = _plain_int(error.get("retry_after")) if error is not None else None
    if retry_after is not None:
        low, high = _RETRY_AFTER_BOUNDS_S
        delay = max(delay, float(max(low, min(retry_after, high))))
    return delay


class RetryAllowance:
    """The sponsored transport retries one run may still spend (thread-safe)."""

    def __init__(self, limit: int = SPONSORED_RUN_RETRIES) -> None:
        self._left = max(0, int(limit))
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._left <= 0:
                return False
            self._left -= 1
            return True


@dataclass(frozen=True)
class CallFailure:
    """Content-free facts about a failed sponsored call.

    ``status`` is the last HTTP status (0 when no reply arrived); ``kind`` the
    typed error ``type``, ``client_timeout`` or ``connection_lost`` for a
    reply that never arrived, or "" for an untyped reply; ``retryable`` is
    :func:`sponsored_retryable` of the last reply (the provider never
    processed it, so no tokens were used); ``elapsed`` the seconds the last
    attempt took.
    """

    status: int
    kind: str
    retryable: bool
    upstream_status: int | None = None
    retry_after: int | None = None
    elapsed: float = 0.0
    attempts: int = 1

    @property
    def permanent(self) -> bool:
        """Whether every later sponsored call of the same run would fail alike."""
        if self.retryable:
            return False
        if self.kind in _PERMANENT_KINDS:
            return True
        status = self.upstream_status if self.kind == "upstream_rejected" else self.status
        return status is not None and 300 <= status < 500 and status not in {408, 429}


def call_failure(
    code: int, body: object, *, elapsed: float = 0.0, attempts: int = 1
) -> CallFailure:
    """The :class:`CallFailure` of a non-2xx sponsored reply."""
    error = typed_error(body) or {}
    return CallFailure(
        status=int(code),
        kind=str(error.get("type") or "").strip()[:40],
        retryable=sponsored_retryable(code, body),
        upstream_status=_plain_int(error.get("upstream_status")),
        retry_after=_plain_int(error.get("retry_after")),
        elapsed=max(0.0, float(elapsed)),
        attempts=max(1, int(attempts)),
    )


def sponsored_request(
    request: Callable[[float], tuple[int, object]],
    *,
    timeout: float,
    allowance: RetryAllowance | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[int, object, CallFailure | None]:
    """Run one sponsored call: up to three attempts, retrying only replies
    :func:`sponsored_retryable` accepts.

    ``request(seconds)`` makes one attempt with ``seconds`` of the call's
    total ``timeout`` left: its socket timeout and its ``X-Ansatze-Timeout``.
    The wait before a retry is :func:`sponsored_retry_delay`; a retry is
    skipped when it would leave less than 30 s of the deadline, or when
    ``allowance`` (the run's shared retry cap) is spent.  A transport error
    raised after the request may have been sent (``OSError`` or
    ``http.client.HTTPException``) ends the call without a retry.  Returns
    the last ``(code, body)`` and, unless it is a 2xx reply, its
    :class:`CallFailure`.  The callback owns every request detail, so this
    helper never sees a credential.
    """
    total = max(1.0, float(timeout))
    pause = sleep or time.sleep
    now = clock or time.monotonic
    deadline = now() + total
    attempts = 0
    while True:
        attempts += 1
        seconds = total if attempts == 1 else max(1.0, deadline - now())
        started = now()
        try:
            code, body = request(seconds)
        except (OSError, http.client.HTTPException) as exc:
            # The request may already have been forwarded: never replayed.
            kind = "client_timeout" if isinstance(exc, TimeoutError) else "connection_lost"
            return 0, None, CallFailure(
                status=0,
                kind=kind,
                retryable=False,
                elapsed=max(0.0, now() - started),
                attempts=attempts,
            )
        elapsed = now() - started
        if 200 <= code < 300:
            return code, body, None
        if not sponsored_retryable(code, body) or attempts >= SPONSORED_ATTEMPTS:
            break
        delay = sponsored_retry_delay(attempts, body)
        if deadline - now() - delay < _SPONSORED_MIN_REMAINING_S:
            break
        if allowance is not None and not allowance.take():
            break
        pause(delay)
    return code, body, call_failure(code, body, elapsed=elapsed, attempts=attempts)


def _duration(seconds: float) -> str:
    whole = max(1, int(seconds))
    if whole >= 120:
        return f"{whole // 60} minutes"
    if whole >= 60:
        return "a minute"
    return f"{whole} second{'s' if whole != 1 else ''}"


def failure_message(failure: CallFailure, *, billed: bool = False) -> str:
    """What happened and what to do, for a person; "" for an untyped reply.

    It says that no tokens were used only for a call refused before any
    processing (``failure.retryable``), and never when ``billed``: other
    calls the message speaks for (earlier calls of the same run, say) may
    have been processed.  The ``(HTTP NNN)`` fragment stays for diagnostics.
    Never includes a prompt, a reply or a credential.
    """
    kind = failure.kind
    status = f"HTTP {failure.status}"
    no_tokens = (
        "No tokens were used. " if failure.retryable and not billed else ""
    ) + "Please try again in a few minutes."
    if kind == "client_timeout":
        return (
            f"Kimi took longer than {_duration(failure.elapsed)} to answer, so the "
            f"call was stopped (no reply after {int(failure.elapsed)} s). "
            "Please try again later."
        )
    if kind == "connection_lost":
        return (
            "The connection to the included Kimi service was lost while it was "
            "answering. Please try again later."
        )
    if kind in {"upstream_timeout", "gateway_timeout"}:
        return (
            f"Kimi took longer than {_duration(failure.elapsed)} to answer, so the "
            f"call was stopped ({status}). Please try again later."
        )
    if kind == "gateway_quota" and (failure.retry_after or 0) > _LONG_QUOTA_WAIT_S:
        # The hourly quota frees as its oldest call ages out: up to an hour.
        minutes = math.ceil((failure.retry_after or 0) / 60)
        return (
            f"This site's hourly Kimi limit is reached ({status}). "
            f"Please try again in about {minutes} minutes."
        )
    if kind in {"gateway_busy", "gateway_quota"}:
        return f"Kimi is busy on this site right now ({status}). Please try again in a few minutes."
    if kind == "sponsor_quota":
        return f"This run used up its Kimi call budget ({status})."
    if kind == "upstream_rejected":
        if failure.upstream_status in {429, 503}:
            if failure.retryable:
                return (
                    "Kimi's provider is temporarily overloaded and refused the "
                    f"request ({status}). {no_tokens}"
                )
            return (
                f"Kimi's provider is temporarily overloaded ({status}). "
                "Please try again in a few minutes."
            )
        if (failure.upstream_status or failure.status) >= 500:
            return f"Kimi's provider failed to answer ({status}). Please try again later."
        return f"Kimi's provider rejected the request ({status})."
    if kind == "upstream_unavailable":
        if failure.retryable:
            return f"Kimi's provider could not be reached ({status}). {no_tokens}"
        return (
            "The connection to Kimi's provider failed while it was answering "
            f"({status}). Please try again later."
        )
    if kind == "gateway_unavailable":
        if failure.retryable:
            return f"The included Kimi service is unavailable right now ({status}). {no_tokens}"
        # The broker had already sent the call on: Kimi may have processed it.
        return (
            "The connection to the included Kimi service failed while it was "
            f"answering ({status}). Please try again later."
        )
    if kind == "response_too_large":
        return f"Kimi's answer was too large to accept ({status}). Please try again later."
    if kind == "unauthorized":
        # A run and a Formal/DAG action alike get fresh access on a new try.
        return f"The included Kimi access has expired ({status}). Please try again."
    if kind == "forbidden":
        return f"This run may not use the included Kimi service this way ({status})."
    if kind == "bad_request":
        return f"Kimi refused the request as invalid ({status})."
    if failure.status == 0:
        return f"The included Kimi service could not be reached. {no_tokens}"
    if kind:
        return f"The included Kimi service could not answer ({status}). Please try again later."
    return ""


def refusal_reason(failure: CallFailure | None) -> str:
    """Why a call was refused before any processing, as a clause for a
    progress note: this site's broker or its own limits, or Kimi's provider."""
    kind = failure.kind if failure is not None else ""
    if failure is not None and (failure.status == 0 or kind == "gateway_unavailable"):
        return "the included Kimi service could not be reached"
    if kind in {"gateway_busy", "gateway_quota"}:
        return "Kimi is busy on this site"
    if kind == "upstream_unavailable":
        return "Kimi's provider could not be reached"
    return "Kimi's provider is busy"


_LOWER_EFFORT = {"max": "high", "high": "low"}


def is_empty_completion(code: int, body: object) -> bool:
    """A 200 reply that carries no assistant text."""
    return code == 200 and (
        not isinstance(body, Mapping) or not response_text(body).strip()
    )


def lower_effort_after_empty(payload: dict[str, Any], code: int, body: object) -> bool:
    """Spend less of the output budget on reasoning after an empty completion.

    K3 counts reasoning against ``max_tokens``; when a long prompt makes it
    reason until the (gateway-capped) budget runs out, the reply has no text
    and an identical retry fails the same way.  Each retry after an empty
    completion steps ``reasoning_effort`` down one level (max -> high -> low).
    Returns whether the payload changed.
    """
    if not is_empty_completion(code, body):
        return False
    current = str(payload.get("reasoning_effort") or DEFAULT_REASONING_EFFORT).strip().lower()
    lower = _LOWER_EFFORT.get(current)
    if lower is None:
        return False
    payload["reasoning_effort"] = lower
    return True


def empty_completion_summary(body: object) -> str:
    """Content-free facts about an empty completion (finish reason, token use)."""
    if not isinstance(body, Mapping):
        return "no JSON body"
    finish = ""
    try:
        finish = str(body["choices"][0].get("finish_reason") or "")  # type: ignore[index,union-attr]
    except (KeyError, IndexError, TypeError, AttributeError):
        finish = ""
    usage = response_usage(body)
    parts = [f"finish_reason={finish[:40] or 'missing'}"]
    parts.append(f"output_tokens={usage['output_tokens']}")
    if usage["reasoning_tokens"]:
        parts.append(f"reasoning_tokens={usage['reasoning_tokens']}")
    return ", ".join(parts)
