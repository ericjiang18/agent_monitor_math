# Third-party notices

## Hermes Agent (vendored core)

- Source: https://github.com/NousResearch/hermes-agent
- License: MIT (see `engines/hermes_core/LICENSE`)
- Copyright: Nous Research

The vendored snapshot includes the core agent loop and tools used by Agent
Monitor, along with upstream CLI, gateway, provider, plugin, cron, locale, and
supporting code. Inclusion does not mean every upstream component is enabled by
this application.

## UCLA / IMProof monitor code

- Derived from the local Token Tracking / Harness Pipeline Monitor project
- UCLA harness sources under `monitor_core/ucla/`

## First Proof problem statements

- Source: https://github.com/1stproof/batch-2
- Console examples are retained under `problems/ucla/`.
- The separate benchmark-operator bundle and human-solution archive are not
  included in this source tree.

## Meta-Harness (adapted)

- Source: https://github.com/stanford-iris-lab/meta-harness (MIT, Stanford IRIS Lab)
- Proving adaptation: `agent_monitor/runners/metaharness_runner.py` — configure via `METAHARNESS_CMD`
- Upstream license and citation retained under `engines/metaharness/`; unrelated reference projects archived locally under `deleted/`.

## External CLI harnesses (detected, not bundled)

- Codex CLI — https://github.com/openai/codex
- OpenClaude — https://github.com/Gitlawb/openclaude
- OpenHands — https://github.com/OpenHands/openhands
- OpenClaw — https://github.com/openclaw/openclaw

## Installed integration dependency (not vendored source)

- DeepAgents — https://github.com/langchain-ai/deepagents
- Installed into the generated `engines/deepagents/.venv`; the maintained
  Agent Monitor adapter is `agent_monitor/runners/deepagents_runner.py`

## Document-parsing dependencies (installed, not vendored)

- pypdf 6.21.4 — https://github.com/py-pdf/pypdf — BSD-3-Clause. Extracts the
  Kourovka Notebook's PDF text for the problem-source facts.
- pylatexenc 2.11 — https://github.com/phfaist/pylatexenc — MIT. LaTeX
  structure parsing. Both are pinned in `pyproject.toml`.

## IMProofBench / ProofStack (vendored)

- Source: https://github.com/1stproof/batch-2/tree/main/batch-2-submissions/improofbench
- Bundled under `engines/improof/` (mathagents + ProofStack Author–Critic workflows)
- Sample WorkflowRuns retained for dashboard demos

## LLNL math-kg-tools (design reference; not bundled)

- Source: user-supplied `math-kg-tools-main.zip` (GitHub archive of commit
  `27d72344e6bab9a895d37dfa92b17c4b7393c256`)
- Project: LLNL tooling for the DARPA expMath project
- License: Apache-2.0 (see the archive's `LICENSE`; the archive ships no
  `NOTICE`)
- Archive SHA-256:
  `1bd0efe730d236089198d4e24ceb532a022bd1fc9fb66defc529ae7137328502`

The archive's compiler-environment extraction informed ProvingConsole's
independent Lean 4.14 compiler-facts adapter. Its Lean 4.29/4.32 binaries,
Grafeo importer, graph database, and generated parser sources are not copied,
executed, or bundled.

The Analyze mode's LaTeX statement graph (`agent_monitor/analyze/latex_structure.py`)
is a design adaptation of the archive's `latex-tree-dump` / `latex-kg`
pipeline: its entity vocabulary (environment, section, label definition,
label reference, citation, text and math entities linked by containment),
reference resolution from labels to their owning statements, statement
numbering approach (`counters.rs`) and dangling-reference accounting.
ProvingConsole's implementation is original Python on the pylatexenc front
end; no Rust code, binary or generated tree-sitter grammar (MIT, Copyright
(c) 2021 Patrick Förster) from the archive is copied, built or run.

## pylatexenc (installed dependency)

- Source: https://github.com/phfaist/pylatexenc, version 2.11 (pinned in
  `pyproject.toml`)
