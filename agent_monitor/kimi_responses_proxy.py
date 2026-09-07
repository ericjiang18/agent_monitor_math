"""Request-sanitizing proxy for Kimi's OpenAI Responses endpoint.

Kimi K3 implements the Responses wire protocol, including function tools, but
it intentionally rejects a few OpenAI/Codex extension fields and built-in tool
types.  Codex CLI currently has no per-provider request-filter hook, so API
runs use this loopback-only proxy to remove unsupported metadata and translate
Codex namespace tool declarations into ordinary Responses function tools.
"""
from __future__ import annotations

import copy
import json
import math
import os
import threading
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib import error as urlerror
from urllib import request as urlrequest

from agent_monitor import kimi_k3


_DROP_RESPONSE_FIELDS = frozenset(
    {
        "client_metadata",
        "context_management",
        "output_config",
    }
)
_DROP_TOOL_TYPES = frozenset(
    {
        "code_interpreter",
        "computer",
        "computer_use_preview",
        "file_search",
        "image_generation",
        "web_search",
        "web_search_preview",
    }
)

_DEFAULT_UPSTREAM_TIMEOUT = 3600.0


def _configured_upstream_timeout(value: float | str | None = None) -> float:
    """Return a bounded per-read timeout suitable for long reasoning turns."""
    if value is None:
        value = os.environ.get("KIMI_UPSTREAM_TIMEOUT", "") or str(
            _DEFAULT_UPSTREAM_TIMEOUT
        )
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = _DEFAULT_UPSTREAM_TIMEOUT
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = _DEFAULT_UPSTREAM_TIMEOUT
    # Avoid both a near-immediate failure loop and an indefinitely stuck socket.
    return max(30.0, min(timeout, 7200.0))


def _safe_retry_after(value: str | None) -> str | None:
    """Validate an upstream Retry-After value before copying it downstream."""
    candidate = str(value or "").strip()
    if (
        not candidate
        or len(candidate) > 128
        or "\r" in candidate
        or "\n" in candidate
    ):
        return None
    if candidate.isascii() and candidate.isdigit():
        return candidate
    try:
        parsed = parsedate_to_datetime(candidate)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    return candidate


def _relay_body(upstream: Any, downstream: Any, *, chunk_size: int = 16 * 1024) -> None:
    """Relay available bytes immediately instead of filling a large SSE buffer."""
    read = getattr(upstream, "read1", None)
    if not callable(read):
        read = upstream.read
    while True:
        chunk = read(chunk_size)
        if not chunk:
            return
        downstream.write(chunk)
        downstream.flush()


