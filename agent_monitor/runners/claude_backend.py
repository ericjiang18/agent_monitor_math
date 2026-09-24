"""Isolated native Claude Code backend for subscription-authenticated runs.

The backend deliberately supports Claude account OAuth only.  API credentials
and cloud-provider selectors are removed from the child environment so an
account run cannot silently fall back to separately billed API traffic.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .outcome_contract import normalize_requested_outcome_file


MODEL_ALIASES = ("sonnet", "opus", "fable", "claude-haiku-4-5")
EFFORT_LEVELS = ("default", "low", "medium", "high", "xhigh", "max")
MIN_PROOF_BYTES = 400
MIN_LEAN_BYTES = 80

# Response-only callers may persist a literal no-tools provenance claim only
# when they require this contract: private temporary cwd when no workspace is
# supplied, safe/strict/no-persistence mode, and an explicit empty --tools set.
RESPONSE_ONLY_TOOL_ISOLATION_VERSION = 1


class ClaudeBackendError(RuntimeError):
    """Raised when native Claude Code does not complete the requested turn."""


class ClaudeAuthenticationError(ClaudeBackendError):
    """Raised when the linked Claude Code subscription cannot authenticate."""


@dataclass(frozen=True)
class ClaudeResult:
    """One terminal Claude Code result and its structured telemetry."""

    text: str
    usage: dict[str, int]
    model: str | None
    cost_usd: float | None
    events: tuple[dict, ...]
    terminal_event: dict


@dataclass(frozen=True)
class ClaudeProofResult(ClaudeResult):
    """A successful agentic run with a fresh root proof artifact."""

    artifact: Path


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    output: str
    events: tuple[dict, ...]


def forward_as_codex_event(
    event: dict,
    emit: Callable[[dict], None],
) -> None:
    """Project response-only Claude JSONL into the monitor's Codex protocol.

    Plain and Meta-Harness are historically parsed as Codex-style streams.
    Their Claude account route disables tools, so only assistant text/thinking
    and the terminal usage record need translation. The native ``claude``
    engine continues to forward the original Claude stream unchanged.
    """
    event_type = str(event.get("type") or "")
    if event_type == "system" and event.get("subtype") == "init":
        emit(
            {
                "type": "thread.started",
                "thread_id": str(event.get("session_id") or "claude-code"),
            }
        )
        return
    if event_type == "assistant":
        # Some Claude Code releases surface a transport-level OAuth diagnostic
        # as an assistant event immediately before the terminal error. It is
        # not model output and must not appear as a successful assistant
        # response in Codex-shaped harnesses.
        if _assistant_is_auth_diagnostic(event):
            return
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type in {"thinking", "redacted_thinking"}:
                body = str(block.get("thinking") or "").strip()
                if body:
                    emit(
                        {
                            "type": "item.completed",
                            "item": {"type": "reasoning", "text": body},
                        }
                    )
            elif block_type == "text":
                body = str(block.get("text") or "").strip()
                if body:
                    emit(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": body},
                        }
                    )
        return
    if event_type != "result":
        return
    # A failed result must never become turn.completed, even when a Claude Code
    # release misleadingly labels its terminal subtype success. The typed
    # exception from _terminal_result reports the error once.
    if event.get("is_error") or _terminal_auth_failure_detail(event):
        return
    usage = event.get("usage") or {}
    emit(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "cached_input_tokens": int(
                    usage.get("cache_read_input_tokens") or 0
                ),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "reasoning_output_tokens": 0,
            },
        }
    )


def _looks_like_oauth_failure(value: object) -> bool:
    """Recognize Claude Code's own OAuth transport diagnostic, not model prose."""
    rendered = str(value or "").strip().lower()
    if not rendered:
        return False
    return (
        "oauth access token has expired" in rendered
        or "oauth access token expired" in rendered
        or (
            "failed to authenticate" in rendered
            and ("401" in rendered or "re-authenticate" in rendered)
        )
    )


