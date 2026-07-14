"""Compute cost/quality analysis for harness dashboard runs."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from harness_dashboard.parse import STAGE_PIPELINE

# Thresholds tuned for UCLA gpt-5.5-pro runs
HIGH_COST_USD = 5.0
EXPENSIVE_SOLVER_USD = 10.0
ADVISOR_BLOAT_INPUT = 80_000
REASONING_HEAVY_RATIO = 0.92
REASONING_HEAVY_MIN_OUT = 8000
REASONING_HEAVY_MIN_COST = 4.0
REASONING_HEAVY_ROLES = {"solver", "refiner", "writeup", "assembly_solver", "author"}
REFINE_LOOP_MIN = 2
COST_TIER_CRITICAL_USD = 10.0
COST_TIER_HIGH_USD = 5.0
COST_TIER_MEDIUM_SHARE = 0.8
REPEAT_MIN_COUNT = 2
WASTE_OUTPUT_SIM = 0.88
WASTE_REFINE_NO_PROGRESS_SIM = 0.85
WASTE_VERIFY_FAIL_MIN_COST = 2.0
WASTE_DEAD_END_MIN_COST = 0.5
WASTE_SOLVER_UNVERIFIED_MIN = 5.0
WASTE_MIN_OUTPUT_LEN = 40

WASTE_LABELS = {
    "duplicate_output": "Duplicate output",
    "no_progress_refine": "Refine unchanged",
    "dead_end": "Chain never verified",
    "verify_failed": "Verify failed (costly)",
    "solver_unverified": "Solver unverified",
    "high_cost_fail": "High cost, failed",
}


def _normalize_output(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text).strip().lower())[:12_000]


def _output_similarity(a: str | None, b: str | None) -> float:
    na, nb = _normalize_output(a), _normalize_output(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _output_hash(text: str | None) -> str | None:
    norm = _normalize_output(text)
    if len(norm) < WASTE_MIN_OUTPUT_LEN:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _chain_agents_by_task(agents: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for a in agents:
        stage = a.get("stage_name") or ""
        tid = _writeup_task_id(stage) or _verify_task_id(stage) or _refine_task_id(stage)
        if tid:
            grouped[tid].append(a)
    for tid in grouped:
        grouped[tid].sort(key=lambda x: x.get("call_seq") or 0)
    return dict(grouped)


def _solver_task_id(stage_name: str, agent_id: str | None) -> str | None:
    if agent_id:
        for tid in re.findall(r"writeup_r\d+_[0-9a-f]+", agent_id):
            return tid
    m = re.search(r"writeup_r\d+_[0-9a-f]+", stage_name)
    return m.group(0) if m else None


def _mark_waste(mon: dict, reason: str, *, similarity: float | None = None, ref_stage: str | None = None) -> None:
    if mon.get("is_token_waste"):
        return
    mon["is_token_waste"] = True
    mon["waste_reason"] = reason
    mon["waste_label"] = WASTE_LABELS.get(reason, reason)
    if similarity is not None:
        mon["waste_similarity"] = round(similarity, 3)
    if ref_stage:
        mon["waste_ref_stage"] = ref_stage
    flag = f"token_waste:{reason}"
    if flag not in mon.get("flags", []):
        mon.setdefault("flags", []).append(flag)


def _annotate_token_waste(agents: list[dict], chains: dict[str, dict]) -> list[dict]:
    """Mark agents that likely wasted tokens (duplicate output, dead ends, etc.)."""
    if not agents:
        return []

    sorted_agents = sorted(agents, key=lambda a: a.get("call_seq") or 0)
    prior_by_stage: dict[str, list[dict]] = defaultdict(list)
    output_hash_first: dict[str, dict] = {}
    chain_agents = _chain_agents_by_task(sorted_agents)
    verified_tasks = {tid for tid, c in chains.items() if _verdict_ok(c.get("final_verdict"))}

    for a in sorted_agents:
        stage = a.get("stage_name") or ""
        role = a.get("role") or ""
        cost = _agent_cost(a)
        out = a.get("output") or ""
        mon = a.setdefault("monitor", {})

        oh = _output_hash(out)
        if oh and oh in output_hash_first:
            ref = output_hash_first[oh]
            if (a.get("call_seq") or 0) > (ref.get("call_seq") or 0):
                sim = _output_similarity(out, ref.get("output"))
                _mark_waste(
                    mon,
                    "duplicate_output",
                    similarity=sim,
                    ref_stage=ref.get("stage_name"),
                )
        elif oh:
            output_hash_first[oh] = a

        for prev in prior_by_stage[stage]:
            sim = _output_similarity(out, prev.get("output"))
            if sim >= WASTE_OUTPUT_SIM:
                _mark_waste(
                    mon,
                    "duplicate_output",
                    similarity=sim,
                    ref_stage=prev.get("stage_name"),
                )
                break
        if out:
            prior_by_stage[stage].append(a)

        if role == "refiner":
            tid = _refine_task_id(stage)
            if tid:
                chain = chain_agents.get(tid, [])
                idx = next((i for i, x in enumerate(chain) if x is a), -1)
                if idx > 0:
                    prev_agent = chain[idx - 1]
                    sim = _output_similarity(out, prev_agent.get("output"))
                    if sim >= WASTE_REFINE_NO_PROGRESS_SIM:
                        _mark_waste(
                            mon,
                            "no_progress_refine",
                            similarity=sim,
                            ref_stage=prev_agent.get("stage_name"),
                        )

        if role == "verifier" and cost >= WASTE_VERIFY_FAIL_MIN_COST:
            v = (a.get("decision_impact") or {}).get("verdict")
            if _verdict_fail(v):
                _mark_waste(mon, "verify_failed")

        if role == "critic" and cost >= WASTE_VERIFY_FAIL_MIN_COST:
            di = a.get("decision_impact") or {}
            if di.get("answer_ready") is False or _verdict_fail(di.get("verdict")):
                _mark_waste(mon, "verify_failed")

        tid = _writeup_task_id(stage) or _verify_task_id(stage) or _refine_task_id(stage)
        if tid and tid in chains and cost >= WASTE_DEAD_END_MIN_COST:
            if not _verdict_ok(chains[tid].get("final_verdict")):
                if role in ("writeup", "verifier", "refiner"):
                    _mark_waste(mon, "dead_end")

        if role == "solver" and cost >= WASTE_SOLVER_UNVERIFIED_MIN:
            stid = _solver_task_id(stage, a.get("agent_id"))
            if stid and stid not in verified_tasks:
                _mark_waste(mon, "solver_unverified")
            elif not stid and mon.get("outcome") == "warn":
                _mark_waste(mon, "solver_unverified")

        if cost >= HIGH_COST_USD and mon.get("outcome") == "fail":
            _mark_waste(mon, "high_cost_fail")

    waste_groups: dict[str, dict] = defaultdict(lambda: {"count": 0, "cost_usd": 0.0, "agents": []})
    for a in agents:
        mon = a.get("monitor") or {}
        if not mon.get("is_token_waste"):
            continue
        reason = mon.get("waste_reason") or "unknown"
        waste_groups[reason]["count"] += 1
        waste_groups[reason]["cost_usd"] += _agent_cost(a)
        waste_groups[reason]["label"] = mon.get("waste_label") or reason

    return [
        {
            "reason": reason,
            "label": info.get("label", reason),
            "count": info["count"],
            "total_cost_usd": round(info["cost_usd"], 2),
        }
        for reason, info in sorted(waste_groups.items(), key=lambda x: (-x[1]["cost_usd"], -x[1]["count"]))
    ]


def _short_flow_label(stage_name: str, role: str) -> str:
    s = stage_name or role or "?"
    if len(s) <= 32:
        return s
    return s[:14] + "…" + s[-14:]


def _advisor_round_from_stage(stage: str) -> int | None:
    m = re.search(r"advisor_r(\d+)", stage or "")
    return int(m.group(1)) if m else None


def _writeup_round_from_stage(stage: str) -> int | None:
    m = re.search(r"writeup_r(\d+)_", stage or "")
    return int(m.group(1)) if m else None


def _resolve_flow_targets(
    target: str,
    from_stage: str,
    *,
    by_stage: dict[str, list[dict]],
    agents: list[dict],
) -> list[dict]:
    """Map edge target strings to concrete agent dicts."""
    if target in by_stage:
        return by_stage[target]

    if target == "writeup (background)":
        rnd = _advisor_round_from_stage(from_stage)
        if rnd is None:
            return []
        prefix = f"writeup_r{rnd}_"
        return [
            a for a in agents
            if a.get("role") == "writeup" and prefix in (a.get("stage_name") or "")
        ]

    matches = [a for a in agents if (a.get("stage_name") or "") == target]
    if matches:
        return matches
    return []


def build_agent_flow(
    agents: list[dict],
    raw_edges: list[dict] | None = None,
    *,
    pipeline: list[dict] | None = None,
) -> dict[str, Any]:
    """Build agent-level flow graph: nodes = agents, edges = dispatch / verify / refine."""
    pipeline = pipeline or STAGE_PIPELINE
    if not agents:
        return {"nodes": [], "edges": [], "columns": []}

    sorted_agents = sorted(agents, key=lambda a: (a.get("call_seq") or 0, a.get("stage_name") or ""))
    by_stage: dict[str, list[dict]] = defaultdict(list)
    by_trace: dict[str, dict] = {}
    for a in sorted_agents:
        sn = a.get("stage_name") or a.get("trace_id") or ""
        by_stage[sn].append(a)
        by_trace[a["trace_id"]] = a

    edges_out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add_edge(from_agent: dict, to_agent: dict, etype: str) -> None:
        fid, tid = from_agent["trace_id"], to_agent["trace_id"]
        if fid == tid:
            return
        key = (fid, tid, etype)
        if key in seen:
            return
        seen.add(key)
        edges_out.append({
            "from": fid,
            "to": tid,
            "type": etype,
            "from_stage": from_agent.get("stage_name"),
            "to_stage": to_agent.get("stage_name"),
        })

    def add_edge_from_stage(from_stage: str, to_target: str, etype: str) -> None:
        from_list = by_stage.get(from_stage) or []
        if not from_list:
            return
        from_agent = from_list[0]
        for to_agent in _resolve_flow_targets(
            to_target, from_stage, by_stage=by_stage, agents=sorted_agents
        ):
            add_edge(from_agent, to_agent, etype)

    for e in raw_edges or []:
        add_edge_from_stage(e.get("from") or "", e.get("to") or "", e.get("type") or "link")

    for a in sorted_agents:
        stage = a.get("stage_name") or ""
        role = a.get("role") or ""

        if a.get("sent_to"):
            for to in a["sent_to"]:
                for to_agent in _resolve_flow_targets(to, stage, by_stage=by_stage, agents=sorted_agents):
                    add_edge(a, to_agent, "dispatch")

        if role == "writeup":
            tid = _writeup_task_id(stage)
            if tid:
                v0 = f"verify_{tid}_round_0"
                for va in by_stage.get(v0) or []:
                    add_edge(a, va, "verify")
                for ps in sorted_agents:
                    if ps.get("stage_name") != "proof_sketch":
                        continue
                    ps_seq = ps.get("call_seq") or 0
                    a_seq = a.get("call_seq") or 0
                    v_seq = min((x.get("call_seq") or 0) for x in (by_stage.get(v0) or [a]))
                    if a_seq < ps_seq < v_seq:
                        add_edge(a, ps, "proof")
                        for va in by_stage.get(v0) or []:
                            add_edge(ps, va, "proof")

        if role == "verifier":
            v = (a.get("decision_impact") or {}).get("verdict")
            m = re.match(r"verify_(.+)_round_(\d+)$", stage)
            if m and _verdict_fail(v):
                tid, rnd = m.group(1), m.group(2)
                refine_stage = f"refine_{tid}_round_{rnd}"
                for ra in by_stage.get(refine_stage) or []:
                    add_edge(a, ra, "refine")
                nxt = f"verify_{tid}_round_{int(rnd) + 1}"
                for nva in by_stage.get(nxt) or []:
                    for ra in by_stage.get(refine_stage) or []:
                        add_edge(ra, nva, "reverify")
            m2 = re.match(r"verify_(.+)_polish_r(\d+)$", stage)
            if m2:
                tid = m2.group(1)
                fin = f"verify_{tid}_final"
                for fa in by_stage.get(fin) or []:
                    add_edge(a, fa, "polish")

        if role == "solver" and a.get("received_from"):
            for ref in a["received_from"]:
                for src in by_stage.get(ref) or []:
                    add_edge(src, a, "input")

    assembly_agents = [a for a in sorted_agents if a.get("role") in ("assembly_advisor", "assembly_solver")]
    finalize_agents = [a for a in sorted_agents if a.get("role") in FINALIZE_ROLES or (a.get("stage_name") or "").startswith("finalize")]
    if assembly_agents and finalize_agents:
        asm = assembly_agents[0]
        for fa in finalize_agents:
            add_edge(asm, fa, "finalize")

    in_degree: dict[str, int] = defaultdict(int)
    out_degree: dict[str, int] = defaultdict(int)
    for e in edges_out:
        out_degree[e["from"]] += 1
        in_degree[e["to"]] += 1

    stage_labels = {s["id"]: s for s in pipeline}
    columns: list[dict] = []
    by_pipeline: dict[str, list[dict]] = defaultdict(list)
    for a in sorted_agents:
        ps = str(a.get("pipeline_stage") or "other")
        by_pipeline[ps].append(a)

    for sid in sorted(by_pipeline.keys(), key=lambda x: float(x) if x.replace(".", "").isdigit() else 99):
        meta = stage_labels.get(sid, {})
        col_nodes = []
        for a in by_pipeline[sid]:
            tid = a["trace_id"]
            mon = a.get("monitor") or {}
            col_nodes.append({
                "id": tid,
                "trace_id": tid,
                "stage_name": a.get("stage_name"),
                "label": _short_flow_label(a.get("stage_name") or "", a.get("role") or ""),
                "role": a.get("role"),
                "pipeline_stage": sid,
                "call_seq": a.get("call_seq"),
                "cost_usd": round(_agent_cost(a), 2),
                "connected": in_degree[tid] + out_degree[tid] > 0,
                "outcome": mon.get("outcome"),
                "is_token_waste": mon.get("is_token_waste"),
            })
        columns.append({
            "id": sid,
            "label": meta.get("label") or f"Stage {sid}",
            "title": meta.get("title") or sid,
            "color": meta.get("color") or "#64748b",
            "nodes": col_nodes,
        })

    compact = _build_compact_flow(columns, edges_out)

    return {
        "nodes": [n for c in columns for n in c["nodes"]],
        "edges": edges_out,
        "columns": columns,
        "compact": compact,
        "edge_types": {
            "dispatch": "Advisor → solver / writeup",
            "verify": "Writeup → verify",
            "proof": "Writeup → proof sketch → verify",
            "refine": "Verify fail → refine",
            "reverify": "Refine → re-verify",
            "polish": "Polish → final verify",
            "input": "Literature / refs → solver",
            "finalize": "Assembly → finalize",
            "link": "Other",
        },
    }


def _flow_group_id(node: dict) -> str:
    ps = str(node.get("pipeline_stage") or "other")
    role = node.get("role") or "unknown"
    return f"{ps}:{role}"


def _role_display(role: str) -> str:
    return (role or "unknown").replace("_", " ")


def _build_compact_flow(columns: list[dict], edges: list[dict]) -> dict[str, Any]:
    """Role-level grouped view: fewer boxes, aggregated arrows."""
    trace_to_group: dict[str, str] = {}
    groups: dict[str, dict] = {}

    for col in columns:
        for n in col["nodes"]:
            gid = _flow_group_id(n)
            trace_to_group[n["id"]] = gid
            if gid not in groups:
                groups[gid] = {
                    "id": gid,
                    "role": n.get("role"),
                    "pipeline_stage": n.get("pipeline_stage"),
                    "label": _role_display(n.get("role")),
                    "count": 0,
                    "cost_usd": 0.0,
                    "trace_ids": [],
                    "waste_count": 0,
                    "connected": False,
                }
            g = groups[gid]
            g["count"] += 1
            g["cost_usd"] += float(n.get("cost_usd") or 0)
            g["trace_ids"].append(n.get("trace_id"))
            if n.get("is_token_waste"):
                g["waste_count"] += 1

    edge_agg: dict[tuple[str, str], dict] = {}
    for e in edges:
        gf = trace_to_group.get(e["from"])
        gt = trace_to_group.get(e["to"])
        if not gf or not gt or gf == gt:
            continue
        groups[gf]["connected"] = True
        groups[gt]["connected"] = True
        key = (gf, gt)
        if key not in edge_agg:
            edge_agg[key] = {"from": gf, "to": gt, "count": 0, "types": []}
        edge_agg[key]["count"] += 1
        if e["type"] not in edge_agg[key]["types"]:
            edge_agg[key]["types"].append(e["type"])

    compact_edges = []
    for item in edge_agg.values():
        types = item["types"]
        etype = types[0]
        for pref in ("dispatch", "verify", "refine", "reverify", "proof", "finalize", "input"):
            if pref in types:
                etype = pref
                break
        compact_edges.append({
            "from": item["from"],
            "to": item["to"],
            "type": etype,
            "count": item["count"],
            "types": types,
        })

    stage_labels = {c["id"]: c for c in columns}
    compact_columns: list[dict] = []
    for col in columns:
        sid = col["id"]
        stage_groups = [groups[gid] for gid in groups if groups[gid]["pipeline_stage"] == sid]
        stage_groups.sort(key=lambda g: (0 if g["connected"] else 1, g["role"] or ""))
        connected = [g for g in stage_groups if g["connected"]]
        isolated = [g for g in stage_groups if not g["connected"]]
        nodes = []
        for g in connected:
            nodes.append({
                "id": g["id"],
                "role": g["role"],
                "label": g["label"],
                "count": g["count"],
                "cost_usd": round(g["cost_usd"], 2),
                "waste_count": g["waste_count"],
                "trace_ids": g["trace_ids"],
                "connected": True,
                "subtitle": f"×{g['count']}" if g["count"] > 1 else "",
            })
        compact_columns.append({
            "id": sid,
            "label": col["label"],
            "title": col["title"],
            "color": col["color"],
            "nodes": nodes,
            "isolated_count": sum(g["count"] for g in isolated),
            "isolated_cost": round(sum(g["cost_usd"] for g in isolated), 2),
            "isolated_roles": sorted({g["role"] for g in isolated}),
        })

    return {
        "columns": compact_columns,
        "edges": compact_edges,
        "groups": list(groups.values()),
        "pipeline_strip": _build_pipeline_strip(compact_columns, groups),
        "link_rows": _build_link_rows(compact_edges, groups),
    }


def _build_pipeline_strip(compact_columns: list[dict], groups: dict[str, dict]) -> list[dict]:
    """One card per pipeline stage — roles listed inside, no cross-role arrows."""
    strip: list[dict] = []
    for col in compact_columns:
        sid = col["id"]
        stage_groups = sorted(
            [groups[n["id"]] for n in col.get("nodes", []) if n["id"] in groups],
            key=lambda g: g.get("role") or "",
        )
        isolated_roles = col.get("isolated_roles") or []
        for role in isolated_roles:
            gid = f"{sid}:{role}"
            if gid in groups and groups[gid] not in stage_groups:
                stage_groups.append(groups[gid])
        if not stage_groups and not col.get("isolated_count"):
            continue
        roles = []
        for g in stage_groups:
            roles.append({
                "role": g.get("role"),
                "label": g.get("label") or _role_display(g.get("role") or ""),
                "count": g.get("count", 0),
                "cost_usd": round(float(g.get("cost_usd") or 0), 2),
                "waste_count": g.get("waste_count", 0),
            })
        total_agents = sum(r["count"] for r in roles) + int(col.get("isolated_count") or 0)
        total_cost = round(sum(r["cost_usd"] for r in roles) + float(col.get("isolated_cost") or 0), 2)
        strip.append({
            "id": sid,
            "label": col.get("label") or f"Stage {sid}",
            "title": col.get("title") or "",
            "color": col.get("color") or "#64748b",
            "agent_count": total_agents,
            "cost_usd": total_cost,
            "roles": roles,
        })
    return strip


def _group_label(gid: str, groups: dict[str, dict]) -> str:
    g = groups.get(gid) or {}
    ps = str(g.get("pipeline_stage") or gid.split(":")[0])
    role = g.get("label") or _role_display(g.get("role") or gid.split(":")[-1])
    return f"S{ps} · {role}"


def _build_link_rows(compact_edges: list[dict], groups: dict[str, dict]) -> list[dict]:
    """Human-readable connection list sorted by harness flow."""
    type_order = {"dispatch": 0, "input": 1, "proof": 2, "verify": 3, "refine": 4, "reverify": 5, "polish": 6, "finalize": 7, "link": 8}
    rows = []
    for e in compact_edges:
        rows.append({
            "from": e["from"],
            "to": e["to"],
            "from_label": _group_label(e["from"], groups),
            "to_label": _group_label(e["to"], groups),
            "type": e.get("type") or "link",
            "count": e.get("count") or 1,
        })
    rows.sort(key=lambda r: (type_order.get(r["type"], 9), r["from_label"], r["to_label"]))
    return rows


def _repeat_group_key(stage_name: str, role: str) -> str:
    """Group key for detecting repeated call patterns."""
    if m := re.match(r"verify_(.+)_round_\d+$", stage_name):
        return f"verify_cycle:{m.group(1)}"
    if m := re.match(r"verify_(.+)_polish_r\d+$", stage_name):
        return f"verify_cycle:{m.group(1)}"
    if m := re.match(r"refine_(.+)_round_\d+$", stage_name):
        return f"refine_cycle:{m.group(1)}"
    if m := re.match(r"lit_read_(.+)$", stage_name):
        return f"lit_read:{m.group(1)}"
    if m := re.match(r"deepread_extract_(.+)$", stage_name):
        return f"deepread:{m.group(1)}"
    return stage_name


def _cost_tier(cost: float, share: float, rank_pct: float) -> str:
    """Rank agents into visual heat tiers."""
    if cost >= COST_TIER_CRITICAL_USD or share >= 2.5 or rank_pct <= 0.08:
        return "critical"
    if cost >= COST_TIER_HIGH_USD or share >= 1.2 or rank_pct <= 0.18:
        return "high"
    if share >= COST_TIER_MEDIUM_SHARE or rank_pct <= 0.35:
        return "medium"
    return "low"


def _annotate_cost_and_repeats(agents: list[dict], chains: dict[str, dict]) -> list[dict]:
    """Add cost tiers and repeat metadata to each agent monitor block."""
    if not agents:
        return []

    sorted_by_cost = sorted(agents, key=lambda a: _agent_cost(a), reverse=True)
    rank_map = {id(a): i / max(len(agents) - 1, 1) for i, a in enumerate(sorted_by_cost)}

    exact_counts: dict[str, int] = defaultdict(int)
    exact_costs: dict[str, float] = defaultdict(float)
    group_counts: dict[str, int] = defaultdict(int)
    group_costs: dict[str, float] = defaultdict(float)
    group_labels: dict[str, str] = {}

    for a in agents:
        stage = a.get("stage_name") or ""
        exact_counts[stage] += 1
        exact_costs[stage] += _agent_cost(a)
        gk = _repeat_group_key(stage, a.get("role") or "")
        group_counts[gk] += 1
        group_costs[gk] += _agent_cost(a)
        group_labels[gk] = stage if exact_counts[stage] == 1 else gk

    seq_by_stage: dict[str, int] = defaultdict(int)
    cycle_task_ids = {tid for tid, c in chains.items() if c.get("refine_rounds", 0) > 0 or c.get("verify_failures", 0) > 1}

    for a in agents:
        stage = a.get("stage_name") or ""
        cost = _agent_cost(a)
        mon = a.setdefault("monitor", {})
        share = float(mon.get("cost_share_pct") or 0)
        rank_pct = rank_map.get(id(a), 1.0)
        mon["cost_tier"] = _cost_tier(cost, share, rank_pct)

        exact_n = exact_counts[stage]
        seq_by_stage[stage] += 1
        mon["repeat_index"] = seq_by_stage[stage]
        mon["repeat_count"] = exact_n
        mon["is_repeat"] = exact_n >= REPEAT_MIN_COUNT

        gk = _repeat_group_key(stage, a.get("role") or "")
        mon["repeat_group"] = gk
        mon["group_repeat_count"] = group_counts[gk]

        tid = _writeup_task_id(stage) or _verify_task_id(stage) or _refine_task_id(stage)
        mon["in_verify_refine_cycle"] = bool(tid and tid in cycle_task_ids)

        if exact_n >= REPEAT_MIN_COUNT and exact_costs[stage] >= 1.0:
            if "repeated_stage" not in mon.get("flags", []):
                mon.setdefault("flags", []).append("repeated_stage")
        if mon["in_verify_refine_cycle"]:
            if "verify_refine_cycle" not in mon.get("flags", []):
                mon.setdefault("flags", []).append("verify_refine_cycle")

    repeat_groups: list[dict] = []
    seen_gk: set[str] = set()
    for gk, cnt in sorted(group_counts.items(), key=lambda x: (-x[1], -group_costs[x[0]])):
        if cnt < REPEAT_MIN_COUNT:
            continue
        if gk in seen_gk:
            continue
        seen_gk.add(gk)
        kind = "verify_refine_cycle" if gk.startswith(("verify_cycle:", "refine_cycle:")) else "identical_stage"
        if gk.startswith("lit_read:") or gk.startswith("deepread:"):
            kind = "literature_batch"
        repeat_groups.append({
            "key": gk,
            "label": group_labels.get(gk, gk),
            "count": cnt,
            "total_cost_usd": round(group_costs[gk], 2),
            "kind": kind,
        })
    return repeat_groups[:15]


def _verdict_ok(verdict: Any) -> bool:
    return verdict is True or str(verdict).lower() in ("true", "pass", "correct")


def _verdict_fail(verdict: Any) -> bool:
    return verdict is False or str(verdict).lower() in ("false", "fail", "incorrect")


def _agent_cost(a: dict) -> float:
    return float(a.get("cost_usd") or 0)


def _agent_tokens(a: dict) -> int:
    return int(a.get("total_input_tokens") or 0) + int(a.get("output_tokens") or 0)


def _writeup_task_id(stage_name: str) -> str | None:
    if stage_name.startswith("writeup_"):
        return stage_name.removeprefix("writeup_")
    return None


def _verify_task_id(stage_name: str) -> str | None:
    m = re.match(r"verify_(.+?)_(?:round_\d+|final|polish_)", stage_name)
    if m:
        return m.group(1)
    m = re.match(r"verify_(.+)_final$", stage_name)
    if m:
        return m.group(1)
    return None


def _refine_task_id(stage_name: str) -> str | None:
    m = re.match(r"refine_(.+)_round_\d+$", stage_name)
    return m.group(1) if m else None


def _load_benchmark_meta(run_output_dir: Path | None) -> dict[str, Any]:
    if not run_output_dir:
        return {}
    path = run_output_dir / "benchmark.json"
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(rows, list) or not rows:
        return {}
    row = rows[0]
    return {
        "is_relaxation": row.get("is_relaxation"),
        "verified": row.get("verified"),
        "solution_idx": row.get("solution_idx"),
        "problem_solved_preview": (row.get("problem_solved") or "")[:200],
    }


def _build_ac_round_chains(agents: list[dict]) -> dict[str, dict]:
    """Group IMProofBench Author/Critic agents by AC round."""
    chains: dict[str, dict] = defaultdict(lambda: {
        "task_id": "",
        "agents": [],
        "stages": [],
        "cost_usd": 0.0,
        "tokens": 0,
        "verify_failures": 0,
        "refine_rounds": 0,
        "final_verdict": None,
    })
    for a in agents:
        rnd = a.get("round_id")
        if rnd is None:
            continue
        tid = f"ac_round_{rnd}"
        c = chains[tid]
        c["task_id"] = tid
        c["agents"].append(a)
        c["stages"].append(a.get("stage_name") or "")
        c["cost_usd"] += _agent_cost(a)
        c["tokens"] += _agent_tokens(a)
        if a.get("role") == "critic":
            di = a.get("decision_impact") or {}
            ready = di.get("answer_ready")
            if ready is False or _verdict_fail(di.get("verdict")):
                c["verify_failures"] += 1
            if ready is True or _verdict_ok(di.get("verdict")):
                c["final_verdict"] = True
    max_round = max(
        (int(a.get("round_id") or 0) for a in agents if a.get("round_id") is not None),
        default=-1,
    )
    for rnd in range(max_round):
        tid = f"ac_round_{rnd}"
        if tid in chains:
            chains[tid]["refine_rounds"] = 1
    for c in chains.values():
        c["agents"].sort(key=lambda x: x.get("call_seq") or 0)
        c["stages"] = sorted(set(c["stages"]), key=lambda s: next(
            (a.get("call_seq") or 0 for a in c["agents"] if a.get("stage_name") == s), 0
        ))
    return dict(chains)


def _build_task_chains(agents: list[dict]) -> dict[str, dict]:
    """Group writeup/verify/refine agents by task_id."""
    if any(a.get("role") == "author" for a in agents):
        return _build_ac_round_chains(agents)
    chains: dict[str, dict] = defaultdict(lambda: {
        "task_id": "",
        "agents": [],
        "stages": [],
        "cost_usd": 0.0,
        "tokens": 0,
        "verify_failures": 0,
        "refine_rounds": 0,
        "final_verdict": None,
    })
    for a in agents:
        stage = a.get("stage_name") or ""
        tid = _writeup_task_id(stage) or _verify_task_id(stage) or _refine_task_id(stage)
        if not tid:
            continue
        c = chains[tid]
        c["task_id"] = tid
        c["agents"].append(a)
        c["stages"].append(stage)
        c["cost_usd"] += _agent_cost(a)
        c["tokens"] += _agent_tokens(a)
        if a.get("role") == "verifier":
            v = (a.get("decision_impact") or {}).get("verdict")
            if _verdict_fail(v):
                c["verify_failures"] += 1
            if stage.endswith("_final") or (a.get("decision_impact") or {}).get("round") == "final":
                c["final_verdict"] = v
            elif stage.endswith("_final"):
                c["final_verdict"] = v
        if a.get("role") == "refiner":
            c["refine_rounds"] += 1
        if a.get("role") == "verifier" and stage.endswith("_final"):
            c["final_verdict"] = (a.get("decision_impact") or {}).get("verdict")
    for c in chains.values():
        c["agents"].sort(key=lambda x: x.get("call_seq") or 0)
        c["stages"] = sorted(set(c["stages"]), key=lambda s: next(
            (a.get("call_seq") or 0 for a in c["agents"] if a.get("stage_name") == s), 0
        ))
    return dict(chains)


def _solver_outcomes(agents: list[dict], chains: dict[str, dict]) -> dict[str, str]:
    """Map solver stage_name -> outcome hint from downstream verify chains."""
    out: dict[str, str] = {}
    verified_tasks = {tid for tid, c in chains.items() if _verdict_ok(c.get("final_verdict"))}
    failed_tasks = {tid for tid, c in chains.items() if c.get("verify_failures", 0) > 0 and not _verdict_ok(c.get("final_verdict"))}
    for a in agents:
        if a.get("role") != "solver":
            continue
        aid = a.get("agent_id") or ""
        linked = False
        for tid in verified_tasks | failed_tasks:
            if aid and aid in tid:
                linked = True
                break
        if not linked:
            out[a["stage_name"]] = "neutral"
        elif any(tid in verified_tasks for tid in verified_tasks if aid in tid):
            out[a["stage_name"]] = "ok"
        else:
            out[a["stage_name"]] = "warn"
    return out


def _annotate_agents(agents: list[dict], chains: dict[str, dict], total_cost: float) -> None:
    solver_out = _solver_outcomes(agents, chains)
    task_final: dict[str, Any] = {tid: c.get("final_verdict") for tid, c in chains.items()}

    for a in agents:
        stage = a.get("stage_name") or ""
        role = a.get("role") or ""
        cost = _agent_cost(a)
        share = (cost / total_cost * 100) if total_cost > 0 else 0.0
        outcome = "neutral"
        label = "—"
        flags: list[str] = []

        if role == "verifier":
            v = (a.get("decision_impact") or {}).get("verdict")
            if _verdict_ok(v):
                outcome, label = "ok", "verify ✓"
            elif _verdict_fail(v):
                outcome, label = "fail", "verify ✗"
                if cost >= HIGH_COST_USD:
                    flags.append("high_cost_failed_verify")
        elif role == "refiner":
            outcome, label = "warn", "refine"
        elif role == "writeup":
            tid = _writeup_task_id(stage)
            fv = task_final.get(tid or "")
            if _verdict_ok(fv):
                outcome, label = "ok", "verified"
            elif _verdict_fail(fv) or (tid and chains.get(tid, {}).get("verify_failures", 0) > 0):
                outcome, label = "warn", "verify loop"
        elif role == "solver":
            so = solver_out.get(stage, "neutral")
            if so == "ok":
                outcome, label = "ok", "downstream ✓"
            elif so == "warn":
                outcome, label = "warn", "downstream ✗"
            else:
                outcome, label = "neutral", "solver"
            if cost >= EXPENSIVE_SOLVER_USD:
                flags.append("expensive_solver")
        elif role == "orchestrator_advisor":
            outcome, label = "neutral", (a.get("decision_impact") or {}).get("action") or "advisor"
            if int(a.get("total_input_tokens") or 0) >= ADVISOR_BLOAT_INPUT:
                flags.append("advisor_bloat")
        elif role == "author":
            outcome, label = "neutral", "author"
            if cost >= EXPENSIVE_SOLVER_USD:
                flags.append("expensive_solver")
        elif role == "critic":
            di = a.get("decision_impact") or {}
            ready = di.get("answer_ready")
            if ready is True or _verdict_ok(di.get("verdict")):
                outcome, label = "ok", "answer ready ✓"
            elif ready is False or _verdict_fail(di.get("verdict")):
                outcome, label = "fail", "answer ready ✗"
                if cost >= HIGH_COST_USD:
                    flags.append("high_cost_failed_verify")
            else:
                outcome, label = "neutral", "critic"
        elif role in ("council_member", "council"):
            outcome, label = "neutral", "council"
        elif role == "compute":
            outcome, label = "neutral", "compute"
        elif role == "workflow":
            outcome, label = "neutral", "workflow"
        elif role in ("lit_search", "lit_reader", "deepread_extract", "deepread_triage", "directions_advisor"):
            outcome, label = "neutral", role

        out_t = int(a.get("output_tokens") or 0)
        reason_t = int(a.get("reasoning_tokens") or 0)
        if (
            role in REASONING_HEAVY_ROLES
            and cost >= REASONING_HEAVY_MIN_COST
            and out_t >= REASONING_HEAVY_MIN_OUT
            and reason_t / max(out_t, 1) >= REASONING_HEAVY_RATIO
        ):
            flags.append("reasoning_heavy")

        if share >= 3.0 and outcome in ("fail", "warn"):
            flags.append("high_cost_low_yield")

        a["monitor"] = {
            "outcome": outcome,
            "outcome_label": label,
            "cost_usd": round(cost, 4),
            "cost_share_pct": round(share, 2),
            "flags": flags,
        }


def compute_run_analysis(
    agents: list[dict],
    totals: dict[str, Any],
    *,
    run_output_dir: Path | None = None,
    pipeline: list[dict] | None = None,
    edges: list[dict] | None = None,
) -> dict[str, Any]:
    """Return analysis block and mutate agents with monitor annotations."""
    pipeline = pipeline or STAGE_PIPELINE
    total_cost = float(totals.get("cost_usd") or 0)
    total_tokens = int(totals.get("input_tokens") or 0) + int(totals.get("output_tokens") or 0)

    chains = _build_task_chains(agents)
    _annotate_agents(agents, chains, total_cost)
    repeat_groups = _annotate_cost_and_repeats(agents, chains)
    waste_groups = _annotate_token_waste(agents, chains)

    stage_costs: dict[str, dict] = {}
    stage_labels = {s["id"]: s["title"] for s in pipeline}
    by_stage: dict[str, list[dict]] = defaultdict(list)
    by_role: dict[str, list[dict]] = defaultdict(list)
    for a in agents:
        ps = str(a.get("pipeline_stage") or "other")
        by_stage[ps].append(a)
        by_role[a.get("role") or "unknown"].append(a)

    for sid, alist in by_stage.items():
        cost = sum(_agent_cost(a) for a in alist)
        tokens = sum(_agent_tokens(a) for a in alist)
        stage_costs[sid] = {
            "label": stage_labels.get(sid, f"Stage {sid}"),
            "cost_usd": round(cost, 2),
            "tokens": tokens,
            "agents": len(alist),
            "pct_cost": round(cost / total_cost * 100, 1) if total_cost else 0,
        }

    role_costs = {}
    for role, alist in sorted(by_role.items(), key=lambda x: -sum(_agent_cost(a) for a in x[1])):
        cost = sum(_agent_cost(a) for a in alist)
        role_costs[role] = {
            "cost_usd": round(cost, 2),
            "agents": len(alist),
            "pct_cost": round(cost / total_cost * 100, 1) if total_cost else 0,
        }

    top_expensive = sorted(
        [
            {
                "trace_id": a["trace_id"],
                "stage_name": a.get("stage_name"),
                "role": a.get("role"),
                "cost_usd": round(_agent_cost(a), 2),
                "tokens": _agent_tokens(a),
                "outcome": (a.get("monitor") or {}).get("outcome"),
                "outcome_label": (a.get("monitor") or {}).get("outcome_label"),
                "flags": (a.get("monitor") or {}).get("flags") or [],
            }
            for a in agents
        ],
        key=lambda x: -x["cost_usd"],
    )[:10]

    advisor_rounds = []
    for a in sorted(
        [x for x in agents if x.get("role") == "orchestrator_advisor"],
        key=lambda x: int(x.get("round_id") or 0),
    ):
        di = a.get("decision_impact") or {}
        advisor_rounds.append({
            "round": a.get("round_id"),
            "stage_name": a.get("stage_name"),
            "cost_usd": round(_agent_cost(a), 2),
            "input_tokens": a.get("total_input_tokens"),
            "tasks": di.get("task_count", 0),
            "writeups": di.get("writeup_count", 0),
            "action": di.get("action"),
        })

    verify_chains = []
    for tid, c in sorted(chains.items(), key=lambda x: -x[1]["cost_usd"]):
        if c["refine_rounds"] == 0 and c["verify_failures"] == 0 and not c.get("stages"):
            continue
        verify_chains.append({
            "task_id": tid,
            "cost_usd": round(c["cost_usd"], 2),
            "tokens": c["tokens"],
            "verify_failures": c["verify_failures"],
            "refine_rounds": c["refine_rounds"],
            "final_verdict": c.get("final_verdict"),
            "stages": c["stages"],
        })
    verify_chains.sort(key=lambda x: -x["cost_usd"])

    lit_roles = {"lit_search", "lit_reader", "deepread_extract", "deepread_triage", "directions_advisor"}
    lit_agents = [a for a in agents if a.get("role") in lit_roles]
    lit_cost = sum(_agent_cost(a) for a in lit_agents)

    benchmark = _load_benchmark_meta(run_output_dir)

    verified_chain_cost = sum(
        c["cost_usd"] for c in chains.values() if _verdict_ok(c.get("final_verdict"))
    )
    verified_chain_tokens = sum(
        c["tokens"] for c in chains.values() if _verdict_ok(c.get("final_verdict"))
    )

    run_flags: list[dict] = []
    seen_flag_keys: set[str] = set()
    stage_total_cost: dict[str, float] = defaultdict(float)
    for a in agents:
        stage_total_cost[a.get("stage_name") or ""] += _agent_cost(a)

    def add_flag(flag_type: str, message: str, trace_id: str | None = None, severity: str = "warn"):
        key = f"{flag_type}:{trace_id or message[:40]}"
        if key in seen_flag_keys:
            return
        seen_flag_keys.add(key)
        run_flags.append({
            "type": flag_type,
            "message": message,
            "trace_id": trace_id,
            "severity": severity,
        })

    for a in agents:
        mon = a.get("monitor") or {}
        for f in mon.get("flags") or []:
            if f == "high_cost_failed_verify":
                add_flag(f, f"{a.get('stage_name')}: verify failed, cost ${mon.get('cost_usd', 0):.2f}", a.get("trace_id"))
            elif f == "expensive_solver":
                add_flag(f, f"{a.get('stage_name')}: solver cost ${mon.get('cost_usd', 0):.2f}", a.get("trace_id"))
            elif f == "reasoning_heavy":
                add_flag(f, f"{a.get('stage_name')}: reasoning share too high", a.get("trace_id"))
            elif f == "advisor_bloat":
                add_flag(f, f"{a.get('stage_name')}: advisor input > {ADVISOR_BLOAT_INPUT // 1000}k", a.get("trace_id"))
            elif f == "high_cost_low_yield":
                add_flag(f, f"{a.get('stage_name')}: high cost, low yield ({mon.get('cost_share_pct')}%)", a.get("trace_id"))
            elif f == "repeated_stage":
                rc = mon.get("repeat_count", 0)
                add_flag(
                    f,
                    f"{a.get('stage_name')}: repeated ×{rc}, total ${stage_total_cost.get(a.get('stage_name',''), 0):.2f}",
                    a.get("trace_id"),
                )
            elif f == "verify_refine_cycle":
                add_flag(f, f"{a.get('stage_name')}: in verify/refine loop", a.get("trace_id"))
            elif f.startswith("token_waste:"):
                reason = f.split(":", 1)[1]
                label = (a.get("monitor") or {}).get("waste_label") or reason
                ref = (a.get("monitor") or {}).get("waste_ref_stage")
                sim = (a.get("monitor") or {}).get("waste_similarity")
                extra = ""
                if ref:
                    extra = f", similar to {ref}"
                if sim is not None:
                    extra += f" ({sim:.0%})"
                add_flag(
                    "token_waste",
                    f"{a.get('stage_name')}: {label}{extra} · ${mon.get('cost_usd', 0):.2f}",
                    a.get("trace_id"),
                )

    for c in verify_chains:
        if c["refine_rounds"] >= REFINE_LOOP_MIN:
            add_flag(
                "refine_loop",
                f"{c['task_id']}: {c['refine_rounds']} refine rounds, total cost ${c['cost_usd']:.2f}",
            )

    if benchmark.get("is_relaxation") is True:
        add_flag(
            "final_relaxation",
            f"Final benchmark is partial/relaxation ({benchmark.get('solution_idx', '?')})",
            severity="info",
        )

    stage4 = stage_costs.get("4", {})
    if stage4.get("pct_cost", 0) >= 50:
        add_flag(
            "stage4_dominant",
            f"Stage 4 is {stage4['pct_cost']}% of cost (${stage4['cost_usd']:.2f})",
            severity="info",
        )

    # Keep the panel readable: prioritize non-info flags, cap total.
    severity_order = {"err": 0, "warn": 1, "info": 2}
    run_flags.sort(key=lambda x: (severity_order.get(x.get("severity", "warn"), 1), x.get("type", "")))
    if len(run_flags) > 24:
        run_flags = run_flags[:24]

    return {
        "stage_costs": stage_costs,
        "role_costs": role_costs,
        "top_expensive": top_expensive,
        "advisor_rounds": advisor_rounds,
        "verify_chains": verify_chains[:12],
        "repeat_groups": repeat_groups,
        "waste_groups": waste_groups,
        "visual_legend": {
            "cost_tiers": {
                "critical": "≥$10 or top 8% — dashed border",
                "high": "≥$5 or top 18% — dashed border",
                "medium": "≥0.8% of run cost",
            },
            "waste": "Red fill = wasted/ineffective tokens (duplicate output, dead chains, failed verify, etc.)",
            "high_cost_mark": "Dashed border = cost_tier critical or high",
            "repeat_note": "Same stage_name called many times (e.g. proof_sketch×N) is not red unless output repeats",
            "hash_note": "Hash match = normalized output strings are identical (case/whitespace ignored, not fuzzy words)",
        },
        "literature": {
            "cost_usd": round(lit_cost, 2),
            "pct_cost": round(lit_cost / total_cost * 100, 1) if total_cost else 0,
            "papers_read": len([a for a in agents if a.get("role") == "lit_reader"]),
            "deepread_extracts": len([a for a in agents if a.get("role") == "deepread_extract"]),
        },
        "benchmark": benchmark,
        "effective": {
            "verified_chain_cost_usd": round(verified_chain_cost, 2),
            "verified_chain_tokens": verified_chain_tokens,
            "cost_efficiency_pct": round(verified_chain_cost / total_cost * 100, 1) if total_cost else 0,
            "token_efficiency_pct": round(verified_chain_tokens / total_tokens * 100, 1) if total_tokens else 0,
        },
        "flags": run_flags,
        "agent_flow": build_agent_flow(agents, edges, pipeline=pipeline),
    }


SANKEY_NODES = [
    {"id": "literature", "label": "Literature", "stage": "1–3", "color": "#6366f1", "depth": 0, "order": 0},
    {"id": "advisor", "label": "Advisor", "stage": "2–4", "color": "#8b5cf6", "depth": 1, "order": 1},
    {"id": "solver", "label": "Solver", "stage": "4", "color": "#3b82f6", "depth": 2, "order": 2},
    {"id": "writeup", "label": "Writeup", "stage": "4", "color": "#0ea5e9", "depth": 2, "order": 3},
    {"id": "assembly", "label": "Assembly", "stage": "5", "color": "#6366f1", "depth": 3, "order": 4},
    {"id": "verify", "label": "Verify", "stage": "6", "color": "#14b8a6", "depth": 4, "order": 5},
    {"id": "refine", "label": "Refine", "stage": "6", "color": "#22c55e", "depth": 5, "order": 6},
    {"id": "finalize", "label": "Finalize", "stage": "7–9", "color": "#f97316", "depth": 6, "order": 7},
]

SANKEY_NODE_IDS = {n["id"] for n in SANKEY_NODES}
SANKEY_NODE_META = {n["id"]: n for n in SANKEY_NODES}

LIT_FLOW_ROLES = {"lit_search", "lit_reader", "deepread_extract", "deepread_triage", "directions_advisor"}
FINALIZE_ROLES = {"finalize_polish", "finalize_typeset", "minor_polish", "benchmark"}


def _role_sankey_node(role: str, stage: str) -> str | None:
    """Map each agent to exactly one Sankey node (no double counting)."""
    if role in LIT_FLOW_ROLES:
        return "literature"
    if role == "orchestrator_advisor":
        return "advisor"
    if role == "solver":
        return "solver"
    if role in ("writeup", "proof_sketch") or stage == "proof_sketch":
        return "writeup"
    if role == "verifier":
        return "verify"
    if role == "refiner":
        return "refine"
    if role == "assembly_advisor" or role == "assembly_solver":
        return "assembly"
    if role in FINALIZE_ROLES or stage.startswith("finalize"):
        return "finalize"
    return None


def _agent_flow_tokens(a: dict) -> int:
    return int(a.get("total_input_tokens") or 0) + int(a.get("output_tokens") or 0)


def _agent_flow_cost(a: dict) -> float:
    return float(a.get("cost_usd") or 0)


def build_sankey_flow(agents: list[dict], *, metric: str = "tokens") -> dict[str, Any]:
    """Build Sankey nodes/links — each agent counted once, fixed pipeline order."""
    val_fn = _agent_flow_tokens if metric == "tokens" else _agent_flow_cost
    link_vals: dict[tuple[str, str], float] = defaultdict(float)
    node_totals: dict[str, float] = defaultdict(float)

    def add_link(src: str, tgt: str, amount: float) -> None:
        if src not in SANKEY_NODE_IDS or tgt not in SANKEY_NODE_IDS or src == tgt:
            return
        if amount <= 0:
            return
        link_vals[(src, tgt)] += amount

    lit_total = 0.0
    solver_total = 0.0
    writeup_total = 0.0
    verify_total = 0.0
    refine_total = 0.0
    assembly_total = 0.0
    finalize_total = 0.0
    advisor_total = 0.0
    assembly_from_advisor = 0.0
    assembly_from_solver = 0.0
    assembly_from_writeup = 0.0

    for a in agents:
        stage = a.get("stage_name") or ""
        role = a.get("role") or ""
        v = val_fn(a)
        node = _role_sankey_node(role, stage)
        if not node:
            continue
        node_totals[node] += v
        if node == "literature":
            lit_total += v
        elif node == "advisor":
            advisor_total += v
        elif node == "solver":
            solver_total += v
        elif node == "writeup":
            writeup_total += v
        elif node == "verify":
            verify_total += v
        elif node == "refine":
            refine_total += v
        elif node == "assembly":
            assembly_total += v
            if role == "assembly_advisor":
                assembly_from_advisor += v
            else:
                assembly_from_solver += v * 0.6
                assembly_from_writeup += v * 0.4
        elif node == "finalize":
            finalize_total += v

    if lit_total > 0 and advisor_total > 0:
        add_link("literature", "advisor", lit_total)
    if solver_total > 0:
        add_link("advisor", "solver", solver_total)
    if writeup_total > 0:
        add_link("advisor", "writeup", writeup_total)
    if verify_total > 0:
        if writeup_total > 0:
            add_link("writeup", "verify", min(writeup_total, verify_total))
            verify_extra = verify_total - min(writeup_total, verify_total)
            if verify_extra > 0:
                add_link("advisor", "verify", verify_extra)
        else:
            add_link("advisor", "verify", verify_total)
    if refine_total > 0 and verify_total > 0:
        add_link("verify", "refine", refine_total)
    if assembly_from_advisor > 0:
        add_link("advisor", "assembly", assembly_from_advisor)
    if assembly_from_solver > 0:
        add_link("solver", "assembly", assembly_from_solver)
    if assembly_from_writeup > 0:
        add_link("writeup", "assembly", assembly_from_writeup)
    if finalize_total > 0:
        if assembly_total > 0:
            add_link("assembly", "finalize", min(assembly_total, finalize_total))
            remainder = finalize_total - min(assembly_total, finalize_total)
            if remainder > 0 and verify_total > 0:
                add_link("verify", "finalize", remainder)
            elif remainder > 0:
                add_link("advisor", "finalize", remainder)
        elif verify_total > 0:
            add_link("verify", "finalize", finalize_total)
        else:
            add_link("advisor", "finalize", finalize_total)

    active_ids = {nid for nid, t in node_totals.items() if t > 0}
    active_ids.update(src for src, _ in link_vals)
    active_ids.update(tgt for _, tgt in link_vals)

    nodes = [
        {
            **SANKEY_NODE_META[nid],
            "total": round(node_totals.get(nid, 0), 2 if metric == "cost" else 0),
        }
        for nid in sorted(active_ids, key=lambda x: SANKEY_NODE_META.get(x, {}).get("order", 99))
        if nid in SANKEY_NODE_META
    ]

    links = [
        {
            "source": src,
            "target": tgt,
            "value": round(v, 2 if metric == "cost" else 0),
        }
        for (src, tgt), v in sorted(link_vals.items(), key=lambda x: (SANKEY_NODE_META[x[0][0]]["order"], -x[1]))
        if v > 0 and src in active_ids and tgt in active_ids
    ]

    return {
        "metric": metric,
        "unit": "USD" if metric == "cost" else "tokens",
        "nodes": nodes,
        "links": links,
    }


def build_sankey_bundle(agents: list[dict]) -> dict[str, Any]:
    return {
        "tokens": build_sankey_flow(agents, metric="tokens"),
        "cost": build_sankey_flow(agents, metric="cost"),
    }


def summarize_run_for_manifest(run_data: dict) -> dict[str, Any]:
    """Compact analysis fields for manifest listing / cross-run compare."""
    analysis = run_data.get("analysis") or {}
    flags = analysis.get("flags") or []
    eff = analysis.get("effective") or {}
    benchmark = analysis.get("benchmark") or {}
    stage4 = (analysis.get("stage_costs") or {}).get("4", {})
    author = (analysis.get("stage_costs") or {}).get("author", {})
    outputs = run_data.get("outputs") or {}
    source = run_data.get("source") or ""
    if source == "improofbench_artifacts":
        ac_rounds = run_data.get("rounds_completed") or outputs.get("rounds_completed")
        answer_ready = outputs.get("last_critic_accepted")
        if answer_ready is None:
            answer_ready = outputs.get("final_critic_answer_ready")
        return {
            "flag_count": len(flags),
            "top_flag": flags[0]["type"] if flags else None,
            "author_pct": author.get("pct_cost"),
            "stage4_pct": author.get("pct_cost"),
            "ac_rounds": ac_rounds,
            "answer_ready": answer_ready,
            "is_relaxation": None if answer_ready is None else (not bool(answer_ready)),
            "cost_efficiency_pct": eff.get("cost_efficiency_pct"),
            "refine_loops": sum(
                1 for c in (analysis.get("verify_chains") or []) if c.get("refine_rounds", 0) >= REFINE_LOOP_MIN
            ),
        }
    return {
        "flag_count": len(flags),
        "top_flag": flags[0]["type"] if flags else None,
        "stage4_pct": stage4.get("pct_cost"),
        "is_relaxation": benchmark.get("is_relaxation"),
        "cost_efficiency_pct": eff.get("cost_efficiency_pct"),
        "refine_loops": sum(1 for c in (analysis.get("verify_chains") or []) if c.get("refine_rounds", 0) >= REFINE_LOOP_MIN),
    }
