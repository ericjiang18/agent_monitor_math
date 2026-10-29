"""Background orchestration for the proof, Lean, and DAG views.

The engine owns the informal proof. As soon as a stable draft exists, the
informal DAG and Lean translation start in parallel (for a guest's run, and
for an Ansatz CGM run whose written-proof round follows its proof, once the
run has ended). The formal DAG follows Lean because it necessarily consumes
Proof.lean. State is persisted in the run workspace so the browser can
reconnect without owning the work.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

STATE_FILENAME = "auto_pipeline.json"
_ACTIVE: dict[str, threading.Thread] = {}
_PENDING_START: dict[
    str, tuple[str, Path, int | None, str | None, str]
] = {}
_ACTIVE_LOCK = threading.Lock()
_FORMAL_REFRESH_ACTIVE: dict[str, threading.Thread] = {}
_FORMAL_REFRESH_VERSION: dict[str, int] = {}
# Callbacks run once the workspace's formal refresh worker has finished all the
# work it coalesced (for example, releasing a guest's derived-action slot).
_FORMAL_REFRESH_ON_EXIT: dict[str, list[Callable[[], None]]] = {}
_FORMAL_REFRESH_LOCK = threading.Lock()
_STATE_LOCKS: dict[str, threading.Lock] = {}
_STATE_LOCKS_GUARD = threading.Lock()
_TERMINAL = {"finished", "failed", "stopped", "cancelled", "interrupted"}
_LEAN_SUCCESS = {"verified"}
_LEAN_ATTENTION = {"incomplete", "unfaithful", "compiled"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state_lock(workspace: Path) -> threading.Lock:
    key = str(workspace.resolve())
    with _STATE_LOCKS_GUARD:
        return _STATE_LOCKS.setdefault(key, threading.Lock())


def load_state(workspace: Path) -> dict[str, Any] | None:
    from agent_monitor import proof_graph

    try:
        raw = proof_graph._read_workspace_text(
            workspace, STATE_FILENAME, limit=proof_graph.MAX_GRAPH_CACHE_BYTES
        )
        if not raw:
            return None
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_state(workspace: Path, state: dict[str, Any]) -> None:
    from agent_monitor import proof_graph

    if _run_deleted(workspace):
        return  # never recreate the workspace of a deleted run
    workspace.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    proof_graph._write_cached(workspace, STATE_FILENAME, state)


def _mutate(workspace: Path, mutator) -> dict[str, Any]:
    with _state_lock(workspace):
        state = load_state(workspace) or {"stages": {}}
        mutator(state)
        _write_state(workspace, state)
        return state


def _set_stage(
    workspace: Path,
    name: str,
    status: str,
    *,
    detail: str = "",
    result: dict[str, Any] | None = None,
    outcome: str | None = None,
) -> None:
    def update(state: dict[str, Any]) -> None:
        stage = dict((state.get("stages") or {}).get(name) or {})
        stage.update({"status": status, "detail": detail[:1000]})
        if outcome is not None:
            stage["outcome"] = outcome
        elif status == "running":
            # A retry has a new execution lifecycle; do not display the prior
            # terminal proof outcome while it is still running.
            stage.pop("outcome", None)
        elif status == "error":
            stage["outcome"] = "error"
        elif status == "skipped":
            stage["outcome"] = "blocked"
        elif status == "stale":
            stage["outcome"] = "attention"
        if status == "running" and not stage.get("started_at"):
            stage["started_at"] = _now()
        if status in {"done", "parsed", "error", "skipped", "stale"}:
            stage["finished_at"] = _now()
        if result is not None:
            stage["result"] = result
        state.setdefault("stages", {})[name] = stage

    _mutate(workspace, update)


def _lean_outcome(status: Any) -> str:
    """Map a terminal Lean proof status independently of worker lifecycle."""
    normalized = str(status or "").strip().lower()
    if normalized in _LEAN_SUCCESS:
        return "success"
    if normalized in _LEAN_ATTENTION:
        return "attention"
    # Unknown terminal values fail closed. In particular failed/error,
    # toolchain_missing, stopped, and a missing status are never success.
    return "error"


def _coverage_detail(summary: dict[str, Any], lean_outcome: str | None) -> str:
    """Coverage stage wording that does not overstate per-lemma evidence.

    A linked informal statement counts when a Lean declaration tagged with (or
    matched to) it compiled inside the kernel-verified file; only the main
    theorem's statement is fidelity-audited, so the audited count is shown
    separately when the coverage record carries it.
    """
    verified = summary.get("verified", 0)
    total = summary.get("total", 0)
    if lean_outcome != "success":
        return f"{verified}/{total} informal statements with conservative formal links"
    text = f"{verified}/{total} informal statements linked to kernel-verified Lean declarations"
    audited = summary.get("statement_audited")
    if isinstance(audited, int) and not isinstance(audited, bool):
        text += f" ({audited} with an audited statement)"
    return text


def _coverage_outcome(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "verified":
        return "success"
    if normalized in {"partial", "unverified"}:
        return "attention"
    return "error"


def _stage_outcome(name: str, stage: Any) -> str:
    """Read new outcome metadata while remaining compatible with old states."""
    if not isinstance(stage, dict):
        return ""
    explicit = str(stage.get("outcome") or "").strip().lower()
    if explicit:
        return explicit
    lifecycle = str(stage.get("status") or "").strip().lower()
    if lifecycle == "error":
        return "error"
    if lifecycle == "skipped":
        return "blocked"
    if lifecycle == "stale":
        return "attention"
    result = stage.get("result")
    if isinstance(result, dict) and result.get("status"):
        if name == "lean":
            return _lean_outcome(result.get("status"))
        if name == "coverage":
            return _coverage_outcome(result.get("status"))
    return ""


def _has_lean_source(workspace: Path) -> bool:
    from agent_monitor import proof_graph

    try:
        return bool(
            proof_graph._read_workspace_text(
                workspace,
                "lean/Proof.lean",
                limit=proof_graph.MAX_SOURCE_BYTES,
            ).strip()
        )
    except (OSError, ValueError):
        return False


# Stages a run started as "informal only" never runs automatically. They stay
# "off" (not an error) until the user verifies the proof from the Lean tab,
# which refreshes them like any manual Lean action.
_FORMAL_STAGES = ("lean", "formal_dag", "coverage")
_FORMALIZATION_OFF = (
    "Formalization is off for this run · press ∎ Verify in the Lean tab to formalize it"
)
# Analyze runs formalize selected statements only (never proofs).
_ANALYSIS_FORMALIZATION_OFF = (
    "Formalization is off for this analysis · select results in the DAG and "
    "formalize their statements (proofs are not attempted)"
)
ANALYSIS_EXECUTOR = "analysis-projection"


def is_analysis_run(run_record: dict[str, Any] | None) -> bool:
    """An Analyze run (``run_kind: "analyze"``; the ``analyze`` engine only analyzes)."""
    record = run_record or {}
    return str(record.get("run_kind") or "") == "analyze" or str(record.get("engine") or "") == "analyze"


def is_readability_run(run_record: dict[str, Any] | None) -> bool:
    """A readability run: a rewrite of someone's manuscript, never a proof,
    so nothing is derived from it (no DAG, no Lean, no coverage)."""
    record = run_record or {}
    return str(record.get("run_kind") or "") == "readability" or str(record.get("engine") or "") == "readability"


def formalization_requested(run_record: dict[str, Any] | None) -> bool:
    """False only for a run started as informal only (an analysis by default)."""
    return (run_record or {}).get("formalize") is not False


def _formalization_off(workspace: Path, *, analysis: bool = False) -> None:
    detail = _ANALYSIS_FORMALIZATION_OFF if analysis else _FORMALIZATION_OFF
    for name in _FORMAL_STAGES:
        _set_stage(workspace, name, "off", detail=detail, outcome="not_run")


def formal_stages_off(workspace: Path) -> bool:
    """True while an informal-only run has not been formalized."""
    stages = (load_state(workspace) or {}).get("stages") or {}
    return str((stages.get("lean") or {}).get("status") or "") == "off"


def _block_coverage(
    workspace: Path,
    *,
    selected: str,
    detail: str,
    blocked_by: str,
) -> None:
    _set_stage(
        workspace,
        "coverage",
        "skipped",
        detail=detail,
        outcome="blocked",
        result={
            "proof_engine": selected,
            "derived_executor": "shared-coverage-tool",
            "status": "blocked",
            "blocked_by": blocked_by,
        },
    )


def _block_formal_downstream(
    workspace: Path,
    *,
    selected: str,
    detail: str,
    blocked_by: str = "lean",
) -> None:
    _set_stage(
        workspace,
        "formal_dag",
        "skipped",
        detail=detail,
        outcome="blocked",
        result={
            "proof_engine": selected,
            "derived_executor": "shared-dag-tool",
            "status": "blocked",
            "blocked_by": blocked_by,
        },
    )
    _block_coverage(
        workspace,
        selected=selected,
        detail="Coverage needs a usable Proof.lean and Formal DAG",
        blocked_by=blocked_by,
    )


def _aggregate_status(stages: Any) -> tuple[str, str]:
    stage_map = stages if isinstance(stages, dict) else {}
    statuses = {
        str(stage.get("status") or "")
        for stage in stage_map.values()
        if isinstance(stage, dict)
    }
    completed = statuses & {"done", "parsed"}
    errors = statuses & {"error", "skipped"}
    outcomes = {
        _stage_outcome(name, stage)
        for name, stage in stage_map.items()
        if isinstance(stage, dict)
    }
    outcomes.discard("")
    proof_outcome = _stage_outcome("lean", stage_map.get("lean")) or "not_run"
    if (errors or "error" in outcomes) and completed:
        return "partial", proof_outcome
    if errors or "error" in outcomes:
        return "failed", proof_outcome
    if "attention" in outcomes:
        return "partial", proof_outcome
    return "done", proof_outcome


def _finish(workspace: Path) -> None:
    def update(state: dict[str, Any]) -> None:
        state["status"], state["proof_outcome"] = _aggregate_status(
            state.get("stages")
        )
        state["finished_at"] = _now()

    _mutate(workspace, update)


# A stage whose view was produced (formal stages are "off" for an informal-only
# run). Its outcome may still be "attention" (Lean unfaithful, say), and
# "parsed" is a DAG built from the text when its model call failed; neither is
# retried by a follow-up (the Lean and DAG tabs refresh them on request).
_PRODUCED_STAGE_STATUSES = frozenset({"done", "parsed", "off"})


def _derived_current_source(
    state: dict[str, Any] | None,
    workspace: Path,
    run_record: dict[str, Any] | None,
) -> bool:
    """Whether every derived view of the run's current proof is recorded as
    produced.

    ``derived_signature`` names the proof the last full pass derived; only
    _run writes it, once its batch has ended (a formal-only refresh never
    does).  The stages are judged as they are recorded now, whether by that
    pass or since by a Lean action (Check, Compile, a Lean harness): every
    one produced, none in error or blocked.  The pass must have ended (no
    restart cut it short, no newer request pending), and formalization must
    be asked for as it is now.  Anything else is derived again, as before.
    """
    if not isinstance(state, dict):
        return False
    if str(state.get("status") or "") not in {"done", "partial"}:
        return False
    if state.get("interruption") or state.get("pending_refresh"):
        return False
    stages = state.get("stages")
    if not isinstance(stages, dict):
        return False
    for name in ("informal_dag", "lean", "formal_dag", "coverage"):
        stage = stages.get(name)
        if not isinstance(stage, dict):
            return False
        if str(stage.get("status") or "") not in _PRODUCED_STAGE_STATUSES:
            return False
        if _stage_outcome(name, stage) in {"error", "blocked"}:
            return False
    if bool(state.get("formalize", True)) != formalization_requested(run_record):
        return False
    derived = str(state.get("derived_signature") or "")
    return bool(derived) and derived == _source_signature(workspace, run_record)


def _prepare_pending_refresh(workspace: Path) -> None:
    """Reset derived stages before consuming one coalesced newer proof."""
    def update(state: dict[str, Any]) -> None:
        state["status"] = "running"
        state["updated_at"] = _now()
        state["rerun_count"] = int(state.get("rerun_count") or 0) + 1
        state.pop("finished_at", None)
        state.pop("proof_outcome", None)
        state.pop("pending_refresh", None)
        state.pop("derived_signature", None)
        for stage in ("informal_dag", "lean", "formal_dag", "coverage"):
            stage_state = state.setdefault("stages", {}).setdefault(stage, {})
            stage_state["status"] = "waiting"
            stage_state["detail"] = "Refreshing from the latest proof draft"
            stage_state.pop("started_at", None)
            stage_state.pop("finished_at", None)
            stage_state.pop("result", None)
            stage_state.pop("outcome", None)

    _mutate(workspace, update)

def _run_record(run_id: str) -> dict[str, Any] | None:
    try:
        from agent_monitor import cgm_max
        from agent_monitor.jobs import _load_run_record

        # A Max run's Lean and graph work runs on its worker (Codex) route, a
        # Max (API) run's on its worker's API model (cgm_max.formal_view).
        return cgm_max.formal_view(_load_run_record(run_id))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _run_user(
    owner_id: int | None, run_record: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Keep the owner's role when background work rebuilds its owner.

    The guest account may already have been removed by logout or expiry. The
    run's role still applies: deriving its views must never gain an account's
    provider permissions merely because only its numeric owner was retained.
    An administrator keeps its role (looked up, never read from the run), so
    its automatic stages use the server keys and its own endpoints, as its
    manual Lean and DAG actions do.
    """
    if (run_record or {}).get("guest"):
        return {"id": owner_id, "guest": True}
    from agent_monitor.jobs import _owner_user

    return _owner_user(owner_id)


