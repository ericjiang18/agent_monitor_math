#!/usr/bin/env python3
"""Plain LLM runner — no harness at all.

One single model call: problem in, proof out. The baseline to compare every
harness against. Streams codex-style JSONL events; writes proof.md in cwd.

Usage: plain_runner.py "<prompt>"   (cwd = run workspace)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if __package__:
    from .api_backend import APIBackendError, api_chat
    from .claude_backend import (
        ClaudeBackendError,
        claude_exec as claude_code_exec,
        forward_as_codex_event,
        subscription_enabled as claude_subscription_enabled,
    )
    from .codex_backend import CodexBackendError, codex_exec, subscription_enabled
    from .outcome_contract import canonicalize_outcome_text
else:
    # The engine registry launches this file by absolute path from the run
    # workspace. Import through the package so transitive relative imports
    # (notably claude_backend -> outcome_contract) keep their package context.
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from agent_monitor.runners.api_backend import APIBackendError, api_chat
    from agent_monitor.runners.claude_backend import (
        ClaudeBackendError,
        claude_exec as claude_code_exec,
        forward_as_codex_event,
        subscription_enabled as claude_subscription_enabled,
    )
    from agent_monitor.runners.codex_backend import (
        CodexBackendError,
        codex_exec,
        subscription_enabled,
    )
    from agent_monitor.runners.outcome_contract import canonicalize_outcome_text


SYSTEM_PROMPT = (
    "You are a rigorous mathematical research evaluator. Return only the "
    "final mathematical write-up in Markdown; the wrapper saves it. Do not "
    "access or modify files, discuss filesystem permissions, or estimate "
    "tokens, time, or cost. A request to prove a statement is not evidence "
    "that it is true: first check its exact quantifiers and determine whether "
    "it is true, false, currently open, or ill-posed. Try small cases and "
    "counterexamples. If it is true, give a complete proof with every "
    "nontrivial step justified. If it is false, give and verify an explicit "
    "counterexample. If it is open or you cannot close every gap, say so "
    "plainly and provide only verified partial progress—never present a "
    "plausible outline as a solution. Use $...$ / $$...$$ for math. "
    "During a continuation, if CURRENT proof.md is already correct and the "
    "human explicitly asks to preserve it exactly, return only "
    "[[PRESERVE_EXISTING_PROOF]]; the wrapper will keep the existing file. "
    "If the task asks for independent file-backed audit passes, disclose that "
    "the single-call Plain baseline cannot execute them; do not imply that it did."
)

PRESERVE_EXISTING_PROOF = "[[PRESERVE_EXISTING_PROOF]]"
DEGRADED_AUDIT_DISCLOSURE = (
    "degraded audit: single-call Plain baseline cannot execute independent "
    "file-backed audit passes."
)
API_RUNTIME_CONTRACT = (
    "\n\nThis is a non-agentic API call. You have no tools and cannot read, "
    "write, or inspect files. Do not emit function calls, tool calls, XML "
    "invocations, shell commands, or wrapper commentary. Return only the "
    "complete final mathematical write-up in Markdown; the wrapper saves it. "
    "The first non-whitespace line must be a level-one Markdown heading (`# ...`)."
)


def _api_artifact_issues(text: str) -> list[str]:
    lowered = text.lower()
    markers = (
        "<function_calls>",
        "</function_calls>",
        "<invoke ",
        "<parameter ",
    )
    issues = [marker for marker in markers if marker in lowered]
    if not text.lstrip().startswith("# "):
        issues.append("missing level-one final heading")
    return issues


def _human_feedback(prompt: str) -> str:
    marker = "HUMAN FEEDBACK \u2014 address this now:"
    return prompt.rsplit(marker, 1)[-1] if marker in prompt else ""


def _preserve_existing_proof(prompt: str, text: str, proof: Path) -> bool:
    """Keep an existing artifact when a continuation only acknowledges it.

    Plain is a one-call baseline and therefore cannot edit files itself: its
    response is normally promoted wholesale to proof.md. On an exact
    preservation request, however, an acknowledgement is not a proof and
    must not replace the artifact the user asked us to retain.
    """
    if not proof.is_file():
        return False
    feedback = " ".join(_human_feedback(prompt).lower().split())
    exact_request = (
        "preserve proof.md exactly" in feedback
        or "leave proof.md unchanged" in feedback
        or "keep proof.md unchanged" in feedback
    )
    if not exact_request:
        return False
    normalized = " ".join(text.strip().lower().split())
    if text.strip() == PRESERVE_EXISTING_PROOF:
        return True
    # Backward-compatible guard for models that answer with a short natural
    # language acknowledgement instead of the requested sentinel.
    return len(text) <= 600 and (
        "left unchanged" in normalized
        or "preserved exactly" in normalized
        or "no changes" in normalized
    )


def _ensure_audit_disclosure(prompt: str, text: str) -> str:
    """Make the no-harness baseline's audit limitation explicit.

    Plain intentionally remains one model call.  When an evaluation prompt
    requests independent file-backed passes, the wrapper records that those
    passes did not occur instead of letting a polished answer imply compliance.
    """
    audit_requested = (
        "candidate audit gate:" in prompt.lower()
        or "_agent/skills/research-proof-audit/skill.md" in prompt.lower()
    )
    if not audit_requested or "degraded audit:" in text.lower():
        return text
    marker = "ProvingConsole outcome:"
    marker_at = text.rfind(marker)
    if marker_at >= 0:
        line_at = text.rfind("\n", 0, marker_at) + 1
        return (
            text[:line_at].rstrip()
            + "\n\n"
            + DEGRADED_AUDIT_DISCLOSURE
            + "\n\n"
            + text[line_at:].lstrip()
        )
    return text.rstrip() + "\n\n" + DEGRADED_AUDIT_DISCLOSURE + "\n"


def _ensure_outcome_contract(text: str) -> str:
    """Guarantee one conservative machine-readable outcome line.

    A single recognized Markdown variant keeps its label but is canonicalized.
    Missing or conflicting labels are downgraded to Partial Progress. The
    wrapper never promotes ordinary prose into Solved or Counterexample.
    """
    return canonicalize_outcome_text(text, allow_legacy_marker=True)


def _save_result(prompt: str, text: str) -> bool:
    proof = Path("proof.md")
    if _preserve_existing_proof(prompt, text, proof):
        emit_item(
            {
                "type": "agent_message",
                "text": "Existing proof.md preserved exactly.",
            }
        )
        return False
    rendered = _ensure_audit_disclosure(prompt, text)
    if "provingconsole outcome:" in prompt.lower():
        rendered = _ensure_outcome_contract(rendered)
    proof.write_text(rendered, encoding="utf-8")
    emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})
    return True


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_item(item: dict) -> None:
    emit({"type": "item.completed", "item": item})


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt.strip():
        emit_item({"type": "error", "message": "empty prompt"})
        return 2

    use_codex = subscription_enabled()
    use_claude = claude_subscription_enabled()
    if use_codex and use_claude:
        emit_item({"type": "error", "message": "Codex and Claude account routes cannot both be active"})
        return 2
    model = (
        os.environ.get("AGENT_MONITOR_CODEX_MODEL", "gpt-5.6-sol")
        if use_codex
        else os.environ.get("AGENT_MONITOR_CLAUDE_MODEL", "sonnet")
        if use_claude else os.environ.get(
            "PLAIN_MODEL",
            os.environ.get("AGENT_MONITOR_OPENAI_MODEL", "gpt-5.2"),
        )
    )
    emit_item({"type": "agent_message", "text": f"plain single call · model {model} · no harness"})

    system = SYSTEM_PROMPT
    if use_codex:
        try:
            result = codex_exec(system, prompt, model=model, emit_event=emit)
        except CodexBackendError as exc:
            emit_item({"type": "error", "message": str(exc)})
            return 1
        _save_result(prompt, result.text)
        return 0

    if use_claude:
        try:
            result = claude_code_exec(
                system,
                prompt,
                model=model,
                emit_event=lambda event: forward_as_codex_event(event, emit),
                source_env=os.environ,
            )
        except ClaudeBackendError as exc:
            emit_item({"type": "error", "message": str(exc)})
            return 1
        _save_result(prompt, result.text)
        return 0

    api_system = system + API_RUNTIME_CONTRACT
    try:
        result = api_chat(api_system, prompt, model=model)
    except APIBackendError as exc:
        emit_item({"type": "error", "message": str(exc)})
        return 1
    issues = _api_artifact_issues(result.text)
    if issues:
        emit_item(
            {
                "type": "agent_message",
                "text": "Rejected pseudo-tool markup; retrying once for clean Markdown.",
            }
        )
        repair_prompt = (
            prompt
            + "\n\nYour prior response was rejected because it contained pseudo-tool "
            "or XML invocation markup. Do not describe or simulate any tool use. "
            "Return only the final mathematical Markdown write-up now."
        )
        try:
            repaired = api_chat(api_system, repair_prompt, model=model)
        except APIBackendError as exc:
            emit_item({"type": "error", "message": str(exc)})
            return 1
        result = type(result)(
            text=repaired.text,
            usage={
                "input_tokens": int(result.usage.get("input_tokens") or 0)
                + int(repaired.usage.get("input_tokens") or 0),
                "output_tokens": int(result.usage.get("output_tokens") or 0)
                + int(repaired.usage.get("output_tokens") or 0),
            },
            model=repaired.model,
            provider=repaired.provider,
        )
        if _api_artifact_issues(result.text):
            emit_item(
                {
                    "type": "error",
                    "message": "API model repeated prohibited pseudo-tool markup; no proof artifact was saved",
                }
            )
            return 1
    text = result.text
    _save_result(prompt, text)
    emit_item({"type": "agent_message", "text": text[:6000]})
    emit({"type": "turn.completed", "usage": result.usage})
    return 0


if __name__ == "__main__":
    sys.exit(main())
