"""Starter skills and memory scaffolds seeded into a fresh agent home.

These are plain Markdown so they work with every engine: Hermes indexes them
natively from ``HERMES_HOME/skills/``, and for engines that cannot read that
directory the enabled skills are rendered into the task prompt (see
``agent_config.persona_preamble``).
"""
from __future__ import annotations

from pathlib import Path

_STARTER_SKILLS_DIR = Path(__file__).with_name("starter_skills")

STARTER_SKILLS: dict[str, str] = {
    "proof-strategies": r"""---
name: proof-strategies
description: Strategies for rigorous informal mathematical proofs, reductions, and bounded structural audits.
---
# Proof strategies

Pick a strategy before writing. If one stalls after a genuine attempt, say so
explicitly and switch rather than padding its length.

- **Direct**: unfold definitions and chain known implications.
- **Contradiction**: assume the negation, derive an impossibility, and state the
  negation precisely.
- **Contrapositive**: prove $\neg Q\Rightarrow\neg P$ when $P\Rightarrow Q$ is
  awkward.
- **Induction**: state the induction hypothesis separately, prove the base case,
  and verify that the step actually uses it.
- **Extremal / pigeonhole**: choose a minimal counterexample or largest element
  and derive a smaller object or a collision.
- **Construction**: build the object explicitly, then verify each required
  property one by one.
- **Reduction**: map the problem to a solved one and justify the mapping in both
  directions.

## Bounded structural audits

When a problem supplies a degree bound and a small order, do not treat an
exhaustive search as the proof. First classify all connected components allowed
by the degree bound, derive an exact invariant formula on each component, and
translate the global condition using additivity. For vertex-critical conditions,
subtract the global invariant before and after deleting a vertex; additivity
then forces the corresponding one-vertex drop in the component containing that
vertex, for **every** vertex of that component. Classify components satisfying
that local condition, then count possible component multisets. This often
replaces a finite enumeration by a short complete argument. See
`references/bounded-complement-audit.md` for the path/cycle clique-partition
pattern used in a $\Delta\le2$ complement audit.

### Pitfalls

1. Include degenerate components, especially isolated vertices and short cycles;
   path/cycle formulas often have exceptional small cases.
2. Prove both directions of each component classification: checking one
   representative vertex is insufficient when the hypothesis is universal.
3. Verify additivity after deletion, since deleting a vertex may split a path;
   include all resulting pieces in the invariant calculation.
4. Use computation only as an adversarial check or census, and report its search
   space and result separately from the mathematical proof.

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
    "literature-search": r"""---
name: literature-search
description: Reproducible search, source verification, applicability audit, and research-status reporting for open mathematical problems.
---

# Literature search

Use this skill when a mathematical claim may already be known, when research status matters, or when an external theorem supplies a bound or reduction.

## Workflow

1. **Search for the statement, not merely the topic.** Use distinctive phrases, parameterized bounds, theorem names, and sequence terms. Prefer primary sources: arXiv records and source, journal DOI pages, standard monographs, and authoritative problem databases.
2. **Record candidate sources immediately.** For each source, note `identifier · title · exact result · how it applies`. Keep this record in the proof's `## References` section or a concise supporting note.
3. **Verify the exact theorem.** Read the theorem/proposition statement, not just an abstract or a database summary. Check the proposition number, all hypotheses, parameter conventions, and whether the conclusion is exactly the claimed one.
4. **Use a reproducible arXiv fallback chain.** Query the arXiv API for title, authors, version, date, and comments. If PDF text extraction is unavailable, download the source archive and inspect the `.tex` proposition/theorem statement directly. Record the exact version (`v1`, etc.) and date actually verified.
5. **Audit applicability separately from quotation.** Check that the source's terminology (for example, a parameterized graph class) is definitionally equivalent to the user's hypotheses. If equivalence is not immediate, prove it inline rather than silently identifying the notions.
6. **Separate imported results from current deductions.** State which bound is proved by the cited source and which facts are self-derived. Never infer a global lower bound from excluding one order unless every smaller order relevant to the claim has also been excluded.
7. **Check research maturity honestly.** For arXiv-only evidence, call it a preprint unless journal publication or peer-review status has been independently verified. Do not upgrade a source's status based on title pages, citations, or assumptions.
8. If a result already exists, cite it and prove only the remaining gap.

## Revision and stale-claim pitfall

When revising an existing mathematical notebook, do not leave a superseded numerical claim in the main argument merely because a correction section follows it. Replace the stale claim in place, retain the earlier reasoning as an explicitly labeled audit/regression artifact when useful, and foreground the strongest verified external result. Ensure every inline citation marker appears exactly once in the References section and every listed reference is used.

## Supporting detail

For the arXiv source-verification sequence and the Proposition-5.1 audit pattern used in a prior graph-criticality review, see `references/arxiv-source-audit.md`.
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

STARTER_SKILL_PACKAGES: dict[str, Path] = {
    path.parent.name: path.parent
    for path in _STARTER_SKILLS_DIR.glob("*/SKILL.md")
    if path.is_file()
}
for _name, _package in STARTER_SKILL_PACKAGES.items():
    STARTER_SKILLS[_name] = (_package / "SKILL.md").read_text(encoding="utf-8")


STARTER_SKILL_REFERENCES: dict[str, dict[str, str]] = {
    "proof-strategies": {
        "bounded-complement-audit.md": r"""# Bounded complement audit: reusable pattern

## Structural template

Suppose a graph $H$ has maximum degree at most $2$. Every component is a path
$P_m$ or cycle $C_m$. For clique-partition number $\theta$:

- $\theta(P_m)=\lceil m/2\rceil$;
- $\theta(C_3)=1$;
- $\theta(C_m)=\lceil m/2\rceil$ for $m\ge4$.

These formulas follow because cliques have size at most two except in $C_3$,
and a minimum clique partition pairs as many vertices as possible.

## Vertex-critical translation

If $\theta(H-v)=\theta(H)-1$ for every vertex and $H$ is a disjoint union of
components, additivity implies that every component $C$ satisfies

$$
\theta(C-v)=\theta(C)-1\quad\text{for every }v\in V(C).
$$

Classify this condition rather than testing only one vertex:

- $P_1$ works;
- every $P_m$ with $m\ge2$ fails (for even $m$, an endpoint deletion gives no
drop; for odd $m\ge3$, deleting the second vertex gives $P_1\sqcup P_{m-2}$ and the wrong value);
- $C_3$ fails because deleting a vertex leaves $P_2$ with the same clique-partition
number;
- $C_m$ works exactly when $m\ge5$ is odd, since deleting a vertex leaves
$P_{m-1}$ and the ceiling formula drops by one precisely for odd $m$.

Thus allowable components are isolated vertices and odd cycles of length at
least five. An isolated vertex contributes $(|V|,\theta)=(1,1)$; an allowed
cycle with clique contribution $q$ has order $2q-1$. If the total clique
partition number is $Q$, then with $s$ isolated vertices and $t$ cycles,

$$
|V(H)|=s+\sum_{i=1}^t(2q_i-1)=2Q-s-t\le 2Q.
$$

For $Q=4$, order $9$ is impossible; direct enumeration of all path/cycle
component multisets of order $9$ is only a sanity check, not the proof.

## Audit checklist

- Check $C_3$ separately.
- Check both endpoint and internal deletions in paths.
- State explicitly that additivity is applied after deletion.
- Distinguish the mathematical classification from the number of enumerated
  multisets.
""",
    },
    "literature-search": {
        "arxiv-source-audit.md": r"""# ArXiv source audit pattern

For a research write-up relying on a specific proposition:

1. Query `https://export.arxiv.org/api/query?id_list=<id>` and record title, authors, version, publication/update date, page comment, and abstract status.
2. Download `https://arxiv.org/src/<id>` and inspect the source archive when PDF extraction is unavailable. Search the extracted `.tex` for the proposition label, exact numerical bound, and the definition of the source's parameterized class.
3. Compare the source definition with the user's quantified statement. For the Skottova–Steiner graph problem, a `(k,r)`-graph means a `k`-vertex-critical graph in which no set of at most `r` edges is critical; the target one-edge condition is the `k=4,r=1` specialization.
4. Cite the exact version, e.g. `arXiv:2508.08703v1`, and state preprint status unless a journal record is independently verified.
5. When correcting an earlier bound, edit the stale claim in place and label any retained weaker argument as an audit artifact. Excluding order `n` alone does not imply a lower bound above `n` unless smaller orders are handled too.
""",
    },
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
