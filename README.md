# Agent Monitor

Agent Monitor is a web console for launching mathematical proving agents,
watching their work as a pipeline or dependency graph, and keeping the resulting
proofs, traces, references, token usage, and cost together. It integrates
several native and external proving engines behind one run format.

If this is your first day in the codebase, read the deployment boundary below
before running setup scripts or assuming that a file edit is live.

## Read this first: this checkout is not the live Python source for moonshot.hailab.io, which is now the old deprecated domain.

> [!IMPORTANT]
> `/home/repo/agent_monitor_math` is the development/workspace checkout. It is
> physically located on the same Amazon EC2 host as production, but systemd
> does **not** import the main console's Python or HTML from this checkout.
> There is no automatic Git-to-production synchronization.

The current hosted deployment is transitional and deliberately split across
several paths. Verify it with [OPERATIONS.md](OPERATIONS.md) before every
deployment because systemd and Caddy—not this README—are the final authority.

### Current hosted request flow

```text
Browser
  │ HTTPS
  ▼
Caddy (:443)
  ├── / and public pages ─────► /home/repo/agent_monitor_math/docs/.vitepress/dist
  ├── /docs/* (old links) ────► temporary compatibility alias during migration
  ├── /sign-in ──────────────► stable redirect to the mounted private console
  │                                │
  │                                ▼ 127.0.0.1:4600
  │                           agent-monitor.service
  │                           ├── Python/HTML source: /home/ubuntu/agent_monitor_math
  │                           ├── interpreter:       /home/repo/agent_monitor_math/.venv
  │                           └── persistent data:   /home/repo/agent_monitor_math/data
  │
  └── kimi.54-82-94-57.sslip.io public routes
                              └──► 127.0.0.1:4610
                                   │
                                   ▼
                              proving-kimi-public.service
                              /opt/proving-kimi-public/releases/<digest>
```

The direct hosted homepage is <https://54-82-94-57.sslip.io/> and the stable
console entry is <https://54-82-94-57.sslip.io/sign-in>. `sslip.io` supplies
DNS for the IP embedded in that hostname; it does not deploy or store this
application. Caddy on the EC2 instance decides what those requests reach.

`moonshot.hailab.io` is the old canonical hostname, but its Cloudflare
configuration is outside this repository and still needs a separate origin
cutover. A working direct `sslip.io` page does not prove that the Cloudflare
hostname is serving the same origin.

### What each host path means

| Path | Current role | Safe editing rule |
|---|---|---|
| `/home/repo/agent_monitor_math` | This checkout, the live Python virtualenv, persistent data, docs source, and published docs output | Edit and test source here, but do not assume Python changes are deployed. On this host, setup and docs commands can still affect production dependencies or content. |
| `/home/ubuntu/agent_monitor_math` | Mutable source checkout imported by `agent-monitor.service` | Treat as live hotfix state. Python changes normally require a controlled restart; the login HTML is read on each request. Do not use this as the long-term development workflow. |
| `/opt/proving-kimi-public/releases/<digest>` | Root-owned immutable release for the separate anonymous public Kimi service | Never patch a release in place. Build a new reviewed release and switch the unit to it. |
| `/opt/agent-monitor-lean/releases/<digest>` | Immutable Lean tooling used through hardened broker services | Never edit in place. This is not the web console source. |
| `/etc/systemd/system/` and `/etc/caddy/Caddyfile` | Process, path, proxy, TLS, and public-route configuration | Inspect and validate deliberately. A repository edit cannot update these files. |
| `/etc/credstore.encrypted/` and `/run/credentials/` | Encrypted-at-rest systemd credentials and their read-only runtime mounts | Never print, copy into Git, or replace casually. Production does not use a plaintext repository `.env`. |

### What updates automatically?

| Change | Does the hosted site update? |
|---|---|
| Edit Python under `/home/repo/agent_monitor_math/agent_monitor/` | **No.** The running service imports `/home/ubuntu/agent_monitor_math/agent_monitor/`. |
| Push or pull Git commits | **No.** There is no deployment hook or checkout synchronization. |
| Edit live Python under `/home/ubuntu/agent_monitor_math/` | Not until the affected process is deliberately restarted; imported modules remain in memory. |
| Edit live `agent_monitor/web/login.html` under `/home/ubuntu` | On the next request with the current server, because that template is read per request. Mirror the change into `/home/repo/agent_monitor_math/agent_monitor/web/login.html` and ultimately a reviewed commit so it is not lost. |
| Edit Markdown, Vue, CSS, or assets under `docs/` | Not until `npm run docs:build`; on this EC2 host that command atomically publishes the live root-mounted static site. |
| Edit Caddy or a systemd unit | Not until validated and explicitly reloaded/restarted. |
| Edit Cloudflare settings | Separate dashboard/API action; nothing in this repository does it. |

