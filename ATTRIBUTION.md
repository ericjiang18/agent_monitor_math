# Third-party notices

## Hermes Agent (vendored core)

- Source: https://github.com/NousResearch/hermes-agent
- License: MIT (see `engines/hermes_core/LICENSE`)
- Copyright: Nous Research

Only the agent harness subset is included (no gateway/TUI/desktop/website).

## FirstProof batch-2 problems

- Source: https://github.com/1stproof/batch-2
- Bundled under `problems/batch2/` (design + human-solution)

## UCLA / IMProof monitor code

- Derived from the local Token Tracking / Harness Pipeline Monitor project
- UCLA harness sources under `engines/ucla/`

## Meta-Harness (bundled)

- Source: https://github.com/stanford-iris-lab/meta-harness (MIT, Stanford IRIS Lab)
- Bundled under `engines/metaharness/` — configure via `META_HARNESS_CMD`

## External CLI harnesses (detected, not bundled)

- Codex CLI — https://github.com/openai/codex
- OpenClaude — https://github.com/Gitlawb/openclaude
- OpenHands — https://github.com/OpenHands/openhands

## IMProofBench / ProofStack (vendored)

- Source: https://github.com/1stproof/batch-2/tree/main/batch-2-submissions/improofbench
- Bundled under `engines/improof/` (mathagents + ProofStack Author–Critic workflows)
- Sample WorkflowRuns retained for dashboard demos

## LLNL math-kg-tools (design reference; not bundled)

- Source: user-supplied `math-kg-tools-main.zip`
- Project: LLNL tooling for the DARPA expMath project
- License: Apache-2.0 (see the archive's `LICENSE`)
- Archive SHA-256:
  `1bd0efe730d236089198d4e24ceb532a022bd1fc9fb66defc529ae7137328502`

The archive's compiler-environment extraction informed ProvingConsole's
independent Lean 4.14 compiler-facts adapter. Its Lean 4.29/4.32 binaries,
Grafeo importer, graph database, and generated parser sources are not copied,
executed, or bundled.

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
