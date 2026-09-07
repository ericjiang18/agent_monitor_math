#!/usr/bin/env python3
"""One-way credential-home bridge for OpenClaw's native Claude CLI backend.

OpenClaw intentionally clears ``CLAUDE_CONFIG_DIR`` before spawning its
bundled ``claude-cli`` backend.  ProvingConsole keeps every linked Claude
account in a separate private home, so the backend command points at this
small wrapper and passes that home through names OpenClaw does not clear.

The wrapper never reads or copies credentials.  It validates the isolated
home and the already-resolved official executable, removes every API/cloud
billing selector, and restores only ``HOME`` and ``CLAUDE_CONFIG_DIR``.

OpenClaw keeps this process alive as a stdio JSONL backend, so the wrapper also
relays the official process while observing terminal result events. A final,
zero-token OAuth 401 marks the shared account as requiring reauthentication;
only a successful terminal turn with model telemetry and positive token usage
clears that marker. This gives the direct bridge the same persistent
fail-closed account state as the shared Claude backend without an API fallback.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Mapping, TextIO

if not __package__:
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

from agent_monitor import claude_login
from agent_monitor.runners.claude_backend import (
    _assistant_is_auth_diagnostic,
    _looks_like_oauth_failure,
    _terminal_auth_failure_detail,
)


CONFIG_HOME_ENV = "PROVINGCONSOLE_CLAUDE_CONFIG_HOME"
PROCESS_HOME_ENV = "PROVINGCONSOLE_CLAUDE_PROCESS_HOME"
BINARY_ENV = "PROVINGCONSOLE_CLAUDE_BINARY"

# Account mode must never become a separately billed API/cloud request.  Keep
# this defense independent from both jobs.py and OpenClaw's own clearEnv list.
PROVIDER_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY_OLD",
        "ANTHROPIC_API_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_UNIX_SOCKET",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "CLOUD_ML_REGION",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        "OPENAI_API_KEY",
        "OPENAI_API_KEYS",
        "CODEX_API_KEY",
        "CODEX_HOME",
        "KIMI_API_KEY",
        "KIMI_API_BASE",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENCLAW_GATEWAY_TOKEN",
    }
)


class BridgeConfigurationError(RuntimeError):
    """Raised before exec when the isolated account bridge is invalid."""


def _token_total(value: Any) -> int:
    """Return a conservative token count from nested Claude usage objects."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            normalized = str(key).replace("_", "").casefold()
            if (
                "token" in normalized
                and isinstance(item, (int, float))
                and not isinstance(item, bool)
            ):
                total += max(0, int(item))
            elif isinstance(item, (dict, list, tuple)):
                total += _token_total(item)
        return total
    if isinstance(value, (list, tuple)):
        return sum(_token_total(item) for item in value)
    return 0


def _has_model_telemetry(event: Mapping[str, Any]) -> bool:
    message = event.get("message")
    if isinstance(message, dict) and str(message.get("model") or "").strip():
        return True
    model_usage = event.get("modelUsage") or event.get("model_usage")
    if isinstance(model_usage, dict) and any(str(key).strip() for key in model_usage):
        return True
    return bool(str(event.get("model") or "").strip())


class _TurnAttestor:
    """Observe one or more Claude JSONL turns without retaining their content."""

    def __init__(self, config_home: Path):
        self.config_home = config_home
        self._lock = threading.Lock()
        self._oauth_failure = False
        self._assistant_activity = False
        self._model_telemetry = False
        self._tokens = 0
        self._terminal_seen = False
        self._tail = ""

    def _observe_text(self, value: object) -> None:
        rendered = str(value or "").casefold()
        if not rendered:
            return
        self._tail = (self._tail + rendered)[-2048:]
        if _looks_like_oauth_failure(self._tail):
            self._oauth_failure = True

    def _reset_turn(self) -> None:
        self._oauth_failure = False
        self._assistant_activity = False
        self._model_telemetry = False
        self._tokens = 0
        self._terminal_seen = False
        self._tail = ""

    def _mark(self) -> None:
        try:
            claude_login.mark_reauth_required(
                self.config_home, reason="oauth_refresh_failed"
            )
        except OSError:
            pass

    def _clear(self) -> None:
        try:
            claude_login.clear_reauth_required(self.config_home)
        except OSError:
            pass

    def observe_stdout(self, line: str) -> None:
        with self._lock:
            self._observe_text(line)
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                return
            if not isinstance(event, dict):
                return
            event_type = str(event.get("type") or "").strip().casefold()
            if event_type == "assistant" and not _assistant_is_auth_diagnostic(event):
                self._assistant_activity = True
            if event_type != "system" and _has_model_telemetry(event):
                self._model_telemetry = True
            usage = event.get("usage")
            if usage is not None:
                self._tokens += _token_total(usage)
            message = event.get("message")
            if isinstance(message, dict) and message.get("usage") is not None:
                self._tokens += _token_total(message.get("usage"))
            if event_type != "result":
                return

            self._terminal_seen = True
            self._observe_text(event.get("result"))
            subtype = str(event.get("subtype") or "").strip().casefold()
            terminal_auth_failure = _terminal_auth_failure_detail(event)
            if terminal_auth_failure:
                self._observe_text(terminal_auth_failure)
            is_error = (
                bool(event.get("is_error"))
                or bool(terminal_auth_failure)
                or subtype in {"error", "failed"}
            )
            if (
                is_error
                and self._oauth_failure
                and not self._assistant_activity
                and self._tokens == 0
            ):
                self._mark()
            elif (
                not is_error
                and subtype in {"", "success"}
                and self._model_telemetry
                and self._tokens > 0
            ):
                self._clear()
            self._reset_turn()

    def observe_stderr(self, line: str) -> None:
        with self._lock:
            self._observe_text(line)

    def finalize(self, returncode: int) -> None:
        """Handle a plain-text final OAuth failure with no result JSON event."""
        with self._lock:
            if (
                returncode != 0
                and not self._terminal_seen
                and self._oauth_failure
                and not self._assistant_activity
                and self._tokens == 0
            ):
                self._mark()


