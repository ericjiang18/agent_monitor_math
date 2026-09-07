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
KIMI_WORKFLOW = "kimi_author_critic"
CLAUDE_WORKFLOW = "claude_author_critic"
_AUDIT_CONTRACT_FILES = {
    "global.json",
    "decomposed.json",
    "verifier-output.json",
    "merge-map.json",
    "run.json",
}
_AUDIT_PROMOTED_FILES = _AUDIT_CONTRACT_FILES | {
    "final-candidate.md",
    "global-rerun.json",
    "decomposed-rerun.json",
    "refuter-rerun.json",
    "boundary-rerun.json",
    "citation-rerun.json",
}
_AUDIT_GATE_MARKERS = ("$research-proof-audit", "CANDIDATE AUDIT GATE:")

_ARXIV_ABS_RE = re.compile(
    r"^https://arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)$"
)


def _is_kimi_model_name(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    for prefix in ("openai:", "openai/", "kimi:", "kimi/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized == "kimi-k3" or normalized.endswith("/kimi/k3")


def _kimi_model_selected(
    extra_args: list[str] | None,
    extra_env: dict[str, str] | None,
) -> bool:
    for key in (
        "AGENT_MONITOR_SELECTED_MODEL",
        "AGENT_MONITOR_OPENAI_MODEL",
    ):
        if _is_kimi_model_name((extra_env or {}).get(key)):
            return True
    args = list(extra_args or [])
    for index, value in enumerate(args):
        candidate = (
            args[index + 1]
            if value == "--model" and index + 1 < len(args)
            else value
        )
        if "=" in candidate:
            candidate = candidate.rsplit("=", 1)[-1]
        if (
            _is_kimi_model_name(candidate)
            or candidate.strip().lower() == "models/kimi/k3"
        ):
            return True
    return False


def _substantive_proof_artifact(path: Path) -> bool:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size < 200:
            return False
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    if len(text) < 200 or sum(character.isalpha() for character in text) < 80:
        return False
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "\\begin{document}",
            "proof",
            "suppose",
            "assume",
            "therefore",
            "hence",
            "we show",
        )
    )


def _kimi_terminal_error(native_workspace: Path, problem_id: str) -> str | None:
    metadata_path = native_workspace / "run-metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"missing or invalid IMProof terminal metadata: {type(exc).__name__}"
    if not isinstance(metadata, dict) or metadata.get("status") != "ok":
        detail = metadata.get("error") if isinstance(metadata, dict) else None
        return f"IMProof internal terminal status is not ok: {detail or metadata!r}"
    outputs = metadata.get("outputs")
    if not isinstance(outputs, dict):
        return "IMProof terminal metadata has no outputs mapping"
    if outputs.get("error"):
        return f"IMProof workflow output error: {outputs['error']}"
    if outputs.get("compiled") is not True:
        return "IMProof Kimi workflow did not compile its final LaTeX artifact"
    candidate = native_workspace / "solutions" / f"{problem_id}.tex"
    if not _substantive_proof_artifact(candidate):
        return "IMProof Kimi workflow produced no substantive final proof artifact"
    return None


def _claude_terminal_error(native_workspace: Path, problem_id: str) -> str | None:
    metadata_path = native_workspace / "run-metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"missing or invalid IMProof Claude terminal metadata: {type(exc).__name__}"
    if not isinstance(metadata, dict) or metadata.get("status") != "ok":
        detail = metadata.get("error") if isinstance(metadata, dict) else None
        return f"IMProof Claude terminal status is not ok: {detail or metadata!r}"
    outputs = metadata.get("outputs")
    if not isinstance(outputs, dict):
        return "IMProof Claude terminal metadata has no outputs mapping"
    if outputs.get("error"):
        return f"IMProof Claude workflow output error: {outputs['error']}"
    candidate = native_workspace / "solutions" / f"{problem_id}.tex"
    if not _substantive_proof_artifact(candidate):
        return "IMProof Claude workflow produced no substantive final proof artifact"
    return None


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