- License: MIT, Copyright (c) 2015-2019 Philippe Faist
- Used by Analyze to parse LaTeX sources into a node tree, in a
  resource-limited subprocess; not vendored.

## pypdf (installed dependency)

- Source: https://github.com/py-pdf/pypdf, version 6.21.4 (pinned in
  `pyproject.toml`)
- License: BSD-3-Clause, Copyright (c) 2006-2008 Mathieu Fenniak; some
  contributions copyright (c) 2007 Ashish Kulkarni and (c) 2014 Steve Witham
- Used by Analyze to extract text from PDF manuscripts, in a resource-limited
  subprocess; not vendored. (Analyze does not use PyMuPDF.)

## openai/math (test fixtures and Analyze source)

- Source: https://github.com/openai/math, commit
  `fd4aeeb2ee4fc729c18d98444fed42fd0529eeeb`
- License: Apache-2.0 (copy in `tests/fixtures/analyze/openai-math/LICENSE`)
- Bundled: the unmodified TeX sources of three manuscripts as parser test
  fixtures under `tests/fixtures/analyze/openai-math/` (paths and notice in
  its `NOTICE.md`). Analyze fetches other openai/math manuscripts at run time
  from pinned commits and stores them only in the run's workspace.

## Math Agent Harness / Danus Exploring Graph (adapted design)

- Source: user-supplied `Math_Agent_Harness-main.zip`
- Upstream project: [frenzymath/Danus](https://github.com/frenzymath/Danus)
- License: Apache-2.0 (the supplied archive contains no `NOTICE` file)
- Archive SHA-256:
  `da482d4e25e58cebedd70843fdc88a2da0ece9706768148aec8b58b3a27d4dcf`

ProvingConsole adapts the archive's content-addressed fact-dependency and typed
exploration-graph concepts behind its existing provider routing, cancellation,
artifact, and Lean-verification boundaries. The archive's privileged Claude
controller, sandbox-bypassed Codex swarm, verifier HTTP service, bootstrap
scripts, experiments, and runtime state are not copied into or invoked by this
adapter. Modified adapter code is identified as ProvingConsole code and keeps LLM/harness
acceptance distinct from a Lean certificate.
## Ansatz / continual-graph-memory (adapted prompts and data contracts)

- Source: user-supplied `continual-graph-memory-main.zip` (public name
  "Ansatz", Python package `cgm`)
- License: Apache-2.0, Copyright 2026 FrenzyMath (the archive contains no
  `NOTICE` file); a copy of the License ships as `agent_monitor/cgm/LICENSE`
- Archive SHA-256:
  `d12630af90d13688a950c8c8ea14c2cf5ac6a52afbc4e69d4c876dc3944cb825`

The Math Agent Harness runs a tool-free, response-only pipeline derived from
the archive's prompts: strategist (from `cgm/strategy/transport.py`
`ADVISOR_SYSTEM`, `agents/contracts/main_agent.md`, and the
`propose-subgoal-decomposition-plans` / `obtain-immediate-conclusions` skills),
workers (from `agents/contracts/worker_text.md`, `worker.md` Step 4, the
`verify-proof` skill and the Procedure sections of the direct-proving,
localization, invariant, extremal, compactness, witness, reduction,
reformulation and generalization skills), an independent per-lemma verifier
(from `agents/contracts/verifier.md` and the `verify-sequential-statements` /
`synthesize-verification-report` skills) and a curator (from
`agents/contracts/curator.md`). Changes: every tool, MCP, file, web, download
and memory-channel reference was removed; verifier Step 3 (arXiv lookup) was
replaced by a tool-free citation policy; "Fact N" / 16-hex citations became
local lemma labels; outputs became JSON control objects and delimited Markdown
blocks; the curator's propagation rules run as deterministic code. Fact files
and exploring-graph rows follow the archive's on-disk format
(`cgm/core/factgraph.py`, `cgm/core/schema.py`, `cgm/core/exploring_graph.py`).
The adapted prompts carry change notices in `agent_monitor/cgm/prompts.py`;
per-prompt changes are listed in `agent_monitor/cgm/NOTICE.md`. The website adaptation does not import or execute the standalone framework.
The archive's `results/*` manuscripts (no license grant) are not bundled. Model-verifier acceptance is never presented
as a Lean certificate.

