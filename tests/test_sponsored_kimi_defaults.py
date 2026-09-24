from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import auth, jobs, settings
from agent_monitor.runners import api_backend


_PLAIN = {
    "id": "plain",
    "label": "Plain",
    "available": True,
    "auth_modes": ["api_key", "codex_subscription", "claude_subscription"],
}


class SponsoredKimiDefaultTests(unittest.TestCase):
    def tearDown(self) -> None:
        with jobs._LOCK:
            jobs._JOBS.clear()
            jobs._STOP_EVENTS.clear()

    def test_password_registration_seeds_api_kimi_plain_defaults(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(auth, "DB_PATH", Path(tmp) / "users.db"),
            patch.object(auth, "_INITIALIZED", False),
        ):
            user = auth.register("new@example.com", "correct horse battery")
            stored = auth.get_user_env(int(user["id"]))

        self.assertEqual(
            stored,
            {
                "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
                "AGENT_MONITOR_USE_CODEX": "0",
                "AGENT_MONITOR_USE_CLAUDE": "0",
                "AGENT_MONITOR_MODEL": "kimi-k3",
                "AGENT_MONITOR_ENGINE": "plain",
            },
        )

    def test_settings_exposes_included_kimi_without_user_key(self) -> None:
        env = dict(auth._NEW_USER_DEFAULTS)
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch.object(settings, "sponsored_kimi_available", return_value=True),
            patch("agent_monitor.codex_login.account_login_ready", return_value=False),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
        ):
            result = settings.get_settings({"id": 41, "is_admin": False})

        kimi = next(item for item in result["providers"] if item["id"] == "kimi")
        self.assertEqual(result["account_runtime"], "api")
        self.assertTrue(result["runtime_ready"])
        self.assertEqual(result["default_model"], "kimi-k3")
        self.assertEqual(result["default_engine"], "plain")
        self.assertIn("kimi-k3", result["models_by_runtime"]["api"])
        self.assertTrue(kimi["sponsored_available"])
        self.assertTrue(kimi["using_sponsored"])
        self.assertFalse(kimi["api_key_set"])
        self.assertFalse(result["kimi_user_key_set"])

    def test_user_kimi_key_overrides_sponsored_key_state(self) -> None:
        env = {
            **auth._NEW_USER_DEFAULTS,
            "KIMI_API_KEY": "user-owned-kimi-key-123456789",
        }
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch.object(settings, "sponsored_kimi_available", return_value=True),
            patch("agent_monitor.codex_login.account_login_ready", return_value=False),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
        ):
            result = settings.get_settings({"id": 42, "is_admin": False})

        kimi = next(item for item in result["providers"] if item["id"] == "kimi")
        self.assertTrue(kimi["api_key_set"])
        self.assertFalse(kimi["using_sponsored"])
        self.assertTrue(result["kimi_user_key_set"])
        self.assertIn("kimi", result["configured_providers"])

    def test_sponsored_plain_start_binds_isolated_route_and_skips_pipeline(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_rest):
                captured.update(kwargs)

            def start(self) -> None:
                return None

            def is_alive(self) -> bool:
                return True

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_PLAIN)]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(jobs, "_user_extra_env", return_value=dict(auth._NEW_USER_DEFAULTS)),
            patch(
                "agent_monitor.sponsored_kimi_client.configured",
                return_value=True,
            ),
            patch(
                "agent_monitor.sponsored_kimi_client.endpoint",
                return_value="http://127.0.0.1:4610",
            ),
            patch.object(jobs, "workspace_dir", return_value=Path(tmp)),
            patch.object(jobs, "_write_run") as write_run,
            patch("agent_monitor.jobs.threading.Thread", FakeThread),
            patch("agent_monitor.auto_pipeline.start") as derived,
        ):
            job = jobs.start_job(
                engine="plain",
                problem_text="Prove that one equals one.",
                model="kimi-k3",
                user={"id": 41, "email": "new@example.com"},
                auth_route="api_key",
            )

        self.assertEqual(job["credential_source"], "sponsored_kimi")
        bootstrap = write_run.call_args.args[0]
        self.assertEqual(bootstrap["model"], "kimi-k3")
        self.assertEqual(bootstrap["live"]["model"], "kimi-k3")
        self.assertEqual(bootstrap["live"]["auth_mode"], "api_key")
        self.assertNotIn("KIMI_API_KEY", captured["extra_env"])
        self.assertEqual(
            captured["extra_env"]["AGENT_MONITOR_SPONSORED_KIMI"], "1"
        )
        self.assertEqual(
            captured["extra_env"]["AGENT_MONITOR_SPONSORED_KIMI_URL"],
            "http://127.0.0.1:4610",
        )
        derived.assert_called_once()

    def test_sponsored_route_rejects_harness_without_attested_chat_transport(self) -> None:
        meta = {
            "id": "hermes",
            "label": "Hermes",
            "available": True,
            "auth_modes": ["api_key"],
        }
        with (
            patch(
                "agent_monitor.engines_registry.all_engine_ids",
                return_value={"hermes"},
            ),
            patch("agent_monitor.engines_registry.list_engines", return_value=[meta]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(jobs, "_user_extra_env", return_value=dict(auth._NEW_USER_DEFAULTS)),
            patch.object(jobs.threading, "Thread") as thread,
        ):
            with self.assertRaisesRegex(ValueError, "not available with the hermes"):
                jobs.start_job(
                    engine="hermes",
                    problem_text="Prove it.",
                    model="kimi-k3",
                    user={"id": 41},
                    auth_route="api_key",
                )
        thread.assert_not_called()

    def test_openclaude_start_receives_only_proxy_token_and_runs_pipeline(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_rest):
                captured.update(kwargs)

            def start(self) -> None:
                return None

            def is_alive(self) -> bool:
                return True

        meta = {
            "id": "openclaude",
            "label": "OpenClaude",
            "available": True,
            "auth_modes": ["api_key"],
        }
        proxy_env = {
            "KIMI_API_KEY": "t" * 64,
            "KIMI_API_BASE": "http://127.0.0.1:4610/internal/v1",
            "AGENT_MONITOR_SPONSORED_KIMI": "1",
            "AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "agent_monitor.engines_registry.all_engine_ids",
                return_value={"openclaude"},
            ),
            patch("agent_monitor.engines_registry.list_engines", return_value=[meta]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(jobs, "_user_extra_env", return_value=dict(auth._NEW_USER_DEFAULTS)),
            patch(
                "agent_monitor.sponsored_kimi_client.configured", return_value=True
            ),
            patch(
                "agent_monitor.sponsored_kimi_client.endpoint",
                return_value="http://127.0.0.1:4610",
            ),
            patch(
                "agent_monitor.sponsored_kimi_client.issue_credentials",
                return_value=proxy_env,
            ) as issue,
            patch.object(jobs, "workspace_dir", return_value=Path(tmp)),
            patch.object(jobs, "_write_run"),
            patch("agent_monitor.jobs.threading.Thread", FakeThread),
            patch("agent_monitor.auto_pipeline.start") as derived,
        ):
            job = jobs.start_job(
                engine="openclaude",
                problem_text="Prove it.",
                model="kimi-k3",
                user={"id": 70},
                auth_route="api_key",
            )

        self.assertEqual(job["credential_source"], "sponsored_kimi")
        self.assertEqual(captured["extra_env"]["KIMI_API_KEY"], "t" * 64)
        self.assertNotIn("upstream-provider-key", repr(captured["extra_env"]))
        self.assertEqual(issue.call_args.kwargs["engine"], "openclaude")
        derived.assert_called_once()

    def test_own_key_allows_non_plain_kimi_without_sponsored_route(self) -> None:
        env = {"KIMI_API_KEY": "user-key", "AGENT_MONITOR_ACCOUNT_RUNTIME": "api"}
        with patch(
            "agent_monitor.sponsored_kimi_client.configured"
        ) as sponsored:
            used = jobs._apply_sponsored_kimi_route(
                env, engine="metaharness", model="kimi-k3"
            )
        self.assertFalse(used)
        self.assertEqual(env["KIMI_API_KEY"], "user-key")
        sponsored.assert_not_called()

    def test_sponsored_continuation_rebinds_recorded_isolated_route(self) -> None:
        env = {
            "KIMI_API_KEY": "new-user-key-must-not-switch-old-run",
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
        }
        with (
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_PLAIN)]),
            patch(
                "agent_monitor.sponsored_kimi_client.configured",
                return_value=True,
            ),
            patch(
                "agent_monitor.sponsored_kimi_client.endpoint",
                return_value="http://127.0.0.1:4610",
            ),
        ):
            configured, route = jobs._continuation_auth_environment(
                run_data={
                    "auth_route": "api_key",
                    "credential_source": "sponsored_kimi",
                    "owner_id": 41,
                },
                engine="plain",
                model="kimi-k3",
                user={"id": 41},
                env=env,
            )

        self.assertEqual(route, "api_key")
        self.assertNotIn("KIMI_API_KEY", configured)
        self.assertEqual(configured["AGENT_MONITOR_SPONSORED_KIMI"], "1")
        self.assertEqual(
            configured["AGENT_MONITOR_SPONSORED_KIMI_URL"],
            "http://127.0.0.1:4610",
        )

    def test_sponsored_api_calls_do_not_replay_ambiguous_failure(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "KIMI_API_KEY": "server-sponsored-secret",
                    "KIMI_API_BASE": "https://kimi.invalid/v1",
                    "AGENT_MONITOR_SPONSORED_KIMI": "1",
                },
                clear=True,
            ),
            patch(
                "agent_monitor.settings._http_json",
                return_value=(503, {"error": {"message": "busy"}}),
            ) as request,
            patch("agent_monitor.kimi_k3.time.sleep"),
        ):
            with self.assertRaisesRegex(api_backend.APIBackendError, "HTTP 503"):
                api_backend.api_chat("system", "problem", model="kimi-k3")

        self.assertEqual(request.call_count, 1)

    def test_original_ui_describes_included_kimi_and_optional_key(self) -> None:
        page = (
            Path(__file__).resolve().parents[1]
            / "agent_monitor"
            / "web"
            / "console.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Kimi K3 included · own keys optional", page)
        self.assertIn("Optional — paste your own Kimi key", page)
        self.assertIn("SETTINGS?.default_engine", page)
        self.assertIn("SETTINGS?.sponsored_kimi_engines", page)


if __name__ == "__main__":
    unittest.main()
