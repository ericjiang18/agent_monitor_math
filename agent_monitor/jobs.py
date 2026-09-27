"""Background job manager for engine runs."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import shutil
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any  # noqa: F401 — used throughout

from agent_monitor import CACHE_DIR, HERMES_HOME, PROBLEMS_DIR, RUNS_DIR
from agent_monitor import attachments as research_attachments
from agent_monitor.research_audit import (
    reconciliation_complete,
    workspace_reconciliation_status,
)
from agent_monitor.schema import normalize_run
from agent_monitor.proof_markdown import normalize_proof_markdown
from agent_monitor import attachments as research_attachments

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_DELETED_RUNS: set[str] = set()
_STOP_EVENTS: dict[str, threading.Event] = {}
_PROCS: dict[str, Any] = {}  # run_id -> subprocess.Popen
_RECONCILE_LOCK = threading.Lock()
_RUN_WRITE_LOCK = threading.Lock()
_HERMES_HEARTBEAT_S = 2.0
# Problem IDs become run-record and cache filenames, so they must never carry a
# path separator or a traversal segment.
_PROBLEM_ID_RE = re.compile(r"^[A-Za-z0-9_.()\[\]-]{1,128}$")

# The tool-free Kimi harness speaks the same bounded chat-completions transport
# as the Plain baseline, so it requests sponsored credentials under that name.
_SPONSORED_TRANSPORT_ENGINES = {"kimi": "plain"}


def _sponsored_transport_engine(engine: str) -> str:
    return _SPONSORED_TRANSPORT_ENGINES.get(engine, engine)

_RESEARCH_AUDIT_GATE_MARKER = "CANDIDATE AUDIT GATE:"
_RESEARCH_AUDIT_RUNTIME_MARKER = "STRICT FILE-BACKED AUDIT CONTRACT:"
_RESEARCH_AUDIT_RUNTIME_CONTRACT = """
STRICT FILE-BACKED AUDIT CONTRACT: If the candidate-audit gate activates, do
not invent a convenient JSON shape and do not treat a model saying "validated"
as validation. Every pass JSON must satisfy the installed verifier-output.v1
schema and must be checked by `_library/tools/audit-output-validator.sh` when
that trusted tool is present. Before finalizing, freeze the exact final
proof.md bytes as audit/<run-id>/final-candidate.md. If the proof changed after
the first review, rerun the independent global and decomposed passes against
that exact file as global-rerun.json and decomposed-rerun.json, rebuild
verifier-output.json and merge-map.json, and retain any refuter rerun. run.json
must use reconciliation schema research-proof-audit.reconciliation.v1 to bind
the root proof hash, final-candidate hash, and current rerun files.
The merge map must preserve standard finding source records and the counts
keys global_only, decomposed_only, and both. Run
`_agent/skills/research-proof-audit/scripts/validate_reconciliation.py
audit/<run-id>` and do not edit proof.md afterward. If that command is
unavailable or nonzero, state `degraded audit:` and list the exact missing or
invalid artifacts. Never convert audit acceptance into a solved claim.
""".strip()

_RESEARCH_AUDIT_REQUIRED_FILES = {
    "global.json",
    "decomposed.json",
    "verifier-output.json",
    "merge-map.json",
    "run.json",
}
_HERMES_AUDIT_LAST_MILE_PROMPT = """
Mandatory audit last mile for this explicit candidate-audit run:

The current proof.md is the frozen candidate. Do not start a new proof or a
new broad literature search. Read `_agent/skills/research-proof-audit/SKILL.md`
completely, then reconcile the existing candidate with independent global and
decomposed passes. Add a refuter pass only if proof.md proposes a solution or
counterexample. Use one `audit/<run-id>/` directory and finish it with valid
global.json and decomposed.json. Update proof.md to repair every accepted
finding or retain it as an explicit unresolved obligation, then freeze its
exact bytes as final-candidate.md. Run fresh global and decomposed passes on
that exact file as global-rerun.json and decomposed-rerun.json, rebuild
verifier-output.json and merge-map.json, and record a
research-proof-audit.reconciliation.v1 object in run.json that binds both
hashes and those reruns. Run `_library/tools/audit-output-validator.sh` on
every verifier-output.v1 file, then run
`_agent/skills/research-proof-audit/scripts/validate_reconciliation.py
audit/<run-id>`. Do not edit proof.md afterward. Preserve the existing
conservative outcome unless the exact original claim is independently closed;
audit acceptance alone is never a solution. Before returning, list the
artifact paths and both validator results.
""".strip()

# Guests may start only these two tool-free engines. A command override takes
# the engine outside that built-in contract, so it disables it for guests.
_GUEST_ENGINES = {"plain", "kimi"}
_GUEST_COMMAND_OVERRIDES = {"plain": "PLAIN_CMD", "kimi": "KIMI_PROOF_CMD"}

# Engines that can consume the run owner's Codex/ChatGPT subscription. Some
# use a native Codex runtime; the small Python harnesses use our codex-exec
# adapter. UCLA is deliberately absent because its current pipeline has no
# subscription-safe model path.
_CODEX_SUBSCRIPTION_ENGINES = {
    "hermes",
    "math_harness",
    "codex",
    "openclaude",
    "improof",
    "openhands",
    "openclaw",
    "deepagents",
    "metaharness",
    "plain",
    "deepseek_harness",
    "danus",
}

# Claude OAuth is intentionally exposed only through adapters that invoke the
# official Claude Code CLI. OAuth credentials are not API keys and must never
# be copied into third-party provider clients.
_CLAUDE_SUBSCRIPTION_ENGINES = {
    "claude", "improof", "math_harness", "openclaw", "deepagents",
    "metaharness", "plain"
}
_AUTH_ROUTES = {"api_key", "codex_subscription", "claude_subscription"}


_SPONSORED_KIMI_CLI_TIMEOUT_MAX = 6_600
_SPONSORED_KIMI_TOKEN_MARGIN = 300
# Provider credentials and endpoint selectors must be dormant in a linked
# account process. This is intentionally broader than the variables used by
# Claude/Codex themselves: compatible third-party harness adapters receive the
# same child environment and must not discover a metered API fallback.
_PROVIDER_ROUTE_ENV_NAMES = {
    "OPENAI_API_KEY", "OPENAI_API_KEYS", "CODEX_API_KEY",
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "AGENT_MONITOR_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "KIMI_API_KEY", "KIMI_API_BASE", "KIMI_BASE_URL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE",
    "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
    "GEMINI_API_KEY", "GEMINI_API_BASE", "GOOGLE_API_KEY",
    "GROQ_API_KEY", "GROQ_API_BASE",
    "TOGETHERAI_API_KEY", "TOGETHERAI_API_BASE",
    "XAI_API_KEY", "XAI_API_BASE",
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "AGENT_MONITOR_MODEL", "HERMES_MODEL", "AGENT_MONITOR_OPENAI_MODEL",
    "PLAIN_MODEL", "METAHARNESS_MODEL", "DEEPAGENTS_MODEL",
    "AGENT_MONITOR_OPENCLAUDE_MODEL", "AGENT_MONITOR_OPENCLAW_MODEL",
    "AGENT_MONITOR_OPENHANDS_MODEL",
}


def _scrub_provider_route_env(env: dict[str, str]) -> dict[str, str]:
    """Remove every known metered model route from a subscription child env."""
    for name in _PROVIDER_ROUTE_ENV_NAMES:
        env.pop(name, None)
    return env


def _apply_sponsored_kimi_route(
    env: dict[str, str],
    *,
    engine: str,
    model: str | None,
    force: bool = False,
) -> bool:
    """Bind Kimi to the isolated response-only service, never its provider key."""
    if not _is_kimi_model(model):
        return False
    engine = "plain" if engine == "kimi" else engine
    if force:
        for name in (
            "KIMI_API_KEY",
            "KIMI_API_BASE",
            "KIMI_BASE_URL",
            "AGENT_MONITOR_SPONSORED_KIMI",
            "AGENT_MONITOR_SPONSORED_KIMI_URL",
        ):
            env.pop(name, None)
    if str(env.get("KIMI_API_KEY") or "").strip():
        return False

    from agent_monitor import sponsored_kimi_client

    if not sponsored_kimi_client.supports_engine(engine):
        raise ValueError(
            f"Server-sponsored Kimi K3 is not available with the {engine} harness; "
            "choose a compatible harness or add your own Kimi API key in Settings"
        )
    if not sponsored_kimi_client.configured():
        raise ValueError(
            "Kimi K3 requires your own API key, or the sponsored Kimi service "
            "must be available"
        )
    env["AGENT_MONITOR_SPONSORED_KIMI"] = "1"
    env["AGENT_MONITOR_SPONSORED_KIMI_URL"] = sponsored_kimi_client.endpoint()
    return True


class StopRequested(Exception):
    """Raised inside a run when the user pressed Stop."""


def _augment_research_audit_prompt(prompt: str) -> str:
    """Add the strict output contract only to explicit candidate-audit runs."""
    if (
        _RESEARCH_AUDIT_GATE_MARKER not in prompt
        or _RESEARCH_AUDIT_RUNTIME_MARKER in prompt
    ):
        return prompt
    return f"{prompt.rstrip()}\n\n{_RESEARCH_AUDIT_RUNTIME_CONTRACT}"


def _research_audit_artifacts_ready(workspace: Path) -> bool:
    """Return true only when one audit directory has the required nonempty files."""
    audit_root = workspace / "audit"
    if not audit_root.is_dir():
        return False
    for directory in audit_root.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        if all(
            (directory / name).is_file()
            and not (directory / name).is_symlink()
            and (directory / name).stat().st_size > 0
            for name in _RESEARCH_AUDIT_REQUIRED_FILES
        ) and reconciliation_complete(workspace, directory):
            return True
    return False


def _hermes_audit_last_mile_needed(problem_text: str, workspace: Path) -> bool:
    """Require one follow-up only for explicit audits with a substantive draft."""
    if _RESEARCH_AUDIT_GATE_MARKER not in problem_text:
        return False
    proof = workspace / "proof.md"
    try:
        substantive = (
            proof.is_file()
            and not proof.is_symlink()
            and proof.stat().st_size >= 400
        )
    except OSError:
        substantive = False
    return substantive and not _research_audit_artifacts_ready(workspace)


def _merge_hermes_turn_results(
    initial: dict[str, Any] | None, followup: dict[str, Any] | None
) -> dict[str, Any]:
    """Keep follow-up output while preserving per-turn usage and completion state."""
    first = dict(initial or {})
    second = dict(followup or {})
    merged = {**first, **second}
    for key in (
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "completion_tokens",
        "api_calls",
        "tool_call_count",
    ):
        values = [value for value in (first.get(key), second.get(key)) if isinstance(value, (int, float))]
        if values:
            merged[key] = sum(values)
    merged["completed"] = bool(first.get("completed", True)) and bool(
        second.get("completed", True)
    )
    merged["audit_last_mile"] = True
    return merged


def _stop_event(run_id: str) -> threading.Event:
    with _LOCK:
        ev = _STOP_EVENTS.get(run_id)
        if ev is None:
            ev = threading.Event()
            _STOP_EVENTS[run_id] = ev
        return ev


def _terminate_registered_proc(proc: Any, *, force: bool = True) -> None:
    """Terminate one registered runner and its isolated descendant group."""
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        pid = int(proc.pid)
        from agent_monitor.process_control import terminate_descendants

        terminate_descendants(pid, force=force)
        pgid = os.getpgid(pid)
        # Registered runners should be session leaders. Refuse to signal a
        # shared service group if an older/custom runner violates that contract.
        if pgid == pid:
            os.killpg(pgid, sig)
            return
    except (AttributeError, OSError, ProcessLookupError, TypeError, ValueError):
        pass
    try:
        proc.kill() if force else proc.terminate()
    except OSError:
        pass


def _register_proc(run_id: str, proc: Any) -> None:
    with _LOCK:
        _PROCS[run_id] = proc
        already_stopped = bool(
            _STOP_EVENTS.get(run_id) and _STOP_EVENTS[run_id].is_set()
        )
    # Stop may arrive in the narrow window after Popen and before registration.
    # Honor it immediately and kill the whole isolated group, not only the
    # wrapper process (which can otherwise orphan Codex or another child CLI).
    if already_stopped:
        _terminate_registered_proc(proc)


def _unregister_proc(run_id: str) -> None:
    with _LOCK:
        _PROCS.pop(run_id, None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_dir() -> Path:
    d = CACHE_DIR / "harness"
    d.mkdir(parents=True, exist_ok=True)
    return d


def workspace_dir(run_id: str) -> Path:
    """Per-run sandbox where engines write .tex / artifacts."""
    d = RUNS_DIR / "workspaces" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_text(path: Path, content: str) -> None:
    # Publish a complete file without exposing a truncated reader view.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_run(run: dict[str, Any]) -> Path:
    # Heartbeats, continuations, and reconciliation can update one record.
    # Serialize the merge and atomically publish both copies.
    with _RUN_WRITE_LOCK:
        return _write_run_locked(run)


def _merge_continuation_run(
    previous: dict[str, Any], current: dict[str, Any], *, job_id: str
) -> dict[str, Any]:
    """Preserve the full monitor/usage lineage when a follow-up finishes."""
    result = dict(current)
    previous_agents = [
        dict(agent) for agent in previous.get("agents") or []
        if isinstance(agent, dict)
    ]
    current_agents = [
        dict(agent) for agent in current.get("agents") or []
        if isinstance(agent, dict)
    ]
    continuation_number = max(0, int(previous.get("continuation_count") or 0)) + 1
    suffix = f"::continue-{job_id}"

    previous_rounds = [agent.get("round_id") for agent in previous_agents]
    previous_rounds = [value for value in previous_rounds if isinstance(value, int)]
    current_rounds = [agent.get("round_id") for agent in current_agents]
    current_rounds = [value for value in current_rounds if isinstance(value, int)]
    round_offset = 0
    if previous_rounds and current_rounds:
        round_offset = max(previous_rounds) + 1 - min(current_rounds)

    id_map: dict[str, str] = {}
    for index, agent in enumerate(current_agents, 1):
        found_identifier = False
        for key in ("trace_id", "id"):
            old = str(agent.get(key) or "")
            if not old:
                continue
            found_identifier = True
            new = f"{old}{suffix}"
            id_map[old] = new
            agent[key] = new
        if not found_identifier:
            run_id = str(current.get("run_id") or previous.get("run_id") or "run")
            agent["trace_id"] = f"{run_id}{suffix}::node-{index}"
        if isinstance(agent.get("round_id"), int):
            agent["round_id"] += round_offset
        stage_name = str(agent.get("stage_name") or "").strip()
        if stage_name:
            agent["stage_name"] = f"{stage_name} · continuation {continuation_number}"
        agent["continuation_job_id"] = job_id

    previous_edges = [
        dict(edge) for edge in previous.get("edges") or []
        if isinstance(edge, dict)
    ]
    current_edges: list[dict[str, Any]] = []
    for edge in current.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        rewritten = dict(edge)
        for key in ("from", "to"):
            old = str(rewritten.get(key) or "")
            if old in id_map:
                rewritten[key] = id_map[old]
        current_edges.append(rewritten)

    def node_id(agent: dict[str, Any]) -> str:
        return str(agent.get("trace_id") or agent.get("id") or "")

    bridge: list[dict[str, Any]] = []
    if previous_agents and current_agents:
        source = node_id(previous_agents[-1])
        target = node_id(current_agents[0])
        if source and target:
            bridge.append({"from": source, "to": target, "type": "continue"})

    totals: dict[str, Any] = {}
    old_totals = previous.get("totals") or {}
    new_totals = current.get("totals") or {}
    for key in (
        "input_tokens", "output_tokens", "cached_input_tokens",
        "reasoning_tokens", "cost_usd", "latency_s",
    ):
        values = [
            value for value in (old_totals.get(key), new_totals.get(key))
            if isinstance(value, (int, float))
        ]
        if values:
            totals[key] = sum(values)
    totals["agents"] = len(previous_agents) + len(current_agents)

    def lineage_item(
        run: dict[str, Any], *, kind: str, fallback_job: str = ""
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "job_id": str(run.get("job_id") or fallback_job),
            "status": str(run.get("status") or "unknown"),
            "updated_at": run.get("updated_at"),
            "agents": len(run.get("agents") or []),
            "totals": dict(run.get("totals") or {}),
        }

    lineage = [
        dict(item) for item in previous.get("session_lineage") or []
        if isinstance(item, dict)
    ]
    if not lineage:
        lineage.append(lineage_item(previous, kind="initial"))
    lineage.append(lineage_item(current, kind="continue", fallback_job=job_id))

    result["agents"] = previous_agents + current_agents
    result["edges"] = previous_edges + bridge + current_edges
    result["totals"] = totals
    result["session_lineage"] = lineage
    result["continuation_count"] = continuation_number
    if previous.get("created_at"):
        result["created_at"] = previous["created_at"]
    previous_route = _route_from_record(previous)
    current_route = _route_from_record(current)
    if previous_route:
        # A continuation is part of the same run and must never change the
        # account/API route selected when that run started.
        result["auth_route"] = previous_route
        result.setdefault("live", {})["auth_mode"] = _live_auth_mode(previous_route)
    elif current_route:
        result["auth_route"] = current_route
    return result


def _write_run_locked(run: dict[str, Any]) -> Path:
    cache = _cache_dir()
    path = cache / f"{run['run_id']}.json"
    old: dict[str, Any] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old = {}
    for k in (
        "owner_id", "owner", "created_at", "problem_id",
        "guest", "use_subagents", "subagent_model", "auth_route", "credential_source",
        "model", "requested_model", "reasoning_effort", "speed_mode", "max_iterations",
        "engine_impl", "engine_impl_version", "artifact_schema", "graph_schema",
    ):
        if run.get(k) in (None, "", "continue") and old.get(k) not in (None, "", "continue"):
            run[k] = old[k]
    if str(run.get("owner") or "").strip().lower().endswith("@public.local"):
        # Legacy guest runs predate the explicit flag; infer it from the owner.
        run["guest"] = True
    if not run.get("problems"):
        run["problems"] = list(old.get("problems") or [])
    incoming_text = str(run.get("problem_text") or "")
    old_text = str(old.get("problem_text") or "")
    if _is_engine_prompt(incoming_text):
        if old_text and not _is_engine_prompt(old_text):
            run["problem_text"] = old_text
        elif run.get("problems"):
            run["problem_text"] = _problem_item_text(run["problems"][0])
    incoming_prev = str(run.get("problem_text_preview") or "")
    old_prev = str(old.get("problem_text_preview") or "")
    if not incoming_prev or _is_engine_prompt(incoming_prev):
        if old_prev and not _is_engine_prompt(old_prev):
            run["problem_text_preview"] = old_prev
    title = _session_title(run.get("problems"), run.get("problem_text_preview") or run.get("problem_text") or "")
    if title and not _is_engine_prompt(title):
        run["problem_text_preview"] = title[:500]
    serialized = json.dumps(run, ensure_ascii=False, indent=2)
    _atomic_write_text(path, serialized)
    runs_path = RUNS_DIR / f"{run['run_id']}.json"
    _atomic_write_text(runs_path, serialized)
    _upsert_manifest(run)
    from agent_monitor import usage_ledger

    usage_ledger.record_run(run)
    return path


def _problem_preview(text: str | None) -> str:
    """Short single-line problem title for run lists (first non-empty line)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:140]
    return ""


def _is_engine_prompt(text: str | None) -> bool:
    t = (text or "").lstrip()
    if not t:
        return False
    return (
        t.startswith("You are continuing")
        or "ORIGINAL PROBLEM:" in t[:800]
        or "HUMAN FEEDBACK — address this" in t
        or "SESSION PROBLEMS:" in t[:1200]
    )


def _problem_item_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("text") or "").strip()
    return str(item or "").strip()


def _session_title(problems: Any, fallback: str = "") -> str:
    texts = [
        _problem_preview(_problem_item_text(p))
        for p in (problems or [])
        if not (
            isinstance(p, dict)
            and p.get("source") in {"human", "hitl", "feedback"}
        )
    ]
    texts = [t for t in texts if t and not _is_engine_prompt(t)]
    if not texts:
        fb = _problem_preview(fallback)
        return "" if _is_engine_prompt(fb) else fb
    if len(texts) == 1:
        return texts[0]
    return f"{texts[0]} · +{len(texts) - 1}"


def _seed_problems(text: str | None, *, source: str = "initial") -> list[dict[str, Any]]:
    t = (text or "").strip()
    if not t or _is_engine_prompt(t):
        return []
    return [{"text": t, "source": source, "ts": _now()}]


