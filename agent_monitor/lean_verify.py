"""Lean verification — translate an informal proof into Lean 4 and check it.

Pipeline
--------
1. Read ``problem.txt`` + ``proof.md`` (or ``proof.tex`` / final agent output).
2. Ask an LLM for a self-contained Lean 4 file that formalises the claim.
3. Write it under ``<workspace>/lean/Proof.lean`` and try to compile with
   ``lake`` / ``lean``.
4. On compiler errors, feed the log back to the model for up to
   ``max_repairs`` repair rounds.
5. Cache the whole result as ``lean_verify.json`` so reopening the tab is free.

If no Lean toolchain is installed the translation still runs — status becomes
``toolchain_missing`` and the generated source is shown for the user to check
elsewhere. Prefer proofs that compile against Lean core (``Init``) so a bare
``lean`` binary is enough; fall back to Mathlib only when the claim truly needs
it.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from agent_monitor.proof_graph import (  # reuse LLM plumbing and safe reads
    CLAUDE_DERIVED_TIMEOUT_ENV,
    MAX_PROBLEM_BYTES as MAX_WORKSPACE_PROBLEM_BYTES,
    MAX_SOURCE_BYTES as MAX_WORKSPACE_SOURCE_BYTES,
    _call_llm,
    _claude_derived_timeout,
    _extract_json,
    _proof_source,
    _read_workspace_text,
)

RESULT_FILENAME = "lean_verify.json"
MAX_RESULT_CACHE_BYTES = 8_000_000
MAX_HARNESS_LOG_BYTES = 1_000_000
MAX_HARNESS_TASK_BYTES = 50_000
LEAN_DIRNAME = "lean"
PROOF_FILENAME = "Proof.lean"

_PRODUCT_AUDIT_PAREN_NOTE_RE = re.compile(
    r"\s*\([^()]*\b(?:degraded\s+audit\s*:|skill\s+file\s+unavailable\b)[^()]*\)",
    re.IGNORECASE,
)
_PRODUCT_AUDIT_SENTENCE_NOTE_RE = re.compile(
    r"(?:^|(?<=[.!?])\s+)"
    r"[^.!?]*\b(?:degraded\s+audit\s*:|skill\s+file\s+unavailable\b)"
    r"[^.!?]*(?:[.!?]|$)",
    re.IGNORECASE,
)


def _sanitize_formalization_notes(value: Any) -> str:
    """Remove product-audit availability claims from Lean-only notes.

    The informal candidate audit and the Lean formalization are separate
    pipelines.  A formalizer cannot infer that the installed audit skill is
    unavailable merely because the Lean sidecar does not expose that skill.
    Preserve mathematical/fidelity audit prose; remove only these operational
    availability markers.
    """
    text = str(value or "").strip()
    text = _PRODUCT_AUDIT_PAREN_NOTE_RE.sub("", text)
    text = _PRODUCT_AUDIT_SENTENCE_NOTE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()

_INTERVENTION_LOCKS: dict[str, threading.Lock] = {}
_INTERVENTION_LOCKS_GUARD = threading.Lock()

# A Formal Lean operation owns a resolved workspace from the first model/file
# action through the final persisted verdict.  The token map (rather than a
# Lock held by one thread) lets a background harness acquire in the request
# thread and release in its worker thread.  Resolving the path makes aliases
# contend for the same reservation while unrelated proofs remain concurrent.
_FORMAL_OPERATIONS: dict[str, dict[str, Any]] = {}
_FORMAL_OPERATIONS_GUARD = threading.Lock()
_FORMAL_IO_LOCKS: dict[str, threading.Lock] = {}
_FORMAL_IO_LOCKS_GUARD = threading.Lock()


def _workspace_key(workspace: Path) -> str:
    return str(Path(workspace).resolve())


def _reserve_formal_operation(workspace: Path, operation: str) -> object:
    key = _workspace_key(workspace)
    token = object()
    with _FORMAL_OPERATIONS_GUARD:
        active = _FORMAL_OPERATIONS.get(key)
        if active:
            label = str(active.get("operation") or "Lean operation")
            raise ValueError(
                f"Another Lean operation ({label}) is already running for this proof"
            )
        _FORMAL_OPERATIONS[key] = {"token": token, "operation": operation}
    return token


def _release_formal_operation(workspace: Path, token: object) -> None:
    key = _workspace_key(workspace)
    with _FORMAL_OPERATIONS_GUARD:
        active = _FORMAL_OPERATIONS.get(key)
        if active and active.get("token") is token:
            _FORMAL_OPERATIONS.pop(key, None)


def _serialized_formal_operation(operation: str):
    """Reserve one resolved workspace for a synchronous public operation."""
    def decorate(function):
        def guarded(*args, **kwargs):
            raw_workspace = kwargs.get("workspace")
            if raw_workspace is None:
                raise TypeError("workspace is required")
            workspace = Path(raw_workspace).resolve()
            kwargs["workspace"] = workspace
            token = _reserve_formal_operation(workspace, operation)
            try:
                return function(*args, **kwargs)
            finally:
                _release_formal_operation(workspace, token)

        guarded.__name__ = function.__name__
        guarded.__doc__ = function.__doc__
        guarded.__wrapped__ = function
        return guarded

    return decorate


def _formal_io_lock(workspace: Path) -> threading.Lock:
    key = _workspace_key(workspace)
    with _FORMAL_IO_LOCKS_GUARD:
        return _FORMAL_IO_LOCKS.setdefault(key, threading.Lock())

try:
    _LEAN_CHECK_CONCURRENCY = max(
        1, min(4, int(os.environ.get("AGENT_MONITOR_LEAN_CHECK_CONCURRENCY", "1")))
    )
except ValueError:
    _LEAN_CHECK_CONCURRENCY = 1
# Mathlib compilation can consume most of this small host's memory. LLM calls
# may proceed in parallel, but kernel checks queue here by default.
_LEAN_CHECK_SLOTS = threading.BoundedSemaphore(_LEAN_CHECK_CONCURRENCY)

# Proof.lean is untrusted program input: elaboration may execute `run_cmd`,
# tactics, macros, plugins, and other Lean metaprograms. The kernel check must
# therefore happen only inside the transient systemd sandbox assembled below.
# These limits bound both the copied input and output captured from that unit.
LEAN_CHECK_MAX_SOURCE_BYTES = 2_000_000
LEAN_CHECK_MAX_OUTPUT_BYTES = 4_000_000
LEAN_CHECK_TRUSTED_SANDBOXES = {
    "systemd-socket-dynamic-user-v1",
}

# Prefer a recent stable Lean 4. Mathlib pins matter only when the generated
# file imports Mathlib — the scaffold omits that dependency by default.
DEFAULT_TOOLCHAIN = "leanprover/lean4:v4.14.0"

STATEMENT_FIDELITY = """STATEMENT FIDELITY — read this first. A file that compiles but states the
wrong thing is worse than one that fails to compile, because it looks verified.
- The main theorem must transcribe the PROBLEM itself, not a convenient special
  case: quantify over everything the problem quantifies over, keep every
  hypothesis, keep the exact conclusion. If the problem says "for all n", the
  statement starts `∀ n` (or takes `(n : Nat)`) — not a fixed `n = 7`.
- Never add a hypothesis that already assumes the conclusion, and never let the
  hypotheses be contradictory: a vacuous theorem proves nothing.
- Do not hand-roll a standard notion (`Odd`, `Even`, `Prime`, `Irrational`,
  `Dvd`, …) unless core Lean lacks it. If you must define one, define it exactly
  as the standard notion and say so in its docstring.
- Forbidden entirely: `axiom` (anything follows from it), `unsafe`, `opaque`,
  `@[implemented_by]`, `set_option debug.skipKernelTC true`.
- `native_decide` is not a kernel proof — avoid it. `decide` is fine when the
  goal is genuinely decidable and small.
"""

CITATION_RULE = """Citations attach to EACH declaration — never one list at the top of the file.
Immediately above every `theorem` / `lemma` / `def`, write a doc comment whose
last line starts with `Cites:`, entries separated by semicolons, each shaped
`<source> — <what it contributes>`:

  /-- Product of two odd naturals is odd.
  Cites: self-derived — direct expansion of (2a+1)(2b+1);
  [Hardy & Wright, Thm 1.2] — parity of products -/
  theorem main (a b : Nat) : ...

Use `self-derived` when you proved it inline here, and `uncited` when the
informal proof leans on something it never sourced. Never invent a source.
Mirror the same entries in the JSON `citations` array, each with a `decl` field
naming the declaration it belongs to.
"""

TRANSLATE_PROMPT = """You are a Lean 4 formalisation expert. Read the PROBLEM and the informal PROOF, then produce a Lean 4 file that states and proves the claim.

Respond with STRICT JSON only — no markdown fences, no commentary:
{
  "title": "short theorem name",
  "lean": "complete Lean 4 source for a single file named Proof.lean",
  "uses_mathlib": false,
  "main_decl": "name of the theorem that IS the problem's claim",
  "notes": "one short sentence about what was formalised / any gaps",
  "citations": [
    {"id": "1", "decl": "main", "text": "identifier or named result — what it contributes", "kind": "arxiv|doi|theorem|self-derived|uncited"}
  ]
}

{fidelity}
{citation_rule}
Rules for the Lean source:
- Lean 4 syntax only (not Lean 3).
- Prefer the Lean **core** library only (`Init` / `Nat` / `Int` / `List`).
  Set `uses_mathlib` to true ONLY if Mathlib is genuinely required, and then
  import only the specific modules required (for example,
  `Mathlib.Data.Nat.Choose.Basic`). Never use the broad `import Mathlib`: it
  loads the full library and can exhaust the checker timeout or memory budget.
  Core-only proofs are strongly preferred.
- Prefer `Nat` over `Int` when the claim is about non-negative integers /
  parity / divisibility — core `Nat` lemmas + `omega` / `ac_rfl` work; `Int`
  algebra and `ring` usually need Mathlib.
- Useful core patterns (copy freely):
  - Existentials: `obtain ⟨k, hk⟩ := h` then `refine ⟨…, ?_⟩`.
  - Nat algebra: `simp [Nat.mul_add, Nat.add_mul, Nat.mul_one, Nat.one_mul,
    Nat.mul_assoc]; ac_rfl`.
  - Linear Nat/Int goals: `omega`.
  - Concrete closed goals: `native_decide` / `decide`.
- Do NOT use Mathlib-only tactics (`ring`, `linarith`, `nlinarith`, `field_simp`,
  `norm_num`, `polyrith`) unless `uses_mathlib` is true.
- Include a single main theorem named `main` when possible, matching the PROBLEM.
- For a **Counterexample**, make `main` prove only a faithful negation of the
  original universal claim using the explicit witness. Do not conjoin optional
  minimality, every-case audits, or stronger side claims into `main`. Omit such
  bonus claims from the file unless the PROBLEM explicitly asks for them; a
  failed optional helper makes the whole Lean file fail.
- The proof must be complete: no `sorry`, no `admit`, no invented `axiom`.
- Keep the file self-contained. Add short helper lemmas with proofs if needed.
- If the informal artifact is **Partial Progress** or **Known/Open Status**, keep
  the exact open target as `main` with one honest `sorry`, but also formalise
  each feasible self-derived lemma as a separate, fully proved declaration.
  Do not discard verified partial progress merely because `main` remains open.
- Use ASCII Lean identifiers. Put informal commentary in `/-- ... -/` docs.
- Do not copy ProvingConsole runtime, harness, tool, skill-availability, or
  candidate-audit status into the Lean source or `notes`. The informal
  candidate audit is a separate pipeline; report only formalization, compiler,
  admission, and statement-fidelity facts here.
- If the informal proof has a genuine gap you cannot close, leave a SINGLE
  `sorry` on that subgoal and explain it in `notes` — do not invent a fake proof.
  A visible `sorry` is far better than a statement weakened until it goes through.

PROBLEM:
{problem}

PROOF:
{proof}
"""

REPAIR_PROMPT = """You are fixing a Lean 4 file that failed to compile. Respond with STRICT JSON only:
{
  "lean": "the full corrected Lean 4 source",
  "uses_mathlib": false,
  "notes": "what you changed",
  "citations": [{"id": "1", "decl": "main", "text": "…", "kind": "theorem|self-derived|arxiv|doi|uncited"}]
}

Rules: fix the ERRORS, do not dodge them. Keep the exact original PROBLEM claim
unchanged — weakening or specialising it to compile counts as a failure. If the
current `main` added nonessential conjuncts beyond the original claim, move those
to separate helpers only when they already compile; otherwise omit them from the
file rather than treating their removal as weakening. If a
subgoal is genuinely out of reach, leave one `sorry` there and
say so in `notes`. Do not invent Mathlib imports unless the errors clearly
require them. Keep the per-declaration `Cites:` doc comments intact.

{fidelity}
ORIGINAL SOURCE:
```lean
{lean}
```

COMPILER OUTPUT:
```
{errors}
```
"""

FEEDBACK_PROMPT = """You are revising a Lean 4 formalisation based on human feedback.
Respond with STRICT JSON only:
{
  "lean": "the full revised Lean 4 source",
  "uses_mathlib": false,
  "notes": "what you changed in response to the feedback",
  "citations": [{"id": "1", "decl": "main", "text": "…", "kind": "theorem|self-derived|arxiv|doi|uncited"}]
}

Rules:
- Apply the human's request faithfully.
- Prefer Lean core (`Init` / `Nat` / `Int`); only set uses_mathlib true if required.
- Prefer eliminating `sorry` / `admit` when the feedback asks for a complete proof.
- Keep theorem names stable unless the human asks to rename them.
- Do not invent Mathlib-only tactics unless uses_mathlib is true.
- Keep the per-declaration `Cites:` doc comments accurate; update them if the
  revision changes what a declaration relies on.

{fidelity}
CURRENT SOURCE:
```lean
{lean}
```

LATEST COMPILER / CHECKER LOG:
```
{errors}
```

HUMAN FEEDBACK:
{feedback}
"""

QUESTION_PROMPT = """You are the Lean assistant for a human-in-the-loop proof session.
Answer the user's question in clear, concise prose. Do not rewrite or weaken the
theorem, do not claim the original mathematical problem is proved merely because
Lean compiled a possibly different statement, and do not output JSON. Distinguish:
1. kernel acceptance of Proof.lean,
2. absence of sorry/admit,
3. statement fidelity to the original problem.

VERIFICATION STATUS:
{status}

STATEMENT FIDELITY:
{fidelity}

CURRENT SOURCE:
```lean
{lean}
```

USER QUESTION:
{question}
"""

AUDIT_PROMPT = """You are a strict Lean 4 formalisation auditor. The file below COMPILES. That
only means it is well-typed — it does NOT mean it formalises the PROBLEM. Your job
is to catch a Lean file that passes the compiler while proving the wrong thing.

Respond with STRICT JSON only:
{
  "faithful": true,
  "severity": "ok|minor|major",
  "main_decl": "name of the declaration that is supposed to be the problem's claim",
  "verdict": "one sentence — what this file actually proves",
  "issues": ["concrete, specific problems (empty if none)"],
  "statement_suggestion": "a faithful Lean signature for the main claim, or \\"\\""
}

Check, in order:
1. Does the main declaration's statement quantify over everything the PROBLEM
   quantifies over, with the same hypotheses and the same conclusion? A statement
   specialised to one instance, or with an extra convenient hypothesis, is `major`.
2. Are the hypotheses satisfiable? Contradictory or self-assuming hypotheses make
   the theorem vacuous — `major`.
3. Is the conclusion the real conclusion, not `True` / a restatement of a
   hypothesis / a strictly weaker claim? — `major`.
4. Do any local `def`s that model standard notions (`Odd`, `Prime`, `Irrational`,
   divisibility, …) actually match the standard meaning? A subtly wrong definition
   is `major`.
5. Is anything smuggled in via `axiom`, `sorry`, `admit`, `native_decide`,
   `set_option debug.skipKernelTC`, `unsafe`, `opaque`, `@[implemented_by]`?
6. Do the helper lemmas actually feed the main result, or is the main result
   proved by an unrelated shortcut?

Set `severity` to `major` if this file would let a wrong or materially weaker
claim be reported as verified, `minor` for cosmetic or trust caveats, `ok` if the
formalisation is faithful. Be concrete: name declarations and quantifiers.

PROBLEM:
{problem}

INFORMAL PROOF (context — may be longer than what was formalised):
{proof}

LEAN SOURCE:
```lean
{lean}
```
"""

STATEMENT_FIX_PROMPT = """A Lean 4 file compiled, but an audit found the STATEMENT unfaithful to the
PROBLEM. Rewrite the file so the statement is faithful, then prove it.

Respond with STRICT JSON only:
{
  "lean": "the full corrected Lean 4 source",
  "uses_mathlib": false,
  "main_decl": "name of the faithful main theorem",
  "notes": "how the statement changed",
  "citations": [{"id": "1", "decl": "main", "text": "…", "kind": "theorem|self-derived|arxiv|doi|uncited"}]
}

Fix the statement FIRST, then the proof. If the faithful statement is harder than
what you can close, keep the faithful statement and leave a single `sorry` on the
open subgoal — an honest `sorry` on the right claim beats a complete proof of the
wrong claim. Do not silence the audit by renaming things.

{fidelity}
{citation_rule}
PROBLEM:
{problem}

AUDIT FINDINGS:
{audit}

CURRENT SOURCE:
```lean
{lean}
```
"""

HARNESS_TASK = """You are formalising a mathematical proof in Lean 4, inside this directory.

Files here:
- `Proof.lean`  — the file you must produce/repair (this is the deliverable).
- `TASK.md`     — the problem statement and the informal proof, for reference.
- `lean-toolchain`, `lakefile.lean` — the project scaffold; leave them alone
  unless you genuinely need Mathlib.

This run requires a fresh `Proof.lean` whose bytes differ from the file present
when you started. Even if the current proof already compiles, make a real,
semantics-preserving improvement (for example simplify the proof term or
improve its theorem documentation/citation annotation), save the file, and
compile the changed bytes. Merely inspecting or compiling an unchanged file is
a failed harness run. Preserve the exact requested theorem statement.

How to work — you have a shell, so use it. Do not guess whether Lean accepts your
file. For a core-only file run:

    lean Proof.lean          # exit 0 and no output means accepted

If the file imports Mathlib, import only the specific modules needed (never the
broad `import Mathlib`) and run:

    lake env lean Proof.lean