def _source_signature(workspace: Path, run_record: dict[str, Any] | None) -> str:
    from agent_monitor import proof_graph

    if is_readability_run(run_record):
        # Its final message is a reading guide, never a proof draft.
        return ""
    if is_analysis_run(run_record):
        # An analysis has no proof: its derived views come from the published
        # analysis graph, once the runner is done with it.  A report is never
        # read as a proof (no GRAPH_PROMPT, no Lean from report.md).
        if str((run_record or {}).get("status") or "") not in _TERMINAL:
            return ""
        body = proof_graph._read_workspace_text(
            workspace, "analysis/analysis_graph.json", limit=8_000_000
        )
        if not body.strip():
            return ""
        return "analysis:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    for name in ("proof.md", "proof.tex"):
        body = proof_graph._read_workspace_text(
            workspace, name, limit=proof_graph.MAX_SOURCE_BYTES
        )
        if body.strip():
            return f"{name}:{hashlib.sha256(body.encode()).hexdigest()}"
    # A failed/stopped engine's activity log is not an informal proof. Feeding
    # it to DAG and Lean workers creates authoritative-looking derived views for
    # a run that never produced an artifact. Successful engines that return a
    # substantial final answer but no file remain eligible for the legacy
    # fallback.
    if str((run_record or {}).get("status") or "") == "finished":
        for agent in reversed((run_record or {}).get("agents") or []):
            output = str(agent.get("output") or "").strip()
            if len(output) > 200:
                return "run:" + hashlib.sha256(output.encode("utf-8")).hexdigest()
    return ""


def _cgm_runner_env(run_record: dict[str, Any] | None = None) -> dict[str, str]:
    """The environment a CGM runner of this console starts from: the
    console's own, without its provider routes (jobs._run_cli_engine starts
    from subprocess_env.child_process_env, so the console's sponsored service
    switch is no run's route), plus the guest or sponsored route of
    ``run_record``."""
    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env()
    if (run_record or {}).get("guest"):
        env["AGENT_MONITOR_GUEST"] = "1"
    if (run_record or {}).get("credential_source") == "sponsored_kimi":
        env["AGENT_MONITOR_SPONSORED_KIMI"] = "1"
    return env


