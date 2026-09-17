"""Background orchestration for the proof, Lean, and DAG views.

The engine owns the informal proof. As soon as a stable draft exists, the
informal DAG and Lean translation start in parallel. The formal DAG follows
Lean because it necessarily consumes Proof.lean. State is persisted in the
run workspace so the browser can reconnect without owning the work.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

STATE_FILENAME = "auto_pipeline.json"
_ACTIVE: dict[str, threading.Thread] = {}
_PENDING_START: dict[
    str, tuple[str, Path, int | None, str | None, str]
] = {}
_ACTIVE_LOCK = threading.Lock()
_FORMAL_REFRESH_ACTIVE: dict[str, threading.Thread] = {}
_FORMAL_REFRESH_VERSION: dict[str, int] = {}
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


def _prepare_pending_refresh(workspace: Path) -> None:
    """Reset derived stages before consuming one coalesced newer proof."""
    def update(state: dict[str, Any]) -> None:
        state["status"] = "running"
        state["updated_at"] = _now()
        state["rerun_count"] = int(state.get("rerun_count") or 0) + 1
        state.pop("finished_at", None)
        state.pop("proof_outcome", None)
        state.pop("pending_refresh", None)
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
        from agent_monitor.jobs import _load_run_record

        return _load_run_record(run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _run_user(
    owner_id: int | None, run_record: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Keep the persisted guest role when background work rebuilds its owner.

    The guest account may already have been removed by logout or expiry. The
    run's role still applies: deriving its views must never gain an account's
    provider permissions merely because only its numeric owner was retained.
    """
    if (run_record or {}).get("guest"):
        return {"id": owner_id, "guest": True}
    return {"id": owner_id} if owner_id is not None else None


def _source_signature(workspace: Path, run_record: dict[str, Any] | None) -> str:
    from agent_monitor import proof_graph

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
    while time.monotonic() < deadline:
        record = _run_record(run_id)
        sig = _source_signature(workspace, record)
        if sig != last_sig:
            last_sig = sig
            stable_since = time.monotonic()
        terminal = str((record or {}).get("status") or "") in _TERMINAL
        # Guest Kimi writes drafts during its proof stages. Derive the final
        # artifact once, after the proof run ends, to bound sponsored work and
        # avoid competing with its in-flight provider request.
        ready = terminal or not bool((record or {}).get("guest"))
        if ready and sig and time.monotonic() - stable_since >= stable_for:
            return sig, record
        if terminal and not sig:
            return "", record
        time.sleep(0.5)
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


def _informal_worker(
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None,
    shared: dict[str, Any] | None = None,
    engine: str | None = None,
) -> None:
    from agent_monitor import proof_graph

    selected = _selected_harness(run_record, engine)
    _set_stage(
        workspace,
        "informal_dag",
        "running",
        detail=f"Shared DAG analysis · informal proof from {selected}",
        result={"proof_engine": selected, "derived_executor": "shared-dag-tool"},
    )
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
        _set_stage(
            workspace,
            "informal_dag",
            "done",
            detail=f"{len(graph.get('nodes') or [])} proof nodes · shared DAG · proof from {selected}",
            result={
                "proof_engine": selected,
                "derived_executor": "shared-dag-tool",
                "model": result.get("model"),
                "source": result.get("source"),
            },
        )
    except Exception as exc:
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


