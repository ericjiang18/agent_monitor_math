# Candidate T controlled evaluation

Status: final. The fixed 24-hour queues were frozen and drained on 2026-08-24.

Candidate T is Candidate S plus `system_prompt_candidate_t_delta.md`. The delta
makes the file-backed proof-audit contract an early, budgeted obligation: freeze
the candidate early, start independent global/decomposed passes before the final
quarter, reserve at least five usable calls for merge/validation/reconciliation,
and fail conservatively when workspace capabilities are unavailable. The delta's
current revision also requires a byte-identical final-candidate snapshot,
raw-byte digests, explicit post-repair reruns, and an exact-final refuter rerun
for high-stakes outcomes. Its SHA-256 is
`15992b88e3948248a3b9f6744c45dbb60b5c4a48afe0b9865767f5c2cdf19ca6`.

All six artifacts below used OpenClaude, `gpt-5.6-sol`, the same target within
each pair, and disabled subagents. Every final artifact has exactly one
`Partial Progress` outcome. The repository's trusted finalizer independently
validated the global, decomposed, merged, provenance, and run-manifest structure
for all six runs under the evaluation-time contract. A later forward test found
that this did not bind the final proof to the audited frozen candidate. The new
fail-closed finalizer therefore marks all six `unreconciled`; none is counted as
a complete audit, although their mathematical artifacts remain useful evidence.
No run claims to solve an Erdős problem.

## Pair 1: frozen order-11 lemma for Erdős #944, 40-iteration cap

| Prompt | Run | Input | Output | Latency | Cost | Tool calls | Artifact |
|---|---|---:|---:|---:|---:|---:|---:|
| S | `openclaude_audit24h_erdos944_order11_audit_openclaude_003_9a23efb8b8` | 573,924 | 51,163 | 1,059.953s | $4.511447 | 57 | 11,772 B |
| T | `openclaude_audit24h_erdos944_order11_audit_openclaude_001_9421fff86f` | 565,388 | 40,596 | 832.631s | $3.988784 | 48 | 11,477 B |

Both prove and audit the same reusable result: every finite graph satisfying the
frozen #944 hypotheses has at least 11 vertices. Both repair omitted complement
clique-partition details and explicitly avoid extrapolating the lemma into an
existence or novelty claim. T is about 21.4% faster, 11.6% cheaper, and uses
20.6% fewer output tokens, with no observed quality regression.

## Pair 2: Erdős #886 primary-source audit, 100-iteration cap

| Prompt | Run | Input | Output | Latency | Cost | Tool calls | Artifact |
|---|---|---:|---:|---:|---:|---:|---:|
| S | `openclaude_audit24h_erdos886_audit_openclaude_008_f9c882a6da` | 1,350,394 | 45,743 | 1,228.417s | $8.873334 | 88 | 16,030 B |
| T | `openclaude_audit24h_erdos886_audit_openclaude_001_a687ca2905` | 2,466,113 | 83,611 | 2,054.790s | $15.951537 | 36* | 21,036 B |

The S artifact exhausted its ten-query literature ceiling without obtaining the
pivotal Erdős--Rosenfeld paper. It retained a correct elementary quarter-power
bound, disclosed the missing source, and stopped conservatively.

T resolved the primary publisher PDF and inspected all seven pages. Its global
and decomposed passes independently found that the fourth factor-difference
inequality printed in Proposition 4.2 is reversed for every positive parameter.
The decomposed pass also found four omitted factor pairs in an intermediate
candidate. T repaired the candidate, gave an exact polynomial certificate for
the source defect, and separately proved that the weaker four-larger-divisors
window still holds. It correctly states that neither fact resolves #886. This is
a material source-audit quality gain, not just a longer write-up.

`*` The T run used a same-harness continuation, so its persisted tool-call count
is not directly comparable with S; token, latency, cost, artifact, and validation
records include the completed lineage.

## Pair 3: general Erdős #944 audit, 100-iteration cap

| Prompt | Run | Input | Output | Latency | Cost | Tool calls | Artifact |
|---|---|---:|---:|---:|---:|---:|---:|
| S | `openclaude_audit24h_erdos944_audit_openclaude_005_3d1cdb3156` | 1,529,189 | 38,388 | 826.681s | $8.852685 | 61 | 18,741 B |
| T | `openclaude_audit24h_erdos944_audit_openclaude_001_3cc86d1c33` | 1,153,250 | 42,961 | 1,011.034s | $7.152595 | 54 | 14,213 B |

Both independently derive `delta(G) >= 6` and exclude orders below 10, while
leaving existence unresolved. S's independent passes accepted its frozen
candidate. T's passes found two repairable issues in its own frozen candidate:
an incomplete bibliography/stale status ledger and an under-expanded order-nine
path/cycle classification. T retained those findings, expanded every boundary
case, reran locked proof/citation/fidelity checks, and records the repairs in a
nonempty run manifest. T is slower here, but uses 24.6% fewer input tokens and
19.2% less recorded cost, with equivalent final mathematical scope and stronger
visible self-correction evidence.

That self-correction was recorded in prose and run metadata but was not bound to
explicit global/decomposed reruns of the byte-identical final artifact. It is
therefore evidence for Candidate T's behavior, not evidence of complete audit
coverage under the strengthened contract.

## Frozen 24-hour result

The final deduplicated aggregate contains 328 non-UCLA, non-Danus runs: 300
finished, 27 failed, and one stopped. The only outcome labels are 241 `Partial
Progress` and 70 `Known/Open Status`; there are zero `Solved` and zero
`Counterexample` results. Eleven audits satisfy the exact-final independent
reconciliation contract and six more validate with explicitly degraded reviewer
independence. A separate strict verdict scan records 6/328 candidates accepted by
their final audit and 322/328 not accepted. Every accepted candidate remains a
bounded lemma or honest status report, not an open-problem solution.

Recorded adapter telemetry is 231,945,547 input tokens, 5,993,354 output tokens,
215,557.176 cumulative latency seconds, and $1,011.2463844. This is observability
data rather than a provider invoice; OpenHands in particular exposes sparse usage
metadata.

The evaluation also exposed two last-mile reliability requirements now included in
T: keep audit history out of the mathematical candidate so independent reviewers
receive a clean frozen artifact, and do not write a terminal `run.json` until all
referenced outputs and the validator certificate exist. DeepAgents receives a
three-hour default wall-clock allowance for full file-backed audits, with a bounded
per-engine override.

## Final decision

Candidate T wins for the requested maximum-reliability objective.
Pair 1 shows no regression and better efficiency; Pair 3 shows equivalent
mathematics plus explicit repair closure; Pair 2 supplies the decisive quality
gain by finding and correctly scoping a genuine error in a peer-reviewed pivotal
source. Candidate S remains the simpler predecessor. The controlled T trials
predate the strengthened exact-final reconciliation gate and are therefore
behavioral evidence, not retroactive complete-audit claims. The frozen forward
evaluation supplies the current validator-backed evidence. Candidate T's deployed
body is 14,190 bytes with SHA-256
`ae66c22073fe22926ac52529b2287ff36277e2c82e8eec0ede8c462e26e33a36`;
the compare-and-swap promotion and post-deployment smoke both passed.
