# Ansatz technical report

**Math Framework LLM Assistant For Research — Richard C, Eric J.**
Revised 20 September 2026; original system inspection and artifact checks dated
15 September, repeated collaboration measurements dated 16 September.

## Open in Overleaf

Upload **[ansatz-overleaf.zip](ansatz-overleaf.zip)** as a new Overleaf project,
or paste **[main.tex](main.tex)** into a blank project's `main.tex`.
Select **pdfLaTeX**. **[Read the compiled PDF](main.pdf).**

The source is self-contained: layout definitions, TikZ architecture diagram,
tables, and bibliography are embedded. No custom style files, external images,
or `.bib` files are needed. Local compilation was checked with Tectonic 0.15.0;
the source uses standard packages supported by pdfLaTeX on Overleaf.

## Template and changes

The layout follows the supplied [mentor's Overleaf project](https://www.overleaf.com/read/wszynkpnmpks#fe3644)
and its `arxiv_version.sty`: Palatino text and mathematics, letter paper with
2.5 cm margins, a blue bordered abstract, compact numbered headings, a ruled
page header, and numeric citations. The relevant layout is embedded directly in
`main.tex`. The report retains the supplied title and author names; its header
uses the revision date. The template's institutional logos, affiliations,
equal-contribution note, and funding statement were specific to that paper and
are not assigned to this report.

Section 6.4 adds an actual saved Kimi run proving **the irrationality of the real
square root of eight**, with a complete human proof and an independent check of
the unchanged Lean source. The [example evidence](examples/sqrt-eight/README.md)
records source identity, original verification, the 20 September recheck, and
the reported axioms. The formal proof reuses Mathlib's theorem for the square
root of two. It is a solved elementary task, not a new discovery or success-rate
measurement. The Erdős–Straus example remains as the partial-progress case.

Section 7 now consistently describes **ten repetitions / twenty trials** from
the [16 September experiment](experiments/2026-09-16-ten-runs/RESULTS.md).
The timings are medians and observed ranges of per-run p95 values, not pooled
request percentiles. No new collaboration performance batch was run for this
revision. Appendix B includes the complete fresh-batch shell loop.

## Evidence and provenance

- `report.md` and `build_latex.py`: editable companion text and deterministic
  generator for `main.tex`.
- `evidence.json`, `lean-check.txt`, `collaboration-tests.xml`: unchanged evidence
  from the original preparation; earlier tests are not reported as rerun today.
- `revisions/2026-09-15-original/`: copies of the pre-revision report, PDF,
  generator, evidence, README, and Overleaf package.
- `revision-evidence.json`: hashes identifying the revised report and added
  evidence, with the scope of checks performed for this revision.
- `examples/sqrt-eight/`: preserved Lean source, selected provenance,
  independent check output, and a reproduction script.
- `experiments/2026-09-16-ten-runs/`: all ten raw reports, twenty trial rows,
  experiment manifest, source snapshots, and aggregation script.
- `package_report.py`: refreshes the revision manifest and Overleaf ZIP after
  document generation and compilation.

The original `evidence.json` document hashes refer to the original documents,
which can be inspected in the retained revision directory. The architecture and
integration findings describe the 15 September source snapshot; this document
revision does not establish that every current launch route has been tested.

## Regenerate locally

From the repository root:

```sh
python3 reports/ansatz-technical-report/build_latex.py
tectonic --untrusted --keep-logs reports/ansatz-technical-report/main.tex
python3 reports/ansatz-technical-report/package_report.py
```

The generator replaces `main.tex` from `report.md` and the embedded layout in
`build_latex.py`. Edits made directly to `main.tex` will be overwritten. The
packager records hashes and checks the retained evidence; it does not itself
compile the document or rerun Lean. These commands do not build or publish the
website.

Appendix B gives artifact-check and collaboration commands. The square-root
check requires an existing prebuilt Mathlib project compatible with Lean 4.14.0;
the example README gives the exact local environment and command.
