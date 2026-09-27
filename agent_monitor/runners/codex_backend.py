"""Shared one-shot Codex CLI backend for subscription-authenticated runners."""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class CodexBackendError(RuntimeError):
    """Raised when a one-shot Codex CLI call cannot produce a response."""


@dataclass(frozen=True)
class CodexResult:
    text: str
    usage: dict[str, int]


def _read_only_sandbox_args() -> list[str]:
    """Select Landlock when Linux cannot create bubblewrap's loopback."""
    configured = os.environ.get("AGENT_MONITOR_CODEX_LEGACY_LANDLOCK")
    enabled = sys.platform.startswith("linux") if configured is None else (
        configured.strip().lower() in {"1", "true", "yes", "on"}
    )
    args: list[str] = []
    if enabled:
        args.extend(["--enable", "use_legacy_landlock"])
    args.extend(["--sandbox", "read-only"])
    return args


def _legacy_landlock_notice(event: dict) -> bool:
    """Recognize the CLI's nonfatal deprecation notice, not real errors."""
    item = event.get("item") or {}
    message = str(item.get("message") or "")
    return (
        event.get("type") == "item.completed"
        and item.get("type") == "error"
        and "use_legacy_landlock" in message
        and "deprecated" in message.lower()
    )


def subscription_enabled() -> bool:
    return os.environ.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"


def codex_exec(
    system: str,
    user: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
    emit_event: Callable[[dict], None] | None = None,
) -> CodexResult:
    """Run one isolated ``codex exec`` turn using the inherited ``CODEX_HOME``."""
    if not os.environ.get("CODEX_HOME", "").strip():
        raise CodexBackendError("CODEX_HOME is required for Codex subscription calls")
    if timeout is None:
        try:
            timeout = float(os.environ.get("AGENT_MONITOR_CODEX_TIMEOUT", "600"))
        except ValueError as exc:
            raise CodexBackendError("AGENT_MONITOR_CODEX_TIMEOUT must be a number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise CodexBackendError("Codex timeout must be greater than zero")

    prompt = f"SYSTEM INSTRUCTIONS:\n{system}\n\nUSER REQUEST:\n{user}"
    codex = shutil.which("codex")
    if not codex:
        raise CodexBackendError("codex executable was not found")
    allowed_env = (
        "PATH",
        "HOME",
        "CODEX_HOME",
        "LANG",
        "LC_ALL",
        "TERM",
        "NO_COLOR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "AGENT_MONITOR_PYTHON",
    )
    env = {key: os.environ[key] for key in allowed_env if key in os.environ}
    env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    env["NO_COLOR"] = "1"
    with tempfile.TemporaryDirectory(prefix="agent-monitor-codex-") as temp_dir:
        last_message = Path(temp_dir) / "last-message.txt"
        command = [
            codex,
            "exec",
            "--json",
            "--output-last-message",
            str(last_message),
            *_read_only_sandbox_args(),
            "--skip-git-repo-check",
        ]
        if model:
            command.extend(["--model", model])
        command.append("-")

        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexBackendError(f"codex exec timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise CodexBackendError(f"could not start codex exec: {exc}") from exc

        events: list[dict] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            if emit_event is not None and not _legacy_landlock_notice(event):
                emit_event(event)
            if event.get("type") == "turn.completed":
                event_usage = event.get("usage") or {}
                usage["input_tokens"] += int(event_usage.get("input_tokens") or 0)
                usage["output_tokens"] += int(event_usage.get("output_tokens") or 0)

        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            suffix = f": {detail[-1000:]}" if detail else ""
            raise CodexBackendError(
                f"codex exec exited with status {completed.returncode}{suffix}"
            )

        try:
            text = last_message.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CodexBackendError("codex exec did not write its last message") from exc
        if not text:
            for event in reversed(events):
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and str(item.get("text") or "").strip():
                    text = str(item["text"]).strip()
                    break
        if not text:
            raise CodexBackendError("codex exec returned an empty response")
        return CodexResult(text=text, usage=usage)
