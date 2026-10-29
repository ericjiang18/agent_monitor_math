from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs, sponsored_kimi_client
from agent_monitor.public_kimi_server import (
    PublicKimiApplication,
    PublicKimiHTTPServer,
    PublicLimits,
)


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


    def test_health_exposes_the_brokers_passive_degraded_flag(self) -> None:
        broker = {
            "AGENT_MONITOR_SPONSORED_KIMI": "1",
            "AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610",
        }
        upstream = {"recent_calls": 4, "recent_failures": 3, "last_success_age_s": None}
        for answer, degraded in (
            ({"ok": True, "service": "public-kimi", "upstream": {**upstream, "degraded": True}}, True),
            ({"ok": True, "service": "public-kimi", "upstream": {**upstream, "degraded": False}}, False),
            # An older broker without the upstream section stays quiet.
            ({"ok": True, "service": "public-kimi"}, False),
            ({"ok": True, "service": "public-kimi", "upstream": {"degraded": "yes"}}, False),
            ({"ok": False, "service": "public-kimi", "upstream": {"degraded": True}}, False),
        ):
            with (
                self.subTest(answer=answer),
                patch.dict(os.environ, broker, clear=False),
                patch.object(sponsored_kimi_client, "_health_cache", (0.0, "", False)),
                patch.object(sponsored_kimi_client, "_health_degraded", ("", False)),
                patch.object(sponsored_kimi_client, "_request", return_value=(answer, "")) as request,
            ):
                self.assertEqual(sponsored_kimi_client.available(), answer["ok"])
                self.assertIs(sponsored_kimi_client.upstream_degraded(), degraded)
                # One cached /healthz answer serves both questions.
                self.assertEqual(request.call_count, 1)

        with (
            patch.dict(os.environ, broker, clear=False),
            patch.object(sponsored_kimi_client, "_health_cache", (0.0, "", False)),
            patch.object(sponsored_kimi_client, "_health_degraded", ("", True)),
            patch.object(
                sponsored_kimi_client,
                "_request",
                side_effect=sponsored_kimi_client.SponsoredKimiError("down"),
            ),
        ):
            self.assertFalse(sponsored_kimi_client.upstream_degraded())

    def test_degraded_notice_marks_the_engines_the_broker_serves(self) -> None:
        engines = [
            {"id": "kimi", "available": True},
            {"id": "plain", "available": True},
            {"id": "hermes", "available": True},
        ]
        with patch.object(sponsored_kimi_client, "upstream_degraded", return_value=True):
            marked = sponsored_kimi_client.with_degraded_notice(engines)
        self.assertEqual(marked[0]["notice"], sponsored_kimi_client.DEGRADED_NOTICE)
        self.assertEqual(marked[1]["notice"], sponsored_kimi_client.DEGRADED_NOTICE)
        self.assertNotIn("notice", marked[2])  # never on the sponsored route
        self.assertNotIn("notice", engines[0])  # the registry's list is not changed
        with patch.object(sponsored_kimi_client, "upstream_degraded", return_value=False):
            self.assertIs(sponsored_kimi_client.with_degraded_notice(engines), engines)

    def test_degraded_notice_skips_accounts_whose_kimi_runs_use_their_own_key(self) -> None:
        engines = [{"id": "kimi", "available": True}, {"id": "plain", "available": True}]
        user = {"id": 7, "guest": False}
        for env, noticed in (
            ({"KIMI_API_KEY": "sk-own-kimi-key-123456"}, False),
            ({"KIMI_API_KEY": "  "}, True),
            ({"OPENAI_API_KEY": "sk-other-provider-1234"}, True),
        ):
            with (
                self.subTest(env=sorted(env)),
                patch.object(sponsored_kimi_client, "upstream_degraded", return_value=True),
                patch("agent_monitor.settings.resolved_user_env", return_value=env) as resolved,
            ):
                marked = sponsored_kimi_client.with_degraded_notice(engines, user)
            self.assertEqual(
                [engine.get("notice") is not None for engine in marked], [noticed, noticed]
            )
            resolved.assert_called_once_with(user)
        # A healthy broker never looks the account up at all.
        with (
            patch.object(sponsored_kimi_client, "upstream_degraded", return_value=False),
            patch("agent_monitor.settings.resolved_user_env") as resolved,
        ):
            self.assertIs(sponsored_kimi_client.with_degraded_notice(engines, user), engines)
        resolved.assert_not_called()

    def test_upstream_degraded_never_probes_a_disabled_broker(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sponsored_kimi_client, "_request") as request,
        ):
            self.assertFalse(sponsored_kimi_client.upstream_degraded())
        request.assert_not_called()

    def test_typed_broker_errors_surface_their_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = PublicKimiApplication(
                jobs=None,
                data_dir=Path(tmp),
                redactions=(),
                limits=PublicLimits(sponsor_token_starts_per_hour=1),
                engine_catalog=lambda: [],
            )
            server = PublicKimiHTTPServer(("127.0.0.1", 0), app, "")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with patch.dict(
                    os.environ,
                    {
                        "AGENT_MONITOR_SPONSORED_KIMI_URL": f"http://127.0.0.1:{server.server_address[1]}",
                        "NO_PROXY": "*",
                    },
                    clear=False,
                ):
                    sponsored_kimi_client.issue_credentials(
                        engine="plain", client_id=31, cache_key="first"
                    )
                    with self.assertRaises(sponsored_kimi_client.SponsoredKimiError) as raised:
                        sponsored_kimi_client.issue_credentials(
                            engine="plain", client_id=31, cache_key="second"
                        )
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)
        self.assertEqual(
            str(raised.exception),
            "Sponsored Kimi request failed: Sponsored Kimi session quota reached",
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

    def test_projection_books_the_usage_the_service_reports(self) -> None:
        from agent_monitor import usage_ledger

        payload = {
            "run_id": "plain_adhoc_remote_0123456789",
            "status": "finished",
            "engine": "plain",
            "model": "kimi-k3",
            "proof": "# Proof\n\nBy reflexivity.",
            "events": [{"stage": "final", "role": "agent", "text": "By reflexivity."}],
            # Counts and cost are booked; a negative, a NaN, the service's
            # latency and any other key are not.
            "usage": {"input_tokens": 1200, "output_tokens": 345, "cost_usd": 0.0123,
                      "reasoning_tokens": -5, "cached_input_tokens": float("nan"),
                      "latency_s": 999.0, "note": "not a metric"},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(jobs, "RUNS_DIR", Path(tmp) / "runs"),
            patch.object(jobs, "CACHE_DIR", Path(tmp) / "cache"),
            patch.object(sponsored_kimi_client, "run_plain", return_value=payload),
        ):
            workspace = Path(tmp) / "runs" / "workspaces" / "plain_local_usage"
            workspace.mkdir(parents=True)
            result = jobs._run_sponsored_kimi_plain(
                run_id="plain_local_usage",
                problem_id="demo",
                problem_text="Prove True.",
                display_problem_text="Prove True.",
                started=time.time(),
                workspace=workspace,
                owner_id=19,
            )
            summary = usage_ledger.usage_summary({})

        totals = result["totals"]
        self.assertEqual((totals["input_tokens"], totals["output_tokens"], totals["cost_usd"]), (1200, 345, 0.0123))
        self.assertNotIn("reasoning_tokens", totals)
        self.assertNotIn("cached_input_tokens", totals)
        self.assertLess(totals["latency_s"], 60)  # the console's own, not the service's
        sponsored = {row["key"]: row for row in summary["by_billing"]}["sponsored"]
        self.assertEqual((sponsored["input_tokens"], sponsored["output_tokens"]), (1200, 345))
        self.assertAlmostEqual(sponsored["cost_usd"], 0.0123)


if __name__ == "__main__":
    unittest.main()