def _relay(
    source: TextIO,
    destination: TextIO,
    attestor: _TurnAttestor,
    *,
    stdout: bool,
) -> None:
    try:
        for line in source:
            if stdout:
                attestor.observe_stdout(line)
            else:
                attestor.observe_stderr(line)
            destination.write(line)
            destination.flush()
    finally:
        source.close()


def _run_official(
    binary: Path,
    argv: list[str],
    env: dict[str, str],
    *,
    config_home: Path,
) -> int:
    """Run and transparently relay the long-lived official Claude backend."""
    try:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        print(
            f"OpenClaw Claude bridge could not start Claude Code: {exc}",
            file=sys.stderr,
        )
        return 127
    assert process.stdout is not None
    assert process.stderr is not None
    attestor = _TurnAttestor(config_home)
    stdout_thread = threading.Thread(
        target=_relay,
        args=(process.stdout, sys.stdout, attestor),
        kwargs={"stdout": True},
        daemon=True,
        name="openclaw-claude-stdout",
    )
    stderr_thread = threading.Thread(
        target=_relay,
        args=(process.stderr, sys.stderr, attestor),
        kwargs={"stdout": False},
        daemon=True,
        name="openclaw-claude-stderr",
    )
    stdout_thread.start()
    stderr_thread.start()
    returncode = int(process.wait())
    stdout_thread.join()
    stderr_thread.join()
    attestor.finalize(returncode)
    return returncode


def _private_directory(raw: str, *, label: str) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise BridgeConfigurationError(f"{label} is required")
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise BridgeConfigurationError(f"{label} must be an existing private directory")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise BridgeConfigurationError(f"{label} cannot be inspected") from exc
    if mode & 0o077:
        raise BridgeConfigurationError(f"{label} must not be accessible by group or others")
    return path.resolve()


def _official_binary(raw: str) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise BridgeConfigurationError(f"{BINARY_ENV} is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise BridgeConfigurationError("Claude executable must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise BridgeConfigurationError("Claude executable cannot be resolved") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise BridgeConfigurationError("Claude executable is not an executable regular file")
    if resolved == Path(__file__).resolve():
        raise BridgeConfigurationError("Claude bridge cannot execute itself")
    return resolved


def build_exec_environment(source: Mapping[str, str]) -> tuple[Path, dict[str, str]]:
    """Return the validated official executable and OAuth-only environment."""
    config_home = _private_directory(source.get(CONFIG_HOME_ENV, ""), label=CONFIG_HOME_ENV)
    process_home = _private_directory(source.get(PROCESS_HOME_ENV, ""), label=PROCESS_HOME_ENV)
    if config_home != process_home:
        raise BridgeConfigurationError("Claude config and process homes must be the same account directory")
    binary = _official_binary(source.get(BINARY_ENV, ""))

    env = dict(source)
    for name in PROVIDER_ENV_NAMES:
        env.pop(name, None)
    for name in (CONFIG_HOME_ENV, PROCESS_HOME_ENV, BINARY_ENV):
        env.pop(name, None)
    env["CLAUDE_CONFIG_DIR"] = str(config_home)
    env["HOME"] = str(process_home)
    env["NO_COLOR"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    return binary, env


def main() -> int:
    try:
        binary, env = build_exec_environment(os.environ)
    except BridgeConfigurationError as exc:
        print(f"OpenClaw Claude bridge refused to start: {exc}", file=sys.stderr)
        return 78
    config_home = Path(env["CLAUDE_CONFIG_DIR"])
    if not claude_login.account_login_ready(config_home):
        print(
            "OpenClaw Claude bridge refused to start: Claude Code requires "
            "reauthentication; reconnect it in Settings",
            file=sys.stderr,
        )
        return 77
    argv = [str(binary), *sys.argv[1:]]
    return _run_official(binary, argv, env, config_home=config_home)


if __name__ == "__main__":
    raise SystemExit(main())