### Why keep a repository instead of coding in `/home/ubuntu`?

The repository should provide reviewable history, reproducible tests, dependency
locks, and a clean input for building releases. A live checkout is optimized for
serving current traffic and may contain emergency hotfixes, generated files, or
state that has never been reviewed. Editing only `/home/ubuntu` makes a fix easy
to lose and impossible to reproduce reliably. As of 2026-09-07, the live Ubuntu
checkout is on a different revision and has substantial uncommitted/hotfixed
state; it must not be overwritten wholesale.

There is currently **no repository command that safely deploys the main Python
console**. Do not `rsync`, recursively copy, pull/reset, or replace
`/home/ubuntu/agent_monitor_math` from this checkout. Before any promotion,
inventory and preserve both trees, reconcile the live-only changes, review the
candidate, and use a controlled service restart plus smoke tests. `setup.sh` is
development bootstrap code, not that deployment procedure.

The desired long-term model is:

1. develop and test in a clean checkout;
2. review a commit;
3. build a fresh immutable release;
4. smoke-test it;
5. switch the service to that release; and
6. retain the previous release for rollback.

The present `/home/repo` + `/home/ubuntu` hybrid is transitional, not a pattern
to copy into new services.

> [!WARNING]
> The zero-byte `.git/index` was safely rebuilt from `HEAD` on 2026-09-08 after
> preserving the tracked changes, all non-ignored untracked files, and the
> unreachable Git objects under `.git/recovery/index-20260908T2125Z/`. Normal
> `git status` and `git diff` now work, but the tree still contains substantial
> unstaged and untracked work. Review it explicitly; do not use reset, checkout,
> clean, bulk-add, garbage-collection, or pruning commands as cleanup shortcuts.

## How the application fits together

1. `agent_monitor/console_server.py` serves the authenticated HTML and JSON API.
2. `auth.py`, `settings.py`, and `library.py` resolve accounts, provider
   configuration, sessions, and reusable user material.
3. `jobs.py` creates a dedicated run workspace and chooses an engine through
   `engines_registry.py`.
4. An adapter under `agent_monitor/runners/` invokes a built-in, vendored, or
   external agent and converts its stream into normalized events.
5. `monitor_core/harness_dashboard/` parses artifacts into the common run/cache
   representation used by the pipeline and graph views.
6. Run records, workspaces, auth state, and caches live below `data/`.

A per-run workspace separates artifacts organizationally; it is **not** an
operating-system security sandbox. Local agent processes share the host trust
boundary, and some run with broad tool/filesystem access. Do not run untrusted
prompts, tools, repositories, or generated code on the production host. Use a
separate sandbox/worker or a hardened broker when hostile-code isolation is
required.

The main engine families are:

| Family | Examples | Where integration lives |
|---|---|---|
| Built-in/vendored proving harnesses | UCLA, IMProof/ProofStack, Hermes | Engine snapshot under `engines/`; console adapter under `agent_monitor/runners/` |
| External agent CLIs | Codex, OpenClaude, OpenHands, OpenClaw | Detection/commands in `engines_registry.py`; wrappers under `runners/` |
| Console-owned runners | Plain baseline, DeepAgents adapter, Meta-Harness adaptation | `agent_monitor/runners/` |
| Verification and audit tools | Lean verification, proof graph, deterministic proof tools | `lean_verify.py`, `proof_bridge.py`, `proof_graph.py`, `proof_tools.py` |

In this development checkout, `agent_monitor/engines_registry.py` is the
authoritative engine list. The hosted console instead executes the corresponding
file under `/home/ubuntu/agent_monitor_math`, which currently differs. Some model
routes and availability checks are also resolved dynamically at runtime.

### Engine catalog

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
| **Kimi** | Tool-free Kimi K3 draft → critique → refine harness; the default for guest sessions |
| **Plain** | Single-model baseline with no harness scaffolding |

UCLA is retired from the new-run catalog. Historical UCLA runs and artifacts
remain readable and keep their original `ucla` provenance; they are never
silently dispatched through Math Agent Harness.

### Trace and DAG views

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

## Library (memory · skills · tools)

Open **Monitor** in the sidebar, then use Memory, Skills, or Tools to store
reusable items that are injected into *every* run, regardless of engine:

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

Guests do not inherit the shared agent profile or library.

## Hermes core

Only the agent harness is vendored (≈20–30MB source): `AIAgent`, tools, providers.
Messaging gateway, desktop app, website, and full skill packs are **not** included.

State is isolated under `data/hermes` via `HERMES_HOME` (does not touch `~/.hermes`).

## Set up a separate development clone

Use these commands on a laptop, disposable environment, or genuinely separate
checkout. **Do not run `setup.sh` casually in this EC2 checkout:** its root
`.venv` is also the interpreter used by the live service.

Requirements:

