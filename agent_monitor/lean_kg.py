"""Observational Lean 4.14 metadata for deterministic Formal DAGs.

This small bridge adopts the useful compiler-environment idea from the
user-supplied Apache-2.0 math-kg-tools archive without running its incompatible
Lean 4.29/4.32 extractors or its graph database importer. It never promotes a
verification result: failures simply leave the existing source parser active.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from agent_monitor import lean_broker


SCHEMA_VERSION = 3
PRODUCER = "agent-monitor-lean-env-v3"
ARTIFACT_FILENAME = "lean_kg_snapshot.json"
MAX_SOURCE_BYTES = 2_000_000
MAX_ARTIFACT_BYTES = 2_000_000
MAX_DECLARATIONS = 256
MAX_DEPENDENCIES = 512
ERROR_CACHE_SECONDS = 300
_KG_SLOTS = threading.BoundedSemaphore(1)
_ATTESTATION_LOCK = threading.Lock()
_ATTESTED_SNAPSHOTS: dict[str, str] = {}
MAX_ATTESTATIONS = 2048
TRUSTED_SANDBOXES = {lean_broker.SANDBOX_MARKER}


def normalized_source_sha256(source: str) -> str:
    return hashlib.sha256(source.strip().encode()).hexdigest()


def _release_identity(value: Any) -> dict[str, str] | None:
    """Validate a broker release tuple; tree hashes are part of cache identity."""
    if not isinstance(value, dict):
        return None
    try:
        release_id, toolchain_tree, mathlib_tree = (
            lean_broker._validate_release_identity(value)
        )
    except lean_broker.LeanBrokerError:
        return None
    return {
        "release_id": release_id,
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }


def _checker_receipt(checked: Any) -> dict[str, str] | None:
    """Return only a source-bound, broker-validated authoritative receipt."""
    if not isinstance(checked, dict):
        return None
    receipt = checked.get("kernel_receipt")
    identity = _release_identity(receipt)
    if not isinstance(receipt, dict) or identity is None:
        return None
    source_sha = str(receipt.get("source_sha256") or "").lower()
    profile = str(receipt.get("checker_profile") or "")
    uses_mathlib = checked.get("uses_mathlib")
    if (
        checked.get("status") not in {"verified", "unfaithful"}
        or receipt.get("ok") is not True
        or receipt.get("status") != "verified"
        or receipt.get("exit_code") != 0
        or isinstance(receipt.get("exit_code"), bool)
        or receipt.get("toolchain") != lean_broker.EXPECTED_TOOLCHAIN
        or receipt.get("sandbox") != lean_broker.SANDBOX_MARKER
        or profile not in {"core", "mathlib"}
        or not isinstance(uses_mathlib, bool)
        or uses_mathlib != (profile == "mathlib")
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha)
        or checked.get("verified_source_sha256") != source_sha
    ):
        return None
    toolchain = checked.get("toolchain")
    if not isinstance(toolchain, dict):
        return None
    mirrored = {
        "release_id": str(toolchain.get("release_id") or ""),
        "toolchain_tree_sha256": str(
            toolchain.get("toolchain_tree_sha256") or ""
        ),
        "mathlib_tree_sha256": str(
            toolchain.get("mathlib_tree_sha256") or ""
        ),
    }
    if (
        mirrored != identity
        or toolchain.get("version") != lean_broker.EXPECTED_TOOLCHAIN
        or toolchain.get("checker_sandbox") != lean_broker.SANDBOX_MARKER
        or toolchain.get("checker_profile") != profile
    ):
        return None
    return {
        **identity,
        "source_sha256": source_sha,
        "checker_profile": profile,
        "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
        "sandbox": lean_broker.SANDBOX_MARKER,
    }


def _regular_source(workspace: Path) -> tuple[str | None, str | None, str]:
    root = Path(workspace).resolve()
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    root_fd = lean_fd = proof_fd = -1
    try:
        root_fd = os.open(
            root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | nofollow
        )
        lean_fd = os.open(
            "lean", os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | nofollow,
            dir_fd=root_fd,
        )
        proof_fd = os.open(
            "Proof.lean", os.O_RDONLY | os.O_CLOEXEC | nofollow,
            dir_fd=lean_fd,
        )
        state = os.fstat(proof_fd)
        if not stat.S_ISREG(state.st_mode):
            return None, None, "lean/Proof.lean must be a regular file"
        if state.st_size > MAX_SOURCE_BYTES:
            return None, None, "lean/Proof.lean exceeds the KG source limit"
        chunks: list[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(proof_fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except FileNotFoundError:
        return None, None, "lean/Proof.lean is missing"
    except OSError as exc:
        return None, None, f"lean/Proof.lean is not a regular file: {exc}"
    finally:
        for fd in (proof_fd, lean_fd, root_fd):
            if fd >= 0:
                os.close(fd)
    if len(raw) > MAX_SOURCE_BYTES:
        return None, None, "lean/Proof.lean exceeds the KG source limit"
    try:
        return raw.decode(), hashlib.sha256(raw).hexdigest(), ""
    except UnicodeDecodeError:
        return None, None, "lean/Proof.lean is not valid UTF-8"


def _instrumentation(marker: str) -> str:
    ns = f"AgentMonitorKg_{marker}"
    prefix = f"AGENT_MONITOR_LEAN_KG::{marker}::"
    return f"""

