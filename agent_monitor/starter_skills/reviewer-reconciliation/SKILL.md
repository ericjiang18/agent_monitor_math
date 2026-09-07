---
name: reviewer-reconciliation
description: Incorporate critic, checker, source, or human feedback without leaving stale mathematical claims. Use after Human-in-the-loop intervention, a counterexample, a source upgrade, a fidelity failure, or any correction that invalidates downstream proof content.
---

# Reviewer reconciliation

1. Enumerate every substantive objection separately before editing.
2. Link each objection to affected claims, sources, computations, Lean declarations, DAG nodes, and outcome labels.
3. Repair the claim or retain it as an explicit unresolved obligation with downgraded status.
4. Invalidate and rerun every downstream obligation that depended on the changed claim.
5. Write the corrected claim and its logical negation, then search headings, lemmas, ledgers, captions, summaries, formal statements, and conclusions for literal or paraphrased stale variants.
6. Remove stale variants or quarantine them in a clearly labeled failed-attempt section.
7. Rerun exact arithmetic, citation, Lean, fidelity, and artifact checks relevant to the correction.
8. Promote only the reconciled artifact and retain exactly one final outcome label.

A new correct paragraph does not neutralize an older contradictory paragraph.
