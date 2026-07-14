# Hermes agent harness core (vendored)

Slim subset of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT).

Includes the agent loop (`run_agent.AIAgent`), tools, providers, and runtime helpers.
Excludes gateway/messaging, TUI, desktop app, website, tests, and optional skill packs.

Use via `agent_monitor.runners.hermes` with `HERMES_HOME` set to `Agent_Monitor/data/hermes`.
