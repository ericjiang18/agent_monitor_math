"""Proof-graph generation — distill a run's proof into a logical DAG.

A small/cheap LLM reads the problem plus the proof write-up and emits a
strict-JSON graph: nodes are logical units (assumptions, lemmas, claims,
steps, contradiction, conclusion) and edges are "supports" arrows from
premise to consequence. The result is cached in the run workspace as
``proof_graph.json`` so it only costs one model call per (re)generation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from agent_monitor import usage_ledger

GRAPH_FILENAME = "proof_graph.json"
LEAN_GRAPH_FILENAME = "lean_proof_graph.json"
MAX_GRAPH_CACHE_BYTES = 4_000_000
MAX_SOURCE_BYTES = 2_000_000
MAX_PROBLEM_BYTES = 1_000_000
MAX_GRAPH_NODES = 256
MAX_GRAPH_EDGES = 4096
MAX_GRAPH_ID_LENGTH = 160
MAX_LEAN_DECLARATIONS = 256

CLAUDE_DERIVED_TIMEOUT_ENV = "AGENT_MONITOR_CLAUDE_DERIVED_TIMEOUT"
DEFAULT_CLAUDE_DERIVED_TIMEOUT = 600.0
MIN_CLAUDE_DERIVED_TIMEOUT = 60.0
MAX_CLAUDE_DERIVED_TIMEOUT = 3_600.0


def _claude_derived_timeout(env: Mapping[str, str]) -> float:
    """Resolve one bounded response-only timeout without exposing its input."""
    raw = str(env.get(CLAUDE_DERIVED_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_CLAUDE_DERIVED_TIMEOUT
    try:
        requested = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{CLAUDE_DERIVED_TIMEOUT_ENV} must be a finite number of seconds "
            "greater than zero"
        ) from exc
    if not math.isfinite(requested) or requested <= 0:
        raise ValueError(
            f"{CLAUDE_DERIVED_TIMEOUT_ENV} must be a finite number of seconds "
            "greater than zero"
        )
    return max(
        MIN_CLAUDE_DERIVED_TIMEOUT,
        min(requested, MAX_CLAUDE_DERIVED_TIMEOUT),
    )

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


def _read_workspace_text(
    workspace: Path, name: str, *, limit: int
) -> str:
    parts = Path(name).parts
    if (
        not parts or Path(name).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return ""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = -1
    fd = -1
    try:
        directory = os.open(
            Path(workspace).resolve(),
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | nofollow,
        )
        for component in parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | nofollow,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_CLOEXEC | nofollow,
            dir_fd=directory,
        )
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            state = os.fstat(handle.fileno())
            if not stat.S_ISREG(state.st_mode) or state.st_size > limit:
                return ""
            raw = handle.read(limit + 1)
    except OSError:
        return ""
    finally:
        if fd >= 0:
            os.close(fd)
        if directory >= 0:
            os.close(directory)
    if len(raw) > limit:
        return ""
    return raw.decode(errors="replace")


def _proof_source(workspace: Path, run_record: dict[str, Any] | None) -> tuple[str, str]:
    """Return (source_label, proof_text). Prefers workspace write-ups."""
    for name in ("proof.md", "proof.tex"):
        text = _read_workspace_text(
            workspace, name, limit=MAX_SOURCE_BYTES
        ).strip()
        if text:
            return name, text
    # Fallback: the final agent output recorded on the run
    agents = (run_record or {}).get("agents") or []
    for a in reversed(agents):
        out = str(a.get("output") or "").strip()
        if len(out) > 200:
            return "final agent output", out[:MAX_SOURCE_BYTES]
    return "", ""


def _resolve_llm_provider(
    env: dict[str, str], model: str | None
) -> tuple[str, str, str, str]:
    """Resolve a selected model to its native provider, endpoint base and key."""
    requested = (
        model
        or env.get("AGENT_MONITOR_GRAPH_MODEL")
        or env.get("AGENT_MONITOR_MODEL")
        or ""
    ).strip()
    if not requested:
        if env.get("OPENAI_API_KEY"):
            requested = "gpt-5-mini-2025-08-07"
        elif env.get("ANTHROPIC_API_KEY"):
            requested = "claude-haiku-4-5"
        elif env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"):
            requested = "gemini-3.6-flash"
        elif env.get("DEEPSEEK_API_KEY"):
            requested = "deepseek-v4-flash"
        elif env.get("KIMI_API_KEY"):
            requested = "kimi-k3"
        elif env.get("XAI_API_KEY"):
            requested = "grok-4.5"
        elif any(
            env.get(name)
            for name in (
                "OPENROUTER_API_KEY",
                "GROQ_API_KEY",
                "TOGETHERAI_API_KEY",
            )
        ):
            raise ValueError(
                "No model selected; use an explicit provider:model id in Settings"
            )
        else:
            requested = "gpt-5-mini-2025-08-07"

    specs = {
        "openai": (
            env.get("OPENAI_API_KEY"),
            env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        ),
        "kimi": (
            env.get("KIMI_API_KEY"),
            env.get("KIMI_API_BASE")
            or "https://2qg3r7w8aefiukbds.flashflame.ai/v1",
        ),
        "anthropic": (
            env.get("ANTHROPIC_API_KEY"),
            env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
        ),
        "google": (
            env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"),
            env.get("GEMINI_API_BASE") or "https://generativelanguage.googleapis.com",
        ),
        "openrouter": (
            env.get("OPENROUTER_API_KEY"),
            env.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",
        ),
        "deepseek": (
            env.get("DEEPSEEK_API_KEY"),
            env.get("DEEPSEEK_API_BASE") or "https://api.deepseek.com",
        ),
        "groq": (
            env.get("GROQ_API_KEY"),
            env.get("GROQ_API_BASE") or "https://api.groq.com/openai/v1",
        ),
        "together": (
            env.get("TOGETHERAI_API_KEY"),
            env.get("TOGETHERAI_API_BASE") or "https://api.together.xyz/v1",
        ),
        "xai": (
            env.get("XAI_API_KEY"),
            env.get("XAI_API_BASE") or "https://api.x.ai/v1",
        ),
    }

    # A colon-qualified model is an explicit billing/provider route.  Do not
    # strip it before resolving: doing so lets another configured key win the
    # name-based fallback (for example ``deepseek:custom`` used to select
    # OpenRouter when both keys were present).  OpenRouter's native model ids
    # use ``vendor/model`` and remain intact; direct custom providers use an
    # unambiguous colon prefix instead.
    provider_aliases = {
        "openai": "openai",
        "kimi": "kimi",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "google": "google",
        "gemini": "google",
        "openrouter": "openrouter",
        "deepseek": "deepseek",
        "groq": "groq",
        "together": "together",
        "togetherai": "together",
        "together_ai": "together",
        "xai": "xai",
    }
    explicit_provider: str | None = None
    chosen = requested
    prefix, separator, remainder = requested.partition(":")
    colon_provider = (
        provider_aliases.get(prefix.strip().lower()) if separator else None
    )
    if colon_provider:
        chosen = remainder.strip()
        if not chosen:
            raise ValueError(f"Model id is empty after provider prefix {prefix!r}")
        explicit_provider = colon_provider

    lowered = chosen.lower()
    if explicit_provider:
        order = [explicit_provider]
    elif lowered.startswith("kimi-"):
        order = ["kimi"]
    elif lowered.startswith("claude-"):
        order = ["anthropic"]
    elif lowered.startswith("gemini-"):
        order = ["google"]
    elif lowered.startswith("deepseek-"):
        order = ["deepseek"]
    elif lowered.startswith("grok-"):
        order = ["xai"]
    elif "/" in chosen:
        order = ["openrouter"]
    elif lowered == "chat-latest" or lowered.startswith(("gpt-", "o1", "o3", "o4")):
        order = ["openai"]
    else:
        raise ValueError(
            f"Model {chosen} has no unambiguous API provider; "
            "use an explicit provider:model id in Settings"
        )

    for provider in order:
        key, base = specs[provider]
        clean_key = str(key or "").strip()
        if clean_key:
            return provider, str(base).strip().rstrip("/"), clean_key, chosen
    expected = {
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GEMINI_API_KEY or GOOGLE_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "groq": "GROQ_API_KEY",
        "together": "TOGETHERAI_API_KEY",
        "xai": "XAI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "kimi": "KIMI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }.get(order[0], "a matching provider API key")
    raise ValueError(
        f"Model {chosen} requires {expected}; add and verify it in Settings"
    )


def _resolve_llm(env: dict[str, str], model: str | None) -> tuple[str, str, str]:
    """Backward-compatible resolver returning (base_url, api_key, model)."""
    _provider, base, key, chosen = _resolve_llm_provider(env, model)
    return base, key, chosen


def _api_error(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or error)
        if error:
            return str(error)
        return str(body.get("message") or body)
    return str(body)


def _openai_response_text(body: dict[str, Any]) -> str:
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"output_text", "text"} and block.get("text"):
                parts.append(str(block["text"]))
    return "\n".join(parts)


def _call_llm(
    env: dict[str, str],
    model: str | None,
    prompt: str,
    *,
    timeout: int = 240,
) -> tuple[str, str, str]:
    """Call OpenAI, Anthropic, Gemini, or an OpenAI-compatible provider natively."""
    from agent_monitor.settings import _http_json

    if env.get("AGENT_MONITOR_GUEST_DERIVED") == "1":
        model = "kimi-k3"
    provider, base, key, chosen = _resolve_llm_provider(env, model)
    max_output = max(1024, min(int(env.get("AGENT_MONITOR_LLM_MAX_OUTPUT") or 32768), 128000))
    headers: dict[str, str]
    payload: dict[str, Any]
    if provider == "openai":
        url = f"{base}/responses"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": chosen,
            "input": prompt,
            "max_output_tokens": max_output,
            "store": False,
        }
        from agent_monitor.run_tuning import openai_payload_options

        payload.update(openai_payload_options(env, chosen))
    elif provider == "kimi":
        from agent_monitor import kimi_k3

        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = kimi_k3.chat_payload(
            user=prompt,
            env=env,
            max_tokens=max_output,
        )
    elif provider == "anthropic":
        api_base = base if base.endswith("/v1") else f"{base}/v1"
        url = f"{api_base}/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": chosen,
            "max_tokens": max_output,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif provider == "google":
        api_base = base if base.endswith("/v1beta") else f"{base}/v1beta"
        url = f"{api_base}/models/{chosen}:generateContent?key={key}"
        headers = {}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output},
        }
    else:
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": chosen,
            "messages": [{"role": "user", "content": prompt}],
        }

    def request() -> tuple[int, object]:
        return _http_json(
            url,
            method="POST",
            headers=headers,
            payload=payload,
            timeout=timeout,
        )

    if provider == "kimi":
        from agent_monitor import kimi_k3

        code, body = kimi_k3.request_with_retry(request)
    else:
        code, body = request()
    if code != 200 or not isinstance(body, dict):
        detail = str(_api_error(body) or "unknown error")
        if key:
            detail = detail.replace(key, "[redacted]")
        raise ValueError(
            f"{provider.title()} model call failed for {chosen} (HTTP {code}): "
            f"{detail[-1600:]}"
        )
    from agent_monitor import usage_ledger

    usage_ledger.record_usage(
        model=str(body.get("model") or chosen),
        usage=usage_ledger.http_usage(provider, body),
        billing="sponsored" if provider == "kimi" and env.get("AGENT_MONITOR_GUEST_DERIVED") == "1" else None,
    )
    try:
        if provider == "openai":
            content = _openai_response_text(body)
        elif provider == "kimi":
            from agent_monitor import kimi_k3

            content = kimi_k3.response_text(body)
        elif provider == "anthropic":
            content = "\n".join(
                str(block.get("text") or "")
                for block in body.get("content") or []
                if isinstance(block, dict) and block.get("type") == "text"
            )
        elif provider == "google":
            content = "\n".join(
                str(part.get("text") or "")
                for candidate in body.get("candidates") or []
                if isinstance(candidate, dict)
                for part in ((candidate.get("content") or {}).get("parts") or [])
                if isinstance(part, dict) and part.get("text")
            )
        else:
            content = str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(
            f"{provider.title()} returned an unexpected response shape for {chosen}"
        ) from exc
    if not content.strip():
        raise ValueError(f"{provider.title()} returned an empty response for {chosen}")
    from agent_monitor.lean_verify import _ObservedModel

    return _ObservedModel(chosen, body.get("model")), content, provider

_AMBIGUOUS_LATEX_ESCAPE = re.compile(
    r'\\u(?![0-9A-Fa-f]{4})'
    r'|\\(?:bar|begin|beta|big(?:l|r)?|Big(?:l|r)?|binom|bmod|boldsymbol|boxed|bra|brack'
    r'|forall|frac'
    r'|nabla|natural|ne|neg|neq|nexists|nleq|nmid|not|notin|nu'
    r'|rangle|rceil|ref|rfloor|rho|right|rightarrow|rm|root'
    r'|tan|tau|text|tfrac|therefore|theta|times|to|top|triangle)\b'
    r'|\\(?!["\\/bfnrtu])'
)


def _repair_latex_json_escapes(blob: str) -> str:
    """Escape unpaired TeX slashes inside JSON strings.

    Already valid ``\\alpha`` JSON must stay untouched. A global regex can
    accidentally match the second slash in that pair and turn it into three
    slashes, producing ``Invalid \\escape``. Count each slash run instead:
    even runs are already paired; only an odd final slash is considered for
    TeX repair. Structural JSON defects remain errors.
    """
    out: list[str] = []
    index = 0
    in_string = False
    while index < len(blob):
        char = blob[index]
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        if char == '"':
            out.append(char)
            in_string = False
            index += 1
            continue
        if char != "\\":
            out.append(char)
            index += 1
            continue

        end = index
        while end < len(blob) and blob[end] == "\\":
            end += 1
        slash_count = end - index
        following = blob[end] if end < len(blob) else ""
        if slash_count % 2 == 0:
            out.append("\\" * slash_count)
            index = end
            continue

        candidate = "\\" + blob[end:]
        needs_tex_escape = bool(_AMBIGUOUS_LATEX_ESCAPE.match(candidate))
        if needs_tex_escape:
            out.append("\\" * (slash_count + 1))
            index = end
        elif following in {'"', '/'} or following in "bfnrt":
            out.append("\\" * slash_count + following)
            index = end + 1
        elif following == "u" and re.match(r"u[0-9A-Fa-f]{4}", blob[end : end + 5]):
            out.append("\\" * slash_count + blob[end : end + 5])
            index = end + 5
        else:
            out.append("\\" * (slash_count + 1))
            index = end
    return "".join(out)


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
    # Models routinely emit LaTeX without JSON-escaping backslashes. Commands
    # such as `\forall` and `\neq` are especially dangerous: JSON accepts
    # their leading `\f` / `\n` and silently produces control characters,
    # so an exception-only retry is too late. The repair regex is intentionally
    # limited to known TeX commands and preserves legitimate escapes such as a
    # newline followed by `next`. Malformed commas/braces still fail normally.
    return json.loads(_repair_latex_json_escapes(blob))


def _sanitize(graph: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(graph, dict):
        raise ValueError("model returned a non-object graph")
    raw_nodes = graph.get("nodes") or []
    raw_edges = graph.get("edges") or []
    if not isinstance(raw_nodes, list) or len(raw_nodes) > MAX_GRAPH_NODES:
        raise ValueError("proof graph has too many or invalid nodes")
    if not isinstance(raw_edges, list) or len(raw_edges) > MAX_GRAPH_EDGES:
        raise ValueError("proof graph has too many or invalid edges")
    nodes = []
    seen: set[str] = set()
    for n in raw_nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "").strip()
        if not nid or len(nid) > MAX_GRAPH_ID_LENGTH or nid in seen:
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
    edge_seen: set[tuple[str, str, str]] = set()
    adjacency: dict[str, set[str]] = {nid: set() for nid in ids}

    def reaches(start: str, target: str) -> bool:
        pending = [start]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(adjacency.get(current, ()))
        return False

    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        src, dst = str(e.get("from") or ""), str(e.get("to") or "")
        label = str(e.get("label") or "")[:40]
        if (
            src in ids
            and dst in ids
            and src != dst
            and (src, dst, label) not in edge_seen
            and not reaches(dst, src)
        ):
            edge_seen.add((src, dst, label))
            adjacency[src].add(dst)
            edges.append({"from": src, "to": dst, "label": label})
    if not nodes:
        raise ValueError("model returned an empty graph")
    return {"title": str(graph.get("title") or "")[:200], "nodes": nodes, "edges": edges}


_CLAUDE_ACCOUNT_MODELS = {"sonnet", "opus", "fable", "claude-haiku-4-5"}
_CLAUDE_DAG_SYSTEM = (
    "You are a mathematical proof analyst. Follow the requested strict JSON "
    "schema exactly and return JSON only, without markdown fences or commentary."
)
_CODEX_DAG_SYSTEM = (
    "You are a mathematical proof analyst. Follow the requested strict JSON "
    "schema exactly and return JSON only, without markdown fences or commentary."
)


def _claude_account_ready(user: dict[str, Any] | None) -> bool:
    if not user or user.get("id") is None:
        return False
    from agent_monitor import claude_login

    return claude_login.account_login_ready(claude_login.account_home(user["id"]))


def _run_claude_prompt(
    user: dict[str, Any] | None,
    prompt: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
    source_env: dict[str, str] | None = None,
) -> tuple[str, str]:
    from agent_monitor.jobs import _configure_auth_route
    from agent_monitor.runners.claude_backend import ClaudeBackendError, claude_exec
    from agent_monitor.settings import resolved_user_env

    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env(
        extra={
            **{key: value for key, value in (source_env or {}).items() if value},
            **{k: v for k, v in (resolved_user_env(user) or {}).items() if v},
        }
    )
    env = _configure_auth_route(
        env,
        route="claude_subscription",
        user_id=(user or {}).get("id"),
    )
    requested = str(model or "").strip().lower()
    selected = requested if requested in _CLAUDE_ACCOUNT_MODELS else "sonnet"
    effective_timeout = (
        _claude_derived_timeout(env) if timeout is None else timeout
    )
    try:
        result = claude_exec(
            _CLAUDE_DAG_SYSTEM,
            prompt,
            model=selected,
            timeout=effective_timeout,
            source_env=env,
        )
    except ClaudeBackendError as exc:
        raise ValueError(str(exc)) from exc
    from agent_monitor import usage_ledger

    usage_ledger.record_usage(
        model=str(result.model or selected),
        usage=usage_ledger.cli_usage(getattr(result, "usage", None), input_includes_cache=False),
        billing="subscription",
        cost_usd=getattr(result, "cost_usd", None),
    )
    content = str(result.text or "").strip()
    if not content:
        raise ValueError("Claude Code returned an empty proof graph response")
    return str(result.model or selected), content


def _codex_account_ready(user: dict[str, Any] | None) -> bool:
    if not user or user.get("id") is None:
        return False
    from agent_monitor import codex_login

    return codex_login.account_login_ready(codex_login.account_home(user["id"]))


def _run_codex_prompt_env(
    env: dict[str, str],
    workspace: Path,
    prompt: str,
    *,
    model: str | None = None,
    timeout: int = 240,
) -> str:
    """One-shot response pinned to the configured Codex subscription env."""
    from agent_monitor.runners.codex_backend import (
        CodexBackendError,
        codex_subscription_exec,
    )

    scratch = workspace / ".graph_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    requested = str(model or "").strip()
    if requested.lower() in {"codex", "codex-cli", "codex cli", "codex subscription"}:
        requested = ""
    try:
        result = codex_subscription_exec(
            _CODEX_DAG_SYSTEM,
            prompt,
            source_env=env,
            model=requested or None,
            timeout=timeout,
            workspace=scratch,
        )
    except CodexBackendError as exc:
        raise ValueError(str(exc)) from exc
    from agent_monitor import usage_ledger

    usage_ledger.record_usage(
        model=requested or "codex",
        usage=usage_ledger.cli_usage(getattr(result, "usage", None), input_includes_cache=True),
        billing="subscription",
    )
    return str(result.text or "").strip()


def _run_codex_prompt(
    user: dict[str, Any] | None,
    workspace: Path,
    prompt: str,
    *,
    model: str | None = None,
    owner_id: int | None = None,
    timeout: int = 240,
    source_env: dict[str, str] | None = None,
) -> str:
    """One-shot answer billed only to the caller's connected Codex account."""
    from agent_monitor.jobs import _configure_auth_route
    from agent_monitor.settings import resolved_user_env

    session_owner = (user or {}).get("id")
    if owner_id is not None and session_owner is not None:
        if int(owner_id) != int(session_owner):
            raise ValueError("The run owner does not match the signed-in account")
    route_owner = owner_id if owner_id is not None else session_owner
    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env(
        extra={
            **{key: value for key, value in (source_env or {}).items() if value},
            **{k: v for k, v in (resolved_user_env(user) or {}).items() if v},
        }
    )
    env = _configure_auth_route(
        env,
        route="codex_subscription",
        user_id=int(route_owner) if route_owner is not None else None,
    )
    return _run_codex_prompt_env(
        env, workspace, prompt, model=model, timeout=timeout
    )


