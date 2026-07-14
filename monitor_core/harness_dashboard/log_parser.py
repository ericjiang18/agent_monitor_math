"""Parse harness stdout logs into dashboard-compatible run data."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_dashboard.parse import STAGE_PIPELINE, parse_stage_name


_DONE_RE = re.compile(
    r"\[(?P<stage>[^\]]+)\] done (?P<elapsed>[\d.]+)s "
    r"tokens\(in=(?P<input>\d+) cached=(?P<cached>\d+) out=(?P<output>\d+) "
    r"reason=(?P<reason>\d+)\) cost=\$(?P<cost>[\d.]+) id=(?P<resp_id>\S+)"
)
_MODEL_RE = re.compile(
    r"\[(?P<stage>[^\]]+)\] model=(?P<model>\S+) reasoning=(?P<reasoning>\S+) "
    r"verbosity=(?P<verbosity>\S+) max_tokens=(?P<max_tokens>\d+) "
    r"background=(?P<background>\S+) web_search=(?P<web_search>\S+)"
)
_STAGE_HDR_RE = re.compile(r"^\[Stage (\d+(?:\.\d+)?)\]\s*(.+)$")
_LIT_SAVED_RE = re.compile(
    r"\[lit_research\] saved (\d+) paper\(s\) \((\d+) successful\)"
)
_LIT_PICK_RE = re.compile(r"^\s*•\s+(.+?)\s+—\s+(https?://\S+)")


def _normalize_stage_name(raw: str) -> str:
    """Normalize bracket stage names (spaces/slashes → underscores)."""
    s = raw.strip()
    if s.startswith("lit_read ") and not s.startswith("lit_read_"):
        s = "lit_read_" + s.removeprefix("lit_read ").strip()
    return s.replace("/", "_").replace(" ", "_")


def _run_id_from_path(path: Path) -> str:
    stem = path.stem.replace("-", "_").replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_]+", "_", stem).strip("_").lower()


def _trace_name(meta: dict, run_id: str) -> str:
    if meta.get("output_root"):
        parts = Path(meta["output_root"].replace("\\", "/")).parts
        for p in reversed(parts):
            if p and p not in ("harness_run_0", "harness_run_1", "_harness_runs"):
                return p.replace("_", " ")
    if meta.get("problem_file"):
        return Path(meta["problem_file"]).stem.replace("_", " ")
    return run_id.replace("_", " ")


def _extract_meta(lines: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    problem_lines: list[str] = []
    in_problem = False
    for line in lines[:80]:
        if line.startswith("[parallel] launch at "):
            meta["launch_at"] = line.split(" at ", 1)[1].strip()
        elif line.startswith("[parallel] PROBLEM_FILE="):
            meta["problem_file"] = line.split("=", 1)[1].strip()
        elif line.startswith("[parallel] OUTPUT_ROOT_DIR="):
            meta["output_root"] = line.split("=", 1)[1].strip()
        elif line.startswith("MODEL:"):
            meta["model"] = line.split(":", 1)[1].strip()
        elif line.startswith("ADVISOR_BUDGET:"):
            meta["advisor_budget"] = line.split(":", 1)[1].strip()
        elif line.startswith("VERIFY_ROUNDS:"):
            meta["verify_rounds"] = line.split(":", 1)[1].strip()
        elif line.startswith("[problem]"):
            in_problem = True
            rest = line.removeprefix("[problem]").strip()
            if rest:
                problem_lines.append(rest)
        elif in_problem:
            if line.startswith("[output]"):
                in_problem = False
            else:
                problem_lines.append(line)
    if problem_lines:
        meta["problem_text"] = "\n".join(problem_lines).strip()
    return meta


def _extract_stage_prompts(text: str) -> dict[str, str]:
    """Map stage_name → prompt text from [Stage N] sections."""
    prompts: dict[str, str] = {}
    stage_blocks: list[tuple[str, int, int]] = []
    for m in re.finditer(
        r"={80}\n\[Stage (\d+(?:\.\d+)?)\] ([^\n]+)\n={80}\n",
        text,
    ):
        stage_num = m.group(1)
        title = m.group(2).strip().lower()
        stage_blocks.append((stage_num, m.end(), title))

    stage_to_name = {
        "0": "lit_search",
        "1": "advisor_directions",
        "2": "advisor_r1",  # rarely present in logs
    }

    for i, (stage_num, start, title) in enumerate(stage_blocks):
        end = stage_blocks[i + 1][1] - len(f"{'=' * 80}\n[Stage ") if i + 1 < len(stage_blocks) else len(text)
        if i + 1 < len(stage_blocks):
            # back up to stage header start
            next_hdr = text.find("=" * 80, start)
            if next_hdr != -1:
                end = next_hdr
        block = text[start:end].strip()
        # Trim at first agent model= line
        cut = re.search(r"\n\[[^\]]+\] model=", block)
        if cut:
            block = block[: cut.start()].strip()
        key = stage_to_name.get(stage_num)
        if not key and "advisor" in title:
            key = "advisor_directions"
        elif not key and "literature" in title:
            key = "lit_search"
        if key and block:
            prompts[key] = block
    return prompts


def _extract_lit_picks(text: str) -> list[dict]:
    picks: list[dict] = []
    in_list = False
    for line in text.splitlines():
        if "[lit_research]" in line and "paper(s) to process:" in line:
            in_list = True
            continue
        if in_list:
            m = _LIT_PICK_RE.match(line)
            if m:
                picks.append({"title": m.group(1).strip(), "url": m.group(2).strip()})
            elif line.startswith("[") and "lit_research" not in line:
                in_list = False
            elif line.startswith("=" * 20):
                in_list = False
    return picks


def _extract_lit_search_json(text: str) -> str | None:
    """Best-effort extract paper JSON from lit_search Response blob."""
    for line in text.splitlines():
        if "ResponseOutputText" not in line or '"url"' not in line:
            continue
        m = re.search(r"text='(\[\s*\\n\s*\{)", line)
        if not m:
            m = re.search(r'text="(\[\s*\\n\s*\{)', line)
        if not m:
            continue
        idx = line.find("text=")
        if idx == -1:
            continue
        quote = line[idx + 5]
        if quote not in ("'", '"'):
            continue
        start = idx + 6
        # Walk until unescaped closing quote before ", type="
        buf: list[str] = []
        i = start
        while i < len(line):
            c = line[i]
            if c == "\\" and i + 1 < len(line):
                n = line[i + 1]
                if n == "n":
                    buf.append("\n")
                elif n == "'":
                    buf.append("'")
                elif n == '"':
                    buf.append('"')
                elif n == "\\":
                    buf.append("\\")
                else:
                    buf.append(n)
                i += 2
                continue
            if c == quote:
                break
            buf.append(c)
            i += 1
        raw = "".join(buf)
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return json.dumps(data, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            continue
    return None


def _build_agent_from_done(
    stage_name: str,
    m: re.Match,
    *,
    run_id: str,
    trace_name: str,
    model_default: str,
    seq: int,
    prompts: dict[str, str],
    outputs: dict[str, str],
    output_sources: dict[str, str],
    launch_at: str | None,
) -> dict:
    parsed = parse_stage_name(stage_name)
    inp = int(m.group("input"))
    cached = int(m.group("cached"))
    out = int(m.group("output"))
    reason = int(m.group("reason"))
    agent = {
        "trace_id": f"{run_id}::{stage_name}",
        "trace_name": trace_name,
        "run_id": run_id,
        "stage_name": stage_name,
        "call_seq": seq,
        "round_id": parsed.get("round_id"),
        "agent_id": parsed.get("agent_id"),
        "role": parsed.get("role"),
        "pipeline_stage": parsed.get("pipeline_stage"),
        "model": model_default,
        "ts": launch_at,
        "latency_s": float(m.group("elapsed")),
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
        "total_input_tokens": inp + cached,
        "reasoning_tokens": reason,
        "cost_usd": float(m.group("cost")),
        "response_id": m.group("resp_id"),
        "received_from": [],
        "sent_to": [],
        "tool_calls": ["web_search"] if stage_name == "lit_search" else [],
        "status": "completed",
        "source": "previous_log",
        "prompt": prompts.get(stage_name),
        "prompt_source": "log_stage_section" if prompts.get(stage_name) else None,
        "output": outputs.get(stage_name),
        "output_source": output_sources.get(stage_name),
        "thinking": None,
        "stop_reason": "completed",
    }
    if stage_name == "lit_search" and agent.get("output"):
        agent["decision_impact"] = {
            "paper_count": len(json.loads(agent["output"])) if agent["output"].startswith("[") else None,
            "type": "literature_search",
        }
    return agent


def parse_log_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    meta = _extract_meta(lines)
    run_id = _run_id_from_path(path)
    trace_name = _trace_name(meta, run_id)
    prompts = _extract_stage_prompts(text)
    lit_picks = _extract_lit_picks(text)
    lit_json = _extract_lit_search_json(text)

    outputs: dict[str, str] = {}
    output_sources: dict[str, str] = {}
    if lit_json:
        outputs["lit_search"] = lit_json
        output_sources["lit_search"] = "log_response_blob"
    elif lit_picks:
        outputs["lit_search"] = json.dumps(
            {"paper_count": len(lit_picks), "papers": lit_picks},
            ensure_ascii=False,
            indent=2,
        )
        output_sources["lit_search"] = "log_lit_picks"
    saved = _LIT_SAVED_RE.search(text)
    if saved:
        outputs["lit_research_summary"] = (
            f"Saved {saved.group(1)} papers ({saved.group(2)} successful deep-read extractions)"
        )

    agents_by_stage: dict[str, dict] = {}
    model_info: dict[str, dict] = {}
    errors: dict[str, list[str]] = defaultdict(list)
    seq = 0

    for line in lines:
        dm = _DONE_RE.search(line)
        if dm:
            stage_name = _normalize_stage_name(dm.group("stage"))
            seq += 1
            agent = _build_agent_from_done(
                stage_name,
                dm,
                run_id=run_id,
                trace_name=trace_name,
                model_default=model_info.get(stage_name, {}).get("model") or meta.get("model", ""),
                seq=seq,
                prompts=prompts,
                outputs=outputs,
                output_sources=output_sources,
                launch_at=meta.get("launch_at"),
            )
            mi = model_info.get(stage_name)
            if mi:
                agent["model"] = mi["model"]
                if mi.get("web_search") == "True":
                    agent.setdefault("tool_calls", []).append("web_search")
            agents_by_stage[stage_name] = agent
            continue

        mm = _MODEL_RE.search(line)
        if mm:
            stage_name = _normalize_stage_name(mm.group("stage"))
            model_info[stage_name] = mm.groupdict()
            if stage_name not in agents_by_stage:
                parsed = parse_stage_name(stage_name)
                agents_by_stage[stage_name] = {
                    "trace_id": f"{run_id}::{stage_name}",
                    "trace_name": trace_name,
                    "run_id": run_id,
                    "stage_name": stage_name,
                    "call_seq": 0,
                    "round_id": parsed.get("round_id"),
                    "agent_id": parsed.get("agent_id"),
                    "role": parsed.get("role"),
                    "pipeline_stage": parsed.get("pipeline_stage"),
                    "model": mm.group("model"),
                    "ts": meta.get("launch_at"),
                    "latency_s": None,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "total_input_tokens": 0,
                    "cost_usd": None,
                    "received_from": [],
                    "sent_to": [],
                    "tool_calls": ["web_search"] if mm.group("web_search") == "True" else [],
                    "status": "in_progress",
                    "source": "previous_log",
                    "prompt": prompts.get(stage_name),
                    "prompt_source": "log_stage_section" if prompts.get(stage_name) else None,
                    "output": None,
                    "thinking": None,
                    "stop_reason": None,
                }
            continue

        em = re.search(r"\[(?P<stage>[^\]]+)\] error: (?P<err>.+)$", line)
        if em:
            stage_name = _normalize_stage_name(em.group("stage"))
            errors[stage_name].append(em.group("err")[:500])

    # Mark interrupted agents (started but no done)
    for stage_name, agent in agents_by_stage.items():
        if agent.get("status") == "in_progress":
            agent["status"] = "interrupted"
            if errors.get(stage_name):
                agent["output"] = errors[stage_name][-1]
                agent["output_source"] = "log_error"
                agent["stop_reason"] = "error"
            else:
                agent["stop_reason"] = "interrupted"
        if errors.get(stage_name) and agent.get("status") == "completed":
            agent["error_count"] = len(errors[stage_name])

    agents = list(agents_by_stage.values())
    agents.sort(key=lambda a: (a.get("pipeline_stage") or "99", a.get("stage_name") or ""))

    by_pipeline: dict[str, list] = defaultdict(list)
    for a in agents:
        ps = a.get("pipeline_stage") or "other"
        by_pipeline[str(ps)].append(a)

    totals = {
        "agents": len(agents),
        "input_tokens": sum(a.get("total_input_tokens", 0) for a in agents),
        "output_tokens": sum(a.get("output_tokens", 0) for a in agents),
        "cost_usd": sum(a.get("cost_usd") or 0 for a in agents if a.get("cost_usd")),
        "latency_s": sum(a.get("latency_s") or 0 for a in agents if a.get("latency_s")),
    }

    last_stage = max(
        (a.get("pipeline_stage") or "0" for a in agents),
        key=lambda x: float(x) if str(x).replace(".", "").isdigit() else 0,
        default="0",
    )

    return {
        "run_id": run_id,
        "trace_name": trace_name,
        "source": "previous_log",
        "log_file": path.name,
        "log_path": str(path.resolve()),
        "meta": meta,
        "pipeline": STAGE_PIPELINE,
        "agents": agents,
        "edges": [],
        "stages_present": sorted(by_pipeline.keys(), key=lambda x: float(x) if x.replace(".", "").isdigit() else 99),
        "last_pipeline_stage": last_stage,
        "totals": totals,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }


def build_all_from_logs(log_dir: Path) -> list[dict]:
    """Parse every *.log in log_dir; return manifest entries."""
    manifest_entries: list[dict] = []
    if not log_dir.exists():
        return manifest_entries

    for path in sorted(log_dir.glob("*.log")):
        run_data = parse_log_file(path)
        run_id = run_data["run_id"]
        out_path = path  # caller writes cache
        manifest_entries.append({
            "run_id": run_id,
            "trace_name": run_data["trace_name"],
            "source": "previous_log",
            "log_file": path.name,
            "agents": run_data["totals"]["agents"],
            "output_tokens": run_data["totals"]["output_tokens"],
            "cost_usd": run_data["totals"]["cost_usd"],
            "latency_s": run_data["totals"]["latency_s"],
            "stages_active": run_data.get("stages_present") or [],
            "last_ts": run_data["meta"].get("launch_at"),
            "file": f"{run_id}.json",
            "_run_data": run_data,
        })
    return manifest_entries
