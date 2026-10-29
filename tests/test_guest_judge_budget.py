"""A guest session's judge calls (easy_route) are bounded: its starts, refused
as not mathematics or refused for having no guest runs left, never make more
than its hourly ratings (found in review of PR #37)."""

from __future__ import annotations

import json

from agent_monitor import console_server as console
from agent_monitor import easy_route
from tests.test_console_guest_access import (  # noqa: F401 - pytest fixture
    APP_MOUNT,
    _issue_guest,
    guest_console,
    request,
    running_server,
)

N = 40  # more than the 30 Auto ratings a guest gets per hour


def _probe(monkeypatch, *, not_math: bool, exhausted: bool):
    calls: list[list[str]] = []

    def fake_decide(state, questions):
        calls.append(sorted(questions))
        if not_math:
            return {"answers": {q: {"choice": "not_math", "probabilities": {"not_math": 0.99}} for q in questions}}, "", 0.0
        return None, "judge unavailable (probe)", 0.0

    monkeypatch.setenv(easy_route.KEY_ENV, "probe-not-a-key")
    monkeypatch.delenv(easy_route.SWITCH_ENV, raising=False)
    monkeypatch.setattr(easy_route, "_decide", fake_decide)
    if exhausted:
        monkeypatch.setattr(console.job_manager, "persisted_run_count", lambda _owner: console.guest_runs_per_session())
    started: list[dict] = []
    monkeypatch.setattr(
        console.job_manager, "start_job_routed",
        lambda **kw: started.append(kw) or {"run_id": f"kimi_adhoc_probe{len(started)}", "job_id": f"j{len(started)}"},
    )
    statuses: list[int] = []
    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        for i in range(N):
            status, _, _raw = request(
                port, "POST", f"{APP_MOUNT}/api/runs",
                body={"engine": "kimi", "problem_text": f"Write a poem, number {i}."},
                headers={"Cookie": cookie},
            )
            statuses.append(status)
    return calls, statuses, started


def test_refused_starts(guest_console, monkeypatch):
    calls, statuses, started = _probe(monkeypatch, not_math=True, exhausted=False)
    print(json.dumps({"case": "not-math starts", "judge_calls": len(calls), "statuses": sorted(set(statuses)),
                      "runs_started": len(started), "guest_rating_buckets": len(console._GUEST_RATINGS)}))
    assert len(calls) <= 30, f"{len(calls)} judge calls from one guest session"


def test_exhausted_guest(guest_console, monkeypatch):
    calls, statuses, started = _probe(monkeypatch, not_math=False, exhausted=True)
    print(json.dumps({"case": "guest with no runs left", "judge_calls": len(calls), "statuses": sorted(set(statuses)),
                      "runs_started": len(started)}))
    assert len(calls) <= 30, f"{len(calls)} judge calls from a guest with no runs left"


def test_a_guests_easy_rating_is_taken_outside_the_guest_start_lock(guest_console, monkeypatch):
    held: list[bool] = []

    def fake_judge(text, problem=""):
        # Held by anyone? (an RLock: tried from another thread)
        import threading

        free: list[bool] = []

        def probe():
            got = console._GUEST_RUN_START_LOCK.acquire(blocking=False)
            free.append(got)
            if got:
                console._GUEST_RUN_START_LOCK.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        held.append(not free[0])
        return {"verdict": "easy", "reason": "", "model": "m", "error": ""}

    monkeypatch.setenv(easy_route.KEY_ENV, "probe-not-a-key")
    monkeypatch.delenv(easy_route.SWITCH_ENV, raising=False)
    monkeypatch.setattr(easy_route, "topic_refusal", lambda *a, **k: "")
    monkeypatch.setattr(easy_route, "judge", fake_judge)
    seen: list[dict] = []
    monkeypatch.setattr(console.job_manager, "start_job_routed",
                        lambda **kw: seen.append(kw) or {"run_id": "kimi_adhoc_lock1", "job_id": "j1"})
    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        status, _, _raw = request(port, "POST", f"{APP_MOUNT}/api/runs",
                                  body={"engine": "kimi", "problem_text": "What is 2 + 3?"}, headers={"Cookie": cookie})
    assert status == 200
    assert held == [False]  # rated once, never under the lock
    assert seen and seen[0]["easy_verdict"]["verdict"] == "easy"  # handed to the start, not rated again
