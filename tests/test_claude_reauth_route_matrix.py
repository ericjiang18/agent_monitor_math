from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import claude_login, jobs, lean_verify, proof_graph


CLAUDE_ENGINES = (
    "plain",
    "metaharness",
    "claude",
    "openclaw",
    "deepagents",
    "improof",
)
FORMAL_CLAUDE_ENGINES = ("claude", "openclaw", "deepagents")
MODEL = "claude-haiku-4-5"
USER = {"id": 71}


def _engine_info(engine: str) -> dict:
    return {
        "id": engine,
        "label": engine,
        "available": True,
        "auth_modes": ["claude_subscription"],
    }


class ClaudeReauthRouteMatrixTests(unittest.TestCase):
    def test_marker_blocks_every_primary_and_continuation_harness_prelaunch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "claude-account"
            claude_login.mark_reauth_required(home)
            auth = {
                "loggedIn": True,
                "authMethod": "oauth",
                "apiProvider": "firstParty",
            }
            with (
                patch.object(claude_login, "account_home", return_value=home),
                patch.object(claude_login, "_auth_status", return_value=auth),
                patch("agent_monitor.jobs._user_extra_env", return_value={}),
                patch(
                    "agent_monitor.engines_registry.all_engine_ids",
                    return_value=set(CLAUDE_ENGINES),
                ),
                patch(
                    "agent_monitor.engines_registry.supported_models",
                    return_value=[MODEL],
                ),
                patch("agent_monitor.jobs.threading.Thread") as thread,
                patch("agent_monitor.jobs.workspace_dir") as workspace_dir,
            ):
                for engine in CLAUDE_ENGINES:
                    info = _engine_info(engine)
                    with (
                        self.subTest(engine=engine, path="primary"),
                        patch(
                            "agent_monitor.engines_registry.list_engines",
                            return_value=[info],
                        ),
                        self.assertRaisesRegex(
                            ValueError, "Claude Code is not connected"
                        ),
                    ):
                        jobs.start_job(
                            engine=engine,
                            problem_text="Prove True.",
                            model=MODEL,
                            user=USER,
                            auth_route="claude_subscription",
                        )

                    run = {
                        "owner_id": USER["id"],
                        "auth_route": "claude_subscription",
                        "model": MODEL,
                    }
                    with (
                        self.subTest(engine=engine, path="continuation"),
                        patch(
                            "agent_monitor.engines_registry.list_engines",
                            return_value=[info],
                        ),
                        self.assertRaisesRegex(
                            ValueError, "Claude Code is not connected"
                        ),
                    ):
                        jobs._continuation_auth_environment(
                            run_data=run,
                            engine=engine,
                            model=MODEL,
                            user=USER,
                            env={},
                        )

        thread.assert_not_called()
        workspace_dir.assert_not_called()

    def test_marker_blocks_dag_lean_hitl_and_manual_formal_before_claude(
        self,
    ) -> None:
        run = {
            "run_id": "claude-marked",
            "owner_id": USER["id"],
            "auth_route": "claude_subscription",
            "model": MODEL,
        }
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "claude-account"
            claude_login.mark_reauth_required(home)
            auth = {
                "loggedIn": True,
                "authMethod": "oauth",
                "apiProvider": "firstParty",
            }
            with (
                patch.object(claude_login, "account_home", return_value=home),
                patch.object(claude_login, "_auth_status", return_value=auth),
                patch(
                    "agent_monitor.settings.resolved_user_env",
                    return_value={},
                ),
                patch(
                    "agent_monitor.runners.claude_backend.claude_exec"
                ) as claude_exec,
            ):
                with self.subTest(path="informal-or-formal-dag"):
                    with self.assertRaisesRegex(
                        ValueError, "Claude Code is not connected"
                    ):
                        proof_graph._run_claude_prompt(
                            USER, "analyze", model=MODEL
                        )

                with self.subTest(path="automatic-lean"):
                    with self.assertRaisesRegex(
                        ValueError, "Claude Code is not connected"
                    ):
                        lean_verify._llm_environment(USER, run)

                for engine in FORMAL_CLAUDE_ENGINES:
                    with (
                        self.subTest(path="manual-formal", engine=engine),
                        self.assertRaisesRegex(
                            ValueError, "Claude Code is not connected"
                        ),
                    ):
                        lean_verify._harness_env(
                            engine,
                            USER,
                            model=MODEL,
                            run_record=run,
                        )

                with self.subTest(path="lean-hitl"):
                    with self.assertRaisesRegex(
                        ValueError, "Claude Code is not connected"
                    ):
                        lean_verify._answer_lean_question(
                            workspace=Path(tmp),
                            user=USER,
                            run_record=run,
                            question="Why does this compile?",
                            model=MODEL,
                            lean_src="theorem t : True := by trivial",
                            cached={"status": "verified"},
                        )

        claude_exec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