- Python 3.11–3.13 for the main package
- Python 3.12+ for the IMProof and DeepAgents environments
- Node/npm for public-documentation development
- Optional LaTeX (`pdflatex` or `tectonic`) for PDF preview
- Optional external agent CLIs for their corresponding engines

```bash
git clone https://github.com/ericjiang18/agent_monitor_math.git
cd agent_monitor_math
./setup.sh
AGENT_MONITOR_HOST=127.0.0.1 ./start.sh
```

Open <http://localhost:4600>. This foreground development server has no Caddy,
TLS, systemd sandbox, Cloudflare routing, or production credential mounts.
Starting it on the hosted EC2 instance may contend with the existing service on
port 4600. Without `AGENT_MONITOR_HOST=127.0.0.1`, the current server default is
`0.0.0.0`, which can expose the development console to a LAN or cloud security
group.

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

Add your API keys in **Settings** (or edit `.env`), or connect a Codex / Claude
Code subscription account there. Account login and API-key routing are mutually
exclusive for each run, so a subscription run cannot silently fall through to
API billing.

### Access modes

The sign-in page offers **Continue as guest** by default. Guest sessions are
bound to the browser cookie: clearing site data or changing browsers loses
access to that guest's runs, and guest work is not transferred into a later
account. Guests default to **Kimi K3** with the tool-free **Kimi proof harness**:
draft an argument, critique it, then refine the result. Hosted access can use
the existing sponsored Kimi broker, or a configured dedicated Kimi credential;
otherwise, visitors can add their own Kimi key. The one-call `plain` baseline
is also available when enabled. Claude Code and Codex require sign-in.

Guests cannot set a custom provider URL, inherit unrelated server credentials
or the shared agent profile/library, use local execution tools or subagents,
or continue a run. Completed guest proofs now generate an informal DAG,
a Lean formalization checked by the isolated Lean broker, and a formal DAG.
These derived views use the included Kimi K3 service. Opening an older completed
guest proof starts its missing pipeline once. Guests can also use Verify,
Compile, Check, Audit, and Lean feedback; coding-agent Formal harnesses require
sign-in. Manual derived actions allow one active request per guest, two globally,
24 requests per guest per hour, and 120 globally. Lean repairs are capped at one.
The defaults allow
one active run per guest, two active guest runs globally, and 10 new guest
sessions per minute. Each guest can start six runs per hour and **10 runs in
total** before sign-in is required. Deleting a run does not restore the
cumulative allowance. All guests share a default 30-start hourly ceiling.
Each Kimi stage and Plain response has a default 4,096-token output cap.
Provider-key checks are limited to one active check per guest, four globally,
and six attempts per guest per hour (60 globally). A signed-in account is the
durable option for history, full proving harnesses, provider choices, and
account connections.

Guest sessions expire after 24 hours. Ending one deletes its database identity
and encrypted provider settings immediately. Expired/orphan guest identities are removed during later session
creation; guest-tagged run artifacts remain subject to the operator's normal
run-retention policy.

Set `AGENT_MONITOR_GUEST_ACCESS=0` to remove the explicit guest option. Operators
can adjust the bounded defaults with the `AGENT_MONITOR_GUEST_*` settings in
`.env.example`. The engine list is always intersected with the built-in
tool-free guest ceiling (`kimi` and `plain`). Setting `PLAIN_CMD` or
`KIMI_PROOF_CMD` removes the corresponding engine from guest eligibility
because an arbitrary command override is outside that reviewed contract. The older
`AGENT_MONITOR_PUBLIC=1` mode is different: it skips the choice screen and
automatically creates the same rate-limited restricted guest session for an
unauthenticated visitor. An explicit `AGENT_MONITOR_GUEST_ACCESS=0` disables
both guest entry paths.

HTTP self-registration is closed by default, including on a fresh database, so
a remote visitor cannot race the operator for the first-admin slot.
`AGENT_MONITOR_REGISTRATION_OPEN=1` deliberately opens an enrollment window;
unset it again after creating the intended account. Do not leave registration
open on a host where ordinary accounts can launch local harnesses:
authentication does not make prompts or generated code trusted, and those
engines are not an OS sandbox.

`setup.sh` performs several material actions:

1. creates or updates `.venv` and installs this package in editable mode;
2. creates `engines/improof/.venv` and `engines/deepagents/.venv` when a suitable
   Python is available;
3. copies `.env.example` to ignored `.env` when missing;
4. may download Node and install OpenClaw under the current user account;
5. checks optional CLIs and may authenticate Codex from `OPENAI_API_KEY`; and
6. checks for a LaTeX compiler.

It is bootstrap automation, not production deployment automation.

### Research attachments

Use **Attach files**, drag files onto a problem or follow-up composer, or paste
an image into its text box. Preview and remove attachments before pressing
**Prove** or **Send**. A submission may contain just attachments. Failed
submissions keep the selected files so they can be retried.

