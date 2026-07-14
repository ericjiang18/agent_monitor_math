#!/usr/bin/env python3
"""DeepAgents (LangChain) proving runner.

Runs a deep agent on an informal proof problem and streams codex-style JSONL
events to stdout so the console's CLIEventParser builds a multi-node trace:

  {"type": "item.completed", "item": {...}}
  {"type": "turn.completed", "usage": {...}}

Executed inside engines/deepagents/.venv (created by setup.sh).
Usage: deepagents_runner.py "<prompt>"   (cwd = run workspace)

Important: DeepAgents' default tool filesystem is virtual/in-memory. We mount a
FilesystemBackend rooted at the run workspace (virtual_mode=True) so read/write
tools see problem.txt / proof.md as real files under `/`.
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


def _content_text(content) -> str:
    """Flatten LangChain / Responses API content into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("text", "output_text", "input_text") and block.get("text"):
                    parts.append(str(block["text"]))
                elif "text" in block:
                    parts.append(str(block["text"]))
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(p for p in parts if p)
    return str(content)


def _looks_like_structured_dump(text: str) -> bool:
    s = (text or "").strip()
    return s.startswith("[{") or s.startswith('{"type"')


def _rewrite_prompt_for_virtual_fs(prompt: str, workspace: Path) -> str:
    """Replace host absolute-path instructions with virtual-root guidance."""
    problem = ""
    pfile = workspace / "problem.txt"
    if pfile.exists():
        try:
            problem = pfile.read_text(encoding="utf-8").strip()
        except OSError:
            problem = ""

    # Strip "Your working directory is: /Users/..." blocks that trick the agent
    # into using host paths that don't exist in the tool filesystem.
    cleaned = re.sub(
        r"Your working directory for this task is:\s*\n\S+\s*\n*",
        "",
        prompt,
        count=1,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.replace(str(workspace), ".")
    cleaned = cleaned.replace(str(workspace.resolve()), ".")

    header = (
        "Filesystem note: your tools see a VIRTUAL root `/` that is bound to this "
        "run's workspace. Use ONLY these paths (never macOS `/Users/...` paths):\n"
        "- `/problem.txt` — problem statement (already present)\n"
        "- `/proof.md` — write the final informal proof here (Markdown; $...$ / $$...$$)\n"
        "- optional `/scratch.md` for notes\n\n"
        "Do not ask the user to paste the problem. If needed, read `/problem.txt`.\n"
        "When finished, `/proof.md` must contain the complete proof.\n"
    )
    if problem:
        header += (
            "\n===== PROBLEM (also in /problem.txt) =====\n"
            f"{problem}\n"
            "===== END PROBLEM =====\n\n"
        )
    return header + cleaned.strip() + "\n"


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt.strip():
        emit_item({"type": "error", "message": "empty prompt"})
        return 2

    workspace = Path.cwd().resolve()
    model = os.environ.get("DEEPAGENTS_MODEL") or "openai:" + (
        os.environ.get("AGENT_MONITOR_OPENAI_MODEL", "gpt-5.2")
    )

    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    user_prompt = _rewrite_prompt_for_virtual_fs(prompt, workspace)
    emit_item({"type": "agent_message", "text": f"deepagents session · model {model} · fs={workspace}"})

    agent = create_deep_agent(
        model=model,
        backend=FilesystemBackend(root_dir=str(workspace), virtual_mode=True),
        system_prompt=(
            "You are a mathematics proving agent. Plan briefly, then write a complete "
            "informal proof.\n"
            "Your file tools operate on a virtual root `/` mapped to the task workspace. "
            "Always use paths like `/problem.txt` and `/proof.md` — never host paths "
            "under /Users or /mnt.\n"
            "Write the final proof to `/proof.md` (Markdown; use $...$ / $$...$$ for math)."
        ),
    )

    total_in = total_out = 0
    final_text = ""
    seen_msgs: set[str] = set()

    def handle_message(msg) -> None:
        nonlocal total_in, total_out, final_text
        mid = getattr(msg, "id", None) or str(id(msg))
        if mid in seen_msgs:
            return
        seen_msgs.add(mid)
        mtype = type(msg).__name__
        usage = getattr(msg, "usage_metadata", None) or {}
        if mtype == "AIMessage":
            text = _content_text(msg.content)
            for tc in getattr(msg, "tool_calls", None) or []:
                name = tc.get("name", "tool")
                args = json.dumps(tc.get("args") or {}, ensure_ascii=False)[:400]
                emit_item({"type": "command_execution", "command": f"{name}({args})"})
            if text and text.strip():
                emit_item({"type": "agent_message", "text": text[:6000]})
                if not _looks_like_structured_dump(text):
                    final_text = text
            if usage:
                total_in += int(usage.get("input_tokens") or 0)
                total_out += int(usage.get("output_tokens") or 0)
                emit({
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": int(usage.get("input_tokens") or 0),
                        "output_tokens": int(usage.get("output_tokens") or 0),
                    },
                })
        elif mtype == "ToolMessage":
            body = _content_text(msg.content)
            emit_item({
                "type": "reasoning",
                "text": f"[tool result · {getattr(msg, 'name', '?')}] {body[:1500]}",
            })

    files: dict = {}
    try:
        for chunk in agent.stream(
            {"messages": [{"role": "user", "content": user_prompt}]},
            stream_mode="values",
            config={"recursion_limit": 80},
        ):
            for m in chunk.get("messages") or []:
                handle_message(m)
            if isinstance(chunk.get("files"), dict):
                files = chunk["files"]
    except Exception as exc:  # noqa: BLE001
        emit_item({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1

    # With FilesystemBackend(virtual_mode=True), writes already land on disk.
    # Still sync any in-memory `files` dict leftovers, and report what exists.
    wrote: list[str] = []
    for name, content in (files or {}).items():
        if not isinstance(content, str):
            try:
                content = content.get("content") if isinstance(content, dict) else str(content)
            except Exception:  # noqa: BLE001
                continue
        rel = Path(name).name if Path(name).is_absolute() and not str(name).startswith(str(workspace)) else str(name).lstrip("/")
        # virtual paths like /proof.md → proof.md under workspace
        if rel.startswith(str(workspace)):
            try:
                rel = str(Path(rel).relative_to(workspace))
            except ValueError:
                rel = Path(rel).name
        target = workspace / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if content is not None:
                target.write_text(content or "", encoding="utf-8")
            wrote.append(rel)
        except OSError:
            continue

    for candidate in ("proof.md", "proof.tex", "problem.txt"):
        if (workspace / candidate).exists() and candidate not in wrote:
            wrote.append(candidate)

    if wrote:
        emit_item({"type": "file_change", "changes": [{"path": w} for w in wrote]})

    # Fallback: persist the final answer as proof.md if the agent didn't write it.
    if not (workspace / "proof.md").exists() and not (workspace / "proof.tex").exists():
        if final_text and not _looks_like_structured_dump(final_text):
            (workspace / "proof.md").write_text(final_text, encoding="utf-8")
            emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})

    emit_item({"type": "agent_message", "text": f"done · files: {', '.join(wrote) or 'none'}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
