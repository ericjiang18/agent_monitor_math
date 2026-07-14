"""Background job manager for engine runs."""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any  # noqa: F401 — used throughout

from agent_monitor import CACHE_DIR, HERMES_HOME, PROBLEMS_DIR, RUNS_DIR
from agent_monitor.schema import normalize_run

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_DELETED_RUNS: set[str] = set()
_STOP_EVENTS: dict[str, threading.Event] = {}
_PROCS: dict[str, Any] = {}  # run_id -> subprocess.Popen


class StopRequested(Exception):
    """Raised inside a run when the user pressed Stop."""


def _stop_event(run_id: str) -> threading.Event:
    with _LOCK:
        ev = _STOP_EVENTS.get(run_id)
        if ev is None:
            ev = threading.Event()
            _STOP_EVENTS[run_id] = ev
        return ev


def _register_proc(run_id: str, proc: Any) -> None:
    with _LOCK:
        _PROCS[run_id] = proc


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


def _write_run(run: dict[str, Any]) -> Path:
    cache = _cache_dir()
    path = cache / f"{run['run_id']}.json"
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    runs_path = RUNS_DIR / f"{run['run_id']}.json"
    runs_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    _upsert_manifest(run)
    return path


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
        "last_ts": run.get("updated_at") or _now(),
    }
    by_id = {e.get("run_id"): e for e in entries if e.get("run_id")}
    by_id[entry["run_id"]] = {**(by_id.get(entry["run_id"]) or {}), **entry}
    payload = {"built_at": _now(), "runs": list(by_id.values())}
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _pipeline_for(engine: str) -> list[dict]:
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


def list_jobs() -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(j) for j in _JOBS.values() if j.get("run_id") not in _DELETED_RUNS]


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        j = _JOBS.get(job_id)
        return dict(j) if j else None


def resolve_problem(problem_id: str | None, problem_text: str | None) -> tuple[str, str, Path | None]:
    """Return (problem_id, text, path_or_none)."""
    if problem_text and problem_text.strip():
        pid = problem_id or f"adhoc_{uuid.uuid4().hex[:8]}"
        return pid, problem_text.strip(), None

    if not problem_id:
        raise ValueError("Provide problem_id or problem_text")

    man = PROBLEMS_DIR / "manifest.json"
    if man.exists():
        for p in json.loads(man.read_text(encoding="utf-8")).get("problems") or []:
            if p.get("problem_id") == problem_id:
                path = PROBLEMS_DIR / p["statement_path"]
                return problem_id, path.read_text(encoding="utf-8", errors="replace"), path

    # direct path under problems/
    cand = PROBLEMS_DIR / problem_id
    if cand.exists():
        return cand.stem, cand.read_text(encoding="utf-8", errors="replace"), cand

    raise FileNotFoundError(f"Unknown problem: {problem_id}")


def start_job(
    *,
    engine: str,
    problem_id: str | None = None,
    problem_text: str | None = None,
    model: str | None = None,
    max_iterations: int = 40,
) -> dict[str, Any]:
    from agent_monitor.engines_registry import all_engine_ids

    if engine not in all_engine_ids():
        raise ValueError(f"Unsupported engine: {engine}")

    pid, text, path = resolve_problem(problem_id, problem_text)
    job_id = uuid.uuid4().hex[:10]
    run_id = f"{engine}_{pid}_{job_id}"
    _stop_event(run_id).clear()
    ws = workspace_dir(run_id)
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
            "problem_text_preview": text[:500],
        },
        engine=engine,  # type: ignore[arg-type]
    )
    _write_run(run)

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
        },
        daemon=True,
        name=f"engine-{engine}-{job_id}",
    )
    thread.start()
    return dict(job)


