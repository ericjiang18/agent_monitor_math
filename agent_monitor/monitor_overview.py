"""Aggregate account-level usage and workspace state for the console Monitor."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_monitor import CACHE_DIR


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _owned(record: dict[str, Any], user: dict[str, Any]) -> bool:
    # Account usage is personal; admins see everyone in /api/admin/usage.
    owner = record.get("owner_id")
    if owner is None:
        return bool(user.get("is_admin"))
    return owner == user.get("id")


def _load_runs(cache_dir: Path, user: dict[str, Any]) -> list[dict[str, Any]]:
    manifest_path = cache_dir / "manifest.json"
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8")).get("runs") or []
    except (OSError, json.JSONDecodeError):
        entries = []
    runs: list[dict[str, Any]] = []
    for entry in entries:
        run_id = str(entry.get("run_id") or "")
        if not run_id or "/" in run_id or ".." in run_id:
            continue
        try:
            record = json.loads((cache_dir / f"{run_id}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = dict(entry)
        if _owned(record, user):
            runs.append(record)
    return runs


def _has_proof_artifact(run: dict[str, Any]) -> bool:
    if run.get("final_latex"):
        return True
    workspace = Path(str(run.get("workspace") or ""))
    if not workspace.is_dir():
        return False
    for name in ("proof.md", "proof.tex"):
        candidate = workspace / name
        try:
            if candidate.is_file() and candidate.stat().st_size >= 40:
                return True
        except OSError:
            pass
    try:
        for candidate in workspace.rglob("*"):
            if not candidate.is_file() or candidate.suffix.lower() not in {".md", ".tex"}:
                continue
            if not any(word in candidate.name.lower() for word in ("proof", "solution", "answer")):
                continue
            if candidate.stat().st_size >= 40:
                return True
    except OSError:
        pass
    return False


def _effective_status(run: dict[str, Any]) -> str:
    status = str(run.get("status") or "unknown")
    if status == "finished" and not _has_proof_artifact(run):
        return "incomplete"
    return status


def _engine_rows(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for run in runs:
        engine = str(run.get("engine") or "unknown")
        row = grouped.setdefault(
            engine,
            {
                "engine": engine,
                "runs": 0,
                "finished": 0,
                "failed": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "latency_s": 0.0,
                "last_status": None,
                "last_updated_at": None,
            },
        )
        totals = run.get("totals") or {}
        status = _effective_status(run)
        updated = str(run.get("updated_at") or run.get("created_at") or "")
        row["runs"] += 1
        row["finished"] += int(status == "finished")
        row["failed"] += int(status in {"failed", "incomplete"})
        row["input_tokens"] += int(_number(totals.get("input_tokens")))
        row["output_tokens"] += int(_number(totals.get("output_tokens")))
        row["cost_usd"] += _number(totals.get("cost_usd"))
        row["latency_s"] += _number(totals.get("latency_s"))
        if not row["last_updated_at"] or updated > row["last_updated_at"]:
            row["last_updated_at"] = updated
            row["last_status"] = status
    return sorted(grouped.values(), key=lambda row: (-row["runs"], row["engine"]))


def build_overview(user: dict[str, Any]) -> dict[str, Any]:
    """Return usage, health, Library and Agent summaries for one account."""
    runs = _load_runs(CACHE_DIR / "harness", user)
    engines = _engine_rows(runs)
    usage = {
        "input_tokens": sum(row["input_tokens"] for row in engines),
        "output_tokens": sum(row["output_tokens"] for row in engines),
        "cost_usd": round(sum(row["cost_usd"] for row in engines), 6),
        "latency_s": round(sum(row["latency_s"] for row in engines), 3),
    }
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    statuses: dict[str, int] = {}
    for run in runs:
        status = _effective_status(run)
        statuses[status] = statuses.get(status, 0) + 1

    from agent_monitor import agent_config, codex_login, library
    from agent_monitor.engines_registry import list_engines
    from agent_monitor.settings import get_settings

    lib = library.get_library()
    agent = agent_config.get_agent_config()
    settings = get_settings(user)
    codex_connected = (
        bool(settings.get("codex_connected"))
        if "codex_connected" in settings
        else bool(codex_login.status(int(user["id"])).get("connected"))
    )
    claude_connected = bool(settings.get("claude_connected"))
    runtime_value = str(settings.get("account_runtime") or "").strip().lower()
    runtime = runtime_value if runtime_value in {"api", "codex", "claude"} else (
        "codex" if codex_connected else "api"
    )
    subscription_connected = (
        codex_connected if runtime == "codex"
        else claude_connected if runtime == "claude"
        else False
    )
    engine_health = list_engines()
    history = {row["engine"]: row for row in engines}
    for item in engine_health:
        prior = history.get(item["id"]) or {}
        item["tested"] = bool(prior)
        item["last_run_status"] = prior.get("last_status")
        item["run_count"] = prior.get("runs", 0)
    enabled = {
        kind: sum(
            1
            for item in lib.get("items") or []
            if item.get("type") == kind and item.get("enabled", True)
        )
        for kind in ("memory", "skill", "tool")
    }
    activity = []
    for run in sorted(
        runs,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )[:8]:
        activity.append(
            {
                "run_id": run.get("run_id"),
                "engine": run.get("engine"),
                "status": _effective_status(run),
                "title": run.get("problem_text_preview") or run.get("problem_id"),
                "updated_at": run.get("updated_at") or run.get("created_at"),
                "cost_usd": _number((run.get("totals") or {}).get("cost_usd")),
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs": {"total": len(runs), "statuses": statuses, "active": statuses.get("running", 0)},
        "usage": usage,
        "engines": engines,
        "engine_health": engine_health,
        "activity": activity,
        "library": {
            "counts": lib.get("counts") or {},
            "enabled": enabled,
            "auto_memory": bool((lib.get("settings") or {}).get("auto_memory")),
        },
        "agent": {
            "memory_enabled": bool(agent.get("memory_enabled")),
            "memory_files": len(agent.get("memory") or []),
            "skills": len(agent.get("skills") or []),
            "enabled_skills": sum(1 for item in agent.get("skills") or [] if item.get("enabled")),
            "system_prompt_chars": len(agent.get("system_prompt") or ""),
        },
        "auth": {
            "account_runtime": runtime,
            "codex_connected": codex_connected,
            "claude_connected": claude_connected,
            "subscription_connected": subscription_connected,
            "configured_providers": settings.get("configured_providers") or [],
        },
    }
