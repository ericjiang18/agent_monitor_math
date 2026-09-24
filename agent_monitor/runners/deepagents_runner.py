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

import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

if __package__:
    from .outcome_contract import (
        normalize_requested_outcome_file,
        strip_outcome_markers,
    )
else:
    # engines_registry executes this file with the DeepAgents interpreter and
    # a run workspace as cwd. Make the owning repository importable without
    # relying on a service-level PYTHONPATH.
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from agent_monitor.runners.outcome_contract import (
        normalize_requested_outcome_file,
        strip_outcome_markers,
    )


_ACTIVE_CODEX_GROUPS: set[int] = set()
_AUDIT_REQUIRED_FILES = {
    "global.json",
    "decomposed.json",
    "verifier-output.json",
    "merge-map.json",
    "run.json",
}
_JSON_ARGUMENT_TOOLS = {
    "audit_output_validator",
    "bounded_counterexample_search",
    "exact_math_certificate",
    "statement_fidelity_audit",
}
_OUTCOME_RE = re.compile(
    r"(?m)^ProvingConsole outcome: "
    r"(Solved|Counterexample|Known/Open Status|Partial Progress)\s*$"
)
_WHOLE_JSON_FENCE_RE = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE,
)
_DEGRADED_AUDIT_RE = re.compile(
    r"(?im)(?:^|[.!?]\s+|\\(?:emph|textbf|textit)\{|[*_`]{1,3})"
    r"(?:this\s+is\s+(?:an?\s+)?)?"
    r"degraded\s+audit\s*(?::|[.\u2014-]|$)"
)
_MIN_FORMAL_LEAN_BYTES = 40


def _subagents_enabled() -> bool:
    return os.environ.get("AGENT_MONITOR_USE_SUBAGENTS", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


def _is_kimi_api_model(model: str | None) -> bool:
    normalized = str(model or "").strip().lower()
    for prefix in ("openai:", "openai/", "kimi:", "kimi/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized == "kimi-k3"


def _kimi_max_completion_tokens() -> int:
    raw = (
        os.environ.get("AGENT_MONITOR_DEEPAGENTS_MAX_TOKENS")
        or os.environ.get("AGENT_MONITOR_API_MAX_TOKENS")
        or "32768"
    )
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        requested = 32768
    return max(1024, min(requested, 128000))


def _create_kimi_api_model(model: str):
    """Build an explicit Chat Completions client for the Kimi gateway."""
    from langchain_openai import ChatOpenAI

    from agent_monitor import kimi_k3

    key = str(os.environ.get(kimi_k3.KEY_VAR) or "").strip()
    if not key:
        raise RuntimeError(f"{kimi_k3.KEY_VAR} is required for {kimi_k3.MODEL_ID}")
    base = str(
        os.environ.get(kimi_k3.BASE_VAR) or kimi_k3.DEFAULT_BASE_URL
    ).strip().rstrip("/")
    return ChatOpenAI(
        model=kimi_k3.MODEL_ID,
        api_key=key,
        base_url=base,
        reasoning_effort=kimi_k3.reasoning_effort(env=os.environ),
        max_completion_tokens=_kimi_max_completion_tokens(),
        use_responses_api=False,
        streaming=False,
        timeout=240,
        max_retries=2,
    )


def _proof_artifact_ready(workspace: Path) -> bool:
    for name in ("proof.md", "proof.tex"):
        candidate = workspace / name
        try:
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and candidate.stat().st_size >= 40
            ):
                return True
        except OSError:
            continue
    return False


def _formal_lean_mode() -> bool:
    return (
        os.environ.get("AGENT_MONITOR_FORMAL_LEAN_MODE") == "1"
        or os.environ.get("AGENT_MONITOR_CLAUDE_LEAN_MODE") == "1"
    )


def _compiled_lean_artifact_ready(
    workspace: Path,
    *,
    initial_sha256: str | None,
    checked_sha256: str | None,
    check_exit_code: int | None,
) -> bool:
    """Accept only a fresh substantive Proof.lean matching a successful check."""
    candidate = workspace / "Proof.lean"
    try:
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.resolve().parent != workspace.resolve()
            or not _MIN_FORMAL_LEAN_BYTES <= candidate.stat().st_size <= 2_000_000
        ):
            return False
        current_sha256 = _file_sha256(candidate)
        if (
            current_sha256 is None
            or current_sha256 == initial_sha256
            or current_sha256 != checked_sha256
            or check_exit_code != 0
        ):
            return False
        source = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not re.search(r"(?m)^\s*(?:theorem|lemma|example)\b", source):
        return False
    return re.search(r"(?i)\b(?:sorry|admit)\b", source) is None


