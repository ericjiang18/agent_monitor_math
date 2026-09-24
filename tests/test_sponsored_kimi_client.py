from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs, sponsored_kimi_client


class SponsoredKimiClientTests(unittest.TestCase):
    def tearDown(self) -> None:
        with jobs._LOCK:
            jobs._STOP_EVENTS.clear()
        with sponsored_kimi_client._credential_lock:
            sponsored_kimi_client._credential_cache.clear()

    def test_endpoint_is_loopback_only(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENT_MONITOR_SPONSORED_KIMI_URL": "https://kimi.example"},
            clear=False,
        ):
            with self.assertRaisesRegex(
                sponsored_kimi_client.SponsoredKimiError, "loopback"
            ):
                sponsored_kimi_client.endpoint()

        with patch.dict(
            os.environ,
            {"AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610"},
            clear=False,
        ):
            self.assertEqual(
                sponsored_kimi_client.endpoint(), "http://127.0.0.1:4610"
            )

    def test_configured_requires_explicit_flag_before_health_probe(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sponsored_kimi_client, "available") as available,
        ):
            self.assertFalse(sponsored_kimi_client.configured())
        available.assert_not_called()

        with (
            patch.dict(
                os.environ, {"AGENT_MONITOR_SPONSORED_KIMI": "1"}, clear=True
            ),
            patch.object(
                sponsored_kimi_client, "available", return_value=True
            ) as available,
        ):
            self.assertTrue(sponsored_kimi_client.configured())
        available.assert_called_once()

    def test_issue_credentials_attests_and_caches_only_proxy_token(self) -> None:
        calls: list[tuple[str, dict]] = []

        def request(path: str, **kwargs):
            calls.append((path, kwargs))
            if path == "/internal/v1/models":
                return (
                    {"data": [{"id": "kimi-k3", "object": "model"}]},
                    "",
                )
            return (
                {
                    "api_token": "t" * 64,
                    "base_url": "http://127.0.0.1:4610/internal/v1",
                    "model": "kimi-k3",
                    "engine": "openclaude",
                    "expires_in": 7200,
                },
                "",
            )

        with (
            patch.dict(
                os.environ,
                {"AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610"},
                clear=False,
            ),
            patch.object(sponsored_kimi_client, "_request", side_effect=request),
        ):
            first = sponsored_kimi_client.issue_credentials(
                engine="openclaude", client_id=70, cache_key="run-1"
            )
            second = sponsored_kimi_client.issue_credentials(
                engine="openclaude", client_id=70, cache_key="run-1"
            )
            with sponsored_kimi_client._credential_lock:
                key = ("70", "openclaude", "run-1")
                _expires, environment = sponsored_kimi_client._credential_cache[key]
                sponsored_kimi_client._credential_cache[key] = (
                    time.monotonic() + 100, environment
                )
            third = sponsored_kimi_client.issue_credentials(
                engine="openclaude", client_id=70, cache_key="run-1", minimum_ttl_seconds=600
            )

        self.assertEqual(first, second)
        self.assertEqual(first["KIMI_API_KEY"], "t" * 64)
        self.assertEqual(second, third)
        self.assertEqual(
            first["KIMI_API_BASE"], "http://127.0.0.1:4610/internal/v1"
        )
        self.assertNotIn("server-sponsored-secret", repr(first))
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0], "/internal/sessions")
        self.assertEqual(calls[0][1]["payload"], {"engine": "openclaude"})
        self.assertEqual(calls[1][0], "/internal/v1/models")
        self.assertEqual(calls[2][0], "/internal/sessions")
    def test_concurrent_issue_is_single_flight_per_cached_scope(self) -> None:
        calls: list[str] = []

        def request(path: str, **_kwargs):
            calls.append(path)
            time.sleep(0.02)
            if path == "/internal/v1/models":
                return {"data": [{"id": "kimi-k3"}]}, ""
            return (
                {
                    "api_token": "u" * 64,
                    "base_url": "http://127.0.0.1:4610/internal/v1",
                    "model": "kimi-k3",
                    "engine": "formal",
                    "expires_in": 7200,
                },
                "",
            )

        with (
            patch.dict(
                os.environ,
                {"AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610"},
                clear=False,
            ),
            patch.object(sponsored_kimi_client, "_request", side_effect=request),
            ThreadPoolExecutor(max_workers=4) as pool,
        ):
            results = list(
                pool.map(
                    lambda _index: sponsored_kimi_client.issue_credentials(
                        engine="formal", client_id=70, cache_key="same-run"
                    ),
                    range(4),
                )
            )

        self.assertEqual(calls.count("/internal/sessions"), 1)
        self.assertEqual(calls.count("/internal/v1/models"), 3)
        self.assertEqual({item["KIMI_API_KEY"] for item in results}, {"u" * 64})


    def test_run_plain_attests_route_and_reuses_secure_cookie_over_loopback(self) -> None:
        run_id = "plain_adhoc_demo_0123456789"
        calls: list[tuple[str, str, str, str]] = []
        statuses = iter(
            [
                {
                    "run_id": run_id,
                    "status": "running",
                    "engine": "plain",
                    "model": "kimi-k3",
                    "events": [],
                },
                {
                    "run_id": run_id,
                    "status": "finished",
                    "engine": "plain",
                    "model": "kimi-k3",
                    "proof": "# Proof\n\nDone.",
                    "events": [{"stage": "final", "text": "Done."}],
                },
            ]
        )

        def request(path: str, **kwargs):
            calls.append(
                (
                    path,
                    str(kwargs.get("method") or "GET"),
                    str(kwargs.get("cookie") or ""),
                    str(kwargs.get("client_ip") or ""),
                )
            )
            if path == "/api/runs":
                return (
                    {
                        "run_id": run_id,
                        "status": "running",
                        "engine": "plain",
                        "model": "kimi-k3",
                    },
                    "a" * 64,
                )
            return next(statuses), str(kwargs.get("cookie") or "")

        updates: list[str] = []
        with (
            patch.object(sponsored_kimi_client, "_request", side_effect=request),
            patch.object(sponsored_kimi_client.time, "sleep"),
        ):
            result = sponsored_kimi_client.run_plain(
                "Prove it.",
                client_id=17,
                stopped=lambda: False,
                on_update=lambda item: updates.append(str(item.get("status"))),
                timeout=10,
            )

        self.assertEqual(result["status"], "finished")
        self.assertEqual(updates, ["running", "finished"])
        self.assertEqual(calls[0][0:2], ("/api/runs", "POST"))
        self.assertTrue(calls[0][3].startswith("198.18."))
        self.assertEqual(calls[1][2], "a" * 64)
        self.assertEqual(calls[2][2], "a" * 64)

    def test_run_plain_stop_calls_only_the_fixed_stop_endpoint(self) -> None:
        run_id = "plain_adhoc_stop_0123456789"
        calls: list[tuple[str, str]] = []

        def request(path: str, **kwargs):
            calls.append((path, str(kwargs.get("method") or "GET")))
            if path == "/api/runs":
                return (
                    {
                        "run_id": run_id,
                        "status": "running",
                        "engine": "plain",
                        "model": "kimi-k3",
                    },
                    "b" * 64,
                )
            return {"run_id": run_id, "status": "stopped"}, "b" * 64

        with patch.object(sponsored_kimi_client, "_request", side_effect=request):
            result = sponsored_kimi_client.run_plain(
                "Prove it.",
                client_id=18,
                stopped=lambda: True,
            )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            calls,
            [
                ("/api/runs", "POST"),
                (f"/api/runs/{run_id}/stop", "POST"),
            ],
        )


    def test_projection_copies_proof_without_any_provider_credential(self) -> None:
        payload = {
            "run_id": "plain_adhoc_remote_0123456789",
            "status": "finished",
            "engine": "plain",
            "model": "kimi-k3",
            "proof": "# Proof\n\nBy reflexivity.",
            "events": [{"stage": "final", "role": "agent", "text": "By reflexivity."}],
        }
        written: list[dict] = []
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                sponsored_kimi_client, "run_plain", return_value=payload
            ) as remote,
            patch.object(
                jobs,
                "_write_run",
                side_effect=lambda run: written.append(run),
            ),
        ):
            workspace = Path(tmp)
            result = jobs._run_sponsored_kimi_plain(
                run_id="plain_local_demo",
                problem_id="demo",
                problem_text="Prove True.",
                display_problem_text="Prove True.",
                started=time.time(),
                workspace=workspace,
                owner_id=19,
            )
            proof = (workspace / "proof.md").read_text(encoding="utf-8")

        self.assertEqual(proof, "# Proof\n\nBy reflexivity.\n")
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["credential_source"], "sponsored_kimi")
        self.assertEqual(result["live"]["provider"], "isolated-sponsored-kimi")
        self.assertEqual(
            result["runner_result"]["transport"], "isolated-sponsored-kimi"
        )
        remote.assert_called_once()
        self.assertTrue(written)


if __name__ == "__main__":
    unittest.main()
