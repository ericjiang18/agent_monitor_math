"""Starter skills and memory scaffolds seeded into a fresh agent home.

These are plain Markdown so they work with every engine: Hermes indexes them
natively from ``HERMES_HOME/skills/``, and for engines that cannot read that
directory the enabled skills are rendered into the task prompt (see
``agent_config.persona_preamble``).
"""
from __future__ import annotations

STARTER_SKILLS: dict[str, str] = {
    "proof-strategies": """# Proof strategies

Pick a strategy before writing. If one stalls after a genuine attempt, say so
explicitly and switch rather than padding the write-up.

- **Direct**: unfold definitions, chain known implications.
- **Contradiction**: assume the negation, derive an impossibility. State the
  negation precisely — this is where most errors hide.
- **Contrapositive**: prove `¬Q ⇒ ¬P` when `P ⇒ Q` is awkward.
- **Induction**: state the induction hypothesis as its own sentence. Prove the
  base case separately, and check the step actually uses the hypothesis.
- **Extremal / pigeonhole**: take a minimal counterexample or a largest
  element; derive a smaller one or a collision.
- **Construction**: build the object explicitly, then verify each required
  property one by one.
- **Reduction**: map the problem to a solved one and justify the mapping both
  ways.

Before finalising, name the strategy in one sentence at the top of the proof.
""",
    "verification-checklist": """# Verification checklist

Run through this before declaring a proof finished. Verify with code where a
computation is involved — do not eyeball arithmetic.

1. **Quantifiers**: is the order of `∀` / `∃` the one the claim needs?
2. **Base cases**: covered, including the degenerate ones (`n = 0`, empty set,
   single element)?
3. **Division / roots**: every denominator non-zero, every root well defined?
4. **Hidden assumptions**: did a step silently assume positivity, integrality,
   finiteness, or continuity?
5. **Converse creep**: did an `⇒` get used as an `⇔` anywhere?
6. **Named results**: does each cited theorem's hypothesis actually hold here?
7. **Numeric sanity**: test small cases in Python (`sympy` / plain arithmetic).
   A counterexample at `n = 3` saves a wrong proof.
8. **Self-containment**: can a reader follow the write-up without the scratch
   work?

If a check fails, fix the proof — do not soften the claim silently.
""",
    "citation-hygiene": """# Citation hygiene

Every non-obvious ingredient must be attributable. This makes a proof auditable
and is required for the run's References section.

- Cite **named results** you invoke (Euclid's lemma, Fermat's little theorem,
  Cauchy–Schwarz, Zorn's lemma, …) with the exact statement you rely on.
- Cite **external sources** with a resolvable identifier: `arXiv:2401.01234`,
  `doi:10.1007/...`, a book with edition and theorem number, or a URL.
- Mark steps you derived yourself as `self-derived` — do not dress them up as
  literature.
- Never invent a reference. If you cannot locate a source, write
  `[uncited: standard result]` and give a short proof inline instead.
- Keep inline markers `[1]`, `[2]` consistent with the `## References` list, and
  make sure every listed reference is actually cited somewhere.
""",
    "literature-search": """# Literature search

Use this when the problem looks like known research, or when a lemma feels like
it should already exist.

1. Search for the **statement**, not the topic: distinctive phrases, extremal
   bounds, sequence terms (try OEIS for integer sequences).
2. Prefer primary sources: arXiv abstracts, journal DOIs, standard textbooks.
3. For each hit worth keeping, record in your notes:
   `identifier · title · what exactly it gives you · how it is used here`.
4. Verify a source says what you think it says before citing it — read the
   theorem statement, not just the abstract.
5. If the result already exists, cite it and prove only the gap that remains.

Record findings in the proof's `## References` section as you go, not at the end.
""",
    "write-up-style": """# Write-up style

`proof.md` is the deliverable. Keep it clean and self-contained; scratch work
belongs in separate files.

- Structure: **Claim → Strategy → Setup/Notation → Lemmas → Main proof →
  References**.
- Math in LaTeX: `$...$` inline, `$$...$$` display. Do not mix in plain-text
  pseudo-math.
- One idea per paragraph. Number lemmas and refer to them by number.
- State each lemma fully before proving it; end proofs with `∎`.
- Justify every step by a definition, a numbered lemma, or a citation `[n]`.
- Say plainly what is *not* proved when the argument is incomplete. An honest
  gap is worth more than a hidden one.
""",
}

MEMORY_SCAFFOLDS: dict[str, str] = {
    "MEMORY.md": """# Agent memory

Durable notes carried across runs. Keep entries short and factual; delete what
turns out to be wrong.

## Environment
- Proofs are written to `proof.md` in the run workspace; Python is available for
  numeric checks.

## Techniques that worked
<!-- e.g. "descent argument closed the sqrt(p) irrationality family quickly" -->

## Pitfalls seen
<!-- e.g. "forgot the n=1 base case twice — check base cases first" -->
""",
    "USER.md": """# About the user

Preferences that should shape every run. Keep it to things that change your
behaviour.

- Wants rigorous informal proofs: every step justified, gaps stated explicitly.
- Wants named results and external sources cited (see the citation-hygiene
  skill).
""",
}
