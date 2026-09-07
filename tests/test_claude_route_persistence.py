from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs, monitor_overview


_ENGINE = {
    "id": "plain",
    "label": "Plain",
    "available": True,
    "auth_modes": ["api_key", "codex_subscription", "claude_subscription"],
}


class ClaudeRoutePersistenceTests(unittest.TestCase):
    def tearDown(self) -> None:
        with jobs._LOCK:
            jobs._JOBS.clear()

    def test_claude_route_uses_private_home_and_scrubs_provider_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_home = Path(tmp) / "user_17"
            polluted = {
                "HOME": "/home/server-account",
                "CLAUDE_CONFIG_DIR": "/home/other-claude-account",
                "CODEX_HOME": "/home/other-codex-account",
                "ANTHROPIC_API_KEY": "metered-key",
                "ANTHROPIC_AUTH_TOKEN": "metered-token",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_ACCESS_KEY_ID": "cloud-account",
                "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
            }
            with (
                patch("agent_monitor.claude_login.account_home", return_value=account_home),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
            ):
                configured = jobs._configure_auth_route(
                    polluted, route="claude_subscription", user_id=17
                )

        self.assertEqual(configured["HOME"], str(account_home))
        self.assertEqual(configured["CLAUDE_CONFIG_DIR"], str(account_home))
        self.assertEqual(configured["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"], "1")
        self.assertEqual(configured["AGENT_MONITOR_AUTH_MODE"], "claude_subscription")
        self.assertNotIn("AGENT_MONITOR_CODEX_SUBSCRIPTION", configured)
        self.assertNotIn("CODEX_HOME", configured)
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_ACCESS_KEY_ID",
        ):
            self.assertNotIn(name, configured)

    def test_new_explicit_claude_run_never_falls_back_to_available_api_key(self) -> None:
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
            patch("agent_monitor.engines_registry.supported_models", return_value=["sonnet"]),
            patch.object(jobs, "_user_extra_env", return_value={"ANTHROPIC_API_KEY": "api-key"}),
            patch("agent_monitor.claude_login.account_home", return_value=Path("/tmp/missing-claude")),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
        ):
            with self.assertRaisesRegex(ValueError, "Claude Code is not connected"):
                jobs.start_job(
                    engine="plain",
                    problem_text="Prove 1 = 1.",
                    model="sonnet",
                    auth_route="claude_subscription",
                    user={"id": 17, "email": "person@example.com"},
                )
        self.assertFalse(jobs._JOBS)


    def test_successful_claude_start_records_and_passes_one_route(self) -> None:
        captured: dict = {}
        written: list[dict] = []

        class FakeThread:
            def __init__(self, *, kwargs, **_unused):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            account_home = Path(tmp) / "claude-user-17"
            with (
                patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
                patch("agent_monitor.engines_registry.supported_models", return_value=["sonnet"]),
                patch.object(
                    jobs,
                    "_user_extra_env",
                    return_value={"HOME": "/server", "ANTHROPIC_API_KEY": "api-key"},
                ),
                patch.object(jobs, "workspace_dir", return_value=workspace),
                patch.object(jobs, "_write_run", side_effect=lambda run: written.append(dict(run))),
                patch("agent_monitor.claude_login.account_home", return_value=account_home),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                job = jobs.start_job(
                    engine="plain",
                    problem_text="Prove 1 = 1.",
                    model="sonnet",
                    auth_route="claude_subscription",
                    user={"id": 17, "email": "person@example.com"},
                )

        self.assertEqual(job["auth_route"], "claude_subscription")
        self.assertEqual(written[0]["auth_route"], "claude_subscription")
        self.assertEqual(written[0]["requested_model"], "sonnet")
        self.assertEqual(written[0]["live"]["requested_model"], "sonnet")
        self.assertEqual(captured["extra_env"]["HOME"], str(account_home))
        self.assertEqual(captured["extra_env"]["CLAUDE_CONFIG_DIR"], str(account_home))
        self.assertNotIn("ANTHROPIC_API_KEY", captured["extra_env"])
        self.assertNotIn("AGENT_MONITOR_CODEX_SUBSCRIPTION", captured["extra_env"])
    def test_recorded_api_continuation_ignores_current_account_toggle(self) -> None:
        record = {
            "run_id": "plain_api",
            "engine": "plain",
            "owner_id": 17,
            "auth_route": "api_key",
        }
        polluted = {
            "ANTHROPIC_API_KEY": "api-key",
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "CLAUDE_CONFIG_DIR": "/home/other-account",
            "CODEX_HOME": "/home/other-account",
        }
        with (
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
            patch("agent_monitor.claude_login.account_login_ready") as claude_ready,
            patch("agent_monitor.codex_login.account_login_ready") as codex_ready,
        ):
            configured, route = jobs._continuation_auth_environment(
                run_data=record,
                engine="plain",
                model="claude-sonnet-4-6",
                user={"id": 17},
                env=polluted,
            )

        self.assertEqual(route, "api_key")
        self.assertEqual(configured["AGENT_MONITOR_AUTH_MODE"], "api_key")
        self.assertNotIn("AGENT_MONITOR_CLAUDE_SUBSCRIPTION", configured)
        self.assertNotIn("CLAUDE_CONFIG_DIR", configured)
        self.assertNotIn("CODEX_HOME", configured)
        claude_ready.assert_not_called()
        codex_ready.assert_not_called()

    def test_recorded_claude_continuation_fails_closed_when_login_is_gone(self) -> None:
        record = {
            "run_id": "plain_claude",
            "engine": "plain",
            "owner_id": 17,
            "auth_route": "claude_subscription",
        }
        with (
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
            patch("agent_monitor.claude_login.account_home", return_value=Path("/tmp/claude-17")),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
        ):
            with self.assertRaisesRegex(ValueError, "Claude Code is not connected"):
                jobs._continuation_auth_environment(
                    run_data=record,
                    engine="plain",
                    model="sonnet",
                    user={"id": 17},
                    env={"ANTHROPIC_API_KEY": "fallback-must-not-run"},
                )

    def test_continue_run_passes_and_records_original_claude_route(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_unused):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "workspaces" / "plain_claude").mkdir(parents=True)
            account_home = runs / "claude-home"
            record = {
                "run_id": "plain_claude",
                "engine": "plain",
                "owner_id": 17,
                "auth_route": "claude_subscription",
                "problem_id": "demo",
                "problem_text": "Prove 1 = 1.",
                "problems": [{"text": "Prove 1 = 1.", "source": "initial"}],
                "requested_model": "fable",
                "live": {
                    "model": "claude-fable-5",
                    "auth_mode": "claude_subscription",
                },
            }
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_append_chat"),
                patch.object(
                    jobs,
                    "_user_extra_env",
                    return_value={"HOME": "/server", "ANTHROPIC_API_KEY": "api-key"},
                ),
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
                patch("agent_monitor.claude_login.account_home", return_value=account_home),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                result = jobs.continue_run(
                    "plain_claude", message="Check the endpoint.", user={"id": 17}
                )

        self.assertEqual(captured["extra_env"]["HOME"], str(account_home))
        self.assertEqual(captured["extra_env"]["CLAUDE_CONFIG_DIR"], str(account_home))
        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_AUTH_MODE"], "claude_subscription")
        self.assertEqual(captured["model"], "fable")
        self.assertNotIn("ANTHROPIC_API_KEY", captured["extra_env"])
        self.assertEqual(jobs._JOBS[result["job_id"]]["auth_route"], "claude_subscription")


    def test_recorded_codex_continuation_cannot_be_redirected_to_claude(self) -> None:
        codex_home = Path("/tmp/codex-user-17")
        record = {
            "run_id": "plain_codex",
            "engine": "plain",
            "owner_id": 17,
            "auth_route": "codex_subscription",
        }
        with (
            patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
            patch("agent_monitor.codex_login.account_home", return_value=codex_home),
            patch("agent_monitor.codex_login.account_login_ready", return_value=True),
            patch("agent_monitor.claude_login.account_login_ready") as claude_ready,
        ):
            configured, route = jobs._continuation_auth_environment(
                run_data=record,
                engine="plain",
                model="gpt-5.6-sol",
                user={"id": 17},
                env={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
                    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                    "CLAUDE_CONFIG_DIR": "/tmp/other-claude",
                },
            )

        self.assertEqual(route, "codex_subscription")
        self.assertEqual(configured["CODEX_HOME"], str(codex_home))
        self.assertEqual(configured["AGENT_MONITOR_AUTH_MODE"], "chatgpt_subscription")
        self.assertNotIn("AGENT_MONITOR_CLAUDE_SUBSCRIPTION", configured)
        self.assertNotIn("CLAUDE_CONFIG_DIR", configured)
        claude_ready.assert_not_called()

    def test_hitl_resume_uses_the_same_recorded_claude_route(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_unused):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "workspaces" / "plain_hitl").mkdir(parents=True)
            account_home = runs / "claude-home"
            record = {
                "run_id": "plain_hitl",
                "engine": "plain",
                "owner_id": 17,
                "auth_route": "claude_subscription",
                "problem_id": "demo",
                "problem_text": "Prove 1 = 1.",
                "problems": [{"text": "Prove 1 = 1.", "source": "initial"}],
                "live": {"model": "sonnet", "auth_mode": "claude_subscription"},
            }
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_write_run"),
                patch.object(jobs, "_write_session_problems"),
                patch.object(jobs, "_append_chat"),
                patch.object(
                    jobs,
                    "_user_extra_env",
                    return_value={"HOME": "/server", "ANTHROPIC_API_KEY": "api-key"},
                ),
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_ENGINE)]),
                patch("agent_monitor.claude_login.account_home", return_value=account_home),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                result = jobs.send_human_message(
                    "plain_hitl", "Check the endpoint.", user={"id": 17}
                )

        self.assertEqual(captured["extra_env"]["HOME"], str(account_home))
        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_AUTH_MODE"], "claude_subscription")
        self.assertNotIn("ANTHROPIC_API_KEY", captured["extra_env"])
        self.assertEqual(jobs._JOBS[result["job_id"]]["auth_route"], "claude_subscription")
    def test_continuation_merge_keeps_previous_route_over_runner_output(self) -> None:
        merged = jobs._merge_continuation_run(
            {
                "run_id": "route-lineage",
                "auth_route": "claude_subscription",
                "live": {"auth_mode": "claude_subscription"},
                "agents": [],
                "edges": [],
                "totals": {},
            },
            {
                "run_id": "route-lineage",
                "auth_route": "api_key",
                "live": {"auth_mode": "api_key"},
                "agents": [],
                "edges": [],
                "totals": {},
            },
            job_id="continue-1",
        )
        self.assertEqual(merged["auth_route"], "claude_subscription")
        self.assertEqual(merged["live"]["auth_mode"], "claude_subscription")


