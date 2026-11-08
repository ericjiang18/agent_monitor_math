"""Lean verification — translate an informal proof into Lean 4 and check it.

Pipeline (``generate``)
-----------------------
1. Read ``problem.txt`` + ``proof.md`` (or ``proof.tex`` / final agent output)
   and an optional blueprint (the run's fact DAG or informal DAG).
2. Statement first: ask for a Lean 4.14 / Mathlib v4.14 skeleton that STATES
   the problem (``main`` plus blueprint lemmas) with ``sorry`` proofs, compile
   it through the sandboxed broker, and run an independent statement-fidelity
   audit before any proof effort. A major finding gets one bounded fix.
3. Lock the audited statement of ``main``: a normalized text hash of its
   signature and of every non-theorem command above it (commands are found at
   any column and after ``… in`` prefixes), plus a Lean-computed fingerprint of
   the elaborated statement and the local definitions it depends on. Every
   later round must keep both; the statement audit is reused for the proved
   file only when Lean confirms the fingerprint, otherwise it is re-run.
4. Prove: fill the skeleton, then repair with line-numbered source, the first
   compiler errors with context, goals at remaining ``sorry`` and a compact
   attempt history. Sorry elimination continues while repair budget remains;
   the best checked candidate (not merely the last one) is kept.
5. Soundness: a clean compile additionally needs a root-anchored
   ``#print axioms`` probe of every named declaration (the locked ``main``
   must exist under exactly that name) showing only ``propext`` /
   ``Classical.choice`` / ``Quot.sound``, and a ``declaration uses 'sorry'``
   warning always means incomplete.
6. Cache the whole result as ``lean_verify.json`` so reopening the tab is free.

The kernel check only ever runs through ``lean_broker.check_source``. If the
broker is unavailable the status becomes ``toolchain_missing`` immediately:
the drafted statement is kept for review and no repair or audit calls are
spent on an infrastructure error. There is no local/unsandboxed fallback.
"""
from __future__ import annotations

import contextvars
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
from typing import Any, Callable

from agent_monitor import account_policy, lean_broker, usage_ledger
from agent_monitor.proof_graph import (  # reuse LLM plumbing and safe reads
    CLAUDE_DERIVED_TIMEOUT_ENV,
    MAX_PROBLEM_BYTES as MAX_WORKSPACE_PROBLEM_BYTES,
    MAX_SOURCE_BYTES as MAX_WORKSPACE_SOURCE_BYTES,
    _call_llm,
    _claude_derived_timeout,
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
# The broker's fixed per-profile wall-clock limits (AMLB_TIMEOUT_SEC written by
# deploy/lean-broker/prepare_release.py), quoted in timeout diagnostics.
LEAN_BROKER_LIMIT_SECONDS = {"core": 60, "mathlib": 90}

# The scaffold pins the exact broker toolchain. The lakefile never declares a
# git dependency: Mathlib is provided by the broker (authoritative checks) or
# by the shared read-only mirror behind the harness `check.sh` wrapper.
DEFAULT_TOOLCHAIN = lean_broker.EXPECTED_LEAN_TOOLCHAIN

# Repair budgets. The automatic pipeline and manual Verify share these
# defaults; guests use the sponsored gateway and are capped at the same four
# rounds (a harder proof often fixes one Lean/Mathlib error per round), while
# signed-in manual requests may ask for more (the console caps them at six).
AUTO_MAX_REPAIRS = 4
GUEST_MAX_REPAIRS = 4
# A guest feedback revision gets a single repair round (the console mirrors it
# as ``_GUEST_REVISE_MAX_REPAIRS`` in its guest policy).
GUEST_REVISE_MAX_REPAIRS = 1
# Skeleton compile repairs and statement-fix rounds are bounded separately so
# statement work cannot silently consume the whole proof budget.
STATEMENT_REPAIRS = 2
GUEST_STATEMENT_REPAIRS = 1
STATEMENT_FIX_ROUNDS = 1
# Optional independent auditor model (for example a different provider). When
# unset, the audit still runs as a cold, separately-prompted call.
AUDIT_MODEL_ENV = "AGENT_MONITOR_FORMAL_AUDIT_MODEL"
# Diagnostics handed to the repair model: the FIRST errors are root causes.
REPAIR_ERROR_LIMIT = 6
# Only these axioms may appear in any `#print axioms` report of a verified file.
STANDARD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})

LEAN_TARGET = f"""LEAN TARGET (exact): Lean 4.14.0 (`{lean_broker.EXPECTED_LEAN_TOOLCHAIN}`) with
Mathlib {lean_broker.EXPECTED_MATHLIB_INPUT_REV} (commit {lean_broker.EXPECTED_MATHLIB_REV[:8]}, early December 2024). Nothing newer is
installed. You may know later Mathlib: declarations added or renamed in
2025-2026 do NOT exist here, so prefer long-standing names and automation.
- Start the file with `import Mathlib` (the whole library is supported and
  recommended; a narrower `Mathlib.*` import also works). Add `open` lines as
  needed, e.g. `open Finset` or `open Real`.
- Tactics available here: `simp`, `simp_all`, `omega`, `decide`, `norm_num`,
  `ring`, `ring_nf`, `linarith`, `nlinarith`, `positivity`, `field_simp`,
  `push_cast`, `exact_mod_cast`, `gcongr`, `interval_cases`, `rcases`/`obtain`,
  `induction ... with`, `aesop`. NOT available in this version: `grind`,
  `decide +kernel`. Never leave `exact?`/`apply?` in the file.
- Notation that works: `∑ i ∈ Finset.range n, f i`, `∏ i ∈ s, f i`, `√x`
  (`Real.sqrt x`), `a ≡ b [ZMOD n]`, `n !` (after `open Nat`), `a ∣ b`.
  Standard notions: `Nat.Prime p`, `Irrational x`, `Even n`, `Odd n`,
  `Nat.Coprime a b`, `Nat.gcd a b`.
- Names checked against this exact Mathlib: `Finset.sum_range_succ`,
  `Finset.prod_range_succ`, `Finset.sum_range_id_mul_two`,
  `Nat.exists_infinite_primes`, `Nat.Prime.two_le`,
  `Nat.Prime.eq_one_or_self_of_dvd`, `Nat.Prime.dvd_mul`, `Nat.minFac_prime`,
  `irrational_sqrt_two`, `Nat.even_or_odd`, `Int.even_or_odd`, `Nat.odd_iff`,
  `Int.odd_iff`, `Odd.mul`, `Nat.coprime_self_add_right`,
  `Int.emod_emod_of_dvd`, `two_mul_le_add_sq`, `abs_add`, `sq_nonneg`,
  `Real.sq_sqrt`, `Real.sqrt_nonneg`.
- 2024 forms differ from newer ones: `le_div_iff₀` and `div_le_iff₀` exist
  (the unsubscripted names are deprecated but still compile); `Nat.dvd_sub'`
  is the two-hypothesis form (`Nat.dvd_sub` here also needs `n ≤ m`);
  `Nat.lt_pow_self` takes `1 < a` explicitly.
- `ℕ` subtraction truncates (`2 - 5 = 0`) and `/` on `ℕ`/`ℤ` is floor
  division. State claims about negatives or fractions over `ℤ`, `ℚ` or `ℝ` and
  cast explicitly, e.g. `(n : ℝ)`.
"""

STATEMENT_FIDELITY = """STATEMENT FIDELITY — read this first. A file that compiles but states the
wrong thing is worse than one that fails to compile, because it looks verified.
- The main theorem must transcribe the PROBLEM itself, not a convenient special
  case: quantify over everything the problem quantifies over, keep every
  hypothesis, keep the exact conclusion. If the problem says "for all n", the
  statement starts `∀ n` (or takes `(n : ℕ)`) — not a fixed `n = 7`.
- The PROBLEM fixes each variable's domain; conventions in the informal proof
  do not. "Every natural number n" is `(n : ℕ)` with no `1 ≤ n`, `0 < n` or
  `n ≠ 0` hypothesis (Lean's ℕ includes 0), even when the proof writes
  ℕ = {1, 2, 3, …} or starts at n = 1. Add a bound only when the problem states
  it, or when the claim is false or undefined without it (say why in `notes`).
- Never add a hypothesis that already assumes the conclusion, and never let the
  hypotheses be contradictory: a vacuous theorem proves nothing.
- Use Mathlib's standard notions (`Odd`, `Even`, `Nat.Prime`, `Irrational`,
  `Nat.Coprime`, `∣`, `Real.sqrt`, `Finset.sum`, …) instead of hand-rolled
  definitions. Define something yourself only when Mathlib lacks it, exactly
  as the problem defines it, and say so in its docstring.
- Forbidden entirely: `axiom`, `sorry`-hiding tricks, `admit`, `unsafe`,
  `opaque`, `@[implemented_by]`, `@[extern]`, `native_decide`,
  `set_option debug.skipKernelTC`, and every metaprogramming command
  (`run_cmd`, `run_tac`, `run_elab`, `elab`, `macro`, `syntax`, `initialize`,
  `#eval`). `decide` is fine when the goal is genuinely decidable and small.
"""

CITATION_RULE = """Citations attach to EACH declaration — never one list at the top of the file.
Immediately above every `theorem` / `lemma` / `def`, write a doc comment whose
last line starts with `Cites:`, entries separated by semicolons, each shaped
`<source> — <what it contributes>`:

  /-- Product of two odd naturals is odd.
  Cites: self-derived — direct expansion of (2a+1)(2b+1);
  [Hardy & Wright, Thm 1.2] — parity of products -/
  theorem main (a b : ℕ) : ...

Use `self-derived` when you proved it inline here, and `uncited` when the
informal proof leans on something it never sourced. Never invent a source.
Mirror the same entries in the metadata `citations` array, each with a `decl`
field naming the declaration it belongs to.
"""

LEAN_IDIOMS = """LEAN 4.14 / MATHLIB IDIOMS (fixes for errors this checker has reported):
- Factorial: `open Nat`, then `n !` with a space (`n!` is an unknown
  identifier). Parenthesise it inside arithmetic, `4 * (n !) - 1` (`n ! - 1`
  misparses: "function expected"), or write `Nat.factorial n`.
- Sums: `Finset.sum_range_succ` rewrites `∑ i ∈ Finset.range (n + 1), f i` to
  `∑ i ∈ Finset.range n, f i + f n`. For `Finset.Icc a (b + 1)` use
  `Finset.sum_Icc_succ_top (h : a ≤ b + 1)`; the `h` you pass fixes `b`, so for
  `Icc 1 (m + 1 + 1)` pass `(show 1 ≤ m + 1 + 1 by omega)`. A wrong `h` fails
  ("did not find instance of the pattern") or rewrites a different sum and
  leaves "unsolved goals".
- Casts: when `↑(m + 1)` meets `↑m + 1` ("type mismatch"), normalise with
  `push_cast` (or `rw [Nat.cast_succ]`) and then `ring` / `field_simp`; move a
  ℕ fact into ℚ or ℝ with `exact_mod_cast`.
- `%` with numerals: `omega` proves linear goals like `(3 + k * 4) % 4 = 3`
  (terms such as `n !` are atoms). `Nat.add_mul_mod_self_right` is
  `(a + b * n) % n = a % n`; the `_left` form reduces modulo `b`.
- "no goals to be solved": an earlier tactic already closed that goal; delete
  the tactic at the reported line instead of adding another.
- "has already been declared": Mathlib owns that name (even `factorization`);
  rename your helper, e.g. `aux_factorization`.
"""

OUTPUT_CONTRACT = """OUTPUT FORMAT — reply with exactly two fenced blocks and nothing else:
1. The COMPLETE Proof.lean in ONE fenced block (plain Lean, never escaped):
```lean
import Mathlib
...
```
2. Small metadata in ONE fenced JSON block (no Lean source inside it):
```json
{"main_decl": "main", "title": "short theorem name", "notes": "one short sentence", "citations": [{"id": "1", "decl": "main", "text": "source — contribution", "kind": "theorem|self-derived|arxiv|doi|uncited"}]}
```
"""

TRANSLATE_PROMPT = """You are a Lean 4 autoformalization expert. PHASE 1 OF 2 — STATE, DO NOT PROVE.
Write the Lean 4 skeleton that STATES the PROBLEM faithfully: imports, any
definitions the statement needs, one lemma per BLUEPRINT item (when a
blueprint is given) and the main theorem `main`. Every proof in this phase is
exactly `sorry`. The skeleton is compiled, audited for fidelity by an
independent reviewer, and then LOCKED: later proof rounds may not change the
statement of `main` or anything above it except theorem/lemma declarations.

{target}
{fidelity}
{citation_rule}
Rules for the skeleton:
- Name the main theorem `main`; its statement is the PROBLEM's claim, exactly.
- Put a helper definition above `main` only when the statement genuinely needs
  it; prefer Mathlib's own definitions.
- For a **Counterexample**, make `main` prove only a faithful negation of the
  original universal claim using the explicit witness. Do not conjoin optional
  minimality, every-case audits, or stronger side claims into `main`. Omit such
  bonus claims from the file unless the PROBLEM explicitly asks for them; a
  failed optional helper makes the whole Lean file fail.
- If the informal artifact is **Partial Progress** or **Known/Open Status**, keep
  the exact open target as `main` (it will keep one honest `sorry`), and formalise
  each feasible self-derived lemma as a separate declaration.
  Do not discard verified partial progress merely because `main` remains open.
- When a BLUEPRINT is given, state one `lemma` per blueprint item in the given
  order, faithfully to its statement, with a doc comment containing the line
  `Informal: <id>` (the item's id). The target item is `main`; give `main` the
  line `Informal: <target id>` as well.
- Use ASCII Lean identifiers. Put informal commentary in `/-- ... -/` docs.
- Do not copy ProvingConsole runtime, harness, tool, skill-availability, or
  candidate-audit status into the Lean source or `notes`. The informal
  candidate audit is a separate pipeline; report only formalization, compiler,
  admission, and statement-fidelity facts here.

{output_contract}
PROBLEM:
{problem}

BLUEPRINT (advisory structure; the PROBLEM stays authoritative):
{blueprint}

INFORMAL PROOF (context for choosing the lemmas; do not prove anything yet):
{proof}
"""

PROVE_PROMPT = """You are a Lean 4 proof engineer. PHASE 2 — PROVE THE LOCKED SKELETON.
The skeleton below compiles. Replace every `sorry` with a complete proof,
following the INFORMAL PROOF and the BLUEPRINT.

LOCK (checked mechanically; a violating reply is rejected unseen): keep the
statement of `{main_decl}` and every non-theorem command above it (definitions,
instances, attributes, `open`, `variable`, notation, options, namespaces and
sections) exactly as written. Lean also checks that `{main_decl}` still
elaborates to the same statement. You may add, split,
reorder or restate helper `lemma`/`theorem` declarations, add imports, and put
anything you like below `{main_decl}`. Keep the `Informal:` and `Cites:` doc lines.

How to prove well here:
- Work bottom-up: prove helper lemmas first, then `{main_decl}` from them.
- Split hard steps into `have` statements; close routine goals with automation
  (`simp`, `omega`, `norm_num`, `ring`, `linarith`, `nlinarith`, `positivity`,
  `decide` for small finite checks, `aesop`).
- If you are unsure a lemma name exists in this 2024 Mathlib, prove the fact
  inline or with automation instead of guessing a name.
- If one step is genuinely out of reach, leave `sorry` on exactly that step and
  say which in `notes`. A precise `sorry` beats a weakened statement.

{target}
{idioms}
{fidelity}
{output_contract}
PROBLEM:
{problem}

BLUEPRINT:
{blueprint}

INFORMAL PROOF:
{proof}

LOCKED SKELETON (Proof.lean):
```lean
{lean}
```
"""

REPAIR_PROMPT = """You are repairing a Lean 4 file for Lean 4.14.0 + Mathlib v4.14.0 (Dec 2024).
TASK: {task}

Rules: fix the ERRORS and close the goals, do not dodge them. Keep the exact
original PROBLEM claim unchanged — weakening or specialising it to compile counts
as a failure. If the current `main` added nonessential conjuncts beyond the
original claim, move those to separate helpers only when they already compile;
otherwise omit them from the file rather than treating their removal as
weakening. {lock_rule}
Make targeted edits: change only the lines the diagnostics point at (and what
they force), keep everything else identical, keep the `Cites:` and `Informal:`
doc lines. The first error is usually the root cause; later errors are often
consequences. An `unknown identifier`/`unknown constant` means the name does
not exist in this 2024 Mathlib: prove the fact inline or with a tactic instead
of guessing another name. If a subgoal is genuinely out of reach, leave one
`sorry` there and say so in `notes`.

{target}
{idioms}
{fidelity}
{output_contract}
PROBLEM:
{problem}

ATTEMPT HISTORY (oldest first):
{history}

CURRENT SOURCE (line-numbered for reference; the numbers are NOT part of the file):
{numbered}

DIAGNOSTICS (first errors first, with the offending lines):
{errors}

GOALS AT REMAINING `sorry` (from the checker):
{goals}

BLUEPRINT:
{blueprint}

INFORMAL PROOF (relevant part):
{proof}
"""

FEEDBACK_PROMPT = """You are revising a Lean 4 formalisation based on human feedback.

Rules:
- Apply the human's request faithfully.
- Target Lean 4.14.0 + Mathlib v4.14.0; `import Mathlib` is supported.
- Prefer eliminating `sorry` / `admit` when the feedback asks for a complete proof.
- Keep theorem names stable unless the human asks to rename them.
- Keep the per-declaration `Cites:` doc comments accurate; update them if the
  revision changes what a declaration relies on.

{target}
{fidelity}
{output_contract}
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

AUDIT_PROMPT = """You are an independent, strict reviewer of a Lean 4 autoformalization. You did
not write this file and owe its author nothing. The file compiles (proofs may
still be `sorry` placeholders — ignore proof completeness here). Compiling only
means it is well-typed; it does NOT mean it states the PROBLEM. Your job is to
catch a file that would let a wrong or weaker claim be reported as proved.

Method:
1. Back-translate first: read ONLY the Lean statement of the main declaration
   (and any local definitions it uses) and write, in `back_translation`, the
   precise English claim it makes — quantifiers, domains (ℕ vs ℤ vs ℝ,
   truncated subtraction, floor division), hypotheses and conclusion.
2. Then compare that claim with the PROBLEM, in order:
   a. Same quantifiers and domains, same hypotheses, same conclusion? A
      statement specialised to one instance, with an extra convenient
      hypothesis, or over a different number system is `major`. Domains come
      from the PROBLEM, never from the informal proof's conventions: for
      "every natural number n" (Lean's ℕ includes 0) an unstated `1 ≤ n`,
      `0 < n` or `n ≠ 0` is an extra hypothesis (`major`) even when the proof
      declares ℕ = {1, 2, 3, …}; it is `minor` only if the claim is false or
      undefined without it (say so in `issues`).
   b. Are the hypotheses satisfiable? Contradictory or self-assuming
      hypotheses make the theorem vacuous — `major`.
   c. Is the conclusion the real one, not `True`, a restatement of a
      hypothesis, or strictly weaker? Otherwise — `major`.
   d. Do local `def`s that model standard notions match the standard meaning?
      A subtly wrong definition is `major`.
   e. Is anything smuggled in via `axiom`, `admit`, `native_decide`,
      `set_option debug.skipKernelTC`, `unsafe`, `opaque`, `@[implemented_by]`,
      or a metaprogramming command (`run_cmd`, `elab`, `macro`, …)? — `major`.
Equivalent reformulations are fine (e.g. `Odd n` vs `n % 2 = 1`, `∀ n` binder
vs argument, `≥` vs `≤` flipped). Set `severity` to `major` only for a real
mismatch, `minor` for cosmetic or trust caveats, `ok` if faithful. Name the
declaration and quantifiers concretely.

Respond with STRICT JSON only (no Lean fences inside strings):
{
  "main_decl": "name of the declaration that is supposed to be the problem's claim",
  "back_translation": "the claim the Lean statement makes, in precise English",
  "faithful": true,
  "severity": "ok|minor|major",
  "verdict": "one sentence — what this file actually states",
  "issues": ["concrete, specific problems (empty if none)"],
  "statement_suggestion": "a faithful Lean signature for the main claim, or \\"\\""
}

PROBLEM:
{problem}

INFORMAL PROOF (context only — may be longer than what was formalised):
{proof}

LEAN SOURCE:
```lean
{lean}
```
"""

STATEMENT_FIX_PROMPT = """An independent audit or a mechanical fidelity check found the STATEMENT of this
Lean 4 skeleton unfaithful to the PROBLEM. Rewrite the skeleton so the statement
is faithful. Proofs may stay `sorry` (they are filled in a later phase); fix the
statement FIRST. An honest `sorry` on the right claim beats a complete proof of
the wrong claim. Do not silence the audit by renaming things. Keep `main` as the
name of the main theorem and keep the `Informal:` / `Cites:` doc lines. When the
findings name a lower bound (`1 ≤ n`, `0 < n`, `n ≠ 0`, …) the PROBLEM does not
state, drop it so `main` covers every value the problem quantifies over; keep it
only if the claim is false or undefined without it, and say why in `notes`.

{target}
{fidelity}
{citation_rule}
{output_contract}
PROBLEM:
{problem}

AUDIT FINDINGS:
{audit}

INFORMAL PROOF (context):
{proof}

CURRENT SOURCE:
```lean
{lean}
```
"""

HARNESS_TASK = """You are formalising a mathematical proof in Lean 4, inside this directory.

Files here:
- `Proof.lean`  — the file you must produce/repair (this is the deliverable).
- `TASK.md`     — the problem statement and the informal proof, for reference.
- `check.sh`    — the local checker wrapper (see below).
- `lean-toolchain`, `lakefile.lean` — the pinned project scaffold; leave them
  alone. The lakefile has no git dependencies, so never run `lake update` or
  `lake build`: Mathlib is already provided by a shared, prebuilt mirror.

This run requires a fresh `Proof.lean` whose bytes differ from the file present
when you started. If the current file is not yet a complete, faithful proof,
that means making real progress on it. If it already compiles cleanly with no
`sorry`, make a genuine, semantics-preserving improvement instead (for example
simplify a proof or improve its documentation/citation annotation), save the
file, and check the changed bytes. Merely inspecting or
compiling an unchanged file is a failed harness run.
Preserve the exact requested theorem statement.

How to work — you have a shell, so use it. Do not guess whether Lean accepts your
file. Run:

    ./check.sh               # checks Proof.lean; no errors and no output = accepted

`check.sh` runs `lean Proof.lean` for a core-only file and, for a file that
imports Mathlib, `lake env lean Proof.lean` inside the shared Mathlib mirror
(Lean 4.14.0 + Mathlib v4.14.0). `import Mathlib` is supported and recommended.
Iterate: edit `Proof.lean`, run `./check.sh`, read the first error (it is
usually the root cause), fix it with a targeted edit, and repeat until it is
clean. Do not rewrite the file from scratch on every failure. `#check @name` in
a scratch copy is a quick way to confirm that a lemma name exists here.

{target}
{fidelity}
{citation_rule}
When you are done, `./check.sh` must report no errors. A `declaration uses
'sorry'` warning means the proof is still incomplete; say so honestly. The
outer sandboxed checker re-checks the file after you finish. Finish by printing
a short summary: what the main theorem states, whether any `sorry` remains and
why, and which declarations carry which citations. Write no other files.
"""


def _fill_prompt_shared(text: str) -> str:
    return (
        text.replace("{fidelity}", STATEMENT_FIDELITY)
        .replace("{idioms}", LEAN_IDIOMS)
        .replace("{citation_rule}", CITATION_RULE)
        .replace("{target}", LEAN_TARGET)
        .replace("{output_contract}", OUTPUT_CONTRACT)
    )


TRANSLATE_PROMPT = _fill_prompt_shared(TRANSLATE_PROMPT)
PROVE_PROMPT = _fill_prompt_shared(PROVE_PROMPT)
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


def _claude_tool_command(block: dict[str, Any]) -> str:
    """One display line for a Claude tool call: its shell command, or tool and target."""
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    if tool_input.get("command"):
        return str(tool_input["command"])
    target = (
        tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") or ""
    )
    return f"{block.get('name') or 'Tool'} {target}".strip()


def _claude_tool_output(content: Any) -> str:
    """A Claude tool_result's content: a string, or a list of text blocks."""
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        ).strip()
    return str(content or "")