def _promote_audit_contracts(native_workspace: Path, output_dir: Path) -> list[str]:
    """Copy only bounded regular audit-contract files out of the isolated run."""
    source_root = native_workspace / "audit"
    if not source_root.is_dir() or source_root.is_symlink():
        return []
    destination_root = output_dir / "audit"
    if destination_root.is_symlink():
        return []
    destination_root.mkdir(parents=True, exist_ok=True)
    if not destination_root.resolve().is_relative_to(output_dir.resolve()):
        return []
    promoted: list[str] = []
    for source_run in sorted(source_root.iterdir()):
        if not source_run.is_dir() or source_run.is_symlink():
            continue
        destination_run = destination_root / source_run.name
        if destination_run.exists() and (
            destination_run.is_symlink() or not destination_run.is_dir()
        ):
            continue
        destination_run.mkdir(parents=True, exist_ok=True)
        if not destination_run.resolve().is_relative_to(destination_root.resolve()):
            continue
        for name in sorted(_AUDIT_PROMOTED_FILES):
            source = source_run / name
            if (
                not source.is_file()
                or source.is_symlink()
                or source.stat().st_size > 5_000_000
            ):
                continue
            destination = destination_run / name
            if destination.exists() and (
                destination.is_symlink() or not destination.is_file()
            ):
                continue
            shutil.copy2(source, destination)
            promoted.append(str(destination.relative_to(output_dir)))
    return promoted


