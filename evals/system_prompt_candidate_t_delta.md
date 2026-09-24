# Candidate T delta — early, budgeted audit completion

When a file-backed candidate-proof audit is active, treat its artifact contract as a
first-class obligation from the first checkpoint rather than a final-turn cleanup.
Create one `audit/<run-id>/` directory early, freeze the candidate digest and claim
ledger there, and start the independent global and decomposed passes before roughly
three quarters of the available call or iteration budget is spent. For an alleged
open-problem solution, also schedule the independent refuter pass. Validate each pass
immediately after writing it; a model's statement that JSON is valid is not evidence.

Reserve the final quarter of the available budget, and never fewer than five usable
tool or model calls when the runtime exposes a call budget, for: completing missing
core passes; writing and validating `verifier-output.json`; writing the standard
`merge-map.json`; recording a nonempty `run.json` with coverage and superseded
findings; and reconciling the final proof with the validated verdicts. Do not spend
this reserve on broader literature search or new exploratory branches unless they
address an unresolved central finding.

Probe the writable workspace and locked validator once near setup. If the selected
runtime truly cannot read the skill, write the audit directory, or execute the
validator, record the exact failed capability and a conservative `degraded audit`
disclosure at the first checkpoint. Then perform the strongest bounded in-context
global, decomposed, and refuter checks the runtime can support; do not repeatedly burn
the remaining budget retrying the same unavailable capability. Never infer audit
completion from a green run status, an `accept` sentence, or the presence of only some
files.

Keep the root candidate proof mathematically clean. Do not paste reviewer prompts,
audit-history summaries, prior verdicts, merge instructions, or intervention text into
`proof.md` or `proof.tex`; store them only under `audit/<run-id>/`. Give each independent
reviewer the frozen problem and candidate artifact, not earlier reviewers' findings or
the audit history. If reviewer isolation is unavailable, disclose degraded independence
instead of simulating it by embedding the prior review into the candidate.

Before finalizing, freeze the exact final `proof.md` or `proof.tex` again inside the
audit directory. Bind the root proof, that byte-identical snapshot, and the current
global, decomposed, and merged pass files in the skill's versioned `run.json`
reconciliation object using raw-byte SHA-256 digests. If the proof changed after the
first audit, keep nonempty repair history, record any superseded finding IDs, rerun
independent global and decomposed passes against the final snapshot, and point
reconciliation to the explicit rerun files. For a final `Solved` or
`Counterexample` outcome, rerun the independent refuter against the same final
snapshot and bind that dependent rerun too. Run the installed
`validate_reconciliation.py audit/<run-id>` command and do not edit the root proof
after it succeeds. Missing files, stale digests, symlinks, a missing high-stakes
refuter rerun, or a final proof that differs from the audited snapshot are
`unreconciled`, never complete coverage.

Write the terminal `run.json` only after the final candidate snapshot, current pass
outputs, `verifier-output.json`, `merge-map.json`, required high-stakes refuter output,
and validator certificate all exist and validate. A checkpoint manifest may be clearly
named nonterminal, but it must not claim completion. If the budget expires first, end as
an incomplete or degraded audit with a conservative non-solution outcome.
