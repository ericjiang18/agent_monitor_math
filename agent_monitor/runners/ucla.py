"""UCLA harness runner stub — invokes engines/ucla entrypoints when configured."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from agent_monitor import ENGINES_DIR, RUNS_DIR
from agent_monitor.paths import ensure_data_dirs


UCLA_ROOT = ENGINES_DIR / "ucla"


def run_problem(
    problem_path: str | Path,
    *,
    problem_id: str | None = None,
    extra_args: list[str] | None = None,
    output_dir: str | Path | None = None,
    on_start=None,
    on_output=None,
) -> dict[str, Any]:
    """Launch UCLA harness for a problem file.

    Prefers `UCLA_ENTRY` env (script path). Default: engines/ucla/harness_0518_Final.py
    if that script accepts CLI args; otherwise returns a not-configured status.
    """
    ensure_data_dirs()
    problem_path = Path(problem_path)
    if not problem_path.exists():
        raise FileNotFoundError(problem_path)

    entry = os.environ.get("UCLA_ENTRY")
    script = Path(entry) if entry else UCLA_ROOT / "harness_0518_Final.py"
    if not script.exists():
        return {
            "engine": "ucla",
            "status": "not_configured",
            "error": f"UCLA entry not found: {script}",
            "hint": "Set UCLA_ENTRY to your harness launcher script.",
        }

    pid = problem_id or problem_path.stem
    cmd = [sys.executable, "-u", str(script), str(problem_path), *(extra_args or [])]
    # Many UCLA harnesses are module-driven; try a conservative invocation and
    # capture output for the operator to inspect.
    env = os.environ.copy()
    # The harness resolves its problem via PROBLEM_FILE (it does not read argv)
    # and writes artifacts (solution.tex etc.) under OUTPUT_ROOT_DIR.
    env["PROBLEM_FILE"] = str(problem_path.resolve())
    if output_dir:
        env.setdefault("OUTPUT_ROOT_DIR", str(Path(output_dir).resolve()))
    env["AGENT_MONITOR_ENGINE"] = "ucla"
    env["AGENT_MONITOR_PROBLEM_ID"] = pid
    env["AGENT_MONITOR_RUNS_DIR"] = str(RUNS_DIR)
    from agent_monitor.runners._stream import stream_subprocess

    output, rc, timed_out = stream_subprocess(
        cmd,
        cwd=str(UCLA_ROOT),
        env=env,
        timeout=int(os.environ.get("UCLA_RUN_TIMEOUT", "86400")),
        on_start=on_start,
        on_output=on_output,
    )
    if timed_out:
        return {"engine": "ucla", "status": "timeout", "error": "run timed out", "problem_id": pid}

    return {
        "engine": "ucla",
        "status": "finished" if rc == 0 else "failed",
        "problem_id": pid,
        "returncode": rc,
        "stdout_tail": output[-4000:],
        "stderr_tail": "",
        "command": cmd,
    }
