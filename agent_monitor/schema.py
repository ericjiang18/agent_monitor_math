"""Unified run / event schema fields shared by UCLA, IMProof, and Hermes."""

from __future__ import annotations

from typing import Any

EngineName = str

MATH_HARNESS_ENGINE_ID = "math_harness"
MATH_HARNESS_ENGINE_IMPL = "danus-exploring-graph-adapter"
MATH_HARNESS_ENGINE_IMPL_VERSION = "da482d4e25e58cebedd70843fdc88a2da0ece9706768148aec8b58b3a27d4dcf"
MATH_HARNESS_ARTIFACT_SCHEMA = "danus-project-v1"
MATH_HARNESS_GRAPH_SCHEMA = "danus-fact+exploring-v1"


def engine_provenance(engine: EngineName) -> dict[str, str]:
    """Immutable implementation identity attached to every new-format record."""
    if engine == MATH_HARNESS_ENGINE_ID:
        return {
            "engine_impl": MATH_HARNESS_ENGINE_IMPL,
            "engine_impl_version": MATH_HARNESS_ENGINE_IMPL_VERSION,
            "artifact_schema": MATH_HARNESS_ARTIFACT_SCHEMA,
            "graph_schema": MATH_HARNESS_GRAPH_SCHEMA,
        }
    return {}


REQUIRED_RUN_KEYS = (
    "run_id",
    "engine",
    "problem_id",
    "pipeline",
    "agents",
    "edges",
    "totals",
)


def _as_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def normalize_run(run: dict[str, Any], *, engine: EngineName) -> dict[str, Any]:
    """Ensure a run dict carries the unified engine tag and defaults.

    Also derives the aggregate fields the monitor dashboard renders:
    per-agent ``total_input_tokens`` and run-level ``totals.agents`` /
    ``totals.input_tokens`` / ``totals.output_tokens``.
    """
    out = dict(run)
    out["engine"] = engine
    out.update(engine_provenance(engine))
    out.setdefault("problem_id", out.get("run_id", "unknown"))
    out.setdefault("pipeline", [])
    out.setdefault("agents", [])
    out.setdefault("edges", [])
    out.setdefault("totals", {})
    out.setdefault("analysis", {})
    out.setdefault("source", engine)

    agents = out["agents"]
    if not str(out.get("model") or "").strip():
        live = out.get("live") if isinstance(out.get("live"), dict) else {}
        candidates = [live.get("model")]
        candidates.extend(
            agent.get("model")
            for agent in reversed(agents)
            if isinstance(agent, dict)
        )
        for candidate in candidates:
            selected = str(candidate or "").strip()
            if selected and selected != "—":
                out["model"] = selected
                break
    for i, a in enumerate(agents):
        if a.get("total_input_tokens") is None:
            a["total_input_tokens"] = (
                _as_int(a.get("input_tokens"))
                + _as_int(a.get("cache_read_tokens"))
                + _as_int(a.get("cache_write_tokens"))
            )
        a.setdefault("input_tokens", _as_int(a.get("input_tokens")))
        a.setdefault("output_tokens", _as_int(a.get("output_tokens")))
        a.setdefault("cache_read_tokens", 0)
        a.setdefault("cache_write_tokens", 0)
        a.setdefault("call_seq", i + 1)
        a.setdefault("model", a.get("model") or "—")

    totals = out["totals"]
    totals.setdefault("agents", len(agents))
    if not totals.get("input_tokens"):
        totals["input_tokens"] = sum(_as_int(a.get("total_input_tokens")) for a in agents)
    if not totals.get("output_tokens"):
        totals["output_tokens"] = sum(_as_int(a.get("output_tokens")) for a in agents)
    if not totals.get("cost_usd"):
        cost = sum(a.get("cost_usd") or 0 for a in agents)
        if cost:
            totals["cost_usd"] = cost
    return out
