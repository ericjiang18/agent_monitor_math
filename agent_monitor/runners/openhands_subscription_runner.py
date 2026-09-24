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
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request

from agent_monitor.runners.outcome_contract import normalize_requested_outcome_file


SDK_VERSION = "1.21"
ACP_PACKAGE = "@agentclientprotocol/codex-acp"
_AUDIT_REQUIRED_FILES = {
    "global.json",
    "decomposed.json",
    "verifier-output.json",
    "merge-map.json",
    "run.json",
}
_OUTCOME_RE = re.compile(
    r"(?m)^ProvingConsole outcome: "
    r"(Solved|Counterexample|Known/Open Status|Partial Progress)\s*$"
)
_CONTROL_OUTCOME_RE = re.compile(
    r"(?:\*\*)?ProvingConsole outcome: "
    r"(Known/Open Status|Partial Progress)(?:\*\*)?"
)
_REFERENCES_HEADING_RE = re.compile(r"(?im)^#{1,6}\s+References\s*$")
_CITATION_MARKER_RE = re.compile(r"\[(\d+)\]")
_REFERENCE_ENTRY_RE = re.compile(r"(?m)^\s*\[(\d+)\]\s+\S")
_KIMI_OPENHANDS_CONTRACT = """

KIMI / OPENHANDS ARTIFACT CONTRACT:
- Follow each tool's published JSON schema exactly. This isolated auto-approved
  run does not use the LLM security analyzer, so no synthetic risk field is
  required unless it is present in the tool schema.
- Work only in the current workspace. The required final artifact is the root
  file ./proof.md, not merely a chat response or a file in another directory.
- Before stopping, read ./proof.md and ensure it is a rigorous, self-contained
  proof with a nonempty `## References` section. Every numeric marker [n] in
  the proof body must have one [n] entry there and every entry must be cited.
  If no external source is used, include a bullet such as
  `- self-derived — elementary argument`.
""".rstrip()


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


def _normalize_requested_proof(
    workspace: Path,
    prompt: str,
    *,
    initial_fingerprint: tuple[int, int, int, str] | None = None,
    require_changed: bool = False,
) -> bool:
    """Canonicalize the footer without making a stale artifact look current."""
    proof = workspace / "proof.md"
    if (
        require_changed
        and _artifact_fingerprint(proof) == initial_fingerprint
    ):
        return False
    return normalize_requested_outcome_file(proof, prompt)


def _is_kimi_api_model(model: str | None) -> bool:
    normalized = str(model or "").strip().lower().replace(":", "/")
    return normalized.rsplit("/", 1)[-1] == "kimi-k3"


def _proof_reference_issues(path: Path) -> list[str]:
    """Validate the root proof and its auditable References contract."""
    if path.is_symlink():
        return ["proof.md must be a regular root file, not a symlink"]
    try:
        if not path.is_file():
            return ["proof.md is missing, empty, or not a substantive proof"]
        stat = path.stat()
        if stat.st_size > 2_000_000:
            return ["proof.md exceeds the 2 MB artifact limit"]
        text = path.read_text(encoding="utf-8")
        if not _is_proof_response(text):
            return ["proof.md is missing, empty, or not a substantive proof"]
        if len(text.strip()) < 200:
            return ["proof.md is too short to be a substantive proof"]
    except OSError:
        return ["proof.md could not be read"]

    heading = _REFERENCES_HEADING_RE.search(text)
    if heading is None:
        return ["proof.md has no References heading"]
    body = text[: heading.start()]
    references = text[heading.end() :]
    meaningful = [
        line.strip()
        for line in references.splitlines()
        if line.strip() and line.strip() not in {"```", "---"}
    ]
    issues: list[str] = []
    if not meaningful:
        issues.append("References section is empty")

    body_markers = set(_CITATION_MARKER_RE.findall(body))
    reference_markers = set(_REFERENCE_ENTRY_RE.findall(references))
    missing = sorted(body_markers - reference_markers, key=int)
    unused = sorted(reference_markers - body_markers, key=int)
    if missing:
        issues.append("missing reference entries for: " + ", ".join(f"[{n}]" for n in missing))
    if unused:
        issues.append("uncited reference entries: " + ", ".join(f"[{n}]" for n in unused))
    if not reference_markers and meaningful and not any(
        line.startswith(("-", "*"))
        or "self-derived" in line.lower()
        or line.lower().startswith("[uncited:")
        for line in meaningful
    ):
        issues.append("References section has no auditable entry")
    return issues


