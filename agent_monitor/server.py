"""Optional thin server re-export; prefer `agent-monitor serve`."""
from __future__ import annotations

from agent_monitor.cli import cmd_serve, main

__all__ = ["cmd_serve", "main"]
