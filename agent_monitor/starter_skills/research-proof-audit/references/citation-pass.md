# Citation-fidelity pass

This is a separate opt-in web-enabled pass. Determine whether each cited work actually licenses the mathematical use made of it.

For a TeX bundle, read `source-manifest.json`, start at `root_tex`, follow logical include order, and read only manifest-listed files, including listed bibliography files. Use relative source paths for every attribution location.

## Network boundary

- Query only works named in the paper's bibliography.
- Build every query solely from the printed bibliography entry: authors, title, venue, and year.
- Never search text from the paper's title, abstract, prose, theorems, or notation, and never try to identify the paper under audit.
- Log every query and fetched URL as it occurs to the assigned `citation-fetch-log.json`: `[{"ref":"[12]","query":"...","url":"...","used":true,"reason":""}]`.
- If a result discusses the paper under audit, discard it without using its content and log `used:false` with the reason.

## Audit

1. Harvest every attribution of mathematical content. Record the reference, bibliography entry, paper quote, and what the surrounding proof needs.
2. Retrieve the exact cited work, preferring arXiv or the publisher, and locate the cited statement.
3. Compare hypotheses, uniformity, ranges, constants, normalizations, quantifier order, and inequality direction. Classify each use as `supported`, `stronger_than_source`, `misattributed`, `not_found`, or `undecidable`.
4. Create findings for `stronger_than_source` and `misattributed`, with both paper-internal evidence and external-source evidence containing the direct URL. Put `not_found` and `undecidable` in external checks not performed.

Write valid `verifier-output.v1` JSON to the assigned output path, keep the fetch log separate, and return a compact completion summary. Do not compile TeX, execute paper code/build files, or inspect other audit results.
