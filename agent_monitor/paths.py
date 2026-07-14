"""Paths and sys.path bootstrap for Agent Monitor."""
from __future__ import annotations

import sys
from pathlib import Path

from agent_monitor import ENGINES_DIR, MONITOR_CORE_DIR, ROOT


def ensure_import_paths() -> None:
    """Make monitor_core packages and hermes_core importable."""
    for path in (MONITOR_CORE_DIR, ENGINES_DIR / "hermes_core", ROOT):
        s = str(path)
        if s not in sys.path:
            sys.path.insert(0, s)


def ensure_data_dirs() -> None:
    from agent_monitor import CACHE_DIR, HERMES_HOME, LOGS_DIR, RUNS_DIR

    for d in (CACHE_DIR / "harness", RUNS_DIR, LOGS_DIR, HERMES_HOME):
        d.mkdir(parents=True, exist_ok=True)