Iterate: edit `Proof.lean`, run the appropriate command above, read the errors,
fix, and repeat until it is clean. Read the actual error messages — do not
rewrite the file from scratch on every failure. `lean --version` tells you the
toolchain. Prefer Lean core (`Init`, `Nat`, `Int`, `List`); `omega`, `decide`,
`simp`, `ac_rfl`, `induction`, `rcases`/`obtain` all work in core. `ring`,
`linarith`, `norm_num`, `field_simp` need Mathlib — use them only with a narrow
module import.

{fidelity}
{citation_rule}
When you are done, `lean Proof.lean` must exit 0. Finish by printing a short
summary: what the main theorem states, whether any `sorry` remains and why, and
which declarations carry which citations. Write no other files.
"""


def _fill_prompt_shared(text: str) -> str:
    return text.replace("{fidelity}", STATEMENT_FIDELITY).replace(
        "{citation_rule}", CITATION_RULE
    )


TRANSLATE_PROMPT = _fill_prompt_shared(TRANSLATE_PROMPT)
REPAIR_PROMPT = _fill_prompt_shared(REPAIR_PROMPT)
FEEDBACK_PROMPT = _fill_prompt_shared(FEEDBACK_PROMPT)
STATEMENT_FIX_PROMPT = _fill_prompt_shared(STATEMENT_FIX_PROMPT)
HARNESS_TASK = _fill_prompt_shared(HARNESS_TASK)


def lean_dir(workspace: Path) -> Path:
    return workspace / LEAN_DIRNAME


def _read_optional_workspace_text(
    workspace: Path,
    name: str,
    *,
    limit: int,
) -> str | None:
    """Read safely, distinguishing a valid empty file from an unsafe/missing one."""
    text = _read_workspace_text(workspace, name, limit=limit)
    if text:
        return text

    parts = Path(name).parts
    if (
        not parts
        or Path(name).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = -1
    descriptor = -1
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
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | nofollow,
            dir_fd=directory,
        )
        state = os.fstat(descriptor)
        if stat.S_ISREG(state.st_mode) and state.st_size == 0:
            return ""
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory >= 0:
            os.close(directory)
    return None


def _mathlib_project() -> Path | None:
    """The broker owns Mathlib; the application has no trusted project path."""
    return None


def toolchain_status() -> dict[str, Any]:
    """Report fixed broker endpoints without executing a local toolchain."""
    from agent_monitor import lean_broker

    sockets = lean_broker.socket_status()
    core_available = bool(sockets.get("core"))
    mathlib_available = bool(sockets.get("mathlib"))
    return {
        "lean": None,
        "lake": None,
        "elan": None,
        "mathlib_project": None,
        "version": lean_broker.EXPECTED_TOOLCHAIN if core_available else None,
        "available": core_available,
        "mathlib_available": mathlib_available,
        "checker_backend": "lean-broker-v1",
        "checker_sandbox": (
            lean_broker.SANDBOX_MARKER if core_available else None
        ),
    }


def _result_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / RESULT_FILENAME


def load_cached(workspace: Path) -> dict[str, Any] | None:
    """Read only a bounded regular cache file and require a JSON object."""
    path = _result_path(workspace)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError):
        return None
    try:
        state = os.fstat(descriptor)
        if (
            not stat.S_ISREG(state.st_mode)
            or state.st_size > MAX_RESULT_CACHE_BYTES
        ):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_RESULT_CACHE_BYTES + 1)
    except OSError:
        return None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if len(raw) > MAX_RESULT_CACHE_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _write_cached(workspace: Path, value: dict[str, Any]) -> None:
    """Atomically replace lean_verify.json with a private regular file."""
    if not isinstance(value, dict):
        raise TypeError("Lean result cache must be a JSON object")
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    if len(payload) > MAX_RESULT_CACHE_BYTES:
        raise ValueError("Lean result cache exceeds its size limit")

    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError("Lean result workspace is unavailable")
    target = root / RESULT_FILENAME
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".lean_verify.",
        suffix=".tmp",
        dir=str(root),
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(root, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _harness_conversation(workspace: Path, cached: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Codex-style JSONL harness log into bounded UI events.

    Started items are replaced by their completed form, so a command appears
    once with its command, full captured output and exit state. Non-JSON stderr
    is kept as runtime output instead of being silently discarded.
    """
    safe_log = _read_optional_workspace_text(
        workspace,
        f"{LEAN_DIRNAME}/harness.log",
        limit=MAX_HARNESS_LOG_BYTES,
    )
    raw_log = (
        safe_log if safe_log is not None else str(cached.get("log") or "")
    )
    # A pathological cached fallback can still be large. Keep the complete
    # normal interaction while protecting the run API from unbounded payloads.
    raw_truncated = len(raw_log) > MAX_HARNESS_LOG_BYTES
    raw_log = raw_log[-MAX_HARNESS_LOG_BYTES:]

    order: list[str] = []
    items: dict[str, dict[str, Any]] = {}
    runtime_lines: list[str] = []
    usage: dict[str, int] = {}
    serial = 0
    for line in raw_log.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                runtime_lines.append(line[:4000])
            continue
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "")
        if etype in {"item.started", "item.completed"} and isinstance(event.get("item"), dict):
            item = dict(event["item"])
            item_id = str(item.get("id") or f"event_{serial}")
            serial += 1
            if item_id not in items:
                order.append(item_id)
            items[item_id] = item
        elif etype == "turn.completed" and isinstance(event.get("usage"), dict):
            for key, value in event["usage"].items():
                try:
                    usage[str(key)] = int(value or 0)
                except (TypeError, ValueError):
                    continue

    conversation: list[dict[str, Any]] = []
    if runtime_lines:
        conversation.append(
            {
                "id": "runtime-output",
                "kind": "runtime",
                "role": "system",
                "title": "Harness runtime",
                "content": "\n".join(runtime_lines)[-12000:],
                "status": "completed",
            }
        )
    for item_id in order[-240:]:
        item = items[item_id]
        kind = str(item.get("type") or "event")[:80]
        status = str(item.get("status") or "completed")[:40]
        title = kind.replace("_", " ").title()
        role = "assistant"
        content = ""
        command = ""
        if kind == "agent_message":
            title = "Assistant message"
            content = str(item.get("text") or item.get("content") or "")
        elif kind == "reasoning":
            title = "Reasoning"
            role = "reasoning"
            content = str(item.get("text") or item.get("summary") or item.get("content") or "")
        elif kind == "command_execution":
            role = "tool"
            command = str(item.get("command") or "")
            title = command.strip().splitlines()[0][:180] or "Shell command"
            content = str(item.get("aggregated_output") or item.get("output") or "")
        elif kind in {"file_change", "file_edit"}:
            role = "tool"
            title = "Workspace edit"
            content = str(item.get("text") or item.get("diff") or item.get("output") or "")
            if not content:
                content = json.dumps(item.get("changes") or item, ensure_ascii=False, indent=2)
        else:
            role = "tool" if "tool" in kind or "search" in kind else "assistant"
            content = str(item.get("text") or item.get("output") or item.get("content") or "")
            if not content:
                content = json.dumps(item, ensure_ascii=False, indent=2)
        conversation.append(
            {
                "id": item_id[:160],
                "kind": kind,
                "role": role,
                "title": title,
                "content": content[-12000:],
                "command": command[:4000],
                "status": status,
                "exit_code": item.get("exit_code"),
            }
        )
    return {
        "events": conversation,
        "event_count": len(order) + (1 if runtime_lines else 0),
        "truncated": raw_truncated or len(order) > 240,
        "usage": usage,
    }


def monitor_summary(workspace: Path) -> dict[str, Any]:
    """Compact, UI-safe Lean telemetry for a run payload.

    The full cache includes the complete Lean source and compiler transcript.
    Monitor and Conversation only need the formalisation timeline, so keep this
    payload bounded while preserving the latest checker, fidelity, harness and
    human-intervention state.
    """
    cached = load_cached(workspace) or {}
    live_job = harness_job(workspace)
    if not cached and not live_job:
        return {
            "status": "none",
            "has_result": False,
            "attempt_count": 0,
            "attempts": [],
            "chat": [],
            "harness_running": False,
        }

    status = str(cached.get("status") or ("running" if live_job else "none"))
    if status == "running" and not live_job:
        status = "failed"

    attempts: list[dict[str, Any]] = []
    for index, raw in enumerate((cached.get("attempts") or [])[-24:]):
        attempt = raw if isinstance(raw, dict) else {}
        check = attempt.get("check") if isinstance(attempt.get("check"), dict) else {}
        sorry_count = int(attempt.get("sorry_count") or 0)
        action = str(attempt.get("action") or "check")
        attempt_status = str(check.get("status") or "")
        if check.get("ok"):
            if sorry_count:
                attempt_status = "incomplete"
            elif action == "compile":
                attempt_status = "compiled"
            else:
                attempt_status = "verified"
        elif not attempt_status:
            attempt_status = "failed"
        log = "\n".join(
            part.strip()
            for part in (
                str(attempt.get("stderr_tail") or ""),
                str(attempt.get("stdout_tail") or ""),
            )
            if part.strip()
        )
        attempts.append(
            {
                "index": index,
                "round": attempt.get("round", index),
                "action": action[:80],
                "status": attempt_status[:40],
                "ok": bool(check.get("ok")),
                "exit_code": check.get("exit_code"),
                "duration_s": float(check.get("duration_s") or 0),
                "sorry_count": sorry_count,
                "uses_mathlib": bool(attempt.get("uses_mathlib")),
                "toolchain": str(check.get("toolchain") or "")[:200],
                "log": log[-2400:],
            }
        )

    fidelity_raw = cached.get("fidelity") if isinstance(cached.get("fidelity"), dict) else {}
    audit_raw = fidelity_raw.get("audit") if isinstance(fidelity_raw.get("audit"), dict) else {}
    flags = []
    for raw in (fidelity_raw.get("flags") or [])[:16]:
        if not isinstance(raw, dict):
            continue
        flags.append(
            {
                "id": str(raw.get("id") or "")[:80],
                "severity": str(raw.get("severity") or "minor")[:20],
                "message": str(raw.get("message") or "")[:500],
                "lines": list(raw.get("lines") or [])[:8],
            }
        )

    chat = []
    for raw in (cached.get("chat") or [])[-40:]:
        if not isinstance(raw, dict):
            continue
        chat.append(
            {
                "role": str(raw.get("role") or "system")[:40],
                "content": str(raw.get("content") or "")[:4000],
                "ts": str(raw.get("ts") or "")[:80],
            }
        )

    harness = dict(cached.get("harness") or {})
    if live_job:
        harness.update(live_job)
    harness_prompt = _read_optional_workspace_text(
        workspace,
        f"{LEAN_DIRNAME}/TASK.md",
        limit=MAX_HARNESS_TASK_BYTES,
    ) or ""
    lean_src = str(cached.get("lean") or "")
    citations = _reconcile_citations(
        cached.get("citations"), lean_src, workspace=workspace
    )
    toolchain = cached.get("toolchain") if isinstance(cached.get("toolchain"), dict) else {}
    try:
        from agent_monitor import lean_kg

        checked_toolchain = str(toolchain.get("version") or "")
        kg_summary = lean_kg.snapshot_summary(
            lean_kg.load_snapshot(
                workspace, source=lean_src, toolchain=checked_toolchain
            )
            if checked_toolchain
            else None
        )
    except (OSError, ValueError, TypeError):
        kg_summary = {"status": "unavailable"}
    audit_issues = [str(item)[:500] for item in (audit_raw.get("issues") or [])[:12]]
    transcript = _harness_conversation(workspace, cached)
    return {
        "status": status,
        "has_result": status != "none" or bool(lean_src) or bool(live_job),
        "generated_at": str(cached.get("generated_at") or "")[:80],
        "action": str(cached.get("action") or "")[:80],
        "title": str(cached.get("title") or "")[:240],
        "notes": str(cached.get("notes") or "")[:1200],
        "model": str(cached.get("model") or "")[:160],
        "source": str(cached.get("source") or "")[:160],
        "lean_path": str(cached.get("lean_path") or f"{LEAN_DIRNAME}/{PROOF_FILENAME}")[:240],
        "lean_lines": len(lean_src.splitlines()),
        "uses_mathlib": bool(cached.get("uses_mathlib")),
        "sorry_count": int(cached.get("sorry_count") or 0),
        "sorry_lines": list(cached.get("sorry_lines") or [])[:20],
        "attempt_count": len(cached.get("attempts") or []),
        "attempts": attempts,
        "duration_s": round(sum(float(item.get("duration_s") or 0) for item in attempts), 2),
        "citation_count": len(citations),
        "declaration_count": len(fidelity_raw.get("declarations") or []),
        "knowledge_graph": kg_summary,
        "fidelity": {
            "severity": str(fidelity_raw.get("severity") or "ok")[:20],
            "flags": flags,
            "flag_count": len(fidelity_raw.get("flags") or []),
            "audit_ok": bool(audit_raw.get("ok")),
            "faithful": audit_raw.get("faithful"),
            "verdict": str(audit_raw.get("verdict") or "")[:800],
            "issues": audit_issues,
            "main_decl": str(audit_raw.get("main_decl") or "")[:120],
        },
        "toolchain": {
            "available": bool(toolchain.get("available")),
            "version": str(toolchain.get("version") or "")[:200],
            "mathlib_available": bool(toolchain.get("mathlib_available")),
        },
        "chat": chat,
        "conversation": transcript["events"],
        "conversation_event_count": transcript["event_count"],
        "conversation_truncated": transcript["truncated"],
        "harness_usage": transcript["usage"],
        "harness_prompt": harness_prompt,
        "harness_running": bool(live_job),
        "harness": harness,
    }


def _write_scaffold(ldir: Path, *, uses_mathlib: bool) -> None:
    ldir.mkdir(parents=True, exist_ok=True)
    (ldir / "lean-toolchain").write_text(DEFAULT_TOOLCHAIN + "\n", encoding="utf-8")
    # Minimal lakefile — Mathlib is only declared when the model asks for it.
    # Users who already have a global Mathlib cache can `lake update` later.
    if uses_mathlib:
        lake = (
            "import Lake\nopen Lake DSL\n\n"
            "package «proof» where\n"
            "  leanOptions := #[⟨`pp.unicode.fun, true⟩]\n\n"
            "require mathlib from git\n"
            '  "https://github.com/leanprover-community/mathlib4.git" @ "v4.14.0"\n\n'
            "@[default_target]\n"
            "lean_lib Proof where\n"
            "  roots := #[`Proof]\n"
        )
    else:
        lake = (
            "import Lake\nopen Lake DSL\n\n"
            "package «proof» where\n"
            "  leanOptions := #[⟨`pp.unicode.fun, true⟩]\n\n"
            "@[default_target]\n"
            "lean_lib Proof where\n"
            "  roots := #[`Proof]\n"
        )
    (ldir / "lakefile.lean").write_text(lake, encoding="utf-8")


def _strip_lean_fences(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:lean|lean4)?\s*(.*?)```", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    return text


def _citation_text_key(text: str) -> str:
    """Compare citations independent of harmless Markdown emphasis."""
    plain = re.sub(r"[`*_\[\]]", "", (text or "").lower())
    return " ".join(plain.split())


def _citation_identity_key(text: str) -> str:
    """Identify the same source across terse anchors and full references."""
    plain = _citation_text_key(text)
    doi = re.search(
        r"(?:\bdoi\s*:?\s*|\b)(10\.\d{4,9}/[^\s)\],;]+)", plain
    )
    if doi:
        return "doi:" + doi.group(1).rstrip(".,;:")
    arxiv = re.search(r"\b(?:arxiv[:\s/]*)?(\d{4}\.\d{4,5})(?:v\d+)?\b", plain)
    if arxiv:
        return "arxiv:" + arxiv.group(1)
    erdos = re.search(
        r"\berd[oő]s\s*problems?(?:\.com/|\s*#?\s*)(\d+)\b", plain
    )
    if erdos:
        return "erdos-problem:" + erdos.group(1)
    generic = re.sub(
        r"^(?:self[ -]derived|uncited)\s*(?:—|–|-|:)+\s*", "", plain
    )
    return generic.rstrip(" .,:;")


def _normalize_citations(raw: Any) -> list[dict[str, str]]:
    """Coerce model / cached citation payloads into a stable list.

    ``decl`` / ``line`` anchor a citation to the declaration it belongs to; they
    stay empty for file-level references harvested from older caches.
    """
    out: list[dict[str, Any]] = []
    if isinstance(raw, str) and raw.strip():
        raw = [raw]
    if not isinstance(raw, list):
        return out
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(raw, 1):
        decl, line = "", 0
        if isinstance(item, str):
            text, cid, kind = item.strip(), str(i), ""
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("title") or item.get("ref") or "").strip()
            cid = str(item.get("id") or item.get("num") or i).strip()
            kind = str(item.get("kind") or "").strip().lower()
            decl = str(item.get("decl") or item.get("declaration") or "").strip()
            try:
                line = int(item.get("line") or 0)
            except (TypeError, ValueError):
                line = 0
        else:
            continue
        # Old caches cap citation text at 300 characters, so the outcome label
        # itself may be truncated. Product control records are never part of a
        # mathematical citation; treat their marker as a hard boundary.
        text = re.split(
            r"\s*(?:ProvingConsole\s+outcome|degraded\s+audit)\s*:",
            text,
            maxsplit=1,
            flags=re.I,
        )[0].strip()
        if not text:
            continue
        # Legacy caches could contain inequality fragments because a numbered-
        # list parser scanned arbitrary Lean source (for example `1 ≤ k`).
        if re.match(r"^[<>=≤≥∧∨→↔]", text):
            continue
        key = (decl.lower(), _citation_text_key(text))
        if key in seen:
            continue
        seen.add(key)
        if not kind:
            low = text.lower()
            if "self-derived" in low or "self derived" in low:
                kind = "self-derived"
            elif "uncited" in low:
                kind = "uncited"
            elif re.search(r"arxiv", low):
                kind = "arxiv"
            elif re.search(r"\bdoi\b|10\.\d{4,9}/", low):
                kind = "doi"
            else:
                kind = "theorem"
        entry: dict[str, Any] = {"id": cid[:16], "text": text[:300], "kind": kind[:40]}
        if decl:
            entry["decl"] = decl[:80]
        if line > 0:
            entry["line"] = line
        out.append(entry)
        if len(out) >= 32:
            break
    return out