def _write_session_problems(ws: Path, problems: list[Any]) -> None:
    """Keep problem.txt as the full session notebook the agent should honor."""
    chunks: list[str] = []
    serial: list[dict[str, Any]] = []
    for i, item in enumerate(problems or [], 1):
        text = _problem_item_text(item)
        if not text:
            continue
        src = item.get("source") if isinstance(item, dict) else "initial"
        serial.append({"text": text, "source": src or "human", "n": i})
        chunks.append(f"## Problem {i}\n{text}\n")
    if not chunks:
        return
    try:
        (ws / "problem.txt").write_text("\n".join(chunks).strip() + "\n", encoding="utf-8")
        (ws / "problems.json").write_text(json.dumps(serial, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def ensure_problems(run: dict[str, Any], run_id: str | None = None) -> list[dict[str, Any]]:
    """Return the session problem list, backfilling from chat if needed."""
    existing = [
        {"text": _problem_item_text(p), "source": (p.get("source") if isinstance(p, dict) else "initial") or "initial"}
        for p in (run.get("problems") or [])
        if _problem_item_text(p)
    ]
    if existing:
        return existing
    original = str(run.get("problem_text") or run.get("problem_text_preview") or "")
    if _is_engine_prompt(original):
        original = ""
        blob = str(run.get("problem_text") or "")
        if "ORIGINAL PROBLEM:" in blob:
            original = blob.split("ORIGINAL PROBLEM:", 1)[1]
            for sep in ("\nCURRENT ", "\nHUMAN FEEDBACK", "\nSESSION PROBLEMS"):
                if sep in original:
                    original = original.split(sep, 1)[0]
            original = original.strip()
    problems = _seed_problems(original, source="initial")
    rid = run_id or run.get("run_id")
    if rid:
        seen = {p["text"] for p in problems}
        for msg in list_chat(str(rid)):
            if msg.get("role") != "user":
                continue
            text = str(msg.get("content") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            problems.append({"text": text, "source": "human", "ts": msg.get("ts") or _now()})
    return problems


def _upsert_manifest(run: dict[str, Any]) -> None:
    cache = _cache_dir()
    manifest_path = cache / "manifest.json"
    entries: list[dict] = []
    if manifest_path.exists():
        try:
            entries = list((json.loads(manifest_path.read_text(encoding="utf-8")).get("runs") or []))
        except (json.JSONDecodeError, OSError):
            entries = []
    totals = run.get("totals") or {}
    entry = {
        "run_id": run["run_id"],
        "engine": run.get("engine"),
        "trace_name": run.get("trace_name"),
        "source": run.get("source") or run.get("engine"),
        "status": run.get("status", "running"),
        "agent_count": len(run.get("agents") or []),
        "total_cost_usd": totals.get("cost_usd"),
        "problem_id": run.get("problem_id"),
        "problem_preview": _session_title(run.get("problems"), run.get("problem_text_preview") or run.get("problem_text") or ""),
        "last_ts": run.get("updated_at") or _now(),
        "owner_id": run.get("owner_id"),
        "guest": bool(run.get("guest")),
    }
    if not entry["problem_preview"]:
        # Runner flushes don't carry problem_text_preview — keep the stored one.
        entry.pop("problem_preview")
    by_id = {e.get("run_id"): e for e in entries if e.get("run_id")}
    by_id[entry["run_id"]] = {**(by_id.get(entry["run_id"]) or {}), **entry}
    payload = {"built_at": _now(), "runs": list(by_id.values())}
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _pipeline_for(engine: str) -> list[dict]:
    if engine == "math_harness":
        return [
            {"id": "scaffold", "label": "1 Scaffold", "title": "Project setup"},
            {"id": "work", "label": "2 Explore", "title": "Independent proof search"},
            {"id": "verify", "label": "3 Verify", "title": "Fact verification"},
            {"id": "curate", "label": "4 Curate", "title": "Exploring Graph"},
            {"id": "finalize", "label": "5 Synthesize", "title": "Final proof"},
        ]
    if engine == "ucla":
        return [
            {"id": "literature", "label": "1 Literature", "title": "Literature"},
            {"id": "advisor", "label": "2 Advisor", "title": "Advisor"},
            {"id": "solvers", "label": "3 Solvers", "title": "Solvers"},
            {"id": "verify", "label": "4 Verify", "title": "Verify"},
            {"id": "finalize", "label": "5 Finalize", "title": "Finalize"},
        ]
    if engine == "improof":
        return [
            {"id": "workflow", "label": "Workflow", "title": "Workflow"},
            {"id": "author", "label": "Author", "title": "Author"},
            {"id": "critic", "label": "Critic", "title": "Critic"},
            {"id": "council", "label": "Council", "title": "Council"},
            {"id": "finalize", "label": "Finalize", "title": "Finalize"},
        ]
    if engine == "hermes":
        return [
            {"id": "understand", "label": "Understand", "title": "Understand"},
            {"id": "plan", "label": "Plan", "title": "Plan"},
            {"id": "draft", "label": "Draft", "title": "Draft"},
            {"id": "verify", "label": "Verify", "title": "Verify"},
            {"id": "finalize", "label": "Finalize", "title": "Finalize"},
        ]
    # external CLI harnesses — per-model-call trace columns
    return [
        {"id": "plan", "label": "Reason", "title": "Reasoning"},
        {"id": "act", "label": "Act", "title": "Tools / commands"},
        {"id": "write", "label": "Write", "title": "Proof writing"},
        {"id": "work", "label": "Agent Session", "title": "Agent working"},
        {"id": "finalize", "label": "Finalize", "title": "Finalize"},
    ]


def list_jobs(owner_id: int | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        jobs = [dict(j) for j in _JOBS.values() if j.get("run_id") not in _DELETED_RUNS]
    if owner_id is not None:
        jobs = [j for j in jobs if j.get("owner_id") == owner_id]
    for j in jobs:
        if j.get("problem_preview"):
            continue
        try:
            rec = _load_run_record(j["run_id"])
            j["problem_preview"] = _session_title(
                rec.get("problems"), rec.get("problem_text_preview") or rec.get("problem_text") or ""
            )
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
    return jobs


def persisted_run_count(owner_id: int) -> int:
    """Count retained run records for one owner without double-counting copies."""
    cache = _cache_dir()
    with _RUN_WRITE_LOCK:
        manifest_path = cache / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("manifest root is not an object")
            raw_entries = payload.get("runs") or []
            if not isinstance(raw_entries, list):
                raise TypeError("manifest runs is not a list")
            return sum(
                isinstance(entry, dict) and entry.get("owner_id") == owner_id
                for entry in raw_entries
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            count = 0
            for path in cache.glob("*.json"):
                if path.name == "manifest.json":
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if isinstance(record, dict) and record.get("owner_id") == owner_id:
                    count += 1
            return count


def run_owner(run_id: str) -> int | None:
    """Owner user id recorded on a run (None for legacy/ownerless runs)."""
    try:
        return _load_run_record(run_id).get("owner_id")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        j = _JOBS.get(job_id)
        return dict(j) if j else None


def _validated_problem_id(problem_id: str) -> str:
    """Return a filesystem-safe ID suitable for run and cache filenames."""
    value = str(problem_id or "").strip()
    if value in {".", ".."} or not _PROBLEM_ID_RE.fullmatch(value):
        raise ValueError("problem_id contains unsupported characters")
    return value


def _manifest_problem_path(statement_path: object) -> Path:
    """Resolve a manifest entry without permitting escape from PROBLEMS_DIR."""
    root = PROBLEMS_DIR.resolve()
    candidate = (root / str(statement_path or "")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("problem manifest path leaves the problem directory") from None
    if not candidate.is_file():
        raise FileNotFoundError(f"Problem statement is missing: {statement_path}")
    return candidate


def resolve_problem(problem_id: str | None, problem_text: str | None) -> tuple[str, str, Path | None]:
    """Return (problem_id, text, path_or_none)."""
    if problem_text and problem_text.strip():
        pid = (
            _validated_problem_id(problem_id)
            if problem_id
            else f"adhoc_{uuid.uuid4().hex[:8]}"
        )
        return pid, problem_text.strip(), None

    if not problem_id:
        raise ValueError("Provide problem_id or problem_text")
    safe_problem_id = _validated_problem_id(problem_id)

    man = PROBLEMS_DIR / "manifest.json"
    if man.exists():
        for p in json.loads(man.read_text(encoding="utf-8")).get("problems") or []:
            if isinstance(p, dict) and p.get("problem_id") == safe_problem_id:
                path = _manifest_problem_path(p.get("statement_path"))
                return (
                    safe_problem_id,
                    path.read_text(encoding="utf-8", errors="replace"),
                    path,
                )

    # Direct statement file under problems/. The validated ID cannot contain a
    # separator or traversal segment, so this stays inside the problem root.
    cand = PROBLEMS_DIR / safe_problem_id
    if cand.is_file():
        return cand.stem, cand.read_text(encoding="utf-8", errors="replace"), cand

    raise FileNotFoundError(f"Unknown problem: {safe_problem_id}")


def _is_kimi_model(model: str | None) -> bool:
    """Return whether the UI selection requires the Kimi API connection."""
    return str(model or "").strip().lower().startswith("kimi-")


def _route_from_record(run: dict[str, Any] | None) -> str | None:
    """Recover the immutable billing/runtime route recorded for a run."""
    record = run or {}
    route = str(record.get("auth_route") or "").strip().lower()
    if route in _AUTH_ROUTES:
        return route
    live_mode = str((record.get("live") or {}).get("auth_mode") or "").strip().lower()
    return {
        "api_key": "api_key",
        "chatgpt_subscription": "codex_subscription",
        "codex_subscription": "codex_subscription",
        "claude_subscription": "claude_subscription",
    }.get(live_mode)


def _live_auth_mode(route: str) -> str:
    """Return the public live-state label for one immutable run route."""
    return {
        "api_key": "api_key",
        "codex_subscription": "chatgpt_subscription",
        "claude_subscription": "claude_subscription",
    }[route]


def _configure_auth_route(
    env: dict[str, str],
    *,
    route: str,
    user_id: int | None,
) -> dict[str, str]:
    """Apply exactly one account/API route to a child-process environment."""
    from agent_monitor.subprocess_env import scrub_control_plane_env

    if route not in _AUTH_ROUTES:
        raise ValueError(f"Unsupported authentication route: {route}")
    configured = dict(env)
    configured.pop("AGENT_MONITOR_CODEX_SUBSCRIPTION", None)
    configured.pop("AGENT_MONITOR_CLAUDE_SUBSCRIPTION", None)
    configured.pop("AGENT_MONITOR_CODEX_MODEL", None)
    configured.pop("AGENT_MONITOR_CLAUDE_MODEL", None)
    configured.pop("CODEX_HOME", None)
    configured.pop("CLAUDE_CONFIG_DIR", None)
    runtime = {
        "api_key": "api", "codex_subscription": "codex", "claude_subscription": "claude"
    }[route]
    configured["AGENT_MONITOR_ACCOUNT_RUNTIME"] = runtime
    configured["AGENT_MONITOR_USE_CODEX"] = "1" if runtime == "codex" else "0"
    configured["AGENT_MONITOR_USE_CLAUDE"] = "1" if runtime == "claude" else "0"

    if route == "api_key":
        configured["AGENT_MONITOR_AUTH_MODE"] = "api_key"
        scrub_control_plane_env(configured)
        return configured
    _scrub_provider_route_env(configured)
    if user_id is None:
        raise ValueError("A signed-in ProvingConsole account is required for subscription mode")

    if route == "codex_subscription":
        from agent_monitor import codex_login

        home = codex_login.account_home(int(user_id))
        if not codex_login.account_login_ready(home):
            raise ValueError("Codex is not connected for this account; connect it in Settings")
        configured["CODEX_HOME"] = str(home)
        configured["AGENT_MONITOR_CODEX_SUBSCRIPTION"] = "1"
        configured["AGENT_MONITOR_AUTH_MODE"] = "chatgpt_subscription"
        scrub_control_plane_env(configured)
        return configured

    from agent_monitor import claude_login

    home = claude_login.account_home(int(user_id))
    if not claude_login.account_login_ready(home):
        raise ValueError("Claude Code is not connected for this account; connect it in Settings")
    configured = claude_login.account_environment(home, configured)
    configured["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"] = "1"
    configured["AGENT_MONITOR_AUTH_MODE"] = "claude_subscription"
    scrub_control_plane_env(configured)
    return configured


def _selected_auth_route(
    *,
    requested: str | None,
    engine: str,
    model: str | None,
    auth_modes: set[str],
    user: dict[str, Any] | None,
    env: dict[str, str],
    enforce_account_runtime: bool = True,
) -> tuple[str, bool]:
    """Resolve the route and report whether the caller selected it explicitly."""
    from agent_monitor.settings import account_runtime

    explicit = bool(str(requested or "").strip())
    if explicit:
        route = str(requested).strip().lower()
        if route not in _AUTH_ROUTES:
            raise ValueError(f"Unsupported authentication route: {route}")
        has_persisted_runtime = bool(
            str(env.get("AGENT_MONITOR_ACCOUNT_RUNTIME") or "").strip()
            or "AGENT_MONITOR_USE_CLAUDE" in env
            or "AGENT_MONITOR_USE_CODEX" in env
        )
        persisted_runtime = account_runtime(user, env=env) if has_persisted_runtime else ""
        expected_route = {
            "api": "api_key",
            "codex": "codex_subscription",
            "claude": "claude_subscription",
        }.get(persisted_runtime)
        if enforce_account_runtime and expected_route and route != expected_route:
            raise ValueError(
                f"Requested {route.replace('_', ' ')} conflicts with the selected "
                f"{persisted_runtime} account runtime; refresh the page or switch "
                "the runtime in Settings"
            )
        return route, True
    preferred = account_runtime(user, env=env)
    if preferred == "claude":
        if (
            engine in _CLAUDE_SUBSCRIPTION_ENGINES
            and "claude_subscription" in auth_modes
        ):
            return "claude_subscription", False
        raise ValueError(
            f"{engine} is not compatible with the selected Claude Code subscription; "
            "switch the account runtime to API or choose a compatible harness"
        )
    if preferred == "codex":
        if (
            engine in _CODEX_SUBSCRIPTION_ENGINES
            and "codex_subscription" in auth_modes
        ):
            return "codex_subscription", False
        raise ValueError(
            f"{engine} is not compatible with the selected Codex subscription; "
            "switch the account runtime to API or choose a compatible harness"
        )
    if "api_key" not in auth_modes:
        raise ValueError(
            f"{engine} is not compatible with the selected API-provider runtime; "
            "switch the account runtime or choose a compatible harness"
        )
    return "api_key", False


def _validate_subscription_model(
    *,
    route: str,
    model: str | None,
    user: dict[str, Any] | None,
    field: str,
) -> None:
    """Reject stale/cross-runtime models before any account runner starts."""
    selected = str(model or "").strip()
    if not selected or route == "api_key":
        return
    if route == "claude_subscription":
        from agent_monitor.claude_login import DEFAULT_CLAUDE_MODELS

        allowed = list(DEFAULT_CLAUDE_MODELS)
        label = "Claude Code subscription"
    else:
        from agent_monitor import codex_login

        owner_id = (user or {}).get("id")
        allowed = (
            list(codex_login.available_models(int(owner_id)))
            if owner_id is not None
            else []
        )
        label = "Codex subscription"
    if selected not in allowed:
        choices = (
            ", ".join(allowed)
            if allowed
            else "a model exposed by the connected account"
        )
        if route == "claude_subscription":
            prefix = (
                "Unsupported Claude account model"
                if field == "Selected model"
                else "Unsupported Claude subagent model"
            )
        else:
            prefix = (
                "Unsupported Codex account model"
                if field == "Selected model"
                else "Unsupported Codex subagent model"
            )
        raise ValueError(
            f"{prefix}: {selected} is not a {label} model; choose {choices}"
        )


def start_job(
    *,
    engine: str,
    problem_id: str | None = None,
    problem_text: str | None = None,
    model: str | None = None,
    max_iterations: int = 40,
    max_output_tokens: int | None = None,
    user: dict[str, Any] | None = None,
    use_subagents: bool = True,
    subagent_model: str | None = None,
    auth_route: str | None = None,
    reasoning_effort: str | None = None,
    speed_mode: str | None = None,
    attachments: object = None,
) -> dict[str, Any]:
    from agent_monitor.engines_registry import all_engine_ids, list_engines, supported_models

    is_guest = bool(user and user.get("guest"))
    if engine not in all_engine_ids():
        raise ValueError(f"Unsupported engine: {engine}")
    # Guests run only the two tool-free engines, never a coding-agent CLI, and
    # never with subagents or a command override that escapes that contract.
    if is_guest:
        if engine not in _GUEST_ENGINES:
            raise ValueError("That engine requires a signed-in account")
        use_subagents = False
        subagent_model = None
        # A guest has no subscription to consume, so pin the billing route.
        auth_route = "api_key"
        override = _GUEST_COMMAND_OVERRIDES.get(engine)
        if override and os.environ.get(override, "").strip():
            raise ValueError(
                f"Guest {engine} is unavailable while {override} is overridden"
            )
    if engine == "kimi":
        from agent_monitor.settings import GUEST_DEFAULT_MODEL

        model = model or GUEST_DEFAULT_MODEL
        if model != GUEST_DEFAULT_MODEL:
            raise ValueError(f"Kimi Proof uses {GUEST_DEFAULT_MODEL}")

    extra_env = _user_extra_env(user, model, subagent_model)
    if is_guest:
        # Two ceilings, one per transport: the Plain/Kimi harnesses read the
        # PLAIN variable, and api_backend clamps every API call with the other.
        try:
            output_limit = int(max_output_tokens or 4096)
        except (TypeError, ValueError):
            output_limit = 4096
        output_limit = max(256, min(output_limit, 8192))
        extra_env = dict(extra_env)
        extra_env["AGENT_MONITOR_PLAIN_MAX_OUTPUT_TOKENS"] = str(output_limit)
        extra_env["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] = str(output_limit)
    elif max_output_tokens:
        extra_env["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] = str(int(max_output_tokens))
    if is_guest and not extra_env.get("KIMI_API_KEY"):
        # Guests inherit no operator credentials, so the only key they may use
        # is the dedicated hosted guest one (when the operator configured it).
        from agent_monitor.settings import hosted_guest_kimi_key

        hosted_key = hosted_guest_kimi_key()
        if hosted_key:
            extra_env["KIMI_API_KEY"] = hosted_key
            extra_env.setdefault("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    engine_info = next((item for item in list_engines() if item.get("id") == engine), {})
    if engine_info and not engine_info.get("available"):
        detail = engine_info.get("health_detail") or engine_info.get("hint") or "runtime unavailable"
        raise ValueError(
            f"{engine_info.get('label') or engine} needs setup — {detail}"
        )

    auth_modes = set(engine_info.get("auth_modes") or [])
    supports_api = "api_key" in auth_modes
    selected_kimi = _is_kimi_model(model)
    if is_guest and selected_kimi and not extra_env.get("KIMI_API_KEY") and os.environ.get("AGENT_MONITOR_GUEST_HOSTED_KIMI", "1").lower() in {"0", "false", "off", "no"}:
        raise ValueError("Hosted Kimi is disabled; add your own Kimi API key in Settings")
    if engine == "kimi" and is_guest and not extra_env.get("KIMI_API_KEY"):
        from agent_monitor.settings import hosted_guest_kimi_key
        from agent_monitor import sponsored_kimi_client
        if not sponsored_kimi_client.configured():
            key = hosted_guest_kimi_key()
            if key:
                extra_env["KIMI_API_KEY"] = key
    sponsored_kimi = _apply_sponsored_kimi_route(
        extra_env, engine="plain" if engine == "kimi" else engine, model=model
    )
    if engine == "kimi" and not sponsored_kimi:
        extra_env["KIMI_API_BASE"] = "https://api.moonshot.ai/v1"
        extra_env["KIMI_BASE_URL"] = "https://api.moonshot.ai/v1"
    has_kimi_key = bool(str(extra_env.get("KIMI_API_KEY") or "").strip())
    if selected_kimi and not supports_api:
        raise ValueError(f"{engine_info.get('label') or engine} does not support API models")
    if selected_kimi and not (has_kimi_key or sponsored_kimi):
        raise ValueError("Kimi K3 requires a configured own key or sponsored route")
    has_api_key = (has_kimi_key or sponsored_kimi) if selected_kimi else any(
        k.endswith("_API_KEY") and bool(str(v or "").strip())
        for k, v in extra_env.items()
    )
    route, _route_was_explicit = _selected_auth_route(
        requested=auth_route,
        engine=engine,
        model=model,
        auth_modes=auth_modes,
        user=user,
        env=extra_env,
    )
    if route not in auth_modes:
        raise ValueError(
            f"{engine_info.get('label') or engine} does not support "
            f"{route.replace('_', ' ')}"
        )
    if route == "claude_subscription" and engine not in _CLAUDE_SUBSCRIPTION_ENGINES:
        raise ValueError(
            f"{engine_info.get('label') or engine} has no Claude Code account adapter; "
            "choose API or a compatible harness"
        )
    if route == "codex_subscription" and engine not in _CODEX_SUBSCRIPTION_ENGINES:
        raise ValueError(
            f"{engine_info.get('label') or engine} has no Codex account adapter; "
            "choose API or a compatible harness"
        )
    if route != "api_key" and selected_kimi:
        raise ValueError("Kimi K3 is an API model and cannot use a linked account subscription")
    if route == "api_key" and user is not None and not has_api_key:
        raise ValueError("No compatible API key is configured; add and verify one in Settings")

    # Validate both the main and delegated model against the selected billing
    # route. A stale API model in either field must never flip a subscription
    # run back to a provider key.
    if route in {"claude_subscription", "codex_subscription"}:
        _validate_subscription_model(
            route=route, model=model, user=user, field="Selected model"
        )
    if use_subagents and subagent_model:
        _validate_subscription_model(
            route=route,
            model=subagent_model,
            user=user,
            field="Subagent model",
        )

    allowed_models = supported_models(engine)
    if route != "api_key" and model and allowed_models and model not in allowed_models:
        raise ValueError(
            f"{engine} does not support {model} with its account adapter; "
            f"choose one of: {', '.join(allowed_models)}"
        )
    from agent_monitor import run_tuning

    tuning = run_tuning.normalize(
        model,
        "sponsored_kimi" if sponsored_kimi else route,
        reasoning_effort=reasoning_effort,
        speed_mode=speed_mode,
        engine=engine,
    )
    extra_env = _configure_auth_route(
        extra_env,
        route=route,
        user_id=(user or {}).get("id"),
    )
    # Carry the UI toggle into every external harness. Engines without an
    # internal delegation tool ignore it; DeepAgents uses it to remove `task`.
    extra_env["AGENT_MONITOR_USE_SUBAGENTS"] = "1" if use_subagents else "0"
    if subagent_model:
        extra_env["AGENT_MONITOR_SUBAGENT_MODEL"] = subagent_model
    else:
        extra_env.pop("AGENT_MONITOR_SUBAGENT_MODEL", None)

    attachment_batch = research_attachments.validate(attachments)
    if attachment_batch and not (problem_text or "").strip() and not problem_id:
        problem_text = (
            "Use the attached research findings to investigate the problem and "
            "develop a rigorous proof."
        )
    pid, text, path = resolve_problem(problem_id, problem_text)

    job_id = uuid.uuid4().hex[:10]
    run_id = f"{engine}_{pid}_{job_id}"
    if sponsored_kimi and engine != "plain":
        from agent_monitor import sponsored_kimi_client

        extra_env.update(
            sponsored_kimi_client.issue_credentials(
                engine=_sponsored_transport_engine(engine),
                client_id=(user or {}).get("id"),
                cache_key=run_id,
                minimum_ttl_seconds=(600 if engine == "kimi" else _cli_timeout_seconds(engine, extra_env) + _SPONSORED_KIMI_TOKEN_MARGIN),
            )
        )
        # The harness reads KIMI_BASE_URL; the broker issues KIMI_API_BASE.
        if str(extra_env.get("KIMI_API_BASE") or "").strip():
            extra_env["KIMI_BASE_URL"] = str(extra_env["KIMI_API_BASE"]).rstrip("/")
    run_tuning.apply_environment(
        extra_env,
        model=model,
        auth_route=route,
        **tuning,
    )
    _stop_event(run_id).clear()
    ws = workspace_dir(run_id)
    attached = research_attachments.save(ws, attachment_batch)
    (ws / "problem.txt").write_text(text, encoding="utf-8")
    run = normalize_run(
        {
            "run_id": run_id,
            "job_id": job_id,
            "problem_id": pid,
            "trace_name": f"[{engine.upper()}] {pid}",
            "status": "running",
            "created_at": _now(),
            "updated_at": _now(),
            "workspace": str(ws),
            "pipeline": _pipeline_for(engine),
            "agents": [
                {
                    "trace_id": f"{run_id}::bootstrap",
                    "stage_name": "bootstrap",
                    "role": "system",
                    "pipeline_stage": _pipeline_for(engine)[0]["id"],
                    "prompt": text[:4000],
                    "output": f"Starting {engine} engine…",
                    "status": "running",
                }
            ],
            "edges": [],
            "totals": {"cost_usd": 0, "latency_s": 0},
            "problem_text": text[:20000],
            "problem_text_preview": text[:500],
            "problems": _seed_problems(text, source="initial"),
            "owner_id": user.get("id") if user else None,
            "owner": user.get("email") if user else None,
            "guest": is_guest,
            "model": model,
            "requested_model": model,
            "max_iterations": max_iterations,
            "guest": is_guest,
            "live": {
                "model": model,
                "model_requested": bool(model),
                "requested_model": model,
                "auth_mode": _live_auth_mode(route),
            },
            "use_subagents": bool(use_subagents),
            "subagent_model": subagent_model if use_subagents else None,
            **tuning,
            "auth_route": route,
            "credential_source": "sponsored_kimi" if sponsored_kimi else "user",
        },
        engine=engine,  # type: ignore[arg-type]
    )
    _write_run(run)

    if attached:
        # Record the opening turn so the transcript shows which files the run
        # started from; the worker's own events follow it.
        _append_chat(run_id, "user", text, attachments=attached)

    job = {
        "job_id": job_id,
        "run_id": run_id,
        "engine": engine,
        "problem_id": pid,
        "status": "running",
        "created_at": _now(),
        "updated_at": _now(),
        "error": None,
        "model": model,
        "owner_id": user.get("id") if user else None,
        "guest": is_guest,
        "use_subagents": use_subagents,
        "subagent_model": subagent_model,
        **tuning,
        "auth_route": route,
        "credential_source": "sponsored_kimi" if sponsored_kimi else "user",
        "problem_preview": _problem_preview(text),
    }
    with _LOCK:
        _JOBS[job_id] = job

    thread = threading.Thread(
        target=_execute_job,
        kwargs={
            "job_id": job_id,
            "run_id": run_id,
            "engine": engine,
            "problem_id": pid,
            "problem_text": text,
            "problem_path": str(path) if path else None,
            "model": model,
            "max_iterations": max_iterations,
            "workspace": str(ws),
            "extra_env": extra_env,
            "use_subagents": use_subagents,
            "subagent_model": subagent_model,
            **tuning,
            "owner_id": user.get("id") if user else None,
            "guest": is_guest,
        },
        daemon=True,
        name=f"engine-{engine}-{job_id}",
    )
    thread.start()

    # The proof engine and derived views are independent background concerns.
    # Start the sidecar now; it waits for a stable draft (the completed proof
    # for guests), then derives Lean and both DAG views from that proof.
    # Lightweight Thread doubles used by embedders/tests intentionally do not
    # launch background work and therefore have no is_alive method.
    if (
        hasattr(thread, "is_alive")
        and not is_guest
        and os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "").strip() != "1"
    ):
        from agent_monitor import auto_pipeline

        auto_pipeline.start(
            run_id=run_id,
            workspace=ws,
            owner_id=user.get("id") if user else None,
            model=model,
            engine=engine,
        )
    return dict(job)


def _user_extra_env(
    user: dict[str, Any] | None,
    *models: str | None,
) -> dict[str, str]:
    """Return ordinary settings plus only the selected models' provider routes."""
    env = {"AGENT_MONITOR_PYTHON": sys.executable}
    if user is None:
        # Ownerless local/CLI runs may intentionally use the invoking process's
        # provider configuration.  Make that trust decision explicit here so
        # `_run_cli_engine` can scrub its inherited environment for every run
        # and then overlay only this selected set.  Account-owned web runs never
        # take this branch.
        env.update(
            {
                name: value
                for name in _PROVIDER_ROUTE_ENV_NAMES
                if (value := str(os.environ.get(name) or "").strip())
            }
        )
        from agent_monitor.subprocess_env import project_provider_env

        requested_models = (
            models if any(str(model or "").strip() for model in models)
            else (env.get("AGENT_MONITOR_MODEL"),)
        )
        return dict(project_provider_env(env, requested_models))
    from agent_monitor.settings import resolved_user_env

    env.update(resolved_user_env(user))
    from agent_monitor.subprocess_env import project_provider_env

    requested_models = (
        models if any(str(model or "").strip() for model in models)
        else (env.get("AGENT_MONITOR_MODEL"),)
    )
    return dict(project_provider_env(env, requested_models))


def _continuation_auth_environment(
    *,
    run_data: dict[str, Any],
    engine: str,
    model: str | None,
    user: dict[str, Any] | None,
    env: dict[str, str],
) -> tuple[dict[str, str], str]:
    """Preserve a run's original account/API route across continuations."""
    from agent_monitor.engines_registry import list_engines

    engine_info = next((item for item in list_engines() if item.get("id") == engine), {})
    auth_modes = set(engine_info.get("auth_modes") or [])
    recorded_route = _route_from_record(run_data)
    legacy_unpinned = recorded_route is None
    sponsored_run = (
        str(run_data.get("credential_source") or "").strip() == "sponsored_kimi"
    )
    route, _explicit = _selected_auth_route(
        requested=recorded_route,
        engine=engine,
        model=model,
        auth_modes=auth_modes,
        user=user,
        env=env,
        enforce_account_runtime=False,
    )
    if route not in auth_modes:
        raise ValueError(
            f"This run used {route.replace('_', ' ')}, which {engine} no longer supports"
        )
    if route == "claude_subscription" and engine not in _CLAUDE_SUBSCRIPTION_ENGINES:
        raise ValueError(
            f"{engine} has no Claude Code account adapter; "
            "the run cannot silently switch providers"
        )
    if route == "claude_subscription":
        _validate_subscription_model(
            route=route,
            model=model,
            user=user,
            field="Selected model",
        )
    if route == "codex_subscription" and engine not in _CODEX_SUBSCRIPTION_ENGINES:
        raise ValueError(
            f"{engine} has no Codex account adapter; "
            "the run cannot silently switch providers"
        )
    if route != "api_key" and _is_kimi_model(model):
        raise ValueError("A Kimi continuation must keep the run on its API route")
    if route == "api_key":
        selected_kimi = _is_kimi_model(model)
        if selected_kimi and sponsored_run:
            _apply_sponsored_kimi_route(
                env, engine=engine, model=model, force=True
            )
        has_key = bool(str(env.get("KIMI_API_KEY") or "").strip())
        if selected_kimi and not (has_key or sponsored_run):
            raise ValueError("Kimi K3 requires its recorded sponsored route or own key")
        if (
            not legacy_unpinned
            and user is not None
            and not sponsored_run
            and not any(
                key.endswith("_API_KEY") and bool(str(value or "").strip())
                for key, value in env.items()
            )
        ):
            raise ValueError("The API connection originally used by this run is unavailable")
    owner_id = (user or {}).get("id") or run_data.get("owner_id")
    try:
        configured = _configure_auth_route(env, route=route, user_id=owner_id)
    except ValueError:
        # Records created before auth_route/live.auth_mode existed are
        # ambiguous. Preserve their historical behavior exactly once: use the
        # currently selected linked account when it is still connected,
        # otherwise pin the continuation to API. A recorded route never enters
        # this compatibility branch and therefore always fails closed.
        if not legacy_unpinned or route == "api_key" or "api_key" not in auth_modes:
            raise
        route = "api_key"
        configured = _configure_auth_route(env, route=route, user_id=None)
    if sponsored_run and _is_kimi_model(model) and engine != "plain":
        from agent_monitor import sponsored_kimi_client

        configured.update(
            sponsored_kimi_client.issue_credentials(
                engine=_sponsored_transport_engine(engine),
                client_id=owner_id,
                cache_key=str(run_data.get("run_id") or "continuation"),
                minimum_ttl_seconds=(600 if engine == "kimi" else _cli_timeout_seconds(engine, configured) + _SPONSORED_KIMI_TOKEN_MARGIN),
            )
        )
    from agent_monitor.run_tuning import from_record as apply_recorded_tuning

    apply_recorded_tuning(configured, run_data)
    return configured, route


def _apply_kimi_compatible_env(env: dict[str, str], model: str | None) -> None:
    """Expose a selected Kimi connection to OpenAI-compatible harness SDKs.

    These aliases live only in the run subprocess environment; the user's
    stored OpenAI connection is never replaced.
    """
    if not str(model or "").strip().lower().startswith("kimi-"):
        return
    key = str(env.get("KIMI_API_KEY") or "").strip()
    if not key:
        return
    base = str(
        env.get("KIMI_API_BASE")
        or "https://2qg3r7w8aefiukbds.flashflame.ai/v1"
    ).rstrip("/")
    env["OPENAI_API_KEY"] = key
    env["OPENAI_BASE_URL"] = base
    env["OPENAI_API_BASE"] = base
    env["AGENT_MONITOR_BASE_URL"] = base
    # The sponsored broker issues KIMI_API_BASE; the tool-free Kimi harness
    # reads KIMI_BASE_URL. Mirror the broker route so both agree, and leave an
    # explicitly configured KIMI_BASE_URL untouched otherwise.
    if str(env.get("KIMI_API_BASE") or "").strip():
        env["KIMI_BASE_URL"] = base
    env["LLM_API_KEY"] = key
    env["LLM_BASE_URL"] = base


def _update_job(job_id: str, **fields: Any) -> None:
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)
            _JOBS[job_id]["updated_at"] = _now()


def _record_run_memory(
    *,
    guest: bool,
    run_id: str,
    engine: str,
    problem_id: str,
    status: str,
    problem_text: str,
    outcome: str,
    final_out: str,
) -> None:
    """Best-effort shared-memory update for registered-account runs only.

    Anonymous guest input and output must never mutate the operator's library.
    """
    if guest:
        return
    try:
        from agent_monitor import library as user_library

        user_library.record_run_memory(
            run_id=run_id,
            engine=engine,
            problem_id=problem_id,
            status=status,
            summary=(
                f"Problem: {problem_text[:600]}\n\n"
                f"Outcome: {outcome}\n\n"
                f"Final answer (excerpt):\n{final_out[:1200]}"
            ),
        )
    except Exception:  # noqa: BLE001
        pass


def _execute_job(
    *,
    job_id: str,
    run_id: str,
    engine: str,
    problem_id: str,
    problem_text: str,
    problem_path: str | None,
    model: str | None,
    max_iterations: int,
    workspace: str,
    extra_env: dict[str, str] | None = None,
    use_subagents: bool = True,
    subagent_model: str | None = None,
    reasoning_effort: str = "default",
    speed_mode: str = "standard",
    owner_id: int | None = None,
    guest: bool = False,
) -> None:
    started = time.time()
    ws = Path(workspace)
    extra_env = dict(extra_env or {})
    public_kimi_runtime = (
        os.environ.get("AGENT_MONITOR_PUBLIC_KIMI", "").strip() == "1"
    )
    sponsored_kimi_runtime = (
        extra_env.get("AGENT_MONITOR_SPONSORED_KIMI", "").strip() == "1"
    )
    _apply_kimi_compatible_env(extra_env, model)
    _append_chat(run_id, "system", f"▶ run started · engine {engine} · problem {problem_id}" + (f" · model {model}" if model else "") + (f" · subagent model {subagent_model}" if subagent_model else ""))
    # Inject the user library (memory / skills / tools) into every engine run:
    # files land in <ws>/_library/ and the prompt gets a USER LIBRARY block.
    lib_ctx = ""
    runtime_library: dict[str, Any] = {
        "materialized": [],
        "enabled_skills": [],
        "enabled_tools": [],
        "persistent_skill_mutation": False,
    }
    if not guest:
        try:
            if public_kimi_runtime or sponsored_kimi_runtime:
                raise LookupError("user library disabled for isolated Kimi service")
            from agent_monitor import library as user_library

            materialized = user_library.materialize(ws)
            lib_ctx = user_library.compose_context()
            library_snapshot = user_library.get_library()
            runtime_library = {
                "materialized": list(materialized.get("written") or []),
                "enabled_skills": [
                    item.get("name") for item in library_snapshot.get("items") or []
                    if item.get("type") == "skill" and item.get("enabled", True)
                ],
                "enabled_tools": [
                    item.get("name") for item in library_snapshot.get("items") or []
                    if item.get("type") == "tool" and item.get("enabled", True)
                ],
                "persistent_skill_mutation": False,
            }
            if materialized.get("written"):
                _append_chat(
                    run_id, "system",
                    "library injected: " + ", ".join(materialized["written"]),
                )
        except Exception:  # noqa: BLE001
            lib_ctx = ""
    if not guest and engine == "hermes":
        # Native Hermes reads profile skills from HERMES_HOME, but its linked
        # Codex app-server is workspace-confined.  Materialize the same package
        # paths advertised to portable harnesses so either execution route can
        # honor an explicit `_agent/skills/<name>/SKILL.md` audit request.
        try:
            from agent_monitor.agent_config import materialize_enabled_skills

            visible_skills = materialize_enabled_skills(ws)
            if visible_skills:
                _append_chat(
                    run_id,
                    "system",
                    "profile skills materialized: "
                    + ", ".join(sorted(visible_skills)),
                )
        except Exception as exc:  # noqa: BLE001
            _append_chat(run_id, "system", f"profile skill materialization unavailable: {exc}")
    # Only explicitly published, provenance-gated DAG results are retrievable;
    # community discussions never become executable harness instructions.
    if not guest and not public_kimi_runtime and not sponsored_kimi_runtime:
        try:
            from agent_monitor.research import compose_context as dag_context

            research_context = dag_context(problem_text)
            if research_context:
                lib_ctx = (lib_ctx + "\n\n" + research_context).strip()
                (ws / "dag_memory.md").write_text(research_context, encoding="utf-8")
                runtime_library["dag_memory"] = "dag_memory.md"
        except (OSError, ValueError, sqlite3.Error):
            pass
    engine_problem_text = (lib_ctx + "\n" + problem_text) if lib_ctx else problem_text
    attachment_context = research_attachments.context(ws)
    if attachment_context:
        engine_problem_text += "\n\n" + attachment_context
    engine_problem_text = _augment_research_audit_prompt(engine_problem_text)
    if not guest and engine in {"improof", "ucla"}:
        try:
            from agent_monitor.agent_config import persona_preamble

            profile_ctx = persona_preamble(workspace=ws)
        except Exception:  # noqa: BLE001
            profile_ctx = ""
        if profile_ctx:
            engine_problem_text = profile_ctx + "\n" + engine_problem_text
            _append_chat(run_id, "system", "native SKILL.md profile injected into this harness")
    try:
        if (
            (extra_env or {}).get("AGENT_MONITOR_SPONSORED_KIMI") == "1"
            and engine == "plain"
        ):
            result_run = _run_sponsored_kimi_plain(
                run_id=run_id,
                problem_id=problem_id,
                problem_text=engine_problem_text,
                display_problem_text=problem_text,
                started=started,
                workspace=ws,
                owner_id=owner_id,
            )
        elif engine == "hermes":
            result_run = _run_hermes(
                run_id=run_id,
                problem_id=problem_id,
                problem_text=engine_problem_text,
                display_problem_text=problem_text,
                model=model,
                max_iterations=max_iterations,
                started=started,
                workspace=ws,
                extra_env=extra_env,
                use_subagents=use_subagents,
                subagent_model=subagent_model,
                owner_id=owner_id,
            )
        elif engine == "improof":
            from agent_monitor.runners import improof as improof_runner

            path = ws / "problem.txt"
            # Library context and research attachments both augment the prompt;
            # either one means the engine must read the augmented file.
            if engine_problem_text != problem_text:
                path = ws / "problem_with_library.txt"
                path.write_text(engine_problem_text, encoding="utf-8")
            improof_args = None
            if model and (extra_env or {}).get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
                improof_args = [
                    "--component", f"cfg_codex_author.model={model}",
                    "--component", f"cfg_codex_critic.model={model}",
                ]
            elif model and (extra_env or {}).get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
                improof_args = [
                    "--component", f"cfg_claude_author.model={model}",
                    "--component", f"cfg_claude_critic.model={model}",
                ]
            elif str(model or "").lower().startswith("kimi-"):
                improof_args = ["--model", "*=models/kimi/k3"]
            result = improof_runner.run_problem(
                path,
                problem_id=problem_id,
                output_dir=ws,
                extra_args=improof_args,
                extra_env=extra_env,
                research_model=model,
                on_start=lambda p: _register_proc(run_id, p),
                on_output=_live_output_flusher(
                    run_id=run_id, engine="improof", problem_id=problem_id,
                    problem_text=problem_text, started=started, workspace=ws,
                ),
            )
            _unregister_proc(run_id)
            result_run = _wrap_subprocess_result(
                run_id=run_id,
                engine="improof",
                problem_id=problem_id,
                problem_text=problem_text,
                result=result,
                started=started,
                workspace=ws,
            )
        elif engine == "ucla":
            from agent_monitor.runners import ucla as ucla_runner

            path = ws / "problem.txt"
            # Library context and research attachments both augment the prompt;
            # either one means the engine must read the augmented file.
            if engine_problem_text != problem_text:
                path = ws / "problem_with_library.txt"
                path.write_text(engine_problem_text, encoding="utf-8")
            result = ucla_runner.run_problem(
                path,
                problem_id=problem_id,
                model=model,
                output_dir=ws,
                extra_env=extra_env,
                on_start=lambda p: _register_proc(run_id, p),
                on_output=_live_output_flusher(
                    run_id=run_id, engine="ucla", problem_id=problem_id,
                    problem_text=problem_text, started=started, workspace=ws,
                ),
            )
            _unregister_proc(run_id)
            result_run = _wrap_subprocess_result(
                run_id=run_id,
                engine="ucla",
                problem_id=problem_id,
                problem_text=problem_text,
                result=result,
                started=started,
                workspace=ws,
            )
        else:
            result_run = _run_cli_engine_with_openclaude_retry(
                run_id=run_id,
                engine=engine,
                problem_id=problem_id,
                problem_text=engine_problem_text,
                started=started,
                workspace=ws,
                display_problem_text=problem_text,
                extra_env=extra_env,
                requested_model=model,
                max_iterations=max_iterations,
                owner_id=owner_id,
                include_persona=not guest,
            )

        if _stop_event(run_id).is_set():
            result_run["status"] = "stopped"
        if (extra_env or {}).get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
            result_run.setdefault("live", {})["auth_mode"] = "claude_subscription"
            final_route = "claude_subscription"
        elif (extra_env or {}).get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
            result_run.setdefault("live", {})["auth_mode"] = "chatgpt_subscription"
            final_route = "codex_subscription"
        else:
            final_route = "api_key"
        effective_result_model = str(
            (result_run.get("live") or {}).get("model") or model or ""
        ).strip()
        if effective_result_model:
            result_run["model"] = effective_result_model
            result_run.setdefault("live", {}).setdefault(
                "model", effective_result_model
            )
            if model:
                result_run.setdefault("live", {})["model_requested"] = True
        if model:
            # Provider telemetry may be a resolved Claude name that is useful
            # for attestation but invalid as a future account-model selector.
            result_run["requested_model"] = model
            result_run.setdefault("live", {})["requested_model"] = model
        result_run["job_id"] = job_id
        result_run["auth_route"] = final_route
        result_run["credential_source"] = (
            "sponsored_kimi"
            if (extra_env or {}).get("AGENT_MONITOR_SPONSORED_KIMI") == "1"
            else "user"
        )
        if result_run["credential_source"] == "sponsored_kimi":
            result_run["sponsored_kimi_proxy"] = "short_lived_loopback_v1"
        result_run["workspace"] = str(ws)
        result_run["guest"] = bool(guest)
        result_run["use_subagents"] = bool(use_subagents)
        result_run["subagent_model"] = subagent_model if use_subagents else None
        result_run["reasoning_effort"] = reasoning_effort
        result_run["speed_mode"] = speed_mode
        result_run["max_iterations"] = max_iterations
        result_run["runtime_library"] = runtime_library
        result_run = normalize_run(result_run, engine=engine)
        _attach_analysis(result_run, ws)
        result_run["updated_at"] = _now()
        _write_run(result_run)
        status = result_run.get("status") or "finished"
        if status not in {"finished", "failed", "stopped"}:
            status = "finished"
        if result_run.get("completed") is False and status == "finished":
            status = "failed"
        _update_job(job_id, status=status, error=result_run.get("error"))
        # Full record in the human-in-the-loop chat: summary + final answer.
        t = result_run.get("totals") or {}
        summary = (
            f"{'✓' if status == 'finished' else '✗'} run {status} · "
            f"{len(result_run.get('agents') or [])} agents · "
            f"in {t.get('input_tokens') or 0} / out {t.get('output_tokens') or 0} tokens"
            + (f" · ${t.get('cost_usd'):.4f}" if t.get("cost_usd") else "")
            + f" · {int(time.time() - started)}s"
        )
        _append_chat(run_id, "system", summary)
        final_out = ""
        for a in reversed(result_run.get("agents") or []):
            if a.get("output"):
                final_out = str(a["output"])
                break
        if final_out:
            _append_chat(run_id, "assistant", final_out[:3000])
        # Neither the standalone public service nor an anonymous guest run
        # imports, reads, or writes the operator's persistent user library.
        if not public_kimi_runtime:
            _record_run_memory(
                guest=guest,
                run_id=run_id,
                engine=engine,
                problem_id=problem_id,
                status=status,
                problem_text=problem_text,
                outcome=summary,
                final_out=final_out,
            )
    except StopRequested:
        stopped_run = _load_run_record(run_id)
        stopped_run["status"] = "stopped"
        stopped_run["updated_at"] = _now()
        _write_run(stopped_run)
        _update_job(job_id, status="stopped")
        _append_chat(run_id, "system", "■ run stopped by user")
    except Exception as exc:  # noqa: BLE001
        _append_chat(run_id, "system", f"✗ run failed: {exc}")
        err = f"{exc}\n{traceback.format_exc()}"
        fail = normalize_run(
            {
                "run_id": run_id,
                "job_id": job_id,
                "problem_id": problem_id,
                "trace_name": f"[{engine.upper()}] {problem_id}",
                "status": "failed",
                "error": str(exc),
                "created_at": _now(),
                "updated_at": _now(),
                "workspace": str(ws),
                "owner_id": owner_id,
                "guest": bool(guest),
                "pipeline": _pipeline_for(engine),
                "problem_text": problem_text[:20000],
                "reasoning_effort": reasoning_effort,
                "speed_mode": speed_mode,
                "max_iterations": max_iterations,
                "agents": [
                    {
                        "trace_id": f"{run_id}::error",
                        "stage_name": "error",
                        "role": "system",
                        "pipeline_stage": "finalize",
                        "prompt": problem_text[:2000],
                        "output": err[-8000:],
                        "status": "failed",
                    }
                ],
                "edges": [],
                "totals": {"latency_s": time.time() - started},
            },
            engine=engine,  # type: ignore[arg-type]
        )
        _write_run(fail)
        _update_job(job_id, status="failed", error=str(exc))
    finally:
        if not guest and not public_kimi_runtime:
            _launch_pending_feedback(
                run_id, owner_id=owner_id, model=model, max_iterations=max_iterations
            )


def _workspace_memory(workspace: Path | None) -> dict[str, Any] | None:
    """Compact workspace snapshot for the Memory tab."""
    if not workspace or not workspace.exists():
        return None
    files = []
    try:
        for p in sorted(workspace.rglob("*")):
            if p.is_file() and ".git" not in p.parts:
                rel = p.relative_to(workspace)
                files.append(f"{rel} ({p.stat().st_size} B)")
            if len(files) >= 40:
                break
    except OSError:
        return None
    return {"workspace_files": "\n".join(files) or "(empty)"} if files else None


def _estimate_cost(
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float | None:
    """Estimate USD cost from token counts via LiteLLM pricing (best-effort)."""
    if not (input_tokens or output_tokens or cache_read or cache_write):
        return None
    try:
        from llm_cost_tracker.pricing import get_cache_pricing, get_token_price, has_real_pricing

        m = (model or "").strip() or "gpt-4o"
        if not has_real_pricing(m):
            # Fallback: treat unknown as gpt-4o-class rates so Monitor isn't all zeros.
            m = "gpt-4o"
        inp, out = get_token_price(m)
        cr, cw = get_cache_pricing(m)
        cost = (
            input_tokens * inp / 1_000_000
            + cache_read * cr / 1_000_000
            + cache_write * cw / 1_000_000
            + output_tokens * out / 1_000_000
        )
        return round(cost, 6) if cost > 0 else None
    except Exception:  # noqa: BLE001
        # Last-resort static rates (gpt-4o-ish).
        cost = (input_tokens + cache_read) * 2.5 / 1_000_000 + output_tokens * 10 / 1_000_000
        return round(cost, 6) if cost > 0 else None


def _root_proof_artifact(workspace: Path | None) -> Path | None:
    """Return the newest non-empty console proof artifact from the root."""
    if not workspace:
        return None
    candidates: list[Path] = []
    for name in ("proof.md", "proof.tex"):
        candidate = workspace / name
        try:
            if candidate.is_file() and candidate.stat().st_size >= 40:
                candidates.append(candidate)
        except OSError:
            continue
    return (
        max(candidates, key=lambda path: path.stat().st_mtime)
        if candidates
        else None
    )


def _promote_proof_artifact(workspace: Path | None) -> Path | None:
    """Expose the newest harness proof at the console's stable root path."""
    if not workspace:
        return None
    candidates: list[Path] = []
    try:
        for candidate in (*workspace.rglob("*.md"), *workspace.rglob("*.tex")):
            lowered = candidate.name.lower()
            if not any(word in lowered for word in ("proof", "solution", "answer")):
                continue
            if "_library" in candidate.parts:
                continue
            if candidate.stat().st_size >= 40:
                candidates.append(candidate)
    except OSError:
        return None
    if not candidates:
        return None
    source = max(candidates, key=lambda path: path.stat().st_mtime)
    target = workspace / ("proof.md" if source.suffix.lower() == ".md" else "proof.tex")
    try:
        if source.resolve() == target.resolve():
            return target
    except OSError:
        return None
    try:
        shutil.copy2(source, target)
    except OSError:
        return None
    return target


def _persist_openclaw_final_answer(parser: Any, workspace: Path) -> Path | None:
    """Keep a complete OpenClaw reply when it did not invoke a file-write tool."""
    existing = _promote_proof_artifact(workspace)
    if existing:
        return existing
    final = str(parser.final_message() or "").strip()
    if len(final) < 40:
        return None
    target = workspace / "proof.md"
    target.write_text(final + "\n", encoding="utf-8")
    parser.lines.append("[agent-monitor] saved OpenClaw's final answer as proof.md")
    return target


def _persist_text_final_answer(final: Any, workspace: Path, *, engine: str) -> Path | None:
    """Persist a complete text response when an agent forgot to write its artifact."""
    text = str(final or "").strip()
    if engine.casefold() in {"kimi", "plain"}:
        text = normalize_proof_markdown(text)
    existing = _promote_proof_artifact(workspace)
    normalized_final = text.casefold().replace("’", "'").replace("`", "")
    def has_explicit_placeholder(value: str) -> bool:
        normalized = value.casefold().replace("’", "'").replace("`", "")
        return any(
            marker in normalized
            for marker in (
                "to be refined",
                "proof.md remains an incomplete draft",
                "file remains an incomplete draft",
            )
        )

    reports_failed_update = engine.casefold() == "hermes" and (
        any(
            marker in normalized_final
            for marker in (
                "could not complete the required file update",
                "couldn't complete the required file update",
                "unable to complete the required file update",
                "failed to complete the required file update",
            )
        )
        or (
            "proof.md" in normalized_final
            and any(
                marker in normalized_final
                for marker in (
                    "could not update",
                    "couldn't update",
                    "unable to update",
                    "failed to update",
                )
            )
        )
    )
    reports_incomplete = engine.casefold() == "hermes" and any(
        marker in normalized_final
        for marker in (
            "proof.md remains an incomplete draft",
            "proof.tex remains an incomplete draft",
            "file remains an incomplete draft",
            "proof.md was not saved",
            "proof.tex was not saved",
            "could not create proof.md",
            "couldn't create proof.md",
            "unable to create proof.md",
            "failed to finalize proof.md",
            "failed while finalizing proof.md",
        )
    ) or reports_failed_update
    marker = "--- proof.md ---"
    try:
        existing_text = existing.read_text(encoding="utf-8").strip() if existing else ""
    except OSError:
        existing_text = ""
    if engine.casefold() == "hermes" and has_explicit_placeholder(existing_text):
        reports_incomplete = True
    prefix, embedded = (text.split(marker, 1) if marker in text else (text, ""))
    embedded = embedded.strip()
    embedded_is_new = (
        len(embedded) >= 40
        and embedded != existing_text
        and not has_explicit_placeholder(embedded)
    )
    if existing and not reports_incomplete and not embedded_is_new:
        return existing
    if embedded_is_new:
        # In Landlock compatibility mode the embedded Codex agent is
        # deliberately read-only. The monitor is the trusted writer and may
        # promote only a substantive artifact that differs from the old file.
        text = embedded
    elif reports_incomplete and marker in text:
        if reports_failed_update:
            return None
        # Never re-promote the exact stale draft that the final response just
        # declared incomplete; retain any substantive diagnostic prefix.
        text = prefix.strip()
    elif reports_failed_update:
        return None
    elif not existing and marker in text:
        embedded = text.split(marker, 1)[1].strip()
        if len(embedded) >= 40:
            text = embedded
    if len(text) < 40:
        return None
    # Avoid wrapping an already-complete Markdown response in a second fence.
    if text.startswith("```markdown") and text.endswith("```"):
        text = text[len("```markdown") : -3].strip()
    if engine.casefold() == "hermes":
        # The embedded Codex transport can return a valid mathematical answer
        # preceded or followed by an operational note that its own sandbox could
        # not write proof.md. The monitor is the trusted artifact writer and is
        # about to save that answer, so either edge note is stale and noisy. Do
        # not remove matching text from the middle: it may be part of a genuine
        # discussion of the mathematical task or its reproducibility.
        paragraphs = text.split("\n\n")
        def stale_artifact_note(paragraph: str) -> bool:
            normalized = paragraph.casefold().replace("’", "'").replace("`", "")
            unable = any(
                marker in normalized
                for marker in ("could not", "couldn't", "unable to", "failed to")
            )
            operational = "proof.md" in normalized and any(
                marker in normalized
                for marker in ("permission", "sandbox", "filesystem", "environment", "write")
            )
            return unable and operational

        # A scoped-network proxy can fail before *every* tool launch on kernels
        # that cannot create its loopback namespace. Remove only sentences
        # carrying that exact runtime signature, even when the model put them
        # in a middle reproducibility section; preserve mathematical content
        # and the honest statement that the literature audit is incomplete.
        if "bwrap: loopback: failed rtm_newaddr" in text.casefold():
            import re

            cleaned: list[str] = []
            for paragraph in paragraphs:
                sentences = re.split(r"(?<=[.!?])\s+", paragraph)
                kept = [
                    sentence
                    for sentence in sentences
                    if not any(
                        marker in sentence.casefold()
                        for marker in (
                            "bwrap: loopback: failed rtm_newaddr",
                            "request to run outside that sandbox",
                            "direct web open/search calls",
                            "remains an incomplete draft",
                        )
                    )
                ]
                paragraph = " ".join(kept).strip()
                if paragraph.casefold().startswith("accordingly,"):
                    paragraph = (
                        "No reproducible primary-source search was completed in this run; "
                        + paragraph[12:].lstrip()
                    )
                if paragraph:
                    cleaned.append(paragraph)
            paragraphs = cleaned

        while paragraphs and stale_artifact_note(paragraphs[0]):
            paragraphs.pop(0)
        while paragraphs and stale_artifact_note(paragraphs[-1]):
            paragraphs.pop()
        text = "\n\n".join(paragraphs).strip()
        if len(text) < 40:
            return None
    target = workspace / "proof.md"
    target.write_text(text + "\n", encoding="utf-8")
    return target


def _attach_proof_provenance(agents: list[dict[str, Any]], workspace: Path | None) -> None:
    """Attach the final proof once so Agent Trace can attribute its lines.

    Generic CLI traces often contain many message-shaped contributors but only
    one shared workspace artifact. Attaching the complete proof to every such
    node both falsely gives every turn the same contribution and makes final
    provenance quadratic in the number of turns. Native multi-agent artifact
    enrichment still runs later and can add each agent's distinct text.
    """
    if not workspace:
        return
    proof = workspace / "proof.md"
    if not proof.exists():
        proof = workspace / "proof.tex"
    if not proof.exists():
        # Common alternates
        for cand in list(workspace.rglob("*.md")) + list(workspace.rglob("*.tex")):
            if cand.name in {"proof.md", "proof.tex", "solution.tex", "answer.tex"} or "proof" in cand.name:
                proof = cand
                break
        else:
            return
    try:
        tex = proof.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if len(tex.strip()) < 40:
        return
    eligible: list[dict[str, Any]] = []
    for a in agents:
        role = (a.get("role") or "").lower()
        pipe = str(a.get("pipeline_stage") or "").lower()
        stage = (a.get("stage_name") or "").lower()
        if role in {"writer", "prover", "assistant", "author"} or pipe in {
            "draft",
            "write",
            "finalize",
            "author",
        } or "message" in stage or "final" in stage or "write" in stage:
            eligible.append(a)
    if not eligible:
        return

    # Agent order is chronological for all runner parsers. Attribute the
    # shared final artifact to the last eligible contributor; artifact-backed
    # harnesses may subsequently enrich earlier agents with their own outputs.
    target = eligible[-1]
    marker = f"\n\n--- {proof.name} ---\n"
    for agent in eligible[:-1]:
        if agent.get("_provenance_text") == tex:
            agent.pop("_provenance_text", None)
        if agent.get("output_source") == f"workspace {proof.name}":
            output = str(agent.get("output") or "")
            if marker in output:
                agent["output"] = output.split(marker, 1)[0]
                agent.pop("output_source", None)
    target["_provenance_text"] = tex
    # Also surface a short preview in output if it is only a tool log.
    out = target.get("output") or ""
    if tex[:200] not in out and "\\documentclass" not in out:
        target["output"] = (out + marker + tex)[:12000]
        target.setdefault("output_source", f"workspace {proof.name}")


def _attach_final_latex(run: dict[str, Any]) -> None:
    try:
        from harness_dashboard.latex_provenance import build_final_latex_bundle

        bundle = build_final_latex_bundle(run)
        if bundle:
            run["final_latex"] = bundle
    except Exception:  # noqa: BLE001
        pass


def _normalize_terminal_kimi_outcome(
    run: dict[str, Any], workspace: Path | None
) -> bool:
    """Publish one conservative outcome footer for finished Kimi Markdown."""
    if str(run.get("status") or "").strip().lower() != "finished" or not workspace:
        return False
    # Math Harness publishes ``proof.md`` and its graph receipt as one
    # content-bound generation, with the receipt deliberately written last.
    # Mutating that proof during generic Kimi cleanup would invalidate the
    # otherwise valid receipt and make the Engine DAG fail closed.  Its critic
    # verdict remains explicit in the receipt/UI instead of this legacy footer.
    if str(run.get("engine") or "").strip().lower() == "math_harness":
        return False
    live = run.get("live") if isinstance(run.get("live"), dict) else {}
    model = str(run.get("model") or live.get("model") or "").strip()
    sponsored = (
        str(run.get("credential_source") or "").strip().lower()
        == "sponsored_kimi"
    )
    if not sponsored and not _is_kimi_model(model):
        return False
    try:
        from agent_monitor import proof_graph
        from agent_monitor.runners.outcome_contract import canonicalize_outcome_text

        original = proof_graph._read_workspace_text(
            workspace, "proof.md", limit=2 * 1024 * 1024
        )
        if len(original.strip()) < 40:
            return False
        rendered = canonicalize_outcome_text(original)
        if rendered == original:
            return False
        target = workspace / "proof.md"
        if target.is_symlink() or target.resolve().parent != workspace.resolve():
            return False
        _atomic_write_text(target, rendered)
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def _attach_analysis(run: dict[str, Any], workspace: Path | None) -> dict[str, Any]:
    """Add the Run Analysis / Agent Flow block the dashboard renders."""
    _normalize_terminal_kimi_outcome(run, workspace)
    try:
        from harness_dashboard.analysis import compute_run_analysis

        run["analysis"] = compute_run_analysis(
            run.get("agents") or [],
            run.get("totals") or {},
            run_output_dir=workspace,
            pipeline=run.get("pipeline") or [],
            edges=run.get("edges") or [],
        )
    except Exception:  # noqa: BLE001 — analysis is best-effort
        pass
    if run.get("status") in {"finished", "failed", "stopped"}:
        _attach_proof_provenance(run.get("agents") or [], workspace)
        _attach_final_latex(run)
    return run


def _artifact_agents(engine: str, workspace: Path, run_id: str) -> dict[str, Any] | None:
    """Build a rich multi-agent view from the engine's own artifacts.

    IMProof logs every agent (Author / Critic / Council…) to events.jsonl;
    the UCLA harness writes per-stage usage.jsonl + conversation memory.
    Reuses the dashboard artifact builders so the Monitor gets the full
    agent graph instead of one wrapper session.
    """
    try:
        if engine == "improof":
            from harness_dashboard.improofbench_builder import build_run_from_improof_artifacts

            candidates = sorted(
                (d for d in workspace.iterdir() if d.is_dir() and (d / "events.jsonl").exists()),
                key=lambda d: d.stat().st_mtime,
            )
            if not candidates:
                return None
            return build_run_from_improof_artifacts(run_id, candidates[-1])
        if engine == "ucla":
            from harness_dashboard.artifact_builder import build_run_from_artifacts

            if not (workspace / "Overall_Usage" / "usage.jsonl").exists():
                return None
            return build_run_from_artifacts(run_id, workspace)
    except Exception:  # noqa: BLE001 — enrichment is best-effort
        return None
    return None


def _merge_artifact_run(base: dict[str, Any], rich: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay artifact-derived agents/pipeline onto the console run record."""
    if not rich or not rich.get("agents"):
        return base
    out = dict(base)
    for key in ("agents", "edges", "pipeline", "stages_present", "analysis", "artifact_dir"):
        if rich.get(key):
            out[key] = rich[key]
    totals = dict(rich.get("totals") or {})
    totals["latency_s"] = (base.get("totals") or {}).get("latency_s") or totals.get("latency_s")
    out["totals"] = totals
    return out


def _live_output_flusher(
    *,
    run_id: str,
    engine: str,
    problem_id: str,
    problem_text: str,
    started: float,
    workspace: Path,
    baseline_run: dict[str, Any] | None = None,
    continuation_job_id: str | None = None,
):
    """Callback that streams subprocess output into the run JSON while running."""
    last_enrich = [0.0]
    rich_cache: list[dict[str, Any] | None] = [None]

    def _flush(output: str) -> None:
        run = normalize_run(
            {
                "run_id": run_id,
                "problem_id": problem_id,
                "trace_name": f"[{engine.upper()}] {problem_id}",
                "status": "running",
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for(engine),
                "problem_text": problem_text[:20000],
                "agents": [
                    {
                        "trace_id": f"{run_id}::{engine}_main",
                        "stage_name": f"{engine}_main",
                        "role": "orchestrator",
                        "pipeline_stage": _pipeline_for(engine)[0]["id"],
                        "prompt": problem_text[:4000],
                        "output": output[-12000:],
                        "status": "running",
                        "latency_s": time.time() - started,
                    }
                ],
                "totals": {"latency_s": time.time() - started},
            },
            engine=engine,  # type: ignore[arg-type]
        )
        now = time.time()
        if now - last_enrich[0] > 10.0:
            last_enrich[0] = now
            rich = _artifact_agents(engine, workspace, run_id)
            if rich and rich.get("agents"):
                rich_cache[0] = rich
        # Heartbeats arrive every two seconds, while native artifact parsing is
        # intentionally throttled. Reuse the latest rich view between parses;
        # otherwise every intervening heartbeat replaces Author/Critic nodes
        # with the one-node orchestrator placeholder and makes Monitor flicker.
        if rich_cache[0] and rich_cache[0].get("agents"):
            merged = _merge_artifact_run(run, rich_cache[0])
            merged["status"] = "running"
            run = normalize_run(merged, engine=engine)  # type: ignore[arg-type]
        if baseline_run and continuation_job_id:
            run = _merge_continuation_run(
                baseline_run, run, job_id=continuation_job_id
            )
            run["status"] = "running"
        _write_run(run)

    return _flush


def _run_hermes(
    *,
    run_id: str,
    problem_id: str,
    problem_text: str,
    model: str | None,
    max_iterations: int,
    started: float,
    workspace: Path,
    display_problem_text: str | None = None,
    extra_env: dict[str, str] | None = None,
    use_subagents: bool = True,
    subagent_model: str | None = None,
    owner_id: int | None = None,
) -> dict[str, Any]:
    from agent_monitor.runners import hermes as hermes_runner

    record_problem_text = (
        display_problem_text if display_problem_text is not None else problem_text
    )

    # Live placeholder updates via step callback when available
    agents: list[dict[str, Any]] = []
    tool_log: list[str] = []
    # Surfaced to the console's human-in-the-loop panel: which model is running
    # and which tools it is touching right now.
    live: dict[str, Any] = {
        "engine": "hermes",
        "model": model or (extra_env or {}).get("AGENT_MONITOR_MODEL") or None,
        "subagent_model": subagent_model if use_subagents else None,
        "subagents": bool(use_subagents),
        "tools_active": [],
        "tools_recent": [],
        "tool_calls": 0,
        "iteration": 0,
    }

    def _flush(partial_output: str = "") -> None:
        run = normalize_run(
            {
                "run_id": run_id,
                "live": {**live, "tools_recent": live["tools_recent"][-10:]},
                "problem_id": problem_id,
                "trace_name": f"[HERMES] {problem_id}",
                "status": "running",
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for("hermes"),
                "problem_text": record_problem_text[:20000],
                "agents": agents
                or [
                    {
                        "trace_id": f"{run_id}::hermes_main",
                        "stage_name": "hermes_main",
                        "role": "prover",
                        "pipeline_stage": "draft",
                        "prompt": problem_text[:4000],
                        "output": partial_output or "Hermes agent running…",
                        "status": "running",
                    }
                ],
                "edges": [
                    {"from": a["trace_id"], "to": b["trace_id"]}
                    for a, b in zip(agents, agents[1:])
                ],
                "totals": {"latency_s": time.time() - started},
            },
            engine="hermes",
        )
        _attach_analysis(run, workspace)
        _write_run(run)

    _flush()
    ue = extra_env or {}
    direct_key = ue.get("OPENAI_API_KEY") or ue.get("OPENROUTER_API_KEY") or None
    codex_home: Path | None = None
    if owner_id is not None and ue.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        from agent_monitor import codex_login

        candidate = codex_login.account_home(owner_id)
        if codex_login.account_login_ready(candidate):
            codex_home = candidate
    if ue.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1" and codex_home is None:
        raise RuntimeError("The selected Codex account is unavailable; reconnect Codex and retry.")
    agent = hermes_runner.create_agent(
        model=model or ue.get("AGENT_MONITOR_MODEL") or None,
        api_key=None if codex_home else direct_key,
        base_url=None if codex_home else (ue.get("AGENT_MONITOR_BASE_URL") or ue.get("OPENAI_BASE_URL") or None),
        codex_home=codex_home,
        use_codex_subscription=codex_home is not None,
        max_iterations=max_iterations,
        enable_subagents=use_subagents,
        subagent_model=subagent_model,
        reasoning_effort=(
            None
            if ue.get("AGENT_MONITOR_RUN_REASONING_EFFORT") in {None, "", "default"}
            else ue.get("AGENT_MONITOR_RUN_REASONING_EFFORT")
        ),
        service_tier=(
            "fast" if ue.get("AGENT_MONITOR_RUN_SPEED_MODE") == "fast"
            else "default" if ue.get("AGENT_MONITOR_RUN_SPEED_MODE") == "standard"
            else None
        ),
    )
    # The embedded Codex app-server must start inside this run's isolated
    # workspace; otherwise it sees the repository cwd and cannot write the
    # required proof.md artifact.
    agent.session_cwd = str(workspace)
    live["auth_mode"] = "chatgpt_subscription" if codex_home else "api_key"
    live["model"] = getattr(agent, "model", None) or live["model"]

    stop_ev = _stop_event(run_id)

    # Tool-level telemetry for the live panel. Flushes are throttled because a
    # busy agent can start tools far faster than the console polls.
    active_tools: dict[str, str] = {}
    progress_tools: dict[str, list[str]] = {}
    progress_counter = [0]
    last_live_flush = [0.0]

    def _flush_live() -> None:
        now = time.monotonic()
        if now - last_live_flush[0] < 1.0:
            return
        last_live_flush[0] = now
        try:
            _flush()
        except Exception:  # noqa: BLE001
            pass

    def tool_start_cb(tool_id: Any = None, name: Any = None, args: Any = None) -> None:
        try:
            active_tools[str(tool_id)] = str(name)
            live["tool_calls"] = int(live.get("tool_calls") or 0) + 1
            live["tools_active"] = list(active_tools.values())
            live["tools_recent"].append(
                {"name": str(name), "args": str(args or "")[:160], "state": "running"}
            )
            del live["tools_recent"][:-20]
            _flush_live()
        except Exception:  # noqa: BLE001
            pass

    def tool_complete_cb(tool_id: Any = None, name: Any = None, args: Any = None, result: Any = None) -> None:
        try:
            active_tools.pop(str(tool_id), None)
            live["tools_active"] = list(active_tools.values())
            for rec in reversed(live["tools_recent"]):
                if rec.get("name") == str(name) and rec.get("state") == "running":
                    rec["state"] = "done"
                    if args:
                        rec["args"] = str(args)[:160]
                    rec["result"] = str(result or "")[:200]
                    break
            _flush_live()
        except Exception:  # noqa: BLE001
            pass

    for attr, cb in (("tool_start_callback", tool_start_cb), ("tool_complete_callback", tool_complete_cb)):
        if hasattr(agent, attr):
            try:
                setattr(agent, attr, cb)
            except Exception:  # noqa: BLE001
                pass

    # Codex subscription runs execute through the embedded app-server rather
    # than Hermes' regular tool executor. That route emits only the generic
    # progress callback, so bridge it into the same structured live telemetry
    # used by every other Hermes provider. Restricting this adapter to the
    # app-server route avoids double-counting ordinary Hermes tool calls, whose
    # executor emits both progress and structured callbacks.
    if codex_home is not None:
        def tool_progress_cb(
            event_type: Any,
            name: Any = None,
            preview: Any = None,
            args: Any = None,
            **metadata: Any,
        ) -> None:
            tool_name = str(name or "tool")
            if event_type == "tool.started":
                progress_counter[0] += 1
                live["iteration"] = max(int(live.get("iteration") or 0), 1)
                tool_id = f"codex-progress-{progress_counter[0]}"
                progress_tools.setdefault(tool_name, []).append(tool_id)
                tool_start_cb(tool_id, tool_name, args or preview)
            elif event_type == "tool.completed":
                pending = progress_tools.get(tool_name) or []
                tool_id = pending.pop(0) if pending else f"codex-progress-{tool_name}"
                if not pending:
                    progress_tools.pop(tool_name, None)
                tool_complete_cb(
                    tool_id,
                    tool_name,
                    args,
                    metadata.get("result") or preview,
                )

        try:
            agent.tool_progress_callback = tool_progress_cb
        except Exception:  # noqa: BLE001
            pass

    def _tok_snapshot() -> dict[str, float]:
        return {
            "in": getattr(agent, "session_input_tokens", 0)
            or getattr(agent, "session_prompt_tokens", 0)
            or 0,
            "out": getattr(agent, "session_output_tokens", 0)
            or getattr(agent, "session_completion_tokens", 0)
            or 0,
            "cost": getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0,
        }

    def _stage_for_tools(names: list[str]) -> tuple[str, str]:
        joined = " ".join(names).lower()
        if any(k in joined for k in ("write", "edit", "create", "append", "apply")):
            return "draft", "writer"
        if any(k in joined for k in ("terminal", "python", "exec", "run", "bash", "shell", "compile")):
            return "verify", "checker"
        if any(k in joined for k in ("read", "search", "grep", "list", "web", "fetch", "browse")):
            return "understand", "reader"
        return "plan", "reasoning"

    last_tok: dict[str, float] = {"in": 0, "out": 0, "cost": 0.0}
    thinking_buf: list[str] = []

    def reasoning_cb(text: str) -> None:
        if text and str(text).strip():
            thinking_buf.append(str(text))

    if hasattr(agent, "reasoning_callback"):
        try:
            agent.reasoning_callback = reasoning_cb
        except Exception:  # noqa: BLE001
            pass

    def _close_last(prev_tools: Any) -> None:
        """Finalize the node for the iteration that just completed."""
        if not agents:
            return
        node = agents[-1]
        snap = _tok_snapshot()
        node["input_tokens"] = int(snap["in"] - last_tok["in"]) or None
        node["total_input_tokens"] = node["input_tokens"]  # recompute (was frozen at 0 while running)
        node["output_tokens"] = int(snap["out"] - last_tok["out"]) or None
        d_cost = snap["cost"] - last_tok["cost"]
        if d_cost > 0:
            node["cost_usd"] = round(d_cost, 6)
        elif node.get("input_tokens") or node.get("output_tokens"):
            node["cost_usd"] = _estimate_cost(
                getattr(agent, "model", model),
                input_tokens=int(node.get("input_tokens") or 0),
                output_tokens=int(node.get("output_tokens") or 0),
            )
        last_tok.update(snap)
        node["status"] = "finished"
        if thinking_buf:
            node["thinking"] = "\n\n".join(thinking_buf)[:8000]
            node["thinking_source"] = "hermes reasoning stream"
            thinking_buf.clear()
        if prev_tools:
            names = [str(t.get("name", "?")) for t in prev_tools if isinstance(t, dict)]
            stage, role = _stage_for_tools(names)
            node["pipeline_stage"] = stage
            node["role"] = role
            node["tools"] = names
            node["stage_name"] = f"iter-{node['round_id']} ({', '.join(names[:3])})"
            parts = []
            for t in prev_tools:
                if isinstance(t, dict):
                    args = str(t.get("arguments") or "")[:400]
                    res = str(t.get("result") or "")[:1200]
                    parts.append(f"▸ {t.get('name')}({args})\n{res}")
            node["output"] = "\n\n".join(parts)[:8000] or node.get("output", "")
            tool_log.append(f"[iter {node['round_id']}] {', '.join(names)}")

    def step_cb(iteration: int, prev_tools: Any = None) -> None:
        if stop_ev.is_set():
            raise StopRequested(run_id)
        try:
            _close_last(prev_tools)
        except Exception:  # noqa: BLE001
            pass
        live["iteration"] = iteration
        agents.append(
            {
                "trace_id": f"{run_id}::iter-{iteration}",
                "stage_name": f"iter-{iteration}",
                "role": "reasoning",
                "pipeline_stage": "plan",
                "call_seq": iteration,
                "round_id": iteration,
                "model": getattr(agent, "model", None) or model,
                "prompt": problem_text[:4000] if iteration == 1
                else "(agent loop continues — model sees the task, conversation history and previous tool results)",
                "prompt_source": "task prompt" if iteration == 1 else "conversation context",
                "output": "model call in progress…",
                "output_source": "hermes tool results",
                "status": "running",
            }
        )
        _flush()

    if hasattr(agent, "step_callback"):
        try:
            agent.step_callback = step_cb
        except Exception:  # noqa: BLE001
            pass

    from agent_monitor.engines_registry import CITATION_REQUIREMENTS

    prompt = (
        "You are working on an informal mathematics proof problem.\n"
        "Use tools as needed (code, files, terminal, subagents).\n\n"
        f"WORKSPACE (your sandbox directory): {workspace}\n"
        "Requirements:\n"
        f"1. Maintain your evolving proof write-up in {workspace}/proof.md "
        "(Markdown; use $...$ / $$...$$ for math, headings for structure). "
        "Update this file as your proof develops — write early drafts, then refine.\n"
        f"2. You may create scratch files (notes, python checks) inside {workspace}.\n"
        "3. Finish by making proof.md a clean, self-contained informal proof.\n"
        "4. End your final response with a line containing exactly "
        "--- proof.md --- followed by the complete final Markdown artifact. "
        "This is the monitor's safe persistence fallback; never paste an "
        "unchanged draft after reporting a failed update.\n\n"
        f"{CITATION_REQUIREMENTS}\n"
        f"PROBLEM:\n{problem_text}\n"
    )
    result: dict[str, Any] | None = None
    heartbeat_stop = threading.Event()

    def _heartbeat() -> None:
        while not heartbeat_stop.wait(_HERMES_HEARTBEAT_S):
            try:
                if stop_ev.is_set():
                    interrupt = getattr(agent, "interrupt", None)
                    if callable(interrupt):
                        interrupt("Run stopped by user.")
                    session = getattr(agent, "_codex_session", None)
                    if session is not None:
                        session.request_interrupt()
                    continue
                _flush()
            except Exception:  # noqa: BLE001 - telemetry cannot fail a run
                pass

    heartbeat_thread = threading.Thread(
        target=_heartbeat,
        name=f"hermes-heartbeat-{run_id}",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        result = agent.run_conversation(prompt)
        initial_final = str((result or {}).get("final_response") or "")
        _persist_text_final_answer(initial_final, workspace, engine="Hermes")
        if (
            not stop_ev.is_set()
            and _hermes_audit_last_mile_needed(problem_text, workspace)
        ):
            audit_result = agent.run_conversation(
                _HERMES_AUDIT_LAST_MILE_PROMPT,
                conversation_history=(result or {}).get("messages") or None,
            )
            result = _merge_hermes_turn_results(result, audit_result)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=max(1.0, _HERMES_HEARTBEAT_S + 0.5))
        # Monitor runs are one-shot sessions. Hermes' AIAgent.close() does not
        # itself retire the lazy Codex app-server transport, so use the runner
        # helper to prevent completed runs leaking app-server/MCP children.
        hermes_runner.close_agent(agent, messages=(result or {}).get("messages") or [])
    if stop_ev.is_set():
        raise StopRequested(run_id)
    elapsed = time.time() - started
    final = (result or {}).get("final_response") or ""
    proof = _persist_text_final_answer(final, workspace, engine="Hermes")
    completed = bool((result or {}).get("completed", True)) and proof is not None
    final_status = "finished" if completed else "failed"
    try:
        _close_last(None)
    except Exception:  # noqa: BLE001
        pass
    # Drop a dangling in-progress node (the final answer call), replace with finalize.
    if agents and agents[-1].get("status") == "running":
        agents.pop()
    snap = _tok_snapshot()
    total_in = (result or {}).get("input_tokens") or (result or {}).get("prompt_tokens") or snap["in"]
    total_out = (result or {}).get("output_tokens") or (result or {}).get("completion_tokens") or snap["out"]
    delta_in = int(snap["in"] - last_tok["in"])
    delta_out = int(snap["out"] - last_tok["out"])
    delta_cost = snap["cost"] - last_tok["cost"]
    fin_cost = round(delta_cost, 6) if delta_cost > 0 else _estimate_cost(
        getattr(agent, "model", model), input_tokens=delta_in, output_tokens=delta_out
    )
    agents.append(
        {
            "trace_id": f"{run_id}::finalize",
            "stage_name": "final answer",
            "role": "prover",
            "pipeline_stage": "finalize",
            "call_seq": len(agents) + 1,
            "model": getattr(agent, "model", model),
            "latency_s": elapsed,
            "input_tokens": delta_in or None,
            "total_input_tokens": delta_in or None,
            "output_tokens": delta_out or None,
            "cost_usd": fin_cost,
            "prompt": prompt,
            "prompt_source": "task prompt",
            "thinking": "\n\n".join(thinking_buf)[:8000] if thinking_buf else None,
            "thinking_source": "hermes reasoning stream" if thinking_buf else None,
            "output": final,
            "output_source": "final response",
            "memory_context": _workspace_memory(workspace),
            "memory_source": "workspace snapshot",
            "status": final_status,
            "tool_calls": (result or {}).get("tool_call_count")
            or int(live.get("tool_calls") or 0),
        }
    )
    total_cost = (result or {}).get("estimated_cost_usd") or (result or {}).get("actual_cost_usd")
    if not total_cost:
        total_cost = sum(float(a.get("cost_usd") or 0) for a in agents) or None
        if not total_cost:
            total_cost = _estimate_cost(
                getattr(agent, "model", model),
                input_tokens=int(total_in or 0),
                output_tokens=int(total_out or 0),
            )
    edges = [
        {"from": a["trace_id"], "to": b["trace_id"]}
        for a, b in zip(agents, agents[1:])
    ]
    live["tools_active"] = []
    run = normalize_run(
        {
            "run_id": run_id,
            "problem_id": problem_id,
            "trace_name": f"[HERMES] {problem_id}",
            "live": {**live, "tools_recent": live["tools_recent"][-10:]},
            "status": final_status,
            "completed": completed,
            "error": None if completed else "Hermes finished without creating a non-empty proof.md or proof.tex",
            "updated_at": _now(),
            "workspace": str(workspace),
            "pipeline": _pipeline_for("hermes"),
            "problem_text": record_problem_text[:20000],
            "agents": agents,
            "edges": edges,
            "totals": {
                "input_tokens": total_in,
                "output_tokens": total_out,
                "cost_usd": total_cost,
                "latency_s": elapsed,
                "api_calls": (result or {}).get("api_calls"),
            },
            "message_count": len((result or {}).get("messages") or []),
        },
        engine="hermes",
    )
    return _attach_analysis(run, workspace)


def _ensure_codex_auth(env: dict[str, str], owner_id: int | None = None) -> None:
    """Point codex at a CODEX_HOME with valid credentials.

    Prefers the *run owner's* ChatGPT/OpenAI-account (OAuth) login — set up
    once via the Settings panel's "Codex Account" card — over API-key
    billing, so runs consume that account's subscription usage instead of
    metered API calls. This is strictly per-user: ``owner_id`` must come from
    the authenticated session that started the run, never from request data,
    so one user's runs can never pick up another user's Codex login. Set
    ``AGENT_MONITOR_CODEX_AUTH_MODE=apikey`` to force API-key mode even when
    an account login exists.

    Falls back to a per-key CODEX_HOME (auto logged in with OPENAI_API_KEY)
    when no account login is present (or no owner is known); keying that
    home dir by the API key keeps different users' credentials separate and
    auto-refreshes when a key changes in Settings.
    """
    import hashlib
    import shutil
    import subprocess

    if owner_id is not None:
        from agent_monitor import codex_login

        account_home = codex_login.account_home(owner_id)
        mode = str(env.get("AGENT_MONITOR_AUTH_MODE") or env.get("AGENT_MONITOR_CODEX_AUTH_MODE") or "").lower()
        use_codex = str(env.get("AGENT_MONITOR_USE_CODEX", "1")).lower() not in {"0", "false", "off", "no"}
        force_apikey = mode in {"api_key", "apikey"} or not use_codex
        if not force_apikey and codex_login.account_login_ready(account_home):
            env["CODEX_HOME"] = str(account_home)
            return

    key = env.get("OPENAI_API_KEY")
    if not key:
        return
    home = CACHE_DIR.parent / "codex_home" / hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    home.mkdir(parents=True, exist_ok=True)
    env["CODEX_HOME"] = str(home)
    if (home / "auth.json").exists():
        return
    from agent_monitor.engines_registry import which_tool

    codex = shutil.which("codex", path=env.get("PATH")) or which_tool("codex")
    if not codex:
        return
    try:
        from agent_monitor.subprocess_env import child_process_env

        login_env = child_process_env(
            source=env,
            extra={"CODEX_HOME": str(home)},
        )
        subprocess.run(
            [codex, "login", "--with-api-key"],
            input=key,
            text=True,
            env=login_env,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _cli_failure_reason(
    *,
    label: str,
    returncode: int | None,
    timed_out: bool,
    timeout_s: int,
    proof_missing: bool,
    detail: str | None = None,
) -> str:
    if timed_out:
        return f"{label} timed out after {timeout_s}s"
    if returncode not in (None, 0):
        suffix = f": {detail.strip()}" if detail and detail.strip() else ""
        return f"{label} exited with code {returncode}{suffix}"
    if proof_missing:
        return f"{label} exited successfully but produced no proof artifact"
    return f"{label} failed before producing a proof artifact"


def _cli_timeout_seconds(engine: str, env: dict[str, str]) -> int:
    """Resolve a bounded CLI timeout, including multi-stage harness budgets."""
    normalized = str(engine or "").strip().upper().replace("-", "_")
    if normalized == "DEEPAGENTS":
        default = 10_800
    elif normalized == "MATH_HARNESS":
        # The response-only workflow can make three candidate calls plus a
        # planner, critic, and one revision. Keep its outer watchdog above the
        # default per-stage Claude budgets so it cannot kill a healthy final
        # stage just before that stage's own bounded timeout.
        default = 7_200
        if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
            # Operator stage overrides can exceed the built-in profile. Derive
            # a coherent outer default from the same bounded helpers, including
            # the optional revision and a small publication/teardown margin.
            try:
                from agent_monitor.runners import math_harness_runner

                candidates = math_harness_runner.candidate_count(env)
                stage_budget = (
                    math_harness_runner._claude_stage_timeout("planner", env)
                    + candidates
                    * math_harness_runner._claude_stage_timeout("candidate-1", env)
                    + math_harness_runner._claude_stage_timeout("critic", env)
                    + math_harness_runner._claude_stage_timeout("revision", env)
                )
                default = max(default, int(stage_budget + 300))
            except (ImportError, ValueError, RuntimeError):
                # The runner will surface malformed stage configuration. Its
                # outer process still receives the normal bounded default.
                default = 7_200
    elif normalized == "CLAUDE":
        # The direct Claude runner has its own inner provider deadline. Keep
        # the default outer watchdog above a valid configured inner deadline
        # so publication and process-group cleanup retain bounded headroom.
        default = 3_600
        try:
            inner = float(env.get("AGENT_MONITOR_CLAUDE_TIMEOUT") or "")
        except (TypeError, ValueError):
            inner = 0.0
        if math.isfinite(inner) and inner > 0:
            default = max(default, math.ceil(inner) + 300)
    else:
        default = 3_600
    engine_override = (
        None
        if normalized == "CLAUDE"
        else env.get(f"AGENT_MONITOR_{normalized}_TIMEOUT")
    )
    raw = (
        engine_override
        or env.get("AGENT_MONITOR_CLI_TIMEOUT")
        or str(default)
    )
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        requested = default
    bounded = max(60, min(requested, 86_400))
    if env.get("AGENT_MONITOR_SPONSORED_KIMI") == "1":
        bounded = min(bounded, _SPONSORED_KIMI_CLI_TIMEOUT_MAX)
    return bounded


def _apply_selected_model_env(
    env: dict[str, str], *, engine: str, model: str | None
) -> None:
    """Route the UI model selection into the selected runner only."""
    requested = str(model or "").strip()
    if not requested:
        return
    if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
        env["AGENT_MONITOR_SELECTED_MODEL"] = requested
        env["AGENT_MONITOR_CLAUDE_MODEL"] = requested
        return
    if _is_kimi_model(requested):
        # Kimi remains an API provider even when this user has a linked Codex
        # subscription.  Leaving this flag set makes wrappers discard the
        # selected Kimi key and quietly execute a different model.
        env.pop("AGENT_MONITOR_CODEX_SUBSCRIPTION", None)
        env.pop("AGENT_MONITOR_CODEX_MODEL", None)
        env["AGENT_MONITOR_AUTH_MODE"] = "api_key"
    _apply_kimi_compatible_env(env, requested)
    env["AGENT_MONITOR_SELECTED_MODEL"] = requested
    if env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        env["AGENT_MONITOR_CODEX_MODEL"] = requested
        return
    env["AGENT_MONITOR_OPENAI_MODEL"] = requested
    if engine == "plain":
        env["PLAIN_MODEL"] = requested
    elif engine == "metaharness":
        env["METAHARNESS_MODEL"] = requested
    elif engine == "deepagents":
        prefix = "anthropic:" if requested.lower().startswith("claude-") else "openai:"
        env["DEEPAGENTS_MODEL"] = prefix + requested
    elif engine == "openclaude":
        env["AGENT_MONITOR_OPENCLAUDE_MODEL"] = requested
    elif engine == "openclaw":
        provider = "anthropic" if requested.lower().startswith("claude-") else "openai"
        env["AGENT_MONITOR_OPENCLAW_MODEL"] = f"{provider}/{requested}"
    elif engine == "openhands":
        openhands_model = f"openai/{requested}"
        env["AGENT_MONITOR_OPENHANDS_MODEL"] = openhands_model
        if _is_kimi_model(requested):
            # An inherited LLM_MODEL may point at OpenAI or Anthropic.  The UI
            # selection is authoritative and must not be weakened by the
            # later runtime's setdefault call.
            env["LLM_MODEL"] = openhands_model


def _run_sponsored_kimi_plain(
    *,
    run_id: str,
    problem_id: str,
    problem_text: str,
    display_problem_text: str,
    started: float,
    workspace: Path,
    owner_id: int | None,
) -> dict[str, Any]:
    """Run Plain through the isolated service and project it into this workspace."""
    from agent_monitor import sponsored_kimi_client

    stop_event = _stop_event(run_id)

    def project(payload: dict[str, Any], *, terminal_status: str | None = None) -> dict[str, Any]:
        public_status = str(payload.get("status") or "running").lower()
        status = terminal_status or (
            public_status if public_status in {"finished", "failed", "stopped"} else "running"
        )
        events = [
            item for item in (payload.get("events") or [])
            if isinstance(item, dict)
        ][-60:]
        agents: list[dict[str, Any]] = []
        for index, item in enumerate(events, 1):
            output = str(item.get("text") or item.get("output") or "")[-12000:]
            agents.append(
                {
                    "trace_id": f"{run_id}::sponsored-{index}",
                    "stage_name": str(item.get("stage") or item.get("title") or "Plain"),
                    "role": str(item.get("role") or "agent"),
                    "pipeline_stage": "finalize" if status != "running" else "work",
                    "prompt": problem_text[:4000],
                    "output": output,
                    "status": status if status != "running" else str(item.get("status") or "running"),
                    "latency_s": time.time() - started,
                    "model": "kimi-k3",
                }
            )
        proof = normalize_proof_markdown(str(payload.get("proof") or ""))
        if status == "finished" and proof:
            agents.append(
                {
                    "trace_id": f"{run_id}::sponsored-final",
                    "stage_name": "Plain final answer",
                    "role": "agent",
                    "pipeline_stage": "finalize",
                    "prompt": problem_text[:4000],
                    "output": proof[-12000:],
                    "status": "finished",
                    "latency_s": time.time() - started,
                    "model": "kimi-k3",
                }
            )
        if not agents:
            agents = [
                {
                    "trace_id": f"{run_id}::sponsored-session",
                    "stage_name": "Sponsored Kimi Plain",
                    "role": "agent",
                    "pipeline_stage": "finalize" if status != "running" else "work",
                    "prompt": problem_text[:4000],
                    "output": str(payload.get("error") or "Waiting for isolated Kimi service"),
                    "status": status,
                    "latency_s": time.time() - started,
                    "model": "kimi-k3",
                }
            ]
        run = normalize_run(
            {
                "run_id": run_id,
                "problem_id": problem_id,
                "trace_name": f"[PLAIN] {problem_id}",
                "status": status,
                "error": str(payload.get("error") or "") or None,
                "live": {
                    "auth_mode": "api_key",
                    "model": "kimi-k3",
                    "model_requested": True,
                    "provider": "isolated-sponsored-kimi",
                    "tools_active": [],
                },
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for("plain"),
                "auth_route": "api_key",
                "credential_source": "sponsored_kimi",
                "problem_text": display_problem_text[:20000],
                "agents": agents,
                "edges": [
                    {"from": left["trace_id"], "to": right["trace_id"]}
                    for left, right in zip(agents, agents[1:])
                ],
                "totals": {"latency_s": time.time() - started},
                "runner_result": {
                    "transport": "isolated-sponsored-kimi",
                    "public_run_id": str(payload.get("run_id") or ""),
                },
            },
            engine="plain",
        )
        _attach_analysis(run, workspace)
        _write_run(run)
        return run

    payload = sponsored_kimi_client.run_plain(
        problem_text,
        client_id=owner_id,
        stopped=stop_event.is_set,
        on_update=lambda update: project(update),
    )
    status = str(payload.get("status") or "").lower()
    if stop_event.is_set() or status == "stopped":
        return project(payload, terminal_status="stopped")
    if status != "finished":
        return project(payload, terminal_status="failed")
    proof = normalize_proof_markdown(str(payload.get("proof") or "")).strip()
    if not proof:
        payload = {**payload, "error": "Sponsored Kimi finished without a proof artifact"}
        return project(payload, terminal_status="failed")
    _atomic_write_text(workspace / "proof.md", proof + "\n")
    return project(payload, terminal_status="finished")


def _apply_math_harness_iteration_env(
    env: dict[str, str],
    *,
    engine: str,
    max_iterations: int | None,
    problem_id: str | None = None,
) -> None:
    """Project the bounded UI iteration choice into the new adapter process."""
    if engine != "math_harness" or max_iterations is None:
        return
    bounded = max(1, min(int(max_iterations), 200))
    env["AGENT_MONITOR_MAX_ITERATIONS"] = str(bounded)
    env["MATH_HARNESS_MAX_ITERATIONS"] = str(bounded)
    if problem_id:
        env["AGENT_MONITOR_PROBLEM_ID"] = str(problem_id)[:240]


def _run_cli_engine(
    *,
    run_id: str,
    engine: str,
    problem_id: str,
    problem_text: str,
    started: float,
    workspace: Path,
    display_problem_text: str | None = None,
    extra_env: dict[str, str] | None = None,
    requested_model: str | None = None,
    max_iterations: int | None = None,
    owner_id: int | None = None,
    include_persona: bool = True,
) -> dict[str, Any]:
    """Run an external CLI harness (codex / openclaude / openhands / …) live."""
    import subprocess

    from agent_monitor.engines_registry import CLI_ENGINES, build_cli_command, proof_prompt

    spec = CLI_ENGINES.get(engine) or {}
    # Keep execution-only library/persona context out of the user-visible
    # canonical problem. Otherwise cloning a run injects the library again.
    record_problem_text = (
        display_problem_text if display_problem_text is not None else problem_text
    )
    # CLI engines can't read the agent home, so the operator's identity/skills/
    # memory ride along in the prompt.
    if (
        not include_persona
        or os.environ.get("AGENT_MONITOR_PUBLIC_KIMI", "").strip() == "1"
    ):
        preamble = ""
    else:
        try:
            from agent_monitor.agent_config import persona_preamble

            preamble = persona_preamble(workspace=workspace)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent-monitor] persona preamble unavailable: {exc}")
            preamble = ""
    prompt = proof_prompt(
        problem_text, workspace=workspace, preamble=preamble,
        response_only=engine in {"kimi", "plain"},
    )
    argv = build_cli_command(
        engine,
        prompt=prompt,
        workspace=workspace,
        problem_file=workspace / "problem.txt",
        model=requested_model,
    )
    if not argv:
        hint = spec.get("install_hint") or "engine not configured"
        return normalize_run(
            {
                "run_id": run_id,
                "problem_id": problem_id,
                "trace_name": f"[{spec.get('label', engine).upper()}] {problem_id}",
                "status": "failed",
                "error": f"{spec.get('label', engine)} is not installed / configured",
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for(engine),
                "problem_text": record_problem_text[:20000],
                "agents": [
                    {
                        "trace_id": f"{run_id}::setup",
                        "stage_name": "setup",
                        "role": "system",
                        "pipeline_stage": "work",
                        "prompt": problem_text[:2000],
                        "output": f"Engine unavailable.\n\nInstall / configure:\n{hint}",
                        "status": "failed",
                    }
                ],
                "totals": {"latency_s": 0},
            },
            engine=engine,  # type: ignore[arg-type]
        )

    from agent_monitor.cli_events import CLIEventParser

    parser = CLIEventParser(spec.get("parser_style") or engine)

    def _flush(
        output_tail: str,
        status: str = "running",
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        u = parser.usage
        # One node per model call/turn when the CLI streams JSON events;
        # otherwise fall back to a single session node (e.g. openhands TTY).
        agents = parser.agent_nodes(run_id, prompt=prompt, status=status)
        if not agents:
            agents = [
                {
                    "trace_id": f"{run_id}::{engine}_session",
                    "stage_name": f"{engine}_session",
                    "role": "agent",
                    "pipeline_stage": "finalize" if status != "running" else "work",
                    "prompt": prompt[:4000],
                    "output": output_tail[-12000:],
                    "status": status,
                    "latency_s": time.time() - started,
                    "model": u.get("model"),
                    "input_tokens": u.get("input_tokens") or None,
                    "output_tokens": u.get("output_tokens") or None,
                    "cache_read_tokens": u.get("cache_read_tokens") or 0,
                    "cache_write_tokens": u.get("cache_write_tokens") or 0,
                    "reasoning_tokens": u.get("reasoning_tokens") or None,
                    "cost_usd": u.get("cost_usd"),
                }
            ]
        elif status != "running":
            # Tokens the CLI only reported as session totals (not per turn)
            # go on the summary node so run totals stay correct.
            def _residual(field: str) -> int:
                return max(0, int(u.get(field) or 0) - sum(int(a.get(field) or 0) for a in agents))

            agents.append(
                {
                    "trace_id": f"{run_id}::{engine}_final",
                    "stage_name": f"{engine} session summary",
                    "role": "orchestrator",
                    "pipeline_stage": "finalize",
                    "call_seq": len(agents) + 1,
                    "prompt": prompt[:4000],
                    "prompt_source": "task prompt",
                    "output": output_tail[-12000:],
                    "output_source": f"{engine} activity log",
                    "memory_context": _workspace_memory(workspace),
                    "memory_source": "workspace snapshot",
                    "status": status,
                    "latency_s": time.time() - started,
                    "model": u.get("model"),
                    "input_tokens": _residual("input_tokens") or None,
                    "output_tokens": _residual("output_tokens") or None,
                    "cache_read_tokens": _residual("cache_read_tokens"),
                    "cache_write_tokens": _residual("cache_write_tokens"),
                    "reasoning_tokens": _residual("reasoning_tokens") or None,
                    "cost_usd": u.get("cost_usd"),
                }
            )
        edges = [
            {"from": a["trace_id"], "to": b["trace_id"]}
            for a, b in zip(agents, agents[1:])
        ]
        # Fill missing per-node costs from tokens + LiteLLM pricing.
        model = u.get("model")
        for a in agents:
            if a.get("cost_usd") is None and (a.get("input_tokens") or a.get("output_tokens")):
                a["cost_usd"] = _estimate_cost(
                    model or a.get("model"),
                    input_tokens=int(a.get("input_tokens") or 0),
                    output_tokens=int(a.get("output_tokens") or 0),
                    cache_read=int(a.get("cache_read_tokens") or 0),
                    cache_write=int(a.get("cache_write_tokens") or 0),
                )
        total_cost = u.get("cost_usd")
        if total_cost is None:
            total_cost = sum(float(a.get("cost_usd") or 0) for a in agents) or None
            if total_cost is None:
                total_cost = _estimate_cost(
                    model,
                    input_tokens=int(u.get("input_tokens") or 0),
                    output_tokens=int(u.get("output_tokens") or 0),
                    cache_read=int(u.get("cache_read_tokens") or 0),
                    cache_write=int(u.get("cache_write_tokens") or 0),
                )
        live = parser.live_state()
        if (extra_env or {}).get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
            live["auth_mode"] = "claude_subscription"
        elif (extra_env or {}).get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
            live["auth_mode"] = "chatgpt_subscription"
        else:
            live["auth_mode"] = "api_key"
        if status != "running":
            live["tools_active"] = []
        if not live.get("model"):
            # Codex and friends don't report a model in their event stream, so
            # fall back to what was asked for — flagged, since the CLI's own
            # configuration wins if it disagrees.
            live["model"] = requested_model or (extra_env or {}).get("AGENT_MONITOR_MODEL") or None
            live["model_requested"] = bool(live["model"])
        if requested_model:
            live["requested_model"] = requested_model
        run = normalize_run(
            {
                "run_id": run_id,
                "problem_id": problem_id,
                "trace_name": f"[{spec.get('label', engine).upper()}] {problem_id}",
                "status": status,
                "error": error,
                "model": live.get("model"),
                "requested_model": requested_model,
                "live": live,
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for(engine),
                "auth_route": _route_from_record({"live": live}),
                "problem_text": record_problem_text[:20000],
                "agents": agents,
                "edges": edges,
                "totals": {
                    "latency_s": time.time() - started,
                    "cost_usd": total_cost,
                    "input_tokens": u.get("input_tokens") or None,
                    "output_tokens": u.get("output_tokens") or None,
                },
                "runner_result": {"command": argv},
            },
            engine=engine,  # type: ignore[arg-type]
        )
        _attach_analysis(run, workspace)
        _write_run(run)
        return run

    _flush(f"$ {' '.join(argv[:6])}…\n\nstarting…")
    # Never let a per-user API run inherit an operator or another provider's
    # credential from the long-lived server process.  Start with ordinary
    # runtime settings, remove every metered route, then overlay only the
    # already-resolved environment for this run owner.  Subscription routes
    # are scrubbed again below after the overlay as defense in depth.
    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env(extra=extra_env)
    if engine == "kimi" and (extra_env or {}).get("AGENT_MONITOR_SPONSORED_KIMI") != "1":
        # Main-service availability flags are not authorization for this run.
        # Personal-key Kimi runs use the fixed public endpoint independently.
        env["AGENT_MONITOR_SPONSORED_KIMI"] = "0"
        env.pop("AGENT_MONITOR_SPONSORED_KIMI_URL", None)
    _apply_math_harness_iteration_env(
        env,
        engine=engine,
        max_iterations=max_iterations,
        problem_id=problem_id,
    )
    _apply_selected_model_env(env, engine=engine, model=requested_model)
    if engine == "codex" and _is_kimi_model(requested_model):
        # Raw Codex CLI ignores OPENAI_BASE_URL and its modern custom-provider
        # path emits OpenAI-specific extensions.  The wrapper uses an isolated
        # Responses provider plus a loopback sanitizer, while preserving native
        # Codex JSONL events for Conversation/Monitor/DAG.
        argv = [
            sys.executable,
            "-u",
            str(Path(__file__).resolve().parent / "runners" / "kimi_codex_runner.py"),
            prompt,
        ]
    elif engine == "codex":
        from agent_monitor.run_tuning import codex_config_args

        config_args = codex_config_args(env, requested_model)
        if config_args:
            insert_at = len(argv) - 1 if argv and argv[-1] == prompt else len(argv)
            argv[insert_at:insert_at] = config_args
    if engine == "openclaude" and max_iterations is not None:
        env["AGENT_MONITOR_OPENCLAUDE_MAX_TURNS"] = str(
            max(1, min(int(max_iterations), 200))
        )
    if engine == "claude" and max_iterations is not None:
        env["AGENT_MONITOR_CLAUDE_MAX_TURNS"] = str(
            max(1, min(int(max_iterations), 200))
        )
    if engine == "openhands" and max_iterations is not None:
        env["AGENT_MONITOR_OPENHANDS_MAX_ITERATIONS"] = str(
            max(1, min(int(max_iterations), 500))
        )
    if engine == "deepagents" and max_iterations is not None:
        # One LangGraph recursion step is smaller than one user-facing agent
        # iteration (assistant/tool transitions are separate graph nodes).
        env["AGENT_MONITOR_DEEPAGENTS_RECURSION_LIMIT"] = str(
            max(20, min(int(max_iterations) * 4, 800))
        )
    if env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        # Subscription is an explicit billing choice. Prevent a server- or
        # user-level API key from becoming an invisible fallback.
        _scrub_provider_route_env(env)
    if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
        # Claude account OAuth is not an Anthropic API key. Remove every API or
        # cloud-provider selector before the official CLI wrapper starts.
        _scrub_provider_route_env(env)
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AZURE_API_KEY",
            "AZURE_OPENAI_API_KEY",
        ):
            env.pop(name, None)
    env.setdefault("NO_COLOR", "1")
    from agent_monitor.engines_registry import engine_extra_path

    extra = engine_extra_path(engine)
    if extra:
        env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    if engine == "codex" and not _is_kimi_model(requested_model):
        _ensure_codex_auth(env, owner_id)
    # Cap node heap so openclaw survives this small-RAM host (no swap).
    if engine == "openclaw":
        env.setdefault("NODE_OPTIONS", "--max-old-space-size=256")
    # OpenHands headless boots from LLM_MODEL / LLM_API_KEY (--override-with-envs).
    if engine == "openhands":
        key = env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY")
        if key:
            env.setdefault("LLM_API_KEY", key)
            env.setdefault("LLM_MODEL", env.get("AGENT_MONITOR_OPENHANDS_MODEL", "openai/gpt-5.2"))
    timeout_s = _cli_timeout_seconds(engine, env)
    stop_ev = _stop_event(run_id)
    buf: list[str] = []
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(workspace),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        return _flush(f"failed to launch: {exc}", status="failed")

    _register_proc(run_id, proc)
    import queue
    import signal
    import threading

    def _terminate_group(*, force: bool = False) -> None:
        """Stop this wrapper and its grandchildren, without touching other runs."""
        _terminate_registered_proc(proc, force=force)
        # The leader may have exited while a tool still holds the output pipe.
        # This Popen always owns a fresh session, so its original PGID is safe.
        try:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                proc.kill() if force else proc.terminate()
            except OSError:
                pass

    output_queue: queue.Queue[str | None] = queue.Queue()

    def _read_output() -> None:
        assert proc.stdout is not None
        try:
            for output_line in proc.stdout:
                output_queue.put(output_line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(
        target=_read_output, name=f"{engine}-{run_id}-stdout", daemon=True
    )
    reader.start()
    stopped = False
    timed_out = False
    last_flush = 0.0
    deadline = started + timeout_s
    try:
        while True:
            now = time.time()
            if stop_ev.is_set():
                _terminate_group()
                parser.lines.append("[agent-monitor] stopped by user")
                stopped = True
                break
            if now > deadline:
                _terminate_group()
                parser.lines.append(
                    f"[agent-monitor] timeout after {timeout_s}s — terminated"
                )
                timed_out = True
                break
            try:
                line = output_queue.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                # A number of CLI harnesses emit one setup event and then stay
                # silent for the whole model call. Keep their run timestamp,
                # elapsed time, and active-state panel moving even when there
                # is no new stdout line to parse.
                if now - last_flush > 2.0:
                    _flush(parser.output())
                    last_flush = now
                continue
            if line is None:
                break
            buf.append(line)
            parser.feed(line)
            if now - last_flush > 2.0:
                _flush(parser.output())
                last_flush = now
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _terminate_group(force=True)
            proc.wait(timeout=10)
    finally:
        _terminate_group(force=True)
        reader.join(timeout=1)
        if proc.stdout is not None:
            proc.stdout.close()
        _unregister_proc(run_id)
    if engine == "openhands":
        parser.finalize_openhands()
    elif engine == "openclaw":
        parser.finalize_openclaw()
        _persist_openclaw_final_answer(parser, workspace)
    if stopped or stop_ev.is_set():
        return _flush(parser.output(), status="stopped")
    if engine in {"codex", "claude"} and parser.terminal_status is None:
        parser.terminal_error = "CLI ended without a terminal completion event"
    ok = proc.returncode == 0 and not timed_out and not parser.terminal_error
    proof_missing = False
    if ok and _promote_proof_artifact(workspace) is None:
        parser.lines.append("[agent-monitor] engine exited successfully but produced no proof.md or proof.tex")
        ok = False
        proof_missing = True
    failure_reason = None
    if not ok:
        label = str(spec.get("label") or engine)
        failure_reason = _cli_failure_reason(
            label=label,
            returncode=proc.returncode,
            timed_out=timed_out,
            timeout_s=timeout_s,
            proof_missing=proof_missing,
            detail=parser.terminal_error,
        )
    return _flush(
        parser.output(),
        status="finished" if ok else "failed",
        error=failure_reason,
    )


_OPENCLAUDE_EXECUTION_RETRY_MARKER = "OPENCLAUDE SAME-HARNESS RECOVERY:"


def _openclaude_recoverable_failure(error: str) -> str | None:
    """Classify bounded OpenClaude failures safe for one continuation."""
    normalized = error.strip().casefold()
    if normalized.endswith(": error_during_execution"):
        return "error_during_execution"
    if normalized.endswith(": error_max_turns"):
        return "error_max_turns"
    if normalized.startswith(
        "openclaude exited with code 1: stopped: repeated tool failures detected."
    ):
        return "repeated_tool_failures"
    return None


def _proof_checkpoint_signature(workspace: Path) -> tuple[str, int, str] | None:
    """Fingerprint a substantive, regular root proof without following symlinks."""
    artifact = _root_proof_artifact(workspace)
    if artifact is None or artifact.is_symlink():
        return None
    try:
        payload = artifact.read_bytes()
        if len(payload) < 400 or artifact.resolve().parent != workspace.resolve():
            return None
    except OSError:
        return None
    return artifact.name, len(payload), hashlib.sha256(payload).hexdigest()


def _openclaude_execution_retry_needed(
    *,
    engine: str,
    result: dict[str, Any],
    before_signature: tuple[str, int, str] | None,
    workspace: Path,
) -> bool:
    """Retry only a transient OpenClaude failure with a new current-run checkpoint."""
    if engine != "openclaude" or result.get("status") != "failed":
        return False
    error = str(result.get("error") or "")
    if _openclaude_recoverable_failure(error) is None:
        return False
    after_signature = _proof_checkpoint_signature(workspace)
    return after_signature is not None and after_signature != before_signature


def _run_cli_engine_with_openclaude_retry(**kwargs: Any) -> dict[str, Any]:
    """Run a CLI engine, retrying one narrow OpenClaude transport failure once.

    The retry remains on OpenClaude, begins only after this invocation wrote a
    new substantive root proof, and preserves both attempts in Monitor lineage.
    A stale artifact, another error subtype, or a failed retry remains failed.
    The second accepted subtype is OpenClaude's own repeated-edit guard: it is
    recoverable only when the failed attempt already produced a new proof
    checkpoint. A max-turn stop follows the same rule so a continuation can
    finish an already-substantial artifact without restarting broad research.
    Every continuation is still limited to one attempt.
    """
    workspace = Path(kwargs["workspace"])
    before_signature = _proof_checkpoint_signature(workspace)
    first = _run_cli_engine(**kwargs)
    engine = str(kwargs.get("engine") or "")
    if not _openclaude_execution_retry_needed(
        engine=engine,
        result=first,
        before_signature=before_signature,
        workspace=workspace,
    ):
        return first
    if _stop_event(str(kwargs.get("run_id") or "")).is_set():
        return first

    original_problem = str(kwargs.get("problem_text") or "")
    recovery = f"""
{_OPENCLAUDE_EXECUTION_RETRY_MARKER} The OpenClaude execution transport ended
after this invocation wrote a new proof checkpoint. Continue once with the
same selected OpenClaude harness. Read the current root proof artifact first,
preserve every supported result, and finish the original request. Do not start
a new broad search and do not switch engines. If the prior stop reported
repeated tool failures, re-read the file before editing and use a fresh safe
whole-file write instead of repeating a stale exact-text edit. If CANDIDATE AUDIT GATE is
active, complete and validate its required audit files or disclose the exact
remaining degradation. A retry does not promote an open-problem outcome.
""".strip()
    retry_kwargs = dict(kwargs)
    retry_kwargs["problem_text"] = original_problem.rstrip() + "\n\n" + recovery
    retry_kwargs["started"] = time.time()
    second = _run_cli_engine(**retry_kwargs)
    retry_id = f"{kwargs.get('run_id') or 'run'}-openclaude-retry-1"
    merged = _merge_continuation_run(first, second, job_id=retry_id)
    merged["internal_retry_count"] = 1
    subtype = _openclaude_recoverable_failure(str(first.get("error") or ""))
    merged["internal_retry_reason"] = f"{subtype}_after_new_checkpoint"
    return merged


def _wrap_subprocess_result(
    *,
    run_id: str,
    engine: str,
    problem_id: str,
    problem_text: str,
    result: dict[str, Any],
    started: float,
    workspace: Path | None = None,
) -> dict[str, Any]:
    status = result.get("status") or "finished"
    ok = status in {"finished", "ok"}
    output = (
        (result.get("stdout_tail") or "")
        + ("\n\n--- stderr ---\n" + (result.get("stderr_tail") or "") if result.get("stderr_tail") else "")
        + ("\n\n" + (result.get("error") or "") if result.get("error") else "")
        + ("\n\n" + (result.get("hint") or "") if result.get("hint") else "")
    )
    proof = _promote_proof_artifact(workspace) if ok else None
    if ok and proof is None:
        ok = False
        status = "failed"
        result = {**result, "error": "Engine exited successfully but produced no proof artifact"}
        output = (output + "\n\n" + result["error"]).strip()
    research_audit_status = (
        workspace_reconciliation_status(workspace)
        if workspace is not None
        and _RESEARCH_AUDIT_GATE_MARKER in problem_text
        else {"status": "not_requested", "valid": False, "audits": []}
    )
    run = normalize_run(
        {
            "run_id": run_id,
            "problem_id": problem_id,
            "trace_name": f"[{engine.upper()}] {problem_id}",
            "status": "finished" if ok else "failed",
            "error": result.get("error"),
            "updated_at": _now(),
            "pipeline": _pipeline_for(engine),
            "problem_text": problem_text[:20000],
            "agents": [
                {
                    "trace_id": f"{run_id}::{engine}_main",
                    "stage_name": f"{engine}_main",
                    "role": "orchestrator",
                    "pipeline_stage": "finalize" if ok else "workflow",
                    "prompt": problem_text[:4000],
                    "output": output[-12000:] or json.dumps(result, indent=2)[:8000],
                    "status": "finished" if ok else "failed",
                    "latency_s": time.time() - started,
                }
            ],
            "edges": [],
            "totals": {"latency_s": time.time() - started},
            "workspace": str(workspace) if workspace else None,
            "runner_result": {
                k: result.get(k)
                for k in (
                    "status",
                    "returncode",
                    "workflow",
                    "command",
                    "hint",
                    "output_dir",
                    "supplemental_audit_status",
                    "supplemental_audit_returncode",
                    "promoted_audit_files",
                )
            }
            | {"research_audit": research_audit_status},
        },
        engine=engine,  # type: ignore[arg-type]
    )
    if workspace:
        rich = _artifact_agents(engine, workspace, run_id)
        if rich and rich.get("agents"):
            run = normalize_run(
                _merge_artifact_run(run, rich),
                engine=engine,  # type: ignore[arg-type]
            )
    if not run.get("analysis"):
        _attach_analysis(run, workspace)
    return run


def _cleanup_hermes_memory(run_id: str, workspace: str | None) -> list[str]:
    """Remove Hermes session dumps / DB rows tied to this run."""
    import sqlite3

    removed: list[str] = []
    sessions_dir = HERMES_HOME / "sessions"
    if not sessions_dir.is_dir():
        return removed

    needles = [run_id]
    if workspace:
        needles.append(workspace)
        needles.append(Path(workspace).name)

    session_ids: set[str] = set()
    for path in list(sessions_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(n and n in text for n in needles):
            continue
        name = path.name
        if name.startswith("request_dump_"):
            rest = name.removeprefix("request_dump_")
            parts = rest.split("_")
            if len(parts) >= 2:
                session_ids.add(f"{parts[0]}_{parts[1]}")
        try:
            path.unlink()
            removed.append(name)
        except OSError:
            pass

    for sid in session_ids:
        for extra in sessions_dir.glob(f"*{sid}*"):
            if extra.is_file() and extra.name not in removed:
                try:
                    extra.unlink()
                    removed.append(extra.name)
                except OSError:
                    pass

    db = HERMES_HOME / "state.db"
    if db.exists() and session_ids:
        try:
            with sqlite3.connect(db) as conn:
                for sid in session_ids:
                    conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                    conn.execute(
                        "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
                        (sid,),
                    )
                    conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                conn.commit()
        except sqlite3.Error:
            pass

    return removed


def _chat_file(run_id: str) -> Path:
    return RUNS_DIR / "workspaces" / run_id / "human_chat.jsonl"


def list_chat(run_id: str) -> list[dict[str, Any]]:
    path = _chat_file(run_id)
    msgs: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Older/evaluation continuations could persist reviewer guidance in the
    # session problem ledger without first emitting a user chat event. Keep
    # those entries out of Sandbox Problem cards, but recover them here so the
    # guidance remains visible in Human in the loop and exported Conversation.
    seen_user: set[str] = set()
    for message in msgs:
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "").strip()
        seen_user.add(content)
        # A turn records the prompt-facing form of its text explicitly. Older
        # records predate that field, so also reconstruct it from the files.
        if message.get("ledger_text"):
            seen_user.add(str(message["ledger_text"]).strip())
        elif message.get("attachments"):
            seen_user.add(
                content
                + "\n\nAttached research files:\n"
                + "\n".join(
                    f"- {json.dumps(item['name'], ensure_ascii=False)}"
                    for item in message["attachments"]
                )
            )
    try:
        run = _load_run_record(run_id)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        run = {}
    for item in run.get("problems") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip().lower()
        text = _problem_item_text(item)
        if source not in {"human", "hitl", "feedback"} or not text or text in seen_user:
            continue
        seen_user.add(text)
        msgs.append(
            {
                "role": "user",
                "content": text,
                "ts": item.get("ts") or run.get("updated_at") or "",
                "recovered_from_session": True,
            }
        )
    return msgs


def _append_chat(
    run_id: str,
    role: str,
    content: str,
    *,
    attachments: list[dict] | None = None,
    ledger_text: str | None = None,
) -> dict[str, Any]:
    path = _chat_file(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"role": role, "content": content, "ts": _now()}
    if attachments:
        entry["attachments"] = attachments
    if ledger_text and ledger_text != content:
        # The session ledger stores the prompt-facing form of this turn. Record
        # it so ledger recovery below does not re-add the turn as a duplicate.
        entry["ledger_text"] = ledger_text
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _run_is_active(run_id: str) -> dict[str, Any] | None:
    with _LOCK:
        for j in _JOBS.values():
            if j.get("run_id") == run_id and j.get("status") == "running":
                return dict(j)
    return None


def _load_run_record(run_id: str) -> dict[str, Any]:
    for path in (_cache_dir() / f"{run_id}.json", RUNS_DIR / f"{run_id}.json"):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"run not found: {run_id}")


def _record_age_seconds(run: dict[str, Any]) -> float:
    raw = str(run.get("updated_at") or run.get("created_at") or "")
    if not raw:
        return float("inf")
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except ValueError:
        return float("inf")


def reconcile_run_status(
    run_id: str,
    *,
    min_age_seconds: float = 10.0,
    reason: str = "The server restarted or the worker process exited without a terminal record.",
) -> dict[str, Any] | None:
    """Turn an orphaned persisted `running` record into `interrupted`.

    In-memory jobs are authoritative while this process is alive. Persisted
    `running` records cannot survive a server restart because engine workers
    are daemon threads/child processes owned by this service.
    """
    with _RECONCILE_LOCK:
        if _run_is_active(run_id):
            try:
                return _load_run_record(run_id)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return None
        try:
            run = _load_run_record(run_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        auto_pipeline_enabled = (
            os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "").strip()
            != "1"
        )
        if str(run.get("status") or "") != "running":
            workspace = Path(str(run.get("workspace") or workspace_dir(run_id)))
            if auto_pipeline_enabled:
                try:
                    from agent_monitor import auto_pipeline

                    auto_pipeline.reconcile_orphaned(workspace, reason)
                except (OSError, ValueError):
                    pass
            return run
        if _record_age_seconds(run) < max(0.0, min_age_seconds):
            return run

        detected_at = _now()
        last_activity = run.get("updated_at") or run.get("created_at")
        run["status"] = "interrupted"
        run["completed"] = False
        run["updated_at"] = detected_at
        run["interruption"] = {
            "detected_at": detected_at,
            "last_activity_at": last_activity,
            "reason": reason,
            "recoverable": True,
        }
        live = dict(run.get("live") or {})
        live["tools_active"] = []
        live["interrupted"] = True
        run["live"] = live
        for agent in run.get("agents") or []:
            if isinstance(agent, dict) and agent.get("status") == "running":
                agent["status"] = "interrupted"
                agent["stop_reason"] = "server_restart_or_worker_lost"
        _write_run(run)

        workspace = Path(str(run.get("workspace") or workspace_dir(run_id)))
        if auto_pipeline_enabled:
            try:
                from agent_monitor import auto_pipeline

                auto_pipeline.mark_interrupted(workspace, reason)
            except (OSError, ValueError):
                pass
        _append_chat(
            run_id,
            "system",
            "Run interrupted: its worker is no longer active. "
            "The saved proof and workspace are intact; press Continue to resume.",
        )
        return run


def reconcile_stale_running_runs(*, min_age_seconds: float = 10.0) -> list[str]:
    """Reconcile every manifest entry still marked running without a live job."""
    manifest_path = _cache_dir() / "manifest.json"
    run_ids: list[str] = []
    if manifest_path.exists():
        try:
            entries = (json.loads(manifest_path.read_text(encoding="utf-8")) or {}).get("runs") or []
            run_ids = [
                str(entry.get("run_id") or "")
                for entry in entries
                if entry.get("run_id") and entry.get("status") == "running"
            ]
        except (AttributeError, json.JSONDecodeError, OSError):
            run_ids = []
    if not run_ids:
        for path in _cache_dir().glob("*.json"):
            if path.name == "manifest.json":
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if value.get("run_id") and value.get("status") == "running":
                run_ids.append(str(value["run_id"]))

    # A main harness may already be terminal while its concurrent Lean/DAG
    # sidecar was lost during a server restart.  Such runs are intentionally
    # absent from the `running` manifest subset above, so discover persisted
    # sidecars independently and let reconcile_run_status close them.
    workspace_root = RUNS_DIR / "workspaces"
    if (
        os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "").strip() != "1"
        and workspace_root.is_dir()
    ):
        for state_path in workspace_root.glob("*/auto_pipeline.json"):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if str(state.get("status") or "") == "running":
                run_ids.append(state_path.parent.name)

    reconciled: list[str] = []
    for run_id in dict.fromkeys(run_ids):
        before = None
        try:
            before = _load_run_record(run_id).get("status")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        result = reconcile_run_status(run_id, min_age_seconds=min_age_seconds)
        if before == "running" and result and result.get("status") == "interrupted":
            reconciled.append(run_id)
    return reconciled


_PENDING_FEEDBACK_FILENAME = "human_feedback_pending.jsonl"


def _canonical_claude_continuation_model(value: object) -> str | None:
    """Map resolved Claude telemetry back to a selectable account model id."""
    selected = str(value or "").strip().lower()
    if selected in {"sonnet", "opus", "fable", "claude-haiku-4-5"}:
        return selected
    for family in ("sonnet", "opus", "fable"):
        if selected.startswith(f"claude-{family}-"):
            return family
    if selected.startswith("claude-haiku-4-5-"):
        return "claude-haiku-4-5"
    return None


def _continuation_model(run_data: dict[str, Any], requested: str | None) -> str | None:
    """Resolve the model a follow-up should display and actually reuse."""
    explicit = str(requested or "").strip()
    if explicit:
        # Explicit input is validated against the recorded route below; never
        # reinterpret an invalid user selection as another account model.
        return explicit
    live = run_data.get("live") or {}
    agents = run_data.get("agents") or []
    candidates: list[Any] = [
        run_data.get("requested_model"),
        live.get("requested_model") if isinstance(live, dict) else None,
        run_data.get("model"),
    ]
    candidates.extend(
        agent.get("model")
        for agent in reversed(agents)
        if isinstance(agent, dict)
    )
    candidates.append(live.get("model") if isinstance(live, dict) else None)
    if _route_from_record(run_data) == "claude_subscription":
        for candidate in candidates:
            canonical = _canonical_claude_continuation_model(candidate)
            if canonical:
                return canonical
        if any(
            str(candidate or "").strip() not in {"", "—"}
            for candidate in candidates
        ):
            raise ValueError(
                "This run stored an unsupported Claude account model; "
                "choose sonnet, opus, fable, or claude-haiku-4-5"
            )
        return None
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _queue_human_feedback(workspace: Path, message: str) -> None:
    """Durably append feedback that arrived while an engine was active."""
    path = workspace / _PENDING_FEEDBACK_FILENAME
    entry = {"content": message, "ts": _now()}
    with _LOCK:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _drain_human_feedback(workspace: Path) -> list[str]:
    """Atomically consume queued feedback, including the legacy text file."""
    queued: list[str] = []
    with _LOCK:
        jsonl_path = workspace / _PENDING_FEEDBACK_FILENAME
        legacy_path = workspace / "human_feedback_pending.txt"
        paths = [path for path in (jsonl_path, legacy_path) if path.exists()]
        for path in paths:
            try:
                raw = path.read_text(encoding="utf-8").strip()
                path.unlink()
            except OSError:
                continue
            if not raw:
                continue
            if path == legacy_path:
                queued.append(raw)
                continue
            for line in raw.splitlines():
                try:
                    content = str((json.loads(line) or {}).get("content") or "").strip()
                except (AttributeError, json.JSONDecodeError):
                    content = line.strip()
                if content:
                    queued.append(content)
    return queued


def _launch_pending_feedback(
    run_id: str, *, owner_id: int | None, model: str | None, max_iterations: int
) -> dict[str, Any] | None:
    """Start exactly one follow-up after the current engine reaches a terminal state."""
    if _stop_event(run_id).is_set() or _run_is_active(run_id):
        return None
    workspace = RUNS_DIR / "workspaces" / run_id
    queued = _drain_human_feedback(workspace)
    if not queued:
        return None
    message = "\n\n".join(queued)
    _append_chat(
        run_id,
        "system",
        f"Applying {len(queued)} queued human message{'s' if len(queued) != 1 else ''} now that the harness finished.",
    )
    try:
        return continue_run(
            run_id,
            message=message,
            model=model,
            max_iterations=max_iterations,
            user={"id": owner_id} if owner_id is not None else None,
            problems_recorded=True,
        )
    except Exception as exc:  # noqa: BLE001
        for item in queued:
            _queue_human_feedback(workspace, item)
        _append_chat(run_id, "system", f"Queued feedback could not start yet: {exc}")
        return None


def send_human_message(
    run_id: str,
    message: str,
    *,
    model: str | None = None,
    max_iterations: int = 40,
    user: dict[str, Any] | None = None,
    attachments: object = None,
) -> dict[str, Any]:
    """Human-in-the-loop chat: queue feedback or continue a finished run."""
    message = (message or "").strip()
    attachment_batch = research_attachments.validate(attachments)
    if not message and not attachment_batch:
        raise ValueError("empty message")
    ws = RUNS_DIR / "workspaces" / run_id
    if not ws.is_dir():
        raise FileNotFoundError("workspace not found")

    attached = research_attachments.save(ws, attachment_batch)
    display_message = message or (
        "Use these additional research findings to continue the proof."
    )
    if attached:
        message = display_message + "\n\nAttached research files:\n" + "\n".join(
            f"- {json.dumps(item['name'], ensure_ascii=False)}" for item in attached
        )
    else:
        message = display_message
    user_entry = _append_chat(
        run_id, "user", display_message, attachments=attached, ledger_text=message
    )
    try:
        run_data = _load_run_record(run_id)
    except FileNotFoundError:
        run_data = {"run_id": run_id}
    problems = ensure_problems(run_data, run_id)
    effective_model = _continuation_model(run_data, model)
    if not any(_problem_item_text(p) == message for p in problems):
        problems.append({"text": message, "source": "human", "ts": _now()})
    run_data["problems"] = problems
    run_data["problem_text_preview"] = _session_title(problems, run_data.get("problem_text") or message)
    if run_data.get("run_id"):
        _write_run(run_data)

    active = _run_is_active(run_id)
    if active:
        _queue_human_feedback(ws, message)
        _append_chat(
            run_id,
            "system",
            "Feedback queued; it will run automatically as soon as the current harness finishes.",
        )
        return {
            "ok": True,
            "status": "queued",
            "job_id": active.get("job_id"),
            "messages": list_chat(run_id),
            "user": user_entry,
        }

    # Do not expose queued feedback to the process that is still running. The
    # continuation writes the updated notebook at the start of
    # ``_execute_continue``; writing it before the active check lets the
    # current harness consume a message that is also queued for the next turn.
    _write_session_problems(ws, problems)

    _stop_event(run_id).clear()
    engine = run_data.get("engine") or run_id.split("_", 1)[0]
    first_problem = _problem_item_text(problems[0]) if problems else (
        run_data.get("problem_text") or message
    )

    continue_owner_id = (user or {}).get("id") or run_data.get("owner_id")
    continue_env, continued_route = _continuation_auth_environment(
        run_data=run_data,
        engine=engine,
        model=effective_model,
        user=user,
        env=_user_extra_env(user, effective_model, run_data.get("subagent_model")),
    )
    job_id = uuid.uuid4().hex[:10]
    with _LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "run_id": run_id,
            "engine": engine,
            "problem_id": run_data.get("problem_id"),
            "status": "running",
            "created_at": _now(),
            "updated_at": _now(),
            "error": None,
            "model": effective_model,
            "kind": "continue",
            "owner_id": continue_owner_id,
            "auth_route": continued_route,
            "reasoning_effort": run_data.get("reasoning_effort", "default"),
            "speed_mode": run_data.get("speed_mode", "standard"),
            "problem_preview": _session_title(problems, first_problem),
        }

    _append_chat(run_id, "system", "Continuing proof with your feedback…")
    continued_subagents = bool(run_data.get("use_subagents", True))
    continue_env["AGENT_MONITOR_USE_SUBAGENTS"] = "1" if continued_subagents else "0"
    continued_subagent_model = run_data.get("subagent_model") if continued_subagents else None
    if continued_subagent_model:
        continue_env["AGENT_MONITOR_SUBAGENT_MODEL"] = str(continued_subagent_model)
    else:
        continue_env.pop("AGENT_MONITOR_SUBAGENT_MODEL", None)
    thread = threading.Thread(
        target=_execute_continue,
        kwargs={
            "job_id": job_id,
            "run_id": run_id,
            "engine": engine,
            "message": message,
            "problem_text": first_problem,
            "model": effective_model,
            "max_iterations": max_iterations,
            "workspace": str(ws),
            "extra_env": continue_env,
            "owner_id": continue_owner_id,
        },
        daemon=True,
        name=f"continue-{run_id}-{job_id}",
    )
    thread.start()
    return {
        "ok": True,
        "status": "continuing",
        "job_id": job_id,
        "messages": list_chat(run_id),
        "user": user_entry,
    }



def _continuation_proof_excerpt(workspace: Path) -> tuple[str, str]:
    """Return a bounded root proof excerpt without following workspace links."""
    from agent_monitor import proof_graph

    for name in ("proof.md", "proof.tex"):
        text = proof_graph._read_workspace_text(
            workspace, name, limit=2 * 1024 * 1024
        )
        if text:
            return name, text[:8000]
    return "proof.md", ""


def _execute_continue(
    *,
    job_id: str,
    run_id: str,
    engine: str,
    message: str,
    problem_text: str,
    model: str | None,
    max_iterations: int,
    workspace: str,
    extra_env: dict[str, str] | None = None,
    owner_id: int | None = None,
    problems_recorded: bool = False,
) -> None:
    started = time.time()
    ws = Path(workspace)
    extra_env = dict(extra_env or {})
    _apply_kimi_compatible_env(extra_env, model)
    try:
        run_data = _load_run_record(run_id)
    except FileNotFoundError:
        run_data = {"run_id": run_id, "problem_id": "continue"}
    previous_run_data = dict(run_data)
    problems = ensure_problems(run_data, run_id)
    if (
        message
        and not problems_recorded
        and not any(_problem_item_text(p) == message.strip() for p in problems)
    ):
        if not message.strip().startswith("Continue from where the previous session stopped"):
            problems.append({"text": message.strip(), "source": "human", "ts": _now()})
            run_data["problems"] = problems
    _write_session_problems(ws, problems)

    proof_name, proof_excerpt = _continuation_proof_excerpt(ws)

    numbered = []
    for i, item in enumerate(problems, 1):
        text = _problem_item_text(item)
        if not text:
            continue
        tag = "  ← new this turn" if i == len(problems) and (item.get("source") if isinstance(item, dict) else "") == "human" else ""
        numbered.append(f"{i}. {text}{tag}")
    problems_block = "\n".join(numbered) or problem_text

    response_only = engine in {"kimi", "plain"}
    workspace_instruction = (
        "Return the complete updated Markdown write-up; the application saves it automatically.\n"
        if response_only else f"WORKSPACE: {ws}\n"
    )
    revision_instruction = (
        "If the human asked to revise an existing proof, include its revised version "
        "in the returned write-up and leave the other proofs intact.\n\n"
        if response_only else
        "If the human asked to revise an existing proof, edit that proof in place "
        "and leave the others intact.\n\n"
    )
    continuation = (
        "You are continuing an informal mathematics session based on human feedback.\n"
        f"{workspace_instruction}"
        f"{proof_name} is a notebook for this WHOLE session. "
        "Do NOT delete or replace earlier proofs. "
        "If the human asked a new theorem, keep the existing write-up intact and "
        "continue with another proof that starts the same way as the first "
        "(a `# Claim` heading, then Strategy / Setup / Lemmas / Main proof). "
        "Do NOT add 'Problem N' or 'Proof N' headings — the console already shows "
        "each user request as its own problem card. "
        f"{revision_instruction}"
        f"SESSION PROBLEMS:\n{problems_block}\n\n"
    )
    if proof_excerpt:
        continuation += f"CURRENT {proof_name} (preserve this content, then extend it):\n{proof_excerpt}\n\n"
    continuation += f"HUMAN FEEDBACK — address this now:\n{message}\n"
    attachment_context = research_attachments.context(ws)
    if attachment_context:
        continuation += "\n\n" + attachment_context

    run_data["status"] = "running"
    run_data["updated_at"] = _now()
    run_data["problems"] = problems
    _write_run(run_data)

    try:
        if engine == "improof":
            result_run = _run_improof_continuation(
                run_id=run_id,
                job_id=job_id,
                problem_id=run_data.get("problem_id") or "continue",
                problem_text=continuation,
                display_problem_text=problem_text,
                model=model,
                started=started,
                workspace=ws,
                extra_env=extra_env,
                baseline_run=previous_run_data,
            )
        elif engine in {"hermes", "ucla"}:
            # UCLA is not resumable mid-flight; its legacy refinement path
            # remains unchanged. IMProof continuations stay on IMProof above.
            result_run = _run_hermes(
                run_id=run_id,
                problem_id=run_data.get("problem_id") or "continue",
                problem_text=continuation,
                display_problem_text=problem_text,
                model=model,
                max_iterations=max_iterations,
                started=started,
                workspace=ws,
                extra_env=extra_env,
                use_subagents=bool(run_data.get("use_subagents", True)),
                subagent_model=run_data.get("subagent_model"),
                owner_id=owner_id,
            )
            if engine != "hermes":
                result_run["engine"] = engine
                result_run["source"] = engine
                result_run["trace_name"] = run_data.get("trace_name") or result_run.get("trace_name")
        else:
            result_run = _run_cli_engine_with_openclaude_retry(
                run_id=run_id,
                engine=engine,
                problem_id=run_data.get("problem_id") or "continue",
                problem_text=continuation,
                started=started,
                workspace=ws,
                display_problem_text=problem_text,
                extra_env=extra_env,
                requested_model=model or (run_data.get("live") or {}).get("model"),
                max_iterations=max_iterations,
                owner_id=owner_id,
            )
        result_run = _merge_continuation_run(
            previous_run_data, result_run, job_id=job_id
        )
        result_run["job_id"] = job_id
        result_run["workspace"] = str(ws)
        result_run["use_subagents"] = bool(run_data.get("use_subagents", True))
        result_run["subagent_model"] = (
            run_data.get("subagent_model") if result_run["use_subagents"] else None
        )
        result_run["updated_at"] = _now()
        result_run["problems"] = problems
        _write_run(result_run)
        status = "finished" if result_run.get("status") != "failed" else "failed"
        _update_job(job_id, status=status, error=result_run.get("error"))
        _append_chat(
            run_id,
            "assistant",
            (result_run.get("agents") or [{}])[-1].get("output", "")[:2000] or f"Run {status}.",
        )
        if _root_proof_artifact(ws):
            try:
                from agent_monitor import auto_pipeline

                auto_pipeline.start(
                    run_id=run_id,
                    workspace=ws,
                    owner_id=owner_id,
                    model=model,
                    engine=engine,
                )
            except Exception as exc:  # noqa: BLE001 - derived views are best-effort
                _append_chat(
                    run_id,
                    "system",
                    f"Automatic Lean/DAG refresh could not start: {exc}",
                )
    except StopRequested:
        stopped_run = _load_run_record(run_id)
        stopped_run["status"] = "stopped"
        stopped_run["updated_at"] = _now()
        _write_run(stopped_run)
        _update_job(job_id, status="stopped")
        _append_chat(run_id, "system", "Run stopped by user.")
    except Exception as exc:  # noqa: BLE001
        try:
            failed_run = _load_run_record(run_id)
            failed_run["status"] = "failed"
            failed_run["error"] = str(exc)
            failed_run["updated_at"] = _now()
            _write_run(failed_run)
        except FileNotFoundError:
            pass
        _update_job(job_id, status="failed", error=str(exc))
        _append_chat(run_id, "assistant", f"I could not apply that feedback: {exc}")
    finally:
        _launch_pending_feedback(
            run_id, owner_id=owner_id, model=model, max_iterations=max_iterations
        )


def _run_improof_continuation(
    *,
    run_id: str,
    job_id: str,
    problem_id: str,
    problem_text: str,
    display_problem_text: str,
    model: str | None,
    started: float,
    workspace: Path,
    extra_env: dict[str, str] | None,
    baseline_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # A continuation is a fresh native Author/Critic workflow over the saved
    # notebook, rather than a Hermes run relabelled as IMProof.
    from agent_monitor.runners import improof as improof_runner

    prompt_path = workspace / f".improof-continue-{job_id}.txt"
    prompt_path.write_text(problem_text, encoding="utf-8")
    improof_args = None
    if model and (extra_env or {}).get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        improof_args = [
            "--component", f"cfg_codex_author.model={model}",
            "--component", f"cfg_codex_critic.model={model}",
        ]
    elif model and (extra_env or {}).get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
        improof_args = [
            "--component", f"cfg_claude_author.model={model}",
            "--component", f"cfg_claude_critic.model={model}",
        ]
    try:
        result = improof_runner.run_problem(
            prompt_path,
            problem_id=problem_id,
            output_dir=workspace,
            extra_args=improof_args,
            extra_env=extra_env,
            research_model=model,
            on_start=lambda process: _register_proc(run_id, process),
            on_output=_live_output_flusher(
                run_id=run_id,
                engine="improof",
                problem_id=problem_id,
                problem_text=display_problem_text,
                started=started,
                workspace=workspace,
                baseline_run=baseline_run,
                continuation_job_id=job_id,
            ),
        )
    finally:
        _unregister_proc(run_id)
        prompt_path.unlink(missing_ok=True)
    return _wrap_subprocess_result(
        run_id=run_id,
        engine="improof",
        problem_id=problem_id,
        problem_text=display_problem_text,
        result=result,
        started=started,
        workspace=workspace,
    )


def stop_run(run_id: str) -> dict[str, Any]:
    """Stop a running job: signal the stop event and kill any subprocess."""
    active = _run_is_active(run_id)
    _stop_event(run_id).set()
    with _LOCK:
        proc = _PROCS.get(run_id)
    if proc is not None:
        _terminate_registered_proc(proc)
    if not active:
        return {"ok": True, "status": "not_running", "run_id": run_id}
    try:
        run_data = _load_run_record(run_id)
        run_data["status"] = "stopped"
        run_data["updated_at"] = _now()
        _write_run(run_data)
    except FileNotFoundError:
        pass
    _update_job(active["job_id"], status="stopped")
    _append_chat(run_id, "system", "Run stopped by user.")
    return {"ok": True, "status": "stopped", "run_id": run_id, "job_id": active["job_id"]}


def continue_run(
    run_id: str,
    *,
    message: str | None = None,
    model: str | None = None,
    max_iterations: int = 40,
    user: dict[str, Any] | None = None,
    problems_recorded: bool = False,
) -> dict[str, Any]:
    """Resume a stopped/finished/failed run, optionally with extra guidance."""
    if _run_is_active(run_id):
        return {"ok": False, "status": "already_running", "run_id": run_id}
    _stop_event(run_id).clear()

    ws = RUNS_DIR / "workspaces" / run_id
    if not ws.is_dir():
        raise FileNotFoundError("workspace not found")
    run_data = _load_run_record(run_id)
    effective_model = _continuation_model(run_data, model)
    # Stored engine identity is provenance, not a catalog alias. In particular,
    # an old UCLA continuation must stay on its explicit legacy path below and
    # must never be rewritten to ``math_harness``.
    engine = run_data.get("engine") or run_id.split("_", 1)[0]
    problems = ensure_problems(run_data, run_id)
    first_problem = _problem_item_text(problems[0]) if problems else (
        run_data.get("problem_text") or ""
    )
    feedback = (message or "").strip() or (
        "Continue from where the previous session stopped. Review the workspace, "
        "then keep improving proof.md until every session problem has a complete, correct proof."
    )

    job_id = uuid.uuid4().hex[:10]
    continue_owner_id = (user or {}).get("id") or run_data.get("owner_id")
    continue_env, continued_route = _continuation_auth_environment(
        run_data=run_data,
        engine=engine,
        model=effective_model,
        user=user,
        env=_user_extra_env(user, effective_model, run_data.get("subagent_model")),
    )
    continued_subagents = bool(run_data.get("use_subagents", True))
    continue_env["AGENT_MONITOR_USE_SUBAGENTS"] = "1" if continued_subagents else "0"
    continued_subagent_model = run_data.get("subagent_model") if continued_subagents else None
    if continued_subagent_model:
        continue_env["AGENT_MONITOR_SUBAGENT_MODEL"] = str(continued_subagent_model)
    else:
        continue_env.pop("AGENT_MONITOR_SUBAGENT_MODEL", None)
    with _LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "run_id": run_id,
            "engine": engine,
            "problem_id": run_data.get("problem_id"),
            "status": "running",
            "created_at": _now(),
            "updated_at": _now(),
            "error": None,
            "model": effective_model,
            "kind": "continue",
            "owner_id": continue_owner_id,
            "auth_route": continued_route,
            "reasoning_effort": run_data.get("reasoning_effort", "default"),
            "speed_mode": run_data.get("speed_mode", "standard"),
            "problem_preview": _session_title(problems, first_problem),
        }
    _append_chat(run_id, "system", "Continuing run…")
    thread = threading.Thread(
        target=_execute_continue,
        kwargs={
            "job_id": job_id,
            "run_id": run_id,
            "engine": engine,
            "message": feedback,
            "problem_text": first_problem,
            "model": effective_model,
            "max_iterations": max_iterations,
            "workspace": str(ws),
            "extra_env": continue_env,
            "owner_id": continue_owner_id,
            "problems_recorded": problems_recorded,
        },
        daemon=True,
        name=f"continue-{run_id}-{job_id}",
    )
    thread.start()
    return {"ok": True, "status": "continuing", "run_id": run_id, "job_id": job_id}


def delete_run(run_id: str) -> dict[str, Any]:
    """Delete a run, its workspace, cache JSON, manifest entry, and Hermes memory."""
    if not run_id or "/" in run_id or ".." in run_id:
        raise ValueError("invalid run_id")

    cache = _cache_dir()
    workspace_path: str | None = None
    run_json = cache / f"{run_id}.json"
    if run_json.exists():
        try:
            workspace_path = json.loads(run_json.read_text(encoding="utf-8")).get("workspace")
        except (json.JSONDecodeError, OSError):
            workspace_path = None

    removed: dict[str, Any] = {"run_id": run_id, "deleted": []}

    # Usage history outlives the run; only the trace link is retired.
    from agent_monitor import usage_ledger

    usage_ledger.mark_deleted(run_id)

    _stop_event(run_id).set()
    with _LOCK:
        proc = _PROCS.pop(run_id, None)
        _DELETED_RUNS.add(run_id)
        for jid in [k for k, j in _JOBS.items() if j.get("run_id") == run_id]:
            _JOBS.pop(jid, None)
    if proc is not None:
        _terminate_registered_proc(proc)

    manifest_path = cache / "manifest.json"
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["runs"] = [
                r for r in (payload.get("runs") or []) if r.get("run_id") != run_id
            ]
            payload["built_at"] = _now()
            manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            removed["deleted"].append(str(manifest_path))
        except (json.JSONDecodeError, OSError):
            pass

    for path in (run_json, RUNS_DIR / f"{run_id}.json"):
        if path.exists():
            path.unlink()
            removed["deleted"].append(str(path))

    pdf = cache / "pdf" / f"{run_id}.pdf"
    if pdf.exists():
        pdf.unlink()
        removed["deleted"].append(str(pdf))

    ws = RUNS_DIR / "workspaces" / run_id
    if ws.is_dir():
        shutil.rmtree(ws)
        removed["deleted"].append(str(ws))

    hermes_removed = _cleanup_hermes_memory(run_id, workspace_path)
    if hermes_removed:
        removed["hermes_sessions"] = hermes_removed

    return removed
