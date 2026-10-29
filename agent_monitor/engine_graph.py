"""Deterministic, provenance-honest graphs emitted by proof engines.

This module deliberately does not invoke a model.  ``math_harness`` exposes its
content-addressed fact DAG ("Facts") and its exploring graph ("Explore"); every
other engine exposes only the agents/edges in the run record and is labelled as
an execution trace, not proof logic.

Two runner generations are recognized, both fail-closed:

* receipt schema 1 (``agent-monitor-math-harness-response-adapter@1``): the
  historical single-critic adapter with exactly one target fact;
* receipt schema 2 (``agent-monitor-math-harness-cgm@2``): the CGM pipeline
  (strategist, workers, per-lemma independent verifier, repair, curator) with a
  multi-lemma fact DAG and a CGM exploring graph whose memory files are bound
  by hash in the receipt.  Attestation additionally requires ``proof.md`` to
  equal ``agent_monitor.cgm.render_verified_closure`` of the target closure,
  in the rendering the receipt names (``verified-closure@1`` or ``@2``).

Model-verifier acceptance is never a Lean certificate: every artifact carries
``certificate: false`` and ``lean_verified: false``.
"""
from __future__ import annotations

import errno
import hashlib
import heapq
import json
import math
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent_monitor.cgm import closure as _cgm_closure
from agent_monitor.cgm import schema as _cgm_schema


SCHEMA_VERSION = 1
CACHE_FILENAME = "engine_graph.json"
WORKFLOW_CACHE_FILENAME = "engine_workflow_graph.json"
MATH_HARNESS_ENGINE = "math_harness"
MATH_HARNESS_PROJECT_PARTS = ("math_harness", "project")

MAX_CACHE_BYTES = 4_000_000
MAX_FACT_BYTES = 256_000
MAX_TARGET_BYTES = 64_000
MAX_RECEIPT_BYTES = 64_000
MAX_PROBLEM_ARTIFACT_BYTES = 128_000
MAX_PROOF_ARTIFACT_BYTES = 200_000
MAX_SOURCE_BYTES = 4_000_000
MAX_NODES = 256
MAX_EDGES = 4096
MAX_ID_LENGTH = 160
MAX_STATEMENT_LENGTH = 4000
MAX_PROOF_EXCERPT = 4000
MAX_INTUITION_LENGTH = 1200
MAX_GLOSSARY_ENTRIES = 128
MAX_EXTERNAL_REFS = 16
MAX_DIRECTORY_ENTRIES = 1024
MAX_EXTERNAL_REF_FIELDS = 24
MAX_EXTERNAL_REF_SEQUENCE = 32
MAX_EXTERNAL_REF_DEPTH = 4
MAX_EXTERNAL_REF_STRING = 2000
MAX_SECTION_CANDIDATES = 64
MAX_WORKFLOW_FILE_BYTES = 1_000_000
MAX_WORKFLOW_ROWS = 256
MATH_HARNESS_AUTHOR = "math_harness_model_critic"
CGM_AUTHOR = _cgm_schema.AUTHOR
# Engines whose runs may carry a math_harness project.  ``kimi`` is included
# for the CGM delegation path; it is only consulted when a receipt exists.
CGM_GRAPH_ENGINES = frozenset({MATH_HARNESS_ENGINE, "kimi"})

_FACT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_FACT_FILENAME_RE = re.compile(r"^([0-9a-f]{16})\.md$")
_SPACE_RE = re.compile(r"\s+")
_WORKFLOW_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.+-]+Z$")
_TARGET_LABEL_RE = re.compile(r"^target(?:_fact_ids)?\s*:\s*", re.IGNORECASE)
_ALLOWED_NODE_KINDS = {
    "assumption", "definition", "lemma", "claim", "case", "step",
    "contradiction", "conclusion",
}
_WORKFLOW_FILES = (
    "plan.jsonl",
    "proof_attempt.jsonl",
    "verification.jsonl",
    "edges.jsonl",
    "annotations.jsonl",
)
_TERMINAL_RUN_STATUSES = frozenset(
    {
        "finished",
        "failed",
        "stopped",
        "cancelled",
        "canceled",
        "interrupted",
        "error",
        "completed",
        "complete",
        "done",
    }
)

_WORKFLOW_ENTRY_FIELDS = frozenset(
    {
        "id", "timestamp_utc", "author", "kind", "claim", "evidence",
        "verifiable", "status", "fact_id", "links", "glossary",
    }
)
_WORKFLOW_EDGE_FIELDS = frozenset(
    {
        "id", "timestamp_utc", "author", "src", "dst", "type",
        "technique", "and_group", "rationale",
    }
)
_WORKFLOW_ANNOTATION_FIELDS = frozenset(
    {
        "id", "timestamp_utc", "author", "node_id", "status", "score",
        "comment", "evidence_considered",
    }
)


class EngineGraphError(ValueError):
    """An engine graph artifact is malformed, unsafe, or exceeds its limits."""


@dataclass(frozen=True)
class _Fact:
    fact_id: str
    problem_id: str
    author: str
    predecessors: tuple[str, ...]
    statement: str
    proof: str
    glossary: dict[str, str]
    intuition: str
    citations: tuple[str, ...]


def _bounded_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _bounded_math_label(value: object, limit: int) -> str:
    """Like ``_bounded_text`` for a one-line title, but never cut inside math.

    A cut inside ``$…$`` / ``$$…$$`` / ``\\(…\\)`` / ``\\[…\\]`` leaves an
    unmatched delimiter that renders raw (``$n…``); the title then ends
    before that span instead.
    """
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    opened: tuple[int, str] | None = None
    index = 0
    while index < len(cut):
        char = cut[index]
        if char == "\\":
            following = cut[index + 1 : index + 2]
            if opened is None and following in {"(", "["}:
                opened = (index, ")" if following == "(" else "]")
            elif opened is not None and following == opened[1]:
                opened = None
            index += 2
            continue
        if char == "$":
            token = "$$" if cut[index + 1 : index + 2] == "$" else "$"
            if opened is None:
                opened = (index, token)
            elif opened[1] == token:
                opened = None
            index += len(token)
            continue
        index += 1
    if opened is not None:
        cut = cut[: opened[0]]
    return cut.rstrip(" ,;:") + "…"


