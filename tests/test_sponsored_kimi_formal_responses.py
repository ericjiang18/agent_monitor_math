from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from agent_monitor.public_kimi_gateway import (
    PublicKimiGateway,
    _sanitize_responses_gateway_payload,
)
from agent_monitor.public_kimi_server import (
    PublicKimiApplication,
    PublicKimiHTTPServer,
)


class _Jobs:
    def list_jobs(self):
        return []

    def delete_run(self, _run_id):
        return None


class _Upstream:
    status = code = 200

    def __init__(self, body: bytes = b'{"id":"resp-test"}') -> None:
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int = -1):
        return self.body if limit < 0 else self.body[:limit]


class SponsoredKimiFormalResponsesTests(unittest.TestCase):
    def test_responses_sanitizer_forces_model_caps_output_and_drops_builtin_tools(self) -> None:
        function = {
            "type": "function",
            "name": "write_file",
            "parameters": {"type": "object"},
        }
        value = _sanitize_responses_gateway_payload(
            {
                "model": "attacker-model",
                "input": "prove",
                "max_output_tokens": 99_999,
                "output_config": {"attacker": True},
                "tools": [{"type": "code_interpreter"}, function],
                "stream": True,
            },
            reasoning_effort="high",
            max_output_tokens=8192,
        )
        self.assertEqual(value["model"], "kimi-k3")
        self.assertEqual(value["max_output_tokens"], 8192)
        self.assertEqual(value["reasoning"], {"effort": "high"})
        self.assertEqual(value["tools"], [function])
        self.assertNotIn("output_config", value)

    def test_credential_gateway_forwards_responses_to_exact_upstream_path(self) -> None:
        captured: dict = {}

        def urlopen(request, **_kwargs):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["payload"] = json.loads(request.data)
            return _Upstream()

        gateway = PublicKimiGateway(
            upstream_base="https://kimi.example/v1",
            upstream_api_key="real-provider-secret",
            local_token="local-boundary-token",
        )
        with patch(
            "agent_monitor.public_kimi_gateway.urlrequest.urlopen",
            side_effect=urlopen,
        ), gateway:
            host_port = gateway.base_url.removeprefix("http://").removesuffix("/v1")
            host, port = host_port.rsplit(":", 1)
            connection = http.client.HTTPConnection(host, int(port), timeout=3)
            connection.request(
                "POST",
                "/v1/responses",
                body=json.dumps({"model": "other", "input": "prove"}),
                headers={
                    "Authorization": "Bearer local-boundary-token",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(captured["url"], "https://kimi.example/v1/responses")
        self.assertEqual(captured["authorization"], "Bearer real-provider-secret")
        self.assertEqual(captured["payload"]["model"], "kimi-k3")

    def test_internal_responses_requires_codex_or_formal_scoped_sponsor_token(self) -> None:
        captured: list[str] = []

        def urlopen(request, **_kwargs):
            captured.append(request.full_url)
            return _Upstream()

        with tempfile.TemporaryDirectory() as tmp:
            app = PublicKimiApplication(
                jobs=_Jobs(),
                data_dir=Path(tmp),
                redactions=(),
                engine_catalog=lambda: [{"id": "plain", "available": True}],
            )
            formal, _ttl = app.sponsor_sessions.issue(engine="formal", ip="198.18.1.1")
            ordinary, _ttl = app.sponsor_sessions.issue(
                engine="openclaude", ip="198.18.1.2"
            )
            codex, _ttl = app.sponsor_sessions.issue(engine="codex", ip="198.18.1.3")
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
            try:
                with patch(
                    "agent_monitor.public_kimi_server.urlrequest.urlopen",
                    side_effect=urlopen,
                ):
                    for token, expected in ((formal, 200), (codex, 200), (ordinary, 403)):
                        connection = http.client.HTTPConnection(host, port, timeout=3)
                        connection.request(
                            "POST",
                            "/internal/v1/responses",
                            body=json.dumps({"model": "kimi-k3", "input": "prove"}),
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Content-Type": "application/json",
                            },
                        )
                        response = connection.getresponse()
                        response.read()
                        connection.close()
                        self.assertEqual(response.status, expected)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        self.assertEqual(
            captured,
            ["http://127.0.0.1:19999/v1/responses"] * 2,
        )


if __name__ == "__main__":
    unittest.main()
