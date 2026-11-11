"""The LiteLLM proxy logger of llm_cost_tracker records every call with its cost.

``_compute_cost`` had lost its ``def`` line: its body sat unreachable under the
``return`` of the function above, the name was undefined, and ``_write_row``
failed with ``NameError`` for every call.  ``_record`` catches the failure, so
the proxy kept working and the trace file simply stayed empty.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture()
def trace_logger(monkeypatch, tmp_path):
    # litellm is not a dependency of the console; the logger needs only the
    # base class its proxy gives to callbacks.
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = type("CustomLogger", (), {"__init__": lambda self: None})
    for name, module in (
        ("litellm", types.ModuleType("litellm")),
        ("litellm.integrations", types.ModuleType("litellm.integrations")),
        ("litellm.integrations.custom_logger", custom_logger),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "llm_cost_tracker.proxy.trace_logger", raising=False)
    monkeypatch.setenv("LLM_MONITOR_LOG", str(tmp_path / "monitor" / "calls.jsonl"))
    module = importlib.import_module("llm_cost_tracker.proxy.trace_logger")
    yield module
    sys.modules.pop("llm_cost_tracker.proxy.trace_logger", None)


def _usage(**tokens):
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, **tokens}


def test_a_call_is_priced_from_the_price_table(trace_logger):
    # tests/conftest.py: gpt-6-sol costs 1.5 (input), 7.5 (output) and 0.15
    # (cache read) per million tokens, and has no cache-write price of its own.
    million = 1_000_000
    assert trace_logger._compute_cost(_usage(input_tokens=million), "gpt-6-sol") == 1.5
    assert trace_logger._compute_cost(_usage(output_tokens=million), "gpt-6-sol") == 7.5
    assert trace_logger._compute_cost(_usage(cache_read_tokens=million), "gpt-6-sol") == 0.15
    assert trace_logger._compute_cost(_usage(cache_write_tokens=million), "gpt-6-sol") == 1.5
    assert trace_logger._compute_cost(
        _usage(input_tokens=2000, output_tokens=1000, cache_read_tokens=10_000), "gpt-6-sol"
    ) == round(2000 * 1.5 / million + 1000 * 7.5 / million + 10_000 * 0.15 / million, 6)


def test_a_model_without_a_price_has_no_cost(trace_logger):
    assert trace_logger._compute_cost(_usage(input_tokens=1000, output_tokens=1000), "self-hosted/no-such-model") is None


def test_a_recorded_call_is_written_with_its_cost(trace_logger, tmp_path, capsys):
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=2000, completion_tokens=1000),
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="42", tool_calls=None))],
    )
    kwargs = {
        "model": "gpt-6-sol",
        "messages": [{"role": "user", "content": "What is six times seven?"}],
        "litellm_params": {"metadata": {"headers": {"x-trace-id": "probe::1", "x-caller": "worker"}}},
    }
    started = datetime(2026, 11, 1, tzinfo=timezone.utc)

    trace_logger.proxy_logger._record(kwargs, response, started, started + timedelta(seconds=2))

    assert "failed to log call" not in capsys.readouterr().out
    rows = [json.loads(line) for line in (tmp_path / "monitor" / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert (row["model"], row["input_tokens"], row["output_tokens"]) == ("gpt-6-sol", 2000, 1000)
    assert row["cost_usd"] == round(2000 * 1.5 / 1e6 + 1000 * 7.5 / 1e6, 6)
    assert row["stop_reason"] == "end_turn"
    assert row["response_message"]["content"] == "42"
    assert row["latency_s"] == 2.0
