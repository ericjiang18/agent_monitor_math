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


# Codex's workspace-write sandbox needs network access enabled on this host to
# avoid the unavailable loopback namespace path. Keep that access behind the
# app-server's supported domain proxy instead of granting general egress.
_CODEX_MATH_NETWORK_DOMAINS = [
    "**.ams.org",
    "**.arxiv.org",
    "**.cambridge.org",
    "**.combinatorica.hu",
    "**.crossref.org",
    "**.dartmouth.edu",
    "**.doi.org",
    "**.erdosproblems.com",
    "**.oeis.org",
    "**.tandfonline.com",
]


def _codex_network_domains() -> list[str]:
    """Enable Codex's scoped proxy only when the deployment opts in.

    Some kernels cannot create the proxy's loopback namespace; on those hosts
    enabling it makes even ordinary workspace file tools fail before launch.
    """
    enabled = os.environ.get("AGENT_MONITOR_HERMES_SCOPED_NETWORK", "").strip().lower()
    return list(_CODEX_MATH_NETWORK_DOMAINS) if enabled in {"1", "true", "yes", "on"} else []


def close_agent(agent: Any, *, messages: list[dict[str, Any]] | None = None) -> None:
    """Close every resource owned by a one-shot embedded Hermes agent.

    Hermes' public ``AIAgent.close()`` currently does not retire the lazily
    created Codex app-server session. Monitor jobs are one-shot, so leaving
    that session open leaks the app-server and its MCP/code-mode children
    after the run has already reached a terminal state.

    Keep this helper idempotent and explicit rather than relying on garbage
    collection: service workers retain enough references for GC to be both
    late and nondeterministic.
    """
    if agent is None:
        return
    try:
        shutdown_memory = getattr(agent, "shutdown_memory_provider", None)
        if callable(shutdown_memory):
            shutdown_memory(messages or [])
    except Exception:  # noqa: BLE001 - best-effort teardown must continue
        pass
    try:
        session = getattr(agent, "_codex_session", None)
        if session is not None:
            session.close()
        agent._codex_session = None
    except Exception:  # noqa: BLE001 - continue to the agent-level cleanup
        pass
    try:
        close = getattr(agent, "close", None)
        if callable(close):
            close()
    except Exception:  # noqa: BLE001 - cleanup cannot mask the run result
        pass


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


def _install_subagent_model_hook() -> None:
    """Patch delegate_tool so each parent agent can pin its subagent model.

    Hermes only reads the subagent model from the global config.yaml
    (``delegation.model``), which is shared across concurrent runs. This hook
    overlays ``cfg["model"]`` from a per-agent attribute
    (``_monitor_subagent_model``) set by :func:`create_agent`, so different
    runs can use different subagent models safely.
    """
    import tools.delegate_tool as dt  # type: ignore

    if getattr(dt, "_monitor_submodel_hook", False):
        return
    orig = dt._resolve_delegation_credentials

    def patched(cfg: dict, parent_agent):  # noqa: ANN001
        override = getattr(parent_agent, "_monitor_subagent_model", None)
        if override:
            cfg = dict(cfg or {})
            cfg["model"] = override
        return orig(cfg, parent_agent)

    dt._resolve_delegation_credentials = patched
    dt._monitor_submodel_hook = True


def create_agent(
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    codex_home: str | Path | None = None,
    use_codex_subscription: bool = False,
    max_iterations: int = 60,
    enable_subagents: bool = True,
    subagent_model: str | None = None,
    reasoning_effort: str | None = None,
    service_tier: str | None = None,
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

    toolsets = ["terminal", "file", "code_execution", "skills"]
    if enable_subagents:
        toolsets.append("delegation")
    kwargs: dict[str, Any] = {
        "model": model,
        "enabled_toolsets": toolsets,
        "skip_context_files": True,
        # Load HERMES_HOME/SOUL.md (editable in the console's Agent panel) as
        # the agent identity; falls back to the default identity when absent.
        "load_soul_identity": True,
        "quiet_mode": True,
        "max_iterations": max_iterations,
        "platform": "embedded",
    }
    if reasoning_effort:
        kwargs["reasoning_config"] = {"effort": reasoning_effort}
    if service_tier:
        kwargs["service_tier"] = service_tier
    if use_codex_subscription:
        # Keep OAuth ownership in Codex CLI. Hermes' app-server runtime reads
        # the user's CODEX_HOME directly, so no refresh token is copied into
        # Hermes' own auth store (which would create token-rotation races).
        kwargs["provider"] = "openai-codex"
        kwargs["api_mode"] = "codex_app_server"
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    # Persistent memory (memories/MEMORY.md + USER.md) is opt-in via the
    # console's Agent panel — it toggles memory.memory_enabled in config.yaml.
    try:
        from agent_monitor.agent_config import memory_enabled

        skip_memory = not memory_enabled()
    except Exception:  # noqa: BLE001
        skip_memory = True

    # skip_memory / load_soul_identity may not exist on all versions
    agent = None
    try:
        agent = AIAgent(**kwargs, skip_memory=skip_memory)
    except TypeError:
        kwargs.pop("platform", None)
        try:
            agent = AIAgent(**kwargs, skip_memory=skip_memory)
        except TypeError:
            kwargs.pop("load_soul_identity", None)
            agent = AIAgent(**{k: v for k, v in kwargs.items() if k != "skip_memory"})
    if enable_subagents and subagent_model:
        try:
            _install_subagent_model_hook()
            agent._monitor_subagent_model = subagent_model
        except Exception as exc:  # noqa: BLE001
            print(f"[agent-monitor] subagent model hook failed: {exc}")
    if use_codex_subscription and codex_home:
        agent._monitor_codex_home = str(Path(codex_home))
        # Monitor jobs are unattended but already confined to a unique
        # workspace-write root. Accept file-change requests inside that root;
        # command elevation and permissions changes remain fail-closed.
        agent._monitor_auto_approve_apply_patch = True
        agent._monitor_codex_network_domains = _codex_network_domains()
    return agent


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
