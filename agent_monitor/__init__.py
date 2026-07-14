"""Agent Monitor — unified informal math proving console."""

__version__ = "0.1.0"

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
RUNS_DIR = DATA_DIR / "runs"
LOGS_DIR = DATA_DIR / "logs"
HERMES_HOME = DATA_DIR / "hermes"
ENGINES_DIR = ROOT / "engines"
PROBLEMS_DIR = ROOT / "problems"
MONITOR_CORE_DIR = ROOT / "monitor_core"