def _function_tool(tool: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one Responses function tool from a namespace member."""
    function = tool.get("function")
    source = function if isinstance(function, Mapping) else tool
    name = str(source.get("name") or "").strip()
    parameters = source.get("parameters")
    if not name or not isinstance(parameters, Mapping):
        return None
    result: dict[str, Any] = {
        "type": "function",
        "name": name,
        "parameters": copy.deepcopy(dict(parameters)),
    }
    description = source.get("description")
    if isinstance(description, str) and description.strip():
        result["description"] = description
    if isinstance(source.get("strict"), bool):
        result["strict"] = source["strict"]
    return result


def _flatten_tools(tools: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep function tools, flatten namespaces, and drop unsupported built-ins."""
    if not isinstance(tools, list):
        return [], []
    flattened: list[dict[str, Any]] = []
    dropped: list[str] = []
    names: set[str] = set()
    for raw in tools:
        if not isinstance(raw, Mapping):
            dropped.append("invalid")
            continue
        kind = str(raw.get("type") or "").strip()
        candidates: list[Mapping[str, Any]]
        if kind == "namespace":
            members = raw.get("tools")
            if not isinstance(members, list):
                members = raw.get("functions")
            candidates = [item for item in (members or []) if isinstance(item, Mapping)]
            if not candidates:
                dropped.append("namespace")
        elif kind == "function":
            candidates = [raw]
        elif kind in _DROP_TOOL_TYPES:
            dropped.append(kind)
            continue
        else:
            dropped.append(kind or "unknown")
            continue
        for candidate in candidates:
            function_tool = _function_tool(candidate)
            if function_tool is None:
                dropped.append(f"{kind or 'tool'}:invalid-function")
                continue
            name = function_tool["name"]
            if name in names:
                dropped.append(f"duplicate:{name}")
                continue
            names.add(name)
            flattened.append(function_tool)
    return flattened, dropped


def sanitize_responses_payload(
    payload: Mapping[str, Any],
    *,
    reasoning_effort: str | None = None,
    min_output_tokens: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a Kimi-compatible copy and non-secret transformation metrics."""
    result = copy.deepcopy(dict(payload))
    removed_fields = sorted(key for key in _DROP_RESPONSE_FIELDS if key in result)
    for key in removed_fields:
        result.pop(key, None)

    dropped_tools: list[str] = []
    if "tools" in result:
        tools, dropped_tools = _flatten_tools(result.get("tools"))
        if tools:
            result["tools"] = tools
        else:
            result.pop("tools", None)
            result.pop("tool_choice", None)
            result.pop("parallel_tool_calls", None)

    tool_choice = result.get("tool_choice")
    if isinstance(tool_choice, Mapping):
        choice_type = str(tool_choice.get("type") or "")
        if choice_type not in {"function", "allowed_tools"}:
            result["tool_choice"] = "auto"

    effort = kimi_k3.reasoning_effort(value=reasoning_effort)
    reasoning = result.get("reasoning")
    if isinstance(reasoning, Mapping):
        result["reasoning"] = {"effort": effort}
    else:
        result["reasoning"] = {"effort": effort}

    if min_output_tokens is not None:
        try:
            current_output_tokens = int(result.get("max_output_tokens") or 0)
        except (TypeError, ValueError):
            current_output_tokens = 0
        # Codex cannot discover metadata for this third-party model and falls
        # back to 128 output tokens. Reasoning consumes that before a proof
        # agent can complete its first workspace edit.
        result["max_output_tokens"] = max(
            current_output_tokens,
            max(1, int(min_output_tokens)),
        )

    include = result.get("include")
    if isinstance(include, list):
        supported = [
            item for item in include
            if item in {"reasoning.encrypted_content", "message.output_text.logprobs"}
        ]
        if supported:
            result["include"] = supported
        else:
            result.pop("include", None)

    return result, {
        "removed_fields": removed_fields,
        "dropped_tool_types": dropped_tools,
        "forwarded_tool_names": [
            str(tool.get("name") or "") for tool in result.get("tools") or []
        ],
        "reasoning_effort": effort,
        "max_output_tokens": result.get("max_output_tokens"),
    }


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        upstream_base: str,
        reasoning_effort: str,
        max_output_tokens: int,
        upstream_timeout: float,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.upstream_base = upstream_base.rstrip("/")
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.upstream_timeout = upstream_timeout
        self.last_summary: dict[str, Any] = {}


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def proxy_server(self) -> _ProxyServer:
        return self.server  # type: ignore[return-value]

    def _target_url(self) -> str:
        path = self.path.split("?", 1)[0]
        suffix = path[3:] if path.startswith("/v1") else path
        query = "?" + self.path.split("?", 1)[1] if "?" in self.path else ""
        return self.proxy_server.upstream_base + suffix + query

    def _forward(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return
        if length > 64 * 1024 * 1024:
            self.send_error(413, "request too large")
            return
        body = self.rfile.read(length) if length else None
        summary: dict[str, Any] = {}
        if body and self.command == "POST" and self.path.split("?", 1)[0].endswith("/responses"):
            try:
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise ValueError("Responses body must be an object")
                parsed, summary = sanitize_responses_payload(
                    parsed,
                    reasoning_effort=self.proxy_server.reasoning_effort,
                    min_output_tokens=self.proxy_server.max_output_tokens,
                )
                body = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode()
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self.send_error(400, str(exc))
                return
        self.proxy_server.last_summary = summary

        headers = {
            "Authorization": self.headers.get("Authorization", ""),
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept": self.headers.get("Accept", "application/json"),
            "User-Agent": "ProvingConsole-Kimi-Responses/1",
        }
        request = urlrequest.Request(
            self._target_url(),
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            upstream = urlrequest.urlopen(
                request,
                timeout=self.proxy_server.upstream_timeout,
            )
        except urlerror.HTTPError as exc:
            upstream = exc
        except (OSError, urlerror.URLError) as exc:
            data = json.dumps(
                {"error": {"type": "proxy_error", "message": str(exc)}}
            ).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
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
            retry_after = _safe_retry_after(upstream.headers.get("Retry-After"))
            if retry_after:
                self.send_header("Retry-After", retry_after)
            content_length = upstream.headers.get("Content-Length")
            if content_length:
                self.send_header("Content-Length", content_length)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.flush()
            _relay_body(upstream, self.wfile)

    do_GET = _forward
    do_POST = _forward
    do_DELETE = _forward


class KimiResponsesProxy:
    """Loopback proxy lifecycle used by one Codex subprocess."""

    def __init__(
        self,
        *,
        upstream_base: str | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        upstream_timeout: float | str | None = None,
    ) -> None:
        if max_output_tokens is None:
            try:
                max_output_tokens = int(
                    os.environ.get("KIMI_MAX_OUTPUT_TOKENS", "32768")
                )
            except ValueError:
                max_output_tokens = 32768
        max_output_tokens = max(1024, min(int(max_output_tokens), 131072))
        self._server = _ProxyServer(
            upstream_base or kimi_k3.DEFAULT_BASE_URL,
            kimi_k3.reasoning_effort(value=reasoning_effort),
            max_output_tokens,
            _configured_upstream_timeout(upstream_timeout),
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="kimi-responses-proxy",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    @property
    def last_summary(self) -> dict[str, Any]:
        return dict(self._server.last_summary)

    def __enter__(self) -> "KimiResponsesProxy":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
