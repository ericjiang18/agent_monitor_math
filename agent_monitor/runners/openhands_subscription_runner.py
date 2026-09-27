#!/usr/bin/env python3
"""Run an OpenHands SDK 1.21 agent on an existing Codex subscription login.

The preferred path is OpenHands ``ACPAgent`` backed by the official Codex ACP
package.  SDK 1.21 only probes ``~/.codex/auth.json`` when choosing ACP's
``chatgpt`` authentication method; it does not probe ``$CODEX_HOME/auth.json``.
For a nonstandard CODEX_HOME this runner creates a temporary *symlink* named
``.codex`` and temporarily points HOME at its parent.  The auth file and its
refresh token are never read, parsed, or copied by this runner.

If ACP cannot start, ``codex exec`` is used as a safe compatibility backend.
That fallback still reuses CODEX_HOME and emits Codex JSONL, but it delegates
the whole agent loop to Codex: OpenHands tools, condenser, and iteration
semantics are therefore not involved.

Usage: openhands_subscription_runner.py "<prompt>"  (cwd = run workspace)
"""
from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Iterator


SDK_VERSION = "1.21"
ACP_PACKAGE = "@agentclientprotocol/codex-acp"


class OpenHandsIterationLimit(RuntimeError):
    # The native OpenHands loop or the inner ACP activity loop reached its cap.
    pass


class _ACPToolBudget:
    """Bound a remote ACP turn by distinct tool calls, not status updates."""

    def __init__(self, limit: int):
        self.limit = limit
        self.tool_call_ids: set[str] = set()
        self.exceeded = False

    def observe(self, event) -> bool:
        tool_call_id = str(getattr(event, "tool_call_id", "") or "")
        if not tool_call_id:
            return False
        self.tool_call_ids.add(tool_call_id)
        if len(self.tool_call_ids) <= self.limit or self.exceeded:
            return False
        self.exceeded = True
        return True


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_item(item: dict) -> None:
    emit({"type": "item.completed", "item": item})


def _is_proof_response(text: str) -> bool:
    """Reject ACP hand-off messages that merely point at a missing artifact."""
    normalized = " ".join(str(text or "").split())
    if len(normalized) < 80:
        return False
    lowered = normalized.lower()
    handoff_words = ("completed", "created", "saved", "updated", "final deliverable")
    if "proof.md" in lowered and len(normalized) < 1000:
        if any(word in lowered for word in handoff_words):
            return False
    proof_signals = (
        "# proof",
        "proof:",
        "we prove",
        "counterexample",
        "suppose ",
        "assume ",
        "let ",
    )
    if "proof.md" in lowered and any(word in lowered for word in handoff_words):
        if not any(signal in lowered for signal in proof_signals):
            return False
    return True


def _valid_proof_file(path: Path) -> bool:
    try:
        return path.is_file() and _is_proof_response(path.read_text(encoding="utf-8"))
    except OSError:
        return False


def _acp_model_id(model: str | None, codex_home: Path) -> str | None:
    """Translate a Codex model slug to codex-acp's ``model[effort]`` ID."""
    model = str(model or "").strip()
    if not model or "[" in model:
        return model or None
    effort = "medium"
    try:
        cache = json.loads((codex_home / "models_cache.json").read_text(encoding="utf-8"))
        for item in cache.get("models") or []:
            if isinstance(item, dict) and item.get("slug") == model:
                effort = str(item.get("default_reasoning_level") or effort)
                break
    except (AttributeError, json.JSONDecodeError, OSError):
        pass
    override = os.environ.get("OPENHANDS_CODEX_REASONING_EFFORT", "").strip()
    return f"{model}[{override or effort}]"


def _max_iterations() -> int:
    try:
        requested = int(os.environ.get("AGENT_MONITOR_OPENHANDS_MAX_ITERATIONS", "40"))
    except ValueError:
        requested = 40
    return max(1, min(requested, 500))


def _reached_iteration_limit(events) -> bool:
    return any(
        getattr(event, "code", None) == "MaxIterationsReached" for event in events
    )


def _usage_payload(usage) -> dict[str, int]:
    fields = (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("cache_read_tokens", "cache_read_tokens"),
        ("cache_write_tokens", "cache_write_tokens"),
        ("reasoning_tokens", "reasoning_tokens"),
    )
    return {
        output_name: int(getattr(usage, source_name, 0) or 0)
        for output_name, source_name in fields
    }


