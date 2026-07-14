"""Build unified dashboard runs from Hermes session exports / runner outputs."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_monitor import RUNS_DIR
from agent_monitor.schema import normalize_run

HERMES_PIPELINE = [
    {"id": "understand", "label": "Understand", "title": "Understand"},
    {"id": "plan", "label": "Plan", "title": "Plan"},
    {"id": "draft", "label": "Draft", "title": "Draft proof"},
    {"id": "verify", "label": "Verify", "title": "Verify"},
    {"id": "finalize", "label": "Finalize", "title": "Finalize"},
]


def discover_hermes_runs(root: Path | None = None) -> list[tuple[str, Path]]:
    root = root or RUNS_DIR
    if not root.exists():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(root.glob("hermes_*.json")):
        out.append((path.stem, path))
    return out


def build_run_from_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    run = normalize_run(data, engine="hermes")
    run.setdefault("pipeline", HERMES_PIPELINE)
    run.setdefault("trace_name", f"[Hermes] {run.get('problem_id') or path.stem}")
    return run


def build_all(cache_dir: Path, runs_dir: Path | None = None) -> list[dict[str, Any]]:
    """Write Hermes runs into dashboard cache and return manifest entries."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for run_id, path in discover_hermes_runs(runs_dir):
        run = build_run_from_file(path)
        (cache_dir / f"{run_id}.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        totals = run.get("totals") or {}
        entries.append(
            {
                "run_id": run_id,
                "engine": "hermes",
                "trace_name": run.get("trace_name"),
                "source": "hermes",
                "agent_count": len(run.get("agents") or []),
                "total_cost_usd": totals.get("cost_usd"),
                "built_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return entries
