from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

import agent_monitor
from agent_monitor import auto_pipeline, jobs
from agent_monitor.public_kimi_gateway import PublicKimiGateway, _sanitize_payload
from agent_monitor.public_kimi_server import (
    SPONSORED_ENGINES,
    PublicAPIError,
    PublicKimiApplication,
    PublicKimiHandler,
    PublicKimiHTTPServer,
    PublicLimits,
    SponsorSessionStore,
    SubmissionLimiter,
    _bounded_regular_file,
    _install_worker_environment,
)
from agent_monitor.runners import metaharness_runner
from agent_monitor.runners import api_backend


class FakeJobs:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.statuses: dict[str, str] = {}
        self.deleted: list[str] = []

    def start_job(self, **kwargs):
        self.started.append(kwargs)
        run_id = f"{kwargs['engine']}_adhoc_test_{len(self.started)}"
        self.statuses[run_id] = "running"
        return {"run_id": run_id, "job_id": f"job{len(self.started)}", "status": "running"}

    def list_jobs(self):
        return [
            {"run_id": run_id, "status": status}
            for run_id, status in self.statuses.items()
        ]

    def reconcile_run_status(self, _run_id):
        return None

    def stop_run(self, run_id):
        self.statuses[run_id] = "stopped"
        return {"run_id": run_id, "status": "stopped"}

    def delete_run(self, run_id):
        self.deleted.append(run_id)
        self.statuses.pop(run_id, None)
        return {"run_id": run_id, "deleted": True}


def catalog():
    return [
        {"id": "plain", "available": True},
        {"id": "metaharness", "available": True},
        {"id": "codex", "available": True},
    ]


