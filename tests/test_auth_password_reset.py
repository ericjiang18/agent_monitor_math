"""Exercise reset-token security, concurrency, mail, and canonical-link behavior."""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_monitor import auth


@pytest.fixture()
def isolated_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    for name in (
        "AGENT_MONITOR_APP_URL",
        "AGENT_MONITOR_BASE_PATH",
        "SMTP_SERVER",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    auth._INITIALIZED = False


def _issue(monkeypatch, email: str, base: str = "https://example.test/proof-deadbeef") -> str:
    monkeypatch.setattr(auth, "_send_reset_email", lambda _to, _link: True)
    token = auth.handle_forgot_password(email, base)
    assert token
    return token


def test_full_reset_lifecycle_hashes_token_and_revokes_sessions(isolated_auth, monkeypatch):
    account = auth.register("user@example.test", "old-password")
    old_session = auth.create_session(account["id"])
    first_token = _issue(monkeypatch, account["email"])
    second_token = _issue(monkeypatch, account["email"])

    with auth._conn() as conn:
        rows = conn.execute("SELECT token_hash FROM password_resets").fetchall()
    assert [row["token_hash"] for row in rows] == [hashlib.sha256(second_token.encode()).hexdigest()]
    assert first_token not in rows[0]["token_hash"]
    assert auth.handle_reset_password(first_token, "ignored-password") is False

    assert auth.handle_reset_password(second_token, "new-password") is True
    assert auth.handle_reset_password(second_token, "another-password") is False
    assert auth.user_for_token(old_session) is None
    with pytest.raises(ValueError, match="Incorrect email or password"):
        auth.login(account["email"], "old-password")
    assert auth.login(account["email"], "new-password")["id"] == account["id"]


def test_concurrent_token_use_has_exactly_one_winner(isolated_auth, monkeypatch):
    account = auth.register("race@example.test", "old-password")
    token = _issue(monkeypatch, account["email"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda password: auth.handle_reset_password(token, password),
                ("winner-one", "winner-two"),
            )
        )

    assert sorted(results) == [False, True]
    successful_passwords = []
    for password in ("winner-one", "winner-two"):
        try:
            auth.login(account["email"], password)
        except ValueError:
            continue
        successful_passwords.append(password)
    assert len(successful_passwords) == 1


def test_inflight_old_password_login_session_is_deleted_by_reset(isolated_auth, monkeypatch):
    account = auth.register("login-race@example.test", "old-password")
    token = _issue(monkeypatch, account["email"])
    original_hash = auth._hash_password
    login_hash_started = threading.Event()
    release_login_hash = threading.Event()

    def blocking_hash(password: str, salt: bytes) -> bytes:
        if password == "old-password" and not login_hash_started.is_set():
            login_hash_started.set()
            assert release_login_hash.wait(timeout=5)
        return original_hash(password, salt)

    monkeypatch.setattr(auth, "_hash_password", blocking_hash)
    with ThreadPoolExecutor(max_workers=2) as pool:
        login_future = pool.submit(auth.login_and_create_session, account["email"], "old-password")
        assert login_hash_started.wait(timeout=5)
        reset_future = pool.submit(auth.handle_reset_password, token, "new-password")
        release_login_hash.set()
        _account, session = login_future.result(timeout=10)
        assert reset_future.result(timeout=10) is True

    assert auth.user_for_token(session) is None


def test_delivery_failure_removes_token_and_never_logs_link(isolated_auth, monkeypatch, caplog, capsys):
    account = auth.register("mail-failure@example.test", "old-password")
    secret = "fixed-secret-reset-token"
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _size: secret)
    caplog.set_level(logging.DEBUG)

    assert auth.handle_forgot_password(account["email"], "https://example.test") is None
    with auth._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM password_resets").fetchone()[0] == 0
    output = capsys.readouterr().out + "\n" + caplog.text
    assert secret not in output
    assert "reset-password?token=" not in output


def test_smtp_uses_timeout_and_verified_tls_context(isolated_auth, monkeypatch):
    sentinel_context = object()
    calls = {}

    class FakeSMTP:
        def __init__(self, server, port, timeout):
            calls.update(server=server, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def ehlo(self):
            calls["ehlo"] = calls.get("ehlo", 0) + 1

        def starttls(self, *, context):
            calls["context"] = context

        def login(self, user, password):
            calls.update(user=user, password=password)

        def send_message(self, message):
            calls["message"] = message

    monkeypatch.setenv("SMTP_SERVER", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "mailer")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setattr(auth.ssl, "create_default_context", lambda: sentinel_context)
    monkeypatch.setattr(auth.smtplib, "SMTP", FakeSMTP)

    assert auth._send_reset_email(
        "user@example.test",
        "https://example.test/reset-password?token=secret",
    ) is True
    assert calls["timeout"] == 15
    assert calls["context"] is sentinel_context
    assert calls["ehlo"] == 2


def test_invalid_and_expired_rows_are_rejected_and_cleaned(isolated_auth, monkeypatch):
    account = auth.register("expiry@example.test", "old-password")
    malformed = _issue(monkeypatch, account["email"])
    malformed_hash = hashlib.sha256(malformed.encode()).hexdigest()
    with auth._conn() as conn:
        conn.execute(
            "UPDATE password_resets SET expires_at='not-a-date' WHERE token_hash=?",
            (malformed_hash,),
        )
    assert auth.verify_reset_token(malformed)["valid"] is False
    with auth._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM password_resets WHERE token_hash=?",
            (malformed_hash,),
        ).fetchone()[0] == 0
