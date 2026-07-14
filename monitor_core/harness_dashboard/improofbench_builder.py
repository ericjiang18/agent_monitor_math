"""Build harness dashboard runs from IMProofBench WorkflowRuns artifacts."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_dashboard.analysis import compute_run_analysis, summarize_run_for_manifest
from harness_dashboard.build import CACHE_DIR, _finalize_agent_text_fields, _load_jsonl
from harness_dashboard.parse import IMPROOF_PIPELINE, parse_improof_agent


def discover_improof_runs(root: Path) -> list[tuple[str, Path]]:
    """Return (run_id, workflow_run_dir) pairs under WorkflowRuns/."""
    if not root.exists():
        return []
    runs: list[tuple[str, Path]] = []
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        events = run_dir / "events.jsonl"
        if not events.exists():
            continue
        meta_path = run_dir / "run-metadata.json"
        prob_id = run_dir.name
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                prob_id = (meta.get("config_snapshot") or {}).get("problem_id") or meta.get("display_name") or prob_id
            except (json.JSONDecodeError, OSError):
                pass
        run_id = prob_id.replace("-", "_").lower()
        if not run_id.startswith("prob"):
            run_id = f"improof_{run_id}"
        else:
            run_id = f"improof_{run_id}"
        runs.append((run_id, run_dir))
    return runs


def _trace_name_for_run(run_dir: Path, prob_id: str, output_root: Path | None = None) -> str:
    label = prob_id
    meta_path = run_dir / "run-metadata.json"
    display = label
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            display = meta.get("display_name") or (meta.get("config_snapshot") or {}).get("display_name") or label
        except (json.JSONDecodeError, OSError):
            pass

    candidates = []
    if output_root:
        candidates.append(output_root / f"{prob_id}.tex")
    candidates.append(Path.cwd() / "IMProofBench" / "Output" / f"{prob_id}.tex")
    candidates.append(run_dir.parent.parent / "Output" / f"{prob_id}.tex")
    for tex in candidates:
        if not tex.exists():
            continue
        text = tex.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"\\title\{([^}]+)", text)
        if not m:
            m = re.search(r"\\begin\{center\}\s*\{?\\bf\s*([^}\\]+)", text)
        if m:
            title = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", m.group(1))
            title = re.sub(r"\s+", " ", title).strip()
            if title:
                return f"[IMProofBench] {label}: {title[:80]}"
        break
    return f"[IMProofBench] {display}"


def _index_agent_folders(run_dir: Path) -> dict[str, Path]:
    """Map agent.start call_id -> agents/{folder} path."""
    index: dict[str, Path] = {}
    agents_dir = run_dir / "agents"
    if not agents_dir.is_dir():
        return index
    for folder in agents_dir.iterdir():
        if not folder.is_dir():
            continue
        parts = folder.name.split("-")
        if len(parts) >= 3:
            index[parts[-1]] = folder
    return index


TEXT_LIMIT = 80_000
MEMORY_INPUT_KEYS = (
    "problem",
    "prev_critique",
    "workflow_feedback",
    "prev_council",
    "prev_compute_response",
    "author_question",
    "instructions",
    "answer_tex",
    "research_notes_tex",
    "references_bib",
    "council_question",
    "compute_instructions",
)
MEMORY_META_KEYS = ("round", "n_rounds", "page_limit", "mode", "budget_used_usd", "budget_max_usd")


def _clip_text(text: str | None, limit: int = TEXT_LIMIT) -> str | None:
    if not text:
        return None
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n\n...[truncated at {limit} chars]"


class ImproofEventIndex:
    """Index agent.start/end payloads and resolve events_blobs $ref files."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.blob_cache: dict[str, str] = {}
        self.agent_starts: dict[str, dict[str, Any]] = {}
        self.agent_ends: dict[str, dict[str, Any]] = {}
        self._load()

    def resolve(self, value: Any, *, limit: int = TEXT_LIMIT) -> Any:
        if isinstance(value, dict):
            if "$ref" in value:
                ref = str(value["$ref"])
                if ref in self.blob_cache:
                    return self.blob_cache[ref]
                path = self.run_dir / ref
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
                text = _clip_text(text, limit) or ""
                self.blob_cache[ref] = text
                return text
            return {k: self.resolve(v, limit=limit) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v, limit=limit) for v in value]
        if isinstance(value, str):
            return _clip_text(value, limit)
        return value

    def _load(self) -> None:
        events_path = self.run_dir / "events.jsonl"
        if not events_path.exists():
            return
        for row in _load_jsonl(events_path):
            kind = row.get("kind")
            call_id = row.get("call_id") or ""
            if not call_id:
                continue
            payload = row.get("payload") or {}
            if kind == "agent.start":
                self.agent_starts[call_id] = {
                    "agent": row.get("agent"),
                    "input": self.resolve(payload.get("input")),
                    "ts": row.get("ts"),
                }
            elif kind == "agent.end":
                self.agent_ends[call_id] = {
                    "agent": row.get("agent"),
                    "output": self.resolve(payload.get("output")),
                    "ts": row.get("ts"),
                }


