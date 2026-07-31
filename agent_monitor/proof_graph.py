"""Proof-graph generation — distill a run's proof into a logical DAG.

A small/cheap LLM reads the problem plus the proof write-up and emits a
strict-JSON graph: nodes are logical units (assumptions, lemmas, claims,
steps, contradiction, conclusion) and edges are "supports" arrows from
premise to consequence. The result is cached in the run workspace as
``proof_graph.json`` so it only costs one model call per (re)generation.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

GRAPH_FILENAME = "proof_graph.json"

_KINDS = {
    "assumption", "definition", "lemma", "claim", "case",
    "step", "contradiction", "conclusion",
}

# Kept as a module constant so it can be tuned/replaced easily (the user may
# supply their own prompt template later).
GRAPH_PROMPT = """You are a mathematical proof analyst. Read the PROBLEM and the PROOF below, then extract the logical structure of the proof as a directed acyclic graph (DAG).

Respond with STRICT JSON only — no markdown fences, no commentary. Schema:
{
  "title": "short name of the theorem being proved",
  "nodes": [
    {
      "id": "n1",
      "kind": "assumption | definition | lemma | claim | case | step | contradiction | conclusion",
      "label": "very short title, at most 8 words",
      "statement": "precise 1-3 sentence statement of this logical unit; LaTeX with $...$ is allowed"
    }
  ],
  "edges": [
    {"from": "n1", "to": "n2", "label": "implies | uses | case of | contradicts | derives"}
  ]
}

Rules:
- 6 to 20 nodes. Each node is ONE meaningful logical unit, not a paragraph dump.
- Edges point from a premise/ingredient to the statement it supports (assumptions at the top, conclusion at the bottom).
- Include every assumption or known fact the proof actually uses.
- Exactly one node with kind "conclusion".
- For proofs by contradiction, include the negated-assumption node (kind "assumption") and the contradiction node (kind "contradiction").
- The graph must be acyclic; every node must be reachable from some root or reach the conclusion.

PROBLEM:
{problem}

PROOF:
{proof}
"""


def _proof_source(workspace: Path, run_record: dict[str, Any] | None) -> tuple[str, str]:
    """Return (source_label, proof_text). Prefers workspace write-ups."""
    for name in ("proof.md", "proof.tex"):
        p = workspace / name
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                text = ""
            if text:
                return name, text
    # Fallback: the final agent output recorded on the run
    agents = (run_record or {}).get("agents") or []
    for a in reversed(agents):
        out = str(a.get("output") or "").strip()
        if len(out) > 200:
            return "final agent output", out
    return "", ""


def _resolve_llm(env: dict[str, str], model: str | None) -> tuple[str, str, str]:
    """Pick an OpenAI-compatible (base_url, api_key, model) from the user env."""
    candidates = [
        ("OPENAI_API_KEY", env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"),
        ("OPENROUTER_API_KEY", env.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"),
        ("DEEPSEEK_API_KEY", env.get("DEEPSEEK_API_BASE") or "https://api.deepseek.com"),
        ("GROQ_API_KEY", env.get("GROQ_API_BASE") or "https://api.groq.com/openai/v1"),
        ("XAI_API_KEY", env.get("XAI_API_BASE") or "https://api.x.ai/v1"),
    ]
    for key_var, base in candidates:
        key = env.get(key_var) or ""
        if key:
            chosen = model or env.get("AGENT_MONITOR_GRAPH_MODEL") or env.get("AGENT_MONITOR_MODEL") or "gpt-5-mini-2025-08-07"
            return base.rstrip("/"), key, chosen
    raise ValueError("No OpenAI-compatible API key configured — add one in Settings first")


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    # Strip a ```json fence if the model added one anyway
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model response contained no JSON object")
    blob = text[start : end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        # Models routinely emit LaTeX (e.g. "$\sqrt5$") without escaping the
        # backslashes, which is invalid JSON. Double any backslash that does
        # not start a valid JSON escape sequence and retry.
        repaired = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", blob)
        return json.loads(repaired)


def _sanitize(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = []
    seen: set[str] = set()
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "").strip()
        if not nid or nid in seen:
            continue
        seen.add(nid)
        kind = str(n.get("kind") or "step").strip().lower()
        nodes.append(
            {
                "id": nid,
                "kind": kind if kind in _KINDS else "step",
                "label": str(n.get("label") or nid)[:120],
                "statement": str(n.get("statement") or "")[:1200],
            }
        )
    ids = {n["id"] for n in nodes}
    edges = []
    edge_seen: set[tuple[str, str]] = set()
    for e in graph.get("edges") or []:
        if not isinstance(e, dict):
            continue
        src, dst = str(e.get("from") or ""), str(e.get("to") or "")
        if src in ids and dst in ids and src != dst and (src, dst) not in edge_seen:
            edge_seen.add((src, dst))
            edges.append({"from": src, "to": dst, "label": str(e.get("label") or "")[:40]})
    if not nodes:
        raise ValueError("model returned an empty graph")
    return {"title": str(graph.get("title") or "")[:200], "nodes": nodes, "edges": edges}


def load_cached(workspace: Path) -> dict[str, Any] | None:
    p = workspace / GRAPH_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def generate(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None = None,
) -> dict[str, Any]:
    """Generate the proof graph via a single LLM call and cache it."""
    from agent_monitor.settings import _http_json, resolved_user_env

    problem = ""
    prob_file = workspace / "problem.txt"
    if prob_file.exists():
        problem = prob_file.read_text(encoding="utf-8", errors="replace").strip()

    source, proof = _proof_source(workspace, run_record)
    if not proof:
        raise ValueError("No proof text yet — wait for the run to produce proof.md")

    env = resolved_user_env(user)
    base, key, chosen_model = _resolve_llm(env, model)

    prompt = GRAPH_PROMPT.replace("{problem}", problem[:6000]).replace("{proof}", proof[:60000])
    code, body = _http_json(
        f"{base}/chat/completions",
        method="POST",
        headers={"Authorization": f"Bearer {key}"},
        # No temperature: newer OpenAI models (gpt-5.x) reject anything but
        # the default value, and the prompt already pins the output format.
        payload={
            "model": chosen_model,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    if code != 200 or not isinstance(body, dict):
        err = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        raise ValueError(f"Graph model call failed (HTTP {code}): {err or 'unknown error'}")
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("Graph model returned an unexpected response shape")

    graph = _sanitize(_extract_json(str(content)))
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": chosen_model,
        "source": source,
        "graph": graph,
    }
    (workspace / GRAPH_FILENAME).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result
