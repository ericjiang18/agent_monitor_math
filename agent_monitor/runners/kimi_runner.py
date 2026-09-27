#!/usr/bin/env python3
"""Bounded Kimi proof harness: three remote calls, no executable model tools."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .plain_runner import emit, emit_item
except ImportError:
    from plain_runner import emit, emit_item


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt.strip():
        emit_item({"type": "error", "message": "Enter a problem to prove"})
        return 2
    from openai import OpenAI
    import httpx

    key = os.environ.get("KIMI_API_KEY", "")
    if not key:
        emit_item({"type": "error", "message": "Add a Kimi API key in Settings"})
        return 2
    # Neither endpoint nor headers are derived from the prompt. Do not follow
    # redirects, expose tools, import generated code, or invoke local agents.
    base_url = "https://api.moonshot.ai/v1"
    if os.environ.get("AGENT_MONITOR_SPONSORED_KIMI") == "1":
        base_url = os.environ.get("KIMI_API_BASE", "")
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
                or not parsed.port or parsed.path != "/internal/v1"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            emit_item({"type": "error", "message": "Sponsored Kimi endpoint attestation failed"})
            return 2
    client = OpenAI(api_key=key, base_url=base_url,
                    timeout=180.0, max_retries=0,
                    http_client=httpx.Client(follow_redirects=False, trust_env=False))
    model = "kimi-k3"
    try:
        output_limit = int(os.environ.get("AGENT_MONITOR_PLAIN_MAX_OUTPUT_TOKENS", "4096"))
    except ValueError:
        output_limit = 4096
    output_limit = max(256, min(output_limit, 8192))
    effort = os.environ.get("KIMI_REASONING_EFFORT", "low").strip().lower()
    if effort not in {"low", "high", "max"}:
        effort = "low"
    system = (
        "You are a careful mathematician. Write in English and Markdown with LaTeX math. "
        "Distinguish complete proofs, conditional arguments, and remaining gaps. "
        "Never claim an open problem is solved without a complete justified proof. "
        "You have no tools. Return text only; do not request shell commands or file access. "
        "Cite only real references you know and state uncertainty where needed."
    )
    draft = critique = ""
    stages = ("Draft", "Critique", "Refine")
    try:
        for stage in stages:
            if stage == "Draft":
                task = f"Develop a proof or clearly identify what remains open.\n\n{prompt}"
            elif stage == "Critique":
                task = f"Audit the proposed argument. Identify gaps, counterexamples, and unsupported steps.\n\nPROBLEM:\n{prompt}\n\nDRAFT:\n{draft}"
            else:
                task = f"Write the final proof with explicit caveats for unresolved gaps. Incorporate the critique.\n\nPROBLEM:\n{prompt}\n\nDRAFT:\n{draft}\n\nCRITIQUE:\n{critique}"
            emit({"type": "turn.started"})
            emit_item({"type": "agent_message", "text": f"{stage} · Kimi K3"})
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": task}],
                max_tokens=output_limit,
                reasoning_effort=effort,
            )
            text = response.choices[0].message.content or ""
            if not text.strip():
                raise ValueError("Kimi returned an empty response")
            if response.choices[0].finish_reason == "length":
                text += "\n\n> This stage reached its output limit. The argument may be incomplete.\n"
            emit_item({"type": "agent_message", "text": text})
            usage = response.usage
            emit({"type": "turn.completed", "usage": {
                "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            }})
            if stage == "Draft":
                draft = text
                Path("draft.md").write_text(text, encoding="utf-8")
            elif stage == "Critique":
                critique = text
                Path("critique.md").write_text(text, encoding="utf-8")
            else:
                Path("proof.md").write_text(text, encoding="utf-8")
        emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})
        return 0
    except Exception as exc:
        # SDK exception text can contain echoed request data. Keep the public
        # trace useful without writing credentials or provider payloads.
        emit_item({"type": "error", "message": f"Kimi could not finish this proof ({type(exc).__name__}). Check your key and try again."})
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
