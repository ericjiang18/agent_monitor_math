---
title: Problem catalogue sources
description: Sources, dated status, selection rules, and attribution for the six Ansätze research collections.
---

# Where the questions come from

The [problem catalogue](/open-problems) contains **1,756 entries across six collections**, selected on **September 16, 2026**. It combines established problem indexes with a smaller set of named questions and sourced research summaries.

| Collection | Entries | Source and status dates |
| --- | ---: | --- |
| Erdős Problems | 590 | Maintainer snapshot: September 9, 2026; selected details reviewed September 16 |
| Kourovka Notebook | 1,151 | September 1, 2026 edition; selected details reviewed September 16 |
| Millennium Prize Problems | 6 | Clay pages checked September 16, 2026: five unsolved, one resolution announced |
| Ramanujan’s legacy | 3 | Research and claim records reviewed September 16, 2026 |
| Smale’s problems | 4 | Selected open questions; sources reviewed September 16, 2026 |
| Hilbert’s problems and open extensions | 2 | Selected questions; sources reviewed September 16, 2026 |

These are dated source observations. A review date does not make an older publication a new result, and a source label does not certify that every subsequent manuscript has been assessed.

## Reading an entry

A **source reference** identifies the original problem and where to read it. Most imported Erdős and Kourovka entries remain references with subject labels; their full statements and research histories have not been reproduced or individually reviewed here.

An **editorial statement** is an original mathematical summary with linked sources. Selected entries also separate established partial results, conditional or computational evidence, and reported proof claims. A preprint claim, a withdrawn argument, and an accepted theorem have different labels. An empty claims list means no report was selected for that entry, not that no attempted proof exists.

Read the linked formulation for its complete assumptions. “Click to solve” opens a research attempt using the selected question and context. It does not promise a solution. A formal statement, a claimed formal proof, and a proof checked by a verifier are separate things; the catalogue does not certify its listed claims.

## Erdős Problems · 590 entries

