#!/usr/bin/env python3
"""Launch OpenClaw with a per-user Codex subscription when available."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _session_offsets(state_dir: Path) -> dict[Path, int]:
    """Snapshot existing transcripts so a continuation cannot reuse an old stop."""
    offsets: dict[Path, int] = {}
    for path in state_dir.glob("agents/*/sessions/*.jsonl"):
        try:
            offsets[path] = path.stat().st_size
        except OSError:
            continue
    return offsets


def _terminal_event_since(
    state_dir: Path, offsets: dict[Path, int]
) -> tuple[Path, str] | None:
    """Return a newly appended, tool-free terminal assistant response."""
    for path in sorted(state_dir.glob("agents/*/sessions/*.jsonl")):
        try:
            size = path.stat().st_size
            start = offsets.get(path, 0)
            if start > size:
                start = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(start)
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("message") or {}
            if (
                event.get("type") != "message"
                or message.get("role") != "assistant"
                or message.get("stopReason") != "stop"
            ):
                continue
            text = "\n".join(
                str(block.get("text") or "").strip()
                for block in message.get("content") or []
                if isinstance(block, dict)
                and block.get("type") == "text"
                and str(block.get("text") or "").strip()
            ).strip()
            if text:
                return path, text
    return None


def _terminate_process_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (OSError, ProcessLookupError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _run_with_terminal_watchdog(
    argv: list[str], state_dir: Path, *, grace_s: float = 15.0
) -> int:
    """Finish a subscription run if OpenClaw leaves Node alive after `stop`."""
    offsets = _session_offsets(state_dir)
    proc = subprocess.Popen(argv, start_new_session=True)
    prior_handlers: dict[int, object] = {}

    def forward(signum: int, _frame: object) -> None:
        _terminate_process_group(proc, signum)
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        prior_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, forward)

    terminal: tuple[Path, str] | None = None
    terminal_at: float | None = None
    try:
        while proc.poll() is None:
            found = _terminal_event_since(state_dir, offsets)
            if found and terminal_at is None:
                terminal, terminal_at = found, time.monotonic()
            if terminal_at is not None and time.monotonic() - terminal_at >= grace_s:
                _terminate_process_group(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _terminate_process_group(proc, signal.SIGKILL)
                    proc.wait(timeout=5)
                assert terminal is not None
                print(
                    json.dumps(
                        {
                            "sessionFile": str(terminal[0]),
                            "payloads": [{"text": terminal[1]}],
                            "agentMonitorTerminalWatchdog": True,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return 0
            time.sleep(0.5)
        return int(proc.returncode or 0)
    finally:
        for signum, handler in prior_handlers.items():
            signal.signal(signum, handler)


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    workspace_name = sys.argv[2] if len(sys.argv) > 2 else Path.cwd().name
    binary = shutil.which("openclaw")
    if not binary:
        print("openclaw is not installed", file=sys.stderr)
        return 127

    subscription = os.environ.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"
    state_dir: Path | None = None
    if subscription:
        # Keep OpenClaw conversations/config isolated per web user while its
        # Codex app-server re-reads the existing CODEX_HOME login.
        codex_home = Path(os.environ["CODEX_HOME"])
        workspace = Path.cwd().resolve()
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_API_KEYS", None)
        os.environ.pop("CODEX_API_KEY", None)
        safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in workspace_name)[:80] or "run"
        workspace_hash = hashlib.sha256(str(workspace).encode()).hexdigest()[:12]
        default_state = codex_home / "openclaw" / "runs" / f"{safe_name}-{workspace_hash}"
        state_dir = Path(os.environ.get("OPENCLAW_STATE_DIR") or default_state)
        os.environ["OPENCLAW_STATE_DIR"] = str(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        model_id = os.environ.get("AGENT_MONITOR_CODEX_MODEL", "gpt-5.6-sol")
        if model_id.startswith("openai/"):
            model_id = model_id.split("/", 1)[1]
        model = f"openai/{model_id}"
        # Explicitly mark this model as the ChatGPT/Codex Responses transport.
        # Without this entry OpenClaw treats openai/* as Platform API models
        # and correctly refuses OAuth credentials.
        config_path = state_dir / "openclaw.json"
        if not config_path.exists():
            config_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "mode": "merge",
                            "providers": {
                                "openai": {
                                    "baseUrl": "https://chatgpt.com/backend-api/codex",
                                    "api": "openai-chatgpt-responses",
                                    "models": [
                                        {
                                            "id": model_id,
                                            "name": model_id,
                                            "reasoning": True,
                                            "input": ["text"],
                                            "contextWindow": 272000,
                                            "maxTokens": 128000,
                                        }
                                    ],
                                }
                            },
                        },
                        "agents": {
                            "defaults": {
                                "model": {"primary": model},
                                "workspace": str(workspace),
                                "skipBootstrap": True,
                            }
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    else:
        model = os.environ.get("AGENT_MONITOR_OPENCLAW_MODEL", "openai/gpt-5.5")

    argv = [
        binary,
        "agent",
        "--local",
        "--json",
        "--agent",
        "main",
        "--session-key",
        f"agent:main:{workspace_name}",
        "--model",
        model,
        "--timeout",
        "3000",
        "-m",
        prompt,
    ]
    if subscription and state_dir is not None:
        grace_s = max(
            2.0,
            min(
                float(os.environ.get("AGENT_MONITOR_OPENCLAW_EXIT_GRACE_S", "15")),
                120.0,
            ),
        )
        return _run_with_terminal_watchdog(argv, state_dir, grace_s=grace_s)
    os.execv(binary, argv)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
