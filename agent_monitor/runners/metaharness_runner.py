#!/usr/bin/env python3
"""Meta-Harness proving runner.

Implements the Meta-Harness optimization loop (stanford-iris-lab/meta-harness,
license and citation under engines/metaharness) for informal proving: a PROPOSER revises
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

try:
    from .api_backend import APIBackendError, api_chat
    from .codex_backend import CodexBackendError, codex_exec, subscription_enabled
except ImportError:
    from api_backend import APIBackendError, api_chat
    from codex_backend import CodexBackendError, codex_exec, subscription_enabled


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_item(item: dict) -> None:
    emit({"type": "item.completed", "item": item})


def chat(client, model: str, system: str, user: str, *, use_codex: bool = False) -> tuple[str, dict]:
    if use_codex:
        result = codex_exec(system, user, model=model, emit_event=emit)
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


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt.strip():
        emit_item({"type": "error", "message": "empty prompt"})
        return 2

    use_codex = subscription_enabled()
    client = None
    model = (
        os.environ.get("AGENT_MONITOR_CODEX_MODEL", "gpt-5.6-sol")
        if use_codex
        else os.environ.get(
            "METAHARNESS_MODEL",
            os.environ.get("AGENT_MONITOR_OPENAI_MODEL", "gpt-5.2"),
        )
    )
    rounds = int(os.environ.get("METAHARNESS_ROUNDS", "3"))
    ws = Path.cwd()
    harness = enforce_solver_runtime_contract(INITIAL_HARNESS)
    best_score = -1.0

    emit_item({"type": "agent_message", "text": f"meta-harness loop · model {model} · max {rounds} rounds"})

    def call(system: str, user: str) -> tuple[str, dict] | None:
        try:
            return chat(client, model, system, user, use_codex=use_codex)
        except (CodexBackendError, APIBackendError) as exc:
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
            "You are a strict proof evaluator. Score the proof 0-10 for correctness, "
            "rigor, and completeness. Output: 'SCORE: <n>' on the first line, then "
            "specific weaknesses.",
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
            "You are the Meta-Harness PROPOSER. You optimize the harness (system "
            "prompt) of a proving agent. Given the current harness and the "
            "evaluator's critique, output ONLY the improved harness text — add "
            "concrete strategy instructions that address the weaknesses.",
            f"CURRENT HARNESS:\n{harness}\n\nEVALUATOR CRITIQUE:\n{critique[:3000]}",
        )
        if proposer_result is None:
            return 1
        harness_new, _ = proposer_result
        if harness_new.strip():
            harness = enforce_solver_runtime_contract(harness_new)
            emit_item({"type": "reasoning", "text": f"[round {rnd} · proposer] evolved harness:\n{harness[:1500]}"})

    emit_item({"type": "agent_message", "text": f"finished · best score {best_score}/10"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
