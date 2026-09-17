"""Admin usage ledger: no double counting, deletion-safe history, admin-only API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from agent_monitor import auth, jobs, usage_ledger
from agent_monitor import console_server as console
from tests.test_console_guest_access import request, running_server

APP_MOUNT = "/proof-deadbeef"


@pytest.fixture()
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(usage_ledger, "DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage_ledger, "_INITIALIZED", set())
    monkeypatch.setattr(usage_ledger, "_OWNER_CACHE", {})
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    data_key = tmp_path / "data.key"
    data_key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("AGENT_MONITOR_DATA_KEY_FILE", str(data_key))
    monkeypatch.setenv("AGENT_MONITOR_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    yield tmp_path
    auth._INITIALIZED = False


def _run(run_id: str, *, owner_id=1, status="running", day="2026-09-01", **totals):
    return {
        "run_id": run_id,
        "owner_id": owner_id,
        "engine": "codex",
        "model": "gpt-5",
        "auth_route": "api_key",
        "status": status,
        "problem_text_preview": "Prove sqrt 2 is irrational",
        "created_at": f"{day}T10:00:00Z",
        "updated_at": f"{day}T10:05:00Z",
        "totals": totals,
    }


def test_continuation_heartbeats_do_not_double_count(ledger):
    usage_ledger.record_run(_run("r1", input_tokens=100, output_tokens=50, cost_usd=0.5))
    usage_ledger.record_run(_run("r1", status="finished", input_tokens=100, output_tokens=50, cost_usd=0.5))
    # A continuation's live flush briefly carries only the new job's totals...
    usage_ledger.record_run(_run("r1", input_tokens=30, output_tokens=10, cost_usd=0.1))
    # ...then the merged record is cumulative.
    usage_ledger.record_run(_run("r1", status="finished", input_tokens=130, output_tokens=60, cost_usd=0.6))

    totals = usage_ledger.usage_summary({})["totals"]
    assert totals["input_tokens"] == 130
    assert totals["output_tokens"] == 60
    assert totals["cost_usd"] == pytest.approx(0.6)
    assert totals["runs"] == 1


def test_deleted_run_keeps_usage_but_leaves_run_list(ledger):
    usage_ledger.record_run(_run("gone", status="failed", input_tokens=10, output_tokens=5, cost_usd=0.2))
    usage_ledger.mark_deleted("gone")
    # A late flush from the stopping job must not resurrect the run.
    usage_ledger.record_run(_run("gone", status="failed", input_tokens=10, output_tokens=5, cost_usd=0.2))

    summary = usage_ledger.usage_summary({})
    assert summary["totals"]["cost_usd"] == pytest.approx(0.2)
    assert summary["totals"]["failed"] == 1
    assert usage_ledger.list_runs({})["total"] == 0
    listed = usage_ledger.list_runs({"include_deleted": "1"})["runs"]
    assert [r["run_id"] for r in listed] == ["gone"]
    assert listed[0]["deleted"] is True


def test_out_of_run_calls_are_attributed_to_their_stage(ledger):
    usage_ledger.record_run(_run("r2", status="finished", input_tokens=100, output_tokens=10, cost_usd=1.0))

    @usage_ledger.attributed("dag")
    def generate(*, workspace, run_record):
        usage_ledger.record_usage(
            model="gpt-5",
            usage=usage_ledger.http_usage("openai", {"usage": {"input_tokens": 40, "output_tokens": 4}}),
            cost_usd=0.25,
        )

    generate(workspace=Path("/tmp/workspaces/r2"), run_record=None)
    # Outside an attributed entry point nothing is booked.
    usage_ledger.record_usage(model="gpt-5", usage={"input_tokens": 999.0}, cost_usd=9.0)

    summary = usage_ledger.usage_summary({})
    stages = {row["key"]: row for row in summary["by_stage"]}
    assert stages["engine"]["input_tokens"] == 100
    assert stages["dag"]["input_tokens"] == 40
    assert stages["dag"]["calls"] == 1
    assert summary["totals"]["cost_usd"] == pytest.approx(1.25)
    run = usage_ledger.list_runs({})["runs"][0]
    assert run["extra_stages"] == ["dag"]
    assert run["cost_usd"] == pytest.approx(1.25)


def test_filters_by_user_day_billing_and_guests(ledger):
    usage_ledger.record_run(_run("a", owner_id=1, day="2026-09-01", input_tokens=10, cost_usd=1.0))
    guest = _run("b", owner_id=2, day="2026-09-03", input_tokens=20, cost_usd=2.0)
    guest.update(guest=True, credential_source="sponsored_kimi")
    usage_ledger.record_run(guest)

    assert usage_ledger.usage_summary({"guests": "exclude"})["totals"]["cost_usd"] == pytest.approx(1.0)
    assert usage_ledger.usage_summary({"billing": "sponsored"})["totals"]["cost_usd"] == pytest.approx(2.0)
    assert usage_ledger.list_runs({"user": "2"})["total"] == 1
    assert usage_ledger.list_runs({"q": "sqrt", "from": "2026-09-02"})["total"] == 1


def test_backfill_uses_lineage_days_and_is_idempotent(ledger):
    cache = ledger / "harness"
    cache.mkdir()
    record = _run("old", status="finished", day="2026-08-01", input_tokens=300, cost_usd=3.0)
    record["session_lineage"] = [
        {"kind": "initial", "updated_at": "2026-08-01T00:00:00Z", "totals": {"input_tokens": 100, "cost_usd": 1.0}},
        {"kind": "continue", "updated_at": "2026-08-05T00:00:00Z", "totals": {"input_tokens": 200, "cost_usd": 2.0}},
    ]
    (cache / "old.json").write_text(json.dumps(record))

    assert usage_ledger.backfill(cache)["imported"] == 1
    assert usage_ledger.backfill(cache, force=True)["imported"] == 0
    by_day = {row["key"]: row for row in usage_ledger.usage_summary({})["by_day"]}
    assert by_day["2026-08-01"]["cost_usd"] == pytest.approx(1.0)
    assert by_day["2026-08-05"]["cost_usd"] == pytest.approx(2.0)


def test_write_run_feeds_the_ledger(ledger, monkeypatch):
    monkeypatch.setattr(jobs, "CACHE_DIR", ledger / "cache")
    monkeypatch.setattr(jobs, "RUNS_DIR", ledger / "runs")
    jobs._write_run(_run("w1", status="finished", input_tokens=7, output_tokens=3))
    jobs.delete_run("w1")
    assert usage_ledger.usage_summary({})["totals"]["input_tokens"] == 7
    assert usage_ledger.list_runs({"include_deleted": "1"})["runs"][0]["deleted"] is True


def test_admin_endpoints_require_a_real_admin(ledger, monkeypatch):
    monkeypatch.setattr(console, "BASE_PATH", APP_MOUNT)
    monkeypatch.setattr(console, "PUBLIC_MODE", False, raising=False)
    admin = auth.register("admin@example.com", "correct horse battery")
    member = auth.register("member@example.com", "correct horse battery")
    assert admin["is_admin"] and not member["is_admin"]
    usage_ledger.record_run(_run("m1", owner_id=member["id"], status="finished", input_tokens=5, cost_usd=0.1))
    guest, guest_token = auth.create_guest_session()

    def get(path, token=None):
        headers = {"Cookie": f"{auth.SESSION_COOKIE}={token}"} if token else {}
        return request(port, "GET", f"{APP_MOUNT}{path}", headers=headers)

    with running_server() as port:
        for path in ("/api/admin/usage", "/api/admin/runs"):
            assert get(path, auth.create_session(member["id"]))[0] == 403
            assert get(path, guest_token)[0] == 403
            assert get(path)[0] in {401, 302, 303}
        status, _, raw = get("/api/admin/usage?guests=exclude", auth.create_session(admin["id"]))
        assert status == 200, raw
        payload = json.loads(raw)
        assert payload["totals"]["cost_usd"] == pytest.approx(0.1)
        assert payload["by_user"][0]["email"] == "member@example.com"
        status, _, raw = get("/api/admin/runs?q=sqrt", auth.create_session(admin["id"]))
        assert status == 200
        assert json.loads(raw)["runs"][0]["owner_email"] == "member@example.com"
        assert "user_env" not in raw
