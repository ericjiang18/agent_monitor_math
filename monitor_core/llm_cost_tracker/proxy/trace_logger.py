"""LiteLLM proxy custom logger for LLM cost monitoring.

Writes one JSONL row per LLM call. Works with any application that routes
calls through the litellm proxy — completely decoupled from specific benchmarks.

Trace grouping uses incoming `x-trace-id` / `x-trace-name` HTTP headers.
Calls without a trace header are logged under a default "untraced" group.

Registered in config.yaml:
    litellm_settings:
      callbacks: llm_cost_tracker.proxy.trace_logger.proxy_logger
"""
from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from litellm.integrations.custom_logger import CustomLogger


def _log_path() -> str:
    return os.environ.get(
        "LLM_MONITOR_LOG", os.path.join(os.getcwd(), "monitor", "calls.jsonl")
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _infer_caller(messages: List[Dict[str, Any]], tools: Optional[list]) -> str:
    """Infer caller role from message context. Override via x-caller header."""
    if tools:
        return "agent"
    system = ""
    for m in messages or []:
        if m.get("role") == "system":
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            system = content
            break
    if "user interacting with an agent" in system:
        return "user_sim"
    return "agent"


def _dig_headers(kwargs: Dict[str, Any]) -> Dict[str, str]:
    """Extract incoming request headers from litellm kwargs."""
    lp = kwargs.get("litellm_params") or {}
    for path in (
        ("proxy_server_request", "headers"),
        ("metadata", "headers"),
    ):
        node: Any = lp
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and isinstance(node, dict):
            return {str(k).lower(): v for k, v in node.items()}
    # newer litellm versions
    slo = kwargs.get("standard_logging_object") or {}
    md = slo.get("metadata") or {}
    h = md.get("requester_custom_headers") or md.get("headers") or {}
    if isinstance(h, dict):
        return {str(k).lower(): v for k, v in h.items()}
    return {}


def _extract_usage(response_obj) -> Dict[str, int]:
    """Extract token usage including cache metrics from litellm response."""
    usage = getattr(response_obj, "usage", None)
    if usage is None and isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}

    # Chat Completions: prompt_tokens / completion_tokens
    # Responses API: input_tokens / output_tokens
    prompt = int(
        getattr(usage, "prompt_tokens", 0)
        or getattr(usage, "input_tokens", 0)
        or (usage.get("prompt_tokens") if isinstance(usage, dict) else 0)
        or (usage.get("input_tokens") if isinstance(usage, dict) else 0)
        or 0
    )
    completion = int(
        getattr(usage, "completion_tokens", 0)
        or getattr(usage, "output_tokens", 0)
        or (usage.get("completion_tokens") if isinstance(usage, dict) else 0)
        or (usage.get("output_tokens") if isinstance(usage, dict) else 0)
        or 0
    )

    # Cache tokens: litellm reports via prompt_tokens_details.cached_tokens
    # and cache_creation_input_tokens (Anthropic-specific)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached_tokens = 0
    if prompt_details:
        cached_tokens = int(getattr(prompt_details, "cached_tokens", 0) or 0)

    cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

    # If we have cache_creation from Anthropic/Bedrock, use it directly.
    # Otherwise, infer: cache_write = prompt - cached (for SGLang/OpenAI style).
    if cache_creation > 0:
        cache_read = cached_tokens
        cache_write = cache_creation
        input_tokens = prompt - cache_read - cache_write
    elif cached_tokens > 0:
        # SGLang/OpenAI style: prompt_tokens is total, cached_tokens is the hit
        cache_read = cached_tokens
        cache_write = prompt - cached_tokens
        input_tokens = 0
    else:
        input_tokens = prompt
        cache_read = 0
        cache_write = 0

    return {
        "input_tokens": max(input_tokens, 0),
        "output_tokens": completion,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def _fallback_response_content(response_obj, kwargs: Dict[str, Any]) -> tuple[str | None, str | None]:
    """Extra extraction paths for LiteLLM / Responses API objects."""
    content = None
    thinking = None

    for attr in ("output_text", "text", "content"):
        val = getattr(response_obj, attr, None)
        if isinstance(val, str) and val.strip():
            content = val
            break

    dumped = None
    if hasattr(response_obj, "model_dump"):
        try:
            dumped = response_obj.model_dump()
        except Exception:
            pass
    elif isinstance(response_obj, dict):
        dumped = response_obj

    if dumped:
        if not content:
            for key in ("output_text", "text", "content"):
                val = dumped.get(key)
                if isinstance(val, str) and val.strip():
                    content = val
                    break
        if not thinking:
            r = dumped.get("reasoning")
            if isinstance(r, str):
                thinking = r

    slo = kwargs.get("standard_logging_object") or {}
    if not content:
        resp = slo.get("response") or {}
        if isinstance(resp, dict):
            content = resp.get("output_text") or resp.get("text")

    return content, thinking


    """Compute dollar cost from token usage. Returns None for unpriced models."""
    try:
        from llm_cost_tracker.pricing import (
            get_token_price, get_cache_pricing, has_real_pricing,
        )
        if not has_real_pricing(model):
            return None
        in_price, out_price = get_token_price(model)
        cache_read_price, cache_write_price = get_cache_pricing(model)
        cost = (
            usage["input_tokens"] * in_price / 1_000_000
            + usage["cache_read_tokens"] * cache_read_price / 1_000_000
            + usage["cache_write_tokens"] * cache_write_price / 1_000_000
            + usage["output_tokens"] * out_price / 1_000_000
        )
        return round(cost, 6)
    except Exception:
        return None


class TraceLogger(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._rounds: Dict[str, int] = defaultdict(int)

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj, start_time, end_time)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj, start_time, end_time)

    def _record(self, kwargs, response_obj, start_time, end_time) -> None:
        try:
            self._write_row(kwargs, response_obj, start_time, end_time)
        except Exception as e:
            print(f"[trace_logger] failed to log call: {e}")

    def _write_row(self, kwargs, response_obj, start_time, end_time) -> None:
        messages = kwargs.get("messages") or []
        opt = kwargs.get("optional_params") or {}
        tools = opt.get("tools")
        model = kwargs.get("model") or ""

        usage = _extract_usage(response_obj)
        # Skip background-job poll noise (responses.retrieve every ~2s until complete).
        if sum(usage.values()) == 0:
            return
        if len(messages) == 1 and messages[0].get("content") == "default-message-value":
            return

        headers = _dig_headers(kwargs)
        trace_id = headers.get("x-trace-id", "untraced")
        trace_name = headers.get("x-trace-name") or trace_id.split("::")[0]
        caller = headers.get("x-caller") or _infer_caller(messages, tools)

        harness_meta = None
        raw_hm = headers.get("x-harness-meta")
        if raw_hm:
            try:
                harness_meta = json.loads(raw_hm) if isinstance(raw_hm, str) else raw_hm
            except (json.JSONDecodeError, TypeError):
                harness_meta = None

        cost = _compute_cost(usage, model)

        # Response message — Chat Completions or Responses API
        resp_msg: Dict[str, Any] = {"role": "assistant", "content": None}
        stop_reason = None
        tool_called = None
        tool_calls_list: List[Dict[str, Any]] = []
        try:
            choice = response_obj.choices[0]
            stop_reason = getattr(choice, "finish_reason", None)
            m = choice.message
            resp_msg["content"] = getattr(m, "content", None)
            tcs = getattr(m, "tool_calls", None)
            if tcs:
                tool_calls_list = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tcs
                ]
                resp_msg["tool_calls"] = tool_calls_list
                tool_called = tcs[0].function.name
        except Exception:
            pass

        # Responses API: output[] with message / reasoning / web_search_call items
        if resp_msg.get("content") is None:
            output_items = getattr(response_obj, "output", None)
            if output_items is None and isinstance(response_obj, dict):
                output_items = response_obj.get("output")
            text_parts: List[str] = []
            thinking_parts: List[str] = []
            if output_items:
                for item in output_items:
                    kind = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
                    if kind == "message":
                        content = getattr(item, "content", None) or (item.get("content") if isinstance(item, dict) else None)
                        if isinstance(content, list):
                            for block in content:
                                bt = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
                                text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
                                if bt in ("output_text", "text") and text:
                                    text_parts.append(text)
                        elif isinstance(content, str):
                            text_parts.append(content)
                    elif kind == "reasoning":
                        summary = getattr(item, "summary", None) or (item.get("summary") if isinstance(item, dict) else None)
                        if summary:
                            if isinstance(summary, list):
                                thinking_parts.extend(str(s) for s in summary)
                            else:
                                thinking_parts.append(str(summary))
                    elif kind == "web_search_call":
                        action = getattr(item, "action", None) or (item.get("action") if isinstance(item, dict) else None)
                        query = ""
                        if isinstance(action, dict):
                            query = action.get("query", "")
                        elif action is not None:
                            query = getattr(action, "query", "") or ""
                        tool_calls_list.append({"type": "web_search", "query": query})
                        tool_called = tool_called or "web_search"
            if text_parts:
                resp_msg["content"] = "\n".join(text_parts)
            if thinking_parts:
                resp_msg["reasoning"] = "\n".join(thinking_parts)
            if tool_calls_list and "tool_calls" not in resp_msg:
                resp_msg["tool_calls"] = tool_calls_list

        fb_content, fb_thinking = _fallback_response_content(response_obj, kwargs)
        if fb_content and not resp_msg.get("content"):
            resp_msg["content"] = fb_content
        if fb_thinking and not resp_msg.get("reasoning"):
            resp_msg["reasoning"] = fb_thinking

        try:
            latency = (end_time - start_time).total_seconds()
        except Exception:
            latency = 0.0

        # Normalize finish_reason to consistent schema
        if stop_reason == "tool_calls":
            stop_reason = "tool_use"
        elif stop_reason == "stop":
            stop_reason = "end_turn"

        with self._lock:
            self._rounds[trace_id] += 1
            round_idx = self._rounds[trace_id]

        row = {
            "kind": "call",
            "ts": _now(),
            "trace_id": trace_id,
            "trace_name": trace_name,
            "round": round_idx,
            "caller": caller,
            "model": model,
            "latency_s": round(latency, 3),
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cache_read_tokens": usage["cache_read_tokens"],
            "cache_write_tokens": usage["cache_write_tokens"],
            "cost_usd": cost,
            "stop_reason": stop_reason,
            "tool_called": tool_called,
            "tool_calls": tool_calls_list or None,
            "request_messages": messages,
            "response_message": resp_msg,
        }
        if harness_meta:
            row["harness"] = harness_meta

        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


proxy_logger = TraceLogger()
