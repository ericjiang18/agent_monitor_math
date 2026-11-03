"""Plain's one model call on the server-sponsored Kimi route: as long as the
broker may take, but ending before the runner's own limit."""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_monitor import jobs, kimi_k3, public_kimi_server, settings, sponsored_kimi_client
from agent_monitor.runners import api_backend, plain_runner

SPONSORED = {"AGENT_MONITOR_PUBLIC_KIMI": "1"}


def call_timeout(env: dict[str, str], *, elapsed: float = 0.0) -> int | None:
    with patch.dict(os.environ, env, clear=True), \
            patch.object(plain_runner.time, "monotonic", return_value=plain_runner._STARTED + elapsed):
        return plain_runner._call_timeout()


def test_off_the_sponsored_route_plain_keeps_the_backend_default():
    assert call_timeout({"AGENT_MONITOR_CLI_TIMEOUT": "600"}) is None


@pytest.mark.parametrize(("env", "elapsed", "expected"), [
    ({}, 0, kimi_k3.SPONSORED_TIMEOUT_SECONDS),  # no runner limit: the broker's
    ({"AGENT_MONITOR_CLI_TIMEOUT": "600"}, 0, 570),  # the isolated service's
    ({"AGENT_MONITOR_CLI_TIMEOUT": "600"}, 100, 470),  # a second call gets what is left
    ({"AGENT_MONITOR_PLAIN_TIMEOUT": "300", "AGENT_MONITOR_CLI_TIMEOUT": "600"}, 0, 270),  # as jobs reads them
    ({"AGENT_MONITOR_CLI_TIMEOUT": "10"}, 0, 30),  # the watchdog's 60 s floor
    ({"AGENT_MONITOR_CLI_TIMEOUT": "600"}, 590, 1),
    ({"AGENT_MONITOR_CLI_TIMEOUT": "soon"}, 0, kimi_k3.SPONSORED_TIMEOUT_SECONDS),  # jobs ignores it too
    ({"AGENT_MONITOR_CLI_TIMEOUT": "3600"}, 0, kimi_k3.SPONSORED_TIMEOUT_SECONDS),
])
def test_a_sponsored_call_waits_for_the_broker_but_ends_before_the_watchdog(env, elapsed, expected):
    assert call_timeout({**SPONSORED, **env}, elapsed=elapsed) == expected


def test_in_the_isolated_service_the_call_ends_before_its_runner_and_the_console(tmp_path):
    with patch.dict(os.environ, {}, clear=False):
        public_kimi_server._install_worker_environment(
            tmp_path, token="local-secret-value", base_url="http://127.0.0.1:1234/v1")
        env = dict(os.environ)
    call = call_timeout(env)
    watchdog = jobs._cli_timeout_seconds("plain", env)
    poll = inspect.signature(sponsored_kimi_client.run_plain).parameters["timeout"].default
    assert call is not None and api_backend._DEFAULT_TIMEOUT < call
    assert call + plain_runner._WATCHDOG_MARGIN_S <= watchdog < poll


def _kimi_reply(url, *, method, headers, payload, timeout, seen):
    seen.append({"url": url, "headers": dict(headers), "timeout": timeout})
    return 200, {"choices": [{"message": {"role": "assistant", "content": "# Result\n\nA proof."}}],
                 "usage": {"prompt_tokens": 7, "completion_tokens": 8}}


@pytest.mark.parametrize("guest_limit", ["", "1024"])
def test_plain_tells_the_broker_its_bounded_budget(monkeypatch, tmp_path: Path, guest_limit: str):
    seen: list[dict] = []
    monkeypatch.setattr(settings, "_http_json", lambda url, **kw: _kimi_reply(url, seen=seen, **kw))
    env = {
        **SPONSORED, "AGENT_MONITOR_CLI_TIMEOUT": "600", "PLAIN_MODEL": "kimi-k3",
        "KIMI_API_KEY": "loopback-token", "KIMI_API_BASE": "http://127.0.0.1:1234/v1",
        "AGENT_MONITOR_USE_CODEX": "0", "AGENT_MONITOR_USE_CLAUDE": "0",
        **({"AGENT_MONITOR_PLAIN_MAX_OUTPUT_TOKENS": guest_limit} if guest_limit else {}),
    }
    monkeypatch.setattr(plain_runner.time, "monotonic", lambda: plain_runner._STARTED)
    monkeypatch.setattr(sys, "argv", ["plain_runner.py", "Prove it."])
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ, env, clear=True):
        assert plain_runner.main() == 0
    assert [(call["headers"][kimi_k3.TIMEOUT_HEADER], call["timeout"]) for call in seen] == [("570", 570)]
    assert (tmp_path / "proof.md").read_text(encoding="utf-8").startswith("# Result")


def test_a_users_own_kimi_key_keeps_the_default_and_sends_no_budget(monkeypatch, tmp_path: Path):
    seen: list[dict] = []
    monkeypatch.setattr(settings, "_http_json", lambda url, **kw: _kimi_reply(url, seen=seen, **kw))
    env = {"AGENT_MONITOR_CLI_TIMEOUT": "600", "PLAIN_MODEL": "kimi-k3", "KIMI_API_KEY": "own-key",
           "AGENT_MONITOR_USE_CODEX": "0", "AGENT_MONITOR_USE_CLAUDE": "0"}
    monkeypatch.setattr(sys, "argv", ["plain_runner.py", "Prove it."])
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ, env, clear=True):
        assert plain_runner.main() == 0
    assert seen[0]["timeout"] == api_backend._DEFAULT_TIMEOUT
    assert kimi_k3.TIMEOUT_HEADER not in seen[0]["headers"]
