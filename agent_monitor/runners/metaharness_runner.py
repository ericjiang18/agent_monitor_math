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


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_item(item: dict) -> None:
    emit({"type": "item.completed", "item": item})


def chat(client, model: str, system: str, user: str) -> tuple[str, dict]:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    usage = resp.usage
    u = {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
    }
    emit({"type": "turn.completed", "usage": u})
    return resp.choices[0].message.content or "", u


INITIAL_HARNESS = (
    "You are a careful mathematics prover. Read the problem, plan the proof "
    "structure, then output ONLY the full informal proof as a Markdown document "
    "(use $...$ / $$...$$ for math, headings for structure, end with ∎)."
)


def extract_md(text: str) -> str:
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt.strip():
        emit_item({"type": "error", "message": "empty prompt"})
        return 2

    from openai import OpenAI

    client = OpenAI()
    model = os.environ.get("METAHARNESS_MODEL", os.environ.get("AGENT_MONITOR_OPENAI_MODEL", "gpt-5.2"))
    rounds = int(os.environ.get("METAHARNESS_ROUNDS", "3"))
    ws = Path.cwd()
    harness = INITIAL_HARNESS
    best_score = -1.0

    emit_item({"type": "agent_message", "text": f"meta-harness loop · model {model} · max {rounds} rounds"})

    for rnd in range(1, rounds + 1):
        # ── 1. solver under current harness ─────────────────────────────
        emit_item({"type": "reasoning", "text": f"[round {rnd}] harness spec:\n{harness[:1200]}"})
        out, _ = chat(client, model, harness, prompt)
        md = extract_md(out)
        (ws / "proof.md").write_text(md, encoding="utf-8")
        emit_item({"type": "agent_message", "text": f"[round {rnd} · solver] {out[:3000]}"})
        emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})

        # ── 2. evaluator: critique ──────────────────────────────────────
        critique, _ = chat(
            client,
            model,
            "You are a strict proof evaluator. Score the proof 0-10 for correctness, "
            "rigor, and completeness. Output: 'SCORE: <n>' on the first line, then "
            "specific weaknesses.",
            f"PROBLEM:\n{prompt}\n\nPROOF (Markdown):\n{md[:8000]}",
        )
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
        harness_new, _ = chat(
            client,
            model,
            "You are the Meta-Harness PROPOSER. You optimize the harness (system "
            "prompt) of a proving agent. Given the current harness and the "
            "evaluator's critique, output ONLY the improved harness text — add "
            "concrete strategy instructions that address the weaknesses.",
            f"CURRENT HARNESS:\n{harness}\n\nEVALUATOR CRITIQUE:\n{critique[:3000]}",
        )
        if harness_new.strip():
            harness = harness_new.strip()[:4000]
            emit_item({"type": "reasoning", "text": f"[round {rnd} · proposer] evolved harness:\n{harness[:1500]}"})

    emit_item({"type": "agent_message", "text": f"finished · best score {best_score}/10"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
