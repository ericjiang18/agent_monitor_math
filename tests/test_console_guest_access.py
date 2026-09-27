"""Contract tests for explicit, isolated guest access to the console.

The HTTP tests use a temporary users database and replace job creation with a
small recorder.  They must never start a real harness or contact a provider.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from agent_monitor import auth
from agent_monitor import console_server as console
from agent_monitor import jobs
from agent_monitor import library
from agent_monitor import settings


APP_MOUNT = "/proof-deadbeef"


@contextmanager
def running_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(
    port: int,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = None if body is None else json.dumps(body)
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    data = response.read().decode("utf-8")
    result = (
        response.status,
        {key.lower(): value for key, value in response.getheaders()},
        data,
    )
    connection.close()
    return result


def _cookie_from(headers: dict[str, str]) -> tuple[str, str]:
    raw = headers["set-cookie"]
    jar = SimpleCookie()
    jar.load(raw)
    token = jar[auth.SESSION_COOKIE].value
    return f"{auth.SESSION_COOKIE}={token}", token


def _issue_guest(port: int) -> tuple[dict, str, str]:
    status, headers, raw = request(
        port,
        "POST",
        f"{APP_MOUNT}/api/auth/guest",
        body={},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert status == 200, raw
    payload = json.loads(raw)
    cookie, token = _cookie_from(headers)
    return payload["user"], cookie, token


@pytest.fixture()
def guest_console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_key = tmp_path / "data.key"
    data_key.write_bytes(Fernet.generate_key())
    env_file = tmp_path / "server.env"

    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    monkeypatch.setattr(console, "BASE_PATH", APP_MOUNT)
    # The old implicit public-mode behavior must not interfere with the new,
    # explicit endpoint while old worktree changes are being reconciled.
    monkeypatch.setattr(console, "PUBLIC_MODE", False, raising=False)
    monkeypatch.setattr(console.job_manager, "persisted_run_count", lambda _owner: 0)
    monkeypatch.setattr(settings, "ENV_PATH", env_file)
    monkeypatch.setattr(
        settings,
        "SYSTEM_SETTINGS_PATH",
        tmp_path / "system_settings.json",
        raising=False,
    )

    monkeypatch.setenv("AGENT_MONITOR_DATA_KEY_FILE", str(data_key))
    monkeypatch.setenv("AGENT_MONITOR_REQUIRE_DATA_KEY", "1")
    monkeypatch.setenv("AGENT_MONITOR_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "")
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", "")
    monkeypatch.delenv("AGENT_MONITOR_GUEST_ACCESS", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_GUEST_ENGINES", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_GUEST_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_GUEST_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_GUEST_RUNS_PER_SESSION", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_GUEST_RUNS_PER_HOUR", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_GUEST_GLOBAL_RUNS_PER_HOUR", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_GUEST_VERIFICATIONS_PER_HOUR", raising=False)
    monkeypatch.delenv(
        "AGENT_MONITOR_GUEST_GLOBAL_VERIFICATIONS_PER_HOUR", raising=False
    )
    monkeypatch.delenv(
        "AGENT_MONITOR_GUEST_VERIFY_GLOBAL_CONCURRENCY", raising=False
    )
    monkeypatch.delenv("AGENT_MONITOR_REGISTRATION_OPEN", raising=False)
    monkeypatch.delenv("PLAIN_CMD", raising=False)
    console._GUEST_SESSION_WINDOW.clear()
    console._GUEST_RUN_WINDOWS.clear()
    console._GUEST_GLOBAL_RUN_WINDOW.clear()
    console._GUEST_VERIFY_WINDOWS.clear()
    console._GUEST_VERIFY_GLOBAL_WINDOW.clear()
    console._GUEST_VERIFY_ACTIVE_USERS.clear()

    yield {"db": auth.DB_PATH, "env": env_file}
    console._GUEST_SESSION_WINDOW.clear()
    console._GUEST_RUN_WINDOWS.clear()
    console._GUEST_GLOBAL_RUN_WINDOW.clear()
    console._GUEST_VERIFY_WINDOWS.clear()
    console._GUEST_VERIFY_GLOBAL_WINDOW.clear()
    console._GUEST_VERIFY_ACTIVE_USERS.clear()
    auth._INITIALIZED = False


def _database_counts(db_path: Path) -> tuple[int, int]:
    # A side-effect-free identity probe is allowed to leave the database
    # entirely absent, which is even stronger than creating two empty tables.
    if not db_path.exists():
        return 0, 0
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        return (
            conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if "users" in tables
            else 0,
            conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            if "sessions" in tables
            else 0,
        )


def test_me_is_side_effect_free_and_guest_access_is_enabled_by_default(
    guest_console,
):
    with running_server() as port:
        status, headers, raw = request(port, "GET", f"{APP_MOUNT}/api/auth/me")

    assert status == 200
    assert "no-store" in headers.get("cache-control", "")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "SAMEORIGIN"
    payload = json.loads(raw)
    assert payload["user"] is None
    assert payload["guest_access"] is True
    assert payload["registration_open"] is False
    assert _database_counts(guest_console["db"]) == (0, 0)


def test_guest_endpoint_is_mount_aware_and_issues_a_hardened_session(
    guest_console,
):
    with running_server() as port:
        bare_status, _, _ = request(port, "POST", "/api/auth/guest", body={})
        status, headers, raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/auth/guest",
            body={},
            headers={"X-Forwarded-Proto": "https"},
        )

    assert bare_status == 404
    assert status == 200
    assert "no-store" in headers.get("cache-control", "")
    cookie_header = headers["set-cookie"]
    assert f"Path={APP_MOUNT}" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=Lax" in cookie_header
    assert "Secure" in cookie_header
    assert "Max-Age=86400" in cookie_header

    payload = json.loads(raw)
    user = payload["user"]
    assert payload["ok"] is True
    assert user["guest"] is True
    assert user["is_admin"] is False

    _, token = _cookie_from(headers)
    with sqlite3.connect(guest_console["db"]) as conn:
        conn.row_factory = sqlite3.Row
        stored_user = conn.execute(
            "SELECT id, is_guest, is_admin FROM users WHERE id=?", (user["id"],)
        ).fetchone()
        stored_session = conn.execute(
            "SELECT token_hash, user_id FROM sessions"
        ).fetchone()
    assert dict(stored_user) == {
        "id": user["id"],
        "is_guest": 1,
        "is_admin": 0,
    }
    assert stored_session["user_id"] == user["id"]
    assert stored_session["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in stored_session["token_hash"]


def test_create_guest_session_rolls_back_user_if_session_creation_fails(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    # Initialize/migrate the schema before installing the failure point.
    with auth._conn():
        pass

    def fail_session(*_args, **_kwargs):
        raise RuntimeError("synthetic session failure")

    monkeypatch.setattr(auth, "_insert_session", fail_session)
    with pytest.raises(RuntimeError, match="synthetic session failure"):
        auth.create_guest_session()

    assert _database_counts(guest_console["db"]) == (0, 0)


def test_guest_endpoint_reuses_the_current_guest(guest_console):
    with running_server() as port:
        first_user, cookie, _ = _issue_guest(port)
        status, _, raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/auth/guest",
            body={},
            headers={"Cookie": cookie},
        )

    assert status == 200
    assert json.loads(raw)["user"]["id"] == first_user["id"]
    assert _database_counts(guest_console["db"]) == (1, 1)


def test_guest_endpoint_refuses_to_replace_a_registered_session(guest_console):
    registered = auth.register("registered@example.test", "long-enough-password")
    token = auth.create_session(registered["id"])
    with running_server() as port:
        status, _, raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/auth/guest",
            body={},
            headers={"Cookie": f"{auth.SESSION_COOKIE}={token}"},
        )

    assert status == 409
    assert json.loads(raw)["error"]
    assert auth.user_for_token(token)["id"] == registered["id"]
    assert _database_counts(guest_console["db"]) == (1, 1)


def test_http_registration_requires_an_explicit_operator_switch(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    with running_server() as port:
        first_status, _, first_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/auth/register",
            body={
                "email": "first@example.test",
                "password": "long-enough-password",
            },
        )
        monkeypatch.delenv("AGENT_MONITOR_REGISTRATION_OPEN")
        second_status, _, second_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/auth/register",
            body={
                "email": "second@example.test",
                "password": "long-enough-password",
            },
        )
        me_status, _, me_raw = request(port, "GET", f"{APP_MOUNT}/api/auth/me")

        monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
        opened_status, _, opened_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/auth/register",
            body={
                "email": "opened@example.test",
                "password": "long-enough-password",
            },
        )

    assert first_status == 200, first_raw
    assert json.loads(first_raw)["user"]["is_admin"] is True
    assert second_status == 403, second_raw
    assert me_status == 200
    assert json.loads(me_raw)["registration_open"] is False
    assert opened_status == 200, opened_raw
    assert json.loads(opened_raw)["user"]["is_admin"] is False


def test_closed_registration_rejects_before_password_hashing(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        auth,
        "_hash_password",
        lambda *_args, **_kwargs: pytest.fail("closed registration ran scrypt"),
    )
    with pytest.raises(PermissionError, match="closed"):
        auth.register(
            "visitor@example.test",
            "long-enough-password",
            require_open=True,
        )


def test_disabled_guest_access_rejects_issuance_and_existing_guest_token(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        monkeypatch.setenv("AGENT_MONITOR_GUEST_ACCESS", "0")

        me_status, _, me_raw = request(
            port,
            "GET",
            f"{APP_MOUNT}/api/auth/me",
            headers={"Cookie": cookie},
        )
        protected_status, _, _ = request(
            port,
            "GET",
            f"{APP_MOUNT}/api/jobs",
            headers={"Cookie": cookie},
        )
        issue_status, _, _ = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/auth/guest",
            body={},
        )

    assert me_status == 200
    me = json.loads(me_raw)
    assert me["guest_access"] is False
    assert me["user"] is None
    assert protected_status == 401
    assert issue_status in {403, 404}
    assert _database_counts(guest_console["db"])[0] == 1


def test_first_guest_does_not_consume_the_first_admin_slot(guest_console):
    guest, _ = auth.create_guest_session()
    first_account = auth.register("first@example.test", "long-enough-password")
    second_account = auth.register("second@example.test", "long-enough-password")

    assert guest["guest"] is True
    assert guest["is_admin"] is False
    assert first_account["is_admin"] is True
    assert second_account["is_admin"] is False


def test_guest_role_overrides_a_stale_admin_bit(guest_console):
    guest, token = auth.create_guest_session()
    with sqlite3.connect(guest_console["db"]) as conn:
        conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (guest["id"],))

    recovered = auth.user_for_token(token)
    assert recovered is not None
    assert recovered["guest"] is True
    assert recovered["is_admin"] is False


@pytest.mark.parametrize(
    "email",
    [
        "guest+0123456789abcdef@public.local",
        "ordinary@public.local",
        "UPPER@PUBLIC.LOCAL",
    ],
)
def test_public_local_identities_cannot_self_register(guest_console, email: str):
    with pytest.raises(ValueError, match="reserved|email|guest"):
        auth.register(email, "long-enough-password")


def test_legacy_guest_identity_is_backfilled_to_explicit_role(guest_console):
    with sqlite3.connect(guest_console["db"]) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                password_hash BLOB,
                salt BLOB,
                google_sub TEXT UNIQUE,
                picture TEXT,
                is_admin INTEGER DEFAULT 0,
                created_at TEXT
            );
            INSERT INTO users (
                email, name, password_hash, salt, is_admin, created_at
            ) VALUES (
                'guest+0123456789abcdef@public.local', 'Guest', NULL, NULL, 1,
                '2026-01-01T00:00:00+00:00'
            );
            """
        )

    auth._INITIALIZED = False
    with auth._conn() as conn:
        row = conn.execute(
            "SELECT is_guest, is_admin FROM users WHERE email=?",
            ("guest+0123456789abcdef@public.local",),
        ).fetchone()
    assert row["is_guest"] == 1
    assert row["is_admin"] == 0


