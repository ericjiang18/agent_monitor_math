# ProvingConsole research integration

## Claim ledger

Give every mathematical assertion a stable `claim_id`. Preserve these fields outside
the locked verifier output when the paper schema does not contain them:

- exact original statement and normalized statement;
- quantifiers, domains, constants, and endpoint conditions;
- `depends_on` claim IDs and downstream consumers;
- source references and exact imported hypotheses;
- computation certificate IDs;
- Lean declaration and statement-fidelity status;
- status: `open`, `claimed`, `discharged`, `failed`, or `invalidated`.

Never mutate a claim silently. Create a revision, invalidate descendants, and preserve
the failed version as audit history.

## DAG mapping

Map a verifier finding to every affected claim, not only to a text location. A finding
on an imported lemma invalidates all reachable consumers until repaired. Rebuild both
informal and formal DAG views after each repair. Keep formally verified claims green,
unmatched claims gray, failed claims red, and invalidated descendants amber.

## Pass selection

Run global plus decomposed audits for a complete candidate solution. Add the refuter for
every proposed solution of an open problem. Add boundary/clause when there are indexed
arguments, constructions, extremal cases, floors, strict inequalities, or small
exceptions. Add display sweep when the proof depends on dense symbolic derivations.
Run citation fidelity only after an independent current-status search identifies the
primary sources that matter.

## Decisive finding gate

Before promoting a central finding or a solved outcome, ask:

1. Is it about the exact original statement rather than a restatement?
2. Is the alleged failure or repair reproduced independently?
3. Does exact computation or Lean settle the point when applicable?
4. Did the check cover the legal domain and first excluded boundary?
5. Were all downstream claims invalidated and rechecked?
6. Does a current primary source change the novelty or open-status conclusion?

Route any unresolved answer to Human in the loop with one concrete question and the
minimum evidence needed to decide it.

## Outcome mapping

`accept` means only that the configured audit found no reportable defect within its
documented coverage. It does not map directly to `Solved`. A nonempty set of central
unresolved checks maps to `Partial Progress` or `Known/Open Status`, never `Solved`.
A verified counterexample may map to `Counterexample`; a proof-local failure merely
rejects that candidate proof.
