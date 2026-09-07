"""Deterministic, provenance-honest graphs emitted by proof engines.

This module deliberately does not invoke a model.  ``math_harness`` exposes its
content-addressed fact DAG; every other engine exposes only the agents/edges in
the run record and is labelled as an execution trace, not proof logic.
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
MAX_WORKFLOW_FILE_BYTES = 256_000
MAX_WORKFLOW_ROWS = 256
MATH_HARNESS_AUTHOR = "math_harness_model_critic"

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
    expected_version = {
        "codex_exec": 4,
        "claude_code": 1,
        "api_chat": 1,
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


def _math_harness_graph(workspace: Path) -> dict[str, Any]:
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
                return _empty_math_harness_graph()
            opened.append(child)
            parent_fd = child
        project_fd = parent_fd
        fact_graph_fd = _open_directory(project_fd, "fact_graph", missing_ok=True)
        if fact_graph_fd is None:
            return _empty_math_harness_graph()
        opened.append(fact_graph_fd)
        facts_fd = _open_directory(fact_graph_fd, "facts", missing_ok=True)
        if facts_fd is None:
            return _empty_math_harness_graph()
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

        target_set = set(targets)
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        for fact_id in order:
            fact = facts[fact_id]
            proof_excerpt = _bounded_text(fact.proof, MAX_PROOF_EXCERPT)
            nodes.append(
                {
                    "id": fact_id,
                    "kind": "conclusion" if fact_id in target_set else "lemma",
                    "label": _bounded_text(_SPACE_RE.sub(" ", fact.statement), 120),
                    "statement": _bounded_text(fact.statement, MAX_STATEMENT_LENGTH),
                    "citations": list(fact.citations),
                    "meta": {
                        "author": _bounded_text(fact.author, 240),
                        "problem_id": _bounded_text(fact.problem_id, 240),
                        "depth": depths[fact_id],
                        "is_target": fact_id in target_set,
                        "proof_excerpt": proof_excerpt,
                        "proof_truncated": len(fact.proof.strip()) > MAX_PROOF_EXCERPT,
                        "intuition": _bounded_text(fact.intuition, MAX_INTUITION_LENGTH),
                    },
                }
            )
            for pred in sorted(fact.predecessors):
                edges.append(
                    {"from": pred, "to": fact_id, "label": "fact dependency"}
                )
        if len(edges) > MAX_EDGES:
            raise EngineGraphError("fact graph exceeds its edge limit")

        receipt = _receipt_provenance(
            receipt_payload,
            targets=targets,
            target_payload=target_payload,
            facts=facts,
            fact_payloads=fact_payloads,
            problem_artifact=problem_artifact,
            proof_payload=proof_payload,
        )
        problem_label = next(iter(problem_ids), "")
        artifact = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "engine-graph",
            "status": "complete" if targets else ("partial" if nodes else "none"),
            "kind": "engine",
            "model": "deterministic-adapter",
            "source": "math_harness/project/fact_graph",
            "source_sha256": _source_hash(source_parts),
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
                **receipt,
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
    finally:
        for fd in reversed(opened):
            os.close(fd)


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
        try:
            with os.scandir(memory_fd) as entries:
                for entry_count, entry in enumerate(entries, start=1):
                    if entry_count > len(_WORKFLOW_FILES):
                        raise EngineGraphError(
                            "workflow memory has unexpected directory entries"
                        )
                    names.add(entry.name)
        except EngineGraphError:
            raise
        except OSError as exc:
            raise EngineGraphError("could not enumerate workflow memory safely") from exc
        expected = set(_WORKFLOW_FILES)
        if not names:
            return None
        if names != expected:
            raise EngineGraphError("workflow memory file set is incomplete or unexpected")
        payloads: dict[str, bytes] = {}
        for name in _WORKFLOW_FILES:
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
    lines = text_payload.splitlines()
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


def _normalize_view(view: object) -> str:
    normalized = str(view or "facts").strip().lower()
    if normalized not in {"facts", "workflow", "auto"}:
        raise EngineGraphError("unsupported engine graph view")
    return normalized


def build(
    workspace: Path,
    run_record: Mapping[str, Any] | None,
    *,
    view: str = "facts",
) -> dict[str, Any]:
    """Build the current graph from owned workspace/run artifacts, offline."""
    selected_view = _normalize_view(view)
    engine = str((run_record or {}).get("engine") or "").strip().lower()
    if engine == MATH_HARNESS_ENGINE:
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