Supported files are PDF, UTF-8 TXT/Markdown/LaTeX/BibTeX/Lean, CSV/TSV/JSON,
and PNG/JPEG/WebP/GIF images. Each message accepts up to 8 files, 10 MB per
file, and 20 MB combined; each run retains up to 80 files and 100 MB.

Original files are saved in the run workspace, with authenticated downloads
in the chat history. They remain available to queued feedback and subsequent
continuations. The harness prompt includes file paths and bounded text
excerpts (12,000 characters per file, 40,000 total). Reading images requires
vision tools and a compatible model; reading PDFs requires the harness's
document tools. Hermes enables its vision toolset for this purpose. Text-only
engines can use text excerpts but cannot interpret binary attachments by
themselves. Files are not added to the shared library.

The create-run and chat JSON endpoints accept an optional `attachments` array
of `{ "name": "notes.tex", "data": "<base64 file bytes>" }` objects. Validation
rejects unsupported types, invalid encodings, and oversized batches before
writing files. Deleting a run also deletes its workspace attachments.

### Common commands

```bash
# Inspect the installed CLI.
./.venv/bin/agent-monitor --help
./.venv/bin/agent-monitor problems

# Rebuild normalized dashboard cache. This mutates data/cache.
./.venv/bin/agent-monitor build

# Equivalent local server entry points.
AGENT_MONITOR_HOST=127.0.0.1 ./start.sh
AGENT_MONITOR_HOST=127.0.0.1 ./.venv/bin/agent-monitor serve
```

### Tests

Install the development extra in a separate development environment and run the
root security/regression suite:

```bash
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/python -m pytest -q tests
```

With `uv`, the equivalent isolated command is:

```bash
uv run --extra dev pytest -q tests
```

Prefer unit/fixture checks before real engine runs. A live model invocation can
spend provider credits and can write substantial workspaces below `data/runs/`.
The root test modules are a focused security/configuration suite, not an
end-to-end engine, browser, Caddy, systemd, or vendored-project test suite.

### Public documentation

```bash
npm ci
npm run docs:dev       # local VitePress development server
npm run docs:preview   # preview an existing build
npm run docs:build     # staged, atomic publication to docs/.vitepress/dist
```

`docs:build` uses GNU `mv --exchange`; it is designed for the Linux host and may
need adaptation on macOS. On this EC2 host it is a production publication
action because Caddy serves the resulting `dist/` directory directly.

## Complete top-level directory map

| Directory | What it contains | Classification / edit guidance |
|---|---|---|
| `.agents/` | Currently empty placeholder | No current application role. |
| `.codex/` | Currently empty placeholder | No current application role. Runtime Codex state is under `data/codex_home/`, not here. |
| `.git/` | Git objects, references, index, and local recovery bundle | Metadata, never application source. The index was recovered on 2026-09-08; preserve `.git/recovery/` and do not hand-edit Git metadata. |
| `.pytest_cache/` | Pytest discovery and last-run cache | Generated; safe to recreate, never edit. |
| `.venv/` | Main editable Python environment | Generated. On this EC2 host it is shared by the live service, so rebuilding it is operationally significant. |
| `.vscode/` | Editor recommendations and local Git UI settings | Developer convenience only; ignored and not used at runtime. |
| `agent_monitor/` | Primary console, API, orchestration, adapters, proof tools, starter skills, and web UI | First-party maintained source in this checkout. See the detailed map below. |
| `agent_monitor.egg-info/` | Package metadata created by the editable install | Generated; recreate by installing the package. |
| `data/` | Runs, workspaces, users, credentials-related state, caches, CLI homes, and user library | Runtime state; sensitive, large, and mostly ignored. Never treat it as source. |
| `docs/` | VitePress homepage/documentation source, theme, assets, cache, and build output | Edit source/theme/assets. Never hand-edit `.vitepress/dist` or `.vitepress/cache`. |
| `engines/` | A mix of vendored engine snapshots and generated engine virtualenvs | Not uniformly source. Prefer console adapters for integration changes and preserve upstream licenses. |
| `monitor_core/` | Shared harness-dashboard parsing/rendering and auxiliary LLM cost tracker | First-party/derived maintained source. |
| `node_modules/` | npm packages for VitePress, Vue, and fonts | Generated from `package-lock.json`; recreate with `npm ci`. |
| `problems/` | Problem statements, manifest, First Proof benchmark inputs, and human references | Curated source/reference data; beware answer leakage and cloud-running scripts. |
| `scripts/` | Atomic docs publisher and operator-only user-setting encryption migration | Maintained operations source; read each script before running it. |
| `tests/` | Password reset, guest-access boundaries, encrypted settings, subprocess isolation, and user-setting encryption tests | Maintained source tests. |

