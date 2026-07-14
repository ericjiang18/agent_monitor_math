"""Cost and efficiency metrics extraction from LLM agent trajectories.

Works with any OpenAI-format message list. No benchmark-specific imports.
"""

import json
from typing import Any, Dict, List, Optional, Set
from collections import Counter

from llm_cost_tracker.pricing import get_token_price, get_cache_pricing, has_real_pricing


def extract_tool_calls(messages: List[Dict[str, Any]]) -> List[str]:
    """Extract tool call names from a message trajectory.

    Handles both native tool-calling (tool_calls field) and
    text-based react/act agents (Action: JSON in content).
    """
    tools = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        if msg.get("tool_calls"):
            tools.append(msg["tool_calls"][0]["function"]["name"])
        elif msg.get("content") and "Action:" in msg.get("content", ""):
            name = _parse_text_action(msg["content"])
            if name:
                tools.append(name)
    return tools


def _parse_text_action(content: str) -> Optional[str]:
    """Parse tool name from react/act agent text output."""
    if "Action:" not in content:
        return None
    action_str = content.split("Action:")[-1].strip()
    try:
        parsed = json.loads(action_str)
        name = parsed.get("name", "")
        if name and name != "respond":
            return name
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def count_redundant_calls(messages: List[Dict[str, Any]]) -> int:
    """Count consecutive identical tool calls (same name + same args)."""
    redundant = 0
    prev = None
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        key = None
        if msg.get("tool_calls"):
            tc = msg["tool_calls"][0]["function"]
            key = (tc["name"], tc.get("arguments", ""))
        elif msg.get("content") and "Action:" in msg.get("content", ""):
            action_str = msg["content"].split("Action:")[-1].strip()
            try:
                parsed = json.loads(action_str)
                name = parsed.get("name", "")
                if name and name != "respond":
                    key = (name, json.dumps(parsed.get("arguments", {}), sort_keys=True))
            except (json.JSONDecodeError, AttributeError):
                pass
        if key:
            if key == prev:
                redundant += 1
            prev = key
    return redundant


def count_invalid_calls(
    messages: List[Dict[str, Any]],
    valid_tools: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Count tool calls to non-existent tools and estimate wasted tokens.

    Args:
        messages: OpenAI-format message list.
        valid_tools: Set of valid tool names. If None, all calls are considered valid.
    """
    if valid_tools is None:
        return {"invalid_calls": 0, "invalid_tool_names": [], "wasted_total_tokens": 0}

    valid_tools = valid_tools | {"respond"}
    invalid_calls = 0
    invalid_tool_names: List[str] = []
    wasted_output_tokens = 0
    wasted_input_tokens = 0

    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        tool_name = None
        if msg.get("tool_calls"):
            tool_name = msg["tool_calls"][0]["function"]["name"]
        elif msg.get("content"):
            tool_name = _parse_text_action(msg["content"])

        if tool_name and tool_name not in valid_tools:
            invalid_calls += 1
            invalid_tool_names.append(tool_name)
            usage = msg.get("_usage", {})
            wasted_output_tokens += usage.get("output_tokens", 0)
            for j in range(i + 1, len(messages)):
                if messages[j].get("role") == "assistant":
                    next_usage = messages[j].get("_usage", {})
                    wasted_input_tokens += next_usage.get("input_tokens", 0)
                    wasted_input_tokens += next_usage.get("cache_read_input_tokens", 0)
                    wasted_input_tokens += next_usage.get("cache_write_input_tokens", 0)
                    break

    return {
        "invalid_calls": invalid_calls,
        "invalid_tool_names": invalid_tool_names,
        "wasted_output_tokens": wasted_output_tokens,
        "wasted_input_tokens": wasted_input_tokens,
        "wasted_total_tokens": wasted_output_tokens + wasted_input_tokens,
    }


def compute_cost_metrics(
    messages: List[Dict[str, Any]],
    model: str,
    valid_tools: Optional[Set[str]] = None,
    total_input_tokens: int = 0,
    total_output_tokens: int = 0,
    total_cache_read_input_tokens: int = 0,
    total_cache_write_input_tokens: int = 0,
) -> Dict[str, Any]:
    """Compute full cost metrics for a single task run.

    Args:
        messages: OpenAI-format message trajectory.
        model: Model identifier for pricing lookup.
        valid_tools: Optional set of valid tool names for invalid call detection.
        total_input_tokens: Aggregated input tokens (non-cached).
        total_output_tokens: Aggregated output tokens.
        total_cache_read_input_tokens: Cache read tokens.
        total_cache_write_input_tokens: Cache write tokens.
    """
    tool_names = extract_tool_calls(messages)
    calls_by_tool = Counter(tool_names)

    _has_pricing = has_real_pricing(model)
    if _has_pricing:
        input_price, output_price = get_token_price(model)
        cache_read_price, cache_write_price = get_cache_pricing(model)
        llm_token_cost = (
            total_input_tokens * input_price / 1_000_000
            + total_cache_read_input_tokens * cache_read_price / 1_000_000
            + total_cache_write_input_tokens * cache_write_price / 1_000_000
            + total_output_tokens * output_price / 1_000_000
        )
    else:
        llm_token_cost = None

    total_all_input_tokens = total_input_tokens + total_cache_read_input_tokens + total_cache_write_input_tokens
    cache_hit_rate = (
        total_cache_read_input_tokens / total_all_input_tokens
        if total_all_input_tokens > 0 else 0.0
    )

    invalid_info = count_invalid_calls(messages, valid_tools)

    return {
        "total_tool_calls": len(tool_names),
        "unique_tools_used": len(set(tool_names)),
        "calls_by_tool": dict(calls_by_tool),
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cache_read_input_tokens": total_cache_read_input_tokens,
        "total_cache_write_input_tokens": total_cache_write_input_tokens,
        "total_all_input_tokens": total_all_input_tokens,
        "cache_hit_rate": round(cache_hit_rate, 4),
        "total_llm_token_cost": round(llm_token_cost, 6) if llm_token_cost is not None else None,
        "redundant_calls": count_redundant_calls(messages),
        **invalid_info,
    }
