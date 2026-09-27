"""Proof-graph generation — distill a run's proof into a logical DAG.

A small/cheap LLM reads the problem plus the proof write-up and emits a
strict-JSON graph: nodes are logical units (assumptions, lemmas, claims,
steps, contradiction, conclusion) and edges are "supports" arrows from
premise to consequence. The result is cached in the run workspace as
``proof_graph.json`` so it only costs one model call per (re)generation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

GRAPH_FILENAME = "proof_graph.json"
LEAN_GRAPH_FILENAME = "lean_proof_graph.json"

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
      "statement": "precise 1-3 sentence statement of this logical unit; LaTeX with $...$ is allowed",
      "citations": ["what this step relies on: a citation marker like [2] used in the proof, a named theorem such as Euclid's lemma, or \"self-derived\""]
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
- "citations" is required on every node: copy the markers the proof itself uses where present, name the theorem when the proof invokes a classical result, and use "self-derived" for steps the proof establishes on its own. Never invent a source.

PROBLEM:
{problem}

PROOF:
{proof}
"""

LEAN_GRAPH_PROMPT = """You are a Lean 4 proof analyst. Read the PROBLEM and the Lean 4 SOURCE below, then extract the logical structure of the *formal* proof as a directed acyclic graph (DAG).

Respond with STRICT JSON only — no markdown fences, no commentary. Schema:
{
  "title": "Lean identifier of the main theorem, e.g. parabolic_upper_bound",
  "nodes": [
    {
      "id": "n1",
      "kind": "assumption | definition | lemma | claim | case | step | contradiction | conclusion",
      "lean_kind": "variable | axiom | def | abbrev | lemma | theorem | have | obtain | refine | sorry",
      "label": "exact Lean name / binder, e.g. hneumann or hopf_strict_comparison",
      "statement": "Lean code snippet only — the type signature or `have` line, NOT English prose",
      "citations": ["Lean name used, Mathlib lemma, or self-derived"]
    }
  ],
  "edges": [
    {"from": "n1", "to": "n2", "label": "uses | applies | cases | rw | exact | intro"}
  ]
}

Rules — this graph is for *Lean code*, not natural language:
- `label` MUST be a Lean identifier (declaration name, hypothesis name, or `have` binder). Never a English title.
- `statement` MUST be Lean syntax copied/abbreviated from the source, e.g.
  `theorem main : ∀ x ∈ closure Ω, ∀ t > 0, u x t ≤ M` or `have h : u x t ≤ M + ε * t := by …`
  Do NOT rewrite into English sentences. Do NOT use LaTeX prose.
- Prefer the type after `:` (proposition/type) for theorems/lemmas; for `have`/`obtain` include the binder and type.
- Nodes: top-level decls + important `have`/`obtain` steps that carry the argument. Skip imports and trivial `let`.
- Map kinds: axiom/opaque/variable → assumption; def/abbrev → definition; lemma → lemma; supporting theorem → claim; main theorem → conclusion; local have → step.
- Edges use Lean-ish labels (`uses`, `applies`, `exact`, `rw`) from premise → dependent.
- Exactly one `conclusion`. 4–24 nodes. Acyclic.

PROBLEM:
{problem}

LEAN SOURCE:
{lean}
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
        raw_cites = n.get("citations")
        if isinstance(raw_cites, str):
            raw_cites = [raw_cites]
        cites = [
            str(c)[:120]
            for c in (raw_cites or [])
            if isinstance(c, (str, int, float)) and str(c).strip()
        ][:6]
        entry = {
            "id": nid,
            "kind": kind if kind in _KINDS else "step",
            "label": str(n.get("label") or nid)[:120],
            "statement": str(n.get("statement") or "")[:1200],
            "citations": cites,
        }
        lk = str(n.get("lean_kind") or "").strip().lower()
        if lk:
            entry["lean_kind"] = lk[:40]
        nodes.append(entry)
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


def _codex_account_ready(user: dict[str, Any] | None) -> bool:
    if not user or user.get("id") is None:
        return False
    from agent_monitor import codex_login

    return codex_login.account_login_ready(codex_login.account_home(user["id"]))


def _run_codex_prompt(
    user: dict[str, Any] | None, workspace: Path, prompt: str, *, timeout: int = 240
) -> str:
    """One-shot answer from the Codex CLI, billed against the caller's connected
    ChatGPT/OpenAI account (or their API key) instead of a direct chat-completions
    HTTP call — lets graph generation work without a metered API key.
    """
    from agent_monitor.engines_registry import which_tool

    codex = which_tool("codex")
    if not codex:
        raise ValueError("Codex CLI not installed on this server")

    from agent_monitor.jobs import _ensure_codex_auth
    from agent_monitor.settings import resolved_user_env

    from agent_monitor.subprocess_env import child_process_env, project_provider_env

    user_env = resolved_user_env(user) or {}
    env = child_process_env(extra=user_env)
    project_provider_env(env, (user_env.get("AGENT_MONITOR_MODEL"),))
    env.setdefault("NO_COLOR", "1")
    _ensure_codex_auth(env, (user or {}).get("id"))

    scratch = workspace / ".graph_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                codex, "exec", "--json", "--cd", str(scratch),
                "--sandbox", "danger-full-access", "--skip-git-repo-check", prompt,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"Codex CLI timed out after {timeout}s") from exc

    texts: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message":
                texts.append(str(item.get("text") or ""))
    if not texts:
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-2000:]
        raise ValueError(f"Codex CLI returned no answer (exit {proc.returncode}). {tail}")
    return texts[-1]


def load_cached(workspace: Path, *, kind: str = "informal") -> dict[str, Any] | None:
    name = LEAN_GRAPH_FILENAME if str(kind).lower() == "formal" else GRAPH_FILENAME
    p = workspace / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _lean_source(workspace: Path) -> str:
    from agent_monitor.lean_verify import read_source

    return (read_source(workspace) or "").strip()


def _lean_sig(body: str) -> str:
    """Best-effort Lean signature / have-line for display (no English prose)."""
    text = (body or "").strip()
    if not text:
        return ""
    # Drop leading attributes / doc comments already stripped by decl parser.
    # Prefer the part before `:=` / `where`, keep binders + type.
    for sep in (":=", " where\n", " where "):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
            break
    # Collapse whitespace but keep it reading like code.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text).strip()
    return text[:900]


def graph_from_lean(lean: str) -> dict[str, Any]:
    """Build a structural DAG from Lean declarations (no LLM)."""
    from agent_monitor.lean_verify import _decls_with_docs

    decls = _decls_with_docs(lean)
    if not decls:
        return {"title": "Lean proof", "nodes": [], "edges": []}
    lines = (lean or "").splitlines()
    kind_map = {
        "axiom": "assumption",
        "opaque": "assumption",
        "def": "definition",
        "abbrev": "definition",
        "structure": "definition",
        "inductive": "definition",
        "instance": "definition",
        "lemma": "lemma",
        "theorem": "claim",
        "example": "step",
    }
    main_idx = next(
        (i for i, d in enumerate(decls) if d.get("kind") == "theorem" and str(d.get("name") or "").lower() == "main"),
        None,
    )
    if main_idx is None:
        main_idx = next(
            (i for i in range(len(decls) - 1, -1, -1) if decls[i].get("kind") in {"theorem", "lemma"}),
            None,
        )

    nodes: list[dict[str, Any]] = []
    id_of: dict[str, str] = {}
    used_ids: set[str] = set()
    for i, d in enumerate(decls):
        raw_name = str(d.get("name") or "").strip() or f"{d.get('kind')}_{i+1}"
        nid = re.sub(r"[^A-Za-z0-9_]+", "_", raw_name) or f"n{i+1}"
        base, n = nid, 2
        while nid in used_ids:
            nid = f"{base}_{n}"
            n += 1
        used_ids.add(nid)
        if raw_name:
            id_of[raw_name] = nid
        start = max(0, int(d.get("line") or 1) - 1)
        end = decls[i + 1]["line"] - 1 if i + 1 < len(decls) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        lean_kind = str(d.get("kind") or "def")
        statement = _lean_sig(body) or f"{lean_kind} {raw_name}"
        cites = []
        for ln in (d.get("doc") or "").splitlines():
            m = re.match(r"(?i)^\s*cites?\s*[:：]\s*(.+)$", ln)
            if m:
                cites.append(m.group(1).strip()[:120])
        kind = kind_map.get(lean_kind, "step")
        if i == main_idx:
            kind = "conclusion"
        nodes.append(
            {
                "id": nid,
                "kind": kind,
                "lean_kind": lean_kind,
                "label": raw_name,
                "statement": statement,
                "citations": cites,
                "_body": body,
            }
        )

    edges: list[dict[str, str]] = []
    names = [(d.get("name") or "", id_of.get(d.get("name") or "")) for d in decls]
    for i, node in enumerate(nodes):
        body = node.pop("_body", "")
        for j in range(i):
            nm, src = names[j]
            if not nm or not src or len(nm) < 2:
                continue
            if re.search(r"\b" + re.escape(nm) + r"\b", body):
                edges.append({"from": src, "to": node["id"], "label": "uses"})
    title = nodes[main_idx]["label"] if main_idx is not None and main_idx < len(nodes) else "Lean proof"
    return _sanitize({"title": title, "nodes": nodes, "edges": edges})


def load_or_parse_formal(workspace: Path) -> dict[str, Any] | None:
    """Cached LLM formal graph, else a structural parse of Proof.lean."""
    cached = load_cached(workspace, kind="formal")
    if cached and cached.get("graph"):
        return cached
    lean = _lean_source(workspace)
    if not lean:
        return None
    graph = graph_from_lean(lean)
    if not graph.get("nodes"):
        return {"status": "none", "reason": "no Lean declarations yet"}
    return {
        "generated_at": None,
        "model": "lean-parse",
        "source": "Proof.lean",
        "kind": "formal",
        "status": "parsed",
        "graph": graph,
    }


def generate(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None = None,
    kind: str = "informal",
) -> dict[str, Any]:
    """Generate the proof graph via a single LLM call and cache it."""
    from agent_monitor.settings import _http_json, resolved_user_env

    kind = "formal" if str(kind or "").strip().lower() == "formal" else "informal"
    problem = ""
    prob_file = workspace / "problem.txt"
    if prob_file.exists():
        problem = prob_file.read_text(encoding="utf-8", errors="replace").strip()

    if kind == "formal":
        lean = _lean_source(workspace)
        if not lean:
            raise ValueError("No Proof.lean yet — run Lean Verification first")
        source = "Proof.lean"
        prompt = LEAN_GRAPH_PROMPT.replace("{problem}", problem[:6000]).replace("{lean}", lean[:60000])
        out_name = LEAN_GRAPH_FILENAME
    else:
        source, proof = _proof_source(workspace, run_record)
        if not proof:
            raise ValueError("No proof text yet — wait for the run to produce proof.md")
        prompt = GRAPH_PROMPT.replace("{problem}", problem[:6000]).replace("{proof}", proof[:60000])
        out_name = GRAPH_FILENAME

    env = resolved_user_env(user)

    use_codex = str(model or "").strip().lower() in {"codex", "codex-cli", "codex cli"}
    if not use_codex:
        try:
            base, key, chosen_model = _resolve_llm(env, model)
        except ValueError:
            # No direct API key on file — fall back to the connected Codex
            # account rather than failing outright, same preference order as
            # the main proving engine.
            if _codex_account_ready(user):
                use_codex = True
            else:
                raise

    if use_codex:
        chosen_model = "codex"
        content = _run_codex_prompt(user, workspace, prompt)
    else:
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
        "kind": kind,
        "graph": graph,
    }
    (workspace / out_name).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result
