"""IMProof / ProofStack runner — uses vendored improofbench from batch-2."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from agent_monitor import ENGINES_DIR, RUNS_DIR
from agent_monitor.paths import ensure_data_dirs


IMPROOF_ROOT = ENGINES_DIR / "improof"
DEFAULT_ENTRY = IMPROOF_ROOT / "scripts" / "run_workflow.py"
DEFAULT_WORKFLOW = os.environ.get("IMPROOF_WORKFLOW", "author_critic")


def run_problem(
    problem_path: str | Path,
    *,
    problem_id: str | None = None,
    workflow: str | None = None,
    extra_args: list[str] | None = None,
    output_dir: str | Path | None = None,
    on_start=None,
    on_output=None,
) -> dict[str, Any]:
    """Launch vendored IMProofBench ``scripts/run_workflow.py``."""
    ensure_data_dirs()
    problem_path = Path(problem_path).resolve()
    if not problem_path.exists():
        raise FileNotFoundError(problem_path)

    pid = problem_id or problem_path.stem
    entry = Path(os.environ.get("IMPROOF_ENTRY") or DEFAULT_ENTRY)
    if not entry.exists():
        return {
            "engine": "improof",
            "status": "not_configured",
            "problem_id": pid,
            "error": f"IMProof entry not found: {entry}",
            "hint": "Expected engines/improof/scripts/run_workflow.py from batch-2 improofbench.",
            "workflow_runs": str(IMPROOF_ROOT / "WorkflowRuns"),
        }

    wf = workflow or DEFAULT_WORKFLOW
    out_dir = Path(output_dir) if output_dir else RUNS_DIR / "improof_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Prefer the engine's own venv (full dependency set: loguru, modal, …).
    venv_python = IMPROOF_ROOT / ".venv" / "bin" / "python"
    python = os.environ.get("IMPROOF_PYTHON") or (
        str(venv_python) if venv_python.exists() else sys.executable
    )
    cmd = [
        python,
        "-u",
        str(entry),
        "--workflow",
        wf,
        "--problem",
        str(problem_path),
        "--problem-id",
        pid,
        "--output",
        str(out_dir),
        *(extra_args or []),
    ]
    env = os.environ.copy()
    # Vendored package is not pip-installed; expose src/ (proofstack, mathagents).
    src_dir = IMPROOF_ROOT / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(src_dir), env.get("PYTHONPATH")) if p
    )
    env["AGENT_MONITOR_ENGINE"] = "improof"
    env["AGENT_MONITOR_PROBLEM_ID"] = pid
    env["AGENT_MONITOR_RUNS_DIR"] = str(RUNS_DIR)
    # Prefer Agent_Monitor .env if present
    root_env = ENGINES_DIR.parent / ".env"
    if root_env.exists() and "DOTENV_PATH" not in env:
        env["DOTENV_PATH"] = str(root_env)

    from agent_monitor.runners._stream import stream_subprocess

    output, rc, _timed_out = stream_subprocess(
        cmd,
        cwd=str(IMPROOF_ROOT),
        env=env,
        on_start=on_start,
        on_output=on_output,
    )
    return {
        "engine": "improof",
        "status": "finished" if rc == 0 else "failed",
        "problem_id": pid,
        "workflow": wf,
        "returncode": rc,
        "stdout_tail": output[-4000:],
        "stderr_tail": "",
        "command": cmd,
        "output_dir": str(out_dir),
    }
