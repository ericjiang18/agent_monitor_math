"""Loopback credential boundary for the standalone public Kimi service.

The public proof workers receive only a short-lived loopback bearer token.  The
real upstream credential remains in this server process and is substituted only
after a request has passed the path, size, authentication, model, and rate
checks below.
"""
from __future__ import annotations

import hmac
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib import error as urlerror
from urllib import request as urlrequest

from agent_monitor.kimi_responses_proxy import sanitize_responses_payload
from agent_monitor.runners.kimi_chat_completions_proxy import sanitize_chat_payload

_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_STREAM_RESPONSE_BYTES = 16 * 1024 * 1024
_CHAT_PATH = "/v1/chat/completions"
_RESPONSES_PATH = "/v1/responses"


def _bounded_positive(value: object, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def _sanitize_payload(
    payload: Mapping[str, Any],
    *,
    reasoning_effort: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Force K3 while retaining bounded OpenAI-compatible tool turns."""
    sanitized, _summary = sanitize_chat_payload(
        payload,
        reasoning_effort=reasoning_effort,
    )
    messages = sanitized.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty array")
    if "stream" in sanitized and not isinstance(sanitized.get("stream"), bool):
        raise ValueError("stream must be a boolean")
    sanitized["max_tokens"] = _bounded_positive(
        sanitized.get("max_tokens"),
        default=max_output_tokens,
        low=1,
        high=max_output_tokens,
    )
    return sanitized


def _sanitize_responses_gateway_payload(
    payload: Mapping[str, Any],
    *,
    reasoning_effort: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Force and cap the Formal-only Responses request sent upstream."""
    sanitized, _summary = sanitize_responses_payload(
        payload,
        reasoning_effort=reasoning_effort,
    )
    if not sanitized.get("input"):
        raise ValueError("input must be nonempty")
    if "stream" in sanitized and not isinstance(sanitized.get("stream"), bool):
        raise ValueError("stream must be a boolean")
    sanitized["model"] = "kimi-k3"
    sanitized["max_output_tokens"] = _bounded_positive(
        sanitized.get("max_output_tokens"),
        default=max_output_tokens,
        low=1,
        high=max_output_tokens,
    )
    return sanitized




class _GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        *,
        upstream_base: str,
        upstream_api_key: str,
        local_token: str,
        reasoning_effort: str,
        max_output_tokens: int,
        max_concurrency: int,
        calls_per_hour: int,
        upstream_timeout: float,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _GatewayHandler)
        self.upstream_base = upstream_base.rstrip("/")
        self.upstream_authorization = "Bearer " + upstream_api_key
        self.expected_authorization = "Bearer " + local_token
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.upstream_timeout = upstream_timeout
        self.capacity = threading.BoundedSemaphore(max_concurrency)
        self.calls_per_hour = calls_per_hour
        self.call_times: deque[float] = deque()
        self.call_lock = threading.Lock()

    def admit_call(self) -> tuple[bool, int]:
        now = time.monotonic()
        with self.call_lock:
            while self.call_times and now - self.call_times[0] >= 3600:
                self.call_times.popleft()
            if len(self.call_times) >= self.calls_per_hour:
                retry = max(1, int(3600 - (now - self.call_times[0])))
                return False, retry
            self.call_times.append(now)
        return True, 0


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def gateway(self) -> _GatewayServer:
        return self.server  # type: ignore[return-value]

    def _json(self, status: int, payload: Mapping[str, Any], **headers: str) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = str(self.headers.get("Authorization") or "").strip()
        return hmac.compare_digest(supplied, self.gateway.expected_authorization)

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def do_GET(self) -> None:  # noqa: N802
        self._json(405, {"error": {"message": "method not allowed"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self._path()
        if path not in {_CHAT_PATH, _RESPONSES_PATH}:
            self._json(404, {"error": {"message": "unsupported gateway path"}})
            return
        if not self._authorized():
            self._json(401, {"error": {"message": "authorization required"}})
            return
        media_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0]
        if media_type.strip().lower() != "application/json":
            self._json(415, {"error": {"message": "application/json required"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 1 or length > _MAX_REQUEST_BYTES:
            self._json(
                413 if length > _MAX_REQUEST_BYTES else 400,
                {"error": {"message": "invalid request size"}},
            )
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            if path == _RESPONSES_PATH:
                sanitized = _sanitize_responses_gateway_payload(
                    payload,
                    reasoning_effort=self.gateway.reasoning_effort,
                    max_output_tokens=self.gateway.max_output_tokens,
                )
            else:
                sanitized = _sanitize_payload(
                    payload,
                    reasoning_effort=self.gateway.reasoning_effort,
                    max_output_tokens=self.gateway.max_output_tokens,
                )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        if not self.gateway.capacity.acquire(blocking=False):
            self._json(
                429,
                {"error": {"message": "public Kimi call concurrency reached"}},
                Retry_After="2",
            )
            return
        try:
            admitted, retry_after = self.gateway.admit_call()
            if not admitted:
                self._json(
                    429,
                    {"error": {"message": "public Kimi call quota reached"}},
                    Retry_After=str(retry_after),
                )
                return
            self._forward(path, sanitized)
        finally:
            self.gateway.capacity.release()

    def _forward(self, path: str, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request = urlrequest.Request(
            self.gateway.upstream_base + path.removeprefix("/v1"),
            data=body,
            headers={
                "Authorization": self.gateway.upstream_authorization,
                "Content-Type": "application/json",
                "Accept": str(self.headers.get("Accept") or "application/json"),
                "User-Agent": "ProvingConsole-Public-Kimi/1",
            },
            method="POST",
        )
        try:
            upstream = urlrequest.urlopen(
                request,
                timeout=self.gateway.upstream_timeout,
            )
        except urlerror.HTTPError as exc:
            upstream = exc
        except (OSError, urlerror.URLError):
            self._json(502, {"error": {"message": "Kimi upstream unavailable"}})
            return
        with upstream:
            status = int(getattr(upstream, "status", upstream.code))
            if status < 200 or status >= 300:
                retry_after = str(upstream.headers.get("Retry-After") or "").strip()
                headers = (
                    {"Retry_After": retry_after}
                    if retry_after.isascii() and retry_after.isdigit() and len(retry_after) <= 6
                    else {}
                )
                # Provider error bodies can contain account, billing, or echoed
                # request details.  Preserve only the status and safe retry hint.
                self._json(
                    status,
                    {"error": {"message": "Kimi request was rejected"}},
                    **headers,
                )
                return
            if payload.get("stream") is True:
                self.send_response(status)
                for key in ("Content-Type", "Cache-Control", "X-Request-ID"):
                    value = upstream.headers.get(key)
                    if value:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.close_connection = True
                read = getattr(upstream, "read1", None)
                if not callable(read):
                    read = upstream.read
                total = 0
                while True:
                    chunk = read(16 * 1024)
                    if not chunk:
                        return
                    total += len(chunk)
                    if total > _MAX_STREAM_RESPONSE_BYTES:
                        return
                    self.wfile.write(chunk)
                    self.wfile.flush()
            try:
                response_body = upstream.read(_MAX_RESPONSE_BYTES + 1)
            except OSError:
                self._json(502, {"error": {"message": "Kimi upstream unavailable"}})
                return
            if len(response_body) > _MAX_RESPONSE_BYTES:
                self._json(502, {"error": {"message": "Kimi response too large"}})
                return
            self.send_response(status)
            for key in ("Content-Type", "Cache-Control", "X-Request-ID"):
                value = upstream.headers.get(key)
                if value:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(response_body)

    def do_PUT(self) -> None:  # noqa: N802
        self._json(405, {"error": {"message": "method not allowed"}})

    do_DELETE = do_PUT
    do_PATCH = do_PUT


class PublicKimiGateway:
    """Lifecycle for the local-token-to-upstream-key credential boundary."""

    def __init__(
        self,
        *,
        upstream_base: str,
        upstream_api_key: str,
        local_token: str,
        reasoning_effort: str = "max",
        max_output_tokens: int = 8192,
        max_concurrency: int = 4,
        calls_per_hour: int = 180,
        upstream_timeout: float = 900,
    ) -> None:
        self._server = _GatewayServer(
            upstream_base=upstream_base,
            upstream_api_key=upstream_api_key,
            local_token=local_token,
            reasoning_effort=reasoning_effort,
            max_output_tokens=_bounded_positive(
                max_output_tokens, default=8192, low=1024, high=32768
            ),
            max_concurrency=_bounded_positive(
                max_concurrency, default=4, low=1, high=16
            ),
            calls_per_hour=_bounded_positive(
                calls_per_hour, default=180, low=1, high=2000
            ),
            upstream_timeout=float(upstream_timeout),
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="public-kimi-credential-gateway",
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "PublicKimiGateway":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