def _writeup_follows_proof(run_record: dict[str, Any] | None) -> bool:
    """Whether a written-proof round may follow this run's proof.md.

    An accepted Ansatz CGM run publishes proof.md and then, with the round
    on (jobs._apply_cgm_writeup_env decides it for the run), writes and
    checks writeup.md on the same Claude Code or Codex account, or the
    sponsored pool, until the run ends.
    """
    from agent_monitor.jobs import _CGM_ENGINES, _apply_cgm_writeup_env

    engine = str((run_record or {}).get("engine") or "")
    if engine not in _CGM_ENGINES:
        return False
    env = _cgm_runner_env(run_record)
    _apply_cgm_writeup_env(env, engine=engine)
    return env.get("AGENT_MONITOR_CGM_WRITEUP") == "1"


def _note_writeup_wait(workspace: Path) -> None:
    """Say why the stages that read the proof still wait once it exists."""
    def update(state: dict[str, Any]) -> None:
        for name in ("informal_dag", "lean"):
            stage = (state.get("stages") or {}).get(name)
            if isinstance(stage, dict) and stage.get("status") == "waiting":
                stage["detail"] = "Waiting for the run's written-proof round to end"

    _mutate(workspace, update)


def _wait_for_source(
    run_id: str,
    workspace: Path,
    *,
    timeout: float,
    stable_for: float = 1.5,
) -> tuple[str, dict[str, Any] | None]:
    deadline = time.monotonic() + timeout
    last_sig = ""
    stable_since = time.monotonic()
    stable = after_writeup = noted = False
    record: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if _run_deleted(workspace):
            return "", None  # deleted while it waited: nothing to derive
        record = _run_record(run_id)
        sig = _source_signature(workspace, record)
        if sig != last_sig:
            last_sig = sig
            stable_since = time.monotonic()
        terminal = str((record or {}).get("status") or "") in _TERMINAL
        # Guest Kimi writes drafts during its proof stages. Derive the final
        # artifact once, after the proof run ends, to bound sponsored work and
        # avoid competing with its in-flight provider request.  Likewise for
        # a CGM run's written-proof round, which follows its proof.md on the
        # same account (or sponsored pool) that derived calls would use.
        guest = bool((record or {}).get("guest"))
        after_writeup = not guest and _writeup_follows_proof(record)
        ready = terminal or not (guest or after_writeup)
        stable = bool(sig) and time.monotonic() - stable_since >= stable_for
        if ready and stable:
            return sig, record
        if terminal and not sig:
            return "", record
        if after_writeup and stable and not noted:
            _note_writeup_wait(workspace)
            noted = True
        time.sleep(0.5)
    if after_writeup and stable:
        # The wait is over but the run has not ended: its published proof is
        # derived all the same, as without the round.
        return last_sig, record
    return "", _run_record(run_id)


def _api_model(model: str | None) -> str | None:
    """Preserve an exact selected model; drop only generic runtime labels.

    Lean and DAG generators are recorded-route aware, so subscription models
    must reach them unchanged. Silently converting gpt-5.6 models or Claude
    aliases to None made derived work use the account default while the UI
    continued to display the selected model.
    """
    value = str(model or "").strip()
    lowered = value.lower()
    if not value or lowered in {
        "codex",
        "codex-cli",
        "codex cli",
        "codex subscription",
    }:
        return None
    return value


def _selected_harness(
    run_record: dict[str, Any] | None, engine: str | None
) -> str:
    return str(engine or (run_record or {}).get("engine") or "unknown").strip().lower()


def _run_deleted(workspace: Path) -> bool:
    """Whether the run of ``workspace`` was deleted in this process
    (jobs.delete_run marks the run before it removes the workspace)."""
    from agent_monitor.jobs import _DELETED_RUNS

    return workspace.name in _DELETED_RUNS


def _run_gone(workspace: Path) -> bool:
    """Whether the run of ``workspace`` was deleted, or its workspace is gone.
    Its derived work then stops: a call in flight finishes, no further model
    call starts (a Lean session checks before each one), and nothing is
    written into a workspace that was removed."""
    return _run_deleted(workspace) or not workspace.is_dir()


def _informal_worker(
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None,
    shared: dict[str, Any] | None = None,
    engine: str | None = None,
) -> None:
    from agent_monitor import proof_graph

    if _run_gone(workspace):
        return
    selected = _selected_harness(run_record, engine)
    _set_stage(
        workspace,
        "informal_dag",
        "running",
        detail=f"Shared DAG analysis · informal proof from {selected}",
        result={"proof_engine": selected, "derived_executor": "shared-dag-tool"},
    )
    if _run_gone(workspace):
        return  # deleted while the stage was being recorded: no model call
    try:
        result = proof_graph.generate(
            workspace=workspace,
            run_record=run_record,
            user=user,
            model=_api_model(model),
            kind="informal",
            allow_codex_fallback=False,
        )
        graph = result.get("graph") or {}
        if shared is not None:
            shared["informal_graph"] = graph
        from_facts = result.get("model") == proof_graph.FACT_GRAPH_MODEL
        # Only a valid harness-gate receipt makes the facts model-verified;
        # unattested facts are labelled as such, never as verified.
        detail = (
            f"{len(graph.get('nodes') or [])} fact nodes · derived from the CGM fact DAG · "
            f"{proof_graph.fact_graph_basis(result)} · no model call"
            if from_facts
            else f"{len(graph.get('nodes') or [])} proof nodes · shared DAG · proof from {selected}"
        )
        stage_result = {
            "proof_engine": selected,
            "derived_executor": "cgm-fact-graph" if from_facts else "shared-dag-tool",
            "model": result.get("model"),
            "source": result.get("source"),
        }
        if from_facts:
            stage_result["attested"] = result.get("attested") is True
        _set_stage(
            workspace,
            "informal_dag",
            "done",
            detail=detail,
            result=stage_result,
        )
    except Exception as exc:
        if _run_gone(workspace):
            return
        fallback = dict((shared or {}).get("informal_graph") or {})
        if fallback.get("nodes"):
            source, _proof = proof_graph._proof_source(workspace, run_record)
            cached = {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "model": "structural-parse",
                "source": source or "proof text",
                "kind": "informal",
                "status": "parsed",
                "fallback_reason": str(exc)[:1000],
                "graph": fallback,
            }
            proof_graph._write_cached(
                workspace, proof_graph.GRAPH_FILENAME, cached
            )
            _set_stage(
                workspace,
                "informal_dag",
                "parsed",
                detail="Built structurally; semantic model analysis failed: " + str(exc),
                result={
                    "proof_engine": selected,
                    "derived_executor": "shared-dag-tool",
                    "model": "structural-parse",
                    "source": source or "proof text",
                },
            )
        else:
            _set_stage(workspace, "informal_dag", "error", detail=str(exc))


def _guidance_wait_seconds() -> float:
    try:
        value = float(os.environ.get("AGENT_MONITOR_LEAN_GUIDANCE_WAIT", "240"))
    except ValueError:
        value = 240.0
    return max(0.0, min(value, 900.0))


def _lean_guidance(
    workspace: Path, shared: dict[str, Any] | None
) -> tuple[str, str]:
    """Blueprint for the Lean translator and where it came from.

    A math-harness run's critic-accepted fact DAG is the strongest blueprint
    (one Lean lemma per fact, ``main`` = target). Otherwise wait, bounded,
    for the semantic informal DAG so Lean does not settle for the structural
    paragraph parse seeded at batch start.
    """
    from agent_monitor import proof_bridge

    try:
        from agent_monitor import engine_graph

        loader = getattr(engine_graph, "load_fact_dag", None)
    except Exception:  # noqa: BLE001 - the blueprint is advisory
        loader = None
    if callable(loader):
        try:
            facts = loader(workspace)
        except Exception:  # noqa: BLE001 - a bad fact DAG must not block Lean
            facts = None
        text = proof_bridge.guidance_from_facts(facts) if facts else ""
        if text:
            return text, "fact_dag"
    ready = (shared or {}).get("informal_ready")
    if ready is not None and hasattr(ready, "wait"):
        ready.wait(timeout=_guidance_wait_seconds())
    graph = (shared or {}).get("informal_graph")
    text = proof_bridge.guidance_for_lean(graph)
    if not text:
        return "", "none"
    parsed = all(
        "structural parse" in (node.get("citations") or [])
        for node in (graph or {}).get("nodes") or []
        if isinstance(node, dict)
    )
    return text, "structural_parse" if parsed else "informal_dag"