def _claude_usage(raw: dict[str, Any]) -> dict[str, int]:
    """Claude's final usage in Codex's shape, the one the monitor reads.

    Claude counts cache reads and writes apart from ``input_tokens``; Codex's
    ``input_tokens`` includes the cached input.
    """

    def count(name: str) -> int:
        try:
            return int(raw.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    cache_read = count("cache_read_input_tokens")
    cache_write = count("cache_creation_input_tokens")
    return {
        "input_tokens": count("input_tokens") + cache_read + cache_write,
        "cached_input_tokens": cache_read,
        "cache_write_input_tokens": cache_write,
        "output_tokens": count("output_tokens"),
    }


def _harness_conversation(workspace: Path, cached: dict[str, Any]) -> dict[str, Any]:
    """Normalize a harness log (Codex JSONL or Claude stream-json) into bounded UI events.

    Started items are replaced by their completed form, and a Claude tool call
    is paired with its result, so a command appears once with its command,
    full captured output and exit state. Non-JSON stderr is kept as runtime
    output instead of being silently discarded.
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
        elif etype == "assistant" and isinstance(event.get("message"), dict):
            # Claude Code stream-json (the claude and openclaude harnesses).
            for block in event["message"].get("content") or []:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text" and str(block.get("text") or "").strip():
                    item = {"type": "agent_message", "text": block["text"]}
                elif kind == "thinking" and str(block.get("thinking") or "").strip():
                    item = {"type": "reasoning", "text": block["thinking"]}
                elif kind == "tool_use":
                    # Its tool_result arrives in a later line, under this id.
                    item = {
                        "id": block.get("id"),
                        "type": "command_execution",
                        "command": _claude_tool_command(block),
                        "status": "in_progress",
                    }
                else:
                    continue
                item_id = str(item.pop("id", None) or f"event_{serial}")
                serial += 1
                if item_id not in items:
                    order.append(item_id)
                items[item_id] = item
        elif etype == "user" and isinstance(event.get("message"), dict):
            for block in event["message"].get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                item = items.get(str(block.get("tool_use_id") or ""))
                if item is not None:
                    item["aggregated_output"] = _claude_tool_output(block.get("content"))
                    item["status"] = "failed" if block.get("is_error") else "completed"
        elif etype == "result":
            # The final, authoritative total; per-message usage would double count.
            if isinstance(event.get("usage"), dict) and event["usage"]:
                usage = _claude_usage(event["usage"])
            if event.get("is_error") or str(event.get("subtype") or "success") != "success":
                errors = event.get("errors") if isinstance(event.get("errors"), list) else []
                text = (
                    str(event.get("result") or "").strip()
                    or "; ".join(str(error).strip() for error in errors if str(error).strip())
                    or str(event.get("subtype") or "Claude Code reported an error")
                )
                # The result usually repeats the last message: show it once.
                if not any(str(item.get("text") or "") == text for item in items.values()):
                    order.append(f"event_{serial}")
                    items[f"event_{serial}"] = {"type": "error", "text": text, "status": "failed"}
                    serial += 1

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
            if sorry_count or check.get("reports_sorry"):
                attempt_status = "incomplete"
            elif action == "compile":
                attempt_status = "compiled"
            else:
                attempt_status = "verified"
        elif not attempt_status:
            attempt_status = "failed"
        recorded = attempt.get("result_status")
        if isinstance(recorded, str) and recorded.strip():
            attempt_status = recorded.strip()
        probe = check.get("axioms") if isinstance(check.get("axioms"), dict) else None
        axioms_view = (
            {
                "ok": bool(probe.get("ok")),
                "decl": str(probe.get("decl") or "")[:200],
                "axioms": [
                    str(name)[:120]
                    for name in (probe.get("axioms") or [])
                    if isinstance(name, str)
                ][:20],
            }
            if probe is not None
            else None
        )
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
                "axioms": axioms_view,
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
        "scope": str(cached.get("scope") or "proof")[:20],
        "proofs_attempted": cached.get("proofs_attempted") is not False,
        "status_label": str(cached.get("status_label") or "")[:200],
        "targets": [str(item)[:80] for item in (cached.get("targets") or [])][:40]
        if isinstance(cached.get("targets"), list) else [],
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
    """Write the pinned toolchain and a dependency-free lakefile.

    The lakefile deliberately never declares ``require mathlib from git``:
    running Lake in a per-run directory would otherwise clone and build
    Mathlib. Authoritative checks go through the broker; harness agents use
    the ``check.sh`` wrapper, which borrows the shared prebuilt mirror.
    """
    del uses_mathlib  # The scaffold is identical for core and Mathlib files.
    ldir.mkdir(parents=True, exist_ok=True)
    (ldir / "lean-toolchain").write_text(DEFAULT_TOOLCHAIN + "\n", encoding="utf-8")
    lake = (
        "import Lake\nopen Lake DSL\n\n"
        "package «proof» where\n"
        "  leanOptions := #[⟨`pp.unicode.fun, true⟩]\n\n"
        "@[default_target]\n"
        "lean_lib Proof where\n"
        "  roots := #[`Proof]\n"
    )
    (ldir / "lakefile.lean").write_text(lake, encoding="utf-8")


def _harness_mathlib_mirror() -> Path | None:
    """The shared prebuilt Mathlib mirror for harness-local advisory checks.

    This is deliberately separate from ``_mathlib_project()``: authoritative
    checks never use a local project. The mirror only lets a coding agent get
    fast feedback before the sandboxed broker re-checks its artifact.
    """
    candidates = [
        os.environ.get("AGENT_MONITOR_MATHLIB_PROJECT") or "",
        str(Path.home() / ".cache" / "agent-monitor" / "mathlib-v4.14.0"),
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if (path / "lake-manifest.json").is_file() and (
            path / ".lake" / "packages" / "mathlib"
        ).is_dir():
            return path.resolve()
    return None


def _harness_lean_bin_dirs() -> list[str]:
    """Directories holding the user's elan shims (``lean``/``lake``)."""
    from agent_monitor.subprocess_env import server_elan_home

    dirs: list[str] = []
    elan_bin = Path(server_elan_home()) / "bin"
    if (elan_bin / "lean").exists():
        dirs.append(str(elan_bin))
    import shutil

    found = shutil.which("lean")
    if found:
        dirs.append(str(Path(found).parent))
    return list(dict.fromkeys(dirs))


def _write_harness_checker(ldir: Path) -> Path:
    """Install ``check.sh``: core files use ``lean``; Mathlib files use the mirror."""
    import shlex

    from agent_monitor.subprocess_env import server_elan_home

    mirror = _harness_mathlib_mirror()
    mirror_literal = shlex.quote(str(mirror) if mirror else "")
    script = (
        "#!/bin/sh\n"
        "# Advisory local Lean check for the Formal harness. The sandboxed broker\n"
        "# re-checks Proof.lean after the run; only that result is authoritative.\n"
        "set -eu\n"
        "# The server's toolchains: an agent may run with a private account HOME,\n"
        "# and elan's shims would otherwise install a toolchain under it.\n"
        f"ELAN_HOME={shlex.quote(server_elan_home())}\n"
        "export ELAN_HOME\n"
        "here=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
        "file=${1:-Proof.lean}\n"
        "case \"$file\" in /*) ;; *) file=\"$here/$file\" ;; esac\n"
        f"mirror={mirror_literal}\n"
        "if grep -Eq '^[[:space:]]*import[[:space:]]+Mathlib' \"$file\"; then\n"
        "  if [ -z \"$mirror\" ] || [ ! -d \"$mirror\" ]; then\n"
        "    echo 'check.sh: no local Mathlib mirror; submit the file for the sandboxed check' >&2\n"
        "    exit 3\n"
        "  fi\n"
        "  cd \"$mirror\" && exec lake env lean \"$file\"\n"
        "fi\n"
        "cd \"$here\" && exec lean \"$file\"\n"
    )
    # A previous harness could have left a symlink named check.sh; replace
    # the directory entry instead of writing through it.
    directory = os.open(
        Path(ldir), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        try:
            os.unlink("check.sh", dir_fd=directory)
        except FileNotFoundError:
            pass
        descriptor = os.open(
            "check.sh",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o755,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(script)
    finally:
        os.close(directory)
    return Path(ldir) / "check.sh"


def _strip_lean_fences(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:lean|lean4)?\s*(.*?)```", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    return text


_FENCE_RE = re.compile(
    r"(?m)^[ \t]*```[ \t]*(?P<tag>[A-Za-z0-9_+-]*)[^\n]*\n(?P<body>.*?)^[ \t]*```[ \t]*$",
    re.S,
)
_LEAN_DECL_HINT_RE = re.compile(r"\b(?:theorem|lemma|example|def|abbrev)\b")


_LEAN_FENCE_OPEN_RE = re.compile(r"(?m)^[ \t]*```[ \t]*(?i:lean4?)(?![\w])[^\n]*\n")
_FENCE_CLOSE_LINE_RE = re.compile(r"(?m)^[ \t]*```[ \t]*$")


def _lean_block_spans(text: str) -> list[tuple[int, int, int, int]]:
    """``(open, body_start, body_end, close_end)`` for each ```lean fence.

    A fence closes at the first ``` line OUTSIDE Lean comments and strings,
    so a markdown example inside a doc comment (idiomatic in Mathlib-style
    docs) does not truncate the file. If Lean text never closes (an
    unterminated comment), the first plain fence line closes it; a reply cut
    off inside the block runs to the end of the text.
    """
    raw = str(text or "")
    spans: list[tuple[int, int, int, int]] = []
    position = 0
    while True:
        opened = _LEAN_FENCE_OPEN_RE.search(raw, position)
        if opened is None:
            break
        body_start = opened.end()
        tail = raw[body_start:]
        close = _FENCE_CLOSE_LINE_RE.search(_lean_executable_text(tail))
        if close is None:
            close = _FENCE_CLOSE_LINE_RE.search(tail)
        if close is None:
            spans.append((opened.start(), body_start, len(raw), len(raw)))
            break
        spans.append(
            (opened.start(), body_start, body_start + close.start(), body_start + close.end())
        )
        position = body_start + close.end()
    return spans


def _extract_lean_block(text: str) -> str | None:
    """Return the Lean file from a ```lean fence, if the reply has one.

    Several Lean fences can appear (a snippet in prose and the full file);
    the longest fence that contains a declaration is the file. An unclosed
    final fence (a truncated reply) still yields its body so the checker can
    report the damage instead of the parser discarding the whole attempt.
    """
    raw = str(text or "")
    candidates = [raw[body:end] for _open, body, end, _close in _lean_block_spans(raw)]
    usable = [body for body in candidates if _LEAN_DECL_HINT_RE.search(body)]
    if not usable:
        return None
    return max(usable, key=len).strip("\n")


def _without_lean_blocks(text: str) -> str:
    """The reply with every ```lean block cut out (where the metadata lives)."""
    raw = str(text or "")
    pieces: list[str] = []
    position = 0
    for opened, _body, _end, close in _lean_block_spans(raw):
        pieces.append(raw[position:opened])
        position = close
    pieces.append(raw[position:])
    return "".join(pieces)


def _json_object_candidates(text: str) -> list[str]:
    """Balanced top-level ``{...}`` spans, string-aware, in reply order."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"' and depth:
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : index + 1])
                start = -1
    return spans


def _extract_json_object(text: str) -> dict[str, Any]:
    """Robust JSON-object extraction for Lean-side replies.

    Unlike ``proof_graph._extract_json`` this never mistakes a ```lean fence
    for the JSON payload, tries strict parsing before any TeX-escape repair
    (the repair can rewrite a JSON ``\\n`` that precedes ``exists``/``eq``),
    and scans balanced objects when prose surrounds the JSON.
    """
    from agent_monitor.proof_graph import _repair_latex_json_escapes

    raw = str(text or "").strip()
    blobs: list[str] = []
    for match in _FENCE_RE.finditer(raw):
        if match.group("tag").lower() in {"json", ""}:
            blobs.append(match.group("body").strip())
    blobs.append(raw)
    for blob in blobs:
        spans = [blob] if blob.startswith("{") else []
        spans += _json_object_candidates(blob)
        for span in spans:
            for candidate in (span, _repair_latex_json_escapes(span)):
                try:
                    value = json.loads(candidate)
                except (json.JSONDecodeError, RecursionError, ValueError):
                    continue
                if isinstance(value, dict):
                    return value
    raise ValueError("model response contained no JSON object")


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


# ── Lean command structure (statement lock, declarations, axioms probe) ──
#
# A Lean 4 command need not start in column 0: `open X in instance …` may be
# indented under a theorem, and `def a := 1 def b := 2` is two commands on one
# line. Reserved command keywords cannot occur inside terms or tactic blocks,
# so the splitter cuts on those tokens wherever they appear in executable text
# (comments and strings blanked) instead of trusting column-0 lines. A keyword
# the splitter does not know is absorbed into the previous command, so the
# statement lock is only a fast, explainable pre-compile screen; the Lean-side
# statement fingerprint (``_statement_fingerprint``) is the semantic check.
_COMMAND_KEYWORDS = frozenset({
    "theorem", "def", "abbrev", "instance", "structure", "inductive",
    "class", "example", "axiom", "opaque", "namespace", "section", "end",
    "variable", "universe", "notation", "infix", "infixl", "infixr", "prefix",
    "postfix", "macro", "macro_rules", "syntax", "elab", "elab_rules",
    "attribute", "import", "mutual", "export", "initialize",
    "builtin_initialize", "run_cmd", "run_elab", "run_meta",
    "declare_syntax_cat", "include", "omit", "unif_hint", "binder_predicate",
    "deriving", "add_decl_doc", "simproc", "dsimproc",
    "simproc_decl", "dsimproc_decl", "open", "set_option",
})
# Batteries/Mathlib commands: reserved only when Mathlib is imported (a
# core-only file may use these words as ordinary identifiers, and cannot use
# them as commands without Mathlib).
_MATHLIB_COMMAND_KEYWORDS = frozenset({
    "lemma", "irreducible_def", "alias", "notation3", "variable?",
    "initialize_simps_projections", "library_note", "assert_not_exists",
    "suppress_compilation", "register_simp_attr", "mk_iff_of_inductive_prop",
})
_HASH_COMMANDS = frozenset({
    "#eval", "#exit", "#print", "#check", "#check_failure", "#reduce", "#synth",
    "#guard", "#guard_msgs", "#guard_expr", "#help", "#lint", "#find", "#simp",
    "#norm_num", "#conv", "#whnf", "#time", "#where", "#version",
    "#adaptation_note", "#count_heartbeats", "#explode", "#kernel_reduce",
})
_COMMAND_MODIFIERS = frozenset({
    "private", "protected", "noncomputable", "partial", "unsafe", "nonrec",
    "local", "scoped",
})
_WORD_TOKEN_RE = re.compile(r"(?<![\w.'«!?])(variable\?|[A-Za-z_][A-Za-z_]*)(?![\w'»!?])")
_HASH_TOKEN_RE = re.compile(r"(?m)^[ \t]*(#[a-z_]+)(?![\w'])")
_IN_TOKEN_RE = re.compile(r"(?<![\w.'«!?])in(?![\w'»!?])")
_TRAILING_IN_RE = re.compile(r"(?<![\w.'«!?])in\s*$")
_NON_SPACE_RE = re.compile(r"\S+")
_DECLARATION_KINDS = frozenset({
    "theorem", "lemma", "def", "abbrev", "instance", "structure", "inductive",
    "class", "axiom", "opaque", "irreducible_def",
})
_DECL_NAME_RE = re.compile(r"(?:«[^»\n]*»|[^\s:({\[⦃«])+")
# Proof-only options may change between rounds without touching the statement.
_LOCK_EXEMPT_OPTION_RE = re.compile(
    r"^set_option\s+(?:maxHeartbeats|maxRecDepth|synthInstance\.maxHeartbeats|linter\.\S+)\b"
)


def _command_keywords(source: str) -> frozenset[str]:
    if _detect_mathlib(source):
        return _COMMAND_KEYWORDS | _MATHLIB_COMMAND_KEYWORDS
    return _COMMAND_KEYWORDS


def _attribute_spans(executable: str) -> tuple[list[tuple[int, int]], dict[int, int]]:
    """Bracket spans of ``@[…]`` and ``attribute […]`` (keywords there are attributes).

    Returns every span plus a map from the end of each ``@[…]`` span to its
    start, used to walk from a keyword back to its command's attributes.
    """
    spans: list[tuple[int, int]] = []
    at_spans: dict[int, int] = {}
    for match in re.finditer(r"@\[|(?<![\w.'«!?])attribute\s*\[", executable):
        bracket = match.end() - 1
        depth = 0
        index = bracket
        while index < len(executable):
            char = executable[index]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        end = min(len(executable), index + 1)
        if match.group(0) == "@[":
            spans.append((match.start(), end))
            at_spans[end] = match.start()
        else:
            spans.append((bracket, end))
    return spans, at_spans


def _in_spans(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def _in_form_target(executable: str, position: int) -> int | None:
    """For ``open …``/``set_option …`` at ``position``: where the ``… in`` body starts."""
    line_end = executable.find("\n", position)
    if line_end < 0:
        line_end = len(executable)
    word = _NON_SPACE_RE.match(executable, position)
    match = _IN_TOKEN_RE.search(executable, word.end() if word else position, line_end)
    return match.end() if match else None


def _starts_command(
    executable: str,
    position: int,
    keywords: frozenset[str],
    spans: list[tuple[int, int]],
    depth: int = 0,
) -> bool:
    """Does a command begin at ``position`` (after whitespace, attributes, modifiers)?"""
    if depth > 8:
        return True
    index = position
    while True:
        while index < len(executable) and executable[index].isspace():
            index += 1
        if executable.startswith("@[", index):
            end = next((e for s, e in spans if s == index), None)
            if end is None:
                return True
            index = end
            continue
        if executable.startswith("#", index):
            word = re.match(r"#[a-z_]+", executable[index : index + 40])
            return bool(word and word.group(0) in _HASH_COMMANDS)
        word = _WORD_TOKEN_RE.match(executable, index)
        if not word:
            return False
        token = word.group(1)
        if token in _COMMAND_MODIFIERS:
            index = word.end()
            continue
        if token in {"open", "set_option"}:
            target = _in_form_target(executable, index)
            if target is None:
                return True
            return _starts_command(executable, target, keywords, spans, depth + 1)
        return token in keywords


def _command_start(
    executable: str, position: int, keyword: str, at_spans: dict[int, int]
) -> int:
    """Walk back from a keyword over modifiers and ``@[…]`` attributes."""
    modifiers = set(_COMMAND_MODIFIERS)
    if keyword in {"inductive", "abbrev"}:
        modifiers.add("class")
    if keyword == "instance":
        modifiers.add("deriving")
    start = position
    while True:
        index = start
        while index > 0 and executable[index - 1].isspace():
            index -= 1
        if index > 0 and executable[index - 1] == "]" and index in at_spans:
            start = at_spans[index]
            continue
        word = re.search(r"(?<![\w.'«!?])([A-Za-z_]+)$", executable[max(0, index - 40) : index])
        if word and word.group(1) in modifiers:
            start = index - len(word.group(1))
            continue
        return start


def _lean_commands(source: str) -> list[dict[str, Any]]:
    """Split a Lean file into top-level commands on executable text.

    Comments and strings are blanked first (offsets are preserved), so a
    commented-out command never counts. A command that ends in ``in``
    (``open … in``, ``variable … in``, ``attribute […] x in`` …) is a scoped
    prefix of the command that follows it and is merged into it.
    """
    source = source or ""
    executable = _lean_executable_text(source)
    keywords = _command_keywords(source)
    spans, at_spans = _attribute_spans(executable)
    candidates: list[tuple[int, str]] = []
    for match in _WORD_TOKEN_RE.finditer(executable):
        token = match.group(1)
        if token not in keywords or _in_spans(match.start(), spans):
            continue
        if token in {"open", "set_option"}:
            target = _in_form_target(executable, match.start())
            if target is not None and not _starts_command(
                executable, target, keywords, spans
            ):
                continue  # a term- or tactic-level `open … in` / `set_option … in`
        candidates.append((match.start(), token))
    for match in _HASH_TOKEN_RE.finditer(executable):
        if match.group(1) in _HASH_COMMANDS:
            candidates.append((match.start(1), match.group(1)))
    candidates.sort()
    points: list[dict[str, Any]] = []
    for position, token in candidates:
        start = _command_start(executable, position, token, at_spans)
        if points and start <= points[-1]["begin"]:
            continue  # `class inductive`, `deriving instance`: one command
        points.append({"begin": start, "keyword": token, "keyword_at": position})
    first = points[0]["begin"] if points else len(executable)
    if executable[:first].strip():
        points.insert(0, {"begin": 0, "keyword": "", "keyword_at": 0})
    raw_commands: list[dict[str, Any]] = []
    for index, point in enumerate(points):
        stop = points[index + 1]["begin"] if index + 1 < len(points) else len(executable)
        raw_commands.append({**point, "stop": stop})
    commands: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for command in raw_commands:
        text = executable[command["begin"] : command["stop"]]
        if _TRAILING_IN_RE.search(text) and command["keyword"]:
            pending.append(command)
            continue
        begin = pending[0]["begin"] if pending else command["begin"]
        entry = {
            "begin": begin,
            "body": command["begin"],
            "stop": command["stop"],
            "keyword": command["keyword"],
            "keyword_at": command["keyword_at"],
            "prefixes": [
                _normalize_lean_text(executable[p["begin"] : p["stop"]]) for p in pending
            ],
        }
        pending = []
        commands.append(entry)
    for command in pending:  # a dangling `… in` at end of file
        commands.append(
            {
                "begin": command["begin"],
                "body": command["begin"],
                "stop": command["stop"],
                "keyword": command["keyword"],
                "keyword_at": command["keyword_at"],
                "prefixes": [],
            }
        )
    for entry in commands:
        segment = executable[entry["begin"] : entry["stop"]]
        trimmed = segment.rstrip()
        entry["text"] = trimmed
        entry["body_text"] = executable[entry["body"] : entry["begin"] + len(trimmed)]
        entry["start"] = executable.count("\n", 0, entry["begin"]) + 1
        entry["end"] = entry["start"] + trimmed.count("\n")
        entry["line"] = executable.count("\n", 0, entry["keyword_at"]) + 1
    return commands


def _command_declaration(command: dict[str, Any]) -> tuple[str, str] | None:
    """``(kind, raw name)`` declared by a command, or None (anonymous/other)."""
    kind = command.get("keyword") or ""
    if kind not in _DECLARATION_KINDS:
        return None
    body = command["body_text"]
    rest = body[command["keyword_at"] - command["body"] + len(kind) :]
    if kind == "class":
        follow = re.match(r"\s*(inductive|abbrev)\b", rest)
        if follow:
            rest = rest[follow.end() :]
    rest = rest.lstrip()
    if kind == "instance":
        priority = re.match(r"\(\s*priority\s*:=[^)]*\)\s*", rest)
        if priority:
            rest = rest[priority.end() :]
        if not rest or rest[0] in ":([{⦃":
            return None
    match = _DECL_NAME_RE.match(rest)
    if not match:
        return None
    return kind, match.group(0)


def _split_lean_name(name: str) -> list[str]:
    """Name components, respecting ``«…»`` quoting (quotes removed)."""
    parts: list[str] = []
    for chunk in re.finditer(r"«([^»]*)»|([^.«»]+)", name or ""):
        parts.append(chunk.group(1) if chunk.group(1) is not None else chunk.group(2))
    return [part for part in parts if part]


def _qualified_declarations(
    source: str, commands: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Named declarations with namespace-qualified names, in file order.

    Scopes come from the same command split as the lock (``namespace``,
    ``section``, ``mutual`` and ``end`` at any column), so a declaration hidden
    on the same line as another command still gets the right name.
    """
    if commands is None:
        commands = _lean_commands(source)
    scopes: list[list[str]] = []
    out: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        keyword = command.get("keyword") or ""
        body = command["body_text"]
        if keyword == "namespace":
            match = re.match(r"\s*namespace\s+(\S+)", body[command["keyword_at"] - command["body"] :])
            scopes.append(_split_lean_name(match.group(1)) if match else [])
            continue
        if keyword in {"section", "mutual"}:
            scopes.append([])
            continue
        if keyword == "end":
            if scopes:
                scopes.pop()
            continue
        declared = _command_declaration(command)
        if declared is None:
            continue
        kind, raw = declared
        if kind == "class" and re.match(
            r"\s*class\s+inductive\b", body[command["keyword_at"] - command["body"] :]
        ):
            kind = "inductive"
        parts = _split_lean_name(raw)
        if parts[:1] == ["_root_"]:
            qualified_parts = parts[1:]
        else:
            qualified_parts = [part for scope in scopes for part in scope] + parts
        qualified = ".".join(qualified_parts)
        if not qualified:
            continue
        out.append(
            {
                "kind": kind,
                "name": ".".join(parts),
                "raw_name": raw,
                "line": command["line"],
                "qualified": qualified,
                "command": index,
            }
        )
    return out


def _signature_end(text: str) -> int:
    """Offset where a declaration's statement ends (its proof begins).

    The statement ends at the first depth-0 ``:=`` that does not belong to a
    ``let``/``have`` binder inside the statement, at equation-compiler
    alternatives (``| pat => …`` starting a line, unless the statement itself
    opened a ``match``/``fun``), or at ``where``.
    """
    depth = 0
    pending_binders = 0
    alternatives_in_statement = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in "([{⟨⦃":
            depth += 1
        elif char in ")]}⟩⦄":
            depth = max(0, depth - 1)
        elif depth == 0:
            if text.startswith(":=", index):
                if pending_binders:
                    pending_binders -= 1
                    index += 2
                    continue
                return index
            if char == "|" and not alternatives_in_statement:
                # `| pat => …` alternatives start a line; an absolute value
                # `|x| …` has no space after its bar.
                line_start = text.rfind("\n", 0, index) + 1
                line_end = text.find("\n", index)
                line_text = text[index : line_end if line_end >= 0 else length]
                if (
                    not text[line_start:index].strip()
                    and re.match(r"\|\s", line_text)
                    and "=>" in line_text
                ):
                    return index
            if char == "λ":
                alternatives_in_statement = True
            elif char.isalpha() or char == "_":
                previous = text[index - 1] if index else " "
                word_match = re.match(r"[^\W\d][\w'!?]*", text[index:])
                if word_match and not (previous.isalnum() or previous in "_.'«!?"):
                    word = word_match.group(0)
                    if word in {"let", "have", "letI", "haveI", "let_fun"}:
                        pending_binders += 1
                    elif word in {"match", "fun"}:
                        alternatives_in_statement = True
                    elif word == "where":
                        return index
                    index += len(word)
                    continue
        index += 1
    return length


def _normalize_lean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _statement_lock(source: str, main_decl: str | None = None) -> dict[str, Any] | None:
    """Fingerprint (textually) what fixes the meaning of the main theorem.

    The lock covers (1) the main declaration's statement, including any
    ``… in`` prefixes scoped to it, and (2) every command above it except
    theorem/lemma/example declarations (with their own scoped prefixes),
    imports and proof-only options — definitions, ``open``, ``variable``,
    notation, instances, attributes and scoping commands can all change what
    the statement means. Helper lemmas may be added, removed or restated
    freely; content after ``main`` cannot affect its statement. The session
    adds a Lean-computed fingerprint (``fingerprint``) on top of this.
    """
    wanted = str(main_decl or "main").strip() or "main"
    commands = _lean_commands(source)
    theorems = [
        decl for decl in _qualified_declarations(source, commands)
        if decl.get("kind") in {"theorem", "lemma"}
    ]
    wanted_key = ".".join(_split_lean_name(wanted))
    target = next(
        (d for d in theorems if wanted_key in {d["qualified"], d["name"]}), None
    ) or next(
        (
            d for d in theorems
            if d["qualified"].split(".")[-1] == wanted_key.split(".")[-1]
        ),
        None,
    )
    if target is None:
        return None
    main_index = int(target["command"])
    main_command = commands[main_index]
    body = main_command["body_text"]
    cut = _signature_end(body)
    prefixes = [
        text for text in main_command["prefixes"]
        if not _LOCK_EXEMPT_OPTION_RE.match(text)
    ]
    signature = _normalize_lean_text(" ".join(prefixes + [body[:cut]]))
    raw_signature = (source or "")[main_command["body"] : main_command["body"] + cut]
    if prefixes:
        raw_signature = "\n".join(prefixes) + "\n" + raw_signature
    preamble: list[str] = []
    for command in commands[:main_index]:
        keyword = command["keyword"]
        text = _normalize_lean_text(command["text"])
        if not text or keyword in {"theorem", "lemma", "example", "import"}:
            continue
        if keyword == "set_option" and not command["prefixes"] and _LOCK_EXEMPT_OPTION_RE.match(text):
            continue
        preamble.append(text)
    signature_sha = hashlib.sha256(
        (target["qualified"] + "\n" + signature).encode("utf-8")
    ).hexdigest()
    preamble_sha = hashlib.sha256("\n".join(preamble).encode("utf-8")).hexdigest()
    return {
        "main_decl": target["qualified"],
        "name": target["name"],
        "signature": raw_signature.rstrip(),
        "signature_sha256": signature_sha,
        "preamble_sha256": preamble_sha,
        "preamble_commands": len(preamble),
        "sha256": hashlib.sha256(
            (signature_sha + preamble_sha).encode("ascii")
        ).hexdigest(),
    }


def _lock_violation(lock: dict[str, Any] | None, source: str) -> str:
    """Explain how ``source`` breaks a statement lock ("" when it holds)."""
    if not lock:
        return ""
    current = _statement_lock(source, str(lock.get("main_decl") or "main"))
    name = str(lock.get("main_decl") or "main")
    if current is None or current["main_decl"] != name:
        return f"the locked main theorem `{name}` is missing or was renamed"
    if current["signature_sha256"] != lock.get("signature_sha256"):
        return f"the statement of the locked theorem `{name}` was changed"
    if current["preamble_sha256"] != lock.get("preamble_sha256"):
        return (
            f"a definition, instance, `open`, `variable`, notation, attribute, "
            f"option or namespace/section command above `{name}` was added, "
            "removed or edited (only theorem/lemma declarations may change there)"
        )
    return ""


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
    """Parse a Lean reply: a ```lean fence plus small JSON metadata.

    Backward compatible with the older contract (Lean escaped inside a JSON
    ``lean`` string) and with a bare Lean dump. ``uses_mathlib`` is always
    derived from the source's imports; a model's own flag is never trusted,
    because a mismatch would route the file to the wrong checker profile.
    """
    text = str(content or "")
    lean_block = _extract_lean_block(text)
    data: dict[str, Any] = {}
    if lean_block is not None:
        remainder = _without_lean_blocks(text)
        try:
            data = _extract_json_object(remainder)
        except ValueError:
            data = {}
        lean = lean_block
        notes_default = ""
    else:
        try:
            data = _extract_json_object(text)
        except ValueError:
            data = {}
        lean = str(data.get("lean") or data.get("code") or "") if data else ""
        if lean.strip():
            lean = _strip_lean_fences(lean)
            notes_default = ""
        else:
            lean = _strip_lean_fences(text)
            if not _LEAN_DECL_HINT_RE.search(lean):
                if data:
                    raise ValueError("model returned empty Lean source")
                raise ValueError(
                    "model response contained neither JSON nor recognisable Lean"
                )
            data = {}
            notes_default = "model returned raw Lean (no metadata block)"
    lean = _normalize_lean_imports(lean)
    if not lean.strip():
        raise ValueError("model returned empty Lean source")
    cites = _merge_citations(
        _anchor_citations(
            _normalize_citations(data.get("citations") or data.get("references")), lean
        ),
        _citations_from_lean(lean),
    )
    return {
        "title": str(data.get("title") or "")[:200],
        "lean": lean,
        "uses_mathlib": _detect_mathlib(lean),
        "notes": str(data.get("notes") or notes_default)[:500],
        "main_decl": str(data.get("main_decl") or "")[:80],
        "citations": cites,
    }


def _lean_is_letter_like(char: str) -> bool:
    """Lean 4 ``isLetterLike`` (Greek except λ/Π/Σ, Coptic, letterlike blocks)."""
    code = ord(char)
    return (
        (0x3B1 <= code <= 0x3C9 and code != 0x3BB)
        or (0x391 <= code <= 0x3A9 and code not in (0x3A0, 0x3A3))
        or 0x3CA <= code <= 0x3FB
        or 0x1F00 <= code <= 0x1FFE
        or 0x2100 <= code <= 0x214F
        or 0x1D49C <= code <= 0x1D59F
    )


def _lean_is_id_first(char: str) -> bool:
    return (char.isascii() and char.isalpha()) or char == "_" or _lean_is_letter_like(char)


def _lean_is_id_rest(char: str) -> bool:
    """Lean 4 ``isIdRest``: ASCII alphanumerics, ``_ ' ! ?``, letterlike, subscripts."""
    if (char.isascii() and char.isalnum()) or char in "_'!?":
        return True
    code = ord(char)
    return (
        _lean_is_letter_like(char)
        or 0x2080 <= code <= 0x2089
        or 0x2090 <= code <= 0x209C
        or 0x1D62 <= code <= 0x1D6A
    )


# Keywords after which Lean parses a string literal as an interpolated string
# (``{…}`` inside it is code).  Any identifier ending in ``!`` (``s!``,
# ``m!``, ``f!``, ``panic!`` …) does the same.
_LEAN_INTERPOLATING_KEYWORDS = frozenset(
    {"throwError", "throwErrorAt", "dbg_trace", "trace", "logInfo", "logWarning", "logError"}
)
_LEAN_MAX_INTERPOLATION_DEPTH = 32
# A character literal directly after one of these is unambiguous: no Lean
# token (core, Mathlib or user notation) can have absorbed its opening quote.
_LEAN_SIMPLE_CHAR_LITERAL_RE = re.compile(r"'[A-Za-z0-9 _]'")


class _LeanLexer:
    """Blank Lean comments, strings and character literals like Lean's tokenizer.

    Follows Lean 4's own rules closely enough that code cannot hide inside
    what the text screens take for a string (or vice versa): a ``'`` at a
    token start is a character literal (``'"'`` included), identifiers use
    Lean's ``isIdRest``, ``«…»`` names, ``r"…"``/``r#"…"#`` raw strings,
    nested block comments and interpolated strings (``s!"…{code}…"``) are
    handled.  Constructs whose reading depends on the notation table in force
    are recorded as ``anomalies`` so the fidelity screen can refuse them.
    """

    def __init__(self, source: str) -> None:
        self.src = source
        self.n = len(source)
        self.out = list(source)
        self.anomalies: list[tuple[str, int]] = []
        # 1 at every position inside an identifier other than its first
        # character: a keyword there (`h1elab`, `Foo.elab`) is not a command.
        self.inside_ident = bytearray(self.n)

    def run(self) -> str:
        self._code(0, until_brace=False, depth=0)
        return "".join(self.out)

    def _blank(self, start: int, end: int) -> None:
        out = self.out
        for index in range(start, min(end, self.n)):
            if out[index] != "\n":
                out[index] = " "

    def _block_comment(self, start: int) -> int:
        src, n = self.src, self.n
        index, depth = start + 2, 1
        while index < n and depth:
            if src.startswith("/-", index):
                depth += 1
                index += 2
            elif src.startswith("-/", index):
                depth -= 1
                index += 2
            else:
                index += 1
        return index

    def _char_literal(self, start: int) -> int | None:
        """End index of a well-formed character literal at ``start``, if any."""
        src, n = self.src, self.n
        index = start + 1
        if index >= n:
            return None
        if src[index] == "\\":
            escape = src[index + 1] if index + 1 < n else ""
            if escape == "x":
                index += 4
            elif escape == "u" and index + 2 < n and src[index + 2] == "{":
                close = src.find("}", index + 3)
                if close < 0:
                    return None
                index = close + 1
            elif escape == "u":
                index += 6
            else:
                index += 2
        else:
            index += 1
        if index < n and src[index] == "'":
            return index + 1
        return None

    def _raw_string_end(self, start: int) -> int | None:
        """End of ``r"…"``/``r#"…"#`` starting at ``start`` (the ``r``), if one."""
        src, n = self.src, self.n
        index = start + 1
        hashes = 0
        while index < n and src[index] == "#":
            hashes += 1
            index += 1
        if index >= n or src[index] != '"':
            return None
        closing = '"' + "#" * hashes
        end = src.find(closing, index + 1)
        return n if end < 0 else end + len(closing)

    def _identifier(self, start: int) -> int:
        src, n = self.src, self.n
        index = start
        while True:
            if index < n and src[index] == "«":
                close = src.find("»", index + 1)
                index = n if close < 0 else close + 1
            elif index < n and _lean_is_id_first(src[index]):
                index += 1
                while index < n and _lean_is_id_rest(src[index]):
                    index += 1
            else:
                break
            if index + 1 < n and src[index] == "." and (
                src[index + 1] == "«" or _lean_is_id_first(src[index + 1])
            ):
                index += 1
                continue
            break
        return index

    def _string(self, start: int, *, interpolated: bool, depth: int) -> int:
        src, n = self.src, self.n
        self._blank(start, start + 1)
        index = start + 1
        while index < n:
            char = src[index]
            if char == "\\":
                self._blank(index, index + 2)
                index += 2
                continue
            if char == '"':
                self._blank(index, index + 1)
                return index + 1
            if interpolated and char == "{":
                self._blank(index, index + 1)
                if depth >= _LEAN_MAX_INTERPOLATION_DEPTH:
                    self.anomalies.append(("interpolated_string", index))
                    return n
                close = self._code(index + 1, until_brace=True, depth=depth + 1)
                if close >= n:
                    return n
                self._blank(close, close + 1)
                index = close + 1
                continue
            self._blank(index, index + 1)
            index += 1
        return n

    def _code(self, start: int, *, until_brace: bool, depth: int) -> int:
        src, n = self.src, self.n
        index = start
        previous = "space"
        last_ident = ""
        braces = 0
        while index < n:
            char = src[index]
            if src.startswith("--", index):
                end = src.find("\n", index)
                end = n if end < 0 else end
                self._blank(index, end)
                index, previous = end, "space"
                continue
            if src.startswith("/-", index):
                end = self._block_comment(index)
                self._blank(index, end)
                index, previous = end, "space"
                continue
            if char.isspace():
                index += 1
                previous = "space"
                continue
            if char == '"':
                # `last_ident` is the previous token when it was an identifier
                # (whitespace and comments in between do not reset it).
                interpolated = last_ident.endswith("!") or last_ident in _LEAN_INTERPOLATING_KEYWORDS
                if interpolated:
                    self.anomalies.append(("interpolated_string", index))
                elif last_ident == "#print":
                    self.anomalies.append(("print_string", index))
                index = self._string(index, interpolated=interpolated, depth=depth)
                previous, last_ident = "string", ""
                continue
            if char == "'" and index + 1 < n and src[index + 1] != "'":
                end = self._char_literal(index)
                if end is not None:
                    if previous == "symbol" and not _LEAN_SIMPLE_CHAR_LITERAL_RE.fullmatch(
                        src[index:end]
                    ):
                        # A notation token ending in `'` (Mathlib `⁻¹'`, a
                        # user `notation`) would read this differently.
                        self.anomalies.append(("ambiguous_char_literal", index))
                    self._blank(index, end)
                    index, previous, last_ident = end, "char", ""
                    continue
                index += 1
                previous, last_ident = "symbol", ""
                continue
            if char.isdigit():
                index += 1
                while index < n and (
                    (src[index].isascii() and (src[index].isalnum() or src[index] == "_"))
                    or (src[index] == "." and index + 1 < n and src[index + 1].isdigit())
                ):
                    index += 1
                previous, last_ident = "number", ""
                continue
            if char == "r":
                end = self._raw_string_end(index)
                if end is not None:
                    self._blank(index, end)
                    index, previous, last_ident = end, "string", ""
                    continue
            if char == "«" or _lean_is_id_first(char):
                end = self._identifier(index)
                self.inside_ident[index + 1 : end] = b"\x01" * max(0, end - index - 1)
                last_ident = src[index:end]
                if index and src[index - 1] == "#":
                    last_ident = "#" + last_ident
                index, previous = end, "ident"
                continue
            if until_brace:
                if char == "{":
                    braces += 1
                elif char == "}":
                    if braces == 0:
                        return index
                    braces -= 1
            index += 1
            previous, last_ident = "symbol", ""
        return n


def _lean_lex(lean: str) -> tuple[str, list[tuple[str, int]]]:
    lexer = _LeanLexer(lean or "")
    text = lexer.run()
    return text, lexer.anomalies


def _lean_executable_text(lean: str) -> str:
    """Blank Lean comments/strings/char literals, preserving line numbers."""
    return _lean_lex(lean)[0]


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
    "You are a Lean 4 formalization expert targeting exactly Lean 4.14.0 with "
    "Mathlib v4.14.0 (December 2024). Preserve the exact mathematical claim, "
    "follow the requested output contract, and return only the requested "
    "content without extra commentary."
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
    """The Claude Code account alias to route to.

    Records and provider telemetry carry resolved names such as
    ``claude-opus-5-5``; they map back to their alias (``opus``) instead of
    silently falling back to Sonnet.
    """
    from agent_monitor.jobs import _canonical_claude_continuation_model as canonical

    return (
        canonical(model)
        or canonical(env.get("AGENT_MONITOR_CLAUDE_MODEL"))
        or "sonnet"
    )


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
    from agent_monitor.settings import restrict_account_base_urls, resolved_user_env

    if (user or {}).get("guest") or (run_record or {}).get("guest"):
        from agent_monitor import sponsored_kimi_client

        if os.environ.get("AGENT_MONITOR_GUEST_HOSTED_KIMI", "1").lower() in {
            "0", "false", "off", "no"
        } or not sponsored_kimi_client.configured():
            raise ValueError("Guest Formal and DAG generation require the included Kimi service")
        # Derived guest work receives only an expiring proxy token. It cannot
        # inherit operator credentials, account subscriptions, or custom URLs.
        return {
            **sponsored_kimi_client.issue_credentials(
                engine="formal",
                client_id=(user or {}).get("id") or (run_record or {}).get("owner_id"),
                cache_key=str((run_record or {}).get("run_id") or "guest-formal"),
                # A Formal action chains calls of up to 630 s each
                # (kimi_k3.SPONSORED_TIMEOUT_SECONDS; 1,260 s for one asked
                # again at lower effort) on one credential.
                minimum_ttl_seconds=1_500,
            ),
            "AGENT_MONITOR_GUEST_DERIVED": "1",
            "AGENT_MONITOR_MODEL": "kimi-k3",
            "AGENT_MONITOR_LLM_MAX_OUTPUT": "8192",
            "AGENT_MONITOR_USE_CODEX": "0",
            "AGENT_MONITOR_USE_CLAUDE": "0",
        }

    runtime_env = {
        name: os.environ[name]
        for name in _FORMAL_RUNTIME_ENV_NAMES
        if os.environ.get(name)
    }
    # SSRF guard: a non-admin account never chooses where the server sends its
    # Lean translation, audit and repair calls; only the operator's own bases
    # (or the providers' built-in endpoints) remain. The operator and admin
    # environments are unchanged.
    env = restrict_account_base_urls(dict(resolved_user_env(user) or {}), user)
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
        owner_id = (user or {}).get("id") or (run_record or {}).get("owner_id")
        if route != "api_key" and owner_id is not None:
            from agent_monitor.auth import may_use_own_subscriptions

            if not may_use_own_subscriptions(owner_id):
                # A member's runtime is always the API route: a setting stored
                # before subscriptions became administrator-only never counts.
                route = "api_key"
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
                # As for guests (above): it must outlast chained 630 s calls
                # (1,260 s for one asked again at lower effort).
                minimum_ttl_seconds=1_500,
            )
        )
    if route == "claude_subscription":
        from agent_monitor.jobs import _canonical_claude_continuation_model as canonical

        # The native Claude Code engine records the resolved model name
        # (claude-opus-5-5); the selection itself is in requested_model.
        recorded_model = canonical((run_record or {}).get("requested_model")) or canonical(
            (run_record or {}).get("model")
        )
        if recorded_model:
            env["AGENT_MONITOR_CLAUDE_MODEL"] = recorded_model
    from agent_monitor.run_tuning import from_record as apply_recorded_tuning

    apply_recorded_tuning(env, run_record)
    return env


# Authorship evidence travels on the model string returned by the provider call
# (see ``proof_graph.ObservedModel``).
from agent_monitor.proof_graph import ObservedModel as _ObservedModel


# The stop check of the Lean work in progress on this thread (generate's
# should_stop, or stop_when's): asked before each model call, Lean probe,
# compile, restore and persist.
_SHOULD_STOP: contextvars.ContextVar[Callable[[], bool] | None] = contextvars.ContextVar(
    "lean_verify_should_stop", default=None
)


class SessionStopped(Exception):
    """The run a Lean session works for is gone (deleted): the session makes
    no further model call or compile and persists nothing."""


def _stop_if_asked() -> None:
    should_stop = _SHOULD_STOP.get()
    if should_stop is not None and should_stop():
        raise SessionStopped("the run was deleted; the Lean session stopped")


def stop_when(should_stop: Callable[[], bool]) -> contextvars.Token:
    """Stop this thread's Lean work (raising :class:`SessionStopped` at its
    next model call, probe, compile or write) once ``should_stop`` answers
    True, until the returned token is passed to :func:`stop_reset`.  Threads
    started meanwhile (the Lean harness) are not affected."""
    return _SHOULD_STOP.set(should_stop)


def stop_reset(token: contextvars.Token) -> None:
    _SHOULD_STOP.reset(token)


def _chat(
    env: dict[str, str],
    model: str | None,
    prompt: str,
    timeout: float | None = None,
) -> tuple[str, str]:
    _stop_if_asked()
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
        usage_ledger.record_usage(
            model=str(result.model or selected),
            usage=usage_ledger.cli_usage(getattr(result, "usage", None), input_includes_cache=False),
            billing="subscription",
            cost_usd=getattr(result, "cost_usd", None),
        )
        content = str(result.text or "").strip()
        if not content:
            raise ValueError("Claude Code returned an empty Lean response")
        # Route every later repair to the selected alias; the resolved model
        # name is authorship evidence only (see proof_graph.ObservedModel).
        return _ObservedModel(selected, result.model), content
    if env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        from agent_monitor.proof_graph import _run_codex_prompt_env

        with tempfile.TemporaryDirectory(prefix="lean-codex-response-") as tmp:
            content = _run_codex_prompt_env(
                env, Path(tmp), prompt, model=model, timeout=legacy_timeout
            ).strip()
        if not content:
            raise ValueError("Codex returned an empty Lean response")
        return str(model or "codex"), content
    # A sponsored Kimi call ignores this timeout: _call_llm waits the broker's
    # own deadline (kimi_k3.SPONSORED_TIMEOUT_SECONDS), sends it as
    # X-Ansatze-Timeout and never replays an ambiguous failure. A reply that
    # reasoned to the output limit is asked once more, after the stop check.
    chosen, content, _provider = _call_llm(
        env, model, prompt, timeout=legacy_timeout, before_resend=_stop_if_asked
    )
    return chosen, content


def _terminate_process_group(proc: subprocess.Popen[str], *, force: bool = False) -> None:
    """Terminate a verifier and every Lake/Lean child it spawned."""
    from agent_monitor.process_control import terminate_descendants

    terminate_descendants(proc.pid, force=force)
    if os.name != "posix" and proc.poll() is not None:
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


def _lean_check_timeout() -> float:
    try:
        timeout_s = max(
            1, int(os.environ.get("AGENT_MONITOR_LEAN_CHECK_TIMEOUT", "100"))
        )
    except ValueError:
        timeout_s = 100
    return min(float(timeout_s), lean_broker.MAX_CLIENT_TIMEOUT)


_SORRY_WARNING_RE = re.compile(r"declaration uses 'sorry'")
# Lean's warning for `#exit`: elaboration stopped, the rest was never checked.
_EXIT_WARNING_RE = re.compile(r"using 'exit' to interrupt Lean")
# Lean prints `'name' depends on axioms: [..]` / `'name' does not depend on
# any axioms`; a name may itself contain primes (`foo'`), so the name match is
# lazy up to the quote that precedes the verdict.
_AXIOMS_LINE_RE = re.compile(
    r"'(?P<name>[^\n]+?)'\s+(?:depends on axioms:\s*\[(?P<axioms>[^\]]*)\]"
    r"|does not depend on any axioms)",
)
_PRIVATE_NAME_RE = re.compile(r"^_private\.(?:[^.]+\.)*?0\.")
# Every named declaration of these kinds is probed, not only `main`: a decoy
# of the same short name elsewhere (`A.main`) or a theorem the parser missed
# can then never stand in for the real one.
_PROBED_KINDS = frozenset({
    "theorem", "lemma", "def", "abbrev", "instance", "axiom", "opaque",
    "irreducible_def",
})
MAX_PROBED_DECLARATIONS = 200


def _output_reports_sorry(check: dict[str, Any]) -> bool:
    """Lean's own ``declaration uses 'sorry'`` warning, wherever it came from."""
    blob = str(check.get("stdout") or "") + "\n" + str(check.get("stderr") or "")
    return bool(_SORRY_WARNING_RE.search(blob))


def _output_reports_exit(check: dict[str, Any]) -> bool:
    """Lean's ``using 'exit' to interrupt Lean`` warning (``#exit``)."""
    blob = str(check.get("stdout") or "") + "\n" + str(check.get("stderr") or "")
    return bool(_EXIT_WARNING_RE.search(blob))


def _marked_output(output: str, begin: str, end: str) -> str | None:
    """The output lines strictly between this probe's own marker lines.

    The markers carry a fresh random nonce the checked file cannot know, so
    anything the file printed itself comes before ``begin``.  Returns None
    unless each marker appears exactly once, ``begin`` first: a file that
    stops elaboration (``#exit``) or swallows the probe never prints them.
    """
    lines = output.splitlines()

    def positions(marker: str) -> list[int]:
        return [
            index
            for index, line in enumerate(lines)
            if line.strip() == marker or line.rstrip().endswith(": " + marker)
        ]

    starts, ends = positions(begin), positions(end)
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return None
    return "\n".join(lines[starts[0] + 1 : ends[0]])


def _lean_name_literal(name: str) -> str:
    """Quote each component so any declared name is accepted by ``#print``."""
    parts = _split_lean_name(str(name))
    if not parts or any("»" in part or "\n" in part for part in parts):
        return ""
    return ".".join(f"«{part}»" for part in parts)


def _normalized_decl_name(name: str) -> str:
    """Compare Lean-printed and parsed names: no quoting, no private prefix."""
    text = _PRIVATE_NAME_RE.sub("", str(name or "").strip())
    return ".".join(_split_lean_name(text))


def _resolve_declared_main(source: str, main_decl: str) -> str | None:
    """The theorem a declared/locked ``main_decl`` names — exactly, or None."""
    theorems = [
        d for d in _qualified_declarations(source)
        if d["kind"] in {"theorem", "lemma"}
    ]
    wanted = _normalized_decl_name(main_decl)
    if not wanted:
        return None
    exact = [d for d in theorems if d["qualified"] == wanted]
    if len(exact) == 1:
        return exact[0]["qualified"]
    named = [d for d in theorems if d["name"] == wanted]
    if len(named) == 1:
        return named[0]["qualified"]
    short = [
        d for d in theorems
        if d["qualified"].split(".")[-1] == wanted.split(".")[-1]
    ]
    return short[0]["qualified"] if len(short) == 1 else None


def _probe_target(source: str, main_decl: str | None = None) -> str | None:
    """Fully qualified name of the declaration reported as the main theorem.

    Used to pick a target when none was locked (manual checks, harness runs):
    the theorem named ``main_decl``/``main`` when unambiguous, otherwise the
    last theorem. Soundness never depends on this choice — the axioms probe
    inspects every named declaration.
    """
    decls = _qualified_declarations(source)
    theorems = [d for d in decls if d["kind"] in {"theorem", "lemma"}]
    if not theorems:
        return None
    resolved = _resolve_declared_main(source, str(main_decl or "main"))
    return resolved or theorems[-1]["qualified"]


def _axioms_probe(
    source_text: str,
    *,
    uses_mathlib: bool,
    timeout: float,
    main_decl: str | None = None,
) -> dict[str, Any]:
    """Second broker check: ``#print axioms`` for every named declaration.

    Exit 0 plus the absence of ``sorry`` tokens is not proof of soundness: a
    macro or elaborator can admit a goal without the literal word. The kernel's
    own axiom report is the ground truth. Each probe line names its target
    from the root (``_root_.…``), so a declaration of the same short name in a
    namespace the file leaves open cannot answer for it, and the reports must
    be the last ones in the output, one per probed name, in order. Only
    ``propext``, ``Classical.choice`` and ``Quot.sound`` are accepted anywhere
    in the file. When the caller locked a main theorem (``main_decl``), it
    must exist under exactly that name; otherwise the probe fails closed.
    """
    decls = _qualified_declarations(source_text)

    def failure(error: str, decl: str = "") -> dict[str, Any]:
        return {
            "ok": False,
            "decl": decl,
            "axioms": [],
            "file_axioms": [],
            "unexpected": [],
            "offenders": [],
            "probed": 0,
            "error": error,
        }

    if main_decl:
        target = _resolve_declared_main(source_text, main_decl)
        if not target:
            return failure(
                f"the main theorem `{main_decl}` was not found under that name"
            )
    else:
        target = _probe_target(source_text)
        if not target:
            return failure("no theorem to probe")
    names = [target] + [
        d["qualified"] for d in decls
        if d["kind"] in _PROBED_KINDS and d["qualified"] != target
    ]
    names = list(dict.fromkeys(names))[:MAX_PROBED_DECLARATIONS]
    probed: list[tuple[str, str]] = []
    for name in names:
        literal = _lean_name_literal(name)
        if not literal:
            if name == target:
                return failure(f"cannot name `{target}` in a probe", target)
            continue
        probed.append((name, literal))
    # Bracket the reports with markers carrying a per-check nonce: output the
    # file printed itself (a forged report followed by `#exit`) can never
    # appear between them, and a file that stops the probe fails closed.
    nonce = secrets.token_hex(16)
    begin = f"AGENT_MONITOR_AXIOMS::{nonce}::begin"
    end = f"AGENT_MONITOR_AXIOMS::{nonce}::end"
    probe = (
        source_text.rstrip()
        + "\n\n"
        + f'#print "{begin}"\n'
        + "".join(f"#print axioms _root_.{literal}\n" for _name, literal in probed)
        + f'#print "{end}"\n'
    )
    probe_sha256 = hashlib.sha256(probe.encode("utf-8")).hexdigest()
    try:
        result = lean_broker.check_source(
            probe, uses_mathlib=uses_mathlib, timeout=timeout
        )
    except lean_broker.LeanBrokerError as exc:
        return failure(f"axioms probe could not run: {exc}", target)
    problems: list[str] = []
    if result.get("source_sha256") != probe_sha256:
        problems.append("broker did not attest the probe source")
    if str(result.get("sandbox") or "") not in LEAN_CHECK_TRUSTED_SANDBOXES:
        problems.append("probe sandbox attestation was not trusted")
    if result.get("status") != "completed" or result.get("exit_code") != 0:
        problems.append(
            "probe did not complete cleanly"
            + (f": {result.get('error')}" if result.get("error") else "")
        )
    if result.get("stdout_truncated") or result.get("stderr_truncated"):
        problems.append("probe output was truncated")
    output = str(result.get("stdout") or "") + "\n" + str(result.get("stderr") or "")
    section = _marked_output(output, begin, end)
    reports = list(_AXIOMS_LINE_RE.finditer(section)) if section is not None else []
    by_name: dict[str, list[str]] = {}
    if section is None:
        problems.append(
            f"no axiom report for `{target}`: the probe's own output markers are "
            "missing or out of order (the file stopped elaboration or rewrote the probe)"
        )
    elif len(reports) != len(probed):
        problems.append(
            f"no axiom report for `{target}` and every other probed declaration "
            f"(expected {len(probed)} report(s), found {len(reports)})"
        )
    else:
        for (name, _literal), report in zip(probed, reports):
            printed = _normalized_decl_name(report.group("name"))
            if printed != _normalized_decl_name(name):
                problems.append(
                    f"axiom report for `{printed}` where `{name}` was probed"
                )
                continue
            by_name[name] = [
                item.strip()
                for item in (report.group("axioms") or "").replace("\n", " ").split(",")
                if item.strip()
            ]
    if target not in by_name and not problems:
        problems.append(f"no axiom report for `{target}`")
    file_axioms = sorted({axiom for axioms in by_name.values() for axiom in axioms})
    unexpected = sorted(set(file_axioms) - STANDARD_AXIOMS)
    offenders = [
        name for name, axioms in by_name.items() if set(axioms) - STANDARD_AXIOMS
    ]
    if unexpected:
        problems.append(
            "non-standard axioms "
            + ", ".join(unexpected)
            + " (used by "
            + ", ".join(f"`{name}`" for name in offenders[:6])
            + ")"
        )
    return {
        "ok": not problems,
        "decl": target,
        "axioms": by_name.get(target, []),
        "file_axioms": file_axioms,
        "unexpected": unexpected,
        "offenders": offenders[:20],
        "probed": len(probed),
        "error": "; ".join(problems),
    }


# Lean-side statement fingerprint. Appended (with `import Lean` prepended) to
# a file that compiles, it walks the main theorem's TYPE and, transitively,
# every local constant that type mentions (definitions with their values,
# inductives with their constructors, instances picked by elaboration) and
# prints a structural hash row per constant. Two files whose main statements
# elaborate to the same term over the same local definitions get the same
# fingerprint, however the text is laid out; an instance, notation, `open`,
# `variable` or definition that changes what the statement means changes it.
# Only `_root_`-qualified names and dot notation are used, so declarations in
# the checked file cannot shadow the probe's own calls.
_FINGERPRINT_PROBE = """

-- agent-monitor statement fingerprint @@MARKER@@
run_cmd do
  let env ← _root_.Lean.MonadEnv.getEnv
  let out ← _root_.IO.getStdout
  let mut todo : _root_.Array _root_.Lean.Name := #[@@TARGET@@]
  let mut seen : _root_.Lean.NameSet := {}
  let mut rows : _root_.Array _root_.Lean.Json := #[]
  let mut fuel : _root_.Nat := 20000
  while !todo.isEmpty && fuel != 0 do
    fuel := fuel - 1
    let name := todo.back!
    todo := todo.pop
    if seen.contains name then continue
    seen := seen.insert name
    if (env.getModuleIdxFor? name).isSome then continue
    match env.find? name with
    | none =>
      rows := rows.push (_root_.Lean.Json.mkObj [("n", _root_.Lean.Json.str name.toString), ("k", _root_.Lean.Json.str "missing")])
    | some ci =>
      let (kind, value?, extra) : _root_.String × _root_.Option _root_.Lean.Expr × _root_.Array _root_.Lean.Name :=
        match ci with
        | .defnInfo d => ("def", some d.value, #[])
        | .thmInfo _ => ("theorem", none, #[])
        | .opaqueInfo d => ("opaque", some d.value, #[])
        | .axiomInfo _ => ("axiom", none, #[])
        | .inductInfo v => ("inductive", none, v.ctors.toArray)
        | .ctorInfo v => ("ctor", none, #[v.induct])
        | .recInfo _ => ("recursor", none, #[])
        | .quotInfo _ => ("quot", none, #[])
      let refs := ci.type.getUsedConstants ++ (match value? with | some v => v.getUsedConstants | none => #[]) ++ extra
      for r in refs do
        if (env.getModuleIdxFor? r).isNone && !seen.contains r then
          todo := todo.push r
      let fields : _root_.List (_root_.String × _root_.Lean.Json) :=
        [("n", _root_.Lean.Json.str name.toString), ("k", _root_.Lean.Json.str kind),
         ("th", _root_.Lean.Json.str ci.type.hash.toNat.repr),
         ("ts", _root_.Lean.Json.str ci.type.dbgToString.hash.toNat.repr),
         ("u", _root_.Lean.Json.str (_root_.String.intercalate "," (ci.levelParams.map (·.toString))))]
      let vfields : _root_.List (_root_.String × _root_.Lean.Json) := match value? with
        | some v => [("vh", _root_.Lean.Json.str v.hash.toNat.repr), ("vs", _root_.Lean.Json.str v.dbgToString.hash.toNat.repr)]
        | none => []
      rows := rows.push (_root_.Lean.Json.mkObj (fields ++ vfields))
  let status := if fuel == 0 then "truncated" else "ok"
  out.putStrLn ("AGENT_MONITOR_STATEMENT::@@MARKER@@::" ++ (_root_.Lean.Json.mkObj [("status", _root_.Lean.Json.str status), ("rows", _root_.Lean.Json.arr rows)]).compress)
"""
STATEMENT_FINGERPRINT_VERSION = 1


def _lean_name_term(name: str) -> str | None:
    """A Lean term building ``name`` component by component (no resolution)."""
    parts = _split_lean_name(name)
    if not parts:
        return None
    term = "_root_.Lean.Name.anonymous"
    for part in parts:
        if any(char in part for char in '"\\') or any(ord(char) < 32 for char in part):
            return None
        term = f'(_root_.Lean.Name.mkStr {term} "{part}")'
    return term


def _fingerprint_program(source: str, marker: str, target_term: str) -> str:
    return (
        "import Lean\n"
        + (source or "").rstrip()
        + "\n"
        + _FINGERPRINT_PROBE.replace("@@MARKER@@", marker).replace("@@TARGET@@", target_term)
    )


def _statement_fingerprint(
    source: str,
    *,
    uses_mathlib: bool,
    main_decl: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Lean-computed fingerprint of what the main theorem states.

    Runs one broker check of the file plus ``_FINGERPRINT_PROBE`` (sorry is
    fine; the file must compile). The result is ``{"ok": True,
    "fingerprint": sha256, …}`` or ``{"ok": False, "error": …}``; callers treat
    a failure as "unconfirmed", never as a match.
    """
    target = _resolve_declared_main(source, main_decl)
    term = _lean_name_term(target) if target else None
    if not target or not term:
        return {"ok": False, "error": f"main theorem `{main_decl}` not found"}
    marker = secrets.token_hex(12)
    program = _fingerprint_program(source, marker, term)
    try:
        with _LEAN_CHECK_SLOTS:
            result = lean_broker.check_source(
                program,
                uses_mathlib=uses_mathlib,
                timeout=float(timeout or _lean_check_timeout()),
            )
    except lean_broker.LeanBrokerError as exc:
        return {"ok": False, "error": f"fingerprint probe could not run: {exc}"}
    if (
        result.get("source_sha256") != hashlib.sha256(program.encode("utf-8")).hexdigest()
        or str(result.get("sandbox") or "") not in LEAN_CHECK_TRUSTED_SANDBOXES
    ):
        return {"ok": False, "error": "fingerprint probe attestation was not trusted"}
    if (
        result.get("status") != "completed"
        or result.get("exit_code") != 0
        or result.get("stdout_truncated")
        or result.get("stderr_truncated")
    ):
        return {"ok": False, "error": "fingerprint probe did not complete cleanly"}
    prefix = f"AGENT_MONITOR_STATEMENT::{marker}::"
    lines = [
        line[len(prefix) :]
        for line in str(result.get("stdout") or "").splitlines()
        if line.startswith(prefix)
    ]
    if len(lines) != 1:
        return {"ok": False, "error": "fingerprint probe printed no single report"}
    try:
        payload = json.loads(lines[0])
    except (json.JSONDecodeError, RecursionError, ValueError):
        return {"ok": False, "error": "fingerprint report was not JSON"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "fingerprint report was not an object"}
    rows = payload.get("rows")
    if payload.get("status") != "ok" or not isinstance(rows, list) or not rows:
        return {"ok": False, "error": "fingerprint closure was truncated or empty"}
    rows = [row for row in rows if isinstance(row, dict)]
    wanted = _normalized_decl_name(target)
    if not any(
        _normalized_decl_name(row.get("n")) == wanted and row.get("k") == "theorem"
        for row in rows
    ) or any(row.get("k") == "missing" for row in rows):
        return {"ok": False, "error": f"fingerprint has no theorem `{target}`"}
    canonical = json.dumps(
        {
            "version": STATEMENT_FINGERPRINT_VERSION,
            "rows": sorted(rows, key=lambda row: str(row.get("n"))),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "ok": True,
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "decl": target,
        "constants": len(rows),
    }


def _run_lean_check(
    ldir: Path, uses_mathlib: bool, *, main_decl: str | None = None
) -> dict[str, Any]:
    """Compile an exact source snapshot through the mandatory Lean broker.

    ``import Mathlib`` is accepted: the Mathlib profile loads the prebuilt
    library from the broker's immutable release. A missing broker yields
    ``toolchain_missing`` (never a local fallback). A clean compile with no
    ``sorry`` must also pass the ``#print axioms`` probe; ``main_decl`` (the
    locked main theorem, when there is one) must exist under exactly that name.
    """
    started = time.monotonic()
    client_timeout = _lean_check_timeout()

    try:
        ldir, source_bytes = _lean_source_snapshot(Path(ldir))
        source_text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _lean_check_failure(
            f"Lean checker input rejected: {exc}",
            started=started,
        )
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    profile = "mathlib" if uses_mathlib else "core"

    try:
        with _LEAN_CHECK_SLOTS:
            broker_result = lean_broker.check_source(
                source_text,
                uses_mathlib=uses_mathlib,
                timeout=client_timeout,
            )
    except lean_broker.LeanBrokerError as exc:
        failure = _lean_check_failure(
            (
                "Lean checker broker unavailable; refused to run Proof.lean "
                f"through any local fallback: {exc}"
            ),
            started=started,
        )
        if isinstance(exc, lean_broker.LeanBrokerUnavailable):
            failure["status"] = "toolchain_missing"
            failure["checker_profile"] = profile
        return failure

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
        failures.append(
            "Lean broker timed out (the fixed server limit is about "
            f"{LEAN_BROKER_LIMIT_SECONDS.get(profile, 90)} seconds for "
            f"{'Mathlib' if profile == 'mathlib' else 'core Lean'} files; "
            "split the proof or use lighter tactics)"
        )
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
    result: dict[str, Any] = {
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
    if ok and _output_reports_exit(result):
        # `#exit` stops elaboration: whatever follows was never checked.
        ok = False
        result.update({"ok": False, "status": "failed"})
        result["stderr"] = (
            result["stderr"]
            + "\nLean stopped elaborating early (`#exit`); the rest of the file was never checked."
        ).strip()
    if ok and _output_reports_sorry(result):
        result["reports_sorry"] = True
    if ok and not result.get("reports_sorry") and not _count_sorry(source_text):
        with _LEAN_CHECK_SLOTS:
            probe = _axioms_probe(
                source_text,
                uses_mathlib=uses_mathlib,
                timeout=client_timeout,
                main_decl=main_decl,
            )
        result["axioms"] = probe
        result["duration_s"] = round(time.monotonic() - started, 2)
        if not probe["ok"]:
            reported_sorry = "sorryAx" in (probe.get("file_axioms") or [])
            result.update({"ok": False, "status": "failed"})
            result["stderr"] = (
                result["stderr"]
                + "\nSoundness probe rejected the file: "
                + str(probe.get("error") or "axioms probe failed")
                + (
                    " (the proof admits a goal without a visible `sorry`)"
                    if reported_sorry
                    else ""
                )
            ).strip()
    return result


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
# Keyword rules match at ANY position — after `… in`, mid-line or indented —
# because a Lean command need not start a line (`open X in axiom …`).
# A keyword is a command only when it does not continue an identifier: the
# character before it must not be one of Lean's identifier characters (ASCII
# letters, `_ ' ! ?`, letterlike and subscript symbols) or a `.`/`«`.  Digits
# are deliberately not excluded: `1macro_rules` is a number, then a command.
_KW = (
    r"(?<![A-Za-z_.'«!?\u0391-\u03a9\u03b1-\u03fb\u1d62-\u1d6a"
    r"\u1f00-\u1ffe\u2080-\u209c\u2100-\u214f\U0001d49c-\U0001d59f])"
)
_CORE_TYPES = r"(?:ℕ|ℤ|ℚ|ℝ|ℂ|Nat|Int|Rat|Real|Complex)\b"
_CORE_CLASSES = (
    r"(LE|LT|Add|Mul|Sub|Div|Neg|Pow|Dvd|Mod|HAdd|HMul|HSub|HDiv|HPow|HMod|OfNat|"
    r"Preorder|PartialOrder|LinearOrder|Decidable|One|Zero|Inv|Abs|Max|Min|Ord|BEq|"
    r"DecidableEq|Semiring|Ring|Field|CommRing|NatCast|IntCast)"
)
_CHEAT_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        _KW + r"axiom\s+([^\s:({\[]+)",
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
        _KW + r"unsafe\b",
        "unsafe",
        "major",
        "`unsafe` declarations are not kernel-checked",
    ),
    (
        r"(?:@\[|" + _KW + r"attribute\s*\[)\s*(?:[^\]]*,\s*)?(?:local\s+|scoped\s+)?implemented_by",
        "implemented_by",
        "major",
        "`@[implemented_by]` swaps in unverified code",
    ),
    (
        r"(?:@\[|" + _KW + r"attribute\s*\[)\s*(?:[^\]]*,\s*)?extern\b",
        "extern",
        "major",
        "`@[extern]` binds a declaration to unverified native code",
    ),
    (
        _KW + r"opaque\s+([^\s:({\[]+)",
        "opaque",
        "major",
        "`opaque {m}` asserts a value exists without defining it",
    ),
    (
        r"\bnative_decide\b|\bofReduceBool\b|\bLean\.ofReduceBool\b",
        "native_decide",
        "major",
        "`native_decide` trusts the compiler, not the kernel",
    ),
    (
        _KW + r"(run_cmd|run_tac|run_elab|run_meta)(?![\w'])",
        "metaprogram",
        "major",
        "`{m}` runs a metaprogram inside the file; it can alter what compiles",
    ),
    (
        # `#` never continues an identifier, so these match wherever they
        # stand (`open Nat#exit` is `open Nat` followed by `#exit`).
        r"(#eval|#exit|#guard_msgs)(?![\w'!?])",
        "metaprogram",
        "major",
        "`{m}` runs a metaprogram, stops elaboration or hides Lean's own "
        "messages; it can alter what the checker sees",
    ),
    (
        _KW + r"(elab|elab_rules|macro|macro_rules|syntax|declare_syntax_cat|binder_predicate)\b",
        "custom_syntax",
        "major",
        "`{m}` defines new syntax or elaboration; proofs may bypass the kernel's view",
    ),
    (
        _KW + r"(initialize|builtin_initialize)\b",
        "initialize",
        "major",
        "`{m}` runs code when the file is loaded",
    ),
    (
        r"\badmitGoal\b|\bLean\.Elab\.admitGoal\b|\bsorryAx\b",
        "hidden_admit",
        "major",
        "a goal is admitted without a visible `sorry`",
    ),
    (
        r"\bLean\.(?:Elab|Meta|Parser|Environment|MonadEnv|Kernel|Compiler)\b|"
        r"\b(?:CommandElabM|TermElabM|TacticM|MetaM|CoreM|MonadEnv|modifyEnv|addDecl)\b",
        "meta_api",
        "major",
        "uses Lean's metaprogramming API; a proof file has no business changing the environment",
    ),
    (
        _KW + r"instance\b[^:\n]*:\s*" + _CORE_CLASSES + r"\s+" + _CORE_TYPES,
        "core_instance",
        "major",
        "redefines the standard `{m}` instance on a built-in number type",
    ),
    (
        r"@\[[^\]]*\binstance\b[^\]]*\]\s*(?:(?:private|protected|noncomputable)\s+)*"
        r"(?:def|abbrev)\b[^:\n]*:\s*" + _CORE_CLASSES + r"\s+" + _CORE_TYPES,
        "core_instance",
        "major",
        "registers a replacement `{m}` instance on a built-in number type",
    ),
    (
        _KW + r"attribute\s*\[[^\]]*\binstance\b",
        "instance_attribute",
        "minor",
        "`attribute [instance]` registers an instance — confirm it does not change a standard structure",
    ),
    (
        _KW + r"(notation3?|infix|infixl|infixr|prefix|postfix)\b",
        "custom_notation",
        "minor",
        "custom `{m}` — confirm it does not shadow a standard symbol",
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
        _KW + r"(?:def|abbrev|inductive|structure)\s+"
        r"(Odd|Even|Prime|Irrational|Rational|Dvd|Divides|Divisible|Coprime|IsSquare"
        r"|Sqrt|Gcd|Lcm|Continuous|Differentiable|Deriv|Integral|Limit|Converges)\b",
        "handrolled_notion",
        "minor",
        "hand-rolled `{m}` — confirm it matches the standard notion",
    ),
)


_LEXICAL_FLAGS = {
    "ambiguous_char_literal": (
        "a character literal directly after a symbol can be tokenized differently "
        "by Lean's notation table, so the text screens cannot vouch for this file"
    ),
    "interpolated_string": (
        "an interpolated string (`s!\"…{code}…\"`, `throwError \"…\"`) runs code "
        "inside a string literal; a proof file does not need one"
    ),
    "print_string": (
        "`#print \"…\"` writes arbitrary text into the checker output"
    ),
}


# ── Narrowed domain (problem-aware, heuristic) ──
#
# A `main` that adds a lower bound such as `(hn : 1 ≤ n)` on a variable the
# PROBLEM quantifies without restriction proves only a special case: Lean's ℕ
# includes 0. Informal proofs often bring their own convention ("ℕ = {1, 2, 3,
# …}"), so the bound is checked against the problem text alone. The screen is
# deliberately conservative — any explicit bound, positivity wording,
# restricted-set notation (ℝ⁺, ℕ \ {0}) or enumeration anywhere in the problem
# suppresses it — and it is only ever
# `minor`: it never downgrades a result, it prompts one statement-fix round.
NARROWED_DOMAIN_FLAG = "narrowed_domain"
# Minor flags worth one statement-fix round even when the audit is satisfied.
STATEMENT_FIX_FLAGS = frozenset({NARROWED_DOMAIN_FLAG})

_DOMAIN_TYPES = {
    "ℕ": "nat", "Nat": "nat", "ℤ": "int", "Int": "int",
    "ℚ": "rat", "Rat": "rat", "ℝ": "real", "Real": "real",
}
_BINDER_OPEN = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}
_IDENT = r"[A-Za-z_][\w']*"
_CMP_OPS = ("≤", "<=", "≥", ">=", "≠", "<", ">")
# Lower-bound hypotheses are short; longer text (or a huge statement) is
# skipped rather than parsed, which also keeps the screen cheap on any input.
_MAX_STATEMENT_CHARS = 4000
_MAX_HYPOTHESIS_CHARS = 200
_NOT_ZERO_RE = re.compile(r"¬\s*\(\s*(" + _IDENT + r")\s*=\s*0\s*\)")
_PRIME_HYP_RE = re.compile(
    r"(?:Nat\.Prime|Prime)(?:\s+|\s*\(\s*)(" + _IDENT + r")\s*\)?|(" + _IDENT + r")\.Prime"
)
_ODD_HYP_RE = re.compile(r"Odd(?:\s+|\s*\(\s*)(" + _IDENT + r")\s*\)?")
# Problem wording that states (or may state) a restriction somewhere. Every
# pattern starts at a fixed token and has no adjacent ambiguous repeats, so
# matching stays linear in the problem length.
_PROBLEM_BOUND_WORDS_RE = re.compile(
    r"(?i)\b(?:positive|non[\s-]?(?:negative|zero|trivial|empty|degenerate)|"
    r"at\s+least|(?:not|no)\s+(?:less|smaller|fewer|lower)\s+than|"
    r"greater|larger|bigger|more\s+than|exceed\w*|strictly|"
    r"not\s+(?:equal\w*|zero)|unequal|different\s+from|other\s+than|except\w*|"
    r"excluding|counting\s+numbers?|(?:starting|beginning)\s+(?:from|at|with)|"
    # Wording whose objects carry an implicit lower bound: an odd prime is
    # at least 3, a composite number at least 4, a side length positive.
    r"odd\s+primes?|composites?|triangles?|polygons?|quadrilaterals?|"
    r"lengths?|radi(?:us|i)|perimeters?|areas?|intervals?|"
    r"(?:un|well[\s-])?defined)\b"
)
_PROBLEM_CMP = (
    r"(?:≥|>=|>|≠|!=|=/=|≤|<=|<|⩾|⩽|≧|≦|"
    r"\\g(?:eqslant|eqq|eq|e|t)(?![A-Za-z])|"
    r"\\l(?:eqslant|eqq|eq|e|t)(?![A-Za-z])|\\neq?(?![A-Za-z]))"
)
_PROBLEM_BOUND_CMP_RE = re.compile(
    r"(?<![\w'])[A-Za-z_]\w*'?\s*(?:[$}]\s*)?" + _PROBLEM_CMP + r"\s*(?:\$\s*)?[-−]?\d"
    r"|\d\s*(?:\$\s*)?" + _PROBLEM_CMP + r"\s*(?:\$\s*)?[A-Za-z_]"
)
# A number set written as ℕ, ℝ, \mathbb{N}, \mathbb Z, \N or \mathbf{Q}.
_PROBLEM_SET = (
    r"(?:[ℕℤℚℝ]|\\(?:mathbb|mathbf|Bbb|mathds|mathrm)(?:\s*\{\s*[NZQR]\s*\}|\s*[NZQR])"
    r"|\\[NZQR](?![A-Za-z]))"
)
# Restricted versions of such a set: ℝ⁺, ℝ₊, ℕ*, ℕ^+, ℝ_{>0}, ℕ_1, ℝ≥0, and
# the set minus zero (ℕ \ {0}, ℕ∖{0}, \mathbb{N} \setminus \{0\}). A plain
# ASCII N/Z/Q/R counts only when the + or * directly follows it and no operand
# (a digit, bracket or one-letter name) comes next, so `N*M`, `N* k` and
# `N + 1` stay arithmetic while `n in N* show …` reads as a restricted set.
_PROBLEM_BOUND_SETS_RE = re.compile(
    _PROBLEM_SET + r"\s*(?:[⁺₊*+]|[≥>≠]"
    r"|\^\s*(?:\{\s*)?(?:[+*×]|\\ast|\\times)"
    r"|_\s*(?:\{\s*)?(?:[+*1>≥≠]|\\ast|\\g|\\ne))"
    r"|(?:" + _PROBLEM_SET + r"|(?<![A-Za-z\\])[NZQR])\s*"
    r"(?:∖|\\(?:small)?setminus(?![A-Za-z])|\\backslash(?![A-Za-z])|\\|[-−])"
    r"\s*(?:\\left\s*)?\\?\{\s*0\s*(?:\\right\s*)?\\?\}"
    r"|(?<![A-Za-z\\])[NZQR](?:\^|_)?\{?[+*]\}?(?![\w(\\{]|\s+[\d(\\{]|\s+[A-Za-z]\b)"
    r"|(?<![A-Za-z\\])[NZQR]_\{?(?:>|≥|\\g)"
    # Membership in an interval: x ∈ (0, 1), x \in [1, n], (0, ∞).
    r"|(?:∈|\\in(?![A-Za-z]))\s*(?:\\left\s*)?[\[(]"
    r"|[\[(]\s*[-−]?\d+\s*,\s*(?:\+\s*)?(?:∞|\\infty)"
    r"|\{\s*[1-9]\d*\s*,"
    r"|\b[1-9]\s*,\s*[2-9]\s*,\s*(?:[3-9]\s*,\s*)?(?:\.\.\.|…|\\l?dots|\\cdots)"
)


def _closing_bracket(text: str, start: int) -> int:
    """Index of the bracket closing ``text[start]`` (-1 if unbalanced)."""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char in "([{⦃⟨":
            depth += 1
        elif char in ")]}⦄⟩":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _depth0_find(text: str, needles: tuple[str, ...], start: int = 0) -> tuple[int, str]:
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char in "([{⦃⟨":
            depth += 1
        elif char in ")]}⦄⟩":
            depth = max(0, depth - 1)
        elif depth == 0:
            for needle in needles:
                if text.startswith(needle, index):
                    return index, needle
    return -1, ""


def _depth0_split(text: str, needles: tuple[str, ...]) -> list[str]:
    parts: list[str] = []
    rest = text
    while True:
        at, needle = _depth0_find(rest, needles)
        if at < 0:
            parts.append(rest)
            return parts
        parts.append(rest[:at])
        rest = rest[at + len(needle):]


def _strip_parens(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and _closing_bracket(text, 0) == len(text) - 1:
        text = text[1:-1].strip()
    return text


def _binder_groups(
    text: str, variables: dict[str, str], hypotheses: list[str]
) -> int:
    """Consume leading ``(x y : T)`` / ``{…}`` / ``[…]`` groups; return the end."""
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] not in _BINDER_OPEN:
            return index
        close = _closing_bracket(text, index)
        if close < 0:
            return len(text)
        group = text[index + 1:close]
        colon, _ = _depth0_find(group, (":",))
        if colon >= 0 and not group.startswith(":=", colon):
            names = group[:colon].split()
            kind = _DOMAIN_TYPES.get(re.sub(r"\s+", "", group[colon + 1:]))
            if kind and all(re.fullmatch(_IDENT, name) for name in names):
                for name in names:
                    variables[name] = kind
            else:
                hypotheses.append(group[colon + 1:])
        index = close + 1


def _statement_hypotheses(signature: str) -> tuple[dict[str, str], list[str]]:
    """Quantified variables (name → domain) and hypotheses of a statement.

    ``signature`` is the text after the theorem's name, up to its ``:=``. Only
    binders before the colon and the leading ``∀ …,`` / ``… →`` chain of the
    conclusion are read; anything under ``∃``, ``fun`` or a sum is conclusion.
    """
    variables: dict[str, str] = {}
    hypotheses: list[str] = []
    end = _binder_groups(signature, variables, hypotheses)
    rest = signature[end:].lstrip()
    if not rest.startswith(":"):
        return variables, hypotheses
    conclusion = rest[1:]
    for _ in range(24):
        text = conclusion.strip()
        quantifier = re.match(r"(?:∀|forall\b)", text)
        if quantifier:
            comma, _ = _depth0_find(text, (",",), quantifier.end())
            if comma < 0:
                break
            binder = text[quantifier.end():comma].strip()
            conclusion = text[comma + 1:]
            if binder[:1] in _BINDER_OPEN:
                _binder_groups(binder, variables, hypotheses)
                continue
            colon, _ = _depth0_find(binder, (":",))
            names = binder[:colon].split() if colon >= 0 else []
            if names and all(re.fullmatch(_IDENT, name) for name in names):
                kind = _DOMAIN_TYPES.get(re.sub(r"\s+", "", binder[colon + 1:]), "")
                for name in names:
                    variables[name] = kind
                continue
            predicate = re.match(_IDENT, binder)
            if predicate and binder[predicate.end():].lstrip()[:1] in {"≥", ">", "≠"}:
                variables.setdefault(predicate.group(0), "")
                hypotheses.append(binder)
                continue
            for name in binder.split():
                if re.fullmatch(_IDENT, name):
                    variables.setdefault(name, "")
            continue
        if re.match(r"(?:∃|¬|fun\b|λ|∑|∏|⋃|⋂)", text):
            break
        parts = _depth0_split(text, ("→", "->"))
        if len(parts) < 2:
            break
        hypotheses.extend(parts[:-1])
        conclusion = parts[-1]
    return variables, hypotheses


def _bound_atom(side: str) -> str:
    """``1``, ``(1 : ℝ)``, ``n``, ``↑n``, ``(n : ℝ)`` → ``1`` / ``n``; else ""."""
    text = _strip_parens(side)
    colon, _ = _depth0_find(text, (":",))
    if colon >= 0:
        text = text[:colon]
    text = _strip_parens(text.strip().lstrip("↑").strip())
    if re.fullmatch(r"[0-9]+", text) or re.fullmatch(_IDENT, text):
        return text
    return ""


def _lower_bound(text: str) -> tuple[str, int, bool] | None:
    """``(variable, literal, strict)`` when ``text`` bounds a variable below."""
    negated = _NOT_ZERO_RE.fullmatch(text)
    if negated:
        return negated.group(1), 0, True
    at, op = _depth0_find(text, _CMP_OPS)
    if at < 0:
        return None
    left, right = text[:at], text[at + len(op):]
    if _depth0_find(right, _CMP_OPS)[0] >= 0:
        return None  # a chain such as `0 < n < 9`
    low, high = _bound_atom(left), _bound_atom(right)
    if op in {"≥", ">=", ">"}:
        low, high = high, low
    if not low or not high:
        return None
    literal = r"[0-9]+"
    if op == "≠":
        if low == "0" and re.fullmatch(_IDENT, high):
            return high, 0, True
        if high == "0" and re.fullmatch(_IDENT, low):
            return low, 0, True
        return None
    if re.fullmatch(literal, low) and re.fullmatch(_IDENT, high):
        return high, int(low), op in {"<", ">"}
    return None


def _lower_bounds(
    hypotheses: list[str], variables: dict[str, str]
) -> tuple[list[tuple[str, str, int]], dict[str, int]]:
    """``(variable, hypothesis text, integral lower bound)`` and implied bounds."""
    bounds: list[tuple[str, str, int]] = []
    implied: dict[str, int] = {}
    for hypothesis in hypotheses:
        if len(hypothesis) > _MAX_HYPOTHESIS_CHARS:
            continue
        for part in _depth0_split(hypothesis, ("∧",)):
            text = _strip_parens(re.sub(r"\s+", " ", part))
            prime = _PRIME_HYP_RE.fullmatch(text)
            if prime:
                name = prime.group(1) or prime.group(2)
                if variables.get(name) in {"nat", ""}:
                    implied[name] = max(implied.get(name, 0), 2)
                continue
            odd = _ODD_HYP_RE.fullmatch(text)
            if odd:
                if variables.get(odd.group(1)) in {"nat", ""}:
                    implied[odd.group(1)] = max(implied.get(odd.group(1), 0), 1)
                continue
            found = _lower_bound(text)
            if found is None or found[0] not in variables:
                continue
            name, literal, strict = found
            kind = variables[name]
            if not strict and literal == 0 and kind in {"nat", ""}:
                continue  # `0 ≤ n` on ℕ restricts nothing
            integral = kind in {"nat", "int", ""}
            floor = literal + 1 if strict and integral else literal
            bounds.append((name, text, floor))
    return bounds, implied


def _problem_states_a_bound(problem: str) -> bool:
    text = problem or ""
    return bool(
        _PROBLEM_BOUND_WORDS_RE.search(text)
        or _PROBLEM_BOUND_CMP_RE.search(text)
        or _PROBLEM_BOUND_SETS_RE.search(text)
    )


def _main_statement(source: str, main_decl: str | None) -> tuple[str, str, int] | None:
    """``(name, statement text after the name, line)`` of the main theorem."""
    commands = _lean_commands(source)
    target = _probe_target(source, main_decl)
    if target is None:
        return None
    decl = next(
        (
            d for d in _qualified_declarations(source, commands)
            if d["qualified"] == target and d["kind"] in {"theorem", "lemma"}
        ),
        None,
    )
    if decl is None:
        return None
    command = commands[int(decl["command"])]
    body = command["body_text"]
    signature = body[: _signature_end(body)]
    after = signature[
        command["keyword_at"] - command["body"] + len(command["keyword"]):
    ].lstrip()
    if not after.startswith(decl["raw_name"]):
        return None
    return decl["name"], after[len(decl["raw_name"]):], int(decl["line"])


def _narrowed_domain_flags(
    lean: str, problem: str, main_decl: str | None = None
) -> list[dict[str, Any]]:
    """Minor flag: `main` bounds a variable from below where the PROBLEM does not."""
    if not (problem or "").strip() or _problem_states_a_bound(problem):
        return []
    try:
        found = _main_statement(lean or "", main_decl)
    except Exception:  # noqa: BLE001 - a heuristic never breaks a fidelity screen
        return []
    if found is None:
        return []
    name, statement, line = found
    if len(statement) > _MAX_STATEMENT_CHARS:
        return []
    variables, hypotheses = _statement_hypotheses(statement)
    bounds, implied = _lower_bounds(hypotheses, variables)
    narrowing = [
        (variable, text) for variable, text, floor in bounds
        if not (variable in implied and floor <= implied[variable])
    ]
    if not narrowing:
        return []
    listed = ", ".join(f"`{text}`" for text in dict.fromkeys(t for _, t in narrowing))[:300]
    # The ℕ-includes-0 reminder only explains a bound on a natural number.
    nat_note = (
        " (Lean's ℕ includes 0)"
        if any(variables.get(variable) == "nat" for variable, _ in narrowing)
        else ""
    )
    return [
        {
            "id": NARROWED_DOMAIN_FLAG,
            "severity": "minor",
            "message": (
                f"`{name}` assumes {listed}, a restriction the PROBLEM never states"
                f"{nat_note} — the theorem may cover only a special case; "
                "drop it unless the claim fails without it"
            ),
            "lines": [line],
        }
    ]


def _fidelity_flags(
    lean: str, *, problem: str = "", main_decl: str | None = None
) -> list[dict[str, Any]]:
    """Mechanical red flags: ways a file can typecheck without proving the claim.

    With the PROBLEM text, a conservative minor ``narrowed_domain`` screen is
    added (see ``_narrowed_domain_flags``); without it only the source is read.
    """
    raw = lean or ""
    lexer = _LeanLexer(raw)
    src, anomalies = lexer.run(), lexer.anomalies
    flags: list[dict[str, Any]] = []
    for kind, message in _LEXICAL_FLAGS.items():
        offsets = [offset for found, offset in anomalies if found == kind]
        if offsets:
            flags.append(
                {
                    "id": kind,
                    "severity": "major",
                    "message": message,
                    "lines": sorted({raw.count("\n", 0, offset) + 1 for offset in offsets})[:8],
                }
            )
    for pattern, fid, severity, message in _CHEAT_RULES:
        hits = [
            hit for hit in re.finditer(pattern, src)
            if not lexer.inside_ident[hit.start()]
        ]
        if not hits:
            continue
        first = hits[0].group(1) if hits[0].groups() else ""
        lines = sorted({src.count("\n", 0, h.start()) + 1 for h in hits})
        existing = next((f for f in flags if f["id"] == fid), None)
        if existing is not None:
            existing["lines"] = sorted(set(existing["lines"]) | set(lines))[:8]
            continue
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
    if problem and named:
        flags.extend(_narrowed_domain_flags(raw, problem, main_decl))
    return flags


def _statement_fix_wanted(
    flags: list[dict[str, Any]], audit: dict[str, Any] | None
) -> bool:
    """A statement-fix round is worth one call: major, or a fixable minor flag."""
    return _fidelity_severity(flags, audit) == "major" or any(
        f.get("id") in STATEMENT_FIX_FLAGS for f in flags
    )


def _fidelity_severity(flags: list[dict[str, Any]], audit: dict[str, Any] | None) -> str:
    if any(f.get("severity") == "major" for f in flags):
        return "major"
    if audit and audit.get("ok") and not audit.get("faithful"):
        return str(audit.get("severity") or "major")
    if any(f.get("severity") == "minor" for f in flags):
        return "minor"
    return "ok"


def _audit_model(
    env: dict[str, str], model: str | None, *, guest: bool = False
) -> str | None:
    """The fidelity auditor model: an explicit independent choice, if any.

    ``AGENT_MONITOR_FORMAL_AUDIT_MODEL`` (user settings first, then the
    server environment) selects a different model for the audit. Guests are
    bound to the sponsored Kimi route, so the override never applies to them.
    """
    if guest:
        return model
    configured = str(
        env.get(AUDIT_MODEL_ENV) or os.environ.get(AUDIT_MODEL_ENV) or ""
    ).strip()
    return configured or model


def _parse_audit_payload(data: Any, used: Any) -> dict[str, Any]:
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
        "back_translation": str(data.get("back_translation") or "")[:1200],
        "issues": issues,
        "main_decl": str(data.get("main_decl") or "")[:80],
        "statement_suggestion": str(data.get("statement_suggestion") or "")[:600],
        "model": used,
    }


def _audit_faithfulness(
    env: dict[str, str],
    model: str | None,
    *,
    problem: str,
    proof: str,
    lean: str,
) -> dict[str, Any]:
    """Ask a model whether the Lean statement really is the problem's claim.

    The prompt is cold (no authoring context) and casts the model as an
    independent reviewer that back-translates the statement before comparing.
    """
    prompt = (
        AUDIT_PROMPT.replace("{problem}", (problem or "(not recorded)")[:6000])
        .replace("{proof}", (proof or "(not available)")[:20000])
        .replace("{lean}", (lean or "")[:40000])
    )
    try:
        used, content = _chat(env, model, prompt)
        data = _extract_json_object(str(content))
    except SessionStopped:
        raise  # the run was deleted: never an audit result
    except Exception as exc:  # noqa: BLE001 - a provider outage (timeout, HTTP
        # error, subprocess failure) means "no audit"; it must never discard a
        # checked Lean result on the generate, revise or harness paths.
        return {"ok": False, "error": (str(exc).strip() or exc.__class__.__name__)[:300]}
    return _parse_audit_payload(data, used)


def _apply_fidelity(
    status: str, flags: list[dict[str, Any]], audit: dict[str, Any] | None
) -> str:
    """Downgrade a compiling file that provably doesn't state the problem.

    This applies to ``incomplete`` too: an honest ``sorry`` on the wrong
    claim must not be presented as progress on the right one.
    """
    if status not in {"verified", "incomplete"}:
        return status
    return "unfaithful" if _fidelity_severity(flags, audit) == "major" else status


def _fidelity_summary(flags: list[dict[str, Any]], audit: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if audit and audit.get("ok"):
        if audit.get("verdict"):
            parts.append(f"Audit: {audit['verdict']}")
        for issue in (audit.get("issues") or [])[:3]:
            parts.append(f"• {issue}")
    majors = [
        f for f in flags
        if f.get("severity") == "major" or f.get("id") in STATEMENT_FIX_FLAGS
    ]
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
    # Lean's own warning is authoritative even when no literal token is
    # visible (for example a macro that expands to `sorry`).
    if sorry_n > 0 or check.get("reports_sorry") or _output_reports_sorry(check):
        return "incomplete"
    return "verified"


def _check_summary(lean: str, check: dict[str, Any], status: str) -> str:
    lines = _sorry_lines(lean)
    parts: list[str] = []
    if status == "verified":
        parts.append("Check passed — Lean accepted the file with no errors and no sorry.")
    elif status == "unfaithful":
        if lines or check.get("reports_sorry") or _output_reports_sorry(check):
            parts.append(
                f"Lean typechecks the file with {len(lines) or 'hidden'} sorry/admit "
                "remaining, and the statement does not faithfully capture the "
                "problem — see Fidelity."
            )
        else:
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


def _workspace_problem(workspace: Path) -> str:
    """The run's problem text for problem-aware fidelity screens ("" if none)."""
    try:
        return (
            _read_optional_workspace_text(
                workspace, "problem.txt", limit=MAX_WORKSPACE_PROBLEM_BYTES
            )
            or ""
        ).strip()
    except (OSError, ValueError):
        return ""


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
    _stop_if_asked()  # nothing is written for a deleted run
    prev = load_cached(workspace) or {}
    lean_src = lean_src.rstrip() + "\n"
    kernel_receipt = _trusted_kernel_receipt(authoritative_check)
    verified_source_sha256 = ""
    checked_source_sha256 = ""
    if status in {"verified", "unfaithful", "incomplete"} and kernel_receipt is not None:
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
            # `verified_source_sha256` keeps its meaning (a sorry-free,
            # probe-clean kernel acceptance); a file that compiled with sorry
            # (incomplete, or incomplete downgraded to unfaithful) is bound
            # separately so compiler facts can mark sorry-dependence.
            with_sorry = bool(
                _count_sorry(lean_src)
                or (authoritative_check or {}).get("reports_sorry")
                or _output_reports_sorry(authoritative_check or {})
            )
            if status == "incomplete" or with_sorry:
                checked_source_sha256 = current_sha256
            else:
                verified_source_sha256 = current_sha256
    if (status == "verified" and not verified_source_sha256) or (
        status == "unfaithful" and not (verified_source_sha256 or checked_source_sha256)
    ):
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
    flags = _fidelity_flags(
        lean_src,
        problem=_workspace_problem(workspace),
        main_decl=str(
            (extra or {}).get("main_decl") or prev.get("main_decl") or ""
        ) or None,
    )
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
    carried_model = prev.get("model")
    if action in {"compile", "check"} and (
        (prev.get("lean") or "").rstrip() != lean_src.rstrip()
    ):
        # A compile/check of edited or pasted source: no model wrote it.
        carried_model = None
    result = {
        "source_provenance": provenance,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model or carried_model,
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
    result["checked_source_sha256"] = (
        checked_source_sha256 or verified_source_sha256 or None
    )
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


def _write_and_run(
    workspace: Path,
    lean_src: str,
    uses_mathlib: bool,
    *,
    main_decl: str | None = None,
) -> dict[str, Any]:
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

        if main_decl:
            check = dict(
                _run_lean_check(ldir, uses_mathlib=uses_mathlib, main_decl=main_decl)
            )
        else:
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


def _install_unchecked_source(
    workspace: Path, lean_src: str, uses_mathlib: bool
) -> None:
    """Write Proof.lean exactly as ``_write_and_run`` would, without checking it."""
    workspace = Path(workspace).resolve()
    with _formal_io_lock(workspace):
        ldir = lean_dir(workspace)
        if ldir.is_symlink():
            raise ValueError("Refusing to write through symbolic-link lean directory")
        _write_scaffold(ldir, uses_mathlib=uses_mathlib)
        _write_lean_source(ldir, _normalize_lean_imports(lean_src))


@_serialized_formal_operation("check")
def compile_or_check(
    *,
    workspace: Path,
    lean: str | None = None,
    mode: str = "check",
    statements_only: bool | None = None,
) -> dict[str, Any]:
    """Run Lean on the current (or provided) source without calling an LLM.

    ``mode`` is ``compile`` (exit-code only) or ``check`` (sorry-aware).
    ``statements_only`` (an Analyze run's Lean; by default, whenever the
    previous result was statement-only) keeps the result a statement-only
    formalization: ``scope: "statements"``, ``proofs_attempted: false``, the
    targets and the statement label, never an incomplete proof.
    """
    if mode not in {"compile", "check"}:
        raise ValueError(f"Unknown mode: {mode}")
    lean_src = (lean if lean is not None else read_source(workspace)).strip()
    if not lean_src:
        raise ValueError("No Proof.lean yet — press Verify first, or paste Lean source")
    uses_mathlib = _detect_mathlib(lean_src)
    flags = _fidelity_flags(lean_src)
    refused = _execution_refusal(flags)
    if refused:
        # Code that runs at elaboration time (IO, run_cmd, #eval, initialize,
        # custom elaborators, the metaprogramming API) never reaches the
        # sandbox from the editor path: such a file cannot be a verified
        # proof, and the text screens already classify it as unfaithful.
        # The source is still saved (unchecked) so Proof.lean and the cached
        # result describe the same text.
        _install_unchecked_source(workspace, lean_src, uses_mathlib)
        check = {
            "ok": False,
            "status": "failed",
            "exit_code": None,
            "stdout": "",
            "stderr": refused,
            "duration_s": 0.0,
            "refused_before_check": True,
        }
    else:
        check = _write_and_run(workspace, lean_src, uses_mathlib)
    status = _status_from_check(check, _count_sorry(lean_src), mode=mode)
    prev = load_cached(workspace) or {}
    # Preserve prior attempt history so the UI can show compile/check rounds;
    # the new action is numbered as the latest round.
    prior = list(prev.get("attempts") or [])
    attempt = _attempt_record(
        round_i=len(prior),
        action=mode,
        lean_src=lean_src,
        uses_mathlib=uses_mathlib,
        check=check,
    )
    if refused:
        attempt["check"]["refused_before_check"] = True
    prior_audit = (prev.get("fidelity") or {}).get("audit")
    if not prior_audit or (prev.get("lean") or "") != lean_src:
        prior_audit = None
    if mode == "check":
        status = _apply_fidelity(status, flags, prior_audit)
    attempt["result_status"] = status
    notes = _check_summary(lean_src, check, status)
    if statements_only is None:
        statements_only = prev.get("scope") == "statements"
    statement_fields: dict[str, Any] | None = None
    if statements_only:
        targets = prev.get("targets") if isinstance(prev.get("targets"), list) else []
        statement_fields = _statements_only_fields(status, None, targets)
        notes = _statements_only_notes(status, statement_fields["status_label"], lean_src, notes)
    if refused:
        notes = f"{notes} {refused}".strip()
    fid_note = _fidelity_summary(flags, prior_audit)
    if fid_note and (mode == "check" or refused):
        notes = f"{notes} {fid_note}".strip()
    # Hand-edited or pasted source was written by no model: only unchanged
    # generated bytes keep the previous model and source labels.
    same_source = (prev.get("lean") or "").rstrip() == lean_src.rstrip()
    return _persist(
        workspace,
        lean_src=lean_src,
        status=status,
        uses_mathlib=uses_mathlib,
        attempts=prior + [attempt],
        notes=notes,
        title=str(prev.get("title") or ""),
        model=prev.get("model") if same_source else None,
        source=str(prev.get("source") or "editor") if same_source else "editor",
        action=mode,
        authoritative_check=check,
        extra=statement_fields,
    )


# Fidelity flags whose source runs code while Lean elaborates the file.
EXECUTION_FLAG_IDS = frozenset(
    {"metaprogram", "custom_syntax", "initialize", "meta_api", "interpolated_string"}
)


def _execution_refusal(flags: list[dict[str, Any]]) -> str:
    """Why the editor path refuses to send this source to the checker ("" if not)."""
    hits = [f for f in flags if f.get("id") in EXECUTION_FLAG_IDS]
    if not hits:
        return ""
    details = "; ".join(
        str(f.get("message") or f.get("id"))
        + (f" (line {', '.join(map(str, f['lines']))})" if f.get("lines") else "")
        for f in hits[:3]
    )
    return (
        "Refused before checking: the file runs code while Lean elaborates it "
        f"({details}). Remove it to compile or check this file."
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


# The Codex intervention is a coding agent with a shell on this server, on a
# linked subscription: administrator-only, like the Formal harness and the
# local-tool engines (see account_policy).
_MEMBER_LEAN_AGENT_ERROR = (
    "Lean coding agents run local tools and are limited to the site administrator"
)


def _codex_route_model(
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None,
    model: str | None,
) -> str | None:
    """``model`` when the run's Codex subscription route offers it, else None.

    A cached Lean model may be an API or Claude model, or a generic label
    such as "Codex subscription"; binding it to ``codex --model`` would fail.
    """
    selected = str(model or "").strip()
    if not selected:
        return None
    try:
        options = harness_model_options(user, run_record)
    except (OSError, ValueError):
        return None
    if options.get("auth_route") != "codex_subscription":
        return None
    return selected if selected in (options.get("models") or []) else None


def _codex_subscription_interaction(
    *,
    workspace: Path,
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None = None,
    prompt: str,
    lean_source: str,
    edit: bool,
    model: str | None = None,
) -> dict[str, Any]:
    """Run the user's OAuth Codex in a disposable copy of lean/.

    The real workspace is never exposed to an unsandboxed intervention call;
    edited source is copied back only after this function returns and the
    normal checker path validates it. ``model`` (with the run's tuning) is
    passed to Codex; without one the CLI uses its default model.
    """
    if (user or {}).get("guest") or (run_record or {}).get("guest"):
        raise ValueError("Guest Formal actions use the included Kimi service")
    if account_policy.is_restricted(user):
        raise ValueError(_MEMBER_LEAN_AGENT_ERROR)
    from agent_monitor import codex_login
    from agent_monitor.cli_events import CLIEventParser
    from agent_monitor.engines_registry import build_cli_command, prompt_on_stdin
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
        from agent_monitor.runners.codex_backend import _run_codex_process

        selected_model = str(model or "").strip() or None
        env = _harness_env(
            "codex",
            user,
            model=selected_model,
            run_record=run_record,
            auth_route="codex_subscription" if recorded_route else None,
        )
        if selected_model:
            argv = _selected_harness_argv(
                engine="codex",
                model=selected_model,
                prompt=task_prompt,
                default=argv,
                env=env,
            )
        try:
            # Its own process group: the npm launcher cannot forward SIGKILL,
            # so a timeout must stop native Codex and its tools directly.
            proc = _run_codex_process(
                argv,
                input=task_prompt if prompt_on_stdin("codex", argv) else "",
                timeout=timeout_s,
                env=env,
                cwd=str(isolated),
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"Codex intervention timed out after {timeout_s}s") from exc
        parser = CLIEventParser("codex")
        parser.feed((proc.stdout or "") + "\n" + (proc.stderr or ""))
        reconnect = False
        if env.get("CODEX_HOME"):
            # Settings offers Reconnect once Codex rejects a linked login.
            reconnect = codex_login.note_turn_outcome(
                env["CODEX_HOME"],
                completed=parser.terminal_status == "completed",
                failure=(parser.terminal_error or "") if parser.terminal_status == "failed" else "",
            )
        usage_ledger.record_usage(
            model=str(parser.usage.get("model") or selected_model or "codex"),
            usage=usage_ledger.cli_usage(parser.usage, input_includes_cache=True),
            billing="subscription",
        )
        answer = parser.final_message().strip()
        if proc.returncode != 0:
            detail = (parser.output() or proc.stderr or proc.stdout or "Codex failed")[-1200:]
            if reconnect:
                # Codex's own reason and what to do, as a native Codex run's
                # error says.
                reason = (parser.terminal_error or "").rstrip(". ")
                detail = f"{reason}; {codex_login.RECONNECT_ADVICE}."
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
            "model": str(
                parser.usage.get("model") or selected_model or "Codex subscription"
            ),
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

        if _is_kimi_model(effective_model) or account_policy.is_restricted(user):
            raise
        result = _codex_subscription_interaction(
            workspace=workspace,
            user=user,
            run_record=run_record,
            prompt=prompt,
            lean_source=lean_src,
            edit=False,
            model=_codex_route_model(user, run_record, effective_model),
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
    # (uses_mathlib is always re-derived from the revised source below.)
    codex_mode = False
    effective_model = model or prev.get("model")
    try:
        chosen_model, content = _chat(env, effective_model, prompt)
        payload = _parse_model_payload(content)
        lean_src = payload["lean"]
        uses_mathlib = _detect_mathlib(lean_src)
        notes = payload.get("notes") or ""
        citations = list(payload.get("citations") or [])
    except ValueError:
        from agent_monitor.jobs import _is_kimi_model

        if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
            raise
        # A member keeps the model's own error: the fallback is a local agent.
        if _is_kimi_model(effective_model) or account_policy.is_restricted(user):
            raise
        codex_model = _codex_route_model(user, run_record, effective_model)
        codex_result = _codex_subscription_interaction(
            workspace=workspace,
            user=user,
            run_record=run_record,
            prompt=prompt,
            lean_source=lean_src,
            edit=True,
            model=codex_model,
        )
        codex_mode = True
        chosen_model = str(codex_result["model"])
        lean_src = str(codex_result["lean"])
        uses_mathlib = _detect_mathlib(lean_src)
        notes = str(codex_result["answer"])
        citations = _citations_from_lean(lean_src)

    attempts: list[dict[str, Any]] = list(prev.get("attempts") or [])
    problem, informal = _problem_and_proof(workspace, run_record)
    stopped = ""
    for round_i in range(max_repairs + 1):
        uses_mathlib = _detect_mathlib(lean_src)
        check = _write_and_run(workspace, lean_src, uses_mathlib)
        sorry_n = _count_sorry(lean_src)
        status = _status_from_check(check, sorry_n, mode="check")
        attempts.append(
            _attempt_record(
                round_i=len(attempts),
                action="revise",
                lean_src=lean_src,
                uses_mathlib=uses_mathlib,
                check=check,
            )
        )
        if status in {"verified", "incomplete", "toolchain_missing"}:
            break
        if round_i >= max_repairs:
            break
        repair_prompt = _fill_repair_prompt(
            task=(
                "The human-requested revision FAILED to compile. Fix the errors "
                "while keeping the requested change. The request was: "
                + feedback[:1500]
            ),
            problem=problem,
            history="\n".join(
                f"- round {a.get('round')}: {a.get('action')} → "
                f"{(a.get('check') or {}).get('status') or 'failed'}"
                for a in attempts[-6:]
            ),
            lean=lean_src,
            errors=_error_digest(check, lean_src),
            proof=informal,
        )
        try:
            if codex_mode:
                codex_result = _codex_subscription_interaction(
                    workspace=workspace,
                    user=user,
                    run_record=run_record,
                    prompt=repair_prompt,
                    lean_source=lean_src,
                    edit=True,
                    model=codex_model,
                )
                lean_src = str(codex_result["lean"])
                notes = str(codex_result["answer"] or notes)
                citations = _citations_from_lean(lean_src)
            else:
                chosen_model, repair_content = _chat(env, chosen_model, repair_prompt)
                repaired = _parse_model_payload(repair_content)
                lean_src = repaired["lean"]
                if repaired.get("notes"):
                    notes = repaired["notes"]
                if repaired.get("citations"):
                    citations = list(repaired["citations"])
        except Exception as exc:  # noqa: BLE001 - keep the checked revision
            stopped = str(exc).strip() or exc.__class__.__name__
            attempts.append(
                {
                    "round": len(attempts),
                    "uses_mathlib": uses_mathlib,
                    "sorry_count": sorry_n,
                    "action": "revise-repair",
                    "check": {"ok": False, "status": "error"},
                    "stderr_tail": f"repair round failed: {stopped}"[-2000:],
                    "stdout_tail": "",
                }
            )
            break
    uses_mathlib = _detect_mathlib(lean_src)
    if stopped:
        notes = f"{notes} Repair stopped early: {stopped[:300]}".strip()

    audit_result: dict[str, Any] | None = None
    if status in {"verified", "incomplete"}:
        guest = bool((user or {}).get("guest") or (run_record or {}).get("guest"))
        audit_result = _audit_faithfulness(
            env,
            _audit_model(env, model or chosen_model, guest=guest),
            problem=problem,
            proof=informal,
            lean=lean_src,
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
@usage_ledger.attributed("lean")
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
    if (user or {}).get("guest") or (run_record or {}).get("guest"):
        model = "kimi-k3"
        max_repairs = max(0, min(int(max_repairs), GUEST_REVISE_MAX_REPAIRS))
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


_CHECK_FIELDS = (
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
_DIAG_HEADER_RE = re.compile(
    r"^(?:[^\n]*?):(?P<line>\d+):(?P<col>\d+):\s*(?P<sev>error|warning|info)\b:?\s?(?P<msg>.*)$"
)
_OUTCOME_RE = re.compile(
    r"ProvingConsole\s+outcome\s*:\s*(Solved|Counterexample|Known/Open Status|Partial Progress)",
    re.I,
)
_STATUS_RANK = {"verified": 3, "incomplete": 2, "failed": 1}


def _attempt_record(
    *,
    round_i: int,
    action: str,
    lean_src: str,
    uses_mathlib: bool,
    check: dict[str, Any],
) -> dict[str, Any]:
    """One bounded, UI-safe attempt row (the shape monitor_summary reads).

    ``check.status`` is the broker's own word ("verified" whenever Lean exited
    0, sorry or not); ``result_status`` is what this round established
    (``verified`` / ``incomplete`` / ``compiled`` / ``failed`` / ...), which is
    what history views display.
    """
    sorry_n = _count_sorry(lean_src)
    if check.get("ok") and not sorry_n and check.get("reports_sorry"):
        sorry_n = 1
    record: dict[str, Any] = {
        "round": round_i,
        "uses_mathlib": uses_mathlib,
        "sorry_count": sorry_n,
        "action": action,
        "result_status": _status_from_check(
            check, sorry_n, mode="compile" if action == "compile" else "check"
        ),
        "check": {k: check[k] for k in _CHECK_FIELDS if k in check},
        "stderr_tail": (check.get("stderr") or "")[-2000:],
        "stdout_tail": (check.get("stdout") or "")[-1000:],
    }
    probe = check.get("axioms")
    if isinstance(probe, dict):
        record["check"]["axioms"] = {
            key: probe.get(key)
            for key in ("ok", "decl", "axioms", "unexpected", "error")
        }
    if check.get("reports_sorry"):
        record["check"]["reports_sorry"] = True
    return record


def _diagnostics(output: str) -> list[dict[str, Any]]:
    """Parse Lean's ``file:line:col: severity: message`` blocks, in order."""
    diagnostics: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in str(output or "").splitlines():
        match = _DIAG_HEADER_RE.match(raw)
        if match:
            current = {
                "line": int(match.group("line")),
                "col": int(match.group("col")),
                "severity": match.group("sev"),
                "message": match.group("msg").strip(),
            }
            diagnostics.append(current)
        elif current is not None and len(current["message"]) < 4000:
            current["message"] = (current["message"] + "\n" + raw).strip("\n")
    return diagnostics


def _numbered_source(source: str, *, limit: int = 60000) -> str:
    lines = (source or "").splitlines()
    width = max(3, len(str(len(lines))))
    text = "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(lines, 1))
    if len(text) > limit:
        text = text[:limit] + "\n… (source truncated)"
    return text


def _error_digest(
    check: dict[str, Any], source: str, *, limit: int = REPAIR_ERROR_LIMIT
) -> str:
    """The FIRST errors (root causes) with their source lines, not the tail."""
    stdout = str(check.get("stdout") or "")
    stderr = str(check.get("stderr") or "")
    diagnostics = _diagnostics(stdout + "\n" + stderr)
    errors = [d for d in diagnostics if d["severity"] == "error"]
    lines = (source or "").splitlines()
    parts: list[str] = []
    for index, diagnostic in enumerate(errors[:limit], 1):
        number = diagnostic["line"]
        context = []
        for ln in range(max(1, number - 1), min(len(lines), number + 1) + 1):
            marker = ">>" if ln == number else "  "
            context.append(f"{marker}{ln:>4} | {lines[ln - 1]}")
        parts.append(
            f"[{index}] error at line {number}, column {diagnostic['col']}:\n"
            + diagnostic["message"][:1800]
            + ("\n" + "\n".join(context) if context else "")
        )
    if len(errors) > limit:
        parts.append(
            f"… {len(errors) - limit} further error(s) omitted; they are often "
            "consequences of the ones above."
        )
    # Non-positional checker text: broker/timeout/soundness-probe messages.
    extra = "\n".join(
        line
        for line in stderr.splitlines()
        if line.strip() and not _DIAG_HEADER_RE.match(line)
    ).strip()
    if extra:
        parts.append("Checker messages:\n" + extra[:2000])
    if not errors and not extra:
        raw = (stdout + "\n" + stderr).strip()
        parts.append(raw[:4000] or "(the checker reported no diagnostics)")
    return "\n\n".join(parts)


def _sorry_goal_probe_source(source: str) -> str | None:
    """Mark tactic-position ``sorry`` with ``trace_state`` to reveal goals.

    Only unambiguous tactic positions are instrumented (``by sorry`` and a
    ``sorry`` tactic line inside a proof); term-mode ``:= sorry`` is left
    alone. Each marker carries its source line so goals map back exactly.
    """
    raw_lines = (source or "").splitlines()
    exe_lines = _lean_executable_text(source).splitlines()
    marked = 0
    out = list(raw_lines)
    previous = ""
    for index, exe in enumerate(exe_lines):
        match = re.search(r"\bby\s+(sorry)\b", exe)
        position = match.start(1) if match else -1
        if position < 0:
            bare = re.match(r"^(\s*(?:[·.]\s*)?)(sorry)\s*$", exe)
            if bare and previous and not previous.rstrip().endswith(":="):
                position = bare.start(2)
        if position >= 0 and index < len(out):
            line = out[index]
            out[index] = (
                line[:position]
                + f'trace "AMGOAL {index + 1}"; trace_state; sorry'
                + line[position + len("sorry") :]
            )
            marked += 1
        if exe.strip():
            previous = exe
    return "\n".join(out) + "\n" if marked else None


def _sorry_goals(source: str, *, uses_mathlib: bool) -> str:
    """Best-effort goal states at each tactic ``sorry`` (one broker check)."""
    probe = _sorry_goal_probe_source(source)
    if not probe:
        return ""
    try:
        with _LEAN_CHECK_SLOTS:
            result = lean_broker.check_source(
                probe, uses_mathlib=uses_mathlib, timeout=_lean_check_timeout()
            )
    except lean_broker.LeanBrokerError:
        return ""
    if result.get("source_sha256") != hashlib.sha256(probe.encode("utf-8")).hexdigest():
        return ""
    goals: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    for raw in str(result.get("stdout") or "").splitlines():
        marker = re.match(r"^AMGOAL (\d+)\s*$", raw.strip())
        if marker:
            current = []
            goals.append((int(marker.group(1)), current))
            continue
        if current is None:
            continue
        if _DIAG_HEADER_RE.match(raw):
            current = None
            continue
        if sum(len(item) for item in current) < 1500:
            current.append(raw)
    rendered = [
        f"line {line}:\n" + "\n".join(body).strip()
        for line, body in goals[:8]
        if "".join(body).strip()
    ]
    return "\n\n".join(rendered)


def _informal_outcome(proof: str) -> str:
    match = _OUTCOME_RE.search(proof or "")
    return match.group(1).lower() if match else ""


def _blueprint_text(guidance: str | None) -> str:
    text = str(guidance or "").strip()
    return text[:16000] if text else "(none — derive the structure from the informal proof)"


class _FormalSession:
    """State for one statement-first ``generate`` run.

    Every model call and compile is recorded as an attempt. A round that
    raises is recorded and ends the loop, and the best checked candidate so
    far is persisted, so a late provider error never discards earlier work.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        env: dict[str, str],
        model: str | None,
        problem: str,
        proof: str,
        guidance: str | None,
        guest: bool,
        max_repairs: int,
        audit: bool,
    ) -> None:
        self.workspace = workspace
        self.env = env
        self.model: Any = model
        self.author_model: Any = model
        self.problem = problem
        self.proof = proof
        self.blueprint = _blueprint_text(guidance)
        self.guest = guest
        self.max_repairs = max(0, int(max_repairs))
        self.audit_enabled = bool(audit)
        self.requested_model = model
        self.consecutive_failures = 0
        self.statement_repairs = GUEST_STATEMENT_REPAIRS if guest else STATEMENT_REPAIRS
        self.open_target = _informal_outcome(proof) in {
            "known/open status",
            "partial progress",
        }
        self._last_author: Any = model
        self.lean = ""
        self.uses_mathlib = False
        self.status = "failed"
        self.check: dict[str, Any] = {}
        self.notes = ""
        self.title = ""
        self.citations: list[dict[str, str]] = []
        self.main_decl = "main"
        self.lock: dict[str, Any] | None = None
        self.lock_message = ""
        self.statement_audit: dict[str, Any] | None = None
        self.attempts: list[dict[str, Any]] = []
        self.best: dict[str, Any] | None = None
        self.stop_reason = ""
        # Lean-computed statement fingerprints by source hash (see lock_statement).
        self._fingerprints: dict[str, dict[str, Any]] = {}
        self.checker_reached = False
        self.broker_lost = False

    # ── model and checker plumbing ──
    def _author(self, prompt: str) -> str:
        used, content = _chat(self.env, self.model, prompt)
        self.model = used
        self._last_author = used
        return content

    def _adopt(self, payload: dict[str, Any]) -> None:
        self.lean = _normalize_lean_imports(payload["lean"])
        self.author_model = getattr(self, "_last_author", self.model)
        if payload.get("notes"):
            self.notes = str(payload["notes"])
        if payload.get("title"):
            self.title = str(payload["title"])
        if payload.get("citations"):
            self.citations = list(payload["citations"])
        declared = str(payload.get("main_decl") or "").strip()
        if declared and self.lock is None and _probe_target(self.lean, declared):
            names = {
                d["name"] for d in _qualified_declarations(self.lean)
            } | {d["qualified"] for d in _qualified_declarations(self.lean)}
            if declared in names:
                self.main_decl = declared

    def _compile(self, action: str) -> str:
        _stop_if_asked()
        self.uses_mathlib = _detect_mathlib(self.lean)
        try:
            check = _write_and_run(
                self.workspace,
                self.lean,
                self.uses_mathlib,
                main_decl=self.main_decl,
            )
        except (OSError, ValueError) as exc:
            check = {
                "ok": False,
                "status": "failed",
                "exit_code": None,
                "stdout": "",
                "stderr": f"Could not install Proof.lean for checking: {exc}",
                "duration_s": 0.0,
            }
        self.check = check
        self.status = _status_from_check(check, _count_sorry(self.lean), mode="check")
        if self.status != "toolchain_missing":
            self.checker_reached = True
        self.attempts.append(
            _attempt_record(
                round_i=len(self.attempts),
                action=action,
                lean_src=self.lean,
                uses_mathlib=self.uses_mathlib,
                check=check,
            )
        )
        self._consider_best()
        return self.status

    def _consider_best(self) -> None:
        rank = _STATUS_RANK.get(self.status)
        if rank is None or _lock_violation(self.lock, self.lean):
            return
        errors = sum(
            1
            for d in _diagnostics(
                str(self.check.get("stdout") or "") + "\n" + str(self.check.get("stderr") or "")
            )
            if d["severity"] == "error"
        )
        key = (rank, -_count_sorry(self.lean), -errors, len(self.attempts))
        if self.status in {"incomplete", "verified"}:
            # Every compiling candidate is checked (one broker call per round,
            # bounded by the repair budget), so later rounds never build on a
            # drifted file, whether or not it would have become the best.
            drift = self._statement_drift(self.lean)
            if drift:
                # The text lock held but Lean elaborates a different statement
                # (e.g. an instance or definition hidden where the text screen
                # could not see it). Never eligible; continue from the best.
                self._reject_candidate(drift)
                return
        if self.best is not None and key < self.best["key"]:
            return
        self.best = {
            "key": key,
            "lean": self.lean,
            "check": self.check,
            "status": self.status,
            "uses_mathlib": self.uses_mathlib,
            "author": self.author_model,
            "notes": self.notes,
        }

    def _record_event(self, action: str, message: str, *, status: str) -> None:
        self.attempts.append(
            {
                "round": len(self.attempts),
                "uses_mathlib": self.uses_mathlib,
                "sorry_count": _count_sorry(self.lean),
                "action": action,
                "check": {"ok": False, "status": status},
                "stderr_tail": message[-2000:],
                "stdout_tail": "",
            }
        )

    def _auditor(self) -> Any:
        return _audit_model(
            self.env, self.requested_model or self.model, guest=self.guest
        )

    def _guarded(self, action: str, step) -> bool:
        """Run one model round; an exception is recorded, never fatal to the run.

        A single failed round (provider hiccup, unparseable or truncated
        reply) costs that round only; two consecutive failures stop the loop.
        Either way the best checked candidate so far is what gets persisted.
        """
        try:
            step()
        except SessionStopped:
            raise
        except Exception as exc:  # noqa: BLE001 - partial progress must persist
            reason = str(exc).strip() or exc.__class__.__name__
            self._record_event(action, f"{action} round failed: {reason}", status="error")
            self.consecutive_failures += 1
            if self.consecutive_failures >= 2:
                self.stop_reason = f"{action} round failed: {reason[:300]}"
            return False
        self.consecutive_failures = 0
        return True

    def _candidate_ok(self, payload: dict[str, Any], action: str) -> bool:
        violation = _lock_violation(self.lock, _normalize_lean_imports(payload["lean"]))
        if not violation:
            self.lock_message = ""
            return True
        self.lock_message = violation
        self._record_event(
            "lock-violation",
            f"{action} reply rejected without compiling: {violation}.",
            status="rejected",
        )
        return False

    # ── phases ──
    def statement_phase(self) -> None:
        prompt = _fill_template(
            TRANSLATE_PROMPT,
            problem=self.problem[:6000] or "(not recorded)",
            blueprint=self.blueprint,
            proof=self.proof[:40000],
        )
        content = self._author(prompt)
        try:
            payload = _parse_model_payload(content)
        except ValueError as exc:
            # One retry: the most common cause is a truncated or unfenced reply.
            content = self._author(
                prompt
                + "\n\nYOUR PREVIOUS REPLY COULD NOT BE PARSED ("
                + str(exc)[:200]
                + "). Reply again with the ```lean block and the ```json block."
            )
            payload = _parse_model_payload(content)
        self._adopt(payload)
        self._compile("statement")
        for _ in range(self.statement_repairs):
            if self.status != "failed" or self.stop_reason:
                break
            self._guarded("statement-repair", lambda: self._repair("statement"))

    def _statement_flags(self) -> list[dict[str, Any]]:
        return _fidelity_flags(
            self.lean, problem=self.problem, main_decl=self.main_decl
        )

    def statement_audit_phase(self) -> None:
        if not self.audit_enabled or self.status not in {"incomplete", "verified"}:
            return
        audit = self._audit()
        flags = self._statement_flags()
        fixes = 0
        while (
            _statement_fix_wanted(flags, audit)
            and fixes < STATEMENT_FIX_ROUNDS
            and not self.stop_reason
        ):
            fixes += 1
            before = _fidelity_severity(flags, audit)
            saved = (
                self.lean, self.check, self.status, self.uses_mathlib,
                self.author_model, self.notes, self.citations, self.main_decl,
            )
            saved_review = (audit, flags, self.statement_audit)
            fixed: dict[str, Any] | None = None

            def fix() -> None:
                nonlocal fixed
                fixed = _repair_statement(
                    self.env,
                    self.model,
                    problem=self.problem,
                    lean=self.lean,
                    audit=audit,
                    flags=flags,
                    proof=self.proof,
                )

            if not self._guarded("statement-fix", fix) or not fixed:
                break
            assert fixed is not None
            self._last_author = fixed.get("model") or self.model
            self._adopt(fixed)
            self._compile("statement-fix")
            if self.status == "failed":
                self._guarded(
                    "statement-repair", lambda: self._repair("statement")
                )
            if self.status in {"incomplete", "verified"}:
                audit = self._audit()
                flags = self._statement_flags()
                if before != "major" and _fidelity_severity(flags, audit) == "major":
                    # A fix prompted only by a minor flag must not trade an
                    # audited statement for one the audit rejects.
                    self._record_event(
                        "statement-fix",
                        "statement fix rejected: the audit found the rewritten "
                        "statement less faithful than the original.",
                        status="rejected",
                    )
                    audit, flags, self.statement_audit = saved_review
                    (
                        self.lean, self.check, self.status, self.uses_mathlib,
                        self.author_model, self.notes, self.citations, self.main_decl,
                    ) = saved
                    try:
                        self._restore_current()
                    except (OSError, ValueError) as exc:
                        self.stop_reason = f"could not restore the skeleton: {exc}"
                    break
            else:
                # An uncompilable "fix" cannot be proved; keep the compiling
                # skeleton and its honest (unfaithful) audit instead.
                (
                    self.lean, self.check, self.status, self.uses_mathlib,
                    self.author_model, self.notes, self.citations, self.main_decl,
                ) = saved
                try:
                    self._restore_current()
                except (OSError, ValueError) as exc:
                    self.stop_reason = f"could not restore the skeleton: {exc}"
                break

    def _audit(self) -> dict[str, Any]:
        try:
            audit = _audit_faithfulness(
                self.env,
                self._auditor(),
                problem=self.problem,
                proof=self.proof,
                lean=self.lean,
            )
        except SessionStopped:
            raise
        except Exception as exc:  # noqa: BLE001 - an audit outage must not lose the skeleton
            reason = str(exc).strip() or exc.__class__.__name__
            self._record_event(
                "statement-audit", f"statement audit failed: {reason}", status="error"
            )
            audit = {"ok": False, "error": reason[:300]}
        audit["scope"] = "statement"
        self.statement_audit = audit
        return audit

    def _fingerprint(self, lean: str) -> dict[str, Any]:
        """Lean-computed fingerprint of the locked main theorem's statement."""
        if not self.lock:
            return {"ok": False, "error": "no statement lock"}
        key = hashlib.sha256(lean.encode("utf-8")).hexdigest()
        cached = self._fingerprints.get(key)
        if cached is None:
            _stop_if_asked()  # a Lean probe, like a compile
            try:
                cached = _statement_fingerprint(
                    lean,
                    uses_mathlib=_detect_mathlib(lean),
                    main_decl=str(self.lock["main_decl"]),
                )
            except Exception as exc:  # noqa: BLE001 - unconfirmed, never a match
                cached = {"ok": False, "error": str(exc)[:300]}
            self._fingerprints[key] = cached
        return cached

    def _statement_drift(self, lean: str) -> str:
        """Why Lean says ``lean`` changed the locked statement ("" if not shown)."""
        expected = (self.lock or {}).get("fingerprint")
        if not expected:
            return ""
        current = self._fingerprint(lean)
        if not current.get("ok"):
            # Fail closed: the locked statement had a Lean fingerprint, so a
            # candidate Lean cannot fingerprint (it stops elaboration, hides
            # the theorem, or the probe did not complete) is never eligible.
            return (
                f"Lean could not confirm that `{self.main_decl}` still states the "
                "locked, audited statement ("
                + (str(current.get("error") or "fingerprint unavailable")[:200])
                + ")"
            )
        if current.get("fingerprint") != expected:
            return (
                f"Lean elaborates the statement of `{self.main_decl}` differently "
                "from the locked, audited statement (a definition, instance, "
                "notation, option or scope it depends on changed)"
            )
        return ""

    def _statement_confirmed(self, lean: str) -> bool:
        """Lean confirms ``lean`` states exactly the locked, audited statement."""
        expected = (self.lock or {}).get("fingerprint")
        if not expected:
            return False
        current = self._fingerprint(lean)
        return bool(current.get("ok") and current.get("fingerprint") == expected)

    def _reject_candidate(self, reason: str) -> None:
        self.lock_message = reason
        self._record_event(
            "lock-violation",
            f"compiled candidate rejected: {reason}.",
            status="rejected",
        )
        best = self.best
        if best is None:
            return
        self.lean = best["lean"]
        self.check = best["check"]
        self.status = best["status"]
        self.uses_mathlib = best["uses_mathlib"]
        self.author_model = best["author"]
        try:
            self._restore_current()
        except (OSError, ValueError) as exc:
            self.stop_reason = f"could not restore the locked candidate: {exc}"

    def lock_statement(self) -> None:
        if self.status in {"incomplete", "verified"}:
            target = _probe_target(self.lean, self.main_decl)
            self.lock = _statement_lock(self.lean, target) if target else None
            if self.lock:
                self.main_decl = str(self.lock["main_decl"])
                # Lean's own view of the audited statement: later candidates
                # must elaborate to exactly this (text layout aside).
                fingerprint = self._fingerprint(self.lean)
                self.lock["fingerprint"] = (
                    fingerprint.get("fingerprint") if fingerprint.get("ok") else None
                )
                if not fingerprint.get("ok"):
                    self.lock["fingerprint_error"] = str(fingerprint.get("error") or "")[:300]
                # Earlier statement drafts (possibly unfaithful, even if they
                # happened to compile without sorry) are no longer eligible
                # as the final artifact; only lock-respecting candidates are.
                self.best = None
                self._consider_best()

    def prove(self) -> None:
        prompt = _fill_template(
            PROVE_PROMPT,
            main_decl=self.main_decl,
            problem=self.problem[:6000] or "(not recorded)",
            blueprint=self.blueprint,
            proof=self.proof[:40000],
            lean=self.lean[:40000],
        )
        payload = _parse_model_payload(self._author(prompt))
        if self._candidate_ok(payload, "prove"):
            self._adopt(payload)
            self._compile("prove")

    def _repair(self, kind: str) -> None:
        if kind == "statement":
            task = (
                "This is the PHASE-1 STATEMENT skeleton and it failed to compile. "
                "Fix only what the diagnostics require so it compiles with `sorry` "
                "proofs. Do not start proving, and keep the meaning of every statement."
            )
        elif kind == "sorry":
            count = max(1, _count_sorry(self.lean))
            task = (
                f"The file compiles but still has {count} `sorry`. Replace them "
                "with complete proofs (the goals are listed below) while keeping "
                "everything that already compiles. Close as many as you can."
            )
        else:
            task = (
                "The file FAILED to compile. Fix the errors listed under "
                "DIAGNOSTICS (the first one is usually the root cause)."
            )
        if self.lock:
            lock_rule = (
                f"LOCK: the statement of `{self.main_decl}` and every non-theorem "
                f"command above it are locked; a reply that changes them is rejected "
                "unseen. Only theorem/lemma declarations may change above it."
            )
            if self.lock_message:
                lock_rule += (
                    f"\nYOUR PREVIOUS REPLY WAS REJECTED because {self.lock_message}. "
                    "Restore this locked statement exactly:\n```lean\n"
                    + str(self.lock.get("signature") or "")
                    + "\n```"
                )
        else:
            lock_rule = ""
        goals = ""
        if kind == "sorry":
            _stop_if_asked()  # a Lean probe, like a compile
            goals = _sorry_goals(self.lean, uses_mathlib=_detect_mathlib(self.lean))
        errors = (
            _error_digest(self.check, self.lean)
            if kind != "sorry"
            else "(the file compiles; see GOALS for what remains)"
        )
        prompt = _fill_repair_prompt(
            task=task,
            lock_rule=lock_rule,
            problem=self.problem,
            history=self.history(),
            lean=self.lean,
            errors=errors,
            goals=goals,
            blueprint=self.blueprint,
            proof=self.proof,
        )
        payload = _parse_model_payload(self._author(prompt))
        action = {
            "statement": "statement-repair",
            "sorry": "sorry-repair",
        }.get(kind, "repair")
        if self._candidate_ok(payload, action):
            self._adopt(payload)
            self._compile(action)

    def history(self) -> str:
        rows: list[str] = []
        for attempt in self.attempts[-10:]:
            check = attempt.get("check") or {}
            status = str(check.get("status") or "")
            if check.get("ok"):
                status = "incomplete" if attempt.get("sorry_count") else "verified"
            detail = ""
            if attempt.get("sorry_count") and check.get("ok"):
                detail = f", {attempt['sorry_count']} sorry left"
            elif not check.get("ok"):
                first = next(
                    (
                        d
                        for d in _diagnostics(
                            str(attempt.get("stdout_tail") or "")
                            + "\n"
                            + str(attempt.get("stderr_tail") or "")
                        )
                        if d["severity"] == "error"
                    ),
                    None,
                )
                if first:
                    detail = (
                        f", first error line {first['line']}: "
                        + first["message"].splitlines()[0][:160]
                    )
                elif attempt.get("stderr_tail"):
                    detail = ", " + str(attempt["stderr_tail"]).strip().splitlines()[-1][:160]
            rows.append(f"- round {attempt.get('round')}: {attempt.get('action')} → {status or 'failed'}{detail}")
        return "\n".join(rows) or "(first attempt)"

    def run_repairs(self) -> None:
        repairs_left = self.max_repairs
        open_rounds = 0
        while (
            repairs_left > 0
            and self.status not in {"verified", "toolchain_missing"}
            and not self.stop_reason
        ):
            kind = "errors" if self.status == "failed" else "sorry"
            if kind == "sorry" and self.open_target:
                # The informal artifact itself leaves the target open: spend
                # at most one round on helpers rather than the whole budget.
                open_rounds += 1
                if open_rounds > 1:
                    break
            repairs_left -= 1
            if not self._guarded(kind + "-repair", lambda: self._repair(kind)):
                continue
            if (
                self.lock is None
                and self.audit_enabled
                and self.statement_audit is None
                and self.status in {"incomplete", "verified"}
            ):
                # The skeleton never compiled; audit and lock the first
                # compiling statement before spending more proof effort.
                self._guarded("statement-audit", self._audit)
                self.lock_statement()

    def _restore_current(self) -> None:
        """Reinstall ``self.lean`` bytes (already checked) without recompiling."""
        _stop_if_asked()  # never write into a deleted run's workspace
        workspace = Path(self.workspace).resolve()
        with _formal_io_lock(workspace):
            ldir = lean_dir(workspace)
            if ldir.is_symlink():
                raise ValueError("Refusing to write through symbolic-link lean directory")
            _write_scaffold(ldir, uses_mathlib=self.uses_mathlib)
            _write_lean_source(ldir, self.lean)

    def select_best(self) -> None:
        best = self.best
        if best is None:
            return
        if self.status == "toolchain_missing":
            # The broker went away mid-run: the unchecked latest draft must not
            # replace the best candidate that was checked before the outage.
            self.broker_lost = True
        else:
            current_rank = _STATUS_RANK.get(self.status, 0)
            if best["lean"] == self.lean and best["status"] == self.status:
                return
            if best["key"][0] < current_rank:
                return
        self.lean = best["lean"]
        self.check = best["check"]
        self.status = best["status"]
        self.uses_mathlib = best["uses_mathlib"]
        self.author_model = best["author"]
        if best.get("notes"):
            self.notes = best["notes"]
        try:
            self._restore_current()
        except (OSError, ValueError) as exc:
            self.stop_reason = (self.stop_reason + f" Could not restore the best candidate: {exc}").strip()

    def final_audit(self) -> dict[str, Any] | None:
        if not self.audit_enabled or self.status not in {"verified", "incomplete"}:
            return None
        statement_audit = self.statement_audit or {}
        if (
            statement_audit.get("ok")
            and self.lock
            and not _lock_violation(self.lock, self.lean)
            and self._statement_confirmed(self.lean)
        ):
            # Reuse the statement audit only when BOTH the text lock holds and
            # Lean confirms the proved file elaborates exactly the audited
            # statement (same term, same local definitions and instances). The
            # kernel and the axioms probe certify the proof itself.
            return {
                **statement_audit,
                "scope": "statement",
                "statement_lock": self.lock.get("sha256"),
                "statement_fingerprint": self.lock.get("fingerprint"),
            }
        audit = _audit_faithfulness(
            self.env,
            self._auditor(),
            problem=self.problem,
            proof=self.proof,
            lean=self.lean,
        )
        audit["scope"] = "final"
        return audit


def _fill_template(template: str, **values: str) -> str:
    """Single-pass placeholder fill.

    Chained ``str.replace`` would re-expand a placeholder that happens to
    occur inside inserted problem, proof or Lean text; a single regex pass
    substitutes only the template's own placeholders.
    """
    pattern = re.compile(r"\{(" + "|".join(re.escape(key) for key in values) + r")\}")
    return pattern.sub(lambda match: str(values[match.group(1)]), template)


def _fill_repair_prompt(
    *,
    task: str,
    problem: str,
    history: str,
    lean: str,
    errors: str,
    proof: str,
    goals: str = "",
    blueprint: str = "",
    lock_rule: str = "",
) -> str:
    """REPAIR_PROMPT with full context: problem, line numbers, first errors."""
    return _fill_template(
        REPAIR_PROMPT,
        task=task,
        lock_rule=lock_rule,
        problem=(problem or "(not recorded)")[:6000],
        history=history or "(first attempt)",
        numbered=_numbered_source((lean or "")[:40000]),
        errors=errors or "(none)",
        goals=goals or "(none reported)",
        blueprint=(blueprint or "(none)")[:8000],
        proof=(proof or "(not available)")[:12000],
    )


def _repair_statement(
    env: dict[str, str],
    model: str | None,
    *,
    problem: str,
    lean: str,
    audit: dict[str, Any] | None,
    flags: list[dict[str, Any]],
    proof: str = "",
) -> dict[str, Any] | None:
    """One round of "the statement is wrong, restate it"."""
    findings: list[str] = []
    if audit and audit.get("back_translation"):
        findings.append(f"What the Lean statement says: {audit['back_translation']}")
    if audit and audit.get("verdict"):
        findings.append(f"Verdict: {audit['verdict']}")
    findings += [f"- {i}" for i in (audit or {}).get("issues") or []]
    if audit and audit.get("statement_suggestion"):
        findings.append(f"Suggested faithful statement: {audit['statement_suggestion']}")
    findings += [
        f"- {f['message']}" + (f" (line {', '.join(map(str, f['lines']))})" if f.get("lines") else "")
        for f in flags
        if f.get("severity") == "major" or f.get("id") in STATEMENT_FIX_FLAGS
    ]
    if not findings:
        return None
    prompt = _fill_template(
        STATEMENT_FIX_PROMPT,
        problem=(problem or "(not recorded)")[:6000],
        audit="\n".join(findings)[:4000],
        proof=(proof or "(not available)")[:12000],
        lean=lean[:40000],
    )
    try:
        used, content = _chat(env, model, prompt)
        payload = _parse_model_payload(content)
    except ValueError:
        # The statement stays as-is; the audit findings still reach the UI.
        return None
    payload["model"] = used
    return payload


@_serialized_formal_operation("generate")
@usage_ledger.attributed("lean")
def generate(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None = None,
    max_repairs: int = AUTO_MAX_REPAIRS,
    audit: bool = True,
    guidance: str | None = None,
    problem_override: str | None = None,
    proof_override: str | None = None,
    prove: bool = True,
    targets: list[str] | None = None,
    source_label: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Translate the run's proof into Lean statement-first and verify it.

    The statement is drafted, compiled, audited by an independent cold
    reviewer and locked before proof effort is spent; proof rounds cannot
    change it. ``max_repairs`` bounds the proof-phase repair rounds (compile
    errors and sorry elimination alike); statement repairs and one statement
    fix have their own small bounds. See the module docstring.

    ``problem_override`` / ``proof_override`` replace ``problem.txt`` and the
    run's proof (an Analyze run formalizes selected statements of someone
    else's manuscript).  ``prove=False`` stops after the statement phase, its
    audit and the lock: no proof round and no repair round run, and the result
    says "Statements compiled · proofs not attempted" (``scope:
    "statements"``), never an incomplete proof.

    ``should_stop`` is asked before each model call, Lean probe, compile and
    write; once it answers True (the run was deleted) the session raises
    :class:`SessionStopped` and persists nothing.  Without one, a caller's
    :func:`stop_when` applies.
    """
    token = _SHOULD_STOP.set(should_stop) if should_stop is not None else None
    try:
        return _generate(
            workspace=workspace, run_record=run_record, user=user, model=model,
            max_repairs=max_repairs, audit=audit, guidance=guidance,
            problem_override=problem_override, proof_override=proof_override,
            prove=prove, targets=targets, source_label=source_label,
        )
    finally:
        if token is not None:
            _SHOULD_STOP.reset(token)


def _generate(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None,
    max_repairs: int,
    audit: bool,
    guidance: str | None,
    problem_override: str | None,
    proof_override: str | None,
    prove: bool,
    targets: list[str] | None,
    source_label: str | None,
) -> dict[str, Any]:
    guest = bool((user or {}).get("guest") or (run_record or {}).get("guest"))
    if guest:
        model = "kimi-k3"
        max_repairs = max(0, min(int(max_repairs), GUEST_MAX_REPAIRS))

    if problem_override is not None:
        problem = str(problem_override).strip()
    else:
        problem = (
            _read_optional_workspace_text(
                workspace,
                "problem.txt",
                limit=MAX_WORKSPACE_PROBLEM_BYTES,
            )
            or ""
        ).strip()

    if proof_override is not None:
        source, proof = (source_label or "selected statements"), str(proof_override).strip()
        if not proof:
            raise ValueError("No statements were selected to formalize")
    else:
        source, proof = _proof_source(workspace, run_record)
        if not proof:
            raise ValueError("No proof text yet — wait for the run to produce proof.md")

    env = _llm_environment(user, run_record)
    session = _FormalSession(
        workspace=workspace,
        env=env,
        model=model,
        problem=problem,
        proof=proof,
        guidance=guidance,
        guest=guest,
        max_repairs=max_repairs,
        audit=audit,
    )
    # The first call has nothing to preserve yet; its failure propagates.
    session.statement_phase()
    if session.status != "toolchain_missing":
        session.statement_audit_phase()
        session.lock_statement()
        if prove:
            if session.status == "incomplete" and not session.stop_reason:
                session._guarded("prove", session.prove)
            session.run_repairs()
        session.select_best()

    status = session.status
    audit_result: dict[str, Any] | None = None
    if status == "toolchain_missing":
        if session.checker_reached:
            summary = (
                "The Lean checker broker became unavailable during the run, so the "
                "latest draft below was not checked, and no earlier attempt compiled."
            )
        else:
            summary = (
                "The Lean checker broker is unavailable on this server, so nothing was "
                "compiled. The drafted statement below (proofs still `sorry`) has not "
                "been checked; no repair or audit calls were spent on the outage."
            )
    else:
        try:
            audit_result = session.final_audit()
        except SessionStopped:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the checked result
            audit_result = {"ok": False, "error": str(exc)[:300]}
        status = _apply_fidelity(status, session._statement_flags(), audit_result)
        summary = _check_summary(session.lean, session.check, status)
        if not prove:
            label = _statements_only_fields(status, session, targets)["status_label"]
            summary = _statements_only_notes(status, label, session.lean or "", summary)
        if session.broker_lost:
            summary += (
                " The Lean checker became unavailable during the run; the best "
                "candidate checked before the outage is shown."
            )
        scope = (audit_result or {}).get("scope")
        if session.lock and not _lock_violation(session.lock, session.lean):
            if scope == "statement":
                summary += (
                    f" The statement of `{session.main_decl}` was audited before the "
                    "proof phase, and Lean confirmed the final file states exactly "
                    "that statement."
                )
            elif scope == "final":
                summary += (
                    f" The statement of `{session.main_decl}` was audited before the "
                    "proof phase; the final file was audited again because Lean could "
                    "not confirm it states exactly the audited statement."
                )
    notes = summary
    if session.notes:
        notes = f"{notes} Model notes: {session.notes}".strip()
    if session.stop_reason:
        notes = f"{notes} Stopped early: {session.stop_reason}".strip()
    fid_note = _fidelity_summary(session._statement_flags(), audit_result)
    if fid_note:
        notes = f"{notes} {fid_note}".strip()
    prev_chat = list((load_cached(workspace) or {}).get("chat") or [])
    extra: dict[str, Any] = {
        "statement_lock": session.lock,
        "main_decl": session.main_decl,
    }
    if session.statement_audit is not None:
        extra["statement_audit"] = session.statement_audit
    if not prove:
        extra.update(_statements_only_fields(status, session, targets))
    return _persist(
        workspace,
        lean_src=session.lean,
        status=status,
        uses_mathlib=_detect_mathlib(session.lean),
        attempts=session.attempts,
        notes=notes,
        title=session.title,
        model=session.author_model,
        source=source,
        action="verify",
        chat=prev_chat,
        citations=session.citations,
        audit=audit_result,
        authoritative_check=session.check,
        extra=extra,
    )


STATEMENTS_ONLY_LABEL = "Statements compiled · proofs not attempted"
# Lean results whose statements typecheck (proofs `sorry` by design).
_STATEMENTS_COMPILED = frozenset({"incomplete", "verified", "compiled"})


def _statements_only_notes(status: str, label: str, lean_src: str, notes: str) -> str:
    """The notes of a statement-only result: never "N sorry remain" as if a
    proof had been attempted."""
    if status in _STATEMENTS_COMPILED or status == "unfaithful":
        detail = (
            "Lean typechecks the stated results; every proof is `sorry` by design "
            "(this formalization states the selected results only)."
            if _count_sorry(lean_src)
            else "Lean typechecks the stated results."
        )
        return f"{label}. {detail}"
    return f"{label}. {notes}".strip()


def _statements_only_fields(
    status: str, session: "_FormalSession | None", targets: list[str] | None
) -> dict[str, Any]:
    """How a statement-only result describes itself (never as incomplete)."""
    if status in _STATEMENTS_COMPILED:
        label = STATEMENTS_ONLY_LABEL
    elif status == "unfaithful":
        label = "Statements compiled · statement fidelity flagged · proofs not attempted"
    elif status == "toolchain_missing":
        label = "Statements not compiled (Lean checker unavailable) · proofs not attempted"
    else:
        label = "Statements did not compile · proofs not attempted"
    return {
        "scope": "statements",
        "proofs_attempted": False,
        "status_label": label,
        "targets": [str(item)[:80] for item in (targets or [])][:40],
        "toolchain_note": (
            "Lean 4.14 / Mathlib (Dec 2024) on this server; upstream Lean, if any, "
            "is linked and not re-checked by this server."
        ),
    }


@_serialized_formal_operation("audit")
@usage_ledger.attributed("lean")
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
    guest = bool((user or {}).get("guest") or (run_record or {}).get("guest"))
    audit = _audit_faithfulness(
        env,
        _audit_model(env, model or prev.get("model"), guest=guest),
        problem=problem,
        proof=informal,
        lean=lean_src,
    )
    if not audit.get("ok"):
        raise ValueError(audit.get("error") or "audit failed")
    flags = _fidelity_flags(
        lean_src, problem=problem, main_decl=prev.get("main_decl") or None
    )
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
    if (user or {}).get("guest") or (run_record or {}).get("guest"):
        return {"auth_route": "api_key", "models": ["kimi-k3"], "selected": "kimi-k3"}
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
    if route == "claude_subscription":
        from agent_monitor.jobs import _canonical_claude_continuation_model as canonical

        recorded = (
            canonical((run_record or {}).get("requested_model"))
            or canonical(recorded)
            or recorded
        )
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
    if (user or {}).get("guest") or (run_record or {}).get("guest"):
        return []
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
        if not job:
            raise ValueError("No harness run in progress")
        if job.get("state") == "finishing":
            # The agent already exited; its result is being checked and audited.
            return {"stopping": False, "finishing": True}
        job["stopping"] = True
        proc = job.get("proc")
    # Still launching: the worker stops the agent as soon as it has a process.
    if proc is not None:
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
    # The harness's own advisory `./check.sh` needs the user's elan shims;
    # the server unit's PATH usually omits ~/.elan/bin.
    prefixes.extend(_harness_lean_bin_dirs())
    # Subscription routes may run with a private account HOME; the shims must
    # still use the server's toolchains rather than install new ones there.
    from agent_monitor.subprocess_env import server_elan_home

    env["ELAN_HOME"] = server_elan_home()
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
    from agent_monitor.engines_registry import prompt_on_stdin
    from agent_monitor.jobs import _is_kimi_model

    if engine != "codex":
        return default
    selected = str(model or "").strip()
    if not selected:
        raise ValueError(
            "Codex Formal Lean requires an explicit model for this run route"
        )
    if _is_kimi_model(selected):
        # The wrapper reads the prompt from stdin ("-"), as native Codex does.
        return [
            sys.executable,
            "-u",
            str(
                Path(__file__).resolve().parent
                / "runners"
                / "kimi_codex_runner.py"
            ),
            "-",
        ]

    def prompt_index(argv: list[str], what: str) -> int:
        # The "-" that reads the prompt from stdin, or an operator command's
        # prompt argument.
        if prompt_on_stdin(engine, argv):
            return len(argv) - 1
        try:
            return argv.index(prompt)
        except ValueError as exc:
            raise ValueError(
                f"Codex harness command cannot bind {what} before its prompt"
            ) from exc

    def with_tuning(argv: list[str]) -> list[str]:
        from agent_monitor.run_tuning import codex_config_args

        args = codex_config_args(env or {}, selected)
        if not args:
            return argv
        index = prompt_index(argv, "run tuning")
        argv[index:index] = args
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
    index = prompt_index(argv, "the selected model")
    argv[index:index] = ["--model", selected]
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


def _close_running_harness_record(
    workspace: Path, *, state: str, notes: str, error: str = ""
) -> bool:
    """Replace a harness record still marked running with a terminal failure.

    A result the worker already persisted is never overwritten. The Lean
    source, attempts, chat and fidelity stay as they are, so nothing claims a
    check that did not happen.
    """
    cached = load_cached(workspace)
    if not cached:
        return False
    harness = cached.get("harness") if isinstance(cached.get("harness"), dict) else {}
    if cached.get("status") != "running" and harness.get("state") != "running":
        return False
    record = dict(cached)
    record["status"] = "failed"
    record["notes"] = notes
    record["harness"] = {
        **harness,
        "state": state,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if error:
        record["harness"]["error"] = error
        record["log"] = (
            str(record.get("log") or "") + f"\n[agent-monitor] harness error: {error}"
        ).strip()[-16000:]
    # auto_deploy treats this flag, like a running state, as active work.
    record.pop("harness_running", None)
    _write_cached(workspace, record)
    return True


HARNESS_INTERRUPTED_NOTE = (
    "Harness run did not finish — the server may have restarted. "
    "Press Compile/Check on the source below, or run it again."
)


def reconcile_orphaned_harness(workspace: Path) -> bool:
    """Persist the end of a running harness record no live worker owns.

    Harness workers are daemon threads of this process, so after a restart (or
    a dead worker) the record can never finish by itself, and auto_deploy
    waits on it for good. A live harness, like every other Lean action, holds
    the workspace's formal reservation: taking it proves the record is an
    orphan.
    """
    try:
        token = _reserve_formal_operation(workspace, "harness recovery")
    except ValueError:
        return False
    try:
        return _close_running_harness_record(
            workspace, state="interrupted", notes=HARNESS_INTERRUPTED_NOTE
        )
    except (OSError, TypeError, ValueError):
        return False
    finally:
        _release_formal_operation(workspace, token)


def reconcile_orphaned_harnesses(workspaces_root: Path) -> list[str]:
    """At startup, close every harness record a previous process left running."""
    closed: list[str] = []
    for result in sorted(Path(workspaces_root).glob(f"*/{RESULT_FILENAME}")):
        workspace = result.parent
        if workspace.is_symlink():
            continue
        if reconcile_orphaned_harness(workspace):
            closed.append(workspace.name)
    return closed


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
    run_id: str | None = None,
    _operation_token: object,
) -> dict[str, Any]:
    """Launch a coding agent on Proof.lean in the background.

    Returns immediately with a ``running`` record; the selected model is bound
    to both the harness process and its post-run audit. With ``run_id`` a
    finished harness that changed Proof.lean refreshes the run's derived
    formal views.
    """
    from agent_monitor.engines_registry import (
        CLI_ENGINES,
        build_cli_command,
        discard_prompt_file,
    )

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
    _write_harness_checker(ldir)
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
    try:
        return _launch_harness(
            workspace=workspace,
            ldir=ldir,
            argv=argv,
            engine=engine,
            spec=spec,
            selected_model=selected_model,
            prompt=prompt,
            harness_env=harness_env,
            auth_route=auth_route,
            lean_src=lean_src,
            initial_fingerprint=initial_fingerprint,
            user=user,
            run_record=run_record,
            problem=problem,
            informal=informal,
            audit=audit,
            run_id=run_id,
            _operation_token=_operation_token,
        )
    except BaseException:
        # No worker will run: remove a prompt file nothing will read.
        discard_prompt_file(ldir)
        raise


def _launch_harness(
    *,
    workspace: Path,
    ldir: Path,
    argv: list[str],
    engine: str,
    spec: dict[str, Any],
    selected_model: str | None,
    prompt: str,
    harness_env: dict[str, str],
    auth_route: str | None,
    lean_src: str,
    initial_fingerprint: dict[str, Any] | None,
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None,
    problem: str,
    informal: str,
    audit: bool,
    run_id: str | None,
    _operation_token: object,
) -> dict[str, Any]:
    """Record the running harness and start its worker thread."""
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
    if str(prev.get("lean") or "").strip() == lean_src.strip():
        # This record replaces the status of the very source the agent starts
        # from; _harness_finish restores it if the agent leaves the file alone.
        record["harness"]["initial_status"] = str(prev.get("status") or "")
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
            "run_id": run_id,
            "workspace": workspace,
            "ldir": ldir,
            "argv": argv,
            "prompt": prompt,
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
    run_id: str | None = None,
) -> dict[str, Any]:
    """Reserve a proof workspace and launch its Formal Lean harness."""
    if (user or {}).get("guest") or (run_record or {}).get("guest"):
        raise ValueError("Sign in to use a coding-agent Formal harness")
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
            run_id=run_id,
            _operation_token=token,
        )
    except BaseException:
        _release_formal_operation(resolved, token)
        raise


def _harness_worker_guarded(
    *,
    operation_token: object,
    run_id: str | None = None,
    **kwargs,
) -> None:
    """Release a cross-thread harness reservation on every worker exit path."""
    workspace = Path(kwargs["workspace"])
    key = _harness_key(workspace)
    try:
        result = _harness_worker(**kwargs)
    except Exception as exc:
        # Still holding the reservation: never leave the record running.
        detail = (str(exc).strip() or exc.__class__.__name__)[:300]
        label = str(kwargs.get("label") or kwargs.get("engine") or "The")
        try:
            _close_running_harness_record(
                workspace,
                state="error",
                notes=(
                    f"{label} harness failed before its result was recorded: "
                    f"{detail}. Press Compile/Check on the source below, or run it again."
                ),
                error=detail,
            )
        except (OSError, TypeError, ValueError):
            pass
        raise
    finally:
        from agent_monitor.engines_registry import discard_prompt_file

        # A wrapper deletes its prompt file once read; an agent that never
        # read it (another command line, or no launch) leaves it behind.
        if kwargs.get("ldir"):
            discard_prompt_file(Path(kwargs["ldir"]))
        with _HARNESS_LOCK:
            _HARNESS_JOBS.pop(key, None)
        _release_formal_operation(workspace, operation_token)
    if run_id and ((result or {}).get("harness") or {}).get("artifact_fresh"):
        # A new Proof.lean: re-derive the run's Lean, formal DAG and coverage
        # stages as a manual Check does (only now, with the reservation free).
        from agent_monitor import auto_pipeline

        run_record = kwargs.get("run_record") or {}
        try:
            auto_pipeline.refresh_formal(
                run_id=run_id,
                workspace=workspace,
                owner_id=(kwargs.get("user") or {}).get("id") or run_record.get("owner_id"),
                model=kwargs.get("model") or result.get("model"),
                engine=str(run_record.get("engine") or "unknown"),
            )
        except Exception:  # noqa: BLE001 - the harness result is already saved
            pass


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
    prompt: str = "",
) -> dict[str, Any]:
    """Run the agent to its end and return the persisted result.

    ``prompt`` goes on stdin when the command reads it from there (Codex's
    ``-``); other commands carry their prompt themselves.
    """
    from agent_monitor.engines_registry import prompt_on_stdin, send_prompt_on_stdin

    key = _harness_key(workspace)
    timeout_s = int(os.environ.get("AGENT_MONITOR_LEAN_HARNESS_TIMEOUT", "1200"))
    log_path = ldir / "harness.log"
    lines: list[str] = []
    started = time.time()
    exit_code: int | None = None
    stopped = False
    timed_out = False
    # What the starting source scored at launch. The agent can rewrite
    # lean_verify.json, so the finish never reads this back from it.
    initial_status = str((base.get("harness") or {}).get("initial_status") or "")
    initial_audit = (base.get("fidelity") or {}).get("audit")

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

    from agent_monitor.jobs import _ensure_codex_auth, _is_kimi_model, _remove_codex_api_home

    codex_api_home = None
    stdin_prompt = prompt_on_stdin(engine, argv)
    try:
        if (
            engine == "codex"
            and env.get("AGENT_MONITOR_AUTH_MODE") == "api_key"
            and not _is_kimi_model(model)
        ):
            # A login of its own with the run's OpenAI key, never the service
            # account's ~/.codex; it is removed once the harness exits.
            codex_api_home = _ensure_codex_auth(
                env, (user or {}).get("id") or (run_record or {}).get("owner_id")
            )
        proc = subprocess.Popen(
            argv,
            cwd=str(ldir),
            env=env,
            stdin=subprocess.PIPE if stdin_prompt else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        _remove_codex_api_home(codex_api_home)
        lines.append(f"failed to launch {label}: {exc}\n")
        with _HARNESS_LOCK:
            stopped = bool((_HARNESS_JOBS.get(key) or {}).get("stopping"))
        result = _harness_finish(
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
            stopped=stopped,
            initial_status=initial_status,
            initial_audit=initial_audit,
        )
        with _HARNESS_LOCK:
            _HARNESS_JOBS.pop(key, None)
        return result

    with _HARNESS_LOCK:
        job = _HARNESS_JOBS.get(key)
        if job is not None:
            job["proc"] = proc
        stop_requested = bool((job or {}).get("stopping"))
    if stdin_prompt:
        send_prompt_on_stdin(proc, prompt)
    if stop_requested:
        # Stop was pressed while the agent was still launching.
        _terminate_process_group(proc)

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
                # A child that already exited (output still draining) did not.
                timed_out = proc.poll() is None
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
        _terminate_process_group(proc, force=True)
        reader.join(timeout=1)
        if proc.stdout is not None:
            proc.stdout.close()
        _remove_codex_api_home(codex_api_home)
        with _HARNESS_LOCK:
            job = _HARNESS_JOBS.get(key) or {}
            stopped = bool(job.get("stopping"))
            job["proc"] = None
            # The agent is done; the Lean check and the audit run next.
            job["state"] = "finishing"
    try:
        log_path.write_text("".join(lines), encoding="utf-8")
    except OSError:
        pass
    child_error = ""
    if engine == "codex" and env.get("CODEX_HOME"):
        from agent_monitor import codex_login
        from agent_monitor.cli_events import CLIEventParser

        parser = CLIEventParser("codex")
        parser.feed("".join(lines))
        # Settings offers Reconnect once Codex rejects a linked login, and the
        # result says so, as a native Codex run's error does.
        if codex_login.note_turn_outcome(
            env["CODEX_HOME"],
            completed=parser.terminal_status == "completed",
            failure=(parser.terminal_error or "") if parser.terminal_status == "failed" else "",
        ):
            child_error = (
                f"{(parser.terminal_error or '').rstrip('. ')}; {codex_login.RECONNECT_ADVICE}"
            )

    try:
        return _harness_finish(
            workspace=workspace,
            engine=engine,
            label=label,
            log="".join(lines),
            exit_code=exit_code,
            child_error=child_error,
            elapsed=round(time.time() - started, 1),
            user=user,
            run_record=run_record,
            model=model,
            problem=problem,
            informal=informal,
            audit=audit,
            initial_fingerprint=initial_fingerprint,
            stopped=stopped,
            timeout_limit=timeout_s if timed_out else None,
            initial_status=initial_status,
            initial_audit=initial_audit,
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
        try:
            env = _llm_environment(user, run_record)
            guest = bool((user or {}).get("guest") or (run_record or {}).get("guest"))
            audit_model = _audit_model(env, model, guest=guest)
        except Exception as exc:  # noqa: BLE001 - e.g. a sign-in revoked during
            # the run: like a provider outage, that means "no audit", never a
            # lost kernel-checked result.
            return {"ok": False, "error": (str(exc).strip() or exc.__class__.__name__)[:300]}
        return _audit_faithfulness(
            env,
            audit_model,
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
            # The harness's own model and tuning judge its fidelity.
            model=_codex_route_model(user, run_record, model),
        )
        used = str(result.get("model") or model or "Codex subscription")
        data = _extract_json_object(str(result.get("answer") or ""))
    except Exception as exc:  # noqa: BLE001 - see the direct-API branch above
        return {"ok": False, "error": (str(exc).strip() or exc.__class__.__name__)[:300]}
    return _parse_audit_payload(data, used)



def _record_harness_usage(
    engine: str,
    log: str,
    model: str,
    route: str,
    run_record: dict[str, Any] | None,
) -> None:
    """Book the Lean harness CLI's own token usage (never part of run totals)."""
    from agent_monitor.cli_events import CLIEventParser

    is_claude = str(engine or "").startswith("claude")
    parser = CLIEventParser("claude" if is_claude else "codex")
    try:
        parser.feed(log or "")
    except Exception:  # noqa: BLE001 - usage capture is best-effort
        return
    if route in {"codex_subscription", "claude_subscription"}:
        billing = "subscription"
    elif str((run_record or {}).get("credential_source") or "") == "sponsored_kimi":
        billing = "sponsored"
    else:
        billing = "api" if route == "api_key" else None
    usage_ledger.record_usage(
        model=str(parser.usage.get("model") or model or engine),
        usage=usage_ledger.cli_usage(parser.usage, input_includes_cache=not is_claude),
        billing=billing,
        cost_usd=parser.usage.get("cost_usd"),
    )


@usage_ledger.attributed("lean")
def _harness_finish(
    *,
    workspace: Path,
    engine: str,
    label: str,
    log: str,
    exit_code: int | None,
    child_error: str = "",
    elapsed: float,
    user: dict[str, Any] | None,
    run_record: dict[str, Any] | None,
    model: str | None,
    problem: str,
    informal: str,
    audit: bool,
    initial_fingerprint: dict[str, Any] | None,
    stopped: bool,
    timeout_limit: int | None = None,
    initial_status: str = "",
    initial_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-check a fresh regular artifact, but never forgive a failed child.

    ``child_error`` says why a failed child failed, when the user must act on
    it (Codex rejected the linked login).
    ``timeout_limit`` is the limit, in seconds, at which the worker killed a
    still-running child; a user Stop takes precedence over it.
    ``initial_status`` and ``initial_audit`` are the status and fidelity audit
    the starting source had at launch, as the worker holds them.
    """
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
    _record_harness_usage(engine, log, effective_model, effective_route, run_record)
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
    timed_out = timeout_limit is not None and not stopped
    rejection_reasons: list[str] = []
    if stopped:
        rejection_reasons.append("the run was stopped")
    elif timed_out:
        rejection_reasons.append(f"the harness exceeded its {timeout_limit}s limit")
    if not child_exit_ok and not timed_out:
        # After a timeout the exit status is only the worker's own kill.
        exited = (
            "the child process did not exit successfully"
            if exit_code is None
            else f"the child process exited {exit_code}"
        )
        rejection_reasons.append(f"{exited}: {child_error}" if child_error else exited)
    if not artifact_regular:
        rejection_reasons.append(artifact_reason or "Proof.lean is not a regular root file")
    elif not lean_src:
        rejection_reasons.append("Proof.lean is empty")
    elif not artifact_fresh:
        rejection_reasons.append("Proof.lean is unchanged from the pre-run artifact")

    artifact_accepted = bool(
        child_exit_ok
        and artifact_regular
        and artifact_fresh
        and lean_src
        and not stopped
        and not timed_out
    )
    harness_meta = {
        "engine": engine,
        "label": label,
        "state": "stopped" if stopped else "timed_out" if timed_out else "finished",
        "timed_out": timed_out,
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
    kept = False
    audit_result: dict[str, Any] | None = None
    if stopped:
        status = "stopped"
    elif not artifact_accepted:
        # A clean exit that left the file as it was keeps the status the file
        # had before the run while Lean still bears it out; it never promotes.
        kept = bool(
            child_exit_ok
            and not timed_out
            and not artifact_fresh
            and kernel_status in {"verified", "incomplete"}
            and initial_status in {"verified", "unfaithful", "incomplete", "compiled"}
            and (initial_status != "verified" or kernel_status == "verified")
        )
        status = "failed"
        if kept:
            # With the audit it had at launch, never one the agent could have
            # written into lean_verify.json since, and no better than today's
            # red flags allow.
            audit_result = initial_audit if isinstance(initial_audit, dict) else {}
            status = _apply_fidelity(
                initial_status, _fidelity_flags(lean_src), audit_result
            )

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

    # A rejected artifact's Failed or Stopped is not Lean's verdict on it.
    notes = _check_summary(
        lean_src, check, status if artifact_accepted or kept else kernel_status
    )
    if rejection_reasons:
        notes = (
            "Artifact rejected: "
            + "; ".join(rejection_reasons)
            + (
                ". The earlier Lean result stands. "
                if kept and status == initial_status
                else ". The outer Lean result is diagnostic only. "
            )
            + notes
        )
    fid_note = _fidelity_summary(_fidelity_flags(lean_src), audit_result)
    verdict = (
        "stopped by user" if stopped else "timed out" if timed_out else f"exit {exit_code}"
    )
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
