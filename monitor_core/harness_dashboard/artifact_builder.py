"""Build harness dashboard runs from on-disk harness artifacts (UCLA-style)."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_dashboard.build import (
    CACHE_DIR,
    _enrich_from_conversation,
    _enrich_from_run_artifacts,
    _finalize_agent_text_fields,
    _index_conversation,
    _load_jsonl,
)
from harness_dashboard.parse import STAGE_PIPELINE, extract_response_parts, parse_stage_name
from harness_dashboard.analysis import compute_run_analysis, summarize_run_for_manifest


def discover_harness_runs(root: Path) -> list[tuple[str, Path]]:
    """Return (run_id, harness_run_dir) pairs under root."""
    if not root.exists():
        return []
    runs: list[tuple[str, Path]] = []
    for prob_dir in sorted(root.iterdir()):
        if not prob_dir.is_dir():
            continue
        for run_dir in sorted(prob_dir.glob("harness_run_*")):
            if not run_dir.is_dir():
                continue
            prob_name = prob_dir.name
            run_id = prob_name.replace("-", "_").lower()
            runs.append((run_id, run_dir))
    return runs


def _trace_name_for_run(prob_dir: Path, run_id: str, output_root: Path | None = None) -> str:
    label = prob_dir.name.replace("_", " ")
    candidates = []
    if output_root:
        candidates.append(output_root / f"{prob_dir.name}.tex")
    candidates.append(Path.cwd() / "UCLA" / "Output" / f"{prob_dir.name}.tex")
    candidates.append(prob_dir.parent.parent / "Output" / f"{prob_dir.name}.tex")
    for tex in candidates:
        if not tex.exists():
            continue
        m = re.search(r"\\title\{([^}]+)", tex.read_text(encoding="utf-8", errors="replace"))
        if m:
            title = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", m.group(1))
            title = re.sub(r"\s+", " ", title).strip()
            if title:
                return f"[UCLA] {label}: {title[:80]}"
        break
    return f"[UCLA] {label}"


def _agent_from_usage_row(row: dict, run_id: str, trace_name: str, seq: int) -> dict:
    stage = row.get("stage", "")
    parsed = parse_stage_name(stage)
    inp = (row.get("input_tokens") or 0) + (row.get("cached_input_tokens") or 0)
    return {
        "trace_id": f"{run_id}::{stage}",
        "trace_name": trace_name,
        "run_id": run_id,
        "stage_name": stage,
        "call_seq": seq,
        "round_id": parsed.get("round_id") if parsed.get("round_id") is not None else parsed.get("verify_round"),
        "agent_id": parsed.get("agent_id"),
        "role": parsed.get("role"),
        "pipeline_stage": parsed.get("pipeline_stage"),
        "model": row.get("model"),
        "latency_s": row.get("elapsed_seconds"),
        "input_tokens": row.get("input_tokens", 0),
        "output_tokens": row.get("output_tokens", 0),
        "cache_read_tokens": row.get("cached_input_tokens", 0),
        "total_input_tokens": inp,
        "reasoning_tokens": row.get("reasoning_tokens"),
        "cost_usd": row.get("cost_usd"),
        "response_id": row.get("response_id"),
        "received_from": [],
        "sent_to": [],
        "tool_calls": [],
    }


def _index_api_responses(path: Path) -> dict[str, dict]:
    """Map response_id -> api_responses.jsonl row."""
    index: dict[str, dict] = {}
    for row in _load_jsonl(path):
        rid = row.get("response_id")
        if rid:
            index[rid] = row
    return index


def _response_message_from_api(response: Any) -> dict[str, Any]:
    """Normalize harness api_responses.response into extract_response_parts shape."""
    if not response or not isinstance(response, dict):
        return {}
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[Any] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            content = item.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in ("output_text", "text") and block.get("text"):
                        text_parts.append(block["text"])
            elif isinstance(content, str):
                text_parts.append(content)
        elif kind == "reasoning":
            summary = item.get("summary")
            if isinstance(summary, list):
                thinking_parts.extend(str(s) for s in summary)
            elif summary:
                thinking_parts.append(str(summary))
        elif kind == "web_search_call":
            action = item.get("action") or {}
            query = action.get("query", "") if isinstance(action, dict) else ""
            tool_calls.append({"type": "web_search", "query": query})
    msg: dict[str, Any] = {}
    if text_parts:
        msg["content"] = "\n".join(text_parts)
    if thinking_parts:
        msg["reasoning"] = "\n".join(thinking_parts)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if not msg.get("content"):
        for key in ("output_text", "text", "content"):
            val = response.get(key)
            if isinstance(val, str) and val.strip():
                msg["content"] = val
                break
    return msg


def _enrich_from_api_responses(agent: dict, api_index: dict[str, dict]) -> None:
    rid = agent.get("response_id")
    row = api_index.get(rid) if rid else None
    if not row:
        return
    if not agent.get("prompt") and row.get("prompt"):
        agent["prompt"] = row["prompt"]
        agent["prompt_source"] = "api_responses.jsonl"
    parts = extract_response_parts(_response_message_from_api(row.get("response")))
    if not agent.get("output") and parts.get("output"):
        agent["output"] = parts["output"]
        agent["output_source"] = "api_responses.jsonl"
    if not agent.get("thinking") and parts.get("thinking"):
        agent["thinking"] = parts["thinking"]
        agent["thinking_source"] = "api_responses.jsonl"
    if not agent.get("tool_calls") and parts.get("tool_calls"):
        names = []
        for t in parts["tool_calls"] or []:
            if isinstance(t, dict):
                names.append(t.get("type") or t.get("query") or "tool")
            else:
                names.append(str(t))
        if names:
            agent["tool_calls"] = names


def _enrich_benchmark(agent: dict, run_output_dir: Path) -> None:
    stage = agent.get("stage_name", "")
    if not stage.startswith("benchmark_strategy_") or agent.get("output"):
        return
    path = run_output_dir / "benchmark.json"
    if not path.exists():
        return
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(rows, list):
        return
    suffix = stage.removeprefix("benchmark_strategy_")
    for row in rows:
        idx = row.get("solution_idx") or row.get("plan_used", {}).get("title")
        if idx == suffix or suffix in str(idx):
            agent["output"] = json.dumps(row, ensure_ascii=False, indent=2)
            agent["output_source"] = "benchmark.json"
            break


def _finalize_run_data(
    run_id: str,
    agents: list[dict],
    trace_name: str,
    *,
    source: str,
    artifact_dir: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict:
    agents.sort(key=lambda a: (a.get("call_seq") or 0, a.get("stage_name") or ""))

    advisor_plans: dict[Any, dict] = {}
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

    edges = []
    for a in agents:
        if a.get("sent_to"):
            for to in a["sent_to"]:
                edges.append({"from": a["stage_name"], "to": to, "type": "dispatch"})
        if a.get("role") == "writeup":
            tid = a["stage_name"].removeprefix("writeup_")
            edges.append({"from": a["stage_name"], "to": f"verify_{tid}_round_0", "type": "verify"})
        if a.get("role") == "verifier" and a.get("decision_impact", {}).get("verdict") in (False, "false"):
            edges.append({"from": a["stage_name"], "to": "refine", "type": "refine"})

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

    data: dict[str, Any] = {
        "run_id": run_id,
        "trace_name": trace_name,
        "source": source,
        "pipeline": STAGE_PIPELINE,
        "agents": agents,
        "edges": edges,
        "stages_present": sorted(
            by_pipeline.keys(),
            key=lambda x: float(x) if str(x).replace(".", "").isdigit() else 99,
        ),
        "totals": totals,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    if artifact_dir:
        data["artifact_dir"] = artifact_dir
    if extra:
        data.update(extra)
    run_dir = Path(artifact_dir) if artifact_dir else None
    data["analysis"] = compute_run_analysis(
        agents, totals, run_output_dir=run_dir, pipeline=STAGE_PIPELINE, edges=edges
    )
    from harness_dashboard.latex_provenance import build_final_latex_bundle

    latex_bundle = build_final_latex_bundle(data)
    if latex_bundle:
        data["final_latex"] = latex_bundle
    return data


def build_run_from_artifacts(run_id: str, run_output_dir: Path, output_root: Path | None = None) -> dict:
    """Build one dashboard run from harness_run_* directory."""
    usage_path = run_output_dir / "Overall_Usage" / "usage.jsonl"
    conv_path = run_output_dir / "memory" / "conversation.jsonl"
    prob_dir = run_output_dir.parent
    trace_name = _trace_name_for_run(prob_dir, run_id, output_root)

    conv_index = _index_conversation(conv_path) if conv_path.exists() else {}
    api_index = _index_api_responses(run_output_dir / "memory" / "api_responses.jsonl")
    agents: list[dict] = []

    for seq, row in enumerate(_load_jsonl(usage_path), 1):
        agent = _agent_from_usage_row(row, run_id, trace_name, seq)
        _enrich_from_conversation(agent, conv_index)
        _enrich_from_api_responses(agent, api_index)
        _enrich_from_run_artifacts(agent, run_output_dir)
        _enrich_benchmark(agent, run_output_dir)
        _finalize_agent_text_fields(agent)
        agents.append(agent)

    from harness_dashboard.ucla_memory import attach_ucla_agent_memory, build_ucla_memory

    attach_ucla_agent_memory(agents, run_output_dir)

    extra: dict[str, Any] = {
        "prob_id": prob_dir.name,
        "memory": build_ucla_memory(run_output_dir),
    }
    status_path = prob_dir / "FINAL_STATUS.txt"
    if status_path.exists():
        extra["final_status"] = status_path.read_text(encoding="utf-8", errors="replace").strip()

    return _finalize_run_data(
        run_id,
        agents,
        trace_name,
        source="ucla_artifacts",
        artifact_dir=str(run_output_dir),
        extra=extra,
    )


def rebuild_from_artifacts(
    harness_root: Path,
    *,
    cache_dir: Path | None = None,
) -> list[dict]:
    """Discover and cache all harness runs under harness_root."""
    cache = cache_dir or CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    output_root = harness_root.parent.parent / "Output"
    if not output_root.exists():
        output_root = harness_root.parent / "Output"

    runs_meta: list[dict] = []
    for run_id, run_dir in discover_harness_runs(harness_root):
        run_data = build_run_from_artifacts(run_id, run_dir, output_root)
        out_path = cache / f"{run_id}.json"
        out_path.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding="utf-8")
        runs_meta.append({
            "run_id": run_id,
            "trace_name": run_data["trace_name"],
            "source": "ucla_artifacts",
            "prob_id": run_data.get("prob_id"),
            "agents": run_data["totals"]["agents"],
            "output_tokens": run_data["totals"]["output_tokens"],
            "cost_usd": run_data["totals"]["cost_usd"],
            "latency_s": run_data["totals"]["latency_s"],
            "stages_active": run_data.get("stages_present") or [],
            "last_ts": run_data.get("built_at"),
            "file": out_path.name,
            **summarize_run_for_manifest(run_data),
        })
        print(
            f"[harness_dashboard] Built UCLA {run_id}: "
            f"{run_data['totals']['agents']} agents, "
            f"${run_data['totals']['cost_usd']:.2f}"
        )
    return runs_meta
