"""Hermes engine runner — thin wrapper around vendored AIAgent."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from agent_monitor import HERMES_HOME, RUNS_DIR
from agent_monitor.paths import ensure_data_dirs, ensure_import_paths
from agent_monitor.schema import normalize_run


def _configure_hermes_home() -> Path:
    ensure_data_dirs()
    os.environ["HERMES_HOME"] = str(HERMES_HOME)
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    _load_env_files()
    return HERMES_HOME


def _scrub_dead_proxy_env() -> None:
    """Drop inherited localhost proxy URLs (e.g. a stopped LiteLLM proxy).

    A stale ANTHROPIC_BASE_URL / OPENAI_BASE_URL pointing at localhost hijacks
    Hermes routing and produces connection errors.
    """
    import socket
    from urllib.parse import urlparse

    for var in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OPENROUTER_BASE_URL"):
        url = os.environ.get(var, "")
        if not url:
            continue
        parsed = urlparse(url if "//" in url else f"http://{url}")
        host = parsed.hostname or ""
        if host not in {"localhost", "127.0.0.1", "0.0.0.0"}:
            continue
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=1):
                pass
        except OSError:
            os.environ.pop(var, None)
            print(f"[agent-monitor] dropped dead proxy env {var}={url}")


def _load_env_files() -> None:
    """Load API keys from Agent_Monitor/.env, then ~/.hermes/.env as fallback."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    root_env = HERMES_HOME.parent.parent / ".env"  # Agent_Monitor/.env
    user_hermes_env = Path.home() / ".hermes" / ".env"
    for env_path in (root_env, user_hermes_env):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def create_agent(
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    max_iterations: int = 60,
) -> Any:
    """Construct a quiet embedded AIAgent for informal proving."""
    _configure_hermes_home()
    ensure_import_paths()
    from run_agent import AIAgent  # type: ignore

    model = model or os.environ.get("AGENT_MONITOR_MODEL") or os.environ.get(
        "HERMES_MODEL", "gpt-5.6-sol"
    )
    _scrub_dead_proxy_env()
    api_key = (
        api_key
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or ""
    )
    base_url = (
        base_url
        or os.environ.get("AGENT_MONITOR_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENROUTER_BASE_URL")
    )
    if not base_url and os.environ.get("OPENAI_API_KEY"):
        base_url = "https://api.openai.com/v1"

    kwargs: dict[str, Any] = {
        "model": model,
        "enabled_toolsets": ["terminal", "file", "code_execution", "delegation"],
        "skip_context_files": True,
        "quiet_mode": True,
        "max_iterations": max_iterations,
        "platform": "embedded",
    }
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    # skip_memory may not exist on all versions; set if supported
    try:
        return AIAgent(**kwargs, skip_memory=True)
    except TypeError:
        kwargs.pop("platform", None)
        try:
            return AIAgent(**kwargs, skip_memory=True)
        except TypeError:
            return AIAgent(**{k: v for k, v in kwargs.items() if k != "skip_memory"})


def run_problem(
    problem_text: str,
    *,
    problem_id: str = "hermes_problem",
    model: str | None = None,
    max_iterations: int = 60,
) -> dict[str, Any]:
    """Run one informal proving session and write a unified run JSON."""
    ensure_data_dirs()
    agent = create_agent(model=model, max_iterations=max_iterations)
    prompt = (
        "You are working on an informal mathematics proof problem.\n"
        "Use tools as needed (code, files, terminal, subagents).\n"
        "Produce a clear informal proof write-up.\n\n"
        f"PROBLEM:\n{problem_text}\n"
    )
    started = time.time()
    result = agent.run_conversation(prompt)
    elapsed = time.time() - started

    run_id = f"hermes_{problem_id}_{uuid.uuid4().hex[:8]}"
    messages = (result or {}).get("messages") or []
    final = (result or {}).get("final_response") or ""
    agents = [
        {
            "trace_id": f"{run_id}::hermes_main",
            "stage_name": "hermes_main",
            "role": "prover",
            "pipeline_stage": "draft",
            "model": getattr(agent, "model", model),
            "latency_s": elapsed,
            "input_tokens": (result or {}).get("input_tokens")
            or (result or {}).get("prompt_tokens"),
            "output_tokens": (result or {}).get("output_tokens")
            or (result or {}).get("completion_tokens"),
            "cost_usd": (result or {}).get("estimated_cost_usd")
            or (result or {}).get("actual_cost_usd"),
            "prompt": prompt,
            "output": final,
            "tool_calls": (result or {}).get("tool_call_count"),
        }
    ]
    run = normalize_run(
        {
            "run_id": run_id,
            "problem_id": problem_id,
            "trace_name": f"[Hermes] {problem_id}",
            "pipeline": [
                {"id": "understand", "label": "Understand", "title": "Understand"},
                {"id": "plan", "label": "Plan", "title": "Plan"},
                {"id": "draft", "label": "Draft", "title": "Draft proof"},
                {"id": "verify", "label": "Verify", "title": "Verify"},
                {"id": "finalize", "label": "Finalize", "title": "Finalize"},
            ],
            "agents": agents,
            "edges": [],
            "totals": {
                "cost_usd": agents[0].get("cost_usd"),
                "latency_s": elapsed,
                "api_calls": (result or {}).get("api_calls"),
            },
            "raw_result_keys": sorted((result or {}).keys()),
            "message_count": len(messages),
            "completed": bool((result or {}).get("completed", True)),
        },
        engine="hermes",
    )
    out = RUNS_DIR / f"{run_id}.json"
    out.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    cache = Path(os.environ.get("LLM_DASHBOARD_CACHE", str(RUNS_DIR.parent / "cache"))) / "harness"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{run_id}.json").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    return run
