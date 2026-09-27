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
_SPONSORED_ENGINES = frozenset(
    {
        "codex", "deepagents", "deepseek_harness", "formal", "improof",
        "kimi", "math_harness", "metaharness", "openclaude", "openclaw",
        "openhands", "plain",
    }
)
_credential_lock = threading.RLock()
_health_cache: tuple[float, str, bool] = (0.0, "", False)
_credential_cache: dict[
    tuple[str, str, str], tuple[float, dict[str, str]]
] = {}


def _singleflight_credential_issue(function):
    def locked(*args, **kwargs):
        with _credential_lock:
            return function(*args, **kwargs)
    return locked


class SponsoredKimiError(RuntimeError):
    """The isolated sponsored service could not complete a Plain run."""


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
) -> tuple[dict[str, Any], str]:
    headers = {"Accept": "application/json"}
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
        message = str(value.get("error") or f"HTTP {exc.code}")
        raise SponsoredKimiError(f"Sponsored Kimi request failed: {message}") from None
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
    global _health_cache
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
        _health_cache = (now + 5, route, False)
        return False
    ok = value.get("ok") is True and value.get("service") == "public-kimi"
    _health_cache = (now + 5, route, ok)
    return ok


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
    if len(problem) > 12_000:
        raise SponsoredKimiError(
            "Sponsored Kimi problems are limited to 12,000 characters"
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
