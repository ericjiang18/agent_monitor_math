"""Resolve final LaTeX/Markdown proofs and attribute spans to contributing agents."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

CONTRIBUTOR_ROLES = {
    "writeup",
    "refiner",
    "assembly_solver",
    "finalize_polish",
    "finalize_typeset",
    # Hermes / CLI session engines
    "writer",
    "prover",
    "assistant",
    "author",
    "minor_polish",
    "author",
    "benchmark",
}

CONTRIBUTOR_STAGE_PREFIXES = (
    "writeup_",
    "refine_",
    "assembly_",
    "finalize_",
    "minor_polish_",
    "iter-",       # hermes draft/write iterations
    "turn-",       # codex / openclaude write turns
    "final",
    "typeset",
    "Author-r",
)

MIN_MATCH_CHARS = 48
PREVIEW_LEN = 120


def _read_text(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text if text.strip() else None


def _normalize_for_match(text: str) -> str:
    text = re.sub(r"%[^\n]*", " ", text)
    text = re.sub(r"\\begin\{[^}]+\}.*?\\end\{[^}]+\}", " ", text, flags=re.DOTALL)
    text = re.sub(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?(?:\{[^}]*\})*", " ", text)
    text = re.sub(r"[$\\{}#_^&]", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _extract_json_strings(raw: str) -> list[str]:
    out: list[str] = []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return out
    if isinstance(data, str):
        return [data]
    if isinstance(data, dict):
        for key in (
            "solution",
            "Final_Solution",
            "refined_solution",
            "full_text",
            "content",
            "output",
            "answer_tex",
            "latex",
            "proof",
            "body",
        ):
            val = data.get(key)
            if isinstance(val, str) and len(val.strip()) > 40:
                out.append(val)
        for val in data.values():
            if isinstance(val, str) and len(val.strip()) > 80:
                out.append(val)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str) and len(item.strip()) > 40:
                out.append(item)
            elif isinstance(item, dict):
                for key in ("solution", "Final_Solution", "content", "text"):
                    val = item.get(key)
                    if isinstance(val, str) and len(val.strip()) > 40:
                        out.append(val)
    return out


def _extract_text_chunks(raw: str | None) -> list[str]:
    if not raw or not str(raw).strip():
        return []
    text = str(raw)
    chunks: list[str] = []

    chunks.extend(_extract_json_strings(text))

    for block in re.findall(r"```(?:latex|tex)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE):
        if len(block.strip()) > 40:
            chunks.append(block.strip())

    for block in re.findall(r"\\begin\{document\}(.*?)\\end\{document\}", text, flags=re.DOTALL):
        if len(block.strip()) > 40:
            chunks.append(block.strip())

    for block in re.findall(
        r"(\\begin\{(?:theorem|proof|lemma|proposition|corollary|definition)\}.*?\\end\{(?:theorem|proof|lemma|proposition|corollary|definition)\})",
        text,
        flags=re.DOTALL,
    ):
        chunks.append(block)

    if not chunks:
        chunks.append(text)

    # Paragraph-level splits for long plain/markdown outputs
    extra: list[str] = []
    for chunk in list(chunks):
        for para in re.split(r"\n\s*\n", chunk):
            para = para.strip()
            if len(para) >= MIN_MATCH_CHARS:
                extra.append(para)
    chunks.extend(extra)

    seen: set[str] = set()
    deduped: list[str] = []
    for c in chunks:
        key = _normalize_for_match(c)[:200]
        if key and key not in seen:
            seen.add(key)
            deduped.append(c)
    return deduped


def _is_contributor(agent: dict) -> bool:
    role = (agent.get("role") or "").lower()
    stage = agent.get("stage_name") or ""
    pipe = str(agent.get("pipeline_stage") or "").lower()
    if role in CONTRIBUTOR_ROLES:
        return True
    if role == "author":
        return True
    if pipe in {"draft", "write", "finalize", "author"}:
        return True
    return any(stage.startswith(p) for p in CONTRIBUTOR_STAGE_PREFIXES)


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
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


def _enrich_contributor_texts_from_artifacts(agents: list[dict], artifact_dir: Path | None) -> None:
    """Attach artifact-backed proof text so provenance can trace writeup/refine chains."""
    if not artifact_dir or not artifact_dir.exists():
        return

    memory = artifact_dir / "memory" if (artifact_dir / "memory").is_dir() else artifact_dir
    task_outputs: dict[str, dict] = {}
    for row in _load_jsonl(memory / "task_outputs.jsonl"):
        tid = row.get("task_id") or row.get("id")
        if tid:
            task_outputs[str(tid)] = row

    refine_index: dict[tuple[str, int], dict] = {}
    for row in _load_jsonl(memory / "refine.jsonl"):
        tid = row.get("task_id") or row.get("writeup_id")
        rnd = row.get("round", 0)
        if tid is not None:
            refine_index[(str(tid), int(rnd))] = row

    final_by_task: dict[str, dict] = {}
    for row in _load_jsonl(memory / "final_solutions.jsonl"):
        tid = row.get("task_id")
        if tid:
            final_by_task[str(tid)] = row

    for agent in agents:
        if not _is_contributor(agent):
            continue
        stage = agent.get("stage_name") or ""
        extras: list[str] = []

        if stage.startswith("writeup_"):
            tid = stage.removeprefix("writeup_")
            row = task_outputs.get(tid) or {}
            for key in ("solution", "full_text", "output", "writeup_text"):
                val = row.get(key)
                if isinstance(val, str) and len(val.strip()) > 40:
                    extras.append(val)
            fin = final_by_task.get(tid) or {}
            fs = fin.get("Final_Solution") or fin.get("solution")
            if isinstance(fs, str) and len(fs.strip()) > 40:
                extras.append(fs)

        m = re.match(r"refine_(.+)_round_(\d+)", stage)
        if m:
            tid, rnd = m.group(1), int(m.group(2))
            rrow = refine_index.get((tid, rnd)) or {}
            for key in ("refined_solution", "solution", "output"):
                val = rrow.get(key)
                if isinstance(val, str) and len(val.strip()) > 40:
                    extras.append(val)

        if stage.startswith("Author-r") or (agent.get("role") == "author" and agent.get("round_id") is not None):
            rnd = _author_round_from_agent(agent)
            if rnd is not None:
                round_text = _improof_author_round_text(artifact_dir, rnd)
                if round_text:
                    extras.append(round_text)

        if extras:
            merged = _agent_contribution_text(agent)
            agent["_provenance_text"] = merged + "\n\n" + "\n\n".join(extras)


def _agent_contribution_text(agent: dict) -> str:
    if agent.get("_provenance_text"):
        return str(agent["_provenance_text"])
    parts = [agent.get("output") or ""]
    ctx = agent.get("memory_context") or {}
    if isinstance(ctx, dict):
        for key in ("refined_solution_in", "writeup_statement", "answer_tex"):
            val = ctx.get(key)
            if val:
                parts.append(str(val))
    return "\n\n".join(p for p in parts if p)


def _ac_workspace(artifact_dir: Path) -> Path | None:
    ws_root = artifact_dir / "ac_workspaces"
    if not ws_root.is_dir():
        return None
    dirs = [d for d in ws_root.iterdir() if d.is_dir()]
    if not dirs:
        return None
    return dirs[0] if len(dirs) == 1 else max(dirs, key=lambda p: p.stat().st_mtime)


def _author_round_from_agent(agent: dict) -> int | None:
    stage = agent.get("stage_name") or ""
    m = re.match(r"Author-r(\d+)", stage, re.IGNORECASE)
    if m:
        return int(m.group(1))
    rid = agent.get("round_id")
    if rid is not None and str(rid).isdigit():
        return int(rid)
    return None


def _improof_author_round_text(artifact_dir: Path, round_num: int) -> str:
    ws = _ac_workspace(artifact_dir)
    if not ws:
        return ""
    parts: list[str] = []
    for rel in (
        f".ac/round-{round_num}/answer.tex",
        f".ac/author-round-{round_num}.md",
    ):
        text = _read_text(ws / rel)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _block_match_score(block_text: str, agent_text: str) -> float:
    block_norm = _normalize_for_match(block_text)
    if len(block_norm) < 20:
        return 0.0
    best = 0.0
    for chunk in _extract_text_chunks(agent_text):
        score = _match_score(block_norm, _normalize_for_match(chunk))
        if score > best:
            best = score
    whole = _match_score(block_norm, _normalize_for_match(agent_text))
    return max(best, whole)


def compute_incremental_provenance(final_tex: str, ordered_agents: list[dict]) -> dict[str, Any]:
    """Attribute final proof blocks to the latest agent whose cumulative output newly covers them."""
    blocks = _split_final_blocks(final_tex)
    by_agent: dict[str, list[dict[str, Any]]] = {}
    assign: list[dict | None] = [None] * len(blocks)
    prev_best = [0.0] * len(blocks)
    unmatched_lines = 0
    total_matched_lines = 0

    for agent in ordered_agents:
        text = _agent_contribution_text(agent)
        if not text.strip():
            continue
        for bi, block in enumerate(blocks):
            score = _block_match_score(block["text"], text)
            if score >= 0.45 and score > prev_best[bi] + 0.02:
                assign[bi] = agent
            prev_best[bi] = max(prev_best[bi], score)

    for bi, block in enumerate(blocks):
        agent = assign[bi]
        span_lines = block["end_line"] - block["start_line"] + 1
        if not agent:
            unmatched_lines += span_lines
            continue
        total_matched_lines += span_lines
        tid = agent["trace_id"]
        preview = re.sub(r"\s+", " ", block["text"]).strip()[:PREVIEW_LEN]
        by_agent.setdefault(tid, []).append({
            "start_line": block["start_line"],
            "end_line": block["end_line"],
            "preview": preview,
            "match_score": round(prev_best[bi], 3),
            "trace_id": tid,
        })

    total_lines = max(final_tex.count("\n") + 1, 1)
    provenance: list[dict[str, Any]] = []
    seen: set[str] = set()
    for agent in ordered_agents:
        tid = agent.get("trace_id")
        if not tid or tid not in by_agent or tid in seen:
            continue
        seen.add(tid)
        regions = _merge_regions(by_agent[tid])
        line_count = sum(r["end_line"] - r["start_line"] + 1 for r in regions)
        provenance.append({
            "trace_id": tid,
            "stage_name": agent.get("stage_name"),
            "role": agent.get("role"),
            "call_seq": agent.get("call_seq"),
            "regions": regions,
            "line_count": line_count,
            "contribution_pct": round(100 * line_count / total_lines, 1),
        })

    provenance.sort(key=lambda p: (-p["contribution_pct"], p.get("call_seq") or 0))
    return {
        "provenance": provenance,
        "matched_line_pct": round(100 * total_matched_lines / total_lines, 1),
        "unmatched_line_pct": round(100 * unmatched_lines / total_lines, 1),
        "contributor_count": len(provenance),
    }


def _ordered_contributors(agents: list[dict], source: str) -> list[dict]:
    contributors = [a for a in agents if _is_contributor(a)]
    if source == "improofbench_artifacts":
        authors = [a for a in contributors if (a.get("role") or "").lower() == "author"]
        authors.sort(key=lambda a: (_author_round_from_agent(a) if _author_round_from_agent(a) is not None else 999, a.get("call_seq") or 0))
        return authors or contributors
    return sorted(contributors, key=lambda a: a.get("call_seq") or 0)


def _split_final_blocks(final_tex: str) -> list[dict[str, Any]]:
    lines = final_tex.splitlines()
    blocks: list[dict[str, Any]] = []
    current: list[str] = []
    start_line = 1

    def flush(end_line: int) -> None:
        nonlocal current, start_line
        body = "\n".join(current).strip()
        if len(body) >= 20:
            blocks.append({
                "start_line": start_line,
                "end_line": end_line,
                "text": body,
            })
        current = []

    for i, line in enumerate(lines, 1):
        is_boundary = (
            line.startswith("## ")
            or line.startswith("\\section")
            or line.startswith("\\subsection")
            or line.startswith("\\begin{theorem")
            or line.startswith("\\begin{proof")
        )
        if is_boundary and current:
            flush(i - 1)
            start_line = i
        current.append(line)
        if not line.strip() and len(current) > 1:
            flush(i)
            start_line = i + 1
            current = []

    if current:
        flush(len(lines))

    if not blocks and final_tex.strip():
        blocks.append({"start_line": 1, "end_line": len(lines), "text": final_tex.strip()})
    return blocks


def _match_score(block_norm: str, chunk_norm: str) -> float:
    if len(block_norm) < 20 or len(chunk_norm) < MIN_MATCH_CHARS:
        return 0.0
    if block_norm == chunk_norm:
        return 1.0
    if block_norm in chunk_norm:
        ratio = len(block_norm) / max(len(chunk_norm), 1)
        return ratio
    if chunk_norm in block_norm:
        ratio = len(chunk_norm) / max(len(block_norm), 1)
        return ratio
    return SequenceMatcher(None, block_norm, chunk_norm).ratio()


def _role_priority(agent: dict) -> int:
    role = (agent.get("role") or "").lower()
    stage = agent.get("stage_name") or ""
    order = {
        "writeup": 0,
        "refiner": 1,
        "assembly_solver": 2,
        "minor_polish": 3,
        "finalize_polish": 4,
        "author": 5,
        "finalize_typeset": 8,
        "benchmark": 9,
    }
    if role in order:
        return order[role]
    if stage.startswith("writeup_"):
        return 0
    if stage.startswith("refine_"):
        return 1
    if stage.startswith("Author-r"):
        return 5
    return 6


def _best_agent_for_block(block_text: str, agents: list[dict]) -> tuple[dict | None, float]:
    block_norm = _normalize_for_match(block_text)
    if len(block_norm) < 20:
        return None, 0.0

    candidates: list[tuple[dict, float]] = []

    for agent in agents:
        if not _is_contributor(agent):
            continue
        best_for_agent = 0.0
        for chunk in _extract_text_chunks(_agent_contribution_text(agent)):
            chunk_norm = _normalize_for_match(chunk)
            score = _match_score(block_norm, chunk_norm)
            if score > best_for_agent:
                best_for_agent = score
        if best_for_agent >= 0.45:
            candidates.append((agent, best_for_agent))

    if not candidates:
        return None, 0.0

    candidates.sort(key=lambda x: (-x[1], _role_priority(x[0]), x[0].get("call_seq") or 0))
    return candidates[0]


def _merge_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not regions:
        return []
    regions = sorted(regions, key=lambda r: (r["start_line"], r["trace_id"]))
    merged: list[dict[str, Any]] = []
    for r in regions:
        if merged and merged[-1]["trace_id"] == r["trace_id"] and r["start_line"] <= merged[-1]["end_line"] + 2:
            merged[-1]["end_line"] = max(merged[-1]["end_line"], r["end_line"])
            merged[-1]["preview"] = (merged[-1].get("preview") or "")[:PREVIEW_LEN]
        else:
            merged.append(dict(r))
    return merged


def compute_provenance(final_tex: str, agents: list[dict]) -> dict[str, Any]:
    blocks = _split_final_blocks(final_tex)
    contributors = [a for a in agents if _is_contributor(a)]
    by_agent: dict[str, list[dict[str, Any]]] = {}
    unmatched_lines = 0
    total_matched_lines = 0

    for block in blocks:
        agent, score = _best_agent_for_block(block["text"], contributors)
        span_lines = block["end_line"] - block["start_line"] + 1
        if not agent or score < 0.45:
            unmatched_lines += span_lines
            continue
        total_matched_lines += span_lines
        tid = agent["trace_id"]
        preview = re.sub(r"\s+", " ", block["text"]).strip()[:PREVIEW_LEN]
        by_agent.setdefault(tid, []).append({
            "start_line": block["start_line"],
            "end_line": block["end_line"],
            "preview": preview,
            "match_score": round(score, 3),
            "trace_id": tid,
        })

    total_lines = max(final_tex.count("\n") + 1, 1)
    provenance: list[dict[str, Any]] = []
    for agent in sorted(contributors, key=lambda a: a.get("call_seq") or 0):
        tid = agent.get("trace_id")
        if not tid or tid not in by_agent:
            continue
        regions = _merge_regions(by_agent[tid])
        line_count = sum(r["end_line"] - r["start_line"] + 1 for r in regions)
        provenance.append({
            "trace_id": tid,
            "stage_name": agent.get("stage_name"),
            "role": agent.get("role"),
            "call_seq": agent.get("call_seq"),
            "regions": regions,
            "line_count": line_count,
            "contribution_pct": round(100 * line_count / total_lines, 1),
        })

    provenance.sort(key=lambda p: (-p["contribution_pct"], p.get("call_seq") or 0))

    return {
        "provenance": provenance,
        "matched_line_pct": round(100 * total_matched_lines / total_lines, 1),
        "unmatched_line_pct": round(100 * unmatched_lines / total_lines, 1),
        "contributor_count": len(provenance),
    }


def resolve_latex_path(run_data: dict) -> tuple[Path | None, str]:
    """Return (path, label) for the best final proof file for this run."""
    source = run_data.get("source") or ""
    prob_id = run_data.get("prob_id") or ""
    artifact_dir = run_data.get("artifact_dir")
    run_dir = Path(artifact_dir) if artifact_dir else None
    cwd = Path.cwd()

    candidates: list[tuple[Path, str]] = []

    # Console runs keep artifacts in a per-run workspace (proof.md / proof.tex).
    workspace = run_data.get("workspace")
    if workspace:
        ws = Path(workspace)
        candidates.append((ws / "proof.md", "proof.md"))
        candidates.append((ws / "proof.tex", "proof.tex"))
        candidates.append((ws / "solution.tex", "solution.tex"))
        if ws.exists():
            for p in sorted(ws.glob("*/solutions/*.tex")):
                candidates.append((p, p.relative_to(ws).as_posix()))
            for p in sorted(ws.glob("*/ac_workspaces/*/answer.tex")):
                candidates.append((p, p.relative_to(ws).as_posix()))
            for p in sorted(ws.glob("*.tex")):
                candidates.append((p, p.name))

    if source == "ucla_artifacts" and run_dir:
        prob_dir = run_dir.parent
        candidates.extend([
            (prob_dir / "selected_solution.tex", "selected_solution.tex"),
            (run_dir / "solution.tex", "solution.tex"),
            (prob_dir / "harness_run_0" / "solution.tex", "solution.tex"),
        ])
        if prob_id:
            candidates.extend([
                (cwd / "UCLA" / "Output" / f"{prob_id}.tex", f"UCLA/Output/{prob_id}.tex"),
                (prob_dir.parent.parent / "Output" / f"{prob_id}.tex", f"Output/{prob_id}.tex"),
            ])

    if source == "improofbench_artifacts" and run_dir:
        if prob_id:
            candidates.extend([
                (run_dir / "solutions" / f"{prob_id}.tex", f"solutions/{prob_id}.tex"),
                (cwd / "IMProofBench" / "Output" / f"{prob_id}.tex", f"IMProofBench/Output/{prob_id}.tex"),
                (run_dir.parent.parent / "Output" / f"{prob_id}.tex", f"Output/{prob_id}.tex"),
            ])
        for ws in sorted((run_dir / "ac_workspaces").glob("*/answer.tex") if (run_dir / "ac_workspaces").exists() else []):
            candidates.append((ws, ws.relative_to(run_dir).as_posix()))
        for ws in sorted((run_dir / "ac_workspaces").glob("*/.ac/round-*/answer.tex") if (run_dir / "ac_workspaces").exists() else []):
            candidates.append((ws, ws.relative_to(run_dir).as_posix()))

    if run_dir:
        candidates.extend([
            (run_dir / "solution.tex", "solution.tex"),
            (run_dir.parent / "selected_solution.tex", "selected_solution.tex"),
        ])

    seen: set[str] = set()
    for path, label in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        text = _read_text(path)
        if text:
            return path, label

    return None, ""


def attach_provenance_to_agents(agents: list[dict], provenance: list[dict]) -> None:
    by_tid = {p["trace_id"]: p for p in provenance}
    for agent in agents:
        p = by_tid.get(agent.get("trace_id") or "")
        if p:
            agent["final_latex_contribution"] = {
                "regions": p.get("regions") or [],
                "contribution_pct": p.get("contribution_pct"),
                "line_count": p.get("line_count"),
            }


def build_final_latex_bundle(run_data: dict) -> dict[str, Any] | None:
    path, label = resolve_latex_path(run_data)
    if not path:
        return None

    tex = _read_text(path)
    if not tex:
        return None

    agents = run_data.get("agents") or []
    artifact_dir = run_data.get("artifact_dir")
    adir = Path(artifact_dir) if artifact_dir else None
    if adir and adir.name.startswith("harness_run"):
        adir = adir
    _enrich_contributor_texts_from_artifacts(agents, adir)
    source = run_data.get("source") or ""
    ordered = _ordered_contributors(agents, source)
    if len(ordered) >= 2:
        prov = compute_incremental_provenance(tex, ordered)
    else:
        prov = compute_provenance(tex, agents)
    attach_provenance_to_agents(agents, prov["provenance"])

    is_latex = "\\documentclass" in tex or "\\begin{document}" in tex
    line_count = tex.count("\n") + 1

    bundle: dict[str, Any] = {
        "source_path": str(path),
        "source_label": label,
        "char_count": len(tex),
        "line_count": line_count,
        "is_latex": is_latex,
        "format": "latex" if is_latex else "markdown",
        **prov,
    }

    selector_path = path.parent / "selector_verdict.json"
    if not selector_path.exists() and path.parent.name.startswith("harness_run"):
        selector_path = path.parent.parent / "selector_verdict.json"
    if selector_path.exists():
        try:
            sel = json.loads(selector_path.read_text(encoding="utf-8"))
            bundle["selector"] = {
                "selected_proof_source": sel.get("selected_proof_source"),
                "selected_verified": sel.get("selected_verified"),
                "selected_is_relaxation": sel.get("selected_is_relaxation"),
                "selected_problem_solved": (sel.get("selected_problem_solved") or "")[:500],
            }
        except (json.JSONDecodeError, OSError):
            pass

    return bundle


def _sanitize_tex_for_compile(tex: str) -> str:
    """Replace optional packages that may be missing in minimal TeX installs."""
    tex = re.sub(
        r"\\usepackage\s*\{fullpage\}",
        r"\\usepackage[margin=1in]{geometry}",
        tex,
    )
    # Drop packages often absent in minimal TeX Live installs.
    tex = re.sub(r",?\s*mathtools", "", tex)
    tex = re.sub(r",?\s*hyperref", "", tex)
    if "geometry" not in tex and "\\documentclass" in tex:
        tex = tex.replace(
            "\\begin{document}",
            "\\usepackage[margin=1in]{geometry}\n\\begin{document}",
            1,
        )
    return tex


def compile_pdf(tex_path: Path, cache_pdf: Path) -> bool:
    """Compile LaTeX to PDF; cache result. Returns True on success."""
    tex = _read_text(tex_path)
    if not tex or "\\documentclass" not in tex:
        return False

    cache_pdf.parent.mkdir(parents=True, exist_ok=True)
    if cache_pdf.exists() and cache_pdf.stat().st_mtime >= tex_path.stat().st_mtime:
        return True

    if not shutil.which("pdflatex"):
        return False

    tex = _sanitize_tex_for_compile(tex)

    with tempfile.TemporaryDirectory(prefix="harness_latex_") as tmp:
        work = Path(tmp)
        local_tex = work / "proof.tex"
        local_tex.write_text(tex, encoding="utf-8")
        cmd = [
            "pdflatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "proof.tex",
        ]
        for _ in range(2):
            try:
                subprocess.run(
                    cmd,
                    cwd=work,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except (subprocess.TimeoutExpired, OSError):
                return False
        pdf = work / "proof.pdf"
        if pdf.exists() and pdf.stat().st_size > 0:
            shutil.copy2(pdf, cache_pdf)
            return True
    return False


def pdf_cache_path(cache_dir: Path, run_id: str) -> Path:
    return cache_dir / "pdf" / f"{run_id}.pdf"