def _cancel_acp_prompt(agent) -> None:
    """Send ACP cancellation through AsyncExecutor using a real async function."""

    async def cancel() -> None:
        await agent._conn.cancel(agent._session_id)

    agent._executor.run_async(cancel)


def _codex_exec_command(
    codex: str, workspace: Path, last_message: Path, model: str | None
) -> list[str]:
    cmd = [
        codex,
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "danger-full-access",
        "--skip-git-repo-check",
        "--output-last-message",
        str(last_message),
        "-C",
        str(workspace),
    ]
    if model:
        cmd.extend(["--model", model])
    return cmd


def _augmented_path() -> str:
    home = Path.home()
    additions = [
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
        home / ".local" / "node24" / "bin",
    ]
    return os.pathsep.join([*(str(p) for p in additions), os.environ.get("PATH", "")])


def _which(name: str) -> str | None:
    return shutil.which(name, path=_augmented_path())


def _ensure_sdk_python() -> None:
    """Re-exec under the OpenHands tool interpreter when needed."""
    try:
        version = importlib.metadata.version("openhands-sdk")
        if version.startswith(SDK_VERSION + ".") or version == SDK_VERSION:
            return
    except importlib.metadata.PackageNotFoundError:
        pass

    candidates: list[Path] = []
    openhands = _which("openhands")
    if openhands:
        launcher = Path(openhands)
        candidates.append(launcher.parent / "python")
        if launcher.is_symlink():
            target = launcher.readlink()
            if not target.is_absolute():
                target = launcher.parent / target
            candidates.append(target.parent / "python")
    candidates.append(
        Path.home() / ".local" / "share" / "uv" / "tools" / "openhands" / "bin" / "python"
    )
    current = Path(sys.executable).absolute()
    for candidate in candidates:
        if candidate.is_file() and candidate.absolute() != current:
            os.execv(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise RuntimeError(
        "OpenHands SDK 1.21 is not importable and its tool interpreter was not found"
    )


def _require_subscription_auth() -> Path:
    if os.environ.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") != "1":
        raise RuntimeError("AGENT_MONITOR_CODEX_SUBSCRIPTION must be exactly 1")
    raw_home = os.environ.get("CODEX_HOME", "").strip()
    if not raw_home:
        raise RuntimeError("CODEX_HOME must point to an existing ChatGPT Codex login")
    codex_home = Path(raw_home).expanduser().resolve()
    if not codex_home.is_dir() or not (codex_home / "auth.json").is_file():
        raise RuntimeError(f"no auth.json found under CODEX_HOME={codex_home}")
    return codex_home


@contextlib.contextmanager
def _codex_home_bridge(codex_home: Path) -> Iterator[Path]:
    """Expose CODEX_HOME as ~/.codex without copying credential material."""
    old_home = os.environ.get("HOME")
    if codex_home.name == ".codex":
        bridge_home = codex_home.parent
        temporary = None
    else:
        temporary = tempfile.TemporaryDirectory(prefix="openhands-codex-home-")
        bridge_home = Path(temporary.name)
        (bridge_home / ".codex").symlink_to(codex_home, target_is_directory=True)
    os.environ["HOME"] = str(bridge_home)
    try:
        yield bridge_home
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if temporary is not None:
            temporary.cleanup()


def _task_prompt(prompt: str, workspace: Path) -> str:
    problem_file = workspace / "problem.txt"
    problem = ""
    if problem_file.is_file():
        try:
            problem = problem_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return (
        "You are solving a mathematics proof task. Work only inside the current "
        f"workspace ({workspace}). Write the complete, rigorous, self-contained "
        "informal proof to proof.md in Markdown, using $...$ and $$...$$ for math. "
        "Do not finish until proof.md exists and is clean enough to be the final "
        "deliverable.\n\n"
        + (f"Problem statement from problem.txt:\n{problem}\n\n" if problem else "")
        + f"Task instructions:\n{prompt}"
    )


def _run_acp(prompt: str, workspace: Path, codex_home: Path) -> int:
    from openhands.sdk import ACPAgentSettings, Conversation
    from openhands.sdk.conversation import get_agent_final_response
    from openhands.sdk.event import ACPToolCallEvent, AgentErrorEvent, MessageEvent
    from openhands.sdk.llm import content_to_str

    npx = _which("npx")
    if not npx:
        raise RuntimeError("npx is required to launch the Codex ACP backend")

    selected_model = (
        os.environ.get("OPENHANDS_CODEX_MODEL")
        or os.environ.get("AGENT_MONITOR_CODEX_MODEL")
        or None
    )
    model = _acp_model_id(selected_model, codex_home)
    timeout = float(os.environ.get("OPENHANDS_CODEX_TIMEOUT", "1800"))
    seen_tool_states: set[tuple[str, str | None]] = set()
    tool_budget = _ACPToolBudget(_max_iterations())
    agent_holder: dict[str, object] = {}
    cancel_threads: list[threading.Thread] = []

    def cancel_inner_acp_turn() -> None:
        agent = agent_holder.get("agent")
        if agent is None:
            return
        try:
            _cancel_acp_prompt(agent)
        except Exception as exc:  # noqa: BLE001
            emit_item(
                {
                    "type": "error",
                    "message": f"Could not cancel over-limit ACP turn: {exc}"[:4000],
                }
            )

    def on_event(event) -> None:
        if isinstance(event, MessageEvent) and event.source == "agent":
            text = "".join(content_to_str(event.llm_message.content)).strip()
            if text:
                emit_item({"type": "agent_message", "text": text[:12000]})
        elif isinstance(event, ACPToolCallEvent):
            if tool_budget.observe(event):
                emit_item(
                    {
                        "type": "error",
                        "message": (
                            "OpenHands ACP reached the configured activity limit "
                            f"({_max_iterations()} distinct tool calls); cancelling."
                        ),
                    }
                )
                cancel_thread = threading.Thread(
                    target=cancel_inner_acp_turn,
                    name="openhands-acp-budget-cancel",
                    daemon=True,
                )
                cancel_threads.append(cancel_thread)
                cancel_thread.start()
            marker = (event.tool_call_id, event.status)
            if marker in seen_tool_states:
                return
            seen_tool_states.add(marker)
            details = event.raw_input
            if isinstance(details, (dict, list)):
                details = json.dumps(details, ensure_ascii=False)
            command = event.title + (f" · {details}" if details else "")
            emit_item(
                {
                    "type": "command_execution",
                    "command": command[:4000],
                    "status": event.status,
                }
            )
        elif isinstance(event, AgentErrorEvent):
            emit_item({"type": "error", "message": event.error[:4000]})

    original_home = Path.home()
    npm_cache = original_home / ".npm"
    subprocess_path = _augmented_path()
    with _codex_home_bridge(codex_home) as bridge_home:
        # SDK 1.21 recognizes the historical "chatgpt" method id, while
        # codex-acp 1.3 reports "chat-gpt". Bridge that upstream version skew
        # without exposing or parsing the credential file.
        import openhands.sdk.agent.acp_agent as acp_agent_module

        original_auth_selector = acp_agent_module._select_auth_method

        def select_auth_method(methods, env):
            if (
                "chat-gpt" in {method.id for method in methods}
                and (Path.home() / ".codex" / "auth.json").is_file()
            ):
                return "chat-gpt"
            return original_auth_selector(methods, env)

        acp_agent_module._select_auth_method = select_auth_method
        settings = ACPAgentSettings(
            acp_server="codex",
            acp_command=[npx, "-y", ACP_PACKAGE],
            acp_env={
                "CODEX_HOME": str(codex_home),
                "HOME": str(bridge_home),
                "PATH": subprocess_path,
                "npm_config_cache": str(npm_cache),
            },
            acp_model=model,
            # codex-acp 1.3 renamed the full-access mode id; OpenHands SDK
            # 1.21 still defaults to the older "full-access" identifier.
            acp_session_mode="agent-full-access",
            acp_prompt_timeout=timeout,
        )
        agent = settings.create_agent()
        agent_holder["agent"] = agent
        conversation = Conversation(
            agent=agent,
            workspace=workspace,
            callbacks=[on_event],
            visualizer=None,
            max_iteration_per_run=_max_iterations(),
            stuck_detection=False,
            delete_on_close=True,
        )
        try:
            conversation.send_message(_task_prompt(prompt, workspace))
            conversation.run()
            events = list(conversation.state.events)
            limit_reached = _reached_iteration_limit(events)
            final_text = get_agent_final_response(events).strip()
            usage = agent.llm.metrics.accumulated_token_usage
        finally:
            for cancel_thread in cancel_threads:
                cancel_thread.join(timeout=5)
            conversation.close()
            acp_agent_module._select_auth_method = original_auth_selector

    if limit_reached or tool_budget.exceeded:
        raise OpenHandsIterationLimit(
            f"Agent reached maximum iterations/activity limit ({_max_iterations()})."
        )

    proof = workspace / "proof.md"
    if not _valid_proof_file(proof) and _is_proof_response(final_text):
        proof.write_text(final_text, encoding="utf-8")
    if not _valid_proof_file(proof):
        try:
            proof.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("Codex ACP completed without producing proof.md")

    emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})
    emit(
        {
            "type": "turn.completed",
            "usage": _usage_payload(usage),
        }
    )
    return 0