def _usage_has_tokens(raw_usage: object) -> bool:
    if not isinstance(raw_usage, Mapping):
        return False
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        try:
            if int(raw_usage.get(name) or 0) > 0:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _assistant_is_auth_diagnostic(event: Mapping[str, object]) -> bool:
    """Return true only for a zero-token, text-only OAuth diagnostic event."""
    if str(event.get("type") or "") != "assistant":
        return False
    message = event.get("message") or {}
    if not isinstance(message, Mapping) or _usage_has_tokens(message.get("usage")):
        return False
    content = message.get("content") or []
    if not isinstance(content, (list, tuple)):
        return False
    texts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            return False
        block_type = str(block.get("type") or "")
        if block_type != "text":
            return False
        text = str(block.get("text") or "").strip()
        if text:
            texts.append(text)
    return bool(texts) and all(_looks_like_oauth_failure(text) for text in texts)


def _terminal_auth_failure_detail(event: Mapping[str, object]) -> str | None:
    """Return a terminal OAuth diagnostic even if the CLI subtype says success."""
    detail = str(event.get("result") or "").strip()
    if not _looks_like_oauth_failure(detail):
        return None
    # A real model response could discuss this diagnostic. Treat the text as a
    # transport failure when Claude flags it as an error, or when it reports no
    # inference usage/model telemetry at all.
    if event.get("is_error"):
        return detail
    if (
        _usage_has_tokens(event.get("usage"))
        or event.get("modelUsage")
        or event.get("model_usage")
    ):
        return None
    return detail


_PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "CLAUDE_CONFIG_DIR",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "NO_COLOR",
    "TMPDIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

# Kept explicit as defense in depth even though the allowlist above excludes
# these values.  Tests exercise this boundary because an OAuth run must never
# inherit an API or cloud billing route.
_PROVIDER_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
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
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
}


def subscription_enabled(source_env: Mapping[str, str] | None = None) -> bool:
    """Return whether the selected environment explicitly routes Claude OAuth."""
    source = source_env if source_env is not None else os.environ
    return source.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1"