# Declarations we anchor citations to. `example` is included so anonymous claims
# still show up, even though it cannot be referenced by name.
_DECL_KINDS = (
    "theorem",
    "lemma",
    "def",
    "abbrev",
    "instance",
    "structure",
    "inductive",
    "example",
    "axiom",
    "opaque",
)
_DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+|partial\s+|scoped\s+|local\s+)*"
    r"(" + "|".join(_DECL_KINDS) + r")\b[ \t]*([^\s:({\[]*)"
)
_CITE_MARKER_RE = re.compile(
    r"(?im)^\s*(?:cites?|citations?|refs?|references?|sources?)\s*[:：]\s*"
)


def _decls_with_docs(lean: str) -> list[dict[str, Any]]:
    """Top-level declarations plus the comment block sitting right above each.

    A blank line breaks the association, matching how Lean attaches doc comments.
    """
    decls: list[dict[str, Any]] = []
    pending: list[str] = []
    block_depth = 0
    source = lean or ""
    executable_lines = _lean_executable_text(source).splitlines()
    for i, raw in enumerate(source.splitlines(), 1):
        executable = (
            executable_lines[i - 1] if i <= len(executable_lines) else ""
        )
        stripped = raw.strip()
        if block_depth:
            pending.append(re.sub(r"-/\s*$", "", stripped).strip())
            block_depth += raw.count("/-") - raw.count("-/")
            block_depth = max(0, block_depth)
            continue
        if stripped.startswith("/-"):
            body = stripped[2:].lstrip("-!")
            block_depth = raw.count("/-") - raw.count("-/")
            if block_depth <= 0:
                pending.append(body.split("-/")[0].strip())
                block_depth = 0
            else:
                pending.append(body.strip())
            continue
        if stripped.startswith("--"):
            pending.append(stripped.lstrip("-").strip())
            continue
        if not stripped:
            pending = []
            continue
        m = _DECL_RE.match(executable)
        if m:
            kind = m.group(1)
            name = (m.group(2) or "").strip()
            decls.append(
                {
                    "kind": kind,
                    "name": name or f"{kind} @{i}",
                    "line": i,
                    "doc": "\n".join(x for x in pending if x).strip(),
                }
            )
        pending = []
    return decls


def _citations_by_decl(lean: str) -> list[dict[str, str]]:
    """Read `Cites:` lines out of each declaration's doc comment."""
    items: list[dict[str, Any]] = []
    for d in _decls_with_docs(lean):
        doc = str(d.get("doc") or "")
        m = _CITE_MARKER_RE.search(doc)
        if not m:
            continue
        blob = doc[m.end() :]
        for entry in re.split(r"\s*;\s*|\n", blob):
            entry = entry.strip().strip(".,").strip()
            if not entry or _CITE_MARKER_RE.match(entry):
                continue
            items.append(
                {
                    "id": str(len(items) + 1),
                    "text": entry,
                    "decl": d["name"],
                    "line": d["line"],
                }
            )
    return _normalize_citations(items)


def _citations_from_lean(lean: str) -> list[dict[str, str]]:
    """Per-declaration `Cites:` entries first, then any file-level references."""
    return _citations_by_decl(lean) + _citations_from_module_doc(lean)


def _citations_from_module_doc(lean: str) -> list[dict[str, str]]:
    """Pull a References list out of a `/-! ... -/` module docstring if present."""
    if not lean:
        return []
    docs = re.findall(r"/-!(.*?)-/", lean, flags=re.S)
    # Numbered reference parsing is valid only inside an actual module doc.
    # Scanning arbitrary Lean source misreads inequalities such as `1 ≤ k` as
    # bibliography entries. Bare arXiv/DOI identifiers are still found below.
    blob = "\n".join(docs)
    # Prefer an explicit References heading inside the docstring.
    m = re.search(r"(?im)^\s*references\s*:?\s*$", blob)
    body = blob[m.end() :] if m else blob
    found: list[dict[str, str]] = []
    for raw in body.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line:
            continue
        num = re.match(r"^\[?(\d{1,3})\]?[.):\s]\s*(.+)$", line)
        if num:
            found.append({"id": num.group(1), "text": num.group(2).strip()[:300], "kind": ""})
        elif m and found:
            found[-1]["text"] = (found[-1]["text"] + " " + line)[:300]
        elif m:
            found.append({"id": str(len(found) + 1), "text": line[:300], "kind": ""})
    if found:
        return _normalize_citations(found)
    # Bare arXiv / DOI anywhere in the Lean file.
    bare = []
    for mm in re.finditer(
        r"(arxiv[:\s/]*\d{4}\.\d{4,5}(?:v\d+)?|doi[:\s]*10\.\d{4,9}/[^\s)\],]+)",
        lean,
        flags=re.I,
    ):
        bare.append(mm.group(1).replace(" ", ""))
    return _normalize_citations(bare)


def _citations_from_proof_md(workspace: Path) -> list[dict[str, str]]:
    """Fallback: harvest the informal proof's References section."""
    for name in ("proof.md", "proof.tex"):
        text = _read_optional_workspace_text(
            workspace,
            name,
            limit=MAX_WORKSPACE_SOURCE_BYTES,
        )
        if not text:
            continue
        lines = text.splitlines()
        start = -1
        for i, ln in enumerate(lines):
            if re.match(r"^\s*#{1,6}\s*(references|bibliography|sources|citations)\b", ln, re.I):
                start = i + 1
                break
        if start < 0:
            continue
        body: list[str] = []
        for ln in lines[start:]:
            if re.match(r"^\s*#{1,6}\s", ln):
                break
            if re.match(
                r"^\s*(?:ProvingConsole\s+outcome|degraded\s+audit)\s*:",
                ln,
                re.I,
            ):
                break
            body.append(ln)
        items = []
        for raw in body:
            line = raw.strip().lstrip("-*+ ").strip()
            if not line:
                continue
            num = re.match(r"^\[?(\d{1,3})\]?[.):\s]\s*(.+)$", line)
            if num:
                items.append({"id": num.group(1), "text": num.group(2).strip(), "kind": ""})
            elif items:
                items[-1]["text"] = (items[-1]["text"] + " " + line)[:300]
            else:
                # Prose-only References ("… is self-derived", bare arXiv, …)
                items.append({"id": str(len(items) + 1), "text": line, "kind": ""})
        if items:
            return _normalize_citations(items)
    return []