def _lean_and_formal_worker(
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None,
    shared: dict[str, Any] | None = None,
    engine: str | None = None,
) -> None:
    from agent_monitor import lean_verify, proof_bridge, proof_graph

    if _run_gone(workspace):
        return
    selected = _selected_harness(run_record, engine)
    _set_stage(
        workspace,
        "lean",
        "running",
        detail=f"Shared Lean translator + kernel checker · proof from {selected}",
        result={"proof_engine": selected, "derived_executor": "shared-lean-translator-kernel"},
    )
    analysis_lean = (shared or {}).get("analysis_lean")
    if analysis_lean:
        # Analyze: statements of the selected results only, from the
        # analysis graph (never the report), proofs not attempted.
        guidance, blueprint_source = analysis_lean["guidance"], "analysis_graph"
    else:
        guidance, blueprint_source = _lean_guidance(workspace, shared)
    try:
        lean_result = lean_verify.generate(
            workspace=workspace,
            run_record=run_record,
            user=user,
            model=_api_model(model),
            max_repairs=(
                lean_verify.GUEST_MAX_REPAIRS
                if (user or {}).get("guest")
                else lean_verify.AUTO_MAX_REPAIRS
            ),
            guidance=guidance,
            # A deletion stops the session before its next model call.
            should_stop=lambda: _run_gone(workspace),
            **(
                {
                    "problem_override": analysis_lean["problem"],
                    "proof_override": analysis_lean["proof"],
                    "prove": False,
                    "targets": analysis_lean["targets"],
                    "source_label": "analysis/analysis_graph.json",
                }
                if analysis_lean
                else {}
            ),
        )
    except Exception as exc:
        if _run_gone(workspace):
            return
        _set_stage(
            workspace,
            "lean",
            "error",
            detail=str(exc),
            outcome="error",
            result={
                "proof_engine": selected,
                "derived_executor": "shared-lean-translator-kernel",
                "status": "error",
                "outcome": "error",
            },
        )
        _block_formal_downstream(
            workspace,
            selected=selected,
            detail="Formal DAG needs Proof.lean; Lean translation did not complete",
            blocked_by="lean_translation",
        )
        return

    if _run_gone(workspace):
        return
    lean_status = str(lean_result.get("status") or "error").strip().lower()
    lean_outcome = _lean_outcome(lean_status)
    has_lean_source = _has_lean_source(workspace)
    detail = (
        f"Lean {lean_status} · shared translator/kernel · proof from {selected}"
    )
    if analysis_lean:
        detail = (
            f"{lean_result.get('status_label') or 'Statements formalized'} · "
            f"{len(analysis_lean['targets'])} selected statement(s) · Lean 4.14 checker"
        )
    if not has_lean_source:
        lean_outcome = "error"
        detail += " · no usable Proof.lean was produced"
    _set_stage(
        workspace,
        "lean",
        "done",
        detail=detail,
        outcome=lean_outcome,
        result={
            "proof_engine": selected,
            "derived_executor": "shared-lean-translator-kernel",
            "status": lean_status,
            "outcome": lean_outcome,
            "model": lean_result.get("model"),
            "used_informal_dag": bool(guidance),
            "blueprint_source": blueprint_source,
            "proof_lean": "available" if has_lean_source else "missing",
            **(
                {"scope": "statements", "targets": list(analysis_lean["targets"])}
                if analysis_lean
                else {}
            ),
        },
    )
    if not has_lean_source:
        _block_formal_downstream(
            workspace,
            selected=selected,
            detail="Formal DAG needs a usable Proof.lean; Lean returned no source",
            blocked_by="lean_artifact",
        )
        return

    structural_only = lean_outcome != "success"
    structural_note = (
        f" · structural only; Lean {lean_status} is not a certificate"
        if structural_only
        else ""
    )
    _set_stage(
        workspace,
        "formal_dag",
        "running",
        detail="Shared formal DAG analysis · from Proof.lean" + structural_note,
        result={"proof_engine": selected, "derived_executor": "shared-dag-tool"},
    )
    if _run_gone(workspace):
        return  # deleted while the stage was being recorded: no model call
    try:
        result = proof_graph.generate(
            workspace=workspace,
            run_record=run_record,
            user=user,
            model=_api_model(model),
            kind="formal",
            allow_codex_fallback=False,
        )
        graph = result.get("graph") or {}
        _set_stage(
            workspace,
            "formal_dag",
            "done",
            detail=(
                f"{len(graph.get('nodes') or [])} Lean nodes · shared formal DAG"
                + structural_note
            ),
            outcome="attention" if structural_only else "success",
            result={
                "proof_engine": selected,
                "derived_executor": "shared-dag-tool",
                "model": result.get("model"),
                "source": result.get("source"),
                "proof_status": lean_status,
                "certificate": not structural_only,
            },
        )
    except Exception as exc:
        parsed = proof_graph.load_or_parse_formal(workspace)
        if parsed and (parsed.get("graph") or {}).get("nodes"):
            proof_graph._write_cached(
                workspace, proof_graph.LEAN_GRAPH_FILENAME, parsed
            )
            _set_stage(
                workspace,
                "formal_dag",
                "parsed",
                detail=(
                    "Built from Lean declarations; semantic model analysis failed: "
                    + str(exc)
                    + structural_note
                ),
                outcome="attention" if structural_only else "success",
                result={
                    "proof_engine": selected,
                    "derived_executor": "shared-dag-tool",
                    "model": parsed.get("model") or "lean-parse",
                    "source": parsed.get("source") or "Proof.lean",
                    "proof_status": lean_status,
                    "certificate": not structural_only,
                },
            )
        else:
            _set_stage(
                workspace,
                "formal_dag",
                "error",
                detail=str(exc) + structural_note,
                outcome="error",
                result={
                    "proof_engine": selected,
                    "derived_executor": "shared-dag-tool",
                    "status": "error",
                    "proof_status": lean_status,
                    "certificate": False,
                },
            )

def _run_batch(
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None,
    engine: str | None = None,
) -> None:
    from agent_monitor import proof_bridge, proof_graph
    from agent_monitor.jobs import _route_from_record

    selected = _selected_harness(run_record, engine)
    shared: dict[str, Any] = {}
    if is_analysis_run(run_record):
        if not _analysis_batch(workspace, run_record, shared, selected, user=user):
            return
        _lean_and_formal_worker(workspace, run_record, user, model, shared, selected)
        _coverage_after_lean(workspace, selected)
        return
    # This fast structural pass lets Lean consume the informal proof topology
    # immediately while the richer informal DAG and Lean run concurrently.
    try:
        _source, proof = proof_graph._proof_source(workspace, run_record)
        if proof:
            shared["informal_graph"] = proof_graph.graph_from_text(proof)
    except (OSError, ValueError):
        pass

    # Lean waits (bounded) for the semantic informal DAG instead of using the
    # structural parse; the event fires however the informal worker ends.
    informal_ready = threading.Event()
    shared["informal_ready"] = informal_ready
    worker_args = (workspace, run_record, user, model, shared, selected)

    def informal_then_signal() -> None:
        try:
            _informal_worker(*worker_args)
        except Exception:
            if not _run_gone(workspace):
                raise
        finally:
            informal_ready.set()

    def lean_and_formal() -> None:
        try:
            _lean_and_formal_worker(*worker_args)
        except Exception:
            if not _run_gone(workspace):
                raise

    if not formalization_requested(run_record):
        _formalization_off(workspace)
        informal_then_signal()
        return

    sponsored_kimi = str(
        (run_record or {}).get("credential_source") or ""
    ).strip().lower() == "sponsored_kimi"
    claude_subscription = _route_from_record(run_record) == "claude_subscription"
    has_fact_dag = proof_graph.fact_graph_result(workspace) is not None
    if has_fact_dag or (user or {}).get("guest") or sponsored_kimi or claude_subscription:
        # A CGM fact DAG makes the informal DAG deterministic (no model call),
        # so it is built first and Lean receives the fact blueprint at once.
        # The sponsored gateway is a shared, cost-bounded service, while two
        # Claude Code processes for one run share the same per-user OAuth
        # account directory. Starting informal DAG and Lean translation
        # together can respectively trigger avoidable HTTP 429s or contention
        # in the account-backed CLI. Keep the same call budget and execute this
        # run's derived model work serially instead.
        informal_then_signal()
        lean_and_formal()
    else:
        informal = threading.Thread(
            target=informal_then_signal,
            daemon=True,
            name=f"informal-dag-{workspace.name}",
        )
        formal = threading.Thread(
            target=lean_and_formal,
            daemon=True,
            name=f"lean-formal-{workspace.name}",
        )
        informal.start()
        formal.start()
        informal.join()
        formal.join()

    _coverage_after_lean(workspace, selected)


