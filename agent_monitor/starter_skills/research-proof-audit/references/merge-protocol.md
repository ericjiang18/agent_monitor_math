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