def _artifact_fingerprint(path: Path) -> tuple[int, int, int, str] | None:
    """Identify an artifact without trusting timestamps alone."""
    try:
        stat = path.stat()
        if not path.is_file() or path.is_symlink():
            return None
        digest = (
            "oversize"
            if stat.st_size > 2_000_000
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns, digest)
    except OSError:
        return None


def _bounded_text(text: str, limit: int = 80_000) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    half = max(1, (limit - 80) // 2)
    return (
        text[:half]
        + "\n\n[... context truncated by the artifact repair boundary ...]\n\n"
        + text[-half:]
    )


def _strip_markdown_fence(text: str) -> str:
    text = str(text or "").strip()
    match = re.fullmatch(
        r"```(?:markdown|md)?\s*\n(?P<body>.*)\n```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group("body").strip() if match else text


def _kimi_text_completion(user_prompt: str) -> tuple[str, dict[str, int]]:
    """Make one bounded, tool-free Kimi Chat Completions request."""
    try:
        from agent_monitor import kimi_k3
    except ModuleNotFoundError as exc:
        if exc.name != "agent_monitor":
            raise
        # The OpenHands CLI is installed in its own uv tool environment.  When
        # this script re-execs under that interpreter from a run workspace,
        # Python only adds this runners/ directory to sys.path.  Resolve the
        # repository package from this file rather than relying on cwd.
        repository_root = str(Path(__file__).resolve().parents[2])
        if repository_root not in sys.path:
            sys.path.insert(0, repository_root)
        from agent_monitor import kimi_k3

    key = str(os.environ.get(kimi_k3.KEY_VAR) or os.environ.get("LLM_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("KIMI_API_KEY is not available to the OpenHands run")
    base = str(
        os.environ.get(kimi_k3.BASE_VAR)
        or os.environ.get("LLM_BASE_URL")
        or kimi_k3.DEFAULT_BASE_URL
    ).strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise RuntimeError("KIMI_API_BASE must be an HTTP(S) URL")
    payload = kimi_k3.chat_payload(
        system=(
            "You are the final artifact writer for a mathematics proving system. "
            "Return only the complete proof.md Markdown, without commentary or "
            "code fences. It must be rigorous and self-contained, and end with a "
            "nonempty ## References section. Use a self-derived bullet when no "
            "external source is needed. Do not invoke tools."
        ),
        user=user_prompt,
        env=os.environ,
        max_tokens=16_000,
    )
    req = urllib_request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        configured_timeout = float(os.environ.get("OPENHANDS_CODEX_TIMEOUT", "600"))
    except ValueError:
        configured_timeout = 600
    timeout = max(30.0, min(configured_timeout, 600.0))
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            raw = response.read(4_000_001)
    except urllib_error.HTTPError as exc:
        raise RuntimeError(f"Kimi artifact request returned HTTP {exc.code}") from exc
    except (OSError, urllib_error.URLError) as exc:
        raise RuntimeError(
            f"Kimi artifact request failed: {type(exc).__name__}"
        ) from exc
    if len(raw) > 4_000_000:
        raise RuntimeError("Kimi artifact response exceeded 4 MB")
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Kimi artifact response was not valid JSON") from exc
    text = _strip_markdown_fence(kimi_k3.response_text(body))
    if not text:
        raise RuntimeError("Kimi artifact response contained no proof text")
    return text, kimi_k3.response_usage(body)


def _write_kimi_artifact_repair(
    *,
    prompt: str,
    workspace: Path,
    issues: list[str],
) -> dict[str, int]:
    """Generate and atomically install one server-validated proof artifact."""
    proof_path = workspace / "proof.md"
    try:
        draft = proof_path.read_text(encoding="utf-8") if proof_path.is_file() else ""
    except OSError:
        draft = ""
    problem_path = workspace / "problem.txt"
    try:
        problem = (
            problem_path.read_text(encoding="utf-8")
            if problem_path.is_file()
            else ""
        )
    except OSError:
        problem = ""
    artifact_prompt = (
        "Rewrite the final proof artifact in full. Correct every listed issue; "
        "do not return a patch or describe edits.\n\n"
        "PREFLIGHT ISSUES:\n- "
        + "\n- ".join(issues)
        + "\n\nPROBLEM:\n"
        + _bounded_text(problem)
        + "\n\nORIGINAL TASK INSTRUCTIONS:\n"
        + _bounded_text(prompt)
        + "\n\nCURRENT PARTIAL ARTIFACT (may be empty or corrupt):\n"
        + _bounded_text(draft)
    )
    proof_text, usage = _kimi_text_completion(artifact_prompt)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".proof-kimi-repair-",
            suffix=".md",
            dir=workspace,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(proof_text.rstrip() + "\n")
        candidate_issues = _proof_reference_issues(temporary)
        if candidate_issues:
            raise RuntimeError(
                "Kimi artifact continuation was rejected: "
                + "; ".join(candidate_issues)
            )
        os.replace(temporary, proof_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return usage


def _native_api_argv(binary: str, prompt: str) -> list[str]:
    return [
        binary,
        "--headless",
        "--json",
        "--always-approve",
        "--exit-without-confirmation",
        "--override-with-envs",
        "-t",
        prompt,
    ]


@contextlib.contextmanager
def _kimi_sdk_conversation(workspace: Path) -> Iterator[object]:
    """Create one isolated API-key conversation with no redundant risk LLM."""
    keys = (
        "OPENHANDS_WORK_DIR",
        "OPENHANDS_PERSISTENCE_DIR",
        "OPENHANDS_CONVERSATIONS_DIR",
    )
    previous = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(prefix="agent-monitor-openhands-kimi-") as td:
        os.environ["OPENHANDS_WORK_DIR"] = str(workspace)
        os.environ["OPENHANDS_PERSISTENCE_DIR"] = td
        os.environ["OPENHANDS_CONVERSATIONS_DIR"] = str(Path(td) / "conversations")
        conversation = None
        try:
            from openhands.sdk.security.confirmation_policy import NeverConfirm
            from openhands_cli.setup import setup_conversation
            from openhands_cli.utils import json_callback

            conversation = setup_conversation(
                uuid.uuid4(),
                NeverConfirm(),
                visualizer=None,
                event_callback=json_callback,
                env_overrides_enabled=True,
                critic_disabled=True,
            )
            conversation.max_iteration_per_run = _max_iterations()
            # setup_conversation always installs LLMSecurityAnalyzer, even with
            # NeverConfirm. In auto-approved mode it cannot gate an action, but
            # it still makes every write tool require a model-predicted
            # security_risk field. Kimi occasionally omits that redundant
            # annotation. Removing the analyzer preserves NeverConfirm behavior
            # while letting the ordinary tool schema remain authoritative.
            conversation.set_security_analyzer(None)
            yield conversation
        finally:
            if conversation is not None:
                try:
                    conversation.close()
                except Exception:  # noqa: BLE001
                    pass
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _run_kimi_native_api(prompt: str, workspace: Path) -> int:
    """Run OpenHands plus at most one bounded, tool-free artifact repair."""
    proof_path = workspace / "proof.md"
    initial_fingerprint = _artifact_fingerprint(proof_path)

    def artifact_issues() -> list[str]:
        issues = _proof_reference_issues(proof_path)
        if (
            not issues
            and initial_fingerprint is not None
            and _artifact_fingerprint(proof_path) == initial_fingerprint
        ):
            issues.append("proof.md was not created or updated by this run")
        return issues

    first_prompt = prompt.rstrip() + _KIMI_OPENHANDS_CONTRACT
    first_error: str | None = None
    try:
        with _kimi_sdk_conversation(workspace) as conversation:
            try:
                conversation.send_message(first_prompt)
                conversation.run()
            except Exception as exc:  # noqa: BLE001
                first_error = f"{type(exc).__name__}: {exc}"

            issues = artifact_issues()
            if not issues:
                _normalize_requested_proof(workspace, prompt)
                if first_error is not None:
                    emit_item(
                        {
                            "type": "agent_message",
                            "text": (
                                "Kimi/OpenHands ended after writing a new proof "
                                "artifact that passed the server-owned content and "
                                "citation checks; recovered that completed artifact."
                            ),
                        }
                    )
                return 0

    except Exception as exc:  # noqa: BLE001
        emit_item(
            {
                "type": "error",
                "message": f"Kimi/OpenHands SDK setup failed: {type(exc).__name__}: {exc}",
            }
        )
        return 1

    reason = "; ".join(issues) if issues else str(first_error)
    emit_item(
        {
            "type": "agent_message",
            "text": (
                "Kimi/OpenHands artifact preflight requested one bounded, "
                "tool-free artifact continuation: " + reason
            )[:4000],
        }
    )
    try:
        usage = _write_kimi_artifact_repair(
            prompt=prompt,
            workspace=workspace,
            issues=issues or [str(first_error)],
        )
        repair_error = None
    except Exception as exc:  # noqa: BLE001
        usage = {}
        repair_error = f"{type(exc).__name__}: {exc}"

    remaining = artifact_issues()
    if not remaining:
        _normalize_requested_proof(workspace, prompt)
        emit_item(
            {
                "type": "agent_message",
                "text": (
                    "Kimi/OpenHands installed the bounded continuation after it "
                    "passed the server-owned proof and citation checks."
                ),
            }
        )
        emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})
        emit({"type": "turn.completed", "usage": usage})
        return 0
    detail_parts = list(remaining)
    if repair_error is not None:
        detail_parts.append(repair_error)
    detail = "; ".join(detail_parts)
    emit_item(
        {
            "type": "error",
            "message": (
                "Kimi/OpenHands failed the proof artifact and citation contract "
                "after one bounded repair: " + detail
            )[:4000],
        }
    )
    return 1


def _recoverable_activity_checkpoint(workspace: Path, prompt: str) -> bool:
    """Accept only reconciled or explicitly degraded conservative checkpoints."""
    if "CANDIDATE AUDIT GATE:" not in prompt:
        return False
    proof = workspace / "proof.md"
    if proof.is_symlink() or not _valid_proof_file(proof):
        return False
    try:
        proof_text = proof.read_text(encoding="utf-8")
    except OSError:
        return False
    outcomes = _OUTCOME_RE.findall(proof_text)
    if len(outcomes) != 1:
        return False
    audit_root = workspace / "audit"
    if (
        not audit_root.is_dir()
        or audit_root.is_symlink()
    ):
        return False
    from agent_monitor.proof_tools import audit_output_validator
    from agent_monitor.runners.deepseek_harness_runner import _valid_merge_map

    for directory in audit_root.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        paths = {name: directory / name for name in _AUDIT_REQUIRED_FILES}
        try:
            if not all(
                path.is_file() and not path.is_symlink() and path.stat().st_size > 0
                for path in paths.values()
            ):
                continue
            merge_map = json.loads(paths["merge-map.json"].read_text(encoding="utf-8"))
            run_manifest = json.loads(paths["run.json"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        counts = merge_map.get("counts") if isinstance(merge_map, dict) else None
        if (
            not isinstance(merge_map, dict)
            or merge_map.get("schema_version")
            != "ensemble-paper-audit-app.merge-map.v1"
            or not _valid_merge_map(merge_map)
            or not isinstance(counts, dict)
            or not all(
                isinstance(counts.get(key), int) and counts[key] >= 0
                for key in ("global_only", "decomposed_only", "both")
            )
            or not isinstance(run_manifest, dict)
            or not run_manifest
        ):
            continue
        valid = True
        for name in ("global.json", "decomposed.json", "verifier-output.json"):
            try:
                response = audit_output_validator(
                    {
                        "document": json.loads(
                            paths[name].read_text(encoding="utf-8")
                        )
                    }
                )
            except (OSError, ValueError, json.JSONDecodeError):
                valid = False
                break
            if response.get("valid") is not True:
                valid = False
                break
        if valid:
            from agent_monitor.research_audit import reconciliation_complete

            if reconciliation_complete(workspace, directory):
                return True
            if (
                outcomes == ["Partial Progress"]
                and re.search(r"(?i)\bdegraded audit\b", proof_text)
            ):
                return True
    return False


def _recoverable_control_checkpoint(workspace: Path, prompt: str) -> bool:
    """Recover a conservative control artifact after the activity boundary.

    This path intentionally excludes Solved and Counterexample.  Those outcomes
    still require the ordinary successful runner path plus downstream manual
    review rather than being promoted from a boundary checkpoint.
    """
    if (
        "CONTROL CONDITION:" not in prompt
        or "CANDIDATE AUDIT GATE:" in prompt
    ):
        return False
    proof = workspace / "proof.md"
    try:
        if (
            proof.is_symlink()
            or not _valid_proof_file(proof)
            or proof.stat().st_size > 2_000_000
        ):
            return False
        proof_text = proof.read_text(encoding="utf-8")
    except OSError:
        return False
    outcome_lines = [
        line.strip()
        for line in proof_text.splitlines()
        if _CONTROL_OUTCOME_RE.fullmatch(line.strip())
    ]
    nonempty_lines = [line.strip() for line in proof_text.splitlines() if line.strip()]
    if (
        len(outcome_lines) != 1
        or not nonempty_lines
        or nonempty_lines[-1] != outcome_lines[0]
    ):
        return False
    for name in ("proof-sanity-check.sh", "citation-audit.sh"):
        tool = workspace / "_library" / "tools" / name
        if not tool.is_file() or tool.is_symlink():
            return False
        try:
            checked = subprocess.run(
                ["bash", str(tool), "proof.md"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if checked.returncode != 0:
            return False
    return True


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

    initial_fingerprint = _artifact_fingerprint(workspace / "proof.md")
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

    proof = workspace / "proof.md"
    if not _valid_proof_file(proof) and _is_proof_response(final_text):
        proof.write_text(final_text, encoding="utf-8")
    _normalize_requested_proof(
        workspace,
        prompt,
        initial_fingerprint=initial_fingerprint,
        require_changed=True,
    )
    if limit_reached or tool_budget.exceeded:
        audit_recovered = _recoverable_activity_checkpoint(workspace, prompt)
        control_recovered = (
            not audit_recovered
            and _recoverable_control_checkpoint(workspace, prompt)
        )
        if audit_recovered or control_recovered:
            emit_item(
                {
                    "type": "agent_message",
                    "text": (
                        (
                            "OpenHands reached its activity boundary after the "
                            "current conservative proof and audit files passed the "
                            "server-owned safety checks; recovered that checkpoint. "
                            "Any missing final reconciliation remains visible in Monitor."
                        )
                        if audit_recovered
                        else (
                            "OpenHands reached its activity boundary after a "
                            "conservative control proof passed the trusted proof and "
                            "citation preflights; recovered that checkpoint."
                        )
                    ),
                }
            )
            emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})
            emit({"type": "turn.completed", "usage": _usage_payload(usage)})
            return 0
        raise OpenHandsIterationLimit(
            f"Agent reached maximum iterations/activity limit ({_max_iterations()})."
        )

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
    initial_fingerprint = _artifact_fingerprint(workspace / "proof.md")
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
    _normalize_requested_proof(
        workspace,
        prompt,
        initial_fingerprint=initial_fingerprint,
        require_changed=True,
    )
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
    codex_subscription = os.environ.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"
    claude_subscription = os.environ.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1"
    if codex_subscription and claude_subscription:
        emit_item(
            {
                "type": "error",
                "message": (
                    "OpenHands refused conflicting Codex and Claude account routes; "
                    "select exactly one authentication route."
                ),
            }
        )
        return 1
    if claude_subscription:
        # OpenHands SDK 1.21 has a genuine Claude ACP integration, but this
        # host does not yet carry the pinned, locally approved
        # claude-agent-acp runtime needed to preserve that integration's
        # conversation and tool semantics. Never let an explicit linked
        # account route fall through to the native API/Kimi launcher: that
        # would silently change both billing and harness behavior.
        emit_item(
            {
                "type": "error",
                "message": (
                    "OpenHands does not yet support the linked Claude Code "
                    "account on this host: the pinned Claude ACP runtime and "
                    "exact-model attestation are not installed. Choose a "
                    "Claude-compatible harness or configure an API provider."
                ),
            }
        )
        return 1
    if not codex_subscription:
        binary = _which("openhands")
        if not binary:
            emit_item({"type": "error", "message": "openhands executable not found"})
            return 127
        requested_model = (
            os.environ.get("LLM_MODEL")
            or os.environ.get("AGENT_MONITOR_OPENHANDS_MODEL")
            or ""
        )
        if _is_kimi_api_model(requested_model):
            try:
                _ensure_sdk_python()
                return _run_kimi_native_api(prompt, Path.cwd().resolve())
            except Exception as exc:  # noqa: BLE001
                emit_item(
                    {
                        "type": "error",
                        "message": (
                            "Kimi/OpenHands SDK startup failed: "
                            f"{type(exc).__name__}: {exc}"
                        )[:4000],
                    }
                )
                return 1
        initial_fingerprint = _artifact_fingerprint(Path.cwd() / "proof.md")
        try:
            returncode = subprocess.call(_native_api_argv(binary, prompt))
        except OSError as exc:
            emit_item(
                {
                    "type": "error",
                    "message": f"OpenHands native API launch failed: {exc}",
                }
            )
            return 1
        if returncode == 0:
            _normalize_requested_proof(
                Path.cwd().resolve(),
                prompt,
                initial_fingerprint=initial_fingerprint,
                require_changed=True,
            )
        return int(returncode)
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
        initial_fingerprint = _artifact_fingerprint(workspace / "proof.md")
        try:
            return _run_acp(prompt, workspace, codex_home)
        except OpenHandsIterationLimit as limit_error:
            emit_item({"type": "error", "message": str(limit_error)})
            return 1
        except Exception as acp_error:  # noqa: BLE001
            _normalize_requested_proof(
                workspace,
                prompt,
                initial_fingerprint=initial_fingerprint,
                require_changed=True,
            )
            audit_recovered = _recoverable_activity_checkpoint(workspace, prompt)
            control_recovered = (
                not audit_recovered
                and _recoverable_control_checkpoint(workspace, prompt)
            )
            if audit_recovered or control_recovered:
                emit_item(
                    {
                        "type": "agent_message",
                        "text": (
                            "OpenHands ACP ended after writing a safety-checked "
                            + (
                                "conservative audit checkpoint; recovered it without "
                                "rerunning the candidate through another backend."
                                if audit_recovered
                                else "conservative control checkpoint; recovered it "
                                "without rerunning the candidate through another backend."
                            )
                        ),
                    }
                )
                emit_item({"type": "file_change", "changes": [{"path": "proof.md"}]})
                return 0
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