namespace {ns}
open Lean
private partial def refs (e : Lean.Expr) (s : Lean.NameSet)
    (fuel : Nat := 800) : Lean.NameSet × Bool :=
  if fuel == 0 then (s, true) else match e with
  | .const n _ => (s.insert n, false)
  | .app f a =>
      let (s, tf) := refs f s (fuel-1)
      let (s, ta) := refs a s (fuel-1)
      (s, tf || ta)
  | .lam _ d b _ | .forallE _ d b _ =>
      let (s, td) := refs d s (fuel-1)
      let (s, tb) := refs b s (fuel-1)
      (s, td || tb)
  | .letE _ t v b _ =>
      let (s, tt) := refs t s (fuel-1)
      let (s, tv) := refs v s (fuel-1)
      let (s, tb) := refs b s (fuel-1)
      (s, tt || tv || tb)
  | .mdata _ b | .proj _ _ b => refs b s (fuel-1)
  | _ => (s, false)
private def value? : Lean.ConstantInfo → Option Lean.Expr
  | .defnInfo d => some d.value
  | .thmInfo d => some d.value
  | .opaqueInfo d => some d.value
  | _ => none
private def names (s : Lean.NameSet) : Array String :=
  s.toList.map (·.toString) |>.toArray
private def typeRefs (ci : Lean.ConstantInfo) :=
  let (refs, truncated) := refs ci.type {{}}
  (names refs, truncated)
