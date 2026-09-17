# Research-proof audit integration: completion audit

Status: **complete; final evaluation frozen and post-deployment verification passed**.

This document records the evidence needed to accept the ensemble paper-audit
adaptation as a ProvingConsole candidate-proof audit layer. It deliberately
separates current-version evidence from historical pre-fix runs and does not
treat a model's self-report, a green UI badge, or absence of an obvious error as
proof of completion.

## Acceptance requirements

1. Preserve the supplied ensemble design: independent global and decomposed
   passes over one frozen candidate, keep-by-default merging by location plus
   failure mechanism, optional risk-selected passes, and conservative outcomes.
2. Materialize the skill, references, scripts, and locked Library tools into
   every selected harness workspace without changing the selected harness or
   model.
3. Validate structured pass output, provenance, merge maps, run manifests,
   outcome cardinality, and fail-closed behavior independently of model prose.
4. Harden untrusted TeX and proof inputs without executing submitted build code
   or weakening the mathematical audit contract.
5. Forward-test non-UCLA harnesses on unsolved Erdős workflows for a fixed
   24-hour window, report failures and telemetry honestly, and manually review
   every `Solved` or `Counterexample` outcome.
6. Compare the incumbent and candidate system prompts without modifying the live
   prompt during the measurement window; promote only after data freeze.

## Skill package evidence

- Integrated package:
  `agent_monitor/starter_skills/research-proof-audit/`
- `skill-creator` structural validation: **passed** on 2026-08-23.
- Current integrated `SKILL.md`: 133 lines; SHA-256
  `b135261e8cb61af12a73881747d7871ad00011895d595937d8fdea2e62973901`.
- UI metadata is isolated in `agents/openai.yaml`; its default prompt explicitly
  invokes `$research-proof-audit`.
- Supplied schemas are byte-identical to the integrated copies:
  - verifier schema: `089b21d55af151db4af577a8f3f71f6f9a65a01f287b4b024735b63545eadc44`
  - common schema: `e2042d690ccb0c43b41c81714634660a662947f4d0089e315e3958bff16cad0e`
- Supplied `global`, `decomposed`, `boundary`, `refuter`, `citation`, and
  `display` pass references are byte-identical. The adapted `latex-bundles`,
  `output-contract`, and `merge-protocol` references add only bounded-input,
  semantic-validation, and rerun-history requirements.
- `prepare_latex_bundle.py` adds bounded file/byte closure, unbraced `\input`
  discovery, copy-after-discovery hash verification, and explicit static-parser
  limitations. It never executes TeX or submitted build scripts.
- `validate_output.py` retains the supplied schema/ranking/ID checks and adds
  line-range, globally unique evidence/task ID, evidence-reference, terminal
  repair-task, and high-confidence central-error invariants.
- `validate_reconciliation.py` independently binds the root proof, a frozen
  final-candidate copy, and validated current pass files with raw-byte SHA-256;
  SHA-256
  `b873b0c6bbe0f01f12835561dd6d5d11880703192963b609f8ebacdeff2b13a1`.
  The validator bounds JSON/manifests/proofs, rejects linked or changing files,
  recomputes merge counts and source IDs against the exact current reruns, and
  exposes independent versus degraded reviewer isolation separately.

## ProvingConsole integration evidence

- The profile materializer writes enabled skills at the stable path
  `_agent/skills/<name>/SKILL.md`; run prompts point agents to that path.
- To preserve the fixed experiment, active workspaces retain the evaluation-time
  112-line `SKILL.md` hash
  `e7319d6162d46a1fb0a1cb1c306d873010d232a0100ea40e61a521e4cceb2f58`;
  the strengthened package is not injected into their already-running prompts.
- The selected engine/model remain authoritative. Supplemental adapters execute
  only when the audit gate is requested and preserve engine-labelled runtime
  artifacts.
- The locked finalizer independently reads each workspace and recomputes audit
  compliance from required JSON files, schemas, merge-map provenance, manifest,
  and final outcome instead of trusting evaluator labels.
- The examples below had valid pass JSON and merge structure under the old gate.
  The strengthened finalizer correctly marks every actual harness
  `unreconciled` because no run cryptographically binds the final artifact to
  current pass files. Plain remains an intentionally degraded single-call run.