def _merge_citations(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    """Union of citation groups, keyed by declaration and source identity.

    The same text may legitimately appear on two declarations, so the key
    includes ``decl``; a floating copy of an already-anchored citation is dropped
    so the UI shows it once, under its declaration.
    """
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for c in group or []:
            identity = _citation_identity_key(c.get("text") or "")
            if not identity:
                continue
            key = ((c.get("decl") or "").lower(), identity)
            if key in seen:
                continue
            seen.add(key)
            merged.append(c)
    anchored = {
        _citation_identity_key(c.get("text") or "")
        for c in merged
        if c.get("decl")
    }
    merged = [
        c
        for c in merged
        if c.get("decl") or _citation_identity_key(c.get("text") or "") not in anchored
    ]
    normalized: list[dict[str, str]] = []
    for index, citation in enumerate(merged[:32], 1):
        entry = dict(citation)
        entry["id"] = str(index)
        normalized.append(entry)
    return normalized


def _anchor_citations(cites: list[dict[str, str]], lean: str) -> list[dict[str, str]]:
    """Resolve each citation's ``decl`` to a line number in the current source."""
    by_name: dict[str, int] = {}
    for d in _decls_with_docs(lean):
        by_name.setdefault(str(d["name"]).lower(), int(d["line"]))
    out: list[dict[str, str]] = []
    for c in cites:
        entry = dict(c)
        decl = str(entry.get("decl") or "").strip()
        if decl:
            line = by_name.get(decl.lower())
            if line:
                entry["line"] = line
            else:
                # The model named a declaration that isn't in the file — keep the
                # citation but don't pretend we know where it lives.
                entry.pop("line", None)
        out.append(entry)
    return out


def _reconcile_citations(
    raw: Any, lean: str, *, workspace: Path | None = None
) -> list[dict[str, str]]:
    """Clean legacy cached citations and merge current source anchors."""
    groups = [
        _anchor_citations(_normalize_citations(raw), lean),
        _citations_from_lean(lean),
    ]
    if workspace is not None:
        groups.append(_citations_from_proof_md(workspace))
    return _merge_citations(*groups)


def _parse_model_payload(content: str) -> dict[str, Any]:
    """Accept either strict JSON or a bare Lean dump."""
    try:
        data = _extract_json(str(content))
    except (ValueError, json.JSONDecodeError):
        lean = _normalize_lean_imports(_strip_lean_fences(str(content)))
        if "theorem" in lean or "lemma" in lean or "example" in lean:
            return {
                "title": "",
                "lean": lean,
                "uses_mathlib": "import Mathlib" in lean or "import mathlib" in lean.lower(),
                "notes": "model returned raw Lean (no JSON wrapper)",
                "main_decl": "",
                "citations": _citations_from_lean(lean),
            }
        raise ValueError("model response contained neither JSON nor recognisable Lean")
    lean = _normalize_lean_imports(
        _strip_lean_fences(str(data.get("lean") or data.get("code") or ""))
    )
    if not lean.strip():
        raise ValueError("model returned empty Lean source")
    uses = data.get("uses_mathlib")
    if uses is None:
        uses = bool(re.search(r"(?m)^\s*import\s+Mathlib\b", lean))
    cites = _merge_citations(
        _anchor_citations(
            _normalize_citations(data.get("citations") or data.get("references")), lean
        ),
        _citations_from_lean(lean),
    )
    return {
        "title": str(data.get("title") or "")[:200],
        "lean": lean,
        "uses_mathlib": bool(uses),
        "notes": str(data.get("notes") or "")[:500],
        "main_decl": str(data.get("main_decl") or "")[:80],
        "citations": cites,
    }


def _lean_executable_text(lean: str) -> str:
    """Blank Lean comments/strings while preserving executable line numbers."""
    source = lean or ""
    out: list[str] = []
    i = 0
    block_depth = 0
    in_string = False
    while i < len(source):
        if block_depth:
            if source.startswith("/-", i):
                block_depth += 1
                out.extend("  ")
                i += 2
            elif source.startswith("-/", i):
                block_depth -= 1
                out.extend("  ")
                i += 2
            else:
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            continue
        if in_string:
            if source[i] == "\\" and i + 1 < len(source):
                out.extend("  ")
                i += 2
            else:
                if source[i] == '"':
                    in_string = False
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            continue
        if source.startswith("--", i):
            while i < len(source) and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if source.startswith("/-", i):
            block_depth = 1
            out.extend("  ")
            i += 2
            continue
        if source[i] == '"':
            in_string = True
            out.append(" ")
            i += 1
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


def _count_sorry(lean: str) -> int:
    return len(re.findall(r"\b(sorry|admit)\b", _lean_executable_text(lean)))


def _normalize_lean_imports(lean: str) -> str:
    """Map model-generated legacy imports onto the installed Lean 4.14 modules."""
    lean = re.sub(
        r"(?m)^\s*import\s+Mathlib\.Tactic\.Omega\s*$",
        "import Lean.Elab.Tactic.Omega",
        lean or "",
    )
    lean = re.sub(
        r"(?m)^\s*import\s+Mathlib\.Tactic\.Nlinarith\s*$",
        "import Mathlib.Tactic.Linarith",
        lean,
    )
    lean = re.sub(
        r"(?m)^\s*import\s+Mathlib\.Data\.Int\.Basic\s*$",
        "import Mathlib.Data.Int.Order.Basic",
        lean,
    )
    lean = re.sub(
        r"(?m)^\s*import\s+Mathlib\.Data\.Fin\.Interval\s*$",
        "import Mathlib.Order.Fin.Basic",
        lean,
    )
    if re.search(r"\bprimeFactors\b", lean):
        lean = re.sub(
            r"(?m)^\s*import\s+Mathlib\.Data\.Nat\.Prime\.(?:Defs|Finset)\s*$",
            "import Mathlib.Data.Nat.PrimeFin",
            lean,
        )
    if re.search(r"\bderiving\s+[^\n]*\bFintype\b", lean):
        lean = re.sub(
            r"(?m)^\s*import\s+Mathlib\.Data\.Fintype\.Basic\s*$",
            "import Mathlib.Tactic.DeriveFintype",
            lean,
        )
    return lean


_CLAUDE_ACCOUNT_MODELS = {"sonnet", "opus", "fable", "claude-haiku-4-5"}
_CLAUDE_LEAN_SYSTEM = (
    "You are a Lean 4 formalization expert. Preserve the exact mathematical "
    "claim, follow the requested strict output contract, and return only the "
    "requested content without markdown commentary."
)

# Response-only subscription adapters receive an explicit ``source_env``.  It
# must contain enough non-secret process context to launch Node-based CLIs
# (whose entrypoints commonly use ``#!/usr/bin/env node``), but it must never
# inherit provider credentials from the long-lived server process.  Per-user
# settings are overlaid separately below and subscription routing then scrubs
# all metered provider variables.
_FORMAL_RUNTIME_ENV_NAMES = (
    CLAUDE_DERIVED_TIMEOUT_ENV,
    "PATH",
    "HOME",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def _claude_account_model(model: str | None, env: dict[str, str]) -> str:
    selected = str(model or "").strip().lower()
    if selected in _CLAUDE_ACCOUNT_MODELS:
        return selected
    configured = str(env.get("AGENT_MONITOR_CLAUDE_MODEL") or "").strip().lower()
    return configured if configured in _CLAUDE_ACCOUNT_MODELS else "sonnet"


def _llm_environment(
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build the LLM environment while preserving a run's account route.

    Direct API and Codex behavior stays unchanged. Claude account mode is
    made explicit so the native backend can guarantee it never falls through
    to an Anthropic API key or a cloud-provider credential.
    """
    from agent_monitor.jobs import _configure_auth_route, _route_from_record
    from agent_monitor.settings import resolved_user_env

    runtime_env = {
        name: os.environ[name]
        for name in _FORMAL_RUNTIME_ENV_NAMES
        if os.environ.get(name)
    }
    env = dict(resolved_user_env(user) or {})
    route = _route_from_record(run_record)
    if route is None:
        # Only an explicitly persisted setting may select a subscription for a
        # legacy/unpinned operation. An empty legacy environment historically
        # used direct API first and then the guarded Codex intervention
        # fallback; account_runtime's old default-to-Codex compatibility rule
        # must not manufacture a connected subscription here.
        selected_runtime = str(
            env.get("AGENT_MONITOR_ACCOUNT_RUNTIME") or ""
        ).strip().lower()
        if not selected_runtime:
            if str(env.get("AGENT_MONITOR_USE_CLAUDE") or "").strip().lower() in {
                "1", "true", "on", "yes"
            }:
                selected_runtime = "claude"
            elif str(env.get("AGENT_MONITOR_USE_CODEX") or "").strip().lower() in {
                "1", "true", "on", "yes"
            }:
                selected_runtime = "codex"
        route = {
            "api": "api_key",
            "codex": "codex_subscription",
            "claude": "claude_subscription",
        }.get(selected_runtime, "api_key")
    if route in {"claude_subscription", "codex_subscription"}:
        env = _configure_auth_route(
            env,
            route=route,
            user_id=(user or {}).get("id") or (run_record or {}).get("owner_id"),
        )
    # Seed runtime values after auth routing so existing route validation sees
    # only the account/provider settings it owns. ``setdefault`` preserves any
    # deliberately supplied non-secret runtime override.
    for name, value in runtime_env.items():
        env.setdefault(name, value)
    if (
        route == "api_key"
        and str((run_record or {}).get("credential_source") or "").strip()
        == "sponsored_kimi"
    ):
        from agent_monitor import sponsored_kimi_client

        owner_id = (user or {}).get("id") or (run_record or {}).get("owner_id")
        cache_key = str((run_record or {}).get("run_id") or "formal")
        env.update(
            sponsored_kimi_client.issue_credentials(
                engine="formal",
                client_id=owner_id,
                cache_key=cache_key,
                minimum_ttl_seconds=600,
            )
        )
    if route == "claude_subscription":
        recorded_model = str((run_record or {}).get("model") or "").strip().lower()
        if recorded_model in _CLAUDE_ACCOUNT_MODELS:
            env["AGENT_MONITOR_CLAUDE_MODEL"] = recorded_model
    from agent_monitor.run_tuning import from_record as apply_recorded_tuning

    apply_recorded_tuning(env, run_record)
    return env


# Authorship evidence travels on the model string returned by the provider call
# (see ``proof_graph.ObservedModel``).
from agent_monitor.proof_graph import ObservedModel as _ObservedModel


def _chat(
    env: dict[str, str],
    model: str | None,
    prompt: str,
    timeout: float | None = None,
) -> tuple[str, str]:
    legacy_timeout = 240 if timeout is None else timeout
    if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
        from agent_monitor.runners.claude_backend import ClaudeBackendError, claude_exec

        selected = _claude_account_model(model, env)
        claude_timeout = (
            _claude_derived_timeout(env) if timeout is None else timeout
        )
        try:
            result = claude_exec(
                _CLAUDE_LEAN_SYSTEM,
                prompt,
                model=selected,
                timeout=claude_timeout,
                source_env=env,
            )
        except ClaudeBackendError as exc:
            raise ValueError(str(exc)) from exc
        content = str(result.text or "").strip()
        if not content:
            raise ValueError("Claude Code returned an empty Lean response")
        return str(result.model or selected), content
    if env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        from agent_monitor.proof_graph import _run_codex_prompt_env

        with tempfile.TemporaryDirectory(prefix="lean-codex-response-") as tmp:
            content = _run_codex_prompt_env(
                env, Path(tmp), prompt, model=model, timeout=legacy_timeout
            ).strip()
        if not content:
            raise ValueError("Codex returned an empty Lean response")
        return str(model or "codex"), content
    chosen, content, _provider = _call_llm(
        env, model, prompt, timeout=legacy_timeout
    )
    return chosen, content


def _terminate_process_group(proc: subprocess.Popen[str], *, force: bool = False) -> None:
    """Terminate a verifier and every Lake/Lean child it spawned."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            proc.kill() if force else proc.terminate()
        except OSError:
            pass


def _lean_check_failure(
    message: str,
    *,
    started: float,
    command: list[str] | None = None,
    toolchain: str | None = None,
    sandbox: str | None = None,
    stdout: str = "",
    exit_code: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": message,
        "duration_s": round(time.monotonic() - started, 2),
    }
    if toolchain:
        result["toolchain"] = toolchain
    if sandbox:
        result["sandbox"] = sandbox
    return result


def _lean_source_snapshot(ldir: Path) -> tuple[Path, bytes]:
    """Read Proof.lean through directory/file descriptors without symlinks."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError("no-follow file-descriptor checks are unavailable")
    requested = Path(ldir).absolute()
    resolved = requested.resolve(strict=True)
    if resolved != requested:
        raise ValueError("lean/ or one of its parents is a symbolic link")

    directory_descriptor = os.open(
        requested,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        directory_state = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(directory_state.st_mode):
            raise ValueError("lean/ must be a regular directory")
        source_descriptor = os.open(
            PROOF_FILENAME,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        try:
            source_state = os.fstat(source_descriptor)
            if not stat.S_ISREG(source_state.st_mode):
                raise ValueError("Proof.lean must be a regular file")
            if source_state.st_size > LEAN_CHECK_MAX_SOURCE_BYTES:
                raise ValueError("Proof.lean exceeds the source-size limit")
            with os.fdopen(source_descriptor, "rb", closefd=False) as stream:
                source = stream.read(LEAN_CHECK_MAX_SOURCE_BYTES + 1)
        finally:
            os.close(source_descriptor)
    finally:
        os.close(directory_descriptor)
    if len(source) > LEAN_CHECK_MAX_SOURCE_BYTES:
        raise ValueError("Proof.lean exceeds the source-size limit")
    return resolved, source


def _run_lean_check(ldir: Path, uses_mathlib: bool) -> dict[str, Any]:
    """Compile an exact source snapshot through the mandatory Lean broker."""
    from agent_monitor import lean_broker

    started = time.monotonic()
    try:
        timeout_s = max(
            1, int(os.environ.get("AGENT_MONITOR_LEAN_CHECK_TIMEOUT", "100"))
        )
    except ValueError:
        timeout_s = 100
    client_timeout = min(float(timeout_s), lean_broker.MAX_CLIENT_TIMEOUT)

    try:
        ldir, source_bytes = _lean_source_snapshot(Path(ldir))
        source_text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _lean_check_failure(
            f"Lean checker input rejected: {exc}",
            started=started,
        )
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if re.search(r"(?m)^\s*import\s+Mathlib\s*$", source_text):
        return _lean_check_failure(
            (
                "Broad `import Mathlib` is disabled on this server because it can "
                "exhaust the shared checker memory. Import only the specific "
                "Mathlib modules required by this proof."
            ),
            started=started,
        )

    try:
        with _LEAN_CHECK_SLOTS:
            broker_result = lean_broker.check_source(
                source_text,
                uses_mathlib=uses_mathlib,
                timeout=client_timeout,
            )
    except lean_broker.LeanBrokerError as exc:
        return _lean_check_failure(
            (
                "Lean checker broker unavailable; refused to run Proof.lean "
                f"through any local fallback: {exc}"
            ),
            started=started,
        )

    try:
        _current_ldir, current_source = _lean_source_snapshot(ldir)
        source_unchanged = current_source == source_bytes
    except (OSError, ValueError):
        source_unchanged = False
    failures: list[str] = []
    if not source_unchanged:
        failures.append("Proof.lean changed while Lean was checking it")
    if broker_result.get("source_sha256") != source_sha256:
        failures.append("Lean broker did not attest the exact source")
    sandbox = str(broker_result.get("sandbox") or "")
    if sandbox not in LEAN_CHECK_TRUSTED_SANDBOXES:
        failures.append("Lean broker sandbox attestation was not trusted")
    broker_status = str(broker_result.get("status") or "")
    exit_code = broker_result.get("exit_code")
    if broker_status == "timed_out":
        failures.append("Lean broker timed out")
    elif broker_status != "completed":
        failures.append(
            str(broker_result.get("error") or "Lean broker check failed")
        )
    if broker_result.get("stdout_truncated") or broker_result.get(
        "stderr_truncated"
    ):
        failures.append("Lean checker diagnostics were truncated")

    stderr = str(broker_result.get("stderr") or "")
    broker_error = str(broker_result.get("error") or "")
    if broker_error:
        stderr = (stderr + "\n" + broker_error).strip()
    if failures:
        stderr = (stderr + "\n" + "\n".join(failures)).strip()
    ok = bool(
        not failures
        and broker_status == "completed"
        and exit_code == 0
        and source_unchanged
    )
    profile = "mathlib" if uses_mathlib else "core"
    return {
        "ok": ok,
        "status": "verified" if ok else "failed",
        "command": ["lean-broker", profile],
        "exit_code": exit_code,
        "stdout": str(broker_result.get("stdout") or ""),
        "stderr": stderr,
        "duration_s": float(broker_result.get("duration") or 0.0),
        "toolchain": str(broker_result.get("toolchain") or ""),
        "sandbox": sandbox,
        "source_sha256": source_sha256,
        "checker_profile": profile,
        "release_id": str(broker_result.get("release_id") or ""),
        "toolchain_tree_sha256": str(
            broker_result.get("toolchain_tree_sha256") or ""
        ),
        "mathlib_tree_sha256": str(
            broker_result.get("mathlib_tree_sha256") or ""
        ),
    }


def _detect_mathlib(lean: str) -> bool:
    return bool(re.search(r"(?m)^\s*import\s+Mathlib\b", lean or ""))


def _sorry_lines(lean: str) -> list[int]:
    executable = _lean_executable_text(lean)
    return [
        i
        for i, line in enumerate(executable.splitlines(), 1)
        if re.search(r"\b(sorry|admit)\b", line)
    ]


# Patterns that make a file compile while proving less than it appears to.
# (regex, id, severity, message) — `{m}` is filled with the first capture group.
_CHEAT_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        r"(?m)^\s*axiom\s+([^\s:({\[]+)",
        "axiom",
        "major",
        "declares `axiom {m}` — an unproved axiom makes anything reachable",
    ),
    (
        r"set_option\s+debug\.skipKernelTC\s+true",
        "skip_kernel_tc",
        "major",
        "`set_option debug.skipKernelTC true` turns off the kernel check",
    ),
    (
        r"(?m)^\s*unsafe\s+(?:def|theorem|lemma|abbrev)\b",
        "unsafe",
        "major",
        "`unsafe` declarations are not kernel-checked",
    ),
    (
        r"@\[implemented_by",
        "implemented_by",
        "major",
        "`@[implemented_by]` swaps in unverified code",
    ),
    (
        r"(?m)^\s*opaque\s+([^\s:({\[]+)",
        "opaque",
        "minor",
        "`opaque {m}` asserts a value exists without defining it",
    ),
    (
        r"\bnative_decide\b",
        "native_decide",
        "minor",
        "`native_decide` trusts the compiler, not the kernel",
    ),
    (
        r"(?m)(?::|→)\s*True\s*(?::=|$)",
        "trivial_conclusion",
        "major",
        "a statement concludes `True`, which says nothing",
    ),
    (
        r"\(\s*[A-Za-z_][^:()]*:\s*False\s*\)",
        "false_hypothesis",
        "major",
        "a hypothesis is `False`, so the theorem is vacuous",
    ),
    (
        r"(?m)^\s*(?:private\s+)?(?:def|abbrev|inductive|structure)\s+"
        r"(Odd|Even|Prime|Irrational|Rational|Dvd|Divides|Divisible|Coprime|IsSquare"
        r"|Sqrt|Gcd|Lcm|Continuous|Differentiable|Deriv|Integral|Limit|Converges)\b",
        "handrolled_notion",
        "minor",
        "hand-rolled `{m}` — confirm it matches the standard notion",
    ),
)


def _fidelity_flags(lean: str) -> list[dict[str, Any]]:
    """Mechanical red flags: ways a file can typecheck without proving the claim."""
    raw = lean or ""
    src = _lean_executable_text(raw)
    flags: list[dict[str, Any]] = []
    for pattern, fid, severity, message in _CHEAT_RULES:
        hits = list(re.finditer(pattern, src))
        if not hits:
            continue
        first = hits[0].group(1) if hits[0].groups() else ""
        lines = sorted({src.count("\n", 0, h.start()) + 1 for h in hits})
        flags.append(
            {
                "id": fid,
                "severity": severity,
                "message": message.replace("{m}", first),
                "lines": lines[:8],
            }
        )
    slines = _sorry_lines(raw)
    if slines:
        flags.append(
            {
                "id": "sorry",
                "severity": "minor",
                "message": f"{len(slines)} sorry/admit — the proof has an admitted gap",
                "lines": slines[:8],
            }
        )
    decls = _decls_with_docs(raw)
    named = [d for d in decls if d["kind"] in {"theorem", "lemma"}]
    if not named:
        flags.append(
            {
                "id": "no_theorem",
                "severity": "major" if not decls else "minor",
                "message": "no named `theorem`/`lemma` — nothing here states the claim",
                "lines": [],
            }
        )
    return flags


def _fidelity_severity(flags: list[dict[str, Any]], audit: dict[str, Any] | None) -> str:
    if any(f.get("severity") == "major" for f in flags):
        return "major"
    if audit and audit.get("ok") and not audit.get("faithful"):
        return str(audit.get("severity") or "major")
    if any(f.get("severity") == "minor" for f in flags):
        return "minor"
    return "ok"


def _audit_faithfulness(
    env: dict[str, str],
    model: str | None,
    *,
    problem: str,
    proof: str,
    lean: str,
) -> dict[str, Any]:
    """Ask a model whether the Lean statement really is the problem's claim."""
    prompt = (
        AUDIT_PROMPT.replace("{problem}", (problem or "(not recorded)")[:6000])
        .replace("{proof}", (proof or "(not available)")[:20000])
        .replace("{lean}", (lean or "")[:40000])
    )
    try:
        used, content = _chat(env, model, prompt)
        data = _extract_json(str(content))
    except (ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return {"ok": False, "error": str(exc)[:300]}
    if not isinstance(data, dict):
        return {"ok": False, "error": "auditor returned a non-object"}
    issues = [str(x)[:400] for x in (data.get("issues") or []) if str(x).strip()][:10]
    severity = str(data.get("severity") or "").strip().lower()
    faithful = bool(data.get("faithful"))
    if severity not in {"ok", "minor", "major"}:
        severity = "ok" if faithful else "major"
    # A model that lists major issues but ticks `faithful` is contradicting itself.
    if severity == "major":
        faithful = False
    return {
        "ok": True,
        "faithful": faithful,
        "severity": severity,
        "verdict": str(data.get("verdict") or "")[:600],
        "issues": issues,
        "main_decl": str(data.get("main_decl") or "")[:80],
        "statement_suggestion": str(data.get("statement_suggestion") or "")[:600],
        "model": used,
    }


def _apply_fidelity(
    status: str, flags: list[dict[str, Any]], audit: dict[str, Any] | None
) -> str:
    """Downgrade a clean compile that provably doesn't state the problem."""
    if status != "verified":
        return status
    return "unfaithful" if _fidelity_severity(flags, audit) == "major" else status


def _fidelity_summary(flags: list[dict[str, Any]], audit: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if audit and audit.get("ok"):
        if audit.get("verdict"):
            parts.append(f"Audit: {audit['verdict']}")
        for issue in (audit.get("issues") or [])[:3]:
            parts.append(f"• {issue}")
    majors = [f for f in flags if f.get("severity") == "major"]
    for f in majors[:3]:
        where = f" (line {', '.join(map(str, f['lines']))})" if f.get("lines") else ""
        parts.append(f"• {f['message']}{where}")
    return " ".join(parts)


def _status_from_check(check: dict[str, Any], sorry_n: int, *, mode: str) -> str:
    """Map compiler result → UI status.

    - compile: exit 0 → ``compiled`` (sorry allowed); errors → ``failed``
    - check / verify: exit 0 + no sorry → ``verified``; exit 0 + sorry →
      ``incomplete``; errors → ``failed``
    """
    if check.get("status") == "toolchain_missing":
        return "toolchain_missing"
    if not check.get("ok"):
        return "failed"
    if mode == "compile":
        return "compiled"
    return "incomplete" if sorry_n > 0 else "verified"


def _check_summary(lean: str, check: dict[str, Any], status: str) -> str:
    lines = _sorry_lines(lean)
    parts: list[str] = []
    if status == "verified":
        parts.append("Check passed — Lean accepted the file with no errors and no sorry.")
    elif status == "unfaithful":
        parts.append(
            "Lean accepted the file, but the statement does not faithfully capture "
            "the problem — see Fidelity."
        )
    elif status == "compiled":
        parts.append("Compile succeeded (exit 0).")
        if lines:
            parts.append(f"Note: {len(lines)} sorry/admit remain at line(s) {', '.join(map(str, lines[:12]))}.")
    elif status == "incomplete":
        parts.append(
            f"Typechecks, but {len(lines)} sorry/admit remain at line(s) "
            f"{', '.join(map(str, lines[:12])) or '?'}."
        )
    elif status == "toolchain_missing":
        parts.append(check.get("stderr") or "Lean toolchain not installed.")
    else:
        parts.append("Compile/check failed — see log.")
    return " ".join(parts)


def _problem_and_proof(
    workspace: Path, run_record: dict[str, Any] | None = None
) -> tuple[str, str]:
    """The problem statement and the informal proof, for prompts and audits."""
    problem = (
        _read_optional_workspace_text(
            workspace,
            "problem.txt",
            limit=MAX_WORKSPACE_PROBLEM_BYTES,
        )
        or ""
    ).strip()
    try:
        _src, proof = _proof_source(workspace, run_record)
    except (OSError, ValueError):
        proof = ""
    return problem, proof or ""


def read_source(workspace: Path) -> str:
    source = _read_optional_workspace_text(
        workspace,
        f"{LEAN_DIRNAME}/{PROOF_FILENAME}",
        limit=LEAN_CHECK_MAX_SOURCE_BYTES,
    )
    if source is not None:
        return source
    cached = load_cached(workspace) or {}
    return str(cached.get("lean") or "")


def _append_chat(workspace: Path, role: str, content: str) -> list[dict[str, Any]]:
    cached = load_cached(workspace) or {}
    chat = list(cached.get("chat") or [])
    chat.append(
        {
            "role": role,
            "content": content,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    # Keep the transcript bounded.
    del chat[:-80]
    cached["chat"] = chat
    # Ensure we don't wipe the rest of the cache if this is the first write.
    if "lean" not in cached:
        cached["lean"] = read_source(workspace)
        cached["status"] = cached.get("status") or "none"
        cached["lean_path"] = f"{LEAN_DIRNAME}/{PROOF_FILENAME}"
    _write_cached(workspace, cached)
    return chat


def _trusted_kernel_receipt(check: Any) -> dict[str, Any] | None:
    """Normalize a fresh in-memory checker result into authoritative evidence."""
    from agent_monitor import lean_broker

    if not isinstance(check, dict):
        return None
    exit_code = check.get("exit_code")
    source_sha256 = str(check.get("source_sha256") or "").strip().lower()
    toolchain = str(check.get("toolchain") or "").strip()
    sandbox = str(check.get("sandbox") or "").strip()
    release_id = str(check.get("release_id") or "").strip().lower()
    toolchain_tree = str(
        check.get("toolchain_tree_sha256") or ""
    ).strip().lower()
    mathlib_tree = str(
        check.get("mathlib_tree_sha256") or ""
    ).strip().lower()
    checker_profile = str(check.get("checker_profile") or "").strip()
    try:
        bound_release = lean_broker._validate_release_identity(check)
    except lean_broker.LeanBrokerError:
        bound_release = None
    if (
        check.get("ok") is not True
        or check.get("status") != "verified"
        or isinstance(exit_code, bool)
        or exit_code != 0
        or sandbox not in LEAN_CHECK_TRUSTED_SANDBOXES
        or toolchain != lean_broker.EXPECTED_TOOLCHAIN
        or checker_profile not in {"core", "mathlib"}
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", release_id)
        or not re.fullmatch(r"[0-9a-f]{64}", toolchain_tree)
        or not re.fullmatch(r"[0-9a-f]{64}", mathlib_tree)
        or bound_release != (release_id, toolchain_tree, mathlib_tree)
    ):
        return None
    return {
        "ok": True,
        "status": "verified",
        "exit_code": 0,
        "source_sha256": source_sha256,
        "toolchain": toolchain[:300],
        "sandbox": sandbox,
        "release_id": release_id,
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
        "checker_profile": checker_profile,
    }


def _persist(
    workspace: Path,
    *,
    lean_src: str,
    status: str,
    uses_mathlib: bool,
    attempts: list[dict[str, Any]],
    notes: str = "",
    title: str = "",
    model: str | None = None,
    source: str = "",
    action: str = "",
    chat: list[dict[str, Any]] | None = None,
    citations: list[dict[str, str]] | None = None,
    audit: dict[str, Any] | None = None,
    authoritative_check: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prev = load_cached(workspace) or {}
    kernel_receipt = _trusted_kernel_receipt(authoritative_check)
    verified_source_sha256 = ""
    if status in {"verified", "unfaithful"} and kernel_receipt is not None:
        try:
            _current_dir, current_bytes = _lean_source_snapshot(
                lean_dir(Path(workspace).resolve())
            )
        except (OSError, ValueError):
            current_bytes = b""
        current_sha256 = hashlib.sha256(current_bytes).hexdigest()
        current_source = current_bytes.decode("utf-8", errors="replace")
        source_matches = current_source in {lean_src, lean_src.rstrip() + "\n"}
        if (
            kernel_receipt["source_sha256"] == current_sha256
            and source_matches
        ):
            verified_source_sha256 = current_sha256
    if status in {"verified", "unfaithful"} and not verified_source_sha256:
        status = "failed"
        kernel_receipt = None
        authority_note = (
            "Authoritative Lean status was rejected because no fresh trusted "
            "sandbox receipt matched the current Proof.lean."
        )
        notes = f"{notes} {authority_note}".strip()
    if status not in {"verified", "unfaithful", "incomplete", "compiled"}:
        kernel_receipt = None
    log = ""
    if attempts:
        log = (attempts[-1].get("stderr_tail") or "") + (
            ("\n" + attempts[-1]["stdout_tail"]) if attempts[-1].get("stdout_tail") else ""
        )
    cites = _merge_citations(
        _anchor_citations(citations if citations is not None else [], lean_src),
        _citations_from_lean(lean_src),
        _anchor_citations(_normalize_citations(prev.get("citations")), lean_src),
        _citations_from_proof_md(workspace),
    )
    flags = _fidelity_flags(lean_src)
    if audit is None:
        prior_audit = (prev.get("fidelity") or {}).get("audit")
        # A stale audit describes different source, so only carry it over when the
        # Lean text is unchanged.
        if prior_audit and (prev.get("lean") or "") == lean_src:
            audit = prior_audit
    fidelity = {
        "severity": _fidelity_severity(flags, audit),
        "flags": flags,
        "audit": audit or {},
        "declarations": [
            {k: d[k] for k in ("kind", "name", "line")} for d in _decls_with_docs(lean_src)
        ][:60],
    }
    toolchain_record = dict(toolchain_status())
    if kernel_receipt is not None:
        toolchain_record.update({
            "available": True,
            "version": kernel_receipt["toolchain"],
            "checker_sandbox": kernel_receipt["sandbox"],
            "release_id": kernel_receipt["release_id"],
            "toolchain_tree_sha256": kernel_receipt[
                "toolchain_tree_sha256"
            ],
            "mathlib_tree_sha256": kernel_receipt[
                "mathlib_tree_sha256"
            ],
            "checker_profile": kernel_receipt["checker_profile"],
        })
    # _write_and_run emits this canonical text to Proof.lean. Bind the cached
    # verification and authorship to the same bytes, including the final LF.
    lean_src = lean_src.rstrip() + "\n"
    # Keep authorship tied to the exact generated source. Checking or auditing a
    # hand-edited file must not inherit the previous model's authorship, and a
    # caller-selected model name is never evidence of what actually ran.
    source_digest = hashlib.sha256(lean_src.encode()).hexdigest()
    provenance = prev.get("source_provenance") or {}
    if action in {"verify", "revise"}:
        observed = getattr(model, "observed_model", "")
        provenance = (
            {
                "model": observed,
                "sha256": source_digest,
                "evidence": "provider_response",
            }
            if observed
            else {}
        )
    elif provenance.get("sha256") != source_digest or action == "harness":
        provenance = {}
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model or prev.get("model"),
        "source_provenance": provenance,
        "source": source or prev.get("source") or "",
        "title": title or prev.get("title") or "",
        "notes": _sanitize_formalization_notes(notes),
        "status": status,
        "action": action,
        "uses_mathlib": uses_mathlib,
        "sorry_count": _count_sorry(lean_src),
        "sorry_lines": _sorry_lines(lean_src)[:20],
        "lean_path": f"{LEAN_DIRNAME}/{PROOF_FILENAME}",
        "lean": lean_src,
        "verified_source_sha256": verified_source_sha256 or None,
        "kernel_receipt": kernel_receipt,
        "citations": cites,
        "fidelity": fidelity,
        "attempts": attempts,
        "toolchain": toolchain_record,
        "log": log,
        "chat": chat if chat is not None else list(prev.get("chat") or []),
    }
    if extra:
        result.update(extra)
    result["status"] = status
    result["verified_source_sha256"] = verified_source_sha256 or None
    result["kernel_receipt"] = kernel_receipt
    result["toolchain"] = toolchain_record
    _write_cached(workspace, result)
    return result


def _regular_root_lean_artifact(
    workspace: Path,
) -> tuple[str | None, dict[str, Any] | None, str]:
    """Read a bounded artifact through the same no-follow fd gate as checks."""
    root = Path(workspace).resolve()
    try:
        _resolved, raw = _lean_source_snapshot(root / LEAN_DIRNAME)
    except FileNotFoundError:
        return None, None, "did not create lean/Proof.lean"
    except (OSError, ValueError) as exc:
        return None, None, f"could not safely read lean/Proof.lean: {exc}"
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "lean/Proof.lean is not valid UTF-8"
    return (
        source,
        {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)},
        "",
    )


def _write_lean_source(ldir: Path, source: str) -> tuple[Path, bytes]:
    """Atomically install Proof.lean without following a target symlink."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError("no-follow file-descriptor writes are unavailable")
    payload = (source.rstrip() + "\n").encode("utf-8")
    if len(payload) > LEAN_CHECK_MAX_SOURCE_BYTES:
        raise ValueError("Proof.lean exceeds the source-size limit")

    requested = Path(ldir).absolute()
    resolved = requested.resolve(strict=True)
    if resolved != requested:
        raise ValueError("lean/ or one of its parents is a symbolic link")
    directory_descriptor = os.open(
        requested,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    temporary_name = f".Proof.{secrets.token_hex(12)}.tmp"
    source_descriptor = -1
    try:
        source_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(source_descriptor, "wb") as stream:
            source_descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary_name,
            PROOF_FILENAME,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
    finally:
        if source_descriptor >= 0:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        os.close(directory_descriptor)
    return resolved, payload


def _write_and_run(workspace: Path, lean_src: str, uses_mathlib: bool) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    with _formal_io_lock(workspace):
        lean_src = _normalize_lean_imports(lean_src)
        ldir = lean_dir(workspace)
        if ldir.is_symlink():
            raise ValueError("Refusing to write through symbolic-link lean directory")
        _write_scaffold(ldir, uses_mathlib=uses_mathlib)
        ldir, expected_source = _write_lean_source(ldir, lean_src)
        expected_sha256 = hashlib.sha256(expected_source).hexdigest()
        _installed_dir, installed_source = _lean_source_snapshot(ldir)
        if installed_source != expected_source:
            raise ValueError("Proof.lean changed immediately after its atomic write")

        check = dict(_run_lean_check(ldir, uses_mathlib=uses_mathlib))
        try:
            _observed_dir, observed_source = _lean_source_snapshot(ldir)
            source_unchanged = observed_source == expected_source
        except (OSError, ValueError):
            source_unchanged = False
        checked_sha256 = str(check.get("source_sha256") or "").strip().lower()
        receipt_matches = checked_sha256 == expected_sha256
        failures: list[str] = []
        if not source_unchanged:
            failures.append("Proof.lean changed while Lean was checking it")
        if check.get("ok") and not receipt_matches:
            failures.append("Lean checker did not bind the expected source hash")
        if failures:
            check.update({"ok": False, "status": "failed"})
            check["stderr"] = (
                str(check.get("stderr") or "") + "\n" + "\n".join(failures)
            ).strip()
        check["source_sha256"] = expected_sha256
        return check


@_serialized_formal_operation("check")
def compile_or_check(
    *,
    workspace: Path,
    lean: str | None = None,
    mode: str = "check",
) -> dict[str, Any]:
    """Run Lean on the current (or provided) source without calling an LLM.

    ``mode`` is ``compile`` (exit-code only) or ``check`` (sorry-aware).
    """
    if mode not in {"compile", "check"}:
        raise ValueError(f"Unknown mode: {mode}")
    lean_src = (lean if lean is not None else read_source(workspace)).strip()
    if not lean_src:
        raise ValueError("No Proof.lean yet — press Verify first, or paste Lean source")
    uses_mathlib = _detect_mathlib(lean_src)
    check = _write_and_run(workspace, lean_src, uses_mathlib)
    sorry_n = _count_sorry(lean_src)
    status = _status_from_check(check, sorry_n, mode=mode)
    attempt = {
        "round": 0,
        "uses_mathlib": uses_mathlib,
        "sorry_count": sorry_n,
        "action": mode,
        "check": {
            k: check[k]
            for k in (
                "ok",
                "status",
                "exit_code",
                "duration_s",
                "command",
                "toolchain",
                "sandbox",
                "source_sha256",
                "checker_profile",
                "release_id",
                "toolchain_tree_sha256",
                "mathlib_tree_sha256",
            )
            if k in check
        },
        "stderr_tail": (check.get("stderr") or "")[-2000:],
        "stdout_tail": (check.get("stdout") or "")[-1000:],
    }
    prev = load_cached(workspace) or {}
    # Preserve prior attempt history so the UI can show compile/check rounds.
    prior = list(prev.get("attempts") or [])
    # Renumber so the new action is the latest round.
    attempt["round"] = len(prior)
    flags = _fidelity_flags(lean_src)
    prior_audit = (prev.get("fidelity") or {}).get("audit")
    if not prior_audit or (prev.get("lean") or "") != lean_src:
        prior_audit = None
    if mode == "check":
        status = _apply_fidelity(status, flags, prior_audit)
    notes = _check_summary(lean_src, check, status)
    fid_note = _fidelity_summary(flags, prior_audit)
    if fid_note and mode == "check":
        notes = f"{notes} {fid_note}".strip()
    return _persist(
        workspace,
        lean_src=lean_src,
        status=status,
        uses_mathlib=uses_mathlib,
        attempts=prior + [attempt],
        notes=notes,
        title=str(prev.get("title") or ""),
        model=prev.get("model"),
        source=str(prev.get("source") or "editor"),
        action=mode,
        authoritative_check=check,
    )


_EDIT_REQUEST_RE = re.compile(
    r"\b(change|replace|fix|revise|rewrite|rename|switch|remove|add|modify|edit|"
    r"discharge|refactor|simplify|generalize|generalise|use|try|make|prove)\b",
    re.I,
)
_QUESTION_RE = re.compile(
    r"\b(what|why|how|when|where|which|who|is|are|was|were|do|does|did|"
    r"can|could|would|should|has|have|explain|status|proved|verified|complete)\b",
    re.I,
)


def _feedback_is_question(feedback: str) -> bool:
    text = (feedback or "").strip()
    if not text:
        return False
    if _EDIT_REQUEST_RE.search(text):
        return False
    return text == "?" or "?" in text or bool(_QUESTION_RE.search(text))


def _status_question(feedback: str) -> bool:
    text = (feedback or "").strip().lower()
    return text in {"?", "status", "done?"} or bool(
        re.search(r"\b(all|fully|complete|completed|done|proved|proven|verified|pass|sorry|admit)\b", text)
    )


def _verification_status_answer(cached: dict[str, Any], *, source_changed: bool = False) -> str:
    status = str(cached.get("status") or "none")
    sorry_count = int(cached.get("sorry_count") or 0)
    fidelity = cached.get("fidelity") if isinstance(cached.get("fidelity"), dict) else {}
    audit = fidelity.get("audit") if isinstance(fidelity.get("audit"), dict) else {}
    severity = str(fidelity.get("severity") or "ok")
    if source_changed:
        return (
            "Not yet. The current editor/source differs from the last checked Proof.lean, so the "
            f"cached `{status}` result applies only to the previous version. Run Check, then Audit, "
            "before treating the edited proof as verified."
        )
    if status == "unfaithful" or severity == "major" or (audit.get("ok") and audit.get("faithful") is False):
        issues = audit.get("issues") or [flag.get("message") for flag in fidelity.get("flags") or []]
        detail = "; ".join(str(item) for item in issues[:3] if item)
        return (
            "No—not yet. Lean may accept the file, but the statement-fidelity check found that "
            "the encoded theorem does not faithfully establish the original problem."
            + (f" Main issue: {detail}" if detail else "")
        )
    if status == "verified" and sorry_count == 0:
        if audit.get("ok") and audit.get("faithful") is True:
            return (
                "Yes, with the usual formal-verification scope: Lean's kernel accepted Proof.lean "
                "with no sorry/admit, and the statement-fidelity audit judged its main declaration "
                "faithful to the original problem."
            )
        return (
            "Lean's kernel accepted Proof.lean with no sorry/admit, so the theorem currently written "
            "in that file is proved. However, the statement-fidelity audit has not successfully "
            "confirmed that this theorem exactly matches the original mathematical problem. Run "
            "Audit before treating the original problem as fully formalized; a compiling theorem can "
            "still be weaker, specialized, or vacuous."
        )
    if status == "incomplete" or sorry_count:
        return f"No. The Lean file still contains {sorry_count} sorry/admit gap(s), so it is not a complete proof."
    if status in {"failed", "toolchain_missing"}:
        return "No. The current Lean source has not passed the kernel checker; inspect the latest checker log first."
    if status == "running":
        return "Not yet—the Lean harness is still running. I can assess completeness after the checker finishes."
    return "There is no completed Lean verification for this run yet. Run Verify or Harness first."


def _intervention_lock(workspace: Path) -> threading.Lock:
    key = str(workspace.resolve())
    with _INTERVENTION_LOCKS_GUARD:
        return _INTERVENTION_LOCKS.setdefault(key, threading.Lock())


def _codex_subscription_interaction(
    *,
    workspace: Path,
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None = None,
    prompt: str,
    lean_source: str,
    edit: bool,
) -> dict[str, Any]:
    """Run the user's OAuth Codex in a disposable copy of lean/.

    The real workspace is never exposed to an unsandboxed intervention call;
    edited source is copied back only after this function returns and the
    normal checker path validates it.
    """
    from agent_monitor import codex_login
    from agent_monitor.cli_events import CLIEventParser
    from agent_monitor.engines_registry import build_cli_command
    from agent_monitor.jobs import _route_from_record
    from agent_monitor.settings import codex_enabled

    recorded_route = _route_from_record(run_record)
    if recorded_route is not None and recorded_route != "codex_subscription":
        raise ValueError(
            f"This run uses {recorded_route.replace('_', ' ')}; "
            "Formal Lean cannot silently fall back to Codex"
        )
    owner_id = (user or {}).get("id") or (run_record or {}).get("owner_id")
    if recorded_route is None and not codex_enabled(user):
        raise ValueError(
            "Codex is turned off in Settings; configure an API provider for Lean assistance"
        )
    if owner_id is None or not codex_login.account_login_ready(
        codex_login.account_home(int(owner_id))
    ):
        raise ValueError(
            "No API key is configured and this account's Codex subscription is not connected"
        )

    timeout_s = int(os.environ.get("AGENT_MONITOR_LEAN_INTERVENTION_TIMEOUT", "300"))
    with tempfile.TemporaryDirectory(prefix="lean-intervention-") as tmp:
        isolated = Path(tmp)
        for name in ("lean-toolchain", "lakefile.lean", "lake-manifest.json"):
            scaffold = _read_optional_workspace_text(
                workspace,
                f"{LEAN_DIRNAME}/{name}",
                limit=MAX_WORKSPACE_SOURCE_BYTES,
            )
            if scaffold is not None:
                (isolated / name).write_text(scaffold, encoding="utf-8")
        (isolated / PROOF_FILENAME).write_text(lean_source.rstrip() + "\n", encoding="utf-8")
        problem, informal = _problem_and_proof(workspace, run_record)
        _write_task_file(isolated, problem=problem, proof=informal, existing=lean_source)
        task_prompt = prompt + (
            "\n\nWork only in this disposable directory. Revise Proof.lean directly, run Lean to "
            "check it, and finish with a concise summary of what changed."
            if edit
            else "\n\nRead TASK.md and Proof.lean, answer in plain text, and do not edit any files."
        )
        argv = build_cli_command(
            "codex", prompt=task_prompt, workspace=isolated, problem_file=isolated / "TASK.md"
        )
        if not argv:
            raise ValueError("Codex CLI is not installed on this server")
        try:
            proc = subprocess.run(
                argv,
                cwd=str(isolated),
                env=_harness_env(
                    "codex",
                    user,
                    run_record=run_record,
                    auth_route="codex_subscription" if recorded_route else None,
                ),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"Codex intervention timed out after {timeout_s}s") from exc
        parser = CLIEventParser("codex")
        parser.feed((proc.stdout or "") + "\n" + (proc.stderr or ""))
        answer = parser.final_message().strip()
        if proc.returncode != 0:
            detail = (parser.output() or proc.stderr or proc.stdout or "Codex failed")[-1200:]
            raise ValueError(f"Codex intervention failed (exit {proc.returncode}): {detail}")
        revised = (
            _read_optional_workspace_text(
                isolated,
                PROOF_FILENAME,
                limit=LEAN_CHECK_MAX_SOURCE_BYTES,
            )
            or ""
        ).strip()
        if not revised:
            raise ValueError(
                "Codex completed without a safe regular Proof.lean within the size limit"
            )
        if edit and revised == lean_source.strip():
            raise ValueError("Codex completed without updating Proof.lean")
        if not answer:
            answer = "Codex completed the requested Lean intervention."
        return {
            "answer": answer[:6000],
            "lean": revised,
            "model": str(parser.usage.get("model") or "Codex subscription"),
        }


def _answer_lean_question(
    *,
    workspace: Path,
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None = None,
    question: str,
    model: str | None,
    lean_src: str,
    cached: dict[str, Any],
    source_changed: bool = False,
) -> tuple[str, str]:
    if _status_question(question):
        return "verification status", _verification_status_answer(cached, source_changed=source_changed)
    fidelity = cached.get("fidelity") if isinstance(cached.get("fidelity"), dict) else {}
    prompt = (
        QUESTION_PROMPT.replace("{status}", str(cached.get("status") or "none") + (" (current source differs from the checked snapshot)" if source_changed else ""))
        .replace("{fidelity}", json.dumps(fidelity, ensure_ascii=False)[:6000])
        .replace("{lean}", lean_src[:40000])
        .replace("{question}", question[:4000])
    )
    effective_model = model or cached.get("model")
    env = _llm_environment(user, run_record)
    try:
        chosen, answer = _chat(env, effective_model, prompt)
        if not answer.strip():
            raise ValueError("Lean assistant returned an empty answer")
        return chosen, answer.strip()[:6000]
    except ValueError:
        if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
            raise
        from agent_monitor.jobs import _is_kimi_model

        if _is_kimi_model(effective_model):
            raise
        result = _codex_subscription_interaction(
            workspace=workspace,
            user=user,
            run_record=run_record,
            prompt=prompt,
            lean_source=lean_src,
            edit=False,
        )
        return str(result["model"]), str(result["answer"])


def _revise_with_feedback_impl(
    *,
    workspace: Path,
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None = None,
    message: str,
    model: str | None = None,
    lean: str | None = None,
    max_repairs: int = 1,
) -> dict[str, Any]:
    """Human-in-the-loop: revise Proof.lean from feedback, then re-check."""
    feedback = (message or "").strip()
    if not feedback:
        raise ValueError("Feedback is empty")
    lean_src = (lean if lean is not None else read_source(workspace)).strip()
    if not lean_src:
        raise ValueError("No Proof.lean yet — press Verify first")

    _append_chat(workspace, "user", feedback)
    prev = load_cached(workspace) or {}
    checked_source = str(prev.get("lean") or "").strip()
    source_changed = bool(checked_source and checked_source != lean_src)
    if _feedback_is_question(feedback):
        chosen_model, answer = _answer_lean_question(
            workspace=workspace,
            user=user,
            run_record=run_record,
            question=feedback,
            model=model,
            lean_src=lean_src,
            cached=prev,
            source_changed=source_changed,
        )
        chat = _append_chat(workspace, "assistant", answer)
        result = load_cached(workspace) or prev
        result["chat"] = chat
        result["interaction"] = {"kind": "question", "model": chosen_model}
        return result

    err_blob = str(prev.get("log") or "")[-6000:]
    env = _llm_environment(user, run_record)
    prompt = (
        FEEDBACK_PROMPT.replace("{lean}", lean_src[:40000])
        .replace("{errors}", err_blob or "(no prior compiler log)")
        .replace("{feedback}", feedback[:4000])
    )
    codex_mode = False
    effective_model = model or prev.get("model")
    try:
        chosen_model, content = _chat(env, effective_model, prompt)
        payload = _parse_model_payload(content)
        lean_src = payload["lean"]
        uses_mathlib = bool(payload["uses_mathlib"]) or _detect_mathlib(lean_src)
        notes = payload.get("notes") or ""
        citations = list(payload.get("citations") or [])
    except ValueError:
        from agent_monitor.jobs import _is_kimi_model

        if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
            raise
        if _is_kimi_model(effective_model):
            raise
        codex_result = _codex_subscription_interaction(
            workspace=workspace,
            user=user,
            run_record=run_record,
            prompt=prompt,
            lean_source=lean_src,
            edit=True,
        )
        codex_mode = True
        chosen_model = str(codex_result["model"])
        lean_src = str(codex_result["lean"])
        uses_mathlib = _detect_mathlib(lean_src)
        notes = str(codex_result["answer"])
        citations = _citations_from_lean(lean_src)

    attempts: list[dict[str, Any]] = list(prev.get("attempts") or [])
    for round_i in range(max_repairs + 1):
        check = _write_and_run(workspace, lean_src, uses_mathlib)
        sorry_n = _count_sorry(lean_src)
        status = _status_from_check(check, sorry_n, mode="check")
        attempts.append(
            {
                "round": len(attempts),
                "uses_mathlib": uses_mathlib,
                "sorry_count": sorry_n,
                "action": "revise",
                "check": {
                    k: check[k]
                    for k in (
                        "ok",
                        "status",
                        "exit_code",
                        "duration_s",
                        "command",
                        "toolchain",
                        "sandbox",
                        "source_sha256",
                        "checker_profile",
                        "release_id",
                        "toolchain_tree_sha256",
                        "mathlib_tree_sha256",
                    )
                    if k in check
                },
                "stderr_tail": (check.get("stderr") or "")[-2000:],
                "stdout_tail": (check.get("stdout") or "")[-1000:],
            }
        )
        if status in {"verified", "incomplete", "toolchain_missing"}:
            break
        if round_i >= max_repairs:
            break
        err_blob = ((check.get("stderr") or "") + "\n" + (check.get("stdout") or ""))[-6000:]
        repair_prompt = REPAIR_PROMPT.replace("{lean}", lean_src[:40000]).replace(
            "{errors}", err_blob
        )
        if codex_mode:
            codex_result = _codex_subscription_interaction(
                workspace=workspace,
                user=user,
                run_record=run_record,
                prompt=repair_prompt,
                lean_source=lean_src,
                edit=True,
            )
            lean_src = str(codex_result["lean"])
            uses_mathlib = _detect_mathlib(lean_src)
            notes = str(codex_result["answer"] or notes)
            citations = _citations_from_lean(lean_src)
        else:
            _, repair_content = _chat(env, chosen_model, repair_prompt)
            repaired = _parse_model_payload(repair_content)
            lean_src = repaired["lean"]
            uses_mathlib = bool(repaired["uses_mathlib"]) or uses_mathlib
            if repaired.get("notes"):
                notes = repaired["notes"]
            if repaired.get("citations"):
                citations = list(repaired["citations"])

    audit_result: dict[str, Any] | None = None
    if status == "verified":
        problem, informal = _problem_and_proof(workspace, run_record)
        audit_result = _audit_faithfulness(
            env, model or chosen_model, problem=problem, proof=informal, lean=lean_src
        )
        status = _apply_fidelity(status, _fidelity_flags(lean_src), audit_result)
    if not notes:
        notes = _check_summary(lean_src, attempts[-1].get("check") or {}, status)
    fid_note = _fidelity_summary(_fidelity_flags(lean_src), audit_result)
    if fid_note:
        notes = f"{notes} {fid_note}".strip()
    assistant_msg = notes or f"Revised Lean formalisation → {status}."
    chat = _append_chat(workspace, "assistant", assistant_msg)
    return _persist(
        workspace,
        lean_src=lean_src,
        status=status,
        uses_mathlib=uses_mathlib,
        attempts=attempts,
        notes=notes,
        title=str(prev.get("title") or ""),
        model=chosen_model,
        source=str(prev.get("source") or "human feedback"),
        action="revise",
        chat=chat,
        citations=citations,
        audit=audit_result,
        authoritative_check=check,
    )


@_serialized_formal_operation("revise")
def revise_with_feedback(
    *,
    workspace: Path,
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None = None,
    message: str,
    model: str | None = None,
    lean: str | None = None,
    max_repairs: int = 1,
) -> dict[str, Any]:
    """Serialize a run's interventions and guarantee visible failure feedback."""
    lock = _intervention_lock(workspace)
    if not lock.acquire(blocking=False):
        raise ValueError("Another Lean intervention is already running for this proof")
    try:
        if harness_job(workspace):
            raise ValueError("The Lean harness is still running; wait for it to finish or stop it first")
        try:
            return _revise_with_feedback_impl(
                workspace=workspace,
                user=user,
                run_record=run_record,
                message=message,
                model=model,
                lean=lean,
                max_repairs=max_repairs,
            )
        except Exception as exc:
            cached = load_cached(workspace) or {}
            chat = list(cached.get("chat") or [])
            if chat and chat[-1].get("role") == "user":
                reason = str(exc).strip() or exc.__class__.__name__
                _append_chat(
                    workspace,
                    "assistant",
                    "I couldn't complete that Lean intervention. "
                    + reason[:1200]
                    + " The current Proof.lean remains available; review it before retrying.",
                )
            raise
    finally:
        lock.release()


@_serialized_formal_operation("generate")
def generate(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None = None,
    max_repairs: int = 2,
    audit: bool = True,
    guidance: str | None = None,
) -> dict[str, Any]:
    """Translate the run's proof into Lean and attempt to verify it.

    A clean compile is not the end of it: with ``audit`` set, a second pass asks
    whether the Lean statement really is the problem's claim, and one
    statement-repair round follows when it isn't. Proving the wrong theorem
    convincingly is the failure mode this guards against.
    """
    from agent_monitor.settings import resolved_user_env

    problem = (
        _read_optional_workspace_text(
            workspace,
            "problem.txt",
            limit=MAX_WORKSPACE_PROBLEM_BYTES,
        )
        or ""
    ).strip()

    source, proof = _proof_source(workspace, run_record)
    if not proof:
        raise ValueError("No proof text yet — wait for the run to produce proof.md")

    env = _llm_environment(user, run_record)
    prompt = TRANSLATE_PROMPT.replace("{problem}", problem[:6000]).replace(
        "{proof}", proof[:50000]
    )
    if guidance:
        prompt += "\n\n" + str(guidance)[:16000]
    chosen_model, content = _chat(env, model, prompt)
    payload = _parse_model_payload(content)

    attempts: list[dict[str, Any]] = []
    lean_src = payload["lean"]
    uses_mathlib = bool(payload["uses_mathlib"])
    notes = payload.get("notes") or ""
    title = payload.get("title") or ""
    citations = list(payload.get("citations") or [])
    status = "failed"

    for round_i in range(max_repairs + 1):
        check = _write_and_run(workspace, lean_src, uses_mathlib)
        sorry_n = _count_sorry(lean_src)
        status = _status_from_check(check, sorry_n, mode="check")
        attempts.append(
            {
                "round": round_i,
                "uses_mathlib": uses_mathlib,
                "sorry_count": sorry_n,
                "action": "verify",
                "check": {
                    k: check[k]
                    for k in (
                        "ok",
                        "status",
                        "exit_code",
                        "duration_s",
                        "command",
                        "toolchain",
                        "sandbox",
                        "source_sha256",
                        "checker_profile",
                        "release_id",
                        "toolchain_tree_sha256",
                        "mathlib_tree_sha256",
                    )
                    if k in check
                },
                "stderr_tail": (check.get("stderr") or "")[-2000:],
                "stdout_tail": (check.get("stdout") or "")[-1000:],
            }
        )

        if status in {"verified", "incomplete", "toolchain_missing"}:
            break
        if round_i >= max_repairs:
            status = "failed"
            break
        err_blob = ((check.get("stderr") or "") + "\n" + (check.get("stdout") or ""))[-6000:]
        repair_prompt = REPAIR_PROMPT.replace("{lean}", lean_src[:40000]).replace(
            "{errors}", err_blob
        )
        _, repair_content = _chat(env, chosen_model, repair_prompt)
        repaired = _parse_model_payload(repair_content)
        lean_src = repaired["lean"]
        uses_mathlib = bool(repaired["uses_mathlib"]) or uses_mathlib
        if repaired.get("notes"):
            notes = repaired["notes"]
        if repaired.get("citations"):
            citations = list(repaired["citations"])

    audit_result: dict[str, Any] | None = None
    if audit and status in {"verified", "incomplete"}:
        audit_result = _audit_faithfulness(
            env, model or chosen_model, problem=problem, proof=proof, lean=lean_src
        )
        needs_fix = _fidelity_severity(_fidelity_flags(lean_src), audit_result) == "major"
        if needs_fix and status == "verified":
            fixed = _repair_statement(
                env,
                chosen_model,
                problem=problem,
                lean=lean_src,
                audit=audit_result,
                flags=_fidelity_flags(lean_src),
            )
            if fixed:
                lean_src = fixed["lean"]
                uses_mathlib = bool(fixed["uses_mathlib"]) or uses_mathlib
                if fixed.get("notes"):
                    notes = fixed["notes"]
                if fixed.get("citations"):
                    citations = list(fixed["citations"])
                check = _write_and_run(workspace, lean_src, uses_mathlib)
                sorry_n = _count_sorry(lean_src)
                status = _status_from_check(check, sorry_n, mode="check")
                attempts.append(
                    {
                        "round": len(attempts),
                        "uses_mathlib": uses_mathlib,
                        "sorry_count": sorry_n,
                        "action": "statement-fix",
                        "check": {
                            k: check[k]
                            for k in (
                                "ok",
                                "status",
                                "exit_code",
                                "duration_s",
                                "command",
                                "toolchain",
                                "sandbox",
                                "source_sha256",
                                "checker_profile",
                                "release_id",
                                "toolchain_tree_sha256",
                                "mathlib_tree_sha256",
                            )
                            if k in check
                        },
                        "stderr_tail": (check.get("stderr") or "")[-2000:],
                        "stdout_tail": (check.get("stdout") or "")[-1000:],
                    }
                )
                if status == "failed" and max_repairs > 0:
                    err_blob = (
                        (check.get("stderr") or "") + "\n" + (check.get("stdout") or "")
                    )[-6000:]
                    repair_prompt = REPAIR_PROMPT.replace("{lean}", lean_src[:40000]).replace(
                        "{errors}", err_blob
                    )
                    _, repair_content = _chat(env, chosen_model, repair_prompt)
                    repaired = _parse_model_payload(repair_content)
                    lean_src = repaired["lean"]
                    uses_mathlib = bool(repaired["uses_mathlib"]) or uses_mathlib
                    if repaired.get("notes"):
                        notes = repaired["notes"]
                    if repaired.get("citations"):
                        citations = list(repaired["citations"])
                    check = _write_and_run(workspace, lean_src, uses_mathlib)
                    sorry_n = _count_sorry(lean_src)
                    status = _status_from_check(check, sorry_n, mode="check")
                    attempts.append(
                        {
                            "round": len(attempts),
                            "uses_mathlib": uses_mathlib,
                            "sorry_count": sorry_n,
                            "action": "statement-repair",
                            "check": {
                                k: check[k]
                                for k in (
                                    "ok",
                                    "status",
                                    "exit_code",
                                    "duration_s",
                                    "command",
                                    "toolchain",
                                    "sandbox",
                                    "source_sha256",
                                    "checker_profile",
                                    "release_id",
                                    "toolchain_tree_sha256",
                                    "mathlib_tree_sha256",
                                )
                                if k in check
                            },
                            "stderr_tail": (check.get("stderr") or "")[-2000:],
                            "stdout_tail": (check.get("stdout") or "")[-1000:],
                        }
                    )
                if status in {"verified", "incomplete"}:
                    audit_result = _audit_faithfulness(
                        env,
                        model or chosen_model,
                        problem=problem,
                        proof=proof,
                        lean=lean_src,
                    )
        status = _apply_fidelity(status, _fidelity_flags(lean_src), audit_result)

    if not notes:
        notes = _check_summary(lean_src, attempts[-1].get("check") or {}, status)
    fid_note = _fidelity_summary(_fidelity_flags(lean_src), audit_result)
    if fid_note:
        notes = f"{notes} {fid_note}".strip()
    prev_chat = list((load_cached(workspace) or {}).get("chat") or [])
    return _persist(
        workspace,
        lean_src=lean_src,
        status=status,
        uses_mathlib=uses_mathlib,
        attempts=attempts,
        notes=notes,
        title=title,
        model=chosen_model,
        source=source,
        action="verify",
        chat=prev_chat,
        citations=citations,
        audit=audit_result,
        authoritative_check=check,
    )


def _repair_statement(
    env: dict[str, str],
    model: str | None,
    *,
    problem: str,
    lean: str,
    audit: dict[str, Any] | None,
    flags: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """One round of "the statement is wrong, restate and reprove"."""
    findings: list[str] = []
    if audit and audit.get("verdict"):
        findings.append(f"Verdict: {audit['verdict']}")
    findings += [f"- {i}" for i in (audit or {}).get("issues") or []]
    if audit and audit.get("statement_suggestion"):
        findings.append(f"Suggested faithful statement: {audit['statement_suggestion']}")
    findings += [
        f"- {f['message']}" + (f" (line {', '.join(map(str, f['lines']))})" if f.get("lines") else "")
        for f in flags
        if f.get("severity") == "major"
    ]
    if not findings:
        return None
    prompt = (
        STATEMENT_FIX_PROMPT.replace("{problem}", (problem or "(not recorded)")[:6000])
        .replace("{audit}", "\n".join(findings)[:4000])
        .replace("{lean}", lean[:40000])
    )
    try:
        _, content = _chat(env, model, prompt)
        return _parse_model_payload(content)
    except ValueError:
        # The statement stays as-is; the audit findings still reach the UI.
        return None


@_serialized_formal_operation("audit")
def audit_current(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None = None,
    user: dict[str, Any] | None = None,
    model: str | None = None,
    lean: str | None = None,
) -> dict[str, Any]:
    """Audit the current source for statement fidelity without touching it."""
    from agent_monitor.settings import resolved_user_env

    lean_src = (lean if lean is not None else read_source(workspace)).strip()
    if not lean_src:
        raise ValueError("No Proof.lean yet — press Verify or Harness first")
    prev = load_cached(workspace) or {}
    problem, informal = _problem_and_proof(workspace, run_record)
    env = _llm_environment(user, run_record)
    audit = _audit_faithfulness(
        env, model or prev.get("model"), problem=problem, proof=informal, lean=lean_src
    )
    if not audit.get("ok"):
        raise ValueError(audit.get("error") or "audit failed")
    flags = _fidelity_flags(lean_src)
    status = str(prev.get("status") or "none")
    attempts = list(prev.get("attempts") or [])
    check: dict[str, Any] | None = None
    # A workspace cache is display state, not authority. Re-check before an
    # audit preserves or re-derives any kernel-accepted status.
    if (prev.get("lean") or "") == lean_src and status in {
        "verified",
        "unfaithful",
    }:
        uses_mathlib = _detect_mathlib(lean_src)
        check = _write_and_run(workspace, lean_src, uses_mathlib)
        sorry_n = _count_sorry(lean_src)
        status = _status_from_check(check, sorry_n, mode="check")
        if status in {"verified", "incomplete"}:
            status = _apply_fidelity(status, flags, audit)
        attempts.append(
            {
                "round": len(attempts),
                "uses_mathlib": uses_mathlib,
                "sorry_count": sorry_n,
                "action": "audit-recheck",
                "check": {
                    key: check[key]
                    for key in (
                        "ok",
                        "status",
                        "exit_code",
                        "duration_s",
                        "command",
                        "toolchain",
                        "sandbox",
                        "source_sha256",
                        "checker_profile",
                        "release_id",
                        "toolchain_tree_sha256",
                        "mathlib_tree_sha256",
                    )
                    if key in check
                },
                "stderr_tail": (check.get("stderr") or "")[-2000:],
                "stdout_tail": (check.get("stdout") or "")[-1000:],
            }
        )
    notes = _fidelity_summary(flags, audit) or "Audit found no fidelity issues."
    return _persist(
        workspace,
        lean_src=lean_src,
        status=status,
        uses_mathlib=_detect_mathlib(lean_src),
        attempts=attempts,
        notes=notes,
        title=str(prev.get("title") or ""),
        model=str(audit.get("model") or prev.get("model") or ""),
        source=str(prev.get("source") or ""),
        action="audit",
        audit=audit,
        authoritative_check=check,
    )


# ── Harness mode: let a coding agent iterate on Proof.lean with a real shell ──
#
# The single-shot LLM path guesses whether Lean will accept a file. An agent with
# a shell can just run `lean Proof.lean`, read the errors and try again, which is
# how a human does it. Runs are long, so they go to a background thread and the
# cache file doubles as the progress channel for the UI.

# Agents that can edit files and run a shell. `plain` has no tools and
# `metaharness` drives the whole proof pipeline, so neither belongs here.
_HARNESS_EXCLUDE = {"plain", "metaharness"}
_HARNESS_JOBS: dict[str, dict[str, Any]] = {}
_HARNESS_LOCK = threading.Lock()


def _harness_auth_route(
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None = None,
    *,
    model: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Resolve one immutable route for a manual Formal Lean harness.

    New runs record their route. Legacy runs (and the global status endpoint,
    which has no run) retain the old current-settings inference. A malformed
    explicit route is never treated as legacy because that could redirect a
    subscription run to an available API key.
    """
    from agent_monitor.jobs import _is_kimi_model, _route_from_record
    from agent_monitor.settings import account_runtime, resolved_user_env

    env = dict(resolved_user_env(user) or {})
    route = _route_from_record(run_record)
    record = run_record or {}
    raw_route = str(record.get("auth_route") or "").strip()
    raw_live_mode = str((record.get("live") or {}).get("auth_mode") or "").strip()
    if route is None and (raw_route or raw_live_mode):
        raise ValueError("This run has an invalid recorded authentication route")
    if route is None:
        if _is_kimi_model(model):
            route = "api_key"
        else:
            route = {
                "api": "api_key",
                "codex": "codex_subscription",
                "claude": "claude_subscription",
            }[account_runtime(user, env=env)]
    return route, env


def _harness_route_supported(engine: str, route: str, auth_modes: list[str]) -> bool:
    from agent_monitor.engines_registry import (
        CLAUDE_SUBSCRIPTION_ENGINES,
        CODEX_SUBSCRIPTION_ENGINES,
    )

    if route not in auth_modes:
        return False
    if route == "claude_subscription":
        return engine in CLAUDE_SUBSCRIPTION_ENGINES
    if route == "codex_subscription":
        return engine in CODEX_SUBSCRIPTION_ENGINES
    return route == "api_key"


def _sponsored_formal_proxy_engine(engine: str) -> str:
    """Return the least-privileged sponsored token scope for a Formal harness.

    The Kimi Codex adapter is a local tool runner, but its only remote operation
    is the same bounded transport used by the shared Formal translator. Even
    though the gateway also exposes a separate ``codex`` scope for main proof
    runs, bind this Formal-only path to the narrower ``formal`` scope.
    """
    return "formal" if str(engine).strip().lower() == "codex" else engine


def _recorded_harness_model(run_record: dict[str, Any] | None) -> str:
    record = run_record or {}
    return str(
        record.get("model") or (record.get("live") or {}).get("model") or ""
    ).strip()


def harness_model_options(
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return route-specific Formal Lean models for the selected run."""
    route, _env = _harness_auth_route(user, run_record)
    owner_id = (user or {}).get("id") or (run_record or {}).get("owner_id")
    models: list[str] = []
    preferred = ""
    if route == "claude_subscription":
        from agent_monitor.claude_login import DEFAULT_CLAUDE_MODELS

        models = list(DEFAULT_CLAUDE_MODELS)
    elif route == "codex_subscription" and owner_id is not None:
        from agent_monitor import codex_login

        try:
            home = codex_login.account_home(int(owner_id))
            if codex_login.account_login_ready(home):
                models = list(codex_login.available_models(int(owner_id)))
        except (OSError, TypeError, ValueError):
            models = []
    elif route == "api_key":
        from agent_monitor.settings import get_settings

        api_settings = get_settings(user)
        models = list(api_settings.get("api_models") or [])
        preferred = str(api_settings.get("default_model") or "").strip()
    recorded = _recorded_harness_model(run_record)
    if recorded and recorded in models:
        models = [recorded, *(item for item in models if item != recorded)]
    return {
        "auth_route": route,
        "models": models,
        "selected": (
            recorded if recorded in models
            else preferred if preferred in models
            else models[0] if models else ""
        ),
    }


def harness_engines(
    user: dict[str, Any] | None = None,
    run_record: dict[str, Any] | None = None,
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """CLI agents usable for Lean work on exactly one authentication route."""
    from agent_monitor import claude_login, codex_login
    from agent_monitor.engines_registry import (
        CLI_ENGINES,
        ENGINE_AUTH_MODES,
        FORMAL_LEAN_ENGINES,
        _cli_available,
    )

    route, env = _harness_auth_route(user, run_record, model=model)
    codex_ready = False
    claude_ready = False
    owner_id = (user or {}).get("id") or (run_record or {}).get("owner_id")
    if owner_id is not None:
        if route == "codex_subscription":
            codex_ready = codex_login.account_login_ready(
                codex_login.account_home(int(owner_id))
            )
        elif route == "claude_subscription":
            claude_ready = claude_login.account_login_ready(
                claude_login.account_home(int(owner_id))
            )
    api_requirements = {
        "deepseek_harness": (["DEEPSEEK_API_KEY"], "DeepSeek API key"),
        "codex": (["OPENAI_API_KEY", "KIMI_API_KEY"], "OpenAI API key"),
        "openclaude": (["OPENAI_API_KEY", "KIMI_API_KEY"], "OpenAI API key"),
        "openhands": (
            ["OPENAI_API_KEY", "LLM_API_KEY", "KIMI_API_KEY"],
            "OpenAI-compatible API key",
        ),
        "openclaw": (["OPENAI_API_KEY", "KIMI_API_KEY"], "OpenAI API key"),
        "deepagents": (["OPENAI_API_KEY", "KIMI_API_KEY"], "OpenAI API key"),
    }
    selected_model = str(model or _recorded_harness_model(run_record) or "").lower()
    sponsored_record = (
        route == "api_key"
        and selected_model.startswith("kimi-")
        and str((run_record or {}).get("credential_source") or "").strip()
        == "sponsored_kimi"
    )
    sponsored_available = sponsored_record
    if (
        not sponsored_available
        and run_record is None
        and route == "api_key"
        and selected_model.startswith("kimi-")
        and not env.get("KIMI_API_KEY")
    ):
        from agent_monitor import sponsored_kimi_client

        sponsored_available = sponsored_kimi_client.configured()
    if sponsored_available:
        from agent_monitor import sponsored_kimi_client

    out: list[dict[str, Any]] = []
    for eid, spec in CLI_ENGINES.items():
        if eid in _HARNESS_EXCLUDE:
            continue
        try:
            runtime_ok, _tmpl = _cli_available(spec)
        except Exception:  # noqa: BLE001 — availability probe must never break the UI
            runtime_ok = False
        auth_modes = list(ENGINE_AUTH_MODES.get(eid) or [])
        formal_supported = eid in FORMAL_LEAN_ENGINES
        route_supported = _harness_route_supported(eid, route, auth_modes)
        required, api_label = api_requirements.get(
            eid, (["OPENAI_API_KEY"], "OpenAI API key")
        )
        sponsored_api_ok = sponsored_available and sponsored_kimi_client.supports_engine(
            _sponsored_formal_proxy_engine(eid)
        )
        api_ok = (
            route == "api_key"
            and route_supported
            and (any(env.get(key) for key in required) or sponsored_api_ok)
        )
        codex_ok = (
            route == "codex_subscription" and route_supported and codex_ready
        )
        claude_ok = (
            route == "claude_subscription" and route_supported and claude_ready
        )
        auth_ok = route_supported and (codex_ok or claude_ok or api_ok)
        if not route_supported:
            route_label = {
                "api_key": "the recorded API-key route",
                "codex_subscription": "the recorded Codex subscription route",
                "claude_subscription": "the recorded Claude Code subscription route",
            }[route]
            auth_detail = f"does not support {route_label}"
        elif codex_ok:
            auth_detail = "Codex subscription"
        elif claude_ok:
            auth_detail = "Claude Code subscription"
        elif sponsored_api_ok:
            auth_detail = "Included Kimi K3 · limited proxy"
        elif api_ok:
            non_kimi_keys = [key for key in required if key != "KIMI_API_KEY"]
            auth_detail = (
                "Kimi K3 API key"
                if env.get("KIMI_API_KEY")
                and not any(env.get(key) for key in non_kimi_keys)
                else api_label
            )
        elif route == "claude_subscription":
            auth_detail = (
                "Claude Code is not connected for this account; connect it in Settings"
            )
        elif route == "codex_subscription":
            auth_detail = (
                "Codex is not connected for this account; connect it in Settings"
            )
        elif eid == "deepseek_harness":
            auth_detail = (
                "needs DEEPSEEK_API_KEY; OpenAI/Anthropic keys cannot run native dsh"
            )
        else:
            auth_detail = f"needs {api_label}"
        if not formal_supported:
            auth_detail = (
                "does not yet support a fresh root Proof.lean artifact; "
                "use Verify or choose a Formal Lean-compatible harness; "
                f"authentication: {auth_detail}"
            )
        detail = (
            auth_detail
            if not formal_supported or runtime_ok
            else str(spec.get("install_hint") or "runtime not installed")
        )
        out.append(
            {
                "id": eid,
                "label": spec.get("label") or eid,
                "available": bool(runtime_ok and auth_ok and formal_supported),
                "runtime_available": bool(runtime_ok),
                "auth_ready": bool(auth_ok),
                "formal_lean_supported": formal_supported,
                "auth_modes": auth_modes,
                "auth_route": route,
                "auth_detail": auth_detail,
                "status_detail": detail,
                "install_hint": spec.get("install_hint") or "",
            }
        )
    preferred_id = "claude" if route == "claude_subscription" else "codex"
    out.sort(key=lambda e: (not e["available"], e["id"] != preferred_id, e["id"]))
    return out

def _harness_key(workspace: Path) -> str:
    return _workspace_key(workspace)


def harness_job(workspace: Path) -> dict[str, Any] | None:
    """The live harness job for a workspace, if one is still running."""
    with _HARNESS_LOCK:
        job = _HARNESS_JOBS.get(_harness_key(workspace))
        if not job:
            return None
        thread = job.get("thread")
        if thread is not None and not thread.is_alive():
            finished = _HARNESS_JOBS.pop(_harness_key(workspace), None) or {}
            token = finished.get("operation_token")
            if token is not None:
                _release_formal_operation(workspace, token)
            return None
        return {k: v for k, v in job.items() if k not in {"thread", "proc", "operation_token"}}


def stop_harness(workspace: Path) -> dict[str, Any]:
    with _HARNESS_LOCK:
        job = _HARNESS_JOBS.get(_harness_key(workspace))
        proc = job.get("proc") if job else None
        if job:
            job["stopping"] = True
    if proc is None:
        raise ValueError("No harness run in progress")
    _terminate_process_group(proc)
    return {"stopping": True}


def _write_task_file(ldir: Path, *, problem: str, proof: str, existing: str) -> None:
    body = [
        "# Lean formalisation task",
        "",
        "## Problem",
        "",
        problem or "(problem statement not recorded)",
        "",
        "## Informal proof",
        "",
        proof or "(no informal proof available)",
        "",
        "## Current Proof.lean",
        "",
    ]
    if existing.strip():
        body += ["```lean", existing.strip(), "```"]
    else:
        body += ["(none yet — create it)"]
    (ldir / "TASK.md").write_text("\n".join(body) + "\n", encoding="utf-8")


def _harness_env(
    engine: str,
    user: dict[str, Any] | None,
    *,
    model: str | None = None,
    run_record: dict[str, Any] | None = None,
    auth_route: str | None = None,
) -> dict[str, str]:
    """Build one fail-closed auth environment with Lean available on PATH."""
    from agent_monitor.engines_registry import (
        ENGINE_AUTH_MODES,
        engine_extra_path,
    )
    from agent_monitor.jobs import (
        _apply_selected_model_env,
        _configure_auth_route,
        _ensure_codex_auth,
        _is_kimi_model,
    )

    resolved_route, user_env = _harness_auth_route(
        user, run_record, model=model
    )
    route = str(auth_route or resolved_route).strip().lower()
    if route not in {"api_key", "codex_subscription", "claude_subscription"}:
        raise ValueError(f"Unsupported authentication route: {route}")
    auth_modes = list(ENGINE_AUTH_MODES.get(engine) or [])
    if not _harness_route_supported(engine, route, auth_modes):
        label = {
            "api_key": "API-key",
            "codex_subscription": "Codex subscription",
            "claude_subscription": "Claude Code subscription",
        }[route]
        raise ValueError(
            f"{engine} does not support the recorded {label} route; "
            "the Formal Lean harness cannot silently switch providers"
        )

    selected_model = str(model or _recorded_harness_model(run_record) or "").strip()
    if route == "claude_subscription":
        from agent_monitor.claude_login import DEFAULT_CLAUDE_MODELS

        selected_model = selected_model or "sonnet"
        if selected_model not in DEFAULT_CLAUDE_MODELS:
            raise ValueError(
                f"{selected_model} is not a Claude Code subscription model; "
                f"choose one of: {', '.join(DEFAULT_CLAUDE_MODELS)}"
            )

    from agent_monitor.subprocess_env import child_process_env, project_provider_env

    env = child_process_env(extra=user_env)
    project_provider_env(env, (selected_model,))
    owner_id = (user or {}).get("id") or (run_record or {}).get("owner_id")
    env = _configure_auth_route(env, route=route, user_id=owner_id)
    sponsored_record = (
        route == "api_key"
        and selected_model.lower().startswith("kimi-")
        and str((run_record or {}).get("credential_source") or "").strip()
        == "sponsored_kimi"
    )
    if sponsored_record:
        from agent_monitor import sponsored_kimi_client

        env.update(
            sponsored_kimi_client.issue_credentials(
                minimum_ttl_seconds=1_500,
                engine=_sponsored_formal_proxy_engine(engine),
                client_id=owner_id,
                cache_key=str((run_record or {}).get("run_id") or "manual-formal"),
            )
        )
    env["AGENT_MONITOR_FORMAL_LEAN_MODE"] = "1"
    if route == "claude_subscription":
        env["AGENT_MONITOR_CLAUDE_LEAN_MODE"] = "1"
        for name in (
            "OPENAI_API_KEY",
            "OPENAI_API_KEYS",
            "CODEX_API_KEY",
            "KIMI_API_KEY",
            "DEEPSEEK_API_KEY",
            "OPENROUTER_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "TOGETHERAI_API_KEY",
            "XAI_API_KEY",
            "LLM_API_KEY",
        ):
            env.pop(name, None)
    elif route == "codex_subscription":
        env.pop("AGENT_MONITOR_CLAUDE_LEAN_MODE", None)
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENAI_API_KEYS", None)
        env.pop("CODEX_API_KEY", None)
    else:
        env.pop("AGENT_MONITOR_CLAUDE_LEAN_MODE", None)

    _apply_selected_model_env(env, engine=engine, model=selected_model or None)
    kimi_model = _is_kimi_model(selected_model)
    if kimi_model and route != "api_key":
        raise ValueError("Kimi K3 is an API model and cannot use a linked account subscription")
    if engine == "codex" and kimi_model:
        env["AGENT_MONITOR_KIMI_CODEX_MODE"] = "lean"
    env.setdefault("NO_COLOR", "1")
    prefixes = list(engine_extra_path(engine))
    tools = toolchain_status()
    for tool in ("lean", "lake", "elan"):
        path = tools.get(tool)
        if path:
            prefixes.append(str(Path(path).parent))
    seen: set[str] = set()
    ordered = [p for p in prefixes if p and not (p in seen or seen.add(p))]
    if ordered:
        env["PATH"] = os.pathsep.join([*ordered, env.get("PATH", "")])
    if engine == "codex" and route == "codex_subscription":
        _ensure_codex_auth(env, owner_id)
    if engine == "openclaw":
        env.setdefault("NODE_OPTIONS", "--max-old-space-size=256")
    if engine == "openhands" and route == "api_key":
        key = env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY")
        if key:
            env.setdefault("LLM_API_KEY", key)
            env.setdefault(
                "LLM_MODEL",
                env.get("AGENT_MONITOR_OPENHANDS_MODEL", "openai/gpt-5.2"),
            )
    from agent_monitor.run_tuning import from_record as apply_recorded_tuning

    apply_recorded_tuning(env, run_record)
    return env


def _selected_harness_argv(
    *,
    engine: str,
    model: str | None,
    prompt: str,
    default: list[str],
    env: dict[str, str] | None = None,
) -> list[str]:
    """Bind the Formal Codex process to the exact selected model."""
    from agent_monitor.jobs import _is_kimi_model

    if engine != "codex":
        return default
    selected = str(model or "").strip()
    if not selected:
        raise ValueError(
            "Codex Formal Lean requires an explicit model for this run route"
        )
    if _is_kimi_model(selected):
        return [
            sys.executable,
            "-u",
            str(
                Path(__file__).resolve().parent
                / "runners"
                / "kimi_codex_runner.py"
            ),
            prompt,
        ]

    def with_tuning(argv: list[str]) -> list[str]:
        from agent_monitor.run_tuning import codex_config_args

        args = codex_config_args(env or {}, selected)
        if not args:
            return argv
        try:
            prompt_index = argv.index(prompt)
        except ValueError as exc:
            raise ValueError(
                "Codex harness command cannot bind run tuning before its prompt"
            ) from exc
        argv[prompt_index:prompt_index] = args
        return argv

    argv = list(default)
    for index, token in enumerate(argv):
        if token == "--model":
            if index + 1 >= len(argv):
                raise ValueError("Codex harness command has an empty --model option")
            argv[index + 1] = selected
            return with_tuning(argv)
        if token.startswith("--model="):
            argv[index] = f"--model={selected}"
            return with_tuning(argv)
    try:
        prompt_index = argv.index(prompt)
    except ValueError as exc:
        raise ValueError(
            "Codex harness command cannot bind the selected model before its prompt"
        ) from exc
    argv[prompt_index:prompt_index] = ["--model", selected]
    return with_tuning(argv)


def _running_record(
    workspace: Path,
    *,
    engine: str,
    label: str,
    model: str | None,
    lean_src: str,
    prev: dict[str, Any],
) -> dict[str, Any]:
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model or prev.get("model"),
        "source": f"{label} harness",
        "title": prev.get("title") or "",
        "notes": f"{label} is working on Proof.lean…",
        "status": "running",
        "action": "harness",
        "uses_mathlib": _detect_mathlib(lean_src),
        "sorry_count": _count_sorry(lean_src),
        "sorry_lines": _sorry_lines(lean_src)[:20],
        "lean_path": f"{LEAN_DIRNAME}/{PROOF_FILENAME}",
        "lean": lean_src,
        "citations": _citations_from_lean(lean_src),
        "fidelity": prev.get("fidelity") or {},
        "attempts": list(prev.get("attempts") or []),
        "toolchain": toolchain_status(),
        "log": "",
        "chat": list(prev.get("chat") or []),
        "harness": {"engine": engine, "label": label, "state": "running"},
    }


def _reset_harness_log(ldir: Path) -> Path:
    """Start each harness with a fresh transcript, never a prior run's log."""
    log_path = ldir / "harness.log"
    log_path.write_text("", encoding="utf-8")
    return log_path


def _start_harness_reserved(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    engine: str = "codex",
    model: str | None = None,
    lean: str | None = None,
    audit: bool = True,
    _operation_token: object,
) -> dict[str, Any]:
    """Launch a coding agent on Proof.lean in the background.

    Returns immediately with a ``running`` record; the selected model is bound
    to both the harness process and its post-run audit.
    """
    from agent_monitor.engines_registry import CLI_ENGINES, build_cli_command

    spec = CLI_ENGINES.get(engine)
    if not spec or engine in _HARNESS_EXCLUDE:
        raise ValueError(f"Unknown harness engine: {engine}")
    selected_model = str(
        model or _recorded_harness_model(run_record) or ""
    ).strip() or None
    auth_route, _user_env = _harness_auth_route(
        user, run_record, model=selected_model
    )
    if engine == "codex":
        options = harness_model_options(user, run_record)
        allowed_models = list(options.get("models") or [])
        if not selected_model:
            raise ValueError(
                "Codex Formal Lean requires an explicit model for this run route"
            )
        if selected_model not in allowed_models:
            detail = (
                ", ".join(allowed_models)
                if allowed_models
                else "no models are available for this run route"
            )
            raise ValueError(
                f"{selected_model} is not available for this run route; choose {detail}"
            )
    state = next(
        (
            item
            for item in harness_engines(user, run_record, model=selected_model)
            if item["id"] == engine
        ),
        None,
    )
    if not state or not state.get("available"):
        detail = (
            (state or {}).get("status_detail")
            or spec.get("install_hint")
            or "not available"
        )
        raise ValueError(f"{spec.get('label') or engine} cannot start — {detail}")
    if harness_job(workspace):
        raise ValueError("A harness run is already in progress for this run")

    tools = toolchain_status()
    if not tools["available"]:
        raise ValueError(
            "Lean toolchain not installed on this server — a harness cannot compile "
            "anything. Install elan first, or use Verify."
        )

    harness_env = _harness_env(
        engine,
        user,
        model=selected_model,
        run_record=run_record,
        auth_route=auth_route,
    )
    ldir = lean_dir(workspace)
    if ldir.is_symlink():
        raise ValueError("Refusing to run a harness through symbolic-link lean directory")
    target = ldir / PROOF_FILENAME
    if target.is_symlink():
        raise ValueError("Refusing to seed a symbolic-link Proof.lean")
    lean_src = (lean if lean is not None else read_source(workspace)) or ""
    uses_mathlib = _detect_mathlib(lean_src)
    _write_scaffold(ldir, uses_mathlib=uses_mathlib)
    _reset_harness_log(ldir)
    if lean_src.strip():
        target.write_text(lean_src.rstrip() + "\n", encoding="utf-8")
    problem, informal = _problem_and_proof(workspace, run_record)
    _write_task_file(ldir, problem=problem, proof=informal, existing=lean_src)
    _initial_source, initial_fingerprint, _initial_reason = (
        _regular_root_lean_artifact(workspace)
    )

    prompt = (
        HARNESS_TASK
        + "\n\nPROBLEM:\n"
        + (problem or "(see TASK.md)")[:6000]
        + "\n\nINFORMAL PROOF:\n"
        + (informal or "(see TASK.md)")[:30000]
    )
    argv = build_cli_command(
        engine, prompt=prompt, workspace=ldir, problem_file=ldir / "TASK.md"
    )
    if not argv:
        raise ValueError(
            f"{spec.get('label') or engine} is not configured on this server — "
            f"{spec.get('install_hint') or 'install it first'}"
        )
    argv = _selected_harness_argv(
        engine=engine,
        model=selected_model,
        prompt=prompt,
        default=argv,
        env=harness_env,
    )

    label = str(spec.get("label") or engine)
    prev = load_cached(workspace) or {}
    record = _running_record(
        workspace,
        engine=engine,
        label=label,
        model=selected_model,
        lean_src=lean_src,
        prev=prev,
    )
    record["harness"]["command"] = " ".join(argv[:8])
    record["harness"]["auth_route"] = auth_route
    record["harness"]["model"] = selected_model or ""
    record["harness"]["initial_sha256"] = (initial_fingerprint or {}).get("sha256")
    _write_cached(workspace, record)

    job: dict[str, Any] = {
        "engine": engine,
        "label": label,
        "started_at": time.time(),
        "state": "running",
        "proc": None,
        "stopping": False,
        "operation_token": _operation_token,
    }
    thread = threading.Thread(
        target=_harness_worker_guarded,
        kwargs={
            "operation_token": _operation_token,
            "workspace": workspace,
            "ldir": ldir,
            "argv": argv,
            "engine": engine,
            "label": label,
            "user": user,
            "model": selected_model,
            "run_record": run_record,
            "env": harness_env,
            "problem": problem,
            "informal": informal,
            "base": record,
            "initial_fingerprint": initial_fingerprint,
            "audit": audit,
        },
        daemon=True,
        name=f"lean-harness-{engine}-{workspace.name}",
    )
    job["thread"] = thread
    with _HARNESS_LOCK:
        _HARNESS_JOBS[_harness_key(workspace)] = job
        try:
            thread.start()
        except BaseException:
            _HARNESS_JOBS.pop(_harness_key(workspace), None)
            raise
    return record


def start_harness(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    engine: str = "codex",
    model: str | None = None,
    lean: str | None = None,
    audit: bool = True,
) -> dict[str, Any]:
    """Reserve a proof workspace and launch its Formal Lean harness."""
    resolved = Path(workspace).resolve()
    token = _reserve_formal_operation(resolved, f"harness/{engine}")
    try:
        return _start_harness_reserved(
            workspace=resolved,
            run_record=run_record,
            user=user,
            engine=engine,
            model=model,
            lean=lean,
            audit=audit,
            _operation_token=token,
        )
    except BaseException:
        _release_formal_operation(resolved, token)
        raise


def _harness_worker_guarded(
    *,
    operation_token: object,
    **kwargs,
) -> None:
    """Release a cross-thread harness reservation on every worker exit path."""
    workspace = Path(kwargs["workspace"])
    key = _harness_key(workspace)
    try:
        _harness_worker(**kwargs)
    finally:
        with _HARNESS_LOCK:
            _HARNESS_JOBS.pop(key, None)
        _release_formal_operation(workspace, operation_token)


def _harness_worker(
    *,
    workspace: Path,
    ldir: Path,
    argv: list[str],
    engine: str,
    label: str,
    user: dict[str, Any] | None,
    model: str | None,
    run_record: dict[str, Any] | None,
    env: dict[str, str],
    problem: str,
    informal: str,
    base: dict[str, Any],
    initial_fingerprint: dict[str, Any] | None,
    audit: bool,
) -> None:
    key = _harness_key(workspace)
    timeout_s = int(os.environ.get("AGENT_MONITOR_LEAN_HARNESS_TIMEOUT", "1200"))
    log_path = ldir / "harness.log"
    lines: list[str] = []
    started = time.time()
    exit_code: int | None = None
    stopped = False

    def flush(note: str | None = None) -> None:
        rec = dict(base)
        rec["log"] = "".join(lines)[-16000:]
        rec["harness"] = {
            **(base.get("harness") or {}),
            "state": "running",
            "elapsed_s": round(time.time() - started, 1),
        }
        if note:
            rec["notes"] = note
        try:
            _write_cached(workspace, rec)
        except (OSError, TypeError, ValueError):
            pass

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(ldir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        lines.append(f"failed to launch {label}: {exc}\n")
        _harness_finish(
            workspace=workspace,
            engine=engine,
            label=label,
            log="".join(lines),
            exit_code=None,
            elapsed=0.0,
            user=user,
            run_record=run_record,
            model=model,
            problem=problem,
            informal=informal,
            audit=audit,
            initial_fingerprint=initial_fingerprint,
            stopped=False,
        )
        with _HARNESS_LOCK:
            _HARNESS_JOBS.pop(key, None)
        return

    with _HARNESS_LOCK:
        if key in _HARNESS_JOBS:
            _HARNESS_JOBS[key]["proc"] = proc

    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert proc.stdout is not None
        try:
            for output_line in proc.stdout:
                output_queue.put(output_line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(
        target=read_output,
        daemon=True,
        name=f"lean-harness-output-{workspace.name}",
    )
    reader.start()
    last_flush = 0.0
    deadline = started + timeout_s
    try:
        while True:
            now = time.time()
            if now > deadline:
                _terminate_process_group(proc, force=True)
                lines.append(f"\n[agent-monitor] harness timeout after {timeout_s}s — killed\n")
                break
            try:
                line = output_queue.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            lines.append(line)
            if len(lines) > 4000:
                del lines[:1000]
            if now - last_flush > 2.5:
                flush()
                last_flush = now
        try:
            exit_code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc, force=True)
            exit_code = proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        lines.append(f"\n[agent-monitor] harness error: {exc}\n")
    finally:
        with _HARNESS_LOCK:
            job = _HARNESS_JOBS.get(key) or {}
            stopped = bool(job.get("stopping"))
            job["proc"] = None
    try:
        log_path.write_text("".join(lines), encoding="utf-8")
    except OSError:
        pass

    try:
        _harness_finish(
            workspace=workspace,
            engine=engine,
            label=label,
            log="".join(lines),
            exit_code=exit_code,
            elapsed=round(time.time() - started, 1),
            user=user,
            run_record=run_record,
            model=model,
            problem=problem,
            informal=informal,
            audit=audit,
            initial_fingerprint=initial_fingerprint,
            stopped=stopped,
        )
    finally:
        with _HARNESS_LOCK:
            _HARNESS_JOBS.pop(key, None)


def _harness_fidelity_audit(
    *,
    workspace: Path,
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None,
    model: str | None,
    problem: str,
    proof: str,
    lean: str,
) -> dict[str, Any]:
    """Audit via the immutable run route, including native Codex subscription."""
    from agent_monitor.jobs import _route_from_record

    if _route_from_record(run_record) != "codex_subscription":
        return _audit_faithfulness(
            _llm_environment(user, run_record),
            model,
            problem=problem,
            proof=proof,
            lean=lean,
        )

    prompt = (
        AUDIT_PROMPT.replace("{problem}", (problem or "(not recorded)")[:6000])
        .replace("{proof}", (proof or "(not available)")[:20000])
        .replace("{lean}", (lean or "")[:40000])
    )
    try:
        result = _codex_subscription_interaction(
            workspace=workspace,
            user=user,
            run_record=run_record,
            prompt=prompt,
            lean_source=lean,
            edit=False,
        )
        used = str(result.get("model") or model or "Codex subscription")
        data = _extract_json(str(result.get("answer") or ""))
    except (ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return {"ok": False, "error": str(exc)[:300]}
    if not isinstance(data, dict):
        return {"ok": False, "error": "auditor returned a non-object"}
    issues = [
        str(item)[:400]
        for item in (data.get("issues") or [])
        if str(item).strip()
    ][:10]
    severity = str(data.get("severity") or "").strip().lower()
    faithful = bool(data.get("faithful"))
    if severity not in {"ok", "minor", "major"}:
        severity = "ok" if faithful else "major"
    if severity == "major":
        faithful = False
    return {
        "ok": True,
        "faithful": faithful,
        "severity": severity,
        "verdict": str(data.get("verdict") or "")[:600],
        "issues": issues,
        "main_decl": str(data.get("main_decl") or "")[:80],
        "statement_suggestion": str(data.get("statement_suggestion") or "")[:600],
        "model": used,
    }



def _harness_finish(
    *,
    workspace: Path,
    engine: str,
    label: str,
    log: str,
    exit_code: int | None,
    elapsed: float,
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None,
    model: str | None,
    problem: str,
    informal: str,
    audit: bool,
    initial_fingerprint: dict[str, Any] | None,
    stopped: bool,
) -> dict[str, Any]:
    """Re-check a fresh regular artifact, but never forgive a failed child."""
    from agent_monitor.jobs import _route_from_record

    prev = load_cached(workspace) or {}
    previous_harness = (
        prev.get("harness") if isinstance(prev.get("harness"), dict) else {}
    )
    effective_model = str(
        model
        or previous_harness.get("model")
        or prev.get("model")
        or _recorded_harness_model(run_record)
        or ""
    ).strip()
    effective_route = str(
        previous_harness.get("auth_route")
        or _route_from_record(run_record)
        or ""
    ).strip()
    tail = log[-4000:]
    lean_text, final_fingerprint, artifact_reason = _regular_root_lean_artifact(
        workspace
    )
    lean_src = (lean_text or "").strip()
    artifact_regular = final_fingerprint is not None
    artifact_fresh = bool(
        final_fingerprint is not None
        and (
            initial_fingerprint is None
            or final_fingerprint.get("sha256")
            != initial_fingerprint.get("sha256")
        )
    )
    child_exit_ok = exit_code == 0
    rejection_reasons: list[str] = []
    if stopped:
        rejection_reasons.append("the run was stopped")
    if not child_exit_ok:
        rejection_reasons.append(
            "the child process did not exit successfully"
            if exit_code is None
            else f"the child process exited {exit_code}"
        )
    if not artifact_regular:
        rejection_reasons.append(artifact_reason or "Proof.lean is not a regular root file")
    elif not lean_src:
        rejection_reasons.append("Proof.lean is empty")
    elif not artifact_fresh:
        rejection_reasons.append("Proof.lean is unchanged from the pre-run artifact")

    artifact_accepted = bool(
        child_exit_ok and artifact_regular and artifact_fresh and lean_src and not stopped
    )
    harness_meta = {
        "engine": engine,
        "label": label,
        "state": "stopped" if stopped else "finished",
        "exit_code": exit_code,
        "child_exit_ok": child_exit_ok,
        "elapsed_s": elapsed,
        "log": tail,
        "artifact_regular": artifact_regular,
        "artifact_fresh": artifact_fresh,
        "artifact_accepted": artifact_accepted,
        "initial_sha256": (initial_fingerprint or {}).get("sha256"),
        "final_sha256": (final_fingerprint or {}).get("sha256"),
    }
    if effective_route:
        harness_meta["auth_route"] = effective_route
    if effective_model:
        harness_meta["model"] = effective_model
    if not lean_src or not artifact_regular:
        saved_source = str(prev.get("lean") or "")
        reason = "; ".join(rejection_reasons) or "no acceptable Proof.lean"
        status = "stopped" if stopped else "failed"
        return _persist(
            workspace,
            lean_src=saved_source,
            status=status,
            uses_mathlib=_detect_mathlib(saved_source),
            attempts=list(prev.get("attempts") or []),
            notes=f"{label} harness rejected its artifact: {reason}.",
            title=str(prev.get("title") or ""),
            model=effective_model or prev.get("model"),
            source=f"{label} harness",
            action="harness",
            extra={
                "log": tail,
                "harness": harness_meta,
            },
        )

    uses_mathlib = _detect_mathlib(lean_src)
    try:
        check = _write_and_run(workspace, lean_src, uses_mathlib)
    except (OSError, ValueError) as exc:
        check = {
            "ok": False,
            "status": "failed",
            "exit_code": None,
            "duration_s": 0.0,
            "command": None,
            "toolchain": None,
            "stderr": str(exc),
            "stdout": "",
        }
    authoritative_sha256 = str(check.get("source_sha256") or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", authoritative_sha256):
        harness_meta["final_sha256"] = authoritative_sha256
    sorry_n = _count_sorry(lean_src)
    kernel_status = _status_from_check(check, sorry_n, mode="check")
    status = kernel_status
    if stopped:
        status = "stopped"
    elif not artifact_accepted:
        status = "failed"

    attempts = list(prev.get("attempts") or [])
    attempts.append(
        {
            "round": len(attempts),
            "uses_mathlib": uses_mathlib,
            "sorry_count": sorry_n,
            "action": f"harness/{engine}",
            "child_exit_code": exit_code,
            "artifact": {
                "regular": artifact_regular,
                "fresh": artifact_fresh,
                "accepted": artifact_accepted,
                "initial_sha256": (initial_fingerprint or {}).get("sha256"),
                "final_sha256": harness_meta.get("final_sha256"),
            },
            "check": {
                k: check[k]
                for k in (
                    "ok",
                    "status",
                    "exit_code",
                    "duration_s",
                    "command",
                    "toolchain",
                    "sandbox",
                    "source_sha256",
                    "checker_profile",
                    "release_id",
                    "toolchain_tree_sha256",
                    "mathlib_tree_sha256",
                )
                if k in check
            },
            "stderr_tail": (check.get("stderr") or "")[-2000:],
            "stdout_tail": (check.get("stdout") or "")[-1000:],
        }
    )

    audit_result: dict[str, Any] | None = None
    if audit and artifact_accepted and status in {"verified", "incomplete"}:
        audit_result = _harness_fidelity_audit(
            workspace=workspace,
            user=user,
            run_record=run_record,
            model=effective_model or None,
            problem=problem,
            proof=informal,
            lean=lean_src,
        )
        status = _apply_fidelity(status, _fidelity_flags(lean_src), audit_result)

    notes = _check_summary(lean_src, check, status)
    if rejection_reasons:
        notes = (
            "Artifact rejected: "
            + "; ".join(rejection_reasons)
            + ". The outer Lean result is diagnostic only. "
            + notes
        )
    fid_note = _fidelity_summary(_fidelity_flags(lean_src), audit_result)
    verdict = "stopped by user" if stopped else f"exit {exit_code}"
    notes = f"{label} harness ({verdict}, {elapsed}s). {notes} {fid_note}".strip()
    compiler_log = (
        (check.get("stderr") or "") + "\n" + (check.get("stdout") or "")
    ).strip()
    if rejection_reasons:
        rejection_log = "[agent-monitor] artifact rejected: " + "; ".join(
            rejection_reasons
        )
        compiler_log = (compiler_log + "\n" + rejection_log).strip()
    harness_meta["outer_check_ok"] = bool(check.get("ok"))
    return _persist(
        workspace,
        lean_src=lean_src,
        status=status,
        uses_mathlib=uses_mathlib,
        attempts=attempts,
        notes=notes,
        title=str(prev.get("title") or ""),
        model=effective_model or prev.get("model"),
        source=f"{label} harness",
        action="harness",
        audit=audit_result,
        authoritative_check=check,
        extra={
            "harness": harness_meta,
            # The compiler remains primary; the full child transcript remains
            # available under harness.log and in the harness metadata.
            "log": compiler_log or tail,
        },
    )
