"""Exercise the console's password-reset HTTP routes and response safeguards."""

from __future__ import annotations

import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from agent_monitor import auth
from agent_monitor import console_server as console


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


def request(port: int, method: str, path: str, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = None if body is None else json.dumps(body)
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    data = response.read().decode("utf-8")
    result = response.status, {key.lower(): value for key, value in response.getheaders()}, data
    connection.close()
    return result


@pytest.fixture()
def private_console(monkeypatch, tmp_path):
    monkeypatch.setattr(console, "BASE_PATH", "/proof-deadbeef")
    monkeypatch.setattr(console, "PUBLIC_MODE", False)
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    yield
    auth._INITIALIZED = False


def test_prefixed_login_and_reset_serve_real_no_store_ui(private_console):
    with running_server() as port:
        for path in (
            "/proof-deadbeef/login",
            "/proof-deadbeef/reset-password?token=invalid-probe",
        ):
            status, headers, body = request(port, "GET", path)
            assert status == 200
            assert "Forgot password?" in body
            assert "PYTHON ROUTE CONFIRMED" not in body
            assert 'window.__PC_BASE__="/proof-deadbeef"' in body
            assert headers["cache-control"].startswith("no-store")

        status, _headers, body = request(port, "GET", "/login")
        assert status == 404
        assert body == "Not Found"


def test_dynamic_prefix_is_request_scoped_when_no_gate(monkeypatch):
    monkeypatch.setattr(console, "BASE_PATH", "")
    with running_server() as port:
        status, _headers, body = request(port, "GET", "/proof-cafe/login")
        assert status == 200
        assert 'window.__PC_BASE__="/proof-cafe"' in body
        status, _headers, body = request(port, "GET", "/login")
        assert status == 200
        assert 'window.__PC_BASE__=""' in body

    assert console._app_path("/proof-cafejunk/login", "") == ("/proof-cafejunk/login", "")


def test_forgot_uses_configured_origin_and_request_mount(private_console, monkeypatch):
    captured = []
    delivered = threading.Event()
    monkeypatch.setenv("AGENT_MONITOR_APP_URL", "https://moonshot.hailab.io")

    def capture(email, base_url):
        captured.append((email, base_url))
        delivered.set()

    monkeypatch.setattr(auth, "handle_forgot_password", capture)
    with running_server() as port:
        status, headers, body = request(
            port,
            "POST",
            "/proof-deadbeef/api/auth/forgot-password",
            body={"email": "USER@EXAMPLE.TEST", "base_url": "https://attacker.invalid"},
            headers={"Host": "attacker.invalid", "X-Forwarded-Host": "attacker.invalid"},
        )
        assert delivered.wait(timeout=2)
    assert status == 200
    assert json.loads(body)["success"] is True
    assert headers["cache-control"] == "no-store"
    assert captured == [
        ("user@example.test", "https://moonshot.hailab.io/proof-deadbeef")
    ]


def test_forgot_is_async_generic_and_throttled(private_console, monkeypatch):
    auth.register("queued-known@example.test", "old-password")
    monkeypatch.setenv("AGENT_MONITOR_APP_URL", "https://moonshot.hailab.io")
    mail_started = threading.Event()
    release_mail = threading.Event()
    mail_finished = threading.Event()
    sent = []

    def blocked_send(to_email, reset_link):
        sent.append((to_email, reset_link))
        mail_started.set()
        assert release_mail.wait(timeout=5)
        mail_finished.set()
        return True

    monkeypatch.setattr(auth, "_send_reset_email", blocked_send)
    with running_server() as port, ThreadPoolExecutor(max_workers=1) as pool:
        known_future = pool.submit(
            request,
            port,
            "POST",
            "/proof-deadbeef/api/auth/forgot-password",
            body={"email": "QUEUED-KNOWN@example.test"},
        )
        assert mail_started.wait(timeout=2)
        known = known_future.result(timeout=1)

        duplicate = request(
            port,
            "POST",
            "/proof-deadbeef/api/auth/forgot-password",
            body={"email": "queued-known@example.test"},
        )
        unknown = request(
            port,
            "POST",
            "/proof-deadbeef/api/auth/forgot-password",
            body={"email": "queued-unknown@example.test"},
        )
        assert (known[0], known[2]) == (duplicate[0], duplicate[2]) == (unknown[0], unknown[2])
        assert known[0] == 200
        assert json.loads(known[2])["message"] == "If that email exists, a reset link has been sent."

        release_mail.set()
        assert mail_finished.wait(timeout=2)
        console._PASSWORD_RESET_QUEUE.join()

    assert len(sent) == 1
    assert sent[0][0] == "queued-known@example.test"
    assert sent[0][1].startswith(
        "https://moonshot.hailab.io/proof-deadbeef/reset-password?token="
    )


def test_public_host_cannot_supply_reset_origin_when_app_url_missing(monkeypatch):
    monkeypatch.setattr(console, "BASE_PATH", "")
    monkeypatch.delenv("AGENT_MONITOR_APP_URL", raising=False)
    captured = []
    monkeypatch.setattr(
        auth,
        "handle_forgot_password",
        lambda email, base_url: captured.append((email, base_url)),
    )
    with running_server() as port:
        status, _headers, body = request(
            port,
            "POST",
            "/api/auth/forgot-password",
            body={"email": "victim@example.test", "base_url": "https://attacker.invalid"},
            headers={"Host": "attacker.invalid", "X-Forwarded-Host": "attacker.invalid"},
        )
    assert status == 200
    assert json.loads(body)["success"] is True
    assert captured == []


def test_verify_response_is_no_store_and_request_log_redacts_token(
    private_console,
    monkeypatch,
    capsys,
):
    secret = "token-must-not-appear-in-logs"
    monkeypatch.setattr(auth, "verify_reset_token", lambda _token: {"valid": False})
    with running_server() as port:
        status, headers, _body = request(
            port,
            "GET",
            f"/proof-deadbeef/api/auth/verify-reset-token?token={secret}",
        )
    assert status == 400
    assert headers["cache-control"] == "no-store"
    assert secret not in capsys.readouterr().out