The [Erdős Problems maintainer database](https://github.com/teorth/erdosproblems) is maintained by Thomas Bloom, Terence Tao, and contributors. The import uses revision [`3c68e941162f81d650fc886eed34e58bed3a6a01`](https://github.com/teorth/erdosproblems/tree/3c68e941162f81d650fc886eed34e58bed3a6a01), dated September 9, 2026.

Only records whose `informal_status.state` is exactly `open` are eligible. Erdős #1163 is omitted because its formulation is flagged as unclear; it remains related context under #1162. Number, source status, tags, and any prize amount come from the maintainer feed. Prize terms and updates remain with the source.

The selected detail for [Erdős #3](https://www.erdosproblems.com/3) links the original question and [Bloom–Sisask’s three-term progression result](https://arxiv.org/abs/2007.03528). The three-term theorem does not settle every progression length. A linked [Lean statement](https://github.com/google-deepmind/formal-conjectures/blob/main/FormalConjectures/ErdosProblems/3.lean) is not a proof of the full conjecture.

Imported metadata is distributed under [Apache License 2.0](https://github.com/teorth/erdosproblems/blob/main/LICENSE); a [license copy](/problem-licenses/erdos-apache-2.0.txt) is included. Editorial summaries are separately written navigation aids, not a redistribution of the website’s full problem text.

## Kourovka Notebook · 1,151 entries

The *Kourovka Notebook: Unsolved Problems in Group Theory*, edited by E. I. Khukhro and V. D. Mazurov, has grown since 1965. Individual authors are credited in the notebook. Canonical problem numbers were imported from ConjectureBench’s July 2026 index and checked against the editors’ [September 2026 edition](https://kourovkanotebookorg.wordpress.com/wp-content/uploads/2026/09/21tkt.pdf), announced in their [September 1 update](https://kourovkanotebookorg.wordpress.com/2026/09/01/september-2026-update-for-the-21st-edition/).

The import stops before the solved archive and excludes any problem containing an answer asterisk, including an answered subpart. This conservative rule omits some questions with unresolved parts. Each retained number links to its page. Kourovka 21.93 and Erdős #274 describe the Herzog–Schönheim conjecture, so they are counted once, with both references under Erdős #274.

Selected details cover two longstanding questions:

- **1.3, integral group-ring zero divisors:** [Linnell’s account](https://personal.math.vt.edu/linnell/research/durham.pdf) gives the right-orderable and elementary amenable cases. [Gardam’s counterexample](https://www.gilesgardam.com/papers/unit-conjecture-counterexamplev3.pdf) disproves the related unit conjecture; its group still satisfies the zero-divisor conjecture.
- **12.20, Thompson F amenability:** the group is defined in [Cannon–Floyd–Parry](https://www.imo.universite-paris-saclay.fr/~emmanuel.breuillard/Cannon.pdf). [Brin–Squier](https://pi.math.cornell.edu/~justin/Limited2Cornell/PLoI.pdf) prove the absence of nonabelian free subgroups. [Kielak’s 2016 appendix](https://arxiv.org/pdf/1605.09133v2) gives the Ore-condition reduction mentioned in the notebook’s 2026 comment. A [withdrawn non-amenability claim](https://arxiv.org/abs/1408.2188) is labeled accordingly.

The book remains copyright © E. I. Khukhro and V. D. Mazurov, 2026. Its full text is not redistributed. The imported entries are bibliographic references; the two selected statements are short, original paraphrases with citations.

### ConjectureBench attribution

The canonical Kourovka index was adapted from [ConjectureBench](https://github.com/bespokelabsai/conjecture-bench), by Anirudha Ramesh and Shreyas Pimpalgaonkar, Bespoke Labs (2026), revision [`357bcb1a1daf93917d42e8206ceaa55645729a09`](https://github.com/bespokelabsai/conjecture-bench/tree/357bcb1a1daf93917d42e8206ceaa55645729a09).

Its original curation metadata is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Ansätze retained canonical identifiers, rechecked the September notebook, omitted imported statement text, excluded answers, and added topic labels. This does not relicense the underlying book. The upstream [data license](/problem-licenses/conjecturebench-license-data.txt) and [source notice](/problem-licenses/conjecturebench-notice.txt) are preserved.

## Millennium Prize Problems · 6 entries

As checked on September 16, the [Clay Mathematics Institute](https://www.claymath.org/millennium-problems/) lists five problems as unsolved and Navier–Stokes as active. The catalogue retains those six entries and excludes the solved Poincaré conjecture.

**Navier–Stokes is labeled “resolution announced.”** Clay’s [September 11 response](https://www.claymath.org/news/navier-stokes-announcement/) acknowledges an apparent resolution and refers to its evaluation process. The entry separately links the [authors’ announcement, manuscript, and Lean source](https://openai.com/index/navier-stokes-solution/). It does not treat a pending prize decision as settled. The announced smooth-forced breakdown must be read against the exact alternatives in [Fefferman’s official formulation](https://www.claymath.org/wp-content/uploads/2022/06/navierstokes.pdf).

The remaining entries link the official descriptions by [Wiles for Birch–Swinnerton-Dyer](https://www.claymath.org/wp-content/uploads/2022/05/birchswin.pdf), [Deligne for Hodge](https://www.claymath.org/wp-content/uploads/2022/06/hodge.pdf), [Cook for P versus NP](https://www.claymath.org/wp-content/uploads/2022/06/pvsnp.pdf), and [Jaffe–Witten for Yang–Mills](https://www.claymath.org/wp-content/uploads/2022/06/yangmills.pdf), plus Clay’s [Riemann hypothesis page](https://www.claymath.org/millennium/riemann-hypothesis/). The Riemann entry cites [Pratt–Robles–Zaharescu–Zeindler](https://arxiv.org/abs/1802.10521) for an established proportion-of-zeros result, not a full proof. Official assumptions, attribution, and [prize rules](https://www.claymath.org/millennium-problems/rules/) remain authoritative.

## Ramanujan’s legacy · 3 entries

These questions were posed by Srinivasa Ramanujan or arise from his mathematics. Later conjectures retain their actual authorship. In particular, Deligne solved the holomorphic Ramanujan–Petersson bound; the selected Maass-form extension is a different, open case.

| Selected question | Sources for the statement, partial results, and reported claims |
| --- | --- |
| Brocard–Ramanujan factorial squares | [Berndt–Galway (2000)](https://doi.org/10.1023/A:1009873805276); [Epstein–Glickman computation](https://github.com/jhg023/brocard); [2026 research on restricted sequences](https://arxiv.org/html/2606.28577v1); [Maiti’s revised preprint claim](https://arxiv.org/abs/2004.09256v2). Finite searches and restricted-sequence theorems do not settle all positive integers. |
| Lehmer nonvanishing for Ramanujan’s tau function | [Bosman’s verified finite range](https://arxiv.org/pdf/0710.1237); [Balakrishnan–Craig–Ono](https://doi.org/10.1016/j.jnt.2020.04.009); [2026 tau-function research](https://arxiv.org/html/2603.29970v2); [Lee’s withdrawal notice](https://arxiv.org/abs/1506.02098); [Shi–Wang–Solé’s 2025 withdrawal](https://arxiv.org/abs/2503.23498v2). The conjecture is Lehmer’s, not Ramanujan’s. |
| Ramanujan bounds for Hecke–Maass cusp forms | [Sarnak’s account and the Kim–Sarnak bound](https://publications.ias.edu/sites/default/files/FieldNotesCurrent.pdf); [Huang–Zhao’s 2026 partial results](https://arxiv.org/html/2605.09807v1); [Unterberger’s preprint claim](https://arxiv.org/abs/2001.10956v16). Later research still describes the all-primes conjecture as unsolved. |

The Maiti and Unterberger manuscripts are reported as unverified claims. The status assessment follows the cited research literature; this catalogue does not independently refute those manuscripts.

## Smale’s problems · 4 entries

The collection selects four questions from [Steve Smale’s 1998 list](https://www.fim.uni-passau.de/fileadmin/dokumente/fakultaeten/fim/lehrstuhl/muller/SmaleProblems1998.pdf). It does not imply that all eighteen original problems remain open.

| Selected question | Primary research supporting the detail |
| --- | --- |
| 6 · Finiteness of planar central configurations | [Leandro’s 2026 formulation](https://arxiv.org/html/2507.13865v4); [Albouy–Kaloshin’s generic five-body theorem](https://annals.math.princeton.edu/wp-content/uploads/annals-v176-n1-p10-p.pdf); [Moczurad–Zgliczyński’s selected exceptional masses](https://arxiv.org/abs/2601.01165v2). These partial results do not cover every positive mass tuple and every number of bodies. |
| 7 · Near-optimal logarithmic energy on the sphere | [Armentano and coauthors](https://arxiv.org/html/2502.10152v3) classify small critical configurations and state the remaining algorithmic question. This concerns logarithmic energy, not the inverse-distance Thomson problem. |
| 9 · Strongly polynomial linear programming | [Dadush–Koh–Natura–Olver–Végh (STACS 2025)](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.STACS.2025.2) establish a sparse-matrix case. General polynomial time in input bit length does not meet the strongly polynomial requirement. |
| 11 · Density of hyperbolicity for complex polynomials | [Kozlovski–Shen–van Strien](https://annals.math.princeton.edu/wp-content/uploads/annals-v166-n1-p04.pdf) solve the real part. [MFCS 2025 research](https://drops.dagstuhl.de/storage/00lipics/lipics-vol345-mfcs2025/html/LIPIcs.MFCS.2025.79/LIPIcs.MFCS.2025.79.html) still uses the complex quadratic conjecture as an assumption. |

## Hilbert’s problems and open extensions · 2 entries

The historical starting point is [Hilbert’s 1900 address, in its authorized English translation](https://www.aemea.org/math/Hilbert_23_Mathematical_Problems_1900.pdf). The selected entries identify the exact part or later extension being considered.

- **16, part II:** a uniform bound on limit cycles of planar polynomial differential equations of fixed degree. [Buzzi–Novaes](https://arxiv.org/html/2411.09594v1) explain established lower growth and refute a claimed quadratic formula. Finiteness for one vector field does not provide a uniform bound. Smale 13 is related and is not counted as a second entry.
- **10 over the rationals:** explicitly labeled an **open extension**. The original integer problem was settled by Davis–Putnam–Robinson–Matiyasevich. [Garcia-Fritz–Pasten–Vidaux (2025)](https://www.sciencedirect.com/science/article/pii/S0022314X25001180), with an [author preprint](https://arxiv.org/abs/2311.01958v2), prove undecidability after adding height-comparison conditions, leaving the plain rational-solvability question open.

The Riemann hypothesis remains under Millennium rather than being counted again under Hilbert.

## Exploration-fit ratings

An exploration-fit score is an **AI-assisted editorial heuristic**, based on how concrete a research starting point the entry provides. It is not a calibrated difficulty, importance, success-probability, or proof-quality score. Imported references generally require a literature review first; broad named conjectures require narrowing to a special case. Read each entry’s reason and confidence. Historical ratings for code and design parameter cases describe those archived records, not this selection.

## Reproducibility and updates

The public snapshot and workspace snapshot contain the same selected records. All entries are in `problems`; `compact_families` is empty. The public page loads `/research/open-problems.json`; the historical `/open-problems.json` route remains available.

Maintained editorial content lives in `agent_monitor/data/open_problem_curation.json`: collection sources, selected problems, and overlays for imported identifiers. `scripts/curate_open_problems.py` applies that selection to canonical metadata. `scripts/import_open_problems.py` rebuilds the imported metadata from pinned local sources and then applies the same curation. Neither importer executes upstream scripts.

Install `PyYAML` and `pypdf`, obtain the pinned Erdős `data/problems.yaml`, extract the pinned ConjectureBench archive, and download the September notebook PDF. Then run:

```bash
python3 scripts/import_open_problems.py \
  --erdos /path/to/problems.yaml \
  --catalog /path/to/conjecture-bench \
  --notebook /path/to/21tkt.pdf \
  --curation agent_monitor/data/open_problem_curation.json
```

When updating, review source dates and claims, regenerate both snapshots, and run `tests/test_open_problem_catalogue.py`, `tests/test_open_problem_curation.py`, `tests/test_problem_expansion.py`, and `node --test tests/test_problem_catalogue.mjs`. Retained source hashes and revision pins describe imported inputs; editorial review dates describe the selected summaries.

### Historical imports

The earlier 8,052-entry selection included Linear Code Tables, Constant-Weight Codes, Covering Designs, and Ramsey Numbers. Those collections are no longer selected. Their input snapshots, checksums, adapters, and provenance remain for reproducibility in `artifacts/open-problems-expansion-2026-09-10/`, `artifacts/remove-difference-sets-2026-09-10/`, and `scripts/problem_expansion.py`. The September 16 research notes and baseline are under `artifacts/open-problems-curation-2026-09-16/`.

Historical numeric data remain attributable to [Markus Grassl and contributors](https://codetables.de/), [A. E. Brouwer and contributors](https://www.win.tue.nl/~aeb/codes/Andw.html), [Daniel M. Gordon and covering-design contributors](https://dmgordon.org/covering-designs/), and the [Leaps in Bounds tracker and its cited researchers](https://leapsinbounds.org/). ConjectureBench supplied the pinned July covering-design metadata. Retaining these records does not imply that their bounds or open status were rechecked for the current selection.

## Community contributions

Anyone can read the forum. Signed-in users can propose a problem, start a discussion, or reply. Proposals remain **unreviewed community submissions** until reviewed; they do not automatically enter the sourced catalogue or harness memory. Include the statement, original source, and what remains open. Discussions can also report a correction or a new result.
