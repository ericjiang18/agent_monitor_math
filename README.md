# Agent Monitor

Unified **informal math proving** console with three engines and one pipeline dashboard:

| Engine | Role |
|--------|------|
| **UCLA** | Multi-stage literature / advisor / solver / verify harness |
| **IMProof** | Author–Critic proving workflow ([batch-2 improofbench](https://github.com/1stproof/batch-2/tree/main/batch-2-submissions/improofbench)) |
| **Hermes** | Vendored agent harness core (`engines/hermes_core`) |
| **Codex CLI / OpenClaude / OpenHands** | External coding-agent CLIs (auto-detected) |
| **OpenClaw** | [openclaw/openclaw](https://github.com/openclaw/openclaw) embedded local agent (`--local`) |
| **DeepAgents** | [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents) LangGraph deep agent (own venv) |
| **Meta-Harness** | [stanford-iris-lab/meta-harness](https://github.com/stanford-iris-lab/meta-harness)-style solver → evaluator → proposer harness-evolution loop |

The Monitor offers two trace views: **Map** (free-form pan/zoom node graph —
each node type renders its own card format) and **Pipeline** (fixed stage columns).

Users select an engine and problem, then monitor agents, tokens, and cost in one UI.

## Library (memory · skills · tools)

Open **📚 Library** in the sidebar to store reusable items that are injected
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
type a problem, pick an engine, and press Start.

What `setup.sh` does:

1. `.venv` with the console + Hermes + UCLA + Monitor dependencies (`pip install -e .`)
2. `engines/improof/.venv` (Python ≥ 3.12) with the IMProof/ProofStack stack
3. Copies `.env.example` → `.env` if missing
4. Prints install commands for the *optional* CLI engines (Codex / OpenClaude /
   OpenHands) and auto-logs Codex in from `OPENAI_API_KEY`
5. Warns if no LaTeX compiler is present (needed for the PDF preview)

The only things not vendored are the optional CLI engine binaries (installed
via npm/uv) and a LaTeX distribution — Hermes, IMProof, and UCLA run entirely
from this repo.

## Layout

```
agent_monitor/          # CLI, runners, Hermes builder
monitor_core/           # Harness dashboard + cost tracker (from Token_Tracking_Monitor)
engines/
  ucla/                 # UCLA harness source (no Logs)
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

Monitor and UCLA/IMProof code follow their original project licenses.
Vendored Hermes core is MIT (Nous Research) — see `engines/hermes_core/LICENSE`.
