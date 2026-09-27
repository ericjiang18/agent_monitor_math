"""The console receives attested broker tokens, never provider credentials."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_monitor import sponsored_kimi_client as broker


@pytest.fixture(autouse=True)
def isolated_bridge(monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_SPONSORED_KIMI", "1")
    monkeypatch.setenv("AGENT_MONITOR_SPONSORED_KIMI_URL", "http://127.0.0.1:4610")
    broker._credential_cache.clear()
    monkeypatch.setattr(broker, "_health_cache", (0.0, "", False))


@pytest.mark.parametrize("endpoint", ["https://remote.example:443", "http://127.0.0.1:4610/other", "http://user@localhost:4610", "http://127.0.0.1:4610?next=elsewhere"])
def test_broker_rejects_non_attested_destinations(monkeypatch, endpoint):
    monkeypatch.setenv("AGENT_MONITOR_SPONSORED_KIMI_URL", endpoint)
    with pytest.raises(broker.SponsoredKimiError):
        broker.endpoint()


def test_broker_credentials_are_bound_to_model_engine_origin_and_ttl(monkeypatch):
    valid = {"api_token": "a" * 64, "base_url": "http://127.0.0.1:4610/internal/v1", "model": "kimi-k3", "engine": "plain", "expires_in": 7200}
    for mismatch in ({"model": "gpt-6-astra"}, {"engine": "codex"}, {"base_url": "https://remote.example"}, {"expires_in": 30}):
        monkeypatch.setattr(broker, "_request", lambda *args, **kwargs: ({**valid, **mismatch}, ""))
        with pytest.raises(broker.SponsoredKimiError, match="attestation"):
            broker.issue_credentials(engine="plain", client_id=1, minimum_ttl_seconds=600)
    monkeypatch.setattr(broker, "_request", lambda *args, **kwargs: (valid, ""))
    env = broker.issue_credentials(engine="plain", client_id=1, minimum_ttl_seconds=600)
    assert env["KIMI_API_KEY"] == valid["api_token"]
    assert env["KIMI_API_BASE"] == valid["base_url"]
    assert "OPENAI_API_KEY" not in env


def test_broker_refuses_redirects_with_a_bearer_token(monkeypatch):
    visited = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("AGENT_MONITOR_SPONSORED_KIMI_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        with pytest.raises(broker.SponsoredKimiError):
            broker._request("/internal/v1/models", bearer="private-loopback-token")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert visited == ["/internal/v1/models"]
