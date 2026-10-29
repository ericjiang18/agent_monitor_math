"""Follow-ups after a turn Plain answered (easy_route).

A turn Plain answered on the included Kimi K3 is continued on Ansatz CGM ·
Kimi K3, since a sponsored Plain run has no follow-ups of its own.  A model
chosen for the next turn must also run on the engine the run started on.
Nothing here starts a harness or contacts a provider: each follow-up's worker
is recorded, never run.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_monitor import easy_route, jobs, sponsored_kimi_client

from test_console_guest_access import APP_MOUNT, request, running_server
from test_member_accounts import (  # noqa: F401  (fixture imports)
    _member,
    guest_console,
    member_console,
)

KEY = "sk-or-v1-" + "0" * 64  # a placeholder, never a real key
ADMIN = {"id": 1, "is_admin": True, "email": "admin@example.test"}
PROBLEM = "Prove that the sum of two even integers is even."
OWN_KEYS = {"KIMI_API_KEY": "test-kimi-key", "ANTHROPIC_API_KEY": "test-anthropic-key",
            "OPENAI_API_KEY": "test-openai-key"}


@pytest.fixture()
def easy_run(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv(easy_route.KEY_ENV, KEY)
    monkeypatch.delenv(easy_route.SWITCH_ENV, raising=False)
    for name in ("KIMI_API_KEY", "KIMI_API_BASE", "KIMI_BASE_URL"):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(sponsored_kimi_client, "configured", lambda *a, **k: True)
    monkeypatch.setattr(sponsored_kimi_client, "supports_engine", lambda *a, **k: True)
    monkeypatch.setattr(sponsored_kimi_client, "endpoint", lambda *a, **k: "http://127.0.0.1:9/v1")
    issued: list[dict] = []

    def issue_credentials(**kwargs):  # the sponsored service's short-lived loopback credential
        issued.append(kwargs)
        return {"KIMI_API_KEY": "loopback-test-credential", "KIMI_BASE_URL": "http://127.0.0.1:9/v1"}

    monkeypatch.setattr(sponsored_kimi_client, "issue_credentials", issue_credentials)
    launches: list[dict] = []

    class Thread:  # each follow-up's worker: recorded, never run
        def __init__(self, *args, target=None, kwargs=None, **_ignored):
            self.launch = {"target": getattr(target, "__name__", ""), **(kwargs or {})}

        def start(self):
            launches.append(self.launch)

        def is_alive(self):
            return False

        def join(self, *args, **kwargs):
            return None

    monkeypatch.setattr(jobs.threading, "Thread", Thread)
    chat: list[dict] = []

    def append_chat(run_id, role, text, **kwargs):
        entry = {"role": role, "content": text, "attachments": kwargs.get("attachments") or []}
        chat.append(entry)
        return entry

    monkeypatch.setattr(jobs, "_append_chat", append_chat)
    judge = MagicMock(return_value={"verdict": "hard", "reason": "", "model": "m", "error": ""})
    monkeypatch.setattr(easy_route, "judge", judge)
    jobs_before = set(jobs._JOBS)
    made: list[str] = []

    def make(run_id, *, home="kimi", sponsored=True, model="kimi-k3"):
        ws = jobs.RUNS_DIR / "workspaces" / run_id
        ws.mkdir(parents=True)
        (ws / "proof.md").write_text("# Answer\n\nWith a = 2m and b = 2n, a + b = 2(m + n).\n", encoding="utf-8")
        record = {"run_id": run_id, "engine": "plain", "status": "finished", "auth_route": "api_key",
                  "model": model, "requested_model": model, "owner_id": 1, "problem_text": PROBLEM,
                  "problems": [{"text": PROBLEM, "source": "initial"}], "agents": [], "edges": []}
        if sponsored:
            record["credential_source"] = "sponsored_kimi"
        jobs._write_run(record)
        if home:
            easy_route.note_turn(ws, home=home, engine="plain", verdict={"verdict": "easy", "model": "m"}, why="judged")
        made.append(run_id)
        return ws

    yield SimpleNamespace(make=make, launches=launches, chat=chat, judge=judge, issued=issued)
    # The follow-ups registered jobs whose workers never ran: no later test may see them.
    for key in set(jobs._JOBS) - jobs_before:
        jobs._JOBS.pop(key, None)
    for run_id in made:
        jobs._release_launch(run_id)


def _launched(easy_run):
    return [(launch.get("engine"), launch.get("model")) for launch in easy_run.launches]


# --- A turn Plain answered on the included Kimi K3 --------------------------


def test_a_message_after_a_sponsored_plain_turn_runs_on_kimi(easy_run):
    ws = easy_run.make("plain_adhoc_home_message")
    result = jobs.send_human_message("plain_adhoc_home_message", "Now show it for odd integers.", user=ADMIN)
    assert (result["ok"], result["status"]) == (True, "continuing")
    assert _launched(easy_run) == [("kimi", "kimi-k3")]
    # On the sponsored service still: its credential, for Ansatz CGM · Kimi K3's turn.
    assert easy_run.issued and easy_run.issued[-1]["engine"] == jobs._sponsored_transport_engine("kimi")
    easy_run.judge.assert_not_called()  # Plain cannot go on here: nothing to rate
    assert jobs._load_run_record("plain_adhoc_home_message")["engine"] == "kimi"
    last = easy_route.load(ws)["turns"][-1]
    assert (last["engine"], last["why"]) == ("kimi", "sponsored")
    assert [m["content"] for m in easy_run.chat if m["role"] == "system"][0] == "This turn runs on Ansatz CGM again."


def test_continue_and_files_after_a_sponsored_plain_turn_run_on_kimi(easy_run):
    easy_run.make("plain_adhoc_home_continue")
    jobs.continue_run("plain_adhoc_home_continue", user=ADMIN)
    assert _launched(easy_run) == [("kimi", "kimi-k3")]
    ws = easy_run.make("plain_adhoc_home_files")
    data = jobs._easy_route_follow_up("plain_adhoc_home_files", jobs._load_run_record("plain_adhoc_home_files"), ws,
                                      message="See the attached notes.", user=ADMIN, model=None, has_files=True)
    assert data["engine"] == "kimi"
    easy_run.judge.assert_not_called()


def test_with_the_judge_off_a_sponsored_plain_turn_still_goes_back(easy_run, monkeypatch):
    monkeypatch.setenv(easy_route.SWITCH_ENV, "off")
    easy_run.make("plain_adhoc_home_off")
    jobs.send_human_message("plain_adhoc_home_off", "Go on.", user=ADMIN)
    assert _launched(easy_run) == [("kimi", "kimi-k3")]


def test_a_plain_run_started_as_plain_on_the_included_kimi_is_still_not_continued(easy_run):
    easy_run.make("plain_adhoc_started_plain", home="")
    with pytest.raises(ValueError, match="can't be continued"):
        jobs.send_human_message("plain_adhoc_started_plain", "Go on.", user=ADMIN)
    with pytest.raises(ValueError, match="can't be continued"):
        jobs.continue_run("plain_adhoc_started_plain", user=ADMIN)
    assert (easy_run.chat, easy_run.launches) == ([], [])  # refused before anything is recorded


# --- A model chosen for the turn after one Plain answered -------------------


@pytest.mark.parametrize("verdict, engine", [("hard", "kimi"), ("easy", "plain")])
def test_a_model_the_starting_engine_cannot_run_gives_way_to_the_runs_own(easy_run, monkeypatch, verdict, engine):
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: dict(OWN_KEYS))
    easy_run.judge.return_value = {"verdict": verdict, "reason": "", "model": "m", "error": ""}
    run_id = f"plain_adhoc_own_{verdict}"
    easy_run.make(run_id, sponsored=False)
    jobs.send_human_message(run_id, "Now the odd case.", model="claude-opus-5-5", user=ADMIN)
    # Ansatz CGM · Kimi K3 runs Kimi K3 alone, so the turn keeps the run's model on either engine.
    assert _launched(easy_run) == [(engine, "kimi-k3")]


def test_a_model_both_engines_run_is_kept(easy_run, monkeypatch):
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: dict(OWN_KEYS))
    easy_run.make("plain_adhoc_own_cgm", home="math_harness", sponsored=False, model="claude-opus-5-5")
    jobs.send_human_message("plain_adhoc_own_cgm", "Keep going.", model="gpt-6.1-sol", user=ADMIN)
    assert _launched(easy_run) == [("math_harness", "gpt-6.1-sol")]


# --- The run's GET says where the run started --------------------------------


def test_the_run_carries_the_engine_it_started_on(member_console):
    with running_server() as port:
        user, cookie = _member(port)
        for run_id, home in (("plain_adhoc_get_home", "kimi"), ("plain_adhoc_get_plain", "")):
            ws = jobs.workspace_dir(run_id)
            jobs._write_run({"run_id": run_id, "engine": "plain", "status": "finished", "owner_id": user["id"]})
            if home:
                easy_route.note_turn(ws, home=home, engine="plain", verdict=None, why="judged")
        got = {}
        for run_id in ("plain_adhoc_get_home", "plain_adhoc_get_plain"):
            status, _headers, raw = request(port, "GET", f"{APP_MOUNT}/api/run/{run_id}", headers={"Cookie": cookie})
            assert status == 200, raw
            got[run_id] = json.loads(raw)
    assert got["plain_adhoc_get_home"]["easy_route_home"] == "kimi"
    assert "easy_route_home" not in got["plain_adhoc_get_plain"]
