"""Build run-level and per-agent memory views from UCLA harness memory/."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from harness_dashboard.build import _load_jsonl

TEXT_LIMIT = 16_000
KB_PREVIEW = 8


def _clip(text: str | None, limit: int = TEXT_LIMIT) -> str | None:
    if not text:
        return None
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n\n...[truncated at {limit} chars]"


def _index_advisor_rounds(path: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in _load_jsonl(path):
        rnd = row.get("round")
        if rnd is not None:
            out[int(rnd)] = row
    return out


def _index_task_outputs(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in _load_jsonl(path):
        tid = row.get("task_id")
        if tid:
            out[str(tid)] = row
    return out


def _round_key(value: Any) -> str:
    if value is None:
        return "0"
    return str(value)


def _index_verify_refine(memory_dir: Path) -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], dict]]:
    verify: dict[tuple[str, str], dict] = {}
    refine: dict[tuple[str, str], dict] = {}
    vpath = memory_dir / "verify.jsonl"
    rpath = memory_dir / "refine.jsonl"
    for row in _load_jsonl(vpath):
        tid = row.get("task_id")
        if tid is not None:
            verify[(str(tid), _round_key(row.get("round")))] = row
    for row in _load_jsonl(rpath):
        tid = row.get("task_id")
        if tid is not None:
            refine[(str(tid), _round_key(row.get("round")))] = row
    return verify, refine


def _kb_summary(path: Path) -> dict[str, Any]:
    events = _load_jsonl(path)
    proven = failed = 0
    frontier = None
    recent: list[dict] = []
    for row in events:
        et = row.get("type") or ""
        if et == "proven_result_add":
            proven += 1
        elif et == "failed_attempt_add":
            failed += 1
        if row.get("frontier"):
            frontier = row["frontier"]
    for row in events[-KB_PREVIEW:]:
        recent.append({
            "type": row.get("type"),
            "statement": _clip(row.get("statement") or row.get("attempt") or "", 400),
            "source_plan": row.get("source_plan"),
        })
    return {
        "proven_results": proven,
        "failed_attempts": failed,
        "frontier": _clip(frontier, 1200) if frontier else None,
        "recent_events": recent,
        "total_events": len(events),
    }


def build_ucla_memory(run_dir: Path) -> dict[str, Any]:
    """Summarize harness_run_*/memory for dashboard run-level panel."""
    memory_dir = run_dir / "memory"
    if not memory_dir.is_dir():
        return {"source": "ucla_harness_memory", "memory_dir": str(memory_dir), "rounds": []}

    advisor_by_round = _index_advisor_rounds(memory_dir / "advisor_rounds.jsonl")
    verify_index, refine_index = _index_verify_refine(memory_dir)

    advisor_rounds: list[dict] = []
    for rnd in sorted(advisor_by_round):
        row = advisor_by_round[rnd]
        plan = row.get("plan") or {}
        kb = plan.get("kb_updates") or {}
        advisor_rounds.append({
            "round": rnd,
            "action": plan.get("action"),
            "frontier": _clip(kb.get("frontier"), 800),
            "strategic_note": _clip(plan.get("strategic_note"), 600),
            "task_count": len(plan.get("task_assignments") or []),
            "writeup_count": len(plan.get("writeup_tasks") or []),
            "budget_remaining": row.get("budget_remaining_after"),
        })

    verify_refine: list[dict] = []
    for (tid, rnd), vrow in sorted(verify_index.items(), key=lambda x: (x[0][0], str(x[0][1]))):
        rrow = refine_index.get((tid, rnd))
        verify_refine.append({
            "task_id": tid,
            "round": rnd,
            "verdict": vrow.get("verdict_class") or vrow.get("correct_text"),
            "major_gaps": _clip(vrow.get("major_gaps_text"), 400),
            "has_refine": rrow is not None,
        })

    final_solutions: list[dict] = []
    for row in _load_jsonl(memory_dir / "final_solutions.jsonl"):
        final_solutions.append({
            "task_id": row.get("task_id"),
            "problem_solved": _clip(row.get("problem_solved"), 600),
            "is_relaxation": row.get("is_relaxation"),
        })

    kb = _kb_summary(memory_dir / "kb_events.jsonl") if (memory_dir / "kb_events.jsonl").exists() else {}

    return {
        "source": "ucla_harness_memory",
        "memory_dir": str(memory_dir),
        "advisor_rounds": advisor_rounds,
        "kb": kb,
        "verify_refine": verify_refine[:24],
        "final_solutions": final_solutions[:12],
    }


def _advisor_plan_for_round(advisor_by_round: dict[int, dict], rnd: int) -> dict | None:
    row = advisor_by_round.get(rnd)
    if not row:
        return None
    plan = row.get("plan")
    return plan if isinstance(plan, dict) else None


def _task_assignment(plan: dict | None, task_id: str) -> dict | None:
    if not plan:
        return None
    for t in plan.get("task_assignments") or []:
        if str(t.get("task_id")) == str(task_id):
            return t
    return None


def attach_ucla_agent_memory(agents: list[dict], run_dir: Path) -> None:
    """Attach memory_context to UCLA agents from memory/*.jsonl."""
    memory_dir = run_dir / "memory"
    if not memory_dir.is_dir():
        return

    advisor_by_round = _index_advisor_rounds(memory_dir / "advisor_rounds.jsonl")
    task_outputs = _index_task_outputs(memory_dir / "task_outputs.jsonl")
    verify_index, refine_index = _index_verify_refine(memory_dir)
    conv_index: dict[str, list] = {}
    conv_path = memory_dir / "conversation.jsonl"
    if conv_path.exists():
        from harness_dashboard.build import _index_conversation
        conv_index = _index_conversation(conv_path)

    for agent in agents:
        if agent.get("memory_context"):
            continue
        stage = agent.get("stage_name") or ""
        role = agent.get("role") or ""
        ctx: dict[str, str] = {}

        if role == "orchestrator_advisor":
            rnd = int(agent.get("round_id") or 0)
            plan = _advisor_plan_for_round(advisor_by_round, rnd)
            prev = _advisor_plan_for_round(advisor_by_round, rnd - 1) if rnd > 0 else None
            if prev:
                ctx["prior_advisor_action"] = str(prev.get("action") or "")
                pkb = prev.get("kb_updates") or {}
                if pkb.get("frontier"):
                    ctx["prior_frontier"] = _clip(str(pkb["frontier"]), 1200) or ""
            if plan:
                kb = plan.get("kb_updates") or {}
                if kb.get("frontier"):
                    ctx["kb_frontier"] = _clip(str(kb["frontier"]), 1200) or ""
                for key in ("new_failed_attempts", "new_bottlenecks"):
                    items = kb.get(key) or []
                    if items:
                        ctx[key] = _clip("\n".join(f"- {x}" for x in items[:6]), 2000) or ""

        elif role == "solver":
            rnd = int(agent.get("round_id") or 0)
            tid = str(agent.get("agent_id") or "")
            plan = _advisor_plan_for_round(advisor_by_round, rnd)
            task = _task_assignment(plan, tid)
            if task:
                if task.get("description"):
                    ctx["advisor_task"] = _clip(str(task["description"]), 2000) or ""
                refs = task.get("reference_task_ids") or []
                if refs:
                    ctx["reference_tasks"] = ", ".join(str(r) for r in refs)
            entries = conv_index.get(stage) or []
            if entries:
                refs = entries[-1].get("reference_task_ids") or agent.get("received_from") or []
                if refs and "reference_tasks" not in ctx:
                    ctx["reference_tasks"] = ", ".join(str(r) for r in refs)

        elif role == "writeup":
            tid = stage.removeprefix("writeup_") if stage.startswith("writeup_") else ""
            prompts = conv_index.get(f"{stage}_prompt") or []
            if prompts:
                p = prompts[-1]
                if p.get("statement"):
                    ctx["writeup_statement"] = _clip(str(p["statement"]), 2000) or ""
                refs = p.get("reference_task_ids") or []
                if refs:
                    ctx["reference_tasks"] = ", ".join(str(r) for r in refs)
                    snippets: list[str] = []
                    for ref in refs[:4]:
                        out = task_outputs.get(str(ref)) or {}
                        text = out.get("solution") or out.get("full_text") or ""
                        if text:
                            snippets.append(f"[{ref}]\n{_clip(str(text), 800)}")
                    if snippets:
                        ctx["solver_outputs"] = "\n\n".join(snippets)

        elif role == "verifier":
            m = re.match(r"verify_(.+)_round_(\d+)$", stage)
            if m:
                tid, rnd_s = m.group(1), m.group(2)
                prev_key = (tid, str(int(rnd_s) - 1))
                if rnd_s.isdigit() and int(rnd_s) > 0:
                    prev = verify_index.get(prev_key)
                    if prev:
                        ctx["prior_verify_verdict"] = str(prev.get("verdict_class") or prev.get("correct_text") or "")
                        gaps = prev.get("major_gaps_text") or prev.get("minor_gaps_text") or ""
                        if gaps:
                            ctx["prior_verify_gaps"] = _clip(str(gaps), 1500) or ""
                rfn = refine_index.get((tid, rnd_s))
                if rfn and rfn.get("refined_solution"):
                    ctx["refined_solution_in"] = _clip(str(rfn["refined_solution"]), 1500) or ""
            else:
                m2 = re.match(r"verify_(.+)_polish_(r\d+|final)$", stage)
                if m2:
                    tid = m2.group(1)
                    polish = m2.group(2)
                    vrow = verify_index.get((tid, f"polish_{polish}" if polish != "final" else "final"))
                    if not vrow and polish != "final":
                        vrow = verify_index.get((tid, polish))
                    if vrow:
                        ctx["polish_verify_verdict"] = str(vrow.get("verdict_class") or vrow.get("correct_text") or "")
                        gaps = vrow.get("major_gaps_text") or vrow.get("minor_gaps_text") or ""
                        if gaps:
                            ctx["polish_verify_gaps"] = _clip(str(gaps), 1500) or ""

        elif role == "refiner":
            m = re.match(r"refine_(.+)_round_(\d+)$", stage)
            if m:
                tid, rnd_s = m.group(1), m.group(2)
                vrow = verify_index.get((tid, rnd_s))
                if vrow:
                    ctx["verify_verdict"] = str(vrow.get("verdict_class") or vrow.get("correct_text") or "")
                    gaps = "\n".join(
                        x for x in (vrow.get("major_gaps_text"), vrow.get("minor_gaps_text")) if x
                    )
                    if gaps:
                        ctx["verify_gaps"] = _clip(gaps, 2000) or ""

        elif role in ("lit_search", "lit_reader", "deepread_extract", "directions_advisor"):
            kb_path = memory_dir / "kb_events.jsonl"
            if kb_path.exists():
                kb = _kb_summary(kb_path)
                if kb.get("frontier"):
                    ctx["kb_frontier"] = kb["frontier"] or ""

        if ctx:
            agent["memory_context"] = ctx
            agent["memory_source"] = "memory/conversation.jsonl + memory/*.jsonl"