def _create_lean_check_tool(
    workspace: Path,
    state: dict[str, Any],
):
    """Expose one fixed compiler action without granting an arbitrary host shell."""
    from langchain_core.tools import StructuredTool

    def lean_check() -> str:
        candidate = workspace / "Proof.lean"
        try:
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or candidate.resolve().parent != workspace.resolve()
            ):
                state.clear()
                state.update({"exit_code": 2, "sha256": None})
                return "exit 2\nProof.lean is missing, linked, or outside the workspace"
        except OSError as exc:
            state.clear()
            state.update({"exit_code": 2, "sha256": None})
            return f"exit 2\nProof.lean cannot be inspected: {exc}"

        lake = shutil.which("lake")
        lean = shutil.which("lean")
        if lake and any(
            (workspace / name).is_file() for name in ("lakefile.lean", "lakefile.toml")
        ):
            command = [lake, "env", "lean", "Proof.lean"]
        elif lean:
            command = [lean, "Proof.lean"]
        else:
            state.clear()
            state.update({"exit_code": 127, "sha256": _file_sha256(candidate)})
            return "exit 127\nLean executable is not available"

        try:
            timeout = int(
                os.environ.get("AGENT_MONITOR_DEEPAGENTS_LEAN_TIMEOUT", "120")
            )
        except ValueError:
            timeout = 120
        timeout = max(5, min(timeout, 300))
        allowed_env = {
            name: os.environ[name]
            for name in (
                "PATH",
                "HOME",
                "LANG",
                "LANGUAGE",
                "LC_ALL",
                "LC_CTYPE",
                "LEAN_PATH",
                "ELAN_HOME",
            )
            if os.environ.get(name)
        }
        allowed_env["NO_COLOR"] = "1"
        checked_sha256 = _file_sha256(candidate)
        if checked_sha256 is None:
            state.clear()
            state.update({"exit_code": 2, "sha256": None})
            return (
                "exit 2\nProof.lean disappeared or became linked before compilation"
            )
        try:
            completed = subprocess.run(
                command,
                cwd=str(workspace),
                env=allowed_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            exit_code = int(completed.returncode)
        except subprocess.TimeoutExpired as exc:
            output = str(exc)
            exit_code = 124
        except OSError as exc:
            output = str(exc)
            exit_code = 127
        post_check_sha256 = _file_sha256(candidate)
        if checked_sha256 != post_check_sha256:
            output = (
                output.rstrip()
                + "\nProof.lean changed during compilation; result rejected"
            )
            exit_code = 3
            checked_sha256 = None
        else:
            checked_sha256 = post_check_sha256
        state.clear()
        state.update(
            {
                "exit_code": exit_code,
                "sha256": checked_sha256,
                "command": [Path(part).name for part in command],
            }
        )
        return f"exit {exit_code}\n{output[-12000:]}".rstrip()

    return StructuredTool.from_function(
        func=lean_check,
        name="lean_check",
        description=(
            "Compile the current /Proof.lean with the workspace's fixed Lean "
            "toolchain. This tool accepts no arguments. Revise Proof.lean and call "
            "lean_check again until it returns exit 0."
        ),
    )


def _recursion_limit() -> int:
    """Return the bounded LangGraph step budget selected by ProvingConsole."""
    raw = os.environ.get("AGENT_MONITOR_DEEPAGENTS_RECURSION_LIMIT", "80")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 80
    return max(20, min(value, 800))


def _library_tool_argv(
    property_names: list[str],
    kwargs: dict[str, Any],
    *,
    json_arguments: bool,
) -> list[str]:
    """Encode one typed Library invocation for its materialized shell tool."""
    if property_names == ["args"]:
        return shlex.split(str(kwargs.get("args") or ""))
    if json_arguments:
        payload = {
            name: kwargs[name]
            for name in property_names
            if kwargs.get(name) is not None
        }
        return [json.dumps(payload, ensure_ascii=False)]
    return [
        str(kwargs[name])
        for name in property_names
        if kwargs.get(name) is not None
    ]


def _schema_python_type(spec: dict[str, Any]) -> Any:
    """Preserve useful JSON Schema container types in dynamic tool models."""
    kind = str(spec.get("type") or "")
    if kind == "array":
        item = spec.get("items")
        item_type = _schema_python_type(item) if isinstance(item, dict) else Any
        return list[item_type]
    if kind == "object":
        value = spec.get("additionalProperties")
        value_type = _schema_python_type(value) if isinstance(value, dict) else Any
        return dict[str, value_type]
    return {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }.get(kind, Any)


def _file_sha256(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _normalize_changed_proof_outcome(
    workspace: Path,
    *,
    prompt: str,
    initial_proof_sha256: str | None,
) -> bool:
    """Normalize only a proof created or changed by this DeepAgents run."""
    proof = workspace / "proof.md"
    current = _file_sha256(proof)
    if current is None or current == initial_proof_sha256:
        return False
    return normalize_requested_outcome_file(proof, prompt)


def _recoverable_recursion_checkpoint(
    workspace: Path,
    *,
    prompt: str,
    initial_proof_sha256: str | None,
    final_text: str,
) -> bool:
    """Accept only a new, validator-backed explicit-audit final checkpoint."""
    # Artifact state, not a model's incidental closing vocabulary, determines
    # whether the checkpoint is final enough to preserve. ``final_text`` stays
    # in the interface for trace compatibility with the stream loop.
    _ = final_text
    if "CANDIDATE AUDIT GATE:" not in prompt:
        return False
    proof = workspace / "proof.md"
    current_sha = _file_sha256(proof)
    if current_sha is None or current_sha == initial_proof_sha256:
        return False
    try:
        proof_text = proof.read_text(encoding="utf-8")
    except OSError:
        return False
    if len(proof_text) < 400 or len(_OUTCOME_RE.findall(proof_text)) != 1:
        return False
    validator = workspace / "_library" / "tools" / "audit-output-validator.sh"
    if not validator.is_file() or validator.is_symlink():
        return False
    audit_root = workspace / "audit"
    if not audit_root.is_dir():
        return False
    for directory in audit_root.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        if not all(
            (directory / name).is_file()
            and not (directory / name).is_symlink()
            and (directory / name).stat().st_size > 0
            for name in _AUDIT_REQUIRED_FILES
        ):
            continue
        valid = True
        for name in ("global.json", "decomposed.json", "verifier-output.json"):
            try:
                checked = subprocess.run(
                    ["bash", str(validator), f"@{name}"],
                    cwd=str(directory),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                response = json.loads(checked.stdout or "{}")
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                valid = False
                break
            if checked.returncode != 0 or response.get("valid") is not True:
                valid = False
                break
        if valid:
            from agent_monitor.research_audit import reconciliation_complete

            if not reconciliation_complete(workspace, directory):
                continue
            return True
    return False


def _recoverable_degraded_checkpoint(
    workspace: Path,
    *,
    prompt: str,
    initial_proof_sha256: str | None,
) -> bool:
    """Accept an honest, current-run degraded audit only with a concrete reason."""
    if "CANDIDATE AUDIT GATE:" not in prompt:
        return False
    proof = workspace / "proof.md"
    current_sha = _file_sha256(proof)
    if current_sha is None or current_sha == initial_proof_sha256:
        return False
    try:
        proof_text = proof.read_text(encoding="utf-8")
    except OSError:
        return False
    if len(proof_text) < 400 or len(_OUTCOME_RE.findall(proof_text)) != 1:
        return False
    match = _DEGRADED_AUDIT_RE.search(proof_text)
    if match is None:
        return False
    reason = proof_text[match.end() : match.end() + 500].lower()
    return any(
        token in reason
        for token in (
            "missing",
            "invalid",
            "unavailable",
            "unsupported",
            "timed out",
            "timeout",
            "failed",
            "failure",
        )
    )


def _persist_tool_serialization_checkpoint(
    workspace: Path,
    *,
    prompt: str,
    initial_proof_sha256: str | None,
    error: str,
) -> bool:
    """Fail closed after repeated model-side tool argument serialization errors.

    A substantial changed proof remains useful even when the model fails to
    serialize its next LangChain call. Preserve that artifact only as explicit
    Partial Progress and name the incomplete file-backed audit contract.
    """
    if (
        "CANDIDATE AUDIT GATE:" not in prompt
        or "invalid LangChain tool response" not in error
    ):
        return False
    proof = workspace / "proof.md"
    current_sha = _file_sha256(proof)
    if current_sha is None or current_sha == initial_proof_sha256:
        return False
    try:
        text = proof.read_text(encoding="utf-8")
    except OSError:
        return False
    if len(text) < 400:
        return False

    best_directory: Path | None = None
    best_present: set[str] = set()
    audit_root = workspace / "audit"
    if audit_root.is_dir() and not audit_root.is_symlink():
        for directory in audit_root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            present = {
                name
                for name in _AUDIT_REQUIRED_FILES
                if (directory / name).is_file()
                and not (directory / name).is_symlink()
                and (directory / name).stat().st_size > 0
            }
            if len(present) > len(best_present):
                best_directory, best_present = directory, present
    missing = sorted(_AUDIT_REQUIRED_FILES - best_present)
    location = (
        str(best_directory.relative_to(workspace))
        if best_directory is not None
        else "audit/<run-id>"
    )
    gap = (
        "missing or unvalidated artifacts: " + ", ".join(missing)
        if missing
        else "one or more existing artifacts remain unvalidated"
    )
    cleaned = strip_outcome_markers(text)
    disclosure = (
        "## Audit status\n\n"
        "degraded audit: DeepAgents stopped after its model repeated an invalid "
        "LangChain tool-argument JSON response. The current mathematical checkpoint "
        f"was preserved, but the contract under `{location}` is incomplete; {gap}. "
        "No audit-completion or solved claim is made."
    )
    try:
        proof.write_text(
            cleaned
            + "\n\n"
            + disclosure
            + "\n\nProvingConsole outcome: Partial Progress\n",
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def _deepagents_api_audit_call(
    system: str, user: str, *, model: str, emit_event=None
):
    from agent_monitor.runners.api_backend import api_chat

    result = api_chat(system, user, model=model)
    callback = emit_event or (lambda _event: None)
    callback({"type": "turn.completed", "usage": result.usage})
    return result


def _deepagents_claude_audit_call(
    system: str, user: str, *, model: str, emit_event=None
):
    """Run one trusted audit pass through the linked Claude Code account."""
    from agent_monitor.runners.claude_backend import (
        ClaudeBackendError,
        claude_exec,
        forward_as_codex_event,
    )
    from agent_monitor.runners.codex_backend import CodexBackendError

    callback = emit_event or (lambda _event: None)
    try:
        return claude_exec(
            system,
            user,
            model=model,
            emit_event=lambda event: forward_as_codex_event(event, callback),
            workspace=Path.cwd(),
            enable_tools=False,
            source_env=os.environ,
        )
    except ClaudeBackendError as exc:
        # The shared audit runner has a Codex-shaped callable contract and
        # deliberately catches CodexBackendError around every verifier pass.
        raise CodexBackendError(str(exc)) from exc


def _run_supplemental_audit(
    workspace: Path,
    *,
    prompt: str,
    model: str,
    subscription_mode: bool,
    initial_proof_sha256: str | None,
    final_text: str,
    claude_subscription_mode: bool = False,
) -> bool:
    """Finish an explicit audit with trusted response-orchestrated passes.

    Native DeepAgents work remains the candidate and its artifacts remain
    untouched. This fallback runs only when the native contract did not already
    validate, and writes a separate audit directory through the shared runner.
    """
    if "CANDIDATE AUDIT GATE:" not in prompt:
        return False
    if _recoverable_recursion_checkpoint(
        workspace,
        prompt=prompt,
        initial_proof_sha256=initial_proof_sha256,
        final_text=final_text,
    ):
        return False
    candidate_path: Path | None = None
    for name in ("proof.md", "proof.tex"):
        raw = workspace / name
        try:
            if (
                raw.is_file()
                and not raw.is_symlink()
                and raw.resolve().parent == workspace.resolve()
                and 40 <= raw.stat().st_size <= 10_000_000
            ):
                candidate_path = raw
                break
        except OSError:
            continue
    if candidate_path is None:
        return False
    candidate = candidate_path.read_text(encoding="utf-8", errors="replace")
    from agent_monitor.runners.api_backend import APIBackendError
    from agent_monitor.runners.codex_backend import CodexBackendError
    from agent_monitor.runners.deepseek_harness_runner import (
        ensure_requested_outcome,
        run_codex_audit,
    )

    emit_item(
        {
            "type": "agent_message",
            "text": (
                "DeepAgents · trusted supplemental audit · global, decomposed, "
                "refuter, merge, adjudication"
            ),
        }
    )
    audit_model = (
        model.removeprefix("openai:") if subscription_mode else model
    )
    call = (
        None
        if subscription_mode
        else _deepagents_claude_audit_call
        if claude_subscription_mode
        else _deepagents_api_audit_call
    )
    try:
        final = run_codex_audit(
            prompt=prompt,
            candidate=candidate,
            model=audit_model,
            call=call,
            adapter="deepagents-response-orchestrated-audit",
            runtime_label="DeepAgents",
            audit_prefix="deepagents",
        )
        final = ensure_requested_outcome(prompt, final)
        target = workspace / "proof.md"
        if target.is_symlink():
            raise OSError("refusing to overwrite linked proof.md")
        target.write_text(final.rstrip() + "\n", encoding="utf-8")
    except (APIBackendError, CodexBackendError, OSError, ValueError) as exc:
        cleaned = strip_outcome_markers(candidate)
        degraded = (
            cleaned
            + "\n\n## Audit status\n\n"
            + "degraded audit: DeepAgents' trusted supplemental structured audit "
            + f"failed ({type(exc).__name__}: {exc}).\n\n"
            + "ProvingConsole outcome: Partial Progress\n"
        )
        target = workspace / "proof.md"
        if not target.is_symlink():
            target.write_text(degraded, encoding="utf-8")
        emit_item({"type": "error", "message": str(exc)[-1600:]})
    return True


def _codex_timeout_response(
    timeout_seconds: int, partial_output: str | bytes | None
) -> tuple[dict[str, Any], str, str]:
    """Return a non-affirmative model message so a parent agent can recover."""
    if isinstance(partial_output, bytes):
        usage_stream = partial_output.decode("utf-8", errors="replace")
    else:
        usage_stream = str(partial_output or "")
    response = {
        "kind": "text",
        "text": (
            f"MODEL CALL TIMEOUT: this model call exceeded {timeout_seconds}s and "
            "was terminated. Treat its review as incomplete. If this came from a "
            "delegated task, retry with a smaller scope or explicitly record a "
            "degraded audit and every missing artifact; never infer acceptance."
        ),
        "tool_calls": [],
    }
    return response, usage_stream, json.dumps(response, ensure_ascii=False)


def _kill_codex_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _run_codex_exec(
    command: list[str], *, request: str, env: dict[str, str], timeout_seconds: int
):
    """Run Codex in a killable group so wrapper timeouts leave no orphan."""
    import subprocess

    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    _ACTIVE_CODEX_GROUPS.add(proc.pid)
    try:
        try:
            stdout, stderr = proc.communicate(input=request, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _kill_codex_group(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            raise subprocess.TimeoutExpired(
                command,
                timeout_seconds,
                output=stdout or getattr(exc, "stdout", None),
                stderr=stderr or getattr(exc, "stderr", None),
            ) from exc
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    finally:
        # A wrapper can exit while its native Codex child still holds the JSONL
        # pipe. Kill any survivor in this dedicated group on every path.
        _kill_codex_group(proc.pid)
        _ACTIVE_CODEX_GROUPS.discard(proc.pid)


def _install_termination_handlers() -> None:
    def terminate(signum, _frame):
        for pgid in tuple(_ACTIVE_CODEX_GROUPS):
            _kill_codex_group(pgid)
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, terminate)


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


def _codex_usage_from_jsonl(text: str) -> dict[str, int]:
    """Accumulate Codex ``turn.completed`` usage for one chat-model call."""
    totals = {"input_tokens": 0, "output_tokens": 0}
    for line in str(text or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage") or {}
        totals["input_tokens"] += int(usage.get("input_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or 0)
    if not any(totals.values()):
        return {}
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return totals


def _tool_call_issues(response: dict[str, Any], tools: list[dict[str, Any]]) -> list[str]:
    """Return actionable schema errors for a structured Codex tool response."""
    if response.get("kind") != "tool_calls":
        return []
    schemas = {
        str(tool.get("function", {}).get("name") or ""): (
            tool.get("function", {}).get("parameters") or {}
        )
        for tool in tools
        if isinstance(tool, dict)
    }
    calls = response.get("tool_calls") or []
    if not isinstance(calls, list) or not calls:
        return ["tool_calls must contain at least one call"]
    issues: list[str] = []
    for index, call in enumerate(calls, 1):
        if not isinstance(call, dict):
            issues.append(f"call {index} is not an object")
            continue
        name = str(call.get("name") or "")
        if name not in schemas:
            issues.append(f"call {index} uses unknown tool {name!r}")
            continue
        raw_arguments = call.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            try:
                arguments = json.loads(raw_arguments or "{}")
            except (TypeError, json.JSONDecodeError):
                issues.append(f"call {index} ({name}) arguments are not valid JSON")
                continue
        if not isinstance(arguments, dict):
            issues.append(f"call {index} ({name}) arguments must encode an object")
            continue
        required = schemas[name].get("required") or []
        missing = [str(field) for field in required if field not in arguments]
        if missing:
            issues.append(
                f"call {index} ({name}) is missing required argument(s): "
                + ", ".join(missing)
            )
    return issues


def _text_only_tool_recovery_request(
    request: str, issues: list[str], raw: str
) -> str:
    """Make one final non-tool turn after two malformed typed tool responses."""
    return (
        request
        + "\n\nFINAL TOOL-SERIALIZATION RECOVERY:\n"
        + "Two typed tool responses failed validation:\n- "
        + "\n- ".join(issues)
        + "\nDo not call any tool in this recovery turn. Return kind='text', "
        + "put the complete self-contained answer requested by the conversation "
        + "in text, and return exactly an empty tool_calls array. The trusted "
        + "runner will persist this final response. Do not discuss this recovery "
        + "instruction or filesystem permissions.\n"
        + f"Last invalid response:\n{raw[:4000]}"
    )


def _text_only_response_schema() -> dict[str, Any]:
    """Return a schema that makes another malformed tool call impossible.

    The ordinary response schema deliberately permits either text or typed
    LangChain calls. Reusing it for the final recovery turn only *asked* the
    model not to call a tool, so a model that kept choosing ``write_file``
    could fail the run before creating its first checkpoint. This reduced
    schema has no tool-call field; the runner adds the canonical empty list
    after validation.
    """
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }


def _structured_tool_call_item_schema(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Use a strict-safe JSON string envelope for arbitrary tool schemas.

    Codex structured outputs reject unconstrained nested objects and arrays,
    while DeepAgents legitimately exposes both (for example validator
    documents and variable maps).  The model still sees the full schemas in
    the prompt; the response schema constrains the selected name and requires
    one JSON-encoded arguments object, which is parsed and checked below.
    """
    variants: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        name = str(function.get("name") or "")
        if not name:
            continue
        variants.append(
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": [name]},
                    "arguments": {
                        "type": "string",
                        "description": (
                            "JSON-encoded arguments object matching this tool's "
                            "parameter schema."
                        ),
                    },
                },
                "required": ["name", "arguments"],
                "additionalProperties": False,
            }
        )
    if variants:
        return {"anyOf": variants}
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "arguments": {"type": "string"},
        },
        "required": ["name", "arguments"],
        "additionalProperties": False,
    }


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

        def _get_ls_params(self, stop=None, **kwargs: Any) -> dict[str, Any]:
            # Let DeepAgents resolve provider profiles for this custom
            # subscription adapter just as it does for an OpenAI model string.
            return {
                "ls_provider": "openai",
                "ls_model_name": self.model_name.removeprefix("openai:"),
                "ls_model_type": "chat",
            }

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
                        "items": _structured_tool_call_item_schema(tools),
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
                "each exact tool name and a JSON-encoded arguments object string in "
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
                "AGENT_MONITOR_RUN_REASONING_EFFORT",
                "AGENT_MONITOR_RUN_SPEED_MODE",
            )
            env = {key: os.environ[key] for key in allowed_env if key in os.environ}
            env["CODEX_HOME"] = self.codex_home
            env["NO_COLOR"] = "1"

            def invoke(
                request_text: str,
                *,
                response_schema: dict[str, Any] | None = None,
            ) -> tuple[dict[str, Any], str, str]:
                with tempfile.TemporaryDirectory(prefix="deepagents-codex-") as temp_dir:
                    schema_path = Path(temp_dir) / "response-schema.json"
                    output_path = Path(temp_dir) / "last-message.json"
                    schema_path.write_text(
                        json.dumps(response_schema or schema, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    command = [
                        self.codex_binary,
                        "exec",
                        "--json",
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
                    from agent_monitor.run_tuning import codex_config_args

                    command.extend(codex_config_args(env, cli_model))
                    if cli_model:
                        command.extend(["--model", cli_model])
                    command.append("-")
                    try:
                        proc = _run_codex_exec(
                            command,
                            request=request_text,
                            env=env,
                            timeout_seconds=self.timeout_seconds,
                        )
                    except subprocess.TimeoutExpired as exc:
                        return _codex_timeout_response(
                            self.timeout_seconds,
                            getattr(exc, "output", None),
                        )
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
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"codex exec returned invalid structured output: {raw[:1000]}"
                    ) from exc
                return parsed, proc.stdout or "", raw

            response, usage_stream, raw = invoke(request)
            usage_streams = [usage_stream]
            issues = _tool_call_issues(response, tools)
            if issues:
                correction = (
                    request
                    + "\n\nTOOL RESPONSE VALIDATION ERROR:\n- "
                    + "\n- ".join(issues)
                    + "\nReturn one corrected structured response now. Include every "
                    "required argument from the selected tool schema; never repeat "
                    "an empty JSON object string when fields are required. If you "
                    "cannot supply a valid call, return kind='text' instead.\n"
                    + f"Previous invalid response:\n{raw[:4000]}"
                )
                response, usage_stream, repair_raw = invoke(correction)
                usage_streams.append(usage_stream)
                issues = _tool_call_issues(response, tools)
                if issues:
                    recovery, usage_stream, _ = invoke(
                        _text_only_tool_recovery_request(request, issues, repair_raw),
                        response_schema=_text_only_response_schema(),
                    )
                    usage_streams.append(usage_stream)
                    response = {
                        "kind": "text",
                        "text": str(recovery.get("text") or ""),
                        "tool_calls": [],
                    }
                    valid_text_recovery = bool(response["text"].strip())
                    if not valid_text_recovery:
                        raise RuntimeError(
                            "codex exec repeated an invalid LangChain tool response: "
                            "strict text-only recovery returned empty text"
                        )

            text = str(response.get("text") or "")
            langchain_calls: list[dict[str, Any]] = []
            if response.get("kind") == "tool_calls":
                for call in response.get("tool_calls") or []:
                    name = str(call.get("name") or "")
                    if name not in tool_names:
                        continue
                    raw_arguments = call.get("arguments")
                    if isinstance(raw_arguments, dict):
                        arguments = dict(raw_arguments)
                    else:
                        try:
                            arguments = json.loads(raw_arguments or "{}")
                        except (TypeError, json.JSONDecodeError):
                            arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {"input": arguments}
                    required = set(
                        next(
                            (
                                tool.get("function", {})
                                .get("parameters", {})
                                .get("required", [])
                                for tool in tools
                                if tool.get("function", {}).get("name") == name
                            ),
                            [],
                        )
                    )
                    arguments = {
                        key: value
                        for key, value in arguments.items()
                        if value is not None or key in required
                    }
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

            message = AIMessage(
                content=text,
                tool_calls=langchain_calls,
                usage_metadata=_codex_usage_from_jsonl("\n".join(usage_streams)) or None,
            )
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


_CLAUDE_DEEPAGENTS_SYSTEM = """You are a response-only chat-model adapter for
DeepAgents. Never use Claude Code tools, shell commands, filesystem access, or
side effects. Return only the exact JSON object requested by the user message,
with no Markdown fence or surrounding commentary. LangChain, not Claude Code,
executes every declared tool call."""


def _json_schema_issues(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    """Validate the useful strict subset of JSON Schema used by LangChain tools."""
    if "anyOf" in schema:
        variants = [item for item in schema["anyOf"] if isinstance(item, dict)]
        if variants and not any(not _json_schema_issues(value, item, path) for item in variants):
            return [f"{path} does not match any allowed schema"]
        return []
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path} is not one of the allowed values"]

    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if kind in valid and not valid[kind]:
        return [f"{path} must be {kind}"]
    issues: list[str] = []
    if kind == "object" and isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for field in required:
            if field not in value:
                issues.append(f"{path} is missing required field {field!r}")
        if schema.get("additionalProperties") is False:
            for field in value:
                if field not in properties:
                    issues.append(f"{path} has unexpected field {field!r}")
        for field, item in value.items():
            child = properties.get(field)
            if isinstance(child, dict):
                issues.extend(_json_schema_issues(item, child, f"{path}.{field}"))
    elif kind == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                issues.extend(_json_schema_issues(item, item_schema, f"{path}[{index}]"))
    return issues


def _parse_claude_deepagents_response(
    raw: str,
    tools: list[dict[str, Any]],
    *,
    text_only: bool = False,
) -> dict[str, Any]:
    """Parse one Claude response and reject every non-contract JSON shape."""
    payload = str(raw or "").strip()
    fenced = _WHOLE_JSON_FENCE_RE.fullmatch(payload)
    if fenced:
        # Haiku occasionally wraps an otherwise exact JSON payload in the
        # conventional `json` fence despite being told not to. Accept only a
        # single fence that covers the entire response; never search for or
        # extract JSON from surrounding prose.
        payload = fenced.group("body").strip()
    try:
        response = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("response is not one exact JSON object") from exc
    if not isinstance(response, dict):
        raise ValueError("response must be a JSON object")

    if text_only:
        if set(response) != {"text"}:
            raise ValueError("text-only recovery must contain exactly the text field")
        if not isinstance(response["text"], str) or not response["text"].strip():
            raise ValueError("text-only recovery text must be non-empty")
        return {"kind": "text", "text": response["text"], "tool_calls": []}

    expected = {"kind", "text", "tool_calls"}
    if set(response) != expected:
        raise ValueError("response must contain exactly kind, text, and tool_calls")
    if response["kind"] not in {"text", "tool_calls"}:
        raise ValueError("kind must be text or tool_calls")
    if not isinstance(response["text"], str):
        raise ValueError("text must be a string")
    if not isinstance(response["tool_calls"], list):
        raise ValueError("tool_calls must be an array")
    if response["kind"] == "text":
        if response["tool_calls"]:
            raise ValueError("text responses must have an empty tool_calls array")
        if not response["text"].strip():
            raise ValueError("text responses must contain non-empty text")
        return response
    if not response["tool_calls"]:
        raise ValueError("tool_calls responses must contain at least one call")

    schemas = {
        str(tool.get("function", {}).get("name") or ""): (
            tool.get("function", {}).get("parameters") or {}
        )
        for tool in tools
        if isinstance(tool, dict)
    }
    issues: list[str] = []
    for index, call in enumerate(response["tool_calls"], 1):
        if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
            issues.append(f"call {index} must contain exactly name and arguments")
            continue
        name = call["name"]
        if not isinstance(name, str) or name not in schemas:
            issues.append(f"call {index} uses unknown tool {name!r}")
            continue
        raw_arguments = call["arguments"]
        if isinstance(raw_arguments, dict):
            # Claude sometimes emits the already-decoded JSON object even
            # though the OpenAI-style wire contract asks for a JSON string.
            # Treat this as a representation difference only: validate the
            # object against the exact declared tool schema below, then
            # canonicalize it back to the string LangChain expects.
            arguments = raw_arguments
            call["arguments"] = json.dumps(
                raw_arguments, ensure_ascii=False, separators=(",", ":")
            )
        elif isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                issues.append(f"call {index} ({name}) arguments are invalid JSON")
                continue
        else:
            issues.append(
                f"call {index} ({name}) arguments must be a JSON string or object"
            )
            continue
        if not isinstance(arguments, dict):
            issues.append(f"call {index} ({name}) arguments must encode an object")
            continue
        issues.extend(
            _json_schema_issues(arguments, schemas[name], f"call {index} ({name}) arguments")
        )
    if issues:
        raise ValueError("; ".join(issues))
    return response


def _claude_deepagents_repair_request(
    request: str,
    error: str,
    raw: str,
    *,
    text_only: bool = False,
    require_tool: bool = False,
) -> str:
    if text_only:
        return (
            request
            + "\n\nFINAL TEXT-ONLY RECOVERY. The two previous responses violated "
            + "the JSON/tool contract. Do not call a tool. Return exactly one JSON "
            + "object with one key, {\"text\": \"complete answer\"}. The text "
            + "must be non-empty. No Markdown fence or other keys.\n"
            + f"Last validation error: {error}\nLast invalid response:\n{raw[:4000]}"
        )
    fallback = (
        "A schema-valid declared tool call is mandatory on this turn; "
        "kind=text is still invalid.\n"
        if require_tool
        else "If you cannot make a valid tool call, return a normal kind=text response instead.\n"
    )
    return (
        request
        + "\n\nJSON CONTRACT REPAIR. Your previous response was invalid. Return "
        + "one corrected JSON object only, with every required tool argument and "
        + "no extra envelope fields. "
        + fallback
        + f"Validation error: {error}\nInvalid response:\n{raw[:4000]}"
    )


def _create_claude_subscription_model(workspace: Path, model_name: str):
    """Build a DeepAgents chat model backed only by the linked Claude account."""
    import uuid

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from agent_monitor.runners.claude_backend import (
        MODEL_ALIASES,
        claude_exec,
        subscription_enabled,
    )

    if not subscription_enabled(os.environ):
        raise RuntimeError(
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION=1 is required for DeepAgents Claude"
        )
    if not str(os.environ.get("CLAUDE_CONFIG_DIR") or "").strip():
        raise RuntimeError("CLAUDE_CONFIG_DIR is required for DeepAgents Claude")

    selected = str(model_name or "").strip().lower()
    for prefix in ("anthropic:", "anthropic/"):
        if selected.startswith(prefix):
            selected = selected[len(prefix) :]
            break
    if selected not in MODEL_ALIASES:
        raise RuntimeError(
            "unsupported Claude account model; expected one of: "
            + ", ".join(MODEL_ALIASES)
        )

    class ClaudeCodeChatModel(BaseChatModel):
        workspace: str
        model_name: str
        timeout_seconds: int = 900

        @property
        def _llm_type(self) -> str:
            return "claude-code-subscription"

        @property
        def _identifying_params(self) -> dict[str, Any]:
            return {"model_name": self.model_name, "auth_route": "claude_subscription"}

        def _get_ls_params(self, stop=None, **kwargs: Any) -> dict[str, Any]:
            return {
                "ls_provider": "anthropic",
                "ls_model_name": self.model_name,
                "ls_model_type": "chat",
            }

        def bind_tools(
            self,
            tools: Sequence[Any],
            *,
            tool_choice: str | dict | bool | None = None,
            **kwargs: Any,
        ):
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
                calls = getattr(message, "tool_calls", None) or []
                if calls:
                    body += "\nTool calls: " + json.dumps(
                        calls, ensure_ascii=False, default=str
                    )
                call_id = getattr(message, "tool_call_id", None)
                if call_id:
                    body = f"tool_call_id={call_id}\n{body}"
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
            tool_required = False
            if _formal_lean_mode():
                last_tool_name = ""
                last_tool_result = ""
                for message in reversed(messages):
                    if type(message).__name__ != "ToolMessage":
                        continue
                    last_tool_name = str(getattr(message, "name", "") or "")
                    last_tool_result = _content_text(
                        getattr(message, "content", "")
                    )
                    break
                tool_required = not (
                    last_tool_name == "lean_check"
                    and re.match(
                        r"^\s*exit\s+0(?:\s|$)", last_tool_result, re.IGNORECASE
                    )
                )
            effective_tool_choice = "required" if tool_required else tool_choice
            formal_contract = (
                "FORMAL LEAN TOOL STATE: A declared tool call is mandatory on "
                "this turn. A narrative claim that a file was written or compiled "
                "is invalid. Return kind=tool_calls. Continue using write_file and "
                "lean_check until an actual lean_check ToolMessage reports exit 0.\n"
                if tool_required
                else ""
            )
            request = (
                "Return the next LangChain assistant message as strict JSON. "
                "The object must contain exactly kind, text, and tool_calls. For a "
                "normal answer use {\"kind\":\"text\",\"text\":\"...\","
                "\"tool_calls\":[]}. For tools use kind=tool_calls and entries "
                "with exactly name and arguments, where arguments is a JSON-encoded "
                "object string matching the declared schema. Never invent a tool.\n"
                + formal_contract
                + f"tool_choice: {json.dumps(effective_tool_choice, default=str)}\n"
                "Available LangChain tools:\n"
                + json.dumps(tools, ensure_ascii=False, default=str)
                + "\n\nConversation:\n"
                + self._render_messages(messages)
            )

            results: list[Any] = []

            def invoke(request_text: str) -> Any:
                result = claude_exec(
                    _CLAUDE_DEEPAGENTS_SYSTEM,
                    request_text,
                    model=self.model_name,
                    timeout=self.timeout_seconds,
                    workspace=Path(self.workspace),
                    enable_tools=False,
                    source_env=os.environ,
                )
                results.append(result)
                return result

            def validate_response(
                raw_response: str, *, text_only: bool = False
            ) -> dict[str, Any]:
                response = _parse_claude_deepagents_response(
                    raw_response, tools, text_only=text_only
                )
                if tool_required and response["kind"] != "tool_calls":
                    raise ValueError(
                        "Formal Lean requires a schema-valid declared tool call "
                        "until lean_check actually reports exit 0"
                    )
                return response

            first = invoke(request)
            raw = first.text
            try:
                response = validate_response(raw)
            except ValueError as first_error:
                repaired = invoke(
                    _claude_deepagents_repair_request(
                        request,
                        str(first_error),
                        raw,
                        require_tool=tool_required,
                    )
                )
                raw = repaired.text
                try:
                    response = validate_response(raw)
                except ValueError as second_error:
                    if tool_required:
                        recovered = invoke(
                            _claude_deepagents_repair_request(
                                request,
                                str(second_error),
                                raw,
                                require_tool=True,
                            )
                        )
                    else:
                        recovered = invoke(
                            _claude_deepagents_repair_request(
                                request, str(second_error), raw, text_only=True
                            )
                        )
                    try:
                        response = validate_response(
                            recovered.text, text_only=not tool_required
                        )
                    except ValueError as final_error:
                        raise RuntimeError(
                            "Claude Code repeated an invalid DeepAgents response: "
                            + str(final_error)
                        ) from final_error

            calls: list[dict[str, Any]] = []
            for call in response["tool_calls"]:
                calls.append(
                    {
                        "name": call["name"],
                        "args": json.loads(call["arguments"]),
                        "id": f"call_{uuid.uuid4().hex}",
                        "type": "tool_call",
                    }
                )

            input_tokens = sum(
                int((result.usage or {}).get("input_tokens") or 0)
                for result in results
            )
            output_tokens = sum(
                int((result.usage or {}).get("output_tokens") or 0)
                for result in results
            )
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
            actual_models = [
                str(result.model)
                for result in results
                if str(result.model or "").strip()
            ]
            actual_model = actual_models[-1] if actual_models else self.model_name
            metadata = {
                "provider": "anthropic",
                "auth_route": "claude_subscription",
                "requested_model": self.model_name,
                "model_name": actual_model,
                "attempts": len(results),
            }
            message = AIMessage(
                content=response["text"],
                tool_calls=calls,
                usage_metadata=usage,
                response_metadata=metadata,
            )
            return ChatResult(
                generations=[ChatGeneration(message=message)],
                llm_output=metadata,
            )

    timeout_raw = os.environ.get("AGENT_MONITOR_CLAUDE_TIMEOUT", "900")
    try:
        timeout_seconds = max(1, int(float(timeout_raw)))
    except (TypeError, ValueError):
        timeout_seconds = 900
    return ClaudeCodeChatModel(
        workspace=str(workspace),
        model_name=selected,
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
    if _formal_lean_mode():
        return (
            "FORMAL LEAN MODE. Your file tools see a virtual root / bound to "
            "this isolated Lean workspace.\n"
            "- Read /TASK.md for the exact problem, informal proof, and current source.\n"
            "- Create or revise /Proof.lean; do not create proof.md or proof.tex.\n"
            "- Even if the current source already compiles, write a fresh changed "
            "version (for example, revise its proof or explanatory doc comment); "
            "checking an unchanged file is insufficient for this run.\n"
            "- Do not use sorry or admit, and do not change the target statement.\n"
            "- After every revision call the argument-free lean_check tool. "
            "Finish only after its latest result is exit 0.\n"
            "- The run is rejected if Proof.lean is stale, changed after the last "
            "successful check, linked, or not substantive.\n\n"
            + cleaned.strip()
            + "\n"
        )


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
    """Register auto-approved typed library tools as callable tools."""
    manifest = workspace / "_library" / "tools.json"
    if not manifest.exists():
        return []
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    import subprocess

    from langchain_core.tools import StructuredTool
    from pydantic import Field, create_model

    def args_model(name: str, schema: dict[str, Any]):
        required = set(schema.get("required") or [])
        fields: dict[str, tuple[Any, Any]] = {}
        for field_name, spec in (schema.get("properties") or {}).items():
            if not isinstance(spec, dict):
                continue
            python_type = _schema_python_type(spec)
            default = ... if field_name in required else spec.get("default", None)
            fields[str(field_name)] = (
                python_type,
                Field(default=default, description=str(spec.get("description") or "")),
            )
        return create_model(f"UserTool_{name}_Input", **fields)

    def runner(
        script: Path,
        property_names: list[str],
        timeout: int,
        *,
        json_arguments: bool,
    ):
        def _run(**kwargs: Any) -> str:
            try:
                argv = _library_tool_argv(
                    property_names,
                    kwargs,
                    json_arguments=json_arguments,
                )
                proc = subprocess.run(
                    ["bash", str(script), *argv],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(workspace),
                )
                out = (proc.stdout or "") + (("\n[stderr] " + proc.stderr) if proc.stderr else "")
                return out.strip()[:6000] or f"(exit {proc.returncode}, no output)"
            except Exception as exc:  # noqa: BLE001
                return f"tool error: {exc}"

        return _run

    tools = []
    for entry in entries:
        if entry.get("approval_mode") != "auto" or not entry.get("read_only"):
            continue
        script = workspace / str(entry.get("script") or "")
        if not script.exists():
            continue
        tname = re.sub(r"\W+", "_", str(entry.get("name") or script.stem)).strip("_") or script.stem
        schema = entry.get("input_schema") if isinstance(entry.get("input_schema"), dict) else {}
        property_names = [str(name) for name in (schema.get("properties") or {})]
        try:
            timeout = max(1, min(300, int(entry.get("timeout_seconds") or 120)))
        except (TypeError, ValueError):
            timeout = 120
        tools.append(
            StructuredTool.from_function(
                func=runner(
                    script,
                    property_names,
                    timeout,
                    json_arguments=tname in _JSON_ARGUMENT_TOOLS,
                ),
                args_schema=args_model(tname, schema),
                name=f"user_tool_{tname}"[:60],
                description=(str(entry.get("description") or "") or f"User library tool {tname}")
                + " (auto-approved, read-only; arguments follow the declared schema)",
            )
        )
    return tools


def main() -> int:
    _install_termination_handlers()
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt.strip():
        emit_item({"type": "error", "message": "empty prompt"})
        return 2

    workspace = Path.cwd().resolve()
    formal_lean_mode = _formal_lean_mode()
    lean_check_state: dict[str, Any] = {}
    initial_lean_sha256 = _file_sha256(workspace / "Proof.lean")

    subscription_mode = os.environ.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"
    claude_subscription_mode = (
        os.environ.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1"
    )
    if subscription_mode and claude_subscription_mode:
        emit_item(
            {
                "type": "error",
                "message": "Codex and Claude account routes cannot both be active",
            }
        )
        return 2
    if subscription_mode:
        model = "openai:" + os.environ.get(
            "AGENT_MONITOR_CODEX_MODEL", "gpt-5.6-sol"
        )
    elif claude_subscription_mode:
        model = os.environ.get("AGENT_MONITOR_CLAUDE_MODEL", "sonnet")
    else:
        model = os.environ.get("DEEPAGENTS_MODEL") or (
            "openai:" + os.environ.get("AGENT_MONITOR_OPENAI_MODEL", "gpt-5.2")
        )

    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    allow_subagents = _subagents_enabled()
    if not allow_subagents:
        # DeepAgents auto-adds a general-purpose subagent even when callers pass
        # `subagents=[]`. Its supported harness profile is the only way to
        # remove the `task` tool completely.
        from deepagents import (
            GeneralPurposeSubagentProfile,
            HarnessProfile,
            register_harness_profile,
        )

        provider = (
            "openai"
            if subscription_mode
            else "anthropic"
            if claude_subscription_mode
            else str(model).split(":", 1)[0]
        )
        register_harness_profile(
            provider,
            HarnessProfile(
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)
            ),
        )

    user_prompt = _rewrite_prompt_for_virtual_fs(prompt, workspace)
    user_tools = _load_user_tools(workspace)
    if formal_lean_mode:
        user_tools.append(_create_lean_check_tool(workspace, lean_check_state))
    if subscription_mode:
        try:
            deepagents_model = _create_codex_subscription_model(workspace, model)
        except Exception as exc:  # noqa: BLE001
            emit_item({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return 1
    elif claude_subscription_mode:
        try:
            deepagents_model = _create_claude_subscription_model(workspace, model)
        except Exception as exc:  # noqa: BLE001
            emit_item({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return 1
    elif _is_kimi_api_model(model):
        try:
            deepagents_model = _create_kimi_api_model(model)
        except Exception as exc:  # noqa: BLE001
            emit_item({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return 1
    else:
        deepagents_model = model
    if formal_lean_mode:
        system_prompt = (
            "You are a formal Lean proving agent inside an isolated workspace. "
            "Read /TASK.md, maintain the exact target in /Proof.lean, and use the "
            "bounded lean_check tool to compile every revision. Finish only after "
            "the latest lean_check returns exit 0. Never use sorry or admit.\n"
        )
    else:
        system_prompt = (
            "You are a mathematics proving agent. Plan briefly, then write a complete "
            "informal proof.\n"
            + (
                "Subagents are disabled for this run; solve directly and do not delegate.\n"
                if not allow_subagents else ""
            )
            + "Your file tools operate on a virtual root / mapped to the task workspace. "
            "Always use paths like /problem.txt and /proof.md, never host paths "
            "under /Users or /mnt.\n"
            "Write the final proof to /proof.md (Markdown; use $...$ / $$...$$ for math)."
        )
    emit_item({
        "type": "agent_message",
        "text": "deepagents session · model "
        + (
            f"codex-subscription:{model}"
            if subscription_mode
            else f"claude-subscription:{model}"
            if claude_subscription_mode
            else model
        )
        + f" · fs={workspace}"
        + f" · subagents={'on' if allow_subagents else 'off'}"
        + (f" · user tools: {', '.join(t.name for t in user_tools)}" if user_tools else ""),
    })

    agent = create_deep_agent(
        model=deepagents_model,
        tools=user_tools or None,
        subagents=None if allow_subagents else [],
        backend=FilesystemBackend(root_dir=str(workspace), virtual_mode=True),
        system_prompt=system_prompt,
    )

    total_in = total_out = 0
    final_text = ""
    seen_msgs: set[str] = set()
    initial_proof_sha256 = _file_sha256(workspace / "proof.md")

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

    def stream_once(content: str) -> None:
        nonlocal files
        for chunk in agent.stream(
            {"messages": [{"role": "user", "content": content}]},
            stream_mode="values",
            config={"recursion_limit": _recursion_limit()},
        ):
            for m in chunk.get("messages") or []:
                handle_message(m)
            if isinstance(chunk.get("files"), dict):
                files = chunk["files"]

    if formal_lean_mode:
        formal_error = ""
        for attempt in range(3):
            request = user_prompt
            if attempt:
                request = (
                    "FORMAL LEAN RECOVERY PASS "
                    + str(attempt + 1)
                    + ". The prior pass ended without an accepted compiler-bound "
                    "artifact. Do not answer with a narrative and do not claim a "
                    "file compiled unless the tool result says so. If the current "
                    "source already compiles, still use write_file to make a fresh "
                    "safe change to its proof or explanatory doc comment; checking "
                    "an unchanged file is insufficient. Read /TASK.md and the "
                    "actual /Proof.lean if present; use write_file to create or "
                    "repair /Proof.lean, then call lean_check. Continue repairs "
                    "and lean_check calls until the latest result is exit 0. Never "
                    "use sorry or admit. Only after that successful tool result may "
                    "you return a brief final summary."
                )
            formal_error = ""
            try:
                stream_once(request)
            except Exception as exc:  # noqa: BLE001
                formal_error = f"{type(exc).__name__}: {exc}"
            if _compiled_lean_artifact_ready(
                workspace,
                initial_sha256=initial_lean_sha256,
                checked_sha256=lean_check_state.get("sha256"),
                check_exit_code=lean_check_state.get("exit_code"),
            ):
                break
            if attempt < 2:
                detail = (
                    f" Adapter error: {formal_error}"
                    if formal_error
                    else ""
                )
                emit_item(
                    {
                        "type": "reasoning",
                        "text": (
                            "[formal recovery] No fresh Proof.lean is bound to a "
                            "successful lean_check yet; starting another bounded "
                            f"tool pass.{detail}"
                        ),
                    }
                )
            elif formal_error:
                emit_item(
                    {
                        "type": "error",
                        "message": (
                            "DeepAgents Formal Lean exhausted its bounded tool "
                            f"recovery after an adapter failure: {formal_error}"
                        ),
                    }
                )
    else:
        try:
            stream_once(user_prompt)
        except Exception as exc:  # noqa: BLE001
            _normalize_changed_proof_outcome(
                workspace,
                prompt=prompt,
                initial_proof_sha256=initial_proof_sha256,
            )
            serialization_recovered = _persist_tool_serialization_checkpoint(
                workspace,
                prompt=prompt,
                initial_proof_sha256=initial_proof_sha256,
                error=str(exc),
            )
            recovered = serialization_recovered or (
                type(exc).__name__ == "GraphRecursionError"
                and _recoverable_recursion_checkpoint(
                    workspace,
                    prompt=prompt,
                    initial_proof_sha256=initial_proof_sha256,
                    final_text=final_text,
                )
            )
            if not recovered:
                emit_item({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
                return 1
            emit_item({
                "type": "agent_message",
                "text": (
                    "DeepAgents preserved the changed proof as an explicitly degraded "
                    "Partial Progress checkpoint after repeated tool-argument JSON "
                    "serialization failure."
                    if serialization_recovered
                    else (
                        "DeepAgents reached its recursion boundary after a final "
                        "current-run proof and structured audit had already passed "
                        "the trusted validators; recovered that checkpoint."
                    )
                ),
            })

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
        if formal_lean_mode and rel not in {"Proof.lean", "TASK.md"}:
            continue
        target = workspace / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if content is not None:
                target.write_text(content or "", encoding="utf-8")
            wrote.append(rel)
        except OSError:
            continue

    reported_candidates = (
        ("Proof.lean", "TASK.md")
        if formal_lean_mode
        else ("proof.md", "proof.tex", "problem.txt")
    )
    for candidate in reported_candidates:
        if (workspace / candidate).exists() and candidate not in wrote:
            wrote.append(candidate)

    if wrote:
        emit_item({"type": "file_change", "changes": [{"path": w} for w in wrote]})
    if formal_lean_mode:
        if not _compiled_lean_artifact_ready(
            workspace,
            initial_sha256=initial_lean_sha256,
            checked_sha256=lean_check_state.get("sha256"),
            check_exit_code=lean_check_state.get("exit_code"),
        ):
            last_exit = lean_check_state.get("exit_code")
            emit_item(
                {
                    "type": "error",
                    "message": (
                        "DeepAgents Formal Lean ended without a fresh, substantive "
                        "Proof.lean matching its latest successful lean_check"
                        + (
                            f" (latest exit {last_exit})"
                            if last_exit is not None
                            else " (lean_check was never completed)"
                        )
                    ),
                }
            )
            return 1
        emit_item(
            {
                "type": "agent_message",
                "text": "done · verified files: Proof.lean",
            }
        )
        return 0


    # Fallback: persist the final answer as proof.md if the agent didn't write it.
    if not _proof_artifact_ready(workspace):
        if final_text and not _looks_like_structured_dump(final_text):
            (workspace / "proof.md").write_text(final_text, encoding="utf-8")
            emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})

    _normalize_changed_proof_outcome(
        workspace,
        prompt=prompt,
        initial_proof_sha256=initial_proof_sha256,
    )

    _run_supplemental_audit(
        workspace,
        prompt=prompt,
        model=model,
        subscription_mode=subscription_mode,
        claude_subscription_mode=claude_subscription_mode,
        initial_proof_sha256=initial_proof_sha256,
        final_text=final_text,
    )
    _normalize_changed_proof_outcome(
        workspace,
        prompt=prompt,
        initial_proof_sha256=initial_proof_sha256,
    )

    if not _proof_artifact_ready(workspace):
        emit_item(
            {
                "type": "error",
                "message": (
                    "DeepAgents ended without a non-empty proof.md or proof.tex "
                    "artifact"
                ),
            }
        )
        return 1

    if "CANDIDATE AUDIT GATE:" in prompt and not (
        _recoverable_recursion_checkpoint(
            workspace,
            prompt=prompt,
            initial_proof_sha256=initial_proof_sha256,
            final_text=final_text,
        )
        or _recoverable_degraded_checkpoint(
            workspace,
            prompt=prompt,
            initial_proof_sha256=initial_proof_sha256,
        )
    ):
        emit_item(
            {
                "type": "error",
                "message": (
                    "DeepAgents ended without either a validator-backed audit "
                    "checkpoint or an explicit current-run degraded audit with "
                    "a concrete missing/failed artifact reason"
                ),
            }
        )
        return 1

    emit_item({"type": "agent_message", "text": f"done · files: {', '.join(wrote) or 'none'}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