def test_guest_does_not_inherit_server_env_but_admin_does(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    guest_console["env"].write_text(
        "OPENAI_API_KEY=server-file-secret\nAGENT_MONITOR_MODEL=gpt-server\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-server-secret")

    guest, _ = auth.create_guest_session()
    admin = auth.register("admin@example.test", "long-enough-password")
    regular = auth.register("regular@example.test", "long-enough-password")

    guest_env = settings.resolved_user_env(guest)
    admin_env = settings.resolved_user_env(admin)
    regular_env = settings.resolved_user_env(regular)
    stale_guest_env = settings.resolved_user_env(
        {**guest, "is_admin": True}
    )
    assert "OPENAI_API_KEY" not in guest_env
    assert "AGENT_MONITOR_MODEL" not in guest_env
    assert "OPENAI_API_KEY" not in regular_env
    assert "AGENT_MONITOR_MODEL" not in regular_env
    assert "OPENAI_API_KEY" not in stale_guest_env
    assert "AGENT_MONITOR_MODEL" not in stale_guest_env
    assert admin_env["OPENAI_API_KEY"] == "server-file-secret"
    assert admin_env["AGENT_MONITOR_MODEL"] == "gpt-server"


def test_guest_cannot_access_shared_profile_library_or_codex_routes(guest_console):
    checks = [
        ("GET", "/api/agent/config", None),
        ("POST", "/api/agent/config", {"system_prompt": "do not write this"}),
        ("POST", "/api/agent/skills", {"name": "guest-skill", "content": "x"}),
        ("POST", "/api/agent/memory", {"name": "MEMORY.md", "content": "x"}),
        ("DELETE", "/api/agent/skills/guest-skill", None),
        ("DELETE", "/api/agent/memory/MEMORY.md", None),
        ("GET", "/api/library", None),
        ("POST", "/api/library", {"type": "memory", "name": "x", "content": "x"}),
        ("POST", "/api/library/settings", {"auto_memory": True}),
        ("DELETE", "/api/library/not-present", None),
        ("GET", "/api/settings/codex/status", None),
        ("GET", "/api/lean/status", None),
        ("GET", "/api/settings/codex/login/poll", None),
        ("POST", "/api/settings/codex/login/start", {}),
        ("POST", "/api/settings/codex/login/cancel", {}),
        ("POST", "/api/settings/codex/logout", {}),
    ]

    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        for method, path, body in checks:
            status, _, raw = request(
                port,
                method,
                APP_MOUNT + path,
                body=body,
                headers={"Cookie": cookie},
            )
            assert status == 403, f"{method} {path}: {status} {raw}"


def test_guest_engine_policy_defaults_and_environment_overrides(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    assert set(console.guest_engine_allowlist()) == {"plain"}
    assert console.guest_max_iterations() == 8

    monkeypatch.setenv("AGENT_MONITOR_GUEST_ENGINES", " plain, improof,ucla ")
    monkeypatch.setenv("AGENT_MONITOR_GUEST_MAX_ITERATIONS", "3")
    assert set(console.guest_engine_allowlist()) == {"plain"}
    assert console.guest_max_iterations() == 3

    monkeypatch.setenv("PLAIN_CMD", "/usr/bin/false")
    assert set(console.guest_engine_allowlist()) == set()
    assert console.guest_access_enabled() is False
    with pytest.raises(ValueError, match="PLAIN_CMD"):
        jobs.start_job(
            engine="plain",
            problem_text="This must never launch the override.",
            user={"id": 7, "email": "guest+abc@public.local", "guest": True},
        )


@pytest.mark.parametrize(
    "problem_id",
    ["/etc/passwd", "../secret", "nested/problem", "..", "bad id"],
)
def test_problem_ids_cannot_escape_run_or_problem_paths(problem_id: str):
    with pytest.raises(ValueError, match="problem_id"):
        jobs.resolve_problem(problem_id, "Prove something.")


def test_manifest_statement_path_cannot_escape_problem_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    problem_root = tmp_path / "problems"
    problem_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private text", encoding="utf-8")
    (problem_root / "manifest.json").write_text(
        json.dumps(
            {
                "problems": [
                    {"problem_id": "safe-id", "statement_path": "../outside.txt"}
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jobs, "PROBLEMS_DIR", problem_root)

    with pytest.raises(ValueError, match="leaves"):
        jobs.resolve_problem("safe-id", None)


def test_guest_completion_never_records_shared_memory(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict] = []
    monkeypatch.setattr(library, "record_run_memory", lambda **kwargs: calls.append(kwargs))
    kwargs = {
        "run_id": "plain_test",
        "engine": "plain",
        "problem_id": "test",
        "status": "finished",
        "problem_text": "problem",
        "outcome": "finished",
        "final_out": "proof",
    }

    jobs._record_run_memory(guest=True, **kwargs)
    assert calls == []
    jobs._record_run_memory(guest=False, **kwargs)
    assert len(calls) == 1


def test_guest_provenance_survives_runner_flushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path / "runs")
    run_id = "plain_adhoc_test_guest"
    jobs._write_run(
        {
            "run_id": run_id,
            "engine": "plain",
            "status": "running",
            "owner_id": 17,
            "owner": "guest+0123456789abcdef@public.local",
            "guest": True,
            "agents": [],
            "totals": {},
        }
    )
    jobs._write_run(
        {
            "run_id": run_id,
            "engine": "plain",
            "status": "finished",
            "agents": [],
            "totals": {},
        }
    )

    record = json.loads(
        (tmp_path / "cache" / "harness" / f"{run_id}.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = json.loads(
        (tmp_path / "cache" / "harness" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["guest"] is True
    assert record["owner_id"] == 17
    assert manifest["runs"][0]["guest"] is True
    assert jobs.persisted_run_count(17) == 1


def test_guest_settings_are_openai_only_and_cannot_set_custom_origins(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    guest, _ = auth.create_guest_session()
    payload = settings.get_settings(guest)
    assert [provider["id"] for provider in payload["providers"]] == ["openai"]

    with pytest.raises(PermissionError, match="OpenAI"):
        settings.save_settings(
            {"updates": {"OPENAI_BASE_URL": "http://127.0.0.1:1"}},
            user=guest,
        )
    with pytest.raises(ValueError, match="model identifiers"):
        settings.save_settings(
            {"updates": {"AGENT_MONITOR_MODEL": '<img src=x onerror="alert(1)">'}},
            user=guest,
        )
    with pytest.raises(ValueError, match="OpenAI API key"):
        settings.save_settings(
            {"updates": {"OPENAI_API_KEY": "sk-" + "x" * 1000}},
            user=guest,
        )

    # Even a legacy row or direct request value cannot influence the target.
    auth.set_user_env(
        guest["id"],
        {
            "OPENAI_API_KEY": "sk-guest-test-key-123456789",
            "OPENAI_BASE_URL": "http://127.0.0.1:1",
        },
    )
    assert "OPENAI_BASE_URL" not in settings.resolved_user_env(guest)
    urls: list[str] = []

    def fake_http(url, **_kwargs):
        urls.append(url)
        return 401, {"error": {"message": "invalid test key"}}

    monkeypatch.setattr(settings, "_http_json", fake_http)
    settings.verify_provider(
        "openai",
        base_url="http://169.254.169.254/latest/meta-data",
        user=guest,
    )
    assert urls == ["https://api.openai.com/v1/models"]


def test_guest_provider_verification_is_rate_limited(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    calls: list[dict] = []
    monkeypatch.setenv("AGENT_MONITOR_GUEST_VERIFICATIONS_PER_HOUR", "1")

    def verify_provider(*_args, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "provider": "openai"}

    monkeypatch.setattr(settings, "verify_provider", verify_provider)
    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        body = {"provider": "openai", "api_key": "sk-guest-test-key-123456789"}
        first_status, _, first_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/settings/verify",
            body=body,
            headers={"Cookie": cookie},
        )
        second_status, second_headers, second_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/settings/verify",
            body=body,
            headers={"Cookie": cookie},
        )

    assert first_status == 200, first_raw
    assert second_status == 429, second_raw
    assert int(second_headers["retry-after"]) > 0
    assert "verification limit" in json.loads(second_raw)["error"]
    assert len(calls) == 1


def test_guest_provider_verification_concurrency_is_bounded(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENT_MONITOR_GUEST_VERIFY_GLOBAL_CONCURRENCY", "1")

    first_error, _ = console._reserve_guest_verification(101)
    same_user_error, _ = console._reserve_guest_verification(101)
    global_error, _ = console._reserve_guest_verification(202)
    console._release_guest_verification(101)
    second_error, _ = console._reserve_guest_verification(202)
    console._release_guest_verification(202)

    assert first_error is None
    assert "current key verification" in str(same_user_error)
    assert "capacity" in str(global_error)
    assert second_error is None


def test_guest_logout_deletes_identity_and_encrypted_settings(guest_console):
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        auth.set_user_env(
            guest["id"], {"OPENAI_API_KEY": "sk-guest-test-key-123456789"}
        )
        status, _, raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/auth/logout",
            body={},
            headers={"Cookie": cookie},
        )

    assert status == 200, raw
    assert _database_counts(guest_console["db"]) == (0, 0)
    with sqlite3.connect(guest_console["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_env").fetchone()[0] == 0


def test_expired_orphan_guest_is_purged_on_later_session_creation(guest_console):
    guest, _ = auth.create_guest_session()
    auth.set_user_env(
        guest["id"], {"OPENAI_API_KEY": "sk-guest-test-key-123456789"}
    )
    admin = auth.register("admin@example.test", "long-enough-password")
    with sqlite3.connect(guest_console["db"]) as conn:
        conn.execute(
            "UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'"
            " WHERE user_id=?",
            (guest["id"],),
        )
    auth.create_session(admin["id"])

    with sqlite3.connect(guest_console["db"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM users WHERE id=?", (guest["id"],)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM user_env WHERE user_id=?", (guest["id"],)
        ).fetchone()[0] == 0


def test_contained_path_rejects_absolute_sibling_and_symlink_escape(tmp_path: Path):
    root = tmp_path / "run"
    sibling = tmp_path / "run-other"
    root.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "outside-link").symlink_to(sibling / "secret.txt")

    assert console._contained_path(root, "/etc/passwd") is None
    assert console._contained_path(root, "../run-other/secret.txt") is None
    assert console._contained_path(root, "outside-link") is None


def test_public_static_route_rejects_absolute_file_reads(guest_console):
    with running_server() as port:
        status, _, _ = request(
            port, "GET", f"{APP_MOUNT}/static//etc/passwd"
        )
    assert status == 404


def test_nested_run_action_cannot_escape_the_owned_run_id(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    import harness_dashboard.server as dashboard

    loaded: list[str] = []
    monkeypatch.setattr(
        dashboard,
        "_load_run",
        lambda run_id: loaded.append(run_id) or {"server_secret": "do-not-return"},
    )

    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        monkeypatch.setattr(
            console.job_manager,
            "run_owner",
            lambda run_id: guest["id"] if run_id == "owned-run" else None,
        )
        status, _, raw = request(
            port,
            "GET",
            f"{APP_MOUNT}/api/run/owned-run/../../../system_settings/export",
            headers={"Cookie": cookie},
        )

    assert status == 404, raw
    assert loaded == []
    assert "do-not-return" not in raw


def test_guest_cannot_read_ownerless_job_details(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        console.job_manager,
        "get_job",
        lambda _job_id: {"job_id": "legacy", "owner_id": None},
    )
    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        status, _, _ = request(
            port,
            "GET",
            f"{APP_MOUNT}/api/jobs/legacy",
            headers={"Cookie": cookie},
        )
    assert status == 404


def test_guest_cannot_invoke_workspace_or_final_tex_compilers(
    guest_console, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "owned-run"
    workspace.mkdir()
    (workspace / "proof.tex").write_text(
        r"\documentclass{article}\begin{document}test\end{document}",
        encoding="utf-8",
    )
    compile_calls: list[tuple] = []
    monkeypatch.setattr(console, "_workspace_for_run", lambda _run_id: workspace)
    monkeypatch.setattr(
        console.Handler, "_owns_run", lambda _self, _run_id, _user: True
    )
    monkeypatch.setattr(
        console,
        "_compile_workspace_pdf",
        lambda *args: compile_calls.append(args),
    )

    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        workspace_status, _, workspace_raw = request(
            port,
            "GET",
            f"{APP_MOUNT}/api/workspace/owned-run/pdf?path=proof.tex",
            headers={"Cookie": cookie},
        )
        final_status, _, final_raw = request(
            port,
            "GET",
            f"{APP_MOUNT}/api/run/owned-run/final.pdf",
            headers={"Cookie": cookie},
        )
        payload_status, _, payload_raw = request(
            port,
            "GET",
            f"{APP_MOUNT}/api/run/owned-run/final_latex",
            headers={"Cookie": cookie},
        )

    assert workspace_status == 403, workspace_raw
    assert final_status == 403, final_raw
    assert payload_status == 403, payload_raw
    assert compile_calls == []


def test_operator_cannot_compile_a_guest_authored_artifact(
    guest_console, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "guest-run"
    workspace.mkdir()
    (workspace / "proof.tex").write_text(
        r"\documentclass{article}\begin{document}test\end{document}",
        encoding="utf-8",
    )
    admin = auth.register("admin@example.test", "long-enough-password")
    token = auth.create_session(admin["id"])
    monkeypatch.setattr(console, "_workspace_for_run", lambda _run_id: workspace)
    monkeypatch.setattr(
        console.Handler, "_owns_run", lambda _self, _run_id, _user: True
    )
    monkeypatch.setattr(
        console,
        "_run_record_for",
        lambda _run_id: {"guest": True, "owner_id": 99},
    )

    with running_server() as port:
        headers = {"Cookie": f"{auth.SESSION_COOKIE}={token}"}
        statuses = [
            request(
                port,
                "GET",
                f"{APP_MOUNT}/api/workspace/guest-run/pdf?path=proof.tex",
                headers=headers,
            )[0],
            request(
                port,
                "GET",
                f"{APP_MOUNT}/api/run/guest-run/final_latex",
                headers=headers,
            )[0],
            request(
                port,
                "GET",
                f"{APP_MOUNT}/api/run/guest-run/final.pdf",
                headers=headers,
            )[0],
        ]

    assert statuses == [403, 403, 403]


def test_public_mode_respects_disable_flag_and_guest_limiter(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(console, "PUBLIC_MODE", True)
    monkeypatch.setenv("AGENT_MONITOR_GUEST_ACCESS", "0")
    assert console.guest_access_enabled() is False
    with running_server() as port:
        status, _, raw = request(port, "GET", f"{APP_MOUNT}/api/auth/me")
    assert status == 200
    assert json.loads(raw)["user"] is None

    monkeypatch.setenv("AGENT_MONITOR_GUEST_ACCESS", "1")
    monkeypatch.setattr(console, "_reserve_guest_session_slot", lambda: (False, 12))
    with running_server() as port:
        status, headers, _ = request(port, "GET", f"{APP_MOUNT}/api/auth/me")
    assert status == 429
    assert headers["retry-after"] == "12"
    assert _database_counts(guest_console["db"]) == (0, 0)


def test_guest_start_uses_allowlist_and_bounded_iterations(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    calls: list[dict] = []

    def fake_start_job(**kwargs):
        calls.append(kwargs)
        return {
            "job_id": "fake-job",
            "run_id": "plain_fake_fake-job",
            "engine": kwargs["engine"],
            "status": "running",
            "owner_id": kwargs["user"]["id"],
        }

    monkeypatch.setattr(console.job_manager, "start_job", fake_start_job)

    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        allowed_status, _, allowed_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/runs",
            body={
                "engine": "plain",
                "problem_text": "Prove that 1 + 1 = 2.",
                "max_iterations": 100_000,
                "subagent_model": '<img src=x onerror="alert(1)">',
            },
            headers={"Cookie": cookie},
        )
        blocked_status, _, blocked_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/runs",
            body={
                "engine": "hermes",
                "problem_text": "Do not start this engine.",
                "max_iterations": 1,
            },
            headers={"Cookie": cookie},
        )
        unsafe_model_status, _, unsafe_model_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/runs",
            body={
                "engine": "plain",
                "problem_text": "Do not persist markup from a model id.",
                "model": '<img src=x onerror="alert(1)">',
            },
            headers={"Cookie": cookie},
        )

    assert allowed_status == 200, allowed_raw
    assert len(calls) == 1
    assert calls[0]["engine"] == "plain"
    assert calls[0]["max_iterations"] == 8
    assert calls[0]["max_output_tokens"] == 4096
    assert calls[0]["user"]["guest"] is True
    assert calls[0]["subagent_model"] is None
    assert blocked_status == 403, blocked_raw
    assert json.loads(blocked_raw)["error"]
    assert unsafe_model_status == 400, unsafe_model_raw
    assert len(calls) == 1


def test_guest_run_starts_have_rolling_and_retained_limits(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    calls: list[dict] = []
    monkeypatch.setenv("AGENT_MONITOR_GUEST_RUNS_PER_HOUR", "1")
    monkeypatch.setattr(console.job_manager, "list_jobs", lambda: [])
    monkeypatch.setattr(console.job_manager, "persisted_run_count", lambda _owner: 0)
    monkeypatch.setattr(
        console.job_manager,
        "start_job",
        lambda **kwargs: calls.append(kwargs)
        or {
            "job_id": "limited-job",
            "run_id": "plain_limited_limited-job",
            "status": "running",
        },
    )

    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        body = {"engine": "plain", "problem_text": "Prove a bounded claim."}
        first_status, _, first_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/runs",
            body=body,
            headers={"Cookie": cookie},
        )
        second_status, second_headers, second_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/runs",
            body=body,
            headers={"Cookie": cookie},
        )

        console._GUEST_RUN_WINDOWS.clear()
        console._GUEST_GLOBAL_RUN_WINDOW.clear()
        monkeypatch.setattr(
            console.job_manager,
            "persisted_run_count",
            lambda _owner: console.guest_runs_per_session(),
        )
        retained_status, _, retained_raw = request(
            port,
            "POST",
            f"{APP_MOUNT}/api/runs",
            body=body,
            headers={"Cookie": cookie},
        )

    assert first_status == 200, first_raw
    assert second_status == 429, second_raw
    assert int(second_headers["retry-after"]) > 0
    assert retained_status == 429, retained_raw
    assert len(calls) == 1


def test_guest_admission_and_job_registration_are_atomic(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    active: list[dict] = []
    calls: list[dict] = []
    first_start_entered = threading.Event()
    release_first_start = threading.Event()
    second_capacity_check = threading.Event()
    capacity_checks: list[None] = []

    def list_jobs():
        capacity_checks.append(None)
        if len(capacity_checks) >= 2:
            second_capacity_check.set()
        return list(active)

    def start_job(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            first_start_entered.set()
            assert release_first_start.wait(3)
        job = {
            "job_id": f"atomic-{len(calls)}",
            "run_id": f"plain_atomic_{len(calls)}",
            "status": "running",
            "owner_id": kwargs["user"]["id"],
            "guest": True,
        }
        active.append(job)
        return job

    monkeypatch.setattr(console.job_manager, "list_jobs", list_jobs)
    monkeypatch.setattr(console.job_manager, "start_job", start_job)

    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        responses: list[tuple[int, dict[str, str], str]] = []

        def submit():
            responses.append(
                request(
                    port,
                    "POST",
                    f"{APP_MOUNT}/api/runs",
                    body={"engine": "plain", "problem_text": "Prove it."},
                    headers={"Cookie": cookie},
                )
            )

        first = threading.Thread(target=submit)
        second = threading.Thread(target=submit)
        first.start()
        assert first_start_entered.wait(2)
        second.start()
        try:
            # The second request must not inspect the same empty active-job
            # snapshot while the first request is still registering its job.
            assert not second_capacity_check.wait(0.25)
        finally:
            release_first_start.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(response[0] for response in responses) == [200, 429]
    assert len(calls) == 1


def test_plain_runner_enforces_the_guest_output_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_monitor.runners import plain_runner

    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="proof"))],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3),
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda: client))
    monkeypatch.delenv("AGENT_MONITOR_CODEX_SUBSCRIPTION", raising=False)
    monkeypatch.setenv("AGENT_MONITOR_PLAIN_MAX_OUTPUT_TOKENS", "999999")
    monkeypatch.setenv("PLAIN_MODEL", "gpt-5.2")
    monkeypatch.setattr(sys, "argv", ["plain_runner.py", "Prove the claim."])
    monkeypatch.chdir(tmp_path)

    assert plain_runner.main() == 0
    assert captured["max_completion_tokens"] == 8192
    assert (tmp_path / "proof.md").read_text(encoding="utf-8") == "proof"


def test_login_contains_feature_gated_guest_call_to_action():
    html = (Path(console.__file__).resolve().parent / "web" / "login.html").read_text(
        encoding="utf-8"
    )
    assert re.search(r"(?:continue|try|use)[^<]{0,40}\bguest\b", html, re.IGNORECASE)
    assert "/api/auth/guest" in html
    assert "guest_access" in html


def test_homepage_guest_bridge_survives_the_stable_sign_in_redirect():
    root = Path(console.__file__).resolve().parent.parent
    homepage = (root / "docs" / "index.md").read_text(encoding="utf-8")
    config = (root / "docs" / ".vitepress" / "config.mjs").read_text(
        encoding="utf-8"
    )
    bridge = (root / "docs" / "guest.md").read_text(encoding="utf-8")
    login = (root / "agent_monitor" / "web" / "login.html").read_text(
        encoding="utf-8"
    )

    assert "link: /guest" in homepage
    assert "link: '/guest'" in config
    assert "agent_monitor_guest_intent=1" in bridge
    assert "/sign-in#guest" in bridge
    assert "agent_monitor_guest_intent=" in login
    assert "location.hash==='#guest'" in login
    assert "takeGuestIntent" in login


def test_untrusted_run_markdown_is_sanitized_before_insertion():
    root = Path(console.__file__).resolve().parent.parent
    console_html = (root / "agent_monitor" / "web" / "console.html").read_text(
        encoding="utf-8"
    )
    dashboard_html = (
        root / "monitor_core" / "harness_dashboard" / "web" / "index.html"
    ).read_text(encoding="utf-8")

    for html in (console_html, dashboard_html):
        assert "SAFE_MARKDOWN_TAGS" in html
        assert "sanitizeRenderedHtml" in html
        assert "['http:', 'https:', 'mailto:']" in html or (
            "['http:','https:','mailto:']" in html
        )
        assert "replace(/\"/g,'&quot;')" in html

    assert "return sanitizeRenderedHtml(html);" in console_html
    assert "return sanitizeRenderedHtml(h);" in console_html
    assert "mdEl.innerHTML = safeMarkdown(text);" in dashboard_html


@pytest.mark.parametrize(
    "relative_path",
    [
        "agent_monitor/web/login.html",
        "agent_monitor/web/console.html",
        "monitor_core/harness_dashboard/web/index.html",
    ],
)
def test_browser_inline_scripts_have_valid_javascript(
    relative_path: str, tmp_path: Path
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    html_path = Path(console.__file__).resolve().parent.parent / relative_path
    html = html_path.read_text(encoding="utf-8")
    scripts = re.findall(
        r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert scripts, f"no inline JavaScript found in {relative_path}"

    script_path = tmp_path / f"{Path(relative_path).name}.inline.js"
    script_path.write_text("\n;\n".join(scripts), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(script_path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
