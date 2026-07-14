# IMProof engine

The original `Token_Tracking_Monitor` tree only ships **WorkflowRuns artifacts**
(events.jsonl + agents/) and the dashboard builder
(`monitor_core/harness_dashboard/improofbench_builder.py`) — not the upstream
IMProofBench orchestrator source.

## Expected artifact layout (for monitoring)

```
WorkflowRuns/<run-name>/
  events.jsonl
  run-metadata.json
  agents/
    Author-r1-<call_id>/
    Critic-r1-<call_id>/
    ...
```

Point `IMPROOF_ROOT` (or place runs under `engines/improof/WorkflowRuns/`) and run:

```bash
agent-monitor build --improof-dir engines/improof/WorkflowRuns
```

## Demo sample

`samples/improof_prob_001.json` is a pre-built dashboard run for UI smoke tests.

## Runner stub

`agent_monitor.runners.improof` launches only when an external IMProof entrypoint
is configured via `IMPROOF_ENTRY` (module:function or script path). Until then,
use UCLA or Hermes runners, and import IMProof artifacts for monitoring.