### `agent_monitor/` in detail

| Path | Responsibility |
|---|---|
| `console_server.py` | Main threaded HTTP server, HTML delivery, routing, auth endpoints, run APIs, and security headers. |
| `jobs.py` | Background run lifecycle, workspaces, engine subprocesses, continuations, persistence, and cleanup. High-risk integration changes often pass through here. |
| `engines_registry.py` | Engine metadata, availability checks, command templates, model compatibility, and prompt construction. |
| `auth.py` | SQLite users/sessions, password hashing/reset, session invalidation, and encrypted per-user environment values. |
| `settings.py` | Operator configuration and user-visible provider/model settings. Production server-level values are operator-managed. |
| `subprocess_env.py` | Removes control-plane/provider secrets from inherited child environments and overlays the selected route. |
| `agent_config.py` | Agent persona/system-prompt, skill, and memory configuration. |
| `library.py` | Persistent reusable memory, skills, and tools injected into run workspaces. |
| `skill_seeds.py` | Seeds default skills and memory into a fresh agent home. |
| `proof_graph.py` | Distills proof/run output into a logical dependency graph. |
| `proof_tools.py` | Bounded deterministic helpers for proof and research audits. |
| `lean_verify.py` | Coordinates informal-to-Lean verification. |
| `proof_bridge.py` | Conservative bridge between the informal proof graph and Lean artifacts. |
| `cli.py` | Installed `agent-monitor` command entry point. |
| `cli_events.py` | Normalizes JSON/event streams emitted by external agent CLIs. |
| `codex_login.py` | Codex account/API authentication helpers used by engine launches. |
| `schema.py` | Canonical run and event field definitions. |
| `paths.py` | Repository path resolution and import-path bootstrap for vendored components. |
| `server.py` | Compatibility/thin server re-export; prefer `console_server.py` or the CLI. |
| `builders/` | Converts engine/session artifacts—currently especially Hermes—into common run records. |
| `runners/` | Adapters for Hermes, IMProof, UCLA, Codex-backed tools, DeepAgents, Meta-Harness, OpenClaude, OpenClaw, OpenHands, and the plain baseline. |
| `starter_skills/` | Source-controlled mathematical workflow skills, agent metadata, audit references, JSON schemas, and validation scripts. Runtime/user copies live elsewhere. |
| `web/console.html` | Main authenticated single-file console UI. |
| `web/login.html` | Shared sign-in, registration, forgot-password, and reset-password UI. Both `/login` and `/reset-password` render this file. |
| `__pycache__/` | Generated bytecode; never edit or review as source. |

### `monitor_core/` in detail

| Path | Responsibility |
|---|---|
| `harness_dashboard/` | Parses logs and UCLA/IMProof artifacts, builds cache/manifests, computes quality/cost summaries, resolves LaTeX provenance, compiles PDF previews, and serves the classic dashboard. |
| `harness_dashboard/web/index.html` | Classic inspection dashboard, distinct from `agent_monitor/web/console.html`; its rebuild action mutates the dashboard cache. |
| `llm_cost_tracker/metrics.py` | Extracts cost and efficiency metrics from trajectories. |
| `llm_cost_tracker/pricing.py` | Token-price lookup/calculation support. |
| `llm_cost_tracker/analysis/` | Offline CSV/report/plot scripts for token consumption and model comparisons. |
| `llm_cost_tracker/proxy/` | Optional standalone LiteLLM proxy configuration, launcher, and trace logger. It is not started by `./start.sh`. |

The package bootstrap exposes `monitor_core/harness_dashboard` as the
`harness_dashboard` import used by older code. Follow existing path helpers
instead of adding ad-hoc `sys.path` changes.

### `engines/` in detail

| Path | What it really is | Editing/testing notes |
|---|---|---|
| `deepagents/` | Generated `.venv` containing the PyPI DeepAgents installation; there is no vendored DeepAgents source here | Do not edit the environment. The owned integration is `agent_monitor/runners/deepagents_runner.py`. |
| `hermes_core/` | Vendored Nous Research Hermes snapshot | Agent Monitor primarily uses the agent loop/tools, although this snapshot also contains upstream CLI, gateway, provider, plugin, cron, locale, and helper code. Preserve its license and avoid broad rewrites. |
| `improof/` | Self-contained vendored ProofStack/mathagents project with its own Python 3.12+ environment, configs, tests, Docker files, app, samples, and captured WorkflowRuns | Read `engines/improof/AGENTS.md` before editing. `.venv/` and `WorkflowRuns/` are generated/runtime. Its upstream Flask app is not the main Agent Monitor UI. |
| `metaharness/` | Upstream license, citation, and integration notes | The console uses `agent_monitor/runners/metaharness_runner.py`. Unrelated upstream reference projects and datasets were moved to the local `deleted/` archive. |
| `ucla/` | Vendored source-only UCLA literature/advisor/solver/verifier harness | The main wrapper is `agent_monitor/runners/ucla.py`; dependencies come from the root environment. Prefer wrapper changes for integration-only behavior. |

