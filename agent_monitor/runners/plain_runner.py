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

try:
    from .codex_backend import CodexBackendError, codex_exec, subscription_enabled
except ImportError:
    from codex_backend import CodexBackendError, codex_exec, subscription_enabled


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
    model = (
        os.environ.get("AGENT_MONITOR_CODEX_MODEL", "gpt-6-astra")
        if use_codex
        else os.environ.get(
            "PLAIN_MODEL",
            os.environ.get("AGENT_MONITOR_OPENAI_MODEL", "gpt-6-astra"),
        )
    )
    emit_item({"type": "agent_message", "text": f"plain single call · model {model} · no harness"})

    system = (
        "You are a mathematician. Write a complete, rigorous informal proof "
        "in Markdown. Use $...$ / $$...$$ for math. Structure: statement, "
        "proof, and a final ∎."
    )
    if use_codex:
        try:
            result = codex_exec(system, prompt, model=model, emit_event=emit)
        except CodexBackendError as exc:
            emit_item({"type": "error", "message": str(exc)})
            return 1
        Path("proof.md").write_text(result.text, encoding="utf-8")
        emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})
        return 0

    from openai import OpenAI

    kimi = model.startswith("kimi-")
    client = (OpenAI(api_key=os.environ.get("KIMI_API_KEY"), base_url="https://api.moonshot.ai/v1")
              if kimi else OpenAI())
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    raw_output_limit = os.environ.get("AGENT_MONITOR_PLAIN_MAX_OUTPUT_TOKENS", "").strip()
    if raw_output_limit:
        try:
            output_limit = int(raw_output_limit)
        except ValueError:
            output_limit = 4096
        request["max_completion_tokens"] = max(256, min(output_limit, 8192))
    if model.startswith(("gpt-6-", "gpt-5.6")):
        # Current reasoning models use the Responses API, including its
        # output-token ceiling and input/output usage field names.
        response_request = {"model": model, "input": request["messages"]}
        if "max_completion_tokens" in request:
            response_request["max_output_tokens"] = request["max_completion_tokens"]
        resp = client.responses.create(**response_request)
        text = resp.output_text or ""
        u = resp.usage
        input_tokens = getattr(u, "input_tokens", 0) or 0
        output_tokens = getattr(u, "output_tokens", 0) or 0
    else:
        if kimi:
            request["max_tokens"] = request.pop("max_completion_tokens", 4096)
            if model.startswith("kimi-k3"):
                request["reasoning_effort"] = "high"
            elif model == "kimi-k2.6":
                request["extra_body"] = {"thinking": {"type": "disabled"}}
        resp = client.chat.completions.create(**request)
        text = resp.choices[0].message.content or ""
        u = resp.usage
        input_tokens = getattr(u, "prompt_tokens", 0) or 0
        output_tokens = getattr(u, "completion_tokens", 0) or 0
    Path("proof.md").write_text(text, encoding="utf-8")
    emit_item({"type": "agent_message", "text": text[:6000]})
    emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})
    emit(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
