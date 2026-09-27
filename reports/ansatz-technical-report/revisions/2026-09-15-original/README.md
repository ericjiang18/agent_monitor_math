# Ansatz technical report

## Open in Overleaf

Copy the complete contents of **[main.tex](main.tex)** into `main.tex` in a blank Overleaf project and compile with **pdfLaTeX**. Alternatively, upload **[ansatz-overleaf.zip](ansatz-overleaf.zip)** as a new project.

The LaTeX file is self-contained: the architecture diagram is TikZ, the tables are native LaTeX, and the bibliography is embedded. No external images, bibliography file, Python scripts, or repository access are required to compile it. You can edit `main.tex` directly in Overleaf.

**[Read the compiled PDF](main.pdf).** Local compilation used Tectonic 0.15.0; the source uses standard packages supported by Overleaf's default pdfLaTeX compiler.

## Contents

- `main.tex`: complete paper, including references and reproduction appendices.
- `main.pdf`: compiled paper.
- `report.md` and `architecture.svg`: readable companion source.
- `evidence.json`: validated inventory, source hashes, fresh Lean result, and recorded test outcome.
- `collaboration-tests.xml`: JUnit output from the six fresh collaboration tests.
- `lean-check.txt`: fresh Lean version and checker output.
- `collect_evidence.py`: standard-library-only inventory and witness checker.
- `build_latex.py`: reproduces `main.tex` from the companion Markdown.
- `ansatz-overleaf.zip`: upload-ready project containing `main.tex` and brief instructions.

## Scope and evidence

The report follows the system-paper organization of [Prove2Me](https://arxiv.org/abs/2608.28433v1), using original prose and this repository's evidence. It describes the **development working tree on 15 September 2026**, including uncommitted source; the Git base alone does not identify it.

Fresh checks validated all 499 stored Erdős–Straus witnesses, compiled the finite Lean example, checked atlas consistency, and ran the six collaboration tests. The latency table reproduces the separately archived **9 September** synthetic HTTP measurements; it is not a new performance run. The mathematical demonstration is curated. The report makes no comparative proving-performance or autonomous-discovery claim.

The paper leaves author names and affiliations unassigned. Set `\author{...}` before submitting it under your name or your team's names.

## Ten-run follow-up experiment

On 16 September, the Section 7.2 workload was repeated ten times sequentially
at both client counts. See the [results and observed ranges](experiments/2026-09-16-ten-runs/RESULTS.md),
[all twenty trial rows](experiments/2026-09-16-ten-runs/trials.csv), and
[LaTeX replacement table](experiments/2026-09-16-ten-runs/results-table.tex).
The original paper retains its historical table. The repeated experiment
records eight logical CPUs; the historical report recorded two.

## Regenerate locally

From the repository root:

```sh
python3 reports/ansatz-technical-report/build_latex.py
tectonic --untrusted reports/ansatz-technical-report/main.tex
```

The first command replaces `main.tex` using `report.md`; edits made directly to `main.tex` will be overwritten. Neither command builds or publishes the website.

To collect a new evidence inventory, choose a new filename:

```sh
python3 reports/ansatz-technical-report/collect_evidence.py \
  --output /tmp/ansatz-report-evidence-new.json
```

Lean and pytest checks are separate; their commands appear in Appendix B. The collector does not run models, start the application, inspect private run data, or regenerate public assets.