def _sanitize_cached_graph(raw: Any) -> dict[str, Any]:
    graph = _sanitize(raw)
    provenance = raw.get("provenance") if isinstance(raw, dict) else None
    if isinstance(provenance, dict):
        clean: dict[str, Any] = {}
        for key, limit in (
            ("kind", 40), ("producer", 80), ("toolchain", 200),
            ("mathlib_fingerprint", 64),
        ):
            clean[key] = str(provenance.get(key) or "")[:limit]
        for key in (
            "matched_declarations", "proof_edges", "statement_edges",
            "omitted_proof_edges", "omitted_statement_edges",
            "dependency_truncated_count",
        ):
            value = provenance.get(key, 0)
            clean[key] = (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= MAX_GRAPH_EDGES
                else 0
            )
        graph["provenance"] = clean
    return graph


def load_cached(workspace: Path, *, kind: str = "informal") -> dict[str, Any] | None:
    name = LEAN_GRAPH_FILENAME if str(kind).lower() == "formal" else GRAPH_FILENAME
    p = Path(workspace).resolve() / name
    try:
        fd = os.open(
            p,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(fd, "rb") as handle:
            state = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(state.st_mode)
                or state.st_size > MAX_GRAPH_CACHE_BYTES
            ):
                return None
            raw = handle.read(MAX_GRAPH_CACHE_BYTES + 1)
        if len(raw) > MAX_GRAPH_CACHE_BYTES:
            return None
        value = json.loads(raw.decode())
    except (
        json.JSONDecodeError, OSError, UnicodeDecodeError, RecursionError,
        ValueError,
    ):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("graph"), dict):
        return None
    try:
        value = dict(value)
        value["graph"] = _sanitize_cached_graph(value["graph"])
        if "compiler_graph" in value:
            value["compiler_graph"] = _sanitize_cached_graph(
                value["compiler_graph"]
            )
    except ValueError:
        return None
    return value


