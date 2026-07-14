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
