"""Ansatz CGM "Max": Claude Code leads, Codex works.

A Max run is an Ansatz CGM (``math_harness``) run on two linked subscriptions
at once.  Claude Code, on its most capable model at maximum effort, is the
orchestrator and the advisors: it plans (strategist), checks every lemma
(verifier) and curates the search.  Codex, on its most capable model at
maximum effort, is the worker: it writes and repairs the proof attempts.

Both accounts must be linked, which on this site means the administrator's
account; a Max run is never billed to an API key.  The run records the model
``max`` and the route ``dual_subscription``; ``route_plan`` on the run record
names the model each side used.

"Max (API)" is the same split on API keys: a Claude model through the
Anthropic API plans, checks and curates, and an OpenAI model through the
OpenAI API writes and repairs, both at maximum effort.  It records the model
``max-api`` on the ordinary ``api_key`` route (billed as API usage), with
``route_plan.mode`` ``"api"``.  The administrator's runs use the server keys,
as its other API runs do; a full-access member's runs use the Anthropic and
OpenAI keys saved on that account.  Neither Max falls back to the other.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

MODEL_ID = "max"
ROUTE = "dual_subscription"
ENGINE = "math_harness"
EFFORT = "max"
SPEED = "standard"

# Child-process markers (see jobs._configure_auth_route and the CGM runner).
ROUTE_MODE_ENV = "AGENT_MONITOR_CGM_ROUTE_MODE"
CODEX_CALL_HOME_ENV = "AGENT_MONITOR_CODEX_CALL_HOME"

# Most capable first; the first model the linked account offers is used.
ADVISOR_MODELS = ("fable", "opus", "sonnet")
WORKER_MODELS = ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol")
ADVISOR_ROLES = ("strategist", "verifier", "curator")
WORKER_ROLES = ("worker", "repair")

# Maximum-effort calls are long.  Per-call limits for each side, and the CGM
# pipeline's own wall clock for one Max run (the outer watchdog adds margin).
CODEX_CALL_TIMEOUT = 1800
CLAUDE_CALL_TIMEOUT = 1800
RUN_SECONDS = 10_800

# Lean and graph work after the run uses the worker route.  Those calls have a
# fixed per-call limit, so they run at high rather than maximum effort.
FORMAL_EFFORT = "high"

# Max (API).
API_MODEL_ID = "max-api"
API_LABEL = "Max (API)"
API_ROUTE = "api_key"
API_MODE = "api"
# ROUTE_MODE_ENV value of an API Max child ("max" stays subscription Max).
API_ROUTE_MODE = "max_api"
API_PROVIDERS = {"advisor": "anthropic", "worker": "openai"}
API_KEY_ENVS = {"advisor": "ANTHROPIC_API_KEY", "worker": "OPENAI_API_KEY"}
# Exact API model ids, most capable first: the first is used unless the
# operator pins another one of the list with the setting below.
API_ADVISOR_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5")
API_WORKER_MODELS = WORKER_MODELS
API_ADVISOR_MODEL_SETTING = "AGENT_MONITOR_CGM_MAX_API_ADVISOR_MODEL"
API_WORKER_MODEL_SETTING = "AGENT_MONITOR_CGM_MAX_API_WORKER_MODEL"
# What the runner reads in an API Max child (set only by apply_models_api).
API_ADVISOR_MODEL_ENV = "AGENT_MONITOR_CGM_ADVISOR_MODEL"
API_WORKER_MODEL_ENV = "AGENT_MONITOR_CGM_WORKER_MODEL"
# Per-call limits: maximum-effort calls are long (as on Max), and reasoning
# counts against the output ceiling.
API_CALL_TIMEOUT = 1800
API_MAX_OUTPUT_TOKENS = 64_000


def is_max_model(model: object) -> bool:
    return str(model or "").strip().lower() == MODEL_ID


def is_max_api_model(model: object) -> bool:
    return str(model or "").strip().lower() == API_MODEL_ID


def is_max_api_record(record: Mapping[str, Any] | None) -> bool:
    """Whether a run record belongs to a Max (API) run."""
    data = record or {}
    if is_max_record(data):
        return False
    plan = data.get("route_plan")
    if isinstance(plan, Mapping) and str(plan.get("mode") or "").strip().lower() == API_MODE:
        return True
    return is_max_api_model(data.get("model")) or is_max_api_model(data.get("requested_model"))


def route_mode(env: Mapping[str, str] | None) -> str | None:
    """``"max"``, ``"max_api"`` or ``None``: the Max kind a child env runs."""
    value = str((env or {}).get(ROUTE_MODE_ENV) or "").strip().lower()
    return value if value in {"max", API_ROUTE_MODE} else None


def is_max_record(record: Mapping[str, Any] | None) -> bool:
    """Whether a run record belongs to a Max run."""
    data = record or {}
    if str(data.get("auth_route") or "").strip().lower() == ROUTE:
        return True
    live = data.get("live") or {}
    return isinstance(live, Mapping) and str(live.get("auth_mode") or "").strip().lower() == ROUTE


def _side(route: str, model: str | None, roles: tuple[str, ...]) -> dict[str, Any]:
    return {"route": route, "model": model, "effort": EFFORT, "roles": list(roles)}


def plan_for(user: Mapping[str, Any] | None) -> dict[str, Any]:
    """Whether ``user`` can start a Max run, and the models it would use."""
    plan: dict[str, Any] = {
        "available": False,
        "reason": "",
        "advisor": _side("claude_subscription", ADVISOR_MODELS[0], ADVISOR_ROLES),
        "worker": _side("codex_subscription", WORKER_MODELS[0], WORKER_ROLES),
    }
    if not user:
        plan["reason"] = "Max needs a signed-in account with Claude Code and Codex linked"
        return plan
    if user.get("guest"):
        # Signing up gives a member account, which cannot link either one.
        plan["reason"] = (
            "Max runs on the site administrator's linked Claude Code and Codex "
            "subscriptions"
        )
        return plan
    from agent_monitor import account_policy

    if account_policy.is_restricted(user):
        plan["reason"] = (
            "Max runs on linked Claude Code and Codex subscriptions, which are "
            "limited to the site administrator"
        )
        return plan
    try:
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError):
        plan["reason"] = "Max needs a signed-in account with Claude Code and Codex linked"
        return plan
    from agent_monitor import claude_login, codex_login

    missing = []
    try:
        claude_ready = claude_login.account_login_ready(claude_login.account_home(user_id))
    except (OSError, ValueError):
        claude_ready = False
    try:
        codex_ready = codex_login.account_login_ready(codex_login.account_home(user_id))
    except (OSError, ValueError):
        codex_ready = False
    if not claude_ready:
        missing.append("Claude Code")
    if not codex_ready:
        missing.append("Codex")
    if missing:
        plan["reason"] = (
            "Max needs both Claude Code and Codex linked; connect "
            + " and ".join(missing)
            + " in Settings"
        )
        return plan
    advisor = next(
        (model for model in ADVISOR_MODELS if model in claude_login.available_models(user_id)),
        None,
    )
    worker = next(
        (model for model in WORKER_MODELS if model in codex_login.available_models(user_id)),
        None,
    )
    if advisor is None or worker is None:
        side = "Claude Code" if advisor is None else "Codex"
        plan["reason"] = f"The linked {side} account offers none of the models Max uses"
        return plan
    plan["advisor"]["model"] = advisor
    plan["worker"]["model"] = worker
    plan["available"] = True
    return plan


def record_plan(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """The route plan stored on a Max run, completed with the defaults."""
    stored = (record or {}).get("route_plan")
    stored = stored if isinstance(stored, Mapping) else {}
    plan = {
        "advisor": _side("claude_subscription", ADVISOR_MODELS[0], ADVISOR_ROLES),
        "worker": _side("codex_subscription", WORKER_MODELS[0], WORKER_ROLES),
    }
    for side, default_models in (("advisor", ADVISOR_MODELS), ("worker", WORKER_MODELS)):
        value = stored.get(side)
        if isinstance(value, Mapping):
            model = str(value.get("model") or "").strip()
            if model in default_models:
                plan[side]["model"] = model
    return plan


def apply_models(env: dict[str, str], plan: Mapping[str, Any]) -> dict[str, str]:
    """Select each side's model and the Max time limits in a child env."""
    env["AGENT_MONITOR_SELECTED_MODEL"] = MODEL_ID
    env["AGENT_MONITOR_CLAUDE_MODEL"] = str(plan["advisor"]["model"])
    env["AGENT_MONITOR_CODEX_MODEL"] = str(plan["worker"]["model"])
    env["AGENT_MONITOR_CODEX_TIMEOUT"] = str(CODEX_CALL_TIMEOUT)
    env["AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT"] = str(CLAUDE_CALL_TIMEOUT)
    env["AGENT_MONITOR_CGM_MAX_SECONDS"] = str(RUN_SECONDS)
    return env


