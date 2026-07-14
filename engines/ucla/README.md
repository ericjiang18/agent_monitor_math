# UCLA harness engine

Vendored from `Token_Tracking_Monitor-main/harness_0518_Final` (source only).

## Contents

- `harness_0518_Final.py` — main orchestrator
- `literature_research.py`, `deep_read.py`, `finalize.py`, `run_parallel_harness.py`
- `verifier_v2/` — verification helpers
- `problems/` — local problem statements (also mirrored under `/problems/ucla`)

Logs and prior `output/` trees are **not** included.

## Run via Agent Monitor

```bash
# After configuring API keys / UCLA_ENTRY if needed:
agent-monitor run ucla problems/ucla/[First_Proof]Example_problem.txt
```

Default entry: `engines/ucla/harness_0518_Final.py` (override with `UCLA_ENTRY`).

To monitor existing UCLA artifact trees, point build at `_harness_runs`:

```bash
agent-monitor build --ucla-dir /path/to/UCLA/Logs/_harness_runs
```
