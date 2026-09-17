#!/usr/bin/env python3
"""Run Codex CLI against Kimi's Responses endpoint through its sanitizer.

The normal Codex engine uses the user's linked Codex account.  A selected
Kimi model is different: it is an API-provider choice, so this wrapper keeps
the invocation isolated, streams native Codex JSONL events to Monitor, and
requires the same root proof artifact as every other harness.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository_root = str(Path(__file__).resolve().parents[2])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

from agent_monitor.runners.codex_backend import CodexBackendError, codex_exec
from agent_monitor.runners.outcome_contract import canonicalize_outcome_text


SYSTEM_PROMPT = """You are the Codex CLI harness inside ProvingConsole.
Work only in the current run workspace. Use the available workspace tools to
create or improve proof.md (or proof.tex when the task explicitly requires
LaTeX). Follow the complete artifact and citation contract in the user prompt.
Do not merely describe what you would write: write the artifact before ending.
End proof.md with exactly one standalone line
`ProvingConsole outcome: <Solved|Counterexample|Known/Open Status|Partial Progress>`.
"""

LEAN_SYSTEM_PROMPT = """You are the Codex CLI formal Lean harness inside
ProvingConsole. Work only in the current Lean task directory. Create or repair
Proof.lean, run the checker requested by the user prompt, and leave the complete
formal artifact on disk before ending. Never replace the requested theorem with
a weaker, specialized, vacuous, or differently quantified statement.
"""


def _emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _lean_mode() -> bool:
    return os.environ.get("AGENT_MONITOR_KIMI_CODEX_MODE", "").strip().lower() == "lean"


def _artifact() -> Path | None:
    names = ("Proof.lean",) if _lean_mode() else ("proof.md", "proof.tex")
    for name in names:
        candidate = Path.cwd() / name
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _artifact_names() -> tuple[str, ...]:
    return ("Proof.lean",) if _lean_mode() else ("proof.md", "proof.tex")


def _artifact_digest(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _artifact_snapshot() -> dict[str, str | None]:
    return {
        name: _artifact_digest(Path.cwd() / name)
        for name in _artifact_names()
    }


def _is_substantive_artifact(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if "\x00" in text:
        return False
    stripped = text.strip()
    if _lean_mode():
        return len(stripped) >= 20 and bool(
            re.search(r"(?m)^\s*(?:theorem|lemma|example|def)\b", stripped)
        )
    return len(stripped) >= 64


def _fresh_artifact(before: dict[str, str | None]) -> Path | None:
    for name in _artifact_names():
        candidate = Path.cwd() / name
        digest = _artifact_digest(candidate)
        if (
            digest is not None
            and digest != before.get(name)
            and _is_substantive_artifact(candidate)
        ):
            return candidate
    return None


def _normalize_informal_outcome(path: Path) -> bool:
    """Fail closed to one canonical footer for Kimi's Markdown artifact."""
    if _lean_mode() or path.name != "proof.md" or path.is_symlink():
        return True
    try:
        original = path.read_text(encoding="utf-8")
        rendered = canonicalize_outcome_text(original)
        path.write_text(rendered, encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return True


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip()
    if not prompt:
        print("Kimi Codex runner requires a prompt", file=sys.stderr)
        return 2
    model = os.environ.get("AGENT_MONITOR_SELECTED_MODEL", "kimi-k3").strip()
    artifact_before = _artifact_snapshot()
    try:
        result = codex_exec(
            LEAN_SYSTEM_PROMPT if _lean_mode() else SYSTEM_PROMPT,
            prompt,
            model=model,
            timeout=float(os.environ.get("AGENT_MONITOR_CODEX_TIMEOUT", "3600")),
            emit_event=_emit,
            workspace=Path.cwd(),
            sandbox="danger-full-access",
            enable_tools=True,
        )
    except (CodexBackendError, OSError, ValueError) as exc:
        print(f"Kimi Codex adapter failed: {exc}", file=sys.stderr, flush=True)
        return 1

    artifact = _fresh_artifact(artifact_before)
    if artifact is None:
        expected = "Proof.lean" if _lean_mode() else "proof.md or proof.tex"
        print(
            f"Kimi Codex adapter finished without a new substantive {expected}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    if not _normalize_informal_outcome(artifact):
        print(
            "Kimi Codex adapter could not normalize proof.md outcome contract",
            file=sys.stderr,
            flush=True,
        )
        return 1

    # codex_exec already streamed the native events.  Keep the final text as a
    # plain diagnostic only; duplicating it as a fabricated event would double
    # count the assistant turn in Monitor.
    if result.text:
        print(f"[kimi-codex] {result.text[:1000]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