def _analysis_off(workspace: Path, detail: str) -> None:
    for name in _FORMAL_STAGES:
        _set_stage(workspace, name, "off", detail=detail, outcome="not_run")


def _analysis_batch(
    workspace: Path,
    run_record: dict[str, Any] | None,
    shared: dict[str, Any],
    selected: str,
    *,
    user: dict[str, Any] | None = None,
) -> bool:
    """Derived views of an analysis; true when statement-only Lean should run.

    The informal DAG is the deterministic projection of
    ``analysis/analysis_graph.json`` (no model call; the report is never
    re-read as a proof).  Lean runs only when formalization was requested and
    the analysis finished: the statements of its main results (at most six),
    proofs not attempted.
    """
    from agent_monitor import analyze_runs, proof_graph
    from agent_monitor.analyze import graph as analysis_graph

    _set_stage(
        workspace,
        "informal_dag",
        "running",
        detail="Manuscript DAG · projecting the analysis graph (no model call)",
        result={"proof_engine": selected, "derived_executor": ANALYSIS_EXECUTOR},
    )
    graph = analysis_graph.load(workspace)
    try:
        if graph is None:
            raise ValueError("The analysis published no readable graph")
        projection = analysis_graph.to_proof_graph(graph)
        proof_graph._write_cached(workspace, proof_graph.GRAPH_FILENAME, projection)
    except (OSError, ValueError) as exc:
        _set_stage(workspace, "informal_dag", "error", detail=str(exc))
        _analysis_off(workspace, "No manuscript DAG to formalize")
        return False
    nodes = len(projection["graph"]["nodes"])
    _set_stage(
        workspace,
        "informal_dag",
        "done",
        detail=(
            f"{nodes} manuscript statements · deterministic projection of the "
            "analysis graph · no model call"
        ),
        result={
            "proof_engine": selected,
            "derived_executor": ANALYSIS_EXECUTOR,
            "model": projection.get("model"),
            "source": projection.get("source"),
        },
    )
    shared["informal_graph"] = projection["graph"]
    if not formalization_requested(run_record):
        _formalization_off(workspace, analysis=True)
        return False
    if str((run_record or {}).get("status") or "") != "finished":
        _analysis_off(
            workspace,
            "The analysis did not finish, so nothing was formalized automatically · "
            "select results in the DAG to formalize their statements",
        )
        return False
    # The Analyze switches cover the automatic statement formalization too.
    # The run was admitted when it started, so the owner's role is not checked
    # again (a member's would refuse an operator's ANALYZE_CMD); only the
    # switches matter here: Analyze off, guests off for a guest's analysis,
    # sponsored Analyze off for a sponsored one.
    guest_run = bool((run_record or {}).get("guest") or (user or {}).get("guest"))
    refusal = analyze_runs.derived_work_error({"guest": True} if guest_run else None, run_record)
    if refusal is not None:
        _analysis_off(workspace, f"{str(refusal).rstrip('.')} · nothing was formalized")
        return False
    targets = analyze_runs.default_targets(graph)
    if not targets:
        _analysis_off(
            workspace,
            "No main result was identified · select results in the DAG to formalize "
            "their statements",
        )
        return False
    shared["analysis_lean"] = analyze_runs.lean_inputs(graph, targets)
    return True


def _coverage_after_lean(workspace: Path, selected: str) -> None:
    """Cross-link coverage once Lean (and the formal DAG) have run."""
    from agent_monitor import proof_bridge

    state = load_state(workspace) or {}
    lean_stage = (state.get("stages") or {}).get("lean") or {}
    if str(lean_stage.get("status") or "") == "error" or not _has_lean_source(workspace):
        _block_formal_downstream(
            workspace,
            selected=selected,
            detail="Formal DAG needs a usable Proof.lean; Lean translation did not complete",
            blocked_by="lean_translation",
        )
        return

    lean_result_status = str(
        ((lean_stage.get("result") or {}).get("status") or "error")
    ).strip().lower()
    lean_outcome = _stage_outcome("lean", lean_stage) or _lean_outcome(
        lean_result_status
    )
    structural_note = (
        f" · Lean {lean_result_status} is not a verified certificate"
        if lean_outcome != "success"
        else ""
    )
    _set_stage(
        workspace,
        "coverage",
        "running",
        detail=(
            f"Shared coverage cross-linker · informal proof from {selected}"
            + structural_note
        ),
        result={"proof_engine": selected, "derived_executor": "shared-coverage-tool"},
    )
    try:
        coverage = proof_bridge.build(workspace, selected_harness=selected)
        summary = coverage.get("summary") or {}
        coverage_status = str(coverage.get("status") or "unverified")
        outcome = _coverage_outcome(coverage_status)
        if lean_outcome != "success" and outcome == "success":
            outcome = "attention"
        _set_stage(
            workspace,
            "coverage",
            "done",
            detail=_coverage_detail(summary, lean_outcome) + structural_note,
            outcome=outcome,
            result={
                "proof_engine": selected,
                "derived_executor": "shared-coverage-tool",
                "status": coverage_status,
                "outcome": outcome,
                "proof_status": lean_result_status,
                "certificate": lean_outcome == "success" and outcome == "success",
            },
        )
    except Exception as exc:
        _set_stage(
            workspace,
            "coverage",
            "error",
            detail=str(exc) + structural_note,
            outcome="error",
            result={
                "proof_engine": selected,
                "derived_executor": "shared-coverage-tool",
                "status": "error",
                "proof_status": lean_result_status,
                "certificate": False,
            },
        )