def _write_cached(
    workspace: Path, name: str, result: dict[str, Any]
) -> None:
    root = Path(workspace).resolve()
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if len(payload.encode()) > MAX_GRAPH_CACHE_BYTES:
        raise ValueError("proof graph cache exceeds its size limit")
    fd, raw = tempfile.mkstemp(prefix=".proof-graph-", suffix=".tmp", dir=root)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, root / name)
    finally:
        tmp.unlink(missing_ok=True)


def _lean_source(workspace: Path) -> str:
    return _read_workspace_text(
        workspace, "lean/Proof.lean", limit=MAX_SOURCE_BYTES
    )


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


def graph_from_lean(
    lean: str, *, kg_snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a structural DAG, preferring exact compiler dependencies.

    The optional snapshot is observational metadata produced by lean_kg. A
    missing/invalid snapshot retains the legacy
    text parser, and metadata can never change Lean verification status.
    """
    from agent_monitor import lean_kg
    from agent_monitor.lean_verify import _decls_with_docs

    if len((lean or "").encode()) > MAX_SOURCE_BYTES:
        raise ValueError("Lean source exceeds the Formal DAG limit")
    decls = _decls_with_docs(lean)
    if len(decls) > MAX_LEAN_DECLARATIONS:
        raise ValueError("Lean source has too many declarations for the DAG")
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
    used_ids: set[str] = set()
    for i, d in enumerate(decls):
        raw_name = str(d.get("name") or "").strip() or f"{d.get('kind')}_{i+1}"
        nid = re.sub(r"[^A-Za-z0-9_]+", "_", raw_name) or f"n{i+1}"
        base, n = nid, 2
        while nid in used_ids:
            nid = f"{base}_{n}"
            n += 1
        used_ids.add(nid)
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
    names = [
        (str(declaration.get("name") or ""), nodes[index]["id"])
        for index, declaration in enumerate(decls)
    ]
    raw_name_counts: dict[str, int] = {}
    for raw_name, _node_id in names:
        if raw_name:
            raw_name_counts[raw_name] = raw_name_counts.get(raw_name, 0) + 1
    node_by_line = {
        int(decl.get("line") or 0): nodes[index]["id"]
        for index, decl in enumerate(decls)
    }
    index_by_node = {node["id"]: index for index, node in enumerate(nodes)}
    compiler_targets: set[str] = set()
    qualified_to_node: dict[str, str] = {}
    kg_ok = bool(
        isinstance(kg_snapshot, dict)
        and kg_snapshot.get("schema_version") == lean_kg.SCHEMA_VERSION
        and kg_snapshot.get("producer") == lean_kg.PRODUCER
        and kg_snapshot.get("status") == "ok"
        and kg_snapshot.get("sandbox") in lean_kg.TRUSTED_SANDBOXES
        and kg_snapshot.get("source_sha256")
        == hashlib.sha256(lean.encode()).hexdigest()
        and kg_snapshot.get("normalized_source_sha256")
        == lean_kg.normalized_source_sha256(lean)
    )
    kg_declarations = (
        kg_snapshot.get("declarations") or [] if kg_ok else []
    )
    for item in kg_declarations:
        if not isinstance(item, dict) or not item.get("matched"):
            continue
        target = node_by_line.get(int(item.get("line") or 0))
        qualified = str(item.get("qualified_name") or "")
        if target and qualified:
            compiler_targets.add(target)
            qualified_to_node[qualified] = target
    for item in kg_declarations:
        if not isinstance(item, dict) or not item.get("matched"):
            continue
        target = node_by_line.get(int(item.get("line") or 0))
        if not target:
            continue
        proof_uses = sorted(
            {
                str(name)
                for name in (item.get("local_proof_uses") or [])
                if str(name).strip()
            }
        )
        statement_mentions = sorted(
            {
                str(name)
                for name in (item.get("local_statement_mentions") or [])
                if str(name).strip()
            }
        )
        linked: set[tuple[str, str]] = set()
        for dependency in proof_uses:
            source = qualified_to_node.get(dependency)
            edge_key = (source or "", "proof uses")
            if (
                source
                and index_by_node[source] < index_by_node[target]
                and edge_key not in linked
            ):
                edges.append(
                    {"from": source, "to": target, "label": "proof uses"}
                )
                linked.add(edge_key)
        for dependency in statement_mentions:
            source = qualified_to_node.get(dependency)
            edge_key = (source or "", "type mentions")
            if (
                source
                and index_by_node[source] < index_by_node[target]
                and edge_key not in linked
            ):
                edges.append(
                    {"from": source, "to": target, "label": "type mentions"}
                )
                linked.add(edge_key)

    for i, node in enumerate(nodes):
        body = node.pop("_body", "")
        if node["id"] in compiler_targets:
            continue
        linked_fallback: set[str] = set()
        current_name = names[i][0]
        for j in range(i):
            nm, src = names[j]
            if (
                not nm
                or not src
                or len(nm) < 2
                or raw_name_counts.get(nm, 0) != 1
                or nm == current_name
                or src in linked_fallback
            ):
                continue
            if re.search(r"\b" + re.escape(nm) + r"\b", body):
                edges.append({"from": src, "to": node["id"], "label": "uses"})
                linked_fallback.add(src)
    title = nodes[main_idx]["label"] if main_idx is not None and main_idx < len(nodes) else "Lean proof"
    graph = _sanitize({"title": title, "nodes": nodes, "edges": edges})
    if compiler_targets:
        retained_proof_edges = sum(
            edge.get("label") == "proof uses" for edge in graph["edges"]
        )
        retained_statement_edges = sum(
            edge.get("label") == "type mentions" for edge in graph["edges"]
        )
        graph["provenance"] = {
            "kind": "lean-compiler",
            "producer": str(kg_snapshot.get("producer") or "")[:80],
            "toolchain": str(kg_snapshot.get("toolchain") or "")[:200],
            "mathlib_fingerprint": str(
                kg_snapshot.get("mathlib_fingerprint") or ""
            )[:64],
            "matched_declarations": len(compiler_targets),
            "proof_edges": retained_proof_edges,
            "statement_edges": retained_statement_edges,
            "omitted_proof_edges": max(
                0,
                int(kg_snapshot.get("proof_edge_count") or 0)
                - retained_proof_edges,
            ),
            "omitted_statement_edges": max(
                0,
                int(kg_snapshot.get("statement_edge_count") or 0)
                - retained_statement_edges,
            ),
            "dependency_truncated_count": int(
                kg_snapshot.get("dependency_truncated_count") or 0
            ),
        }
    return graph


def graph_from_text(text: str) -> dict[str, Any]:
    """Deterministic fallback used by the automatic derived-tool pipeline.

    It deliberately avoids launching a second agent harness when a direct DAG
    model is unavailable. The result is structural rather than semantic, but it
    keeps the selected engine as the run's sole harness.
    """
    src = str(text or "").strip()
    if not src:
        raise ValueError("proof text is empty")
    blocks = [
        re.sub(r"(?m)^#{1,6}\s+", "", part).strip()
        for part in re.split(r"\n\s*\n+", src)
        if part.strip()
    ]
    if len(blocks) < 2:
        blocks = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+(?=[A-Z])", src)
            if part.strip()
        ]
    blocks = [block for block in blocks if len(block) > 12][:16] or [src[:4000]]
    nodes: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        plain = re.sub(r"[#*_~`]+", " ", block)
        plain = re.sub(r"\s+", " ", plain).strip()
        lowered = plain.lower()
        if index == len(blocks) - 1:
            kind = "conclusion"
        elif index == 0 and any(word in lowered for word in ("assume", "given", "let ", "suppose")):
            kind = "assumption"
        elif any(word in lowered for word in ("lemma", "claim", "we show", "it remains")):
            kind = "claim"
        elif "contradict" in lowered:
            kind = "contradiction"
        else:
            kind = "step"
        label_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", plain)[:8]
        nodes.append(
            {
                "id": f"n{index + 1}",
                "kind": kind,
                "label": " ".join(label_words)[:100] or f"Step {index + 1}",
                "statement": plain[:1500],
                "citations": ["structural parse"],
            }
        )
    edges = [
        {"from": nodes[index - 1]["id"], "to": nodes[index]["id"], "label": "supports"}
        for index in range(1, len(nodes))
    ]
    return _sanitize({"title": nodes[-1]["label"], "nodes": nodes, "edges": edges})


def _compiler_snapshot_for_verified_source(
    workspace: Path, lean: str
) -> dict[str, Any] | None:
    """Load/extract KG only for the exact kernel-checked workspace source."""
    from agent_monitor import lean_kg, lean_verify

    checked = lean_verify.load_cached(workspace) or {}
    if str(checked.get("status") or "") not in {"verified", "unfaithful"}:
        return None
    recorded = str(checked.get("lean") or "")
    if lean not in {recorded, recorded.rstrip() + "\n"}:
        return None
    if str(checked.get("verified_source_sha256") or "") != hashlib.sha256(
        lean.encode()
    ).hexdigest():
        return None
    checked_toolchain = str(
        ((checked.get("toolchain") or {}).get("version") or "")
        if isinstance(checked.get("toolchain"), dict)
        else ""
    )
    if not checked_toolchain:
        return None
    toolchain = checked_toolchain
    snapshot = lean_kg.load_snapshot(
        workspace, source=lean, toolchain=toolchain
    )
    if snapshot is None:
        snapshot = lean_kg.extract_snapshot(
            workspace,
            source=lean,
            uses_mathlib=bool(checked.get("uses_mathlib")),
        )
    return snapshot


def load_or_parse_formal(workspace: Path) -> dict[str, Any] | None:
    """Cached current graph, else compiler-derived or structural Formal DAG."""
    from agent_monitor import lean_kg

    lean = _lean_source(workspace)
    if not lean.strip():
        return None
    source_hash = lean_kg.normalized_source_sha256(lean)
    cached = load_cached(workspace, kind="formal")
    if (
        cached
        and cached.get("graph")
        and cached.get("source_sha256") == source_hash
    ):
        result = dict(cached)
        result.pop("compiler_graph", None)
        result.pop("knowledge_graph", None)
        result.pop("provenance", None)
        cached_graph = result.get("graph")
        cached_provenance = (
            cached_graph.get("provenance")
            if isinstance(cached_graph, dict)
            else None
        )
        compiler_edge_labels = {"proof uses", "type mentions"}
        cached_was_compiler = bool(
            str(result.get("model") or "") == "lean-compiler"
            or (
                isinstance(cached_provenance, dict)
                and (
                    cached_provenance.get("kind") == "lean-compiler"
                    or str(cached_provenance.get("producer") or "").startswith(
                        "agent-monitor-lean-env-"
                    )
                )
            )
            or any(
                str(edge.get("label") or "") in compiler_edge_labels
                for edge in ((cached_graph or {}).get("edges") or [])
                if isinstance(edge, dict)
            )
        )
        if isinstance(cached_graph, dict):
            untrusted_graph = dict(cached_graph)
            untrusted_graph.pop("provenance", None)
            result["graph"] = untrusted_graph
        snapshot = _compiler_snapshot_for_verified_source(workspace, lean)
        result["knowledge_graph"] = lean_kg.snapshot_summary(snapshot)
        refreshed_graph = graph_from_lean(lean, kg_snapshot=snapshot)
        if cached_was_compiler:
            result["graph"] = refreshed_graph
            result["model"] = (
                "lean-compiler"
                if refreshed_graph.get("provenance")
                else "lean-parse"
            )
        elif refreshed_graph.get("provenance"):
            result["compiler_graph"] = refreshed_graph
        return result
    snapshot = _compiler_snapshot_for_verified_source(workspace, lean)
    graph = graph_from_lean(lean, kg_snapshot=snapshot)
    if not graph.get("nodes"):
        return {"status": "none", "reason": "no Lean declarations yet"}
    kg_summary = lean_kg.snapshot_summary(snapshot)
    compiler_graph = bool(graph.get("provenance"))
    return {
        "generated_at": None,
        "model": "lean-compiler" if compiler_graph else "lean-parse",
        "source": "Proof.lean",
        "source_sha256": source_hash,
        "kind": "formal",
        "status": "parsed",
        "knowledge_graph": kg_summary,
        "graph": graph,
    }


@usage_ledger.attributed("dag")
def generate(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None = None,
    kind: str = "informal",
    allow_codex_fallback: bool = True,
) -> dict[str, Any]:
    """Generate and cache a proof graph.

    Manual generation may use the connected Codex account. The automatic
    cooperative pipeline disables that fallback so it never launches a hidden
    second harness when another engine was selected.
    """
    from agent_monitor.settings import resolved_user_env

    kind = "formal" if str(kind or "").strip().lower() == "formal" else "informal"
    problem = ""
    problem = _read_workspace_text(
        workspace, "problem.txt", limit=MAX_PROBLEM_BYTES
    ).strip()

    if kind == "formal":
        lean = _lean_source(workspace)
        if not lean.strip():
            raise ValueError("No Proof.lean yet — run Lean Verification first")
        source = "Proof.lean"
        structural_source = lean
        prompt = LEAN_GRAPH_PROMPT.replace("{problem}", problem[:6000]).replace("{lean}", lean[:60000])
        out_name = LEAN_GRAPH_FILENAME
    else:
        source, proof = _proof_source(workspace, run_record)
        if not proof:
            raise ValueError("No proof text yet — wait for the run to produce proof.md")
        structural_source = proof
        prompt = GRAPH_PROMPT.replace("{problem}", problem[:6000]).replace("{proof}", proof[:60000])
        out_name = GRAPH_FILENAME

    is_guest = bool((user or {}).get("guest") or (run_record or {}).get("guest"))
    if is_guest:
        from agent_monitor.lean_verify import _llm_environment

        env = _llm_environment(user, run_record)
        model = "kimi-k3"
        allow_codex_fallback = False
    else:
        env = resolved_user_env(user)
    if (
        not is_guest
        and str((run_record or {}).get("credential_source") or "").strip()
        == "sponsored_kimi"
    ):
        from agent_monitor import sponsored_kimi_client

        owner_id = (user or {}).get("id") or (run_record or {}).get("owner_id")
        cache_key = str((run_record or {}).get("run_id") or workspace.name)
        env.update(
            sponsored_kimi_client.issue_credentials(
                engine="formal",
                client_id=owner_id,
                cache_key=cache_key,
                minimum_ttl_seconds=600,
            )
        )
    from agent_monitor.run_tuning import from_record as apply_recorded_tuning

    if not is_guest:
        apply_recorded_tuning(env, run_record)
    graph: dict[str, Any] | None = None
    compiler_snapshot: dict[str, Any] | None = None
    used_structural_fallback = False
    chosen_model = ""
    content = ""
    from agent_monitor.jobs import _route_from_record

    recorded_route = "api_key" if is_guest else _route_from_record(run_record)
    requested = str(model or "").strip().lower()
    requested_codex = requested in {"codex", "codex-cli", "codex cli"}
    requested_claude = requested in _CLAUDE_ACCOUNT_MODELS
    use_codex = recorded_route == "codex_subscription" or (
        requested_codex and allow_codex_fallback
    )
    use_claude = recorded_route == "claude_subscription"
    if recorded_route is None and not use_claude:
        from agent_monitor.settings import claude_enabled

        use_claude = (
            (requested_claude or (allow_codex_fallback and claude_enabled(user, env=env)))
            and _claude_account_ready(user)
        )

    if use_claude:
        chosen_model, content = _run_claude_prompt(
            user,
            prompt,
            model=model or str((run_record or {}).get("model") or "").strip() or None,
            source_env=env,
        )
    elif not use_codex:
        try:
            chosen_model, content, _provider = _call_llm(
                env, model, prompt, timeout=180
            )
        except ValueError:
            if allow_codex_fallback:
                # Manual DAG generation may use the user's connected Codex
                # account, subject to their preference.
                from agent_monitor.settings import codex_enabled

                if codex_enabled(user, env=env) and _codex_account_ready(user):
                    use_codex = True
                else:
                    raise
            else:
                used_structural_fallback = True
                chosen_model = "lean-parse" if kind == "formal" else "structural-parse"
                if kind == "formal":
                    compiler_snapshot = _compiler_snapshot_for_verified_source(
                        workspace, structural_source
                    )
                    graph = graph_from_lean(
                        structural_source, kg_snapshot=compiler_snapshot
                    )
                else:
                    graph = graph_from_text(structural_source)

    if use_codex:
        codex_model = str(
            model or (run_record or {}).get("model") or ""
        ).strip() or None
        chosen_model = codex_model or "codex"
        content = _run_codex_prompt(
            user,
            workspace,
            prompt,
            model=codex_model,
            owner_id=(run_record or {}).get("owner_id"),
            source_env=env,
        )

    if graph is None:
        graph = _sanitize(_extract_json(str(content)))
    compiler_graph: dict[str, Any] | None = None
    kg_summary: dict[str, Any] | None = None
    if kind == "formal":
        from agent_monitor import lean_kg

        if compiler_snapshot is None:
            compiler_snapshot = _compiler_snapshot_for_verified_source(
                workspace, structural_source
            )
        kg_summary = lean_kg.snapshot_summary(compiler_snapshot)
        compiler_graph = graph_from_lean(
            structural_source, kg_snapshot=compiler_snapshot
        )
        if (
            used_structural_fallback
            and compiler_graph.get("provenance")
        ):
            graph = compiler_graph
            chosen_model = "lean-compiler"
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": chosen_model,
        "source": source,
        "source_sha256": hashlib.sha256(
            structural_source.strip().encode()
        ).hexdigest(),
        "kind": kind,
        "graph": graph,
    }
    if kg_summary is not None:
        result["knowledge_graph"] = kg_summary
    if (
        compiler_graph is not None
        and compiler_graph.get("provenance")
        and compiler_graph is not graph
    ):
        result["compiler_graph"] = compiler_graph
    _write_cached(workspace, out_name, result)
    return result