| Engine | Current-version evidence | Trusted compliance | Outcome |
|---|---|---|---|
| Codex | `codex_audit24h_erdos944_order11_audit_codex_001_44da0b02e4` | unreconciled | Partial Progress |
| DeepAgents | `deepagents_audit24h_erdos944_order11_audit_deepagents_001_525298a627` | unreconciled | Partial Progress |
| DeepSeek Harness | `deepseek_harness_audit24h_erdos886_audit_deepseek_harness_035_7ca31f0835` | unreconciled | Partial Progress |
| Hermes | `hermes_audit24h_erdos886_audit_hermes_028_01c731a37c` | unreconciled | Partial Progress |
| IMProof | `improof_direct_erdos944_order11_audit_604d0f1c655e` | unreconciled | Partial Progress |
| MetaHarness | `metaharness_audit24h_erdos196_audit_metaharness_022_1ba6c18451` | unreconciled | Partial Progress |
| OpenClaude | `openclaude_audit24h_erdos944_audit_openclaude_001_3cc86d1c33` | unreconciled | Partial Progress |
| OpenClaw | `openclaw_audit24h_erdos413_audit_openclaw_034_4dffa4c4e8` | unreconciled | Partial Progress |
| OpenHands | `openhands_audit24h_erdos886_audit_openhands_016_ae97348abb` | unreconciled | Partial Progress |
| Plain baseline | `plain_audit24h_erdos886_audit_plain_038_b7bc8e0466` | degraded_disclosed | Partial Progress |

The implementation boundary subsequently produced two real exact-final forward
samples while Candidate S remained the live system prompt:

- `metaharness_audit24h_erdos413_audit_metaharness_052_29277d135a`
  is fully `complete_validated`: fresh-context serial global, decomposed, and
  refuter reruns, exact final/candidate digest
  `16eeb504841027807de6d487852b8530442f925af906545a5c8bd28ebfef50cf`,
  merge-source reconciliation, and `Partial Progress`.
- `openclaude_audit24h_erdos196_audit_openclaude_024_a43afbb156` has the same
  exact-final and merge integrity, but its manifest honestly records unavailable
  fresh-context isolation. It is therefore
  `validated_degraded_independence`, not counted as a complete independent
  ensemble. Its outcome is `Partial Progress`.
- `deepagents_audit24h_erdos886_audit_deepagents_068_8420d433f2`
  is independently `complete_validated`: fresh-context serial global,
  decomposed, and refuter reruns; repaired-and-revalidated reconciliation; and
  byte-identical root/final-candidate SHA-256
  `ea584e03b003da8a1f6cb28ddd92b43fde03e2f2ec4e6b16988e0c4cd9008971`.
  It proves only the fixed-window consequence for
  `epsilon >= 1/4`, leaves `0 < epsilon < 1/4` unresolved, and ends as
  `Partial Progress`.

This is a forward test of the strengthened implementation, not a retroactive
upgrade of earlier artifacts. The final aggregate preserves the version boundary.

Historical workspaces without the skill and historical invalid outputs remain in
the aggregate as failures/degraded evidence; they are not relabelled as current
successes. UCLA is excluded by operator request. Danus is not part of this
evaluation.

The sole contaminated control is the 09:11 UTC historical DeepSeek structured
regression `deepseek_harness_audit24h_erdos413_baseline_deepseek_harness_002_4cff162b08`.
It remains excluded from clean-baseline statistics. Replaying its frozen prompt
against the current adapter returns `audit_requested=false`; the latest DeepSeek
baseline has no audit directory, and explicit-marker plus passive-skill-listing
control-isolation regressions both pass. This is preserved historical evidence,
not relabelled as a current clean control or hidden from totals.

## Final frozen evaluation

The fixed queues reached their deadlines and drained without manual truncation.
`evals/results/research-proof-audit-aggregate-final.json`, generated at
`2026-08-24T07:35:23.026804Z`, deduplicates all persisted evaluator states into:

- 328 non-UCLA, non-Danus runs: 300 finished, 27 failed, one stopped;
- outcomes: 241 `Partial Progress`, 70 `Known/Open Status`, zero `Solved`, and
  zero `Counterexample`;
- compliance: 11 `complete_validated`, 6
  `validated_degraded_independence`, 28 `degraded_disclosed`, 137
  `unreconciled`, 27 `incomplete`, 3 `invalid_structured`, 2 `not_executed`,
  113 clean controls, and one preserved contaminated historical control;