def _audit_contracts_complete(output_dir: Path) -> bool:
    """Require parseable core files and an affirmative trusted manifest."""
    audit_root = output_dir / "audit"
    if not audit_root.is_dir() or audit_root.is_symlink():
        return False
    for directory in audit_root.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        documents: dict[str, Any] = {}
        for name in _AUDIT_CONTRACT_FILES:
            path = directory / name
            if not path.is_file() or path.is_symlink():
                break
            try:
                documents[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                break
        else:
            manifest = documents.get("run.json")
            if isinstance(manifest, dict) and manifest.get("complete_core") is True:
                from agent_monitor.research_audit import reconciliation_complete

                if not reconciliation_complete(output_dir, directory):
                    continue
                return True
    return False


def _publish_native_candidate(
    native_workspace: Path, output_dir: Path, problem_id: str
) -> Path | None:
    """Publish only IMProof's final regular TeX solution at the console root."""
    candidates = (
        native_workspace / "solutions" / f"{problem_id}.tex",
        native_workspace / "answer.tex",
    )
    native_root = native_workspace.resolve()
    for source in candidates:
        try:
            resolved = source.resolve()
            if (
                not source.is_file()
                or source.is_symlink()
                or not resolved.is_relative_to(native_root)
                or not 40 <= source.stat().st_size <= 10_000_000
            ):
                continue
            target = output_dir / "proof.tex"
            if target.is_symlink():
                return None
            shutil.copy2(source, target)
            return target
        except OSError:
            continue
    return None


def _audit_explicitly_requested(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(marker.lower() in lowered for marker in _AUDIT_GATE_MARKERS)


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
    from agent_monitor.subprocess_env import child_process_env

    tool_env = child_process_env(source=env)
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
    if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "agent_monitor.runners.improof_claude_worker",
            "--role",
            "planner",
            "--output",
            output_path.name,
            "--model",
            model,
        ]
    else:
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
                env=tool_env,
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
                    env=tool_env,
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

    codex_subscription = (
        (extra_env or {}).get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"
    )
    claude_subscription = (
        (extra_env or {}).get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1"
    )
    kimi_api = not (codex_subscription or claude_subscription) and _kimi_model_selected(
        extra_args, extra_env
    )
    if workflow:
        wf = workflow
    elif claude_subscription:
        wf = CLAUDE_WORKFLOW
    elif codex_subscription:
        wf = "codex_author_critic"
    else:
        wf = DEFAULT_WORKFLOW
    if kimi_api and wf in {"author_critic", "author_critic_long"}:
        wf = KIMI_WORKFLOW
    out_dir = Path(output_dir) if output_dir else RUNS_DIR / "improof_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    native_workspace: Path | None = None
    native_args: list[str] = []
    if wf in {"codex_author_critic", CLAUDE_WORKFLOW, KIMI_WORKFLOW}:
        # Give the selected workflow one run-owned native directory. Codex
        # rounds share it; Kimi writes its terminal metadata and stashed proof
        # there so the wrapper can validate them before publication.
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
        # Human-in-the-loop continuations may operate on an existing notebook
        # larger than the bounded prompt excerpt. Give the isolated native
        # workflow an exact full copy while keeping writes inside its run dir.
        for artifact_name in ("proof.md", "proof.tex"):
            source = out_dir / artifact_name
            if source.is_file():
                shutil.copy2(source, native_workspace / artifact_name)
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
    if codex_subscription or claude_subscription:
        # ``os.environ.copy()`` can reintroduce server-level provider keys even
        # after the request route was scrubbed. Close that last child-process
        # boundary for both linked-account workflows.
        from agent_monitor.jobs import _scrub_provider_route_env

        _scrub_provider_route_env(env)
    if claude_subscription:
        from agent_monitor import claude_login

        config_raw = str(env.get("CLAUDE_CONFIG_DIR") or "").strip()
        if not config_raw:
            raise ValueError(
                "CLAUDE_CONFIG_DIR is required for IMProof Claude subscription mode"
            )
        env = claude_login.account_environment(Path(config_raw), env)
        env["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"] = "1"
    # Vendored packages and the trusted response worker are not pip-installed.
    src_dir = IMPROOF_ROOT / "src"
    repo_root = ENGINES_DIR.parent
    env["PYTHONPATH"] = os.pathsep.join(
        p
        for p in (str(src_dir), str(repo_root), env.get("PYTHONPATH"))
        if p
    )
    env["AGENT_MONITOR_ENGINE"] = "improof"
    env["AGENT_MONITOR_PROBLEM_ID"] = pid
    env["AGENT_MONITOR_RUNS_DIR"] = str(RUNS_DIR)
    if native_workspace is not None and research_model and not kimi_api:
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
    terminal_error = None
    if rc == 0 and native_workspace is not None:
        if kimi_api:
            terminal_error = _kimi_terminal_error(native_workspace, pid)
        elif claude_subscription:
            terminal_error = _claude_terminal_error(native_workspace, pid)
    if terminal_error:
        output = (output + "\n" + terminal_error).strip()
        rc = 1
    problem_text = problem_path.read_text(encoding="utf-8", errors="replace")
    published_candidate = (
        _publish_native_candidate(native_workspace, out_dir, pid)
        if rc == 0 and native_workspace is not None
        else None
    )
    promoted_audit_files = (
        _promote_audit_contracts(native_workspace, out_dir)
        if native_workspace is not None
        else []
    )
    supplemental_audit_status = "not_requested"
    supplemental_audit_returncode: int | None = None
    if (
        rc == 0
        and published_candidate is not None
        and _audit_explicitly_requested(problem_text)
        and not _audit_contracts_complete(out_dir)
    ):
        selected_model = (
            research_model
            or env.get("AGENT_MONITOR_SELECTED_MODEL")
            or env.get("AGENT_MONITOR_CLAUDE_MODEL")
            or env.get("AGENT_MONITOR_CODEX_MODEL")
            or env.get("AGENT_MONITOR_OPENAI_MODEL")
        )
        if selected_model:
            supplemental_audit_status = "running"
            prompt_copy = out_dir / f".improof-audit-prompt-{uuid.uuid4().hex[:10]}.txt"
            prompt_copy.write_text(problem_text, encoding="utf-8")
            adapter_env = dict(env)
            repo_root = str(ENGINES_DIR.parent)
            adapter_env["PYTHONPATH"] = os.pathsep.join(
                p
                for p in (
                    repo_root,
                    adapter_env.get("PYTHONPATH"),
                )
                if p
            )
            adapter_command = [
                sys.executable,
                "-u",
                "-m",
                "agent_monitor.runners.improof_audit_adapter",
                "--prompt",
                prompt_copy.name,
                "--candidate",
                published_candidate.name,
                "--model",
                selected_model,
            ]
            try:
                audit_output, supplemental_audit_returncode, _ = stream_subprocess(
                    adapter_command,
                    cwd=str(out_dir),
                    env=adapter_env,
                    on_start=on_start,
                    on_output=on_output,
                )
                output = (output + "\n" + audit_output).strip()
                supplemental_audit_status = (
                    "complete"
                    if supplemental_audit_returncode == 0
                    and _audit_contracts_complete(out_dir)
                    else "degraded"
                )
            finally:
                prompt_copy.unlink(missing_ok=True)
            promoted_audit_files = [
                str(path.relative_to(out_dir))
                for path in sorted((out_dir / "audit").glob("*/*"))
                if path.is_file() and not path.is_symlink()
            ]
        else:
            supplemental_audit_status = "degraded_no_selected_model"
    elif _audit_explicitly_requested(problem_text):
        supplemental_audit_status = (
            "native_complete" if _audit_contracts_complete(out_dir) else "degraded"
        )
    result = {
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
        "promoted_audit_files": promoted_audit_files,
        "supplemental_audit_status": supplemental_audit_status,
        "supplemental_audit_returncode": supplemental_audit_returncode,
    }
    if terminal_error:
        result["error"] = terminal_error
    return result
