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

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from agent_monitor.proof_graph import (  # reuse LLM plumbing
    _extract_json,
    _proof_source,
    _resolve_llm,
)

RESULT_FILENAME = "lean_verify.json"
LEAN_DIRNAME = "lean"
PROOF_FILENAME = "Proof.lean"

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
  start with `import Mathlib`. On this server Mathlib is often unavailable, so
  core-only proofs are strongly preferred.
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
- The proof must be complete: no `sorry`, no `admit`, no invented `axiom`.
- Keep the file self-contained. Add short helper lemmas with proofs if needed.
- Use ASCII Lean identifiers. Put informal commentary in `/-- ... -/` docs.
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

Rules: fix the ERRORS, do not dodge them. Keep the theorem statement exactly as
it is — weakening or specialising the statement to make the file compile counts
as a failure. If a subgoal is genuinely out of reach, leave one `sorry` there and
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

How to work — you have a shell, so use it. Do not guess whether Lean accepts your
file, run it:

    lean Proof.lean          # exit 0 and no output means accepted

Iterate: edit `Proof.lean`, run `lean Proof.lean`, read the errors, fix, repeat
until it is clean. Read the actual error messages — do not rewrite the file from
scratch on every failure. `lean --version` tells you the toolchain. Mathlib is
usually NOT available here, so prefer Lean core (`Init`, `Nat`, `Int`, `List`);
`omega`, `decide`, `simp`, `ac_rfl`, `induction`, `rcases`/`obtain` all work in
core. `ring`, `linarith`, `norm_num`, `field_simp` need Mathlib — do not use them.

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