- manual high-stakes review queue: zero;
- recorded telemetry: 231,945,547 input tokens, 5,993,354 output tokens,
  215,557.176 cumulative latency seconds, and $1,011.2463844.

A separate strict final-verdict scan records 6 accepted candidates and 322 not
accepted. The accepted artifacts are bounded lemmas or honest open-status/partial
results; none solves an Erdős problem. Execution/compliance and mathematical
acceptance are deliberately reported separately.

## Historical interim snapshot

The chained wall-clock evaluation began at `2026-08-23T06:54:25.198303Z` and
has a fixed final deadline of `2026-08-24T07:05:40.414108Z`, a scheduled span
of 24.1876 hours. It uses persisted continuation states across process restarts
rather than claiming one uninterrupted evaluator PID; the restart history remains
visible in the state files.

Trusted aggregate generated 2026-08-23T20:48:49Z from 40 evaluator state files:

- 204 unique runs: 180 finished, 20 failed, 3 running, 1 stopped;
- outcomes: 148 `Partial Progress`, 46 `Known/Open Status`, zero `Solved`,
  zero `Counterexample`;
- audit compliance: zero complete validated, 96 unreconciled, 57 clean controls,
  22 degraded disclosed, 22 incomplete, 3 invalid structured, 3 not executed,
  and 1 contaminated control;
- high-stakes manual-review queue: zero;
- recorded telemetry: 129,732,491 input tokens, 3,341,081 output tokens,
  123,972.781 seconds cumulative latency (34.44 hours across parallel runs),
  and $565.5527192.

Telemetry is recorded adapter data, not a provider invoice. In particular,
OpenHands ACP exposes unusually sparse token/cost telemetry, so total actual spend
may be higher. Historical failures remain counted. The authoritative live snapshot
is `evals/results/research-proof-audit-aggregate-live.json`.

The 20 visible failures currently break down as eight DeepAgents nonzero exits,
eight OpenClaude exits (five maximum-turn boundaries, two execution errors, and
one repeated file-tool failure), three OpenHands nonzero exits, and one OpenClaw
nonzero exit. These are not rewritten as successes merely because some preserve a
useful draft. Three jobs were live at this snapshot; they will count as
reconciled only if the exact final proof, final-candidate snapshot, explicit
reruns, merge provenance, manifest hashes, and trusted validators all close.

Two failures exposed adapter defects whose historical results remain failed:

- `deepagents_audit24h_erdos413_baseline_deepagents_014_df3f210493`
  failed before its first checkpoint because the model repeated a malformed
  JSON-encoded `write_file` call even after two correction prompts. The final
  recovery turn now runs under a reduced response schema containing only a
  required text field; a third tool call is structurally impossible, and the
  runner canonicalizes the result to text plus an empty tool-call list before
  the ordinary proof persistence and audit gates run.
- `openhands_audit24h_erdos196_baseline_openhands_030_18f6a544d8`
  produced a substantial `proof.md` that passes the conservative control
  checkpoint validator, then an ACP exception triggered a second backend and
  the job ended nonzero. The exception path now first runs the same strict
  audit/control checkpoint validators used at activity boundaries. It recovers
  only a validated artifact and otherwise retains the original fallback. This
  avoids both discarding useful audited work and treating an unchecked draft as
  success.

Neither repair is retroactively applied to evaluation labels.

Two later finished IMProof audit samples,
`improof_audit24h_erdos886_audit_improof_062_a812c3a842` and
`improof_audit24h_erdos944_audit_improof_066_4bbd628a9d`, reproduced a
deployment-version boundary rather than a new mathematical failure. Their native
Author--Critic runs disclosed degraded audit and returned conservative partial
results, but the still-running pre-freeze service process had loaded the old
IMProof runner: its persisted result contains none of
`supplemental_audit_status`, `supplemental_audit_returncode`, or
`promoted_audit_files`, and no trusted supplemental adapter ran. Both remain
`incomplete`. The pending runner performs the supplemental audit and persists
all three diagnostics; a direct wrapping regression proves those fields survive
into Monitor. The change will be loaded only after data freeze and service
restart.

