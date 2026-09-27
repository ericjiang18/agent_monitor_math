"""IMProof / ProofStack runner — uses vendored improofbench from batch-2."""
from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from agent_monitor import ENGINES_DIR, RUNS_DIR
from agent_monitor.paths import ensure_data_dirs


IMPROOF_ROOT = ENGINES_DIR / "improof"
DEFAULT_ENTRY = IMPROOF_ROOT / "scripts" / "run_workflow.py"
DEFAULT_WORKFLOW = os.environ.get("IMPROOF_WORKFLOW", "author_critic")

_ARXIV_ABS_RE = re.compile(
    r"^https://arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)$"
)


def _preferred_source_url(url: str) -> str:
    """Prefer arXiv's official full HTML while retaining a safe fallback."""
    match = _ARXIV_ABS_RE.fullmatch(url)
    return f"https://arxiv.org/html/{match.group(1)}" if match else url


def _bounded_capture(text: str, *, limit: int) -> str:
    """Preserve both theorem-bearing headers and tail diagnostics."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = max(1, (limit * 3) // 4)
    marker = "\n\n[... bounded capture omitted middle ...]\n\n"
    tail = max(1, limit - head - len(marker))
    return text[:head] + marker + text[-tail:]


def _research_packet(
    *,
    problem_text: str,
    workspace: Path,
    env: dict[str, str],
    model: str,
    on_start=None,
) -> str:
    """Build an auditable packet with a tool-free planner and typed tools.

    Codex's Linux code-mode sandbox cannot initialize its loopback namespace on
    this host. Rather than bypassing it, ask a read-only, tool-disabled turn for
    three structured queries, then execute only the materialized read-only
    literature/source wrappers on the host.
    """
    query_schema = {
        "type": "object",
        "properties": {
            "requires_research": {"type": "boolean"},
            "queries": {
                "type": "array",
                "minItems": 0,
                "maxItems": 3,
                "items": {"type": "string"},
            },
            "source_urls": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
            },
        },
        "required": ["requires_research", "queries", "source_urls"],
        "additionalProperties": False,
    }
    schema_path = workspace / "research-query-schema.json"
    output_path = workspace / "research-queries.json"
    schema_path.write_text(json.dumps(query_schema), encoding="utf-8")
    planner_prompt = (
        "Return only JSON matching the schema. Do not call tools. For this "
        "mathematics task, first decide whether current/historical literature, "
        "source entailment, or novelty is actually at issue. Set "
        "requires_research=false only for a purely self-contained task whose "
        "status does not depend on sources; then return zero queries and URLs. "
        "Otherwise set it true and propose exactly three separate bounded "
        "literature queries: (1) exact identifier or historical name; (2) the "
        "mathematical object plus conclusion or quantifier; (3) a standard "
        "field synonym or equivalent class plus conclusion. Each query must "
        "contain 2-6 discriminative terms and be at most 120 characters. "
        "Include up to four credential-free HTTPS source URLs explicit in or "
        "canonical from the exact problem identifier.\n\nTASK:\n" + problem_text
    )
    cmd = [
        shutil.which("codex") or "codex",
        "exec",
        "-c",
        'model_reasoning_effort="low"',
        "-m",
        model,
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "--disable",
        "code_mode_host",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-",
    ]
    planner_stdout = ""
    planner_error = ""
    planner_returncode = 1
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workspace),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        if on_start:
            on_start(proc)
        try:
            planner_stdout, planner_error = proc.communicate(
                planner_prompt, timeout=180
            )
            planner_returncode = int(getattr(proc, "returncode", 0) or 0)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            planner_stdout, planner_error = proc.communicate()
            planner_returncode = int(getattr(proc, "returncode", 1) or 1)
            planner_error = (planner_error + "\nquery planner timed out").strip()
    except Exception as exc:  # noqa: BLE001 - packet records degraded mode
        planner_error = f"{type(exc).__name__}: {exc}"

    plan: dict[str, Any] = {}
    if output_path.exists():
        try:
            parsed = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                plan = parsed
        except (OSError, json.JSONDecodeError):
            pass
    requires_research = plan.get("requires_research") is not False
    queries: list[str] = []
    for raw in plan.get("queries") or []:
        query = " ".join(str(raw).split())
        term_count = len(re.findall(r"[A-Za-z0-9]+", query))
        if 1 <= len(query) <= 120 and 2 <= term_count <= 8:
            queries.append(query)
    queries = queries[:3]
    if requires_research and len(queries) < 3:
        match = re.search(
            r"(?:Erdős|Erdos)\s+(?:problem\s*)?#?(\d+)", problem_text, re.I
        )
        identifier = (
            f"Erdos problem {match.group(1)}"
            if match
            else "mathematics problem exact statement"
        )
        for query in (
            identifier,
            "mathematical object exact conclusion",
            "equivalent formulation theorem",
        ):
            if query not in queries:
                queries.append(query)
            if len(queries) == 3:
                break
    if not requires_research:
        queries = []

    urls: list[str] = []
    for raw in plan.get("source_urls") or []:
        url = str(raw).strip()
        if url.startswith("https://") and len(url) <= 500:
            urls.append(url)
    if not requires_research:
        urls = []
    elif not urls:
        match = re.search(
            r"(?:Erdős|Erdos)\s+(?:problem\s*)?#?(\d+)", problem_text, re.I
        )
        if match:
            urls.append(f"https://www.erdosproblems.com/{match.group(1)}")

    usage = None
    for line in planner_stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage")

    sections = [
        "# Host-generated research packet",
        "Generated by a tool-disabled read-only query planner followed by the "
        "materialized allowlisted literature/source scripts. Treat negative "
        "results as bounded index evidence, not proof of absence.",
        "PLANNER_USAGE " + json.dumps(usage or {}, sort_keys=True),
        "RESEARCH_REQUIRED " + str(requires_research).lower(),
    ]
    if planner_error.strip() and (planner_returncode != 0 or not plan):
        sections.append(
            "PLANNER_DIAGNOSTIC " + " ".join(planner_error.split())[-1200:]
        )

    literature_tool = workspace / "_library" / "tools" / "literature-search.sh"
    for index, query in enumerate(queries, 1):
        sections.append(f"## Query {index}: {query}")
        if not literature_tool.is_file():
            sections.append("ERROR: materialized literature-search tool missing")
            continue
        try:
            result = subprocess.run(
                [
                    "bash",
                    str(literature_tool),
                    query,
                    "1900-01-01",
                    date.today().isoformat(),
                    "10",
                ],
                cwd=str(workspace),
                env=env,
                capture_output=True,
                text=True,
                timeout=40,
            )
            sections.append(
                _bounded_capture(result.stdout + "\n" + result.stderr, limit=18000)
            )
        except Exception as exc:  # noqa: BLE001
            sections.append(f"ERROR: {type(exc).__name__}: {exc}")

    source_tool = workspace / "_library" / "tools" / "primary-source-fetch.sh"
    for index, url in enumerate(urls[:4], 1):
        sections.append(f"## Source {index}: {url}")
        if not source_tool.is_file():
            sections.append("ERROR: materialized primary-source tool missing")
            continue
        preferred_url = _preferred_source_url(url)
        attempts = [preferred_url] if preferred_url == url else [preferred_url, url]
        diagnostics: list[str] = []
        for fetch_url in attempts:
            try:
                result = subprocess.run(
                    ["bash", str(source_tool), fetch_url],
                    cwd=str(workspace),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                output = result.stdout + "\n" + result.stderr
                if getattr(result, "returncode", 0) == 0:
                    if fetch_url != url:
                        sections.append(f"FULL_TEXT_FETCH {fetch_url}")
                    sections.append(_bounded_capture(output, limit=24000))
                    break
                diagnostics.append(
                    f"{fetch_url}: exit {getattr(result, 'returncode', 'unknown')} "
                    + " ".join(output.split())[-1000:]
                )
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(f"{fetch_url}: {type(exc).__name__}: {exc}")
        else:
            sections.append("ERROR: " + " | ".join(diagnostics))
        if diagnostics:
            sections.append("SOURCE_FETCH_DIAGNOSTIC " + " | ".join(diagnostics))

    packet = "\n\n".join(sections).strip()[:80000]
    (workspace / "research_packet.md").write_text(
        packet + "\n", encoding="utf-8"
    )
    return packet


def run_problem(
    problem_path: str | Path,
    *,
    problem_id: str | None = None,
    workflow: str | None = None,
    extra_args: list[str] | None = None,
    output_dir: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
    research_model: str | None = None,
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

    subscription = (extra_env or {}).get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"
    wf = workflow or ("codex_author_critic" if subscription else DEFAULT_WORKFLOW)
    out_dir = Path(output_dir) if output_dir else RUNS_DIR / "improof_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    native_workspace: Path | None = None
    native_args: list[str] = []
    if wf == "codex_author_critic":
        # ProofStack otherwise gives every author/critic a separate empty
        # temporary sandbox while the prompt advertises tools from the outer
        # console workspace. Create one run-owned native directory, copy only
        # the explicitly materialized agent/library context, and share it
        # across the sequential author/critic rounds.
        native_run_id = f"console-{uuid.uuid4().hex[:10]}"
        native_workspace = (out_dir / native_run_id).resolve()
        native_workspace.mkdir(parents=True, exist_ok=False)
        for context_name in ("_library", "_agent"):
            source = out_dir / context_name
            if source.is_dir():
                shutil.copytree(
                    source,
                    native_workspace / context_name,
                    dirs_exist_ok=True,
                )
        native_args = [
            "--run-id",
            native_run_id,
            "--input",
            f"workspace={native_workspace}",
        ]
    # Prefer the engine's own venv (full dependency set: loguru, modal, …).
    venv_python = IMPROOF_ROOT / ".venv" / "bin" / "python"
    python = os.environ.get("IMPROOF_PYTHON") or (
        str(venv_python) if venv_python.exists() else sys.executable
    )
    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env(extra=extra_env)
    if subscription:
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENAI_API_KEYS", None)
        env.pop("CODEX_API_KEY", None)
    # Vendored package is not pip-installed; expose src/ (proofstack, mathagents).
    src_dir = IMPROOF_ROOT / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(src_dir), env.get("PYTHONPATH")) if p
    )
    env["AGENT_MONITOR_ENGINE"] = "improof"
    env["AGENT_MONITOR_PROBLEM_ID"] = pid
    env["AGENT_MONITOR_RUNS_DIR"] = str(RUNS_DIR)
    if native_workspace is not None and research_model:
        packet = _research_packet(
            problem_text=problem_path.read_text(encoding="utf-8", errors="replace"),
            workspace=native_workspace,
            env=env,
            model=research_model,
            on_start=on_start,
        )
        native_args.extend(["--input", f"research_packet={packet}"])

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
        *native_args,
        *(extra_args or []),
    ]

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
        "workflow_run_dir": str(native_workspace) if native_workspace else None,
    }
