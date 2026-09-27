# Third-party notices

## Hermes Agent (vendored core)

- Source: https://github.com/NousResearch/hermes-agent
- License: MIT (see `engines/hermes_core/LICENSE`)
- Copyright: Nous Research

The vendored snapshot includes the core agent loop and tools used by Agent
Monitor, along with upstream CLI, gateway, provider, plugin, cron, locale, and
supporting code. Inclusion does not mean every upstream component is enabled by
this application.

## FirstProof batch-2 problems

- Source: https://github.com/1stproof/batch-2
- Bundled under `problems/batch2/` (design + human-solution)

## UCLA / IMProof monitor code

- Derived from the local Token Tracking / Harness Pipeline Monitor project
- UCLA harness sources under `engines/ucla/`

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

## IMProofBench / ProofStack (vendored)

- Source: https://github.com/1stproof/batch-2/tree/main/batch-2-submissions/improofbench
- Bundled under `engines/improof/` (mathagents + ProofStack Author–Critic workflows)
- Sample WorkflowRuns retained for dashboard demos

## Public research catalogue

The September 16, 2026 selection contains 1,756 entries from six collections:
Erdős Problems (590), Kourovka Notebook (1,151), Millennium Prize Problems (6),
Ramanujan’s legacy (3), Smale’s problems (4), and Hilbert’s problems and open
extensions (2). Source references and original editorial summaries are
distinguished in the dataset. Sources retain their own rights; citing a work
does not place its full text under this repository’s license. See the
[source guide](docs/problem-sources.md) for individual research references,
status dates, and reported proof claims.

### Imported metadata

- **Erdős Problems:** Thomas Bloom, Terence Tao, and contributors,
  [maintainer repository](https://github.com/teorth/erdosproblems), revision
  `3c68e941162f81d650fc886eed34e58bed3a6a01` (September 9, 2026).
  Metadata is licensed under Apache-2.0; the notice is retained in
  `docs/public/problem-licenses/erdos-apache-2.0.txt`.
- **Kourovka Notebook:** E. I. Khukhro and V. D. Mazurov, editors, and the
  individual problem authors, [21st edition, September 2026](https://kourovkanotebookorg.wordpress.com/wp-content/uploads/2026/09/21tkt.pdf).
  Copyright © E. I. Khukhro and V. D. Mazurov, 2026. The catalogue retains
  bibliographic identifiers and page links, with original paraphrases for
  selected highlights; the book’s full text is not redistributed.
- **ConjectureBench:** Anirudha Ramesh and Shreyas Pimpalgaonkar, Bespoke Labs
  (2026), [repository](https://github.com/bespokelabsai/conjecture-bench), revision
  `357bcb1a1daf93917d42e8206ceaa55645729a09`. Original curation metadata is
  licensed under CC BY 4.0. Changes include retaining canonical Kourovka
  identifiers, checking the September notebook, excluding answer markers,
  omitting imported statements, adding topics, and merging duplicate references.
  Preserve `docs/public/problem-licenses/conjecturebench-license-data.txt`
  and `docs/public/problem-licenses/conjecturebench-notice.txt`. This metadata
  license does not relicense the Kourovka book.

### Named collections and research summaries

- **Millennium Prize Problems:** [Clay Mathematics Institute](https://www.claymath.org/millennium-problems/)
  and the authors of its official formulations: Andrew Wiles, Pierre Deligne,
  Charles Fefferman, Stephen Cook, Enrico Bombieri, Arthur Jaffe, and Edward Witten.
  Original summaries link the official descriptions and research cited for
  partial results. Navier–Stokes is marked resolution announced, following
  [Clay’s September 2026 response](https://www.claymath.org/news/navier-stokes-announcement/).
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
