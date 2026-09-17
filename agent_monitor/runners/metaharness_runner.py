#!/usr/bin/env python3
"""Meta-Harness proving runner.

Implements the Meta-Harness optimization loop (stanford-iris-lab/meta-harness,
vendored under engines/metaharness) for informal proving: a PROPOSER revises
the solver harness (system prompt / strategy) between rounds based on an
EVALUATOR's critique of the produced proof.

Round structure (each = distinct Monitor nodes):
  1. solver   — writes proof.tex using the current harness spec
  2. evaluator— compiles + critiques the proof, scores 0-10
  3. proposer — rewrites the harness spec if the score is low

Streams codex-style JSONL events to stdout for the console's CLIEventParser.
Usage: metaharness_runner.py "<prompt>"   (cwd = run workspace)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

if __package__:
    from .api_backend import APIBackendError, api_chat
    from .claude_backend import (
        ClaudeBackendError,
        claude_exec as claude_code_exec,
        forward_as_codex_event,
        subscription_enabled as claude_subscription_enabled,
    )
    from .codex_backend import CodexBackendError, codex_exec, subscription_enabled
    from .deepseek_harness_runner import (
        audit_requested,
        ensure_requested_outcome,
        run_codex_audit,
    )
else:
    # Direct registry execution starts with only this directory on sys.path.
    # Restore the repository package root before importing package-qualified
    # modules so their own relative imports remain valid.
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
    from agent_monitor.runners.deepseek_harness_runner import (
        audit_requested,
        ensure_requested_outcome,
        run_codex_audit,
    )


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_item(item: dict) -> None:
    emit({"type": "item.completed", "item": item})


def chat(client, model: str, system: str, user: str, *, use_codex: bool = False, use_claude: bool = False) -> tuple[str, dict]:
    if use_codex:
        result = codex_exec(system, user, model=model, emit_event=emit)
        return result.text, result.usage

    if use_claude:
        result = claude_code_exec(
            system,
            user,
            model=model,
            emit_event=lambda event: forward_as_codex_event(event, emit),
            source_env=os.environ,
        )
        return result.text, result.usage

    result = api_chat(system, user, model=model)
    u = result.usage
    emit({"type": "turn.completed", "usage": u})
    return result.text, u


INITIAL_HARNESS = (
    "You are a careful mathematics prover. Read the problem, plan the proof "
    "structure, then output ONLY the full informal proof as a Markdown document "
    "(use $...$ / $$...$$ for math, headings for structure, end with ∎)."
)

SOLVER_RUNTIME_CONTRACT = (
    "\n\nRuntime contract (retain verbatim in every proposed harness): "
    "You may use available read-only research and computation tools, but do not "
    "attempt filesystem writes or discuss runtime permissions. The runner saves "
    "your response as proof.md. Return ONLY the complete informal proof as a "
    "self-contained Markdown document."
)
DEGRADED_AUDIT_DISCLOSURE = (
    "degraded audit: Meta-Harness could not complete its internal file-backed "
    "global, decomposed, refuter, merge, and adjudication passes."
)
EVALUATOR_SYSTEM = (
    "You are a strict proof evaluator. Score the candidate proof 0-10 for "
    "mathematical correctness, rigor, and completeness. Output: 'SCORE: <n>' on "
    "the first line, then specific weaknesses. Meta-Harness runs the independent "
    "file-backed audit after this optimization loop, so do not lower the candidate "
    "score merely because audit JSON files are not yet present and do not ask this "
    "response-only solver to write them."
)
PROPOSER_SYSTEM = (
    "You are the Meta-Harness PROPOSER. You optimize the harness (system prompt) "
    "of a proving agent. Given the current harness and the evaluator's critique, "
    "output ONLY the improved harness text and add concrete mathematical strategy "
    "instructions that address the weaknesses. The runner performs file-backed "
    "auditing after the optimization loop; never add instructions for the "
    "response-only solver to create audit files."
)


def enforce_solver_runtime_contract(text: str, *, limit: int = 4000) -> str:
    """Keep Meta-Harness evolution inside its read-only response contract."""
    base = text.strip()
    budget = max(0, limit - len(SOLVER_RUNTIME_CONTRACT))
    return base[:budget].rstrip() + SOLVER_RUNTIME_CONTRACT


def extract_md(text: str) -> str:
    stripped = text.strip()
    # Unwrap only when the entire response is one optional Markdown fence. A
    # proof commonly contains its own fenced command or output snippets.
    m = re.fullmatch(
        r"```(?:markdown|md)?[ \t]*\n(.*?)\n?```[ \t]*",
        stripped,
        re.S | re.IGNORECASE,
    )
    return (m.group(1) if m else stripped).strip()


def evaluator_proof_excerpt(text: str, *, limit: int = 50000) -> str:
    """Give the evaluator a bounded view without silently deleting the ending."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = max(1, (limit * 3) // 4)
    marker = "\n\n[... evaluator proof window omitted middle ...]\n\n"
    tail = max(1, limit - head - len(marker))
    return text[:head] + marker + text[-tail:]


def ensure_audit_disclosure(prompt: str, text: str) -> str:
    """Disclose the response-only harness limitation without duplicating outcomes."""
    if (
        "candidate audit gate:" not in prompt.lower()
        or "degraded audit:" in text.lower()
    ):
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