def _refresh_formal_once(
    *,
    run_id: str,
    workspace: Path,
    owner_id: int | None,
    model: str | None,
    engine: str | None,
) -> None:
    """Refresh formal DAG/coverage after a manual Lean edit or HITL revision.

    The informal proof and its DAG are unchanged, and the already checked Lean
    source must not be regenerated. This keeps Monitor aligned with the Lean
    Verification tab without starting a second proving harness.
    """
    from agent_monitor import lean_verify, proof_bridge, proof_graph

    run_record = _run_record(run_id)
    selected = _selected_harness(run_record, engine)
    user = _run_user(owner_id, run_record)
    lean_result = lean_verify.load_cached(workspace) or {}
    lean_status = str(lean_result.get("status") or "none")

    def begin(state: dict[str, Any]) -> None:
        state["status"] = "running"
        state.pop("finished_at", None)
        signature = _source_signature(workspace, run_record)
        if signature:
            state["source_signature"] = signature

    _mutate(workspace, begin)
    has_lean_source = _has_lean_source(workspace)
    lean_outcome = _lean_outcome(lean_status)
    lean_lifecycle = "done" if lean_status not in {"none", "running"} else "skipped"
    if not has_lean_source:
        lean_outcome = "error"
    _set_stage(
        workspace,
        "lean",
        lean_lifecycle,
        detail=(
            f"Lean {lean_status} · shared translator/kernel · proof from {selected}"
            + ("" if has_lean_source else " · no usable Proof.lean is available")
        ),
        outcome=lean_outcome,
        result={
            "proof_engine": selected,
            "derived_executor": "shared-lean-translator-kernel",
            "status": lean_status,
            "outcome": lean_outcome,
            "model": lean_result.get("model"),
            "source": "manual-check-or-hitl",
            "proof_lean": "available" if has_lean_source else "missing",
        },
    )
    if not has_lean_source or lean_lifecycle == "skipped":
        _block_formal_downstream(
            workspace,
            selected=selected,
            detail="Formal DAG needs a usable Proof.lean",
            blocked_by="lean_artifact",
        )
        _finish(workspace)
        return

    structural_only = lean_outcome != "success"
    structural_note = (
        f" · structural only; Lean {lean_status} is not a certificate"
        if structural_only
        else ""
    )
    _set_stage(
        workspace,
        "formal_dag",
        "running",
        detail="Shared formal DAG refresh · edited Proof.lean" + structural_note,
        result={"proof_engine": selected, "derived_executor": "shared-dag-tool"},
    )
    try:
        result = proof_graph.generate(
            workspace=workspace,
            run_record=run_record,
            user=user,
            model=_api_model(model),
            kind="formal",
            allow_codex_fallback=False,
        )
        graph = result.get("graph") or {}
        _set_stage(
            workspace,
            "formal_dag",
            "done",
            detail=(
                f"{len(graph.get('nodes') or [])} Lean nodes · shared formal DAG"
                + structural_note
            ),
            outcome="attention" if structural_only else "success",
            result={
                "proof_engine": selected,
                "derived_executor": "shared-dag-tool",
                "model": result.get("model"),
                "source": result.get("source"),
                "proof_status": lean_status,
                "certificate": not structural_only,
            },
        )
    except Exception as exc:
        parsed = proof_graph.load_or_parse_formal(workspace)
        if parsed and (parsed.get("graph") or {}).get("nodes"):
            proof_graph._write_cached(
                workspace, proof_graph.LEAN_GRAPH_FILENAME, parsed
            )
            _set_stage(
                workspace,
                "formal_dag",
                "parsed",
                detail=(
                    "Built from Lean declarations; semantic model analysis failed: "
                    + str(exc)
                    + structural_note
                ),
                outcome="attention" if structural_only else "success",
                result={
                    "proof_engine": selected,
                    "derived_executor": "shared-dag-tool",
                    "model": parsed.get("model") or "lean-parse",
                    "source": parsed.get("source") or "Proof.lean",
                    "proof_status": lean_status,
                    "certificate": not structural_only,
                },
            )
        else:
            _set_stage(
                workspace,
                "formal_dag",
                "error",
                detail=str(exc) + structural_note,
                outcome="error",
                result={
                    "proof_engine": selected,
                    "derived_executor": "shared-dag-tool",
                    "status": "error",
                    "proof_status": lean_status,
                    "certificate": False,
                },
            )

    _set_stage(
        workspace,
        "coverage",
        "running",
        detail=(
            f"Shared coverage refresh · informal proof from {selected}"
            + structural_note
        ),
        result={"proof_engine": selected, "derived_executor": "shared-coverage-tool"},
    )
    try:
        coverage = proof_bridge.build(workspace, selected_harness=selected)
        summary = coverage.get("summary") or {}
        coverage_status = str(coverage.get("status") or "unverified")
        outcome = _coverage_outcome(coverage_status)
        if lean_outcome != "success" and outcome == "success":
            outcome = "attention"
        _set_stage(
            workspace,
            "coverage",
            "done",
            detail=_coverage_detail(summary, lean_outcome) + structural_note,
            outcome=outcome,
            result={
                "proof_engine": selected,
                "derived_executor": "shared-coverage-tool",
                "status": coverage_status,
                "outcome": outcome,
                "proof_status": lean_status,
                "certificate": lean_outcome == "success" and outcome == "success",
            },
        )
    except Exception as exc:
        _set_stage(
            workspace,
            "coverage",
            "error",
            detail=str(exc) + structural_note,
            outcome="error",
            result={
                "proof_engine": selected,
                "derived_executor": "shared-coverage-tool",
                "status": "error",
                "proof_status": lean_status,
                "certificate": False,
            },
        )
    _finish(workspace)


def _formal_refresh_loop(
    run_id: str,
    workspace: Path,
    owner_id: int | None,
    model: str | None,
    engine: str | None,
) -> None:
    """Wait out the initial pipeline and coalesce consecutive formal edits."""
    key = str(workspace.resolve())
    on_exit: list[Callable[[], None]] = []
    try:
        while True:
            with _ACTIVE_LOCK:
                initial = _ACTIVE.get(key)
            if initial and initial.is_alive():
                initial.join(timeout=0.5)
                continue
            with _FORMAL_REFRESH_LOCK:
                version = _FORMAL_REFRESH_VERSION.get(key, 0)
            try:
                if not _run_gone(workspace):
                    _refresh_formal_once(
                        run_id=run_id,
                        workspace=workspace,
                        owner_id=owner_id,
                        model=model,
                        engine=engine,
                    )
            except Exception:
                # Deleted during the refresh: nothing is left to refresh.
                if not _run_gone(workspace):
                    raise
            with _FORMAL_REFRESH_LOCK:
                if _FORMAL_REFRESH_VERSION.get(key, 0) == version:
                    _FORMAL_REFRESH_ACTIVE.pop(key, None)
                    _FORMAL_REFRESH_VERSION.pop(key, None)
                    # Taken atomically with retiring this worker, so a
                    # callback registered for a newer worker is never run here.
                    on_exit = _FORMAL_REFRESH_ON_EXIT.pop(key, [])
                    return
    finally:
        with _FORMAL_REFRESH_LOCK:
            active = _FORMAL_REFRESH_ACTIVE.get(key)
            if active is threading.current_thread():
                _FORMAL_REFRESH_ACTIVE.pop(key, None)
                _FORMAL_REFRESH_VERSION.pop(key, None)
                on_exit = on_exit + _FORMAL_REFRESH_ON_EXIT.pop(key, [])
        _run_callbacks(on_exit)


def _run_callbacks(callbacks: list[Callable[[], None]]) -> None:
    for callback in callbacks:
        try:
            callback()
        except Exception:  # noqa: BLE001 - a release hook must not kill the worker
            pass


def refresh_formal(
    *,
    run_id: str,
    workspace: Path,
    owner_id: int | None,
    model: str | None,
    engine: str | None,
    on_exit: Callable[[], None] | None = None,
) -> bool:
    """Schedule a formal-only derived-view refresh; return true if spawned.

    ``on_exit`` is called exactly once, after the worker that performs this
    request's refresh (a new one, or the one already running for the
    workspace, which picks the request up) has finished all its work, or at
    once if no worker can be started.  The console uses it to keep a guest's
    derived-action slot held for as long as the background model call runs.
    """
    key = str(workspace.resolve())
    with _FORMAL_REFRESH_LOCK:
        _FORMAL_REFRESH_VERSION[key] = _FORMAL_REFRESH_VERSION.get(key, 0) + 1
        active = _FORMAL_REFRESH_ACTIVE.get(key)
        if active and active.is_alive():
            if on_exit is not None:
                _FORMAL_REFRESH_ON_EXIT.setdefault(key, []).append(on_exit)
            return False
        thread = threading.Thread(
            target=_formal_refresh_loop,
            args=(run_id, workspace, owner_id, model, engine),
            daemon=True,
            name=f"formal-refresh-{run_id}",
        )
        _FORMAL_REFRESH_ACTIVE[key] = thread
        if on_exit is not None:
            _FORMAL_REFRESH_ON_EXIT.setdefault(key, []).append(on_exit)
        try:
            thread.start()
        except BaseException as exc:  # e.g. "can't start new thread"
            _FORMAL_REFRESH_ACTIVE.pop(key, None)
            _FORMAL_REFRESH_VERSION.pop(key, None)
            pending = _FORMAL_REFRESH_ON_EXIT.pop(key, [])
            failure = exc
        else:
            return True
    _run_callbacks(pending)
    raise failure

# An analysis's own deadline (analysis/request.json caps.max_seconds), plus
# the longest single model call, the CLI watchdog margin and the record write.
_ANALYSIS_WAIT_MARGIN_SECONDS = 900 + 300 + 300


