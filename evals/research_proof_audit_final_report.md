# Research-proof audit: final 24-hour report

Status: complete on 2026-08-24. Candidate T is deployed. No evaluated run
solved or refuted an open Erdős problem.

## What was integrated

The supplied ensemble paper-audit workflow is installed as the
`research-proof-audit` starter skill. It preserves independent global and
decomposed review, conservative merging, risk-selected boundary/refuter/
citation/display passes, exact-final hash reconciliation, and fail-closed
outcomes. ProvingConsole materializes the enabled skill and locked audit tools
inside the selected harness workspace without changing the chosen harness or
model.

Candidate T, Candidate S plus `system_prompt_candidate_t_delta.md`, is the
maximum-reliability prompt. It reserves audit budget early, isolates a clean
frozen candidate, requires global/decomposed passes before the final quarter,
binds reruns to the exact final bytes, and requires an exact-final refuter for
high-stakes outcomes. The deployed body is 14,190 bytes with SHA-256
`ae66c22073fe22926ac52529b2287ff36277e2c82e8eec0ede8c462e26e33a36`.

No external plugin was added. Zotero could help only when a private literature
library is connected; adding it mid-run would have changed the fixed experiment.
The existing bounded web, source/PDF, citation, local proof, and Lean tools
covered the evaluation requirements.

## Frozen 24-hour result

The final deduplicated aggregate contains 328 non-UCLA, non-Danus runs:

- 300 finished, 27 failed, and one stopped;
- 241 `Partial Progress`, 70 `Known/Open Status`, zero `Solved`, and zero
  `Counterexample` outcomes;
- 11 `complete_validated` independent audits and 6 validated audits with
  disclosed degraded reviewer independence;
- 6/328 strict audit-accepted candidates and 322/328 not accepted;
- zero entries in the manual high-stakes review queue;
- recorded adapter telemetry of 231,945,547 input tokens, 5,993,354 output
  tokens, 215,557.176 cumulative latency seconds, and $1,011.2463844.

The six strict accepted artifacts are bounded lemmas or honest status reports.
None is an open-problem solution. Telemetry is observability data, not a provider
invoice, because some engines expose incomplete usage metadata.

Candidate T wins over S for reliability. The decisive controlled #886 run
retrieved the primary publisher PDF, found that a printed Proposition 4.2
factor-difference inequality is reversed for every positive parameter, repaired
the candidate, preserved the weaker valid divisor-window theorem, and refused
to claim the original open problem solved. This is a useful source correction,
not a solution of Erdős #886.

## Post-deployment proof

A fresh fixed-target run used the deployed Candidate T and current skill on
IMProof, DeepSeek Harness, and MetaHarness. All three finished as
`Partial Progress`, all three were independently `complete_validated`, and the
manual-review queue remained empty. DeepSeek's candidate was accepted with zero
open findings. IMProof and MetaHarness completed the execution contract but each
retained one open finding, so they were not mathematically accepted. This
demonstrates that execution success and proof acceptance remain separate.

Two simultaneous real Lean kernel checks also passed in isolated workspaces
with distinct source hashes and no stale-log or source leakage.

## Verification and boundaries

- Full maintained suite: 289 tests and 9 subtests passed.
- Python compilation, `git diff --check`, first-party secret scan, skill
  structural validation, authenticated/anonymous local smoke, and public
  prefixed HTTPS health/login checks passed.
- UCLA was excluded at the operator's request; Danus was not evaluated.
- Historical failures and weaker/unreconciled audits remain visible in the
  aggregate; they were not relabelled after fixes.
- Candidate T improves the probability of detecting unsupported claims. It does
  not make a model-generated proof true and does not justify novelty or solution
  claims without independent mathematical validation.

## Evidence

- Final aggregate: `results/research-proof-audit-aggregate-final.json`
- Post-deployment evaluation:
  `results/research-proof-audit-postdeploy-20260824T0752Z/finalized.json`
- Prompt comparison: `system_prompt_candidate_t_eval.md`
- Final prompt recommendation: `final_prompt_recommendation.md`
- Detailed completion audit: `research_proof_audit_completion_audit.md`