def _update_job(job_id: str, **fields: Any) -> None:
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)
            _JOBS[job_id]["updated_at"] = _now()


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
) -> None:
    started = time.time()
    ws = Path(workspace)
    _append_chat(run_id, "system", f"▶ run started · engine {engine} · problem {problem_id}" + (f" · model {model}" if model else ""))
    try:
        if engine == "hermes":
            result_run = _run_hermes(
                run_id=run_id,
                problem_id=problem_id,
                problem_text=problem_text,
                model=model,
                max_iterations=max_iterations,
                started=started,
                workspace=ws,
            )
        elif engine == "improof":
            from agent_monitor.runners import improof as improof_runner

            path = ws / "problem.txt"
            result = improof_runner.run_problem(
                path,
                problem_id=problem_id,
                output_dir=ws,
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
            result = ucla_runner.run_problem(
                path,
                problem_id=problem_id,
                output_dir=ws,
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
            result_run = _run_cli_engine(
                run_id=run_id,
                engine=engine,
                problem_id=problem_id,
                problem_text=problem_text,
                started=started,
                workspace=ws,
            )

        if _stop_event(run_id).is_set():
            result_run["status"] = "stopped"
        result_run["job_id"] = job_id
        result_run["workspace"] = str(ws)
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
                "pipeline": _pipeline_for(engine),
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


def _attach_proof_provenance(agents: list[dict[str, Any]], workspace: Path | None) -> None:
    """Give write/finalize agents the proof.tex text so Agent Trace can attribute lines."""
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
            a["_provenance_text"] = tex
            # Also surface a short preview in output if it's only a tool log.
            out = a.get("output") or ""
            if tex[:200] not in out and "\\documentclass" not in out:
                a["output"] = (out + f"\n\n--- {proof.name} ---\n" + tex)[:12000]
                a.setdefault("output_source", f"workspace {proof.name}")


def _attach_final_latex(run: dict[str, Any]) -> None:
    try:
        from harness_dashboard.latex_provenance import build_final_latex_bundle

        bundle = build_final_latex_bundle(run)
        if bundle:
            run["final_latex"] = bundle
    except Exception:  # noqa: BLE001
        pass


def _attach_analysis(run: dict[str, Any], workspace: Path | None) -> dict[str, Any]:
    """Add the Run Analysis / Agent Flow block the dashboard renders."""
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
):
    """Callback that streams subprocess output into the run JSON while running."""
    last_enrich = [0.0]

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
                merged = _merge_artifact_run(run, rich)
                merged["status"] = "running"
                run = normalize_run(merged, engine=engine)  # type: ignore[arg-type]
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
) -> dict[str, Any]:
    from agent_monitor.runners import hermes as hermes_runner

    # Live placeholder updates via step callback when available
    agents: list[dict[str, Any]] = []
    tool_log: list[str] = []

    def _flush(partial_output: str = "") -> None:
        run = normalize_run(
            {
                "run_id": run_id,
                "problem_id": problem_id,
                "trace_name": f"[HERMES] {problem_id}",
                "status": "running",
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for("hermes"),
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
    agent = hermes_runner.create_agent(model=model, max_iterations=max_iterations)

    stop_ev = _stop_event(run_id)

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
        agents.append(
            {
                "trace_id": f"{run_id}::iter-{iteration}",
                "stage_name": f"iter-{iteration}",
                "role": "reasoning",
                "pipeline_stage": "plan",
                "call_seq": iteration,
                "round_id": iteration,
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

    prompt = (
        "You are working on an informal mathematics proof problem.\n"
        "Use tools as needed (code, files, terminal, subagents).\n\n"
        f"WORKSPACE (your sandbox directory): {workspace}\n"
        "Requirements:\n"
        f"1. Maintain your evolving proof write-up in {workspace}/proof.md "
        "(Markdown; use $...$ / $$...$$ for math, headings for structure). "
        "Update this file as your proof develops — write early drafts, then refine.\n"
        f"2. You may create scratch files (notes, python checks) inside {workspace}.\n"
        "3. Finish by making proof.md a clean, self-contained informal proof.\n\n"
        f"PROBLEM:\n{problem_text}\n"
    )
    result = agent.run_conversation(prompt)
    elapsed = time.time() - started
    final = (result or {}).get("final_response") or ""
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
            "status": "finished",
            "tool_calls": (result or {}).get("tool_call_count"),
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
    run = normalize_run(
        {
            "run_id": run_id,
            "problem_id": problem_id,
            "trace_name": f"[HERMES] {problem_id}",
            "status": "finished" if (result or {}).get("completed", True) else "failed",
            "completed": (result or {}).get("completed", True),
            "updated_at": _now(),
            "workspace": str(workspace),
            "pipeline": _pipeline_for("hermes"),
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


def _run_cli_engine(
    *,
    run_id: str,
    engine: str,
    problem_id: str,
    problem_text: str,
    started: float,
    workspace: Path,
) -> dict[str, Any]:
    """Run an external CLI harness (codex / openclaude / openhands / …) live."""
    import subprocess

    from agent_monitor.engines_registry import CLI_ENGINES, build_cli_command, proof_prompt

    spec = CLI_ENGINES.get(engine) or {}
    prompt = proof_prompt(problem_text, workspace=workspace)
    argv = build_cli_command(
        engine, prompt=prompt, workspace=workspace, problem_file=workspace / "problem.txt"
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

    def _flush(output_tail: str, status: str = "running") -> dict[str, Any]:
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
        run = normalize_run(
            {
                "run_id": run_id,
                "problem_id": problem_id,
                "trace_name": f"[{spec.get('label', engine).upper()}] {problem_id}",
                "status": status,
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for(engine),
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
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    from agent_monitor.engines_registry import engine_extra_path

    extra = engine_extra_path(engine)
    if extra:
        env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    # OpenHands headless boots from LLM_MODEL / LLM_API_KEY (--override-with-envs).
    if engine == "openhands":
        key = env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY")
        if key:
            env.setdefault("LLM_API_KEY", key)
            env.setdefault("LLM_MODEL", env.get("AGENT_MONITOR_OPENHANDS_MODEL", "openai/gpt-5.2"))
    timeout_s = int(os.environ.get("AGENT_MONITOR_CLI_TIMEOUT", "3600"))
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
        )
    except OSError as exc:
        return _flush(f"failed to launch: {exc}", status="failed")

    _register_proc(run_id, proc)
    stopped = False
    last_flush = 0.0
    deadline = started + timeout_s
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            buf.append(line)
            parser.feed(line)
            now = time.time()
            if stop_ev.is_set():
                proc.kill()
                parser.lines.append("[agent-monitor] stopped by user")
                stopped = True
                break
            if now - last_flush > 2.0:
                _flush(parser.output())
                last_flush = now
            if now > deadline:
                proc.kill()
                parser.lines.append(f"[agent-monitor] timeout after {timeout_s}s — killed")
                break
        proc.wait(timeout=30)
    finally:
        _unregister_proc(run_id)
    if engine == "openhands":
        parser.finalize_openhands()
    elif engine == "openclaw":
        parser.finalize_openclaw()
    if stopped or stop_ev.is_set():
        return _flush(parser.output(), status="stopped")
    ok = proc.returncode == 0
    return _flush(parser.output(), status="finished" if ok else "failed")


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
    run = normalize_run(
        {
            "run_id": run_id,
            "problem_id": problem_id,
            "trace_name": f"[{engine.upper()}] {problem_id}",
            "status": "finished" if ok else "failed",
            "error": result.get("error"),
            "updated_at": _now(),
            "pipeline": _pipeline_for(engine),
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
                for k in ("status", "returncode", "workflow", "command", "hint", "output_dir")
            },
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
    if not path.exists():
        return []
    msgs: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return msgs


def _append_chat(run_id: str, role: str, content: str) -> dict[str, Any]:
    path = _chat_file(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"role": role, "content": content, "ts": _now()}
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


def send_human_message(
    run_id: str,
    message: str,
    *,
    model: str | None = None,
    max_iterations: int = 40,
) -> dict[str, Any]:
    """Human-in-the-loop chat: queue feedback or continue a finished run."""
    message = (message or "").strip()
    if not message:
        raise ValueError("empty message")
    ws = RUNS_DIR / "workspaces" / run_id
    if not ws.is_dir():
        raise FileNotFoundError("workspace not found")

    user_entry = _append_chat(run_id, "user", message)
    active = _run_is_active(run_id)
    if active:
        (ws / "human_feedback_pending.txt").write_text(message, encoding="utf-8")
        _append_chat(run_id, "system", "Feedback saved while the agent is running.")
        return {
            "ok": True,
            "status": "queued",
            "job_id": active.get("job_id"),
            "messages": list_chat(run_id),
            "user": user_entry,
        }

    _stop_event(run_id).clear()
    run_data = _load_run_record(run_id)
    engine = run_data.get("engine") or run_id.split("_", 1)[0]
    problem_text = (ws / "problem.txt").read_text(encoding="utf-8") if (ws / "problem.txt").exists() else (
        run_data.get("problem_text_preview") or ""
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
            "model": model,
            "kind": "continue",
        }

    _append_chat(run_id, "system", "Continuing proof with your feedback…")
    thread = threading.Thread(
        target=_execute_continue,
        kwargs={
            "job_id": job_id,
            "run_id": run_id,
            "engine": engine,
            "message": message,
            "problem_text": problem_text,
            "model": model,
            "max_iterations": max_iterations,
            "workspace": str(ws),
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
) -> None:
    started = time.time()
    ws = Path(workspace)
    proof_excerpt = ""
    proof_name = "proof.md"
    for cand in ("proof.md", "proof.tex"):
        pf = ws / cand
        if pf.exists():
            proof_excerpt = pf.read_text(encoding="utf-8", errors="replace")[:8000]
            proof_name = cand
            break

    continuation = (
        "You are continuing work on an informal mathematics proof based on human feedback.\n"
        f"WORKSPACE: {ws}\n"
        f"Maintain/update {proof_name} "
        + (
            "as a Markdown document (use $...$ / $$...$$ for math).\n\n"
            if proof_name.endswith(".md")
            else "as a complete compilable LaTeX document (\\documentclass{article}).\n\n"
        )
        + f"ORIGINAL PROBLEM:\n{problem_text}\n\n"
    )
    if proof_excerpt:
        continuation += f"CURRENT {proof_name}:\n{proof_excerpt}\n\n"
    continuation += f"HUMAN FEEDBACK — address this in the proof:\n{message}\n"

    run_data = _load_run_record(run_id)
    run_data["status"] = "running"
    run_data["updated_at"] = _now()
    _write_run(run_data)

    try:
        if engine in {"hermes", "improof", "ucla"}:
            # improof/ucla pipelines are not resumable mid-flight; refinements
            # go through the Hermes agent operating on the same workspace.
            result_run = _run_hermes(
                run_id=run_id,
                problem_id=run_data.get("problem_id") or "continue",
                problem_text=continuation,
                model=model,
                max_iterations=max_iterations,
                started=started,
                workspace=ws,
            )
            result_run["engine"] = engine
            result_run["source"] = engine
            result_run["trace_name"] = run_data.get("trace_name") or result_run.get("trace_name")
        else:
            result_run = _run_cli_engine(
                run_id=run_id,
                engine=engine,
                problem_id=run_data.get("problem_id") or "continue",
                problem_text=continuation,
                started=started,
                workspace=ws,
            )
        result_run["job_id"] = job_id
        result_run["workspace"] = str(ws)
        result_run["updated_at"] = _now()
        _write_run(result_run)
        status = "finished" if result_run.get("status") != "failed" else "failed"
        _update_job(job_id, status=status, error=result_run.get("error"))
        _append_chat(
            run_id,
            "assistant",
            (result_run.get("agents") or [{}])[-1].get("output", "")[:2000] or f"Run {status}.",
        )
    except StopRequested:
        stopped_run = _load_run_record(run_id)
        stopped_run["status"] = "stopped"
        stopped_run["updated_at"] = _now()
        _write_run(stopped_run)
        _update_job(job_id, status="stopped")
        _append_chat(run_id, "system", "Run stopped by user.")
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, status="failed", error=str(exc))
        _append_chat(run_id, "system", f"Continue failed: {exc}")


def stop_run(run_id: str) -> dict[str, Any]:
    """Stop a running job: signal the stop event and kill any subprocess."""
    active = _run_is_active(run_id)
    _stop_event(run_id).set()
    with _LOCK:
        proc = _PROCS.get(run_id)
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass
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
) -> dict[str, Any]:
    """Resume a stopped/finished/failed run, optionally with extra guidance."""
    if _run_is_active(run_id):
        return {"ok": False, "status": "already_running", "run_id": run_id}
    _stop_event(run_id).clear()

    ws = RUNS_DIR / "workspaces" / run_id
    if not ws.is_dir():
        raise FileNotFoundError("workspace not found")
    run_data = _load_run_record(run_id)
    engine = run_data.get("engine") or run_id.split("_", 1)[0]
    problem_text = (
        (ws / "problem.txt").read_text(encoding="utf-8")
        if (ws / "problem.txt").exists()
        else (run_data.get("problem_text_preview") or "")
    )
    feedback = (message or "").strip() or (
        "Continue from where the previous session stopped. Review the workspace, "
        "then keep improving proof.tex until it is a complete, correct proof."
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
            "model": model,
            "kind": "continue",
        }
    _append_chat(run_id, "system", "Continuing run…")
    thread = threading.Thread(
        target=_execute_continue,
        kwargs={
            "job_id": job_id,
            "run_id": run_id,
            "engine": engine,
            "message": feedback,
            "problem_text": problem_text,
            "model": model,
            "max_iterations": max_iterations,
            "workspace": str(ws),
        },
        daemon=True,
        name=f"continue-{run_id}-{job_id}",
    )
    thread.start()
    return {"ok": True, "status": "continuing", "run_id": run_id, "job_id": job_id}


def delete_run(run_id: str) -> dict[str, Any]:
    """Delete a run, its workspace, cache JSON, manifest entry, and Hermes memory."""
    import shutil

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

    _stop_event(run_id).set()
    with _LOCK:
        proc = _PROCS.pop(run_id, None)
        _DELETED_RUNS.add(run_id)
        for jid in [k for k, j in _JOBS.items() if j.get("run_id") == run_id]:
            _JOBS.pop(jid, None)
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass

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
