#!/usr/bin/env python3
"""Run a bounded, resumable ProvingConsole evaluation through the live HTTP API.

The supervisor never reads or stores a password or provider key. It creates a
temporary local session for an existing user id, submits jobs through the same API
as the browser, records evidence digests, and destroys the session on exit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TERMINAL_JOB_STATUSES = {"finished", "failed", "stopped"}
TERMINAL_SIDECAR_STATUSES = {
    "compiled",
    "done",
    "failed",
    "none",
    "partial",
    "parsed",
    "ready",
    "verified",
}
OUTCOME_RE = re.compile(
    r"^(?:final\s+)?(?:provingconsole\s+)?outcome\s*:\s*"
    r"(Solved|Counterexample|Known/Open Status|Partial Progress)\s*[.!]?$",
    re.IGNORECASE,
)
OUTCOME_HEADING_RE = re.compile(
    r"^(?:provingconsole\s+)?outcome\s*:?\s*$",
    re.IGNORECASE,
)
BARE_OUTCOME_RE = re.compile(
    r"^(Solved|Counterexample|Known/Open Status|Partial Progress)\s*[.!]?$",
    re.IGNORECASE,
)
AUDIT_HINT_RE = re.compile(
    r"research-proof-audit|global pass|decomposed pass|refuter pass|claim ledger",
    re.IGNORECASE,
)
DEFAULT_ENGINES = (
    "codex",
    "deepseek_harness",
    "deepagents",
    "hermes",
    "improof",
    "metaharness",
    "openclaude",
    "openhands",
    "plain",
    "openclaw",
)

STOP_REQUESTED = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_outcomes(content: str) -> list[str]:
    """Read only explicit outcome lines, never incidental or negated mentions."""
    found: list[str] = []
    awaiting_heading_value = False
    for raw_line in content.splitlines():
        line = raw_line.strip().strip("#*_` ")
        # IMProof emits the contract inside presentation-only TeX wrappers,
        # e.g. \boxed{\text{\bfseries ProvingConsole outcome: ...}}.
        line = re.sub(
            r"\\(?:boxed|text|textbf|mathbf|bfseries)\b\s*",
            "",
            line,
        ).strip("{}[]$ ")
        if not line:
            continue
        match = OUTCOME_RE.match(line)
        if match is None and awaiting_heading_value:
            match = BARE_OUTCOME_RE.match(line)
        awaiting_heading_value = False
        if match is None and OUTCOME_HEADING_RE.match(line):
            awaiting_heading_value = True
            continue
        if match:
            label = match.group(1)
            canonical = next(
                item
                for item in ("Solved", "Counterexample", "Known/Open Status", "Partial Progress")
                if item.lower() == label.lower()
            )
            if canonical not in found:
                found.append(canonical)
    return found


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def persisted_terminal_run(data_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Recover a terminal run after the HTTP job registry was restarted.

    The console keeps run records on disk but job handles are process-local. This
    helper is deliberately fail-closed: it accepts only a simple run id, a regular
    non-symlink JSON file beneath data/runs, and an explicit terminal status.
    """
    if not run_id or run_id in {".", ".."} or Path(run_id).name != run_id:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", run_id):
        return None
    run_path = data_dir / "runs" / f"{run_id}.json"
    try:
        if run_path.is_symlink() or not run_path.is_file():
            return None
        payload = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status") or "")
    if status not in TERMINAL_JOB_STATUSES:
        return None
    return {
        "status": status,
        "error": payload.get("error"),
        "updated_at": payload.get("updated_at"),
    }


class LiveClient:
    def __init__(self, *, origin: str, base_path: str, cookie_name: str, token: str):
        self.origin = origin.rstrip("/")
        self.base_path = "/" + base_path.strip("/") if base_path.strip("/") else ""
        self.cookie = f"{cookie_name}={token}"

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        app_path = "/" + path.lstrip("/")
        url = f"{self.origin}{self.base_path}{app_path}"
        payload = None
        headers = {"Accept": "application/json", "Cookie": self.cookie}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw).get("error") or raw
            except json.JSONDecodeError:
                detail = raw
            raise RuntimeError(f"{method} {app_path} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {app_path} failed: {exc.reason}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{method} {app_path} returned non-JSON data") from exc

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("POST", path, body or {})