def max_run_wait_seconds() -> float:
    """How long the pipeline waits for an Ansatz CGM Max run: its own wall
    clock, its written-proof round, the CLI watchdog's margin and the record
    write."""
    from agent_monitor import cgm_max
    from agent_monitor.jobs import _apply_cgm_writeup_env, _cgm_writeup_allowance

    env = {**_cgm_runner_env(), cgm_max.ROUTE_MODE_ENV: "max"}
    _apply_cgm_writeup_env(env, engine=cgm_max.ENGINE)
    return float(cgm_max.RUN_SECONDS + _cgm_writeup_allowance(env) + 300 + 300)


def cgm_run_wait_seconds(engine: str) -> float:
    """How long the pipeline waits for a CGM run (``math_harness``, ``kimi``):
    its outer watchdog under the console's budgets (its written-proof round
    included), and the record write.

    A guest or sponsored run's own environment only lowers that bound (their
    profiles), so a signed-in run's environment gives an upper bound.
    """
    from agent_monitor.jobs import _apply_cgm_writeup_env, _cli_timeout_seconds

    env = _cgm_runner_env()
    _apply_cgm_writeup_env(env, engine=engine)
    return float(_cli_timeout_seconds(engine, env) + 300)


def max_api_run_wait_seconds() -> float:
    """How long the pipeline waits for a Max (API) run: its outer watchdog
    (its own wall clock, last call and written-proof round, from the run's
    own limits; cgm_max.apply_models_api), and the record write."""
    from agent_monitor import cgm_max
    from agent_monitor.jobs import _apply_cgm_writeup_env, _cli_timeout_seconds

    env = _cgm_runner_env()
    advisor, worker = cgm_max.api_models(env)
    cgm_max.apply_models_api(env, {"advisor": {"model": advisor}, "worker": {"model": worker}})
    _apply_cgm_writeup_env(env, engine=cgm_max.ENGINE)
    return float(_cli_timeout_seconds(cgm_max.ENGINE, env) + 300)


def _long_effort_run(record: dict[str, Any] | None) -> bool:
    """Whether a run was made on an API key at extra-high or maximum effort:
    a Plain or Meta-Harness run on an OpenAI model then has a three-hour
    watchdog (jobs._cli_timeout_seconds).  Another model's run ends sooner,
    and the waits end with it."""
    record = record or {}
    return str(record.get("auth_route") or "").strip().lower() == "api_key" and str(
        record.get("reasoning_effort") or ""
    ).strip().lower() in {"xhigh", "max"}


def _max_run_wait_seconds(run_id: str) -> float | None:
    """How long the pipeline waits for ``run_id`` when it is a Max or a Max
    (API) run (``_run_record`` returns its worker view); ``None`` otherwise.
    The two have one wall clock but different watchdogs."""
    from agent_monitor import cgm_max
    from agent_monitor.jobs import _load_run_record

    try:
        record = _load_run_record(run_id)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if cgm_max.is_max_api_record(record):
        return max_api_run_wait_seconds()
    if cgm_max.is_max_record(record):
        return max_run_wait_seconds()
    return None


def analysis_wait_seconds(workspace: Path) -> float:
    """How long the pipeline waits for an analysis to end: never less than the
    analysis's own watchdog (an administrator's may run four hours)."""
    from agent_monitor.analyze import limits as analyze_limits
    from agent_monitor.analyze import request as analysis_request

    try:
        seconds = int(analysis_request.load(workspace)["caps"].get("max_seconds") or 0)
    except (analysis_request.RequestError, KeyError, TypeError, ValueError, OSError):
        seconds = 0
    if seconds <= 0:
        seconds = analyze_limits.CEILING_SECONDS
    return float(min(seconds, analyze_limits.CEILING_SECONDS) + _ANALYSIS_WAIT_MARGIN_SECONDS)


def _run(
    run_id: str,
    workspace: Path,
    owner_id: int | None,
    model: str | None,
    engine: str,
) -> None:
    timeout = max(
        60.0, float(os.environ.get("AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT") or 7200)
    )
    if is_analysis_run(_run_record(run_id)):
        # Its derived views come from the published graph, once it has ended.
        timeout = max(timeout, analysis_wait_seconds(workspace))
    elif (max_wait := _max_run_wait_seconds(run_id)) is not None:
        # An Ansatz CGM Max run may take three hours and writes proof.md only
        # when it publishes; giving up at the default two hours left every
        # long Max run without derived views ("produced no proof draft").
        timeout = max(timeout, max_wait)
    else:
        from agent_monitor.jobs import _CGM_ENGINES, _LONG_EFFORT_CLI_TIMEOUT

        if engine in _CGM_ENGINES:
            # A CGM run also publishes proof.md only at its end, which an
            # operator-raised wall clock (AGENT_MONITOR_CGM_MAX_SECONDS) can
            # put past the default wait.
            timeout = max(timeout, cgm_run_wait_seconds(engine))
        elif engine == "deepagents" or (
            engine in {"plain", "metaharness"} and _long_effort_run(_run_record(run_id))
        ):
            # Their watchdog may be three hours (DeepAgents always; Plain and
            # Meta-Harness on an OpenAI model at extra-high or maximum
            # effort), and the pass waits for the run's end to derive its
            # final proof.  Both waits end as soon as the run does.
            timeout = max(timeout, float(_LONG_EFFORT_CLI_TIMEOUT + 300))
    processed_sig = ""
    try:
        sig, record = _wait_for_source(run_id, workspace, timeout=timeout)
        if not sig:
            if str((record or {}).get("status") or "") in {"stopped", "cancelled"}:
                _stopped_before_source(
                    workspace,
                    "The analysis was stopped before it published a graph"
                    if is_analysis_run(record)
                    else "The run was stopped before it produced a proof draft",
                )
                return
            still_running = str((record or {}).get("status") or "") not in _TERMINAL
            missing = (
                (
                    "The analysis was still running when the automatic steps stopped waiting · "
                    "the Manuscript DAG builds when opened"
                    if still_running
                    else "The analysis published no graph"
                )
                if is_analysis_run(record)
                else "The engine produced no proof draft"
            )
            for stage in ("informal_dag", "lean", "formal_dag", "coverage"):
                _set_stage(workspace, stage, "skipped", detail=missing)
            _finish(workspace)
            return
        _mutate(workspace, lambda state: state.update({"source_signature": sig}))
        user = _run_user(owner_id, record)
        _run_batch(workspace, record, user, model, engine)
        processed_sig = sig
        # The proof this full pass derived (see _derived_current_source).
        _mutate(workspace, lambda state: state.update({"derived_signature": sig}))
        if (user or {}).get("guest"):
            _finish(workspace)
            return

        deadline = time.monotonic() + timeout
        record = _run_record(run_id)
        while (
            str((record or {}).get("status") or "") not in _TERMINAL
            and time.monotonic() < deadline
            and not _run_deleted(workspace)
        ):
            time.sleep(0.75)
            record = _run_record(run_id)
        if _run_deleted(workspace):
            return  # deleted while the run went on: nothing left to derive
        final_sig = _source_signature(workspace, record)
        if final_sig and final_sig != sig:
            def rerun(state: dict[str, Any]) -> None:
                state["rerun_count"] = int(state.get("rerun_count") or 0) + 1
                state["source_signature"] = final_sig
                state.pop("derived_signature", None)
                for stage in ("informal_dag", "lean", "formal_dag", "coverage"):
                    stage_state = state.setdefault("stages", {}).setdefault(stage, {})
                    stage_state["status"] = "waiting"
                    stage_state.pop("started_at", None)
                    stage_state.pop("finished_at", None)
                    stage_state.pop("result", None)
                    stage_state.pop("outcome", None)

            _mutate(workspace, rerun)
            _run_batch(workspace, record, user, model, engine)
            processed_sig = final_sig
            _mutate(
                workspace, lambda state: state.update({"derived_signature": final_sig})
            )
        _finish(workspace)
    except Exception:
        # Deleted while its views were being derived: nothing is left to
        # write, and the pass ends quietly instead of failing the thread.
        if not _run_gone(workspace):
            raise
    finally:
        if _run_deleted(workspace) and workspace.is_dir():
            # A model call still in flight at the deletion wrote files after it.
            shutil.rmtree(workspace, ignore_errors=True)
        key = str(workspace.resolve())
        with _ACTIVE_LOCK:
            active = _ACTIVE.get(key)
            if active is threading.current_thread():
                _ACTIVE.pop(key, None)
                pending = _PENDING_START.pop(key, None)
                if pending is not None and _run_gone(pending[1]):
                    pending = None  # a refresh queued for a run since deleted
                if pending is not None:
                    next_run_id, next_workspace, next_owner, next_model, next_engine = pending
                    current_sig = _source_signature(
                        next_workspace, _run_record(next_run_id)
                    )
                    if current_sig and current_sig == processed_sig:
                        _mutate(
                            next_workspace,
                            lambda state: state.pop("pending_refresh", None),
                        )
                    else:
                        _prepare_pending_refresh(next_workspace)
                        thread = threading.Thread(
                            target=_run,
                            args=(
                                next_run_id,
                                next_workspace,
                                next_owner,
                                next_model,
                                next_engine,
                            ),
                            daemon=True,
                            name=f"auto-pipeline-{next_run_id}",
                        )
                        _ACTIVE[key] = thread
                        thread.start()