def _api_side(side: str, model: str) -> dict[str, Any]:
    roles = ADVISOR_ROLES if side == "advisor" else WORKER_ROLES
    return {
        "route": API_ROUTE,
        "provider": API_PROVIDERS[side],
        "model": model,
        "effort": EFFORT,
        "roles": list(roles),
    }


def api_models(source: Mapping[str, str] | None = None) -> tuple[str, str]:
    """The (advisor, worker) API models new Max (API) runs use: the most
    capable of each list, unless the operator's console settings pin another
    model of that list."""
    env = os.environ if source is None else source
    advisor = str(env.get(API_ADVISOR_MODEL_SETTING) or "").strip()
    worker = str(env.get(API_WORKER_MODEL_SETTING) or "").strip()
    return (
        advisor if advisor in API_ADVISOR_MODELS else API_ADVISOR_MODELS[0],
        worker if worker in API_WORKER_MODELS else API_WORKER_MODELS[0],
    )


def _api_plan(advisor: str, worker: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": "",
        "mode": API_MODE,
        "advisor": _api_side("advisor", advisor),
        "worker": _api_side("worker", worker),
    }


def plan_for_api(user: Mapping[str, Any] | None) -> dict[str, Any]:
    """Whether ``user`` can start a Max (API) run, and the models it would use.

    Open to the administrator, on the server's Anthropic and OpenAI keys
    (unless it saved its own), and to full-access members, on the keys saved
    on their own account: never on the server's, even in public mode.
    """
    plan = _api_plan(*api_models())
    if not user:
        plan["reason"] = (
            "Max (API) needs a signed-in account with Anthropic and OpenAI API keys"
        )
        return plan
    if user.get("guest"):
        plan["reason"] = "Max (API) needs a signed-in account; guests can't start it"
        return plan
    from agent_monitor import account_policy

    if account_policy.is_restricted(user):
        plan["reason"] = (
            "Max (API) is limited to the site administrator and full-access accounts"
        )
        return plan
    try:
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError):
        plan["reason"] = (
            "Max (API) needs a signed-in account with Anthropic and OpenAI API keys"
        )
        return plan
    try:
        if user.get("is_admin"):
            from agent_monitor.settings import resolved_user_env

            keys = resolved_user_env(user)
        else:
            from agent_monitor.auth import get_user_env

            keys = get_user_env(user_id)
    except (OSError, ValueError):
        keys = {}
    missing = [
        "Anthropic" if side == "advisor" else "OpenAI"
        for side, name in API_KEY_ENVS.items()
        if not str((keys or {}).get(name) or "").strip()
    ]
    if missing:
        plan["reason"] = (
            "Max (API) needs both an Anthropic and an OpenAI API key; add your "
            + " and ".join(missing)
            + (" keys" if len(missing) > 1 else " key")
            + " in Settings"
        )
        return plan
    plan["available"] = True
    return plan