def _lean_and_formal_worker(
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None,
    shared: dict[str, Any] | None = None,
    engine: str | None = None,
) -> None:
    from agent_monitor import lean_verify, proof_bridge, proof_graph

    selected = _selected_harness(run_record, engine)
    guidance = proof_bridge.guidance_for_lean(
        (shared or {}).get("informal_graph")
    )
    _set_stage(
        workspace,
        "lean",
        "running",
        detail=f"Shared Lean translator + kernel checker · proof from {selected}",
        result={"proof_engine": selected, "derived_executor": "shared-lean-translator-kernel"},
    )
    try:
        lean_result = lean_verify.generate(
            workspace=workspace,
            run_record=run_record,
            user=user,
            model=_api_model(model),
            max_repairs=1 if (user or {}).get("guest") else 2,
            guidance=guidance,
        )
    except Exception as exc:
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

    lean_status = str(lean_result.get("status") or "error").strip().lower()
    lean_outcome = _lean_outcome(lean_status)
    has_lean_source = _has_lean_source(workspace)
    detail = (
        f"Lean {lean_status} · shared translator/kernel · proof from {selected}"
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
            "proof_lean": "available" if has_lean_source else "missing",
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
    # This fast structural pass lets Lean consume the informal proof topology
    # immediately while the richer informal DAG and Lean run concurrently.
    try:
        _source, proof = proof_graph._proof_source(workspace, run_record)
        if proof:
            shared["informal_graph"] = proof_graph.graph_from_text(proof)
    except (OSError, ValueError):
        pass

    worker_args = (workspace, run_record, user, model, shared, selected)
    sponsored_kimi = str(
        (run_record or {}).get("credential_source") or ""
    ).strip().lower() == "sponsored_kimi"
    claude_subscription = _route_from_record(run_record) == "claude_subscription"
    if (user or {}).get("guest") or sponsored_kimi or claude_subscription:
        # The sponsored gateway is a shared, cost-bounded service, while two
        # Claude Code processes for one run share the same per-user OAuth
        # account directory. Starting informal DAG and Lean translation
        # together can respectively trigger avoidable HTTP 429s or contention
        # in the account-backed CLI. Keep the same call budget and execute this
        # run's derived model work serially instead.
        _informal_worker(*worker_args)
        _lean_and_formal_worker(*worker_args)
    else:
        informal = threading.Thread(
            target=_informal_worker,
            args=worker_args,
            daemon=True,
            name=f"informal-dag-{workspace.name}",
        )
        formal = threading.Thread(
            target=_lean_and_formal_worker,
            args=worker_args,
            daemon=True,
            name=f"lean-formal-{workspace.name}",
        )
        informal.start()
        formal.start()
        informal.join()
        formal.join()

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
            detail=(
                f"{summary.get('verified', 0)}/{summary.get('total', 0)} "
                + (
                    "informal statements formally verified"
                    if lean_outcome == "success"
                    else "informal statements with conservative formal links"
                )
                + structural_note
            ),
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
            detail=(
                f"{summary.get('verified', 0)}/{summary.get('total', 0)} "
                + (
                    "informal statements formally verified"
                    if lean_outcome == "success"
                    else "informal statements with conservative formal links"
                )
                + structural_note
            ),
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
    try:
        while True:
            with _ACTIVE_LOCK:
                initial = _ACTIVE.get(key)
            if initial and initial.is_alive():
                initial.join(timeout=0.5)
                continue
            with _FORMAL_REFRESH_LOCK:
                version = _FORMAL_REFRESH_VERSION.get(key, 0)
            _refresh_formal_once(
                run_id=run_id,
                workspace=workspace,
                owner_id=owner_id,
                model=model,
                engine=engine,
            )
            with _FORMAL_REFRESH_LOCK:
                if _FORMAL_REFRESH_VERSION.get(key, 0) == version:
                    _FORMAL_REFRESH_ACTIVE.pop(key, None)
                    _FORMAL_REFRESH_VERSION.pop(key, None)
                    return
    finally:
        with _FORMAL_REFRESH_LOCK:
            active = _FORMAL_REFRESH_ACTIVE.get(key)
            if active is threading.current_thread():
                _FORMAL_REFRESH_ACTIVE.pop(key, None)
                _FORMAL_REFRESH_VERSION.pop(key, None)


def refresh_formal(
    *,
    run_id: str,
    workspace: Path,
    owner_id: int | None,
    model: str | None,
    engine: str | None,
) -> bool:
    """Schedule a formal-only derived-view refresh; return true if spawned."""
    key = str(workspace.resolve())
    with _FORMAL_REFRESH_LOCK:
        _FORMAL_REFRESH_VERSION[key] = _FORMAL_REFRESH_VERSION.get(key, 0) + 1
        active = _FORMAL_REFRESH_ACTIVE.get(key)
        if active and active.is_alive():
            return False
        thread = threading.Thread(
            target=_formal_refresh_loop,
            args=(run_id, workspace, owner_id, model, engine),
            daemon=True,
            name=f"formal-refresh-{run_id}",
        )
        _FORMAL_REFRESH_ACTIVE[key] = thread
        thread.start()
        return True

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
    processed_sig = ""
    try:
        sig, record = _wait_for_source(run_id, workspace, timeout=timeout)
        if not sig:
            for stage in ("informal_dag", "lean", "formal_dag", "coverage"):
                _set_stage(
                    workspace, stage, "skipped", detail="The engine produced no proof draft"
                )
            _finish(workspace)
            return
        _mutate(workspace, lambda state: state.update({"source_signature": sig}))
        user = _run_user(owner_id, record)
        _run_batch(workspace, record, user, model, engine)
        processed_sig = sig
        if (user or {}).get("guest"):
            _finish(workspace)
            return

        deadline = time.monotonic() + timeout
        record = _run_record(run_id)
        while (
            str((record or {}).get("status") or "") not in _TERMINAL
            and time.monotonic() < deadline
        ):
            time.sleep(0.75)
            record = _run_record(run_id)
        final_sig = _source_signature(workspace, record)
        if final_sig and final_sig != sig:
            def rerun(state: dict[str, Any]) -> None:
                state["rerun_count"] = int(state.get("rerun_count") or 0) + 1
                state["source_signature"] = final_sig
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
        _finish(workspace)
    finally:
        key = str(workspace.resolve())
        with _ACTIVE_LOCK:
            active = _ACTIVE.get(key)
            if active is threading.current_thread():
                _ACTIVE.pop(key, None)
                pending = _PENDING_START.pop(key, None)
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
    """Start the durable sidecar once for a run and return its initial state."""
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


def mark_stale(workspace: Path, detail: str = "proof.md was edited") -> None:
    """Mark derived views stale after a manual Sandbox edit."""
    def update(state: dict[str, Any]) -> None:
        state["status"] = "stale"
        for name in ("informal_dag", "lean", "formal_dag", "coverage"):
            stage = state.setdefault("stages", {}).setdefault(name, {})
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