def toolchain_status() -> dict[str, Any]:
    """Report which Lean tools are on PATH / under ~/.elan."""
    lean = shutil.which("lean") or _elan_bin("lean")
    lake = shutil.which("lake") or _elan_bin("lake")
    elan = shutil.which("elan") or _elan_bin("elan")
    version = None
    if lean:
        try:
            out = subprocess.run(
                [lean, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            version = (out.stdout or out.stderr or "").strip().splitlines()[:1]
            version = version[0] if version else None
        except (OSError, subprocess.TimeoutExpired):
            version = None
    return {
        "lean": lean,
        "lake": lake,
        "elan": elan,
        "version": version,
        "available": bool(lean),
    }


def _elan_bin(name: str) -> str | None:
    home = Path(os.environ.get("ELAN_HOME") or (Path.home() / ".elan"))
    cand = home / "bin" / name
    return str(cand) if cand.is_file() and os.access(cand, os.X_OK) else None


def load_cached(workspace: Path) -> dict[str, Any] | None:
    p = workspace / RESULT_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


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
        if not text:
            continue
        key = (decl.lower(), text.lower())
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
    in_block = False
    for i, raw in enumerate((lean or "").splitlines(), 1):
        stripped = raw.strip()
        if in_block:
            pending.append(re.sub(r"-/\s*$", "", stripped).strip())
            if "-/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/-"):
            body = stripped[2:].lstrip("-!")
            if "-/" in body:
                pending.append(body.split("-/")[0].strip())
            else:
                pending.append(body.strip())
                in_block = True
            continue
        if stripped.startswith("--"):
            pending.append(stripped.lstrip("-").strip())
            continue
        if not stripped:
            pending = []
            continue
        m = _DECL_RE.match(raw)
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
    blob = "\n".join(docs) if docs else lean
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
        p = workspace / name
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
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
    """Union of citation groups, keyed by (declaration, text).

    The same text may legitimately appear on two declarations, so the key
    includes ``decl``; a floating copy of an already-anchored citation is dropped
    so the UI shows it once, under its declaration.
    """
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for c in group or []:
            text = (c.get("text") or "").strip().lower()
            if not text:
                continue
            key = ((c.get("decl") or "").lower(), text)
            if key in seen:
                continue
            seen.add(key)
            merged.append(c)
    anchored = {(c.get("text") or "").strip().lower() for c in merged if c.get("decl")}
    merged = [
        c
        for c in merged
        if c.get("decl") or (c.get("text") or "").strip().lower() not in anchored
    ]
    return merged[:32]


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


def _parse_model_payload(content: str) -> dict[str, Any]:
    """Accept either strict JSON or a bare Lean dump."""
    try:
        data = _extract_json(str(content))
    except (ValueError, json.JSONDecodeError):
        lean = _strip_lean_fences(str(content))
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
    lean = _strip_lean_fences(str(data.get("lean") or data.get("code") or ""))
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


def _count_sorry(lean: str) -> int:
    return len(re.findall(r"\b(sorry|admit)\b", lean))


def _chat(env: dict[str, str], model: str | None, prompt: str, timeout: int = 240) -> tuple[str, str]:
    from agent_monitor.settings import _http_json

    base, key, chosen = _resolve_llm(env, model)
    code, body = _http_json(
        f"{base}/chat/completions",
        method="POST",
        headers={"Authorization": f"Bearer {key}"},
        payload={
            "model": chosen,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    if code != 200 or not isinstance(body, dict):
        err = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        raise ValueError(f"Lean model call failed (HTTP {code}): {err or 'unknown error'}")
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Lean model returned an unexpected response shape") from exc
    return chosen, str(content)


def _run_lean_check(ldir: Path, uses_mathlib: bool) -> dict[str, Any]:
    """Compile Proof.lean. Prefer lake; fall back to bare `lean`."""
    tools = toolchain_status()
    if not tools["available"]:
        return {
            "ok": False,
            "status": "toolchain_missing",
            "command": None,
            "exit_code": None,
            "stdout": "",
            "stderr": (
                "Lean toolchain not found on this server. "
                "Install with: curl https://elan.lean-lang.org/elan-init.sh -sSf | sh "
                "then re-run Verify. The generated Proof.lean is still available below."
            ),
            "duration_s": 0.0,
        }

    env = os.environ.copy()
    elan_bin = Path(tools["elan"]).parent if tools.get("elan") else None
    if elan_bin:
        env["PATH"] = f"{elan_bin}:{env.get('PATH', '')}"

    started = time.time()
    cmd: list[str]
    # Mathlib projects need `lake build`. Core-only files can use `lean` directly,
    # which avoids a lake bootstrap on first run.
    if uses_mathlib and tools.get("lake"):
        cmd = [tools["lake"], "build"]
        cwd = str(ldir)
    elif tools.get("lean"):
        cmd = [tools["lean"], PROOF_FILENAME]
        cwd = str(ldir)
    elif tools.get("lake"):
        cmd = [tools["lake"], "build"]
        cwd = str(ldir)
    else:
        return {
            "ok": False,
            "status": "toolchain_missing",
            "command": None,
            "exit_code": None,
            "stdout": "",
            "stderr": "lean binary not executable",
            "duration_s": 0.0,
        }

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "failed",
            "command": cmd,
            "exit_code": None,
            "stdout": "",
            "stderr": "Lean check timed out after 300s",
            "duration_s": round(time.time() - started, 2),
        }
    except OSError as exc:
        return {
            "ok": False,
            "status": "failed",
            "command": cmd,
            "exit_code": None,
            "stdout": "",
            "stderr": f"failed to launch Lean: {exc}",
            "duration_s": round(time.time() - started, 2),
        }

    out = (proc.stdout or "")[-12000:]
    err = (proc.stderr or "")[-12000:]
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "status": "verified" if ok else "failed",
        "command": cmd,
        "exit_code": proc.returncode,
        "stdout": out,
        "stderr": err,
        "duration_s": round(time.time() - started, 2),
        "toolchain": tools.get("version"),
    }


def _detect_mathlib(lean: str) -> bool:
    return bool(re.search(r"(?m)^\s*import\s+Mathlib\b", lean or ""))


def _sorry_lines(lean: str) -> list[int]:
    return [i for i, ln in enumerate((lean or "").splitlines(), 1) if re.search(r"\b(sorry|admit)\b", ln)]


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
    src = lean or ""
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
    slines = _sorry_lines(src)
    if slines:
        flags.append(
            {
                "id": "sorry",
                "severity": "minor",
                "message": f"{len(slines)} sorry/admit — the proof has an admitted gap",
                "lines": slines[:8],
            }
        )
    decls = _decls_with_docs(src)
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
    problem = ""
    prob_file = workspace / "problem.txt"
    if prob_file.exists():
        try:
            problem = prob_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            problem = ""
    try:
        _src, proof = _proof_source(workspace, run_record)
    except (OSError, ValueError):
        proof = ""
    return problem, proof or ""


def read_source(workspace: Path) -> str:
    p = lean_dir(workspace) / PROOF_FILENAME
    if p.exists():
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
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
    (workspace / RESULT_FILENAME).write_text(
        json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return chat


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
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prev = load_cached(workspace) or {}
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
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model or prev.get("model"),
        "source": source or prev.get("source") or "",
        "title": title or prev.get("title") or "",
        "notes": notes,
        "status": status,
        "action": action,
        "uses_mathlib": uses_mathlib,
        "sorry_count": _count_sorry(lean_src),
        "sorry_lines": _sorry_lines(lean_src)[:20],
        "lean_path": f"{LEAN_DIRNAME}/{PROOF_FILENAME}",
        "lean": lean_src,
        "citations": cites,
        "fidelity": fidelity,
        "attempts": attempts,
        "toolchain": toolchain_status(),
        "log": log,
        "chat": chat if chat is not None else list(prev.get("chat") or []),
    }
    if extra:
        result.update(extra)
    (workspace / RESULT_FILENAME).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def _write_and_run(workspace: Path, lean_src: str, uses_mathlib: bool) -> dict[str, Any]:
    ldir = lean_dir(workspace)
    _write_scaffold(ldir, uses_mathlib=uses_mathlib)
    (ldir / PROOF_FILENAME).write_text(lean_src.rstrip() + "\n", encoding="utf-8")
    return _run_lean_check(ldir, uses_mathlib=uses_mathlib)


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
            for k in ("ok", "status", "exit_code", "duration_s", "command", "toolchain")
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
    )


