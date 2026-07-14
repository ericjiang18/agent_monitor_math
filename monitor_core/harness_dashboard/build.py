"""Build harness dashboard cache from calls.jsonl and optional harness output."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from harness_dashboard.parse import (
    STAGE_PIPELINE,
    extract_prompt_text,
    extract_response_parts,
    infer_edges_from_advisor,
    parse_advisor_decision,
    parse_stage_name,
    split_trace_id,
)

CALLS_JSONL = Path(os.environ.get("LLM_MONITOR_LOG", "monitor/calls.jsonl"))
HARNESS_OUTPUTS_JSONL = Path(
    os.environ.get("LLM_HARNESS_OUTPUTS", "monitor/harness_outputs.jsonl")
)
CACHE_DIR = Path(os.environ.get("LLM_DASHBOARD_CACHE", "monitor/.cache")) / "harness"
HARNESS_OUTPUT_ROOT = Path(
    os.environ.get("HARNESS_OUTPUT_ROOT", "harness_0518_Final/output")
)
PREVIOUS_LOG_DIR = Path(
    os.environ.get("PREVIOUS_LOG_DIR", "previous_log")
)


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _find_conversation_files() -> dict[str, Path]:
    """Map run_id -> conversation.jsonl path."""
    out: dict[str, Path] = {}
    if not HARNESS_OUTPUT_ROOT.exists():
        return out
    for conv in HARNESS_OUTPUT_ROOT.rglob("conversation.jsonl"):
        if conv.parent.name != "memory":
            continue
        parent = conv.parent.parent.parent.name
        run_key = parent.replace("-", "_").lower()
        out[run_key] = conv
        out[parent] = conv
    return out


def _find_run_output_dirs() -> dict[str, Path]:
    """Map run_id -> harness_run_* directory."""
    out: dict[str, Path] = {}
    if not HARNESS_OUTPUT_ROOT.exists():
        return out
    for run_output in HARNESS_OUTPUT_ROOT.glob("*/harness_run_*"):
        if not run_output.is_dir():
            continue
        parent = run_output.parent.name
        key = parent.replace("-", "_").lower()
        out[key] = run_output
        out[parent] = run_output
    return out


def _index_harness_outputs() -> dict[str, dict]:
    """Latest harness_outputs.jsonl row per trace_id."""
    index: dict[str, dict] = {}
    for row in _load_jsonl(HARNESS_OUTPUTS_JSONL):
        tid = row.get("trace_id")
        if tid:
            index[tid] = row
    return index


def _enrich_from_harness_outputs(agent: dict, ho_index: dict[str, dict]) -> None:
    tid = agent.get("trace_id")
    row = ho_index.get(tid) if tid else None
    if not row:
        return
    if row.get("prompt"):
        agent["prompt"] = row["prompt"]
        agent["prompt_source"] = "harness_outputs.jsonl"
    if row.get("output"):
        agent["output"] = row["output"]
        agent["output_source"] = "harness_outputs.jsonl"
    if row.get("thinking"):
        agent["thinking"] = row["thinking"]
        agent["thinking_source"] = "harness_outputs.jsonl"
    u = row.get("usage") or {}
    if u.get("reasoning_tokens"):
        agent["reasoning_tokens"] = u["reasoning_tokens"]
    tc = row.get("tool_calls") or []
    if tc:
        names = []
        for t in tc:
            if isinstance(t, str):
                names.append(t)
            elif isinstance(t, dict):
                names.append(t.get("type") or t.get("query") or "tool")
        agent["tool_calls"] = names


def _enrich_from_run_artifacts(agent: dict, run_output_dir: Path | None) -> None:
    if not run_output_dir:
        return
    stage = agent.get("stage_name", "")

    if stage == "advisor_directions" and not agent.get("output"):
        path = run_output_dir / "advisor_directions.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                body = {k: v for k, v in data.items() if k != "_usage"}
                agent["output"] = json.dumps(body, ensure_ascii=False, indent=2)
                agent["output_source"] = "advisor_directions.json"
            except (json.JSONDecodeError, OSError):
                pass

    if stage == "lit_search" and not agent.get("output"):
        lit_path = run_output_dir / "literature_research.jsonl"
        if lit_path.exists():
            papers = _load_jsonl(lit_path)
            if papers:
                summary = {
                    "paper_count": len(papers),
                    "papers": [
                        {
                            "title": p.get("title"),
                            "url": p.get("url"),
                            "status": p.get("error") or ("ok" if p.get("overall_summary") else "pending"),
                        }
                        for p in papers[:20]
                    ],
                }
                agent["output"] = json.dumps(summary, ensure_ascii=False, indent=2)
                agent["output_source"] = "literature_research.jsonl"

    if stage.startswith("lit_read_") and not agent.get("output"):
        aid = stage.removeprefix("lit_read_")
        lit_path = run_output_dir / "literature_research.jsonl"
        for row in _load_jsonl(lit_path):
            if row.get("arxiv_id") == aid or aid in (row.get("url") or ""):
                agent["output"] = json.dumps(row, ensure_ascii=False, indent=2)
                agent["output_source"] = "literature_research.jsonl"
                break


def _finalize_agent_text_fields(agent: dict) -> None:
    """Tag output source; prefer richest available text."""
    if agent.get("output"):
        agent.setdefault("output_source", "proxy")
    if agent.get("prompt"):
        agent.setdefault("prompt_source", "proxy")
    if agent.get("thinking"):
        agent.setdefault("thinking_source", "proxy")


def _index_conversation(conv_path: Path) -> dict[str, list]:
    """Index conversation entries by stage_name for enrichment."""
    by_stage: dict[str, list[dict]] = defaultdict(list)
    for row in _load_jsonl(conv_path):
        t = row.get("type", "")
        stage = None
        if t == "advisor":
            stage = f"advisor_r{row.get('round', '?')}"
        elif t == "solver":
            stage = f"solver_r{row.get('round', '?')}_{row.get('task_id', '?')}"
        elif t in ("writeup_prompt", "writeup_response"):
            tid = row.get("task_id", "")
            stage = f"writeup_{tid}" if t == "writeup_response" else None
            if t == "writeup_response":
                by_stage[f"writeup_{tid}"].append(row)
            elif t == "writeup_prompt":
                by_stage.setdefault(f"writeup_{tid}_prompt", []).append(row)
            continue
        elif t == "verify":
            tid = row.get("task_id", "?")
            rnd = row.get("round", 0)
            stage = f"verify_{tid}_round_{rnd}" if not row.get("is_final") else f"verify_{tid}_final"
        elif t == "refine":
            stage = f"refine_{row.get('task_id', '?')}_round_{row.get('round', 0)}"
        elif t == "assembly_advisor":
            stage = f"assembly_advisor_{row.get('round', '?')}"
        elif t in ("finalize_polish", "finalize_typeset"):
            stage = t
        elif t == "minor_polish":
            stage = f"minor_polish_{row.get('task_id', '?')}_{row.get('stage_tag', '')}"
        if stage:
            by_stage[stage].append(row)
    return by_stage


def _enrich_from_conversation(agent: dict, conv_index: dict[str, list]) -> None:
    stage = agent.get("stage_name", "")
    entries = conv_index.get(stage, [])
    if not entries:
        # writeup: merge prompt + response entries
        if stage.startswith("writeup_"):
            prompts = conv_index.get(f"{stage}_prompt", [])
            responses = conv_index.get(stage, [])
            if prompts:
                agent["prompt"] = prompts[-1].get("prompt")
                agent["prompt_source"] = "conversation.jsonl"
                refs = prompts[-1].get("reference_task_ids") or []
                if refs:
                    agent["received_from"] = refs
            if responses:
                agent["output"] = responses[-1].get("response")
                agent["output_source"] = "conversation.jsonl"
                u = responses[-1].get("usage") or {}
                if u.get("reasoning_tokens"):
                    agent["reasoning_tokens"] = u["reasoning_tokens"]
        return

    entry = entries[-1]
    if stage.startswith("writeup_"):
        prompts = conv_index.get(f"{stage}_prompt", [])
        if prompts:
            agent["prompt"] = prompts[-1].get("prompt")
            agent["prompt_source"] = "conversation.jsonl"
            refs = prompts[-1].get("reference_task_ids") or []
            if refs:
                agent["received_from"] = refs
    if entry.get("prompt") and not agent.get("prompt"):
        agent["prompt"] = entry["prompt"]
        agent["prompt_source"] = "conversation.jsonl"
    if entry.get("response"):
        agent["output"] = entry["response"]
        agent["output_source"] = "conversation.jsonl"
        resp = entry["response"]
        if "<ADVISOR_PLAN>" in resp:
            agent["thinking"] = resp.split("<ADVISOR_PLAN>")[0].strip()
            agent["output"] = resp  # full output includes plan
        elif "<SOLVER_REPORT>" in resp:
            agent["thinking"] = resp.split("<SOLVER_REPORT>")[0].strip()
    u = entry.get("usage") or {}
    if u.get("reasoning_tokens"):
        agent["reasoning_tokens"] = u["reasoning_tokens"]

    if entry.get("type") == "advisor":
        plan = parse_advisor_decision(entry.get("response", ""))
        if plan:
            agent["decision_impact"] = {
                "action": plan.get("action"),
                "strategic_note": plan.get("strategic_note"),
                "frontier": (plan.get("kb_updates") or {}).get("frontier"),
                "task_count": len(plan.get("task_assignments") or []),
                "writeup_count": len(plan.get("writeup_tasks") or []),
                "tasks": [
                    {"id": t.get("task_id"), "description": t.get("description"),
                     "refs": t.get("reference_task_ids") or []}
                    for t in (plan.get("task_assignments") or [])
                ],
                "writeups": [
                    {"statement": (w.get("statement") or "")[:120],
                     "solves_original": w.get("solves_original_problem"),
                     "refs": w.get("reference_task_ids") or []}
                    for w in (plan.get("writeup_tasks") or [])
                ],
            }
            sent_to = []
            r = entry.get("round", 1)
            for t in plan.get("task_assignments") or []:
                sent_to.append(f"solver_r{r}_{t.get('task_id')}")
            for w in plan.get("writeup_tasks") or []:
                sent_to.append("writeup (background)")
            agent["sent_to"] = sent_to

    if entry.get("type") == "writeup_prompt":
        refs = entry.get("reference_task_ids") or []
        if refs:
            agent["received_from"] = refs
    if entry.get("type") == "solver":
        agent.setdefault("received_from", [])  # from advisor plan refs - filled later
    if entry.get("type") == "verify":
        agent["decision_impact"] = {
            "verdict": entry.get("correct"),
            "round": entry.get("round"),
        }


def _build_agent_row(call: dict, harness_meta: dict | None = None) -> dict:
    run_id, stage = split_trace_id(call.get("trace_id", ""))
    parsed = parse_stage_name(stage or call.get("caller", ""))
    hm = harness_meta or call.get("harness") or {}

    req = call.get("request_messages") or []
    resp_parts = extract_response_parts(call.get("response_message"))

    inp = (call.get("input_tokens") or 0) + (call.get("cache_read_tokens") or 0) + (call.get("cache_write_tokens") or 0)
    out = call.get("output_tokens") or 0

    tools = hm.get("tool_calls") or call.get("tool_calls") or []
    if call.get("tool_called"):
        if not any(t == call["tool_called"] or (isinstance(t, dict) and t.get("type") == call["tool_called"]) for t in tools):
            tools = list(tools) + [call["tool_called"]]
    tool_names = []
    for t in tools:
        if isinstance(t, str):
            tool_names.append(t)
        elif isinstance(t, dict):
            tool_names.append(t.get("function", {}).get("name") or t.get("type") or "tool")

    agent = {
        "trace_id": call.get("trace_id"),
        "trace_name": call.get("trace_name", run_id),
        "run_id": run_id,
        "stage_name": stage or call.get("caller"),
        "call_seq": call.get("round", 1),
        "round_id": hm.get("round_id") or parsed.get("round_id") or parsed.get("verify_round"),
        "agent_id": hm.get("agent_id") or parsed.get("agent_id"),
        "role": hm.get("role") or parsed.get("role"),
        "pipeline_stage": parsed.get("pipeline_stage"),
        "model": call.get("model"),
        "ts": call.get("ts"),
        "latency_s": call.get("latency_s"),
        "input_tokens": call.get("input_tokens", 0),
        "output_tokens": out,
        "cache_read_tokens": call.get("cache_read_tokens", 0),
        "cache_write_tokens": call.get("cache_write_tokens", 0),
        "total_input_tokens": inp,
        "cost_usd": call.get("cost_usd"),
        "received_from": hm.get("received_from") or [],
        "sent_to": hm.get("sent_to") or [],
        "tool_calls": tool_names,
        "final_used_span": hm.get("final_used_span"),
        "decision_impact": hm.get("decision_impact"),
        "prompt": extract_prompt_text(req) if req else None,
        "thinking": resp_parts.get("thinking") or hm.get("thinking"),
        "output": resp_parts.get("output") or hm.get("output"),
        "stop_reason": call.get("stop_reason"),
    }
    return agent


def build_run(
    run_id: str,
    calls: list[dict],
    conv_index: dict[str, list],
    ho_index: dict[str, dict],
    run_output_dir: Path | None,
) -> dict:
    agents = []
    for call in calls:
        if call.get("kind") != "call":
            continue
        cid, _ = split_trace_id(call.get("trace_id", ""))
        if cid != run_id:
            continue
        agent = _build_agent_row(call)
        _enrich_from_harness_outputs(agent, ho_index)
        _enrich_from_conversation(agent, conv_index)
        _enrich_from_run_artifacts(agent, run_output_dir)
        _finalize_agent_text_fields(agent)
        agents.append(agent)

    agents.sort(key=lambda a: (a.get("ts") or "", a.get("stage_name") or ""))

    # Keep latest call per trace_id (retries create duplicate rows in calls.jsonl).
    deduped: dict[str, dict] = {}
    for a in agents:
        deduped[a["trace_id"]] = a
    agents = list(deduped.values())
    agents.sort(key=lambda a: (a.get("ts") or "", a.get("stage_name") or ""))

    # Infer received_from for solvers from advisor decisions
    advisor_plans: dict[int, dict] = {}
    for a in agents:
        if a.get("role") == "orchestrator_advisor" and a.get("decision_impact"):
            advisor_plans[a.get("round_id") or 0] = a["decision_impact"]
    for a in agents:
        if a.get("role") == "solver" and not a.get("received_from"):
            di = advisor_plans.get(a.get("round_id") or 0) or {}
            for t in di.get("tasks") or []:
                if t.get("id") == a.get("agent_id"):
                    a["received_from"] = t.get("refs") or [f"advisor_r{a.get('round_id')}"]
                    a["decision_impact"] = {"task_description": t.get("description")}
                    break

    # Build edges
    edges = []
    for a in agents:
        if a.get("sent_to"):
            for to in a["sent_to"]:
                edges.append({"from": a["stage_name"], "to": to, "type": "dispatch"})
        if a.get("role") == "writeup":
            edges.append({"from": a["stage_name"], "to": a["stage_name"].replace("writeup_", "verify_") + "_round_0", "type": "verify"})
        if a.get("role") == "verifier" and a.get("decision_impact", {}).get("verdict") == "false":
            edges.append({"from": a["stage_name"], "to": "refine", "type": "refine"})

    # Stage summary
    by_pipeline: dict[str, list] = defaultdict(list)
    for a in agents:
        ps = a.get("pipeline_stage") or "other"
        by_pipeline[ps].append(a)

    totals = {
        "agents": len(agents),
        "input_tokens": sum(a.get("total_input_tokens", 0) for a in agents),
        "output_tokens": sum(a.get("output_tokens", 0) for a in agents),
        "cost_usd": sum(a.get("cost_usd") or 0 for a in agents if a.get("cost_usd")),
        "latency_s": sum(a.get("latency_s") or 0 for a in agents),
    }

    trace_name = agents[0].get("trace_name", run_id) if agents else run_id

    run_data = {
        "run_id": run_id,
        "trace_name": trace_name,
        "pipeline": STAGE_PIPELINE,
        "agents": agents,
        "edges": edges,
        "stages_present": sorted(by_pipeline.keys(), key=lambda x: float(x) if x.replace(".", "").isdigit() else 99),
        "totals": totals,
        "built_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    from harness_dashboard.analysis import compute_run_analysis
    run_data["analysis"] = compute_run_analysis(
        agents, totals, run_output_dir=run_output_dir, pipeline=STAGE_PIPELINE, edges=edges
    )
    from harness_dashboard.latex_provenance import build_final_latex_bundle

    latex_bundle = build_final_latex_bundle(run_data)
    if latex_bundle:
        run_data["final_latex"] = latex_bundle
    return run_data


def rebuild_from_logs(log_dir: Path | None = None) -> list[dict]:
    """Parse previous harness stdout logs into dashboard cache entries."""
    from harness_dashboard.log_parser import build_all_from_logs

    log_dir = log_dir or PREVIOUS_LOG_DIR
    if not log_dir.is_absolute():
        log_dir = Path.cwd() / log_dir
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    entries = build_all_from_logs(log_dir)
    runs_meta: list[dict] = []
    for entry in entries:
        run_data = entry.pop("_run_data")
        run_id = run_data["run_id"]
        out_path = CACHE_DIR / f"{run_id}.json"
        out_path.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding="utf-8")
        entry.pop("file", None)
        entry["file"] = out_path.name
        runs_meta.append(entry)
        print(
            f"[harness_dashboard] Built log {run_id}: "
            f"{run_data['totals']['agents']} agents from {run_data.get('log_file')}"
        )
    return runs_meta


def rebuild(
    force: bool = False,
    log_dir: Path | None = None,
    artifact_root: Path | None = None,
    skip_logs: bool = False,
) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = CACHE_DIR / "manifest.json"
    runs_meta: list[dict] = []
    built_at = None

    if CALLS_JSONL.exists():
        calls = _load_jsonl(CALLS_JSONL)
        conv_files = _find_conversation_files()
        run_dirs = _find_run_output_dirs()
        ho_index = _index_harness_outputs()

        by_run: dict[str, list[dict]] = defaultdict(list)
        for call in calls:
            if call.get("kind") != "call":
                continue
            run_id, _ = split_trace_id(call.get("trace_id", ""))
            if run_id in ("untraced", "key-verify", "gpt55-test"):
                continue
            by_run[run_id].append(call)

        for run_id, run_calls in sorted(by_run.items()):
            conv_path = conv_files.get(run_id)
            if not conv_path:
                for k, p in conv_files.items():
                    if run_id.replace("_", "") in k.replace("_", "").lower():
                        conv_path = p
                        break
            conv_index = _index_conversation(conv_path) if conv_path else {}
            run_output_dir = run_dirs.get(run_id)
            if not run_output_dir:
                for k, p in run_dirs.items():
                    if run_id.replace("_", "") in k.replace("_", "").lower():
                        run_output_dir = p
                        break

            run_data = build_run(run_id, run_calls, conv_index, ho_index, run_output_dir)
            out_path = CACHE_DIR / f"{run_id}.json"
            out_path.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding="utf-8")
            runs_meta.append({
                "run_id": run_id,
                "trace_name": run_data["trace_name"],
                "source": "live",
                "agents": run_data["totals"]["agents"],
                "output_tokens": run_data["totals"]["output_tokens"],
                "cost_usd": run_data["totals"]["cost_usd"],
                "latency_s": run_data["totals"]["latency_s"],
                "stages_active": run_data.get("stages_present") or [],
                "last_ts": max((a.get("ts") or "") for a in run_data["agents"]) if run_data["agents"] else None,
                "file": out_path.name,
            })
            built_at = run_data.get("built_at")
            print(f"[harness_dashboard] Built {run_id}: {run_data['totals']['agents']} agents")
    else:
        print(f"[harness_dashboard] No calls file at {CALLS_JSONL}")

    if not skip_logs:
        log_runs = rebuild_from_logs(log_dir)
        existing_ids = {r["run_id"] for r in runs_meta}
        for lr in log_runs:
            if lr["run_id"] not in existing_ids:
                runs_meta.append(lr)
                built_at = lr.get("last_ts") or built_at

    if artifact_root:
        from harness_dashboard.artifact_builder import rebuild_from_artifacts

        art_root = artifact_root if artifact_root.is_absolute() else Path.cwd() / artifact_root
        art_runs = rebuild_from_artifacts(art_root, cache_dir=CACHE_DIR)
        existing_ids = {r["run_id"] for r in runs_meta}
        for ar in art_runs:
            if ar["run_id"] not in existing_ids:
                runs_meta.append(ar)
            else:
                runs_meta = [ar if r["run_id"] == ar["run_id"] else r for r in runs_meta]
            built_at = built_at or __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat()

    runs_meta.sort(key=lambda r: r.get("last_ts") or "", reverse=True)
    manifest = {"runs": runs_meta, "built_at": built_at}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[harness_dashboard] Wrote manifest ({len(runs_meta)} runs)")
    return manifest_path


def merge_manifest_entries(cache_dir: Path, new_entries: list[dict]) -> Path:
    """Merge new run entries into manifest.json without dropping other sources."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / "manifest.json"
    existing: list[dict] = []
    built_at = None
    if manifest_path.exists():
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing = raw.get("runs") or []
            built_at = raw.get("built_at")
        except (json.JSONDecodeError, OSError):
            existing = []
    by_id = {r["run_id"]: r for r in existing if r.get("run_id")}
    for entry in new_entries:
        if entry.get("run_id"):
            by_id[entry["run_id"]] = entry
    merged = sorted(by_id.values(), key=lambda r: r.get("last_ts") or "", reverse=True)
    if merged:
        built_at = merged[0].get("last_ts") or built_at
    manifest = {"runs": merged, "built_at": built_at}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[harness_dashboard] Merged manifest ({len(merged)} runs)")
    return manifest_path