def _positive_number(value: str, *, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ClaudeBackendError(f"{name} must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ClaudeBackendError(f"{name} must be greater than zero")
    return parsed


def _max_turns(
    value: int | str | None,
    source_env: Mapping[str, str] | None = None,
) -> int:
    source = source_env if source_env is not None else os.environ
    raw = (
        str(value)
        if value is not None
        else source.get("AGENT_MONITOR_CLAUDE_MAX_TURNS", "60")
    )
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ClaudeBackendError("AGENT_MONITOR_CLAUDE_MAX_TURNS must be an integer") from exc
    if parsed < 1 or parsed > 200:
        raise ClaudeBackendError("Claude max turns must be between 1 and 200")
    return parsed


def _effort(
    value: str | None,
    source_env: Mapping[str, str] | None = None,
) -> str:
    source = source_env if source_env is not None else os.environ
    selected = (value or source.get("AGENT_MONITOR_CLAUDE_EFFORT") or "default").strip().lower()
    if selected not in EFFORT_LEVELS:
        raise ClaudeBackendError(
            "Claude effort must be one of: " + ", ".join(EFFORT_LEVELS)
        )
    return selected


def _model(
    value: str | None,
    source_env: Mapping[str, str] | None = None,
) -> str:
    source = source_env if source_env is not None else os.environ
    selected = (value or source.get("AGENT_MONITOR_CLAUDE_MODEL") or "sonnet").strip().lower()
    if selected not in MODEL_ALIASES:
        raise ClaudeBackendError(
            "Claude account model must be one of: " + ", ".join(MODEL_ALIASES)
        )
    return selected


def _is_haiku_4_5(model: str) -> bool:
    return model == "claude-haiku-4-5" or model.startswith("claude-haiku-4-5-")


def _actual_model_telemetry(
    events: tuple[dict, ...], terminal_event: Mapping[str, object]
) -> tuple[str, ...]:
    """Return every model identity reported by actual inference events.

    The ``system/init`` model is only the requested configuration and therefore
    is intentionally excluded. Claude Code currently reports the resolved model
    on assistant messages and/or in the terminal ``modelUsage`` map. Exact-model
    runs must inspect all values so a secondary or silently substituted model
    cannot be hidden by an earlier init event.
    """
    observed: list[str] = []

    def add(value: object) -> None:
        rendered = str(value or "").strip().lower()
        if rendered and rendered not in observed:
            observed.append(rendered)

    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        if isinstance(message, dict):
            add(message.get("model"))

    add(terminal_event.get("model"))
    model_usage = terminal_event.get("modelUsage")
    if not isinstance(model_usage, dict):
        model_usage = terminal_event.get("model_usage")
    if isinstance(model_usage, dict):
        for usage_model, details in model_usage.items():
            add(usage_model)
            if not isinstance(details, dict):
                continue
            for field in ("canonicalModel", "canonical_model", "model", "modelName"):
                add(details.get(field))
    return tuple(observed)


def _require_requested_model(result: ClaudeResult, requested: str) -> None:
    """Fail closed when a full model id resolves to a different model.

    Claude aliases intentionally float, so their resolved model cannot be
    compared byte-for-byte. A full model family id is explicit, however, and
    must never be silently replaced by another family. The CLI reports pinned
    releases with a date suffix, which is still the requested model family.
    """
    if not requested.startswith("claude-"):
        return
    actual_models = _actual_model_telemetry(result.events, result.terminal_event)
    mismatches = [
        actual
        for actual in actual_models
        if actual != requested and not actual.startswith(requested + "-")
    ]
    if actual_models and not mismatches:
        return
    rendered = ", ".join(mismatches or actual_models) or "missing actual model telemetry"
    raise ClaudeBackendError(
        f"Claude Code model mismatch: requested {requested}, received {rendered}"
    )


def _claude_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the minimum OAuth-only environment inherited by Claude Code."""
    inherited = source if source is not None else os.environ
    env = {name: inherited[name] for name in _PASSTHROUGH_ENV if inherited.get(name)}
    for name in _PROVIDER_ENV_NAMES:
        env.pop(name, None)
    config_raw = str(inherited.get("CLAUDE_CONFIG_DIR") or "").strip()
    if not config_raw:
        raise ClaudeBackendError(
            "CLAUDE_CONFIG_DIR is required for an isolated Claude account run"
        )
    config = Path(config_raw).expanduser()
    if config.is_symlink() or not config.is_dir():
        raise ClaudeBackendError("CLAUDE_CONFIG_DIR is not an isolated account directory")
    env["CLAUDE_CONFIG_DIR"] = str(config.resolve())
    env["PATH"] = _claude_runtime_path(inherited)
    env.setdefault("HOME", str(Path.home()))
    env["NO_COLOR"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    return env


def _claude_runtime_path(source: Mapping[str, str] | None = None) -> str:
    """Explicit route PATH plus the registry's approved user install roots."""
    inherited = source if source is not None else os.environ
    from agent_monitor.engines_registry import tool_search_path

    parts: list[str] = []
    for raw_path in (inherited.get("PATH"), tool_search_path(), os.defpath):
        for part in str(raw_path or "").split(os.pathsep):
            if part and part not in parts:
                parts.append(part)
    return os.pathsep.join(parts)


def _claude_binary(source: Mapping[str, str] | None = None) -> str:
    binary = shutil.which("claude", path=_claude_runtime_path(source))
    if not binary:
        raise ClaudeBackendError("claude executable was not found")
    return binary


def _build_command(
    *,
    prompt: str,
    model: str,
    max_turns: int,
    effort: str,
    system: str | None = None,
    enable_tools: bool = True,
    binary: str | None = None,
) -> list[str]:
    """Build one non-interactive, non-resumable official Claude Code turn."""
    command = [
        binary or _claude_binary(),
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--safe-mode",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--max-turns",
        str(max_turns),
        "--model",
        model,
    ]
    # Haiku 4.5 does not support the effort parameter.
    if not _is_haiku_4_5(model) and effort != "default":
        command.extend(["--effort", effort])
    if system:
        command.extend(["--system-prompt", system])
    if not enable_tools:
        # Claude Code documents an empty --tools value as disabling all tools.
        command.extend(["--tools", ""])
    return command


def _stop_process_group(process: subprocess.Popen[str], *, force: bool = False) -> None:
    if process.poll() is not None:
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    if os.name == "posix":
        try:
            os.killpg(process.pid, sig)
            return
        except OSError:
            pass
    try:
        process.kill() if force else process.terminate()
    except OSError:
        pass


def _run_stream(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    emit_line: Callable[[str], None] | None = None,
    emit_event: Callable[[dict], None] | None = None,
) -> _ProcessResult:
    """Stream one Claude process while retaining complete timeout control."""
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise ClaudeBackendError(f"could not start Claude Code: {exc}") from exc

    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                output_queue.put(line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(target=read_output, name="claude-code-stdout", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    output: list[str] = []
    events: list[dict] = []
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _stop_process_group(process)
                break
            try:
                line = output_queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            if line is None:
                break
            output.append(line)
            if emit_line is not None:
                emit_line(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            if emit_event is not None:
                emit_event(event)
        try:
            process.wait(timeout=3 if timed_out else 30)
        except subprocess.TimeoutExpired:
            _stop_process_group(process, force=True)
            process.wait(timeout=10)
    finally:
        reader.join(timeout=1)
        if process.stdout is not None:
            process.stdout.close()

    if timed_out:
        raise ClaudeBackendError(f"Claude Code timed out after {timeout:g}s")
    return _ProcessResult(process.returncode, "".join(output), tuple(events))


def _assistant_text(events: tuple[dict, ...]) -> str:
    chunks: list[str] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    chunks.append(text)
    return "\n\n".join(chunks).strip()


def _terminal_result(process: _ProcessResult) -> ClaudeResult:
    terminal = next(
        (event for event in reversed(process.events) if event.get("type") == "result"),
        None,
    )
    if terminal is None:
        detail = process.output.strip()[-1000:]
        suffix = f": {detail}" if detail else ""
        raise ClaudeBackendError(f"Claude Code ended without a terminal result event{suffix}")
    result_text = str(terminal.get("result") or "").strip()
    subtype = str(terminal.get("subtype") or "").strip()
    auth_detail = _terminal_auth_failure_detail(terminal)
    if auth_detail is None:
        for event in reversed(process.events):
            if not _assistant_is_auth_diagnostic(event):
                continue
            message = event.get("message") or {}
            auth_detail = " ".join(
                str(block.get("text") or "").strip()
                for block in message.get("content") or []
                if isinstance(block, dict)
            ).strip()
            break
    if auth_detail:
        if _looks_like_oauth_failure(auth_detail):
            raise ClaudeAuthenticationError(
                "Claude Code subscription authentication failed: OAuth access "
                "token has expired (HTTP 401); reconnect Claude Code and retry."
            )
        raise ClaudeAuthenticationError(
            f"Claude Code subscription authentication failed: {auth_detail}"
        )
    if terminal.get("is_error"):
        detail = result_text or subtype or "unknown Claude Code error"
        safe_subtype = subtype if subtype.lower() not in {"success", "completed"} else ""
        suffix = f" ({safe_subtype})" if safe_subtype else ""
        raise ClaudeBackendError(f"Claude Code failed{suffix}: {detail}")
    if process.returncode:
        detail = process.output.strip()[-1000:]
        suffix = f": {detail}" if detail else ""
        raise ClaudeBackendError(
            f"Claude Code exited with status {process.returncode}{suffix}"
        )
    text = result_text or _assistant_text(process.events)
    if not text:
        raise ClaudeBackendError("Claude Code returned an empty response")
    raw_usage = terminal.get("usage") or {}
    usage = {
        "input_tokens": int(raw_usage.get("input_tokens") or 0),
        "output_tokens": int(raw_usage.get("output_tokens") or 0),
        "cache_read_tokens": int(raw_usage.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(raw_usage.get("cache_creation_input_tokens") or 0),
    }
    actual_models = _actual_model_telemetry(process.events, terminal)
    model = actual_models[0] if actual_models else None
    if model is None:
        for event in process.events:
            if event.get("type") == "system" and event.get("subtype") == "init":
                model = str(event.get("model") or "").strip() or None
                break
    cost = terminal.get("total_cost_usd")
    return ClaudeResult(
        text=text,
        usage=usage,
        model=model,
        cost_usd=float(cost) if cost is not None else None,
        events=process.events,
        terminal_event=terminal,
    )


def _is_oauth_expiry(error: ClaudeBackendError) -> bool:
    """Return whether an exception is the bounded OAuth-expiry failure."""
    return _looks_like_oauth_failure(str(error))


def _is_preinference_oauth_expiry(
    error: ClaudeBackendError,
    process: _ProcessResult,
) -> bool:
    """Recognize the one transient OAuth failure that is safe to replay.

    A linked-account access token can expire between the status probe and a
    non-interactive turn.  The official CLI refreshes its credential for the
    next process, but the first process can still return a 401.  Replay is only
    allowed when the failed stream contains no assistant/tool activity; once a
    model has acted, callers must see the original failure.
    """
    if not _is_oauth_expiry(error):
        return False
    for event in process.events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").lower()
        if event_type == "user":
            return False
        if event_type == "assistant" and not _assistant_is_auth_diagnostic(event):
            return False
    return True


def _clear_reauth_required(env: Mapping[str, str]) -> None:
    config_dir = str(env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if not config_dir:
        return
    from agent_monitor import claude_login
    try:
        claude_login.clear_reauth_required(config_dir)
    except OSError:
        return


def _mark_reauth_required(env: Mapping[str, str]) -> None:
    config_dir = str(env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if not config_dir:
        return
    from agent_monitor import claude_login
    try:
        claude_login.mark_reauth_required(
            config_dir,
            reason="oauth_refresh_failed",
        )
    except OSError:
        return


def _run_terminal_with_oauth_retry(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    emit_line: Callable[[str], None] | None = None,
    emit_event: Callable[[dict], None] | None = None,
    replay_is_safe: Callable[[], bool],
) -> ClaudeResult:
    """Run one turn, replaying exactly once after a pre-inference OAuth 401."""
    for attempt in range(2):
        process = _run_stream(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            emit_line=emit_line,
            emit_event=emit_event,
        )
        try:
            result = _terminal_result(process)
        except ClaudeBackendError as exc:
            authentication_failed = isinstance(exc, ClaudeAuthenticationError)
            replayable = (
                _is_preinference_oauth_expiry(exc, process)
                and replay_is_safe()
            )
            if attempt == 0 and replayable:
                # A new official CLI process reads the refresh credential
                # written by the failed process. Replay exactly once.
                time.sleep(1.0)
                continue
            # Replay safety and account readiness are separate decisions.  A
            # final typed authentication failure must fail the shared account
            # gate even when model/tool activity makes replay unsafe.
            if authentication_failed:
                _mark_reauth_required(env)
            raise
        _clear_reauth_required(env)
        return result
    raise ClaudeBackendError("Claude Code OAuth retry ended unexpectedly")


def _validate_subscription(source_env: Mapping[str, str] | None = None) -> None:
    if not subscription_enabled(source_env):
        raise ClaudeBackendError(
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION=1 is required for Claude Code"
        )


def _workspace(path: str | Path) -> Path:
    workspace = Path(path).expanduser()
    if workspace.is_symlink() or not workspace.is_dir():
        raise ClaudeBackendError("Claude workspace must be a regular directory")
    return workspace.resolve()


def _artifact_signature(path: Path, *, minimum: int = 0) -> tuple[int, str, int] | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size < minimum:
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    if len(payload) < minimum:
        return None
    return len(payload), hashlib.sha256(payload).hexdigest(), info.st_mtime_ns


def _artifact_signatures(
    workspace: Path,
    names: tuple[str, ...],
) -> dict[str, tuple[int, str, int] | None]:
    return {name: _artifact_signature(workspace / name) for name in names}


def _proof_signatures(workspace: Path) -> dict[str, tuple[int, str, int] | None]:
    return _artifact_signatures(workspace, ("proof.md", "proof.tex"))


def _fresh_artifact(
    workspace: Path,
    before: dict[str, tuple[int, str, int] | None],
    *,
    names: tuple[str, ...],
    minimum: int,
    label: str,
) -> Path:
    changed: list[tuple[Path, tuple[int, str, int]]] = []
    for name in names:
        path = workspace / name
        signature = _artifact_signature(path, minimum=minimum)
        previous = before.get(name)
        if signature is not None and (previous is None or signature[:2] != previous[:2]):
            changed.append((path, signature))
    if not changed:
        raise ClaudeBackendError(
            f"Claude Code completed without a fresh substantive regular root {label} artifact"
        )
    return max(changed, key=lambda item: item[1][2])[0]


def _fresh_proof(
    workspace: Path,
    before: dict[str, tuple[int, str, int] | None],
) -> Path:
    return _fresh_artifact(
        workspace,
        before,
        names=("proof.md", "proof.tex"),
        minimum=MIN_PROOF_BYTES,
        label="proof",
    )

def claude_exec(
    system: str,
    user: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
    emit_event: Callable[[dict], None] | None = None,
    workspace: str | Path | None = None,
    enable_tools: bool = False,
    max_turns: int | None = None,
    effort: str | None = None,
    source_env: Mapping[str, str] | None = None,
) -> ClaudeResult:
    """Return one response-only Claude Code turn for Plain/Meta/Lean adapters."""
    source = source_env if source_env is not None else os.environ
    _validate_subscription(source)
    env = _claude_environment(source)
    selected_model = _model(model, source)
    selected_effort = _effort(effort, source)
    turns = _max_turns(max_turns, source)
    timeout_s = (
        _positive_number(str(timeout), name="Claude timeout")
        if timeout is not None
        else _positive_number(
            source.get("AGENT_MONITOR_CLAUDE_TIMEOUT", "600"),
            name="AGENT_MONITOR_CLAUDE_TIMEOUT",
        )
    )
    prompt = f"USER REQUEST:\n{user}"

    if workspace is None:
        temp = tempfile.TemporaryDirectory(prefix="agent-monitor-claude-response-")
        cwd = Path(temp.name)
    else:
        temp = None
        cwd = _workspace(workspace)
    try:
        command = _build_command(
            prompt=prompt,
            system=system,
            model=selected_model,
            max_turns=turns,
            effort=selected_effort,
            enable_tools=enable_tools,
            binary=_claude_binary(source),
        )
        result = _run_terminal_with_oauth_retry(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout_s,
            emit_event=emit_event,
            replay_is_safe=lambda: not enable_tools,
        )
        _require_requested_model(result, selected_model)
        return result
    finally:
        if temp is not None:
            temp.cleanup()

_PROOF_SYSTEM = """You are the native Claude Code proof harness.
Work only inside the current task directory. Do not read or modify files outside
it. Maintain the complete mathematical deliverable as ./proof.md (or
./proof.tex) directly at the workspace root, never as a symlink. Check exact
quantifiers and boundary cases before claiming a result. A requested theorem
may be false or open. Never promote an outline or an unverified open-problem
attempt to a proof. End with the outcome contract requested by the task."""

_LEAN_SYSTEM = """You are the native Claude Code Lean formalization harness.
Work only inside the current Lean task directory. Preserve the requested theorem
statement and maintain the complete formal artifact as ./Proof.lean directly at
the workspace root, never as a symlink. Use the installed Lean toolchain to
compile and repair the file. Do not claim success with sorry, admit, axioms that
encode the result, or an uncompiled artifact."""


def claude_proof(
    prompt: str,
    workspace: str | Path,
    *,
    model: str | None = None,
    timeout: float | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    emit_line: Callable[[str], None] | None = None,
    emit_event: Callable[[dict], None] | None = None,
    source_env: Mapping[str, str] | None = None,
) -> ClaudeProofResult:
    """Run Claude agentically and require a fresh root proof or Proof.lean."""
    source = source_env if source_env is not None else os.environ
    _validate_subscription(source)
    cwd = _workspace(workspace)
    lean_mode = (
        source.get("AGENT_MONITOR_FORMAL_LEAN_MODE") == "1"
        or source.get("AGENT_MONITOR_CLAUDE_LEAN_MODE") == "1"
    )
    names = ("Proof.lean",) if lean_mode else ("proof.md", "proof.tex")
    minimum = MIN_LEAN_BYTES if lean_mode else MIN_PROOF_BYTES
    before = _artifact_signatures(cwd, names)
    env = _claude_environment(source)
    timeout_s = (
        _positive_number(str(timeout), name="Claude timeout")
        if timeout is not None
        else _positive_number(
            # Direct, tool-using proof runs can make substantive progress for
            # longer than the compact response-only adapters. Keep the inner
            # deadline below jobs.py's one-hour outer watchdog so cleanup and
            # terminal persistence retain five minutes of headroom.
            source.get("AGENT_MONITOR_CLAUDE_TIMEOUT", "3300"),
            name="AGENT_MONITOR_CLAUDE_TIMEOUT",
        )
    )
    selected_model = _model(model, source)
    artifact_name = "Proof.lean" if lean_mode else "proof.md"
    artifact_path = cwd / artifact_name
    workspace_contract = (
        "\nFor this run the exact absolute workspace root is "
        f"{json.dumps(str(cwd))}. You MUST create or modify the final artifact "
        f"at exactly {json.dumps(str(artifact_path))}. Do not substitute "
        "/tmp/proof.md, the account home, a parent path, or a nested path. "
        "Keep every scratch file inside the exact workspace root."
    )
    command = _build_command(
        prompt=prompt,
        system=(
            (_LEAN_SYSTEM if lean_mode else _PROOF_SYSTEM)
            + workspace_contract
        ),
        model=selected_model,
        max_turns=_max_turns(max_turns, source),
        effort=_effort(effort, source),
        enable_tools=True,
        binary=_claude_binary(source),
    )
    result = _run_terminal_with_oauth_retry(
        command,
        cwd=cwd,
        env=env,
        timeout=timeout_s,
        emit_line=emit_line,
        emit_event=emit_event,
        replay_is_safe=lambda: _artifact_signatures(cwd, names) == before,
    )
    _require_requested_model(result, selected_model)
    artifact = _fresh_artifact(
        cwd,
        before,
        names=names,
        minimum=minimum,
        label="Proof.lean" if lean_mode else "proof",
    )
    if not lean_mode:
        normalize_requested_outcome_file(artifact, prompt)
    # Finalization must not be able to turn a replaced/linked artifact into an
    # accepted result. Recheck the regular root-file invariant afterward.
    if _artifact_signature(artifact, minimum=minimum) is None:
        raise ClaudeBackendError("Claude proof artifact became invalid during finalization")
    return ClaudeProofResult(
        text=result.text,
        usage=result.usage,
        model=result.model,
        cost_usd=result.cost_usd,
        events=result.events,
        terminal_event=result.terminal_event,
        artifact=artifact,
    )