def revise_with_feedback(
    *,
    workspace: Path,
    user: dict[str, Any] | None,
    message: str,
    model: str | None = None,
    lean: str | None = None,
    max_repairs: int = 1,
) -> dict[str, Any]:
    """Human-in-the-loop: revise Proof.lean from feedback, then re-check."""
    from agent_monitor.settings import resolved_user_env

    feedback = (message or "").strip()
    if not feedback:
        raise ValueError("Feedback is empty")
    lean_src = (lean if lean is not None else read_source(workspace)).strip()
    if not lean_src:
        raise ValueError("No Proof.lean yet — press Verify first")

    _append_chat(workspace, "user", feedback)
    prev = load_cached(workspace) or {}
    err_blob = str(prev.get("log") or "")[-6000:]
    env = resolved_user_env(user)
    prompt = (
        FEEDBACK_PROMPT.replace("{lean}", lean_src[:40000])
        .replace("{errors}", err_blob or "(no prior compiler log)")
        .replace("{feedback}", feedback[:4000])
    )
    chosen_model, content = _chat(env, model or prev.get("model"), prompt)
    payload = _parse_model_payload(content)
    lean_src = payload["lean"]
    uses_mathlib = bool(payload["uses_mathlib"]) or _detect_mathlib(lean_src)
    notes = payload.get("notes") or ""
    citations = list(payload.get("citations") or [])

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
                    for k in ("ok", "status", "exit_code", "duration_s", "command", "toolchain")
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
        problem, informal = _problem_and_proof(workspace)
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
    )


