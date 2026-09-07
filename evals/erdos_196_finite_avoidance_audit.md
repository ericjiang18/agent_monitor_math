# Erdős #196 finite-avoidance checkpoint

Source run: `openclaw_audit24h_erdos196_audit_openclaw_022_90937174c9`
Reviewed: 2026-08-23 UTC

## Retained mathematical checkpoint

The run gives a self-contained parity-recursion proof that every finite
set of integers has an ordering containing no monotone three-term arithmetic
progression, hence no monotone four-term arithmetic progression.  Translate
the set so its minimum is zero, order the odd and even parts recursively after
affine division by two, and concatenate the odd block before the even block.
An even common difference would create a forbidden progression inside one
recursive block; an odd common difference would require an element of one
parity block to lie between two elements of the other block.

This is valid partial progress and a useful boundary certificate.  It has the
quantifier pattern `for every finite A, there exists an order P_A`.  It does
not produce a compatible sequence of finite orders or one order of all natural
numbers, so it does not solve Erdős #196.  No novelty claim is made.

The run's bounded checker tested the recursive construction for intervals
`[1,n]`, `1 <= n <= 32`, and four additional finite sets, checking 40,964
triples without a violation.  That computation corroborates only the declared
finite scope; the induction proof is the mathematical justification.

## Audit limitation

The run predates the exact-final reconciliation contract.  Its final
`proof.md` was not byte-bound to a frozen final candidate with explicit
post-repair global/decomposed reruns, so the aggregate correctly classifies
the file-backed audit as `unreconciled`.  The finite lemma remains readable
partial evidence, but the run is not a completed candidate-proof audit and
contains no solution or counterexample to the original open problem.
