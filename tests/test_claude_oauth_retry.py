from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_monitor import claude_login
from agent_monitor.runners import claude_backend, metaharness_runner


def _expired(
    *,
    with_activity: bool = False,
    with_auth_diagnostic: bool = False,
) -> claude_backend._ProcessResult:
    events: list[dict] = [
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-haiku-4-5",
        }
    ]
    if with_activity:
        events.append(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-haiku-4-5-20251001",
                    "content": [{"type": "text", "text": "acted"}],
                },
            }
        )
    if with_auth_diagnostic:
        events.append(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-haiku-4-5",
                    "content": [{"type": "text", "text": (
                        "Failed to authenticate. API Error: 401 OAuth access "
                        "token has expired. Re-authenticate to continue."
                    )}],
                },
            }
        )
    events.append(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": (
                "Failed to authenticate. API Error: 401 OAuth access token "
                "has expired. Re-authenticate to continue."
            ),
        }
    )
    return claude_backend._ProcessResult(1, "", tuple(events))


def _success() -> claude_backend._ProcessResult:
    model = "claude-haiku-4-5-20251001"
    return claude_backend._ProcessResult(
        0,
        "",
        (
            {
                "type": "system",
                "subtype": "init",
                "model": "claude-haiku-4-5",
            },
            {
                "type": "assistant",
                "message": {
                    "model": model,
                    "content": [{"type": "text", "text": "Completed."}],
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Completed.",
                "usage": {"input_tokens": 2, "output_tokens": 3},
                "modelUsage": {
                    model: {"canonicalModel": model}
                },
            },
        ),
    )


def _env(config: str) -> dict[str, str]:
    return {
        "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
        "CLAUDE_CONFIG_DIR": config,
        "PATH": os.environ.get("PATH", ""),
    }


class ClaudeOAuthRetryTests(unittest.TestCase):
    def test_response_retries_one_preinference_expired_token(self) -> None:
        with (
            tempfile.TemporaryDirectory() as config,
            patch.object(
                claude_backend,
                "_run_stream",
                side_effect=[_expired(), _success()],
            ) as run,
            patch.object(
                claude_backend, "_claude_binary", return_value="/bin/claude"
            ),
            patch.object(claude_backend.time, "sleep") as sleep,
        ):
            result = claude_backend.claude_exec(
                "system",
                "user",
                model="claude-haiku-4-5",
                source_env=_env(config),
            )

        self.assertEqual(result.model, "claude-haiku-4-5-20251001")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_response_does_not_replay_after_model_activity(self) -> None:
        with (
            tempfile.TemporaryDirectory() as config,
            patch.object(
                claude_backend,
                "_run_stream",
                side_effect=[_expired(with_activity=True), _success()],
            ) as run,
            patch.object(
                claude_backend, "_claude_binary", return_value="/bin/claude"
            ),
            patch.object(claude_backend.time, "sleep") as sleep,
            patch.object(claude_login, "mark_reauth_required") as mark,
            patch.object(claude_login, "clear_reauth_required") as clear,
            self.assertRaisesRegex(
                claude_backend.ClaudeBackendError,
                "OAuth access token has expired",
            ),
        ):
            claude_backend.claude_exec(
                "system",
                "user",
                model="claude-haiku-4-5",
                source_env=_env(config),
            )

        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        mark.assert_called_once_with(
            str(Path(config).resolve()), reason="oauth_refresh_failed"
        )
        clear.assert_not_called()

    def test_zero_token_auth_diagnostic_retries_and_clears_marker(self) -> None:
        with (
            tempfile.TemporaryDirectory() as config,
            patch.object(
                claude_backend,
                "_run_stream",
                side_effect=[_expired(with_auth_diagnostic=True), _success()],
            ) as run,
            patch.object(claude_backend, "_claude_binary", return_value="/bin/claude"),
            patch.object(claude_backend.time, "sleep") as sleep,
            patch.object(claude_login, "mark_reauth_required") as mark,
            patch.object(claude_login, "clear_reauth_required") as clear,
        ):
            result = claude_backend.claude_exec(
                "system",
                "user",
                model="claude-haiku-4-5",
                source_env=_env(config),
            )

        self.assertEqual(result.model, "claude-haiku-4-5-20251001")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(1.0)
        mark.assert_not_called()
        clear.assert_called_once_with(str(Path(config).resolve()))


    def test_final_oauth_401_marks_reauth_and_never_reports_success(self) -> None:
        with (
            tempfile.TemporaryDirectory() as config,
            patch.object(
                claude_backend,
                "_run_stream",
                side_effect=[
                    _expired(with_auth_diagnostic=True),
                    _expired(with_auth_diagnostic=True),
                ],
            ) as run,
            patch.object(claude_backend, "_claude_binary", return_value="/bin/claude"),
            patch.object(claude_backend.time, "sleep") as sleep,
            patch.object(claude_login, "mark_reauth_required") as mark,
            patch.object(claude_login, "clear_reauth_required") as clear,
            self.assertRaises(
                claude_backend.ClaudeAuthenticationError
            ) as raised,
        ):
            claude_backend.claude_exec(
                "system",
                "user",
                model="claude-haiku-4-5",
                source_env=_env(config),
            )

        message = str(raised.exception)
        self.assertIn("subscription authentication failed", message)
        self.assertIn("reconnect Claude Code", message)
        self.assertNotIn("reported success", message)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(1.0)
        mark.assert_called_once_with(
            str(Path(config).resolve()), reason="oauth_refresh_failed"
        )
        clear.assert_not_called()


    def test_agentic_retry_requires_an_unchanged_target_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as config:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text("S" * 500, encoding="utf-8")

            def mutate_then_expire(*_args, **_kwargs):
                proof.write_text("M" * 500, encoding="utf-8")
                return _expired()

            with (
                patch.object(
                    claude_backend,
                    "_run_stream",
                    side_effect=mutate_then_expire,
                ) as run,
                patch.object(
                    claude_backend, "_claude_binary", return_value="/bin/claude"
                ),
                patch.object(claude_backend.time, "sleep") as sleep,
                self.assertRaisesRegex(
                    claude_backend.ClaudeBackendError,
                    "OAuth access token has expired",
                ),
            ):
                claude_backend.claude_proof(
                    "Prove it.",
                    workspace,
                    model="claude-haiku-4-5",
                    source_env=_env(config),
                )

            self.assertEqual(run.call_count, 1)
            sleep.assert_not_called()

    def test_agentic_retry_accepts_a_fresh_artifact_from_second_process(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as config:
            workspace = Path(td)
            proof = workspace / "proof.md"

            def complete(*_args, **_kwargs):
                proof.write_text(
                    "# Proof\n\n" + "A rigorous derivation. " * 30
                    + "\n\n## References\n\nself-derived.\n\n"
                    + "ProvingConsole outcome: Solved\n",
                    encoding="utf-8",
                )
                return _success()

            calls = 0

            def expire_then_complete(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return _expired()
                return complete(*args, **kwargs)

            with (
                patch.object(
                    claude_backend,
                    "_run_stream",
                    side_effect=expire_then_complete,
                ) as run,
                patch.object(
                    claude_backend, "_claude_binary", return_value="/bin/claude"
                ),
                patch.object(claude_backend.time, "sleep") as sleep,
            ):
                result = claude_backend.claude_proof(
                    "End with ProvingConsole outcome: Solved.",
                    workspace,
                    model="claude-haiku-4-5",
                    source_env=_env(config),
                )

            self.assertEqual(result.artifact, proof)
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(1.0)


    def test_auth_error_projection_never_emits_a_completed_turn(self) -> None:
        projected: list[dict] = []
        for event in _expired(with_auth_diagnostic=True).events:
            claude_backend.forward_as_codex_event(event, projected.append)
        self.assertEqual(
            [event["type"] for event in projected],
            ["thread.started"],
        )


    def test_metaharness_auth_failure_is_one_precise_error(self) -> None:
        process = _expired(with_auth_diagnostic=True)

        def fail(*_args, **kwargs):
            callback = kwargs["emit_event"]
            for event in process.events:
                callback(event)
            return claude_backend._terminal_result(process)

        with tempfile.TemporaryDirectory() as td:
            output = io.StringIO()
            with (
                patch.object(
                    metaharness_runner,
                    "claude_code_exec",
                    side_effect=fail,
                ),
                patch.object(
                    metaharness_runner,
                    "claude_subscription_enabled",
                    return_value=True,
                ),
                patch.object(
                    metaharness_runner,
                    "subscription_enabled",
                    return_value=False,
                ),
                patch.object(
                    metaharness_runner,
                    "api_chat",
                    side_effect=AssertionError("API route must not run"),
                ),
                patch.object(
                    metaharness_runner,
                    "codex_exec",
                    side_effect=AssertionError("Codex route must not run"),
                ),
                patch.object(
                    metaharness_runner.Path,
                    "cwd",
                    return_value=Path(td),
                ),
                patch.object(
                    sys,
                    "argv",
                    ["metaharness_runner.py", "prove 1 + 1 = 2"],
                ),
                patch.dict(
                    os.environ,
                    {"AGENT_MONITOR_CLAUDE_MODEL": "claude-haiku-4-5"},
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                returncode = metaharness_runner.main()

            events = [
                json.loads(line)
                for line in output.getvalue().splitlines()
                if line.strip()
            ]
            self.assertEqual(returncode, 1)
            self.assertFalse((Path(td) / "proof.md").exists())

        errors = [
            event["item"]["message"]
            for event in events
            if event.get("type") == "item.completed"
            and (event.get("item") or {}).get("type") == "error"
        ]
        self.assertEqual(len(errors), 1)
        self.assertIn("subscription authentication failed", errors[0])
        self.assertNotIn("reported success", errors[0])
        self.assertFalse(
            any(event.get("type") == "turn.completed" for event in events)
        )


if __name__ == "__main__":
    unittest.main()