def complete_file_backed_audit(
    *,
    prompt: str,
    candidate: str,
    model: str,
    call,
) -> str:
    """Run fresh-context verifier calls while the runner owns all writes.

    Meta-Harness model turns remain response-only and read-only. The trusted
    runner validates their JSON and writes the bounded audit contract, exactly
    as it already writes ``proof.md`` from a solver response.
    """

    def audit_call(system: str, user: str, **_kwargs):
        result = call(system, user)
        if result is None:
            raise CodexBackendError("Meta-Harness audit model call failed")
        text, usage = result
        return SimpleNamespace(text=text, usage=usage)

    return run_codex_audit(
        prompt=prompt,
        candidate=candidate,
        model=model,
        call=audit_call,
        adapter="metaharness-response-orchestrated-audit",
        runtime_label="Meta-Harness",
        audit_prefix="metaharness",
    )


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
    client = None
    model = (
        os.environ.get("AGENT_MONITOR_CODEX_MODEL", "gpt-5.6-sol")
        if use_codex
        else os.environ.get("AGENT_MONITOR_CLAUDE_MODEL", "sonnet")
        if use_claude else os.environ.get(
            "METAHARNESS_MODEL",
            os.environ.get("AGENT_MONITOR_OPENAI_MODEL", "gpt-5.2"),
        )
    )
    public_kimi = os.environ.get("AGENT_MONITOR_PUBLIC_KIMI", "").strip() == "1"
    # Two public rounds are at most five response-only calls:
    # solver/evaluator/proposer, then solver/evaluator.  Request text and
    # deployment overrides cannot raise this bound.
    rounds = 2 if public_kimi else int(os.environ.get("METAHARNESS_ROUNDS", "3"))
    ws = Path.cwd()
    harness = enforce_solver_runtime_contract(INITIAL_HARNESS)
    best_score = -1.0

    emit_item({"type": "agent_message", "text": f"meta-harness loop · model {model} · max {rounds} rounds"})

    def call(system: str, user: str) -> tuple[str, dict] | None:
        try:
            return chat(client, model, system, user, use_codex=use_codex, use_claude=use_claude)
        except (CodexBackendError, ClaudeBackendError, APIBackendError) as exc:
            emit_item({"type": "error", "message": str(exc)})
            return None

    for rnd in range(1, rounds + 1):
        # ── 1. solver under current harness ─────────────────────────────
        emit_item({"type": "reasoning", "text": f"[round {rnd}] harness spec:\n{harness[:1200]}"})
        solver_result = call(harness, prompt)
        if solver_result is None:
            return 1
        out, _ = solver_result
        md = extract_md(out)
        (ws / "proof.md").write_text(md, encoding="utf-8")
        emit_item({"type": "agent_message", "text": f"[round {rnd} · solver] {out[:3000]}"})
        emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})

        # ── 2. evaluator: critique ──────────────────────────────────────
        evaluator_result = call(
            EVALUATOR_SYSTEM,
            (
                f"PROBLEM:\n{prompt}\n\nPROOF (Markdown):\n"
                + evaluator_proof_excerpt(md)
            ),
        )
        if evaluator_result is None:
            return 1
        critique, _ = evaluator_result
        m = re.search(r"SCORE:\s*([0-9.]+)", critique)
        score = float(m.group(1)) if m else 0.0
        emit_item({"type": "agent_message", "text": f"[round {rnd} · evaluator] score {score}/10\n{critique[:2500]}"})
        best_score = max(best_score, score)
        if score >= float(os.environ.get("METAHARNESS_TARGET", "8")):
            emit_item({"type": "agent_message", "text": f"target reached at round {rnd} — stopping"})
            break
        if rnd == rounds:
            break

        # ── 3. proposer: evolve the harness spec ────────────────────────
        proposer_result = call(
            PROPOSER_SYSTEM,
            f"CURRENT HARNESS:\n{harness}\n\nEVALUATOR CRITIQUE:\n{critique[:3000]}",
        )
        if proposer_result is None:
            return 1
        harness_new, _ = proposer_result
        if harness_new.strip():
            harness = enforce_solver_runtime_contract(harness_new)
            emit_item({"type": "reasoning", "text": f"[round {rnd} · proposer] evolved harness:\n{harness[:1500]}"})

    proof = ws / "proof.md"
    if proof.is_file():
        candidate = proof.read_text(encoding="utf-8", errors="replace")
        final_text = candidate
        if audit_requested(prompt) and not public_kimi:
            emit_item({
                "type": "agent_message",
                "text": (
                    "Meta-Harness · running fresh-context global, decomposed, "
                    "refuter, merge, and adjudication passes"
                ),
            })
            try:
                final_text = complete_file_backed_audit(
                    prompt=prompt,
                    candidate=candidate,
                    model=model,
                    call=call,
                )
            except (CodexBackendError, OSError, ValueError) as exc:
                emit_item({"type": "error", "message": f"Meta-Harness audit failed: {exc}"})
                final_text = ensure_audit_disclosure(prompt, candidate)
        final_text = ensure_requested_outcome(prompt, final_text)
        proof.write_text(final_text, encoding="utf-8")
    emit_item({"type": "agent_message", "text": f"finished · best score {best_score}/10"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
