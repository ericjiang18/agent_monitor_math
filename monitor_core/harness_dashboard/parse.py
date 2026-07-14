"""Parse harness stage names and infer pipeline metadata."""
from __future__ import annotations

import json
import re
from typing import Any


IMPROOF_PIPELINE = [
    {"id": "wf", "label": "Workflow", "title": "AC Workflow orchestration", "color": "#8b5cf6"},
    {"id": "author", "label": "Author", "title": "Proof author (LaTeX)", "color": "#3b82f6"},
    {"id": "critic", "label": "Critic", "title": "AC Critic review", "color": "#14b8a6"},
    {"id": "council", "label": "Council", "title": "Multi-model council", "color": "#6366f1"},
    {"id": "compute", "label": "Compute", "title": "Codex / sandbox compute", "color": "#0ea5e9"},
    {"id": "finalize", "label": "Finalize", "title": "Compile & output", "color": "#f97316"},
]


def parse_improof_agent(agent_name: str, *, folder_name: str | None = None, round_id: int | None = None) -> dict[str, Any]:
    """Map IMProofBench agent names to dashboard role / pipeline metadata."""
    name = agent_name or ""
    folder = folder_name or ""
    rnd = round_id
    if rnd is None and folder:
        m = re.match(r"^[A-Za-z]+-c(\d+)-", folder)
        if m:
            rnd = int(m.group(1))

    role_map = {
        "ACWorkflow": ("workflow", "wf"),
        "Author": ("author", "author"),
        "ACCritic": ("critic", "critic"),
        "Council": ("council", "council"),
        "CouncilMember": ("council_member", "council"),
        "Compute": ("compute", "compute"),
    }
    role, pipeline_stage = role_map.get(name, ("unknown", "other"))
    stage = folder or f"{name}_r{rnd if rnd is not None else 0}"
    return {
        "stage_name": stage,
        "role": role,
        "pipeline_stage": pipeline_stage,
        "round_id": rnd,
        "agent_id": folder.split("-")[-1] if folder and "-" in folder else stage,
    }


STAGE_PIPELINE = [
    {"id": "1", "label": "Stage 1", "title": "Literature Research", "color": "#6366f1",
     "env": "LIT_ENABLED", "default_on": True},
    {"id": "2", "label": "Stage 2", "title": "Advisor Directions", "color": "#8b5cf6"},
    {"id": "3", "label": "Stage 3", "title": "Deep Read (optional)", "color": "#a855f7",
     "env": "DEEP_READ_ENABLED", "default_on": False},
    {"id": "4", "label": "Stage 4", "title": "Advisor + Solvers + Writeups", "color": "#3b82f6"},
    {"id": "5", "label": "Stage 5", "title": "Assembly", "color": "#0ea5e9"},
    {"id": "6", "label": "Stage 6", "title": "Verify + Refine", "color": "#14b8a6"},
    {"id": "7", "label": "Stage 7", "title": "Finalize (optional)", "color": "#22c55e",
     "env": "FINALIZE_ENABLED", "default_on": False},
    {"id": "8", "label": "Stage 8", "title": "Benchmark", "color": "#eab308"},
    {"id": "9", "label": "Stage 9", "title": "Typeset (LaTeX)", "color": "#f97316"},
]


