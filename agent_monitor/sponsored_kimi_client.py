"""Loopback client for the isolated, rate-limited sponsored Kimi service.

The original ProvingConsole process never receives the provider credential.
It can submit only the public service's fixed Plain/Kimi request schema and
copy the resulting proof back into the signed-in user's workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlparse


DEFAULT_URL = "http://127.0.0.1:4610"
_SESSION_RE = re.compile(r"^[a-f0-9]{64}$")
_RUN_RE = re.compile(r"^[a-z0-9_]{1,180}$")
_SPONSOR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_TERMINAL = frozenset({"finished", "failed", "stopped", "cancelled", "error"})
_MAX_RESPONSE_BYTES = 512 * 1024
# The longest problem run_plain sends (the console fits a Plain run's
# attachment excerpts within it: jobs._plain_attachments).
MAX_PROBLEM_CHARS = 12_000
_SPONSORED_ENGINES = frozenset(
    {
        "codex", "deepagents", "deepseek_harness", "formal", "improof",
        "kimi", "math_harness", "metaharness", "openclaude", "openclaw",
        "openhands", "plain",
    }
)
# Shown with the sponsored Kimi engine while the broker reports its provider
# as degraded (recent sponsored calls failed or were refused at once).
DEGRADED_NOTICE = (
    "Kimi's provider is slow or refusing requests right now, so runs may fail. "
    "Try again later."
)
_credential_lock = threading.RLock()
_health_cache: tuple[float, str, bool] = (0.0, "", False)
# The broker's passive view of its provider, from the same /healthz answer.
_health_degraded: tuple[str, bool] = ("", False)
_credential_cache: dict[
    tuple[str, str, str], tuple[float, dict[str, str]]
] = {}


def _singleflight_credential_issue(function):
    def locked(*args, **kwargs):
        with _credential_lock:
            return function(*args, **kwargs)
    return locked


class SponsoredKimiError(RuntimeError):
    """The isolated sponsored service could not complete a Plain run.

    ``status`` and ``error`` are set only for an HTTP error reply: its status
    code and, when the reply was typed, its ``error`` object (``type``,
    ``forwarded``, the upstream status and timing, ``retry_after``; the
    gateway never copies provider text into it).  A caller can then tell a
    refusal that certainly never reached processing
    (``kimi_k3.sponsored_retryable``) from any other failure.
    """

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error = dict(error) if isinstance(error, Mapping) else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


# A loopback token must never follow an HTTP redirect or an ambient proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def endpoint() -> str:
    raw = str(
        os.environ.get("AGENT_MONITOR_SPONSORED_KIMI_URL") or DEFAULT_URL
    ).strip()
    parsed = urlparse(raw)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise SponsoredKimiError(
            "The sponsored Kimi service must be an HTTP loopback endpoint"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise SponsoredKimiError("The sponsored Kimi service port is invalid") from exc
    if port is None:
        raise SponsoredKimiError("The sponsored Kimi service port is required")
    return raw.rstrip("/")


def _decode_response(response: Any) -> dict[str, Any]:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise SponsoredKimiError("Sponsored Kimi response was too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SponsoredKimiError("Sponsored Kimi returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise SponsoredKimiError("Sponsored Kimi returned an invalid response")
    return value


def _request(
    path: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    cookie: str = "",
    client_ip: str = "",
    bearer: str = "",
    timeout: float = 10.0,
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], str]:
    # Extra headers (such as X-Ansatze-Timeout) never replace the fixed ones.
    headers = {**dict(extra_headers or {}), "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = f"public_kimi_session={cookie}"
    if client_ip:
        headers["X-Public-Client-IP"] = client_ip
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(
        endpoint() + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with _opener.open(request, timeout=max(0.2, timeout)) as response:
            value = _decode_response(response)
            set_cookie = str(response.headers.get("Set-Cookie") or "")
    except urllib.error.HTTPError as exc:
        try:
            value = _decode_response(exc)
        except SponsoredKimiError:
            value = {}
        error = value.get("error")
        typed = error if isinstance(error, Mapping) else None
        if typed is not None:
            # Internal endpoints answer with a typed {"error": {"message": ...}}.
            error = typed.get("message")
        message = str(error or f"HTTP {exc.code}")
        raise SponsoredKimiError(
            f"Sponsored Kimi request failed: {message}",
            status=exc.code if isinstance(exc.code, int) else None,
            error=typed,
        ) from None
    except (OSError, urllib.error.URLError) as exc:
        raise SponsoredKimiError(
            "Sponsored Kimi service is temporarily unavailable"
        ) from exc

    token = cookie
    if set_cookie:
        parsed_cookie = SimpleCookie()
        try:
            parsed_cookie.load(set_cookie)
            morsel = parsed_cookie.get("public_kimi_session")
            candidate = str(morsel.value if morsel else "")
        except Exception:  # noqa: BLE001
            candidate = ""
        if _SESSION_RE.fullmatch(candidate):
            token = candidate
    return value, token


def enabled() -> bool:
    value = str(os.environ.get("AGENT_MONITOR_SPONSORED_KIMI") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def configured(*, timeout: float = 1.5) -> bool:
    return enabled() and available(timeout=timeout)


def available(*, timeout: float = 1.5) -> bool:
    global _health_cache, _health_degraded
    try:
        route = endpoint()
    except SponsoredKimiError:
        return False
    now = time.monotonic()
    if _health_cache[0] > now and _health_cache[1] == route:
        return _health_cache[2]
    try:
        value, _cookie = _request("/healthz", timeout=timeout)
    except SponsoredKimiError:
        _health_degraded = (route, False)
        _health_cache = (now + 5, route, False)
        return False
    ok = value.get("ok") is True and value.get("service") == "public-kimi"
    upstream = value.get("upstream")
    _health_degraded = (
        route,
        ok and isinstance(upstream, Mapping) and upstream.get("degraded") is True,
    )
    _health_cache = (now + 5, route, ok)
    return ok


def upstream_degraded() -> bool:
    """Whether the broker reports its Kimi provider as slow or refusing.

    Read from the cached /healthz answer behind ``configured``; the broker
    reports only what recent sponsored calls saw and never probes Kimi.
    """
    if not configured():
        return False
    route, degraded = _health_degraded
    try:
        return degraded and route == endpoint()
    except SponsoredKimiError:
        return False


def _own_kimi_key(user: Mapping[str, Any] | None) -> bool:
    """Whether this account's Kimi runs use its own key, never the broker."""
    if user is None:
        return False
    from agent_monitor.settings import resolved_user_env

    try:
        env = resolved_user_env(dict(user))
    except Exception:  # noqa: BLE001 - the notice must not break the engine list
        return False
    return bool(str(env.get("KIMI_API_KEY") or "").strip())


