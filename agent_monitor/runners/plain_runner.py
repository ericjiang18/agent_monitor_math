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

    from openai import OpenAI

    model = os.environ.get("PLAIN_MODEL", os.environ.get("AGENT_MONITOR_OPENAI_MODEL", "gpt-5.2"))
    emit_item({"type": "agent_message", "text": f"plain single call · model {model} · no harness"})

    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a mathematician. Write a complete, rigorous informal proof "
                    "in Markdown. Use $...$ / $$...$$ for math. Structure: statement, "
                    "proof, and a final ∎."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    text = resp.choices[0].message.content or ""
    u = resp.usage
    Path("proof.md").write_text(text, encoding="utf-8")
    emit_item({"type": "agent_message", "text": text[:6000]})
    emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})
    emit(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(u, "completion_tokens", 0) or 0,
            },
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
