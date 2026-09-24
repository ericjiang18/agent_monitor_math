# Final system-prompt recommendation

Validated through the completed 24-hour freeze on 2026-08-24.

## Winner: Candidate T

Use Candidate S plus `system_prompt_candidate_t_delta.md` for
maximum-reliability open-problem research. Candidate T keeps S's truth, source,
formal-fidelity, and reconciliation gates, then reserves budget early for an
independent file-backed audit of the exact final candidate.

- Candidate S file SHA-256:
  `0d157d5163953e3110569e840f5cc398e1ed58c3801475615834df3db9cd2821`
- Candidate T delta SHA-256:
  `15992b88e3948248a3b9f6744c45dbb60b5c4a48afe0b9865767f5c2cdf19ca6`
- deployed Candidate T body SHA-256:
  `ae66c22073fe22926ac52529b2287ff36277e2c82e8eec0ede8c462e26e33a36`
- strongest differentiators: early audit-budget reservation, clean frozen
  candidate isolation, exact-final hash binding, explicit post-repair reruns,
  high-stakes refuter reruns, and fail-closed terminal manifests

No evaluated prompt solved an open problem. The frozen aggregate contains 328
runs, zero `Solved`, zero `Counterexample`, 11 exact-final complete independent
audits, and six validated audits with disclosed degraded independence. Candidate
T is selected because the controlled pairs preserved useful mathematics while
improving early closure and, decisively, finding and correctly scoping a printed
source error in the pivotal #886 paper.

## Ranked alternatives

1. **Candidate T** — maximum-reliability default.
2. **Candidate S** — simpler predecessor when file-backed ensemble auditing is
   unavailable.
3. **Candidate Q** — lower-cost predecessor; the controlled #413 A/B ended
   correctly but Q discovered the decisive parameter translation later and S
   cost 31.9% more.
4. **Candidate P** — strong bounded source-first predecessor when a shorter
   workflow is preferred.
5. **Candidate B** — lean historical baseline with the core truth/source gates,
   but without later crash-safe, formal, HITL, consistency, translation, and
   reconciliation protections.

Candidate R is rejected: its added gate did not prevent its own asymptotic
overclaim and required a corrective round.

## Current evidence boundaries

- Candidate S/T's #195 result is only `3 <= max forced length <= 4`; `P_4`
  remains open.
- The #944 work proves useful necessary conditions; the graph-existence target
  remains open.
- UCLA was paused at the operator's request; Danus was excluded.
- For maximum-reliability OpenClaude research, use at least 100 maximum
  iterations. Both late 40-turn samples ended at the configured boundary; the
  100-turn queue had no such failures. This remains an operator setting.
- A final audit verdict is distinct from execution success. The UI and exports
  report contract completion, verdict, open findings, and candidate acceptance
  separately.
- Complete evidence and limitations are in
  `system_prompt_candidate_t_eval.md` and
  `research_proof_audit_completion_audit.md`.

## Deployment verification

Candidate T was promoted after the frozen aggregate using the compare-and-swap
guard. The deployed 14,190-byte body has the hash above. A fresh three-engine
run on the frozen #944 order-11 target then finished on IMProof, DeepSeek
Harness, and MetaHarness; all three were independently `complete_validated` and
retained the conservative `Partial Progress` outcome. Only DeepSeek's candidate
was mathematically accepted without open audit findings. The result is recorded
in `results/research-proof-audit-postdeploy-20260824T0752Z/finalized.json`.
