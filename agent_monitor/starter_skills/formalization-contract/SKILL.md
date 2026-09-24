---
name: formalization-contract
description: Preserve mathematical meaning between an original problem, informal proof, and Lean formalization. Use when translating to Lean, reviewing formal coverage, auditing quantifiers or assumptions, or deciding whether formal verification supports the original claim.
---

# Formalization contract

1. Assign stable identifiers to the original claim and every lemma used by the informal proof.
2. Map each identifier to its exact quantifiers, domains, hypotheses, constants, conclusion, and Lean declaration.
3. Run a statement-fidelity preflight when available, then perform a semantic mathematical review; a clear preflight is not proof of equivalence.
4. Reject weakened conclusions, strengthened or inconsistent assumptions, changed endpoint conditions, and vacuous encodings.
5. Compile every declaration and report sorry or admit per declaration.
6. Mark a statement formally covered only when a faithful, admission-free declaration proves it.
7. Keep unmatched informal statements gray and preserve verified helper declarations even when the main theorem remains incomplete.
8. Rebuild formal DAG and coverage after any informal or Lean edit.

Kernel acceptance certifies only the encoded theorem. It does not by itself certify fidelity to the original problem.