def parse_stage_name(stage: str) -> dict[str, Any]:
    """Infer role, pipeline stage, round, agent_id from harness stage_name."""
    s = stage or ""
    meta: dict[str, Any] = {
        "stage_name": s,
        "role": "unknown",
        "pipeline_stage": None,
        "round_id": None,
        "agent_id": s,
        "verify_round": None,
    }

    if s == "lit_search":
        meta.update(role="lit_search", pipeline_stage="1")
    elif s.startswith("lit_read_"):
        meta.update(role="lit_reader", pipeline_stage="1", agent_id=s.removeprefix("lit_read_"))
    elif s == "advisor_directions":
        meta.update(role="directions_advisor", pipeline_stage="2")
    elif s.startswith("deepread_triage"):
        meta.update(role="deepread_triage", pipeline_stage="3")
    elif s.startswith("deepread_extract_"):
        meta.update(role="deepread_extract", pipeline_stage="3", agent_id=s.removeprefix("deepread_extract_"))
    elif m := re.match(r"advisor_r(\d+)$", s):
        meta.update(role="orchestrator_advisor", pipeline_stage="4", round_id=int(m.group(1)))
    elif m := re.match(r"solver_r(\d+)_(.+)$", s):
        meta.update(role="solver", pipeline_stage="4", round_id=int(m.group(1)), agent_id=m.group(2))
    elif m := re.match(r"major_gap_review_r(\d+)_(.+)$", s):
        meta.update(role="major_gap_reviewer", pipeline_stage="4", round_id=int(m.group(1)), agent_id=m.group(2))
    elif s.startswith("writeup_"):
        idx = s.removeprefix("writeup_").removesuffix("_retry")
        meta.update(role="writeup", pipeline_stage="4", agent_id=idx)
        if m := re.search(r"_r(\d+)", idx):
            meta["round_id"] = int(m.group(1))
    elif m := re.match(r"assembly_advisor_(.+)$", s):
        meta.update(role="assembly_advisor", pipeline_stage="5", round_id=m.group(1))
    elif m := re.match(r"solver_r(\d+)_assembly_final$", s):
        meta.update(role="assembly_solver", pipeline_stage="5", round_id=int(m.group(1)))
    elif m := re.match(r"verify_(.+)_round_(\d+)$", s):
        meta.update(role="verifier", pipeline_stage="6", agent_id=m.group(1), verify_round=int(m.group(2)))
    elif m := re.match(r"verify_(.+)_final$", s):
        meta.update(role="verifier", pipeline_stage="6", agent_id=m.group(1), verify_round="final")
    elif m := re.match(r"verify_(.+)_polish_(.+)$", s):
        meta.update(role="verifier", pipeline_stage="6", agent_id=m.group(1))
    elif m := re.match(r"refine_(.+)_round_(\d+)$", s):
        meta.update(role="refiner", pipeline_stage="6", agent_id=m.group(1), verify_round=int(m.group(2)))
    elif m := re.match(r"minor_polish_(.+)$", s):
        meta.update(role="minor_polish", pipeline_stage="6", agent_id=m.group(1))
    elif s == "proof_sketch":
        meta.update(role="proof_sketch", pipeline_stage="4")
    elif s.startswith("finalize_"):
        meta.update(role=s, pipeline_stage="7")
    elif s.startswith("benchmark_strategy_"):
        meta.update(role="benchmark", pipeline_stage="8", agent_id=s.removeprefix("benchmark_strategy_"))
    elif s == "typeset":
        meta.update(role="typeset", pipeline_stage="9")
    elif s == "agent":
        meta.update(role="agent", pipeline_stage=None)

    return meta


def split_trace_id(trace_id: str) -> tuple[str, str]:
    if "::" in trace_id:
        run_id, stage = trace_id.split("::", 1)
        return run_id, stage
    return trace_id, ""


def extract_prompt_text(request_messages: list) -> str:
    parts = []
    for m in request_messages or []:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


def extract_response_parts(response_message: dict | None) -> dict[str, str | None]:
    if not response_message:
        return {"output": None, "thinking": None, "tool_calls": []}
    content = response_message.get("content")
    thinking = response_message.get("reasoning") or response_message.get("thinking")
    tool_calls = response_message.get("tool_calls") or []
    return {
        "output": content,
        "thinking": thinking,
        "tool_calls": tool_calls,
    }


def parse_advisor_decision(response_text: str) -> dict[str, Any] | None:
    if not response_text:
        return None
    m = re.search(r"<ADVISOR_PLAN>\s*(.*?)\s*</ADVISOR_PLAN>", response_text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return None


def infer_edges_from_advisor(plan: dict | None, advisor_stage: str, round_id: int | None) -> list[dict]:
    """Infer sent_to edges from advisor plan."""
    if not plan:
        return []
    edges = []
    r = round_id or 1
    for t in plan.get("task_assignments") or []:
        tid = t.get("task_id", "?")
        edges.append({
            "from": advisor_stage,
            "to": f"solver_r{r}_{tid}",
            "type": "task_assignment",
            "label": t.get("description", tid),
        })
    for w in plan.get("writeup_tasks") or []:
        stmt = (w.get("statement") or "")[:60]
        edges.append({
            "from": advisor_stage,
            "to": "writeup_*",
            "type": "writeup_task",
            "label": stmt,
        })
    return edges