Real control outputs also exposed two human-facing footer variants. OpenClaw
emitted a two-line Markdown `ProvingConsole Outcome` heading, while Plain
retained a nonstandard `ProvingConsole: Known/Open Status` line before its
canonical conservative footer. Their parsers already avoided a high-stakes
misclassification, but the new runner cleanup preserves a sole unambiguous label,
canonicalizes it to the exact one-line contract, and fails conflicting labels
closed to `Partial Progress`. Historical artifacts are not rewritten.

## Prompt comparison (final)

Candidate T is Candidate S plus
`evals/system_prompt_candidate_t_delta.md` (SHA-256
`15992b88e3948248a3b9f6744c45dbb60b5c4a48afe0b9865767f5c2cdf19ca6`).
Three controlled OpenClaude pairs currently favor T for maximum reliability. The
decisive #886 pair retrieved and inspected the pivotal publisher PDF, found that
a printed Proposition 4.2 factor-difference inequality is reversed, repaired the
candidate, preserved the weaker valid divisor-window result, and correctly refused
to claim the open problem solved. Full pair evidence is in
`evals/system_prompt_candidate_t_eval.md`.

The live prompt remained Candidate S through the fixed data freeze, preventing
Candidate T from contaminating the baseline/audit samples. The post-freeze
promotion used a compare-and-swap guard requiring Candidate S SHA-256
`25b692842865f631af9a917c30ff581bda55c25bb9705fe83807cd11123063b3`.
The final Candidate T body is 14,190 bytes, contains its reserve-budget
clause exactly once, remains below the 20,000-byte persona ceiling, and has
SHA-256 `ae66c22073fe22926ac52529b2287ff36277e2c82e8eec0ede8c462e26e33a36`.
`evals/promote_research_prompt.py` implements this guard and defaults to dry-run.
Its isolated tests cover atomic application, post-write hash verification,
duplicate-application refusal, wrong-output-hash refusal, temporary-file cleanup,
and symlink refusal. The production dry-run reproduced both expected hashes
without changing SOUL.md; the runtime file and deployment process share uid/gid
1000, so the atomic replacement preserves the existing ownership model.

## Verification already completed

- Pre-freeze maintained application suite: **284 passed and 9 subtests passed** on
  2026-08-23. This does not replace the required post-deployment rerun.
- Focused exact-final skill/runtime/finalizer/adapter suite:
  **38 passed and 2 subtests passed** on 2026-08-23; the complete harness integration module passes
  **219 tests and 2 subtests**.
- Forward testing exposed duplicated Lean citation identities and a trailing
  `ProvingConsole outcome` marker absorbed into reference text. The source fix
  canonicalizes DOI/arXiv/Erdős IDs, normalizes self-derived text, prefers
  anchored provenance, assigns unique display IDs, and stops References parsing
  at the outcome contract. A real OpenHands #196 replay reduced 20 raw entries
  to 8 distinct records with unique IDs and no outcome contamination; the full
  Monitor/engine file then passed **214 tests and 2 subtests** at that checkpoint. Replaying every
  latest workspace with a Lean sidecar produced clean, uniquely numbered output
  for Codex, DeepAgents, DeepSeek, Hermes, MetaHarness, OpenClaude, OpenClaw,
  OpenHands, and Plain. The standalone IMProof regression has no Lean sidecar,
  so citation replay is explicitly not applicable there.
- A forward Codex #944 sidecar exposed a misleading Lean note saying
  `degraded audit: skill file unavailable` even though its informal workspace
  contained the exact skill and all six verifier JSON files passed. The phrase
  was confined to notes, not Lean source. The formalizer prompt now separates
  proof-audit runtime status from formalization, and persistence removes only
  product-level degraded-audit availability markers while retaining compiler,
  admission, and statement-fidelity findings. The complete Monitor/engine file
  now passes **219 tests and 2 subtests**.
- A later DeepAgents #944 forward run changed the final proof after producing
  valid initial global/decomposed/merged JSON but supplied no explicit reruns or
  final-artifact digest binding. The finalizer had incorrectly treated schema
  validity as completion. It now fails closed as `unreconciled`; regression
  tests cover unchanged, repaired/rerun, stale-hash, missing-manifest, and
  symlink cases. Recomputing all historical states downgraded 91 formerly green
  records instead of preserving a misleading success metric.