def _run_codex_exec(prompt: str, workspace: Path, codex_home: Path) -> int:
    codex = _which("codex")
    if not codex:
        raise RuntimeError("Codex ACP failed and codex CLI fallback was not found")

    emit_item(
        {
            "type": "agent_message",
            "text": (
                "OpenHands ACP could not initialize; falling back to codex exec. "
                "This preserves CODEX_HOME subscription auth, but Codex owns the "
                "agent loop and OpenHands tools/condenser semantics do not apply."
            ),
        }
    )
    model = os.environ.get("OPENHANDS_CODEX_MODEL") or os.environ.get(
        "AGENT_MONITOR_CODEX_MODEL"
    )
    with tempfile.NamedTemporaryFile(
        prefix="agent-monitor-openhands-last-", suffix=".txt", delete=False
    ) as handle:
        last_message = Path(handle.name)
    cmd = _codex_exec_command(codex, workspace, last_message, model)
    cmd.append(_task_prompt(prompt, workspace))

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
    )
    env = {key: os.environ[key] for key in allowed_env if key in os.environ}
    env["CODEX_HOME"] = str(codex_home)
    env["PATH"] = _augmented_path()
    timeout = float(os.environ.get("OPENHANDS_CODEX_TIMEOUT", "1800"))
    try:
        completed = subprocess.run(
            cmd,
            cwd=workspace,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        last_message.unlink(missing_ok=True)
        raise RuntimeError(f"codex exec timed out after {timeout:g}s") from exc
    except OSError:
        last_message.unlink(missing_ok=True)
        raise

    last_message.unlink(missing_ok=True)
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                emit_item({"type": "reasoning", "text": line[:4000]})
        else:
            if isinstance(event, dict):
                emit(event)
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        suffix = detail[-1][:1000] if detail else "no diagnostic output"
        raise RuntimeError(f"codex exec exited {completed.returncode}: {suffix}")
    if not _valid_proof_file(workspace / "proof.md"):
        raise RuntimeError("codex exec completed without producing proof.md")
    emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})
    return 0


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt.strip():
        emit_item({"type": "error", "message": "empty prompt"})
        return 2

    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    if os.environ.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") != "1":
        binary = _which("openhands")
        if not binary:
            emit_item({"type": "error", "message": "openhands executable not found"})
            return 127
        os.execv(
            binary,
            [
                binary,
                "--headless",
                "--json",
                "--always-approve",
                "--exit-without-confirmation",
                "--override-with-envs",
                "-t",
                prompt,
            ],
        )
    try:
        _ensure_sdk_python()
        codex_home = _require_subscription_auth()
        workspace = Path.cwd().resolve()
        emit_item(
            {
                "type": "agent_message",
                "text": (
                    f"OpenHands SDK {importlib.metadata.version('openhands-sdk')} · "
                    "Codex ACP · existing CODEX_HOME (credentials are not copied)"
                ),
            }
        )
        try:
            return _run_acp(prompt, workspace, codex_home)
        except OpenHandsIterationLimit as limit_error:
            emit_item({"type": "error", "message": str(limit_error)})
            return 1
        except Exception as acp_error:  # noqa: BLE001
            emit_item(
                {
                    "type": "reasoning",
                    "text": f"Codex ACP unavailable: {type(acp_error).__name__}: {acp_error}",
                }
            )
            return _run_codex_exec(prompt, workspace, codex_home)
    except Exception as exc:  # noqa: BLE001
        emit_item({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