def generate(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None = None,
    max_repairs: int = 2,
    audit: bool = True,
) -> dict[str, Any]:
    """Translate the run's proof into Lean and attempt to verify it.

    A clean compile is not the end of it: with ``audit`` set, a second pass asks
    whether the Lean statement really is the problem's claim, and one
    statement-repair round follows when it isn't. Proving the wrong theorem
    convincingly is the failure mode this guards against.
    """
    from agent_monitor.settings import resolved_user_env

    problem = ""
    prob_file = workspace / "problem.txt"
    if prob_file.exists():
        problem = prob_file.read_text(encoding="utf-8", errors="replace").strip()

    source, proof = _proof_source(workspace, run_record)
    if not proof:
        raise ValueError("No proof text yet — wait for the run to produce proof.md")

    env = resolved_user_env(user)
    prompt = TRANSLATE_PROMPT.replace("{problem}", problem[:6000]).replace(
        "{proof}", proof[:50000]
    )
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
                    for k in ("ok", "status", "exit_code", "duration_s", "command", "toolchain")
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
    env = resolved_user_env(user)
    audit = _audit_faithfulness(
        env, model or prev.get("model"), problem=problem, proof=informal, lean=lean_src
    )
    if not audit.get("ok"):
        raise ValueError(audit.get("error") or "audit failed")
    flags = _fidelity_flags(lean_src)
    status = str(prev.get("status") or "none")
    # Re-derive the verdict only when the source on disk is what was last checked.
    if (prev.get("lean") or "") == lean_src and status in {"verified", "unfaithful"}:
        status = _apply_fidelity("verified", flags, audit)
    notes = _fidelity_summary(flags, audit) or "Audit found no fidelity issues."
    return _persist(
        workspace,
        lean_src=lean_src,
        status=status,
        uses_mathlib=_detect_mathlib(lean_src),
        attempts=list(prev.get("attempts") or []),
        notes=notes,
        title=str(prev.get("title") or ""),
        model=str(audit.get("model") or prev.get("model") or ""),
        source=str(prev.get("source") or ""),
        action="audit",
        audit=audit,
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


def harness_engines() -> list[dict[str, Any]]:
    """CLI agents usable for Lean work, with availability."""
    from agent_monitor.engines_registry import CLI_ENGINES, _cli_available

    out: list[dict[str, Any]] = []
    for eid, spec in CLI_ENGINES.items():
        if eid in _HARNESS_EXCLUDE:
            continue
        try:
            ok, _tmpl = _cli_available(spec)
        except Exception:  # noqa: BLE001 — availability probe must never break the UI
            ok = False
        out.append(
            {
                "id": eid,
                "label": spec.get("label") or eid,
                "available": bool(ok),
                "install_hint": spec.get("install_hint") or "",
            }
        )
    out.sort(key=lambda e: (not e["available"], e["id"] != "codex", e["id"]))
    return out


def _harness_key(workspace: Path) -> str:
    return str(workspace)


def harness_job(workspace: Path) -> dict[str, Any] | None:
    """The live harness job for a workspace, if one is still running."""
    with _HARNESS_LOCK:
        job = _HARNESS_JOBS.get(_harness_key(workspace))
        if not job:
            return None
        thread = job.get("thread")
        if thread is not None and not thread.is_alive():
            _HARNESS_JOBS.pop(_harness_key(workspace), None)
            return None
        return {k: v for k, v in job.items() if k not in {"thread", "proc"}}


def stop_harness(workspace: Path) -> dict[str, Any]:
    with _HARNESS_LOCK:
        job = _HARNESS_JOBS.get(_harness_key(workspace))
        proc = job.get("proc") if job else None
        if job:
            job["stopping"] = True
    if proc is None:
        raise ValueError("No harness run in progress")
    try:
        proc.kill()
    except OSError:
        pass
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


def _harness_env(engine: str, user: dict[str, Any] | None) -> dict[str, str]:
    """Agent environment: provider keys, plus Lean on PATH so it can compile."""
    from agent_monitor.engines_registry import engine_extra_path
    from agent_monitor.settings import resolved_user_env

    env = os.environ.copy()
    for k, v in (resolved_user_env(user) or {}).items():
        if v:
            env[k] = v
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
    if engine == "codex":
        from agent_monitor.jobs import _ensure_codex_auth

        owner_id = (user or {}).get("id")
        _ensure_codex_auth(env, owner_id)
    if engine == "openclaw":
        env.setdefault("NODE_OPTIONS", "--max-old-space-size=256")
    if engine == "openhands":
        key = env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY")
        if key:
            env.setdefault("LLM_API_KEY", key)
            env.setdefault("LLM_MODEL", env.get("AGENT_MONITOR_OPENHANDS_MODEL", "openai/gpt-5.2"))
    return env


def _running_record(
    workspace: Path,
    *,
    engine: str,
    label: str,
    lean_src: str,
    prev: dict[str, Any],
) -> dict[str, Any]:
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": prev.get("model"),
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
    """Launch a coding agent on Proof.lean in the background.

    Returns immediately with a ``running`` record; the UI polls the cache while
    the agent iterates. ``model`` applies to the post-run audit — each CLI agent
    picks its own model from its own configuration.
    """
    from agent_monitor.engines_registry import CLI_ENGINES, build_cli_command

    spec = CLI_ENGINES.get(engine)
    if not spec or engine in _HARNESS_EXCLUDE:
        raise ValueError(f"Unknown harness engine: {engine}")
    if harness_job(workspace):
        raise ValueError("A harness run is already in progress for this run")

    tools = toolchain_status()
    if not tools["available"]:
        raise ValueError(
            "Lean toolchain not installed on this server — a harness cannot compile "
            "anything. Install elan first, or use Verify."
        )

    ldir = lean_dir(workspace)
    lean_src = (lean if lean is not None else read_source(workspace)) or ""
    uses_mathlib = _detect_mathlib(lean_src)
    _write_scaffold(ldir, uses_mathlib=uses_mathlib)
    if lean_src.strip():
        (ldir / PROOF_FILENAME).write_text(lean_src.rstrip() + "\n", encoding="utf-8")
    problem, informal = _problem_and_proof(workspace, run_record)
    _write_task_file(ldir, problem=problem, proof=informal, existing=lean_src)

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

    label = str(spec.get("label") or engine)
    prev = load_cached(workspace) or {}
    record = _running_record(
        workspace, engine=engine, label=label, lean_src=lean_src, prev=prev
    )
    record["harness"]["command"] = " ".join(argv[:8])
    (workspace / RESULT_FILENAME).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    job: dict[str, Any] = {
        "engine": engine,
        "label": label,
        "started_at": time.time(),
        "state": "running",
        "proc": None,
        "stopping": False,
    }
    thread = threading.Thread(
        target=_harness_worker,
        kwargs={
            "workspace": workspace,
            "ldir": ldir,
            "argv": argv,
            "engine": engine,
            "label": label,
            "user": user,
            "model": model,
            "problem": problem,
            "informal": informal,
            "base": record,
            "audit": audit,
        },
        daemon=True,
        name=f"lean-harness-{engine}-{workspace.name}",
    )
    job["thread"] = thread
    with _HARNESS_LOCK:
        _HARNESS_JOBS[_harness_key(workspace)] = job
    thread.start()
    return record


def _harness_worker(
    *,
    workspace: Path,
    ldir: Path,
    argv: list[str],
    engine: str,
    label: str,
    user: dict[str, Any] | None,
    model: str | None,
    problem: str,
    informal: str,
    base: dict[str, Any],
    audit: bool,
) -> None:
    key = _harness_key(workspace)
    env = _harness_env(engine, user)
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
            (workspace / RESULT_FILENAME).write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
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
            model=model,
            problem=problem,
            informal=informal,
            audit=audit,
            stopped=False,
        )
        with _HARNESS_LOCK:
            _HARNESS_JOBS.pop(key, None)
        return

    with _HARNESS_LOCK:
        if key in _HARNESS_JOBS:
            _HARNESS_JOBS[key]["proc"] = proc

    last_flush = 0.0
    deadline = started + timeout_s
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if len(lines) > 4000:
                del lines[:1000]
            now = time.time()
            if now - last_flush > 2.5:
                flush()
                last_flush = now
            if now > deadline:
                proc.kill()
                lines.append(f"\n[agent-monitor] harness timeout after {timeout_s}s — killed\n")
                break
        exit_code = proc.wait(timeout=30)
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
            model=model,
            problem=problem,
            informal=informal,
            audit=audit,
            stopped=stopped,
        )
    finally:
        with _HARNESS_LOCK:
            _HARNESS_JOBS.pop(key, None)


