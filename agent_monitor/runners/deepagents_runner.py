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
from typing import Any, Sequence


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


def _create_codex_subscription_model(workspace: Path, model_name: str):
    """Build a BaseChatModel backed by the logged-in ``codex exec`` CLI.

    Authentication is deliberately left entirely to Codex via CODEX_HOME.  The
    subprocess receives a small allow-list of environment variables, excluding
    API keys, and this adapter never opens or copies Codex's auth files.
    """
    import shutil
    import subprocess
    import tempfile
    import uuid

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.utils.function_calling import convert_to_openai_tool

    codex_home = os.environ.get("CODEX_HOME")
    if not codex_home:
        raise RuntimeError(
            "AGENT_MONITOR_CODEX_SUBSCRIPTION=1 requires a logged-in CODEX_HOME"
        )

    codex_binary = shutil.which("codex")
    if not codex_binary:
        for candidate in (
            Path.home() / ".npm-global" / "bin" / "codex",
            Path("/usr/local/bin/codex"),
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                codex_binary = str(candidate)
                break
    if not codex_binary:
        raise RuntimeError("codex executable not found")

    class CodexExecChatModel(BaseChatModel):
        """Minimal synchronous LangChain chat model using Codex subscription auth."""

        codex_binary: str
        codex_home: str
        workspace: str
        model_name: str = ""
        timeout_seconds: int = 900

        @property
        def _llm_type(self) -> str:
            return "codex-exec-subscription"

        @property
        def _identifying_params(self) -> dict[str, Any]:
            return {"model_name": self.model_name, "codex_home": self.codex_home}

        def bind_tools(
            self,
            tools: Sequence[Any],
            *,
            tool_choice: str | dict | bool | None = None,
            **kwargs: Any,
        ):
            """Expose LangChain tools to ``_generate`` through Runnable binding."""
            formatted = [convert_to_openai_tool(tool) for tool in tools]
            return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

        @staticmethod
        def _render_messages(messages: Sequence[Any]) -> str:
            rendered: list[str] = []
            for message in messages:
                role = getattr(message, "type", type(message).__name__)
                if role == "human":
                    role = "user"
                elif role == "ai":
                    role = "assistant"
                body = _content_text(getattr(message, "content", ""))
                tool_calls = getattr(message, "tool_calls", None) or []
                if tool_calls:
                    body += "\nTool calls: " + json.dumps(
                        tool_calls, ensure_ascii=False, default=str
                    )
                tool_call_id = getattr(message, "tool_call_id", None)
                if tool_call_id:
                    body = f"tool_call_id={tool_call_id}\n{body}"
                rendered.append(f"<{role}>\n{body}\n</{role}>")
            return "\n\n".join(rendered)

        def _generate(
            self,
            messages: list[Any],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> ChatResult:
            tools = list(kwargs.get("tools") or [])
            tool_choice = kwargs.get("tool_choice")
            tool_names = [
                str(tool.get("function", {}).get("name") or "")
                for tool in tools
                if isinstance(tool, dict)
            ]
            tool_names = [name for name in tool_names if name]

            schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["text", "tool_calls"]},
                    "text": {"type": "string"},
                    "tool_calls": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": (
                                    {"type": "string", "enum": tool_names}
                                    if tool_names
                                    else {"type": "string"}
                                ),
                                # A JSON string avoids an unconstrained nested
                                # object, which Codex structured output rejects.
                                "arguments": {"type": "string"},
                            },
                            "required": ["name", "arguments"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["kind", "text", "tool_calls"],
                "additionalProperties": False,
            }
            tool_description = (
                json.dumps(tools, ensure_ascii=False, default=str)
                if tools
                else "[]"
            )
            request = (
                "Act only as the chat model for the LangChain conversation below. "
                "Do not use Codex's own shell or file tools and do not edit files. "
                "Return the next assistant message using the required output schema.\n"
                "For a normal answer set kind='text', put the answer in text, and use "
                "an empty tool_calls array. To call tools, set kind='tool_calls', put "
                "each exact tool name and a JSON-encoded arguments object in "
                "tool_calls, and keep text empty unless a short preamble is useful. "
                "Never invent a tool name. Multiple tool calls are allowed.\n"
                f"LangChain tool_choice: {json.dumps(tool_choice, default=str)}\n"
                f"Available LangChain tools: {tool_description}\n\n"
                "Conversation:\n"
                f"{self._render_messages(messages)}"
            )

            # Do not pass OPENAI_API_KEY (or any other credential) into Codex.
            # Codex resolves its already-stored login itself from CODEX_HOME.
            allowed_env = (
                "PATH",
                "HOME",
                "CODEX_HOME",
                "LANG",
                "LC_ALL",
                "TERM",
                "NO_COLOR",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "NIX_SSL_CERT_FILE",
            )
            env = {key: os.environ[key] for key in allowed_env if key in os.environ}
            env["CODEX_HOME"] = self.codex_home
            env["NO_COLOR"] = "1"

            with tempfile.TemporaryDirectory(prefix="deepagents-codex-") as temp_dir:
                schema_path = Path(temp_dir) / "response-schema.json"
                output_path = Path(temp_dir) / "last-message.json"
                schema_path.write_text(
                    json.dumps(schema, ensure_ascii=False), encoding="utf-8"
                )
                command = [
                    self.codex_binary,
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--cd",
                    self.workspace,
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                ]
                cli_model = self.model_name.removeprefix("openai:").strip()
                if cli_model:
                    command.extend(["--model", cli_model])
                command.append("-")
                try:
                    proc = subprocess.run(
                        command,
                        input=request,
                        text=True,
                        capture_output=True,
                        timeout=self.timeout_seconds,
                        env=env,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(
                        f"codex exec timed out after {self.timeout_seconds}s"
                    ) from exc
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout or "no diagnostic").strip()
                    raise RuntimeError(
                        f"codex exec failed ({proc.returncode}): {detail[-4000:]}"
                    )
                raw = (
                    output_path.read_text(encoding="utf-8")
                    if output_path.exists()
                    else (proc.stdout or "")
                ).strip()

            try:
                response = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"codex exec returned invalid structured output: {raw[:1000]}"
                ) from exc

            text = str(response.get("text") or "")
            langchain_calls: list[dict[str, Any]] = []
            if response.get("kind") == "tool_calls":
                for call in response.get("tool_calls") or []:
                    name = str(call.get("name") or "")
                    if name not in tool_names:
                        continue
                    try:
                        arguments = json.loads(call.get("arguments") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {"input": arguments}
                    langchain_calls.append(
                        {
                            "name": name,
                            "args": arguments,
                            "id": f"call_{uuid.uuid4().hex}",
                            "type": "tool_call",
                        }
                    )
            if response.get("kind") == "tool_calls" and not langchain_calls:
                raise RuntimeError("codex exec requested no valid LangChain tool calls")

            message = AIMessage(content=text, tool_calls=langchain_calls)
            return ChatResult(generations=[ChatGeneration(message=message)])

    timeout_text = os.environ.get("AGENT_MONITOR_CODEX_TIMEOUT", "900")
    try:
        timeout_seconds = max(1, int(timeout_text))
    except ValueError:
        timeout_seconds = 900
    return CodexExecChatModel(
        codex_binary=codex_binary,
        codex_home=codex_home,
        workspace=str(workspace),
        model_name=model_name,
        timeout_seconds=timeout_seconds,
    )


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


def _load_user_tools(workspace: Path) -> list:
    """Register library tools (workspace/_library/tools.json) as callable tools."""
    manifest = workspace / "_library" / "tools.json"
    if not manifest.exists():
        return []
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    import subprocess

    from langchain_core.tools import StructuredTool

    tools = []
    for entry in entries:
        script = workspace / str(entry.get("script") or "")
        if not script.exists():
            continue
        tname = re.sub(r"\W+", "_", str(entry.get("name") or script.stem)).strip("_") or script.stem

        def _run(args: str = "", _script=script) -> str:
            try:
                proc = subprocess.run(
                    ["bash", str(_script), *([a for a in args.split() if a])],
                    capture_output=True, text=True, timeout=120, cwd=str(workspace),
                )
                out = (proc.stdout or "") + (("\n[stderr] " + proc.stderr) if proc.stderr else "")
                return out.strip()[:6000] or f"(exit {proc.returncode}, no output)"
            except Exception as exc:  # noqa: BLE001
                return f"tool error: {exc}"

        tools.append(
            StructuredTool.from_function(
                func=_run,
                name=f"user_tool_{tname}"[:60],
                description=(str(entry.get("description") or "") or f"User library tool {tname}")
                + " (args: optional space-separated arguments)",
            )
        )
    return tools


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt.strip():
        emit_item({"type": "error", "message": "empty prompt"})
        return 2

    workspace = Path.cwd().resolve()
    subscription_mode = os.environ.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"
    model = (
        "openai:" + os.environ.get("AGENT_MONITOR_CODEX_MODEL", "gpt-5.6-sol")
        if subscription_mode
        else os.environ.get("DEEPAGENTS_MODEL")
        or "openai:" + os.environ.get("AGENT_MONITOR_OPENAI_MODEL", "gpt-5.2")
    )

    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    user_prompt = _rewrite_prompt_for_virtual_fs(prompt, workspace)
    user_tools = _load_user_tools(workspace)
    if subscription_mode:
        try:
            deepagents_model = _create_codex_subscription_model(workspace, model)
        except Exception as exc:  # noqa: BLE001
            emit_item({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return 1
    else:
        deepagents_model = model
    emit_item({
        "type": "agent_message",
        "text": "deepagents session · model "
        + (f"codex-subscription:{model}" if subscription_mode else model)
        + f" · fs={workspace}"
        + (f" · user tools: {', '.join(t.name for t in user_tools)}" if user_tools else ""),
    })

    agent = create_deep_agent(
        model=deepagents_model,
        tools=user_tools or None,
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