class PublicKimiApplicationTests(unittest.TestCase):
    def make_app(self, root: Path, fake: FakeJobs | None = None) -> tuple[PublicKimiApplication, FakeJobs]:
        manager = fake or FakeJobs()
        return (
            PublicKimiApplication(
                jobs=manager,
                data_dir=root,
                redactions=("real-secret-value", "local-secret-value"),
                engine_catalog=catalog,
            ),
            manager,
        )

    def test_config_is_fixed_and_exposes_only_vetted_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, _fake = self.make_app(Path(tmp))
            config = app.config()
        self.assertEqual(config["model"], {"id": "kimi-k3", "label": "Kimi K3"})
        self.assertEqual([item["id"] for item in config["harnesses"]], ["plain"])
        self.assertNotIn("max_iterations", config["limits"])
        self.assertEqual(config["limits"]["problem_characters"], 12_000)
        self.assertEqual(config["limits"]["max_output_tokens"], 8192)
        self.assertEqual(app.limits.gateway_concurrent_calls, 4)

    def test_start_accepts_exact_contract_and_binds_route_model_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, fake = self.make_app(Path(tmp))
            result = app.start(
                owner_id=7,
                session_key="session",
                ip="192.0.2.10",
                body={"engine": "plain", "problem": "Prove that 1 + 1 = 2."},
            )
            captured = fake.started[0]
            self.assertEqual(result["model"], "kimi-k3")
            self.assertEqual(captured["model"], "kimi-k3")
            self.assertEqual(captured["auth_route"], "api_key")
            self.assertEqual(captured["max_iterations"], 8)
            self.assertFalse(captured["use_subagents"])
            self.assertEqual(captured["user"]["id"], 7)
            for forbidden in ("model", "provider", "api_key", "max_iterations"):
                with self.assertRaises(PublicAPIError):
                    app.start(
                        owner_id=8,
                        session_key=f"other-{forbidden}",
                        ip="192.0.2.11",
                        body={
                            "engine": "plain",
                            "problem": "Prove it.",
                            forbidden: "attacker-value",
                        },
                    )

            with self.assertRaises(PublicAPIError):
                app.start(
                    owner_id=9,
                    session_key="other-metaharness",
                    ip="192.0.2.12",
                    body={"engine": "metaharness", "problem": "Prove it."},
                )


    def test_concurrency_is_partitioned_by_session_and_ip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, _fake = self.make_app(Path(tmp))
            app.start(
                owner_id=1,
                session_key="one",
                ip="192.0.2.1",
                body={"engine": "plain", "problem": "First problem"},
            )
            with self.assertRaisesRegex(PublicAPIError, "already active"):
                app.start(
                    owner_id=2,
                    session_key="two",
                    ip="192.0.2.1",
                    body={"engine": "plain", "problem": "Second problem"},
                )

    def test_parallel_start_admission_is_atomic(self) -> None:
        class SlowJobs(FakeJobs):
            def start_job(self, **kwargs):
                time.sleep(0.05)
                return super().start_job(**kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            app, fake = self.make_app(Path(tmp), SlowJobs())
            barrier = threading.Barrier(3)
            successes: list[dict] = []
            errors: list[PublicAPIError] = []

            def submit() -> None:
                barrier.wait()
                try:
                    successes.append(
                        app.start(
                            owner_id=1,
                            session_key="same-session",
                            ip="192.0.2.1",
                            body={"engine": "plain", "problem": "Concurrent problem"},
                        )
                    )
                except PublicAPIError as exc:
                    errors.append(exc)

            workers = [threading.Thread(target=submit) for _ in range(2)]
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(timeout=2)
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(errors), 1)
            self.assertEqual(len(fake.started), 1)

    def test_global_denials_do_not_allocate_unbounded_visitor_buckets(self) -> None:
        limiter = SubmissionLimiter(PublicLimits(global_requests_per_hour=2))
        limiter.admit("session-1", "ip-1")
        limiter.admit("session-2", "ip-2")
        for index in range(200):
            with self.assertRaises(PublicAPIError):
                limiter.admit(f"denied-session-{index}", f"denied-ip-{index}")
        self.assertEqual(len(limiter._sessions), 2)
        self.assertEqual(len(limiter._ips), 2)

    def test_redaction_removes_upstream_and_local_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, _fake = self.make_app(Path(tmp))
            rendered = app.redact(
                "real-secret-value local-secret-value sk_attackersecret Bearer abcdefghijklmnop"
            )
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("sk_attackersecret", rendered)
        self.assertNotIn("abcdefghijklmnop", rendered)

    def test_periodic_ttl_cleanup_deletes_expired_isolated_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, fake = self.make_app(root)
            expired = {
                "run_id": "plain_expired_abc",
                "owner_id": 1,
                "created_epoch": time.time() - 25 * 3600,
            }
            path = app.metadata_dir / "plain_expired_abc.json"
            path.write_text(json.dumps(expired), encoding="utf-8")
            app._last_expiry_check = 0
            app.config()
            self.assertEqual(fake.deleted, ["plain_expired_abc"])
            self.assertFalse(path.exists())

    def test_interrupted_internal_status_is_publicly_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, _fake = self.make_app(Path(tmp))
            started = app.start(
                owner_id=7,
                session_key="visitor",
                ip="192.0.2.7",
                body={"engine": "plain", "problem": "Prove it"},
            )
            with patch.object(
                app,
                "_record",
                return_value={"status": "interrupted", "agents": []},
            ):
                result = app.status(run_id=started["run_id"], owner_id=7)
        self.assertEqual(result["status"], "failed")
        self.assertIn("interrupted", str(result["error"]).lower())

    def test_status_schema_redacts_events_and_ignores_symlink_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, _fake = self.make_app(root)
            started = app.start(
                owner_id=7,
                session_key="visitor",
                ip="192.0.2.7",
                body={"engine": "plain", "problem": "Prove it"},
            )
            runs = root / "runs"
            workspace = runs / "workspaces" / started["run_id"]
            workspace.mkdir(parents=True)
            outside = root / "outside-secret"
            outside.write_text("real-secret-value", encoding="utf-8")
            (workspace / "proof.md").symlink_to(outside)
            record = {
                "status": "finished",
                "auth_route": "api_key",
                "created_at": "created",
                "updated_at": "updated",
                "private_field": "must not escape",
                "agents": [
                    {
                        "stage_name": "solver",
                        "role": "agent",
                        "status": "finished",
                        "output": "answer real-secret-value",
                        "private": "must not escape",
                    }
                ],
            }
            with (
                patch.object(app, "_record", return_value=record),
                patch.object(agent_monitor, "RUNS_DIR", runs),
            ):
                result = app.status(run_id=started["run_id"], owner_id=7)
        self.assertEqual(
            set(result),
            {
                "run_id", "status", "engine", "model", "created_at",
                "updated_at", "error", "proof", "events",
            },
        )
        self.assertEqual(result["proof"], "")
        self.assertEqual(
            set(result["events"][0]),
            {"stage", "role", "title", "status", "output", "text"},
        )
        self.assertNotIn("real-secret-value", json.dumps(result))