class ClaudeMonitorOverviewTests(unittest.TestCase):
    def test_overview_reports_both_accounts_and_selected_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "harness"
            cache.mkdir(parents=True)
            (cache / "manifest.json").write_text(json.dumps({"runs": []}), encoding="utf-8")
            with (
                patch.object(monitor_overview, "CACHE_DIR", Path(tmp)),
                patch("agent_monitor.library.get_library", return_value={"items": []}),
                patch(
                    "agent_monitor.agent_config.get_agent_config",
                    return_value={"memory": [], "skills": [], "system_prompt": ""},
                ),
                patch(
                    "agent_monitor.settings.get_settings",
                    return_value={
                        "account_runtime": "claude",
                        "codex_connected": True,
                        "claude_connected": True,
                        "configured_providers": ["anthropic"],
                    },
                ),
                patch("agent_monitor.engines_registry.list_engines", return_value=[]),
            ):
                result = monitor_overview.build_overview({"id": 17, "is_admin": False})

        self.assertEqual(result["auth"]["account_runtime"], "claude")
        self.assertTrue(result["auth"]["codex_connected"])
        self.assertTrue(result["auth"]["claude_connected"])
        self.assertTrue(result["auth"]["subscription_connected"])

    def test_monitor_card_renders_selected_runtime_not_codex_only(self) -> None:
        html = Path("agent_monitor/web/console.html").read_text(encoding="utf-8")
        self.assertIn("auth.account_runtime||'api'", html)
        self.assertIn("runtime==='claude'?'Claude Code'", html)
        self.assertNotIn("MONITOR.auth&&MONITOR.auth.codex_connected", html)


if __name__ == "__main__":
    unittest.main()