The Analyze mode's verifier prompt (`agent_monitor/analyze/prompts.py`
`ANALYZE_VERIFIER_SYSTEM`) is derived from the adapted `VERIFIER_SYSTEM` and is
listed with its changes in `agent_monitor/cgm/NOTICE.md`; Analyze verdicts are
likewise model judgements, never certificates.

## Standalone Continual Graph Memory framework

- Source: the supplied `continual-graph-memory-main` code release.
- Bundled under `cgm-framework/`, separately from the website adaptation.
- Licence: Apache-2.0, Copyright 2026 FrenzyMath; retained in
  `cgm-framework/LICENSE`.

The implementation, contracts, agent skills, configuration templates, examples,
problem inputs, and tests are preserved. Packaging documentation is adapted for
this repository, and CI runs from the subproject directory. Historical experiment
outputs, research manuscripts, and the original archive checksums are excluded.
The framework's code is not implicitly loaded by the website.

## Public research catalogue

The October 2026 catalogue (schema 3) lists every Erdős problem, every problem
of the Kourovka Notebook’s main section, the seven Millennium problems, Hilbert’s
and Smale’s lists, Ramanujan’s legacy, and researched entries of Wikipedia’s list
of unsolved problems, each with a status category and its evidence. Source
references, source facts (states, markers, pages, dates, links, revision ids)
and original editorial summaries are distinguished in the dataset; the facts
are pinned in `agent_monitor/data/problem_sources/`. Sources retain their own
rights; citing a work does not place its full text under this repository’s
license. See the [source guide](docs/problem-sources.md) for individual research
references, status dates, the status method and reported proof claims.

### Imported metadata