class PublicKimiBoundaryTests(unittest.TestCase):
    def test_http_surface_cookie_continuity_owner_isolation_and_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"PUBLIC_KIMI_COOKIE_SECURE": "1"}, clear=False
        ):
            app = PublicKimiApplication(
                jobs=FakeJobs(),
                data_dir=Path(tmp),
                redactions=("secret",),
                engine_catalog=catalog,
            )
            server = PublicKimiHTTPServer(("127.0.0.1", 0), app, "")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            port = int(server.server_address[1])

            def request(method: str, path: str, body: bytes = b"", headers: dict | None = None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                payload = response.read()
                result = (response.status, dict(response.getheaders()), payload)
                connection.close()
                return result

            try:
                status, headers, _body = request("GET", "/api/config")
                self.assertEqual(status, 200)
                cookie = headers["Set-Cookie"]
                self.assertIn("HttpOnly", cookie)
                self.assertIn("SameSite=Strict", cookie)
                self.assertIn("Secure", cookie)
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
                cookie_pair = cookie.split(";", 1)[0]
                encoded = json.dumps({"engine": "plain", "problem": "Prove it"}).encode()
                common = {
                    "Content-Type": "application/json",
                    "Cookie": cookie_pair,
                    "Origin": f"http://127.0.0.1:{port}",
                }
                status, _headers, body = request("POST", "/api/runs", encoded, common)
                self.assertEqual(status, 202)
                run_id = json.loads(body)["run_id"]
                with patch.object(app, "_record", return_value={"status": "running", "agents": []}):
                    status, _headers, _body = request(
                        "GET", f"/api/runs/{run_id}", headers={"Cookie": cookie_pair}
                    )
                    self.assertEqual(status, 200)
                    status, _headers, _body = request("GET", f"/api/runs/{run_id}")
                    self.assertEqual(status, 404)
                status, _headers, _body = request(
                    "POST",
                    "/api/runs",
                    encoded,
                    {**common, "Origin": "https://attacker.example"},
                )
                self.assertEqual(status, 403)
                status, _headers, _body = request("GET", "/api/settings")
                self.assertEqual(status, 404)
                status, _headers, _body = request("OPTIONS", "/api/config")
                self.assertEqual(status, 405)
                status, _headers, _body = request(
                    "POST",
                    "/api/runs",
                    b"x" * (32 * 1024 + 1),
                    {"Content-Type": "application/json", "Cookie": cookie_pair},
                )
                self.assertEqual(status, 413)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_bounded_file_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "key"
            target.write_text("x" * 32, encoding="utf-8")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                _bounded_regular_file(link)

    def test_slow_body_times_out_while_health_remains_responsive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "agent_monitor.public_kimi_server._REQUEST_BODY_TIMEOUT_SECONDS", 1.0
        ):
            fake = FakeJobs()
            app = PublicKimiApplication(
                jobs=fake,
                data_dir=Path(tmp),
                redactions=("secret",),
                engine_catalog=catalog,
            )
            server = PublicKimiHTTPServer(("127.0.0.1", 0), app, "")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            port = int(server.server_address[1])
            slow = socket.create_connection(("127.0.0.1", port), timeout=2)
            try:
                slow.sendall(
                    (
                        "POST /api/runs HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        "Content-Type: application/json\r\n"
                        "Content-Length: 1024\r\n"
                        "Connection: keep-alive\r\n\r\n"
                        '{"engine":"plain"'
                    ).encode("ascii")
                )
                time.sleep(0.05)

                health = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
                health.request("GET", "/healthz")
                response = health.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                health.close()

                slow.settimeout(2)
                received = bytearray()
                while True:
                    chunk = slow.recv(4096)
                    if not chunk:
                        break
                    received.extend(chunk)
                self.assertIn(b"HTTP/1.1 408", received)
                self.assertIn(b"Connection: close", received)
                self.assertIn(b"Request body timed out", received)
                self.assertEqual(fake.started, [])
            finally:
                slow.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_worker_environment_scrubs_real_credentials_and_credential_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "CREDENTIALS_DIRECTORY": "/run/credentials/secret",
                "KIMI_API_KEY": "real-secret-value",
                "OPENAI_API_KEY": "other-secret",
                "CODEX_HOME": "/private/codex",
                "METAHARNESS_CMD": "/untrusted/override",
                "DEEPAGENTS_CMD": "/untrusted/deepagents",
                "SURPRISE_API_KEY": "unknown-provider-secret",
                "SURPRISE_AUTH_TOKEN": "unknown-auth-secret",
                "HTTPS_PROXY": "http://untrusted-proxy",
            },
            clear=False,
        ):
            runtime = _install_worker_environment(
                Path(tmp), token="local-secret-value", base_url="http://127.0.0.1:1234/v1"
            )
            text = runtime.read_text(encoding="utf-8")
            self.assertEqual(os.environ["KIMI_API_KEY"], "local-secret-value")
            self.assertNotIn("CREDENTIALS_DIRECTORY", os.environ)
            self.assertNotIn("OPENAI_API_KEY", os.environ)
            self.assertNotIn("CODEX_HOME", os.environ)
            self.assertNotIn("METAHARNESS_CMD", os.environ)
            self.assertNotIn("DEEPAGENTS_CMD", os.environ)
            self.assertNotIn("SURPRISE_API_KEY", os.environ)
            self.assertNotIn("SURPRISE_AUTH_TOKEN", os.environ)
            self.assertNotIn("HTTPS_PROXY", os.environ)
            self.assertNotIn("real-secret-value", text)
            self.assertIn("KIMI_REASONING_EFFORT=high", text)

    def test_proxy_header_is_trusted_only_from_loopback_and_only_as_one_ip(self) -> None:
        handler = object.__new__(PublicKimiHandler)
        handler.headers = Message()
        handler.headers["X-Public-Client-IP"] = "198.51.100.8"
        handler.client_address = ("127.0.0.1", 1234)
        self.assertEqual(handler._client_ip(), "198.51.100.8")
        handler.client_address = ("203.0.113.9", 1234)
        self.assertEqual(handler._client_ip(), "203.0.113.9")
        handler.client_address = ("127.0.0.1", 1234)
        handler.headers.replace_header("X-Public-Client-IP", "198.51.100.8, 10.0.0.1")
        self.assertEqual(handler._client_ip(), "127.0.0.1")


