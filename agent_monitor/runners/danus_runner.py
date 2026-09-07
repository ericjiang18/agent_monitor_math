#!/usr/bin/env python3
"""Safe integration boundary for the official Danus Codex branch."""
from __future__ import annotations

import json
import os
from pathlib import Path


def emit_error(message: str) -> None:
    payload = {"type": "item.completed", "item": {"type": "error", "message": message}}
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    root = Path(
        os.environ.get("DANUS_ROOT")
        or Path(__file__).resolve().parents[2] / "engines" / "danus"
    )
    marker = root / "runtime" / ".danus-initialized"
    if not marker.is_file():
        emit_error(
            "Danus source is installed but not initialized. Its Codex branch "
            "requires an isolated host, operator choices, bootstrap, Codex "
            "login, and the verifier service."
        )
        return 78
    emit_error(
        "Danus is initialized, but automatic swarm launch remains disabled. "
        "Set DANUS_CMD only after approving its sandbox-bypass worker policy."
    )
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
