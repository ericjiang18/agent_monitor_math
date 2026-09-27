# Output contract

Write `verifier-output.json` in the bundled `verifier-output.v1` shape. The authoritative schemas are `schema/verifier-output.schema.json` and `schema/common.schema.json`.

Validate with:

```sh
python3 <skill-dir>/scripts/validate_output.py <path-to-verifier-output.json>
```

The root object contains only `verdict`, `summary`, `findings`, and `coverage_notes`. Verdict is one of `reject`, `major_revision`, `minor_revision`, or `accept`.

Every finding contains `finding_id`, `class`, `location`, `claim_under_review`, `problem`, `centrality`, `repairability`, `evidence`, `confidence`, `unresolved_checks`, and `repair_tasks`. Express uncertainty through confidence and unresolved checks; there is no uncertainty class. Do not include paper identity, lineage, run metadata, or pass provenance.

For a multi-file TeX bundle, make the relative source path explicit without changing the locked schema. Include it at the start of a semantic location field, preferably `location.section`, such as `sections/estimate.tex — Section 4, proof of Lemma 4.3`, and include `line_start`/`line_end` when available.

Use exactly one class per finding, ordered most-severe-first:

1. `central_unsalvageable_error`
2. `noncentral_unsalvageable_error`
3. `major_repairable_gap`
4. `minor_repairable_gap`
5. `cosmetic_or_exposition_only`

A substantive missing argument is a major repairable gap when a credible new argument could preserve the result. Difficulty alone does not make a defect unsalvageable. `reject` requires at least one central unsalvageable error.

Within each class, order by centrality and confidence. Quote or precisely transcribe the paper's own mathematics as evidence. List actual coverage, including every region that was difficult or not fully checked and all external checks not performed.

The validator also enforces research-integrity invariants: line ranges must be ordered; evidence and repair-task IDs must be globally unique; repair-task evidence references must resolve within their finding; a discharged or failed repair task must cite evidence; and `central_unsalvageable_error` requires high confidence. These checks validate bookkeeping, not mathematical truth.
