#!/usr/bin/env python3
"""Trusted response-only audit adapter for an IMProof candidate.

The adapter is intentionally a separate process whose working directory is the
run-owned console workspace.  IMProof's native Author/Critic sandbox therefore
keeps its existing filesystem boundary while the trusted runner owns validation
and bounded artifact writes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_monitor.runners.api_backend import APIBackendError, api_chat
from agent_monitor.runners.claude_backend import (
    ClaudeBackendError,
    claude_exec,
    forward_as_codex_event,
    subscription_enabled as claude_subscription_enabled,
)
from agent_monitor.runners.codex_backend import (
    CodexBackendError,
    subscription_enabled as codex_subscription_enabled,
)
from agent_monitor.runners.deepseek_harness_runner import (
    ensure_requested_outcome,
    run_codex_audit,
)


def _regular_workspace_file(relative: str) -> Path:
    root = Path.cwd().resolve()
    raw = root / relative
    path = raw.resolve()
    if (
        Path(relative).is_absolute()
        or raw.is_symlink()
        or path.parent != root
        or not path.is_file()
    ):
        raise OSError(f"refusing non-regular or escaped adapter input: {relative}")
    return path


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _api_call(system: str, user: str, *, model: str, emit_event=None):
    result = api_chat(system, user, model=model)
    event = {"type": "turn.completed", "usage": result.usage}
    (emit_event or _emit)(event)
    return result
def _claude_call(system: str, user: str, *, model: str, emit_event=None):
    def project(event: dict) -> None:
        forward_as_codex_event(event, emit_event or _emit)

    try:
        return claude_exec(
            system,
            user,
            model=model,
            enable_tools=False,
            emit_event=project,
        )
    except ClaudeBackendError as exc:
        # The shared audit orchestrator handles CodexBackendError fail-closed.
        # Rewrap only the exception type; never retry another provider.
        raise CodexBackendError(str(exc)) from exc




def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args(argv)

    prompt_path = _regular_workspace_file(args.prompt)
    candidate_path = _regular_workspace_file(args.candidate)
    prompt = prompt_path.read_text(encoding="utf-8", errors="replace")
    candidate = candidate_path.read_text(encoding="utf-8", errors="replace")
    if len(candidate.strip()) < 40:
        raise SystemExit("IMProof candidate is empty or too short to audit")

    _emit(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": (
                    "IMProof · trusted supplemental audit · "
                    "global, decomposed, refuter, merge, adjudication"
                ),
            },
        }
    )
    if claude_subscription_enabled():
        call = _claude_call
    elif codex_subscription_enabled():
        call = None
    else:
        call = _api_call
    try:
        final = run_codex_audit(
            prompt=prompt,
            candidate=candidate,
            model=args.model,
            call=call,
            adapter="improof-response-orchestrated-audit",
            runtime_label="IMProof",
            audit_prefix="improof",
        )
        final = ensure_requested_outcome(prompt, final)
        Path("proof.md").write_text(final.rstrip() + "\n", encoding="utf-8")
    except (APIBackendError, CodexBackendError, OSError, ValueError) as exc:
        degraded = ensure_requested_outcome(
            prompt,
            candidate.rstrip()
            + "\n\n## Audit status\n\n"
            + "degraded audit: IMProof's trusted supplemental structured audit "
            + f"did not complete ({type(exc).__name__}: {exc}).",
        )
        Path("proof.md").write_text(degraded.rstrip() + "\n", encoding="utf-8")
        _emit(
            {
                "type": "item.completed",
                "item": {"type": "error", "message": str(exc)[-1600:]},
            }
        )
        return 1

    _emit(
        {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [{"path": "proof.md"}, {"path": "audit/"}],
            },
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
