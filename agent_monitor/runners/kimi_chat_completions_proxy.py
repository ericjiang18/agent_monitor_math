"""Loopback-only compatibility proxy for Kimi Chat Completions."""
from __future__ import annotations

import copy
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib import error as urlerror
from urllib import request as urlrequest


_ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})
_ALLOWED_REASONING_EFFORTS = frozenset({"low", "high", "max"})
_DROP_CHAT_FIELDS = frozenset(
    {
        "background",
        "context_management",
        "include",
        "metadata",
        "modalities",
        "output_config",
        "prediction",
        "reasoning",
        "service_tier",
        "store",
        "verbosity",
        "web_search_options",
    }
)
_MAX_REQUEST_BYTES = 64 * 1024 * 1024


def _reasoning_effort(value: object, fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _ALLOWED_REASONING_EFFORTS:
        return normalized
    normalized_fallback = str(fallback or "").strip().lower()
    if normalized_fallback in _ALLOWED_REASONING_EFFORTS:
        return normalized_fallback
    return "max"


def sanitize_chat_payload(
    payload: Mapping[str, Any],
    *,
    reasoning_effort: str = "max",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a Kimi-compatible request and content-free transformation facts."""
    result = copy.deepcopy(dict(payload))
    removed_fields = sorted(key for key in _DROP_CHAT_FIELDS if key in result)
    nested_reasoning = result.get("reasoning")
    requested_effort = result.get("reasoning_effort")
    if requested_effort is None and isinstance(nested_reasoning, Mapping):
        requested_effort = nested_reasoning.get("effort")
    for key in removed_fields:
        result.pop(key, None)

    messages = result.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    rewritten = 0
    normalized_messages: list[dict[str, Any]] = []
    for raw in messages:
        if not isinstance(raw, Mapping):
            raise ValueError("each message must be an object")
        message = copy.deepcopy(dict(raw))
        role = str(message.get("role") or "").strip().lower()
        if role == "developer":
            role = "system"
            rewritten += 1
        if role not in _ALLOWED_ROLES:
            raise ValueError("unsupported message role")
        message["role"] = role
        normalized_messages.append(message)
    result["messages"] = normalized_messages
    result["model"] = "kimi-k3"
    result["reasoning_effort"] = _reasoning_effort(
        requested_effort, reasoning_effort
    )

    if "max_tokens" not in result and "max_completion_tokens" in result:
        result["max_tokens"] = result.pop("max_completion_tokens")
    else:
        result.pop("max_completion_tokens", None)

    return result, {
        "removed_fields": removed_fields,
        "rewritten_developer_messages": rewritten,
        "reasoning_effort": result["reasoning_effort"],
    }


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        upstream_base: str,
        reasoning_effort: str,
        upstream_api_key: str,
        local_token: str,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.upstream_url = upstream_base.rstrip("/") + "/chat/completions"
        self.reasoning_effort = _reasoning_effort(None, reasoning_effort)
        self.expected_authorization = "Bearer " + local_token
        self.upstream_authorization = "Bearer " + upstream_api_key
        self.last_summary: dict[str, Any] = {}


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def proxy_server(self) -> _ProxyServer:
        return self.server  # type: ignore[return-value]

    def _json_error(self, status: int, message: str) -> None:
        body = json.dumps(
            {"error": {"type": "proxy_error", "message": message}},
            separators=(",", ":"),
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] not in {
            "/chat/completions",
            "/v1/chat/completions",
        }:
            self._json_error(404, "unsupported proxy path")
            return
        authorization = str(self.headers.get("Authorization") or "").strip()
        if not hmac.compare_digest(
            authorization, self.proxy_server.expected_authorization
        ):
            self._json_error(401, "Bearer authorization is required")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._json_error(400, "invalid Content-Length")
            return
        if length < 1 or length > _MAX_REQUEST_BYTES:
            self._json_error(
                413 if length > _MAX_REQUEST_BYTES else 400,
                "invalid request size",
            )
            return
        try:
            parsed = json.loads(self.rfile.read(length))
            if not isinstance(parsed, dict):
                raise ValueError("request body must be an object")
            sanitized, summary = sanitize_chat_payload(
                parsed,
                reasoning_effort=self.proxy_server.reasoning_effort,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._json_error(400, str(exc))
            return
        body = json.dumps(
            sanitized, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.proxy_server.last_summary = summary
        request = urlrequest.Request(
            self.proxy_server.upstream_url,
            data=body,
            headers={
                "Authorization": self.proxy_server.upstream_authorization,
                "Content-Type": "application/json",
                "Accept": self.headers.get("Accept", "application/json"),
                "User-Agent": "ProvingConsole-Kimi-Chat/1",
            },
            method="POST",
        )
        try:
            upstream = urlrequest.urlopen(request, timeout=900)
        except urlerror.HTTPError as exc:
            upstream = exc
        except (OSError, urlerror.URLError):
            self._json_error(502, "Kimi upstream request failed")
            return

        with upstream:
            self.send_response(int(getattr(upstream, "status", upstream.code)))
            for key in (
                "Content-Type",
                "Cache-Control",
                "OpenAI-Request-ID",
                "X-Request-ID",
            ):
                value = upstream.headers.get(key)
                if value:
                    self.send_header(key, value)
            content_length = upstream.headers.get("Content-Length")
            if content_length:
                self.send_header("Content-Length", content_length)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()

    def do_GET(self) -> None:
        self._json_error(405, "method not allowed")


class KimiChatCompletionsProxy:
    """Lifecycle for one authenticated, loopback-only Kimi compatibility proxy."""

    def __init__(
        self,
        *,
        upstream_base: str,
        reasoning_effort: str,
        upstream_api_key: str,
        local_token: str,
    ) -> None:
        self._server = _ProxyServer(
            upstream_base,
            reasoning_effort,
            upstream_api_key,
            local_token,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="kimi-chat-completions-proxy",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    @property
    def last_summary(self) -> dict[str, Any]:
        return dict(self._server.last_summary)

    def __enter__(self) -> "KimiChatCompletionsProxy":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
