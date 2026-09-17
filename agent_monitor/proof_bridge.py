"""Conservative cooperation bridge between the informal DAG and Lean proof.

The selected engine remains the run's only agent harness. DAG extraction and
Lean checking are derived tools. This module turns their shared artifacts into
coverage annotations; it never claims formal verification unless the Lean
kernel result and the fidelity audit both pass.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

FILENAME = "proof_coordination.json"
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "if", "in", "is", "it", "let", "of", "on", "or", "that", "the",
    "then", "this", "to", "using", "we", "with",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_cached(workspace: Path) -> dict[str, Any] | None:
    value = _read_json(workspace / FILENAME)
    if not value:
        return None
    recorded = str(value.get("source_revision") or "")
    if recorded and recorded != _source_revision(workspace):
        value["status"] = "stale"
        value["formal_verified"] = False
        value["trust_reason"] = "The informal or formal proof changed after coverage was computed"
        for item in value.get("items") or []:
            if isinstance(item, dict) and item.get("status") == "verified":
                item["status"] = "stale"
        summary = dict(value.get("summary") or {})
        summary["verified"] = 0
        value["summary"] = summary
    return value


def _tokens(value: str) -> set[str]:
    # Split Lean camelCase names before normalizing so informal names such as
    # "upper bound" can match `upperBound` conservatively.
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", value.lower().replace("_", " "))
    normalized = {
        (
            "force" if word in {"forced", "forces", "forcing"}
            else "delete" if word in {"deleted", "deletes", "deleting", "deletion"}
            else word
        )
        for word in words
        if word not in _STOPWORDS
    }
    return normalized


def _node_text(node: dict[str, Any]) -> str:
    return " ".join(
        [
            str(node.get("label") or ""),
            str(node.get("statement") or ""),
            " ".join(str(x) for x in (node.get("citations") or [])),
        ]
    )


def _match_score(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, int]:
    a, b = _tokens(_node_text(left)), _tokens(_node_text(right))
    common = len(a & b)
    if not a or not b or not common:
        return 0.0, 0
    return (2.0 * common / (len(a) + len(b))), common


def _label_match_score(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, int]:
    """Prefer an exact theorem/lemma label over incidental body vocabulary."""
    a = _tokens(str(left.get("label") or ""))
    b = _tokens(str(right.get("label") or ""))
    common = len(a & b)
    if not a or not b or not common:
        return 0.0, 0
    return (2.0 * common / (len(a) + len(b))), common


def _distinctive_numeric_tokens(value: str) -> set[str]:
    """Multi-digit integers that can strengthen an otherwise near-exact label."""
    return set(re.findall(r"(?<![A-Za-z0-9])\d{2,}(?![A-Za-z0-9])", str(value or "")))


def _exact_indexed_value_signature(value: str) -> tuple[str, str, str] | None:
    """Normalize an exact informal ``n_i = j`` or formal ``Nk i j`` claim."""
    text = str(value or "")
    informal = re.search(
        r"\bn\s*_\s*\{?\s*(\d+)\s*\}?\s*=\s*(\d+)(?!\d)",
        text,
        flags=re.IGNORECASE,
    )
    if informal:
        return "nk", informal.group(1), informal.group(2)
    formal = re.search(r"\bNk\s+(\d+)\s+(\d+)\b", text)
    if formal:
        return "nk", formal.group(1), formal.group(2)
    return None


def _edge_deletion_three_coloring_signature(value: str) -> bool:
    """Recognize only the exact delete-edge/3-color/same-endpoint biconditional."""
    text = str(value or "")
    lowered = text.casefold()
    tokens = _tokens(text)
    return bool(
        {"delete", "edge"} <= tokens
        and re.search(r"(?<!\d)3(?!\d)", text)
        and any(word.startswith("color") for word in tokens)
        and "same" in tokens
        and ("iff" in tokens or "if and only if" in lowered or "↔" in text)
    )


def _partial_kernel_declarations(workspace: Path, lean: dict[str, Any]) -> set[str]:
    """Return admission-independent results in a successfully checked file."""
    if str(lean.get("status") or "") != "incomplete":
        return set()
    attempts = [a for a in (lean.get("attempts") or []) if isinstance(a, dict)]
    if attempts:
        check = attempts[-1].get("check")
        if not isinstance(check, dict) or not check.get("ok"):
            return set()
    source = str(lean.get("lean") or "")
    if not source:
        try:
            source = (workspace / "lean" / "Proof.lean").read_text(encoding="utf-8")
        except OSError:
            return set()

    from agent_monitor.lean_verify import _decls_with_docs, _sorry_lines
    from agent_monitor.proof_graph import graph_from_lean

    decls = _decls_with_docs(source)
    if not decls:
        return set()
    lines = source.splitlines()
    gap_lines = set(_sorry_lines(source))
    bad: set[str] = set()
    for index, decl in enumerate(decls):
        name = str(decl.get("name") or "")
        start = int(decl.get("line") or 1)
        if index + 1 < len(decls):
            end = int(decls[index + 1].get("line") or len(lines) + 1) - 1
        else:
            end = len(lines)
        header = lines[start - 1] if 0 < start <= len(lines) else ""
        if (
            any(start <= line <= end for line in gap_lines)
            or str(decl.get("kind") or "") in {"axiom", "opaque"}
            or bool(re.search(r"\bunsafe\b", header))
        ):
            bad.add(name)

    graph = graph_from_lean(source)
    labels = {
        str(node.get("id") or ""): str(node.get("label") or "")
        for node in graph.get("nodes") or []
    }
    dependencies: dict[str, set[str]] = {}
    for edge in graph.get("edges") or []:
        source_name = labels.get(str(edge.get("from") or ""), "")
        target_name = labels.get(str(edge.get("to") or ""), "")
        if source_name and target_name:
            dependencies.setdefault(target_name, set()).add(source_name)
    changed = True
    while changed:
        changed = False
        for target, sources in dependencies.items():
            if target not in bad and sources & bad:
                bad.add(target)
                changed = True

    return {
        str(decl.get("name") or "")
        for decl in decls
        if str(decl.get("kind") or "") in {"theorem", "lemma"}
        and str(decl.get("name") or "") not in bad
    }


def guidance_for_lean(graph: dict[str, Any] | None) -> str:
    """Compact, untrusted structural context that helps Lean cover the proof."""
    if not graph or not (graph.get("nodes") or []):
        return ""
    compact = {
        "title": str(graph.get("title") or "")[:200],
        "nodes": [
            {
                "id": str(node.get("id") or "")[:80],
                "kind": str(node.get("kind") or "")[:40],
                "label": str(node.get("label") or "")[:180],
                "statement": str(node.get("statement") or "")[:1200],
            }
            for node in (graph.get("nodes") or [])[:24]
            if isinstance(node, dict)
        ],
        "edges": [
            {
                "from": str(edge.get("from") or "")[:80],
                "to": str(edge.get("to") or "")[:80],
                "label": str(edge.get("label") or "")[:80],
            }
            for edge in (graph.get("edges") or [])[:48]
            if isinstance(edge, dict)
        ],
    }
    return (
        "COOPERATIVE INFORMAL DAG (advisory; the original problem and proof remain "
        "authoritative):\n"
        + json.dumps(compact, ensure_ascii=False)
        + "\nCover the relevant assumptions and conclusion without strengthening, "
        "weakening, or replacing the theorem statement."
    )


def _source_revision(workspace: Path) -> str:
    digest = hashlib.sha256()
    found = False
    for rel in ("proof.md", "lean/Proof.lean", "proof_graph.json", "lean_proof_graph.json"):
        path = workspace / rel
        try:
            body = path.read_bytes()
        except OSError:
            continue
        found = True
        digest.update(rel.encode("utf-8"))
        digest.update(body)
    return digest.hexdigest() if found else ""


def build(workspace: Path, *, selected_harness: str = "") -> dict[str, Any]:
    informal_doc = _read_json(workspace / "proof_graph.json")
    formal_doc = _read_json(workspace / "lean_proof_graph.json")
    lean = _read_json(workspace / "lean_verify.json")
    informal = (informal_doc.get("graph") or {}) if informal_doc else {}
    formal = (formal_doc.get("graph") or {}) if formal_doc else {}
    informal_nodes = [n for n in (informal.get("nodes") or []) if isinstance(n, dict)]
    formal_nodes = [n for n in (formal.get("nodes") or []) if isinstance(n, dict)]

    audit = ((lean.get("fidelity") or {}).get("audit") or {}) if lean else {}
    trusted = bool(
        lean.get("status") == "verified"
        and int(lean.get("sorry_count") or 0) == 0
        and (lean.get("fidelity") or {}).get("severity") != "major"
        and audit.get("ok")
        and audit.get("faithful")
    )
    if trusted:
        trust_reason = "Lean kernel verified; no sorry/admit; fidelity audit faithful"
    elif not lean:
        trust_reason = "Lean result is not available"
    elif lean.get("status") != "verified":
        trust_reason = f"Lean status is {lean.get('status') or 'unknown'}"
    elif int(lean.get("sorry_count") or 0):
        trust_reason = "Lean source contains sorry/admit"
    elif not audit.get("ok"):
        trust_reason = "Fidelity audit did not complete"
    else:
        trust_reason = "Fidelity audit did not confirm the original statement"

    partial_declarations = _partial_kernel_declarations(workspace, lean) if lean else set()
    formal_conclusions = [n for n in formal_nodes if n.get("kind") == "conclusion"]
    items: list[dict[str, Any]] = []
    verified_count = 0
    for node in informal_nodes:
        node_id = str(node.get("id") or "")
        kind = str(node.get("kind") or "")
        candidates = formal_conclusions if kind == "conclusion" and formal_conclusions else formal_nodes
        best: dict[str, Any] | None = None
        score, common = 0.0, 0
        label_score, label_common = 0.0, 0
        strong_label_match = False
        exact_signature_match = False
        node_signature = _exact_indexed_value_signature(_node_text(node))
        best_rank = (-1, -1, -1.0, -1.0, -1, -1)
        for candidate in candidates:
            candidate_score, candidate_common = _match_score(node, candidate)
            candidate_label_score, candidate_label_common = _label_match_score(
                node, candidate
            )
            informal_label_tokens = _tokens(str(node.get("label") or ""))
            formal_label_tokens = _tokens(str(candidate.get("label") or ""))
            semantic_label_subset = bool(
                len(formal_label_tokens) >= 2
                and formal_label_tokens <= informal_label_tokens
            )
            numeric_common = len(
                _distinctive_numeric_tokens(_node_text(node))
                & _distinctive_numeric_tokens(_node_text(candidate))
            )
            candidate_exact_signature = bool(
                node_signature
                and node_signature
                == _exact_indexed_value_signature(_node_text(candidate))
            )
            candidate_exact_signature = bool(
                candidate_exact_signature
                or (
                    _edge_deletion_three_coloring_signature(_node_text(node))
                    and _edge_deletion_three_coloring_signature(
                        _node_text(candidate)
                    )
                )
            )
            candidate_strong_label = bool(
                candidate_exact_signature
                or semantic_label_subset
                or (
                    candidate_label_common >= 2
                    and (
                        candidate_label_score >= 0.72
                        or (
                            candidate_label_score >= (2.0 / 3.0 - 1e-9)
                            and numeric_common >= 2
                        )
                    )
                )
            )
            rank = (
                int(candidate_exact_signature),
                int(candidate_strong_label),
                max(candidate_score, candidate_label_score),
                candidate_label_score,
                numeric_common,
                max(candidate_common, candidate_label_common),
            )
            if rank > best_rank:
                best_rank = rank
                best = candidate
                score = max(candidate_score, candidate_label_score)
                common = max(candidate_common, candidate_label_common)
                label_score = candidate_label_score
                label_common = candidate_label_common
                strong_label_match = candidate_strong_label
                exact_signature_match = candidate_exact_signature

        # A faithful verified main theorem certifies the informal conclusion.
        mapped_conclusion = kind == "conclusion" and bool(formal_conclusions)
        mapped_step = (
            kind not in {"assumption", "definition"}
            and best is not None
            and (
                exact_signature_match
                or (score >= 0.34 and common >= 2)
            )
        )
        declaration = str(best.get("label") or "") if best else ""
        partial_verified = bool(
            not trusted
            and mapped_step
            and declaration in partial_declarations
            and strong_label_match
        )
        is_verified = (trusted and (mapped_conclusion or mapped_step)) or partial_verified
        if is_verified:
            verified_count += 1
        formal_ids = [str(best.get("id") or "")] if best and (mapped_conclusion or mapped_step) else []
        declarations = [str(best.get("label") or "")] if formal_ids else []
        items.append(
            {
                "informal_id": node_id,
                "informal_kind": kind,
                "informal_label": str(node.get("label") or ""),
                "informal_statement": str(node.get("statement") or ""),
                "status": "verified" if is_verified else ("linked" if formal_ids else "unmapped"),
                "formal_ids": formal_ids,
                "lean_declarations": declarations,
                "confidence": round(1.0 if mapped_conclusion else score, 3),
                "reason": trust_reason if not is_verified else (
                    "Faithful Lean conclusion" if trusted and mapped_conclusion
                    else (
                        "Mapped to an admission-independent kernel-checked Lean declaration"
                        if partial_verified
                        else "Mapped to a verified Lean declaration"
                    )
                ),
            }
        )

    result = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "selected_harness": str(selected_harness or ""),
        "executor": "derived-tools",
        "status": "verified" if trusted else (
            "partial" if verified_count else ("unverified" if lean else "waiting")
        ),
        "formal_verified": trusted,
        "trust_reason": trust_reason,
        "source_revision": _source_revision(workspace),
        "items": items,
        "cross_links": [
            {
                "informal_id": item["informal_id"],
                "formal_id": formal_id,
                "status": item["status"],
            }
            for item in items
            for formal_id in item["formal_ids"]
        ],
        "summary": {
            "verified": verified_count,
            "total": len(informal_nodes),
            "linked": sum(1 for item in items if item["formal_ids"]),
        },
    }
    path = workspace / FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return result
