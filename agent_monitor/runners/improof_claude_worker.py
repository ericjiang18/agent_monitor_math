"""Response-only Claude Code worker for IMProof's native CLI DAG.

The worker is deliberately small: ProofStack owns the isolated workspace and
the Author/Critic ordering, while the shared Claude backend owns OAuth routing,
model validation, timeouts, and the no-API-fallback boundary.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import Mapping

from agent_monitor.runners.claude_backend import (
    ClaudeBackendError,
    claude_exec,
    forward_as_codex_event,
)


DEFAULT_MODEL = "claude-haiku-4-5"
_ALLOWED_OUTPUTS = {"answer.tex", "critique.md", "research-queries.json"}
_SOURCE_ENV_ALLOWLIST = {
    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION",
    "AGENT_MONITOR_CLAUDE_MODEL",
    "AGENT_MONITOR_CLAUDE_TIMEOUT",
    "AGENT_MONITOR_CLAUDE_MAX_TURNS",
    "AGENT_MONITOR_CLAUDE_EFFORT",
    "CLAUDE_CONFIG_DIR",
    "PATH",
    "HOME",
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
}

_ROLE_SYSTEM = {
    "author": (
        "You are the Author in IMProof's fixed-round Author/Critic workflow. "
        "Produce only the requested rigorous LaTeX proof body. Do not use tools, "
        "read files, or claim an open/false statement is solved."
    ),
    "critic": (
        "You are the independent Critic in IMProof's fixed-round Author/Critic "
        "workflow. Return only the requested concise referee report. Check the "
        "exact statement, quantifiers, boundary cases, and every essential step. "
        "Do not use tools."
    ),
    "planner": (
        "You are IMProof's tool-disabled literature-query planner. Return only "
        "one JSON object matching the schema in the user prompt. Never use tools, "
        "invent source access, or add Markdown fences."
    ),
}


def _source_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Pass only OAuth routing and harmless process settings to Claude."""
    inherited = source if source is not None else os.environ
    return {
        name: str(inherited[name])
        for name in _SOURCE_ENV_ALLOWLIST
        if inherited.get(name)
    }


def _safe_output(relative: str) -> Path:
    if relative not in _ALLOWED_OUTPUTS:
        raise ValueError("unsupported IMProof Claude output filename")
    root = Path.cwd().resolve()
    raw = root / relative
    if raw.is_symlink():
        raise ValueError("refusing to replace a symlinked IMProof Claude output")
    if raw.exists() and not stat.S_ISREG(raw.lstat().st_mode):
        raise ValueError("refusing to replace a non-regular IMProof Claude output")
    target = raw.resolve()
    if target.parent != root:
        raise ValueError("IMProof Claude output escapes its isolated workspace")
    return target


def _write_output(path: Path, text: str) -> None:
    payload = text.strip()
    if not payload:
        raise ValueError("Claude returned an empty IMProof response")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(payload + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _emit_projected(event: dict) -> None:
    def emit(payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    forward_as_codex_event(event, emit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=sorted(_ROLE_SYSTEM), required=True)
    parser.add_argument("--output", choices=sorted(_ALLOWED_OUTPUTS), required=True)
    parser.add_argument("--model")
    args = parser.parse_args(argv)

    prompt = sys.stdin.read(200_001)
    if not prompt.strip():
        raise SystemExit("IMProof Claude prompt is empty")
    if len(prompt) > 200_000:
        raise SystemExit("IMProof Claude prompt exceeds 200000 characters")

    source_env = _source_environment()
    model = str(
        args.model
        or source_env.get("AGENT_MONITOR_CLAUDE_MODEL")
        or DEFAULT_MODEL
    ).strip()
    try:
        result = claude_exec(
            _ROLE_SYSTEM[args.role],
            prompt,
            model=model,
            enable_tools=False,
            source_env=source_env,
            emit_event=_emit_projected,
        )
        _write_output(_safe_output(args.output), result.text)
    except (ClaudeBackendError, OSError, ValueError) as exc:
        sys.stderr.write(f"IMProof Claude {args.role} failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