def start(
    *,
    run_id: str,
    workspace: Path,
    owner_id: int | None,
    model: str | None,
    engine: str,
) -> dict[str, Any]:
    """Start the durable sidecar once for a run and return its initial state.

    When no pass is alive and the last one already produced every derived
    view of the run's current proof, return that pass's state instead and
    start nothing (see _derived_current_source).
    """
    if str(engine or "").strip().lower() == "readability" or is_readability_run(_run_record(run_id)):
        # A readability run has no proof: no derived views, no model calls.
        return {
            "version": 2,
            "run_id": run_id,
            "proof_engine": "readability",
            "status": "disabled",
            "detail": "A readability run has no proof to derive views from",
            "stages": {},
        }
    if os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "").strip() == "1":
        # The standalone public Kimi service is deliberately a single bounded
        # proof job.  Its request surface cannot re-enable derived model calls.
        return {
            "version": 2,
            "run_id": run_id,
            "proof_engine": str(engine or "unknown").strip().lower(),
            "status": "disabled",
            "detail": "Automatic derived stages are disabled for this service",
            "stages": {},
        }
    key = str(workspace.resolve())
    selected = str(engine or "unknown").strip().lower()
    with _ACTIVE_LOCK:
        active = _ACTIVE.get(key)
        if active and active.is_alive():
            _PENDING_START[key] = (
                run_id,
                workspace,
                owner_id,
                model,
                selected,
            )
            _mutate(
                workspace,
                lambda state: state.update(
                    {
                        "pending_refresh": True,
                        "updated_at": _now(),
                    }
                ),
            )
            return load_state(workspace) or {}
        _PENDING_START.pop(key, None)
        record = _run_record(run_id)
        previous = load_state(workspace)
        if _derived_current_source(previous, workspace, record):
            # Every view of exactly this proof is already there (a follow-up
            # that published nothing new, say): another pass would only repeat
            # its paid model calls.  A pass still alive also skips an unchanged
            # proof when it ends (the pending refresh in _run).
            return previous
        initial = {
            "version": 2,
            "run_id": run_id,
            "proof_engine": selected,
            "single_harness": False,
            "status": "running",
            "started_at": _now(),
            "updated_at": _now(),
            "rerun_count": 0,
            "stages": {
                "informal_dag": {
                    "status": "waiting",
                    "detail": "Waiting for a proof draft",
                    "proof_engine": selected,
                    "derived_executor": "shared-dag-tool",
                },
                "lean": {
                    "status": "waiting",
                    "detail": "Waiting for a proof draft",
                    "proof_engine": selected,
                    "derived_executor": "shared-lean-translator-kernel",
                },
                "formal_dag": {
                    "status": "waiting",
                    "detail": "Waiting for Proof.lean",
                    "proof_engine": selected,
                    "derived_executor": "shared-dag-tool",
                },
                "coverage": {
                    "status": "waiting",
                    "detail": "Waiting for informal and formal results",
                    "proof_engine": selected,
                    "derived_executor": "shared-coverage-tool",
                },
            },
        }
        analysis = is_analysis_run(record)
        if analysis:
            initial["run_kind"] = "analyze"
            initial["stages"]["informal_dag"].update(
                detail="Waiting for the analysis", derived_executor=ANALYSIS_EXECUTOR
            )
            initial["stages"]["lean"]["detail"] = "Waiting for the analysis"
        if not formalization_requested(record):
            initial["formalize"] = False
            for name in _FORMAL_STAGES:
                initial["stages"][name].update(
                    status="off",
                    detail=_ANALYSIS_FORMALIZATION_OFF if analysis else _FORMALIZATION_OFF,
                    outcome="not_run",
                )
        with _state_lock(workspace):
            _write_state(workspace, initial)
        thread = threading.Thread(
            target=_run,
            args=(run_id, workspace, owner_id, model, selected),
            daemon=True,
            name=f"auto-pipeline-{run_id}",
        )
        _ACTIVE[key] = thread
        thread.start()
        return initial


def mark_interrupted(workspace: Path, detail: str) -> None:
    """Close a derived pipeline whose owning engine worker disappeared."""
    def update(state: dict[str, Any]) -> None:
        for name, existing in (state.get("stages") or {}).items():
            stage = existing if isinstance(existing, dict) else {}
            status = str(stage.get("status") or "waiting")
            if status in {"waiting", "running"}:
                stage.update(
                    {
                        "status": "skipped",
                        "outcome": "blocked",
                        "detail": f"Interrupted before {name} completed: {detail}"[:1000],
                        "finished_at": _now(),
                    }
                )
        state["status"], state["proof_outcome"] = _aggregate_status(
            state.get("stages")
        )
        state["finished_at"] = _now()
        state["interruption"] = {"detected_at": _now(), "reason": detail}

    _mutate(workspace, update)


def reconcile_orphaned(workspace: Path, detail: str) -> bool:
    """Close a persisted running sidecar when this process owns no worker."""
    key = str(workspace.resolve())
    with _ACTIVE_LOCK:
        active = _ACTIVE.get(key)
        if active and active.is_alive():
            return False
    # A manual Lean Check/HITL revision owns a distinct formal-only worker.
    # Treat it as live too, otherwise each /api/run poll can immediately mark
    # its running formal DAG stage as an orphan left by a server restart.
    with _FORMAL_REFRESH_LOCK:
        formal_refresh = _FORMAL_REFRESH_ACTIVE.get(key)
        if formal_refresh and formal_refresh.is_alive():
            return False
    state = load_state(workspace) or {}
    if str(state.get("status") or "") != "running":
        return False
    mark_interrupted(workspace, detail)
    return True


def _stopped_before_source(workspace: Path, detail: str) -> None:
    """Close the stages of a run its user stopped before anything existed to
    derive: a stop is not a failure, so nothing reads as an error."""
    def update(state: dict[str, Any]) -> None:
        for name in ("informal_dag", "lean", "formal_dag", "coverage"):
            stage = state.setdefault("stages", {}).setdefault(name, {})
            if stage.get("status") == "off":
                continue  # an informal-only run never asked for it
            stage.update(
                {
                    "status": "stopped",
                    "outcome": "not_run",
                    "detail": detail,
                    "finished_at": _now(),
                }
            )
        # _aggregate_status would read stages that never ran as done.
        state["status"] = "stopped"
        state["proof_outcome"] = "not_run"
        state["finished_at"] = _now()

    _mutate(workspace, update)


def mark_stale(workspace: Path, detail: str = "proof.md was edited") -> None:
    """Mark derived views stale after a manual Sandbox edit."""
    def update(state: dict[str, Any]) -> None:
        state["status"] = "stale"
        state.pop("derived_signature", None)
        for name in ("informal_dag", "lean", "formal_dag", "coverage"):
            stage = state.setdefault("stages", {}).setdefault(name, {})
            # A stage turned off for the run (formalization off) stays off:
            # nothing of it went stale.
            if stage.get("status") == "off":
                continue
            stage.update(
                {
                    "status": "stale",
                    "outcome": "attention",
                    "detail": detail,
                    "finished_at": _now(),
                }
            )
        state["proof_outcome"] = "attention"

    _mutate(workspace, update)