def _prompt_from_messages(messages: list[dict] | None) -> str | None:
    if not messages:
        return None
    chunks: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            chunks.append(f"[{role}]\n{content.strip()}")
    return "\n\n".join(chunks) if chunks else None


def _format_input_prompt(agent_name: str, inp: dict[str, Any] | None) -> str | None:
    if not isinstance(inp, dict):
        return None
    sections: list[str] = []
    meta_bits = []
    for key in MEMORY_META_KEYS:
        if inp.get(key) is not None:
            meta_bits.append(f"{key}={inp[key]}")
    if meta_bits:
        sections.append("### run context ###\n" + ", ".join(meta_bits))
    for key in MEMORY_INPUT_KEYS:
        val = inp.get(key)
        if val and str(val).strip():
            sections.append(f"### {key} ###\n{val}")
    return "\n\n".join(sections) if sections else None


def _format_memory_context(inp: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(inp, dict):
        return None
    ctx: dict[str, str] = {}
    for key in ("prev_critique", "workflow_feedback", "prev_council", "prev_compute_response"):
        val = inp.get(key)
        if val and str(val).strip():
            ctx[key] = _clip_text(str(val)) or ""
    for key in ("answer_tex", "research_notes_tex"):
        val = inp.get(key)
        if val and str(val).strip():
            ctx[key] = _clip_text(str(val), 12_000) or ""
    return ctx or None


def _agent_label(agent: dict) -> str:
    name = agent.get("improof_agent") or ""
    if name:
        return name
    role = agent.get("role") or ""
    return {
        "author": "Author",
        "critic": "ACCritic",
        "council_member": "CouncilMember",
        "compute": "Compute",
        "council": "Council",
        "workflow": "ACWorkflow",
    }.get(role, role)


def _format_output_payload(agent_name: str, out: dict[str, Any] | None) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(out, dict):
        return None, None
    name = agent_name
    if name not in ("Author", "ACCritic", "CouncilMember", "Compute", "Council"):
        name = {
            "author": "Author",
            "critic": "ACCritic",
            "council_member": "CouncilMember",
            "compute": "Compute",
        }.get(agent_name, agent_name)
    decision: dict[str, Any] | None = None
    parts: list[str] = []

    if name == "Author":
        if out.get("thinking_summary"):
            parts.append(str(out["thinking_summary"]))
        if out.get("raw_text"):
            parts.append(str(out["raw_text"]))
        if out.get("answer_tex"):
            parts.append("[answer.tex]\n" + str(out["answer_tex"]))
        if out.get("research_notes_tex"):
            parts.append("[research_notes.tex]\n" + str(out["research_notes_tex"]))
        if out.get("ready") is not None:
            decision = {"ready": bool(out["ready"])}
    elif name == "ACCritic":
        if out.get("review_md"):
            parts.append(str(out["review_md"]))
        msgs = out.get("messages_after") or []
        if isinstance(msgs, list):
            for msg in msgs:
                if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("content"):
                    parts.append(str(msg["content"]))
        answer_ready = out.get("answer_ready")
        if answer_ready is not None:
            decision = {
                "verdict": bool(answer_ready),
                "answer_ready": bool(answer_ready),
                "mode": out.get("mode"),
            }
    elif name == "CouncilMember":
        if out.get("text"):
            parts.append(str(out["text"]))
    elif name == "Compute":
        if out.get("summary"):
            parts.append("[summary]\n" + str(out["summary"]))
        if out.get("response_md"):
            parts.append(str(out["response_md"]))
        if out.get("status"):
            decision = {"status": out.get("status"), "workspace": out.get("workspace")}
    elif name == "Council":
        if out.get("summary"):
            parts.append(str(out["summary"]))
    else:
        for key in ("review_md", "raw_text", "thinking_summary", "text", "response_md"):
            if out.get(key):
                parts.append(str(out[key]))

    text = _clip_text("\n\n".join(p for p in parts if p)) if parts else None
    return text, decision


def _build_workspace_memory(run_dir: Path) -> dict[str, Any]:
    """Summarize AC workspace snapshots (round artifacts + resume state)."""
    memory: dict[str, Any] = {"rounds": [], "resume_state": None, "workspace_paths": []}
    for ws in sorted(run_dir.glob("ac_workspaces/*")):
        if not ws.is_dir():
            continue
        memory["workspace_paths"].append(str(ws))
        ac_dir = ws / ".ac"
        resume_path = ac_dir / "resume-state.json"
        if resume_path.exists() and memory["resume_state"] is None:
            try:
                memory["resume_state"] = json.loads(resume_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        for round_dir in sorted(ac_dir.glob("round-*")):
            try:
                rnd = int(round_dir.name.split("-", 1)[1])
            except (IndexError, ValueError):
                continue
            entry: dict[str, Any] = {"round": rnd, "files": {}}
            for fname in (
                "answer.tex",
                "research_notes.tex",
                "review.md",
                "author_outputs.json",
                "review_outputs.json",
                "forced_fresh_review.md",
            ):
                path = round_dir / fname
                if not path.exists():
                    continue
                try:
                    raw = path.read_text(encoding="utf-8", errors="replace")
                    if fname.endswith(".json"):
                        entry["files"][fname] = json.loads(raw)
                    else:
                        entry["files"][fname] = _clip_text(raw, 16_000)
                except (json.JSONDecodeError, OSError):
                    continue
            if entry["files"]:
                memory["rounds"].append(entry)
    memory["rounds"].sort(key=lambda x: x.get("round", 0))
    return memory


def _enrich_from_agent_folder(agent: dict, folder: Path | None) -> None:
    if not folder or not folder.is_dir():
        return
    messages_path = folder / "messages.json"
    if messages_path.exists() and not agent.get("prompt"):
        try:
            messages = json.loads(messages_path.read_text(encoding="utf-8"))
            prompt = _prompt_from_messages(messages if isinstance(messages, list) else None)
            if prompt:
                agent["prompt"] = _clip_text(prompt)
                agent["prompt_source"] = str(messages_path.relative_to(folder.parent.parent))
        except (json.JSONDecodeError, OSError):
            pass

    input_path = folder / "input.json"
    if input_path.exists() and not agent.get("memory_context"):
        try:
            inp = json.loads(input_path.read_text(encoding="utf-8"))
            ctx = _format_memory_context(inp if isinstance(inp, dict) else None)
            if ctx:
                agent["memory_context"] = ctx
                agent["memory_source"] = "agents/input.json"
            if not agent.get("prompt"):
                prompt = _format_input_prompt(_agent_label(agent), inp if isinstance(inp, dict) else None)
                if prompt:
                    agent["prompt"] = prompt
                    agent["prompt_source"] = "agents/input.json"
        except (json.JSONDecodeError, OSError):
            pass

    output_path = folder / "output.json"
    if output_path.exists():
        try:
            out_data = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            out_data = {}
        if isinstance(out_data, dict):
            text, decision = _format_output_payload(_agent_label(agent), out_data)
            if text and not agent.get("output"):
                agent["output"] = text
                agent["output_source"] = "agents/output.json"
            if decision and not agent.get("decision_impact"):
                agent["decision_impact"] = decision

    raw_path = folder / "raw_response.txt"
    if raw_path.exists() and not agent.get("output"):
        try:
            agent["output"] = _clip_text(raw_path.read_text(encoding="utf-8", errors="replace"))
            agent["output_source"] = "agents/raw_response.txt"
        except OSError:
            pass


def _enrich_from_events(agent: dict, parent_call_id: str, event_index: ImproofEventIndex) -> None:
    start = event_index.agent_starts.get(parent_call_id) or {}
    end = event_index.agent_ends.get(parent_call_id) or {}
    agent_name = agent.get("improof_agent") or start.get("agent") or end.get("agent") or ""

    inp = start.get("input")
    if isinstance(inp, dict):
        if not agent.get("memory_context"):
            ctx = _format_memory_context(inp)
            if ctx:
                agent["memory_context"] = ctx
                agent["memory_source"] = "events.jsonl:agent.start"
        if not agent.get("prompt"):
            prompt = _format_input_prompt(agent_name, inp)
            if prompt:
                agent["prompt"] = prompt
                agent["prompt_source"] = "events.jsonl:agent.start"

    out = end.get("output")
    if isinstance(out, dict):
        text, decision = _format_output_payload(agent_name, out)
        if text and not agent.get("output"):
            agent["output"] = text
            agent["output_source"] = "events.jsonl:agent.end"
        if decision and not agent.get("decision_impact"):
            agent["decision_impact"] = decision
        if not agent.get("thinking"):
            if agent_name == "Author" and out.get("thinking_summary"):
                agent["thinking"] = _clip_text(str(out["thinking_summary"]))
                agent["thinking_source"] = "events.jsonl:agent.end"


def _enrich_agent(agent: dict, folder: Path | None, parent_call_id: str, event_index: ImproofEventIndex) -> None:
    _enrich_from_agent_folder(agent, folder)
    _enrich_from_events(agent, parent_call_id, event_index)
    _finalize_agent_text_fields(agent)


def _agent_from_model_call(
    event: dict,
    *,
    run_id: str,
    trace_name: str,
    seq: int,
    round_id: int | None,
    folder: Path | None,
) -> dict:
    agent_name = event.get("agent") or "unknown"
    parent_id = event.get("parent_call_id") or ""
    folder_name = folder.name if folder else None
    if not folder_name and round_id is not None and parent_id:
        folder_name = f"{agent_name}-r{round_id}-{parent_id}"
    parsed = parse_improof_agent(agent_name, folder_name=folder_name, round_id=round_id)
    payload = event.get("payload") or {}
    inp = int(payload.get("in_tokens") or 0)
    return {
        "trace_id": f"{run_id}::{parsed['stage_name']}",
        "trace_name": trace_name,
        "run_id": run_id,
        "stage_name": parsed["stage_name"],
        "call_seq": seq,
        "round_id": parsed.get("round_id") if parsed.get("round_id") is not None else round_id,
        "agent_id": parsed.get("agent_id"),
        "role": parsed.get("role"),
        "pipeline_stage": parsed.get("pipeline_stage"),
        "model": payload.get("model"),
        "latency_s": payload.get("duration_s"),
        "input_tokens": inp,
        "output_tokens": int(payload.get("out_tokens") or 0),
        "cache_read_tokens": 0,
        "total_input_tokens": inp,
        "reasoning_tokens": payload.get("reasoning_tokens"),
        "cost_usd": payload.get("cost_usd"),
        "response_id": parent_id or event.get("call_id"),
        "ts": event.get("ts"),
        "received_from": [],
        "sent_to": [],
        "tool_calls": [],
        "improof_agent": agent_name,
        "improof_via": payload.get("via"),
    }


def _build_ac_edges(agents: list[dict]) -> list[dict]:
    """Author → Critic per round; failed critic → next author."""
    edges: list[dict] = []
    by_round: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for a in agents:
        rnd = a.get("round_id")
        if rnd is None:
            continue
        role = a.get("role") or ""
        by_round[int(rnd)][role].append(a)

    rounds = sorted(by_round.keys())
    for rnd in rounds:
        authors = by_round[rnd].get("author") or []
        critics = by_round[rnd].get("critic") or []
        if authors and critics:
            author = authors[-1]
            for critic in critics:
                edges.append({
                    "from": author["stage_name"],
                    "to": critic["stage_name"],
                    "type": "verify",
                })
            for i in range(1, len(critics)):
                edges.append({
                    "from": critics[i - 1]["stage_name"],
                    "to": critics[i]["stage_name"],
                    "type": "reverify",
                })
        for cm in by_round[rnd].get("council_member") or []:
            if authors:
                edges.append({"from": authors[-1]["stage_name"], "to": cm["stage_name"], "type": "dispatch"})
            if critics:
                edges.append({"from": cm["stage_name"], "to": critics[-1]["stage_name"], "type": "input"})
        for comp in by_round[rnd].get("compute") or []:
            if authors:
                edges.append({"from": authors[-1]["stage_name"], "to": comp["stage_name"], "type": "dispatch"})

        critic = critics[-1] if critics else None
        if critic and rnd + 1 in by_round:
            di = critic.get("decision_impact") or {}
            if di.get("answer_ready") is False or di.get("verdict") is False:
                nxt_authors = by_round[rnd + 1].get("author") or []
                if nxt_authors:
                    edges.append({
                        "from": critic["stage_name"],
                        "to": nxt_authors[0]["stage_name"],
                        "type": "refine",
                    })
    return edges


def build_run_from_improof_artifacts(run_id: str, run_dir: Path, output_root: Path | None = None) -> dict | None:
    """Build one dashboard run from a WorkflowRuns/* directory."""
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return None

    meta: dict[str, Any] = {}
    meta_path = run_dir / "run-metadata.json"
    prob_id = run_id.replace("improof_", "").replace("_", "-")
    if not prob_id.startswith("prob"):
        prob_id = re.sub(r"^improof_", "prob-", run_id.replace("_", "-"))
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            prob_id = (meta.get("config_snapshot") or {}).get("problem_id") or prob_id
        except (json.JSONDecodeError, OSError):
            meta = {}

    trace_name = _trace_name_for_run(run_dir, prob_id, output_root)
    folder_index = _index_agent_folders(run_dir)
    event_index = ImproofEventIndex(run_dir)

    agents: list[dict] = []
    seq = 0
    current_round: int | None = 0
    last_ts: str | None = None
    agent_round: dict[str, int] = {}

    for row in _load_jsonl(events_path):
        kind = row.get("kind")
        if kind == "ac.round_start":
            payload = row.get("payload") or {}
            if payload.get("round") is not None:
                current_round = int(payload["round"])
            continue
        if kind == "agent.start":
            call_id = row.get("call_id") or ""
            agent_name = row.get("agent") or ""
            if call_id and agent_name in ("Author", "ACCritic", "CouncilMember", "Compute", "Council"):
                if current_round is not None:
                    agent_round[call_id] = current_round
            continue
        if kind != "model.call":
            continue
        parent_id = row.get("parent_call_id") or ""
        folder = folder_index.get(parent_id)
        round_id = agent_round.get(parent_id, current_round)
        seq += 1
        agent = _agent_from_model_call(
            row,
            run_id=run_id,
            trace_name=trace_name,
            seq=seq,
            round_id=round_id,
            folder=folder,
        )
        _enrich_agent(agent, folder, parent_id, event_index)
        agents.append(agent)
        last_ts = row.get("ts") or last_ts

    if not agents:
        return None

    edges = _build_ac_edges(agents)

    by_pipeline: dict[str, list] = defaultdict(list)
    for a in agents:
        by_pipeline[a.get("pipeline_stage") or "other"].append(a)

    totals = {
        "agents": len(agents),
        "input_tokens": sum(a.get("total_input_tokens", 0) for a in agents),
        "output_tokens": sum(a.get("output_tokens", 0) for a in agents),
        "cost_usd": sum(a.get("cost_usd") or 0 for a in agents if a.get("cost_usd")),
        "latency_s": sum(a.get("latency_s") or 0 for a in agents),
    }

    outputs = meta.get("outputs") or {}
    workspace_memory = _build_workspace_memory(run_dir)
    extra: dict[str, Any] = {
        "prob_id": prob_id,
        "workflow_run_id": meta.get("run_id") or run_dir.name,
        "status": meta.get("status"),
        "memory": workspace_memory,
        "outputs": {
            "rounds_completed": outputs.get("rounds_completed"),
            "early_stopped": outputs.get("early_stopped"),
            "last_critic_accepted": outputs.get("last_critic_accepted"),
            "final_critic_answer_ready": outputs.get("final_critic_answer_ready"),
            "compiled": outputs.get("compiled"),
            "pages": outputs.get("pages"),
        },
        "rounds_completed": outputs.get("rounds_completed"),
    }

    pipeline_order = [s["id"] for s in IMPROOF_PIPELINE]
    data: dict[str, Any] = {
        "run_id": run_id,
        "trace_name": trace_name,
        "source": "improofbench_artifacts",
        "pipeline": IMPROOF_PIPELINE,
        "agents": agents,
        "edges": edges,
        "stages_present": [sid for sid in pipeline_order if sid in by_pipeline],
        "totals": totals,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "artifact_dir": str(run_dir),
        **extra,
    }
    data["analysis"] = compute_run_analysis(
        agents, totals, run_output_dir=run_dir, pipeline=IMPROOF_PIPELINE, edges=edges
    )
    from harness_dashboard.latex_provenance import build_final_latex_bundle

    latex_bundle = build_final_latex_bundle(data)
    if latex_bundle:
        data["final_latex"] = latex_bundle
    if last_ts:
        data["last_ts"] = last_ts
    return data


def rebuild_from_improof_artifacts(
    workflow_root: Path,
    *,
    cache_dir: Path | None = None,
) -> list[dict]:
    """Discover and cache all IMProofBench runs under workflow_root."""
    cache = cache_dir or CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    output_root = workflow_root.parent.parent / "Output"
    if not output_root.exists():
        output_root = workflow_root.parent / "Output"

    runs_meta: list[dict] = []
    for run_id, run_dir in discover_improof_runs(workflow_root):
        run_data = build_run_from_improof_artifacts(run_id, run_dir, output_root)
        if not run_data:
            print(f"[harness_dashboard] Skip IMProofBench {run_id}: no model.call events")
            continue
        out_path = cache / f"{run_id}.json"
        out_path.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding="utf-8")
        entry = {
            "run_id": run_id,
            "trace_name": run_data["trace_name"],
            "source": "improofbench_artifacts",
            "prob_id": run_data.get("prob_id"),
            "agents": run_data["totals"]["agents"],
            "output_tokens": run_data["totals"]["output_tokens"],
            "cost_usd": run_data["totals"]["cost_usd"],
            "latency_s": run_data["totals"]["latency_s"],
            "stages_active": run_data.get("stages_present") or [],
            "last_ts": run_data.get("last_ts") or run_data.get("built_at"),
            "file": out_path.name,
            **summarize_run_for_manifest(run_data),
        }
        runs_meta.append(entry)
        print(
            f"[harness_dashboard] Built IMProofBench {run_id}: "
            f"{run_data['totals']['agents']} agents, "
            f"${run_data['totals']['cost_usd']:.2f}"
        )
    return runs_meta
