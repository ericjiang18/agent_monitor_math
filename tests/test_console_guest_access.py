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
    # Each test's guests start with their judge ratings (easy_route) unused.
    monkeypatch.setattr(console, "_GUEST_RATINGS", {})
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
    for name in ("AGENT_MONITOR_ANALYZE_ENABLED", "AGENT_MONITOR_ANALYZE_GUESTS", "ANALYZE_CMD"):
        monkeypatch.delenv(name, raising=False)
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
    monkeypatch.delenv("KIMI_PROOF_CMD", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_GUEST_KIMI_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_GUEST_HOSTED_KIMI", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_SPONSORED_KIMI", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_SPONSORED_KIMI_URL", raising=False)
    console._GUEST_SESSION_WINDOW.clear()
    console._GUEST_RUN_WINDOWS.clear()
    console._GUEST_GLOBAL_RUN_WINDOW.clear()
    console._GUEST_VERIFY_WINDOWS.clear()
    console._GUEST_VERIFY_GLOBAL_WINDOW.clear()
    console._GUEST_VERIFY_ACTIVE_USERS.clear()
    console._GUEST_SESSION_CLIENT_WINDOWS.clear()
    console._GUEST_RUN_CLIENT_WINDOWS.clear()
    console._GUEST_DERIVED_CLIENT_WINDOWS.clear()
    console._GUEST_RUN_CLIENTS.clear()

    yield {"db": auth.DB_PATH, "env": env_file}
    console._GUEST_SESSION_WINDOW.clear()
    console._GUEST_RUN_WINDOWS.clear()
    console._GUEST_GLOBAL_RUN_WINDOW.clear()
    console._GUEST_VERIFY_WINDOWS.clear()
    console._GUEST_VERIFY_GLOBAL_WINDOW.clear()
    console._GUEST_VERIFY_ACTIVE_USERS.clear()
    console._GUEST_SESSION_CLIENT_WINDOWS.clear()
    console._GUEST_RUN_CLIENT_WINDOWS.clear()
    console._GUEST_DERIVED_CLIENT_WINDOWS.clear()
    console._GUEST_RUN_CLIENTS.clear()
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
    # Deployment defaults may seed a personal model. Remove that override so
    # this assertion specifically exercises the administrator's fallback.
    auth.set_user_env(admin["id"], {}, clear=["AGENT_MONITOR_MODEL"])

    guest_env = settings.resolved_user_env(guest)
    admin_env = settings.resolved_user_env(admin)
    regular_env = settings.resolved_user_env(regular)
    stale_guest_env = settings.resolved_user_env(
        {**guest, "is_admin": True}
    )
    assert "OPENAI_API_KEY" not in guest_env
    assert "AGENT_MONITOR_MODEL" not in guest_env
    assert "OPENAI_API_KEY" not in regular_env
    # Registration seeds the account's own Kimi default. It must never be the
    # operator's model override; once cleared, nothing falls back to it.
    assert regular_env.get("AGENT_MONITOR_MODEL") == auth._NEW_USER_DEFAULTS[
        "AGENT_MONITOR_MODEL"
    ]
    assert regular_env.get("AGENT_MONITOR_MODEL") != "gpt-server"
    auth.set_user_env(regular["id"], {}, clear=["AGENT_MONITOR_MODEL"])
    assert "AGENT_MONITOR_MODEL" not in settings.resolved_user_env(regular)
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


def test_guest_lean_status_is_read_only_and_reveals_no_host_details(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    from agent_monitor import lean_verify

    # Even if toolchain_status ever grew host details again, guests get only
    # the allowlisted broker fields.
    monkeypatch.setattr(
        lean_verify,
        "toolchain_status",
        lambda: {
            "lean": "/home/ubuntu/.elan/bin/lean",
            "lake": "/home/ubuntu/.elan/bin/lake",
            "elan": "/home/ubuntu/.elan/bin/elan",
            "mathlib_project": "/home/ubuntu/.cache/agent-monitor/mathlib-v4.14.0",
            "socket": "/run/agent-monitor-lean/core.sock",
            "version": "Lean (version 4.14.0, x86_64-unknown-linux-gnu, commit 410fab728470, Release)",
            "available": True,
            "mathlib_available": False,
            "checker_backend": "lean-broker-v1",
            "checker_sandbox": "systemd-socket-dynamic-user-v1",
        },
    )

    def no_discovery(*_args, **_kwargs):
        raise AssertionError("guests never trigger local harness discovery")

    monkeypatch.setattr(lean_verify, "harness_engines", no_discovery)
    monkeypatch.setattr(lean_verify, "harness_model_options", no_discovery)
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        before = dict(console._GUEST_DERIVED_WINDOWS)
        status, _, raw = request(
            port, "GET", f"{APP_MOUNT}/api/lean/status", headers={"Cookie": cookie}
        )
        assert status == 200, raw
        payload = json.loads(raw)
        assert set(payload) == {
            "available", "mathlib_available", "version", "checker_backend", "checker_sandbox",
            "engines", "harness_auth_route", "harness_models", "harness_model",
            "harness_engine", "harness_available",
        }
        assert payload["available"] is True and payload["mathlib_available"] is False
        assert payload["engines"] == [] and payload["harness_available"] is False
        assert payload["harness_models"] == ["kimi-k3"]
        assert "/home/" not in raw and "/run/" not in raw and ".elan" not in raw
        # Reading the status is not a guest Formal/DAG action.
        assert dict(console._GUEST_DERIVED_WINDOWS) == before
        # Writes and the local harness stay sign-in only.
        status, _, _ = request(
            port, "POST", f"{APP_MOUNT}/api/lean/status", body={}, headers={"Cookie": cookie}
        )
        assert status in {404, 405}
    # Without a session it is still refused.
    with running_server() as port:
        status, _, _ = request(port, "GET", f"{APP_MOUNT}/api/lean/status")
        assert status == 401


def test_guest_engine_policy_defaults_and_environment_overrides(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    # Guests may analyze and make manuscripts readable by default (Ansatz
    # Analyze and Ansatz Readability, each behind its own switch).
    assert set(console.guest_engine_allowlist()) == {"kimi", "plain", "analyze", "readability"}
    assert console.guest_max_iterations() == 8

    monkeypatch.setenv("AGENT_MONITOR_GUEST_ENGINES", " plain, improof,ucla ")
    monkeypatch.setenv("AGENT_MONITOR_GUEST_MAX_ITERATIONS", "3")
    assert set(console.guest_engine_allowlist()) == {"plain", "analyze", "readability"}
    assert console.guest_max_iterations() == 3

    monkeypatch.setenv("PLAIN_CMD", "/usr/bin/false")
    assert set(console.guest_engine_allowlist()) == {"analyze", "readability"}
    assert console.guest_access_enabled() is True
    # Analyze has its own guest switch (and kill switch, and command guard);
    # Readability's guest switch follows it unless it is set itself.
    monkeypatch.setenv("AGENT_MONITOR_ANALYZE_GUESTS", "0")
    assert set(console.guest_engine_allowlist()) == set()
    assert console.guest_access_enabled() is False
    monkeypatch.setenv("AGENT_MONITOR_READABILITY_GUESTS", "1")
    assert set(console.guest_engine_allowlist()) == {"readability"}
    monkeypatch.delenv("AGENT_MONITOR_READABILITY_GUESTS")
    monkeypatch.delenv("AGENT_MONITOR_ANALYZE_GUESTS")
    monkeypatch.setenv("ANALYZE_CMD", "/usr/bin/false")
    assert set(console.guest_engine_allowlist()) == {"readability"}
    monkeypatch.setenv("READABILITY_CMD", "/usr/bin/false")
    assert set(console.guest_engine_allowlist()) == set()
    monkeypatch.delenv("ANALYZE_CMD")
    monkeypatch.delenv("READABILITY_CMD")
    monkeypatch.setenv("AGENT_MONITOR_ANALYZE_ENABLED", "0")
    monkeypatch.setenv("AGENT_MONITOR_READABILITY_ENABLED", "0")
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


def test_only_operator_runs_record_shared_library_memory(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    calls: list[dict] = []
    monkeypatch.setattr(library, "record_run_memory", lambda **kwargs: calls.append(kwargs))
    admin = auth.register("owner@example.test", "long-enough-password")
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    member = auth.register("member@example.test", "long-enough-password")
    guest, _token = auth.create_guest_session()
    assert admin["is_admin"] and not member["is_admin"]
    kwargs = {
        "run_id": "kimi_test",
        "engine": "kimi",
        "problem_id": "test",
        "status": "finished",
        "problem_text": "problem",
        "outcome": "finished",
        "final_out": "proof",
    }

    # A signed-in member's problem and output never reach the operator's
    # shared library (the persona preamble source), like guests.
    jobs._record_run_memory(guest=False, owner_id=member["id"], **kwargs)
    jobs._record_run_memory(guest=False, owner_id=guest["id"], **kwargs)
    jobs._record_run_memory(guest=False, owner_id=987654, **kwargs)
    assert calls == []
    jobs._record_run_memory(guest=False, owner_id=admin["id"], **kwargs)
    assert [call["run_id"] for call in calls] == ["kimi_test"]
    assert auth.is_operator_account(admin["id"]) is True
    assert auth.is_operator_account(member["id"]) is False
    assert auth.is_operator_account(guest["id"]) is False
    assert auth.is_operator_account(None) is False


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


def test_guest_settings_support_kimi_and_openai_without_custom_origins(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    guest, _ = auth.create_guest_session()
    payload = settings.get_settings(guest)
    assert [provider["id"] for provider in payload["providers"]] == ["kimi", "openai"]
    assert payload["default_model"] == "kimi-k3"

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
    with pytest.raises(ValueError, match="API key"):
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
    monkeypatch.setattr(console, "_reserve_guest_session_slot", lambda *_client: (False, 12))
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
    """Guest Plain clamps every provider request to the [256, 8192] ceiling.

    Plain calls providers through api_backend's HTTP transport rather than the
    OpenAI SDK, so the request is captured at that transport boundary. The
    ceiling must hold on its own, even below api_backend's 1024-token request
    floor and without the job-level AGENT_MONITOR_MAX_OUTPUT_TOKENS.
    """
    import os

    from agent_monitor.runners import plain_runner

    requests: list[dict] = []

    def fake_http_json(url, *, method, headers, payload, timeout):
        requests.append({"url": url, "method": method, "payload": payload})
        return 200, {
            "model": "gpt-5.2",
            "output_text": "# Proof\n\nproof",
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }

    monkeypatch.setattr(settings, "_http_json", fake_http_json)
    for name in (
        "AGENT_MONITOR_CODEX_SUBSCRIPTION",
        "AGENT_MONITOR_CLAUDE_SUBSCRIPTION",
        "AGENT_MONITOR_MAX_OUTPUT_TOKENS",
        "AGENT_MONITOR_API_MAX_TOKENS",
        "AGENT_MONITOR_SELECTED_MODEL",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("PLAIN_MODEL", "gpt-5.2")
    monkeypatch.setattr(sys, "argv", ["plain_runner.py", "Prove the claim."])
    monkeypatch.chdir(tmp_path)

    for requested, enforced in (("999999", 8192), ("1", 256)):
        monkeypatch.setenv("AGENT_MONITOR_PLAIN_MAX_OUTPUT_TOKENS", requested)
        assert plain_runner.main() == 0
        assert requests[-1]["url"].endswith("/responses")
        assert requests[-1]["payload"]["max_output_tokens"] == enforced
    # One billable call per guest run, and the per-call limits do not leak
    # into later in-process runs.
    assert len(requests) == 2
    assert "AGENT_MONITOR_MAX_OUTPUT_TOKENS" not in os.environ
    assert "AGENT_MONITOR_API_MAX_TOKENS" not in os.environ
    assert (tmp_path / "proof.md").read_text(encoding="utf-8").startswith("# Proof")

    # A stricter job-level ceiling is never raised by the Plain limit.
    monkeypatch.setenv("AGENT_MONITOR_PLAIN_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("AGENT_MONITOR_MAX_OUTPUT_TOKENS", "300")
    assert plain_runner.main() == 0
    assert requests[-1]["payload"]["max_output_tokens"] == 300
    assert os.environ["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] == "300"


def test_guest_kimi_default_and_locked_account_engines(guest_console):
    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        status, _, raw = request(port, "GET", f"{APP_MOUNT}/api/engines", headers={"Cookie": cookie})
        assert status == 200
        engines = {engine["id"]: engine for engine in json.loads(raw)["engines"]}
        assert engines["kimi"]["requires_login"] is False
        # Signing up gives a member account: the own-key CGM harness is
        # available after sign-in, the coding-agent CLIs never are.
        assert engines["math_harness"]["requires_login"] is True
        for engine in ("claude", "codex"):
            assert engines[engine]["requires_admin"] is True
            assert engines[engine]["available"] is False
            assert not engines[engine].get("requires_login")
            assert engines[engine]["hint"] == "Administrator only on this site: this harness runs local tools"
        for engine in ("claude", "codex"):
            status, _, raw = request(port, "POST", f"{APP_MOUNT}/api/runs",
                                     body={"engine": engine, "problem_text": "Prove it."}, headers={"Cookie": cookie})
            assert status == 403, raw


def test_guest_engines_name_only_the_api_route(guest_console):
    """A guest, and the member it becomes on sign-up, bills an API key: the
    engine menu must not offer Plain on "Codex / Claude Code / API"."""
    with running_server() as port:
        _, cookie, _ = _issue_guest(port)
        status, _, raw = request(port, "GET", f"{APP_MOUNT}/api/engines", headers={"Cookie": cookie})
    assert status == 200
    engines = {engine["id"]: engine for engine in json.loads(raw)["engines"]}
    for engine in ("kimi", "plain", "math_harness"):
        assert engines[engine]["auth_modes"] == ["api_key"], engine
    # Administrator-only entries still describe the engine itself.
    assert "codex_subscription" in engines["codex"]["auth_modes"]


def test_guest_ten_run_budget_survives_deleted_runs_and_restart(guest_console, monkeypatch):
    calls = []
    monkeypatch.setenv("AGENT_MONITOR_GUEST_RUNS_PER_HOUR", "24")
    monkeypatch.setattr(console.job_manager, "list_jobs", lambda **kw: [])
    monkeypatch.setattr(console.job_manager, "start_job", lambda **kw: calls.append(kw) or {"run_id": f"run-{len(calls)}"})
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        for n in range(10):
            status, _, raw = request(port, "POST", f"{APP_MOUNT}/api/runs",
                                     body={"problem_text": "Prove it."}, headers={"Cookie": cookie})
            assert status == 200, raw
            assert json.loads(raw)["guest_policy"]["runs_remaining"] == 9 - n
        # A process restart clears the memory limiters; deleted runs no longer
        # appear in persisted_run_count, which the fixture fixes at zero.
        auth._INITIALIZED = False
        console._GUEST_RUN_WINDOWS.clear()
        console._GUEST_GLOBAL_RUN_WINDOW.clear()
        status, _, raw = request(port, "POST", f"{APP_MOUNT}/api/runs",
                                 body={"problem_text": "One more."}, headers={"Cookie": cookie})
        assert status == 429
        assert json.loads(raw)["login_required"] is True
        assert auth.guest_runs_started(guest["id"]) == 10
        assert all(call["engine"] == "kimi" for call in calls)


def test_failed_guest_validation_refunds_usage(guest_console, monkeypatch):
    def fail(**kwargs):
        raise ValueError("Kimi is not configured")
    monkeypatch.setattr(console.job_manager, "start_job", fail)
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        status, _, _ = request(port, "POST", f"{APP_MOUNT}/api/runs",
                               body={"problem_text": "Prove it."}, headers={"Cookie": cookie})
        assert status == 400
        assert auth.guest_runs_started(guest["id"]) == 0


def test_refused_guest_starts_give_back_the_hourly_run_slots(guest_console, monkeypatch):
    # A guest without a key presses Prove as often as the hourly limit allows,
    # then adds a key as the banner says: that start must still run.
    started: list[dict] = []

    def start_job(**kwargs):
        if not settings.resolved_user_env(kwargs["user"]).get("KIMI_API_KEY"):
            raise ValueError(
                "Kimi K3 requires your own API key, or the sponsored Kimi service must be available"
            )
        started.append(kwargs)
        return {"run_id": "kimi_keyed_run", "status": "running"}

    monkeypatch.setattr(console.job_manager, "start_job", start_job)
    monkeypatch.setattr(console.job_manager, "list_jobs", lambda **_kwargs: [])
    client = "203.0.113.9"
    body = {"engine": "kimi", "problem_text": "Prove it."}
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        headers = {"Cookie": cookie, "X-Forwarded-For": client}
        for _ in range(console.guest_runs_per_hour()):
            status, _, raw = request(port, "POST", f"{APP_MOUNT}/api/runs", body=body, headers=headers)
            assert status == 400 and "own API key" in raw, raw
        assert not console._GUEST_RUN_WINDOWS.get(guest["id"])
        assert not console._GUEST_RUN_CLIENT_WINDOWS.get(client)
        assert not console._GUEST_GLOBAL_RUN_WINDOW
        saved, _, saved_raw = request(
            port, "POST", f"{APP_MOUNT}/api/settings",
            body={"updates": {"KIMI_API_KEY": "sk-guest-kimi-123456789"}}, headers=headers,
        )
        assert saved == 200, saved_raw
        status, _, raw = request(port, "POST", f"{APP_MOUNT}/api/runs", body=body, headers=headers)
    assert status == 200, raw
    assert len(started) == 1
    assert auth.guest_runs_started(guest["id"]) == 1
    assert len(console._GUEST_RUN_WINDOWS[guest["id"]]) == 1
    assert len(console._GUEST_GLOBAL_RUN_WINDOW) == 1


def test_a_guest_refused_by_the_session_limit_keeps_its_hourly_run_slots(guest_console, monkeypatch):
    monkeypatch.setattr(auth, "reserve_guest_run", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(console.job_manager, "list_jobs", lambda **_kwargs: [])
    client = "203.0.113.9"
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        status, _, raw = request(port, "POST", f"{APP_MOUNT}/api/runs",
                                 body={"engine": "kimi", "problem_text": "Prove it."},
                                 headers={"Cookie": cookie, "X-Forwarded-For": client})
    assert status == 403 and "Guest limit reached" in raw
    assert not console._GUEST_RUN_WINDOWS.get(guest["id"])
    assert not console._GUEST_RUN_CLIENT_WINDOWS.get(client)
    assert not console._GUEST_GLOBAL_RUN_WINDOW


def test_hosted_kimi_key_is_not_in_general_guest_settings(guest_console, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_GUEST_KIMI_API_KEY", "sk-dedicated-secret-123456789")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-operator-openai-123456789")
    guest, _ = auth.create_guest_session()
    assert "KIMI_API_KEY" not in settings.resolved_user_env(guest)
    assert "OPENAI_API_KEY" not in settings.resolved_user_env(guest)
    payload = settings.get_settings(guest)
    assert payload["hosted_guest_available"] is True
    assert "secret" not in json.dumps(payload)
    assert payload["providers"][0]["api_key_masked"] is None
    assert "AGENT_MONITOR_GUEST_KIMI_API_KEY" not in jobs._user_extra_env(guest, "kimi-k3")
    assert settings.verify_provider("kimi", user=guest)["hosted"] is True


def test_hosted_guest_kimi_routes_only_the_dedicated_key(guest_console, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MONITOR_GUEST_KIMI_API_KEY", "sk-dedicated-123456789")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-operator-openai-123456789")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-operator-anthropic-123456789")
    guest, _ = auth.create_guest_session()
    captured = []
    class FakeThread:
        def __init__(self, **kwargs):
            captured.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr(jobs.threading, "Thread", FakeThread)
    monkeypatch.setattr(jobs, "workspace_dir", lambda run_id: tmp_path)
    monkeypatch.setattr(jobs, "_write_run", lambda run: None)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    job = jobs.start_job(engine="kimi", problem_text="Prove it.", user=guest,
                         use_subagents=True, subagent_model="gpt-6-astra")
    assert job["model"] == "kimi-k3"
    env = captured[0]["kwargs"]["extra_env"]
    assert env["KIMI_API_KEY"] == "sk-dedicated-123456789"
    # Non-sponsored Kimi runs use the operator's endpoint (default: the
    # built-in Kimi K3 base), never an account-level base URL.
    from agent_monitor import kimi_k3

    assert env["KIMI_BASE_URL"] == kimi_k3.DEFAULT_BASE_URL
    assert env["KIMI_API_BASE"] == kimi_k3.DEFAULT_BASE_URL
    assert env["AGENT_MONITOR_KIMI_BASE_URL"] == kimi_k3.DEFAULT_BASE_URL
    # The hosted-key path sets no sponsored flag, so the guest CGM budget
    # profile must come from the explicit guest marker.
    assert env["AGENT_MONITOR_GUEST"] == "1"
    assert env["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] == "8192"
    assert "OPENAI_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env
    assert "CODEX_HOME" not in env
    assert captured[0]["kwargs"]["use_subagents"] is False
    assert captured[0]["kwargs"]["subagent_model"] is None


def test_guest_kimi_uses_isolated_broker_without_upstream_key(guest_console, monkeypatch, tmp_path):
    from agent_monitor import sponsored_kimi_client as broker
    guest, _ = auth.create_guest_session()
    issued = []
    captured = []
    records = []
    monkeypatch.setattr(broker, "configured", lambda: True)
    def credentials(**kwargs):
        issued.append(kwargs)
        return {"KIMI_API_KEY": "short-lived-loopback-token", "KIMI_API_BASE": "http://127.0.0.1:4610/internal/v1",
                "AGENT_MONITOR_SPONSORED_KIMI": "1", "AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610"}
    monkeypatch.setattr(broker, "issue_credentials", credentials)
    monkeypatch.setattr(jobs.threading, "Thread", lambda **kwargs: captured.append(kwargs) or SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(jobs, "workspace_dir", lambda run_id: tmp_path)
    monkeypatch.setattr(jobs, "_write_run", records.append)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    assert settings.get_settings(guest)["hosted_guest_available"] is True
    job = jobs.start_job(engine="kimi", problem_text="Prove it.", user=guest)
    assert job["model"] == "kimi-k3"
    assert issued[0]["engine"] == "plain" and issued[0]["client_id"] == guest["id"]
    # The token must outlive the whole guest CGM pipeline (2400 s budget plus
    # one API timeout), not just three short calls.
    assert issued[0]["minimum_ttl_seconds"] >= 2400 + 300
    env = captured[0]["kwargs"]["extra_env"]
    assert env["KIMI_BASE_URL"] == "http://127.0.0.1:4610/internal/v1"
    assert env["KIMI_API_KEY"] == "short-lived-loopback-token"
    assert env["AGENT_MONITOR_GUEST"] == "1"
    assert env["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] == "8192"
    assert "short-lived-loopback-token" not in json.dumps([records, job, settings.get_settings(guest)])


def test_configured_broker_wins_over_a_hosted_guest_key(guest_console, monkeypatch, tmp_path):
    from agent_monitor import sponsored_kimi_client as broker

    # A dev-style console env with a raw key must not let guests bypass the
    # broker's per-client and gateway call caps.
    monkeypatch.setenv("AGENT_MONITOR_GUEST_KIMI_API_KEY", "sk-dedicated-secret-123456789")
    monkeypatch.setenv("KIMI_API_KEY", "sk-operator-kimi-secret-123456789")
    guest, _ = auth.create_guest_session()
    issued = []
    captured = []
    monkeypatch.setattr(broker, "configured", lambda: True)
    monkeypatch.setattr(
        broker, "issue_credentials",
        lambda **kwargs: issued.append(kwargs) or {
            "KIMI_API_KEY": "short-lived-loopback-token",
            "KIMI_API_BASE": "http://127.0.0.1:4610/internal/v1",
            "AGENT_MONITOR_SPONSORED_KIMI": "1",
            "AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610",
        },
    )
    monkeypatch.setattr(jobs.threading, "Thread", lambda **kwargs: captured.append(kwargs) or SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(jobs, "workspace_dir", lambda run_id: tmp_path)
    monkeypatch.setattr(jobs, "_write_run", lambda run: None)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    job = jobs.start_job(engine="kimi", problem_text="Prove it.", user=guest)
    assert job["credential_source"] == "sponsored_kimi"
    assert issued and issued[0]["client_id"] == guest["id"]
    env = captured[0]["kwargs"]["extra_env"]
    assert env["KIMI_API_KEY"] == "short-lived-loopback-token"
    assert env["KIMI_BASE_URL"] == "http://127.0.0.1:4610/internal/v1"
    assert "secret" not in json.dumps(env)


def test_cgm_problems_over_the_byte_limit_are_refused_before_a_run(
    guest_console, monkeypatch, tmp_path
):
    from agent_monitor.runners import math_harness_runner as harness

    started = []
    monkeypatch.setattr(
        console.job_manager, "start_job", lambda **kwargs: started.append(kwargs) or {}
    )
    # 20,000 characters (within the guest form limit) but 60,000 UTF-8 bytes.
    multibyte = "\u2200" * 20_000
    assert len(multibyte.encode("utf-8")) > harness.MAX_PROBLEM_BYTES
    with pytest.raises(ValueError, match="44,000 bytes"):
        jobs._check_cgm_problem_size("kimi", multibyte)
    jobs._check_cgm_problem_size("plain", multibyte)

    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        status, _, raw = request(
            port, "POST", f"{APP_MOUNT}/api/runs",
            body={"engine": "kimi", "problem_text": multibyte}, headers={"Cookie": cookie},
        )
        assert status == 400 and "bytes of UTF-8 text" in raw
        assert auth.guest_runs_started(guest["id"]) == 0
    assert started == []
    assert not console._GUEST_RUN_WINDOWS.get(guest["id"])


def test_kimi_runner_delegates_to_the_tool_free_cgm_pipeline(tmp_path, monkeypatch):
    from agent_monitor import kimi_k3
    from agent_monitor.runners import kimi_runner
    from agent_monitor.runners import math_harness_runner as harness

    calls = []

    def run_pipeline(workspace, problem_text, route, **kwargs):
        calls.append(
            {
                "workspace": workspace,
                "problem": problem_text,
                "route": route,
                "kwargs": kwargs,
                "base": kimi_runner.os.environ.get("KIMI_API_BASE"),
                "env": dict(kimi_runner.os.environ),
            }
        )
        (Path(workspace) / "proof.md").write_text("# Claim\n\nproof", encoding="utf-8")
        return {
            "status": "accepted", "accepted": True, "written": ["proof.md"],
            "fact_ids": ["a" * 16, "b" * 16], "proof_text": "# Claim\n\nproof",
            "observed_model": "kimi-k3",
        }

    monkeypatch.setattr(harness, "run_pipeline", run_pipeline)
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-kimi-123456789")
    # Account-level bases, account routes and ambient proxies must not count.
    monkeypatch.setenv("KIMI_API_BASE", "http://169.254.169.254/v1")
    monkeypatch.setenv("KIMI_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AGENT_MONITOR_CODEX_SUBSCRIPTION", "1")
    monkeypatch.setenv("AGENT_MONITOR_CLAUDE_SUBSCRIPTION", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.delenv("AGENT_MONITOR_SPONSORED_KIMI", raising=False)
    monkeypatch.delenv("AGENT_MONITOR_KIMI_BASE_URL", raising=False)
    (tmp_path / "problem.txt").write_text("Prove that 1 + 1 = 2.", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["kimi_runner.py", "PROMPT WRAPPER\nProve it."])
    monkeypatch.chdir(tmp_path)

    assert kimi_runner.main() == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["route"] == harness.Route("api", "kimi-k3", "api_chat")
    assert call["problem"] == "Prove that 1 + 1 = 2."
    assert call["kwargs"]["task"] == "PROMPT WRAPPER\nProve it."
    assert isinstance(call["kwargs"]["budgets"], harness.Budgets)
    assert call["base"] == kimi_k3.DEFAULT_BASE_URL
    for name in (
        "AGENT_MONITOR_CODEX_SUBSCRIPTION", "AGENT_MONITOR_CLAUDE_SUBSCRIPTION",
        "HTTPS_PROXY", "KIMI_BASE_URL",
    ):
        assert name not in call["env"]

    # An operator-chosen HTTPS base is honored; a non-HTTPS one never is.
    calls.clear()
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-kimi-123456789")
    monkeypatch.setenv("AGENT_MONITOR_KIMI_BASE_URL", "https://kimi.operator.example/v1")
    assert kimi_runner.main() == 0
    assert calls[0]["base"] == "https://kimi.operator.example/v1"
    calls.clear()
    monkeypatch.setenv("AGENT_MONITOR_KIMI_BASE_URL", "http://10.0.0.1/v1")
    assert kimi_runner.main() == 0
    assert calls[0]["base"] == kimi_k3.DEFAULT_BASE_URL


def test_sponsored_kimi_runner_requires_the_attested_loopback_broker(
    tmp_path, monkeypatch, capsys
):
    from agent_monitor.runners import kimi_runner
    from agent_monitor.runners import math_harness_runner as harness

    calls = []
    monkeypatch.setattr(
        harness,
        "run_pipeline",
        lambda *args, **kwargs: calls.append(kimi_runner.os.environ.get("KIMI_API_BASE"))
        or {"accepted": False, "written": [], "fact_ids": [], "proof_text": ""},
    )
    monkeypatch.setenv("KIMI_API_KEY", "short-lived-loopback-token")
    monkeypatch.setenv("AGENT_MONITOR_SPONSORED_KIMI", "1")
    monkeypatch.setattr(sys, "argv", ["kimi_runner.py", "Prove it."])
    monkeypatch.chdir(tmp_path)
    for bad in (
        "https://api.example/v1",
        "http://10.0.0.1:4610/internal/v1",
        "http://127.0.0.1/internal/v1",
        "http://127.0.0.1:4610/v1",
        "http://user:pw@127.0.0.1:4610/internal/v1",
    ):
        monkeypatch.setenv("KIMI_API_BASE", bad)
        assert kimi_runner.main() == 2
    assert calls == []
    assert "attestation failed" in capsys.readouterr().out
    monkeypatch.setenv("KIMI_API_BASE", "http://127.0.0.1:4610/internal/v1/")
    assert kimi_runner.main() == 0
    assert calls == ["http://127.0.0.1:4610/internal/v1"]


def test_kimi_runner_reports_pipeline_failures_without_the_key(
    tmp_path, monkeypatch, capsys
):
    from agent_monitor.runners import kimi_runner
    from agent_monitor.runners import math_harness_runner as harness

    def fail(*_args, **_kwargs):
        raise harness.ProviderCallError(
            "strategist provider call failed: HTTP 401 for sk-test-kimi-123456789"
        )

    monkeypatch.setattr(harness, "run_pipeline", fail)
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-kimi-123456789")
    monkeypatch.delenv("AGENT_MONITOR_SPONSORED_KIMI", raising=False)
    monkeypatch.setattr(sys, "argv", ["kimi_runner.py", "Prove it."])
    monkeypatch.chdir(tmp_path)
    assert kimi_runner.main() == 1
    out = capsys.readouterr().out
    assert "sk-test-kimi-123456789" not in out
    assert '"terminal": true' in out and "Kimi could not finish this proof" in out


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

    assert "SAFE_MARKDOWN_TAGS" in console_html
    assert "sanitizeRenderedHtml" in console_html
    assert "['http:', 'https:', 'mailto:']" in console_html or (
        "['http:','https:','mailto:']" in console_html
    )
    assert "replace(/\"/g,'&quot;')" in console_html

    assert "return sanitizeRenderedHtml(html);" in console_html
    assert "return sanitizeRenderedHtml(h);" in console_html
    # The monitor renders no markdown since its Final Proof panel was removed;
    # run text reaches its DOM only through esc().
    assert "marked.parse" not in dashboard_html
    assert "safeMarkdown" not in dashboard_html
    assert (
        "const esc = s => String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;')"
        ".replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;');"
    ) in dashboard_html


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


@pytest.fixture()
def guest_derived_state():
    def clear():
        console._GUEST_DERIVED_WINDOWS.clear()
        console._GUEST_DERIVED_GLOBAL_WINDOW.clear()
        console._GUEST_DERIVED_ACTIVE_USERS.clear()
        console._GUEST_PIPELINE_BOOTSTRAPS.clear()

    clear()
    yield
    clear()


def _owned_guest_run(monkeypatch, workspace: Path, owner: dict) -> None:
    monkeypatch.setattr(console, "_workspace_for_run", lambda _run_id: workspace)
    monkeypatch.setattr(
        console.Handler, "_owns_run", lambda _self, _run_id, _user: True
    )
    monkeypatch.setattr(
        console.Handler, "_is_run_owner", lambda _self, _run_id, _user: True
    )
    monkeypatch.setattr(
        console,
        "_run_record_for",
        lambda _run_id: {
            "run_id": "kimi_run",
            "guest": True,
            "owner_id": owner["id"],
            "engine": "kimi",
            "status": "finished",
        },
    )


def test_guest_can_refresh_dags_through_the_bounded_guest_branch(
    guest_console, guest_derived_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_monitor import engine_graph, proof_graph

    workspace = tmp_path / "kimi_run"
    workspace.mkdir()
    generated: list[dict] = []
    rebuilt: list[str] = []
    monkeypatch.setattr(
        proof_graph,
        "generate",
        lambda **kwargs: generated.append(kwargs) or {"kind": kwargs["kind"], "graph": {"nodes": []}},
    )
    monkeypatch.setattr(
        engine_graph,
        "load_or_build",
        lambda _ws, _record, view="auto": rebuilt.append(view) or {"view": view},
    )
    monkeypatch.setattr(
        engine_graph, "rebuild",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("guests read the GET view")),
    )

    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        _owned_guest_run(monkeypatch, workspace, guest)
        headers = {"Cookie": cookie}
        informal = request(
            port, "POST", f"{APP_MOUNT}/api/run/kimi_run/proof_graph",
            body={"kind": "informal", "model": "gpt-6-astra"}, headers=headers,
        )
        engine = request(
            port, "POST", f"{APP_MOUNT}/api/run/kimi_run/proof_graph",
            body={"kind": "engine", "view": "facts"}, headers=headers,
        )
        unknown = request(
            port, "POST", f"{APP_MOUNT}/api/run/kimi_run/proof_graph",
            body={"kind": "compiler"}, headers=headers,
        )
        # Once the hourly limit is used up, model work is refused while the
        # model-free engine view still refreshes.
        monkeypatch.setenv("AGENT_MONITOR_GUEST_DERIVED_ACTIONS_PER_HOUR", "1")
        limited = request(
            port, "POST", f"{APP_MOUNT}/api/run/kimi_run/proof_graph",
            body={"kind": "informal"}, headers=headers,
        )
        engine_again = request(
            port, "POST", f"{APP_MOUNT}/api/run/kimi_run/proof_graph",
            body={"kind": "engine", "view": "workflow"}, headers=headers,
        )

    assert informal[0] == 200, informal[2]
    assert engine[0] == 200, engine[2]
    assert unknown[0] == 400
    assert limited[0] == 429 and limited[1].get("retry-after")
    assert engine_again[0] == 200, engine_again[2]
    # Guests never choose the model: derived DAG work stays on Kimi K3.
    assert len(generated) == 1
    assert generated[0]["model"] == "kimi-k3" and generated[0]["kind"] == "informal"
    assert rebuilt == ["facts", "workflow"]
    # Only the model-backed action is counted against the guest's hourly
    # window (and released afterwards); engine views are free like GET.
    assert len(console._GUEST_DERIVED_WINDOWS[guest["id"]]) == 1
    assert console._GUEST_DERIVED_ACTIVE_USERS == set()


def test_guest_lean_actions_are_bounded_and_harnesses_stay_sign_in_only(
    guest_console, guest_derived_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_monitor import auto_pipeline, lean_verify

    workspace = tmp_path / "kimi_run"
    workspace.mkdir()
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        lean_verify, "generate",
        lambda **kwargs: calls.append(("generate", kwargs)) or {"status": "toolchain_missing"},
    )
    monkeypatch.setattr(
        lean_verify, "revise_with_feedback",
        lambda **kwargs: calls.append(("revise", kwargs)) or {"status": "running"},
    )
    monkeypatch.setattr(
        lean_verify, "start_harness",
        lambda **kwargs: calls.append(("harness", kwargs)) or {"status": "running"},
    )
    monkeypatch.setattr(auto_pipeline, "refresh_formal", lambda **_kwargs: None)

    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        _owned_guest_run(monkeypatch, workspace, guest)
        headers = {"Cookie": cookie}
        verify = request(
            port, "POST", f"{APP_MOUNT}/api/run/kimi_run/lean_verify",
            body={"action": "verify", "max_repairs": 99, "model": "gpt-6-astra"},
            headers=headers,
        )
        default = request(
            port, "POST", f"{APP_MOUNT}/api/run/kimi_run/lean_verify",
            body={"action": "generate"}, headers=headers,
        )
        revise = request(
            port, "POST", f"{APP_MOUNT}/api/run/kimi_run/lean_verify",
            body={"action": "revise", "message": "fix it", "max_repairs": 9},
            headers=headers,
        )
        harness = request(
            port, "POST", f"{APP_MOUNT}/api/run/kimi_run/lean_verify",
            body={"action": "harness", "engine": "codex"}, headers=headers,
        )
        bad = request(
            port, "POST", f"{APP_MOUNT}/api/run/kimi_run/lean_verify",
            body={"action": "generate", "max_repairs": [1]}, headers=headers,
        )

    assert verify[0] == 200, verify[2]
    assert default[0] == 200, default[2]
    assert revise[0] == 200, revise[2]
    assert harness[0] == 403 and "Sign in" in harness[2]
    assert bad[0] == 400
    by_action = [(name, kwargs["max_repairs"], kwargs["model"]) for name, kwargs in calls]
    # Guest feedback revisions get one repair round, exactly what
    # lean_verify.revise_with_feedback itself enforces for guests.
    assert by_action == [
        ("generate", lean_verify.GUEST_MAX_REPAIRS, "kimi-k3"),
        ("generate", lean_verify.GUEST_MAX_REPAIRS, "kimi-k3"),
        ("revise", console._GUEST_REVISE_MAX_REPAIRS, "kimi-k3"),
    ]
    assert lean_verify.GUEST_MAX_REPAIRS == 4
    assert console._GUEST_REVISE_MAX_REPAIRS == 1


def test_verify_refreshes_the_formal_stages_only_of_an_informal_only_run(
    guest_console, guest_derived_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_monitor import auto_pipeline, lean_verify

    workspace = tmp_path / "kimi_run"
    workspace.mkdir()
    refreshed: list[dict] = []
    monkeypatch.setattr(lean_verify, "generate", lambda **_kwargs: {"status": "verified"})

    def refresh_formal(**kwargs):
        refreshed.append(kwargs)
        if kwargs.get("on_exit"):
            kwargs["on_exit"]()
        return True

    monkeypatch.setattr(auto_pipeline, "refresh_formal", refresh_formal)

    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        _owned_guest_run(monkeypatch, workspace, guest)
        verify = {"action": "verify"}
        auto_pipeline._write_state(workspace, {"stages": {"lean": {"status": "done"}}})
        formalized = request(port, "POST", f"{APP_MOUNT}/api/run/kimi_run/lean_verify",
                             body=verify, headers={"Cookie": cookie})
        auto_pipeline._write_state(workspace, {"stages": {"lean": {"status": "off"}}})
        informal_only = request(port, "POST", f"{APP_MOUNT}/api/run/kimi_run/lean_verify",
                                body=verify, headers={"Cookie": cookie})

    assert formalized[0] == 200, formalized[2]
    assert informal_only[0] == 200, informal_only[2]
    assert [call["run_id"] for call in refreshed] == ["kimi_run"]


def test_signed_in_manual_lean_repairs_default_to_auto_and_cap_at_six(
    guest_console, guest_derived_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_monitor import auto_pipeline, lean_verify

    workspace = tmp_path / "own_run"
    workspace.mkdir()
    admin = auth.register("owner@example.test", "long-enough-password")
    token = auth.create_session(admin["id"])
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        lean_verify, "generate",
        lambda **kwargs: calls.append(("generate", kwargs["max_repairs"])) or {"status": "running"},
    )
    monkeypatch.setattr(
        lean_verify, "revise_with_feedback",
        lambda **kwargs: calls.append(("revise", kwargs["max_repairs"])) or {"status": "running"},
    )
    monkeypatch.setattr(auto_pipeline, "refresh_formal", lambda **_kwargs: None)
    monkeypatch.setattr(console, "_workspace_for_run", lambda _run_id: workspace)
    monkeypatch.setattr(console.Handler, "_owns_run", lambda _self, _run_id, _user: True)
    monkeypatch.setattr(console, "_run_record_for", lambda _run_id: {"owner_id": admin["id"]})

    with running_server() as port:
        headers = {"Cookie": f"{auth.SESSION_COOKIE}={token}"}
        for body in (
            {"action": "generate"},
            {"action": "generate", "max_repairs": 50},
            {"action": "generate", "max_repairs": -2},
            {"action": "revise", "message": "m"},
            {"action": "revise", "message": "m", "max_repairs": 50},
        ):
            status, _, raw = request(
                port, "POST", f"{APP_MOUNT}/api/run/own_run/lean_verify",
                body=body, headers=headers,
            )
            assert status == 200, raw

    assert calls == [
        ("generate", lean_verify.AUTO_MAX_REPAIRS),
        ("generate", 6),
        ("generate", 0),
        ("revise", 1),
        ("revise", 3),
    ]
    assert lean_verify.AUTO_MAX_REPAIRS == 4


def test_shared_profile_and_library_writes_are_operator_only(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    from agent_monitor import agent_config

    admin = auth.register("admin@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    assert admin["is_admin"] and not member["is_admin"]
    writes: list[str] = []
    monkeypatch.setattr(agent_config, "save_system_prompt", lambda text: writes.append("prompt"))
    monkeypatch.setattr(agent_config, "save_skill", lambda name, text: writes.append("skill"))
    monkeypatch.setattr(agent_config, "list_skills", lambda: [])
    monkeypatch.setattr(agent_config, "save_memory", lambda name, text: writes.append("memory"))
    monkeypatch.setattr(agent_config, "delete_skill", lambda name: writes.append("delete-skill"))
    monkeypatch.setattr(library, "upsert_item", lambda body: writes.append("library") or {"id": "x"})
    monkeypatch.setattr(library, "update_settings", lambda body: writes.append("settings") or {})
    monkeypatch.setattr(library, "delete_item", lambda item_id: writes.append("delete") or True)
    monkeypatch.setattr(agent_config, "get_agent_config", lambda: {"system_prompt": ""})
    monkeypatch.setattr(library, "get_library", lambda: {"items": []})
    checks = [
        ("POST", "/api/agent/config", {"system_prompt": "x"}),
        ("POST", "/api/agent/skills", {"name": "s", "content": "x"}),
        ("POST", "/api/agent/memory", {"name": "MEMORY.md", "content": "x"}),
        ("DELETE", "/api/agent/skills/s", None),
        ("DELETE", "/api/agent/memory/MEMORY.md", None),
        ("POST", "/api/library", {"type": "memory", "name": "x", "content": "x"}),
        ("POST", "/api/library/settings", {"auto_memory": True}),
        ("DELETE", "/api/library/item", None),
    ]

    with running_server() as port:
        member_cookie = {"Cookie": f"{auth.SESSION_COOKIE}={auth.create_session(member['id'])}"}
        admin_cookie = {"Cookie": f"{auth.SESSION_COOKIE}={auth.create_session(admin['id'])}"}
        for method, path, body in checks:
            status, _, raw = request(port, method, APP_MOUNT + path, body=body, headers=member_cookie)
            assert status == 403, f"{method} {path}: {status} {raw}"
        assert writes == []
        # Signed-in members may still read the shared profile and library.
        assert request(port, "GET", APP_MOUNT + "/api/agent/config", headers=member_cookie)[0] == 200
        assert request(port, "GET", APP_MOUNT + "/api/library", headers=member_cookie)[0] == 200
        for method, path, body in checks:
            status, _, raw = request(port, method, APP_MOUNT + path, body=body, headers=admin_cookie)
            assert status == 200, f"{method} {path}: {status} {raw}"
    assert len(writes) == len(checks)


def test_members_never_read_the_operators_run_records_in_the_library(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    admin = auth.register("admin@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    items = [
        {"id": "memory_run_codex_adhoc_1", "type": "memory", "name": "Run p1 (codex)",
         "content": "Problem: the operator's private problem", "source": "run:codex_adhoc_1"},
        {"id": "memory_notes", "type": "memory", "name": "Notes", "content": "Shared notes"},
        {"id": "skill_lean", "type": "skill", "name": "lean", "content": "Use Lean."},
    ]
    monkeypatch.setattr(library, "get_library", lambda: {
        "items": items, "counts": {"memory": 2, "skill": 1, "tool": 0}, "settings": {"auto_memory": True},
    })

    with running_server() as port:
        def read(account):
            cookie = {"Cookie": f"{auth.SESSION_COOKIE}={auth.create_session(account['id'])}"}
            status, _, raw = request(port, "GET", APP_MOUNT + "/api/library", headers=cookie)
            assert status == 200, raw
            return json.loads(raw)

        shown = read(member)
        everything = read(admin)
    assert [item["id"] for item in shown["items"]] == ["memory_notes", "skill_lean"]
    assert shown["counts"] == {"memory": 1, "skill": 1, "tool": 0}
    assert "private problem" not in json.dumps(shown)
    assert [item["id"] for item in everything["items"]] == [item["id"] for item in items]


def test_finished_guest_run_starts_its_derived_pipeline_immediately(
    guest_console, guest_derived_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_monitor import auto_pipeline, sponsored_kimi_client

    workspace = tmp_path / "kimi_run"
    workspace.mkdir()
    (workspace / "proof.md").write_text("# Claim\n\n" + "A finished guest proof. " * 20, encoding="utf-8")
    guest, _ = auth.create_guest_session()
    record = {
        "run_id": "kimi_run", "guest": True, "owner_id": guest["id"],
        "engine": "kimi", "status": "finished",
    }
    monkeypatch.setattr(console, "_run_record_for", lambda run_id: dict(record) if run_id == "kimi_run" else None)
    monkeypatch.setattr(console, "_workspace_for_run", lambda run_id: workspace if run_id == "kimi_run" else None)
    monkeypatch.setattr(sponsored_kimi_client, "configured", lambda: True)
    monkeypatch.setattr(settings, "hosted_guest_kimi_available", lambda: True)
    started: list[dict] = []
    monkeypatch.setattr(
        auto_pipeline, "start",
        lambda **kwargs: started.append(kwargs) or {"status": "running"},
    )
    assert jobs._GUEST_COMPLETION_HOOK is console._guest_run_completed

    jobs._notify_guest_completion("kimi_run", guest["id"], workspace)
    assert [(item["run_id"], item["owner_id"], item["model"], item["engine"]) for item in started] == [
        ("kimi_run", guest["id"], "kimi-k3", "kimi")
    ]
    # Counted like any guest derived action, then released.
    assert len(console._GUEST_DERIVED_WINDOWS[guest["id"]]) == 1
    assert console._GUEST_DERIVED_ACTIVE_USERS == set()

    # Another owner's id, an unfinished run, or a foreign workspace never starts one.
    jobs._notify_guest_completion("kimi_run", guest["id"] + 1, workspace)
    record["status"] = "running"
    jobs._notify_guest_completion("kimi_run", guest["id"], workspace)
    record["status"] = "finished"
    jobs._notify_guest_completion("kimi_run", guest["id"], tmp_path)
    assert len(started) == 1


def test_enabled_records_of_the_operators_runs_reach_only_the_operators_runs(tmp_path: Path):
    from unittest.mock import patch

    record = {"id": "memory_run_codex_adhoc_1", "type": "memory", "name": "Run p1 (codex)",
              "content": "Problem: the operator's private problem", "source": "run:codex_adhoc_1"}
    notes = {"id": "memory_notes", "type": "memory", "name": "Notes", "content": "Shared notes"}
    with patch.object(library, "enabled_items",
                      side_effect=lambda item_type=None: [record, notes] if item_type == "memory" else []):
        member_ctx = library.compose_context(run_memories=False)
        operator_ctx = library.compose_context()
        library.materialize(tmp_path / "member", run_memories=False)
        library.materialize(tmp_path / "operator")
    assert "private problem" not in member_ctx and "Shared notes" in member_ctx
    assert "private problem" in operator_ctx
    assert "private problem" not in (tmp_path / "member" / "_library" / "MEMORY.md").read_text()
    assert "private problem" in (tmp_path / "operator" / "_library" / "MEMORY.md").read_text()