Cheap engine checks should use unit tests or documented fake adapters. Real
model calls consume provider credits. Archived Meta-Harness reference workflows can
also provision remote sandboxes and incur substantial costs.

#### Inside `engines/improof/`

| Path | Responsibility |
|---|---|
| `.venv/` | Generated Python 3.12+ environment; never edit installed files directly. |
| `src/mathagents/` | Normalized provider clients, configuration, and mathematical-agent tools. |
| `src/proofstack/` | Author–Critic DAG, agents, sandbox abstractions, events, and run state. |
| `configs/` | Source-controlled model, tool, and workflow definitions, including workflow instructions. |
| `scripts/` | Workflow, batch, evaluation, First Proof, and wiring/smoke entry points. Read a script before running it. |
| `app/` | Upstream Flask workflow viewer/editor; it is not the main Agent Monitor console. |
| `deploy/` | Workflow-container and isolated Codex-sandbox image definitions; these serve different roles. |
| `tests/` | Vendored unit and contract tests. Pytest is not installed by the root setup script into this environment. |
| `evals/` | Engine-specific regression/evaluation area. |
| `problems/` | Upstream examples and engine-local problem material. |
| `smoke/` | Fake First Proof fixtures plus local smoke scripts; full variants can make paid API calls. |
| `samples/` | Prebuilt normalized dashboard fixture. |
| `WorkflowRuns/` | Captured/generated run artifacts consumed by dashboard builds, including retained demo material; do not hand-edit or delete blindly. |
| `test_run_problems/` | Confidential pressure-test staging, ignored except for its README/rules. Do not expose it to solver workspaces. |

Useful low-risk checks in a separate clone include:

```bash
engines/deepagents/.venv/bin/python -c 'import deepagents'
cd engines/improof
.venv/bin/python scripts/smoke_firstproof_adapter.py  # documented no-API adapter check
uv run --with pytest pytest -q                       # resolves pytest if needed
```

Do not confuse the adapter smoke with `smoke/run_local.sh fast`, which makes
real provider calls. The unrelated Meta-Harness reference experiments are
archived locally under `deleted/engines/metaharness/`; they are not required
for the console.

### `problems/` in detail

| Path | Responsibility |
|---|---|
| `manifest.json` | Console/CLI selectable-text lookup. IDs must be unique and paths valid. It currently needs curation: it includes documentation/reference-answer assets and a duplicate ID, rather than being a clean benchmark-problem catalog. |
| `ucla/` | Primary local UCLA statements and sample/correct variants used by the console. It mirrors `monitor_core/ucla/problems/`; keep intentional updates synchronized. |
| `batch2/design/` | First Proof benchmark-operator mini-project, protocol, shared input, allowlist, cloud runner, secret/result placeholders, and dummy Docker submission. |
| `batch2/human-solution/` | Human TeX/PDF reference solutions and figures. Keep these out of blinded solver workspaces to avoid answer leakage. |

> [!CAUTION]
> `problems/batch2/design/run.sh` is **not** a local launcher and is unrelated
> to deploying the hosted console. It provisions a temporary billable EC2
> instance, uploads a submission/input/secrets, runs Docker, downloads results,
> and terminates the instance. Review its configuration and AWS account impact
> before ever executing it.

### Public research catalogue