- The shared DeepSeek/IMProof/MetaHarness supplemental adapter now freezes the
  exact adjudicated artifact, reruns global, decomposed, and refuter passes,
  rebuilds the merged output, and writes raw-byte reconciliation hashes. Hermes,
  OpenClaude, OpenClaw, DeepAgents, OpenHands, and IMProof runtime recovery now
  call the server-owned validator and fail closed on stale or missing bindings.
  The Monitor/export payload records `validated` versus `unreconciled`.
  A final `Solved` or `Counterexample` additionally requires a validated
  exact-final refuter rerun.
- No current aggregate run requires high-stakes manual review.
- A real DeepSeek #886 audit closed all merged findings in its final artifact:
  it repaired square/non-square endpoint counting, removed unconditional
  sharpness language, retained the unverified primary-source obligation, and
  ended as `Partial Progress`.
- A high-budget OpenClaude #413 treatment located and inspected Lau's current
  primary preprint `arXiv:2604.15042v2`. Theorem 1.3 plus the independently
  checked translation `n=N-1`, `j=k+1`, `epsilon=1/C` proves the already-known
  relaxed-epsilon question while retaining the coefficient-one target as open.
  The current Erdős Problems page independently reports the same scope. All
  three verifier objects, merge provenance, and run manifest validate; the
  derivation and limitations are preserved in
  `evals/erdos_413_relaxed_translation_audit.md` (SHA-256
  `a334df2c3f828e23e956493145b08086609b6bf63369d5fd36d5f8f41a439ddc`).
- An OpenClaw #196 treatment gave a self-contained parity-recursion proof that
  every finite integer set admits an ordering without a monotone 3-AP, hence
  without a monotone 4-AP. This is useful finite-boundary progress but has
  quantifiers `forall finite A, exists P_A` and does not construct one order
  of all natural numbers. No novelty claim or open-problem solution is made.
  The review is preserved in `evals/erdos_196_finite_avoidance_audit.md`,
  SHA-256
  `7a9b84149a77f6c7cdf7f373e1e758de9d73b8f84f24642462f016bc9c05014f`.
- The pivotal #886 source-defect claim was independently reproduced with exact
  standard-library polynomial arithmetic and all 240 factor pairs at `a=5`.
  Separate admission-free Lean declarations prove the identity, strict sign,
  and boundary arithmetic; the unrestricted open conjecture remains visibly
  isolated behind one `sorry`. The corrected human-readable certificate is
  `evals/erdos_886_prop42_certificate.md`, SHA-256
  `38bc8dbc84bc9a7480ddb493e668c54514b5d0306f4f463ff104d4338b3ef568`.

## Post-deployment acceptance

Candidate T was promoted after freeze, seeded into the current skill profile,
and loaded by a successful service restart. Public prefixed HTTPS health/login,
local anonymous authorization boundaries, and authenticated engines, profile,
Library, and Codex-status endpoints all passed.

The fresh frozen-target evaluation at
`evals/results/research-proof-audit-postdeploy-20260824T0752Z/finalized.json`
ran IMProof, DeepSeek Harness, and MetaHarness with the deployed prompt and
current skill. All three finished, all three retained `Partial Progress`, all
three were independently `complete_validated`, and the manual-review queue was
empty. The final audit accepted DeepSeek's candidate with zero open findings;
IMProof and MetaHarness executed the complete contract but retained one open
finding each, so they were not mathematically accepted. This distinction is
intentional and visible in Monitor/export data.

Two simultaneous real Lean kernel checks in isolated workspaces both returned
`verified`, exit 0, with distinct source hashes and no log/source leakage. The
complete maintained test suite passes 289 tests and 9 subtests; Python
compilation, `git diff --check`, the first-party secret scan, skill structural
validation, and runtime smoke checks pass.

## Acceptance gates

- [x] Reached the evaluator deadline and drained active jobs without truncation.
- [x] Recomputed the final deduplicated aggregate from every persisted state.
- [x] Reviewed the high-stakes queue; it contained no `Solved` or
      `Counterexample` entry.
- [x] Froze the Candidate S/T decision and selected Candidate T.
- [x] Promoted Candidate T after freeze and verified the deployed hash.
- [x] Completed a fresh reconciled three-engine post-deployment audit.
- [x] Passed the full tests, compilation, diff, secret, Lean-concurrency, and
      authenticated/anonymous/public runtime checks.
- [x] Recorded the final conclusion and limitations without an open-problem
      solution claim.
