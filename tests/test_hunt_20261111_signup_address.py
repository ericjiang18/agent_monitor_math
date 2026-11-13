"""Fixes from the 2026-11-11 bug hunt of sign-up addresses: a crafted
address is refused at once instead of stalling the console, a new account's
address is one plain mailbox (the one its mail goes to), a sign-up name is
bounded, and an account stored under an older, looser rule still signs in."""

from __future__ import annotations

import secrets
import time
from contextlib import closing

import pytest

from agent_monitor import auth

_ENV = (
    "AGENT_MONITOR_APP_URL",
    "AGENT_MONITOR_BASE_PATH",
    "AGENT_MONITOR_REGISTRATION_OPEN",
    "AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES",
    "AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION",
    "SMTP_SERVER",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM_EMAIL",
)
PASSWORD = "long-enough-password"


@pytest.fixture()
def isolated_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    yield
    auth._INITIALIZED = False


def test_a_crafted_sign_up_address_is_refused_at_once(isolated_auth):
    # "a@...@" made the address pattern backtrack quadratically while holding the GIL.
    address = "a@" + "." * 60_000 + "@"
    started = time.monotonic()
    assert auth._EMAIL_RE.match(address) is None
    with pytest.raises(ValueError):
        auth.register(address, PASSWORD, require_open=True)
    assert time.monotonic() - started < 2.0


def test_the_address_pattern_still_accepts_and_refuses_what_it_did():
    for address in ("a@b.c", "member@example.test", "x.y+z@cs.ucla.edu", "a@.b.c", "a@...", "a@b..c"):
        assert auth._EMAIL_RE.match(address), address
    for address in ("", "a@b", "a@b.", "a@.b", "@b.c", "a@b@c.d", "a b@c.d", "a@b .c", "a@..", "a@b.c@"):
        assert not auth._EMAIL_RE.match(address), address


def test_a_sign_up_address_longer_than_an_address_can_be_is_refused(isolated_auth):
    for address in ("a" * 900_000 + "@example.test", "a@" + "b" * 250 + ".test"):
        with pytest.raises(ValueError):
            auth.register(address, PASSWORD, require_open=True)


def test_a_sign_up_address_that_mail_would_deliver_elsewhere_is_refused(isolated_auth, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES", ".edu")
    # The mail library reads "(x.edu" as a comment: the mail goes to attacker@evil.example.
    with pytest.raises(ValueError):
        auth.register("attacker@evil.example(x.edu", PASSWORD, require_open=True)
    monkeypatch.delenv("AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES")
    for address in ("x<victim@target.example>", "['member@example.test']",
                    "mеmber@example.test", "a,b@example.test", '"a b"@example.test',
                    "a" * 65 + "@example.test"):
        with pytest.raises(ValueError):
            auth.register(address, PASSWORD, require_open=True)
        # An account an operator creates is held to the same rule.
        with pytest.raises(ValueError):
            auth.register(address, PASSWORD)
    account = auth.register("Student.Name+tag@cs.ucla.edu", PASSWORD, require_open=True)
    assert account["email"] == "student.name+tag@cs.ucla.edu"


def test_a_sign_up_name_is_trimmed_to_120_characters(isolated_auth):
    account = auth.register("verbose@example.test", PASSWORD, name="  " + "N" * 900_000, require_open=True)
    assert account["name"] == "N" * 120
    account = auth.register("short@example.test", PASSWORD, name=" Ada Lovelace ", require_open=True)
    assert account["name"] == "Ada Lovelace"


def test_an_account_stored_under_the_older_address_rule_still_signs_in(isolated_auth, monkeypatch):
    address = "old_style@under_score.example"
    salt = secrets.token_bytes(16)
    with closing(auth._conn()) as conn, conn:
        conn.execute(
            "INSERT INTO users (email, name, password_hash, salt, is_admin, is_guest, created_at,"
            " email_verified_at) VALUES (?,?,?,?,?,?,?,?)",
            (address, "Old", auth._hash_password(PASSWORD, salt), salt, 0, 0, auth._now(), auth._now()),
        )
    assert auth.login(address, PASSWORD)["email"] == address
    monkeypatch.setenv("AGENT_MONITOR_APP_URL", "https://ansatze.test")
    sent = []
    monkeypatch.setattr(auth, "_send_reset_email", lambda to, link: sent.append(to) or True)
    assert auth.handle_forgot_password(address)
    assert sent == [address]