def load_targets(path: Path) -> tuple[list[dict[str, str]], dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("target file must contain a nonempty targets array")
    seen: set[str] = set()
    clean: list[dict[str, str]] = []
    for item in targets:
        target_id = str(item.get("id") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not target_id or target_id in seen or not prompt:
            raise ValueError("every target needs a unique id and nonempty prompt")
        seen.add(target_id)
        clean.append({"id": target_id, "title": str(item.get("title") or target_id), "prompt": prompt})
    suffixes = {
        "audit": str(data.get("audit_suffix") or "").strip(),
        "baseline": str(data.get("baseline_suffix") or "").strip(),
    }
    if not all(suffixes.values()):
        raise ValueError("target file must define audit_suffix and baseline_suffix")
    return clean, suffixes


def append_audit_suffix(
    suffixes: dict[str, str], append_text: str | None
) -> dict[str, str]:
    """Return condition suffixes with an optional audit-only prompt delta."""
    result = dict(suffixes)
    delta = str(append_text or "").strip()
    if delta:
        result["audit"] = result["audit"].rstrip() + "\n\n" + delta
    return result


def make_schedule(
    engines: list[str],
    targets: list[dict[str, str]],
    *,
    seed: int,
    first_mode: str = "audit",
) -> list[dict[str, str]]:
    """Prioritize one audit per harness, then matched audit/control pairs."""
    if not engines:
        return []
    if first_mode not in {"audit", "baseline"}:
        raise ValueError("first_mode must be 'audit' or 'baseline'")
    first_pass = [
        {
            "engine": engine,
            "target": targets[index % len(targets)]["id"],
            "mode": first_mode,
        }
        for index, engine in enumerate(engines)
    ]
    paired = [
        {"engine": engine, "target": target["id"], "mode": mode}
        for target in targets
        for engine in engines
        for mode in ("audit", "baseline")
    ]
    random.Random(seed).shuffle(paired)
    return first_pass + paired


def sidecar_snapshot(client: LiveClient, run_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    endpoints = {
        "lean": f"/api/run/{run_id}/lean_verify",
        "dag_informal": f"/api/run/{run_id}/proof_graph?kind=informal",
        "dag_formal": f"/api/run/{run_id}/proof_graph?kind=formal",
    }
    for key, endpoint in endpoints.items():
        try:
            data = client.get(endpoint)
            result[key] = {
                "status": data.get("status") or "none",
                "error": data.get("error") or data.get("notes"),
            }
        except RuntimeError as exc:
            result[key] = {"status": "unavailable", "error": str(exc)}
    try:
        quoted = urllib.parse.quote("auto_pipeline.json", safe="")
        payload = client.get(f"/api/workspace/{run_id}/file?path={quoted}")
        pipeline = json.loads(str(payload.get("content") or "{}"))
        result["auto_pipeline"] = {
            "status": pipeline.get("status") or "none",
            "stages": {
                str(name): str((stage or {}).get("status") or "none")
                for name, stage in (pipeline.get("stages") or {}).items()
            },
        }
    except (RuntimeError, json.JSONDecodeError) as exc:
        result["auto_pipeline"] = {"status": "unavailable", "error": str(exc)}
    return result


def sidecars_settled(snapshot: dict[str, Any]) -> bool:
    pipeline = snapshot.get("auto_pipeline") or {}
    pipeline_status = str(pipeline.get("status") or "").lower()
    if pipeline_status not in {"", "none", "unavailable"}:
        return pipeline_status in {"done", "failed", "partial"}
    for value in snapshot.values():
        status = str(value.get("status") or "none").lower()
        if status not in TERMINAL_SIDECAR_STATUSES:
            return False
    return True


def audit_pass_coverage(paths: list[str]) -> dict[str, Any]:
    """Classify file-backed audit coverage by semantic pass, not file count."""
    detected: dict[str, list[str]] = {
        role: []
        for role in ("global", "decomposed", "boundary", "citation", "refuter", "merged")
    }
    for path in paths:
        if not path.startswith("audit/") or not path.lower().endswith(".json"):
            continue
        name = Path(path).name.lower().replace("_", "-")
        stem = name.removesuffix(".json")
        role: str | None = None
        if stem in {
            "decomposed",
            "decomposed-pass",
            "decomposed-audit",
            "decomposed-rerun",
        }:
            role = "decomposed"
        elif stem in {"global", "global-pass", "global-audit", "global-rerun"}:
            role = "global"
        elif stem in {
            "boundary",
            "boundary-pass",
            "boundary-audit",
            "boundary-clause-sweep",
            "boundary-rerun",
        }:
            role = "boundary"
        elif stem in {
            "citation",
            "citation-pass",
            "citation-audit",
            "citation-rerun",
        }:
            role = "citation"
        elif stem in {
            "refuter",
            "refuter-pass",
            "refuter-audit",
            "refuter-rerun",
        } or stem.endswith("-refuter"):
            role = "refuter"
        elif name in {"verifier-output.json", "merged.json", "merge.json"}:
            role = "merged"
        if role:
            detected[role].append(path)
    passes = sorted(role for role, files in detected.items() if files)
    return {
        "passes": passes,
        "files": {role: files for role, files in detected.items() if files},
        "core_complete": all(role in passes for role in ("global", "decomposed", "merged")),
    }


def collect_artifact_evidence(client: LiveClient, run_id: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "proof_md": None,
        "proof_artifact": None,
        "audit_files": [],
        "skill_package_materialized": False,
        "outcomes": [],
        "audit_language_present": False,
        "audit_pass_coverage": {"passes": [], "files": {}, "core_complete": False},
        "audit_compliance": "not_executed",
    }
    try:
        files = client.get(f"/api/workspace/{run_id}/files").get("files") or []
    except RuntimeError as exc:
        evidence["workspace_error"] = str(exc)
        return evidence
    paths = [str(item.get("path") if isinstance(item, dict) else item) for item in files]
    evidence["audit_files"] = sorted(path for path in paths if path.startswith("audit/"))[:200]
    evidence["audit_pass_coverage"] = audit_pass_coverage(paths)
    evidence["skill_package_materialized"] = (
        "_agent/skills/research-proof-audit/SKILL.md" in paths
    )
    proof_path = next(
        (candidate for candidate in ("proof.md", "proof.tex") if candidate in paths),
        None,
    )
    if proof_path is None:
        return evidence
    try:
        quoted = urllib.parse.quote(proof_path, safe="")
        payload = client.get(f"/api/workspace/{run_id}/file?path={quoted}")
        content = str(payload.get("content") or "")
    except RuntimeError as exc:
        evidence["proof_error"] = str(exc)
        return evidence
    artifact = {
        "path": proof_path,
        "bytes": len(content.encode("utf-8")),
        "sha256": sha256_text(content),
    }
    evidence["proof_artifact"] = artifact
    if proof_path == "proof.md":
        evidence["proof_md"] = artifact
    evidence["outcomes"] = extract_outcomes(content)
    evidence["audit_language_present"] = bool(AUDIT_HINT_RE.search(content))
    lowered = content.lower()
    if evidence["audit_pass_coverage"]["core_complete"]:
        evidence["audit_compliance"] = "complete_core"
    elif "degraded audit:" in lowered:
        evidence["audit_compliance"] = "degraded_disclosed"
    elif evidence["audit_files"] or evidence["audit_language_present"]:
        evidence["audit_compliance"] = "incomplete"
    return evidence


def write_summary(path: Path, state: dict[str, Any]) -> None:
    runs = state.get("runs") or []
    terminal = [run for run in runs if run.get("status") in TERMINAL_JOB_STATUSES]
    status_counts = Counter(str(run.get("status") or "unknown") for run in runs)
    engine_counts = Counter(str(run.get("engine") or "unknown") for run in terminal)
    mode_counts = Counter(str(run.get("mode") or "unknown") for run in terminal)
    outcome_counts: Counter[str] = Counter()
    for run in terminal:
        for outcome in (run.get("evidence") or {}).get("outcomes") or []:
            outcome_counts[outcome] += 1
    lines = [
        "# Research proof audit evaluation",
        "",
        f"- Started: {state.get('started_at')}",
        f"- Deadline: {state.get('deadline_at')}",
        f"- Last update: {state.get('updated_at')}",
        f"- Runs submitted: {len(runs)}",
        f"- Terminal runs: {len(terminal)}",
        f"- Active runs: {sum(1 for run in runs if run.get('status') not in TERMINAL_JOB_STATUSES)}",
        f"- Job statuses: {dict(status_counts)}",
        f"- Engines completed: {dict(engine_counts)}",
        f"- Conditions completed: {dict(mode_counts)}",
        f"- Outcome labels observed: {dict(outcome_counts)}",
        "",
        "This file is operational telemetry, not a claim that any open problem was solved.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_signal_handlers() -> None:
    def handler(_signum: int, _frame: Any) -> None:
        global STOP_REQUESTED
        STOP_REQUESTED = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-hours", type=float, default=24.0)
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--poll-seconds", type=float, default=20.0)
    parser.add_argument("--max-run-hours", type=float, default=3.0)
    parser.add_argument("--sidecar-min-wait-seconds", type=float, default=30.0)
    parser.add_argument("--sidecar-settle-minutes", type=float, default=20.0)
    # Research-status checks plus an independent candidate audit routinely need
    # more than 40 tool turns. Keep enough headroom for reconciliation instead
    # of reporting a mathematically complete artifact as an engine failure.
    parser.add_argument("--max-iterations", type=int, default=100)
    parser.add_argument("--max-runs", type=int, default=0, help="0 means no explicit cap")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--origin", default="http://127.0.0.1:4600")
    parser.add_argument("--base-path", required=True)
    parser.add_argument("--server-root", default="/home/repo/agent_monitor_math")
    parser.add_argument("--data-dir", default="/home/repo/agent_monitor_math/data")
    parser.add_argument(
        "--targets",
        type=Path,
        default=Path(__file__).with_name("research_audit_targets.json"),
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--engines", default=",".join(DEFAULT_ENGINES))
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument(
        "--first-mode",
        choices=("audit", "baseline"),
        default="audit",
        help="condition used for the one-per-engine priority pass",
    )
    parser.add_argument(
        "--audit-append-file",
        type=Path,
        help="append this UTF-8 prompt delta only to the audit condition",
    )
    args = parser.parse_args()

    if args.duration_hours <= 0 or args.parallel <= 0 or args.poll_seconds <= 0:
        parser.error("duration, parallelism, and poll interval must be positive")
    if (
        args.max_run_hours <= 0
        or args.sidecar_min_wait_seconds < 0
        or args.sidecar_settle_minutes < 0
    ):
        parser.error("run timeout must be positive and settle delays nonnegative")

    args.out.mkdir(parents=True, exist_ok=True)
    state_path = args.out / "state.json"
    events_path = args.out / "events.jsonl"
    summary_path = args.out / "summary.md"
    targets, suffixes = load_targets(args.targets)
    audit_append_text = (
        args.audit_append_file.read_text(encoding="utf-8")
        if args.audit_append_file is not None
        else None
    )
    suffixes = append_audit_suffix(suffixes, audit_append_text)
    target_map = {target["id"]: target for target in targets}

    server_root = str(Path(args.server_root).resolve())
    data_dir = Path(args.data_dir).resolve()
    os.environ["AGENT_MONITOR_DATA_DIR"] = str(data_dir)
    sys.path.insert(0, server_root)
    from agent_monitor import auth  # imported only after DATA_DIR is configured

    with auth._conn() as connection:
        row = connection.execute("SELECT * FROM users WHERE id=?", (args.user_id,)).fetchone()
    user = auth._user_dict(row)
    if user is None or user.get("guest"):
        raise SystemExit("user id does not identify a persistent local account")

    token = auth.create_session(args.user_id)
    client = LiveClient(
        origin=args.origin,
        base_path=args.base_path,
        cookie_name=auth.SESSION_COOKIE,
        token=token,
    )
    install_signal_handlers()
    started = time.time()
    deadline = started + args.duration_hours * 3600
    state: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "deadline_at": datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
        "updated_at": now_iso(),
        "config": {
            "duration_hours": args.duration_hours,
            "parallel": args.parallel,
            "max_iterations": args.max_iterations,
            "max_run_hours": args.max_run_hours,
            "sidecar_min_wait_seconds": args.sidecar_min_wait_seconds,
            "sidecar_settle_minutes": args.sidecar_settle_minutes,
            "model": args.model,
            "targets_sha256": sha256_text(args.targets.read_text(encoding="utf-8")),
            "audit_append_sha256": (
                sha256_text(audit_append_text)
                if audit_append_text is not None
                else None
            ),
            "ucla_excluded": True,
        },
        "runs": [],
        "startup_errors": [],
    }

    try:
        health = client.get("/api/engines")
        available = {
            str(item.get("id"))
            for item in health.get("engines") or []
            if item.get("available") is not False
        }
        requested = [item.strip() for item in args.engines.split(",") if item.strip()]
        engines = [engine for engine in requested if engine in available and engine != "ucla"]
        skipped = [engine for engine in requested if engine not in engines]
        state["config"]["engines"] = engines
        state["config"]["skipped_engines"] = skipped
        if not engines:
            raise RuntimeError("no requested non-UCLA engine is currently available")
        schedule = make_schedule(
            engines,
            targets,
            seed=args.seed,
            first_mode=args.first_mode,
        )
        active: dict[str, dict[str, Any]] = {}
        cursor = 0

        while True:
            now = time.time()
            can_launch = now < deadline and not STOP_REQUESTED
            if args.max_runs and len(state["runs"]) >= args.max_runs:
                can_launch = False

            while can_launch and len(active) < args.parallel:
                spec = schedule[cursor % len(schedule)]
                cursor += 1
                target = target_map[spec["target"]]
                prompt = target["prompt"] + "\n\n" + suffixes[spec["mode"]]
                problem_id = (
                    f"audit24h_{target['id']}_{spec['mode']}_{spec['engine']}_"
                    f"{len(state['runs']) + 1:03d}"
                )
                request_body = {
                    "engine": spec["engine"],
                    "problem_id": problem_id,
                    "problem_text": prompt,
                    "model": args.model,
                    "max_iterations": args.max_iterations,
                    "use_subagents": True,
                }
                try:
                    job = client.post("/api/runs", request_body)
                except RuntimeError as exc:
                    failure = {
                        **spec,
                        "problem_id": problem_id,
                        "status": "submit_failed",
                        "submitted_at": now_iso(),
                        "error": str(exc),
                        "prompt_sha256": sha256_text(prompt),
                    }
                    state["runs"].append(failure)
                    append_event(events_path, {"event": "submit_failed", **failure})
                    if args.max_runs and len(state["runs"]) >= args.max_runs:
                        can_launch = False
                    # A server outage or shared configuration defect should not
                    # spin through the whole schedule in one polling interval.
                    break
                record = {
                    **spec,
                    "problem_id": problem_id,
                    "job_id": job.get("job_id"),
                    "run_id": job.get("run_id"),
                    "status": job.get("status") or "running",
                    "submitted_at": now_iso(),
                    "submitted_epoch": now,
                    "prompt_sha256": sha256_text(prompt),
                    "model": args.model,
                }
                state["runs"].append(record)
                active[str(record["job_id"])] = record
                append_event(events_path, {"event": "submitted", **record})
                if args.max_runs and len(state["runs"]) >= args.max_runs:
                    can_launch = False

            for job_id, record in list(active.items()):
                run_id = str(record.get("run_id") or "")
                try:
                    job = client.get(f"/api/jobs/{job_id}")
                    record["status"] = job.get("status") or record.get("status")
                    record["error"] = job.get("error")
                    record["last_seen_at"] = now_iso()
                except RuntimeError as exc:
                    record["poll_error"] = str(exc)
                    persisted = persisted_terminal_run(data_dir, run_id)
                    if persisted is not None:
                        record["status"] = persisted["status"]
                        record["error"] = persisted["error"]
                        record["recovered_from_persisted_run_at"] = now_iso()
                        record.pop("poll_error", None)

                runtime = now - float(record.get("submitted_epoch") or now)
                if (
                    record.get("status") not in TERMINAL_JOB_STATUSES
                    and runtime > args.max_run_hours * 3600
                ):
                    try:
                        client.post(f"/api/runs/{run_id}/stop")
                        record["timeout_stop_requested_at"] = now_iso()
                    except RuntimeError as exc:
                        record["timeout_stop_error"] = str(exc)

                if record.get("status") in TERMINAL_JOB_STATUSES:
                    snapshot = sidecar_snapshot(client, run_id)
                    record["sidecars"] = snapshot
                    settled_at = float(record.get("engine_terminal_epoch") or 0)
                    if not settled_at:
                        settled_at = now
                        record["engine_terminal_epoch"] = now
                        record["engine_terminal_at"] = now_iso()
                    settle_expired = now - settled_at >= args.sidecar_settle_minutes * 60
                    minimum_wait_elapsed = (
                        now - settled_at >= args.sidecar_min_wait_seconds
                    )
                    if minimum_wait_elapsed and (sidecars_settled(snapshot) or settle_expired):
                        record["sidecar_settle_expired"] = bool(
                            settle_expired and not sidecars_settled(snapshot)
                        )
                        record["evidence"] = collect_artifact_evidence(client, run_id)
                        record["collected_at"] = now_iso()
                        append_event(events_path, {"event": "collected", **record})
                        active.pop(job_id, None)

            state["updated_at"] = now_iso()
            state["schedule_cursor"] = cursor
            atomic_json(state_path, state)
            write_summary(summary_path, state)

            launch_limit_reached = bool(
                args.max_runs and len(state["runs"]) >= args.max_runs
            )
            if (now >= deadline or STOP_REQUESTED or launch_limit_reached) and not active:
                break
            time.sleep(min(args.poll_seconds, 60.0))

        state["stopped_at"] = now_iso()
        state["stop_reason"] = (
            "signal" if STOP_REQUESTED else "max_runs" if args.max_runs else "deadline"
        )
        state["updated_at"] = now_iso()
        atomic_json(state_path, state)
        write_summary(summary_path, state)
        return 0
    finally:
        auth.destroy_session(token)


if __name__ == "__main__":
    raise SystemExit(main())
