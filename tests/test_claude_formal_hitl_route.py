from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import console_server, lean_verify


CLAUDE_MODEL = "claude-haiku-4-5"


class ClaudeFormalHitlRouteTests(unittest.TestCase):
    def test_recorded_claude_question_ignores_current_api_runtime(self) -> None:
        run_record = {
            "auth_route": "claude_subscription",
            "model": CLAUDE_MODEL,
            "owner_id": 7,
        }
        configured = {
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "AGENT_MONITOR_CLAUDE_MODEL": CLAUDE_MODEL,
            "CLAUDE_CONFIG_DIR": "/tmp/claude-user-7",
        }
        with (
            patch(
                "agent_monitor.settings.resolved_user_env",
                return_value={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
                    "OPENAI_API_KEY": "must-not-be-used",
                },
            ),
            patch(
                "agent_monitor.jobs._configure_auth_route",
                return_value=configured,
            ) as configure,
            patch.object(
                lean_verify,
                "_chat",
                return_value=(CLAUDE_MODEL, "The induction step is faithful."),
            ) as chat,
            patch.object(lean_verify, "_codex_subscription_interaction") as codex,
        ):
            selected, answer = lean_verify._answer_lean_question(
                workspace=Path("/tmp"),
                user={"id": 7},
                run_record=run_record,
                question="Why does the induction step preserve the claim?",
                model=CLAUDE_MODEL,
                lean_src="theorem main : True := by trivial",
                cached={"status": "verified", "fidelity": {}, "model": CLAUDE_MODEL},
            )

        self.assertEqual(selected, CLAUDE_MODEL)
        self.assertEqual(answer, "The induction step is faithful.")
        configure.assert_called_once_with(
            {
                "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
                "OPENAI_API_KEY": "must-not-be-used",
            },
            route="claude_subscription",
            user_id=7,
        )
        self.assertEqual(chat.call_args.args[0]["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"], "1")
        self.assertNotIn("OPENAI_API_KEY", chat.call_args.args[0])
        self.assertEqual(chat.call_args.args[1], CLAUDE_MODEL)
        codex.assert_not_called()

    def test_recorded_api_question_does_not_switch_to_current_claude_runtime(self) -> None:
        with (
            patch(
                "agent_monitor.settings.resolved_user_env",
                return_value={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
                    "ANTHROPIC_API_KEY": "api-key",
                },
            ),
            patch("agent_monitor.jobs._configure_auth_route") as configure,
            patch.object(
                lean_verify,
                "_call_llm",
                return_value=(CLAUDE_MODEL, "API-route answer.", "anthropic"),
            ) as direct_api,
            patch(
                "agent_monitor.runners.claude_backend.claude_exec"
            ) as claude_subscription,
        ):
            selected, answer = lean_verify._answer_lean_question(
                workspace=Path("/tmp"),
                user={"id": 7},
                run_record={"auth_route": "api_key", "model": CLAUDE_MODEL},
                question="Why is the statement faithful?",
                model=CLAUDE_MODEL,
                lean_src="theorem main : True := by trivial",
                cached={"status": "verified", "fidelity": {}, "model": CLAUDE_MODEL},
            )

        self.assertEqual((selected, answer), (CLAUDE_MODEL, "API-route answer."))
        direct_api.assert_called_once()
        configure.assert_not_called()
        claude_subscription.assert_not_called()

    def test_missing_recorded_claude_login_fails_closed_and_preserves_lean(self) -> None:
        source = "theorem main : True := by\n  trivial\n"
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            proof_path = ldir / lean_verify.PROOF_FILENAME
            proof_path.write_text(source, encoding="utf-8")
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps(
                    {
                        "status": "verified",
                        "lean": source,
                        "model": CLAUDE_MODEL,
                        "chat": [],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch(
                    "agent_monitor.settings.resolved_user_env",
                    return_value={"AGENT_MONITOR_ACCOUNT_RUNTIME": "api"},
                ),
                patch(
                    "agent_monitor.jobs._configure_auth_route",
                    side_effect=ValueError("Claude Code is not connected"),
                ),
                patch.object(lean_verify, "harness_job", return_value=None),
                patch.object(lean_verify, "_call_llm") as direct_api,
                patch.object(lean_verify, "_codex_subscription_interaction") as codex,
            ):
                with self.assertRaisesRegex(ValueError, "not connected"):
                    lean_verify.revise_with_feedback(
                        workspace=workspace,
                        user={"id": 7},
                        run_record={
                            "auth_route": "claude_subscription",
                            "model": CLAUDE_MODEL,
                            "owner_id": 7,
                        },
                        message="Strengthen the proof without changing the theorem statement.",
                        model=CLAUDE_MODEL,
                    )

            self.assertEqual(proof_path.read_text(encoding="utf-8"), source)
            cached = lean_verify.load_cached(workspace) or {}
            chat = cached.get("chat") or []
            self.assertEqual([item.get("role") for item in chat], ["user", "assistant"])
            self.assertIn("not connected", str(chat[-1].get("content")))
            direct_api.assert_not_called()
            codex.assert_not_called()

    def test_http_revise_passes_owned_run_record(self) -> None:
        run_record = {
            "run_id": "claude-demo",
            "auth_route": "claude_subscription",
            "model": CLAUDE_MODEL,
            "owner_id": 7,
        }
        body = json.dumps(
            {
                "action": "revise",
                "message": "Check the boundary case.",
                "model": CLAUDE_MODEL,
            }
        ).encode()
        handler = object.__new__(console_server.Handler)
        handler.path = "/api/run/claude-demo/lean_verify"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._resolve_path = lambda: "/api/run/claude-demo/lean_verify"
        handler._require_user = lambda _path: {"id": 7, "is_admin": False}
        handler._owns_run = lambda _rid, _user: True
        sent: list[tuple[int, dict]] = []
        handler._send = lambda code, payload, *args, **kwargs: sent.append(
            (code, json.loads(payload))
        )

        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(console_server, "_workspace_for_run", return_value=Path(td)),
            patch.object(console_server, "_run_record_for", return_value=run_record),
            patch.object(
                lean_verify,
                "revise_with_feedback",
                return_value={"status": "running"},
            ) as revise,
        ):
            handler.do_POST()

        self.assertEqual(sent[0][0], 200)
        self.assertIs(revise.call_args.kwargs["run_record"], run_record)
        self.assertEqual(revise.call_args.kwargs["model"], CLAUDE_MODEL)


if __name__ == "__main__":
    unittest.main()