class SponsoredSessionTests(unittest.TestCase):
    def test_default_capacity_covers_every_advertised_scope(self) -> None:
        limits = PublicLimits()
        self.assertGreaterEqual(limits.sponsor_tokens_per_ip, len(SPONSORED_ENGINES))
        self.assertGreaterEqual(
            limits.sponsor_token_starts_per_hour, len(SPONSORED_ENGINES)
        )
        self.assertGreaterEqual(
            limits.sponsor_tokens_per_ip, limits.sponsor_token_starts_per_hour
        )
        store = SponsorSessionStore(limits)
        tokens = [
            store.issue(engine=engine, ip="198.18.1.20")[0]
            for engine in sorted(SPONSORED_ENGINES)
        ]
        self.assertEqual(len(tokens), len(SPONSORED_ENGINES))
        self.assertEqual(len(set(tokens)), len(tokens))

    def test_session_is_engine_bound_short_lived_and_call_capped(self) -> None:
        store = SponsorSessionStore(
            PublicLimits(sponsor_calls_per_token=2, sponsor_tokens_per_ip=2)
        )
        token, ttl = store.issue(engine="openclaude", ip="198.18.1.2")
        self.assertGreaterEqual(ttl, 60)
        self.assertEqual(store.authorize(token, charge=False), "openclaude")
        self.assertEqual(store.authorize(token, charge=True), "openclaude")
        self.assertEqual(store.authorize(token, charge=True), "openclaude")
        with self.assertRaisesRegex(PublicAPIError, "call quota"):
            store.authorize(token, charge=True)
        with self.assertRaisesRegex(PublicAPIError, "authorization required"):
            store.authorize("not-a-token", charge=False)

    def test_session_rejects_unattested_engine_and_bounds_active_tokens(self) -> None:
        store = SponsorSessionStore(PublicLimits(sponsor_tokens_per_ip=1))
        with self.assertRaisesRegex(PublicAPIError, "Unsupported"):
            store.issue(engine="hermes", ip="198.18.1.3")
        store.issue(engine="deepagents", ip="198.18.1.3")
        with self.assertRaisesRegex(PublicAPIError, "Too many"):
            store.issue(engine="openclaw", ip="198.18.1.3")

    def test_internal_http_session_models_and_tool_chat_use_only_gateway_token(self) -> None:
        captured: dict = {}

        class Upstream:
            status = code = 200

            def __init__(self) -> None:
                self.headers = Message()
                self.headers["Content-Type"] = "application/json"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _size=-1):
                return b'{"id":"chatcmpl-test","choices":[]}'

        def urlopen(request, **_kwargs):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["payload"] = json.loads(request.data)
            return Upstream()

        with tempfile.TemporaryDirectory() as tmp:
            app = PublicKimiApplication(
                jobs=FakeJobs(),
                data_dir=Path(tmp),
                redactions=("upstream-provider-key",),
                engine_catalog=catalog,
            )
            server = PublicKimiHTTPServer(
                ("127.0.0.1", 0),
                app,
                "",
                gateway_base_url="http://127.0.0.1:19999/v1",
                gateway_token="gateway-local-token",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            connection = http.client.HTTPConnection(host, port, timeout=3)
            try:
                body = json.dumps({"engine": "openclaude"})
                connection.request(
                    "POST",
                    "/internal/sessions",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                issued = json.loads(response.read())
                self.assertEqual(response.status, 201)
                token = issued["api_token"]
                self.assertEqual(
                    issued["base_url"], f"http://{host}:{port}/internal/v1"
                )

                connection.request(
                    "GET",
                    "/internal/v1/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
                response = connection.getresponse()
                models = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual([item["id"] for item in models["data"]], ["kimi-k3"])

                tool = {
                    "type": "function",
                    "function": {"name": "write_file", "parameters": {"type": "object"}},
                }
                with patch(
                    "agent_monitor.public_kimi_server.urlrequest.urlopen",
                    side_effect=urlopen,
                ):
                    connection.request(
                        "POST",
                        "/internal/v1/chat/completions",
                        body=json.dumps(
                            {
                                "model": "kimi-k3",
                                "messages": [{"role": "user", "content": "prove"}],
                                "tools": [tool],
                            }
                        ),
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                    )
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(captured["authorization"], "Bearer gateway-local-token")
        self.assertEqual(captured["payload"]["tools"], [tool])
        self.assertNotIn("upstream-provider-key", repr(captured))


class PublicKimiGatewayTests(unittest.TestCase):
    def test_sanitizer_is_chat_only_toolless_fixed_model_and_always_capped(self) -> None:
        clean = _sanitize_payload(
            {"model": "attacker", "messages": [{"role": "user", "content": "prove"}]},
            reasoning_effort="high",
            max_output_tokens=8192,
        )
        self.assertEqual(clean["model"], "kimi-k3")
        self.assertEqual(clean["reasoning_effort"], "high")
        self.assertEqual(clean["max_tokens"], 8192)
        capped = _sanitize_payload(
            {
                "model": "kimi-k3",
                "messages": [{"role": "user", "content": "prove"}],
                "max_tokens": 99_999,
            },
            reasoning_effort="high",
            max_output_tokens=8192,
        )
        self.assertEqual(capped["max_tokens"], 8192)
        tool = {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write one workspace file",
                "parameters": {"type": "object"},
            },
        }
        tooling = _sanitize_payload(
            {
                "model": "attacker",
                "messages": [{"role": "user", "content": "prove"}],
                "tools": [tool],
                "stream": True,
            },
            reasoning_effort="high",
            max_output_tokens=8192,
        )
        self.assertEqual(tooling["tools"], [tool])
        self.assertTrue(tooling["stream"])

    @staticmethod
    def _request(gateway: PublicKimiGateway, token: str, body: dict) -> tuple[int, bytes, dict]:
        host_port = gateway.base_url.removeprefix("http://").removesuffix("/v1")
        host, port = host_port.rsplit(":", 1)
        connection = http.client.HTTPConnection(host, int(port), timeout=3)
        encoded = json.dumps(body).encode("utf-8")
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=encoded,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        output = response.read()
        headers = dict(response.getheaders())
        status = response.status
        connection.close()
        return status, output, headers

    def test_gateway_substitutes_real_key_and_never_forwards_non_2xx_body(self) -> None:
        class Upstream:
            def __init__(self, status: int, body: bytes, headers: dict | None = None) -> None:
                self.status = self.code = status
                self.body = body
                self.headers = headers or {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, limit: int = -1):
                return self.body if limit < 0 else self.body[:limit]

        captured: list = []

        def rejected(request, **_kwargs):
            captured.append(request)
            return Upstream(
                401,
                b'{"error":"account real-upstream-secret billing detail"}',
                {"Retry-After": "17"},
            )

        gateway = PublicKimiGateway(
            upstream_base="https://kimi.example/v1",
            upstream_api_key="real-upstream-secret",
            local_token="local-loopback-token",
            calls_per_hour=10,
        )
        with patch("agent_monitor.public_kimi_gateway.urlrequest.urlopen", side_effect=rejected):
            with gateway:
                status, body, headers = self._request(
                    gateway,
                    "local-loopback-token",
                    {"model": "other", "messages": [{"role": "user", "content": "prove"}]},
                )
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("Retry-After"), "17")
        self.assertNotIn(b"real-upstream-secret", body)
        self.assertNotIn(b"billing", body)
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer real-upstream-secret")
        forwarded = json.loads(captured[0].data)
        self.assertEqual(forwarded["model"], "kimi-k3")
        self.assertEqual(forwarded["max_tokens"], 8192)


class PublicKimiExecutionGuardTests(unittest.TestCase):
    def test_public_reconciliation_never_calls_auto_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"AGENT_MONITOR_DISABLE_AUTO_PIPELINE": "1"}, clear=False
        ):
            root = Path(tmp)
            cache_dir = root / "cache"
            runs_dir = root / "runs"
            terminal_workspace = runs_dir / "workspaces" / "plain_terminal"
            stale_workspace = runs_dir / "workspaces" / "plain_stale"
            terminal_workspace.mkdir(parents=True)
            stale_workspace.mkdir(parents=True)
            terminal = {
                "run_id": "plain_terminal",
                "engine": "plain",
                "status": "finished",
                "updated_at": "2026-08-21T00:00:00+00:00",
                "workspace": str(terminal_workspace),
                "agents": [],
                "pipeline": [],
                "edges": [],
                "totals": {},
            }
            stale = {
                **terminal,
                "run_id": "plain_stale",
                "status": "running",
                "workspace": str(stale_workspace),
                "agents": [{"status": "running"}],
            }
            with (
                patch.object(jobs, "CACHE_DIR", cache_dir),
                patch.object(jobs, "RUNS_DIR", runs_dir),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(auto_pipeline, "reconcile_orphaned") as orphaned,
                patch.object(auto_pipeline, "mark_interrupted") as interrupted,
            ):
                jobs._write_run(terminal)
                jobs._write_run(stale)
                terminal_result = jobs.reconcile_run_status(
                    "plain_terminal", min_age_seconds=0
                )
                stale_result = jobs.reconcile_run_status(
                    "plain_stale", min_age_seconds=0
                )
            self.assertEqual(terminal_result["status"], "finished")
            self.assertEqual(stale_result["status"], "interrupted")
            orphaned.assert_not_called()
            interrupted.assert_not_called()

    def test_public_api_backend_never_retries_ambiguous_kimi_failure(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_MONITOR_PUBLIC_KIMI": "1",
                "KIMI_API_KEY": "local-loopback-token",
                "KIMI_API_BASE": "http://127.0.0.1:1234/v1",
                "KIMI_REASONING_EFFORT": "high",
            },
            clear=False,
        ), patch(
            "agent_monitor.settings._http_json",
            side_effect=[(503, {"error": "ambiguous"}), (200, {"choices": []})],
        ) as request:
            with self.assertRaises(api_backend.APIBackendError):
                api_backend.api_chat("system", "problem", model="kimi-k3")
        self.assertEqual(request.call_count, 1)

    def test_start_job_public_flag_never_calls_auto_pipeline(self) -> None:
        class FakeThread:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                return None

            def is_alive(self):
                return True

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {"AGENT_MONITOR_DISABLE_AUTO_PIPELINE": "1"},
                clear=False,
            ),
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
            patch(
                "agent_monitor.engines_registry.list_engines",
                return_value=[
                    {
                        "id": "plain",
                        "label": "Plain",
                        "available": True,
                        "auth_modes": ["api_key"],
                    }
                ],
            ),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(
                jobs,
                "_user_extra_env",
                return_value={
                    "KIMI_API_KEY": "local-loopback-token",
                    "KIMI_API_BASE": "http://127.0.0.1:1234/v1",
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
                },
            ),
            patch.object(jobs, "workspace_dir", return_value=Path(tmp)),
            patch.object(jobs, "_write_run"),
            patch("agent_monitor.jobs.threading.Thread", FakeThread),
            patch("agent_monitor.auto_pipeline.start") as derived,
        ):
            job = jobs.start_job(
                engine="plain",
                problem_text="Prove it.",
                model="kimi-k3",
                max_iterations=8,
                user={"id": 7, "is_admin": True},
                use_subagents=False,
                auth_route="api_key",
            )
        derived.assert_not_called()
        with jobs._LOCK:
            jobs._JOBS.pop(job["job_id"], None)
            jobs._STOP_EVENTS.pop(job["run_id"], None)

    def test_auto_pipeline_flag_returns_before_thread_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"AGENT_MONITOR_DISABLE_AUTO_PIPELINE": "1"}, clear=False
        ), patch("agent_monitor.auto_pipeline.threading.Thread") as thread:
            result = auto_pipeline.start(
                run_id="plain_public_guard",
                workspace=Path(tmp),
                owner_id=1,
                model="kimi-k3",
                engine="plain",
            )
            self.assertEqual(result["status"], "disabled")
            thread.assert_not_called()
            self.assertFalse((Path(tmp) / auto_pipeline.STATE_FILENAME).exists())

    def test_public_job_skips_library_memory_and_feedback_paths(self) -> None:
        fake_run = {
            "run_id": "plain_public_guard",
            "status": "finished",
            "agents": [],
            "totals": {},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ, {"AGENT_MONITOR_PUBLIC_KIMI": "1"}, clear=False
            ),
            patch("agent_monitor.library.materialize") as materialize,
            patch("agent_monitor.library.record_run_memory") as memory,
            patch.object(jobs, "_run_cli_engine_with_openclaude_retry", return_value=fake_run),
            patch.object(jobs, "_append_chat"),
            patch.object(jobs, "_write_run"),
            patch.object(jobs, "_update_job"),
            patch.object(jobs, "_launch_pending_feedback") as feedback,
        ):
            jobs._execute_job(
                job_id="job",
                run_id="plain_public_guard",
                engine="plain",
                problem_id="p",
                problem_text="prove it",
                problem_path=None,
                model="kimi-k3",
                max_iterations=8,
                workspace=tmp,
                extra_env={"KIMI_API_KEY": "local"},
                use_subagents=False,
                owner_id=1,
            )
        materialize.assert_not_called()
        memory.assert_not_called()
        feedback.assert_not_called()

    def test_public_metaharness_is_two_rounds_five_calls_and_no_audit(self) -> None:
        calls: list[str] = []

        def fake_chat(_client, _model, system, _user, **_kwargs):
            calls.append(system)
            if system == metaharness_runner.EVALUATOR_SYSTEM:
                return "SCORE: 0\nneeds work", {}
            if system == metaharness_runner.PROPOSER_SYSTEM:
                return "improved harness", {}
            return "# Proof\n\nA bounded candidate proof. ∎", {}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "AGENT_MONITOR_PUBLIC_KIMI": "1",
                    "METAHARNESS_ROUNDS": "99",
                    "METAHARNESS_MODEL": "kimi-k3",
                },
                clear=False,
            ),
            patch.object(metaharness_runner.sys, "argv", ["metaharness_runner.py", "candidate audit gate: prove it"]),
            patch.object(metaharness_runner, "chat", side_effect=fake_chat),
            patch.object(metaharness_runner, "emit"),
            patch.object(metaharness_runner, "audit_requested", return_value=True),
            patch.object(metaharness_runner, "complete_file_backed_audit") as audit,
            patch.object(metaharness_runner, "ensure_requested_outcome", side_effect=lambda _p, text: text),
        ):
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                result = metaharness_runner.main()
            finally:
                os.chdir(previous)
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 5)
        audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