def with_degraded_notice(
    engines: list[dict[str, Any]], user: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Add ``DEGRADED_NOTICE`` to the engines the sponsored broker serves.

    Only while it applies, and only for a viewer whose Kimi runs would use the
    broker (an account with its own Kimi key never does).  The console shows
    it while one of those engines runs a Kimi model.
    """
    if not upstream_degraded() or _own_kimi_key(user):
        return engines
    return [
        {**engine, "notice": DEGRADED_NOTICE} if supports_engine(engine.get("id")) else engine
        for engine in engines
    ]


def _client_ip(client_id: int | str | None) -> str:
    digest = hashlib.sha256(str(client_id or "anonymous").encode("utf-8")).digest()
    # Benchmarking range; stable per signed-in account and never routed.
    return f"198.18.{digest[0]}.{digest[1]}"


def supported_engines() -> tuple[str, ...]:
    """Harnesses with an attested Kimi Chat Completions transport."""
    return tuple(sorted(_SPONSORED_ENGINES - {"formal"}))


def supports_engine(engine: str) -> bool:
    return str(engine or "").strip().lower() in _SPONSORED_ENGINES


@_singleflight_credential_issue
def issue_credentials(
    *,
    engine: str,
    client_id: int | str | None,
    cache_key: str = "",
    timeout: float = 5.0,
    minimum_ttl_seconds: int = 60,
) -> dict[str, str]:
    """Return a short-lived loopback credential, never the provider key."""
    selected = str(engine or "").strip().lower()
    if selected not in _SPONSORED_ENGINES:
        raise SponsoredKimiError(
            f"Sponsored Kimi is not available for the {selected or 'selected'} harness"
        )
    key = (str(client_id or "anonymous"), selected, str(cache_key or "default"))
    now = time.monotonic()
    required_ttl = max(60, min(int(minimum_ttl_seconds), 24 * 3600))
    with _credential_lock:
        cached = _credential_cache.get(key)
        if cached is not None and cached[0] > now + required_ttl:
            environment = dict(cached[1])
            try:
                models, _cookie = _request(
                    "/internal/v1/models",
                    bearer=environment.get("KIMI_API_KEY", ""),
                    timeout=timeout,
                )
                model_ids = {
                    str(item.get("id") or "").strip().lower()
                    for item in (models.get("data") or [])
                    if isinstance(item, Mapping)
                }
                if model_ids == {"kimi-k3"}:
                    return environment
            except SponsoredKimiError:
                pass
            _credential_cache.pop(key, None)
    issued, _cookie = _request(
        "/internal/sessions",
        method="POST",
        payload={"engine": selected},
        client_ip=_client_ip(client_id),
        timeout=timeout,
    )
    token = str(issued.get("api_token") or "").strip()
    base_url = str(issued.get("base_url") or "").strip().rstrip("/")
    model = str(issued.get("model") or "").strip().lower()
    returned_engine = str(issued.get("engine") or "").strip().lower()
    try:
        ttl = int(issued.get("expires_in") or 0)
    except (TypeError, ValueError):
        ttl = 0
    if (
        not _SPONSOR_TOKEN_RE.fullmatch(token)
        or base_url != endpoint() + "/internal/v1"
        or model != "kimi-k3"
        or returned_engine != selected
        or ttl < required_ttl
        or ttl > 24 * 3600
    ):
        raise SponsoredKimiError("Sponsored Kimi credential attestation failed")
    environment = {
        "KIMI_API_KEY": token,
        "KIMI_API_BASE": base_url,
        "KIMI_REASONING_EFFORT": "high",
        "AGENT_MONITOR_SPONSORED_KIMI": "1",
        "AGENT_MONITOR_SPONSORED_KIMI_URL": endpoint(),
    }
    with _credential_lock:
        _credential_cache[key] = (time.monotonic() + ttl, dict(environment))
    return environment


def run_plain(
    problem: str,
    *,
    client_id: int | str | None,
    stopped: Callable[[], bool],
    on_update: Callable[[dict[str, Any]], None] | None = None,
    timeout: float = 620.0,
    poll_interval: float = 0.75,
) -> dict[str, Any]:
    if not isinstance(problem, str) or not problem.strip():
        raise SponsoredKimiError("Sponsored Kimi requires a nonempty problem")
    if len(problem) > MAX_PROBLEM_CHARS:
        raise SponsoredKimiError(
            f"Sponsored Kimi problems are limited to {MAX_PROBLEM_CHARS:,} characters"
        )

    started, cookie = _request(
        "/api/runs",
        method="POST",
        payload={"engine": "plain", "problem": problem.strip()},
        client_ip=_client_ip(client_id),
        timeout=10.0,
    )
    run_id = str(started.get("run_id") or "")
    if not _RUN_RE.fullmatch(run_id) or not _SESSION_RE.fullmatch(cookie):
        raise SponsoredKimiError("Sponsored Kimi returned invalid run ownership")
    if started.get("engine") not in {None, "plain"} or started.get("model") not in {
        None,
        "kimi-k3",
    }:
        raise SponsoredKimiError("Sponsored Kimi route attestation failed")

    deadline = time.monotonic() + max(1.0, timeout)
    last_projection = ""
    transient_failures = 0
    while time.monotonic() < deadline:
        if stopped():
            try:
                _request(
                    f"/api/runs/{run_id}/stop",
                    method="POST",
                    cookie=cookie,
                    timeout=5.0,
                )
            except SponsoredKimiError:
                pass
            return {
                "run_id": run_id,
                "status": "stopped",
                "engine": "plain",
                "model": "kimi-k3",
                "error": "Stopped by user",
                "proof": "",
                "events": [],
            }
        try:
            status, _ignored = _request(
                f"/api/runs/{run_id}",
                cookie=cookie,
                timeout=min(10.0, max(0.2, deadline - time.monotonic())),
            )
            transient_failures = 0
        except SponsoredKimiError:
            transient_failures += 1
            if transient_failures >= 5:
                raise
            time.sleep(min(poll_interval, 0.5))
            continue
        if (
            status.get("run_id") != run_id
            or status.get("engine") != "plain"
            or status.get("model") != "kimi-k3"
        ):
            raise SponsoredKimiError("Sponsored Kimi status attestation failed")
        projection = json.dumps(
            {
                "status": status.get("status"),
                "error": status.get("error"),
                "proof": status.get("proof"),
                "events": status.get("events"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if on_update is not None and projection != last_projection:
            on_update(status)
            last_projection = projection
        terminal = str(status.get("status") or "").lower()
        if terminal in _TERMINAL:
            return status
        time.sleep(max(0.05, poll_interval))

    try:
        _request(
            f"/api/runs/{run_id}/stop",
            method="POST",
            cookie=cookie,
            timeout=5.0,
        )
    except SponsoredKimiError:
        pass
    raise SponsoredKimiError("Sponsored Kimi run timed out")