def record_plan_api(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """The route plan stored on a Max (API) run, completed with the defaults.

    The run file is read back into a child env, so a stored model counts only
    if it is one of the allowlisted ids for its side.
    """
    stored = (record or {}).get("route_plan")
    stored = stored if isinstance(stored, Mapping) else {}
    advisor, worker = API_ADVISOR_MODELS[0], API_WORKER_MODELS[0]
    for side, allowed in (("advisor", API_ADVISOR_MODELS), ("worker", API_WORKER_MODELS)):
        value = stored.get(side)
        if isinstance(value, Mapping):
            model = str(value.get("model") or "").strip()
            if model in allowed:
                if side == "advisor":
                    advisor = model
                else:
                    worker = model
    plan = _api_plan(advisor, worker)
    del plan["available"], plan["reason"]
    return plan


def apply_models_api(env: dict[str, str], plan: Mapping[str, Any]) -> dict[str, str]:
    """Select each side's API model and the Max time and output limits in a
    child env already configured for the ``api_key`` route.  Never sets a
    subscription marker: the run stays on its API keys."""
    env[ROUTE_MODE_ENV] = API_ROUTE_MODE
    env["AGENT_MONITOR_SELECTED_MODEL"] = API_MODEL_ID
    env[API_ADVISOR_MODEL_ENV] = str(plan["advisor"]["model"])
    env[API_WORKER_MODEL_ENV] = str(plan["worker"]["model"])
    env["AGENT_MONITOR_CGM_API_TIMEOUT"] = str(API_CALL_TIMEOUT)
    env["AGENT_MONITOR_CGM_MAX_SECONDS"] = str(RUN_SECONDS)
    env["AGENT_MONITOR_API_MAX_TOKENS"] = str(API_MAX_OUTPUT_TOKENS)
    # A request's output ceiling would cap that budget (reasoning counts
    # against it); like the request's effort and speed, it does not apply.
    env.pop("AGENT_MONITOR_MAX_OUTPUT_TOKENS", None)
    return env


def formal_view(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A run as Lean and graph work after the run sees it.

    Derived calls run on one route; for Max that is the linked Codex account
    with the worker model.  Derived calls have a fixed per-call limit (on a
    linked Codex account 240 s for a DAG or Lean response; on an API key
    180 s for a DAG response and 240 s for a Lean one), so like Max they run
    at no more than high effort: a Codex-subscription or API-key run made at
    extra-high or maximum effort derives its views at high.  (On 2026-10-26 a
    gpt-6-astra run at maximum effort lost its DAG's model analysis to that
    limit; Max's derived DAG at high took 110 s.)  Other records are returned
    unchanged.
    """
    if record is None:
        return record  # type: ignore[return-value]
    if is_max_api_record(record):
        # Max (API) derives its views on its worker's API model and key.
        model = str(record_plan_api(record)["worker"]["model"])
        view = dict(record)
        view["auth_route"] = API_ROUTE
        view["model"] = model
        view["requested_model"] = model
        view["reasoning_effort"] = FORMAL_EFFORT
        view["speed_mode"] = SPEED
        live = dict(view.get("live") or {})
        live.update(auth_mode=API_ROUTE, model=model, requested_model=model)
        view["live"] = live
        return view
    if not is_max_record(record):
        live = record.get("live") or {}
        route = str(record.get("auth_route") or "").strip().lower() or (
            "codex_subscription"
            if isinstance(live, Mapping)
            and str(live.get("auth_mode") or "").strip().lower()
            in {"codex_subscription", "chatgpt_subscription"}
            else ""
        )
        effort = str(record.get("reasoning_effort") or "").strip().lower()
        if route in {"codex_subscription", API_ROUTE} and effort in {"xhigh", "max"}:
            view = dict(record)
            view["reasoning_effort"] = FORMAL_EFFORT
            return view
        return record  # type: ignore[return-value]
    worker = record_plan(record)["worker"]
    model = str(worker["model"])
    view = dict(record)
    view["auth_route"] = "codex_subscription"
    view["model"] = model
    view["requested_model"] = model
    view["reasoning_effort"] = FORMAL_EFFORT
    view["speed_mode"] = SPEED
    live = dict(view.get("live") or {})
    live.update(auth_mode="chatgpt_subscription", model=model, requested_model=model)
    view["live"] = live
    return view


def formal_model(record: Mapping[str, Any] | None, fallback: str | None) -> str | None:
    """The model derived work should use for ``record``."""
    if is_max_record(record):
        return str(record_plan(record)["worker"]["model"])
    if is_max_api_record(record):
        return str(record_plan_api(record)["worker"]["model"])
    return fallback