def _harness_finish(
    *,
    workspace: Path,
    engine: str,
    label: str,
    log: str,
    exit_code: int | None,
    elapsed: float,
    user: dict[str, Any] | None,
    model: str | None,
    problem: str,
    informal: str,
    audit: bool,
    stopped: bool,
) -> dict[str, Any]:
    """Take the agent's word for nothing: re-check and re-audit what it left."""
    from agent_monitor.settings import resolved_user_env

    prev = load_cached(workspace) or {}
    lean_src = read_source(workspace).strip()
    tail = log[-4000:]
    if not lean_src:
        return _persist(
            workspace,
            lean_src="",
            status="failed",
            uses_mathlib=False,
            attempts=list(prev.get("attempts") or []),
            notes=f"{label} produced no Proof.lean.",
            title=str(prev.get("title") or ""),
            model=prev.get("model"),
            source=f"{label} harness",
            action="harness",
            extra={
                "log": tail,
                "harness": {
                    "engine": engine,
                    "label": label,
                    "state": "stopped" if stopped else "finished",
                    "exit_code": exit_code,
                    "elapsed_s": elapsed,
                    "log": tail,
                },
            },
        )

    uses_mathlib = _detect_mathlib(lean_src)
    check = _write_and_run(workspace, lean_src, uses_mathlib)
    sorry_n = _count_sorry(lean_src)
    status = _status_from_check(check, sorry_n, mode="check")
    attempts = list(prev.get("attempts") or [])
    attempts.append(
        {
            "round": len(attempts),
            "uses_mathlib": uses_mathlib,
            "sorry_count": sorry_n,
            "action": f"harness/{engine}",
            "check": {
                k: check[k]
                for k in ("ok", "status", "exit_code", "duration_s", "command", "toolchain")
                if k in check
            },
            "stderr_tail": (check.get("stderr") or "")[-2000:],
            "stdout_tail": (check.get("stdout") or "")[-1000:],
        }
    )

    audit_result: dict[str, Any] | None = None
    if audit and status in {"verified", "incomplete"}:
        audit_result = _audit_faithfulness(
            resolved_user_env(user),
            model or prev.get("model"),
            problem=problem,
            proof=informal,
            lean=lean_src,
        )
        status = _apply_fidelity(status, _fidelity_flags(lean_src), audit_result)
    if stopped:
        status = "stopped" if status not in {"verified", "unfaithful"} else status

    notes = _check_summary(lean_src, check, status)
    fid_note = _fidelity_summary(_fidelity_flags(lean_src), audit_result)
    verdict = "stopped by user" if stopped else f"exit {exit_code}"
    notes = f"{label} harness ({verdict}, {elapsed}s). {notes} {fid_note}".strip()
    compiler_log = ((check.get("stderr") or "") + "\n" + (check.get("stdout") or "")).strip()
    return _persist(
        workspace,
        lean_src=lean_src,
        status=status,
        uses_mathlib=uses_mathlib,
        attempts=attempts,
        notes=notes,
        title=str(prev.get("title") or ""),
        model=prev.get("model"),
        source=f"{label} harness",
        action="harness",
        audit=audit_result,
        extra={
            "harness": {
                "engine": engine,
                "label": label,
                "state": "stopped" if stopped else "finished",
                "exit_code": exit_code,
                "elapsed_s": elapsed,
                "log": tail,
            },
            # Prefer the compiler's own words in the log pane; the agent
            # transcript stays available under `harness.log`.
            "log": compiler_log or tail,
        },
    )