- **Erdős Problems:** Thomas Bloom, Terence Tao, and contributors,
  [maintainer repository](https://github.com/teorth/erdosproblems), revision
  `48e0c21bd250e6e725e8a1fca31acabe542dbeb1` (October 11, 2026; earlier
  catalogues used `3c68e941162f81d650fc886eed34e58bed3a6a01`). Metadata is
  licensed under Apache-2.0; the notice is retained in
  `docs/public/problem-licenses/erdos-apache-2.0.txt`. The text of
  erdosproblems.com is not reproduced. The project’s
  [AI-contributions wiki](https://github.com/teorth/erdosproblems/wiki/AI-contributions-to-Erd%C5%91s-problems)
  (frozen June 30, 2026, commit `c8ad4309`) is used for facts only: outcome
  colours, AI systems, dates and sections.
- **Kourovka Notebook:** E. I. Khukhro and V. D. Mazurov, editors, and the
  individual problem authors, No. 21, [arXiv:1401.0300v48](https://arxiv.org/abs/1401.0300v48)
  (October 6, 2026), distributed under arXiv’s non-exclusive licence.
  Copyright © E. I. Khukhro and V. D. Mazurov, 2026. The catalogue uses problem
  numbers, page links, the editors’ ∗/♯ markers and the solution links they
  cite, with original paraphrases for selected highlights; the book’s text is
  not redistributed. PDF text is extracted with pypdf (BSD-3-Clause).
- **ConjectureBench:** Anirudha Ramesh and Shreyas Pimpalgaonkar, Bespoke Labs
  (2026), [repository](https://github.com/bespokelabsai/conjecture-bench), revision
  `357bcb1a1daf93917d42e8206ceaa55645729a09`. Original curation metadata is
  licensed under CC BY 4.0. Changes include retaining canonical Kourovka
  identifiers, checking the September notebook, excluding answer markers,
  omitting imported statements, adding topics, and merging duplicate references.
  Preserve `docs/public/problem-licenses/conjecturebench-license-data.txt`
  and `docs/public/problem-licenses/conjecturebench-notice.txt`. This metadata
  license does not relicense the Kourovka book.

### Status sources

- **Wikipedia:** [List of unsolved problems in mathematics](https://en.wikipedia.org/w/index.php?title=List_of_unsolved_problems_in_mathematics&oldid=1379514120),
  revision 1379514120, by Wikipedia contributors, used as a discovery source:
  entry names, links and revision ids only. Statements and statuses are
  original and cite primary sources; no CC BY-SA 4.0 text is reproduced.
- **formal-conjectures:** [google-deepmind/formal-conjectures](https://github.com/google-deepmind/formal-conjectures),
  The Formal Conjectures Authors, Apache-2.0, commit
  `d352839cee8a6265ae40bd51e009634c24600fc5`. The catalogue links Lean statement
  files and uses the maintainers’ “research open / research solved” categories
  as a cross-check; no Lean source is copied.
- **openai/math:** [openai/math](https://github.com/openai/math), OpenAI,
  Apache-2.0, commit `fd4aeeb2ee4fc729c18d98444fed42fd0529eeeb` (withdrawals
  resolved against `adc7f1241b42e322a6451854ab7e4b4c146bf78a`). Family and
  manuscript titles, dates, withdrawals and Lean-document links are used as
  attributed evidence; claims are never presented as accepted results.
- **Palomar:** [Palomar registry](https://palomar-registry.org/) of Lean-verified
  results; its data is dedicated to the public domain (CC0). Registration
  titles, abstracts and links appear as unreviewed reports.
- **Clay Mathematics Institute:** the Millennium page’s Active, Unsolved and
  Solved listing decides the Millennium source status.

### Named collections and research summaries

- **Millennium Prize Problems:** [Clay Mathematics Institute](https://www.claymath.org/millennium-problems/)
  and the authors of its official formulations: Andrew Wiles, Pierre Deligne,
  Charles Fefferman, Stephen Cook, Enrico Bombieri, Arthur Jaffe, and Edward Witten.
  Original summaries link the official descriptions and research cited for
  partial results. Navier–Stokes is “AI proposed” (resolution announced; Clay’s
  evaluation pending), following
  [Clay’s September 2026 response](https://www.claymath.org/news/navier-stokes-announcement/);
  Poincaré is solved per Clay’s listing.
- **Ramanujan’s legacy:** Srinivasa Ramanujan’s problems and mathematical work,
  with Brocard, Lehmer, and later contributors credited individually. The
  collection uses original statements and references to the research authors;
  it does not attribute every later conjecture to Ramanujan.
- **Smale’s problems:** Steve Smale,
  [Mathematical Problems for the Next Century (1998)](https://www.fim.uni-passau.de/fileadmin/dokumente/fakultaeten/fim/lehrstuhl/muller/SmaleProblems1998.pdf),
  with partial-result authors credited on each selected entry.
- **Hilbert’s problems and open extensions:** David Hilbert,
  [Mathematical Problems (1900 address; authorized English translation)](https://www.aemea.org/math/Hilbert_23_Mathematical_Problems_1900.pdf).
  The rational version of the tenth problem is explicitly identified as a
  later open extension. Modern formulations and partial results cite their authors.

### Historical numeric imports

Code, covering-design, and Ramsey table families are excluded from the current
selection. Preserved snapshots and adapters remain attributable to Markus
Grassl; A. E. Brouwer, J. B. Shearer, N. J. A. Sloane, W. D. Smith, E. Agrell,
A. Vardy, K. Zeger, and later code-table contributors; Daniel M. Gordon and
covering-design contributors; and the Leaps in Bounds tracker, Stanisław
Radziszowski, and the researchers credited by its references. ConjectureBench
supplied the pinned July 2026 covering-design metadata. These retained imports
contain numeric bounds, parameters, and references, not construction files or
full research texts. Historical input hashes and dates remain in
`artifacts/open-problems-expansion-2026-09-10/`.
