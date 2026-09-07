# Agent Monitor

Unified **informal math proving** console with selectable engines and one account-level Monitor:

| Engine | Role |
|--------|------|
| **Math Agent Harness** | Bounded, response-only planner → independent proof candidates → strict critic, with a native fact/exploration graph (adapted from the attached Danus design) |
| **IMProof** | Author–Critic proving workflow ([batch-2 improofbench](https://github.com/1stproof/batch-2/tree/main/batch-2-submissions/improofbench)) |
| **Hermes** | Vendored agent harness core (`engines/hermes_core`) |
| **Codex CLI / Claude Code / OpenClaude / OpenHands** | External coding-agent CLIs (auto-detected); Codex and Claude Code support private per-user subscription login |
| **OpenClaw** | [openclaw/openclaw](https://github.com/openclaw/openclaw) embedded local agent (`--local`) |
| **DeepAgents** | [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents) LangGraph deep agent (own venv) |
| **Meta-Harness** | [stanford-iris-lab/meta-harness](https://github.com/stanford-iris-lab/meta-harness)-style solver → evaluator → proposer harness-evolution loop |
| **DeepSeek Harness** | Official [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) headless API path plus a linked-Codex compatibility path |
| **Danus** | [frenzymath/Danus](https://github.com/frenzymath/Danus/tree/codex) Codex-only proof swarm; setup-required until an isolated deployment is explicitly configured |
| **Plain** | Single-model baseline with no harness scaffolding |

UCLA is retired from the new-run catalog. Historical UCLA runs and artifacts
remain readable and keep their original `ucla` provenance; they are never
silently dispatched through Math Agent Harness.

The Monitor offers two trace views: **Map** (free-form pan/zoom node graph —
each node type renders its own card format) and **Pipeline** (fixed stage columns).

The DAG workspace has four deliberately separate views:

- **Engine graph** (default) has an **Explore / Facts** switch. Explore shows
  the Math Agent Harness workflow (or another engine's execution trace), while
  Facts shows its receipt-bound fact dependencies. A fact accepted by the
  harness critic is still explicitly marked as *not* a Lean certificate.
- **Informal** is the model-distilled logical outline of `proof.md`.
- **Formal · Lean** is the structural graph associated with generated Lean.
- **Compiler facts** shows compiler-observed proof-body uses and type mentions.

Only the Lean checker determines whether a formal proof is verified. Engine,
informal, and compiler graphs are useful provenance/structure views and cannot
promote a run's verification status.

Users select an engine and problem, then monitor agents, tokens, and cost in one UI.

## Library (memory · skills · tools)

Open **Monitor** in the sidebar, then use Memory, Skills, or Tools to store reusable items that are injected
into *every* run, regardless of engine:

- **Memory** — facts/context (e.g. results from earlier runs). A compact memory
  entry is auto-saved after each run (disabled by default; enable what you want reused).
- **Skills** — proof methods/strategies in Markdown.
- **Tools** — bash scripts; saved as `_library/tools/<name>.sh` in the run
  workspace so CLI agents can execute them. DeepAgents additionally registers
  each script as a callable tool.

Items live in `data/library/library.json`. At run start the enabled items are
materialized into the workspace (`_library/MEMORY.md`, `_library/SKILLS.md`,
`_library/tools/`) and a `USER LIBRARY` block is prepended to the prompt.
API: `GET/POST /api/library`, `DELETE /api/library/{id}`, `POST /api/library/settings`.

## Quick start

Everything is vendored in this repo — clone, run two scripts, done:

```bash
git clone <this-repo> && cd Agent_Monitor
./setup.sh      # creates .venv + IMProof venv, copies .env, checks optional CLIs
./start.sh      # serves the proving console
```

Open http://localhost:4600, add your API keys in **Settings** (or edit `.env`),
or connect a Codex / Claude Code subscription account there. Then type a
problem, pick an engine and model, and press Start. Account login and API-key
routing are mutually exclusive for each run, so a subscription run cannot
silently fall through to API billing.

What `setup.sh` does:

1. `.venv` with the console, response-only Math Agent Harness adapter, Hermes,
   legacy UCLA compatibility, and Monitor dependencies (`pip install -e .`)
2. `engines/improof/.venv` (Python ≥ 3.12) with the IMProof/ProofStack stack
3. Copies `.env.example` → `.env` if missing
4. Prints install/status guidance for the *optional* CLI engines (Codex /
   Claude Code / OpenClaude / OpenHands) and auto-logs Codex in from
   `OPENAI_API_KEY`. It does not download Claude Code or request credentials;
   install the official CLI separately, then connect it from **Settings**.
5. Warns if no LaTeX compiler is present (needed for the PDF preview)

The only things not vendored are the optional CLI engine binaries (installed
via npm/uv) and a LaTeX distribution. Math Agent Harness uses the already
selected Codex subscription, Claude Code subscription, or API response backend;
it does not execute the upstream sandbox-bypassing Danus worker runtime.

## Layout

```
agent_monitor/          # CLI, runners, Hermes builder
monitor_core/           # Harness dashboard + cost tracker (from Token_Tracking_Monitor)
engines/
  ucla/                 # legacy UCLA harness source (historical runs only)
  improof/              # Vendored IMProofBench (ProofStack) + sample WorkflowRuns
  hermes_core/          # Slim Hermes agent loop (no TUI/gateway/desktop)
problems/
  ucla/                 # Local problem statements
  batch2/               # FirstProof batch-2 design/statements
data/                   # Runtime runs / cache / logs / hermes home (gitignored)
```

## Hermes core

Only the agent harness is vendored (≈20–30MB source): `AIAgent`, tools, providers.
Messaging gateway, desktop app, website, and full skill packs are **not** included.

State is isolated under `data/hermes` via `HERMES_HOME` (does not touch `~/.hermes`).

## Engines (Phase 1)

- `agent-monitor serve` — dashboard for existing / built runs
- `agent-monitor build` — rebuild unified manifest into `data/cache`
- Runners under `agent_monitor/runners/` wrap each engine for later one-click launch

## License

Monitor and legacy UCLA/IMProof code follow their original project licenses.
Vendored Hermes core is MIT (Nous Research) — see `engines/hermes_core/LICENSE`.
The Math Agent Harness graph design is adapted from Danus under Apache-2.0;
see `ATTRIBUTION.md`. The upstream privileged worker/runtime code is not bundled
into, imported by, or executed by this adapter.
