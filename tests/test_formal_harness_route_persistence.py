from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import lean_verify


_CLAUDE_RUN = {
    "run_id": "claude-formal",
    "owner_id": 17,
    "auth_route": "claude_subscription",
    "model": "claude-haiku-4-5",
    "problem_text": "Prove True.",
}


class FormalHarnessRoutePersistenceTests(unittest.TestCase):
    def tearDown(self) -> None:
        with lean_verify._HARNESS_LOCK:
            lean_verify._HARNESS_JOBS.clear()
        with lean_verify._FORMAL_OPERATIONS_GUARD:
            lean_verify._FORMAL_OPERATIONS.clear()

    def test_recorded_claude_route_ignores_current_api_and_hides_api_only_engines(
        self,
    ) -> None:
        with (
            patch(
                "agent_monitor.settings.resolved_user_env",
                return_value={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
                    "OPENAI_API_KEY": "must-not-be-used",
                },
            ),
            patch("agent_monitor.settings.account_runtime") as current_runtime,
            patch("agent_monitor.claude_login.account_login_ready", return_value=True),
            patch("agent_monitor.codex_login.account_login_ready") as codex_ready,
            patch("agent_monitor.engines_registry._cli_available", return_value=(True, "cmd")),
        ):
            engines = lean_verify.harness_engines(
                {"id": 17}, _CLAUDE_RUN, model="claude-haiku-4-5"
            )

        by_id = {item["id"]: item for item in engines}
        self.assertTrue(by_id["claude"]["available"])
        self.assertTrue(by_id["openclaw"]["available"])
        self.assertTrue(by_id["deepagents"]["available"])
        self.assertFalse(by_id["codex"]["available"])
        self.assertFalse(by_id["openhands"]["available"])
        self.assertIn("recorded Claude Code", by_id["codex"]["status_detail"])
        current_runtime.assert_not_called()
        codex_ready.assert_not_called()

    def test_shared_subscription_environment_keeps_runtime_path_without_process_keys(
        self,
    ) -> None:
        def configure(env, *, route, user_id):
            self.assertNotIn("PATH", env)
            self.assertNotIn("HOME", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            return {
                **env,
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "CLAUDE_CONFIG_DIR": "/account/claude",
            }

        with (
            patch.dict(
                os.environ,
                {
                    "PATH": "/runtime/bin",
                    "HOME": "/runtime/home",
                    "OPENAI_API_KEY": "process-openai-must-not-leak",
                    "ANTHROPIC_API_KEY": "process-anthropic-must-not-leak",
                },
                clear=True,
            ),
            patch(
                "agent_monitor.settings.resolved_user_env",
                return_value={"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"},
            ),
            patch(
                "agent_monitor.jobs._configure_auth_route",
                side_effect=configure,
            ) as route,
        ):
            env = lean_verify._llm_environment(
                {"id": 17},
                {
                    "owner_id": 17,
                    "auth_route": "claude_subscription",
                    "model": "claude-haiku-4-5",
                },
            )

        self.assertEqual(env["PATH"], "/runtime/bin")
        self.assertEqual(env["HOME"], "/runtime/home")
        self.assertEqual(env["AGENT_MONITOR_CLAUDE_MODEL"], "claude-haiku-4-5")
        route.assert_called_once()

    def test_shared_api_environment_overlays_only_the_users_provider_key(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "PATH": "/runtime/bin",
                    "OPENAI_API_KEY": "process-openai-must-not-leak",
                    "ANTHROPIC_API_KEY": "process-anthropic-must-not-leak",
                },
                clear=True,
            ),
            patch(
                "agent_monitor.settings.resolved_user_env",
                return_value={"OPENAI_API_KEY": "owner-openai-key"},
            ),
        ):
            env = lean_verify._llm_environment(
                {"id": 17},
                {"owner_id": 17, "auth_route": "api_key"},
            )

        self.assertEqual(env["PATH"], "/runtime/bin")
        self.assertEqual(env["OPENAI_API_KEY"], "owner-openai-key")
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_sponsored_run_uses_formal_scoped_proxy_for_shared_translator(self) -> None:
        run = {
            "run_id": "openclaude-sponsored",
            "owner_id": 70,
            "auth_route": "api_key",
            "credential_source": "sponsored_kimi",
            "model": "kimi-k3",
        }
        proxy_env = {
            "KIMI_API_KEY": "t" * 64,
            "KIMI_API_BASE": "http://127.0.0.1:4610/internal/v1",
        }
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch(
                "agent_monitor.sponsored_kimi_client.issue_credentials",
                return_value=proxy_env,
            ) as issue,
        ):
            env = lean_verify._llm_environment({"id": 70}, run)

        self.assertEqual(env["KIMI_API_KEY"], "t" * 64)
        self.assertEqual(issue.call_args.kwargs["engine"], "formal")
        self.assertEqual(issue.call_args.kwargs["cache_key"], run["run_id"])

    def test_sponsored_formal_lists_and_configures_only_compatible_harnesses(self) -> None:
        run = {
            "run_id": "openclaude-sponsored",
            "owner_id": 70,
            "auth_route": "api_key",
            "credential_source": "sponsored_kimi",
            "model": "kimi-k3",
        }
        proxy_env = {
            "KIMI_API_KEY": "t" * 64,
            "KIMI_API_BASE": "http://127.0.0.1:4610/internal/v1",
            "AGENT_MONITOR_SPONSORED_KIMI": "1",
        }
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch("agent_monitor.engines_registry._cli_available", return_value=(True, "cmd")),
            patch(
                "agent_monitor.sponsored_kimi_client.issue_credentials",
                return_value=proxy_env,
            ) as issue,
            patch.object(
                lean_verify,
                "toolchain_status",
                return_value={
                    "available": True,
                    "lean": "/bin/lean",
                    "lake": None,
                    "elan": None,
                },
            ),
        ):
            engines = lean_verify.harness_engines(
                {"id": 70}, run, model="kimi-k3"
            )
            env = lean_verify._harness_env(
                "openclaude",
                {"id": 70},
                model="kimi-k3",
                run_record=run,
            )
            codex_env = lean_verify._harness_env(
                "codex",
                {"id": 70},
                model="kimi-k3",
                run_record=run,
            )

        by_id = {item["id"]: item for item in engines}
        self.assertTrue(by_id["openclaude"]["available"])
        self.assertTrue(by_id["openclaw"]["available"])
        self.assertTrue(by_id["deepagents"]["available"])
        self.assertTrue(by_id["codex"]["available"])
        self.assertIn("limited proxy", by_id["openclaude"]["auth_detail"])
        self.assertEqual(env["KIMI_API_KEY"], "t" * 64)
        self.assertEqual(codex_env["KIMI_API_KEY"], "t" * 64)
        self.assertEqual(env["AGENT_MONITOR_FORMAL_LEAN_MODE"], "1")
        self.assertEqual(issue.call_args_list[0].kwargs["engine"], "openclaude")
        self.assertEqual(issue.call_args_list[1].kwargs["engine"], "formal")

    def test_recorded_api_route_ignores_current_claude_and_keeps_api_environment(
        self,
    ) -> None:
        api_run = {
            "owner_id": 17,
            "auth_route": "api_key",
            "model": "gpt-5-mini",
        }
        with (
            patch(
                "agent_monitor.settings.resolved_user_env",
                return_value={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
                    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                    "CLAUDE_CONFIG_DIR": "/private/claude",
                    "OPENAI_API_KEY": "api-key",
                },
            ),
            patch("agent_monitor.settings.account_runtime") as current_runtime,
            patch("agent_monitor.claude_login.account_login_ready") as claude_ready,
            patch("agent_monitor.codex_login.account_login_ready") as codex_ready,
            patch("agent_monitor.engines_registry._cli_available", return_value=(True, "cmd")),
            patch.object(
                lean_verify,
                "toolchain_status",
                return_value={
                    "available": True,
                    "lean": "/bin/lean",
                    "lake": None,
                    "elan": None,
                },
            ),
            patch("agent_monitor.jobs._ensure_codex_auth") as ensure_codex,
        ):
            engines = lean_verify.harness_engines({"id": 17}, api_run)
            env = lean_verify._harness_env(
                "codex",
                {"id": 17},
                model="gpt-5-mini",
                run_record=api_run,
            )

        by_id = {item["id"]: item for item in engines}
        self.assertTrue(by_id["codex"]["available"])
        self.assertFalse(by_id["claude"]["available"])
        self.assertEqual(env["AGENT_MONITOR_AUTH_MODE"], "api_key")
        self.assertEqual(env["AGENT_MONITOR_ACCOUNT_RUNTIME"], "api")
        self.assertEqual(env["OPENAI_API_KEY"], "api-key")
        self.assertEqual(env["AGENT_MONITOR_FORMAL_LEAN_MODE"], "1")
        self.assertNotIn("AGENT_MONITOR_CLAUDE_SUBSCRIPTION", env)
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        current_runtime.assert_not_called()
        claude_ready.assert_not_called()
        codex_ready.assert_not_called()
        ensure_codex.assert_not_called()

    def test_unsupported_recorded_claude_engine_fails_before_launch(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("agent_monitor.settings.resolved_user_env", return_value={"OPENAI_API_KEY": "fallback"}),
            patch("agent_monitor.claude_login.account_login_ready", return_value=True),
            patch("agent_monitor.engines_registry._cli_available", return_value=(True, "cmd")),
            patch("agent_monitor.lean_verify.subprocess.Popen") as popen,
            patch("agent_monitor.lean_verify.threading.Thread") as thread,
        ):
            workspace = Path(tmp)
            with self.assertRaisesRegex(ValueError, "Proof.lean"):
                lean_verify.start_harness(
                    workspace=workspace,
                    run_record=_CLAUDE_RUN,
                    user={"id": 17},
                    engine="openhands",
                    model="claude-haiku-4-5",
                )
            self.assertFalse((workspace / lean_verify.LEAN_DIRNAME).exists())

        popen.assert_not_called()
        thread.assert_not_called()

    def test_missing_recorded_claude_login_fails_without_api_fallback(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("agent_monitor.settings.resolved_user_env", return_value={"ANTHROPIC_API_KEY": "fallback"}),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
            patch("agent_monitor.engines_registry._cli_available", return_value=(True, "cmd")),
            patch("agent_monitor.lean_verify.subprocess.Popen") as popen,
            patch("agent_monitor.lean_verify.threading.Thread") as thread,
        ):
            workspace = Path(tmp)
            with self.assertRaisesRegex(ValueError, "Claude Code is not connected"):
                lean_verify.start_harness(
                    workspace=workspace,
                    run_record=_CLAUDE_RUN,
                    user={"id": 17},
                    engine="claude",
                    model="claude-haiku-4-5",
                )
            self.assertFalse((workspace / lean_verify.LEAN_DIRNAME).exists())

        popen.assert_not_called()
        thread.assert_not_called()

    def test_recorded_exact_haiku_is_preflighted_and_passed_to_worker(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_unused):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            account_home = workspace / "claude-account"
            toolchain = {
                "available": True,
                "lean": "/bin/lean",
                "lake": None,
                "elan": None,
            }
            with (
                patch(
                    "agent_monitor.settings.resolved_user_env",
                    return_value={"OPENAI_API_KEY": "must-be-scrubbed"},
                ),
                patch("agent_monitor.claude_login.account_home", return_value=account_home),
                patch("agent_monitor.claude_login.account_login_ready", return_value=True),
                patch("agent_monitor.engines_registry._cli_available", return_value=(True, "echo {prompt}")),
                patch.object(lean_verify, "toolchain_status", return_value=toolchain),
                patch("agent_monitor.lean_verify.threading.Thread", FakeThread),
            ):
                result = lean_verify.start_harness(
                    workspace=workspace,
                    run_record=_CLAUDE_RUN,
                    user={"id": 17},
                    engine="claude",
                    model=None,
                    audit=False,
                )

        self.assertEqual(captured["model"], "claude-haiku-4-5")
        self.assertIs(captured["run_record"], _CLAUDE_RUN)
        self.assertEqual(
            captured["env"]["AGENT_MONITOR_CLAUDE_MODEL"],
            "claude-haiku-4-5",
        )
        self.assertEqual(captured["env"]["AGENT_MONITOR_FORMAL_LEAN_MODE"], "1")
        self.assertEqual(captured["env"]["AGENT_MONITOR_CLAUDE_LEAN_MODE"], "1")
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertEqual(result["harness"]["auth_route"], "claude_subscription")
        self.assertEqual(result["harness"]["model"], "claude-haiku-4-5")
        self.assertEqual(result["model"], "claude-haiku-4-5")

    def test_finished_harness_preserves_route_model_and_normalized_checker_hash(
        self,
    ) -> None:
        source = "theorem main : True := by trivial"
        normalized = source + "\n"
        normalized_sha = hashlib.sha256(normalized.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            lean_dir = workspace / lean_verify.LEAN_DIRNAME
            lean_dir.mkdir()
            (lean_dir / lean_verify.PROOF_FILENAME).write_text(source, encoding="utf-8")
            lean_verify._write_cached(
                workspace,
                {
                    "status": "running",
                    "action": "harness",
                    "model": "claude-haiku-4-5",
                    "harness": {
                        "engine": "deepagents",
                        "auth_route": "claude_subscription",
                        "model": "claude-haiku-4-5",
                    },
                },
            )
            check = {
                "ok": True,
                "status": "verified",
                "exit_code": 0,
                "duration_s": 0.01,
                "command": ["lean-broker", "core"],
                "toolchain": "Lean test",
                "sandbox": "systemd-socket-dynamic-user-v1",
                "source_sha256": normalized_sha,
                "stderr": "",
                "stdout": "",
            }
            with (
                patch.object(lean_verify, "_write_and_run", return_value=check),
                patch.object(lean_verify, "toolchain_status", return_value={}),
            ):
                result = lean_verify._harness_finish(
                    workspace=workspace,
                    engine="deepagents",
                    label="DeepAgents",
                    log="finished",
                    exit_code=0,
                    elapsed=1.0,
                    user={"id": 17},
                    run_record=_CLAUDE_RUN,
                    model=None,
                    problem="Prove True.",
                    informal="Trivial.",
                    audit=False,
                    initial_fingerprint=None,
                    stopped=False,
                )

        self.assertEqual(result["model"], "claude-haiku-4-5")
        self.assertEqual(result["harness"]["auth_route"], "claude_subscription")
        self.assertEqual(result["harness"]["model"], "claude-haiku-4-5")
        self.assertEqual(result["harness"]["final_sha256"], normalized_sha)
        self.assertEqual(result["attempts"][-1]["artifact"]["final_sha256"], normalized_sha)

    def test_post_run_fidelity_audit_uses_recorded_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            lean_dir = workspace / lean_verify.LEAN_DIRNAME
            lean_dir.mkdir()
            (lean_dir / lean_verify.PROOF_FILENAME).write_text(
                "theorem main : True := by trivial\n",
                encoding="utf-8",
            )
            checked = {
                "ok": True,
                "status": "verified",
                "exit_code": 0,
                "duration_s": 0.01,
                "command": ["lean", "Proof.lean"],
                "toolchain": {},
                "stderr": "",
                "stdout": "",
            }
            with (
                patch.object(lean_verify, "_write_and_run", return_value=checked),
                patch.object(
                    lean_verify,
                    "_llm_environment",
                    return_value={"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1"},
                ) as llm_environment,
                patch.object(
                    lean_verify,
                    "_audit_faithfulness",
                    return_value={"ok": True, "model": "claude-haiku-4-5"},
                ),
                patch.object(lean_verify, "toolchain_status", return_value={}),
            ):
                lean_verify._harness_finish(
                    workspace=workspace,
                    engine="claude",
                    label="Claude Code",
                    log="",
                    exit_code=0,
                    elapsed=0.1,
                    user={"id": 17},
                    run_record=_CLAUDE_RUN,
                    model="claude-haiku-4-5",
                    problem="Prove True.",
                    informal="Trivial.",
                    audit=True,
                    initial_fingerprint=None,
                    stopped=False,
                )

        llm_environment.assert_called_once_with({"id": 17}, _CLAUDE_RUN)

    def test_recorded_codex_route_ignores_current_api(self) -> None:
        codex_run = {
            "owner_id": 17,
            "auth_route": "codex_subscription",
            "model": "gpt-5.6-sol",
        }
        with (
            patch(
                "agent_monitor.settings.resolved_user_env",
                return_value={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
                    "OPENAI_API_KEY": "must-not-be-used",
                },
            ),
            patch("agent_monitor.settings.account_runtime") as current_runtime,
            patch("agent_monitor.codex_login.account_login_ready", return_value=True),
            patch("agent_monitor.claude_login.account_login_ready") as claude_ready,
            patch("agent_monitor.engines_registry._cli_available", return_value=(True, "cmd")),
        ):
            engines = lean_verify.harness_engines({"id": 17}, codex_run)

        by_id = {item["id"]: item for item in engines}
        self.assertTrue(by_id["codex"]["available"])
        self.assertFalse(by_id["claude"]["available"])
        self.assertEqual(by_id["codex"]["auth_detail"], "Codex subscription")
        current_runtime.assert_not_called()
        claude_ready.assert_not_called()

    def test_only_runners_with_a_real_proof_lean_contract_are_advertised(self) -> None:
        codex_run = {
            "owner_id": 17,
            "auth_route": "codex_subscription",
            "model": "gpt-5.6-sol",
        }
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch("agent_monitor.codex_login.account_login_ready", return_value=True),
            patch("agent_monitor.engines_registry._cli_available", return_value=(True, "cmd")),
        ):
            engines = lean_verify.harness_engines({"id": 17}, codex_run)

        by_id = {item["id"]: item for item in engines}
        for engine in ("codex", "openclaude", "openclaw", "deepagents"):
            with self.subTest(engine=engine):
                self.assertTrue(by_id[engine]["formal_lean_supported"])
                self.assertTrue(by_id[engine]["available"])
        for engine in ("openhands", "deepseek_harness", "danus"):
            with self.subTest(engine=engine):
                self.assertFalse(by_id[engine]["formal_lean_supported"])
                self.assertFalse(by_id[engine]["available"])
                self.assertIn("Proof.lean", by_id[engine]["status_detail"])

    def test_false_formal_harness_fails_before_workspace_or_process_mutation(self) -> None:
        run = {
            "owner_id": 17,
            "auth_route": "codex_subscription",
            "model": "gpt-5.6-sol",
        }
        for engine in ("openhands", "deepseek_harness", "danus"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                with (
                    patch("agent_monitor.settings.resolved_user_env", return_value={}),
                    patch("agent_monitor.codex_login.account_login_ready", return_value=True),
                    patch("agent_monitor.engines_registry._cli_available", return_value=(True, "cmd")),
                    patch("agent_monitor.lean_verify.subprocess.Popen") as popen,
                    patch("agent_monitor.lean_verify.threading.Thread") as thread,
                ):
                    with self.assertRaisesRegex(ValueError, "Proof.lean"):
                        lean_verify.start_harness(
                            workspace=workspace,
                            run_record=run,
                            user={"id": 17},
                            engine=engine,
                            model="gpt-5.6-sol",
                        )
                self.assertFalse((workspace / lean_verify.LEAN_DIRNAME).exists())
                popen.assert_not_called()
                thread.assert_not_called()

    def test_recorded_api_interaction_never_falls_back_to_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("agent_monitor.settings.codex_enabled", return_value=True),
                patch("agent_monitor.codex_login.account_login_ready") as ready,
                patch("agent_monitor.lean_verify.subprocess.run") as run,
            ):
                with self.assertRaisesRegex(ValueError, "cannot silently fall back"):
                    lean_verify._codex_subscription_interaction(
                        workspace=Path(tmp),
                        user={"id": 17},
                        run_record={"auth_route": "api_key", "owner_id": 17},
                        prompt="Audit.",
                        lean_source="theorem main : True := by trivial",
                        edit=False,
                    )

        ready.assert_not_called()
        run.assert_not_called()

    def test_recorded_codex_fidelity_audit_never_tries_direct_api(self) -> None:
        codex_run = {
            "owner_id": 17,
            "auth_route": "codex_subscription",
            "model": "gpt-5.6-sol",
        }
        answer = (
            '{"faithful":true,"severity":"ok","verdict":"matches",'
            '"issues":[],"main_decl":"main","statement_suggestion":""}'
        )
        with (
            patch.object(
                lean_verify,
                "_codex_subscription_interaction",
                return_value={
                    "answer": answer,
                    "lean": "theorem main : True := by trivial",
                    "model": "gpt-5.6-sol",
                },
            ) as codex,
            patch.object(lean_verify, "_audit_faithfulness") as direct_api,
        ):
            result = lean_verify._harness_fidelity_audit(
                workspace=Path("/tmp/formal-route-test"),
                user={"id": 17},
                run_record=codex_run,
                model="gpt-5.6-sol",
                problem="Prove True.",
                proof="Trivial.",
                lean="theorem main : True := by trivial",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["faithful"])
        self.assertIs(codex.call_args.kwargs["run_record"], codex_run)
        direct_api.assert_not_called()


    def test_console_uses_per_run_engine_and_model_lists(self) -> None:
        html = Path("agent_monitor/web/console.html").read_text(encoding="utf-8")
        self.assertIn("d&&Array.isArray(d.engines)", html)
        self.assertIn("fillLeanModels(d.harness_models,d.harness_model)", html)


if __name__ == "__main__":
    unittest.main()
