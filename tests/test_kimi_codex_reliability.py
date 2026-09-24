from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from agent_monitor import kimi_responses_proxy
from agent_monitor.runners import codex_backend, kimi_codex_runner
from agent_monitor.runners.codex_backend import CodexBackendError, CodexResult


class KimiResponsesReliabilityTests(unittest.TestCase):
    def test_relay_uses_read1_and_flushes_each_available_chunk(self) -> None:
        upstream = Mock()
        upstream.read1.side_effect = [b"data: first\n\n", b"data: second\n\n", b""]
        upstream.read.side_effect = AssertionError("buffering read must not be used")
        downstream = Mock()

        kimi_responses_proxy._relay_body(upstream, downstream)

        self.assertEqual(downstream.write.call_count, 2)
        self.assertEqual(downstream.flush.call_count, 2)
        upstream.read.assert_not_called()

    def test_retry_after_allows_only_valid_delta_or_http_date(self) -> None:
        self.assertEqual(kimi_responses_proxy._safe_retry_after("17"), "17")
        date = "Wed, 21 Oct 2015 07:28:00 GMT"
        self.assertEqual(kimi_responses_proxy._safe_retry_after(date), date)
        self.assertIsNone(kimi_responses_proxy._safe_retry_after("soon"))
        self.assertIsNone(kimi_responses_proxy._safe_retry_after("1\r\nX-Evil: yes"))

    def test_upstream_timeout_is_configurable_and_bounded(self) -> None:
        with patch.dict(os.environ, {"KIMI_UPSTREAM_TIMEOUT": "1800"}, clear=False):
            self.assertEqual(kimi_responses_proxy._configured_upstream_timeout(), 1800)
        self.assertEqual(kimi_responses_proxy._configured_upstream_timeout("1"), 30)
        self.assertEqual(kimi_responses_proxy._configured_upstream_timeout("99999"), 7200)
        self.assertEqual(
            kimi_responses_proxy._configured_upstream_timeout("invalid"),
            kimi_responses_proxy._DEFAULT_UPSTREAM_TIMEOUT,
        )


class CodexTerminalReliabilityTests(unittest.TestCase):
    def _call_with_stdout(self, stdout: str) -> CodexResult:
        def fake_run(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION + "\n",
                    stderr="",
                )
            if command[1:] == ["debug", "models", "--bundled"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"models": [{"slug": "gpt-5.6-sol"}]}),
                    stderr="",
                )
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text("finished", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with (
            patch.dict(os.environ, {"CODEX_HOME": "/tmp/codex-test"}, clear=True),
            patch.object(
                codex_backend.shutil, "which", return_value="/usr/local/bin/codex"
            ),
            patch.object(codex_backend, "_run_codex_process", side_effect=fake_run),
        ):
            return codex_backend.codex_exec("system", "user")

    def test_zero_exit_without_terminal_event_is_failure(self) -> None:
        with self.assertRaisesRegex(CodexBackendError, "without a terminal turn"):
            self._call_with_stdout(json.dumps({"type": "item.completed"}))

    def test_failed_terminal_event_is_failure_even_with_zero_exit(self) -> None:
        failed = {"type": "turn.failed", "error": {"message": "upstream failed"}}
        with self.assertRaisesRegex(CodexBackendError, "turn.failed"):
            self._call_with_stdout(json.dumps(failed))

    def test_completed_terminal_event_succeeds(self) -> None:
        result = self._call_with_stdout(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            )
        )
        self.assertEqual(result.usage, {"input_tokens": 3, "output_tokens": 2})

    def test_timeout_cleanup_terminates_then_kills_process_group(self) -> None:
        process = Mock()
        process.pid = 4321
        process.poll.return_value = None
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("codex", 2),
            ("", ""),
        ]
        with (
            patch.object(codex_backend.os, "name", "posix"),
            patch.object(codex_backend.os, "killpg") as killpg,
        ):
            codex_backend._stop_process_group(process)

        self.assertEqual(
            killpg.call_args_list,
            [
                call(4321, signal.SIGTERM),
                call(4321, signal.SIGKILL),
            ],
        )


class KimiArtifactReliabilityTests(unittest.TestCase):
    def test_stale_preexisting_artifact_does_not_count_as_success(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path("proof.md").write_text(
                    "# Existing\n\nThis is a substantive but stale proof artifact. " * 3,
                    encoding="utf-8",
                )
                with (
                    patch.dict(os.environ, {"AGENT_MONITOR_SELECTED_MODEL": "kimi-k3"}),
                    patch.object(os.sys, "argv", ["kimi_codex_runner.py", "prove it"]),
                    patch.object(
                        kimi_codex_runner,
                        "codex_exec",
                        return_value=CodexResult("done", {}),
                    ),
                ):
                    self.assertEqual(kimi_codex_runner.main(), 1)
            finally:
                os.chdir(previous)

    def test_changed_substantive_artifact_counts_as_success(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path("proof.md").write_text(
                    "# Existing\n\nThis is a substantive but stale proof artifact. " * 3,
                    encoding="utf-8",
                )

                def update(*_args: object, **_kwargs: object) -> CodexResult:
                    Path("proof.md").write_text(
                        "# New proof\n\nA corrected, substantive mathematical argument. " * 4,
                        encoding="utf-8",
                    )
                    return CodexResult("done", {})

                with (
                    patch.dict(os.environ, {"AGENT_MONITOR_SELECTED_MODEL": "kimi-k3"}),
                    patch.object(os.sys, "argv", ["kimi_codex_runner.py", "prove it"]),
                    patch.object(kimi_codex_runner, "codex_exec", side_effect=update),
                ):
                    self.assertEqual(kimi_codex_runner.main(), 0)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