The public catalogue selects **1,756 entries across six collections** as of
September 16, 2026: 590 Erdős, 1,151 Kourovka, six Millennium, three Ramanujan,
four Smale, and two Hilbert problems or open extensions. Clay currently lists
five of the retained Millennium problems as unsolved; Navier–Stokes carries a
separate **resolution announced** status while the prize process remains pending.
See [Clay’s announcement](https://www.claymath.org/news/navier-stokes-announcement/).

Most imported records are references to original statements. Selected entries
add original, cited summaries, partial results, and separately labeled proof
claims. These records do not certify proofs. The [source guide](docs/problem-sources.md)
documents status dates, source pins, attribution, and the historical imports.

Maintain selected content in `agent_monitor/data/open_problem_curation.json`.
`scripts/curate_open_problems.py` applies it to canonical metadata;
`scripts/import_open_problems.py` rebuilds from the pinned source inputs and
applies the same selection. Regenerate both public and workspace snapshots
when changing the catalogue. Neither a source edit nor regeneration publishes
the website; follow the deployment procedure above.

### `data/` in detail

`data/` is host-local state. A Git push, pull, clone, or source deployment does
not transfer users, sessions, API settings, runs, OAuth logins, or caches.

| Path | Contents and handling |
|---|---|
| `users.db`, `users.db-wal`, `users.db-shm` | Active SQLite auth/settings database and WAL sidecars. It contains accounts, password/session material, and encrypted per-user settings. Never copy casually or manipulate while the service is running. |
| `runs/` | Canonical run JSON plus `workspaces/` containing proofs, source downloads, logs, libraries, nested repositories, Node/Lean builds, and other generated artifacts. Potentially sensitive and often very large. |
| `cache/` | Derived dashboard JSON, manifests, and PDF previews. Rebuildable, but keep it coherent with run records. |
| `codex_home/` | Isolated per-user/account Codex homes containing auth tokens, sessions, logs, plugins, skills, and caches. Highly sensitive. |
| `claude_home/` | Retained per-user Claude CLI homes/backups/history. Treat as sensitive runtime state; confirm consumers before cleanup. |
| `hermes/` | Runtime `HERMES_HOME`: persona, config, memories, user skills, sessions, logs, caches, verification state, and LSP packages. Default source skills belong under `agent_monitor/starter_skills/`, not here. |
| `library/` | Persistent user library injected into runs and materialized under each workspace's `_library/`. Potentially private. |
| `logs/` | Application log target/placeholder. The systemd service's authoritative output is in journald. |
| `calls.jsonl` | Optional LiteLLM/cost-proxy trace input. It may contain detailed request metadata or content. |

Use the console's Delete action or its run-deletion API so a run record, cache,
PDF, workspace, manifest entry, and related Hermes session records/messages are
removed together. This does not delete the user's login session, shared Library,
or persistent Hermes memories. Do not manually delete a random subset from a
live data directory.

### `docs/`, `scripts/`, and `tests/`

| Path | Responsibility |
|---|---|
| `docs/index.md` | Public homepage content. |
| `docs/overview.md` | Product and architecture overview for users. |
| `docs/quickstart.md` | End-user onboarding. |
| `docs/sign-in.md` | Documentation-side sign-in bridge. |
| `docs/.vitepress/config.mjs` | Public routing, navigation, search, and metadata. |
| `docs/.vitepress/theme/` | Custom Vue/CSS theme and UCLA institution rail. |
| `docs/public/` | Static public artwork and partner assets; keep provenance in `.vitepress/partner-assets.md`. |
| `docs/.vitepress/dist/` | Generated published site. Never hand-edit. |
| `docs/.vitepress/cache/` | Generated Vite cache. Never hand-edit. |
| `scripts/build_docs_atomic.mjs` | Builds into a private staging directory and atomically exchanges it with the published `dist/`. |
| `scripts/migrate_user_env_encryption.py` | Operator-only, credential-bound migration/verification for user environment encryption. Do not use as routine startup. |
| `tests/test_auth_password_reset.py` | Reset token, concurrency, canonical URL, mail, and logging safeguards. |
| `tests/test_console_password_reset.py` | Password-reset HTTP routes and response behavior. |
| `tests/test_console_guest_access.py` | Explicit guest sessions, isolation, limits, engine policy, and browser-script syntax. |
| `tests/test_encrypted_server_settings.py` | Separation between immutable server settings and writable per-user settings. |
| `tests/test_subprocess_env.py` | Child-process secret scrubbing and selected-provider projection. |
| `tests/test_user_env_encryption.py` | Versioned encryption, migration, rollback, and legacy-value handling. |

## Every standalone file at the repository root

| File | Purpose | Edit/run guidance |
|---|---|---|
| `.env.example` | Blank, safe local-development configuration template | Copy to ignored `.env` only in a development clone. Production uses encrypted systemd credentials. |
| `.gitignore` | Excludes known runtime data, virtualenvs, Node modules, build output, caches, local logs, `.env`, and editor artifacts | Update when introducing new generated or secret-bearing paths. Ignore rules do not untrack files already committed. |
| `README.md` | This newcomer and architecture guide | Keep it synchronized with `OPERATIONS.md` and effective deployment paths. |
| `OPERATIONS.md` | Hosted discovery, docs publication, proxy validation, smoke tests, and secret-handling runbook | Read before any hosted change. Do not add secret values or the private console mount. |
| `ATTRIBUTION.md` | Third-party provenance and available license notices | The real filename is uppercase and has no leading dot; Linux paths are case-sensitive. There is no `.attribution.md`. |
| `pyproject.toml` | Python package metadata, Python compatibility, dependencies, package data, and installed `agent-monitor` entry point | Maintained source. |
| `uv.lock` | Reproducible Python dependency resolution for uv | Generated; do not hand-edit. `setup.sh` currently uses pip editable installation rather than enforcing this lock. |
| `package.json` | VitePress/Vue/font dependencies and docs commands | JSON cannot contain comments. Maintained through npm. |
| `package-lock.json` | Exact npm dependency graph used by `npm ci` | Generated; commit intentional dependency updates, never hand-edit. |
| `setup.sh` | Development bootstrap described above | Executable and material. Not a deployment script; dangerous to run casually on this host because `.venv` is shared with production. |
| `start.sh` | Foreground local console launcher using `.venv` | No TLS/proxy/systemd isolation. May collide with live port 4600 on this host. |
| `codex_account_login.sh` | Legacy/manual standalone Codex CLI device login under `data/codex_home/account` | The current multi-user console uses Settings-created per-user homes instead; this script's home is not selected automatically. Protect the resulting OAuth state. |
| `cluster_process_explore.py` | Standalone Fenwick-tree Monte Carlo experiment for a mass-biased cluster process | Research utility; nothing in the web service imports it. |
| `math-kg-tools-main.zip` | Unreferenced LLNL DARPA expMath source snapshot with LaTeX/Lean knowledge-graph tools | Not installed or imported by this project. The archive contains an Apache-2.0 license; see `ATTRIBUTION.md`. Do not unzip into runtime paths casually. |
| `console.log` | Currently empty and unreferenced local artifact | Not the live console log. Use `journalctl -u agent-monitor.service`. Candidate for cleanup/ignore. |
| `server.log` | Currently empty and unreferenced local artifact | Not an operational input or authoritative log. Candidate for cleanup/ignore. |
| `udo systemctl restart agent-monitor.service` | Stale ANSI-colored capture of `systemctl status`; the filename is missing the leading `s` | **Not a script or command.** Do not execute/follow it as deployment guidance. Candidate for cleanup after preserving any needed history. |

## Sensitive information and production safety

- Never commit `.env`, provider keys, SMTP credentials, OAuth tokens, reset
  links, session tokens, the private console mount, Cloudflare credentials, or
  systemd credential plaintext.
- Production server secrets are encrypted systemd credentials mounted read-only
  for the service. Local `.env` support is for development.
- `data/users.db*`, `data/codex_home/`, `data/claude_home/`, run workspaces, and
  user libraries may contain private or credential-adjacent material.
- Ignore rules reduce accidental additions but are not a security boundary.
  Current HEAD already contains the `data/calls.jsonl` placeholder and several
  historical workspace files. Now that the Git index is recovered, audit and
  untrack inappropriate runtime artifacts without deleting live data.
- Child-environment filtering limits accidental secret inheritance, but a local
  engine is not hostile-code isolation. It shares the service account and may
  reach files allowed to that account.
- Do not print credential files, include secret hashes in diagnostics, or pass
  secrets on command lines where they may appear in process listings/history.
- Do not hand-edit `/opt/.../releases/*`; immutability is part of rollback and
  forensic safety.
- Do not run paid engine, benchmark, or remote-sandbox smoke tests when a unit
  test/fake adapter is enough.

For hosted status, prefer bounded checks that do not print redirect destinations
or unit contents. The `/sign-in` destination is publicly observable, but avoid
pasting the rotating mount into documentation or tickets:

```bash
sudo systemctl show agent-monitor.service \
  -p FragmentPath -p DropInPaths -p WorkingDirectory -p ExecStart
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
curl -sS -o /dev/null -w 'homepage: %{http_code}\n' \
  https://54-82-94-57.sslip.io/
curl -sS -o /dev/null -w 'sign-in: %{http_code}\n' \
  https://54-82-94-57.sslip.io/sign-in
```

## Source-file documentation convention

Maintained Python modules should begin with a concise module docstring. Shell
and Node executables keep their shebang first and put the purpose comment
immediately after it. HTML keeps `<!DOCTYPE html>` first, followed by a purpose
comment. Vue/JavaScript/CSS files should identify their responsibility before
imports or rules.

Do not add fake comments to formats that do not support them (`package.json`,
JSON schemas, lockfiles), generated output, virtual environments, caches, or
vendored upstream files merely to satisfy this convention. Markdown pages and
skills are self-identifying through their front matter or first heading.

## Third-party provenance and licenses

See [ATTRIBUTION.md](ATTRIBUTION.md) for upstream projects and
`engines/hermes_core/LICENSE` for Hermes. Preserve nested licenses and notices
when updating vendored engines. The repository's package metadata currently
declares MIT; bundled projects retain their own licenses.

Monitor and legacy UCLA/IMProof code follow their original project licenses.
Vendored Hermes core is MIT (Nous Research) — see `engines/hermes_core/LICENSE`.
The Math Agent Harness graph design is adapted from Danus under Apache-2.0;
see `ATTRIBUTION.md`. The upstream privileged worker/runtime code is not bundled
into, imported by, or executed by this adapter.

### Local cleanup archive

`deleted/` holds unused upstream Meta-Harness examples and generated development
caches, preserving their original relative paths. It is ignored by Git. See
`deleted/manifest.json` for moved paths and the separate verified backup location.
User data and the published website were left in place.