private def valueRefs (ci : Lean.ConstantInfo) :=
  match value? ci with
  | some v =>
      let (refs, truncated) := refs v {{}}
      (names refs, truncated)
  | none => (#[], false)
private def kind : Lean.ConstantInfo → String
  | .axiomInfo _ => "axiom" | .defnInfo _ => "def"
  | .thmInfo _ => "theorem" | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quot" | .inductInfo _ => "inductive"
  | .ctorInfo _ => "ctor" | .recInfo _ => "recursor"
private partial def hasPart (n : Lean.Name) (wanted : String) : Bool :=
  match n with
  | .anonymous => false
  | .str parent value => value == wanted || hasPart parent wanted
  | .num parent _ => hasPart parent wanted
run_cmd do
  let env ← getEnv
  let rows := env.constants.foldStage2 (fun acc name ci =>
    if (env.getModuleIdxFor? name).isNone && !hasPart name "{ns}"
    then acc.push (name, ci) else acc) #[]
  let mut out : Array Lean.Json := #[]
  for (name, ci) in rows do
    let (statementDeps, statementTruncated) := typeRefs ci
    let (proofDeps, proofTruncated) := valueRefs ci
    out := out.push <| Lean.Json.mkObj [
      ("name", toJson name.toString), ("kind", toJson (kind ci)),
      ("statementDeps", toJson statementDeps),
      ("proofDeps", toJson proofDeps),
      ("statementTruncated", toJson statementTruncated),
      ("proofTruncated", toJson proofTruncated)]
  IO.println ("{prefix}" ++ Lean.Json.compress (Lean.Json.arr out))
end {ns}
"""


def _unsafe_source_reason(source: str) -> str:
    """Reject avoidable metaprogram execution before the real sandbox.

    This is defense in depth only. Lean tactics are metaprograms themselves,
    so the fixed root-owned broker remains the actual security boundary.
    """
    from agent_monitor.lean_verify import _lean_executable_text

    executable = _lean_executable_text(source)
    unsafe = re.search(
        r"(?m)(?:^|\s)(run_cmd|builtin_initialize|initialize|run_tac|"
        r"elab_rules|elab|macro_rules|macro|syntax|unsafe|foreign|"
        r"native_decide|#eval|#reduce)\b|"
        r"set_option\s+debug\.skipKernelTC\s+true\b|"
        r"@\[\s*(?:extern|implemented_by)\b",
        executable,
    )
    if unsafe:
        token = unsafe.group(1) or unsafe.group(0)
        return f"executable Lean command is not allowed in KG mode: {token}"
    for match in re.finditer(r"(?m)^\s*import\s+([^\n]+)$", executable):
        modules = [part for part in match.group(1).split() if part]
        for module in modules:
            if module == "Mathlib":
                return "broad import Mathlib is disabled"
            if not (
                module in {"Init", "Lean", "Std"}
                or module.startswith(("Init.", "Lean.", "Std.", "Mathlib."))
            ):
                return (
                    "local or untrusted import is disabled in KG mode: "
                    f"{module[:160]}"
                )
    return ""


def _run(
    workspace: Path, source: str, uses_mathlib: bool, timeout: int
) -> dict[str, Any]:
    del workspace  # The fixed broker accepts no caller-controlled path.
    unsafe_reason = _unsafe_source_reason(source)
    if unsafe_reason:
        raise ValueError(unsafe_reason)
    marker = secrets.token_hex(12)
    prefix = f"AGENT_MONITOR_LEAN_KG::{marker}::"
    program = "import Lean\n" + source.rstrip() + _instrumentation(marker)
    requested_timeout = min(
        lean_broker.MAX_CLIENT_TIMEOUT, max(1.0, float(timeout))
    )
    try:
        result = lean_broker.check_source(
            program,
            uses_mathlib=uses_mathlib,
            timeout=requested_timeout,
        )
    except lean_broker.LeanBrokerError as exc:
        raise ValueError(
            "fixed isolated Lean KG broker is unavailable; no local fallback: "
            f"{exc}"
        ) from exc
    expected_profile = "mathlib" if uses_mathlib else "core"
    identity = _release_identity(result)
    instrumented_sha = hashlib.sha256(program.encode()).hexdigest()
    if (
        identity is None
        or result.get("profile") != expected_profile
        or result.get("toolchain") != lean_broker.EXPECTED_TOOLCHAIN
        or result.get("sandbox") != lean_broker.SANDBOX_MARKER
        or result.get("source_sha256") != instrumented_sha
    ):
        raise ValueError("Lean KG broker provenance attestation was invalid")
    status = str(result.get("status") or "")
    exit_code = result.get("exit_code")
    if (
        result.get("stdout_truncated") is not False
        or result.get("stderr_truncated") is not False
    ):
        raise ValueError("Lean KG broker output was truncated")
    if status != "completed" or isinstance(exit_code, bool) or exit_code != 0:
        detail = str(
            result.get("error")
            or result.get("stderr")
            or result.get("stdout")
            or "no output"
        )[-1200:]
        raise ValueError(
            f"instrumented Lean broker {status or 'failed'} "
            f"(exit {exit_code}): {detail}"
        )
    output = str(result.get("stdout") or "")
    payloads = [
        line.split(prefix, 1)[1]
        for line in output.splitlines()
        if prefix in line
    ]
    if len(payloads) != 1:
        raise ValueError("instrumented Lean returned no unique KG payload")
    try:
        rows = json.loads(payloads[0])
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("instrumented Lean returned invalid KG JSON") from exc
    if not isinstance(rows, list):
        raise ValueError("instrumented Lean returned non-array KG data")
    return {
        "rows": rows,
        "duration_s": round(float(result.get("duration") or 0.0), 3),
        "toolchain": str(result.get("toolchain") or ""),
        "sandbox": str(result.get("sandbox") or ""),
        "checker_profile": expected_profile,
        "instrumented_source_sha256": instrumented_sha,
        **identity,
    }


def _namespace_at_lines(source: str) -> dict[int, str]:
    from agent_monitor.lean_verify import _lean_executable_text

    namespace: list[str] = []
    blocks: list[int] = []
    result: dict[int, str] = {}
    executable = _lean_executable_text(source)
    for no, raw in enumerate(executable.splitlines(), 1):
        result[no] = ".".join(namespace)
        line = raw.strip()
        match = re.match(r"^namespace\s+([^\s]+)", line)
        if match:
            blocks.append(len(namespace))
            namespace.extend(match.group(1).split("."))
        elif re.match(r"^section(?:\s+\S+)?$", line):
            blocks.append(len(namespace))
        elif re.match(r"^mutual\b", line):
            blocks.append(len(namespace))
        elif re.match(r"^end(?:\s+\S+)?$", line) and blocks:
            del namespace[blocks.pop():]
    return result


def _names(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], False
    bounded = value[: MAX_DEPENDENCIES * 4]
    names = sorted({
        item[:512] for item in bounded
        if isinstance(item, str) and item.strip()
    })
    truncated = len(value) > len(bounded) or len(names) > MAX_DEPENDENCIES
    return names[:MAX_DEPENDENCIES], truncated


def _normalize(source: str, rows: Any) -> list[dict[str, Any]]:
    from agent_monitor.lean_verify import _decls_with_docs

    if not isinstance(rows, list) or len(rows) > 4096:
        raise ValueError("invalid or oversized Lean KG payload")
    compiler: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")[:512].strip()
        if name and name not in compiler:
            if not isinstance(row.get("statementTruncated"), bool) or not isinstance(
                row.get("proofTruncated"), bool
            ):
                raise ValueError("Lean KG payload omitted dependency truncation metadata")
            statement_dependencies, statement_truncated = _names(
                row.get("statementDeps")
            )
            proof_dependencies, proof_truncated = _names(row.get("proofDeps"))
            compiler[name] = {
                "kind": str(row.get("kind") or "unknown")[:40],
                "statement_dependencies": statement_dependencies,
                "proof_dependencies": proof_dependencies,
                "statement_dependencies_truncated": bool(
                    statement_truncated or row["statementTruncated"]
                ),
                "proof_dependencies_truncated": bool(
                    proof_truncated or row["proofTruncated"]
                ),
            }
    declarations = _decls_with_docs(source)
    if len(declarations) > MAX_DECLARATIONS:
        raise ValueError("too many declarations for Lean KG")
    prefixes = _namespace_at_lines(source)
    normalized: list[dict[str, Any]] = []
    for decl in declarations:
        raw = str(decl.get("name") or "")
        if not raw or " @" in raw:
            continue
        prefix = prefixes.get(int(decl.get("line") or 0), "")
        expected = (
            raw.removeprefix("_root_.") if raw.startswith("_root_.")
            else ".".join(part for part in (prefix, raw) if part)
        )
        candidates = [n for n in (expected, raw) if n in compiler]
        matched = len(dict.fromkeys(candidates)) == 1
        qualified = list(dict.fromkeys(candidates))[0] if matched else expected
        row = compiler.get(qualified, {})
        normalized.append({
            "source_name": raw,
            "qualified_name": qualified,
            "line": int(decl.get("line") or 0),
            "source_kind": str(decl.get("kind") or "")[:40],
            "compiler_kind": str(row.get("kind") or "")[:40],
            "matched": matched,
            "statement_dependencies": list(row.get("statement_dependencies") or []),
            "proof_dependencies": list(row.get("proof_dependencies") or []),
            "statement_dependencies_truncated": bool(
                row.get("statement_dependencies_truncated")
            ),
            "proof_dependencies_truncated": bool(
                row.get("proof_dependencies_truncated")
            ),
            "local_statement_mentions": [],
            "local_proof_uses": [],
        })
    local = {d["qualified_name"] for d in normalized if d["matched"]}
    for decl in normalized:
        decl["local_statement_mentions"] = [
            n for n in decl["statement_dependencies"] if n in local
        ]
        decl["local_proof_uses"] = [
            n for n in decl["proof_dependencies"] if n in local
        ]
    return normalized


def _path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ARTIFACT_FILENAME


def _verified_source_reason(workspace: Path, current: str) -> str:
    """Bind extraction to the exact source last accepted by the kernel path."""
    from agent_monitor import lean_verify

    checked = lean_verify.load_cached(workspace) or {}
    if str(checked.get("status") or "") not in {"verified", "unfaithful"}:
        return "Lean KG requires a kernel-verified current Proof.lean"
    recorded = str(checked.get("lean") or "")
    if not recorded:
        return "Lean verification cache has no source to attest"
    expected_sha = str(checked.get("verified_source_sha256") or "").lower()
    current_sha = hashlib.sha256(current.encode()).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        return "Lean verification cache has no authoritative source hash"
    if expected_sha != current_sha:
        return "current Proof.lean hash differs from the kernel-verified source"
    if current not in {recorded, recorded.rstrip() + "\n"}:
        return "current Proof.lean differs from the kernel-verified source bytes"
    if lean_verify._sorry_lines(current):
        return "Lean KG is unavailable while sorry/admit remains"
    blocked_flags = {
        str(flag.get("id") or "")
        for flag in lean_verify._fidelity_flags(current)
        if isinstance(flag, dict)
    } & {
        "skip_kernel_tc", "unsafe", "implemented_by", "native_decide",
    }
    if blocked_flags:
        return (
            "Lean KG requires a kernel-trusted source; blocked construct(s): "
            + ", ".join(sorted(blocked_flags))
        )
    return ""


def load_snapshot(
    workspace: Path, *, source: str | None = None, toolchain: str | None = None
) -> dict[str, Any] | None:
    path = _path(workspace)
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(fd, "rb") as handle:
            state = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(state.st_mode)
                or state.st_size > MAX_ARTIFACT_BYTES
            ):
                return None
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            return None
        value = json.loads(raw.decode())
    except (
        FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError,
        RecursionError, ValueError,
    ):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != SCHEMA_VERSION or value.get("producer") != PRODUCER:
        return None
    if value.get("status") not in {"ok", "error"}:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    with _ATTESTATION_LOCK:
        if _ATTESTED_SNAPSHOTS.get(str(path)) != digest:
            return None
    if (
        value.get("status") == "ok"
        and value.get("sandbox") not in TRUSTED_SANDBOXES
    ):
        return None
    current, exact_sha, _error = _regular_source(Path(workspace))
    if current is None or exact_sha is None:
        return None
    if value.get("source_sha256") != exact_sha:
        return None
    if value.get("normalized_source_sha256") != normalized_source_sha256(current):
        return None
    if source is not None and source != current:
        return None
    if not isinstance(value.get("uses_mathlib"), bool):
        return None
    from agent_monitor import lean_verify

    receipt = _checker_receipt(lean_verify.load_cached(workspace))
    identity = _release_identity(value)
    expected_profile = "mathlib" if value["uses_mathlib"] else "core"
    if (
        receipt is None
        or identity is None
        or receipt["source_sha256"] != exact_sha
        or receipt["checker_profile"] != expected_profile
        or value.get("checker_profile") != expected_profile
        or value.get("toolchain") != lean_broker.EXPECTED_TOOLCHAIN
        or (toolchain and value.get("toolchain") != toolchain)
        or value.get("sandbox") != lean_broker.SANDBOX_MARKER
        or any(value.get(key) != receipt[key] for key in identity)
    ):
        return None
    instrumented_sha = value.get("instrumented_source_sha256")
    if not isinstance(instrumented_sha, str) or not re.fullmatch(
        r"[0-9a-f]{64}", instrumented_sha
    ):
        return None
    declarations = value.get("declarations")
    if not isinstance(declarations, list) or len(declarations) > MAX_DECLARATIONS:
        return None
    numeric_limits = {
        "declaration_count": MAX_DECLARATIONS,
        "matched_declaration_count": MAX_DECLARATIONS,
        "proof_edge_count": MAX_DECLARATIONS * MAX_DEPENDENCIES,
        "statement_edge_count": MAX_DECLARATIONS * MAX_DEPENDENCIES,
        "dependency_truncated_count": MAX_DECLARATIONS * 2,
    }
    for key, limit in numeric_limits.items():
        raw = value.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= limit:
            return None
    seen_qualified: set[str] = set()
    matched = 0
    proof_edges = 0
    statement_edges = 0
    for declaration in declarations:
        if not isinstance(declaration, dict):
            return None
        if not isinstance(declaration.get("matched"), bool):
            return None
        line = declaration.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            return None
        for key in (
            "source_name", "qualified_name", "source_kind", "compiler_kind"
        ):
            raw = declaration.get(key)
            if not isinstance(raw, str) or len(raw) > 512:
                return None
        for key in (
            "statement_dependencies", "proof_dependencies",
            "local_statement_mentions", "local_proof_uses",
        ):
            raw = declaration.get(key)
            if (
                not isinstance(raw, list)
                or len(raw) > MAX_DEPENDENCIES
                or any(not isinstance(item, str) or len(item) > 512 for item in raw)
            ):
                return None
        for key in (
            "statement_dependencies_truncated", "proof_dependencies_truncated"
        ):
            if not isinstance(declaration.get(key), bool):
                return None
        if declaration["matched"]:
            qualified = declaration["qualified_name"]
            if not qualified or qualified in seen_qualified:
                return None
            seen_qualified.add(qualified)
            matched += 1
        statement_dependencies = set(declaration["statement_dependencies"])
        proof_dependencies = set(declaration["proof_dependencies"])
        statement_local = declaration["local_statement_mentions"]
        proof_local = declaration["local_proof_uses"]
        if (
            not set(statement_local).issubset(statement_dependencies)
            or not set(proof_local).issubset(proof_dependencies)
        ):
            return None
        statement_edges += len(statement_local)
        proof_edges += len(proof_local)
    if value.get("status") == "ok" and (
        value.get("declaration_count") != len(declarations)
        or value.get("matched_declaration_count") != matched
        or value.get("proof_edge_count") != proof_edges
        or value.get("statement_edge_count") != statement_edges
        or value.get("dependency_truncated_count") != sum(
            int(declaration["statement_dependencies_truncated"])
            + int(declaration["proof_dependencies_truncated"])
            for declaration in declarations
        )
        or matched == 0
    ):
        return None
    return value


def _write(workspace: Path, value: dict[str, Any]) -> None:
    root = Path(workspace).resolve()
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    if len(data.encode()) > MAX_ARTIFACT_BYTES:
        raise ValueError("Lean KG artifact exceeds its limit")
    fd, name = tempfile.mkstemp(prefix=".lean-kg-", suffix=".tmp", dir=str(root))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        target = _path(root)
        os.replace(tmp, target)
        digest = hashlib.sha256(data.encode()).hexdigest()
        with _ATTESTATION_LOCK:
            key = str(target)
            _ATTESTED_SNAPSHOTS.pop(key, None)
            while len(_ATTESTED_SNAPSHOTS) >= MAX_ATTESTATIONS:
                _ATTESTED_SNAPSHOTS.pop(next(iter(_ATTESTED_SNAPSHOTS)))
            _ATTESTED_SNAPSHOTS[key] = digest
    finally:
        tmp.unlink(missing_ok=True)


def extract_snapshot(
    workspace: Path, *, source: str | None = None,
    uses_mathlib: bool | None = None, timeout: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Extract metadata for source already accepted by the regular checker."""
    from agent_monitor import lean_verify

    workspace = Path(workspace).resolve()
    current, exact_sha, error = _regular_source(workspace)
    if current is None or exact_sha is None:
        return {
            "schema_version": SCHEMA_VERSION, "producer": PRODUCER,
            "status": "error", "error": error, "declarations": [],
        }
    if source is not None and source != current:
        return {
            "schema_version": SCHEMA_VERSION, "producer": PRODUCER,
            "status": "error", "source_sha256": exact_sha,
            "normalized_source_sha256": normalized_source_sha256(current),
            "error": "requested source differs from lean/Proof.lean",
            "declarations": [],
        }
    source = current
    uses_mathlib = (
        bool(uses_mathlib) if uses_mathlib is not None else
        bool(re.search(r"(?m)^\s*import\s+Mathlib(?:\.|\s)", source))
    )
    verified_error = _verified_source_reason(workspace, source)
    if verified_error:
        return {
            "schema_version": SCHEMA_VERSION, "producer": PRODUCER,
            "status": "error", "source_sha256": exact_sha,
            "normalized_source_sha256": normalized_source_sha256(source),
            "error": verified_error, "declarations": [],
        }
    checked = lean_verify.load_cached(workspace) or {}
    receipt = _checker_receipt(checked)
    expected_profile = "mathlib" if uses_mathlib else "core"
    if (
        receipt is None
        or receipt["source_sha256"] != exact_sha
        or receipt["checker_profile"] != expected_profile
        or bool(checked.get("uses_mathlib")) != uses_mathlib
    ):
        return {
            "schema_version": SCHEMA_VERSION, "producer": PRODUCER,
            "status": "error", "source_sha256": exact_sha,
            "normalized_source_sha256": normalized_source_sha256(source),
            "error": (
                "Lean verification cache has no authoritative broker release "
                "receipt for this source/profile"
            ),
            "declarations": [],
        }
    toolchain = receipt["toolchain"]
    cached = load_snapshot(workspace, source=source, toolchain=toolchain)
    if cached and not force:
        if cached.get("status") == "ok":
            return cached
        try:
            age = time.time() - _path(workspace).stat().st_mtime
        except OSError:
            age = ERROR_CACHE_SECONDS + 1
        if age < ERROR_CACHE_SECONDS:
            return cached
    try:
        configured_timeout = int(
            os.environ.get("AGENT_MONITOR_LEAN_KG_TIMEOUT", "120")
        )
    except ValueError:
        configured_timeout = 120
    started = time.monotonic()
    raw: dict[str, Any] | None = None
    try:
        with _KG_SLOTS:
            with lean_verify._formal_io_lock(workspace):
                _before, before_sha, before_error = _regular_source(workspace)
                if before_sha != exact_sha:
                    raise ValueError(
                        before_error or "Proof.lean changed before extraction"
                    )
                current_verified_error = _verified_source_reason(workspace, source)
                if current_verified_error:
                    raise ValueError(current_verified_error)
                raw = _run(
                    workspace, source, uses_mathlib,
                    timeout or max(1, configured_timeout),
                )
                _after, after_sha, after_error = _regular_source(workspace)
                if after_sha != exact_sha:
                    raise ValueError(
                        after_error or "Proof.lean changed during extraction"
                    )
                actual_toolchain = str(raw.get("toolchain") or "")
                raw_identity = _release_identity(raw)
                if (
                    raw_identity is None
                    or actual_toolchain != receipt["toolchain"]
                    or raw.get("sandbox") != receipt["sandbox"]
                    or raw.get("checker_profile") != receipt["checker_profile"]
                    or any(raw_identity[key] != receipt[key] for key in raw_identity)
                ):
                    raise ValueError(
                        "Lean KG broker release differs from the verified checker release"
                    )
                declarations = _normalize(source, raw["rows"])
                matched_count = sum(d["matched"] for d in declarations)
                if not declarations or not matched_count:
                    raise ValueError(
                        "Lean KG could not bind any compiler declaration to Proof.lean"
                    )
                value = {
                    "schema_version": SCHEMA_VERSION, "producer": PRODUCER,
                    "status": "ok",
                    "generated_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    ),
                    "source": "lean/Proof.lean", "source_sha256": exact_sha,
                    "normalized_source_sha256": normalized_source_sha256(source),
                    "toolchain": actual_toolchain,
                    "sandbox": str(raw.get("sandbox") or "")[:40],
                    "checker_profile": expected_profile,
                    "uses_mathlib": uses_mathlib,
                    "instrumented_source_sha256": str(
                        raw.get("instrumented_source_sha256") or ""
                    ),
                    **raw_identity,
                    "duration_s": float(raw.get("duration_s") or 0),
                    "declaration_count": len(declarations),
                    "matched_declaration_count": matched_count,
                    "proof_edge_count": sum(
                        len(d["local_proof_uses"]) for d in declarations
                    ),
                    "statement_edge_count": sum(
                        len(d["local_statement_mentions"]) for d in declarations
                    ),
                    "dependency_truncated_count": sum(
                        int(d["statement_dependencies_truncated"])
                        + int(d["proof_dependencies_truncated"])
                        for d in declarations
                    ),
                    "declarations": declarations,
                }
                try:
                    _write(workspace, value)
                except (OSError, ValueError):
                    pass
    except (OSError, ValueError) as exc:
        value = {
            "schema_version": SCHEMA_VERSION, "producer": PRODUCER,
            "status": "error",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "lean/Proof.lean", "source_sha256": exact_sha,
            "normalized_source_sha256": normalized_source_sha256(source),
            "toolchain": toolchain, "uses_mathlib": uses_mathlib,
            "duration_s": round(time.monotonic() - started, 3),
            "error": str(exc)[:1000], "declarations": [],
        }
        raw_identity = _release_identity(raw)
        if raw_identity is not None and raw is not None:
            value.update({
                "sandbox": str(raw.get("sandbox") or "")[:40],
                "checker_profile": str(
                    raw.get("checker_profile") or ""
                )[:20],
                "instrumented_source_sha256": str(
                    raw.get("instrumented_source_sha256") or ""
                ),
                **raw_identity,
            })
        with lean_verify._formal_io_lock(workspace):
            _current, current_sha, _current_error = _regular_source(workspace)
            if current_sha == exact_sha:
                try:
                    _write(workspace, value)
                except (OSError, ValueError):
                    pass
    return value


def snapshot_summary(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "unavailable"}

    def integer(name: str) -> int:
        raw = value.get(name, 0)
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0

    try:
        duration = float(value.get("duration_s") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration < 0:
        duration = 0.0
    return {
        "status": str(value.get("status") or "unavailable")[:24],
        "producer": str(value.get("producer") or "")[:80],
        "sandbox": str(value.get("sandbox") or "")[:40],
        "toolchain": str(value.get("toolchain") or "")[:200],
        "checker_profile": str(value.get("checker_profile") or "")[:20],
        "release_id": str(value.get("release_id") or "")[:64],
        "toolchain_tree_sha256": str(
            value.get("toolchain_tree_sha256") or ""
        )[:64],
        "mathlib_tree_sha256": str(
            value.get("mathlib_tree_sha256") or ""
        )[:64],
        "declaration_count": integer("declaration_count"),
        "matched_declaration_count": integer("matched_declaration_count"),
        "proof_edge_count": integer("proof_edge_count"),
        "statement_edge_count": integer("statement_edge_count"),
        "dependency_truncated_count": integer("dependency_truncated_count"),
        "duration_s": duration,
        "error": str(value.get("error") or "")[:300],
    }