def _bounded_positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    text = str(value if value is not None else "").strip()
    if not text or len(text) > 12 or not text.isdigit():
        return default
    try:
        parsed = int(text)
    except (TypeError, ValueError):
        return default
    return parsed if 0 < parsed <= 1_000_000_000 else default


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _source_hash(parts: Sequence[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"engine-graph-source-v1\0")
    for name, payload in parts:
        name_bytes = name.encode("utf-8")
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _open_directory(parent_fd: int, name: str, *, missing_ok: bool = False) -> int | None:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise EngineGraphError(f"required engine graph directory is missing: {name}") from None
    except OSError as exc:
        if missing_ok and exc.errno == errno.ENOENT:
            return None
        raise EngineGraphError(f"unsafe engine graph directory: {name}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise EngineGraphError(f"engine graph path is not a directory: {name}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_regular_file(
    parent_fd: int,
    name: str,
    *,
    limit: int,
    missing_ok: bool = False,
) -> bytes | None:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise EngineGraphError(f"required engine graph file is missing: {name}") from None
    except OSError as exc:
        if missing_ok and exc.errno == errno.ENOENT:
            return None
        raise EngineGraphError(f"unsafe engine graph file: {name}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise EngineGraphError(f"engine graph path is not a regular file: {name}")
        if before.st_size > limit:
            raise EngineGraphError(f"engine graph file exceeds its size limit: {name}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
        if len(payload) > limit:
            raise EngineGraphError(f"engine graph file exceeds its size limit: {name}")
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise EngineGraphError(f"engine graph file changed while being read: {name}")
        return payload
    finally:
        os.close(fd)


def _decode(payload: bytes, name: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EngineGraphError(f"engine graph file is not UTF-8: {name}") from exc


def _validate_external_ref_value(value: object, name: str, *, depth: int = 0) -> None:
    if depth > MAX_EXTERNAL_REF_DEPTH:
        raise EngineGraphError(f"external reference is too deeply nested in {name}")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EngineGraphError(f"invalid external reference number in {name}")
        return
    if isinstance(value, str):
        if len(value) > MAX_EXTERNAL_REF_STRING:
            raise EngineGraphError(f"external reference field is too long in {name}")
        return
    if isinstance(value, list):
        if len(value) > MAX_EXTERNAL_REF_SEQUENCE:
            raise EngineGraphError(f"external reference list is too long in {name}")
        for item in value:
            _validate_external_ref_value(item, name, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_EXTERNAL_REF_FIELDS:
            raise EngineGraphError(f"external reference object is too large in {name}")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 120:
                raise EngineGraphError(f"invalid external reference key in {name}")
            _validate_external_ref_value(item, name, depth=depth + 1)
        return
    raise EngineGraphError(f"invalid external reference field in {name}")


def _render_external_ref_value(value: object) -> str:
    if isinstance(value, list):
        return _bounded_text(
            ", ".join(_render_external_ref_value(item) for item in value), 240
        )
    if isinstance(value, dict):
        return _bounded_text(_canonical_bytes(value).decode("utf-8"), 240)
    return _bounded_text(value, 160)


def _parse_external_refs(raw: str, name: str) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        refs = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise EngineGraphError(f"malformed external_refs in {name}") from exc
    if not isinstance(refs, list) or len(refs) > MAX_EXTERNAL_REFS:
        raise EngineGraphError(f"invalid external_refs in {name}")
    citations: list[str] = []
    for ref in refs:
        if not isinstance(ref, dict) or len(ref) > MAX_EXTERNAL_REF_FIELDS:
            raise EngineGraphError(f"invalid external reference in {name}")
        _validate_external_ref_value(ref, name)
        pieces: list[str] = []
        for key in (
            "key", "authors", "title", "arxiv", "year", "venue", "doi", "cited_for"
        ):
            value = ref.get(key)
            if value is None:
                continue
            rendered = _render_external_ref_value(value)
            if rendered:
                pieces.append(rendered)
        citation = _bounded_text(" · ".join(pieces), 600)
        if citation and citation not in citations:
            citations.append(citation)
    return tuple(citations)


def _body_section_candidates(
    body: str, name: str
) -> list[tuple[str, str, str]]:
    lines = body.splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if (
        start >= len(lines)
        or lines[start].lower() != "## statement"
    ):
        raise EngineGraphError(f"fact is missing its statement delimiter: {name}")
    proof_markers = [
        index
        for index in range(start + 1, len(lines))
        if lines[index].lower() == "## proof"
    ]
    if not proof_markers:
        raise EngineGraphError(f"fact is missing its proof delimiter: {name}")
    candidates: list[tuple[str, str, str]] = []
    for proof_index in proof_markers:
        statement = "\n".join(lines[start + 1 : proof_index]).strip()
        intuition_markers = [
            index
            for index in range(proof_index + 1, len(lines))
            if lines[index].lower() == "## intuition"
        ]
        for intuition_index in [None, *intuition_markers]:
            proof_end = len(lines) if intuition_index is None else intuition_index
            proof = "\n".join(lines[proof_index + 1 : proof_end]).strip()
            intuition = (
                ""
                if intuition_index is None
                else "\n".join(lines[intuition_index + 1 :]).strip()
            )
            if not statement or not proof:
                continue
            candidates.append((statement, proof, intuition))
            if len(candidates) > MAX_SECTION_CANDIDATES:
                raise EngineGraphError(f"too many fact section delimiters in {name}")
    if not candidates:
        raise EngineGraphError(f"fact is missing statement or proof: {name}")
    return candidates


def _parse_fact(payload: bytes, expected_id: str, name: str) -> _Fact:
    text = _decode(payload, name)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise EngineGraphError(f"fact has no frontmatter: {name}")
    try:
        close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise EngineGraphError(f"fact frontmatter is not closed: {name}") from None

    scalars: dict[str, str] = {}
    glossary: dict[str, str] = {}
    in_glossary = False
    allowed = {
        "fact_id", "problem_id", "author", "predecessors",
        "glossary_introduces", "external_refs",
    }
    for line in lines[1:close]:
        if in_glossary and line.startswith("  "):
            if ":" not in line:
                raise EngineGraphError(f"malformed glossary entry in {name}")
            key, value = line.strip().split(":", 1)
            key, value = key.strip(), value.strip()
            if not key or key in glossary or len(key) > 160 or len(value) > 1200:
                raise EngineGraphError(f"invalid glossary entry in {name}")
            glossary[key] = value
            if len(glossary) > MAX_GLOSSARY_ENTRIES:
                raise EngineGraphError(f"too many glossary entries in {name}")
            continue
        in_glossary = False
        if not line.strip():
            continue
        if ":" not in line:
            raise EngineGraphError(f"malformed fact frontmatter in {name}")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key not in allowed or key in scalars:
            raise EngineGraphError(f"unsupported or duplicate frontmatter field in {name}: {key}")
        scalars[key] = value
        if key == "glossary_introduces":
            if value not in {"", "{}"}:
                raise EngineGraphError(f"malformed glossary in {name}")
            in_glossary = value == ""

    for required in ("fact_id", "problem_id", "author", "predecessors", "glossary_introduces"):
        if required not in scalars:
            raise EngineGraphError(f"fact is missing {required}: {name}")
    fact_id = scalars["fact_id"]
    if fact_id != expected_id or not _FACT_ID_RE.fullmatch(fact_id):
        raise EngineGraphError(f"fact id does not match its filename: {name}")
    problem_id = scalars["problem_id"].strip()
    author = scalars["author"].strip()
    if not problem_id or len(problem_id) > 240 or not author or len(author) > 240:
        raise EngineGraphError(f"invalid problem_id or author in {name}")

    pred_raw = scalars["predecessors"]
    if not (pred_raw.startswith("[") and pred_raw.endswith("]")):
        raise EngineGraphError(f"malformed predecessors in {name}")
    predecessors = tuple(
        item.strip() for item in pred_raw[1:-1].split(",") if item.strip()
    )
    if len(predecessors) != len(set(predecessors)) or any(
        not _FACT_ID_RE.fullmatch(item) for item in predecessors
    ):
        raise EngineGraphError(f"invalid predecessors in {name}")

    citations = _parse_external_refs(scalars.get("external_refs", "[]"), name)
    candidates = _body_section_candidates("\n".join(lines[close + 1 :]), name)
    matched: tuple[str, str, str] | None = None
    for statement, proof, intuition in candidates:
        canonical = {
            "problem_id": problem_id,
            "predecessors": sorted(predecessors),
            "glossary_introduces": dict(
                sorted((str(key), str(value)) for key, value in glossary.items())
            ),
            "statement": _SPACE_RE.sub(" ", statement or "").strip(),
            "proof": _SPACE_RE.sub(" ", proof or "").strip(),
        }
        computed = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        if computed == fact_id:
            matched = (statement, proof, intuition)
            break
    if matched is None:
        raise EngineGraphError(f"fact content id mismatch: {name}")
    statement, proof, intuition = matched
    return _Fact(
        fact_id=fact_id,
        problem_id=problem_id,
        author=author,
        predecessors=predecessors,
        statement=statement,
        proof=proof,
        glossary=glossary,
        intuition=intuition,
        citations=citations,
    )


def _parse_targets(payload: bytes | None) -> tuple[str, ...]:
    if payload is None:
        return ()
    text = _decode(payload, "TARGET.md")
    targets: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        line = _TARGET_LABEL_RE.sub("", line)
        tokens = [token for token in re.split(r"[\s,]+", line) if token]
        if not tokens or any(not _FACT_ID_RE.fullmatch(token) for token in tokens):
            raise EngineGraphError("TARGET.md contains an invalid fact id")
        for token in tokens:
            if token not in targets:
                targets.append(token)
    return tuple(targets)


def _topological_order(facts: Mapping[str, _Fact]) -> tuple[list[str], dict[str, int]]:
    children: dict[str, list[str]] = {fact_id: [] for fact_id in facts}
    indegree: dict[str, int] = {}
    for fact_id, fact in facts.items():
        if fact_id in fact.predecessors:
            raise EngineGraphError(f"fact graph contains a self dependency: {fact_id}")
        unknown = [pred for pred in fact.predecessors if pred not in facts]
        if unknown:
            raise EngineGraphError(
                f"fact graph contains an unknown predecessor: {unknown[0]}"
            )
        indegree[fact_id] = len(fact.predecessors)
        for pred in fact.predecessors:
            children[pred].append(fact_id)
    ready = [fact_id for fact_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    depths: dict[str, int] = {}
    while ready:
        fact_id = heapq.heappop(ready)
        fact = facts[fact_id]
        depths[fact_id] = (
            max((depths[pred] for pred in fact.predecessors), default=-1) + 1
        )
        order.append(fact_id)
        for child in sorted(children[fact_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(facts):
        raise EngineGraphError("fact graph contains a cycle")
    return order, depths


_RECEIPT_REQUIRED_FIELDS = frozenset(
    {
        "schema_version", "engine", "source", "source_sha256", "semantics",
        "verdict", "critic_verdict", "critic", "certificate", "problem_id",
        "problem_sha256", "proof_sha256", "provider", "model",
        "requested_model", "transport", "target_fact_id", "target_fact_ids",
        "target_sha256", "fact_path", "fact_bytes", "fact_bytes_sha256",
        "task_sha256", "critic_response_sha256", "binding_sha256",
        "workflow", "calls",
    }
)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _receipt_response_only_contract(
    receipt: Mapping[str, Any],
) -> dict[str, object] | None:
    workflow = receipt.get("workflow")
    if not isinstance(workflow, dict):
        return None
    contract = workflow.get("response_only_contract")
    if not isinstance(contract, dict) or set(contract) != {
        "transport",
        "isolation_version",
    }:
        return None
    transport = receipt.get("transport")
    if transport == _cgm_schema.HYBRID_TRANSPORT and (
        receipt.get("schema_version") != _cgm_schema.HYBRID_RECEIPT_SCHEMA_VERSION
        or _hybrid_routes(receipt) is None
    ):
        # A Max contract is only as good as the per-route contracts it binds.
        return None
    expected_version = {
        **_ISOLATION_VERSIONS,
        _cgm_schema.HYBRID_TRANSPORT: _cgm_schema.HYBRID_CONTRACT["isolation_version"],
    }.get(transport)
    version = contract.get("isolation_version")
    if (
        expected_version is None
        or contract.get("transport") != transport
        or version != expected_version
        or isinstance(version, bool)
    ):
        return None
    return {
        "transport": transport,
        "isolation_version": expected_version,
    }


# The response-only isolation version a receipt names for each transport.
# Version 4 Codex receipts predate the tool-free model catalog (their "no
# tools" claim was not true for every model); never attest them.
_ISOLATION_VERSIONS = {"codex_exec": 5, "claude_code": 1, "api_chat": 1}
_HYBRID_ROUTE_FIELDS = frozenset(
    {"families", "provider", "model", "transport", "isolation_version", "effort"}
)
# The call families each side of a schema-4 route table answers.
_HYBRID_FAMILIES = {
    "advisor": sorted(_cgm_schema.ADVISOR_CALL_FAMILIES),
    "worker": sorted(_cgm_schema.WORKER_CALL_FAMILIES),
}


def _hybrid_routes(receipt: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    """The validated route table of a schema-4 receipt, else ``None``.

    The receipt's requested model names its table (Max or Max (API); see
    ``cgm_schema.HYBRID_ROUTE_TABLES``), which must match whole: a side from
    the other table, or the other mode's model, never attests.
    """
    workflow = receipt.get("workflow")
    routes = workflow.get("routes") if isinstance(workflow, dict) else None
    requested = receipt.get("requested_model")
    table = (
        _cgm_schema.HYBRID_ROUTE_TABLES.get(requested) if isinstance(requested, str) else None
    )
    if table is None or not isinstance(routes, dict) or set(routes) != set(table):
        return None
    for side, (provider, transport) in table.items():
        route = routes[side]
        if (
            not isinstance(route, dict)
            or set(route) != _HYBRID_ROUTE_FIELDS
            or route["provider"] != provider
            or route["transport"] != transport
            or route["isolation_version"] != _ISOLATION_VERSIONS[transport]
            or isinstance(route["isolation_version"], bool)
            or route["families"] != _HYBRID_FAMILIES[side]
            or not isinstance(route["model"], str)
            or not route["model"].strip()
            or len(route["model"]) > 240
            or route["effort"] not in _cgm_schema.HYBRID_EFFORTS
        ):
            return None
    return routes


def _runner_problem_bytes(payload: bytes | None) -> bytes | None:
    prefix = b"# Problem\n\n"
    if payload is None or not payload.startswith(prefix) or not payload.endswith(b"\n"):
        return None
    problem = payload[len(prefix) : -1]
    if not problem or problem.strip() != problem:
        return None
    try:
        problem.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return problem


def _receipt_provenance(
    payload: bytes | None,
    *,
    targets: tuple[str, ...],
    target_payload: bytes | None,
    facts: Mapping[str, _Fact],
    fact_payloads: Mapping[str, bytes],
    problem_artifact: bytes | None,
    proof_payload: bytes | None,
) -> dict[str, object]:
    if payload is None:
        return {"attestation": "unattested", "receipt_status": "missing"}
    try:
        receipt = json.loads(_decode(payload, "engine_receipt.json"))
    except (json.JSONDecodeError, RecursionError, EngineGraphError):
        return {"attestation": "unattested", "receipt_status": "invalid"}
    if not isinstance(receipt, dict) or not _RECEIPT_REQUIRED_FIELDS.issubset(receipt):
        return {"attestation": "unattested", "receipt_status": "invalid"}
    if len(targets) != 1:
        return {"attestation": "unattested", "receipt_status": "invalid"}
    target_id = targets[0]
    fact = facts.get(target_id)
    fact_payload = fact_payloads.get(target_id)
    bound_fact_ids: set[str] = set()
    pending = [target_id] if fact is not None else []
    while pending:
        current = pending.pop()
        if current in bound_fact_ids:
            continue
        bound_fact_ids.add(current)
        pending.extend(facts[current].predecessors)
    problem = _runner_problem_bytes(problem_artifact)
    try:
        proof_text = proof_payload.decode("utf-8") if proof_payload is not None else ""
    except UnicodeDecodeError:
        proof_text = ""
    hash_fields = {
        "problem_sha256": problem,
        "proof_sha256": proof_payload,
        "target_sha256": target_payload,
        "fact_bytes_sha256": fact_payload,
    }
    hashes_match = all(
        bound is not None
        and _is_sha256(receipt.get(field))
        and hashlib.sha256(bound).hexdigest() == receipt[field]
        for field, bound in hash_fields.items()
    )
    receipt_hashes_valid = all(
        _is_sha256(receipt.get(field))
        for field in (
            "source_sha256", "task_sha256", "critic_response_sha256", "binding_sha256"
        )
    )
    binding = {
        "source_sha256": receipt.get("source_sha256"),
        "problem_sha256": receipt.get("problem_sha256"),
        "task_sha256": receipt.get("task_sha256"),
        "proof_sha256": receipt.get("proof_sha256"),
        "provider": receipt.get("provider"),
        "model": receipt.get("model"),
        "verdict": receipt.get("verdict"),
        "critic_verdict": receipt.get("critic_verdict"),
        "critic_response_sha256": receipt.get("critic_response_sha256"),
        "target_fact_ids": receipt.get("target_fact_ids"),
        "target_sha256": receipt.get("target_sha256"),
        "fact_bytes_sha256": receipt.get("fact_bytes_sha256"),
        "certificate": receipt.get("certificate"),
    }
    binding_matches = receipt_hashes_valid and hashlib.sha256(
        _canonical_bytes(binding)
    ).hexdigest() == receipt.get("binding_sha256")
    response_only = _receipt_response_only_contract(receipt)

    strings_present = all(
        isinstance(receipt.get(field), str) and bool(receipt[field].strip())
        for field in (
            "source", "source_sha256", "semantics", "critic", "problem_id",
            "provider", "model", "requested_model", "transport", "fact_path",
        )
    )
    valid = (
        fact is not None
        and fact_payload is not None
        and bound_fact_ids == set(facts)
        and problem is not None
        and proof_payload is not None
        and target_payload == f"{target_id}\n".encode("ascii")
        and proof_payload.endswith(b"\n")
        and _SPACE_RE.sub(" ", proof_text).strip()
        == _SPACE_RE.sub(" ", fact.proof).strip()
        and _SPACE_RE.sub(" ", problem.decode("utf-8")).strip()
        == _SPACE_RE.sub(" ", fact.statement).strip()
        and receipt.get("schema_version") == 1
        and receipt.get("engine") == MATH_HARNESS_ENGINE
        and receipt.get("source") == "agent-monitor-math-harness-response-adapter@1"
        and receipt_hashes_valid
        and receipt.get("semantics") == "model-critic"
        and receipt.get("verdict") == "accepted"
        and receipt.get("critic_verdict") == "correct"
        and receipt.get("critic") == "response-only-model-critic"
        and receipt.get("certificate") is False
        and receipt.get("problem_id") == fact.problem_id
        and receipt.get("target_fact_id") == target_id
        and receipt.get("target_fact_ids") == [target_id]
        and receipt.get("fact_path") == f"fact_graph/facts/{target_id}.md"
        and isinstance(receipt.get("fact_bytes"), int)
        and not isinstance(receipt.get("fact_bytes"), bool)
        and receipt.get("fact_bytes") == len(fact_payload)
        and isinstance(receipt.get("workflow"), dict)
        and receipt["workflow"].get("tools_enabled") is False
        and isinstance(receipt.get("calls"), list)
        and bool(receipt["calls"])
        and strings_present
        and hashes_match
        and binding_matches
        and response_only is not None
    )
    if not valid:
        return {"attestation": "unattested", "receipt_status": "invalid"}
    return {
        "attestation": "harness-gate-receipt",
        "receipt_status": "critic-correct",
        "critic": _bounded_text(receipt["critic"], 120),
        "critic_model": _bounded_text(receipt["model"], 120),
        "response_only_transport": response_only["transport"],
        "response_only_isolation_version": response_only["isolation_version"],
    }


def _empty_math_harness_graph() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "engine-graph",
        "status": "none",
        "kind": "engine",
        "model": "deterministic-adapter",
        "source": "math_harness/project/fact_graph",
        "source_sha256": "",
        "graph_role": "fact-dependency",
        "selection": {
            "target_source": "unset",
            "target_ids": [],
            "complete_closure": False,
        },
        "provenance": {
            "engine": MATH_HARNESS_ENGINE,
            "producer": "math-harness",
            "adapter": "math-harness-fact-graph@1",
            "semantics": "fact-predecessor",
            "attestation": "unattested",
            "receipt_status": "missing",
            "certificate": False,
            "proof_logic": True,
            "lean_verified": False,
            "acyclic": True,
            "all_predecessors_known": True,
            "truncated": False,
        },
        "graph": {"title": "Math Harness fact graph", "nodes": [], "edges": []},
    }


_CGM_RECEIPT_FIELDS = frozenset(
    {
        "schema_version", "engine", "source", "source_sha256",
        "prompt_pack_sha256", "prompt_pack_version", "semantics", "verifier",
        "verdict", "certificate", "lean_verified", "problem_id",
        "problem_sha256", "task_sha256", "proof_sha256", "proof_rendering",
        "provider", "model", "requested_model", "transport", "target_fact_id",
        "target_sha256", "facts", "memory", "selected_worker",
        "selected_attempt", "verdicts", "workflow", "calls", "binding_sha256",
    }
)
_CGM_WORKFLOW_FIELDS = frozenset(
    {
        "pipeline", "verification_policy", "stages", "calls", "retries",
        "budgets", "rows", "degraded", "tools_enabled", "response_only_contract",
    }
)
_CGM_BUDGET_FIELDS = frozenset(
    {
        "max_calls", "max_workers", "max_repairs", "max_verify_candidates",
        "max_lemmas", "max_seconds", "guest",
    }
)
# Schema 3 (concurrent writer) adds the canonical call order and the resolved
# concurrency; schema 2 (sequential phase-1 writer) keeps its exact shape.
_CGM_WORKFLOW_FIELDS_BY_VERSION = {
    2: _CGM_WORKFLOW_FIELDS,
    3: _CGM_WORKFLOW_FIELDS | {"call_order"},
    # Schema 4 (Max) adds the route table that binds call families to routes.
    4: _CGM_WORKFLOW_FIELDS | {"call_order", "routes"},
}
_CGM_BUDGET_FIELDS_BY_VERSION = {
    2: _CGM_BUDGET_FIELDS,
    3: _CGM_BUDGET_FIELDS | {"concurrency"},
    4: _CGM_BUDGET_FIELDS | {"concurrency"},
}
_CGM_CALL_FIELDS = frozenset(
    {"stage", "provider", "model", "usage", "response_sha256", "effort"}
)


def _plain_int(value: object, *, minimum: int = 0, maximum: int = 1_000_000_000) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _short_text(value: object, limit: int = 240) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _cgm_receipt(payload: bytes | None) -> dict[str, Any] | None:
    """Parse and statically validate a CGM (schema 2 or 3) receipt; ``None``
    if invalid."""
    if payload is None:
        return None
    try:
        receipt = json.loads(_decode(payload, "engine_receipt.json"))
    except (json.JSONDecodeError, RecursionError, EngineGraphError):
        return None
    if not isinstance(receipt, dict) or set(receipt) != _CGM_RECEIPT_FIELDS:
        return None
    try:
        return receipt if _cgm_receipt_is_valid(receipt) else None
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _cgm_receipt_is_valid(receipt: dict[str, Any]) -> bool:
    version = receipt["schema_version"]
    if (
        not _plain_int(version)
        or version not in _cgm_schema.CGM_RECEIPT_SCHEMA_VERSIONS
    ):
        return False
    if not (
        receipt["engine"] == MATH_HARNESS_ENGINE
        and receipt["source"] == _cgm_schema.PIPELINE_SOURCE
        and receipt["semantics"] == _cgm_schema.SEMANTICS
        and receipt["verifier"] == _cgm_schema.VERIFIER_LABEL
        and receipt["certificate"] is False
        and receipt["lean_verified"] is False
        and all(
            _is_sha256(receipt[name])
            for name in (
                "source_sha256", "prompt_pack_sha256", "problem_sha256",
                "task_sha256", "proof_sha256", "binding_sha256",
            )
        )
        and all(
            _short_text(receipt[name])
            for name in (
                "prompt_pack_version", "problem_id", "provider", "model",
                "requested_model", "transport",
            )
        )
        and _plain_int(receipt["selected_worker"], minimum=1, maximum=16)
        and _plain_int(receipt["selected_attempt"], minimum=1, maximum=64)
    ):
        return False
    verdict = receipt["verdict"]
    target = receipt["target_fact_id"]
    if verdict == "accepted":
        if (
            not isinstance(target, str)
            or _FACT_ID_RE.fullmatch(target) is None
            or not _is_sha256(receipt["target_sha256"])
            or not isinstance(receipt["proof_rendering"], str)
            or receipt["proof_rendering"]
            not in _cgm_schema.ACCEPTED_PROOF_RENDERINGS[version]
        ):
            return False
    elif verdict == "unattested":
        if (
            target is not None
            or receipt["target_sha256"] is not None
            or receipt["proof_rendering"] != _cgm_schema.PROOF_RENDERING_UNVERIFIED
        ):
            return False
    else:
        return False

    facts = receipt["facts"]
    if not isinstance(facts, list) or len(facts) > MAX_NODES:
        return False
    seen: set[str] = set()
    targets = []
    for item in facts:
        if (
            not isinstance(item, dict)
            or set(item) != {"fact_id", "label", "bytes", "sha256", "is_target"}
            or not isinstance(item["fact_id"], str)
            or _FACT_ID_RE.fullmatch(item["fact_id"]) is None
            or item["fact_id"] in seen
            or not _short_text(item["label"], 32)
            or not _plain_int(item["bytes"], minimum=1, maximum=MAX_FACT_BYTES)
            or not _is_sha256(item["sha256"])
            or not isinstance(item["is_target"], bool)
        ):
            return False
        seen.add(item["fact_id"])
        if item["is_target"]:
            targets.append(item["fact_id"])
    if targets != ([target] if verdict == "accepted" else []):
        return False

    memory = receipt["memory"]
    if (
        not isinstance(memory, dict)
        or not set(_cgm_schema.REQUIRED_MEMORY_FILES).issubset(memory)
        or not set(memory).issubset(_cgm_schema.MEMORY_FILES)
        or not all(_is_sha256(value) for value in memory.values())
    ):
        return False

    verdicts = receipt["verdicts"]
    if not isinstance(verdicts, list) or len(verdicts) > MAX_WORKFLOW_ROWS:
        return False
    for item in verdicts:
        if (
            not isinstance(item, dict)
            or set(item) != {"call", "attempt", "label", "verdict", "response_sha256"}
            or _cgm_schema.call_family(item["call"]) != "verify"
            or not _plain_int(item["attempt"], minimum=1, maximum=64)
            or not _short_text(item["label"], 32)
            or item["verdict"] not in {"correct", "wrong", "error"}
            or not (item["response_sha256"] is None or _is_sha256(item["response_sha256"]))
        ):
            return False

    workflow = receipt["workflow"]
    if (
        not isinstance(workflow, dict)
        or set(workflow) != _CGM_WORKFLOW_FIELDS_BY_VERSION[version]
    ):
        return False
    if (
        workflow["pipeline"] != _cgm_schema.PIPELINE_NAME
        or not isinstance(workflow["verification_policy"], str)
        or workflow["verification_policy"]
        not in _cgm_schema.ACCEPTED_VERIFICATION_POLICIES[version]
        or workflow["tools_enabled"] is not False
        or _receipt_response_only_contract(receipt) is None
    ):
        return False
    if version >= 3 and workflow["call_order"] != _cgm_schema.CALL_ORDER:
        return False
    stages = workflow["stages"]
    if (
        not isinstance(stages, list)
        or stages[:2] != ["scaffold", "work"]
        or stages[-1:] != ["finalize"]
        or [stage for stage in _cgm_schema.STAGE_ORDER if stage in stages] != stages
    ):
        return False
    budgets = workflow["budgets"]
    if (
        not isinstance(budgets, dict)
        or set(budgets) != _CGM_BUDGET_FIELDS_BY_VERSION[version]
        or not isinstance(budgets["guest"], bool)
        or not _plain_int(budgets["max_calls"], minimum=4, maximum=64)
        or not _plain_int(budgets["max_workers"], minimum=1, maximum=3)
        or not _plain_int(budgets["max_repairs"], minimum=0, maximum=4)
        or not _plain_int(budgets["max_verify_candidates"], minimum=1, maximum=3)
        or not _plain_int(budgets["max_lemmas"], minimum=1, maximum=16)
        or not _plain_int(budgets["max_seconds"], minimum=1, maximum=86_400)
    ):
        return False
    calls = receipt["calls"]
    if not isinstance(calls, list) or not calls or len(calls) > budgets["max_calls"]:
        return False
    counts = {family: 0 for family in _cgm_schema.CALL_FAMILIES}
    for call in calls:
        if (
            not isinstance(call, dict)
            or set(call) != _CGM_CALL_FIELDS
            or not isinstance(call["usage"], dict)
            or not all(
                isinstance(key, str) and _plain_int(value)
                for key, value in call["usage"].items()
            )
            or not (call["response_sha256"] is None or _is_sha256(call["response_sha256"]))
            or not _short_text(call["provider"])
            or not _short_text(call["model"])
            or call["effort"] not in {None, "low", "high", "max"}
        ):
            return False
        family = _cgm_schema.call_family(call["stage"])
        if family is None:
            return False
        counts[family] += 1
    expected_counts = {**counts, "total": len(calls)}
    if workflow["calls"] != expected_counts or counts["strategist"] < 1 or counts["worker"] < 1:
        return False
    if version == _cgm_schema.HYBRID_RECEIPT_SCHEMA_VERSION:
        # Max: the receipt speaks for the advisor, and every call came from the
        # route its family belongs to.
        routes = _hybrid_routes(receipt)
        if (
            routes is None
            or receipt["transport"] != _cgm_schema.HYBRID_TRANSPORT
            or receipt["provider"] != routes["advisor"]["provider"]
        ):
            return False
        for call in calls:
            family = _cgm_schema.call_family(call["stage"])
            side = "worker" if family in _cgm_schema.WORKER_CALL_FAMILIES else "advisor"
            if call["provider"] != routes[side]["provider"]:
                return False
    elif receipt["transport"] == _cgm_schema.HYBRID_TRANSPORT:
        return False
    retries = workflow["retries"]
    if (
        not isinstance(retries, dict)
        or set(retries) != {"format", "empty"}
        or not all(_plain_int(value, maximum=64) for value in retries.values())
    ):
        return False
    rows = workflow["rows"]
    if (
        not isinstance(rows, dict)
        or set(rows) != set(memory)
        or not all(_plain_int(value, maximum=MAX_WORKFLOW_ROWS) for value in rows.values())
    ):
        return False
    degraded = workflow["degraded"]
    if not isinstance(degraded, list) or not all(_short_text(item, 80) for item in degraded):
        return False
    if version >= 3 and not _cgm_v3_calls_are_canonical(receipt):
        return False
    return receipt["binding_sha256"] == _cgm_schema.receipt_binding_digest(receipt)


def _cgm_v3_calls_are_canonical(receipt: dict[str, Any]) -> bool:
    """Schema-3 checks that bind calls, verdicts, retries and concurrency.

    Calls are in canonical turn order (so the receipt cannot depend on
    completion order), the retry counts are exactly those the call stages
    imply, verdicts appear in verifier-call order and each names a verifier
    turn whose last answered call is the verdict's reply, and the recorded
    concurrency is within bounds (the guest ceiling for guest runs).
    """
    workflow = receipt["workflow"]
    budgets = workflow["budgets"]
    concurrency_ceiling = (
        _cgm_schema.GUEST_MAX_CONCURRENCY
        if budgets["guest"]
        else _cgm_schema.MAX_CONCURRENCY
    )
    if not _plain_int(budgets["concurrency"], minimum=1, maximum=concurrency_ceiling):
        return False
    calls = receipt["calls"]
    stages = [call["stage"] for call in calls]
    if (
        not _cgm_schema.canonical_call_order(stages)
        or workflow["retries"] != _cgm_schema.retry_counts(stages)
    ):
        return False
    # The reply a verdict was taken from: the last answered call of its turn.
    last_answer: dict[str, str | None] = {}
    for call in calls:
        turn = _cgm_schema.call_turn(call["stage"])
        assert turn is not None
        if turn[0] != "verify":
            continue
        base = f"verify-{turn[1]}"
        if call["response_sha256"] is not None or base not in last_answer:
            last_answer[base] = call["response_sha256"] or last_answer.get(base)
    previous = 0
    for item in receipt["verdicts"]:
        turn = _cgm_schema.call_turn(item["call"])
        if (
            turn is None
            or turn[2] != 0
            or turn[1] <= previous
            or item["call"] not in last_answer
            or last_answer[item["call"]] != item["response_sha256"]
        ):
            return False
        previous = turn[1]
    return True


def _receipt_provenance_v2(
    receipt: dict[str, Any] | None,
    *,
    targets: tuple[str, ...],
    target_payload: bytes | None,
    facts: Mapping[str, _Fact],
    fact_payloads: Mapping[str, bytes],
    problem_artifact: bytes | None,
    proof_payload: bytes | None,
) -> dict[str, object]:
    """Attest a CGM generation; fail closed on any mismatch."""
    invalid: dict[str, object] = {"attestation": "unattested", "receipt_status": "invalid"}
    if receipt is None:
        return invalid
    problem = _runner_problem_bytes(problem_artifact)
    bound = (
        problem is not None
        and proof_payload is not None
        and hashlib.sha256(problem).hexdigest() == receipt["problem_sha256"]
        and hashlib.sha256(proof_payload).hexdigest() == receipt["proof_sha256"]
        and _receipt_lists_facts(receipt, facts, fact_payloads)
    )
    if not bound:
        return invalid
    common: dict[str, object] = {
        "verifier": _bounded_text(receipt["verifier"], 120),
        "verifier_model": _bounded_text(receipt["model"], 120),
        "pipeline": "cgm",
        "prompt_pack_sha256": receipt["prompt_pack_sha256"],
        "response_only_transport": receipt["workflow"]["response_only_contract"]["transport"],
        "response_only_isolation_version": receipt["workflow"]["response_only_contract"][
            "isolation_version"
        ],
    }
    if receipt["verdict"] == "unattested":
        if targets or target_payload is not None:
            return invalid
        return {"attestation": "unattested", "receipt_status": "verifier-partial", **common}
    target_id = receipt["target_fact_id"]
    if (
        targets != (target_id,)
        or target_id not in facts
        or target_payload != f"{target_id}\n".encode("ascii")
        or hashlib.sha256(target_payload).hexdigest() != receipt["target_sha256"]
    ):
        return invalid
    assert problem is not None and proof_payload is not None
    try:
        closure = _cgm_closure.predecessor_closure(
            {fact_id: {"predecessors": fact.predecessors} for fact_id, fact in facts.items()},
            target_id,
        )
        # The rendering the receipt names, re-rendered from the fact files.
        rendered = _cgm_closure.render_verified_closure(
            [_fact_mapping(fact) for fact in facts.values()],
            target_id,
            labels=_proof_labels(receipt),
        )
    except _cgm_closure.ClosureError:
        return invalid
    if (
        closure != set(facts)
        or _SPACE_RE.sub(" ", problem.decode("utf-8")).strip()
        != _SPACE_RE.sub(" ", facts[target_id].statement).strip()
        or proof_payload != rendered.encode("utf-8")
    ):
        return invalid
    return {"attestation": "harness-gate-receipt", "receipt_status": "verifier-correct", **common}


def _receipt_lists_facts(
    receipt: Mapping[str, Any],
    facts: Mapping[str, _Fact],
    fact_payloads: Mapping[str, bytes],
) -> bool:
    """Whether a CGM receipt lists exactly these fact files (ids, sizes,
    hashes and problem id)."""
    listed = {item["fact_id"]: item for item in receipt["facts"]}
    return set(listed) == set(facts) and all(
        len(fact_payloads[fact_id]) == listed[fact_id]["bytes"]
        and hashlib.sha256(fact_payloads[fact_id]).hexdigest() == listed[fact_id]["sha256"]
        and facts[fact_id].problem_id == receipt["problem_id"]
        for fact_id in facts
    )


def _proof_labels(receipt: Mapping[str, Any]) -> dict[str, str] | None:
    """The labels an accepted ``verified-closure@2`` proof.md names its lemmas
    by (``{fact_id: label}`` from the receipt); ``None`` for @1, whose lemmas
    are numbered."""
    if receipt["proof_rendering"] != _cgm_schema.PROOF_RENDERING_VERIFIED_V2:
        return None
    return {
        item["fact_id"]: item["label"] for item in receipt["facts"] if not item["is_target"]
    }


def _fact_mapping(fact: _Fact) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "statement": fact.statement,
        "proof": fact.proof,
        "predecessors": list(fact.predecessors),
    }


def _receipt_schema_version(payload: bytes | None) -> int | None:
    if payload is None:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    version = value.get("schema_version") if isinstance(value, dict) else None
    return version if _plain_int(version, minimum=1, maximum=64) else None


@dataclass
class _FactProject:
    facts: dict[str, _Fact]
    fact_payloads: dict[str, bytes]
    targets: tuple[str, ...]
    order: list[str]
    depths: dict[str, int]
    problem_ids: set[str]
    source_parts: list[tuple[str, bytes]]
    provenance: dict[str, object]
    receipt: dict[str, Any] | None
    # The labels proof.md was published with (see ``load_proof_labels``).
    proof_labels: dict[str, str] | None = None


def _read_fact_project(workspace: Path) -> _FactProject | None:
    """Read and validate ``math_harness/project`` facts; ``None`` when absent."""
    root_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    root_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(os.fspath(workspace), root_flags)
    except OSError as exc:
        raise EngineGraphError("workspace is not a safe directory") from exc
    opened: list[int] = [root_fd]
    try:
        parent_fd = root_fd
        for index, part in enumerate(MATH_HARNESS_PROJECT_PARTS):
            child = _open_directory(parent_fd, part, missing_ok=index == 0)
            if child is None:
                return None
            opened.append(child)
            parent_fd = child
        project_fd = parent_fd
        fact_graph_fd = _open_directory(project_fd, "fact_graph", missing_ok=True)
        if fact_graph_fd is None:
            return None
        opened.append(fact_graph_fd)
        facts_fd = _open_directory(fact_graph_fd, "facts", missing_ok=True)
        if facts_fd is None:
            return None
        opened.append(facts_fd)

        fact_names: list[str] = []
        try:
            with os.scandir(facts_fd) as entries:
                for entry_count, entry in enumerate(entries, start=1):
                    if entry_count > MAX_DIRECTORY_ENTRIES:
                        raise EngineGraphError(
                            "fact graph directory exceeds its entry limit"
                        )
                    name = entry.name
                    if not _FACT_FILENAME_RE.fullmatch(name):
                        raise EngineGraphError(
                            f"unexpected entry in fact graph: {name}"
                        )
                    fact_names.append(name)
                    if len(fact_names) > MAX_NODES:
                        raise EngineGraphError("fact graph exceeds its node limit")
        except EngineGraphError:
            raise
        except OSError as exc:
            raise EngineGraphError("could not enumerate the fact graph safely") from exc
        fact_names.sort()
        facts: dict[str, _Fact] = {}
        fact_payloads: dict[str, bytes] = {}
        source_parts: list[tuple[str, bytes]] = []
        total_bytes = 0
        for name in fact_names:
            expected_id = _FACT_FILENAME_RE.fullmatch(name).group(1)  # type: ignore[union-attr]
            payload = _read_regular_file(facts_fd, name, limit=MAX_FACT_BYTES)
            assert payload is not None
            total_bytes += len(payload)
            if total_bytes > MAX_SOURCE_BYTES:
                raise EngineGraphError("fact graph exceeds its aggregate size limit")
            fact = _parse_fact(payload, expected_id, name)
            if fact.fact_id in facts:
                raise EngineGraphError(f"duplicate fact id: {fact.fact_id}")
            facts[fact.fact_id] = fact
            fact_payloads[fact.fact_id] = payload
            source_parts.append((f"facts/{name}", payload))

        target_payload = _read_regular_file(
            project_fd, "TARGET.md", limit=MAX_TARGET_BYTES, missing_ok=True
        )

        def optional_bound_file(parent: int, name: str, limit: int) -> bytes | None:
            try:
                return _read_regular_file(
                    parent, name, limit=limit, missing_ok=True
                )
            except EngineGraphError:
                return None

        receipt_payload = optional_bound_file(
            project_fd, "engine_receipt.json", MAX_RECEIPT_BYTES
        )
        problem_artifact = optional_bound_file(
            project_fd, "PROBLEM.md", MAX_PROBLEM_ARTIFACT_BYTES
        )
        proof_payload = optional_bound_file(
            root_fd, "proof.md", MAX_PROOF_ARTIFACT_BYTES
        )
        source_parts.append(("TARGET.md", target_payload or b""))
        source_parts.append(("engine_receipt.json", receipt_payload or b""))
        source_parts.append(("PROBLEM.md", problem_artifact or b""))
        source_parts.append(("proof.md", proof_payload or b""))
        targets = _parse_targets(target_payload)
        unknown_targets = [target for target in targets if target not in facts]
        if unknown_targets:
            raise EngineGraphError(
                f"TARGET.md names an unknown fact: {unknown_targets[0]}"
            )
        order, depths = _topological_order(facts)
        problem_ids = {fact.problem_id for fact in facts.values()}
        if len(problem_ids) > 1:
            raise EngineGraphError("fact graph mixes multiple problem ids")

        receipt: dict[str, Any] | None = None
        proof_labels: dict[str, str] | None = None
        if _receipt_schema_version(receipt_payload) in _cgm_schema.CGM_RECEIPT_SCHEMA_VERSIONS:
            receipt = _cgm_receipt(receipt_payload)
            provenance = _receipt_provenance_v2(
                receipt,
                targets=targets,
                target_payload=target_payload,
                facts=facts,
                fact_payloads=fact_payloads,
                problem_artifact=problem_artifact,
                proof_payload=proof_payload,
            )
            if receipt is not None and _receipt_lists_facts(receipt, facts, fact_payloads):
                # The names stay with the facts: a Sandbox edit of proof.md
                # unbinds the receipt, not the lemma names proof.md was
                # published with (its headings keep them).
                proof_labels = _proof_labels(receipt)
            if provenance.get("receipt_status") == "invalid":
                receipt = None
        else:
            provenance = _receipt_provenance(
                receipt_payload,
                targets=targets,
                target_payload=target_payload,
                facts=facts,
                fact_payloads=fact_payloads,
                problem_artifact=problem_artifact,
                proof_payload=proof_payload,
            )
        return _FactProject(
            facts=facts,
            fact_payloads=fact_payloads,
            targets=targets,
            order=order,
            depths=depths,
            problem_ids=problem_ids,
            source_parts=source_parts,
            provenance=provenance,
            receipt=receipt,
            proof_labels=proof_labels,
        )
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _math_harness_graph(workspace: Path) -> dict[str, Any]:
    project = _read_fact_project(workspace)
    if project is None:
        return _empty_math_harness_graph()
    facts, targets = project.facts, project.targets
    target_set = set(targets)
    attested = project.provenance.get("attestation") == "harness-gate-receipt"
    labels = (
        {item["fact_id"]: item["label"] for item in project.receipt["facts"]}
        if project.receipt is not None
        else {}
    )
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for fact_id in project.order:
        fact = facts[fact_id]
        proof_excerpt = _bounded_text(fact.proof, MAX_PROOF_EXCERPT)
        meta: dict[str, Any] = {
            "author": _bounded_text(fact.author, 240),
            "problem_id": _bounded_text(fact.problem_id, 240),
            "depth": project.depths[fact_id],
            "is_target": fact_id in target_set,
            "proof_excerpt": proof_excerpt,
            "proof_truncated": len(fact.proof.strip()) > MAX_PROOF_EXCERPT,
            "intuition": _bounded_text(fact.intuition, MAX_INTUITION_LENGTH),
            # Accepted by the runner's (model) verifier/critic gate, as bound
            # by a valid receipt; never a formal certificate.
            "verifier_accepted": attested or project.receipt is not None,
        }
        if fact_id in labels:
            meta["label"] = _bounded_text(labels[fact_id], 32)
        nodes.append(
            {
                "id": fact_id,
                "kind": "conclusion" if fact_id in target_set else "lemma",
                "label": _bounded_math_label(_SPACE_RE.sub(" ", fact.statement), 120),
                "statement": _bounded_text(fact.statement, MAX_STATEMENT_LENGTH),
                "citations": list(fact.citations),
                "meta": meta,
            }
        )
        for pred in sorted(fact.predecessors):
            edges.append(
                {"from": pred, "to": fact_id, "label": "fact dependency"}
            )
    if len(edges) > MAX_EDGES:
        raise EngineGraphError("fact graph exceeds its edge limit")

    problem_label = next(iter(project.problem_ids), "")
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "engine-graph",
        "status": "complete" if targets else ("partial" if nodes else "none"),
        "kind": "engine",
        "model": "deterministic-adapter",
        "source": "math_harness/project/fact_graph",
        "source_sha256": _source_hash(project.source_parts),
        "graph_role": "fact-dependency",
        "selection": {
            "target_source": "TARGET.md" if targets else "unset",
            "target_ids": list(targets),
            "complete_closure": bool(targets),
        },
        "provenance": {
            "engine": MATH_HARNESS_ENGINE,
            "producer": "math-harness",
            "adapter": "math-harness-fact-graph@1",
            "semantics": "fact-predecessor",
            **project.provenance,
            "certificate": False,
            "proof_logic": True,
            "lean_verified": False,
            "acyclic": True,
            "all_predecessors_known": True,
            "truncated": False,
        },
        "graph": {
            "title": (
                f"Math Harness facts · {problem_label}"
                if problem_label
                else "Math Harness fact graph"
            ),
            "nodes": nodes,
            "edges": edges,
        },
    }
    return _validate_artifact(artifact)


def load_fact_dag(workspace: Path) -> list[dict[str, Any]] | None:
    """Return the run's math_harness fact DAG in topological order.

    Each item is ``{"fact_id", "statement", "proof", "predecessors":
    [fact_id, ...], "is_target": bool, "attested": bool}`` with the full
    statement and proof text as stored in the CGM fact file (predecessor
    order as written).  ``attested`` is true only when the whole graph carries
    a valid ``harness-gate-receipt`` attestation (a model-verifier acceptance,
    never a Lean certificate).  Returns ``None`` when the run has no readable
    math_harness fact graph: no project, no facts, or artifacts that fail the
    fail-closed validation (unsafe paths, content-id mismatch, cycles, unknown
    predecessors).  Never invokes a model and never writes.
    """
    try:
        project = _read_fact_project(Path(workspace))
    except EngineGraphError:
        return None
    if project is None or not project.facts:
        return None
    attested = project.provenance.get("attestation") == "harness-gate-receipt"
    target_set = set(project.targets)
    return [
        {
            "fact_id": fact_id,
            "statement": project.facts[fact_id].statement,
            "proof": project.facts[fact_id].proof,
            "predecessors": list(project.facts[fact_id].predecessors),
            "is_target": fact_id in target_set,
            "attested": attested,
        }
        for fact_id in project.order
    ]


def load_proof_labels(workspace: Path) -> dict[str, str] | None:
    """The labels the run's ``proof.md`` names its lemmas by, if any.

    ``{fact_id: label}`` for every lemma when the run's CGM receipt records
    the ``verified-closure@2`` rendering ("Lemma L1") for exactly these fact
    files, even after a Sandbox edit of ``proof.md`` has unbound the receipt
    (the edited headings keep the published names); ``None`` when it records
    @1 (lemmas numbered "Lemma 1", "Lemma 2", ... in topological order) or
    the run has no such receipt.  Pass it to ``render_fact_closure`` to
    reproduce the published rendering.  Never invokes a model and never
    writes.
    """
    try:
        project = _read_fact_project(Path(workspace))
    except EngineGraphError:
        return None
    return None if project is None else project.proof_labels


def render_fact_closure(
    facts: Sequence[Mapping[str, Any]],
    target_id: str,
    *,
    labels: Mapping[str, str] | None = None,
) -> str:
    """Pure deterministic rendering of a target's verified closure.

    ``facts`` may be ``load_fact_dag`` items (or any mappings with
    ``fact_id``, ``statement``, ``proof`` and ``predecessors``).  With the
    run's ``load_proof_labels`` this is the exact rendering the CGM runner
    published as ``proof.md`` and that attestation recomputes (``None``:
    ``verified-closure@1``; labels: ``verified-closure@2``).  Raises
    ``EngineGraphError`` for a malformed DAG or unusable labels.
    """
    try:
        return _cgm_closure.render_verified_closure(facts, target_id, labels=labels)
    except _cgm_closure.ClosureError as exc:
        raise EngineGraphError(str(exc)) from exc


def _execution_graph(run_record: Mapping[str, Any] | None) -> dict[str, Any]:
    run = run_record if isinstance(run_record, Mapping) else {}
    engine = _bounded_text(run.get("engine") or "unknown", 80) or "unknown"
    run_status = _bounded_text(run.get("status"), 80).lower()
    raw_agents = run.get("agents")
    agents = raw_agents if isinstance(raw_agents, list) else []
    if len(agents) > MAX_NODES:
        raise EngineGraphError("execution graph exceeds its node limit")

    nodes: list[dict[str, Any]] = []
    raw_id_map: dict[str, str] = {}
    used: set[str] = set()
    for index, raw_agent in enumerate(agents):
        agent = raw_agent if isinstance(raw_agent, Mapping) else {}
        raw_id = _bounded_text(
            agent.get("trace_id") or agent.get("id") or agent.get("agent_id"),
            MAX_ID_LENGTH,
        )
        candidate = raw_id or f"agent-{index + 1:03d}"
        node_id = candidate
        suffix = 2
        while node_id in used:
            tail = f"-{suffix}"
            node_id = candidate[: MAX_ID_LENGTH - len(tail)] + tail
            suffix += 1
        used.add(node_id)
        if raw_id and raw_id not in raw_id_map:
            raw_id_map[raw_id] = node_id
        stage = _bounded_text(
            agent.get("stage_name") or agent.get("name") or agent.get("role"), 120
        )
        label = stage or f"Agent {index + 1}"
        status = _bounded_text(agent.get("status"), 80)
        model = _bounded_text(agent.get("model"), 120)
        output = _bounded_text(
            agent.get("output") or agent.get("summary") or agent.get("message"),
            MAX_STATEMENT_LENGTH,
        )
        details = [part for part in (status, model, output) if part]
        nodes.append(
            {
                "id": node_id,
                "kind": "step",
                "label": label,
                "statement": "\n".join(details) or "Recorded engine step",
                "citations": [],
                "meta": {
                    "role": _bounded_text(agent.get("role"), 120),
                    "stage": stage,
                    "status": status,
                    "model": model,
                    "call_seq": _bounded_positive_int(
                        agent.get("call_seq"), index + 1
                    ),
                },
            }
        )

    raw_edges = run.get("edges")
    candidates = raw_edges if isinstance(raw_edges, list) else []
    if len(candidates) > MAX_EDGES:
        raise EngineGraphError("execution graph exceeds its edge limit")
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for raw_edge in candidates:
        if not isinstance(raw_edge, Mapping):
            continue
        raw_from = _bounded_text(raw_edge.get("from") or raw_edge.get("source"), MAX_ID_LENGTH)
        raw_to = _bounded_text(raw_edge.get("to") or raw_edge.get("target"), MAX_ID_LENGTH)
        source = raw_id_map.get(raw_from, raw_from if raw_from in used else "")
        target = raw_id_map.get(raw_to, raw_to if raw_to in used else "")
        if not source or not target:
            continue
        label = _bounded_text(raw_edge.get("label") or raw_edge.get("type") or "next", 80)
        key = (source, target, label)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append({"from": source, "to": target, "label": label})

    canonical_source = {
        "engine": engine,
        "run_id": _bounded_text(run.get("run_id"), MAX_ID_LENGTH),
        "run_status": run_status,
        "nodes": nodes,
        "edges": edges,
    }
    graph_status = (
        "none"
        if not nodes
        else "complete"
        if run_status in _TERMINAL_RUN_STATUSES
        else "partial"
    )
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "engine-graph",
        "status": graph_status,
        "kind": "engine",
        "model": "deterministic-adapter",
        "source": "run-record agents/edges",
        "source_sha256": hashlib.sha256(_canonical_bytes(canonical_source)).hexdigest(),
        "graph_role": "execution",
        "selection": {
            "target_source": "not-applicable",
            "target_ids": [],
            "complete_closure": False,
        },
        "provenance": {
            "engine": engine,
            "producer": "agent-monitor-run-record",
            "adapter": "execution-trace@1",
            "semantics": "agent-execution",
            "attestation": "run-record",
            "certificate": False,
            "proof_logic": False,
            "lean_verified": False,
            "acyclic": False,
            "all_predecessors_known": True,
            "truncated": False,
        },
        "graph": {
            "title": f"{engine} execution trace",
            "nodes": nodes,
            "edges": edges,
        },
    }
    return _validate_artifact(artifact)


def _read_workflow_sources(
    workspace: Path,
) -> tuple[dict[str, bytes], bytes, bytes, bytes] | None:
    root_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    root_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(os.fspath(workspace), root_flags)
    except OSError as exc:
        raise EngineGraphError("workspace is not a safe directory") from exc
    opened: list[int] = [root_fd]
    try:
        parent_fd = root_fd
        for part in MATH_HARNESS_PROJECT_PARTS:
            child = _open_directory(parent_fd, part, missing_ok=True)
            if child is None:
                return None
            opened.append(child)
            parent_fd = child
        project_fd = parent_fd
        receipt_payload = _read_regular_file(
            project_fd,
            "engine_receipt.json",
            limit=MAX_RECEIPT_BYTES,
            missing_ok=True,
        )
        if receipt_payload is None:
            return None
        memory_fd = _open_directory(project_fd, "global_memory", missing_ok=True)
        if memory_fd is None:
            return None
        opened.append(memory_fd)
        names: set[str] = set()
        allowed = set(_WORKFLOW_FILES) | set(_cgm_schema.MEMORY_FILES)
        try:
            with os.scandir(memory_fd) as entries:
                for entry_count, entry in enumerate(entries, start=1):
                    if entry_count > len(allowed):
                        raise EngineGraphError(
                            "workflow memory has unexpected directory entries"
                        )
                    names.add(entry.name)
        except EngineGraphError:
            raise
        except OSError as exc:
            raise EngineGraphError("could not enumerate workflow memory safely") from exc
        if not names:
            return None
        if not names.issubset(allowed):
            raise EngineGraphError("workflow memory file set is incomplete or unexpected")
        payloads: dict[str, bytes] = {}
        for name in sorted(names):
            payload = _read_regular_file(
                memory_fd, name, limit=MAX_WORKFLOW_FILE_BYTES
            )
            assert payload is not None
            payloads[name] = payload
        problem_payload = _read_regular_file(
            project_fd, "PROBLEM.md", limit=MAX_PROBLEM_ARTIFACT_BYTES
        )
        proof_payload = _read_regular_file(
            root_fd, "proof.md", limit=MAX_PROOF_ARTIFACT_BYTES
        )
        assert problem_payload is not None and proof_payload is not None
        return payloads, receipt_payload, problem_payload, proof_payload
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _workflow_jsonl(payload: bytes, name: str) -> list[dict[str, Any]]:
    if payload and not payload.endswith(b"\n"):
        raise EngineGraphError(f"workflow JSONL is not newline terminated: {name}")
    text_payload = _decode(payload, name)
    # JSONL rows end at "\n" only.  str.splitlines would also break inside a
    # JSON string at U+0085/U+2028/U+2029 (which json.dumps leaves raw).
    lines = text_payload.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_WORKFLOW_ROWS:
        raise EngineGraphError(f"workflow JSONL exceeds its row limit: {name}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise EngineGraphError(
                f"workflow JSONL contains a blank row: {name}:{line_number}"
            )
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise EngineGraphError(
                f"malformed workflow JSONL row: {name}:{line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise EngineGraphError(
                f"workflow JSONL row is not an object: {name}:{line_number}"
            )
        rows.append(row)
    return rows


def _workflow_text(
    value: object,
    *,
    field: str,
    limit: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise EngineGraphError(f"invalid workflow {field}")
    if not allow_empty and not value.strip():
        raise EngineGraphError(f"empty workflow {field}")
    return value


def _workflow_timestamp(value: object) -> str:
    timestamp = _workflow_text(value, field="timestamp", limit=64)
    if _WORKFLOW_TIMESTAMP_RE.fullmatch(timestamp) is None:
        raise EngineGraphError("invalid workflow timestamp")
    return timestamp


def _workflow_entry_id(
    kind: str,
    claim: str,
    timestamp: str,
    ordinal: int,
) -> str:
    payload = json.dumps(
        [kind, claim, MATH_HARNESS_AUTHOR, timestamp, ordinal],
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _workflow_edge_id(
    source: str,
    target: str,
    edge_type: str,
    timestamp: str,
    ordinal: int,
) -> str:
    payload = json.dumps(
        [source, target, edge_type, MATH_HARNESS_AUTHOR, timestamp, ordinal],
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _parse_workflow_entries(
    payload: bytes,
    *,
    name: str,
    kind: str,
    minimum: int,
    maximum: int,
) -> list[dict[str, Any]]:
    rows = _workflow_jsonl(payload, name)
    if not minimum <= len(rows) <= maximum:
        raise EngineGraphError(f"unexpected workflow row count in {name}")
    expected_verifiable = kind == "proof_attempt"
    allowed_status = {
        "plan": {"open"},
        "proof_attempt": {"verified", "unverified"},
        "verification": {"supported", "open"},
    }[kind]
    seen: set[str] = set()
    for ordinal, row in enumerate(rows, start=1):
        if set(row) != _WORKFLOW_ENTRY_FIELDS:
            raise EngineGraphError(f"unsupported workflow entry schema in {name}")
        entry_id = _workflow_text(row.get("id"), field="entry id", limit=16)
        claim = _workflow_text(row.get("claim"), field="claim", limit=4000)
        timestamp = _workflow_timestamp(row.get("timestamp_utc"))
        if (
            _FACT_ID_RE.fullmatch(entry_id) is None
            or entry_id in seen
            or entry_id != _workflow_entry_id(kind, claim, timestamp, ordinal)
        ):
            raise EngineGraphError(f"invalid workflow entry id in {name}")
        seen.add(entry_id)
        if row.get("author") != MATH_HARNESS_AUTHOR or row.get("kind") != kind:
            raise EngineGraphError(f"invalid workflow entry ownership in {name}")
        _workflow_text(row.get("evidence"), field="evidence", limit=120_000)
        if row.get("verifiable") is not expected_verifiable:
            raise EngineGraphError(f"invalid workflow verifiable flag in {name}")
        if row.get("status") not in allowed_status:
            raise EngineGraphError(f"invalid workflow status in {name}")
        fact_id = row.get("fact_id")
        if fact_id is not None and (
            not isinstance(fact_id, str) or _FACT_ID_RE.fullmatch(fact_id) is None
        ):
            raise EngineGraphError(f"invalid workflow fact reference in {name}")
        links = row.get("links")
        if not isinstance(links, dict) or set(links) != {"subgoal", "predecessors"}:
            raise EngineGraphError(f"invalid workflow links in {name}")
        if links.get("subgoal") != "goal:root":
            raise EngineGraphError(f"invalid workflow subgoal in {name}")
        predecessors = links.get("predecessors")
        if (
            not isinstance(predecessors, list)
            or len(predecessors) > MAX_NODES
            or len(predecessors) != len(set(predecessors))
            or any(
                not isinstance(item, str) or _FACT_ID_RE.fullmatch(item) is None
                for item in predecessors
            )
        ):
            raise EngineGraphError(f"invalid workflow predecessor links in {name}")
        if kind != "verification" and predecessors:
            raise EngineGraphError(f"unexpected workflow predecessor links in {name}")
        if row.get("glossary") != {}:
            raise EngineGraphError(f"unexpected workflow glossary in {name}")
        if kind == "plan" and fact_id is not None:
            raise EngineGraphError(f"plan unexpectedly references a fact in {name}")
    return rows


def _workflow_receipt(
    payload: bytes,
    *,
    problem_payload: bytes,
    proof_payload: bytes,
) -> dict[str, Any]:
    try:
        receipt = json.loads(_decode(payload, "engine_receipt.json"))
    except (json.JSONDecodeError, RecursionError, EngineGraphError) as exc:
        raise EngineGraphError("invalid workflow ownership receipt") from exc
    if not isinstance(receipt, dict) or not _RECEIPT_REQUIRED_FIELDS.issubset(receipt):
        raise EngineGraphError("invalid workflow ownership receipt")
    problem = _runner_problem_bytes(problem_payload)
    if problem is None:
        raise EngineGraphError("invalid workflow problem artifact")
    for field in (
        "source_sha256",
        "task_sha256",
        "critic_response_sha256",
        "binding_sha256",
        "problem_sha256",
        "proof_sha256",
    ):
        if not _is_sha256(receipt.get(field)):
            raise EngineGraphError("invalid workflow ownership receipt hash")
    if (
        hashlib.sha256(problem).hexdigest() != receipt.get("problem_sha256")
        or hashlib.sha256(proof_payload).hexdigest() != receipt.get("proof_sha256")
    ):
        raise EngineGraphError("workflow receipt does not bind problem/proof artifacts")
    binding = {
        "source_sha256": receipt.get("source_sha256"),
        "problem_sha256": receipt.get("problem_sha256"),
        "task_sha256": receipt.get("task_sha256"),
        "proof_sha256": receipt.get("proof_sha256"),
        "provider": receipt.get("provider"),
        "model": receipt.get("model"),
        "verdict": receipt.get("verdict"),
        "critic_verdict": receipt.get("critic_verdict"),
        "critic_response_sha256": receipt.get("critic_response_sha256"),
        "target_fact_ids": receipt.get("target_fact_ids"),
        "target_sha256": receipt.get("target_sha256"),
        "fact_bytes_sha256": receipt.get("fact_bytes_sha256"),
        "certificate": receipt.get("certificate"),
    }
    strings_present = all(
        isinstance(receipt.get(field), str) and bool(receipt[field].strip())
        for field in (
            "source",
            "problem_id",
            "provider",
            "model",
            "requested_model",
            "transport",
        )
    )
    if (
        receipt.get("schema_version") != 1
        or receipt.get("engine") != MATH_HARNESS_ENGINE
        or receipt.get("source") != "agent-monitor-math-harness-response-adapter@1"
        or receipt.get("semantics") != "model-critic"
        or receipt.get("critic") != "response-only-model-critic"
        or receipt.get("certificate") is not False
        or not strings_present
        or not isinstance(receipt.get("workflow"), dict)
        or receipt["workflow"].get("tools_enabled") is not False
        or _receipt_response_only_contract(receipt) is None
        or not isinstance(receipt.get("calls"), list)
        or not receipt["calls"]
        or hashlib.sha256(_canonical_bytes(binding)).hexdigest()
        != receipt.get("binding_sha256")
    ):
        raise EngineGraphError("invalid workflow ownership receipt")
    accepted = (
        receipt.get("verdict") == "accepted"
        and receipt.get("critic_verdict") == "correct"
    )
    rejected = (
        receipt.get("verdict") == "unattested"
        and receipt.get("critic_verdict") == "needs_revision"
    )
    if not (accepted or rejected):
        raise EngineGraphError("invalid workflow receipt verdict")
    if accepted:
        target_id = receipt.get("target_fact_id")
        if (
            not isinstance(target_id, str)
            or _FACT_ID_RE.fullmatch(target_id) is None
            or receipt.get("target_fact_ids") != [target_id]
            or not _is_sha256(receipt.get("target_sha256"))
            or not _is_sha256(receipt.get("fact_bytes_sha256"))
            or receipt.get("fact_path") != f"fact_graph/facts/{target_id}.md"
            or not isinstance(receipt.get("fact_bytes"), int)
            or isinstance(receipt.get("fact_bytes"), bool)
            or receipt["fact_bytes"] <= 0
        ):
            raise EngineGraphError("invalid accepted workflow receipt")
    elif (
        receipt.get("target_fact_id") is not None
        or receipt.get("target_fact_ids") != []
        or receipt.get("target_sha256") is not None
        or receipt.get("fact_path") is not None
        or receipt.get("fact_bytes") != 0
        or receipt.get("fact_bytes_sha256") is not None
    ):
        raise EngineGraphError("invalid unattested workflow receipt")
    return receipt


def _parse_workflow_edge_rows(
    payload: bytes,
    *,
    known_ids: set[str],
    plan_ids: set[str],
    attempt_ids: set[str],
    fact_ids: set[str],
    timestamp: str,
) -> list[dict[str, str]]:
    rows = _workflow_jsonl(payload, "edges.jsonl")
    if not rows:
        raise EngineGraphError("workflow edge memory is empty")
    edges: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    for row in rows:
        if set(row) != _WORKFLOW_EDGE_FIELDS:
            raise EngineGraphError("unsupported workflow edge schema")
        edge_id = _workflow_text(row.get("id"), field="edge id", limit=16)
        source = _workflow_text(row.get("src"), field="edge source", limit=MAX_ID_LENGTH)
        target = _workflow_text(row.get("dst"), field="edge target", limit=MAX_ID_LENGTH)
        edge_type = _workflow_text(row.get("type"), field="edge type", limit=32)
        if (
            _FACT_ID_RE.fullmatch(edge_id) is None
            or edge_id in seen_ids
            or source not in known_ids
            or target not in known_ids
            or source == target
            or row.get("timestamp_utc") != timestamp
            or row.get("author") != MATH_HARNESS_AUTHOR
            or row.get("technique") is not None
            or row.get("and_group") is not None
        ):
            raise EngineGraphError("invalid workflow edge")
        if edge_type == "addresses":
            valid_shape = source in plan_ids and target == "goal:root"
        elif edge_type == "supports":
            valid_shape = (
                source in attempt_ids or source in fact_ids
            ) and target == "goal:root"
        elif edge_type == "refines":
            valid_shape = source in attempt_ids and target in attempt_ids
        elif edge_type == "promoted-to":
            valid_shape = source in attempt_ids and target in fact_ids
        else:
            valid_shape = False
        if not valid_shape:
            raise EngineGraphError("invalid workflow edge type/endpoints")
        if not any(
            edge_id == _workflow_edge_id(
                source, target, edge_type, timestamp, ordinal
            )
            for ordinal in [*range(1, MAX_NODES + 1), 99]
        ):
            raise EngineGraphError("workflow edge id does not match its content")
        _workflow_text(row.get("rationale"), field="edge rationale", limit=4000)
        edge_key = (source, target, edge_type)
        if edge_key in seen_edges:
            raise EngineGraphError("duplicate workflow edge")
        seen_ids.add(edge_id)
        seen_edges.add(edge_key)
        edges.append({"from": source, "to": target, "label": edge_type})
    return edges


def _parse_workflow_annotation(
    payload: bytes,
    *,
    attempt_ids: list[str],
    timestamp: str,
    complete: bool,
) -> dict[str, Any]:
    rows = _workflow_jsonl(payload, "annotations.jsonl")
    if len(rows) != 1 or set(rows[0]) != _WORKFLOW_ANNOTATION_FIELDS:
        raise EngineGraphError("invalid workflow annotation schema")
    row = rows[0]
    annotation_id = _workflow_text(
        row.get("id"), field="annotation id", limit=16
    )
    expected_payload = json.dumps(
        ["goal:root", MATH_HARNESS_AUTHOR, timestamp],
        ensure_ascii=False,
    ).encode("utf-8")
    expected_id = hashlib.sha256(expected_payload).hexdigest()[:16]
    expected_status = "closed" if complete else "active"
    expected_score: float | None = 1.0 if complete else None
    if (
        annotation_id != expected_id
        or row.get("timestamp_utc") != timestamp
        or row.get("author") != MATH_HARNESS_AUTHOR
        or row.get("node_id") != "goal:root"
        or row.get("status") != expected_status
        or row.get("score") != expected_score
        or row.get("evidence_considered") != attempt_ids
    ):
        raise EngineGraphError("invalid workflow annotation")
    comment = _workflow_text(row.get("comment"), field="annotation comment", limit=4000)
    return {
        "id": annotation_id,
        "status": expected_status,
        "score": expected_score,
        "comment": _bounded_text(comment, 1000),
        "evidence_considered": list(attempt_ids),
    }


def _assert_workflow_acyclic(
    node_ids: set[str],
    edges: list[dict[str, str]],
) -> None:
    indegree = {node_id: 0 for node_id in node_ids}
    children: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        source, target = edge["from"], edge["to"]
        children[source].append(target)
        indegree[target] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    visited = 0
    while ready:
        node_id = heapq.heappop(ready)
        visited += 1
        for child in children[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if visited != len(node_ids):
        raise EngineGraphError("workflow graph contains a cycle")


def _empty_workflow_graph() -> dict[str, Any]:
    return _validate_artifact(
        {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "engine-graph",
            "status": "none",
            "kind": "engine",
            "model": "deterministic-adapter",
            "source": "math_harness/project/global_memory",
            "source_sha256": "",
            "graph_role": "workflow",
            "selection": {
                "target_source": "unset",
                "target_ids": [],
                "complete_closure": False,
            },
            "provenance": {
                "engine": MATH_HARNESS_ENGINE,
                "producer": "math-harness",
                "adapter": "math-harness-workflow@1",
                "semantics": "exploration-workflow",
                "attestation": "unattested",
                "receipt_status": "missing",
                "memory_receipt_bound": False,
                "certificate": False,
                "proof_logic": False,
                "lean_verified": False,
                "acyclic": True,
                "all_predecessors_known": True,
                "truncated": False,
            },
            "graph": {
                "title": "Math Harness exploration workflow",
                "nodes": [],
                "edges": [],
            },
        }
    )


def _math_harness_workflow_graph(
    workspace: Path,
    fact_graph: Mapping[str, Any],
) -> dict[str, Any] | None:
    sources = _read_workflow_sources(workspace)
    if sources is None:
        return None
    memory_payloads, receipt_payload, problem_payload, proof_payload = sources
    if _receipt_schema_version(receipt_payload) in _cgm_schema.CGM_RECEIPT_SCHEMA_VERSIONS:
        return _cgm_workflow_graph(
            memory_payloads,
            receipt_payload=receipt_payload,
            problem_payload=problem_payload,
            proof_payload=proof_payload,
            fact_graph=fact_graph,
        )
    if set(memory_payloads) != set(_WORKFLOW_FILES):
        raise EngineGraphError("workflow memory file set is incomplete or unexpected")
    receipt = _workflow_receipt(
        receipt_payload,
        problem_payload=problem_payload,
        proof_payload=proof_payload,
    )
    plans = _parse_workflow_entries(
        memory_payloads["plan.jsonl"],
        name="plan.jsonl",
        kind="plan",
        minimum=1,
        maximum=3,
    )
    attempts = _parse_workflow_entries(
        memory_payloads["proof_attempt.jsonl"],
        name="proof_attempt.jsonl",
        kind="proof_attempt",
        minimum=1,
        maximum=4,
    )
    verifications = _parse_workflow_entries(
        memory_payloads["verification.jsonl"],
        name="verification.jsonl",
        kind="verification",
        minimum=1,
        maximum=1,
    )
    if len(attempts) not in {len(plans), len(plans) + 1}:
        raise EngineGraphError("workflow plan/attempt counts do not match")
    all_entries = [*plans, *attempts, *verifications]
    timestamps = {row["timestamp_utc"] for row in all_entries}
    if len(timestamps) != 1:
        raise EngineGraphError("workflow entries do not share one timestamp")
    timestamp = next(iter(timestamps))
    entry_ids = [row["id"] for row in all_entries]
    if len(entry_ids) != len(set(entry_ids)):
        raise EngineGraphError("duplicate workflow entry id")
    attempt_ids = [row["id"] for row in attempts]
    verification = verifications[0]
    if verification["links"]["predecessors"] != attempt_ids:
        raise EngineGraphError("verification does not bind every proof attempt")

    workflow_meta = receipt["workflow"]
    revision_count = len(attempts) - len(plans)
    if (
        workflow_meta.get("planner_calls") != 1
        or workflow_meta.get("candidate_calls") != len(plans)
        or workflow_meta.get("critic_calls") != 1
        or workflow_meta.get("revision_calls") != revision_count
        or workflow_meta.get("max_candidates") != 3
    ):
        raise EngineGraphError("workflow receipt call counts do not match memory")
    expected_stages = (
        ["planner"]
        + [f"candidate-{index}" for index in range(1, len(plans) + 1)]
        + ["critic"]
        + (["revision"] if revision_count else [])
    )
    if (
        len(receipt["calls"]) != len(expected_stages)
        or any(
            not isinstance(call, dict) or call.get("stage") != stage
            for call, stage in zip(receipt["calls"], expected_stages)
        )
    ):
        raise EngineGraphError("workflow receipt stages do not match memory")

    receipt_complete = (
        receipt["verdict"] == "accepted"
        and receipt["critic_verdict"] == "correct"
    )
    verified_attempts = [row for row in attempts if row["status"] == "verified"]
    if receipt_complete:
        target_id = receipt["target_fact_id"]
        if (
            len(verified_attempts) != 1
            or verified_attempts[0]["fact_id"] != target_id
            or verification["status"] != "supported"
            or verification["fact_id"] != target_id
            or fact_graph.get("provenance", {}).get("attestation")
            != "harness-gate-receipt"
            or fact_graph.get("selection", {}).get("target_ids") != [target_id]
        ):
            raise EngineGraphError("accepted workflow does not match its fact receipt")
    elif (
        verified_attempts
        or any(row["fact_id"] is not None for row in attempts)
        or verification["status"] != "open"
        or verification["fact_id"] is not None
    ):
        raise EngineGraphError("unattested workflow contains accepted proof state")

    annotation = _parse_workflow_annotation(
        memory_payloads["annotations.jsonl"],
        attempt_ids=attempt_ids,
        timestamp=timestamp,
        complete=receipt_complete,
    )
    fact_nodes = {
        node["id"]: node
        for node in fact_graph.get("graph", {}).get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    fact_ids = set(fact_nodes)
    plan_ids = {row["id"] for row in plans}
    attempt_id_set = set(attempt_ids)
    verification_ids = {verification["id"]}
    known_ids = {
        "goal:root",
        *plan_ids,
        *attempt_id_set,
        *verification_ids,
        *fact_ids,
    }
    if len(known_ids) > MAX_NODES:
        raise EngineGraphError("workflow graph exceeds its node limit")
    edges = _parse_workflow_edge_rows(
        memory_payloads["edges.jsonl"],
        known_ids=known_ids,
        plan_ids=plan_ids,
        attempt_ids=attempt_id_set,
        fact_ids=fact_ids,
        timestamp=timestamp,
    )
    seen_edges = {
        (edge["from"], edge["to"], edge["label"])
        for edge in edges
    }
    for predecessor in attempt_ids:
        key = (predecessor, verification["id"], "critic-input")
        if key not in seen_edges:
            edges.append(
                {"from": key[0], "to": key[1], "label": key[2]}
            )
            seen_edges.add(key)
    verdict_key = (verification["id"], "goal:root", "critic-verdict")
    if verdict_key not in seen_edges:
        edges.append(
            {
                "from": verdict_key[0],
                "to": verdict_key[1],
                "label": verdict_key[2],
            }
        )
    if len(edges) > MAX_EDGES:
        raise EngineGraphError("workflow graph exceeds its edge limit")
    _assert_workflow_acyclic(known_ids, edges)

    problem = _runner_problem_bytes(problem_payload)
    assert problem is not None
    goal_statement = problem.decode("utf-8")
    nodes: list[dict[str, Any]] = [
        {
            "id": "goal:root",
            "kind": "claim",
            "label": "Root problem",
            "statement": _bounded_text(goal_statement, MAX_STATEMENT_LENGTH),
            "citations": [],
            "meta": {
                "workflow_kind": "goal",
                "status": annotation["status"],
                "annotation": annotation,
                "timestamp_utc": timestamp,
            },
        }
    ]
    call_seq = 1
    for row in all_entries:
        call_seq += 1
        evidence = row["evidence"]
        nodes.append(
            {
                "id": row["id"],
                "kind": "claim" if row["kind"] == "verification" else "step",
                "label": _bounded_text(row["claim"], 120),
                "statement": _bounded_text(evidence, MAX_STATEMENT_LENGTH),
                "citations": [],
                "meta": {
                    "workflow_kind": row["kind"],
                    "author": row["author"],
                    "timestamp_utc": row["timestamp_utc"],
                    "status": row["status"],
                    "verifiable": row["verifiable"],
                    "fact_id": row["fact_id"],
                    "subgoal": row["links"]["subgoal"],
                    "predecessors": list(row["links"]["predecessors"]),
                    "evidence_truncated": len(evidence.strip()) > MAX_STATEMENT_LENGTH,
                    "call_seq": call_seq,
                },
            }
        )
    for node_id in sorted(fact_nodes):
        fact_node = fact_nodes[node_id]
        fact_meta = dict(fact_node.get("meta") or {})
        fact_meta.update(
            {
                "workflow_kind": "fact",
                "receipt_attested": fact_graph.get("provenance", {}).get(
                    "attestation"
                )
                == "harness-gate-receipt",
            }
        )
        nodes.append(
            {
                "id": node_id,
                "kind": fact_node["kind"],
                "label": fact_node["label"],
                "statement": fact_node["statement"],
                "citations": list(fact_node["citations"]),
                "meta": fact_meta,
            }
        )

    source_parts = [
        (
            "fact_graph.source_sha256",
            str(fact_graph.get("source_sha256") or "").encode("ascii"),
        ),
        ("PROBLEM.md", problem_payload),
        ("proof.md", proof_payload),
        ("engine_receipt.json", receipt_payload),
    ]
    source_parts.extend(
        (f"global_memory/{name}", memory_payloads[name])
        for name in _WORKFLOW_FILES
    )
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "engine-graph",
        "status": "complete" if receipt_complete else "partial",
        "kind": "engine",
        "model": "deterministic-adapter",
        "source": "math_harness/project/global_memory",
        "source_sha256": _source_hash(source_parts),
        "graph_role": "workflow",
        "selection": {
            "target_source": "workflow-goal",
            "target_ids": ["goal:root"],
            "complete_closure": False,
        },
        "provenance": {
            "engine": MATH_HARNESS_ENGINE,
            "producer": "math-harness",
            "adapter": "math-harness-workflow@1",
            "semantics": "exploration-workflow",
            "attestation": "unattested",
            "receipt_status": "runner-receipt-valid",
            "critic_verdict": receipt["critic_verdict"],
            "memory_receipt_bound": False,
            "response_only_transport": receipt["workflow"][
                "response_only_contract"
            ]["transport"],
            "response_only_isolation_version": receipt["workflow"][
                "response_only_contract"
            ]["isolation_version"],
            "fact_receipt_status": fact_graph.get("provenance", {}).get(
                "receipt_status", "missing"
            ),
            "certificate": False,
            "proof_logic": False,
            "lean_verified": False,
            "acyclic": True,
            "all_predecessors_known": True,
            "truncated": any(
                node.get("meta", {}).get("evidence_truncated") is True
                for node in nodes
            ),
        },
        "graph": {
            "title": "Math Harness exploration workflow",
            "nodes": nodes,
            "edges": edges,
        },
    }
    return _validate_artifact(artifact)


# ---------------------------------------------------------------------------
# CGM exploring graph (receipt schema 2)
# ---------------------------------------------------------------------------

# CGM kinds are projected onto the shared node kinds; the real kind is kept in
# ``meta.workflow_kind``.
_CGM_NODE_KIND = {
    "goal": "claim",
    "plan": "step",
    "proof_attempt": "step",
    "verification": "claim",
    "conclusion": "claim",
    "example": "case",
    "counterexample": "contradiction",
    "dead_end": "contradiction",
    "obstacle": "assumption",
}
_CGM_AND_GROUP_RE = re.compile(r"^plan-[1-9][0-9]?$")
_CGM_EMITTED_EDGE_TYPES = frozenset(_cgm_schema.EDGE_TYPES) - {"supersedes", "prioritizes"}
_VERIFICATION_STATUS = {"correct": "supported", "wrong": "challenged", "error": "open"}


def _parse_cgm_entries(
    payload: bytes, *, name: str, kind: str
) -> list[dict[str, Any]]:
    rows = _workflow_jsonl(payload, name)
    allowed_status = _cgm_schema.ENTRY_STATUSES[kind]
    for ordinal, row in enumerate(rows, start=1):
        if set(row) != _cgm_schema.ENTRY_FIELDS:
            raise EngineGraphError(f"unsupported workflow entry schema in {name}")
        entry_id = _workflow_text(row.get("id"), field="entry id", limit=16)
        claim = _workflow_text(row.get("claim"), field="claim", limit=4000)
        timestamp = _workflow_timestamp(row.get("timestamp_utc"))
        if (
            _FACT_ID_RE.fullmatch(entry_id) is None
            or entry_id
            != _cgm_schema.entry_id(kind, claim, CGM_AUTHOR, timestamp, ordinal)
        ):
            raise EngineGraphError(f"invalid workflow entry id in {name}")
        if row.get("author") != CGM_AUTHOR or row.get("kind") != kind:
            raise EngineGraphError(f"invalid workflow entry ownership in {name}")
        _workflow_text(row.get("evidence"), field="evidence", limit=120_000)
        if row.get("verifiable") is not _cgm_schema.GLOBAL_KINDS[kind]:
            raise EngineGraphError(f"invalid workflow verifiable flag in {name}")
        if row.get("status") not in allowed_status:
            raise EngineGraphError(f"invalid workflow status in {name}")
        fact_id = row.get("fact_id")
        if fact_id is not None and (
            not isinstance(fact_id, str) or _FACT_ID_RE.fullmatch(fact_id) is None
        ):
            raise EngineGraphError(f"invalid workflow fact reference in {name}")
        links = row.get("links")
        if not isinstance(links, dict) or set(links) != {"subgoal", "predecessors"}:
            raise EngineGraphError(f"invalid workflow links in {name}")
        subgoal = links.get("subgoal")
        if not isinstance(subgoal, str) or _cgm_schema.GOAL_ID_RE.fullmatch(subgoal) is None:
            raise EngineGraphError(f"invalid workflow subgoal in {name}")
        predecessors = links.get("predecessors")
        if (
            not isinstance(predecessors, list)
            or len(predecessors) > 1
            or any(
                not isinstance(item, str) or _FACT_ID_RE.fullmatch(item) is None
                for item in predecessors
            )
        ):
            raise EngineGraphError(f"invalid workflow predecessor links in {name}")
        if row.get("glossary") != {}:
            raise EngineGraphError(f"unexpected workflow glossary in {name}")
    return rows


def _cgm_workflow_graph(
    memory_payloads: Mapping[str, bytes],
    *,
    receipt_payload: bytes,
    problem_payload: bytes,
    proof_payload: bytes,
    fact_graph: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = _cgm_receipt(receipt_payload)
    if receipt is None:
        raise EngineGraphError("invalid workflow ownership receipt")
    problem = _runner_problem_bytes(problem_payload)
    if (
        problem is None
        or hashlib.sha256(problem).hexdigest() != receipt["problem_sha256"]
        or hashlib.sha256(proof_payload).hexdigest() != receipt["proof_sha256"]
    ):
        raise EngineGraphError("workflow receipt does not bind problem/proof artifacts")
    if set(memory_payloads) != set(receipt["memory"]):
        raise EngineGraphError("workflow memory file set is incomplete or unexpected")
    for name, payload in memory_payloads.items():
        if hashlib.sha256(payload).hexdigest() != receipt["memory"][name]:
            raise EngineGraphError("workflow memory does not match its receipt")

    accepted = receipt["verdict"] == "accepted"
    target_id = receipt["target_fact_id"]
    fact_nodes = {
        node["id"]: node
        for node in fact_graph.get("graph", {}).get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    fact_ids = set(fact_nodes)
    if fact_ids != {item["fact_id"] for item in receipt["facts"]}:
        raise EngineGraphError("workflow receipt does not bind the fact graph")
    if accepted and (
        fact_graph.get("provenance", {}).get("attestation") != "harness-gate-receipt"
        or fact_graph.get("selection", {}).get("target_ids") != [target_id]
    ):
        raise EngineGraphError("accepted workflow does not match its fact receipt")

    entries: dict[str, list[dict[str, Any]]] = {}
    for name in _cgm_schema.ENTRY_FILES:
        if name in memory_payloads:
            entries[name[: -len(".jsonl")]] = _parse_cgm_entries(
                memory_payloads[name], name=name, kind=name[: -len(".jsonl")]
            )
    edge_rows = _workflow_jsonl(memory_payloads["edges.jsonl"], "edges.jsonl")
    annotation_rows = _workflow_jsonl(
        memory_payloads["annotations.jsonl"], "annotations.jsonl"
    )
    rows_expected = receipt["workflow"]["rows"]
    counts = {f"{kind}.jsonl": len(rows) for kind, rows in entries.items()}
    counts["edges.jsonl"] = len(edge_rows)
    counts["annotations.jsonl"] = len(annotation_rows)
    if counts != rows_expected:
        raise EngineGraphError("workflow receipt row counts do not match memory")

    all_rows = [row for rows in entries.values() for row in rows]
    timestamps = {
        row.get("timestamp_utc") for row in [*all_rows, *edge_rows, *annotation_rows]
    }
    if len(timestamps) != 1:
        raise EngineGraphError("workflow entries do not share one timestamp")
    timestamp = _workflow_timestamp(next(iter(timestamps)))
    kind_of: dict[str, str] = {}
    for row in all_rows:
        if row["id"] in kind_of or row["id"] in fact_ids:
            raise EngineGraphError("duplicate workflow entry id")
        kind_of[row["id"]] = row["kind"]
    plans = entries.get("plan", [])
    attempts = entries.get("proof_attempt", [])
    verifications = entries.get("verification", [])
    if not 1 <= len(plans) <= 3 or not attempts:
        raise EngineGraphError("unexpected workflow row count")
    attempt_ids = [row["id"] for row in attempts]
    for row in all_rows:
        if row["links"]["subgoal"] != "goal:root":
            raise EngineGraphError("invalid workflow subgoal link")
    for position, row in enumerate(attempts):
        previous = set(attempt_ids[:position])
        if any(pred not in previous for pred in row["links"]["predecessors"]):
            raise EngineGraphError("invalid proof-attempt lineage")
        verified = row["status"] == "verified"
        if verified != (accepted and row["fact_id"] == target_id) or (
            not verified and row["fact_id"] is not None
        ):
            raise EngineGraphError("proof attempt state does not match the receipt")
    if accepted and sum(row["status"] == "verified" for row in attempts) != 1:
        raise EngineGraphError("accepted workflow must have one verified attempt")
    if len(verifications) != len(receipt["verdicts"]):
        raise EngineGraphError("verification memory does not match receipt verdicts")
    for row, verdict in zip(verifications, receipt["verdicts"]):
        predecessors = row["links"]["predecessors"]
        if (
            len(predecessors) != 1
            or predecessors[0] not in attempt_ids
            or row["status"] != _VERIFICATION_STATUS[verdict["verdict"]]
            or (row["fact_id"] is not None and (
                row["fact_id"] not in fact_ids or verdict["verdict"] != "correct"
            ))
        ):
            raise EngineGraphError("invalid verification memory row")
    for kind, rows in entries.items():
        if kind in {"plan", "proof_attempt", "verification"}:
            continue
        for row in rows:
            if row["fact_id"] is not None or any(
                pred not in attempt_ids for pred in row["links"]["predecessors"]
            ):
                raise EngineGraphError(f"invalid {kind} memory row")
    for row in plans:
        if row["fact_id"] is not None or row["links"]["predecessors"]:
            raise EngineGraphError("invalid plan memory row")

    # Goal nodes are symbolic: goal:root plus every decomposition target.
    goal_ids: list[str] = ["goal:root"]
    for row in edge_rows:
        if (
            isinstance(row, dict)
            and row.get("type") == "decomposes-into"
            and isinstance(row.get("dst"), str)
            and _cgm_schema.GOAL_ID_RE.fullmatch(row["dst"])
            and row["dst"] not in goal_ids
        ):
            goal_ids.append(row["dst"])
    for goal in goal_ids:
        kind_of[goal] = "goal"
    for fact_id in fact_ids:
        kind_of[fact_id] = "fact"
    if len(kind_of) > MAX_NODES:
        raise EngineGraphError("workflow graph exceeds its node limit")

    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    goal_meta: dict[str, dict[str, Any]] = {}
    plan_meta: dict[str, dict[str, Any]] = {}
    promoted: dict[str, int] = {}
    for ordinal, row in enumerate(edge_rows, start=1):
        if set(row) != _cgm_schema.EDGE_FIELDS:
            raise EngineGraphError("unsupported workflow edge schema")
        source = _workflow_text(row.get("src"), field="edge source", limit=MAX_ID_LENGTH)
        target = _workflow_text(row.get("dst"), field="edge target", limit=MAX_ID_LENGTH)
        edge_type = _workflow_text(row.get("type"), field="edge type", limit=32)
        technique = row.get("technique")
        and_group = row.get("and_group")
        if (
            row.get("author") != CGM_AUTHOR
            or row.get("timestamp_utc") != timestamp
            or edge_type not in _CGM_EMITTED_EDGE_TYPES
            or source not in kind_of
            or target not in kind_of
            or source == target
            or row.get("id")
            != _cgm_schema.edge_id(source, target, edge_type, CGM_AUTHOR, timestamp, ordinal)
        ):
            raise EngineGraphError("invalid workflow edge")
        if edge_type == "decomposes-into":
            if (
                technique not in _cgm_schema.TECHNIQUES
                or not isinstance(and_group, str)
                or _CGM_AND_GROUP_RE.fullmatch(and_group) is None
            ):
                raise EngineGraphError("invalid workflow decomposition edge")
        elif technique is not None or and_group is not None:
            raise EngineGraphError("invalid workflow edge")
        src_kind, dst_kind = kind_of[source], kind_of[target]
        root = "goal:root"
        valid_shape = {
            "addresses": src_kind in {"plan", "proof_attempt"} and dst_kind == "goal",
            "decomposes-into": src_kind == "plan" and dst_kind == "goal" and target != root,
            "alternative-of": src_kind == "plan" and dst_kind == "plan",
            "refines": src_kind in {"plan", "proof_attempt"} and dst_kind == "proof_attempt",
            "depends-on": src_kind == "goal" and dst_kind == "goal"
            and root not in {source, target},
            "reduces-to": source == root and dst_kind == "goal",
            "equivalent-to": source == root and dst_kind == "goal",
            "generalizes": target == root and src_kind == "goal",
            "supports": (
                src_kind in {"conclusion", "example"} and dst_kind in {"plan", "goal"}
            )
            or (src_kind == "fact" and dst_kind == "goal"),
            "blocks": src_kind in {"dead_end", "obstacle"} and dst_kind in {"plan", "goal"},
            "refutes": src_kind == "counterexample" and dst_kind in {"plan", "goal"},
            "promoted-to": src_kind == "proof_attempt" and dst_kind == "fact",
        }.get(edge_type, False)
        if not valid_shape:
            raise EngineGraphError("invalid workflow edge type/endpoints")
        rationale = _workflow_text(row.get("rationale"), field="edge rationale", limit=4000)
        key = (source, target, edge_type)
        if key in seen_edges:
            raise EngineGraphError("duplicate workflow edge")
        seen_edges.add(key)
        edges.append({"from": source, "to": target, "label": edge_type})
        if edge_type == "decomposes-into":
            goal_meta[target] = {
                "and_group": and_group,
                "technique": technique,
                "plan": source,
                "rationale": rationale,
            }
            shape = {"technique": technique, "and_group": and_group}
            if plan_meta.setdefault(source, shape) != shape:
                raise EngineGraphError("inconsistent workflow decomposition edges")
        if edge_type == "promoted-to":
            promoted[target] = promoted.get(target, 0) + 1
    if any(promoted.get(fact_id, 0) != 1 for fact_id in fact_ids):
        raise EngineGraphError("every fact needs exactly one promoted-to edge")
    # The strategist's technique opens every plan row; a decomposition's CGM
    # technique tag must be derived from it (goal transformations tag "plain").
    plan_technique: dict[str, str] = {}
    for row in plans:
        match = _cgm_schema.PLAN_TECHNIQUE_RE.match(row["evidence"])
        if match is None:
            raise EngineGraphError("invalid workflow plan technique")
        technique = match.group("technique")
        tag = technique if technique in _cgm_schema.TECHNIQUES else "plain"
        info = plan_meta.get(row["id"])
        if info is not None and info["technique"] != tag:
            raise EngineGraphError("workflow decomposition technique does not match its plan")
        plan_technique[row["id"]] = technique
    root_supported = any(
        edge["label"] == "supports"
        and edge["to"] == "goal:root"
        and kind_of[edge["from"]] == "fact"
        for edge in edges
    )
    if root_supported != accepted or (
        accepted and ("goal:root" not in {e["to"] for e in edges if e["from"] == target_id})
    ):
        raise EngineGraphError("workflow root support does not match the receipt")

    annotations: dict[str, dict[str, Any]] = {}
    annotatable = set(goal_ids) | {row["id"] for row in plans}
    for row in annotation_rows:
        if set(row) != _cgm_schema.ANNOTATION_FIELDS:
            raise EngineGraphError("invalid workflow annotation schema")
        node_id = row.get("node_id")
        score = row.get("score")
        evidence = row.get("evidence_considered")
        if (
            node_id not in annotatable
            or node_id in annotations
            or row.get("author") != CGM_AUTHOR
            or row.get("timestamp_utc") != timestamp
            or row.get("id") != _cgm_schema.annotation_id(str(node_id), CGM_AUTHOR, timestamp)
            or row.get("status") not in _cgm_schema.ANNOTATION_STATUSES
            or not (
                score is None
                or (
                    isinstance(score, (int, float))
                    and not isinstance(score, bool)
                    and math.isfinite(score)
                    and 0.0 <= float(score) <= 1.0
                )
            )
            or not isinstance(evidence, list)
            or len(evidence) > 64
            or any(item not in kind_of for item in evidence)
        ):
            raise EngineGraphError("invalid workflow annotation")
        comment = _workflow_text(row.get("comment"), field="annotation comment", limit=4000)
        status = row["status"]
        if (status == "closed" and score != 1.0) or (status == "dead" and score != 0.0):
            raise EngineGraphError("invalid workflow annotation score")
        annotations[str(node_id)] = {
            "id": row["id"],
            "status": status,
            "score": score,
            "comment": _bounded_text(comment, 1000),
            "evidence_considered": list(evidence),
        }
    if set(annotations) != annotatable:
        raise EngineGraphError("workflow annotations do not cover the frontier")
    if (annotations["goal:root"]["status"] == "closed") != accepted:
        raise EngineGraphError("workflow root annotation does not match the receipt")
    # Closure is mechanical (curator.md): a subgoal closes only through a
    # supporting fact, and a plan only through the verified attempt it refined
    # into.  A model-authored "closed" anywhere else fails closed.
    fact_supported = {
        edge["to"]
        for edge in edges
        if edge["label"] == "supports" and kind_of[edge["from"]] == "fact"
    }
    refined_from: dict[str, str] = {
        edge["to"]: edge["from"] for edge in edges if edge["label"] == "refines"
    }
    closing_plans: set[str] = set()
    for row in attempts:
        if row["status"] != "verified":
            continue
        node = row["id"]
        for _ in range(len(attempts) + 1):
            parent = refined_from.get(node)
            if parent is None:
                break
            if kind_of.get(parent) == "plan":
                closing_plans.add(parent)
                break
            node = parent
    for node_id, note in annotations.items():
        if node_id == "goal:root":
            continue
        closed = note["status"] == "closed"
        expected = (
            node_id in closing_plans
            if kind_of[node_id] == "plan"
            else node_id in fact_supported
        )
        if closed != expected:
            raise EngineGraphError("workflow annotation closure is not mechanical")

    # View-only edges connect verifier traces to the attempts they judged and
    # to the facts they admitted.
    for row in verifications:
        edges.append(
            {
                "from": row["links"]["predecessors"][0],
                "to": row["id"],
                "label": "verifier-input",
            }
        )
        if row["fact_id"] is not None:
            edges.append({"from": row["id"], "to": row["fact_id"], "label": "verifier-accepted"})
    if len(edges) > MAX_EDGES:
        raise EngineGraphError("workflow graph exceeds its edge limit")
    _assert_workflow_acyclic(set(kind_of), edges)

    def annotated(node_id: str, base: Mapping[str, Any]) -> dict[str, Any]:
        note = annotations.get(node_id)
        meta = dict(base)
        if note is not None:
            meta.update(
                {
                    "status": note["status"],
                    "score": note["score"],
                    "comment": note["comment"],
                    "annotation": note,
                }
            )
        return meta

    nodes: list[dict[str, Any]] = [
        {
            "id": "goal:root",
            "kind": _CGM_NODE_KIND["goal"],
            "label": "Root problem",
            "statement": _bounded_text(problem.decode("utf-8"), MAX_STATEMENT_LENGTH),
            "citations": [],
            "meta": annotated(
                "goal:root",
                {"workflow_kind": "goal", "timestamp_utc": timestamp},
            ),
        }
    ]
    for goal in goal_ids[1:]:
        info = goal_meta[goal]
        rationale = str(info["rationale"])
        nodes.append(
            {
                "id": goal,
                "kind": _CGM_NODE_KIND["goal"],
                "label": _bounded_text(rationale.split(":", 1)[0] or goal, 120),
                "statement": _bounded_text(rationale, MAX_STATEMENT_LENGTH),
                "citations": [],
                "meta": annotated(
                    goal,
                    {
                        "workflow_kind": "goal",
                        "technique": info["technique"],
                        "and_group": info["and_group"],
                        "plan": info["plan"],
                        "timestamp_utc": timestamp,
                    },
                ),
            }
        )
    verdict_by_row = {
        row["id"]: verdict for row, verdict in zip(verifications, receipt["verdicts"])
    }
    call_seq = 1
    for kind in ("plan", "proof_attempt", "verification", *_cgm_schema.FINDING_KINDS):
        for row in entries.get(kind, []):
            call_seq += 1
            evidence = row["evidence"]
            meta: dict[str, Any] = {
                "workflow_kind": row["kind"],
                "author": row["author"],
                "timestamp_utc": row["timestamp_utc"],
                "status": row["status"],
                "entry_status": row["status"],
                "verifiable": row["verifiable"],
                "fact_id": row["fact_id"],
                "subgoal": row["links"]["subgoal"],
                "predecessors": list(row["links"]["predecessors"]),
                "evidence_truncated": len(evidence.strip()) > MAX_STATEMENT_LENGTH,
                "call_seq": call_seq,
            }
            if row["kind"] == "plan":
                info = plan_meta.get(row["id"], {})
                meta.update(
                    {
                        "technique": plan_technique[row["id"]],
                        "decomposition_technique": info.get("technique"),
                        "and_group": info.get("and_group"),
                    }
                )
            if row["kind"] == "verification":
                verdict = verdict_by_row[row["id"]]
                meta.update({"verdict": verdict["verdict"], "lemma": verdict["label"]})
            nodes.append(
                {
                    "id": row["id"],
                    "kind": _CGM_NODE_KIND[row["kind"]],
                    "label": _bounded_text(row["claim"], 120),
                    "statement": _bounded_text(evidence, MAX_STATEMENT_LENGTH),
                    "citations": [],
                    "meta": annotated(row["id"], meta),
                }
            )
    attested = fact_graph.get("provenance", {}).get("attestation") == "harness-gate-receipt"
    for node_id in sorted(fact_nodes):
        fact_node = fact_nodes[node_id]
        fact_meta = dict(fact_node.get("meta") or {})
        fact_meta.update({"workflow_kind": "fact", "receipt_attested": attested})
        nodes.append(
            {
                "id": node_id,
                "kind": fact_node["kind"],
                "label": fact_node["label"],
                "statement": fact_node["statement"],
                "citations": list(fact_node["citations"]),
                "meta": fact_meta,
            }
        )

    source_parts = [
        (
            "fact_graph.source_sha256",
            str(fact_graph.get("source_sha256") or "").encode("ascii"),
        ),
        ("PROBLEM.md", problem_payload),
        ("proof.md", proof_payload),
        ("engine_receipt.json", receipt_payload),
    ]
    source_parts.extend(
        (f"global_memory/{name}", memory_payloads[name]) for name in sorted(memory_payloads)
    )
    contract = receipt["workflow"]["response_only_contract"]
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "engine-graph",
        "status": "complete" if accepted else "partial",
        "kind": "engine",
        "model": "deterministic-adapter",
        "source": "math_harness/project/global_memory",
        "source_sha256": _source_hash(source_parts),
        "graph_role": "workflow",
        "selection": {
            "target_source": "workflow-goal",
            "target_ids": ["goal:root"],
            "complete_closure": False,
        },
        "provenance": {
            "engine": MATH_HARNESS_ENGINE,
            "producer": "math-harness",
            "adapter": "cgm-exploring-graph@1",
            "semantics": "exploration-workflow",
            "attestation": "unattested",
            "receipt_status": "runner-receipt-valid",
            "pipeline": "cgm",
            "verifier_verdict": receipt["verdict"],
            "memory_receipt_bound": True,
            "prompt_pack_sha256": receipt["prompt_pack_sha256"],
            "response_only_transport": contract["transport"],
            "response_only_isolation_version": contract["isolation_version"],
            "fact_receipt_status": fact_graph.get("provenance", {}).get(
                "receipt_status", "missing"
            ),
            "certificate": False,
            "proof_logic": False,
            "lean_verified": False,
            "acyclic": True,
            "all_predecessors_known": True,
            "truncated": any(
                node.get("meta", {}).get("evidence_truncated") is True for node in nodes
            ),
        },
        "graph": {
            "title": "Math Harness exploration graph (CGM)",
            "nodes": nodes,
            "edges": edges,
        },
    }
    return _validate_artifact(artifact)


def _normalize_view(view: object) -> str:
    normalized = str(view or "facts").strip().lower()
    if normalized not in {"facts", "workflow", "auto"}:
        raise EngineGraphError("unsupported engine graph view")
    return normalized


def _has_project_receipt(workspace: Path) -> bool:
    """Whether a delegating engine's workspace holds a math_harness receipt."""
    try:
        info = (workspace / "math_harness" / "project" / "engine_receipt.json").lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def build(
    workspace: Path,
    run_record: Mapping[str, Any] | None,
    *,
    view: str = "facts",
) -> dict[str, Any]:
    """Build the current graph from owned workspace/run artifacts, offline."""
    selected_view = _normalize_view(view)
    engine = str((run_record or {}).get("engine") or "").strip().lower()
    if engine == "plain":
        # An easy request Plain answered on a run Ansatz CGM works on
        # (easy_route): the run's graph is still Ansatz CGM's, when it has one.
        try:
            from agent_monitor import easy_route

            home = easy_route.home(workspace)
        except Exception:  # noqa: BLE001 - the routing record is optional
            home = ""
        # Only once Ansatz CGM has run here (its receipt): a run Plain alone
        # answered keeps its own trace.
        if home in CGM_GRAPH_ENGINES and _has_project_receipt(Path(workspace)):
            engine = home
    if engine in CGM_GRAPH_ENGINES and (
        engine == MATH_HARNESS_ENGINE or _has_project_receipt(Path(workspace))
    ):
        facts = _math_harness_graph(Path(workspace))
        if selected_view in {"workflow", "auto"}:
            workflow = _math_harness_workflow_graph(Path(workspace), facts)
            if workflow is not None:
                return workflow
            if selected_view == "workflow":
                return _empty_workflow_graph()
        if facts["graph"]["nodes"]:
            return facts
        # Keep execution activity separate from fact/proof dependencies.
        return _execution_graph(run_record)
    return _execution_graph(run_record)


def _validate_artifact(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EngineGraphError("engine graph cache is not an object")
    required = {
        "schema_version", "artifact_kind", "status", "kind", "model", "source",
        "source_sha256", "graph_role", "selection", "provenance", "graph",
    }
    if set(value) != required:
        raise EngineGraphError("engine graph cache has an unsupported schema")
    if value.get("schema_version") != SCHEMA_VERSION or value.get("artifact_kind") != "engine-graph":
        raise EngineGraphError("unsupported engine graph schema version")
    if value.get("kind") != "engine" or value.get("status") not in {"none", "partial", "complete"}:
        raise EngineGraphError("invalid engine graph status")
    if value.get("graph_role") not in {"fact-dependency", "execution", "workflow"}:
        raise EngineGraphError("invalid engine graph role")
    source_hash = value.get("source_sha256")
    if not isinstance(source_hash, str) or (source_hash and not re.fullmatch(r"[0-9a-f]{64}", source_hash)):
        raise EngineGraphError("invalid engine graph source hash")
    graph = value.get("graph")
    if not isinstance(graph, dict) or set(graph) != {"title", "nodes", "edges"}:
        raise EngineGraphError("invalid engine graph payload")
    if not isinstance(graph["title"], str) or not isinstance(graph["nodes"], list) or not isinstance(graph["edges"], list):
        raise EngineGraphError("invalid engine graph payload fields")
    if len(graph["nodes"]) > MAX_NODES or len(graph["edges"]) > MAX_EDGES:
        raise EngineGraphError("engine graph cache exceeds its graph limits")
    ids: set[str] = set()
    for node in graph["nodes"]:
        if not isinstance(node, dict) or set(node) != {"id", "kind", "label", "statement", "citations", "meta"}:
            raise EngineGraphError("invalid engine graph node")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id or len(node_id) > MAX_ID_LENGTH or node_id in ids:
            raise EngineGraphError("invalid engine graph node id")
        if node.get("kind") not in _ALLOWED_NODE_KINDS:
            raise EngineGraphError("invalid engine graph node kind")
        if not isinstance(node.get("label"), str) or not isinstance(node.get("statement"), str):
            raise EngineGraphError("invalid engine graph node text")
        citations = node.get("citations")
        if not isinstance(citations, list) or any(not isinstance(item, str) for item in citations):
            raise EngineGraphError("invalid engine graph citations")
        if not isinstance(node.get("meta"), dict):
            raise EngineGraphError("invalid engine graph node metadata")
        ids.add(node_id)
    for edge in graph["edges"]:
        if not isinstance(edge, dict) or set(edge) != {"from", "to", "label"}:
            raise EngineGraphError("invalid engine graph edge")
        if edge.get("from") not in ids or edge.get("to") not in ids or not isinstance(edge.get("label"), str):
            raise EngineGraphError("engine graph edge has an unknown endpoint")
    selection = value.get("selection")
    provenance = value.get("provenance")
    if not isinstance(selection, dict) or set(selection) != {"target_source", "target_ids", "complete_closure"}:
        raise EngineGraphError("invalid engine graph selection")
    if not isinstance(selection.get("target_ids"), list) or any(target not in ids for target in selection["target_ids"]):
        raise EngineGraphError("invalid engine graph targets")
    if not isinstance(provenance, dict) or provenance.get("certificate") is not False or provenance.get("lean_verified") is not False:
        raise EngineGraphError("invalid engine graph provenance")
    return value


def load_cached(
    workspace: Path,
    *,
    view: str = "facts",
) -> dict[str, Any] | None:
    """Read one strict v1 view cache without following workspace symlinks."""
    selected_view = _normalize_view(view)
    if selected_view == "auto":
        raise EngineGraphError("cached engine graph view must be explicit")
    cache_filename = (
        WORKFLOW_CACHE_FILENAME if selected_view == "workflow" else CACHE_FILENAME
    )
    root = Path(workspace)
    try:
        root_fd = os.open(
            os.fspath(root),
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        return None
    try:
        try:
            payload = _read_regular_file(
                root_fd, cache_filename, limit=MAX_CACHE_BYTES, missing_ok=True
            )
        except EngineGraphError:
            return None
    finally:
        os.close(root_fd)
    if payload is None:
        return None
    try:
        value = json.loads(_decode(payload, cache_filename))
        artifact = _validate_artifact(value)
        if selected_view == "workflow" and artifact["graph_role"] != "workflow":
            return None
        if selected_view == "facts" and artifact["graph_role"] == "workflow":
            return None
        return artifact
    except (json.JSONDecodeError, RecursionError, EngineGraphError):
        return None


def _write_cached(workspace: Path, artifact: dict[str, Any]) -> None:
    artifact = _validate_artifact(artifact)
    cache_filename = (
        WORKFLOW_CACHE_FILENAME
        if artifact["graph_role"] == "workflow"
        else CACHE_FILENAME
    )
    payload = json.dumps(artifact, ensure_ascii=False, indent=2).encode("utf-8")
    if len(payload) > MAX_CACHE_BYTES:
        raise EngineGraphError("engine graph cache exceeds its size limit")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(os.fspath(workspace), flags)
    except OSError as exc:
        raise EngineGraphError("workspace is not a safe directory") from exc
    tmp_name = ""
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise EngineGraphError("workspace is not a safe directory")
        for _attempt in range(16):
            candidate = f".{cache_filename}-{secrets.token_hex(16)}.tmp"
            try:
                tmp_fd = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=root_fd,
                )
                tmp_name = candidate
                break
            except FileExistsError:
                continue
        else:
            raise EngineGraphError("could not allocate an engine graph cache file")
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(tmp_fd, view[written:])
                if count <= 0:
                    raise EngineGraphError("could not write engine graph cache")
                written += count
            os.fchmod(tmp_fd, 0o600)
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)
        os.replace(
            tmp_name,
            cache_filename,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        tmp_name = ""
        os.fsync(root_fd)
    except OSError as exc:
        raise EngineGraphError("could not publish engine graph cache safely") from exc
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name, dir_fd=root_fd)
            except OSError:
                pass
        os.close(root_fd)


def rebuild(
    workspace: Path,
    run_record: Mapping[str, Any] | None,
    *,
    view: str = "facts",
) -> dict[str, Any]:
    """Rebuild and atomically publish one engine graph view; never calls a model."""
    artifact = build(workspace, run_record, view=view)
    _write_cached(workspace, artifact)
    return artifact


def load_or_build(
    workspace: Path,
    run_record: Mapping[str, Any] | None,
    *,
    view: str = "facts",
) -> dict[str, Any]:
    """Return an exact source-bound view cache, otherwise rebuild it offline."""
    current = build(workspace, run_record, view=view)
    effective_view = (
        "workflow" if current["graph_role"] == "workflow" else "facts"
    )
    cached = load_cached(workspace, view=effective_view)
    if cached == current:
        return cached
    _write_cached(workspace, current)
    return current
