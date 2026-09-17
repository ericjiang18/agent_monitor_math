# Merge protocol

Merge completed pass outputs only after validating each one.

## Match defects

Two findings match only when both their location and mathematical failure mechanism match. Nearby findings are not duplicates if they concern different mechanisms. Never use pass-local finding IDs for matching.

For a match, retain the clearest claim/problem explanation, union nonduplicative evidence and unresolved checks, and preserve the most informative repair task. Reassess confidence from the combined evidence; corroboration may raise confidence, but disagreement must remain visible.

Keep every unmatched finding. A singleton is not weaker merely because only one complementary pass detected it.

## Rank and classify

Order findings by:

1. `central_unsalvageable_error`
2. `noncentral_unsalvageable_error`
3. `major_repairable_gap`
4. `minor_repairable_gap`
5. `cosmetic_or_exposition_only`

Within a class, order by centrality and then confidence. Reassign `F1`, `F2`, ... and renumber evidence and repair-task IDs so references remain consistent.

Use `reject` only when the merged output contains a `central_unsalvageable_error`. Do not classify a missing but plausibly supplyable substantive argument as unsalvageable.

## Provenance file

Write `merge-map.json` separately:

```json
{
  "schema_version": "ensemble-paper-audit-app.merge-map.v1",
  "findings": [
    {
      "finding_id": "F1",
      "sources": [
        {"pass": "global", "finding_id": "F2"},
        {"pass": "decomposed", "finding_id": "F7"}
      ]
    }
  ],
  "counts": {"global_only": 0, "decomposed_only": 0, "both": 1}
}
```

Optional passes use `display_sweep`, `boundary_clause_sweep`, `refuter`, or `citation` as the pass label. Never add provenance fields to `verifier-output.json`.

When a repair triggers one or more reruns, retain those pass files with explicit names
such as `global-rerun.json`, then rebuild the final verifier output and this standard
merge map from the validated current results. The merge map schema does not change:
keep only `schema_version`, `findings`, and the three required count categories shown
above. Record repair history, superseded finding IDs, and rerun filenames in
`run.json`, not in new merge-map fields.

## Reconcile the final artifact

Keep audit history outside the candidate proof. Independent passes receive the frozen
problem and candidate, not earlier verdicts, reviewer prompts, merge instructions, or
intervention text. Store those records only in this audit directory; when clean-context
isolation is unavailable, mark independence degraded.

Compute SHA-256 over raw file bytes. Keep `candidate_sha256` in `run.json` as the
initial frozen-candidate digest. Copy the exact final root proof to
`final-candidate.md` or `final-candidate.tex` in the audit run directory, then add this
machine-checkable object to `run.json`:

```json
{
  "candidate_sha256": "<initial-frozen-candidate-sha256>",
  "independence": "fresh_context_serial_passes",
  "degraded_independence": false,
  "repair_history": [],
  "reconciliation": {
    "schema_version": "research-proof-audit.reconciliation.v1",
    "status": "audited_unchanged",
    "final_artifact": {"path": "proof.md", "sha256": "<final-sha256>"},
    "audited_candidate": {"path": "final-candidate.md", "sha256": "<final-sha256>"},
    "reruns": {
      "global": "global.json",
      "decomposed": "decomposed.json",
      "merged": "verifier-output.json"
    },
    "dependent_reruns": {},
    "superseded_findings": []
  }
}
```

Use `audited_unchanged` only when the final digest equals the initial digest. After any
proof change, set `status` to `repaired_revalidated`, keep nonempty `repair_history`,
record every superseded finding ID (the list may be empty when no finding drove the
change), and rerun at least the independent global and decomposed passes
against the final-candidate snapshot, and point `reruns.global` and
`reruns.decomposed` to explicit files such as `global-rerun.json` and
`decomposed-rerun.json`. Rerun every other invalidated optional, computation,
citation, fidelity, and Lean check too, and record those files in run metadata. Rebuild
and revalidate `verifier-output.json` and `merge-map.json` from the current reruns.
For a final `Solved` or `Counterexample` outcome, an independent refuter rerun
against the exact final candidate is mandatory; record it as
`"dependent_reruns": {"refuter": "refuter-rerun.json"}`.

Paths under `audited_candidate` and `reruns` are relative to the audit run directory;
`final_artifact.path` is relative to the workspace root. Do not use absolute paths,
symlinks, or parent traversal. A valid schema without matching files and digests is
not a completed audit. Keep each audit JSON at or below 5 MB, `run.json` at or below
2 MB, and the final proof and snapshot at or below 10 MB; the trusted runtime
validator rejects empty, oversized, linked, or concurrently changing artifacts.
Create terminal `run.json` only after every referenced artifact and required validator
certificate is present and valid. A work-in-progress manifest must use a distinct
nonterminal name and cannot advertise complete coverage.
