"""Background job manager for engine runs."""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import signal
import shutil
import sqlite3
import stat
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence  # noqa: F401 — used throughout

from agent_monitor import CACHE_DIR, HERMES_HOME, PROBLEMS_DIR, RUNS_DIR
from agent_monitor import account_policy
from agent_monitor import cgm_max
from agent_monitor import cgm_sources
from agent_monitor import attachments as research_attachments
from agent_monitor.research_audit import (
    reconciliation_complete,
    workspace_reconciliation_status,
)
from agent_monitor.schema import normalize_run
from agent_monitor.proof_markdown import normalize_proof_markdown
from agent_monitor import attachments as research_attachments

# Re-entrant: a chat message decides between joining the feedback queue and
# claiming the run under one hold of it (see ``send_human_message``).
_LOCK = threading.RLock()
_JOBS: dict[str, dict[str, Any]] = {}
_DELETED_RUNS: set[str] = set()
# The scalar fields (owner, route, model...) of each deleted run's last
# record: the usage a worker still reports after the delete is booked under
# them (see _write_run_locked).  Kept for the latest deletions only
# (delete_run): a worker's last write comes within its wind-down.
_DELETED_RUN_IDENTITY: dict[str, dict[str, Any]] = {}
_DELETED_RUN_IDENTITY_MAX = 1000
# The record of a run deleted while one of its sessions ran, as that session's
# worker last wrote it (the removed file's stand-in, kept in memory only),
# until the worker ends (_end_worker).  A follow-up's live records hold only
# its own turns and usage: merged onto the run's earlier sessions as it stops
# (_record_at_session_end), they still book that usage.
_DELETED_RUN_LAST: dict[str, dict[str, Any]] = {}
_STOP_EVENTS: dict[str, threading.Event] = {}
_PROCS: dict[str, Any] = {}  # run_id -> subprocess.Popen
# The worker thread of each session (a first run or a continuation) by job id,
# with its run id, until that worker is done with the run.  A Stop records the
# session stopped at once (_record_stopped) while its worker winds down: the
# runner may take up to _RUNNER_SETTLE_SECONDS to exit, then come its last
# record and chat writes and the removal of its prompt file.  Nothing new may
# start on the run meanwhile (see _session_worker_alive).
_WORKERS: dict[str, tuple[str, Any]] = {}
_RECONCILE_LOCK = threading.Lock()
# Re-entrant: a read-modify-write of one record holds it around _write_run.
_RUN_WRITE_LOCK = threading.RLock()
# Held around each chat append, and by delete_run while it removes the
# workspace, so a line a winding-down worker writes never recreates it.
_CHAT_LOCK = threading.Lock()
_HERMES_HEARTBEAT_S = 2.0
# Problem IDs become run-record and cache filenames, so they must never carry a
# path separator or a traversal segment.
_PROBLEM_ID_RE = re.compile(r"^[A-Za-z0-9_.()\[\]-]{1,128}$")

# The tool-free Kimi harness speaks the same bounded chat-completions transport
# as the Plain baseline, so it requests sponsored credentials under that name.
# Ansatz Analyze and Ansatz Readability reuse the CGM response-only transport,
# so they request the ``math_harness`` scope (the broker allowlists need no
# new engine).
_SPONSORED_TRANSPORT_ENGINES = {"kimi": "plain", "analyze": "math_harness", "readability": "math_harness"}

# Engines that run the Ansatz CGM pipeline (math_harness_runner.run_pipeline).
# ``kimi`` is its tool-free, guest-safe Kimi K3 entry point.
_CGM_ENGINES = frozenset({"kimi", "math_harness"})
# Ansatz Analyze (run_kind "analyze"): the only engine of the Analyze mode.
_ANALYZE_ENGINE = "analyze"
# Ansatz Readability (run_kind "readability"): the only engine of its mode.
_READABILITY_ENGINE = "readability"
# Engines that read someone's manuscript: never a proof (no promotion, no
# derived proof views, no library, no continuation; see
# analyze_runs.is_manuscript_record).
_MANUSCRIPT_ENGINES = frozenset({_ANALYZE_ENGINE, _READABILITY_ENGINE})
# Each manuscript runner's own model variable (analyze_runner.RouteRules
# .model_env), which, like MATH_HARNESS_MODEL, it reads before the selection.
_MANUSCRIPT_MODEL_ENV = {
    _ANALYZE_ENGINE: "AGENT_MONITOR_ANALYZE_MODEL",
    _READABILITY_ENGINE: "AGENT_MONITOR_READABILITY_MODEL",
}
# Kimi reasoning tokens count against max_tokens; guest and sponsored CGM runs
# get exactly this bounded ceiling (the sponsored gateway's default clamp).
_CGM_BOUNDED_OUTPUT_TOKENS = 8192
# Engines whose guest and sponsored runs get exactly that ceiling.
_BOUNDED_OUTPUT_ENGINES = _CGM_ENGINES | _MANUSCRIPT_ENGINES

# Called once when an anonymous guest run finishes, so the console can start
# the bounded derived Formal/DAG pipeline under its guest admission limits.
_GUEST_COMPLETION_HOOK: Any = None


def set_guest_completion_hook(hook: Any) -> None:
    """Register ``hook(run_id, owner_id, workspace)`` for finished guest runs."""
    global _GUEST_COMPLETION_HOOK
    _GUEST_COMPLETION_HOOK = hook


def _notify_guest_completion(run_id: str, owner_id: int | None, workspace: Path) -> None:
    hook = _GUEST_COMPLETION_HOOK
    if hook is None or owner_id is None or _run_deleted(run_id):
        return
    try:
        hook(run_id, owner_id, workspace)
    except Exception as exc:  # noqa: BLE001 - derived views are best-effort
        print(f"[agent-monitor] guest derived pipeline did not start: {type(exc).__name__}")


def _sponsored_transport_engine(engine: str) -> str:
    return _SPONSORED_TRANSPORT_ENGINES.get(engine, engine)


def _operator_kimi_base() -> str:
    """Own-key Kimi endpoint chosen by the operator, never by an account."""
    from agent_monitor.settings import operator_kimi_base_url

    return operator_kimi_base_url()


def _own_kimi_base_env(env: dict[str, str], *, engine: str, model: str | None) -> None:
    """Send an own Kimi key of a proof engine that runs Kimi K3 alone (Plain,
    the CGM harness, Meta-Harness, DeepSeek Harness, IMProof: the proof
    engines of engines_registry.KIMI_K3_ONLY_ENGINE_IDS) to the operator's
    Kimi endpoint (``AGENT_MONITOR_KIMI_BASE_URL``), as the ``kimi`` engine
    and the manuscript engines do, unless the account chose one: an
    administrator's or a full-access account's own ``KIMI_API_BASE`` stands,
    and a restricted member's runs have the operator's already
    (settings.restrict_account_base_urls).  Their runners would otherwise
    use the built-in Kimi base.  Starts and continuations alike; nothing on
    the sponsored route, without a Kimi key or for another model, and the
    coding-agent harnesses keep their own endpoint resolution."""
    from agent_monitor import kimi_k3
    from agent_monitor.engines_registry import KIMI_K3_ONLY_ENGINE_IDS

    if engine == "kimi" or engine in _MANUSCRIPT_ENGINES or engine not in KIMI_K3_ONLY_ENGINE_IDS:
        return
    if not (_is_kimi_model(model) or kimi_k3.names_kimi_k3(model)):
        return
    if str(env.get("AGENT_MONITOR_SPONSORED_KIMI") or "").strip() == "1":
        return
    if not str(env.get("KIMI_API_KEY") or "").strip() or str(env.get(kimi_k3.BASE_VAR) or "").strip():
        return
    env[kimi_k3.BASE_VAR] = _operator_kimi_base()

_RESEARCH_AUDIT_GATE_MARKER = "CANDIDATE AUDIT GATE:"
_RESEARCH_AUDIT_RUNTIME_MARKER = "STRICT FILE-BACKED AUDIT CONTRACT:"
_RESEARCH_AUDIT_RUNTIME_CONTRACT = """
STRICT FILE-BACKED AUDIT CONTRACT: If the candidate-audit gate activates, do
not invent a convenient JSON shape and do not treat a model saying "validated"
as validation. Every pass JSON must satisfy the installed verifier-output.v1
schema and must be checked by `_library/tools/audit-output-validator.sh` when
that trusted tool is present. Before finalizing, freeze the exact final
proof.md bytes as audit/<run-id>/final-candidate.md. If the proof changed after
the first review, rerun the independent global and decomposed passes against
that exact file as global-rerun.json and decomposed-rerun.json, rebuild
verifier-output.json and merge-map.json, and retain any refuter rerun. run.json
must use reconciliation schema research-proof-audit.reconciliation.v1 to bind
the root proof hash, final-candidate hash, and current rerun files.
The merge map must preserve standard finding source records and the counts
keys global_only, decomposed_only, and both. Run
`_agent/skills/research-proof-audit/scripts/validate_reconciliation.py
audit/<run-id>` and do not edit proof.md afterward. If that command is
unavailable or nonzero, state `degraded audit:` and list the exact missing or
invalid artifacts. Never convert audit acceptance into a solved claim.
""".strip()

_RESEARCH_AUDIT_REQUIRED_FILES = {
    "global.json",
    "decomposed.json",
    "verifier-output.json",
    "merge-map.json",
    "run.json",
}
_HERMES_AUDIT_LAST_MILE_PROMPT = """
Mandatory audit last mile for this explicit candidate-audit run:

The current proof.md is the frozen candidate. Do not start a new proof or a
new broad literature search. Read `_agent/skills/research-proof-audit/SKILL.md`
completely, then reconcile the existing candidate with independent global and
decomposed passes. Add a refuter pass only if proof.md proposes a solution or
counterexample. Use one `audit/<run-id>/` directory and finish it with valid
global.json and decomposed.json. Update proof.md to repair every accepted
finding or retain it as an explicit unresolved obligation, then freeze its
exact bytes as final-candidate.md. Run fresh global and decomposed passes on
that exact file as global-rerun.json and decomposed-rerun.json, rebuild
verifier-output.json and merge-map.json, and record a
research-proof-audit.reconciliation.v1 object in run.json that binds both
hashes and those reruns. Run `_library/tools/audit-output-validator.sh` on
every verifier-output.v1 file, then run
`_agent/skills/research-proof-audit/scripts/validate_reconciliation.py
audit/<run-id>`. Do not edit proof.md afterward. Preserve the existing
conservative outcome unless the exact original claim is independently closed;
audit acceptance alone is never a solution. Before returning, list the
artifact paths and both validator results.
""".strip()

# Guests may start only these tool-free engines (Analyze and Readability
# behind their own guest switches). A command override takes the engine
# outside that built-in contract, so it disables it for guests.
_GUEST_ENGINES = {"plain", "kimi", "analyze", "readability"}
_GUEST_COMMAND_OVERRIDES = {
    "plain": "PLAIN_CMD", "kimi": "KIMI_PROOF_CMD", "analyze": "ANALYZE_CMD",
    "readability": "READABILITY_CMD",
}

# Engines that can consume the run owner's Codex/ChatGPT subscription. Some
# use a native Codex runtime; the small Python harnesses use our codex-exec
# adapter. UCLA is deliberately absent because its current pipeline has no
# subscription-safe model path.
_CODEX_SUBSCRIPTION_ENGINES = {
    "hermes",
    "math_harness",
    "codex",
    "openclaude",
    "improof",
    "openhands",
    "openclaw",
    "deepagents",
    "metaharness",
    "plain",
    "deepseek_harness",
    "analyze",
    "readability",
}

# Claude OAuth is intentionally exposed only through adapters that invoke the
# official Claude Code CLI. OAuth credentials are not API keys and must never
# be copied into third-party provider clients.
_CLAUDE_SUBSCRIPTION_ENGINES = {
    "claude", "improof", "math_harness", "openclaw", "deepagents",
    "metaharness", "plain", "analyze", "readability",
}
# ``dual_subscription`` is Ansatz CGM Max: Claude Code and Codex together.
_AUTH_ROUTES = {"api_key", "codex_subscription", "claude_subscription", cgm_max.ROUTE}


_SPONSORED_KIMI_CLI_TIMEOUT_MAX = 6_600
_SPONSORED_KIMI_TOKEN_MARGIN = 300
# Provider credentials and endpoint selectors must be dormant in a linked
# account process. This is intentionally broader than the variables used by
# Claude/Codex themselves: compatible third-party harness adapters receive the
# same child environment and must not discover a metered API fallback.
_PROVIDER_ROUTE_ENV_NAMES = {
    "OPENAI_API_KEY", "OPENAI_API_KEYS", "CODEX_API_KEY",
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "AGENT_MONITOR_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "KIMI_API_KEY", "KIMI_API_BASE", "KIMI_BASE_URL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE",
    "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
    "GEMINI_API_KEY", "GEMINI_API_BASE", "GOOGLE_API_KEY",
    "GROQ_API_KEY", "GROQ_API_BASE",
    "TOGETHERAI_API_KEY", "TOGETHERAI_API_BASE",
    "XAI_API_KEY", "XAI_API_BASE",
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "AGENT_MONITOR_MODEL", "HERMES_MODEL", "AGENT_MONITOR_OPENAI_MODEL",
    "PLAIN_MODEL", "METAHARNESS_MODEL", "DEEPAGENTS_MODEL",
    "AGENT_MONITOR_OPENCLAUDE_MODEL", "AGENT_MONITOR_OPENCLAW_MODEL",
    "AGENT_MONITOR_OPENHANDS_MODEL",
}


def _scrub_provider_route_env(env: dict[str, str]) -> dict[str, str]:
    """Remove every known metered model route from a subscription child env."""
    for name in _PROVIDER_ROUTE_ENV_NAMES:
        env.pop(name, None)
    return env


def _apply_sponsored_kimi_route(
    env: dict[str, str],
    *,
    engine: str,
    model: str | None,
    force: bool = False,
) -> bool:
    """Bind Kimi to the isolated response-only service, never its provider key."""
    if not _is_kimi_model(model):
        return False
    engine = _sponsored_transport_engine(engine)
    if force:
        for name in (
            "KIMI_API_KEY",
            "KIMI_API_BASE",
            "KIMI_BASE_URL",
            "AGENT_MONITOR_SPONSORED_KIMI",
            "AGENT_MONITOR_SPONSORED_KIMI_URL",
        ):
            env.pop(name, None)
    if str(env.get("KIMI_API_KEY") or "").strip():
        return False

    from agent_monitor import sponsored_kimi_client

    if not sponsored_kimi_client.supports_engine(engine):
        raise ValueError(
            f"Server-sponsored Kimi K3 is not available with the {engine} harness; "
            "choose a compatible harness or add your own Kimi API key in Settings"
        )
    if not sponsored_kimi_client.configured():
        raise ValueError(
            "Kimi K3 requires your own API key, or the sponsored Kimi service "
            "must be available"
        )
    env["AGENT_MONITOR_SPONSORED_KIMI"] = "1"
    env["AGENT_MONITOR_SPONSORED_KIMI_URL"] = sponsored_kimi_client.endpoint()
    return True


def _sponsored_broker_serves(engine: str, model: str | None) -> bool:
    """Whether the healthy sponsored broker would carry this Kimi run."""
    if not _is_kimi_model(model):
        return False
    from agent_monitor import sponsored_kimi_client

    return sponsored_kimi_client.supports_engine(
        _sponsored_transport_engine(engine)
    ) and sponsored_kimi_client.configured()


def _check_cgm_problem_size(engine: str, text: str) -> None:
    """Refuse a problem the CGM pipeline would reject, before a run starts.

    ``math_harness_runner`` bounds the canonical ``problem.txt`` in UTF-8
    bytes; a character limit alone (the guest form) admits multibyte text
    that would otherwise fail only after a run slot and credential were spent.
    """
    if engine not in _CGM_ENGINES:
        return
    from agent_monitor.runners.math_harness_runner import MAX_PROBLEM_BYTES

    size = len(str(text or "").encode("utf-8"))
    if size > MAX_PROBLEM_BYTES:
        raise ValueError(
            f"The Ansatz CGM engines accept problems up to {MAX_PROBLEM_BYTES:,} "
            f"bytes of UTF-8 text (this one is {size:,} bytes); shorten the "
            "problem statement"
        )


def _analyze_model(
    user: dict[str, Any] | None,
    model: str | None,
    *,
    engine: str = _ANALYZE_ENGINE,
    auth_route: str | None = None,
) -> str:
    """The model of an Analyze run (or, with its ``engine``, a readability
    run): Kimi K3 for guests (sponsored only), the requested one (Kimi K3
    named in any case, or as ``kimi:kimi-k3``, as ``kimi-k3``:
    analyze_runs.manuscript_model), else Kimi K3 in public mode, a restricted
    member's default as account_policy.member_run_model resolves it, or the
    account's default for the run's route (:func:`_manuscript_default_model`);
    Kimi K3 when there is none.  A saved Max is passed over, and a saved Kimi
    model other than Kimi K3 stands for Kimi K3, the mode's only Kimi model
    (analyze_runs.kimi_model_error)."""
    from agent_monitor import analyze_runs
    from agent_monitor.settings import GUEST_DEFAULT_MODEL

    requested = analyze_runs.manuscript_model(model)
    if user is not None and user.get("guest"):
        if requested and requested != GUEST_DEFAULT_MODEL:
            raise ValueError(f"Guest analyses use {GUEST_DEFAULT_MODEL}")
        return GUEST_DEFAULT_MODEL
    if requested:
        return requested
    if account_policy.member_analyze_sponsored_only(user):
        # Public mode: the saved default would be the server's (see
        # account_policy.member_analyze_sponsored_only).
        return GUEST_DEFAULT_MODEL
    if account_policy.is_restricted(user):
        # As for a blank Plain model: the model the console's Analyze form
        # offers, not a saved default (a new account's Kimi K3) that the
        # member's keys cannot run.
        return analyze_runs.manuscript_model(account_policy.member_run_model(user, engine, None)) or GUEST_DEFAULT_MODEL
    if user is not None:
        return _manuscript_default_model(user, engine=engine, auth_route=auth_route) or GUEST_DEFAULT_MODEL
    return GUEST_DEFAULT_MODEL


def _readability_model(
    user: dict[str, Any] | None, model: str | None, auth_route: str | None
) -> str:
    """The model of a readability run, as an analysis's (:func:`_analyze_model`):
    Kimi K3 for guests (sponsored only), the requested one (any model the
    account's keys or linked account run), else Kimi K3 in public mode, a
    restricted member's default as account_policy.member_run_model resolves
    it, or the account's default for the run's route; Kimi K3 when there is
    none."""
    from agent_monitor import analyze_runs
    from agent_monitor.settings import GUEST_DEFAULT_MODEL

    requested = analyze_runs.manuscript_model(model)
    if user is not None and user.get("guest"):
        if requested and requested != GUEST_DEFAULT_MODEL:
            raise ValueError(f"Guest readability runs use {GUEST_DEFAULT_MODEL}")
        return GUEST_DEFAULT_MODEL
    return _analyze_model(user, requested or None, engine=_READABILITY_ENGINE, auth_route=auth_route)


# A linked account's routes, and the route of each account runtime.
_LINKED_ROUTES = frozenset({"codex_subscription", "claude_subscription"})
_RUNTIME_ROUTES = {"api": "api_key", "codex": "codex_subscription", "claude": "claude_subscription"}


def _manuscript_default_model(user: dict[str, Any], *, engine: str, auth_route: str | None) -> str | None:
    """The model of an administrator's (or a full-access account's) Analyze
    or Readability run that names none (an API caller's, or the Projects
    page's "Default account model").

    On a linked account's route (the requested one, or the account
    runtime's when none is) the saved default that route can run
    (:func:`_saved_default_model`), else the first model the account offers
    the mode (:func:`_first_linked_model`), as the console's model menu
    selects it; refused when it offers none.  On the API route the saved
    default where the account's keys or the sponsored service run it (a
    saved Kimi model other than Kimi K3 stands for Kimi K3, these modes'
    only Kimi model), else the first model of the account's API keys that
    runs, as :func:`_first_api_model` finds it (Kimi K3 first where the
    sponsored service runs it); when none runs, the saved default (Kimi K3
    when there is none), whose refusal says what is missing.  Never Max."""
    from agent_monitor import analyze_runs, kimi_k3
    from agent_monitor.settings import account_runtime, resolved_user_env

    env = resolved_user_env(user)
    route = str(auth_route or "").strip().lower() or _RUNTIME_ROUTES.get(account_runtime(user, env=env), "api_key")
    if route in _LINKED_ROUTES:
        model = analyze_runs.manuscript_model(
            _saved_default_model(user, engine=engine, auth_route=route)
        ) or _first_linked_model(user, engine=engine, route=route)
        if not model:
            account = "Codex" if route == "codex_subscription" else "Claude Code"
            noun = "readability run" if engine == _READABILITY_ENGINE else "analysis"
            raise ValueError(
                f"Choose a model for this {noun}: your linked {account} account offers none; "
                "connect it in Settings, or switch the account runtime"
            )
        return model
    saved = analyze_runs.manuscript_model(env.get("AGENT_MONITOR_MODEL"))
    if kimi_k3.is_other_kimi_model(saved):
        saved = kimi_k3.MODEL_ID
    if cgm_max.is_max_model(saved) or cgm_max.is_max_api_model(saved):
        saved = ""
    return _first_api_model(user, env, engine=engine, first=saved) or saved or None


def _first_api_model(user: dict[str, Any], env: dict[str, str], *, engine: str, first: str | None) -> str | None:
    """``first``, else the first of the account's API models (as Settings and
    the console's menu list them: Kimi K3 first where the sponsored service
    runs it, then each key's models), that ``user`` may run on ``engine``
    on the API route: one its keys run (``_api_keys_run``), or Kimi K3 where
    the sponsored service serves the engine for this account.  Never Max, a
    model the account may not run there (account_policy.member_model_error),
    nor a Kimi model other than Kimi K3, which these engines would run as
    Kimi K3 under its name.  None when none runs.  As
    account_policy.member_run_model resolves a restricted member's.  The
    list comes from the account's keys (``env``) and the sponsored
    service's health alone (settings.account_api_models): a start never
    probes a linked Claude Code or Codex account for it."""
    from agent_monitor import kimi_k3
    from agent_monitor.settings import account_api_models

    member = account_policy.is_member(user)

    def runs(candidate: object) -> bool:
        name = str(candidate or "").strip()
        if (
            not name
            or cgm_max.is_max_model(name)
            or cgm_max.is_max_api_model(name)
            or kimi_k3.is_other_kimi_model(name)
            or account_policy.member_model_error(user, engine, name)
        ):
            return False
        if _api_keys_run(env, name):
            return True
        return _sponsored_broker_serves(engine, name) and (
            not member or engine in account_policy.MEMBER_SPONSORED_ENGINES
        )

    if runs(first):
        return str(first).strip()
    return next((str(name) for name in account_api_models(user, env=env) if runs(name)), None)


def _first_linked_model(user: dict[str, Any], *, engine: str, route: str) -> str | None:
    """The first model a linked account's ``route`` offers ``engine``, in the
    order the console's model menu lists them (Claude Code's models, or the
    account's Codex models but its chat ones), or None."""
    from agent_monitor.engines_registry import supported_models

    if route == "claude_subscription":
        from agent_monitor.claude_login import DEFAULT_CLAUDE_MODELS

        models = list(DEFAULT_CLAUDE_MODELS)
    else:
        from agent_monitor import codex_login

        owner_id = user.get("id")
        models = [
            name for name in (codex_login.available_models(int(owner_id)) if owner_id is not None else [])
            if not re.search(r"(?:^|[:/])chat-latest$", str(name), re.IGNORECASE)
        ]
    allowed = supported_models(engine)
    return next((name for name in models if name and (not allowed or name in allowed)), None)


def _saved_default_model(
    user: dict[str, Any], *, engine: str, auth_route: str | None
) -> str | None:
    """An administrator's (or a full-access account's) saved default model,
    for a run that names none.

    Only a model the run's route can run (the requested route or, as
    ``_selected_auth_route`` picks it, the account runtime's): one of the
    linked Claude Code or Codex account that the engine supports, or on the
    API route one the account's keys can run (``_api_keys_run``), or Kimi K3
    where the sponsored service serves the engine.  Never Max, which is always
    a deliberate choice.  A saved Kimi model other than Kimi K3 runs as named
    only where a coding-agent harness sends it to the account's own Kimi
    key.  Where it would be served as Kimi K3 it stands for Kimi K3: on the
    sponsored route, and on an engine that runs Kimi K3 alone
    (engines_registry.KIMI_K3_ONLY_ENGINE_IDS), which otherwise takes the
    first model of the account's keys that runs (:func:`_first_api_model`)
    or, when none does, Kimi K3, whose refusal says what is missing: never a
    model the account cannot run.  Otherwise None, and the harness uses its
    own default.
    """
    from agent_monitor import kimi_k3
    from agent_monitor.engines_registry import KIMI_K3_ONLY_ENGINE_IDS, supported_models
    from agent_monitor.settings import account_runtime, resolved_user_env

    env = resolved_user_env(user)
    saved = str(env.get("AGENT_MONITOR_MODEL") or "").strip()
    if not saved or cgm_max.is_max_model(saved) or cgm_max.is_max_api_model(saved):
        return None
    route = str(auth_route or "").strip().lower() or _RUNTIME_ROUTES[account_runtime(user, env=env)]
    other_kimi = kimi_k3.is_other_kimi_model(saved)
    if route in {"claude_subscription", "codex_subscription"}:
        allowed = supported_models(engine)
        if other_kimi or (allowed and saved not in allowed):
            return None
        try:
            _validate_subscription_model(route=route, model=saved, user=user, field="Selected model")
        except ValueError:
            return None
        return saved
    if route != "api_key":
        return None
    if other_kimi:
        if engine in KIMI_K3_ONLY_ENGINE_IDS:
            return _first_api_model(user, env, engine=engine, first=kimi_k3.MODEL_ID) or kimi_k3.MODEL_ID
        if _api_keys_run(env, saved):
            # A coding-agent harness sends it as named to the account's own Kimi key.
            return saved
        # The sponsored route asks for Kimi K3 whatever is named.
        return kimi_k3.MODEL_ID if _sponsored_broker_serves(engine, saved) else None
    if _is_kimi_model(saved) and _sponsored_broker_serves(engine, saved):
        return saved
    return saved if _api_keys_run(env, saved) else None


def _api_keys_run(env: dict[str, str], model: str | None) -> bool:
    """Whether the API route can run ``model`` on the keys in ``env``.

    As the API runners resolve it: the model names its provider (a listed
    model, or a custom ID with the provider's prefix, as Settings allows) and
    that provider's key is set.  A subscription alias such as ``opus`` names
    no provider.
    """
    from agent_monitor.proof_graph import _resolve_llm_provider

    if not str(model or "").strip():
        return False
    try:
        _resolve_llm_provider(dict(env), model)
    except ValueError:
        return False
    return True


def _analyze_api_timeout(sponsored: bool, *, variable: str = "AGENT_MONITOR_ANALYZE_API_TIMEOUT") -> int:
    """The Analyze (or, with its ``variable``, Readability) runner's per-call
    API timeout (the runners read the same variables)."""
    from agent_monitor import kimi_k3

    default = kimi_k3.SPONSORED_TIMEOUT_SECONDS if sponsored else 300
    for name in (variable, "AGENT_MONITOR_CGM_API_TIMEOUT"):
        raw = str(os.environ.get(name) or "").strip()
        if raw:
            try:
                return max(60, min(int(raw), 900))
            except ValueError:
                break
    return default


def _apply_bounded_run_env(
    env: dict[str, str], *, engine: str, guest: bool, sponsored: bool
) -> None:
    """Pin the guest/sponsored budget profile in a run's child environment.

    Every guest run carries ``AGENT_MONITOR_GUEST=1`` (the CGM guest budget
    profile keys on it even when a hosted guest key, not the broker, is used).
    Guest and sponsored CGM runs get exactly the bounded output ceiling. Call
    this after every user-derived overlay so neither can be relaxed.
    """
    if guest:
        env["AGENT_MONITOR_GUEST"] = "1"
    if engine in _BOUNDED_OUTPUT_ENGINES and (guest or sponsored):
        env["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] = str(_CGM_BOUNDED_OUTPUT_TOKENS)


def _service_switch(name: str) -> bool:
    """A console switch that is on unless set to 0/false/off/no."""
    return str(os.environ.get(name, "1")).strip().lower() not in {"0", "false", "off", "no"}


def _apply_cgm_writeup_env(env: dict[str, str], *, engine: str) -> None:
    """Switch an Ansatz CGM run's written-proof round on or off.

    The round (math_harness_runner ``_write_up``) follows the console's
    ``AGENT_MONITOR_CGM_WRITEUP`` (default on) for every CGM run, Max
    included; guest and sponsored runs (as the runner recognizes them) also
    need ``AGENT_MONITOR_CGM_WRITEUP_GUESTS`` (default on).  The runner reads
    only ``AGENT_MONITOR_CGM_WRITEUP``, so this decides it for the child.
    """
    if engine not in _CGM_ENGINES:
        return
    bounded = any(
        env.get(name) == "1"
        for name in (
            "AGENT_MONITOR_GUEST",
            "AGENT_MONITOR_PUBLIC_KIMI",
            "AGENT_MONITOR_SPONSORED_KIMI",
        )
    )
    enabled = _service_switch("AGENT_MONITOR_CGM_WRITEUP") and (
        not bounded or _service_switch("AGENT_MONITOR_CGM_WRITEUP_GUESTS")
    )
    env["AGENT_MONITOR_CGM_WRITEUP"] = "1" if enabled else "0"


def _cgm_operator_timeout_seconds(engine: str, env: dict[str, str]) -> int | None:
    """The operator's timeout (``AGENT_MONITOR_<ENGINE>_TIMEOUT`` or
    ``AGENT_MONITOR_CLI_TIMEOUT``) that bounds a CGM run up to its
    publication; the written-proof round after it has its own allowance on
    top (_cli_timeout_seconds).  ``None`` without one, and for Max, which
    operator timeouts never cut short."""
    normalized = str(engine or "").strip().upper().replace("-", "_")
    if normalized not in {"MATH_HARNESS", "KIMI"} or cgm_max.route_mode(env) is not None:
        return None
    override = env.get(f"AGENT_MONITOR_{normalized}_TIMEOUT") or env.get("AGENT_MONITOR_CLI_TIMEOUT")
    try:
        return max(60, min(int(override), 86_400)) if override else None
    except (TypeError, ValueError):
        return None


def _cgm_writeup_allowance(env: dict[str, str]) -> int:
    """Seconds a CGM run's written-proof round may add after its pipeline."""
    try:
        from agent_monitor.runners import math_harness_runner

        return math_harness_runner.writeup_allowance_seconds(env)
    except (ImportError, ValueError, RuntimeError):
        # An invalid write-up setting makes the runner skip the round.
        return 0


class StopRequested(Exception):
    """Raised inside a run when the user pressed Stop."""


def _augment_research_audit_prompt(prompt: str) -> str:
    """Add the strict output contract only to explicit candidate-audit runs."""
    if (
        _RESEARCH_AUDIT_GATE_MARKER not in prompt
        or _RESEARCH_AUDIT_RUNTIME_MARKER in prompt
    ):
        return prompt
    return f"{prompt.rstrip()}\n\n{_RESEARCH_AUDIT_RUNTIME_CONTRACT}"


def _research_audit_artifacts_ready(workspace: Path) -> bool:
    """Return true only when one audit directory has the required nonempty files."""
    audit_root = workspace / "audit"
    if not audit_root.is_dir():
        return False
    for directory in audit_root.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        if all(
            (directory / name).is_file()
            and not (directory / name).is_symlink()
            and (directory / name).stat().st_size > 0
            for name in _RESEARCH_AUDIT_REQUIRED_FILES
        ) and reconciliation_complete(workspace, directory):
            return True
    return False


def _hermes_audit_last_mile_needed(problem_text: str, workspace: Path) -> bool:
    """Require one follow-up only for explicit audits with a substantive draft."""
    if _RESEARCH_AUDIT_GATE_MARKER not in problem_text:
        return False
    proof = workspace / "proof.md"
    try:
        substantive = (
            proof.is_file()
            and not proof.is_symlink()
            and proof.stat().st_size >= 400
        )
    except OSError:
        substantive = False
    return substantive and not _research_audit_artifacts_ready(workspace)


def _merge_hermes_turn_results(
    initial: dict[str, Any] | None, followup: dict[str, Any] | None
) -> dict[str, Any]:
    """Keep follow-up output while preserving per-turn usage and completion state."""
    first = dict(initial or {})
    second = dict(followup or {})
    merged = {**first, **second}
    for key in (
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "completion_tokens",
        "api_calls",
        "tool_call_count",
    ):
        values = [value for value in (first.get(key), second.get(key)) if isinstance(value, (int, float))]
        if values:
            merged[key] = sum(values)
    merged["completed"] = bool(first.get("completed", True)) and bool(
        second.get("completed", True)
    )
    merged["audit_last_mile"] = True
    return merged


def _stop_event(run_id: str) -> threading.Event:
    with _LOCK:
        ev = _STOP_EVENTS.get(run_id)
        if ev is None:
            ev = threading.Event()
            _STOP_EVENTS[run_id] = ev
        return ev


def _run_deleted(run_id: str) -> bool:
    """Whether ``run_id`` was deleted while this process ran (delete_run)."""
    with _LOCK:
        return run_id in _DELETED_RUNS


def _terminate_registered_proc(proc: Any, *, force: bool = True) -> None:
    """Terminate one registered runner and its isolated descendant group."""
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        pid = int(proc.pid)
        from agent_monitor.process_control import terminate_descendants

        terminate_descendants(pid, force=force)
        pgid = os.getpgid(pid)
        # Registered runners should be session leaders. Refuse to signal a
        # shared service group if an older/custom runner violates that contract.
        if pgid == pid:
            os.killpg(pgid, sig)
            return
    except (AttributeError, OSError, ProcessLookupError, TypeError, ValueError):
        pass
    try:
        proc.kill() if force else proc.terminate()
    except OSError:
        pass


def _stop_cgm_runner(proc: Any) -> None:
    """SIGTERM an Ansatz CGM runner, then the processes of its model calls.

    The runner takes the request first (its handler only sets an event), so
    a call the stop cuts short never reads to it as a provider failure to
    carry on from (publishing without its curator, say).  Those processes
    (Claude Code, Codex) run in sessions of their own, and the runner exits
    as soon as its coordinator next looks, without waiting for them: they
    are listed through it before it is signalled.  The runner gets this one
    SIGTERM: a second one after its publication could end it before its
    report (see stop_run).
    """
    from agent_monitor.process_control import terminate_descendants

    try:
        pid = int(proc.pid)
    except (AttributeError, TypeError, ValueError):
        return

    def signal_runner() -> None:
        try:
            # As _terminate_registered_proc: only a runner's own group.
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
            else:
                proc.terminate()
        except OSError:
            pass

    terminate_descendants(pid, force=False, signal_wrapper=signal_runner)


def _register_proc(run_id: str, proc: Any) -> None:
    with _LOCK:
        _PROCS[run_id] = proc
        already_stopped = bool(
            _STOP_EVENTS.get(run_id) and _STOP_EVENTS[run_id].is_set()
        )
    # Stop may arrive in the narrow window after Popen and before registration.
    # Honor it immediately and kill the whole isolated group, not only the
    # wrapper process (which can otherwise orphan Codex or another child CLI).
    if already_stopped:
        _terminate_registered_proc(proc)


def _unregister_proc(run_id: str, proc: Any = None) -> None:
    """Forget the process registered for ``run_id``: with ``proc``, only if
    it is still that one (the watcher of a stopped re-check or question may
    exit after the next one has registered its own)."""
    with _LOCK:
        if proc is None or _PROCS.get(run_id) is proc:
            _PROCS.pop(run_id, None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_dir() -> Path:
    d = CACHE_DIR / "harness"
    d.mkdir(parents=True, exist_ok=True)
    return d


def workspace_dir(run_id: str) -> Path:
    """Per-run sandbox where engines write .tex / artifacts."""
    d = RUNS_DIR / "workspaces" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_text(path: Path, content: str) -> None:
    # Publish a complete file without exposing a truncated reader view.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_run(run: dict[str, Any]) -> Path:
    # Heartbeats, continuations, and reconciliation can update one record.
    # Serialize the merge and atomically publish both copies.
    with _RUN_WRITE_LOCK:
        return _write_run_locked(run)


def _merge_continuation_run(
    previous: dict[str, Any], current: dict[str, Any], *, job_id: str
) -> dict[str, Any]:
    """Preserve the full monitor/usage lineage when a follow-up finishes."""
    result = dict(current)
    previous_agents = [
        dict(agent) for agent in previous.get("agents") or []
        if isinstance(agent, dict)
    ]
    current_agents = [
        dict(agent) for agent in current.get("agents") or []
        if isinstance(agent, dict)
    ]
    continuation_number = max(0, int(previous.get("continuation_count") or 0)) + 1
    suffix = f"::continue-{job_id}"

    previous_rounds = [agent.get("round_id") for agent in previous_agents]
    previous_rounds = [value for value in previous_rounds if isinstance(value, int)]
    current_rounds = [agent.get("round_id") for agent in current_agents]
    current_rounds = [value for value in current_rounds if isinstance(value, int)]
    round_offset = 0
    if previous_rounds and current_rounds:
        round_offset = max(previous_rounds) + 1 - min(current_rounds)

    id_map: dict[str, str] = {}
    for index, agent in enumerate(current_agents, 1):
        found_identifier = False
        for key in ("trace_id", "id"):
            old = str(agent.get(key) or "")
            if not old:
                continue
            found_identifier = True
            new = f"{old}{suffix}"
            id_map[old] = new
            agent[key] = new
        if not found_identifier:
            run_id = str(current.get("run_id") or previous.get("run_id") or "run")
            agent["trace_id"] = f"{run_id}{suffix}::node-{index}"
        if isinstance(agent.get("round_id"), int):
            agent["round_id"] += round_offset
        stage_name = str(agent.get("stage_name") or "").strip()
        if stage_name:
            agent["stage_name"] = f"{stage_name} · continuation {continuation_number}"
        agent["continuation_job_id"] = job_id

    previous_edges = [
        dict(edge) for edge in previous.get("edges") or []
        if isinstance(edge, dict)
    ]
    current_edges: list[dict[str, Any]] = []
    for edge in current.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        rewritten = dict(edge)
        for key in ("from", "to"):
            old = str(rewritten.get(key) or "")
            if old in id_map:
                rewritten[key] = id_map[old]
        current_edges.append(rewritten)

    def node_id(agent: dict[str, Any]) -> str:
        return str(agent.get("trace_id") or agent.get("id") or "")

    bridge: list[dict[str, Any]] = []
    if previous_agents and current_agents:
        source = node_id(previous_agents[-1])
        target = node_id(current_agents[0])
        if source and target:
            bridge.append({"from": source, "to": target, "type": "continue"})

    totals: dict[str, Any] = {}
    old_totals = previous.get("totals") or {}
    new_totals = current.get("totals") or {}
    for key in (
        "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens",
        "reasoning_tokens", "cost_usd", "latency_s",
    ):
        values = [
            value for value in (old_totals.get(key), new_totals.get(key))
            if isinstance(value, (int, float))
        ]
        if values:
            totals[key] = sum(values)
    totals["agents"] = len(previous_agents) + len(current_agents)

    def lineage_item(
        run: dict[str, Any], *, kind: str, fallback_job: str = ""
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "job_id": str(run.get("job_id") or fallback_job),
            "status": str(run.get("status") or "unknown"),
            "updated_at": run.get("updated_at"),
            "agents": len(run.get("agents") or []),
            "totals": dict(run.get("totals") or {}),
        }

    lineage = [
        dict(item) for item in previous.get("session_lineage") or []
        if isinstance(item, dict)
    ]
    if not lineage:
        lineage.append(lineage_item(previous, kind="initial"))
    lineage.append(lineage_item(current, kind="continue", fallback_job=job_id))

    result["agents"] = previous_agents + current_agents
    result["edges"] = previous_edges + bridge + current_edges
    result["totals"] = totals
    result["session_lineage"] = lineage
    result["continuation_count"] = continuation_number
    if previous.get("created_at"):
        result["created_at"] = previous["created_at"]
    previous_route = _route_from_record(previous)
    current_route = _route_from_record(current)
    if previous_route:
        # A continuation is part of the same run and must never change the
        # account/API route selected when that run started.
        result["auth_route"] = previous_route
        result.setdefault("live", {})["auth_mode"] = _live_auth_mode(previous_route)
    elif current_route:
        result["auth_route"] = current_route
    return result


# The fields a record write keeps from the record already written when the
# new copy lacks them (and, for a deleted run, from what it was deleted with).
_RECORD_IDENTITY_KEYS = (
    "owner_id", "owner", "created_at", "problem_id",
    "guest", "use_subagents", "subagent_model", "auth_route", "credential_source",
    "model", "requested_model", "reasoning_effort", "speed_mode", "max_iterations", "formalize",
    "route_plan", "run_kind", "analyze", "readability", "tool_free",
    "engine_impl", "engine_impl_version", "artifact_schema", "graph_schema",
)


def _write_run_locked(run: dict[str, Any]) -> Path:
    cache = _cache_dir()
    path = cache / f"{run['run_id']}.json"
    deleted = _run_deleted(str(run["run_id"]))
    old: dict[str, Any] = {}
    if deleted:
        # Its record went with it: the fields it was deleted with stand in.
        old = dict(_DELETED_RUN_IDENTITY.get(str(run["run_id"])) or {})
    elif path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old = {}
    for k in _RECORD_IDENTITY_KEYS:
        if run.get(k) in (None, "", "continue") and old.get(k) not in (None, "", "continue"):
            run[k] = old[k]
    if str(run.get("owner") or "").strip().lower().endswith("@public.local"):
        # Legacy guest runs predate the explicit flag; infer it from the owner.
        run["guest"] = True
    if not run.get("problems"):
        run["problems"] = list(old.get("problems") or [])
    incoming_text = str(run.get("problem_text") or "")
    old_text = str(old.get("problem_text") or "")
    if _is_engine_prompt(incoming_text):
        if old_text and not _is_engine_prompt(old_text):
            run["problem_text"] = old_text
        elif run.get("problems"):
            run["problem_text"] = _problem_item_text(run["problems"][0])
    incoming_prev = str(run.get("problem_text_preview") or "")
    old_prev = str(old.get("problem_text_preview") or "")
    if not incoming_prev or _is_engine_prompt(incoming_prev):
        if old_prev and not _is_engine_prompt(old_prev):
            run["problem_text_preview"] = old_prev
    title = _session_title(run.get("problems"), run.get("problem_text_preview") or run.get("problem_text") or "")
    if title and not _is_engine_prompt(title):
        run["problem_text_preview"] = title[:500]
    if deleted:
        # A worker still winding down (a heartbeat, its final record): the
        # run stays deleted, so neither record file nor its manifest entry
        # comes back.  Only the usage it reports is booked, as usage
        # outlives the run (see delete_run).
        with _LOCK:
            if str(run["run_id"]) in _DELETED_RUN_LAST:
                _DELETED_RUN_LAST[str(run["run_id"])] = run
        from agent_monitor import usage_ledger

        usage_ledger.record_run(run)
        return path
    serialized = json.dumps(run, ensure_ascii=False, indent=2)
    _atomic_write_text(path, serialized)
    runs_path = RUNS_DIR / f"{run['run_id']}.json"
    _atomic_write_text(runs_path, serialized)
    _upsert_manifest(run)
    from agent_monitor import usage_ledger

    usage_ledger.record_run(run)
    return path


def _problem_preview(text: str | None) -> str:
    """Short single-line problem title for run lists (first non-empty line)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:140]
    return ""


def _is_engine_prompt(text: str | None) -> bool:
    t = (text or "").lstrip()
    if not t:
        return False
    return (
        t.startswith("You are continuing")
        or "ORIGINAL PROBLEM:" in t[:800]
        or "HUMAN FEEDBACK — address this" in t
        or "SESSION PROBLEMS:" in t[:1200]
    )


def _problem_item_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("text") or "").strip()
    return str(item or "").strip()


def _session_title(problems: Any, fallback: str = "") -> str:
    texts = [
        _problem_preview(_problem_item_text(p))
        for p in (problems or [])
        if not (
            isinstance(p, dict)
            and p.get("source") in {"human", "hitl", "feedback"}
        )
    ]
    texts = [t for t in texts if t and not _is_engine_prompt(t)]
    if not texts:
        fb = _problem_preview(fallback)
        return "" if _is_engine_prompt(fb) else fb
    if len(texts) == 1:
        return texts[0]
    return f"{texts[0]} · +{len(texts) - 1}"


def _seed_problems(text: str | None, *, source: str = "initial") -> list[dict[str, Any]]:
    t = (text or "").strip()
    if not t or _is_engine_prompt(t):
        return []
    return [{"text": t, "source": source, "ts": _now()}]


def _write_session_problems(ws: Path, problems: list[Any]) -> None:
    """Keep problem.txt as the full session notebook the agent should honor."""
    chunks: list[str] = []
    serial: list[dict[str, Any]] = []
    for i, item in enumerate(problems or [], 1):
        text = _problem_item_text(item)
        if not text:
            continue
        src = item.get("source") if isinstance(item, dict) else "initial"
        serial.append({"text": text, "source": src or "human", "n": i})
        chunks.append(f"## Problem {i}\n{text}\n")
    if not chunks:
        return
    try:
        (ws / "problem.txt").write_text("\n".join(chunks).strip() + "\n", encoding="utf-8")
        (ws / "problems.json").write_text(json.dumps(serial, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def ensure_problems(run: dict[str, Any], run_id: str | None = None) -> list[dict[str, Any]]:
    """Return the session problem list, backfilling from chat if needed."""
    existing = [
        {"text": _problem_item_text(p), "source": (p.get("source") if isinstance(p, dict) else "initial") or "initial"}
        for p in (run.get("problems") or [])
        if _problem_item_text(p)
    ]
    if existing:
        return existing
    original = str(run.get("problem_text") or run.get("problem_text_preview") or "")
    if _is_engine_prompt(original):
        original = ""
        blob = str(run.get("problem_text") or "")
        if "ORIGINAL PROBLEM:" in blob:
            original = blob.split("ORIGINAL PROBLEM:", 1)[1]
            for sep in ("\nCURRENT ", "\nHUMAN FEEDBACK", "\nSESSION PROBLEMS"):
                if sep in original:
                    original = original.split(sep, 1)[0]
            original = original.strip()
    problems = _seed_problems(original, source="initial")
    rid = run_id or run.get("run_id")
    if rid:
        seen = {p["text"] for p in problems}
        for msg in list_chat(str(rid)):
            if msg.get("role") != "user":
                continue
            text = str(msg.get("content") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            problems.append({"text": text, "source": "human", "ts": msg.get("ts") or _now()})
    return problems


def _problems_with_recorded(problems: list[Any], run_id: str, *, own: str | None = None) -> list[Any]:
    """``problems`` followed by the record's entries it lacks, in their order.

    A follow-up keeps the problem list it started with, while feedback sent
    during it is appended to the record (``send_human_message`` queues it for
    the next turn).  The follow-up holds ``_RUN_WRITE_LOCK`` from this read
    through its write; ``send_human_message`` and the stop and failure
    handlers read and write under the same lock, so no write drops another's
    change.
    """
    try:
        record = _load_run_record(run_id)
    except (OSError, ValueError):  # missing or unreadable: nothing to keep
        record = {}
    recorded = record.get("problems") if isinstance(record, dict) else None
    merged = list(problems)
    if isinstance(recorded, list) and any(_problem_item_text(item) for item in recorded):
        # What the record lost since this follow-up read it (a Stop dropped
        # queued feedback, _record_stopped) stays lost; only this turn's own
        # message, not yet written, is kept.
        kept = {_problem_item_text(item) for item in recorded}
        merged = [
            item for item in merged
            if _problem_item_text(item) in kept or (own and _problem_item_text(item) == own)
        ]
    seen = {_problem_item_text(item) for item in merged}
    for item in recorded if isinstance(recorded, list) else []:
        text = _problem_item_text(item)
        if text and text not in seen:
            seen.add(text)
            merged.append(item)
    return merged


def _upsert_manifest(run: dict[str, Any]) -> None:
    cache = _cache_dir()
    manifest_path = cache / "manifest.json"
    entries: list[dict] = []
    if manifest_path.exists():
        try:
            entries = list((json.loads(manifest_path.read_text(encoding="utf-8")).get("runs") or []))
        except (json.JSONDecodeError, OSError):
            entries = []
    totals = run.get("totals") or {}
    entry = {
        "run_id": run["run_id"],
        "engine": run.get("engine"),
        "trace_name": run.get("trace_name"),
        "source": run.get("source") or run.get("engine"),
        "status": run.get("status", "running"),
        "agent_count": len(run.get("agents") or []),
        "total_cost_usd": totals.get("cost_usd"),
        "problem_id": run.get("problem_id"),
        "problem_preview": _session_title(run.get("problems"), run.get("problem_text_preview") or run.get("problem_text") or ""),
        "last_ts": run.get("updated_at") or _now(),
        "owner_id": run.get("owner_id"),
        "guest": bool(run.get("guest")),
        "run_kind": str(run.get("run_kind") or "prove"),
    }
    if isinstance(run.get("analyze"), dict):
        # Lets a restart find re-checks whose worker it lost (the analysis
        # itself is no longer "running").
        recheck = run["analyze"].get("recheck")
        entry["recheck_running"] = isinstance(recheck, dict) and recheck.get("status") == "running"
    blocks = [run[name] for name in ASK_BLOCKS if isinstance(run.get(name), dict)]
    if blocks:
        # Likewise for a question about a readability run or an analysis.
        entry["ask_running"] = any(isinstance(block.get("ask"), dict) and block["ask"].get("status") == "running"
                                   for block in blocks)
    if not entry["problem_preview"]:
        # Runner flushes don't carry problem_text_preview — keep the stored one.
        entry.pop("problem_preview")
    by_id = {e.get("run_id"): e for e in entries if e.get("run_id")}
    by_id[entry["run_id"]] = {**(by_id.get(entry["run_id"]) or {}), **entry}
    payload = {"built_at": _now(), "runs": list(by_id.values())}
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _pipeline_for(engine: str) -> list[dict]:
    if engine == _READABILITY_ENGINE:
        # The Readability runner reuses the CGM stage ids (cli_events trusts them).
        return [
            {"id": "scaffold", "label": "1 Ingest", "title": "Fetch, read and split the manuscript"},
            {"id": "work", "label": "2 Guide", "title": "Reading guide: summary, roadmap, notation"},
            {"id": "verify", "label": "3 Rewrite", "title": "Readable version, one part at a time"},
            {"id": "curate", "label": "4 Check", "title": "Independent check of each part"},
            {"id": "finalize", "label": "5 Publish", "title": "Readable version, paper map and notation"},
        ]
    if engine == _ANALYZE_ENGINE:
        # The Analyze runner reuses the CGM stage ids (cli_events trusts them).
        return [
            {"id": "scaffold", "label": "1 Ingest", "title": "Fetch or unpack the manuscript"},
            {"id": "work", "label": "2 Segment", "title": "Statement DAG from the manuscript"},
            {"id": "verify", "label": "3 Check", "title": "Model verifier, one result at a time"},
            {"id": "curate", "label": "4 Roll-up", "title": "Verdicts with dependencies"},
            {"id": "finalize", "label": "5 Report", "title": "Report and manuscript DAG"},
        ]
    if engine in _CGM_ENGINES:
        return [
            {"id": "scaffold", "label": "1 Scaffold", "title": "Project setup"},
            {"id": "work", "label": "2 Explore", "title": "Independent proof search"},
            {"id": "verify", "label": "3 Verify", "title": "Fact verification"},
            {"id": "curate", "label": "4 Curate", "title": "Exploring Graph"},
            {"id": "finalize", "label": "5 Synthesize", "title": "Final proof"},
        ]
    if engine == "ucla":
        return [
            {"id": "literature", "label": "1 Literature", "title": "Literature"},
            {"id": "advisor", "label": "2 Advisor", "title": "Advisor"},
            {"id": "solvers", "label": "3 Solvers", "title": "Solvers"},
            {"id": "verify", "label": "4 Verify", "title": "Verify"},
            {"id": "finalize", "label": "5 Finalize", "title": "Finalize"},
        ]
    if engine == "improof":
        return [
            {"id": "workflow", "label": "Workflow", "title": "Workflow"},
            {"id": "author", "label": "Author", "title": "Author"},
            {"id": "critic", "label": "Critic", "title": "Critic"},
            {"id": "council", "label": "Council", "title": "Council"},
            {"id": "finalize", "label": "Finalize", "title": "Finalize"},
        ]
    if engine == "hermes":
        return [
            {"id": "understand", "label": "Understand", "title": "Understand"},
            {"id": "plan", "label": "Plan", "title": "Plan"},
            {"id": "draft", "label": "Draft", "title": "Draft"},
            {"id": "verify", "label": "Verify", "title": "Verify"},
            {"id": "finalize", "label": "Finalize", "title": "Finalize"},
        ]
    # external CLI harnesses — per-model-call trace columns
    return [
        {"id": "plan", "label": "Reason", "title": "Reasoning"},
        {"id": "act", "label": "Act", "title": "Tools / commands"},
        {"id": "write", "label": "Write", "title": "Proof writing"},
        {"id": "work", "label": "Agent Session", "title": "Agent working"},
        {"id": "finalize", "label": "Finalize", "title": "Finalize"},
    ]


def list_jobs(owner_id: int | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        jobs = [dict(j) for j in _JOBS.values() if j.get("run_id") not in _DELETED_RUNS]
        for j in jobs:
            worker = _WORKERS.get(str(j.get("job_id") or ""))
            if j.get("status") != "running" and worker is not None and _worker_not_done(worker[1]):
                # Stopped while its worker still winds down: the run list
                # offers no Continue yet (see run_is_stopping).
                j["stopping"] = True
    if owner_id is not None:
        jobs = [j for j in jobs if j.get("owner_id") == owner_id]
    for j in jobs:
        if j.get("problem_preview"):
            continue
        try:
            rec = _load_run_record(j["run_id"])
            j["problem_preview"] = _session_title(
                rec.get("problems"), rec.get("problem_text_preview") or rec.get("problem_text") or ""
            )
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
    return jobs


def persisted_run_count(owner_id: int) -> int:
    """Count retained run records for one owner without double-counting copies."""
    cache = _cache_dir()
    with _RUN_WRITE_LOCK:
        manifest_path = cache / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("manifest root is not an object")
            raw_entries = payload.get("runs") or []
            if not isinstance(raw_entries, list):
                raise TypeError("manifest runs is not a list")
            return sum(
                isinstance(entry, dict) and entry.get("owner_id") == owner_id
                for entry in raw_entries
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            count = 0
            for path in cache.glob("*.json"):
                if path.name == "manifest.json":
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if isinstance(record, dict) and record.get("owner_id") == owner_id:
                    count += 1
            return count


def run_owner(run_id: str) -> int | None:
    """Owner user id recorded on a run (None for legacy/ownerless runs)."""
    try:
        return _load_run_record(run_id).get("owner_id")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        j = _JOBS.get(job_id)
        return dict(j) if j else None


def _validated_problem_id(problem_id: str) -> str:
    """Return a filesystem-safe ID suitable for run and cache filenames."""
    value = str(problem_id or "").strip()
    if value in {".", ".."} or not _PROBLEM_ID_RE.fullmatch(value):
        raise ValueError("problem_id contains unsupported characters")
    return value


def _manifest_problem_path(statement_path: object) -> Path:
    """Resolve a manifest entry without permitting escape from PROBLEMS_DIR."""
    root = PROBLEMS_DIR.resolve()
    candidate = (root / str(statement_path or "")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("problem manifest path leaves the problem directory") from None
    if not candidate.is_file():
        raise FileNotFoundError(f"Problem statement is missing: {statement_path}")
    return candidate


def resolve_problem(problem_id: str | None, problem_text: str | None) -> tuple[str, str, Path | None]:
    """Return (problem_id, text, path_or_none)."""
    if problem_text and problem_text.strip():
        pid = (
            _validated_problem_id(problem_id)
            if problem_id
            else f"adhoc_{uuid.uuid4().hex[:8]}"
        )
        return pid, problem_text.strip(), None

    if not problem_id:
        raise ValueError("Provide problem_id or problem_text")
    safe_problem_id = _validated_problem_id(problem_id)

    man = PROBLEMS_DIR / "manifest.json"
    if man.exists():
        for p in json.loads(man.read_text(encoding="utf-8")).get("problems") or []:
            if isinstance(p, dict) and p.get("problem_id") == safe_problem_id:
                path = _manifest_problem_path(p.get("statement_path"))
                return (
                    safe_problem_id,
                    path.read_text(encoding="utf-8", errors="replace"),
                    path,
                )

    # Direct statement file under problems/. The validated ID cannot contain a
    # separator or traversal segment, so this stays inside the problem root.
    cand = PROBLEMS_DIR / safe_problem_id
    if cand.is_file():
        return cand.stem, cand.read_text(encoding="utf-8", errors="replace"), cand

    raise FileNotFoundError(f"Unknown problem: {safe_problem_id}")


def _is_kimi_model(model: str | None) -> bool:
    """Return whether the UI selection requires the Kimi API connection."""
    return str(model or "").strip().lower().startswith("kimi-")


def _engine_label(engine: str) -> str:
    """An engine's name as the console shows it ("Plain (no harness)")."""
    from agent_monitor.engines_registry import BUILTIN_ENGINES, CLI_ENGINES

    spec = CLI_ENGINES.get(engine) or next((item for item in BUILTIN_ENGINES if item["id"] == engine), {})
    return str(spec.get("label") or engine)


# Engines of engines_registry.KIMI_K3_ONLY_ENGINE_IDS that run another Kimi
# model name as a model that is no Kimi model (DeepSeek's, IMProof's default
# workflow) rather than as Kimi K3.
_OTHER_KIMI_NAME_RUNS_NO_KIMI = frozenset({"deepseek_harness", "improof"})


def _other_kimi_model_error(
    engine: str, kind: str, model: str | None, *, ran: bool = False, sponsored: bool = False
) -> str | None:
    """Why a run of ``engine`` (``kind``) may not use ``model``, a Kimi model
    other than Kimi K3 (``kimi_k3.is_other_kimi_model``), or None.

    Only where the run would be served as Kimi K3 whatever Kimi model it
    names, or as no Kimi model at all, so that its record, receipt and usage
    would name a model it never ran on: Analyze and Readability (worded for
    their mode, analyze_runs.kimi_model_error), the other engines of
    engines_registry.KIMI_K3_ONLY_ENGINE_IDS on any route, and any engine on
    the sponsored route (``sponsored``: that service asks for Kimi K3
    whatever is named).  A coding-agent harness (Codex CLI, DeepAgents,
    OpenHands, OpenClaw, OpenClaude, Hermes) sends the name it is given to an
    account's own Kimi endpoint, where another Kimi model runs as named.
    ``ran``: the model of an existing run (a follow-up of it), which the
    refusal names."""
    from agent_monitor import analyze_runs, kimi_k3
    from agent_monitor.engines_registry import KIMI_K3_ONLY_ENGINE_IDS

    if kind in {"analyze", "readability"}:
        return analyze_runs.kimi_model_error(kind, model, ran=ran)
    if not kimi_k3.is_other_kimi_model(model):
        return None
    if engine in KIMI_K3_ONLY_ENGINE_IDS:
        verb = (
            "runs Kimi K3 as its only Kimi model"
            if engine in _OTHER_KIMI_NAME_RUNS_NO_KIMI
            else "calls Kimi K3 for every Kimi model name"
        )
        reason, hint = f"{_engine_label(engine)} {verb}", ""
    elif sponsored:
        reason = "The sponsored Kimi service calls Kimi K3 for every Kimi model name"
        hint = ", or add your own Kimi API key in Settings"
    else:
        return None
    if ran:
        return (f"{reason}; this run ran on {str(model).strip()}, so it cannot continue: "
                f"start a new run on {kimi_k3.MODEL_ID}")
    return f"{reason}; choose {kimi_k3.MODEL_ID}{hint}"


def _route_from_record(run: dict[str, Any] | None) -> str | None:
    """Recover the immutable billing/runtime route recorded for a run."""
    record = run or {}
    route = str(record.get("auth_route") or "").strip().lower()
    if route in _AUTH_ROUTES:
        return route
    live_mode = str((record.get("live") or {}).get("auth_mode") or "").strip().lower()
    return {
        "api_key": "api_key",
        "chatgpt_subscription": "codex_subscription",
        "codex_subscription": "codex_subscription",
        "claude_subscription": "claude_subscription",
        cgm_max.ROUTE: cgm_max.ROUTE,
    }.get(live_mode)


def _live_auth_mode(route: str) -> str:
    """Return the public live-state label for one immutable run route."""
    return {
        "api_key": "api_key",
        "codex_subscription": "chatgpt_subscription",
        "claude_subscription": "claude_subscription",
        cgm_max.ROUTE: cgm_max.ROUTE,
    }[route]


def _configure_auth_route(
    env: dict[str, str],
    *,
    route: str,
    user_id: int | None,
) -> dict[str, str]:
    """Apply exactly one account/API route to a child-process environment."""
    from agent_monitor.subprocess_env import scrub_control_plane_env, server_elan_home

    if route not in _AUTH_ROUTES:
        raise ValueError(f"Unsupported authentication route: {route}")
    configured = dict(env)
    configured.pop("AGENT_MONITOR_CODEX_SUBSCRIPTION", None)
    configured.pop("AGENT_MONITOR_CLAUDE_SUBSCRIPTION", None)
    configured.pop("AGENT_MONITOR_CODEX_MODEL", None)
    configured.pop("AGENT_MONITOR_CLAUDE_MODEL", None)
    configured.pop("CODEX_HOME", None)
    configured.pop("CLAUDE_CONFIG_DIR", None)
    configured.pop(cgm_max.ROUTE_MODE_ENV, None)
    configured.pop(cgm_max.CODEX_CALL_HOME_ENV, None)
    runtime = {
        "api_key": "api",
        "codex_subscription": "codex",
        "claude_subscription": "claude",
        cgm_max.ROUTE: "max",
    }[route]
    configured["AGENT_MONITOR_ACCOUNT_RUNTIME"] = runtime
    configured["AGENT_MONITOR_USE_CODEX"] = "1" if runtime in {"codex", "max"} else "0"
    configured["AGENT_MONITOR_USE_CLAUDE"] = "1" if runtime in {"claude", "max"} else "0"

    if route == "api_key":
        configured["AGENT_MONITOR_AUTH_MODE"] = "api_key"
        scrub_control_plane_env(configured)
        return configured
    _scrub_provider_route_env(configured)
    if user_id is None:
        raise ValueError("A signed-in ProvingConsole account is required for subscription mode")
    from agent_monitor.auth import may_use_own_subscriptions

    if not may_use_own_subscriptions(user_id):
        # Subscriptions and Max are the administrator's, and those of accounts
        # given full access (each on its own linked accounts), whichever path
        # (run, continuation, DAG or Formal work) asks: the stored role decides,
        # so a sign-in a member linked before that policy is never used.
        raise ValueError(
            account_policy.member_route_error({"id": user_id}, route)
            or "Account subscriptions are limited to the site administrator"
        )

    if route == cgm_max.ROUTE:
        from agent_monitor import claude_login, codex_login

        codex_home = codex_login.account_home(int(user_id))
        if not codex_login.account_login_ready(codex_home):
            raise ValueError("Codex is not connected for this account; connect it in Settings")
        claude_home = claude_login.account_home(int(user_id))
        if not claude_login.account_login_ready(claude_home):
            raise ValueError("Claude Code is not connected for this account; connect it in Settings")
        # Claude moves HOME to its account home; Codex calls keep the original.
        codex_call_home = str(configured.get("HOME") or Path.home())
        # The Claude environment is rebuilt from a scrubbed copy, so the Codex
        # account is added afterwards.
        configured = claude_login.account_environment(claude_home, configured)
        configured["CODEX_HOME"] = str(codex_home)
        configured["AGENT_MONITOR_CODEX_SUBSCRIPTION"] = "1"
        configured[cgm_max.CODEX_CALL_HOME_ENV] = codex_call_home
        configured["ELAN_HOME"] = server_elan_home()
        configured["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"] = "1"
        configured["AGENT_MONITOR_AUTH_MODE"] = cgm_max.ROUTE
        configured[cgm_max.ROUTE_MODE_ENV] = "max"
        scrub_control_plane_env(configured)
        return configured

    if route == "codex_subscription":
        from agent_monitor import codex_login

        home = codex_login.account_home(int(user_id))
        if not codex_login.account_login_ready(home):
            raise ValueError("Codex is not connected for this account; connect it in Settings")
        configured["CODEX_HOME"] = str(home)
        configured["AGENT_MONITOR_CODEX_SUBSCRIPTION"] = "1"
        configured["AGENT_MONITOR_AUTH_MODE"] = "chatgpt_subscription"
        configured["ELAN_HOME"] = server_elan_home()
        scrub_control_plane_env(configured)
        return configured

    from agent_monitor import claude_login

    home = claude_login.account_home(int(user_id))
    if not claude_login.account_login_ready(home):
        raise ValueError("Claude Code is not connected for this account; connect it in Settings")
    configured = claude_login.account_environment(home, configured)
    # HOME is now the account home; Lean must keep using the server's elan.
    configured["ELAN_HOME"] = server_elan_home()
    configured["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"] = "1"
    configured["AGENT_MONITOR_AUTH_MODE"] = "claude_subscription"
    scrub_control_plane_env(configured)
    return configured


def _own_subscriptions_allowed(user: dict[str, Any] | None) -> bool:
    """Whether a manuscript run's request lets its runner use a linked
    account subscription (``own_subscriptions`` in ``analysis/request.json``
    and ``readability/request.json``): an administrator's, or that of an
    account given full access, each its own linked login.

    As ``_configure_auth_route`` decides it for every route: the stored
    account (``auth.may_use_own_subscriptions``), never only the session's
    copy of it; never a guest or a restricted member.  Fails closed."""
    if not user or user.get("guest") or account_policy.is_restricted(user):
        return False
    from agent_monitor.auth import may_use_own_subscriptions

    try:
        return bool(may_use_own_subscriptions(user.get("id")))
    except Exception:  # noqa: BLE001 - fail closed
        return False


def _selected_auth_route(
    *,
    requested: str | None,
    engine: str,
    model: str | None,
    auth_modes: set[str],
    user: dict[str, Any] | None,
    env: dict[str, str],
    enforce_account_runtime: bool = True,
) -> tuple[str, bool]:
    """Resolve the route and report whether the caller selected it explicitly."""
    from agent_monitor.settings import account_runtime

    explicit = bool(str(requested or "").strip())
    if explicit:
        route = str(requested).strip().lower()
        if route not in _AUTH_ROUTES:
            raise ValueError(f"Unsupported authentication route: {route}")
        has_persisted_runtime = bool(
            str(env.get("AGENT_MONITOR_ACCOUNT_RUNTIME") or "").strip()
            or "AGENT_MONITOR_USE_CLAUDE" in env
            or "AGENT_MONITOR_USE_CODEX" in env
        )
        persisted_runtime = account_runtime(user, env=env) if has_persisted_runtime else ""
        expected_route = {
            "api": "api_key",
            "codex": "codex_subscription",
            "claude": "claude_subscription",
        }.get(persisted_runtime)
        if enforce_account_runtime and expected_route and route != expected_route:
            raise ValueError(
                f"Requested {route.replace('_', ' ')} conflicts with the selected "
                f"{persisted_runtime} account runtime; refresh the page or switch "
                "the runtime in Settings"
            )
        return route, True
    preferred = account_runtime(user, env=env)
    if preferred == "claude":
        if (
            engine in _CLAUDE_SUBSCRIPTION_ENGINES
            and "claude_subscription" in auth_modes
        ):
            return "claude_subscription", False
        raise ValueError(
            f"{engine} is not compatible with the selected Claude Code subscription; "
            "switch the account runtime to API or choose a compatible harness"
        )
    if preferred == "codex":
        if (
            engine in _CODEX_SUBSCRIPTION_ENGINES
            and "codex_subscription" in auth_modes
        ):
            return "codex_subscription", False
        raise ValueError(
            f"{engine} is not compatible with the selected Codex subscription; "
            "switch the account runtime to API or choose a compatible harness"
        )
    if "api_key" not in auth_modes:
        raise ValueError(
            f"{engine} is not compatible with the selected API-provider runtime; "
            "switch the account runtime or choose a compatible harness"
        )
    return "api_key", False


def _validate_subscription_model(
    *,
    route: str,
    model: str | None,
    user: dict[str, Any] | None,
    field: str,
) -> None:
    """Reject stale/cross-runtime models before any account runner starts."""
    selected = str(model or "").strip()
    if not selected or route == "api_key":
        return
    if route == "claude_subscription":
        from agent_monitor.claude_login import DEFAULT_CLAUDE_MODELS

        allowed = list(DEFAULT_CLAUDE_MODELS)
        label = "Claude Code subscription"
    else:
        from agent_monitor import codex_login

        owner_id = (user or {}).get("id")
        allowed = (
            list(codex_login.available_models(int(owner_id)))
            if owner_id is not None
            else []
        )
        label = "Codex subscription"
    if selected not in allowed:
        choices = (
            ", ".join(allowed)
            if allowed
            else "a model exposed by the connected account"
        )
        if route == "claude_subscription":
            prefix = (
                "Unsupported Claude account model"
                if field == "Selected model"
                else "Unsupported Claude subagent model"
            )
        else:
            prefix = (
                "Unsupported Codex account model"
                if field == "Selected model"
                else "Unsupported Codex subagent model"
            )
        raise ValueError(
            f"{prefix}: {selected} is not a {label} model; choose {choices}"
        )


def _raise_long_effort_timeouts(
    env: dict[str, str], *, engine: str, route: str, effort: object
) -> None:
    """Give extra-high and maximum effort CGM, Analyze and Readability calls 30 minutes.

    Those calls on a linked account are long (Max allows them 30 minutes
    each) and would otherwise get the 600 s default; a timed-out call is
    never retried.  Every call stays capped at the run's deadline, so the
    watchdogs are unchanged.  Other engines read these variables with
    defaults of their own.  An operator setting wins, the shared Claude
    timeout included (the CGM-specific one would take precedence over it).
    """
    if (
        engine not in _CGM_ENGINES | _MANUSCRIPT_ENGINES
        or route not in {"claude_subscription", "codex_subscription"}
        or effort not in {"xhigh", "max"}
    ):
        return
    for names, seconds in (
        (
            ("AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT", "AGENT_MONITOR_CLAUDE_TIMEOUT"),
            cgm_max.CLAUDE_CALL_TIMEOUT,
        ),
        (("AGENT_MONITOR_CODEX_TIMEOUT",), cgm_max.CODEX_CALL_TIMEOUT),
    ):
        if not any(str(env.get(name) or os.environ.get(name) or "").strip() for name in names):
            env[names[0]] = str(seconds)


def start_job(
    *,
    engine: str,
    problem_id: str | None = None,
    problem_text: str | None = None,
    model: str | None = None,
    max_iterations: int = 40,
    max_output_tokens: int | None = None,
    user: dict[str, Any] | None = None,
    use_subagents: bool = True,
    subagent_model: str | None = None,
    auth_route: str | None = None,
    reasoning_effort: str | None = None,
    speed_mode: str | None = None,
    attachments: object = None,
    formalize: bool | None = None,
    run_kind: str | None = None,
    source: object = None,
    analyze: object = None,
    readability: object = None,
    tool_free: bool = False,
    easy_route: bool = False,
    easy_route_home: str = "",
) -> dict[str, Any]:
    """Start one run (``run_kind`` ``"prove"``, the default, ``"analyze"`` or
    ``"readability"``).

    An Analyze run takes ``engine="analyze"``, a ``source`` (``{"url"}`` or
    ``{"text", "format"?}``) or manuscript ``attachments``, and ``analyze``
    options (``{"max_nodes"?, "focus"?}``); ``formalize`` defaults to false for
    it and to true for proofs.  See :mod:`agent_monitor.analyze_runs`.  A
    readability run takes ``engine="readability"``, a source as Analyze does
    and ``readability`` options (``{"audience"?, "focus"?}``); it is never
    formalized.  See :mod:`agent_monitor.readability_runs`.

    ``tool_free`` marks a proof from a project that has, or had, a member who
    is not an administrator: it runs only a tool-free engine on an API key,
    without subagents (``account_policy.PROJECT_TOOL_FREE_ERROR``, an
    ``account_policy.ProjectPolicyError``).

    ``easy_route`` marks an Ansatz CGM start moved to Plain (start_job_routed):
    it is refused, before anything is written, where Plain would not use the
    route Ansatz CGM would (the sponsored Kimi service).
    """
    from agent_monitor.engines_registry import all_engine_ids, list_engines, supported_models

    is_guest = bool(user and user.get("guest"))
    if engine not in all_engine_ids():
        raise ValueError(f"Unsupported engine: {engine}")
    default_kind = {_ANALYZE_ENGINE: "analyze", _READABILITY_ENGINE: "readability"}.get(engine, "prove")
    kind = str(run_kind or default_kind).strip().lower()
    if kind not in {"prove", "analyze", "readability"}:
        raise ValueError('run_kind must be "prove", "analyze" or "readability"')
    if engine == _ANALYZE_ENGINE and kind != "analyze":
        raise ValueError(
            "Ansatz Analyze runs in the Analyze mode; reload the console and choose Analyze"
        )
    if engine == _READABILITY_ENGINE and kind != "readability":
        raise ValueError(
            "Ansatz Readability runs in the Readability mode; reload the console and choose Readability"
        )
    analyze_request: dict[str, Any] | None = None
    readability_request: dict[str, Any] | None = None
    if kind == "analyze":
        from agent_monitor import analyze_runs

        refusal = analyze_runs.availability_error(user)
        if refusal is not None:
            raise refusal
        analyze_request = analyze_runs.normalize_request(
            {
                "engine": engine,
                "source": source,
                "attachments": attachments,
                "analyze": analyze,
                "formalize": formalize if isinstance(formalize, bool) else None,
            },
            user,
        )
        formalize = analyze_request["formalize"]
        # The manuscript is the source, never the run's problem text: the
        # record shows "Analyze · <label>" (and the focus).
        problem_id = f"analysis_{uuid.uuid4().hex[:8]}"
        problem_text = analyze_runs.problem_text(
            analyze_request["label"], analyze_request["focus"]
        )
        model = _analyze_model(user, model, auth_route=auth_route)
        use_subagents = False
        subagent_model = None
    elif kind == "readability":
        from agent_monitor import readability_runs

        refusal = readability_runs.availability_error(user)
        if refusal is not None:
            raise refusal
        readability_request = readability_runs.normalize_request(
            {"engine": engine, "source": source, "attachments": attachments, "readability": readability},
            user,
        )
        formalize = False
        # The manuscript is the source; the record shows "Readability · <label>".
        problem_id = f"readable_{uuid.uuid4().hex[:8]}"
        problem_text = readability_runs.problem_text(
            readability_request["label"], readability_request["focus"]
        )
        model = _readability_model(user, model, auth_route)
        use_subagents = False
        subagent_model = None
    elif formalize is None:
        formalize = True
    if kind in {"analyze", "readability"} and account_policy.member_analyze_sponsored_only(user):
        # Public mode: a member's manuscript runs on sponsored Kimi K3 alone,
        # full access or not, on the API route whatever its account runtime
        # (its start names no model, and the route of a linked Codex or
        # Claude Code account that runs nothing here).  Another model is
        # refused as for a restricted member (account_policy.member_model_error).
        refusal_text = account_policy.member_model_error(user, engine, model)
        if refusal_text:
            raise ValueError(refusal_text)
        auth_route = "api_key"
    tool_free = tool_free and kind == "prove"
    if tool_free:
        refusal = account_policy.project_run_error(engine, model=model, auth_route=auth_route)
        if refusal:
            raise account_policy.ProjectPolicyError(refusal)
        use_subagents = False
        subagent_model = None
        auth_route = "api_key"
    max_plan: dict[str, Any] | None = None
    if cgm_max.is_max_model(model) or str(auth_route or "").strip().lower() == cgm_max.ROUTE:
        # Max: Claude Code leads and Codex works, on the run owner's two linked
        # accounts, each on its most capable model at maximum effort.
        if engine not in _CGM_ENGINES:
            raise ValueError("Max is available only for Ansatz CGM")
        max_plan = cgm_max.plan_for(user)
        if not max_plan["available"]:
            raise ValueError(max_plan["reason"])
        engine = cgm_max.ENGINE
        model = cgm_max.MODEL_ID
        auth_route = cgm_max.ROUTE
        reasoning_effort = cgm_max.EFFORT
        speed_mode = cgm_max.SPEED
        use_subagents = False
        subagent_model = None
    api_max_plan: dict[str, Any] | None = None
    if cgm_max.is_max_api_model(model):
        # Max (API): the same split on the run owner's Anthropic and OpenAI
        # keys, never a linked account (and never a fallback from Max).
        if engine not in _CGM_ENGINES:
            raise ValueError("Max (API) is available only for Ansatz CGM")
        api_max_plan = cgm_max.plan_for_api(user)
        if not api_max_plan["available"]:
            raise ValueError(api_max_plan["reason"])
        engine = cgm_max.ENGINE
        model = cgm_max.API_MODEL_ID
        auth_route = cgm_max.API_ROUTE
        reasoning_effort = cgm_max.EFFORT
        speed_mode = cgm_max.SPEED
        use_subagents = False
        subagent_model = None
    # Guests run only the two tool-free engines, never a coding-agent CLI, and
    # never with subagents or a command override that escapes that contract.
    if is_guest:
        if engine not in _GUEST_ENGINES:
            raise ValueError("That engine requires a signed-in account")
        use_subagents = False
        subagent_model = None
        # A guest has no subscription to consume, so pin the billing route.
        auth_route = "api_key"
        override = _GUEST_COMMAND_OVERRIDES.get(engine)
        if override and os.environ.get(override, "").strip():
            raise ValueError(
                f"Guest {engine} is unavailable while {override} is overridden"
            )
    elif account_policy.is_restricted(user):
        # Self-registered accounts are untrusted: tool-free engines only,
        # billed to their own key or the sponsored route, never a subscription
        # (the own-key CGM harness never takes Kimi K3 or Max either).
        model = account_policy.member_run_model(user, engine, model)
        member_error = account_policy.member_run_error(
            user, engine, model=model, auth_route=auth_route
        )
        if member_error:
            raise ValueError(member_error)
        use_subagents = False
        subagent_model = None
        auth_route = "api_key"
    if engine == "kimi":
        from agent_monitor.settings import GUEST_DEFAULT_MODEL

        model = model or GUEST_DEFAULT_MODEL
        if model != GUEST_DEFAULT_MODEL:
            raise ValueError(f"Kimi Proof uses {GUEST_DEFAULT_MODEL}")
    if (
        not str(model or "").strip()
        and user is not None
        and not is_guest
        and not account_policy.is_restricted(user)
    ):
        # A blank model (the Projects page's "Default account model", or an
        # API caller's) is the saved default, as it is for members.  Resolved
        # before the provider keys are projected for it.
        model = _saved_default_model(user, engine=engine, auth_route=auth_route) or model
    # Kimi K3 is the only Kimi model of Analyze, Readability and the engines
    # that serve every Kimi model name as Kimi K3 (or as no Kimi model:
    # engines_registry.KIMI_K3_ONLY_ENGINE_IDS), on every route, with or
    # without a Kimi key of the account's own: another one (kimi-k2.5 typed
    # as a custom model) would run under a name it never ran on.  Refused once
    # the model is final, before any key is projected, a sponsored start taken
    # or a broker credential issued, whatever the sponsored service's health
    # (the runners refuse it too: analyze_runner.configure_route,
    # api_backend.api_chat).  A coding-agent harness takes it on an account's
    # own Kimi key (it sends the model as named), not on the sponsored route
    # (below).
    kimi_refusal = _other_kimi_model_error(engine, kind, model)
    if kimi_refusal:
        raise ValueError(kimi_refusal)

    extra_env = _user_extra_env(
        user,
        *(
            (api_max_plan["advisor"]["model"], api_max_plan["worker"]["model"])
            if api_max_plan is not None
            else (model, subagent_model)
        ),
    )
    if is_guest:
        # Two ceilings, one per transport: the Plain/Kimi harnesses read the
        # PLAIN variable, and api_backend clamps every API call with the other.
        try:
            output_limit = int(max_output_tokens or 4096)
        except (TypeError, ValueError):
            output_limit = 4096
        output_limit = max(256, min(output_limit, 8192))
        if engine in _BOUNDED_OUTPUT_ENGINES:
            # Multi-lemma CGM replies (and Analyze verdicts) need the full
            # bounded ceiling; Kimi's reasoning tokens count against it.
            output_limit = _CGM_BOUNDED_OUTPUT_TOKENS
        extra_env = dict(extra_env)
        extra_env["AGENT_MONITOR_PLAIN_MAX_OUTPUT_TOKENS"] = str(output_limit)
        extra_env["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] = str(output_limit)
    elif max_output_tokens:
        extra_env["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] = str(int(max_output_tokens))
    if (
        is_guest
        and kind == "prove"
        and not extra_env.get("KIMI_API_KEY")
        and not _sponsored_broker_serves(engine, model)
    ):
        # Guests inherit no operator credentials, so the only key they may use
        # is the dedicated hosted guest one (when the operator configured it).
        # A healthy sponsored broker always wins: the raw hosted key would skip
        # its per-client and gateway call caps.
        from agent_monitor.settings import hosted_guest_kimi_key

        hosted_key = hosted_guest_kimi_key()
        if hosted_key:
            extra_env["KIMI_API_KEY"] = hosted_key
            extra_env["KIMI_BASE_URL"] = _operator_kimi_base()
    engine_info = next((item for item in list_engines() if item.get("id") == engine), {})
    if engine_info and not engine_info.get("available"):
        detail = engine_info.get("health_detail") or engine_info.get("hint") or "runtime unavailable"
        raise ValueError(
            f"{engine_info.get('label') or engine} needs setup — {detail}"
        )

    auth_modes = set(engine_info.get("auth_modes") or [])
    supports_api = "api_key" in auth_modes
    selected_kimi = _is_kimi_model(model)
    if is_guest and selected_kimi and not extra_env.get("KIMI_API_KEY") and os.environ.get("AGENT_MONITOR_GUEST_HOSTED_KIMI", "1").lower() in {"0", "false", "off", "no"}:
        raise ValueError("Hosted Kimi is disabled; add your own Kimi API key in Settings")
    sponsored_only = kind in {"analyze", "readability"} and (
        is_guest or account_policy.member_analyze_sponsored_only(user)
    )
    if sponsored_only:
        # Guests read manuscripts on the sponsored Kimi K3 route only: never a
        # key of their own (or the hosted guest key, which would skip the
        # broker's per-client and gateway caps).  So do members in public
        # mode, whose "own" keys would be the server's.
        try:
            sponsored_kimi = _apply_sponsored_kimi_route(
                extra_env, engine=engine, model=model, force=True
            )
        except ValueError:
            sponsored_kimi = False
        if not sponsored_kimi:
            one, many = ("an analysis", "analyses") if kind == "analyze" else ("a readability run", "readability runs")
            raise ValueError(
                f"Sponsored Kimi K3 is not available right now, so guests can't start {one}; try again later"
                if is_guest
                else f"Sponsored Kimi K3 is not available right now, so {many} can't start; try again later"
            )
    else:
        sponsored_kimi = _apply_sponsored_kimi_route(
            extra_env, engine="plain" if engine == "kimi" else engine, model=model
        )
    if (
        sponsored_kimi
        and account_policy.is_member(user)
        and engine not in account_policy.MEMBER_SPONSORED_ENGINES
    ):
        # A member reaches the sponsored Kimi K3 service only through the
        # included engines, full access or not: here also when a blank model
        # resolved to a Kimi default.
        raise ValueError(
            "Kimi K3 runs on the included Ansatz CGM · Kimi K3 engine; for this engine "
            "choose a model from your own API keys or linked accounts, or add your own "
            "Kimi API key in Settings"
        )
    if easy_route and sponsored_kimi and easy_route_home != "kimi":
        # An easy request moves to Plain only on the account route Ansatz CGM
        # would use, never onto the sponsored service (start_job_routed then
        # starts Ansatz CGM as asked).  Ansatz CGM · Kimi K3 takes the same
        # sponsored route itself (through Plain), so its easy requests may.
        raise ValueError("Plain would run on the sponsored Kimi service")
    if sponsored_kimi:
        # The sponsored service asks for Kimi K3 whatever any engine names, so
        # another Kimi model that a coding-agent harness would send as named
        # to an account's own Kimi key is refused on this route (the other
        # engines refused it above), before any broker credential is issued.
        kimi_refusal = _other_kimi_model_error(engine, kind, model, sponsored=True)
        if kimi_refusal:
            raise ValueError(kimi_refusal)
    if engine in _MANUSCRIPT_ENGINES and not sponsored_kimi and _is_kimi_model(model):
        # Own-key Kimi manuscript runs use the operator's endpoint, like the kimi engine.
        extra_env["AGENT_MONITOR_KIMI_BASE_URL"] = _operator_kimi_base()
    if engine == "kimi" and not sponsored_kimi:
        # Own-key runs use the operator's endpoint; a user-settable
        # KIMI_API_BASE must never choose where the key is sent.
        operator_base = _operator_kimi_base()
        extra_env["KIMI_API_BASE"] = operator_base
        extra_env["KIMI_BASE_URL"] = operator_base
        extra_env["AGENT_MONITOR_KIMI_BASE_URL"] = operator_base
    if kind == "prove" and not sponsored_kimi:
        # Plain and the other proof engines that run Kimi K3 alone send an
        # own Kimi key to the operator's endpoint too (_own_kimi_base_env).
        _own_kimi_base_env(extra_env, engine=engine, model=model)
    has_kimi_key = bool(str(extra_env.get("KIMI_API_KEY") or "").strip())
    if selected_kimi and not supports_api:
        raise ValueError(f"{engine_info.get('label') or engine} does not support API models")
    if selected_kimi and not (has_kimi_key or sponsored_kimi):
        raise ValueError("Kimi K3 requires a configured own key or sponsored route")
    has_api_key = (has_kimi_key or sponsored_kimi) if selected_kimi else any(
        k.endswith("_API_KEY") and bool(str(v or "").strip())
        for k, v in extra_env.items()
    )
    if max_plan is not None:
        # Max does not depend on the account runtime selection: it always uses
        # both linked accounts, which plan_for has just checked.
        if not {"claude_subscription", "codex_subscription"} <= auth_modes:
            raise ValueError(f"{engine_info.get('label') or engine} cannot run Max")
        route = cgm_max.ROUTE
    elif api_max_plan is not None:
        # Nor does Max (API): it always uses the two API keys, which must both
        # have reached the run (one key is never enough).
        if cgm_max.API_ROUTE not in auth_modes:
            raise ValueError(f"{engine_info.get('label') or engine} cannot run Max (API)")
        if not all(
            str(extra_env.get(name) or "").strip() for name in cgm_max.API_KEY_ENVS.values()
        ):
            raise ValueError(
                "Max (API) needs both an Anthropic and an OpenAI API key; add them in Settings"
            )
        route = cgm_max.API_ROUTE
    else:
        route, _route_was_explicit = _selected_auth_route(
            requested=auth_route,
            engine=engine,
            model=model,
            auth_modes=auth_modes,
            user=user,
            env=extra_env,
            # A member's runtime (and a tool-free run's, and that of a
            # manuscript run on sponsored Kimi K3 only) is always the API
            # route, whatever the account stored.
            enforce_account_runtime=not (account_policy.is_restricted(user) or tool_free or sponsored_only),
        )
    if route not in auth_modes and route != cgm_max.ROUTE:
        raise ValueError(
            f"{engine_info.get('label') or engine} does not support "
            f"{route.replace('_', ' ')}"
        )
    if route == "claude_subscription" and engine not in _CLAUDE_SUBSCRIPTION_ENGINES:
        raise ValueError(
            f"{engine_info.get('label') or engine} has no Claude Code account adapter; "
            "choose API or a compatible harness"
        )
    if route == "codex_subscription" and engine not in _CODEX_SUBSCRIPTION_ENGINES:
        raise ValueError(
            f"{engine_info.get('label') or engine} has no Codex account adapter; "
            "choose API or a compatible harness"
        )
    if route != "api_key" and selected_kimi:
        raise ValueError("Kimi K3 is an API model and cannot use a linked account subscription")
    if route == "api_key" and user is not None and not has_api_key:
        raise ValueError("No compatible API key is configured; add and verify one in Settings")
    if (
        engine == "codex"
        and route == "api_key"
        and not selected_kimi
        and user is not None
        and not str(extra_env.get("OPENAI_API_KEY") or "").strip()
    ):
        # Codex signs in only with an OpenAI key; another provider's key would
        # leave it nothing to use but the service account's own login.
        raise ValueError("Codex CLI on the API route needs an OpenAI API key; add one in Settings")

    # Validate both the main and delegated model against the selected billing
    # route. A stale API model in either field must never flip a subscription
    # run back to a provider key.
    if route in {"claude_subscription", "codex_subscription"}:
        _validate_subscription_model(
            route=route, model=model, user=user, field="Selected model"
        )
    if use_subagents and subagent_model:
        _validate_subscription_model(
            route=route,
            model=subagent_model,
            user=user,
            field="Subagent model",
        )

    allowed_models = supported_models(engine)
    if route != "api_key" and model and allowed_models and model not in allowed_models:
        raise ValueError(
            f"{engine} does not support {model} with its account adapter; "
            f"choose one of: {', '.join(allowed_models)}"
        )
    from agent_monitor import run_tuning

    tuning = run_tuning.normalize(
        model,
        "sponsored_kimi" if sponsored_kimi else route,
        reasoning_effort=reasoning_effort,
        speed_mode=speed_mode,
        engine=engine,
    )
    extra_env = _configure_auth_route(
        extra_env,
        route=route,
        user_id=(user or {}).get("id"),
    )
    if max_plan is not None:
        cgm_max.apply_models(extra_env, max_plan)
    if api_max_plan is not None:
        # After _configure_auth_route, which clears every route-mode marker.
        cgm_max.apply_models_api(extra_env, api_max_plan)
    _raise_long_effort_timeouts(
        extra_env, engine=engine, route=route, effort=tuning.get("reasoning_effort")
    )
    # Carry the UI toggle into every external harness. Engines without an
    # internal delegation tool ignore it; DeepAgents uses it to remove `task`.
    extra_env["AGENT_MONITOR_USE_SUBAGENTS"] = "1" if use_subagents else "0"
    if subagent_model:
        extra_env["AGENT_MONITOR_SUBAGENT_MODEL"] = subagent_model
    else:
        extra_env.pop("AGENT_MONITOR_SUBAGENT_MODEL", None)

    attachment_batch = (
        research_attachments.validate(attachments, guest=is_guest, analyze=True)
        if analyze_request is not None or readability_request is not None
        else research_attachments.validate(attachments, guest=is_guest)
    )
    if kind == "prove":
        # A tool-free engine cannot see an image: refused before anything is saved.
        upload_error = cgm_sources.upload_error(engine, attachment_batch)
        if upload_error:
            raise ValueError(upload_error)
    if attachment_batch and not (problem_text or "").strip() and not problem_id:
        problem_text = cgm_sources.FIRST_RUN_MESSAGE
    pid, text, path = resolve_problem(problem_id, problem_text)
    _check_cgm_problem_size(engine, text)

    job_id = uuid.uuid4().hex[:10]
    run_id = f"{engine}_{pid}_{job_id}"
    analyze_route = ""
    analyze_caps: dict[str, int] = {}
    gate_key: str | None = None
    if analyze_request is not None:
        from agent_monitor import analyze_runs

        analyze_route = "sponsored_kimi" if sponsored_kimi else route
        analyze_role = analyze_runs.role_of(user)
        effective = analyze_runs.effective_caps(
            analyze_role,
            sponsored=sponsored_kimi,
            subscription=route in {"codex_subscription", "claude_subscription"},
            requested=analyze_runs.request_caps(
                analyze_role, sponsored=sponsored_kimi, max_nodes=analyze_request["max_nodes"]
            ),
        )
        # The request carries the effective caps (it may only lower the role
        # caps, so the runner applies exactly these), and run["analyze"]
        # shows them from the start.
        analyze_caps = {
            "max_nodes": effective.max_nodes,
            "max_calls": effective.max_calls,
            "max_seconds": effective.max_seconds,
        }
        extra_env[analyze_runs.WATCHDOG_ENV] = str(
            analyze_runs.watchdog_seconds(
                effective, sponsored=sponsored_kimi, api_timeout=_analyze_api_timeout(sponsored_kimi)
            )
        )
        if not sponsored_kimi:
            # A flag inherited from the account environment is not this run's
            # route: the runner would otherwise expect a broker credential.
            extra_env["AGENT_MONITOR_SPONSORED_KIMI"] = "0"
            extra_env.pop("AGENT_MONITOR_SPONSORED_KIMI_URL", None)
        if sponsored_kimi:
            # Two sponsored analyses at a time and a daily cap (refusals are
            # 429/403 with a plain message); released when the worker exits.
            analyze_runs.GATE.reserve(run_id, run_id=run_id, guest=is_guest)
            gate_key = run_id
    readability_route = ""
    readability_caps: dict[str, int] = {}
    if readability_request is not None:
        from agent_monitor import analyze_runs, readability_runs

        readability_route = "sponsored_kimi" if sponsored_kimi else route
        effective = readability_runs.effective_caps(
            analyze_runs.role_of(user),
            sponsored=sponsored_kimi,
            subscription=route in {"codex_subscription", "claude_subscription"},
            requested=readability_runs.request_caps(sponsored=sponsored_kimi),
        )
        # The request carries the effective caps, as for an analysis.
        readability_caps = {
            "max_parts": effective.max_parts,
            "max_calls": effective.max_calls,
            "max_seconds": effective.max_seconds,
        }
        extra_env[readability_runs.WATCHDOG_ENV] = str(
            readability_runs.watchdog_seconds(
                effective, sponsored=sponsored_kimi,
                api_timeout=_analyze_api_timeout(
                    sponsored_kimi, variable="AGENT_MONITOR_READABILITY_API_TIMEOUT"),
            )
        )
        if not sponsored_kimi:
            extra_env["AGENT_MONITOR_SPONSORED_KIMI"] = "0"
            extra_env.pop("AGENT_MONITOR_SPONSORED_KIMI_URL", None)
        if sponsored_kimi:
            # The same sponsored pool as Analyze, worded for Readability.
            analyze_runs.GATE.reserve(run_id, run_id=run_id, guest=is_guest,
                                      messages=readability_runs.GATE_MESSAGES)
            gate_key = run_id
    # Until the worker starts, a failure returns the sponsored slot (and its
    # daily count); afterwards the worker releases it on exit.
    try:
        return _start_job_launch(
            engine=engine,
            extra_env=extra_env,
            is_guest=is_guest,
            sponsored_kimi=sponsored_kimi,
            run_id=run_id,
            job_id=job_id,
            user=user,
            model=model,
            route=route,
            tuning=tuning,
            text=text,
            pid=pid,
            path=path,
            # Either Max: the plan names both sides (an API plan has mode "api").
            max_plan=max_plan if max_plan is not None else api_max_plan,
            max_iterations=max_iterations,
            use_subagents=use_subagents,
            subagent_model=subagent_model,
            attachment_batch=attachment_batch,
            formalize=formalize,
            kind=kind,
            analyze_request=analyze_request,
            analyze_route=analyze_route,
            analyze_caps=analyze_caps,
            readability_request=readability_request,
            readability_route=readability_route,
            readability_caps=readability_caps,
            tool_free=tool_free,
        )
    except BaseException:
        if gate_key is not None:
            from agent_monitor import analyze_runs

            analyze_runs.GATE.release(gate_key, refund=True)
        raise


def _start_job_launch(
    *,
    engine: str,
    extra_env: dict[str, str],
    is_guest: bool,
    sponsored_kimi: bool,
    run_id: str,
    job_id: str,
    user: dict[str, Any] | None,
    model: str | None,
    route: str,
    tuning: dict[str, str],
    text: str,
    pid: str,
    path: Path | None,
    max_plan: dict[str, Any] | None,
    max_iterations: int,
    use_subagents: bool,
    subagent_model: str | None,
    attachment_batch: list[Any],
    formalize: bool,
    kind: str,
    analyze_request: dict[str, Any] | None,
    analyze_route: str,
    analyze_caps: dict[str, int],
    readability_request: dict[str, Any] | None = None,
    readability_route: str = "",
    readability_caps: dict[str, int] | None = None,
    tool_free: bool = False,
) -> dict[str, Any]:
    """The launch half of :func:`start_job`: credentials, workspace, record,
    worker thread and derived pipeline (everything validated already)."""
    from agent_monitor import run_tuning

    _apply_bounded_run_env(
        extra_env, engine=engine, guest=is_guest, sponsored=sponsored_kimi
    )
    # Before the token lifetime below: the write-up round extends the run.
    _apply_cgm_writeup_env(extra_env, engine=engine)
    if sponsored_kimi and engine != "plain":
        from agent_monitor import sponsored_kimi_client

        extra_env.update(
            sponsored_kimi_client.issue_credentials(
                engine=_sponsored_transport_engine(engine),
                client_id=(user or {}).get("id"),
                cache_key=run_id,
                minimum_ttl_seconds=_cli_timeout_seconds(engine, extra_env) + _SPONSORED_KIMI_TOKEN_MARGIN,
            )
        )
        # The harness reads KIMI_BASE_URL; the broker issues KIMI_API_BASE.
        if str(extra_env.get("KIMI_API_BASE") or "").strip():
            extra_env["KIMI_BASE_URL"] = str(extra_env["KIMI_API_BASE"]).rstrip("/")
    run_tuning.apply_environment(
        extra_env,
        model=model,
        auth_route=route,
        **tuning,
    )
    # Re-assert last: nothing user-controlled may relax the guest profile.
    _apply_bounded_run_env(
        extra_env, engine=engine, guest=is_guest, sponsored=sponsored_kimi
    )
    _stop_event(run_id).clear()
    ws = workspace_dir(run_id)
    attached = research_attachments.save(ws, attachment_batch)
    (ws / "problem.txt").write_text(text, encoding="utf-8")
    manuscript_fields: dict[str, Any] = {}
    # A manuscript runner takes a linked account's route only for an account
    # that may use its own (an administrator, or one given full access).
    own_subscriptions = (
        _own_subscriptions_allowed(user)
        if analyze_request is not None or readability_request is not None
        else False
    )
    if analyze_request is not None:
        from agent_monitor import analyze_runs
        from agent_monitor.analyze import record as analyze_record
        from agent_monitor.analyze import request as analysis_request

        analysis_source = dict(analyze_request["source"])
        if analysis_source["kind"] == "upload":
            analysis_source["attachments"] = [str(item["id"]) for item in attached]
        analysis_request.write(
            ws,
            source=analysis_source,
            caps=analyze_caps,
            focus=analyze_request["focus"],
            formalize=bool(formalize),
            role=analyze_runs.role_of(user),
            route=analyze_route,
            model=str(model or ""),
            reasoning_effort=str(tuning.get("reasoning_effort") or "default"),
            sponsored=bool(sponsored_kimi),
            own_subscriptions=own_subscriptions,
        )
        manuscript_fields = {
            "run_kind": "analyze",
            "analyze": analyze_record.run_block(ws) or {},
        }
    if readability_request is not None:
        from agent_monitor import analyze_runs
        from agent_monitor.readability import record as readability_record
        from agent_monitor.readability import request as readability_request_file

        readability_source = dict(readability_request["source"])
        if readability_source["kind"] == "upload":
            readability_source["attachments"] = [str(item["id"]) for item in attached]
        readability_request_file.write(
            ws,
            source=readability_source,
            caps=readability_caps or {},
            audience=readability_request["audience"],
            focus=readability_request["focus"],
            role=analyze_runs.role_of(user),
            route=readability_route,
            model=str(model or ""),
            reasoning_effort=str(tuning.get("reasoning_effort") or "default"),
            sponsored=bool(sponsored_kimi),
            own_subscriptions=own_subscriptions,
        )
        manuscript_fields = {
            "run_kind": "readability",
            "readability": readability_record.run_block(ws) or {},
        }
    run = normalize_run(
        {
            "run_id": run_id,
            "job_id": job_id,
            "problem_id": pid,
            "trace_name": f"[{engine.upper()}] {pid}",
            "status": "running",
            "created_at": _now(),
            "updated_at": _now(),
            "workspace": str(ws),
            "pipeline": _pipeline_for(engine),
            "agents": [
                {
                    "trace_id": f"{run_id}::bootstrap",
                    "stage_name": "bootstrap",
                    "role": "system",
                    "pipeline_stage": _pipeline_for(engine)[0]["id"],
                    "prompt": text[:4000],
                    "output": (
                        "Starting Ansatz Analyze…"
                        if analyze_request is not None
                        else "Starting Ansatz Readability…"
                        if readability_request is not None
                        else f"Starting {engine} engine…"
                    ),
                    "status": "running",
                }
            ],
            "edges": [],
            "totals": {"cost_usd": 0, "latency_s": 0},
            "problem_text": text[:20000],
            "problem_text_preview": text[:500],
            "problems": _seed_problems(text, source="initial"),
            "owner_id": user.get("id") if user else None,
            "owner": user.get("email") if user else None,
            "guest": is_guest,
            "model": model,
            "requested_model": model,
            "max_iterations": max_iterations,
            "live": {
                "model": model,
                "model_requested": bool(model),
                "requested_model": model,
                "auth_mode": _live_auth_mode(route),
            },
            "use_subagents": bool(use_subagents),
            "subagent_model": subagent_model if use_subagents else None,
            **tuning,
            "auth_route": route,
            "credential_source": "sponsored_kimi" if sponsored_kimi else "user",
            # False keeps the run informal: the derived pipeline skips Lean.
            "formalize": bool(formalize),
            **manuscript_fields,
            # Its prompt carries a non-administrator's project text for good,
            # whoever belongs to the project later (see tool_free_run).
            **({"tool_free": True} if tool_free else {}),
            **(
                {
                    "route_plan": {
                        **({"mode": max_plan["mode"]} if max_plan.get("mode") else {}),
                        "advisor": dict(max_plan["advisor"]),
                        "worker": dict(max_plan["worker"]),
                    }
                }
                if max_plan is not None
                else {}
            ),
        },
        engine=engine,  # type: ignore[arg-type]
    )
    _write_run(run)

    if attached:
        # Record the opening turn so the transcript shows which files the run
        # started from; the worker's own events follow it.
        _append_chat(run_id, "user", text, attachments=attached)

    job = {
        "job_id": job_id,
        "run_id": run_id,
        "engine": engine,
        "problem_id": pid,
        "status": "running",
        "created_at": _now(),
        "updated_at": _now(),
        "error": None,
        "model": model,
        "owner_id": user.get("id") if user else None,
        "guest": is_guest,
        "use_subagents": use_subagents,
        "subagent_model": subagent_model,
        "formalize": bool(formalize),
        **tuning,
        "auth_route": route,
        "credential_source": "sponsored_kimi" if sponsored_kimi else "user",
        "problem_preview": _problem_preview(text),
        **({"run_kind": kind} if kind != "prove" else {}),
    }
    with _LOCK:
        _JOBS[job_id] = job

    thread = threading.Thread(
        target=_execute_job,
        kwargs={
            "job_id": job_id,
            "run_id": run_id,
            "engine": engine,
            "problem_id": pid,
            "problem_text": text,
            "problem_path": str(path) if path else None,
            "model": model,
            "max_iterations": max_iterations,
            "workspace": str(ws),
            "extra_env": extra_env,
            "use_subagents": use_subagents,
            "subagent_model": subagent_model,
            **tuning,
            "owner_id": user.get("id") if user else None,
            "guest": is_guest,
            "run_kind": kind,
        },
        daemon=True,
        name=f"engine-{engine}-{job_id}",
    )
    thread.start()
    _track_worker(job_id, run_id, thread)

    # The proof engine and derived views are independent background concerns.
    # Start the sidecar now; it waits for a stable draft (the completed proof
    # for guests), then derives Lean and both DAG views from that proof.
    # Lightweight Thread doubles used by embedders/tests intentionally do not
    # launch background work and therefore have no is_alive method.
    # A readability run has no proof and no derived views: no sidecar.
    if (
        hasattr(thread, "is_alive")
        and not is_guest
        and kind != "readability"
        and os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "").strip() != "1"
    ):
        from agent_monitor import auto_pipeline

        auto_pipeline.start(
            run_id=run_id,
            workspace=ws,
            owner_id=user.get("id") if user else None,
            # Max derives Lean and graphs on its worker route (cgm_max.formal_view).
            model=str(max_plan["worker"]["model"]) if max_plan is not None else model,
            engine=engine,
        )
    return dict(job)


def _user_extra_env(
    user: dict[str, Any] | None,
    *models: str | None,
) -> dict[str, str]:
    """Return ordinary settings plus only the selected models' provider routes."""
    env = {"AGENT_MONITOR_PYTHON": sys.executable}
    if user is None:
        # Ownerless local/CLI runs may intentionally use the invoking process's
        # provider configuration.  Make that trust decision explicit here so
        # `_run_cli_engine` can scrub its inherited environment for every run
        # and then overlay only this selected set.  Account-owned web runs never
        # take this branch.
        env.update(
            {
                name: value
                for name in _PROVIDER_ROUTE_ENV_NAMES
                if (value := str(os.environ.get(name) or "").strip())
            }
        )
        from agent_monitor.subprocess_env import project_provider_env

        requested_models = (
            models if any(str(model or "").strip() for model in models)
            else (env.get("AGENT_MONITOR_MODEL"),)
        )
        return dict(project_provider_env(env, requested_models))
    from agent_monitor.settings import resolved_user_env

    from agent_monitor.settings import restrict_account_base_urls

    # SSRF guard: only the operator chooses where an account's runs send
    # requests (a no-op for administrators).
    env.update(restrict_account_base_urls(dict(resolved_user_env(user)), user))
    from agent_monitor.subprocess_env import project_provider_env

    requested_models = (
        models if any(str(model or "").strip() for model in models)
        else (env.get("AGENT_MONITOR_MODEL"),)
    )
    return dict(project_provider_env(env, requested_models))


def _run_provider_models(
    run_data: dict[str, Any], model: str | None, subagent_model: str | None
) -> tuple[str | None, str | None]:
    """The models whose provider keys a follow-up's env keeps: both sides of a
    Max (API) run (its mode id names no provider), else the run's model and
    its subagent model."""
    if cgm_max.is_max_api_record(run_data) or cgm_max.is_max_api_model(model):
        plan = cgm_max.record_plan_api(run_data)
        return plan["advisor"]["model"], plan["worker"]["model"]
    return model, subagent_model


def _account_awaits_reconnect(route: str, user_id: int | None) -> bool:
    """True when the route's linked login was rejected and needs a new sign-in."""
    if user_id is None:
        return False
    if route == "codex_subscription":
        from agent_monitor import codex_login

        return codex_login.account_reauth_required(codex_login.account_home(int(user_id)))
    if route == "claude_subscription":
        from agent_monitor import claude_login

        return claude_login.account_reauth_required(claude_login.account_home(int(user_id)))
    return False


def start_job_routed(**kwargs: Any) -> dict[str, Any]:
    """``start_job``, with the easy-request rating (easy_route): a proof
    started on Ansatz CGM by a signed-in account that the judge rates easy
    runs on Plain instead, on the same model and account route (a start that
    Plain cannot make that way starts Ansatz CGM as asked).  Every other start
    is ``start_job`` unchanged."""
    from agent_monitor import easy_route

    user = kwargs.get("user") or {}
    kind = str(kwargs.get("run_kind") or "prove").strip().lower()
    if (
        kwargs.get("engine") not in easy_route.HOMES
        or kind != "prove"
        or not easy_route.enabled()
        # Max and Max (API) are Ansatz CGM's alone.
        or str(kwargs.get("auth_route") or "").strip().lower() == cgm_max.ROUTE
        or cgm_max.is_max_model(kwargs.get("model"))
        or cgm_max.is_max_api_model(kwargs.get("model"))
    ):
        return start_job(**kwargs)
    home = str(kwargs.get("engine"))
    text = str(kwargs.get("problem_text") or "").strip()
    verdict: dict[str, Any] | None = None
    why = "judged"
    if not text:
        why = "no text"
    elif easy_route.asks_for_cgm(text):
        why = "asked"
    else:
        verdict = easy_route.judge(text)
    job = None
    if verdict and verdict.get("verdict") == "easy" and _easy_plain_allowed(user, kwargs):
        try:
            job = start_job(**{**kwargs, "engine": easy_route.EASY_ENGINE, "easy_route": True,
                               "easy_route_home": home})
        except (ValueError, account_policy.ProjectPolicyError):
            job = None
    engine = easy_route.EASY_ENGINE if job is not None else home
    if job is None:
        job = start_job(**kwargs)
    run_id = str(job.get("run_id") or "")
    if run_id:
        easy_route.note_turn(
            RUNS_DIR / "workspaces" / run_id, home=home, engine=engine, verdict=verdict, why=why
        )
        if engine == easy_route.EASY_ENGINE and verdict:
            _append_chat(run_id, "system", easy_route.plain_note(verdict))
    return job


def _easy_plain_allowed(user: dict[str, Any], kwargs: dict[str, Any]) -> bool:
    """Whether the account may run Plain for this start (member policy; for a
    guest, the operator's guest engines)."""
    if user.get("guest"):
        try:
            from agent_monitor.console_server import guest_engine_allowlist

            return "plain" in guest_engine_allowlist()
        except Exception:  # noqa: BLE001
            return False
    if not account_policy.is_member(user):
        return True
    try:
        plain_model = account_policy.member_run_model(user, "plain", kwargs.get("model"))
        if plain_model != kwargs.get("model"):
            return False  # Plain would answer with another model
        return not account_policy.member_run_error(
            user, "plain", model=plain_model, auth_route=kwargs.get("auth_route")
        )
    except Exception:  # noqa: BLE001
        return False


def _easy_route_follow_up(
    run_id: str,
    run_data: dict[str, Any],
    ws: Path,
    *,
    message: str | None,
    user: dict[str, Any] | None,
    model: str | None,
    has_files: bool = False,
) -> dict[str, Any]:
    """The engine of one follow-up turn of a run started on Ansatz CGM
    (easy_route): Plain when the judge rates the message easy, Ansatz CGM
    otherwise or when the message asks for it.  A bare Continue, a message
    with files, a Max run, or a turn Plain cannot run on the run's own route
    and model keeps the run's engine.  Returns the record to continue with
    (its ``engine`` switched, and the switch said in the chat, when it
    changes).  Never raises: any failure keeps the run's engine."""
    from agent_monitor import easy_route

    try:
        home = easy_route.home(ws)
        if not easy_route.enabled() or home not in easy_route.HOMES:
            return run_data
        current = str(run_data.get("engine") or "")
        if current not in {home, easy_route.EASY_ENGINE}:
            return run_data
        text = (message or "").strip()
        if not text or text.startswith(_BARE_CONTINUE_PREFIX) or has_files:
            return run_data
        verdict: dict[str, Any] | None = None
        if easy_route.asks_for_cgm(text):
            target, why = home, "asked"
        else:
            problems = ensure_problems(run_data, run_id)
            first = _problem_item_text(problems[0]) if problems else str(run_data.get("problem_text") or "")
            verdict = easy_route.judge(text, problem=first)
            why = "judged"
            target = easy_route.EASY_ENGINE if verdict.get("verdict") == "easy" else home
        if target == easy_route.EASY_ENGINE and not _easy_plain_follow_up_ok(run_id, run_data, user, model):
            target = home
        easy_route.note_turn(ws, home=home, engine=target, verdict=verdict, why=why)
        if target == current:
            return run_data
        with _RUN_WRITE_LOCK:
            fresh = _load_run_record(run_id)
            fresh["engine"] = target
            fresh["updated_at"] = _now()
            _write_run(fresh)
        _append_chat(
            run_id,
            "system",
            easy_route.plain_note(verdict or {}) if target == easy_route.EASY_ENGINE
            else easy_route.cgm_note(why, verdict),
        )
        return fresh
    except Exception:  # noqa: BLE001 - routing never stops a follow-up
        traceback.print_exc()
        return run_data


def _easy_answer_only(ws: Path, run_data: dict[str, Any]) -> bool:
    """Whether this Plain turn of easy_route answers without touching the
    proof: an earlier turn of the run ran on Ansatz CGM, so the proof is
    Ansatz CGM's (a run that has only ever been on Plain lets Plain write it)."""
    try:
        from agent_monitor import easy_route

        if str(run_data.get("engine") or "") != easy_route.EASY_ENGINE:
            return False
        state = easy_route.load(ws)
        home = state.get("home")
        if home not in easy_route.HOMES:
            return False
        earlier = (state.get("turns") or [])[:-1]
        return any(isinstance(t, dict) and t.get("engine") == home for t in earlier) and (
            (ws / "proof.md").is_file() and (ws / "proof.md").stat().st_size > 0
        )
    except Exception:  # noqa: BLE001
        return False


def _easy_plain_follow_up_ok(
    run_id: str, run_data: dict[str, Any], user: dict[str, Any] | None, model: str | None
) -> bool:
    """Whether Plain can run this follow-up on the run's own route and model."""
    try:
        if cgm_max.is_max_record(run_data) or cgm_max.is_max_api_record(run_data) or run_data.get("route_plan"):
            return False  # Max and Max (API) are Ansatz CGM's alone
        if cgm_max.is_max_model(model) or cgm_max.is_max_api_model(model):
            return False
        if _route_from_record(run_data) == cgm_max.ROUTE:
            return False
        plain_data = {**run_data, "engine": "plain"}
        plain_model = _member_continuation_model(user, "plain", plain_data, model)
        if plain_model != _member_continuation_model(user, str(run_data.get("engine") or ""), run_data, model):
            return False
        if account_policy.is_member(user) and account_policy.member_run_error(
            user, "plain", model=plain_model, auth_route=_route_from_record(run_data)
        ):
            return False
        _tool_free_follow_up(run_id, plain_data, "plain", plain_model)
        _continuation_auth_environment(
            run_data=plain_data,
            engine="plain",
            model=plain_model,
            user=user,
            env=_user_extra_env(
                user, *_run_provider_models(plain_data, plain_model, plain_data.get("subagent_model"))
            ),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _continuation_auth_environment(
    *,
    run_data: dict[str, Any],
    engine: str,
    model: str | None,
    user: dict[str, Any] | None,
    env: dict[str, str],
) -> tuple[dict[str, str], str]:
    """Preserve a run's original account/API route across continuations."""
    from agent_monitor.engines_registry import list_engines

    engine_info = next((item for item in list_engines() if item.get("id") == engine), {})
    auth_modes = set(engine_info.get("auth_modes") or [])
    recorded_route = _route_from_record(run_data)
    legacy_unpinned = recorded_route is None
    sponsored_run = (
        str(run_data.get("credential_source") or "").strip() == "sponsored_kimi"
    )
    route, _explicit = _selected_auth_route(
        requested=recorded_route,
        engine=engine,
        model=model,
        auth_modes=auth_modes,
        user=user,
        env=env,
        enforce_account_runtime=False,
    )
    if route == cgm_max.ROUTE:
        if not {"claude_subscription", "codex_subscription"} <= auth_modes:
            raise ValueError(f"This run used Max, which {engine} no longer supports")
    elif route not in auth_modes:
        raise ValueError(
            f"This run used {route.replace('_', ' ')}, which {engine} no longer supports"
        )
    if route == "claude_subscription" and engine not in _CLAUDE_SUBSCRIPTION_ENGINES:
        raise ValueError(
            f"{engine} has no Claude Code account adapter; "
            "the run cannot silently switch providers"
        )
    if route == "claude_subscription":
        _validate_subscription_model(
            route=route,
            model=model,
            user=user,
            field="Selected model",
        )
    if route == "codex_subscription" and engine not in _CODEX_SUBSCRIPTION_ENGINES:
        raise ValueError(
            f"{engine} has no Codex account adapter; "
            "the run cannot silently switch providers"
        )
    if route != "api_key" and _is_kimi_model(model):
        raise ValueError("A Kimi continuation must keep the run on its API route")
    if route == "api_key":
        selected_kimi = _is_kimi_model(model)
        if selected_kimi and sponsored_run:
            _apply_sponsored_kimi_route(
                env, engine=engine, model=model, force=True
            )
        has_key = bool(str(env.get("KIMI_API_KEY") or "").strip())
        if selected_kimi and not (has_key or sponsored_run):
            raise ValueError("Kimi K3 requires its recorded sponsored route or own key")
        if (
            not legacy_unpinned
            and user is not None
            and not sponsored_run
            and not any(
                key.endswith("_API_KEY") and bool(str(value or "").strip())
                for key, value in env.items()
            )
        ):
            raise ValueError("The API connection originally used by this run is unavailable")
        if cgm_max.is_max_api_record(run_data) and not all(
            str(env.get(name) or "").strip() for name in cgm_max.API_KEY_ENVS.values()
        ):
            # Both sides keep their provider: one key is never enough.
            raise ValueError(
                "This Max (API) run needs both an Anthropic and an OpenAI API key; "
                "add them in Settings"
            )
    owner_id = (user or {}).get("id") or run_data.get("owner_id")
    try:
        configured = _configure_auth_route(env, route=route, user_id=owner_id)
    except ValueError:
        # Records created before auth_route/live.auth_mode existed are
        # ambiguous. Preserve their historical behavior exactly once: use the
        # currently selected linked account when it is still connected,
        # otherwise pin the continuation to API. A recorded route never enters
        # this compatibility branch and therefore always fails closed.
        if not legacy_unpinned or route == "api_key" or "api_key" not in auth_modes:
            raise
        # A login waiting for Reconnect is still the selected account, and
        # Settings promises its API keys are never the fallback.
        if _account_awaits_reconnect(route, owner_id):
            raise
        route = "api_key"
        configured = _configure_auth_route(env, route=route, user_id=None)
    if sponsored_run and _is_kimi_model(model) and engine != "plain":
        from agent_monitor import sponsored_kimi_client

        _apply_bounded_run_env(
            configured,
            engine=engine,
            guest=bool(run_data.get("guest")),
            sponsored=True,
        )
        _apply_cgm_writeup_env(configured, engine=engine)
        configured.update(
            sponsored_kimi_client.issue_credentials(
                engine=_sponsored_transport_engine(engine),
                client_id=owner_id,
                cache_key=str(run_data.get("run_id") or "continuation"),
                minimum_ttl_seconds=_cli_timeout_seconds(engine, configured) + _SPONSORED_KIMI_TOKEN_MARGIN,
            )
        )
    if route == "api_key" and not sponsored_run:
        # As the run's start did: an own Kimi key to the operator's endpoint.
        _own_kimi_base_env(configured, engine=engine, model=model)
    if route == cgm_max.ROUTE:
        cgm_max.apply_models(configured, cgm_max.record_plan(run_data))
    elif route == cgm_max.API_ROUTE and cgm_max.is_max_api_record(run_data):
        cgm_max.apply_models_api(configured, cgm_max.record_plan_api(run_data))
    from agent_monitor.run_tuning import from_record as apply_recorded_tuning

    tuning = apply_recorded_tuning(configured, run_data) or {}
    _raise_long_effort_timeouts(
        configured, engine=engine, route=route, effort=tuning.get("reasoning_effort")
    )
    return configured, route


def _apply_kimi_compatible_env(env: dict[str, str], model: str | None) -> None:
    """Expose a selected Kimi connection to OpenAI-compatible harness SDKs.

    These aliases live only in the run subprocess environment; the user's
    stored OpenAI connection is never replaced.
    """
    if not str(model or "").strip().lower().startswith("kimi-"):
        return
    key = str(env.get("KIMI_API_KEY") or "").strip()
    if not key:
        return
    base = str(
        env.get("KIMI_API_BASE")
        or "https://2qg3r7w8aefiukbds.flashflame.ai/v1"
    ).rstrip("/")
    env["OPENAI_API_KEY"] = key
    env["OPENAI_BASE_URL"] = base
    env["OPENAI_API_BASE"] = base
    env["AGENT_MONITOR_BASE_URL"] = base
    # The sponsored broker issues KIMI_API_BASE; the tool-free Kimi harness
    # reads KIMI_BASE_URL. Mirror the broker route so both agree, and leave an
    # explicitly configured KIMI_BASE_URL untouched otherwise.
    if str(env.get("KIMI_API_BASE") or "").strip():
        env["KIMI_BASE_URL"] = base
    env["LLM_API_KEY"] = key
    env["LLM_BASE_URL"] = base


def _update_job(job_id: str, **fields: Any) -> None:
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)
            _JOBS[job_id]["updated_at"] = _now()


def _record_run_memory(
    *,
    guest: bool,
    run_id: str,
    engine: str,
    problem_id: str,
    status: str,
    problem_text: str,
    outcome: str,
    final_out: str,
    owner_id: int | None = None,
) -> None:
    """Best-effort shared-memory update for operator runs only.

    The library is the operator's shared profile: anonymous guest input and
    output never mutate it, and neither do other signed-in accounts (the same
    operator-only rule as the library write routes).  ``owner_id`` ``None`` is
    the local, sign-in-free operator.  A run deleted while it ran leaves no
    memory of it.
    """
    if guest or _run_deleted(run_id):
        return
    try:
        if owner_id is not None:
            from agent_monitor.auth import is_operator_account

            if not is_operator_account(int(owner_id)):
                return
        from agent_monitor import library as user_library

        user_library.record_run_memory(
            run_id=run_id,
            engine=engine,
            problem_id=problem_id,
            status=status,
            summary=(
                f"Problem: {problem_text[:600]}\n\n"
                f"Outcome: {outcome}\n\n"
                f"Final answer (excerpt):\n{final_out[:1200]}"
            ),
        )
    except Exception:  # noqa: BLE001
        pass


def _execute_job(
    *,
    job_id: str,
    run_id: str,
    engine: str,
    problem_id: str,
    problem_text: str,
    problem_path: str | None,
    model: str | None,
    max_iterations: int,
    workspace: str,
    extra_env: dict[str, str] | None = None,
    use_subagents: bool = True,
    subagent_model: str | None = None,
    reasoning_effort: str = "default",
    speed_mode: str = "standard",
    owner_id: int | None = None,
    guest: bool = False,
    run_kind: str = "prove",
) -> None:
    started = time.time()
    ws = Path(workspace)
    extra_env = dict(extra_env or {})
    if run_kind == "analyze" or engine == _ANALYZE_ENGINE:
        run_kind = "analyze"
    elif run_kind == "readability" or engine == _READABILITY_ENGINE:
        run_kind = "readability"
    # A proof run's first turn: its proof is kept when it ends (proof_turns).
    turn = _proof_turn_begin(run_id, ws) if run_kind == "prove" else None
    try:
        _execute_job_body(
            job_id=job_id, run_id=run_id, engine=engine, problem_id=problem_id,
            problem_text=problem_text, problem_path=problem_path, model=model,
            max_iterations=max_iterations, workspace=workspace, extra_env=extra_env,
            use_subagents=use_subagents, subagent_model=subagent_model,
            reasoning_effort=reasoning_effort, speed_mode=speed_mode,
            owner_id=owner_id, guest=guest, run_kind=run_kind,
            started=started, ws=ws, turn=turn,
        )
    finally:
        # Ended already unless the body failed before its own end.
        _proof_turn_end(run_id, ws, turn)
        # Done already unless the body failed before its own end.
        _end_worker(job_id)
        if run_kind in {"analyze", "readability"}:
            # Every exit path (finished, failed, stopped, crashed) frees the
            # sponsored slot; a server restart starts with none held.
            from agent_monitor import analyze_runs

            analyze_runs.GATE.release(run_id)


def _execute_job_body(
    *,
    job_id: str,
    run_id: str,
    engine: str,
    problem_id: str,
    problem_text: str,
    problem_path: str | None,
    model: str | None,
    max_iterations: int,
    workspace: str,
    extra_env: dict[str, str],
    use_subagents: bool,
    subagent_model: str | None,
    reasoning_effort: str,
    speed_mode: str,
    owner_id: int | None,
    guest: bool,
    run_kind: str,
    started: float,
    ws: Path,
    turn: int | None = None,
) -> None:
    # A manuscript run (Analyze, Readability) reads someone's manuscript with
    # a fixed prompt pack: no library, no DAG memory, no persona, no
    # attachment excerpts, and no proof in its outcome.
    analyze_run = run_kind == "analyze"
    readability_run = run_kind == "readability"
    manuscript_run = analyze_run or readability_run
    public_kimi_runtime = (
        os.environ.get("AGENT_MONITOR_PUBLIC_KIMI", "").strip() == "1"
    )
    sponsored_kimi_runtime = (
        extra_env.get("AGENT_MONITOR_SPONSORED_KIMI", "").strip() == "1"
    )
    _apply_kimi_compatible_env(extra_env, model)
    _append_chat(run_id, "system", f"▶ run started · engine {engine} · problem {problem_id}" + (f" · model {model}" if model else "") + (f" · subagent model {subagent_model}" if subagent_model else ""))
    # Inject the user library (memory / skills / tools) into every engine run:
    # files land in <ws>/_library/ and the prompt gets a USER LIBRARY block.
    # The tool-free CGM pipeline can neither run those tools nor read those
    # files, so it gets neither (the published DAG memory below still applies).
    lib_ctx = ""
    runtime_library: dict[str, Any] = {
        "materialized": [],
        "enabled_skills": [],
        "enabled_tools": [],
        "persistent_skill_mutation": False,
    }
    if not guest and not manuscript_run and engine not in _CGM_ENGINES:
        # An analysis checks someone else's manuscript with a fixed prompt
        # pack: neither the user library nor published DAG memory applies.
        try:
            if public_kimi_runtime or sponsored_kimi_runtime:
                raise LookupError("user library disabled for isolated Kimi service")
            from agent_monitor import library as user_library

            # Records of the operator's own runs reach only the operator's runs.
            if owner_id is None:
                operator_run = True
            else:
                from agent_monitor.auth import is_operator_account

                operator_run = is_operator_account(int(owner_id))
            materialized = user_library.materialize(ws, run_memories=operator_run)
            lib_ctx = user_library.compose_context(run_memories=operator_run)
            library_snapshot = user_library.get_library()
            runtime_library = {
                "materialized": list(materialized.get("written") or []),
                "enabled_skills": [
                    item.get("name") for item in library_snapshot.get("items") or []
                    if item.get("type") == "skill" and item.get("enabled", True)
                ],
                "enabled_tools": [
                    item.get("name") for item in library_snapshot.get("items") or []
                    if item.get("type") == "tool" and item.get("enabled", True)
                ],
                "persistent_skill_mutation": False,
            }
            if materialized.get("written"):
                _append_chat(
                    run_id, "system",
                    "library injected: " + ", ".join(materialized["written"]),
                )
        except Exception:  # noqa: BLE001
            lib_ctx = ""
    if not guest and engine == "hermes":
        # Native Hermes reads profile skills from HERMES_HOME, but its linked
        # Codex app-server is workspace-confined.  Materialize the same package
        # paths advertised to portable harnesses so either execution route can
        # honor an explicit `_agent/skills/<name>/SKILL.md` audit request.
        try:
            from agent_monitor.agent_config import materialize_enabled_skills

            visible_skills = materialize_enabled_skills(ws)
            if visible_skills:
                _append_chat(
                    run_id,
                    "system",
                    "profile skills materialized: "
                    + ", ".join(sorted(visible_skills)),
                )
        except Exception as exc:  # noqa: BLE001
            _append_chat(run_id, "system", f"profile skill materialization unavailable: {exc}")
    # Only explicitly published, provenance-gated DAG results are retrievable;
    # community discussions never become executable harness instructions.
    if not guest and not manuscript_run and not public_kimi_runtime and not sponsored_kimi_runtime:
        try:
            from agent_monitor.research import compose_context as dag_context

            research_context = dag_context(problem_text)
            if research_context:
                lib_ctx = (lib_ctx + "\n\n" + research_context).strip()
                (ws / "dag_memory.md").write_text(research_context, encoding="utf-8")
                runtime_library["dag_memory"] = "dag_memory.md"
        except (OSError, ValueError, sqlite3.Error):
            pass
    base_problem_text = (lib_ctx + "\n" + problem_text) if lib_ctx else problem_text

    def with_attachments(attachment_context: str) -> str:
        text = base_problem_text
        if attachment_context:
            text += "\n\n" + attachment_context
        return _augment_research_audit_prompt(text)

    # The manuscript runners read their uploads from their request.json.
    # Plain's excerpts may need its PDFs extracted: they are added in the
    # run's try below, where a Stop or Delete meanwhile stops the run.
    plain_excerpts = engine == "plain" and not manuscript_run
    engine_problem_text = with_attachments(
        "" if manuscript_run or plain_excerpts else _attachment_context(engine, ws)
    )
    if not guest and engine in {"improof", "ucla"}:
        try:
            from agent_monitor.agent_config import persona_preamble

            profile_ctx = persona_preamble(workspace=ws)
        except Exception:  # noqa: BLE001
            profile_ctx = ""
        if profile_ctx:
            engine_problem_text = profile_ctx + "\n" + engine_problem_text
            _append_chat(run_id, "system", "native SKILL.md profile injected into this harness")
    try:
        sponsored_plain = (
            (extra_env or {}).get("AGENT_MONITOR_SPONSORED_KIMI") == "1"
            and engine == "plain"
        )
        if plain_excerpts:
            # A sponsored problem has a hard length limit: its excerpts fit it.
            engine_problem_text = with_attachments(
                _plain_attachments(
                    run_id, ws, compose=with_attachments if sponsored_plain else None
                )
            )
        if sponsored_plain:
            result_run = _run_sponsored_kimi_plain(
                run_id=run_id,
                problem_id=problem_id,
                problem_text=engine_problem_text,
                display_problem_text=problem_text,
                started=started,
                workspace=ws,
                owner_id=owner_id,
            )
        elif engine == "hermes":
            result_run = _run_hermes(
                run_id=run_id,
                problem_id=problem_id,
                problem_text=engine_problem_text,
                display_problem_text=problem_text,
                model=model,
                max_iterations=max_iterations,
                started=started,
                workspace=ws,
                extra_env=extra_env,
                use_subagents=use_subagents,
                subagent_model=subagent_model,
                owner_id=owner_id,
            )
        elif engine == "improof":
            from agent_monitor.runners import improof as improof_runner

            path = ws / "problem.txt"
            # Library context and research attachments both augment the prompt;
            # either one means the engine must read the augmented file.
            if engine_problem_text != problem_text:
                path = ws / "problem_with_library.txt"
                path.write_text(engine_problem_text, encoding="utf-8")
            improof_args = None
            if model and (extra_env or {}).get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
                improof_args = [
                    "--component", f"cfg_codex_author.model={model}",
                    "--component", f"cfg_codex_critic.model={model}",
                ]
            elif model and (extra_env or {}).get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
                improof_args = [
                    "--component", f"cfg_claude_author.model={model}",
                    "--component", f"cfg_claude_critic.model={model}",
                ]
                # The Claude worker is not a codex command, so the component
                # model never reaches its command line: it reads this variable
                # (and otherwise falls back to Haiku).
                extra_env = {**(extra_env or {}), "AGENT_MONITOR_CLAUDE_MODEL": model}
            elif str(model or "").lower().startswith("kimi-"):
                improof_args = ["--model", "*=models/kimi/k3"]
            result = improof_runner.run_problem(
                path,
                problem_id=problem_id,
                output_dir=ws,
                extra_args=improof_args,
                extra_env=extra_env,
                research_model=model,
                on_start=lambda p: _register_proc(run_id, p),
                on_output=_live_output_flusher(
                    run_id=run_id, engine="improof", problem_id=problem_id,
                    problem_text=problem_text, started=started, workspace=ws,
                ),
            )
            _unregister_proc(run_id)
            result_run = _wrap_subprocess_result(
                run_id=run_id,
                engine="improof",
                problem_id=problem_id,
                problem_text=problem_text,
                result=result,
                started=started,
                workspace=ws,
            )
        elif engine == "ucla":
            from agent_monitor.runners import ucla as ucla_runner

            path = ws / "problem.txt"
            # Library context and research attachments both augment the prompt;
            # either one means the engine must read the augmented file.
            if engine_problem_text != problem_text:
                path = ws / "problem_with_library.txt"
                path.write_text(engine_problem_text, encoding="utf-8")
            result = ucla_runner.run_problem(
                path,
                problem_id=problem_id,
                model=model,
                output_dir=ws,
                extra_env=extra_env,
                on_start=lambda p: _register_proc(run_id, p),
                on_output=_live_output_flusher(
                    run_id=run_id, engine="ucla", problem_id=problem_id,
                    problem_text=problem_text, started=started, workspace=ws,
                ),
            )
            _unregister_proc(run_id)
            result_run = _wrap_subprocess_result(
                run_id=run_id,
                engine="ucla",
                problem_id=problem_id,
                problem_text=problem_text,
                result=result,
                started=started,
                workspace=ws,
            )
        else:
            if engine in _CGM_ENGINES:
                _prepare_cgm_sources(
                    run_id, ws, extra_env, task_text=engine_problem_text, message=problem_text
                )
            if not manuscript_run and (_run_deleted(run_id) or _stop_event(run_id).is_set()):
                # Stopped or deleted while the run was being prepared (Plain's
                # or a CGM turn's files read, say): no runner starts, and no
                # persona's skills are copied into a deleted workspace.  (A
                # manuscript run's runner records its own stopped outcome.)
                # The SOURCE MATERIAL just written is read by no runner, and
                # only _run_cli_engine would remove it.
                cgm_sources.discard(ws)
                raise StopRequested(run_id)
            result_run = _run_cli_engine_with_openclaude_retry(
                run_id=run_id,
                engine=engine,
                problem_id=problem_id,
                problem_text=engine_problem_text,
                started=started,
                workspace=ws,
                display_problem_text=problem_text,
                extra_env=extra_env,
                requested_model=model,
                max_iterations=max_iterations,
                owner_id=owner_id,
                # The operator's persona is written for agents with tools.
                include_persona=not guest and not manuscript_run and engine not in _CGM_ENGINES,
            )

        published_before_stop = _published_before_stop(result_run)
        if _stop_event(run_id).is_set() and not published_before_stop:
            result_run["status"] = "stopped"
        max_kind = cgm_max.route_mode(extra_env)
        max_run = max_kind is not None
        if max_kind == "max":
            result_run.setdefault("live", {})["auth_mode"] = cgm_max.ROUTE
            final_route = cgm_max.ROUTE
        elif max_kind == cgm_max.API_ROUTE_MODE:
            result_run.setdefault("live", {})["auth_mode"] = cgm_max.API_ROUTE
            final_route = cgm_max.API_ROUTE
        elif (extra_env or {}).get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
            result_run.setdefault("live", {})["auth_mode"] = "claude_subscription"
            final_route = "claude_subscription"
        elif (extra_env or {}).get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
            result_run.setdefault("live", {})["auth_mode"] = "chatgpt_subscription"
            final_route = "codex_subscription"
        else:
            final_route = "api_key"
        effective_result_model = str(
            cgm_max.MODEL_ID
            if max_kind == "max"
            else cgm_max.API_MODEL_ID
            if max_kind == cgm_max.API_ROUTE_MODE
            else (result_run.get("live") or {}).get("model") or model or ""
        ).strip()
        if effective_result_model:
            result_run["model"] = effective_result_model
            result_run.setdefault("live", {}).setdefault(
                "model", effective_result_model
            )
            if model:
                # A Max run names its mode; route_plan names both models.
                result_run.setdefault("live", {})["model_requested"] = not max_run
        if model:
            # Provider telemetry may be a resolved Claude name that is useful
            # for attestation but invalid as a future account-model selector.
            result_run["requested_model"] = model
            result_run.setdefault("live", {})["requested_model"] = model
        result_run["job_id"] = job_id
        result_run["auth_route"] = final_route
        result_run["credential_source"] = (
            "sponsored_kimi"
            if (extra_env or {}).get("AGENT_MONITOR_SPONSORED_KIMI") == "1"
            else "user"
        )
        if result_run["credential_source"] == "sponsored_kimi":
            result_run["sponsored_kimi_proxy"] = "short_lived_loopback_v1"
        result_run["workspace"] = str(ws)
        result_run["guest"] = bool(guest)
        result_run["use_subagents"] = bool(use_subagents)
        result_run["subagent_model"] = subagent_model if use_subagents else None
        result_run["reasoning_effort"] = reasoning_effort
        result_run["speed_mode"] = speed_mode
        result_run["max_iterations"] = max_iterations
        result_run["runtime_library"] = runtime_library
        result_run = normalize_run(result_run, engine=engine)
        _attach_analysis(result_run, ws)
        if analyze_run:
            _finalize_analyze_record(result_run, ws)
        elif readability_run:
            _finalize_readability_record(result_run, ws)
        result_run["updated_at"] = _now()
        _write_run(result_run)
        status = result_run.get("status") or "finished"
        if status not in {"finished", "failed", "stopped"}:
            status = "finished"
        if result_run.get("completed") is False and status == "finished":
            status = "failed"
        _update_job(job_id, status=status, error=result_run.get("error"))
        # Full record in the human-in-the-loop chat: summary + final answer.
        summary = _run_finished_line(result_run, status, time.time() - started, analyze=analyze_run,
                                     readability=readability_run)
        _append_chat(run_id, "system", summary)
        final_out = ""
        for a in reversed(result_run.get("agents") or []):
            if a.get("output"):
                final_out = str(a["output"])
                break
        if analyze_run:
            # An analysis's last agent is its process log: the Progress panel
            # gets a plain summary instead (the report is in the Sandbox).
            _append_chat(run_id, "system", _analyze_chat_summary(result_run, status))
        elif readability_run:
            _append_chat(run_id, "system", _readability_chat_summary(result_run, status))
        elif final_out:
            _append_chat(run_id, "assistant", final_out[:3000])
        # Neither the standalone public service nor an anonymous guest run
        # imports, reads, or writes the operator's persistent user library.
        if not public_kimi_runtime:
            _record_run_memory(
                guest=guest,
                run_id=run_id,
                engine=engine,
                problem_id=problem_id,
                status=status,
                problem_text=(
                    _analyze_memory_label(result_run) if analyze_run
                    else _readability_memory_label(result_run) if readability_run
                    else problem_text
                ),
                outcome=summary,
                # Manuscript text is untrusted: a manuscript run adds its label
                # and outcome to the operator's memory, never excerpts.
                final_out="" if manuscript_run else final_out,
                owner_id=owner_id,
            )
        if guest and status == "finished" and not public_kimi_runtime and not readability_run:
            # Guests get the bounded derived Formal/DAG pipeline as soon as the
            # proof exists; opening the run later remains a fallback.  A
            # readability run has no proof and no derived views.
            _notify_guest_completion(run_id, owner_id, ws)
    except StopRequested:
        try:
            with _RUN_WRITE_LOCK:  # no stale copy over a message recorded meanwhile
                stopped_run = _load_run_record(run_id)
                stopped_run["status"] = "stopped"
                stopped_run["updated_at"] = _now()
                _write_run(stopped_run)
        except FileNotFoundError:
            pass  # the run was deleted, which stops it too
        _update_job(job_id, status="stopped")
        _append_chat(run_id, "system", "■ run stopped by user")
    except Exception as exc:  # noqa: BLE001
        _append_chat(run_id, "system", f"✗ run failed: {exc}")
        err = f"{exc}\n{traceback.format_exc()}"
        fail = normalize_run(
            {
                "run_id": run_id,
                "job_id": job_id,
                "problem_id": problem_id,
                "trace_name": f"[{engine.upper()}] {problem_id}",
                "status": "failed",
                "error": str(exc),
                "created_at": _now(),
                "updated_at": _now(),
                "workspace": str(ws),
                "owner_id": owner_id,
                "guest": bool(guest),
                "pipeline": _pipeline_for(engine),
                "problem_text": problem_text[:20000],
                "reasoning_effort": reasoning_effort,
                "speed_mode": speed_mode,
                "max_iterations": max_iterations,
                **({"run_kind": run_kind} if manuscript_run else {}),
                "agents": [
                    {
                        "trace_id": f"{run_id}::error",
                        "stage_name": "error",
                        "role": "system",
                        "pipeline_stage": "finalize",
                        "prompt": problem_text[:2000],
                        "output": err[-8000:],
                        "status": "failed",
                    }
                ],
                "edges": [],
                "totals": {"latency_s": time.time() - started},
            },
            engine=engine,  # type: ignore[arg-type]
        )
        _write_run(fail)
        _update_job(job_id, status="failed", error=str(exc))
    finally:
        # The turn ends before the feedback queued for the next one starts.
        _proof_turn_end(run_id, ws, turn)
        # This worker is done with the run (runner, record, chat, prompt
        # file): what it starts now, or a Continue, never runs beside it.
        _end_worker(job_id)
        if not guest and not public_kimi_runtime:
            _launch_pending_feedback(
                run_id, owner_id=owner_id, model=model, max_iterations=max_iterations
            )


def _workspace_memory(workspace: Path | None) -> dict[str, Any] | None:
    """Compact workspace snapshot for the Memory tab."""
    if not workspace or not workspace.exists():
        return None
    files = []
    try:
        for p in sorted(workspace.rglob("*")):
            if p.is_file() and ".git" not in p.parts:
                rel = p.relative_to(workspace)
                files.append(f"{rel} ({p.stat().st_size} B)")
            if len(files) >= 40:
                break
    except OSError:
        return None
    return {"workspace_files": "\n".join(files) or "(empty)"} if files else None


def _estimate_cost(
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float | None:
    """Estimate USD cost from token counts via LiteLLM pricing (best-effort)."""
    if not (input_tokens or output_tokens or cache_read or cache_write):
        return None
    try:
        from llm_cost_tracker.pricing import get_cache_pricing, get_token_price, has_real_pricing

        m = (model or "").strip() or "gpt-4o"
        if not has_real_pricing(m):
            # Fallback: treat unknown as gpt-4o-class rates so Monitor isn't all zeros.
            m = "gpt-4o"
        inp, out = get_token_price(m)
        cr, cw = get_cache_pricing(m)
        cost = (
            input_tokens * inp / 1_000_000
            + cache_read * cr / 1_000_000
            + cache_write * cw / 1_000_000
            + output_tokens * out / 1_000_000
        )
        return round(cost, 6) if cost > 0 else None
    except Exception:  # noqa: BLE001
        # Last-resort static rates (gpt-4o-ish).
        cost = (input_tokens + cache_read + cache_write) * 2.5 / 1_000_000 + output_tokens * 10 / 1_000_000
        return round(cost, 6) if cost > 0 else None


def _has_price(model: str) -> bool:
    try:
        from llm_cost_tracker.pricing import has_real_pricing

        return has_real_pricing(model)
    except Exception:  # noqa: BLE001
        return False


# The provider keys _resolve_llm_provider reads (see _requested_model_id).
_RESOLVER_KEY_NAMES = (
    "OPENAI_API_KEY", "KIMI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY", "GROQ_API_KEY", "TOGETHERAI_API_KEY", "XAI_API_KEY",
)


def _requested_model_id(model: str | None) -> str | None:
    """``model`` as an API call requests it: a provider prefix
    ("openai:gpt-5.2") resolved as the API runners resolve it
    (proof_graph._resolve_llm_provider, as math_harness_runner._requested_id).

    The id never depends on the keys set (they only decide whether the call
    can be made), so a stand-in for each lets it resolve whatever the run's
    environment held.  A name no API provider takes ("opus", "max") is kept
    as it is.  Never raises."""
    name = str(model or "").strip()
    if not name:
        return None
    from agent_monitor.proof_graph import _resolve_llm_provider

    try:
        return _resolve_llm_provider(dict.fromkeys(_RESOLVER_KEY_NAMES, "-"), name)[3]
    except (TypeError, ValueError):
        return name


def _exact_price_model(model: str | None) -> str | None:
    """The id ``model`` requests (a provider prefix resolved,
    ``_requested_model_id``) if the pricing table has an exact entry for it,
    else None.

    Calls that name no priced model are priced at the model their run
    selected only through this check.  A selection may be a route alias
    (Claude Code's "opus", "sonnet" or "fable"; Max's "max") that the
    lookup's family match resolves to an unrelated model's price ("opus" to
    Claude 3 Opus), so only an exact entry is trusted; None leaves
    ``_estimate_cost`` its gpt-4o fallback.
    """
    name = _requested_model_id(model)
    if not name:
        return None
    try:
        from llm_cost_tracker.pricing import has_exact_pricing

        return name if has_exact_pricing(name) else None
    except Exception:  # noqa: BLE001 - a price estimate is best-effort
        return None


def _root_proof_artifact(workspace: Path | None) -> Path | None:
    """Return the newest non-empty console proof artifact from the root."""
    if not workspace:
        return None
    candidates: list[Path] = []
    for name in ("proof.md", "proof.tex"):
        candidate = workspace / name
        try:
            if candidate.is_file() and candidate.stat().st_size >= 40:
                candidates.append(candidate)
        except OSError:
            continue
    return (
        max(candidates, key=lambda path: path.stat().st_mtime)
        if candidates
        else None
    )


def _promote_proof_artifact(workspace: Path | None) -> Path | None:
    """Expose the newest harness proof at the console's stable root path."""
    if not workspace:
        return None
    candidates: list[Path] = []
    try:
        for candidate in (*workspace.rglob("*.md"), *workspace.rglob("*.tex")):
            lowered = candidate.name.lower()
            if not any(word in lowered for word in ("proof", "solution", "answer")):
                continue
            if "_library" in candidate.parts:
                continue
            # A turn's own reply or kept copy (proof_turns) is never the proof.
            if candidate.name == ".turn_reply.md" or ".proof_turns" in candidate.parts:
                continue
            if candidate.stat().st_size >= 40:
                candidates.append(candidate)
    except OSError:
        return None
    if not candidates:
        return None
    source = max(candidates, key=lambda path: path.stat().st_mtime)
    target = workspace / ("proof.md" if source.suffix.lower() == ".md" else "proof.tex")
    try:
        if source.resolve() == target.resolve():
            return target
    except OSError:
        return None
    try:
        shutil.copy2(source, target)
    except OSError:
        return None
    return target


def _persist_openclaw_final_answer(parser: Any, workspace: Path) -> Path | None:
    """Keep a complete OpenClaw reply when it did not invoke a file-write tool."""
    existing = _promote_proof_artifact(workspace)
    if existing:
        return existing
    final = str(parser.final_message() or "").strip()
    if len(final) < 40:
        return None
    target = workspace / "proof.md"
    target.write_text(final + "\n", encoding="utf-8")
    parser.lines.append("[agent-monitor] saved OpenClaw's final answer as proof.md")
    return target


def _persist_text_final_answer(final: Any, workspace: Path, *, engine: str) -> Path | None:
    """Persist a complete text response when an agent forgot to write its artifact."""
    text = str(final or "").strip()
    if engine.casefold() in {"kimi", "plain"}:
        text = normalize_proof_markdown(text)
    existing = _promote_proof_artifact(workspace)
    normalized_final = text.casefold().replace("’", "'").replace("`", "")
    def has_explicit_placeholder(value: str) -> bool:
        normalized = value.casefold().replace("’", "'").replace("`", "")
        return any(
            marker in normalized
            for marker in (
                "to be refined",
                "proof.md remains an incomplete draft",
                "file remains an incomplete draft",
            )
        )

    reports_failed_update = engine.casefold() == "hermes" and (
        any(
            marker in normalized_final
            for marker in (
                "could not complete the required file update",
                "couldn't complete the required file update",
                "unable to complete the required file update",
                "failed to complete the required file update",
            )
        )
        or (
            "proof.md" in normalized_final
            and any(
                marker in normalized_final
                for marker in (
                    "could not update",
                    "couldn't update",
                    "unable to update",
                    "failed to update",
                )
            )
        )
    )
    reports_incomplete = engine.casefold() == "hermes" and any(
        marker in normalized_final
        for marker in (
            "proof.md remains an incomplete draft",
            "proof.tex remains an incomplete draft",
            "file remains an incomplete draft",
            "proof.md was not saved",
            "proof.tex was not saved",
            "could not create proof.md",
            "couldn't create proof.md",
            "unable to create proof.md",
            "failed to finalize proof.md",
            "failed while finalizing proof.md",
        )
    ) or reports_failed_update
    marker = "--- proof.md ---"
    try:
        existing_text = existing.read_text(encoding="utf-8").strip() if existing else ""
    except OSError:
        existing_text = ""
    if engine.casefold() == "hermes" and has_explicit_placeholder(existing_text):
        reports_incomplete = True
    prefix, embedded = (text.split(marker, 1) if marker in text else (text, ""))
    embedded = embedded.strip()
    embedded_is_new = (
        len(embedded) >= 40
        and embedded != existing_text
        and not has_explicit_placeholder(embedded)
    )
    if existing and not reports_incomplete and not embedded_is_new:
        return existing
    if embedded_is_new:
        # In Landlock compatibility mode the embedded Codex agent is
        # deliberately read-only. The monitor is the trusted writer and may
        # promote only a substantive artifact that differs from the old file.
        text = embedded
    elif reports_incomplete and marker in text:
        if reports_failed_update:
            return None
        # Never re-promote the exact stale draft that the final response just
        # declared incomplete; retain any substantive diagnostic prefix.
        text = prefix.strip()
    elif reports_failed_update:
        return None
    elif not existing and marker in text:
        embedded = text.split(marker, 1)[1].strip()
        if len(embedded) >= 40:
            text = embedded
    if len(text) < 40:
        return None
    # Avoid wrapping an already-complete Markdown response in a second fence.
    if text.startswith("```markdown") and text.endswith("```"):
        text = text[len("```markdown") : -3].strip()
    if engine.casefold() == "hermes":
        # The embedded Codex transport can return a valid mathematical answer
        # preceded or followed by an operational note that its own sandbox could
        # not write proof.md. The monitor is the trusted artifact writer and is
        # about to save that answer, so either edge note is stale and noisy. Do
        # not remove matching text from the middle: it may be part of a genuine
        # discussion of the mathematical task or its reproducibility.
        paragraphs = text.split("\n\n")
        def stale_artifact_note(paragraph: str) -> bool:
            normalized = paragraph.casefold().replace("’", "'").replace("`", "")
            unable = any(
                marker in normalized
                for marker in ("could not", "couldn't", "unable to", "failed to")
            )
            operational = "proof.md" in normalized and any(
                marker in normalized
                for marker in ("permission", "sandbox", "filesystem", "environment", "write")
            )
            return unable and operational

        # A scoped-network proxy can fail before *every* tool launch on kernels
        # that cannot create its loopback namespace. Remove only sentences
        # carrying that exact runtime signature, even when the model put them
        # in a middle reproducibility section; preserve mathematical content
        # and the honest statement that the literature audit is incomplete.
        if "bwrap: loopback: failed rtm_newaddr" in text.casefold():
            import re

            cleaned: list[str] = []
            for paragraph in paragraphs:
                sentences = re.split(r"(?<=[.!?])\s+", paragraph)
                kept = [
                    sentence
                    for sentence in sentences
                    if not any(
                        marker in sentence.casefold()
                        for marker in (
                            "bwrap: loopback: failed rtm_newaddr",
                            "request to run outside that sandbox",
                            "direct web open/search calls",
                            "remains an incomplete draft",
                        )
                    )
                ]
                paragraph = " ".join(kept).strip()
                if paragraph.casefold().startswith("accordingly,"):
                    paragraph = (
                        "No reproducible primary-source search was completed in this run; "
                        + paragraph[12:].lstrip()
                    )
                if paragraph:
                    cleaned.append(paragraph)
            paragraphs = cleaned

        while paragraphs and stale_artifact_note(paragraphs[0]):
            paragraphs.pop(0)
        while paragraphs and stale_artifact_note(paragraphs[-1]):
            paragraphs.pop()
        text = "\n\n".join(paragraphs).strip()
        if len(text) < 40:
            return None
    target = workspace / "proof.md"
    target.write_text(text + "\n", encoding="utf-8")
    return target


def _attach_proof_provenance(agents: list[dict[str, Any]], workspace: Path | None) -> None:
    """Attach the final proof once so Agent Trace can attribute its lines.

    Generic CLI traces often contain many message-shaped contributors but only
    one shared workspace artifact. Attaching the complete proof to every such
    node both falsely gives every turn the same contribution and makes final
    provenance quadratic in the number of turns. Native multi-agent artifact
    enrichment still runs later and can add each agent's distinct text.
    """
    if not workspace:
        return
    proof = workspace / "proof.md"
    if not proof.exists():
        proof = workspace / "proof.tex"
    if not proof.exists():
        # Common alternates
        for cand in list(workspace.rglob("*.md")) + list(workspace.rglob("*.tex")):
            if cand.name in {"proof.md", "proof.tex", "solution.tex", "answer.tex"} or "proof" in cand.name:
                proof = cand
                break
        else:
            return
    try:
        tex = proof.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if len(tex.strip()) < 40:
        return
    eligible: list[dict[str, Any]] = []
    for a in agents:
        role = (a.get("role") or "").lower()
        pipe = str(a.get("pipeline_stage") or "").lower()
        stage = (a.get("stage_name") or "").lower()
        if role in {"writer", "prover", "assistant", "author"} or pipe in {
            "draft",
            "write",
            "finalize",
            "author",
        } or "message" in stage or "final" in stage or "write" in stage:
            eligible.append(a)
    if not eligible:
        return

    # Agent order is chronological for all runner parsers. Attribute the
    # shared final artifact to the last eligible contributor; artifact-backed
    # harnesses may subsequently enrich earlier agents with their own outputs.
    target = eligible[-1]
    marker = f"\n\n--- {proof.name} ---\n"
    for agent in eligible[:-1]:
        if agent.get("_provenance_text") == tex:
            agent.pop("_provenance_text", None)
        if agent.get("output_source") == f"workspace {proof.name}":
            output = str(agent.get("output") or "")
            if marker in output:
                agent["output"] = output.split(marker, 1)[0]
                agent.pop("output_source", None)
    target["_provenance_text"] = tex
    # Also surface a short preview in output if it is only a tool log.
    out = target.get("output") or ""
    if tex[:200] not in out and "\\documentclass" not in out:
        target["output"] = (out + marker + tex)[:12000]
        target.setdefault("output_source", f"workspace {proof.name}")


def _attach_final_latex(run: dict[str, Any]) -> None:
    try:
        from harness_dashboard.latex_provenance import build_final_latex_bundle

        bundle = build_final_latex_bundle(run)
        if bundle:
            run["final_latex"] = bundle
    except Exception:  # noqa: BLE001
        pass


def _normalize_terminal_kimi_outcome(
    run: dict[str, Any], workspace: Path | None
) -> bool:
    """Publish one conservative outcome footer for finished Kimi Markdown."""
    if str(run.get("status") or "").strip().lower() != "finished" or not workspace:
        return False
    # Math Harness publishes ``proof.md`` and its graph receipt as one
    # content-bound generation, with the receipt deliberately written last.
    # Mutating that proof during generic Kimi cleanup would invalidate the
    # otherwise valid receipt and make the Engine DAG fail closed.  Its critic
    # verdict remains explicit in the receipt/UI instead of this legacy footer.
    # The ``kimi`` engine delegates to the same pipeline, so the receipt
    # itself (not the engine id) decides.
    if str(run.get("engine") or "").strip().lower() == "math_harness":
        return False
    try:
        receipt = workspace / "math_harness" / "project" / "engine_receipt.json"
        if receipt.exists() or receipt.is_symlink():
            return False
    except OSError:
        return False
    live = run.get("live") if isinstance(run.get("live"), dict) else {}
    model = str(run.get("model") or live.get("model") or "").strip()
    sponsored = (
        str(run.get("credential_source") or "").strip().lower()
        == "sponsored_kimi"
    )
    if not sponsored and not _is_kimi_model(model):
        return False
    try:
        from agent_monitor import proof_graph
        from agent_monitor.runners.outcome_contract import canonicalize_outcome_text

        original = proof_graph._read_workspace_text(
            workspace, "proof.md", limit=2 * 1024 * 1024
        )
        if len(original.strip()) < 40:
            return False
        rendered = canonicalize_outcome_text(original)
        if rendered == original:
            return False
        target = workspace / "proof.md"
        if target.is_symlink() or target.resolve().parent != workspace.resolve():
            return False
        _atomic_write_text(target, rendered)
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def _attach_analysis(run: dict[str, Any], workspace: Path | None) -> dict[str, Any]:
    """Add the Run Analysis / Agent Flow block the dashboard renders.

    (The Monitor's ``run["analysis"]`` block; an Analyze run's results are
    ``run["analyze"]``.)  A manuscript run (Analyze, Readability) has no
    proof: its workspace holds a third-party manuscript under
    ``analysis/source`` or ``readability/source``, so no proof file is looked
    for, rewritten or attributed.
    """
    manuscript_run = str(run.get("engine") or "") in _MANUSCRIPT_ENGINES
    if not manuscript_run:
        _normalize_terminal_kimi_outcome(run, workspace)
    try:
        from harness_dashboard.analysis import compute_run_analysis

        run["analysis"] = compute_run_analysis(
            run.get("agents") or [],
            run.get("totals") or {},
            run_output_dir=workspace,
            pipeline=run.get("pipeline") or [],
            edges=run.get("edges") or [],
        )
    except Exception:  # noqa: BLE001 — analysis is best-effort
        pass
    if run.get("status") in {"finished", "failed", "stopped"} and not manuscript_run:
        _attach_proof_provenance(run.get("agents") or [], workspace)
        _attach_final_latex(run)
    return run


def _analyze_title(block: dict[str, Any] | None) -> str:
    from agent_monitor.analyze.titles import plain_title

    source = (block or {}).get("source") or {}
    return plain_title(source.get("title"), 160)


def _finalize_analyze_record(run: dict[str, Any], workspace: Path) -> None:
    """Store ``run["analyze"]`` (never ``run["analysis"]``) and the run-list
    label "Analyze · <title>" once the runner has exited."""
    from agent_monitor.analyze import record as analyze_record

    run["run_kind"] = "analyze"
    try:
        stored = dict(_load_run_record(str(run.get("run_id"))).get("analyze") or {})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        stored = {}
    try:
        block = analyze_record.run_block(workspace)
    except Exception:  # noqa: BLE001 - a damaged artifact must not lose the run
        block = None
    block = dict(block if block is not None else stored)
    # Its questions, re-checks and their tallies live only in the record.
    for key in (*ASK_KEYS, "recheck", "rechecks"):
        if stored.get(key) and key not in block:
            block[key] = stored[key]
    run["analyze"] = block
    title = _analyze_title(block)
    if not title:
        return
    focus = str(block.get("focus") or "")
    from agent_monitor.analyze_runs import problem_text as analyze_problem_text

    text = analyze_problem_text(title, focus)
    run["problem_text"] = text
    run["problem_text_preview"] = text[:500]
    run["problems"] = _seed_problems(text, source="initial")
    try:
        (workspace / "problem.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass


def _analyze_running_label(workspace: Path) -> str:
    """"Analyze · <title>" once the running analysis has read its manuscript
    (``analysis/manuscript.json`` exists), else ``""``."""
    if not (workspace / "analysis" / "manuscript.json").is_file():
        return ""
    from agent_monitor.analyze import record as analyze_record
    from agent_monitor.analyze_runs import problem_text as analyze_problem_text

    try:
        block = analyze_record.run_block(workspace) or {}
    except Exception:  # noqa: BLE001 - the label waits for the finished record
        return ""
    title = _analyze_title(block)
    return analyze_problem_text(title, str(block.get("focus") or "")) if title else ""


def _finalize_readability_record(run: dict[str, Any], workspace: Path) -> None:
    """Store ``run["readability"]`` (keeping its questions) and the run-list
    label "Readability · <title>" once the runner has exited."""
    from agent_monitor.readability import record as readability_record

    run["run_kind"] = "readability"
    try:
        stored = dict(_load_run_record(str(run.get("run_id"))).get("readability") or {})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        stored = {}
    try:
        block = readability_record.run_block(workspace)
    except Exception:  # noqa: BLE001 - a damaged artifact must not lose the run
        block = None
    block = dict(block if block is not None else stored)
    for key in ASK_KEYS:
        if stored.get(key) and key not in block:
            block[key] = stored[key]
    run["readability"] = block
    title = _analyze_title(block)
    if not title:
        return
    from agent_monitor.readability_runs import problem_text as readability_problem_text

    text = readability_problem_text(title, str(block.get("focus") or ""))
    run["problem_text"] = text
    run["problem_text_preview"] = text[:500]
    run["problems"] = _seed_problems(text, source="initial")
    try:
        (workspace / "problem.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass


def _readability_running_label(workspace: Path) -> str:
    """"Readability · <title>" once the running readability run has read its
    manuscript (``readability/manuscript.json`` exists), else ``""``."""
    if not (workspace / "readability" / "manuscript.json").is_file():
        return ""
    from agent_monitor.readability import record as readability_record
    from agent_monitor.readability_runs import problem_text as readability_problem_text

    try:
        block = readability_record.run_block(workspace) or {}
    except Exception:  # noqa: BLE001 - the label waits for the finished record
        return ""
    title = _analyze_title(block)
    return readability_problem_text(title, str(block.get("focus") or "")) if title else ""


def _readability_chat_summary(run: dict[str, Any], status: str) -> str:
    """The closing message of a readability run in its Progress panel, in
    plain words (never the runner's process log or model output)."""
    summary = (run.get("readability") or {}).get("summary") or {}
    head = {"finished": "Readable version published", "stopped": "Readability run stopped"}.get(
        status, "Readability run failed")
    parts: list[str] = []
    if summary:
        total, rewritten = int(summary.get("parts") or 0), int(summary.get("rewritten") or 0)
        counts = ", ".join(
            text for value, text in (
                (summary.get("faithful"), f"{int(summary.get('faithful') or 0)} faithful"),
                (summary.get("issues"), f"{int(summary.get('issues') or 0)} with issues"),
                (summary.get("unchecked"), f"{int(summary.get('unchecked') or 0)} unchecked"),
                (summary.get("not_rewritten"), f"{int(summary.get('not_rewritten') or 0)} not rewritten"),
            ) if value
        )
        parts.append(f"{head}: {rewritten} of {total} parts rewritten" + (f" ({counts})" if counts else "") + ".")
        parts.append("Read it in the Sandbox; the paper map is in the DAG. "
                     "Each part was checked against the original by a model — not a certificate.")
    else:
        parts.append(f"{head}; no readable version was published.")
    if status == "failed":
        errors = [
            " ".join(str(agent.get("output") or "").split())
            for agent in run.get("agents") or []
            if isinstance(agent, dict) and str(agent.get("stage_name") or "").endswith("(error)")
        ]
        error = next((item for item in reversed(errors) if item), "") or re.sub(
            r"^Ansatz Readability exited with code \d+: ", "", " ".join(str(run.get("error") or "").split()))
        if error:
            parts.append(error[:600])
    return "\n\n".join(parts)


def _readability_memory_label(run: dict[str, Any]) -> str:
    """The operator-memory line of a readability run: its label and counts."""
    block = run.get("readability") or {}
    title = _analyze_title(block) or "manuscript"
    summary = block.get("summary") or {}
    counts = (f"{summary.get('rewritten', 0)} of {summary.get('parts', 0)} parts rewritten, "
              f"{summary.get('faithful', 0)} faithful") if summary else "nothing published"
    return f"Readability · {title} ({counts}; checked by a model — not a certificate)"


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _run_finished_line(
    run: dict[str, Any], status: str, seconds: float, *, analyze: bool = False, readability: bool = False
) -> str:
    """A run's closing line in its chat. An analysis or a readability run
    counts its model calls, as its Progress pane does: its agents are the
    runner's process log."""
    totals = run.get("totals") or {}
    if analyze or readability:
        block = run.get("readability" if readability else "analyze") or {}
        calls = int(((block.get("summary") or {}).get("calls")) or 0)
        count = _plural(calls, "model call", "model calls")
    else:
        count = f"{len(run.get('agents') or [])} agents"
    return (
        f"{'✓' if status == 'finished' else '✗'} run {status} · {count} · "
        f"in {totals.get('input_tokens') or 0} / out {totals.get('output_tokens') or 0} tokens"
        + (f" · ${totals.get('cost_usd'):.4f}" if totals.get("cost_usd") else "")
        + f" · {int(seconds)}s"
    )


def _analyze_chat_summary(run: dict[str, Any], status: str) -> str:
    """The closing message of an analysis in its Progress panel: what was
    checked, in plain words (never the runner's process log or model output)."""
    block = run.get("analyze") or {}
    summary = block.get("summary") or {}
    head = {"finished": "Analysis finished", "stopped": "Analysis stopped"}.get(status, "Analysis failed")
    parts: list[str] = []
    if summary:
        coverage = summary.get("coverage") or {}
        checked, total = int(coverage.get("checked") or 0), int(coverage.get("total") or 0)
        # Results with a located proof that were not checked (statements
        # without a proof are never checkable and are not counted here).
        unchecked = max(0, total - checked)
        counts = [
            text for value, text in (
                (summary.get("checked"), f"{int(summary.get('checked') or 0)} with no issue found"),
                (summary.get("gap"), _plural(int(summary.get("gap") or 0), "with a gap", "with gaps")),
                (summary.get("error"), _plural(int(summary.get("error") or 0), "with an error", "with errors")),
                (summary.get("unclear"), f"{int(summary.get('unclear') or 0)} unclear"),
                (unchecked, f"{unchecked} not checked"),
            ) if value
        ]
        if checked:
            parts.append(f"{head}: {checked} of {total} results checked"
                         + (f" ({', '.join(counts)})" if counts else "") + ".")
            parts.append("The report is in the Sandbox and the verdicts are in the DAG. "
                         "Checked by a model verifier — not a certificate.")
        else:
            parts.append(f"{head}: no result was checked. The report in the Sandbox says why.")
    else:
        parts.append(f"{head}; no report was published.")
    if status == "failed":
        errors = [
            " ".join(str(agent.get("output") or "").split())
            for agent in run.get("agents") or []
            if isinstance(agent, dict) and str(agent.get("stage_name") or "").endswith("(error)")
        ]
        error = next((item for item in reversed(errors) if item), "") or re.sub(
            r"^Ansatz Analyze exited with code \d+: ", "", " ".join(str(run.get("error") or "").split()))
        if error:
            parts.append(error[:600])
    return "\n\n".join(parts)


def _analyze_memory_label(run: dict[str, Any]) -> str:
    """The operator-memory line of an analysis: its label and verdict counts."""
    block = run.get("analyze") or {}
    title = _analyze_title(block) or "manuscript"
    summary = block.get("summary") or {}
    counts = ", ".join(
        f"{summary.get(key, 0)} {label}"
        for key, label in (("checked", "checked"), ("gap", "gap"), ("error", "error"),
                           ("unclear", "unclear"), ("not_checked", "not checked"))
    ) if summary else "no verdicts"
    return f"Analyze · {title} ({counts}; checked by a model verifier — not a certificate)"


def _artifact_agents(engine: str, workspace: Path, run_id: str) -> dict[str, Any] | None:
    """Build a rich multi-agent view from the engine's own artifacts.

    IMProof logs every agent (Author / Critic / Council…) to events.jsonl;
    the UCLA harness writes per-stage usage.jsonl + conversation memory.
    Reuses the dashboard artifact builders so the Monitor gets the full
    agent graph instead of one wrapper session.
    """
    try:
        if engine == "improof":
            from harness_dashboard.improofbench_builder import build_run_from_improof_artifacts

            candidates = sorted(
                (d for d in workspace.iterdir() if d.is_dir() and (d / "events.jsonl").exists()),
                key=lambda d: d.stat().st_mtime,
            )
            if not candidates:
                return None
            return build_run_from_improof_artifacts(run_id, candidates[-1])
        if engine == "ucla":
            from harness_dashboard.artifact_builder import build_run_from_artifacts

            if not (workspace / "Overall_Usage" / "usage.jsonl").exists():
                return None
            return build_run_from_artifacts(run_id, workspace)
    except Exception:  # noqa: BLE001 — enrichment is best-effort
        return None
    return None


def _merge_artifact_run(base: dict[str, Any], rich: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay artifact-derived agents/pipeline onto the console run record."""
    if not rich or not rich.get("agents"):
        return base
    out = dict(base)
    for key in ("agents", "edges", "pipeline", "stages_present", "analysis", "artifact_dir"):
        if rich.get(key):
            out[key] = rich[key]
    totals = dict(rich.get("totals") or {})
    totals["latency_s"] = (base.get("totals") or {}).get("latency_s") or totals.get("latency_s")
    out["totals"] = totals
    return out


def _live_output_flusher(
    *,
    run_id: str,
    engine: str,
    problem_id: str,
    problem_text: str,
    started: float,
    workspace: Path,
    baseline_run: dict[str, Any] | None = None,
    continuation_job_id: str | None = None,
):
    """Callback that streams subprocess output into the run JSON while running."""
    last_enrich = [0.0]
    rich_cache: list[dict[str, Any] | None] = [None]

    def _flush(output: str) -> None:
        run = normalize_run(
            {
                "run_id": run_id,
                "problem_id": problem_id,
                "trace_name": f"[{engine.upper()}] {problem_id}",
                "status": "running",
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for(engine),
                "problem_text": problem_text[:20000],
                "agents": [
                    {
                        "trace_id": f"{run_id}::{engine}_main",
                        "stage_name": f"{engine}_main",
                        "role": "orchestrator",
                        "pipeline_stage": _pipeline_for(engine)[0]["id"],
                        "prompt": problem_text[:4000],
                        "output": output[-12000:],
                        "status": "running",
                        "latency_s": time.time() - started,
                    }
                ],
                "totals": {"latency_s": time.time() - started},
            },
            engine=engine,  # type: ignore[arg-type]
        )
        now = time.time()
        if now - last_enrich[0] > 10.0:
            last_enrich[0] = now
            rich = _artifact_agents(engine, workspace, run_id)
            if rich and rich.get("agents"):
                rich_cache[0] = rich
        # Heartbeats arrive every two seconds, while native artifact parsing is
        # intentionally throttled. Reuse the latest rich view between parses;
        # otherwise every intervening heartbeat replaces Author/Critic nodes
        # with the one-node orchestrator placeholder and makes Monitor flicker.
        if rich_cache[0] and rich_cache[0].get("agents"):
            merged = _merge_artifact_run(run, rich_cache[0])
            merged["status"] = "running"
            run = normalize_run(merged, engine=engine)  # type: ignore[arg-type]
        if baseline_run and continuation_job_id:
            run = _merge_continuation_run(
                baseline_run, run, job_id=continuation_job_id
            )
            run["status"] = "running"
        _write_run(run)

    return _flush


def _run_hermes(
    *,
    run_id: str,
    problem_id: str,
    problem_text: str,
    model: str | None,
    max_iterations: int,
    started: float,
    workspace: Path,
    display_problem_text: str | None = None,
    extra_env: dict[str, str] | None = None,
    use_subagents: bool = True,
    subagent_model: str | None = None,
    owner_id: int | None = None,
) -> dict[str, Any]:
    from agent_monitor.runners import hermes as hermes_runner

    record_problem_text = (
        display_problem_text if display_problem_text is not None else problem_text
    )

    # Live placeholder updates via step callback when available
    agents: list[dict[str, Any]] = []
    tool_log: list[str] = []
    # Surfaced to the console's human-in-the-loop panel: which model is running
    # and which tools it is touching right now.
    live: dict[str, Any] = {
        "engine": "hermes",
        "model": model or (extra_env or {}).get("AGENT_MONITOR_MODEL") or None,
        "subagent_model": subagent_model if use_subagents else None,
        "subagents": bool(use_subagents),
        "tools_active": [],
        "tools_recent": [],
        "tool_calls": 0,
        "iteration": 0,
    }

    def _flush(partial_output: str = "") -> None:
        run = normalize_run(
            {
                "run_id": run_id,
                "live": {**live, "tools_recent": live["tools_recent"][-10:]},
                "problem_id": problem_id,
                "trace_name": f"[HERMES] {problem_id}",
                "status": "running",
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for("hermes"),
                "problem_text": record_problem_text[:20000],
                "agents": agents
                or [
                    {
                        "trace_id": f"{run_id}::hermes_main",
                        "stage_name": "hermes_main",
                        "role": "prover",
                        "pipeline_stage": "draft",
                        "prompt": problem_text[:4000],
                        "output": partial_output or "Hermes agent running…",
                        "status": "running",
                    }
                ],
                "edges": [
                    {"from": a["trace_id"], "to": b["trace_id"]}
                    for a, b in zip(agents, agents[1:])
                ],
                "totals": {"latency_s": time.time() - started},
            },
            engine="hermes",
        )
        _attach_analysis(run, workspace)
        _write_run(run)

    _flush()
    ue = extra_env or {}
    direct_key = ue.get("OPENAI_API_KEY") or ue.get("OPENROUTER_API_KEY") or None
    codex_home: Path | None = None
    if owner_id is not None and ue.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        from agent_monitor import codex_login

        candidate = codex_login.account_home(owner_id)
        if codex_login.account_login_ready(candidate):
            codex_home = candidate
    if ue.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1" and codex_home is None:
        raise RuntimeError("The selected Codex account is unavailable; reconnect Codex and retry.")
    from agent_monitor.run_tuning import service_tier as run_service_tier

    agent = hermes_runner.create_agent(
        model=model or ue.get("AGENT_MONITOR_MODEL") or None,
        api_key=None if codex_home else direct_key,
        base_url=None if codex_home else (ue.get("AGENT_MONITOR_BASE_URL") or ue.get("OPENAI_BASE_URL") or None),
        codex_home=codex_home,
        use_codex_subscription=codex_home is not None,
        max_iterations=max_iterations,
        enable_subagents=use_subagents,
        subagent_model=subagent_model,
        reasoning_effort=(
            None
            if ue.get("AGENT_MONITOR_RUN_REASONING_EFFORT") in {None, "", "default"}
            else ue.get("AGENT_MONITOR_RUN_REASONING_EFFORT")
        ),
        service_tier=run_service_tier(ue.get("AGENT_MONITOR_RUN_SPEED_MODE")),
    )
    # The embedded Codex app-server must start inside this run's isolated
    # workspace; otherwise it sees the repository cwd and cannot write the
    # required proof.md artifact.
    agent.session_cwd = str(workspace)
    live["auth_mode"] = "chatgpt_subscription" if codex_home else "api_key"
    live["model"] = getattr(agent, "model", None) or live["model"]

    stop_ev = _stop_event(run_id)

    # Tool-level telemetry for the live panel. Flushes are throttled because a
    # busy agent can start tools far faster than the console polls.
    active_tools: dict[str, str] = {}
    progress_tools: dict[str, list[str]] = {}
    progress_counter = [0]
    last_live_flush = [0.0]

    def _flush_live() -> None:
        now = time.monotonic()
        if now - last_live_flush[0] < 1.0:
            return
        last_live_flush[0] = now
        try:
            _flush()
        except Exception:  # noqa: BLE001
            pass

    def tool_start_cb(tool_id: Any = None, name: Any = None, args: Any = None) -> None:
        try:
            active_tools[str(tool_id)] = str(name)
            live["tool_calls"] = int(live.get("tool_calls") or 0) + 1
            live["tools_active"] = list(active_tools.values())
            live["tools_recent"].append(
                {"name": str(name), "args": str(args or "")[:160], "state": "running"}
            )
            del live["tools_recent"][:-20]
            _flush_live()
        except Exception:  # noqa: BLE001
            pass

    def tool_complete_cb(tool_id: Any = None, name: Any = None, args: Any = None, result: Any = None) -> None:
        try:
            active_tools.pop(str(tool_id), None)
            live["tools_active"] = list(active_tools.values())
            for rec in reversed(live["tools_recent"]):
                if rec.get("name") == str(name) and rec.get("state") == "running":
                    rec["state"] = "done"
                    if args:
                        rec["args"] = str(args)[:160]
                    rec["result"] = str(result or "")[:200]
                    break
            _flush_live()
        except Exception:  # noqa: BLE001
            pass

    for attr, cb in (("tool_start_callback", tool_start_cb), ("tool_complete_callback", tool_complete_cb)):
        if hasattr(agent, attr):
            try:
                setattr(agent, attr, cb)
            except Exception:  # noqa: BLE001
                pass

    # Codex subscription runs execute through the embedded app-server rather
    # than Hermes' regular tool executor. That route emits only the generic
    # progress callback, so bridge it into the same structured live telemetry
    # used by every other Hermes provider. Restricting this adapter to the
    # app-server route avoids double-counting ordinary Hermes tool calls, whose
    # executor emits both progress and structured callbacks.
    if codex_home is not None:
        def tool_progress_cb(
            event_type: Any,
            name: Any = None,
            preview: Any = None,
            args: Any = None,
            **metadata: Any,
        ) -> None:
            tool_name = str(name or "tool")
            if event_type == "tool.started":
                progress_counter[0] += 1
                live["iteration"] = max(int(live.get("iteration") or 0), 1)
                tool_id = f"codex-progress-{progress_counter[0]}"
                progress_tools.setdefault(tool_name, []).append(tool_id)
                tool_start_cb(tool_id, tool_name, args or preview)
            elif event_type == "tool.completed":
                pending = progress_tools.get(tool_name) or []
                tool_id = pending.pop(0) if pending else f"codex-progress-{tool_name}"
                if not pending:
                    progress_tools.pop(tool_name, None)
                tool_complete_cb(
                    tool_id,
                    tool_name,
                    args,
                    metadata.get("result") or preview,
                )

        try:
            agent.tool_progress_callback = tool_progress_cb
        except Exception:  # noqa: BLE001
            pass

    def _tok_snapshot() -> dict[str, float]:
        return {
            "in": getattr(agent, "session_input_tokens", 0)
            or getattr(agent, "session_prompt_tokens", 0)
            or 0,
            "out": getattr(agent, "session_output_tokens", 0)
            or getattr(agent, "session_completion_tokens", 0)
            or 0,
            "cost": getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0,
        }

    def _stage_for_tools(names: list[str]) -> tuple[str, str]:
        joined = " ".join(names).lower()
        if any(k in joined for k in ("write", "edit", "create", "append", "apply")):
            return "draft", "writer"
        if any(k in joined for k in ("terminal", "python", "exec", "run", "bash", "shell", "compile")):
            return "verify", "checker"
        if any(k in joined for k in ("read", "search", "grep", "list", "web", "fetch", "browse")):
            return "understand", "reader"
        return "plan", "reasoning"

    last_tok: dict[str, float] = {"in": 0, "out": 0, "cost": 0.0}
    thinking_buf: list[str] = []

    def reasoning_cb(text: str) -> None:
        if text and str(text).strip():
            thinking_buf.append(str(text))

    if hasattr(agent, "reasoning_callback"):
        try:
            agent.reasoning_callback = reasoning_cb
        except Exception:  # noqa: BLE001
            pass

    def _close_last(prev_tools: Any) -> None:
        """Finalize the node for the iteration that just completed."""
        if not agents:
            return
        node = agents[-1]
        snap = _tok_snapshot()
        node["input_tokens"] = int(snap["in"] - last_tok["in"]) or None
        node["total_input_tokens"] = node["input_tokens"]  # recompute (was frozen at 0 while running)
        node["output_tokens"] = int(snap["out"] - last_tok["out"]) or None
        d_cost = snap["cost"] - last_tok["cost"]
        if d_cost > 0:
            node["cost_usd"] = round(d_cost, 6)
        elif node.get("input_tokens") or node.get("output_tokens"):
            node["cost_usd"] = _estimate_cost(
                getattr(agent, "model", model),
                input_tokens=int(node.get("input_tokens") or 0),
                output_tokens=int(node.get("output_tokens") or 0),
            )
        last_tok.update(snap)
        node["status"] = "finished"
        if thinking_buf:
            node["thinking"] = "\n\n".join(thinking_buf)[:8000]
            node["thinking_source"] = "hermes reasoning stream"
            thinking_buf.clear()
        if prev_tools:
            names = [str(t.get("name", "?")) for t in prev_tools if isinstance(t, dict)]
            stage, role = _stage_for_tools(names)
            node["pipeline_stage"] = stage
            node["role"] = role
            node["tools"] = names
            node["stage_name"] = f"iter-{node['round_id']} ({', '.join(names[:3])})"
            parts = []
            for t in prev_tools:
                if isinstance(t, dict):
                    args = str(t.get("arguments") or "")[:400]
                    res = str(t.get("result") or "")[:1200]
                    parts.append(f"▸ {t.get('name')}({args})\n{res}")
            node["output"] = "\n\n".join(parts)[:8000] or node.get("output", "")
            tool_log.append(f"[iter {node['round_id']}] {', '.join(names)}")

    def step_cb(iteration: int, prev_tools: Any = None) -> None:
        if stop_ev.is_set():
            raise StopRequested(run_id)
        try:
            _close_last(prev_tools)
        except Exception:  # noqa: BLE001
            pass
        live["iteration"] = iteration
        agents.append(
            {
                "trace_id": f"{run_id}::iter-{iteration}",
                "stage_name": f"iter-{iteration}",
                "role": "reasoning",
                "pipeline_stage": "plan",
                "call_seq": iteration,
                "round_id": iteration,
                "model": getattr(agent, "model", None) or model,
                "prompt": problem_text[:4000] if iteration == 1
                else "(agent loop continues — model sees the task, conversation history and previous tool results)",
                "prompt_source": "task prompt" if iteration == 1 else "conversation context",
                "output": "model call in progress…",
                "output_source": "hermes tool results",
                "status": "running",
            }
        )
        _flush()

    if hasattr(agent, "step_callback"):
        try:
            agent.step_callback = step_cb
        except Exception:  # noqa: BLE001
            pass

    from agent_monitor.engines_registry import CITATION_REQUIREMENTS

    prompt = (
        "You are working on an informal mathematics proof problem.\n"
        "Use tools as needed (code, files, terminal, subagents).\n\n"
        f"WORKSPACE (your sandbox directory): {workspace}\n"
        "Requirements:\n"
        f"1. Maintain your evolving proof write-up in {workspace}/proof.md "
        "(Markdown; use $...$ / $$...$$ for math, headings for structure). "
        "Update this file as your proof develops — write early drafts, then refine.\n"
        f"2. You may create scratch files (notes, python checks) inside {workspace}.\n"
        "3. Finish by making proof.md a clean, self-contained informal proof.\n"
        "4. End your final response with a line containing exactly "
        "--- proof.md --- followed by the complete final Markdown artifact. "
        "This is the monitor's safe persistence fallback; never paste an "
        "unchanged draft after reporting a failed update.\n\n"
        f"{CITATION_REQUIREMENTS}\n"
        f"PROBLEM:\n{problem_text}\n"
    )
    result: dict[str, Any] | None = None
    heartbeat_stop = threading.Event()

    def _heartbeat() -> None:
        while not heartbeat_stop.wait(_HERMES_HEARTBEAT_S):
            try:
                if stop_ev.is_set():
                    interrupt = getattr(agent, "interrupt", None)
                    if callable(interrupt):
                        interrupt("Run stopped by user.")
                    session = getattr(agent, "_codex_session", None)
                    if session is not None:
                        session.request_interrupt()
                    continue
                _flush()
            except Exception:  # noqa: BLE001 - telemetry cannot fail a run
                pass

    heartbeat_thread = threading.Thread(
        target=_heartbeat,
        name=f"hermes-heartbeat-{run_id}",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        result = agent.run_conversation(prompt)
        initial_final = str((result or {}).get("final_response") or "")
        if not _hermes_text_is_error_report(result):
            _persist_text_final_answer(initial_final, workspace, engine="Hermes")
        if (
            not stop_ev.is_set()
            and _hermes_audit_last_mile_needed(problem_text, workspace)
        ):
            audit_result = agent.run_conversation(
                _HERMES_AUDIT_LAST_MILE_PROMPT,
                conversation_history=(result or {}).get("messages") or None,
            )
            result = _merge_hermes_turn_results(result, audit_result)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=max(1.0, _HERMES_HEARTBEAT_S + 0.5))
        # Monitor runs are one-shot sessions. Hermes' AIAgent.close() does not
        # itself retire the lazy Codex app-server transport, so use the runner
        # helper to prevent completed runs leaking app-server/MCP children.
        hermes_runner.close_agent(agent, messages=(result or {}).get("messages") or [])
    if stop_ev.is_set():
        raise StopRequested(run_id)
    elapsed = time.time() - started
    final = (result or {}).get("final_response") or ""
    turn_error = _hermes_turn_error(result)
    proof = (
        _root_proof_artifact(workspace)
        if _hermes_text_is_error_report(result)
        else _persist_text_final_answer(final, workspace, engine="Hermes")
    )
    completed = bool((result or {}).get("completed", True)) and proof is not None
    final_status = "finished" if completed else "failed"
    try:
        _close_last(None)
    except Exception:  # noqa: BLE001
        pass
    # Drop a dangling in-progress node (the final answer call), replace with finalize.
    if agents and agents[-1].get("status") == "running":
        agents.pop()
    snap = _tok_snapshot()
    total_in = (result or {}).get("input_tokens") or (result or {}).get("prompt_tokens") or snap["in"]
    total_out = (result or {}).get("output_tokens") or (result or {}).get("completion_tokens") or snap["out"]
    delta_in = int(snap["in"] - last_tok["in"])
    delta_out = int(snap["out"] - last_tok["out"])
    delta_cost = snap["cost"] - last_tok["cost"]
    fin_cost = round(delta_cost, 6) if delta_cost > 0 else _estimate_cost(
        getattr(agent, "model", model), input_tokens=delta_in, output_tokens=delta_out
    )
    agents.append(
        {
            "trace_id": f"{run_id}::finalize",
            "stage_name": "final answer",
            "role": "prover",
            "pipeline_stage": "finalize",
            "call_seq": len(agents) + 1,
            "model": getattr(agent, "model", model),
            "latency_s": elapsed,
            "input_tokens": delta_in or None,
            "total_input_tokens": delta_in or None,
            "output_tokens": delta_out or None,
            "cost_usd": fin_cost,
            "prompt": prompt,
            "prompt_source": "task prompt",
            "thinking": "\n\n".join(thinking_buf)[:8000] if thinking_buf else None,
            "thinking_source": "hermes reasoning stream" if thinking_buf else None,
            "output": final,
            "output_source": "final response",
            "memory_context": _workspace_memory(workspace),
            "memory_source": "workspace snapshot",
            "status": final_status,
            "tool_calls": (result or {}).get("tool_call_count")
            or int(live.get("tool_calls") or 0),
        }
    )
    total_cost = (result or {}).get("estimated_cost_usd") or (result or {}).get("actual_cost_usd")
    if not total_cost:
        total_cost = sum(float(a.get("cost_usd") or 0) for a in agents) or None
        if not total_cost:
            total_cost = _estimate_cost(
                getattr(agent, "model", model),
                input_tokens=int(total_in or 0),
                output_tokens=int(total_out or 0),
            )
    edges = [
        {"from": a["trace_id"], "to": b["trace_id"]}
        for a, b in zip(agents, agents[1:])
    ]
    live["tools_active"] = []
    run = normalize_run(
        {
            "run_id": run_id,
            "problem_id": problem_id,
            "trace_name": f"[HERMES] {problem_id}",
            "live": {**live, "tools_recent": live["tools_recent"][-10:]},
            "status": final_status,
            "completed": completed,
            "error": None if completed else (
                f"Hermes stopped: {turn_error}"
                if turn_error
                else "Hermes finished without creating a non-empty proof.md or proof.tex"
            ),
            "updated_at": _now(),
            "workspace": str(workspace),
            "pipeline": _pipeline_for("hermes"),
            "problem_text": record_problem_text[:20000],
            "agents": agents,
            "edges": edges,
            "totals": {
                "input_tokens": total_in,
                "output_tokens": total_out,
                "cost_usd": total_cost,
                "latency_s": elapsed,
                "api_calls": (result or {}).get("api_calls"),
            },
            "message_count": len((result or {}).get("messages") or []),
        },
        engine="hermes",
    )
    return _attach_analysis(run, workspace)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _hermes_turn_error(result: Any) -> str:
    """The reason Hermes ended a turn unsuccessfully (a Codex usage limit,
    an expired login, a crashed app-server), cleaned for the run record."""
    if not isinstance(result, dict) or result.get("completed") is not False:
        return ""
    raw = str(result.get("error") or "").strip()
    if not raw:
        return ""
    # Codex app-server failures append its stderr tail; keep the reason.
    raw = raw.split("\ncodex stderr", 1)[0]
    return " ".join(_ANSI_ESCAPE_RE.sub("", raw).split())[:600]


def _hermes_text_is_error_report(result: Any) -> bool:
    """Whether Hermes' final text is its own failure report, never a proof draft.

    A crashed app-server ("Codex app-server turn failed: ..."), a result
    marked ``failed`` or text that is the error itself is a report.  A turn
    cut short (truncated, a usage limit mid-answer) still returns the model's
    partial draft, which is kept as before.
    """
    if not isinstance(result, dict) or result.get("completed") is not False:
        return False
    text = str(result.get("final_response") or "").strip()
    if result.get("failed") is True or text.startswith("Codex app-server turn failed"):
        return True
    return bool(text) and text == str(result.get("error") or "").strip()


def _ensure_codex_auth(env: dict[str, str], owner_id: int | None = None) -> Path | None:
    """Point codex at a CODEX_HOME with valid credentials.

    Prefers the *run owner's* ChatGPT/OpenAI-account (OAuth) login — set up
    once via the Settings panel's "Codex Account" card — over API-key
    billing, so runs consume that account's subscription usage instead of
    metered API calls. This is strictly per-user: ``owner_id`` must come from
    the authenticated session that started the run, never from request data,
    so one user's runs can never pick up another user's Codex login. Set
    ``AGENT_MONITOR_CODEX_AUTH_MODE=apikey`` to force API-key mode even when
    an account login exists.

    Falls back to a private CODEX_HOME made for this run and logged in with
    OPENAI_API_KEY when no account login is present (or no owner is known).
    ``codex login`` stores the key in plaintext, so that home lives outside
    the data directory and is returned for the caller to remove
    (``_remove_codex_api_home``) once Codex has exited. An account's API-key
    run without an OpenAI key gets that home empty, so Codex reports it is
    not logged in rather than signing in as the service account's ~/.codex.
    """
    import subprocess
    import tempfile

    if owner_id is not None:
        from agent_monitor import codex_login

        account_home = codex_login.account_home(owner_id)
        mode = str(env.get("AGENT_MONITOR_AUTH_MODE") or env.get("AGENT_MONITOR_CODEX_AUTH_MODE") or "").lower()
        use_codex = str(env.get("AGENT_MONITOR_USE_CODEX", "1")).lower() not in {"0", "false", "off", "no"}
        force_apikey = mode in {"api_key", "apikey"} or not use_codex
        if not force_apikey and codex_login.account_login_ready(account_home):
            env["CODEX_HOME"] = str(account_home)
            return None

    key = env.get("OPENAI_API_KEY")
    if not key and (owner_id is None or env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"):
        # A local run without an account keeps the invoking user's own Codex
        # setup; a subscription run keeps the account home its route set.
        return None
    home = Path(tempfile.mkdtemp(prefix="agent-monitor-codex-"))
    env["CODEX_HOME"] = str(home)
    if not key:
        return home
    from agent_monitor.engines_registry import which_tool

    codex = shutil.which("codex", path=env.get("PATH")) or which_tool("codex")
    if not codex:
        return home
    try:
        from agent_monitor.subprocess_env import child_process_env

        login_env = child_process_env(
            source=env,
            extra={"CODEX_HOME": str(home)},
        )
        subprocess.run(
            [codex, "login", "--with-api-key"],
            input=key,
            text=True,
            env=login_env,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return home


def _remove_codex_api_home(home: Path | None) -> None:
    """Delete a run's private API-key CODEX_HOME (from ``_ensure_codex_auth``)."""
    if home is not None:
        shutil.rmtree(home, ignore_errors=True)


# Earlier versions kept one Codex login per API key at codex_home/<16 hex>.
_LEGACY_CODEX_KEY_HOME = re.compile(r"[0-9a-f]{16}")


def remove_legacy_codex_key_homes() -> int:
    """Delete the per-key Codex logins earlier versions left in the data directory.

    Each held an OpenAI key in plaintext, outside the encrypted settings, and
    nothing removed it when the key changed. Account sign-ins (``users/``)
    are kept. Returns how many were removed.
    """
    root = CACHE_DIR.parent / "codex_home"
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        if (
            _LEGACY_CODEX_KEY_HOME.fullmatch(entry.name)
            and entry.is_dir()
            and not entry.is_symlink()
        ):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def _cli_failure_reason(
    *,
    label: str,
    returncode: int | None,
    timed_out: bool,
    timeout_s: int,
    proof_missing: bool,
    detail: str | None = None,
    artifact: str = "proof artifact",
) -> str:
    if timed_out:
        return f"{label} timed out after {timeout_s}s"
    if returncode not in (None, 0):
        suffix = f": {detail.strip()}" if detail and detail.strip() else ""
        return f"{label} exited with code {returncode}{suffix}"
    if proof_missing:
        return f"{label} exited successfully but produced no {artifact}"
    suffix = f": {detail.strip()}" if detail and detail.strip() else ""
    return f"{label} failed before producing a {artifact}{suffix}"


def _readability_published(workspace: Path) -> bool:
    """The Readability runner's success artifacts: the readable version and
    its receipt, written last (no proof)."""
    folder = Path(workspace) / "readability"
    try:
        return all(
            (folder / name).is_file() and not (folder / name).is_symlink()
            for name in ("readable.md", "receipt.json")
        )
    except OSError:
        return False


def _analysis_published(workspace: Path) -> bool:
    """The Analyze runner's success artifacts: report and graph (no proof)."""
    analysis = Path(workspace) / "analysis"
    try:
        return all(
            (analysis / name).is_file() and not (analysis / name).is_symlink()
            for name in ("report.md", "analysis_graph.json")
        )
    except OSError:
        return False


def _cgm_receipt_signature(workspace: Path) -> tuple[int, int, int] | None:
    """Which engine receipt a CGM run's workspace holds: (inode, mtime, size).

    The runner writes the receipt last when it publishes a generation, as a
    new file, so a signature that changed during a run means that run's
    generation was published in full.
    """
    try:
        info = (Path(workspace) / "math_harness" / "project" / "engine_receipt.json").lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_ino, info.st_mtime_ns, info.st_size


def _apply_analyze_runner_env(env: dict[str, str], extra_env: dict[str, str]) -> None:
    """What only the manuscript runners (Analyze, Readability) receive, after
    the child-env scrub.

    * the operator's optional GitHub token (child_process_env removes it
      from every other child; the GitHub connector sends it only with its own
      api.github.com calls);
    * the console data directory, so arXiv requests share one 3-second
      politeness slot with the console and the other runners;
    * on an own-key route, an explicit "not sponsored" (an inherited flag
      would make the runner expect a broker credential).
    """
    from agent_monitor import DATA_DIR

    token = str(os.environ.get("AGENT_MONITOR_ANALYZE_GITHUB_TOKEN") or "").strip()
    if token:
        env["AGENT_MONITOR_ANALYZE_GITHUB_TOKEN"] = token
    else:
        env.pop("AGENT_MONITOR_ANALYZE_GITHUB_TOKEN", None)
    env["AGENT_MONITOR_DATA_DIR"] = str(DATA_DIR)
    if extra_env.get("AGENT_MONITOR_SPONSORED_KIMI") != "1":
        env["AGENT_MONITOR_SPONSORED_KIMI"] = "0"
        env.pop("AGENT_MONITOR_SPONSORED_KIMI_URL", None)


# The outer watchdog of a Plain or Meta-Harness run whose OpenAI calls run at
# extra-high or maximum effort (see _cli_timeout_seconds).
_LONG_EFFORT_CLI_TIMEOUT = 10_800


def _openai_long_effort(env: dict[str, str], model: object) -> bool:
    """Whether a run's ``api_chat`` calls of ``model`` are OpenAI calls at
    extra-high or maximum effort (api_backend then allows each one
    HIGH_EFFORT_TIMEOUT).  Never on a linked account's route."""
    if not str(model or "").strip() or any(
        env.get(name) == "1"
        for name in ("AGENT_MONITOR_CODEX_SUBSCRIPTION", "AGENT_MONITOR_CLAUDE_SUBSCRIPTION")
    ):
        return False
    from agent_monitor.runners import api_backend

    return api_backend.openai_high_effort(env, str(model))


def _cli_timeout_seconds(engine: str, env: dict[str, str]) -> int:
    """Resolve a bounded CLI timeout, including multi-stage harness budgets."""
    normalized = str(engine or "").strip().upper().replace("-", "_")
    if normalized in ("ANALYZE", "READABILITY"):
        # start_job sets the watchdog from the run's own caps: its deadline,
        # one in-flight call and publication.  Operator timeouts for other
        # engines (AGENT_MONITOR_CLI_TIMEOUT) never cut an analysis or a
        # readability run short; their caps are the knob.  Without the
        # variable (a re-check, a question), the administrator ceiling applies.
        try:
            requested = int(str(env.get(f"AGENT_MONITOR_{normalized}_WATCHDOG_SECONDS") or "").strip())
        except ValueError:
            requested = 14_400 + 900 + 300
        bounded = max(60, min(requested, 86_400))
        if env.get("AGENT_MONITOR_SPONSORED_KIMI") == "1":
            bounded = min(bounded, _SPONSORED_KIMI_CLI_TIMEOUT_MAX)
        return bounded
    writeup = 0
    if normalized == "DEEPAGENTS":
        default = 10_800
    elif normalized in {"MATH_HARNESS", "KIMI"}:
        # Both engines run the CGM pipeline. Keep the outer watchdog above the
        # pipeline's own bounded wall clock so it cannot kill a healthy final
        # stage (or the publication step) just before the pipeline's own limit.
        default = 7_200 if normalized == "MATH_HARNESS" else 3_600
        # An accepted run's written-proof round follows publication on its
        # own time budget.
        writeup = _cgm_writeup_allowance(env)
        try:
            from agent_monitor.runners import math_harness_runner

            if (
                normalized == "MATH_HARNESS"
                and env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1"
            ):
                # Every Claude call is bounded by its stage timeout; the helper
                # already includes a publication/teardown margin.
                default = max(
                    default,
                    math_harness_runner.claude_pipeline_budget_seconds(env) + writeup,
                )
            else:
                # API/Codex calls start only while max_seconds remains; the last
                # one may run for one more API timeout, then publication.
                budgets = math_harness_runner.Budgets.from_env(env)
                api_timeout = math_harness_runner._bounded_int(
                    env,
                    ("AGENT_MONITOR_CGM_API_TIMEOUT",),
                    default=300,
                    minimum=60,
                    maximum=900,
                )
                default = max(
                    default, int(budgets.max_seconds + api_timeout + 300) + writeup
                )
        except (ImportError, ValueError, RuntimeError, AttributeError):
            # The runner will surface malformed stage configuration. Its
            # outer process still receives the normal bounded default.
            pass
    elif normalized == "CLAUDE":
        # The direct Claude runner has its own inner provider deadline. Keep
        # the default outer watchdog above a valid configured inner deadline
        # so publication and process-group cleanup retain bounded headroom.
        default = 3_600
        try:
            inner = float(env.get("AGENT_MONITOR_CLAUDE_TIMEOUT") or "")
        except (TypeError, ValueError):
            inner = 0.0
        if math.isfinite(inner) and inner > 0:
            default = max(default, math.ceil(inner) + 300)
    elif normalized in {"PLAIN", "METAHARNESS"} and _openai_long_effort(
        env, env.get(f"{normalized}_MODEL")
    ):
        # Their calls go through api_chat, which gives an OpenAI call at
        # extra-high or maximum effort 64,000 output tokens and 30 minutes;
        # a run of several such calls gets the three hours DeepAgents has.
        default = _LONG_EFFORT_CLI_TIMEOUT
    else:
        default = 3_600
    engine_override = (
        None
        if normalized == "CLAUDE"
        else env.get(f"AGENT_MONITOR_{normalized}_TIMEOUT")
    )
    override = engine_override or env.get("AGENT_MONITOR_CLI_TIMEOUT")
    try:
        # An operator timeout bounds a CGM run up to its publication; the
        # written-proof round after it adds its own allowance.
        requested = int(override) + writeup if override else default
    except (TypeError, ValueError):
        requested = default
    bounded = max(60, min(requested, 86_400))
    if normalized == "MATH_HARNESS" and cgm_max.route_mode(env) is not None:
        # Either Max fixes its own call and run limits (cgm_max.apply_models,
        # apply_models_api), and a runner the watchdog stops publishes
        # nothing: operator timeouts may raise a Max run's watchdog but never
        # cut the run short.
        bounded = max(bounded, default, cgm_max.RUN_SECONDS + 300 + writeup)
    if env.get("AGENT_MONITOR_SPONSORED_KIMI") == "1":
        bounded = min(bounded, _SPONSORED_KIMI_CLI_TIMEOUT_MAX)
    return bounded


def _apply_selected_model_env(
    env: dict[str, str], *, engine: str, model: str | None
) -> None:
    """Route the UI model selection into the selected runner only."""
    requested = str(model or "").strip()
    if engine in _MANUSCRIPT_MODEL_ENV:
        # A manuscript runner reads these before the selection
        # (analyze_runner.configure_route), so one the service inherited
        # would replace the model the run was started with and is recorded
        # and billed at: only the runner's own variable stays, set to it.
        for name in ("MATH_HARNESS_MODEL", *_MANUSCRIPT_MODEL_ENV.values()):
            env.pop(name, None)
        if requested:
            env[_MANUSCRIPT_MODEL_ENV[engine]] = requested
    if not requested:
        return
    if cgm_max.route_mode(env) is not None:
        # cgm_max.apply_models (or apply_models_api) already chose both models.
        return
    if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
        env["AGENT_MONITOR_SELECTED_MODEL"] = requested
        env["AGENT_MONITOR_CLAUDE_MODEL"] = requested
        return
    if _is_kimi_model(requested):
        # Kimi remains an API provider even when this user has a linked Codex
        # subscription.  Leaving this flag set makes wrappers discard the
        # selected Kimi key and quietly execute a different model.
        env.pop("AGENT_MONITOR_CODEX_SUBSCRIPTION", None)
        env.pop("AGENT_MONITOR_CODEX_MODEL", None)
        env["AGENT_MONITOR_AUTH_MODE"] = "api_key"
    _apply_kimi_compatible_env(env, requested)
    env["AGENT_MONITOR_SELECTED_MODEL"] = requested
    if env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        env["AGENT_MONITOR_CODEX_MODEL"] = requested
        return
    env["AGENT_MONITOR_OPENAI_MODEL"] = requested
    if engine == "plain":
        env["PLAIN_MODEL"] = requested
    elif engine == "metaharness":
        env["METAHARNESS_MODEL"] = requested
    elif engine == "deepagents":
        prefix = "anthropic:" if requested.lower().startswith("claude-") else "openai:"
        env["DEEPAGENTS_MODEL"] = prefix + requested
    elif engine == "openclaude":
        env["AGENT_MONITOR_OPENCLAUDE_MODEL"] = requested
    elif engine == "openclaw":
        provider = "anthropic" if requested.lower().startswith("claude-") else "openai"
        env["AGENT_MONITOR_OPENCLAW_MODEL"] = f"{provider}/{requested}"
    elif engine == "openhands":
        openhands_model = f"openai/{requested}"
        env["AGENT_MONITOR_OPENHANDS_MODEL"] = openhands_model
        if _is_kimi_model(requested):
            # An inherited LLM_MODEL may point at OpenAI or Anthropic.  The UI
            # selection is authoritative and must not be weakened by the
            # later runtime's setdefault call.
            env["LLM_MODEL"] = openhands_model


def _run_sponsored_kimi_plain(
    *,
    run_id: str,
    problem_id: str,
    problem_text: str,
    display_problem_text: str,
    started: float,
    workspace: Path,
    owner_id: int | None,
) -> dict[str, Any]:
    """Run Plain through the isolated service and project it into this workspace."""
    from agent_monitor import sponsored_kimi_client

    stop_event = _stop_event(run_id)

    def project(payload: dict[str, Any], *, terminal_status: str | None = None) -> dict[str, Any]:
        public_status = str(payload.get("status") or "running").lower()
        status = terminal_status or (
            public_status if public_status in {"finished", "failed", "stopped"} else "running"
        )
        events = [
            item for item in (payload.get("events") or [])
            if isinstance(item, dict)
        ][-60:]
        agents: list[dict[str, Any]] = []
        for index, item in enumerate(events, 1):
            output = str(item.get("text") or item.get("output") or "")[-12000:]
            agents.append(
                {
                    "trace_id": f"{run_id}::sponsored-{index}",
                    "stage_name": str(item.get("stage") or item.get("title") or "Plain"),
                    "role": str(item.get("role") or "agent"),
                    "pipeline_stage": "finalize" if status != "running" else "work",
                    "prompt": problem_text[:4000],
                    "output": output,
                    "status": status if status != "running" else str(item.get("status") or "running"),
                    "latency_s": time.time() - started,
                    "model": "kimi-k3",
                }
            )
        proof = normalize_proof_markdown(str(payload.get("proof") or ""))
        if status == "finished" and proof:
            agents.append(
                {
                    "trace_id": f"{run_id}::sponsored-final",
                    "stage_name": "Plain final answer",
                    "role": "agent",
                    "pipeline_stage": "finalize",
                    "prompt": problem_text[:4000],
                    "output": proof[-12000:],
                    "status": "finished",
                    "latency_s": time.time() - started,
                    "model": "kimi-k3",
                }
            )
        if not agents:
            agents = [
                {
                    "trace_id": f"{run_id}::sponsored-session",
                    "stage_name": "Sponsored Kimi Plain",
                    "role": "agent",
                    "pipeline_stage": "finalize" if status != "running" else "work",
                    "prompt": problem_text[:4000],
                    "output": str(payload.get("error") or "Waiting for isolated Kimi service"),
                    "status": status,
                    "latency_s": time.time() - started,
                    "model": "kimi-k3",
                }
            ]
        run = normalize_run(
            {
                "run_id": run_id,
                "problem_id": problem_id,
                "trace_name": f"[PLAIN] {problem_id}",
                "status": status,
                "error": str(payload.get("error") or "") or None,
                "live": {
                    "auth_mode": "api_key",
                    "model": "kimi-k3",
                    "model_requested": True,
                    "provider": "isolated-sponsored-kimi",
                    "tools_active": [],
                },
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for("plain"),
                "auth_route": "api_key",
                "credential_source": "sponsored_kimi",
                "problem_text": display_problem_text[:20000],
                "agents": agents,
                "edges": [
                    {"from": left["trace_id"], "to": right["trace_id"]}
                    for left, right in zip(agents, agents[1:])
                ],
                "totals": {"latency_s": time.time() - started},
                "runner_result": {
                    "transport": "isolated-sponsored-kimi",
                    "public_run_id": str(payload.get("run_id") or ""),
                },
            },
            engine="plain",
        )
        _attach_analysis(run, workspace)
        _write_run(run)
        return run

    payload = sponsored_kimi_client.run_plain(
        problem_text,
        client_id=owner_id,
        stopped=stop_event.is_set,
        on_update=lambda update: project(update),
    )
    status = str(payload.get("status") or "").lower()
    if stop_event.is_set() or status == "stopped":
        return project(payload, terminal_status="stopped")
    if status != "finished":
        return project(payload, terminal_status="failed")
    proof = normalize_proof_markdown(str(payload.get("proof") or "")).strip()
    if not proof:
        payload = {**payload, "error": "Sponsored Kimi finished without a proof artifact"}
        return project(payload, terminal_status="failed")
    _atomic_write_text(workspace / "proof.md", proof + "\n")
    return project(payload, terminal_status="finished")


def _apply_math_harness_iteration_env(
    env: dict[str, str],
    *,
    engine: str,
    max_iterations: int | None,
    problem_id: str | None = None,
) -> None:
    """Project the bounded UI iteration choice into the new adapter process."""
    if engine not in _CGM_ENGINES or max_iterations is None:
        return
    bounded = max(1, min(int(max_iterations), 200))
    env["AGENT_MONITOR_MAX_ITERATIONS"] = str(bounded)
    env["MATH_HARNESS_MAX_ITERATIONS"] = str(bounded)
    if problem_id:
        env["AGENT_MONITOR_PROBLEM_ID"] = str(problem_id)[:240]


# Set by _run_cli_engine on a CGM run that published a new generation, so a
# Stop that reached it afterwards, or raced its normal end, never makes it
# "stopped" (the callers read it only when a Stop was seen).
_PUBLISHED_BEFORE_STOP = "published_before_stop"
# How long a CLI runner that Stop or the watchdog signalled may take to exit
# before _run_cli_engine kills its group.
_RUNNER_SETTLE_SECONDS = 30


def _published_before_stop(result: dict[str, Any]) -> bool:
    """Whether the run had published its proof (an Ansatz CGM run), so a Stop
    that reached it in its written-proof round, or just after it ended, does
    not make it "stopped"; takes the flag off ``result``."""
    return bool(result.pop(_PUBLISHED_BEFORE_STOP, False))


def _run_cli_engine(
    *,
    run_id: str,
    engine: str,
    problem_id: str,
    problem_text: str,
    started: float,
    workspace: Path,
    display_problem_text: str | None = None,
    extra_env: dict[str, str] | None = None,
    requested_model: str | None = None,
    max_iterations: int | None = None,
    owner_id: int | None = None,
    include_persona: bool = True,
) -> dict[str, Any]:
    """Run an external CLI harness (codex / openclaude / openhands / …) live."""
    import subprocess

    from agent_monitor.engines_registry import (
        CLI_ENGINES,
        build_cli_command,
        discard_prompt_file,
        prompt_on_stdin,
        proof_prompt,
        send_prompt_on_stdin,
    )

    spec = CLI_ENGINES.get(engine) or {}
    # Keep execution-only library/persona context out of the user-visible
    # canonical problem. Otherwise cloning a run injects the library again.
    record_problem_text = (
        display_problem_text if display_problem_text is not None else problem_text
    )
    # CLI engines can't read the agent home, so the operator's identity/skills/
    # memory ride along in the prompt.
    if (
        not include_persona
        or os.environ.get("AGENT_MONITOR_PUBLIC_KIMI", "").strip() == "1"
    ):
        preamble = ""
    else:
        try:
            from agent_monitor.agent_config import persona_preamble

            preamble = persona_preamble(workspace=workspace)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent-monitor] persona preamble unavailable: {exc}")
            preamble = ""
    analyze_run = engine == _ANALYZE_ENGINE
    manuscript_run = engine in _MANUSCRIPT_ENGINES
    if manuscript_run:
        # The runner reads its request.json; its command has no {prompt}.
        # The record shows the run's label instead of a proof task prompt.
        prompt = record_problem_text
    else:
        prompt = proof_prompt(
            problem_text, workspace=workspace, preamble=preamble,
            response_only=engine in {"kimi", "plain", "math_harness"},
        )
    argv = build_cli_command(
        engine,
        prompt=prompt,
        workspace=workspace,
        problem_file=workspace / "problem.txt",
        model=requested_model,
    )
    if not argv:
        hint = spec.get("install_hint") or "engine not configured"
        return normalize_run(
            {
                "run_id": run_id,
                "problem_id": problem_id,
                "trace_name": f"[{spec.get('label', engine).upper()}] {problem_id}",
                "status": "failed",
                "error": f"{spec.get('label', engine)} is not installed / configured",
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for(engine),
                "problem_text": record_problem_text[:20000],
                "agents": [
                    {
                        "trace_id": f"{run_id}::setup",
                        "stage_name": "setup",
                        "role": "system",
                        "pipeline_stage": "work",
                        "prompt": problem_text[:2000],
                        "output": f"Engine unavailable.\n\nInstall / configure:\n{hint}",
                        "status": "failed",
                    }
                ],
                "totals": {"latency_s": 0},
            },
            engine=engine,  # type: ignore[arg-type]
        )

    from agent_monitor.cli_events import CLIEventParser

    parser = CLIEventParser(spec.get("parser_style") or engine, source_engine=engine)
    # An analysis is listed as "Analyze · <title>" (a readability run as
    # "Readability · <title>") as soon as the runner has read the manuscript
    # (not only once it has exited).
    running_label: dict[str, str] = {}
    run_price_model: dict[str, str | None] = {}

    def _price_model(node_model: Any) -> str | None:
        """The model a trace node's tokens are priced at.

        The node's own model, then the stream's, when the pricing table has
        it.  The CGM runners name the model that served each call on its
        usage event, which the parser stamps on the node; other Codex-style
        streams name neither, and a reply may name a model the table lacks
        (a dated OpenAI snapshot).  Such a node is priced at the run's model,
        a provider prefix ("openai:gpt-5.2") resolved, when the table has an
        exact entry for it (``_exact_price_model``; either Max run has two
        models, so none).  Resolved on first need: the check loads the
        pricing table.
        """
        named = str(node_model or parser.usage.get("model") or "").strip()
        if named and _has_price(named):
            return named
        if "model" not in run_price_model:
            max_run = cgm_max.route_mode(extra_env) is not None
            run_price_model["model"] = None if max_run else _exact_price_model(
                requested_model or (extra_env or {}).get("AGENT_MONITOR_MODEL")
            )
        return run_price_model["model"]

    def _flush(
        output_tail: str,
        status: str = "running",
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        nonlocal record_problem_text
        if manuscript_run and not running_label:
            label = (_analyze_running_label if analyze_run else _readability_running_label)(workspace)
            if label:
                running_label["text"] = record_problem_text = label
        u = parser.usage
        # One node per model call/turn when the CLI streams JSON events;
        # otherwise fall back to a single session node (e.g. openhands TTY).
        agents = parser.agent_nodes(run_id, prompt=prompt, status=status)
        if not agents:
            agents = [
                {
                    "trace_id": f"{run_id}::{engine}_session",
                    "stage_name": f"{engine}_session",
                    "role": "agent",
                    "pipeline_stage": "finalize" if status != "running" else "work",
                    "prompt": prompt[:4000],
                    "output": output_tail[-12000:],
                    "status": status,
                    "latency_s": time.time() - started,
                    "model": u.get("model"),
                    "input_tokens": u.get("input_tokens") or None,
                    "total_input_tokens": parser.total_input_tokens(),
                    "output_tokens": u.get("output_tokens") or None,
                    "cache_read_tokens": u.get("cache_read_tokens") or 0,
                    "cache_write_tokens": u.get("cache_write_tokens") or 0,
                    "reasoning_tokens": u.get("reasoning_tokens") or None,
                    "cost_usd": u.get("cost_usd"),
                }
            ]
        elif status != "running":
            # Tokens the CLI only reported as session totals (not per turn)
            # go on the summary node so run totals stay correct.
            def _residual(field: str) -> int:
                return max(0, int(u.get(field) or 0) - sum(int(a.get(field) or 0) for a in agents))

            agents.append(
                {
                    "trace_id": f"{run_id}::{engine}_final",
                    "stage_name": f"{engine} session summary",
                    "role": "orchestrator",
                    "pipeline_stage": "finalize",
                    "call_seq": len(agents) + 1,
                    "prompt": prompt[:4000],
                    "prompt_source": "task prompt",
                    "output": output_tail[-12000:],
                    "output_source": f"{engine} activity log",
                    "memory_context": _workspace_memory(workspace),
                    "memory_source": "workspace snapshot",
                    "status": status,
                    "latency_s": time.time() - started,
                    "model": u.get("model"),
                    "input_tokens": _residual("input_tokens") or None,
                    "total_input_tokens": parser.input_total(
                        _residual("input_tokens"),
                        _residual("cache_read_tokens"),
                        _residual("cache_write_tokens"),
                    ),
                    "output_tokens": _residual("output_tokens") or None,
                    "cache_read_tokens": _residual("cache_read_tokens"),
                    "cache_write_tokens": _residual("cache_write_tokens"),
                    "reasoning_tokens": _residual("reasoning_tokens") or None,
                    "cost_usd": u.get("cost_usd"),
                }
            )
        edges = [
            {"from": a["trace_id"], "to": b["trace_id"]}
            for a, b in zip(agents, agents[1:])
        ]
        # Fill missing per-node costs from tokens + LiteLLM pricing, each at
        # the model that served it (_price_model).  Codex semantics (also used
        # for projected Claude usage) count cache reads and writes inside
        # input_tokens; only the uncached part is priced at the input rate so
        # cached input is not charged twice.
        for a in agents:
            if a.get("cost_usd") is None and (a.get("input_tokens") or a.get("output_tokens")):
                a["cost_usd"] = _estimate_cost(
                    _price_model(a.get("model")),
                    input_tokens=parser.uncached_input(
                        a.get("input_tokens"), a.get("cache_read_tokens"), a.get("cache_write_tokens")
                    ),
                    output_tokens=int(a.get("output_tokens") or 0),
                    cache_read=int(a.get("cache_read_tokens") or 0),
                    cache_write=int(a.get("cache_write_tokens") or 0),
                )
        total_cost = u.get("cost_usd")
        if total_cost is None:
            total_cost = sum(float(a.get("cost_usd") or 0) for a in agents) or None
            if total_cost is None and any(
                u.get(field)
                for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
            ):
                # Usage that no node holds yet (it came before the first node).
                total_cost = _estimate_cost(
                    _price_model(None),
                    input_tokens=parser.uncached_input(
                        u.get("input_tokens"), u.get("cache_read_tokens"), u.get("cache_write_tokens")
                    ),
                    output_tokens=int(u.get("output_tokens") or 0),
                    cache_read=int(u.get("cache_read_tokens") or 0),
                    cache_write=int(u.get("cache_write_tokens") or 0),
                )
        live = parser.live_state()
        if cgm_max.route_mode(extra_env) == "max":
            # Both models answer in one Max run; the run shows the mode, and
            # route_plan on the record names both models.
            live["auth_mode"] = cgm_max.ROUTE
            live["model"] = cgm_max.MODEL_ID
            live["model_requested"] = False
        elif cgm_max.route_mode(extra_env) == cgm_max.API_ROUTE_MODE:
            live["auth_mode"] = cgm_max.API_ROUTE
            live["model"] = cgm_max.API_MODEL_ID
            live["model_requested"] = False
        elif (extra_env or {}).get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
            live["auth_mode"] = "claude_subscription"
        elif (extra_env or {}).get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
            live["auth_mode"] = "chatgpt_subscription"
        else:
            live["auth_mode"] = "api_key"
        if status != "running":
            live["tools_active"] = []
        if not live.get("model"):
            # Codex and friends don't report a model in their event stream, so
            # fall back to what was asked for — flagged, since the CLI's own
            # configuration wins if it disagrees.
            live["model"] = requested_model or (extra_env or {}).get("AGENT_MONITOR_MODEL") or None
            live["model_requested"] = bool(live["model"])
        if requested_model:
            live["requested_model"] = requested_model
        run = normalize_run(
            {
                "run_id": run_id,
                "problem_id": problem_id,
                "trace_name": f"[{spec.get('label', engine).upper()}] {problem_id}",
                "status": status,
                "error": error,
                "model": live.get("model"),
                "requested_model": requested_model,
                "live": live,
                "updated_at": _now(),
                "workspace": str(workspace),
                "pipeline": _pipeline_for(engine),
                "auth_route": _route_from_record({"live": live}),
                "problem_text": record_problem_text[:20000],
                "agents": agents,
                "edges": edges,
                "totals": {
                    "latency_s": time.time() - started,
                    "cost_usd": total_cost,
                    # All input the model read: Claude's cache reads/writes are
                    # reported apart from input_tokens and would otherwise be
                    # missing (Codex already counts cached input inside it).
                    "input_tokens": parser.total_input_tokens() or None,
                    "cached_input_tokens": int(u.get("cache_read_tokens") or 0) or None,
                    "cache_write_tokens": int(u.get("cache_write_tokens") or 0) or None,
                    "output_tokens": u.get("output_tokens") or None,
                    # Part of output_tokens (never priced on top); the usage
                    # ledger books it from here, and continuations sum it.
                    "reasoning_tokens": int(u.get("reasoning_tokens") or 0) or None,
                },
                "runner_result": {"command": argv},
            },
            engine=engine,  # type: ignore[arg-type]
        )
        if running_label:
            # The run list's label comes from the session problems.
            run["problems"] = _seed_problems(record_problem_text, source="initial")
            run["problem_text_preview"] = record_problem_text[:500]
        _attach_analysis(run, workspace)
        _write_run(run)
        return run

    _flush(f"$ {' '.join(argv[:6])}…\n\nstarting…")
    # Never let a per-user API run inherit an operator or another provider's
    # credential from the long-lived server process.  Start with ordinary
    # runtime settings, remove every metered route, then overlay only the
    # already-resolved environment for this run owner.  Subscription routes
    # are scrubbed again below after the overlay as defense in depth.
    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env(extra=extra_env)
    if manuscript_run:
        _apply_analyze_runner_env(env, extra_env or {})
    if engine in _CGM_ENGINES and not (extra_env or {}).get(cgm_sources.SOURCES_ENV):
        # A CGM turn reads only the source material prepared for it.
        env.pop(cgm_sources.SOURCES_ENV, None)
    if engine == "kimi" and (extra_env or {}).get("AGENT_MONITOR_SPONSORED_KIMI") != "1":
        # Main-service availability flags are not authorization for this run.
        # Personal-key Kimi runs use the operator's endpoint independently of
        # any account-level base URL (starts and continuations alike).
        env["AGENT_MONITOR_SPONSORED_KIMI"] = "0"
        env.pop("AGENT_MONITOR_SPONSORED_KIMI_URL", None)
        env["AGENT_MONITOR_KIMI_BASE_URL"] = _operator_kimi_base()
    _apply_math_harness_iteration_env(
        env,
        engine=engine,
        max_iterations=max_iterations,
        problem_id=problem_id,
    )
    # Starts and continuations alike: the console's switch decides the round.
    _apply_cgm_writeup_env(env, engine=engine)
    _apply_selected_model_env(env, engine=engine, model=requested_model)
    if engine == "codex" and _is_kimi_model(requested_model):
        # Raw Codex CLI ignores OPENAI_BASE_URL and its modern custom-provider
        # path emits OpenAI-specific extensions.  The wrapper uses an isolated
        # Responses provider plus a loopback sanitizer, while preserving native
        # Codex JSONL events for Conversation/Monitor/DAG.  Like native Codex
        # it reads the prompt from stdin ("-", see prompt_on_stdin).
        argv = [
            sys.executable,
            "-u",
            str(Path(__file__).resolve().parent / "runners" / "kimi_codex_runner.py"),
            "-",
        ]
    elif engine == "codex":
        from agent_monitor.run_tuning import codex_config_args

        config_args = codex_config_args(env, requested_model)
        if config_args:
            # Before the prompt, or the "-" that reads it from stdin.
            insert_at = len(argv) - 1 if argv and argv[-1] in {prompt, "-"} else len(argv)
            argv[insert_at:insert_at] = config_args
    if engine == "openclaude" and max_iterations is not None:
        env["AGENT_MONITOR_OPENCLAUDE_MAX_TURNS"] = str(
            max(1, min(int(max_iterations), 200))
        )
    if engine == "claude" and max_iterations is not None:
        env["AGENT_MONITOR_CLAUDE_MAX_TURNS"] = str(
            max(1, min(int(max_iterations), 200))
        )
    if engine == "openhands" and max_iterations is not None:
        env["AGENT_MONITOR_OPENHANDS_MAX_ITERATIONS"] = str(
            max(1, min(int(max_iterations), 500))
        )
    if engine == "deepagents" and max_iterations is not None:
        # One LangGraph recursion step is smaller than one user-facing agent
        # iteration (assistant/tool transitions are separate graph nodes).
        env["AGENT_MONITOR_DEEPAGENTS_RECURSION_LIMIT"] = str(
            max(20, min(int(max_iterations) * 4, 800))
        )
    if env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        # Subscription is an explicit billing choice. Prevent a server- or
        # user-level API key from becoming an invisible fallback.
        _scrub_provider_route_env(env)
    if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
        # Claude account OAuth is not an Anthropic API key. Remove every API or
        # cloud-provider selector before the official CLI wrapper starts.
        _scrub_provider_route_env(env)
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AZURE_API_KEY",
            "AZURE_OPENAI_API_KEY",
        ):
            env.pop(name, None)
    env.setdefault("NO_COLOR", "1")
    from agent_monitor.engines_registry import engine_extra_path

    extra = engine_extra_path(engine)
    if extra:
        env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    codex_api_home = None
    if engine == "codex" and not _is_kimi_model(requested_model):
        codex_api_home = _ensure_codex_auth(env, owner_id)
    # Cap node heap so openclaw survives this small-RAM host (no swap).
    if engine == "openclaw":
        env.setdefault("NODE_OPTIONS", "--max-old-space-size=256")
    # OpenHands headless boots from LLM_MODEL / LLM_API_KEY (--override-with-envs).
    if engine == "openhands":
        key = env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY")
        if key:
            env.setdefault("LLM_API_KEY", key)
            env.setdefault("LLM_MODEL", env.get("AGENT_MONITOR_OPENHANDS_MODEL", "openai/gpt-5.2"))
    timeout_s = _cli_timeout_seconds(engine, env)
    stop_ev = _stop_event(run_id)
    buf: list[str] = []
    stdin_prompt = prompt_on_stdin(engine, argv)
    # A CGM runner publishes its generation before its written-proof round;
    # see "ended_after_publication" below.
    cgm_run = engine in _CGM_ENGINES
    receipt_before = _cgm_receipt_signature(workspace) if cgm_run else None
    operator_s = _cgm_operator_timeout_seconds(engine, env) if cgm_run else None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(workspace),
            env=env,
            stdin=subprocess.PIPE if stdin_prompt else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        _remove_codex_api_home(codex_api_home)
        discard_prompt_file(workspace)
        if cgm_run:
            cgm_sources.discard(workspace)
        label = str(spec.get("label") or engine)
        reason = f"{label} could not start: {exc.strerror or exc}"
        if getattr(exc, "errno", None) == errno.E2BIG:
            # Linux limits one command-line argument to 128 KiB.  The prompt
            # also holds the agent profile and library context, and up to
            # 40,000 characters of attachment excerpts.
            reason = (
                f"{label} could not start: the task prompt is too large to pass "
                "on the command line (it holds the problem text plus the agent "
                "profile, library context and attachment excerpts); shorten the "
                "problem text, or move long material into an attachment, of "
                "which only an excerpt is included"
            )
        return _flush(f"failed to launch: {exc}", status="failed", error=reason)

    _register_proc(run_id, proc)
    if stdin_prompt:
        send_prompt_on_stdin(proc, prompt)
    import queue
    import signal
    import threading

    def _terminate_group(*, force: bool = False) -> None:
        """Stop this wrapper and its grandchildren, without touching other runs."""
        if cgm_run and not force:
            # The watchdog stops a CGM runner as Stop does.
            _stop_cgm_runner(proc)
            return
        _terminate_registered_proc(proc, force=force)
        # The leader may have exited while a tool still holds the output pipe.
        # This Popen always owns a fresh session, so its original PGID is safe.
        try:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                proc.kill() if force else proc.terminate()
            except OSError:
                pass

    output_queue: queue.Queue[str | None] = queue.Queue()

    def _read_output() -> None:
        assert proc.stdout is not None
        try:
            for output_line in proc.stdout:
                output_queue.put(output_line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(
        target=_read_output, name=f"{engine}-{run_id}-stdout", daemon=True
    )
    reader.start()

    def _drain_output(until: float) -> None:
        """Parse what a stopping runner still prints, until it exits."""
        while time.time() < until:
            try:
                line = output_queue.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    return
                continue
            if line is None:
                return
            buf.append(line)
            parser.feed(line)

    stopped = False
    timed_out = False
    last_flush = 0.0
    deadline = started + timeout_s
    # An operator timeout bounds a CGM run until it publishes; only then may
    # the written-proof round's allowance (in timeout_s) be used.
    publish_by = started + min(operator_s, timeout_s) if operator_s else None
    try:
        while True:
            now = time.time()
            if publish_by is not None and now > publish_by:
                if _cgm_receipt_signature(workspace) in (None, receipt_before):
                    _terminate_group()
                    timeout_s = min(operator_s, timeout_s)
                    parser.lines.append(
                        f"[agent-monitor] timeout after {timeout_s}s before publication — terminated"
                    )
                    timed_out = True
                    break
                publish_by = None
            if stop_ev.is_set():
                # stop_run has signalled the runner already.  A CGM runner
                # gets no second SIGTERM: one arriving after its publication
                # could end it before its report.
                if not cgm_run:
                    _terminate_group()
                parser.lines.append("[agent-monitor] stopped by user")
                stopped = True
                break
            if now > deadline:
                _terminate_group()
                parser.lines.append(
                    f"[agent-monitor] timeout after {timeout_s}s — terminated"
                )
                timed_out = True
                break
            try:
                line = output_queue.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                # A number of CLI harnesses emit one setup event and then stay
                # silent for the whole model call. Keep their run timestamp,
                # elapsed time, and active-state panel moving even when there
                # is no new stdout line to parse.
                if now - last_flush > 2.0:
                    _flush(parser.output())
                    last_flush = now
                continue
            if line is None:
                break
            buf.append(line)
            parser.feed(line)
            if now - last_flush > 2.0:
                _flush(parser.output())
                last_flush = now
        settle = time.time() + _RUNNER_SETTLE_SECONDS
        if cgm_run and (stopped or timed_out):
            if stopped:
                # A progress write above may have crossed stop_run's: the
                # record reads "stopped" while the runner winds down.
                _flush(parser.output(), status="stopped")
            # On SIGTERM a CGM runner exits 143 before its publication, and
            # after it ends its written-proof round and reports the run.
            _drain_output(settle)
        try:
            proc.wait(timeout=max(0.1, settle - time.time()))
        except subprocess.TimeoutExpired:
            _terminate_group(force=True)
            proc.wait(timeout=10)
    finally:
        _terminate_group(force=True)
        reader.join(timeout=1)
        if proc.stdout is not None:
            proc.stdout.close()
        _unregister_proc(run_id, proc)
        _remove_codex_api_home(codex_api_home)
        # A wrapper that ended, or was stopped, before reading its prompt
        # file leaves it behind (a CGM runner its source material too).
        discard_prompt_file(workspace)
        if cgm_run:
            cgm_sources.discard(workspace)
    if engine == "openhands":
        parser.finalize_openhands()
    elif engine == "openclaw":
        parser.finalize_openclaw()
        _persist_openclaw_final_answer(parser, workspace)
    # A Stop or the watchdog that reaches a CGM run after it published its
    # generation (the receipt is written last) cuts short only its
    # written-proof round: the run is complete, and is recorded so.
    stop_requested = stopped or stop_ev.is_set()
    published = cgm_run and _cgm_receipt_signature(workspace) not in (None, receipt_before)
    ended_after_publication = published and (stop_requested or timed_out)
    if ended_after_publication:
        parser.lines.append(
            "[agent-monitor] the proof was already published; only the "
            "written-proof round was cut short"
        )
        timed_out = False
    elif stop_requested:
        return _flush(parser.output(), status="stopped")
    reconnect_codex = False
    if engine == "codex" and not _is_kimi_model(requested_model) and env.get("CODEX_HOME"):
        from agent_monitor import codex_login

        # Settings offers Reconnect once Codex rejects a linked login.
        reconnect_codex = codex_login.note_turn_outcome(
            env["CODEX_HOME"],
            completed=parser.terminal_status == "completed",
            failure=(parser.terminal_error or "") if parser.terminal_status == "failed" else "",
        )
    # Wrappers (claude_runner and friends) report their typed failure on
    # stderr as "[agent-monitor] <reason>"; it is the run's best explanation
    # when the structured stream itself carries none.
    runner_detail = next(
        (
            line.strip()[len("[agent-monitor] "):].strip()
            for line in reversed(buf)
            if line.strip().startswith("[agent-monitor] ")
        ),
        "",
    )
    if engine in {"codex", "claude"} and parser.terminal_status is None:
        parser.terminal_error = (
            runner_detail[-1200:] or "CLI ended without a terminal completion event"
        )
    ok = (
        (proc.returncode == 0 or ended_after_publication)
        and not timed_out
        and not parser.terminal_error
    )
    proof_missing = False
    if analyze_run:
        # An analysis succeeds when the runner exits 0 having published its
        # report and graph.  Its workspace has no proof, and a manuscript file
        # named like one (analysis/source/**/proof.tex) must never be promoted.
        if ok and not _analysis_published(workspace):
            parser.lines.append("[agent-monitor] Analyze exited successfully but published no report and graph")
            ok = False
            proof_missing = True
    elif manuscript_run:
        # Likewise a readability run: its readable version and receipt, and
        # never a promoted manuscript file (readability/source/**/proof.tex).
        if ok and not _readability_published(workspace):
            parser.lines.append("[agent-monitor] Readability exited successfully but published no readable version")
            ok = False
            proof_missing = True
    elif ok and _promote_proof_artifact(workspace) is None:
        parser.lines.append("[agent-monitor] engine exited successfully but produced no proof.md or proof.tex")
        ok = False
        proof_missing = True
    failure_reason = None
    if not ok:
        label = str(spec.get("label") or engine)
        # claude_runner's typed failure quotes the CLI's own result text and
        # adds what a web user can do ("reconnect Claude Code in Settings"
        # rather than the CLI's "Please run /login"), so it comes first.
        detail = (
            (runner_detail[-1200:] or parser.terminal_error)
            if engine == "claude"
            else (parser.terminal_error or runner_detail[-1200:])
        )
        if reconnect_codex and detail:
            # Likewise for a linked Codex login, instead of its CLI's "Please
            # log out and sign in again".
            detail = f"{detail.rstrip('. ')}; {codex_login.RECONNECT_ADVICE}."
        failure_reason = _cli_failure_reason(
            label=label,
            returncode=proc.returncode,
            timed_out=timed_out,
            timeout_s=timeout_s,
            proof_missing=proof_missing,
            detail=detail or None,
            artifact=("analysis report" if analyze_run else "readable version" if manuscript_run
                      else "proof artifact"),
        )
    run = _flush(
        parser.output(),
        status="finished" if ok else "failed",
        error=failure_reason,
    )
    if published:
        # For the worker (see _published_before_stop), not the record: a Stop
        # that reaches the worker after this point (it raced the run's normal
        # end) must not turn this published run into a stopped one either.
        run[_PUBLISHED_BEFORE_STOP] = True
    return run


_OPENCLAUDE_EXECUTION_RETRY_MARKER = "OPENCLAUDE SAME-HARNESS RECOVERY:"


def _openclaude_recoverable_failure(error: str) -> str | None:
    """Classify bounded OpenClaude failures safe for one continuation."""
    normalized = error.strip().casefold()
    if normalized.endswith(": error_during_execution"):
        return "error_during_execution"
    if normalized.endswith(": error_max_turns"):
        return "error_max_turns"
    if normalized.startswith(
        "openclaude exited with code 1: stopped: repeated tool failures detected."
    ):
        return "repeated_tool_failures"
    return None


def _proof_checkpoint_signature(workspace: Path) -> tuple[str, int, str] | None:
    """Fingerprint a substantive, regular root proof without following symlinks."""
    artifact = _root_proof_artifact(workspace)
    if artifact is None or artifact.is_symlink():
        return None
    try:
        payload = artifact.read_bytes()
        if len(payload) < 400 or artifact.resolve().parent != workspace.resolve():
            return None
    except OSError:
        return None
    return artifact.name, len(payload), hashlib.sha256(payload).hexdigest()


def _openclaude_execution_retry_needed(
    *,
    engine: str,
    result: dict[str, Any],
    before_signature: tuple[str, int, str] | None,
    workspace: Path,
) -> bool:
    """Retry only a transient OpenClaude failure with a new current-run checkpoint."""
    if engine != "openclaude" or result.get("status") != "failed":
        return False
    error = str(result.get("error") or "")
    if _openclaude_recoverable_failure(error) is None:
        return False
    after_signature = _proof_checkpoint_signature(workspace)
    return after_signature is not None and after_signature != before_signature


def _run_cli_engine_with_openclaude_retry(**kwargs: Any) -> dict[str, Any]:
    """Run a CLI engine, retrying one narrow OpenClaude transport failure once.

    The retry remains on OpenClaude, begins only after this invocation wrote a
    new substantive root proof, and preserves both attempts in Monitor lineage.
    A stale artifact, another error subtype, or a failed retry remains failed.
    The second accepted subtype is OpenClaude's own repeated-edit guard: it is
    recoverable only when the failed attempt already produced a new proof
    checkpoint. A max-turn stop follows the same rule so a continuation can
    finish an already-substantial artifact without restarting broad research.
    Every continuation is still limited to one attempt.
    """
    workspace = Path(kwargs["workspace"])
    before_signature = _proof_checkpoint_signature(workspace)
    first = _run_cli_engine(**kwargs)
    engine = str(kwargs.get("engine") or "")
    if not _openclaude_execution_retry_needed(
        engine=engine,
        result=first,
        before_signature=before_signature,
        workspace=workspace,
    ):
        return first
    if _stop_event(str(kwargs.get("run_id") or "")).is_set():
        return first

    original_problem = str(kwargs.get("problem_text") or "")
    recovery = f"""
{_OPENCLAUDE_EXECUTION_RETRY_MARKER} The OpenClaude execution transport ended
after this invocation wrote a new proof checkpoint. Continue once with the
same selected OpenClaude harness. Read the current root proof artifact first,
preserve every supported result, and finish the original request. Do not start
a new broad search and do not switch engines. If the prior stop reported
repeated tool failures, re-read the file before editing and use a fresh safe
whole-file write instead of repeating a stale exact-text edit. If CANDIDATE AUDIT GATE is
active, complete and validate its required audit files or disclose the exact
remaining degradation. A retry does not promote an open-problem outcome.
""".strip()
    retry_kwargs = dict(kwargs)
    retry_kwargs["problem_text"] = original_problem.rstrip() + "\n\n" + recovery
    retry_kwargs["started"] = time.time()
    second = _run_cli_engine(**retry_kwargs)
    retry_id = f"{kwargs.get('run_id') or 'run'}-openclaude-retry-1"
    merged = _merge_continuation_run(first, second, job_id=retry_id)
    merged["internal_retry_count"] = 1
    subtype = _openclaude_recoverable_failure(str(first.get("error") or ""))
    merged["internal_retry_reason"] = f"{subtype}_after_new_checkpoint"
    return merged


def _wrap_subprocess_result(
    *,
    run_id: str,
    engine: str,
    problem_id: str,
    problem_text: str,
    result: dict[str, Any],
    started: float,
    workspace: Path | None = None,
) -> dict[str, Any]:
    status = result.get("status") or "finished"
    ok = status in {"finished", "ok"}
    output = (
        (result.get("stdout_tail") or "")
        + ("\n\n--- stderr ---\n" + (result.get("stderr_tail") or "") if result.get("stderr_tail") else "")
        + ("\n\n" + (result.get("error") or "") if result.get("error") else "")
        + ("\n\n" + (result.get("hint") or "") if result.get("hint") else "")
    )
    proof = _promote_proof_artifact(workspace) if ok else None
    if ok and proof is None:
        ok = False
        status = "failed"
        result = {**result, "error": "Engine exited successfully but produced no proof artifact"}
        output = (output + "\n\n" + result["error"]).strip()
    research_audit_status = (
        workspace_reconciliation_status(workspace)
        if workspace is not None
        and _RESEARCH_AUDIT_GATE_MARKER in problem_text
        else {"status": "not_requested", "valid": False, "audits": []}
    )
    run = normalize_run(
        {
            "run_id": run_id,
            "problem_id": problem_id,
            "trace_name": f"[{engine.upper()}] {problem_id}",
            "status": "finished" if ok else "failed",
            "error": result.get("error"),
            "updated_at": _now(),
            "pipeline": _pipeline_for(engine),
            "problem_text": problem_text[:20000],
            "agents": [
                {
                    "trace_id": f"{run_id}::{engine}_main",
                    "stage_name": f"{engine}_main",
                    "role": "orchestrator",
                    "pipeline_stage": "finalize" if ok else "workflow",
                    "prompt": problem_text[:4000],
                    "output": output[-12000:] or json.dumps(result, indent=2)[:8000],
                    "status": "finished" if ok else "failed",
                    "latency_s": time.time() - started,
                }
            ],
            "edges": [],
            "totals": {"latency_s": time.time() - started},
            "workspace": str(workspace) if workspace else None,
            "runner_result": {
                k: result.get(k)
                for k in (
                    "status",
                    "returncode",
                    "workflow",
                    "command",
                    "hint",
                    "output_dir",
                    "supplemental_audit_status",
                    "supplemental_audit_returncode",
                    "promoted_audit_files",
                )
            }
            | {"research_audit": research_audit_status},
        },
        engine=engine,  # type: ignore[arg-type]
    )
    if workspace:
        rich = _artifact_agents(engine, workspace, run_id)
        if rich and rich.get("agents"):
            run = normalize_run(
                _merge_artifact_run(run, rich),
                engine=engine,  # type: ignore[arg-type]
            )
    if not run.get("analysis"):
        _attach_analysis(run, workspace)
    return run


def _cleanup_hermes_memory(run_id: str, workspace: str | None) -> list[str]:
    """Remove Hermes session dumps / DB rows tied to this run."""
    import sqlite3

    removed: list[str] = []
    sessions_dir = HERMES_HOME / "sessions"
    if not sessions_dir.is_dir():
        return removed

    needles = [run_id]
    if workspace:
        needles.append(workspace)
        needles.append(Path(workspace).name)

    session_ids: set[str] = set()
    for path in list(sessions_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(n and n in text for n in needles):
            continue
        name = path.name
        if name.startswith("request_dump_"):
            rest = name.removeprefix("request_dump_")
            parts = rest.split("_")
            if len(parts) >= 2:
                session_ids.add(f"{parts[0]}_{parts[1]}")
        try:
            path.unlink()
            removed.append(name)
        except OSError:
            pass

    for sid in session_ids:
        for extra in sessions_dir.glob(f"*{sid}*"):
            if extra.is_file() and extra.name not in removed:
                try:
                    extra.unlink()
                    removed.append(extra.name)
                except OSError:
                    pass

    db = HERMES_HOME / "state.db"
    if db.exists() and session_ids:
        try:
            with sqlite3.connect(db) as conn:
                for sid in session_ids:
                    conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                    conn.execute(
                        "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
                        (sid,),
                    )
                    conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                conn.commit()
        except sqlite3.Error:
            pass

    return removed


def _chat_file(run_id: str) -> Path:
    return RUNS_DIR / "workspaces" / run_id / "human_chat.jsonl"


def list_chat(run_id: str) -> list[dict[str, Any]]:
    path = _chat_file(run_id)
    msgs: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Older/evaluation continuations could persist reviewer guidance in the
    # session problem ledger without first emitting a user chat event. Keep
    # those entries out of Sandbox Problem cards, but recover them here so the
    # guidance remains visible in Human in the loop and exported Conversation.
    seen_user: set[str] = set()
    for message in msgs:
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "").strip()
        seen_user.add(content)
        # A turn records the prompt-facing form of its text explicitly. Older
        # records predate that field, so also reconstruct it from the files.
        if message.get("ledger_text"):
            seen_user.add(str(message["ledger_text"]).strip())
        elif message.get("attachments"):
            seen_user.add(
                content
                + "\n\nAttached research files:\n"
                + "\n".join(
                    f"- {json.dumps(item['name'], ensure_ascii=False)}"
                    for item in message["attachments"]
                )
            )
    try:
        run = _load_run_record(run_id)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        run = {}
    for item in run.get("problems") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip().lower()
        text = _problem_item_text(item)
        if source not in {"human", "hitl", "feedback"} or not text or text in seen_user:
            continue
        seen_user.add(text)
        msgs.append(
            {
                "role": "user",
                "content": text,
                "ts": item.get("ts") or run.get("updated_at") or "",
                "recovered_from_session": True,
            }
        )
    return msgs


def _append_chat(
    run_id: str,
    role: str,
    content: str,
    *,
    attachments: list[dict] | None = None,
    ledger_text: str | None = None,
) -> dict[str, Any]:
    path = _chat_file(run_id)
    entry = {"role": role, "content": content, "ts": _now()}
    if attachments:
        entry["attachments"] = attachments
    if ledger_text and ledger_text != content:
        # The session ledger stores the prompt-facing form of this turn. Record
        # it so ledger recovery below does not re-add the turn as a duplicate.
        entry["ledger_text"] = ledger_text
    with _CHAT_LOCK:
        if _run_deleted(run_id):
            # A worker of a deleted run winding down: its lines would bring
            # the workspace back (with only this chat in it).
            return entry
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _run_is_active(run_id: str) -> dict[str, Any] | None:
    with _LOCK:
        for j in _JOBS.values():
            if j.get("run_id") == run_id and j.get("status") == "running":
                return dict(j)
    return None


# The real thread class: the doubles embedders and tests install for
# ``threading.Thread`` run nothing, so there is no worker of theirs to wait for.
_THREAD_CLASS = threading.Thread


def _track_worker(job_id: str, run_id: str, thread: Any) -> None:
    """Track a session's started worker thread until it is done (_WORKERS)."""
    if isinstance(thread, _THREAD_CLASS):
        with _LOCK:
            _WORKERS[job_id] = (run_id, thread)


def _proof_turn_begin(run_id: str, ws: Path, *, answer_only: bool = False) -> int | None:
    """A proof turn starting (proof_turns): never for a deleted run, whose
    workspace must not come back.  Best-effort: it never raises, so keeping
    a turn's copy can never fail the turn."""
    try:
        if _run_deleted(run_id):
            return None
        from agent_monitor import proof_turns

        return proof_turns.begin(ws, started_at=_now(), answer_only=answer_only)
    except Exception as exc:  # noqa: BLE001 - a turn's copy is best-effort
        print(f"[proof_turns] a turn start was not recorded: {type(exc).__name__}", file=sys.stderr, flush=True)
        return None


def _proof_turn_end(run_id: str, ws: Path, turn: int | None) -> None:
    """A proof turn ending: its proof kept, with the status its record says
    (once: a turn already ended is left as it is).  It runs in the workers'
    ``finally`` blocks just before ``_end_worker``, so it never raises: an
    error here must never leave the run's worker registered (the run would
    stay running and no Continue could start)."""
    try:
        if not turn or _run_deleted(run_id):
            return
        try:
            status = str(_load_run_record(run_id).get("status") or "finished")
        except (FileNotFoundError, OSError, ValueError):
            status = "finished"
        if status not in {"finished", "failed", "stopped"}:
            status = "finished"
        from agent_monitor import proof_turns

        proof_turns.end(ws, turn, status=status, finished_at=_now())
    except Exception as exc:  # noqa: BLE001 - a turn's copy is best-effort
        print(f"[proof_turns] a turn end was not recorded: {type(exc).__name__}", file=sys.stderr, flush=True)


def _end_worker(job_id: str) -> None:
    """The worker of ``job_id`` is done with its run: a continuation may start."""
    with _LOCK:
        worker = _WORKERS.pop(job_id, None)
        if worker is not None:
            # A deleted run's record is written no more (_DELETED_RUN_LAST).
            _DELETED_RUN_LAST.pop(worker[0], None)


def _worker_not_done(thread: Any) -> bool:
    """Whether a tracked worker thread is not done with its run: it runs, or
    it was registered and has not started yet (_register_claimed_job)."""
    return thread.ident is None or thread.is_alive()


def _session_worker_alive(run_id: str) -> bool:
    """Whether a worker of one of ``run_id``'s sessions is not done with it yet.

    The calling thread never counts: an ending worker starts the feedback
    queued for its run (_launch_pending_feedback), which must not wait for
    the worker that starts it.
    """
    current = threading.get_ident()
    with _LOCK:
        for job_id, (_run_id, thread) in list(_WORKERS.items()):
            if not _worker_not_done(thread):
                _WORKERS.pop(job_id, None)  # ended without saying so
        return any(
            worker_run == run_id and thread.ident != current
            for worker_run, thread in _WORKERS.values()
        )


RUN_STOPPING_ERROR = "This run is still stopping; try again in a few seconds."


def run_is_stopping(run_id: str) -> bool:
    """Whether ``run_id`` no longer runs but its stopped session still winds
    down: its worker is not done with the run yet (see _WORKERS)."""
    return _run_is_active(run_id) is None and _session_worker_alive(run_id)


# Runs whose continuation is being prepared (route and credential checks) but
# is not registered in _JOBS yet.  A second continue or chat request in that
# window must not start a duplicate continuation.
_LAUNCHING_RUNS: set[str] = set()


def _claim_launch(run_id: str) -> bool:
    """Reserve ``run_id`` for one new continuation; False while it is active.

    That includes a stopped session whose worker still winds down: a
    continuation beside it would share its workspace, record and prompt
    file, and clear the stop flag that worker still reads.
    """
    with _LOCK:
        if run_id in _LAUNCHING_RUNS or any(
            job.get("run_id") == run_id and job.get("status") == "running"
            for job in _JOBS.values()
        ) or _session_worker_alive(run_id):
            return False
        _LAUNCHING_RUNS.add(run_id)
        return True


def _release_launch(run_id: str) -> None:
    """Drop a claim whose continuation did not start."""
    with _LOCK:
        _LAUNCHING_RUNS.discard(run_id)


def _register_claimed_job(run_id: str, job: dict[str, Any], thread: Any = None) -> None:
    """Register a claimed run's continuation; its running job replaces the claim.

    Its worker (not started yet) is tracked in the same hold: a Stop and a
    Continue before the thread starts must not find the run idle."""
    with _LOCK:
        _JOBS[job["job_id"]] = job
        if isinstance(thread, _THREAD_CLASS):
            _WORKERS[job["job_id"]] = (run_id, thread)
        _LAUNCHING_RUNS.discard(run_id)


def _start_claimed_worker(run_id: str, job_id: str, thread: Any, announce: str) -> None:
    """Say the claimed run continues and start the worker registered with it.

    Should either fail (the chat line on a full disk, the thread's start),
    the worker that never ran is ended and its job failed: the run is left
    neither running nor stopping with no thread to end it."""
    try:
        _append_chat(run_id, "system", announce)
        thread.start()
    except BaseException:
        with _LOCK:
            _end_worker(job_id)
            _update_job(job_id, status="failed", error="worker did not start")
        raise


def run_is_active(run_id: str) -> bool:
    """Whether ``run_id`` is running, a continuation of it is starting, or
    its stopped session still winds down (run_is_stopping)."""
    if _run_is_active(run_id):
        return True
    with _LOCK:
        return run_id in _LAUNCHING_RUNS or _session_worker_alive(run_id)


def _load_run_record(run_id: str) -> dict[str, Any]:
    for path in (_cache_dir() / f"{run_id}.json", RUNS_DIR / f"{run_id}.json"):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"run not found: {run_id}")


def _record_age_seconds(run: dict[str, Any]) -> float:
    raw = str(run.get("updated_at") or run.get("created_at") or "")
    if not raw:
        return float("inf")
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except ValueError:
        return float("inf")


def reconcile_run_status(
    run_id: str,
    *,
    min_age_seconds: float = 10.0,
    reason: str = "The server restarted or the worker process exited without a terminal record.",
) -> dict[str, Any] | None:
    """Turn an orphaned persisted `running` record into `interrupted`.

    In-memory jobs are authoritative while this process is alive. Persisted
    `running` records cannot survive a server restart because engine workers
    are daemon threads/child processes owned by this service.
    """
    with _RECONCILE_LOCK:
        if _run_is_active(run_id):
            try:
                return _load_run_record(run_id)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return None
        try:
            run = _load_run_record(run_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        auto_pipeline_enabled = (
            os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "").strip()
            != "1"
        )
        if str(run.get("status") or "") != "running":
            workspace = Path(str(run.get("workspace") or workspace_dir(run_id)))
            if auto_pipeline_enabled:
                try:
                    from agent_monitor import auto_pipeline

                    auto_pipeline.reconcile_orphaned(workspace, reason)
                except (OSError, ValueError):
                    pass
            return reconcile_ask_state(run_id, reconcile_recheck_state(run_id, run))
        if _record_age_seconds(run) < max(0.0, min_age_seconds):
            return run

        detected_at = _now()
        last_activity = run.get("updated_at") or run.get("created_at")
        run["status"] = "interrupted"
        run["completed"] = False
        run["updated_at"] = detected_at
        run["interruption"] = {
            "detected_at": detected_at,
            "last_activity_at": last_activity,
            "reason": reason,
            "recoverable": True,
        }
        live = dict(run.get("live") or {})
        live["tools_active"] = []
        live["interrupted"] = True
        run["live"] = live
        for agent in run.get("agents") or []:
            if isinstance(agent, dict) and agent.get("status") == "running":
                agent["status"] = "interrupted"
                agent["stop_reason"] = "server_restart_or_worker_lost"
        _write_run(run)

        workspace = Path(str(run.get("workspace") or workspace_dir(run_id)))
        if auto_pipeline_enabled:
            try:
                from agent_monitor import auto_pipeline

                auto_pipeline.mark_interrupted(workspace, reason)
            except (OSError, ValueError):
                pass
        if run.get("run_kind") == "analyze" or run.get("engine") == _ANALYZE_ENGINE:
            run["interruption"]["recoverable"] = False
            _write_run(run)
            _append_chat(
                run_id,
                "system",
                "Analysis interrupted: its worker is no longer active (the server "
                "may have restarted). Start a new analysis to run it again.",
            )
            return run
        if run.get("run_kind") == "readability" or run.get("engine") == _READABILITY_ENGINE:
            run["interruption"]["recoverable"] = False
            _write_run(run)
            _append_chat(
                run_id,
                "system",
                "Readability run interrupted: its worker is no longer active (the server "
                "may have restarted). Start a new readability run to read it again.",
            )
            return run
        _append_chat(
            run_id,
            "system",
            "Run interrupted: its worker is no longer active. "
            "The saved proof and workspace are intact; press Continue to resume.",
        )
        return run


def reconcile_stale_running_runs(*, min_age_seconds: float = 10.0) -> list[str]:
    """Reconcile every manifest entry still marked running without a live job."""
    manifest_path = _cache_dir() / "manifest.json"
    run_ids: list[str] = []
    if manifest_path.exists():
        try:
            entries = (json.loads(manifest_path.read_text(encoding="utf-8")) or {}).get("runs") or []
            run_ids = [
                str(entry.get("run_id") or "")
                for entry in entries
                if entry.get("run_id") and entry.get("status") == "running"
            ]
        except (AttributeError, json.JSONDecodeError, OSError):
            run_ids = []
    if not run_ids:
        for path in _cache_dir().glob("*.json"):
            if path.name == "manifest.json":
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if value.get("run_id") and value.get("status") == "running":
                run_ids.append(str(value["run_id"]))

    # A main harness may already be terminal while its concurrent Lean/DAG
    # sidecar was lost during a server restart.  Such runs are intentionally
    # absent from the `running` manifest subset above, so discover persisted
    # sidecars independently and let reconcile_run_status close them.
    workspace_root = RUNS_DIR / "workspaces"
    if (
        os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "").strip() != "1"
        and workspace_root.is_dir()
    ):
        for state_path in workspace_root.glob("*/auto_pipeline.json"):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if str(state.get("status") or "") == "running":
                run_ids.append(state_path.parent.name)

    # An analysis whose re-check worker was lost is "finished" with a re-check
    # still "running" (a readability run likewise with a question): the
    # manifest flags it, so a restart repairs it too.
    if manifest_path.exists():
        try:
            entries = (json.loads(manifest_path.read_text(encoding="utf-8")) or {}).get("runs") or []
            run_ids.extend(
                str(entry.get("run_id") or "")
                for entry in entries
                if entry.get("run_id")
                and (entry.get("recheck_running") is True or entry.get("ask_running") is True)
            )
        except (AttributeError, json.JSONDecodeError, OSError):
            pass

    reconciled: list[str] = []
    for run_id in dict.fromkeys(run_ids):
        before = None
        try:
            before = _load_run_record(run_id).get("status")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        result = reconcile_run_status(run_id, min_age_seconds=min_age_seconds)
        if before == "running" and result and result.get("status") == "interrupted":
            reconciled.append(run_id)
    return reconciled


_PENDING_FEEDBACK_FILENAME = "human_feedback_pending.jsonl"


def _canonical_claude_continuation_model(value: object) -> str | None:
    """Map resolved Claude telemetry back to a selectable account model id."""
    selected = str(value or "").strip().lower()
    if selected in {"sonnet", "opus", "fable", "claude-haiku-4-5"}:
        return selected
    for family in ("sonnet", "opus", "fable"):
        if selected.startswith(f"claude-{family}-"):
            return family
    if selected.startswith("claude-haiku-4-5-"):
        return "claude-haiku-4-5"
    return None


def _continuation_model(run_data: dict[str, Any], requested: str | None) -> str | None:
    """Resolve the model a follow-up should display and actually reuse."""
    explicit = str(requested or "").strip()
    if cgm_max.is_max_record(run_data):
        # A Max run continues as Max; its two models come from the route plan.
        return cgm_max.MODEL_ID
    if cgm_max.is_max_api_record(run_data):
        # Likewise Max (API), on its recorded API models.
        return cgm_max.API_MODEL_ID
    if explicit:
        # Explicit input is checked against the recorded route by the caller
        # (``_member_continuation_model``); never reinterpret a selection as
        # another account model.
        return explicit
    live = run_data.get("live") or {}
    agents = run_data.get("agents") or []
    candidates: list[Any] = [
        run_data.get("requested_model"),
        live.get("requested_model") if isinstance(live, dict) else None,
        run_data.get("model"),
    ]
    candidates.extend(
        agent.get("model")
        for agent in reversed(agents)
        if isinstance(agent, dict)
    )
    candidates.append(live.get("model") if isinstance(live, dict) else None)
    if _route_from_record(run_data) == "claude_subscription":
        for candidate in candidates:
            canonical = _canonical_claude_continuation_model(candidate)
            if canonical:
                return canonical
        if any(
            str(candidate or "").strip() not in {"", "—"}
            for candidate in candidates
        ):
            # The console's follow-ups keep the run's model and offer no
            # choice, so point to a new run rather than ask for a model.
            raise ValueError(
                "This run stored an unsupported Claude account model and cannot "
                "continue; start a new run on sonnet, opus, fable, or claude-haiku-4-5"
            )
        return None
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _recorded_route_runs(
    user: dict[str, Any] | None, engine: str, run_data: dict[str, Any], model: str | None
) -> bool:
    """Whether the run's recorded route can run ``model`` on ``engine``.

    As a start checks it: Max only on the Max route, the ``kimi`` engine only
    Kimi K3, and a linked account only a model of its own that the engine
    supports.  Any other API model is left to the continuation's credential
    check.
    """
    from agent_monitor.engines_registry import supported_models
    from agent_monitor.settings import GUEST_DEFAULT_MODEL

    route = _route_from_record(run_data)
    if cgm_max.is_max_model(model):
        return route == cgm_max.ROUTE
    if cgm_max.is_max_api_model(model):
        # Only a Max (API) run continues as Max (API), on its API route.
        return route == cgm_max.API_ROUTE and cgm_max.is_max_api_record(run_data)
    if engine == "kimi":
        return model == GUEST_DEFAULT_MODEL
    if route not in {"claude_subscription", "codex_subscription"}:
        return True
    allowed = supported_models(engine)
    if allowed and model not in allowed:
        return False
    try:
        _validate_subscription_model(route=route, model=model, user=user, field="Selected model")
    except ValueError:
        return False
    return True


def _member_continuation_model(
    user: dict[str, Any] | None,
    engine: str,
    run_data: dict[str, Any],
    requested: str | None,
) -> str | None:
    """The continuation's model, after refusing what a member may not run.

    A member continues only on an engine and recorded route it may start
    (never a subscription or Max).  A requested model the account may not run
    on this engine, or that the run's recorded route cannot run, leaves the
    run on its recorded model: an older console's chat composer sends the
    launcher's current model, which may belong to another route or to the
    other Ansatz CGM engine (Kimi K3, or a model from the member's own keys).
    So does a Kimi model other than Kimi K3 where the follow-up would run as
    Kimi K3 under its name (an engine that runs Kimi K3 alone, or a run on
    the sponsored route: :func:`_other_kimi_model_error`), for every account;
    a run recorded on one there (started before such models were refused) is
    not continued.  A coding-agent harness's own-key run continues on the
    Kimi model it names, as it started.  Nor is a Plain run on the included
    Kimi K3 continued (:data:`SPONSORED_PLAIN_FOLLOW_UP_ERROR`).
    """
    route_error = account_policy.member_continuation_error(
        user, _route_from_record(run_data), record=run_data
    )
    error = account_policy.member_engine_error(user, engine) or route_error
    if error:
        raise ValueError(error)
    sponsored = str(run_data.get("credential_source") or "").strip() == "sponsored_kimi"
    if engine == "plain" and sponsored:
        raise ValueError(SPONSORED_PLAIN_FOLLOW_UP_ERROR)
    model = _continuation_model(run_data, requested)
    if str(requested or "").strip() and (
        account_policy.member_model_error(user, engine, model)
        or not _recorded_route_runs(user, engine, run_data, model)
        or _other_kimi_model_error(engine, "prove", model, sponsored=sponsored)
    ):
        model = _continuation_model(run_data, None)
    error = account_policy.member_model_error(user, engine, model) or _other_kimi_model_error(
        engine, "prove", model, ran=True, sponsored=sponsored
    )
    if error:
        raise ValueError(error)
    return model


# The sponsored service runs a Plain problem as one isolated run of its own
# (_run_sponsored_kimi_plain), and a follow-up has no such route: the Plain
# runner it would start has no sponsored credential.  Refused before the
# message is recorded or a run start is kept, rather than failing after.
SPONSORED_PLAIN_FOLLOW_UP_ERROR = (
    "A Plain run on the included Kimi K3 can't be continued; start a new Plain run with your feedback"
)


def _owner_user(owner_id: int | None) -> dict[str, Any] | None:
    """The account for a background follow-up, with its administrator role
    and whether it has full access."""
    if owner_id is None:
        return None
    from agent_monitor.auth import is_full_access_account, is_operator_account

    return {"id": owner_id, "is_admin": is_operator_account(int(owner_id)),
            "full_access": is_full_access_account(int(owner_id))}


def tool_free_run(run_id: str, run: dict[str, Any]) -> bool:
    """Whether a proof's prompt carries project text that an account which is
    not an administrator could write: it started so, or its project has, or
    had, such a member.  It, its follow-ups and its Formal harness stay
    tool-free (``account_policy.PROJECT_TOOL_FREE_ERROR``)."""
    if run.get("tool_free"):
        return True
    from agent_monitor import analyze_runs

    if analyze_runs.is_manuscript_record(run):
        return False  # a manuscript run never receives the project's document
    from agent_monitor import projects

    return projects.untrusted_run(run_id)


def _tool_free_follow_up(
    run_id: str, run: dict[str, Any], engine: str, model: str | None
) -> bool:
    """Whether a follow-up must stay tool-free; ProjectPolicyError when this
    one would use a tool engine, a subscription or Max."""
    if not tool_free_run(run_id, run):
        return False
    # A follow-up keeps the run's recorded route; one never recorded would
    # take the account's runtime, which may be a subscription.
    route = _route_from_record(run)
    if route != "api_key" or account_policy.project_run_error(engine, model=model, auth_route=route):
        raise account_policy.ProjectPolicyError(account_policy.PROJECT_TOOL_FREE_ERROR)
    return True


def _feedback_entries(raw: str) -> list[dict[str, Any]]:
    """Parse queued feedback lines into ``content``/``author_id``/``ts``
    entries, with ``problem_added`` (whether queueing the message added it to
    the session problems; ``None`` for feedback queued before that was
    recorded), ``attachment_ids`` (the files it brought) and ``booking`` (the
    run start the console booked for it, see _queue_human_feedback)."""
    entries: list[dict[str, Any]] = []
    # One JSON object a line, each ended by "\n" (_feedback_line), with any
    # "\n" or "\r" inside it escaped.  Not splitlines(): a message's own
    # U+2028, U+2029 or U+0085 stays raw in its line (ensure_ascii=False),
    # and splitting there turned one message into two of raw JSON, without
    # its author (it ran as the run's owner) or what a Stop takes back.
    for line in raw.split("\n"):
        try:
            item = json.loads(line) or {}
            content = str(item.get("content") or "").strip()
            author = item.get("author_id")
            stamp = item.get("ts")
            added = item.get("problem_added")
            files = item.get("attachment_ids")
            booking = item.get("booking")
        except (AttributeError, json.JSONDecodeError):
            content, author, stamp, added, files, booking = line.strip(), None, None, None, None, None
        if content:
            entries.append({
                "content": content,
                # Feedback queued before authors were recorded has none.
                "author_id": author if type(author) is int else None,
                "ts": stamp or _now(),
                "problem_added": added if type(added) is bool else None,
                "attachment_ids": [
                    ident for ident in files if isinstance(ident, str)
                ] if isinstance(files, list) else [],
                "booking": booking if type(booking) in (int, float) else None,
            })
    return entries


def _feedback_line(entry: dict[str, Any]) -> str:
    """One queued message as its queue file stores it."""
    kept = {
        key: entry[key]
        for key in ("content", "ts", "author_id", "problem_added", "booking")
        if entry.get(key) is not None
    }
    if entry.get("attachment_ids"):
        kept["attachment_ids"] = list(entry["attachment_ids"])
    return json.dumps(kept, ensure_ascii=False) + "\n"


def _queue_human_feedback(
    workspace: Path,
    message: str,
    *,
    author_id: int | None = None,
    problem_added: bool | None = None,
    attachment_ids: Sequence[str] = (),
    booking: float | None = None,
) -> bool:
    """Durably append feedback that arrived while an engine was active.

    ``problem_added`` records whether the message was added to the session
    problems for it (a message the session already had was not), and
    ``attachment_ids`` the files it brought: a Stop that drops the message
    takes back exactly that (_record_stopped).  So is ``booking``, the run
    start the console booked for the follow-up this message will start (a
    member's): kept unless the message joins one already booked.

    Returns whether ``author_id`` already had feedback waiting, so this message
    joins the follow-up that feedback booked rather than booking another.
    """
    path = workspace / _PENDING_FEEDBACK_FILENAME
    entry: dict[str, Any] = {"content": message, "ts": _now()}
    if author_id is not None:
        entry["author_id"] = author_id
    if problem_added is not None:
        entry["problem_added"] = bool(problem_added)
    if attachment_ids:
        entry["attachment_ids"] = list(attachment_ids)
    with _LOCK:
        joined = False
        if author_id is not None:
            try:
                waiting = _feedback_entries(path.read_text(encoding="utf-8"))
            except OSError:
                waiting = []
            joined = any(item["author_id"] == author_id for item in waiting)
        if booking is not None and not joined:
            entry["booking"] = booking
        with path.open("a", encoding="utf-8") as stream:
            stream.write(_feedback_line(entry))
    return joined


def _requeue_human_feedback(workspace: Path, entries: list[dict[str, Any]]) -> None:
    """Put drained feedback back at the head of the queue, in its order."""
    if not entries:
        return
    path = workspace / _PENDING_FEEDBACK_FILENAME
    lines = "".join(_feedback_line(entry) for entry in entries)
    with _LOCK:
        try:
            newer = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            newer = ""
        staged = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        staged.write_text(lines + newer, encoding="utf-8")
        os.replace(staged, path)


def feedback_pending(run_id: str) -> bool:
    """Whether queued feedback already waits for the run's next continuation."""
    return _pending_feedback_bytes(RUNS_DIR / "workspaces" / run_id) > 0


def _pending_feedback_bytes(workspace: Path) -> int:
    """Size of the feedback waiting for a run's next continuation."""
    size = 0
    with _LOCK:
        for name in (_PENDING_FEEDBACK_FILENAME, "human_feedback_pending.txt"):
            try:
                size += (workspace / name).stat().st_size
            except OSError:
                continue
    return size


def _human_feedback_chars(run: dict[str, Any]) -> int:
    """Characters of human feedback a run's session problems already carry."""
    return sum(
        len(_problem_item_text(item))
        for item in run.get("problems") or []
        if isinstance(item, dict)
        and str(item.get("source") or "").strip().lower() in {"human", "hitl", "feedback"}
    )


def _drain_feedback_entries(workspace: Path) -> list[dict[str, Any]]:
    """Atomically consume queued feedback, including the legacy text file."""
    queued: list[dict[str, Any]] = []
    with _LOCK:
        jsonl_path = workspace / _PENDING_FEEDBACK_FILENAME
        legacy_path = workspace / "human_feedback_pending.txt"
        paths = [path for path in (jsonl_path, legacy_path) if path.exists()]
        for path in paths:
            try:
                raw = path.read_text(encoding="utf-8").strip()
                path.unlink()
            except OSError:
                continue
            if not raw:
                continue
            if path == legacy_path:
                queued.append({
                    "content": raw, "author_id": None, "ts": _now(),
                    "problem_added": None, "attachment_ids": [],
                })
                continue
            queued.extend(_feedback_entries(raw))
    return queued


def _drain_human_feedback(workspace: Path) -> list[str]:
    """Atomically consume the text of all queued feedback."""
    return [entry["content"] for entry in _drain_feedback_entries(workspace)]


def _launch_pending_feedback(
    run_id: str,
    *,
    owner_id: int | None,
    model: str | None,
    max_iterations: int,
    reason: str = "now that the harness finished",
) -> dict[str, Any] | None:
    """Start exactly one follow-up once the run's engine reaches a terminal state.

    Feedback continues as the account that wrote it (its keys, member policy
    and role), oldest author first, never as the run owner or as whoever
    started the finishing job: a project collaborator's message must not run
    on the administrator's credentials, nor one member's on another's keys.
    Other authors' feedback stays queued for the next follow-up.
    ``owner_id`` stands in for feedback queued before authors were recorded.
    After a Stop the queue holds only what was sent once the stop was
    recorded (it dropped the rest, see _record_stopped): that continues the
    run.  A deleted run is continued no more.
    """
    if _run_deleted(run_id) or run_is_active(run_id):
        return None
    workspace = RUNS_DIR / "workspaces" / run_id
    entries = _drain_feedback_entries(workspace)
    if not entries:
        return None
    authors = [owner_id if entry["author_id"] is None else entry["author_id"] for entry in entries]
    started: set[int] = set()
    dropped: set[int] = set()
    result: dict[str, Any] | None = None
    for author in dict.fromkeys(authors):
        batch = [index for index, item in enumerate(authors) if item == author]
        _append_chat(
            run_id,
            "system",
            f"Applying {len(batch)} queued human message{'s' if len(batch) != 1 else ''} {reason}.",
        )
        try:
            result = continue_run(
                run_id,
                message="\n\n".join(entries[index]["content"] for index in batch),
                model=model,
                max_iterations=max_iterations,
                # The author's real role: an administrator's follow-up is not
                # subject to the member policy, and a member's never escapes it.
                user=_owner_user(author),
                problems_recorded=True,
                # What queueing added to the session problems is this turn's
                # news; a message the session already had is not.
                new_problems=[
                    entries[index]["content"] for index in batch if entries[index].get("problem_added")
                ],
            )
        except account_policy.ProjectPolicyError as exc:
            # The project's rule never lifts for this run, so the feedback
            # would wait forever; the messages stay in the chat.
            _append_chat(run_id, "system", f"Queued feedback was not applied: {exc}")
            dropped.update(batch)
            continue
        except Exception as exc:  # noqa: BLE001
            # This author's feedback waits; another author's may still start.
            _append_chat(run_id, "system", f"Queued feedback could not start yet: {exc}")
            continue
        if result.get("ok"):
            started.update(batch)
        # Otherwise another continuation started first: all of it waits.
        break
    _requeue_human_feedback(
        workspace, [entry for index, entry in enumerate(entries) if index not in started | dropped]
    )
    return result


def _start_feedback_queued_behind(run_id: str, *, max_iterations: int) -> None:
    """Start feedback queued behind a continuation that then failed to start.

    Feedback that arrives while another request prepares a continuation waits
    for that continuation; when the preparation fails no harness runs, and
    nothing else would ever start the queue.
    """
    if not feedback_pending(run_id):
        return
    try:
        _launch_pending_feedback(
            run_id,
            owner_id=run_owner(run_id),
            model=None,
            max_iterations=max_iterations,
            reason="now that the other continuation did not start",
        )
    except Exception:  # noqa: BLE001 - the failed request reports its own error
        return


def _attached_files_list(engine: str, attached: list[dict[str, Any]]) -> str:
    """The list of a message's files the server appends to its text.

    A tool-free engine's prompts quote that text (a CGM turn's task inside
    <UNTRUSTED_TASK_CONTEXT>, its problem.txt inside <UNTRUSTED_PROBLEM>,
    Plain's follow-up), so the names are fenced as everywhere else such a
    prompt names a file: a file named "<UNTRUSTED_PROBLEM> notes.txt" opens
    no block there.  Another engine's text is unchanged (and an ordinary
    name is the same either way).
    """
    fenced = engine in cgm_sources.TOOL_FREE_ENGINES
    return cgm_sources.ATTACHED_FILES_LEAD + "\n".join(
        f"- {json.dumps(cgm_sources.fence_name(item['name']) if fenced else item['name'], ensure_ascii=False)}"
        for item in attached
    )


def send_human_message(
    run_id: str,
    message: str,
    *,
    model: str | None = None,
    max_iterations: int = 40,
    user: dict[str, Any] | None = None,
    attachments: object = None,
    booking: float | None = None,
) -> dict[str, Any]:
    """Human-in-the-loop chat: queue feedback or continue a finished run.

    A queued answer reports ``joined_follow_up``: whether the message joined
    feedback its author already queued, so one follow-up carries both.
    ``booking``: the run start the console booked for this message (a
    member's); queued, the message keeps it (see _queue_human_feedback).
    """
    message = (message or "").strip()
    attachment_batch = research_attachments.validate(
        attachments, guest=bool(user and user.get("guest"))
    )
    if not message and not attachment_batch:
        raise ValueError("empty message")
    ws = RUNS_DIR / "workspaces" / run_id
    if not ws.is_dir():
        raise FileNotFoundError("workspace not found")
    try:
        recorded = _load_run_record(run_id)
    except FileNotFoundError:
        recorded = {}
    recorded_engine = str(recorded.get("engine") or run_id.split("_", 1)[0])
    # Refuse before the message is recorded, so the chat shows nothing unsent.
    from agent_monitor.engines_registry import retired_engine_error

    retired_error = retired_engine_error(recorded_engine)
    if retired_error:
        raise ValueError(retired_error)
    _refuse_analyze_continuation({**recorded, "engine": recorded_engine})
    # Before anything is saved, as in start_job: the run's engine decides.
    upload_error = cgm_sources.upload_error(recorded_engine, attachment_batch)
    if upload_error:
        raise ValueError(upload_error)
    model = _member_continuation_model(user, recorded_engine, recorded, model)
    if account_policy.is_member(user):
        feedback_error = account_policy.member_feedback_error(
            message,
            run_feedback_chars=_human_feedback_chars(recorded),
            queued_bytes=_pending_feedback_bytes(ws) if run_is_active(run_id) else 0,
        )
        if feedback_error:
            raise ValueError(feedback_error)
    _tool_free_follow_up(run_id, recorded, recorded_engine, model)
    if not run_is_active(run_id):
        # This message would continue the run now, so its route must pass
        # (linked accounts, keys) before anything is recorded; the launch
        # checks again.  Feedback for an active run is checked when it starts.
        preflight_model = _continuation_model(recorded, model)
        _continuation_auth_environment(
            run_data=recorded,
            engine=recorded_engine,
            model=preflight_model,
            user=user,
            env=_user_extra_env(
                user,
                *_run_provider_models(recorded, preflight_model, recorded.get("subagent_model")),
            ),
        )

    attached = research_attachments.save(ws, attachment_batch)
    # The server's words (cgm_sources tells them from the user's).
    display_message = message or cgm_sources.FILES_ONLY_MESSAGE
    if attached:
        message = display_message + _attached_files_list(recorded_engine, attached)
    else:
        message = display_message
    user_entry = _append_chat(
        run_id, "user", display_message, attachments=attached, ledger_text=message
    )
    # Read and write under the record lock: a follow-up finishing meanwhile
    # must neither lose this message nor be overwritten by a stale copy.
    with _RUN_WRITE_LOCK:
        try:
            run_data = _load_run_record(run_id)
        except FileNotFoundError:
            run_data = {"run_id": run_id}
        problems = ensure_problems(run_data, run_id)
        effective_model = _continuation_model(run_data, model)
        problem_added = not any(_problem_item_text(p) == message for p in problems)
        if problem_added:
            problems.append({"text": message, "source": "human", "ts": _now()})
        run_data["problems"] = problems
        run_data["problem_text_preview"] = _session_title(problems, run_data.get("problem_text") or message)
        if run_data.get("run_id"):
            _write_run(run_data)

    # One decision under the job lock: feedback for a running run, or for one
    # whose continuation another request is preparing, waits for the next
    # turn; otherwise this message claims the run and continues it now.
    joined = False
    with _LOCK:
        active = _run_is_active(run_id)
        claimed = not active and _claim_launch(run_id)
        if not claimed:
            joined = _queue_human_feedback(
                ws,
                message,
                author_id=(user or {}).get("id"),
                problem_added=problem_added,
                attachment_ids=[item["id"] for item in attached],
                booking=booking,
            )
    if not claimed:
        _append_chat(
            run_id,
            "system",
            "Feedback queued; it will run automatically as soon as the current harness finishes.",
        )
        return {
            "ok": True,
            "status": "queued",
            "job_id": (active or {}).get("job_id"),
            "joined_follow_up": joined,
            "messages": list_chat(run_id),
            "user": user_entry,
        }
    try:
        return _continue_with_message(
            run_id,
            message=message,
            effective_model=effective_model,
            max_iterations=max_iterations,
            user=user,
            run_data=run_data,
            problems=problems,
            ws=ws,
            user_entry=user_entry,
            new_problems=[message] if problem_added else [],
        )
    except BaseException as exc:
        _release_launch(run_id)
        if isinstance(exc, Exception):
            _start_feedback_queued_behind(run_id, max_iterations=max_iterations)
        raise


def _continue_with_message(
    run_id: str,
    *,
    message: str,
    effective_model: str | None,
    max_iterations: int,
    user: dict[str, Any] | None,
    run_data: dict[str, Any],
    problems: list[Any],
    ws: Path,
    user_entry: dict[str, Any],
    new_problems: Sequence[str] = (),
) -> dict[str, Any]:
    """Start the chat continuation of a run claimed by ``send_human_message``."""
    # Do not expose queued feedback to the process that is still running. The
    # continuation writes the updated notebook at the start of
    # ``_execute_continue``; writing it before the active check lets the
    # current harness consume a message that is also queued for the next turn.
    _write_session_problems(ws, problems)

    _stop_event(run_id).clear()
    # A run started on Ansatz CGM: this turn's engine (easy_route).
    run_data = _easy_route_follow_up(
        run_id, run_data, ws, message=message, user=user, model=effective_model,
        has_files=bool(user_entry.get("attachments")),
    )
    engine = run_data.get("engine") or run_id.split("_", 1)[0]
    # Re-checked at launch, after send_human_message's check.
    tool_free = _tool_free_follow_up(run_id, run_data, engine, effective_model)
    first_problem = _problem_item_text(problems[0]) if problems else (
        run_data.get("problem_text") or message
    )

    continue_owner_id = (user or {}).get("id") or run_data.get("owner_id")
    continue_env, continued_route = _continuation_auth_environment(
        run_data=run_data,
        engine=engine,
        model=effective_model,
        user=user,
        env=_user_extra_env(
            user,
            *_run_provider_models(run_data, effective_model, run_data.get("subagent_model")),
        ),
    )
    job_id = uuid.uuid4().hex[:10]
    continued_subagents = bool(run_data.get("use_subagents", True)) and not tool_free
    continue_env["AGENT_MONITOR_USE_SUBAGENTS"] = "1" if continued_subagents else "0"
    continued_subagent_model = run_data.get("subagent_model") if continued_subagents else None
    if continued_subagent_model:
        continue_env["AGENT_MONITOR_SUBAGENT_MODEL"] = str(continued_subagent_model)
    else:
        continue_env.pop("AGENT_MONITOR_SUBAGENT_MODEL", None)
    thread = threading.Thread(
        target=_execute_continue,
        kwargs={
            "job_id": job_id,
            "run_id": run_id,
            "engine": engine,
            "message": message,
            "problem_text": first_problem,
            "model": effective_model,
            "max_iterations": max_iterations,
            "workspace": str(ws),
            "extra_env": continue_env,
            "owner_id": continue_owner_id,
            "new_problems": list(new_problems),
            "answer_only": _easy_answer_only(ws, run_data),
        },
        daemon=True,
        name=f"continue-{run_id}-{job_id}",
    )
    _register_claimed_job(run_id, {
        "job_id": job_id,
        "run_id": run_id,
        "engine": engine,
        "problem_id": run_data.get("problem_id"),
        "status": "running",
        "created_at": _now(),
        "updated_at": _now(),
        "error": None,
        "model": effective_model,
        "kind": "continue",
        "owner_id": continue_owner_id,
        "auth_route": continued_route,
        "reasoning_effort": run_data.get("reasoning_effort", "default"),
        "speed_mode": run_data.get("speed_mode", "standard"),
        "problem_preview": _session_title(problems, first_problem),
    }, thread)
    _start_claimed_worker(run_id, job_id, thread, "Continuing proof with your feedback…")
    return {
        "ok": True,
        "status": "continuing",
        "job_id": job_id,
        "messages": list_chat(run_id),
        "user": user_entry,
    }



def _interrupted_continuation_record(
    previous: dict[str, Any], current: dict[str, Any], *, job_id: str
) -> dict[str, Any]:
    """The record to keep when a follow-up stops or fails before finishing.

    Live heartbeats of most follow-ups (Hermes, the CLI engines) hold only the
    follow-up's own nodes and totals.  Writing that back as the final record
    would erase the run's earlier sessions (and their usage), so merge it onto
    the record as it was before the follow-up.  Node ids cannot tell whether
    that already happened (Hermes' ``{run_id}::iter-N`` restart every session);
    a live merge (IMProof) tags the follow-up's nodes with this job instead.
    """
    def agents(run: dict[str, Any]) -> list[dict[str, Any]]:
        return [agent for agent in run.get("agents") or [] if isinstance(agent, dict)]

    previous_agents, current_agents = agents(previous), agents(current)
    if not previous_agents or current_agents == previous_agents:
        # Nothing to keep, or the follow-up wrote no live record yet.
        return current
    if any(agent.get("continuation_job_id") == job_id for agent in current_agents):
        # Already merged onto the history while it ran.
        return current
    return _merge_continuation_run(previous, current, job_id=job_id)


def _record_at_session_end(run_id: str) -> dict[str, Any]:
    """The run's record as a follow-up stops or fails.

    For a run deleted while the follow-up ran, the record its worker last
    wrote (_DELETED_RUN_LAST): merged onto the earlier sessions and written,
    it books the follow-up's usage, as the record file did before the run
    was deleted (the write itself goes nowhere, see _write_run_locked).
    """
    try:
        return _load_run_record(run_id)
    except FileNotFoundError:
        with _LOCK:
            last = _DELETED_RUN_LAST.get(run_id)
        if last is None:
            raise
        return dict(last)


def _continuation_proof_text(workspace: Path) -> tuple[str, str]:
    """The run's root proof (at most 2 MB of it), read without following
    workspace links."""
    from agent_monitor import proof_graph

    for name in ("proof.md", "proof.tex"):
        text = proof_graph._read_workspace_text(
            workspace, name, limit=2 * 1024 * 1024
        )
        if text:
            return name, text
    return "proof.md", ""


def _continuation_proof_excerpt(workspace: Path) -> tuple[str, str]:
    """Return a bounded root proof excerpt without following workspace links."""
    name, text = _continuation_proof_text(workspace)
    return name, text[:_PROOF_EXCERPT_CHARS]


# A follow-up quotes at most this much of the previous turn's write-up.  A
# CGM turn opens no file, so for it a longer write-up is quoted as its
# opening, then its end from the main theorem's section (the published
# closure ends with it), with the cut marked.
_PROOF_EXCERPT_CHARS = 8_000
_CGM_PROOF_HEAD_CHARS = 2_000
_MAIN_THEOREM_RE = re.compile(r"^## Main theorem\b", re.MULTILINE)
_HEADING_RE = re.compile(r"^#{1,6} ", re.MULTILINE)


def _cgm_proof_excerpt(text: str) -> str:
    """At most ``_PROOF_EXCERPT_CHARS`` of ``text`` (and a marker): all of
    it when it fits; else its first ``_CGM_PROOF_HEAD_CHARS`` or so, ended at
    a line, the marker ``[… N characters of the previous write-up omitted
    here …]``, and its end.  When the last ``## Main theorem`` section (to
    the end) fits the remaining room, the end starts at a heading: the
    earliest one from which all the rest fits, so the lemmas just before
    the main theorem come with it when there is room.  Otherwise it is the
    last characters that fit, from a line start."""
    if len(text) <= _PROOF_EXCERPT_CHARS:
        return text
    head = text[:_CGM_PROOF_HEAD_CHARS]
    line_end = head.rfind("\n")
    if line_end >= len(head) * 3 // 4:
        head = head[: line_end + 1]
    room = _PROOF_EXCERPT_CHARS - len(head)
    mains = [match.start() for match in _MAIN_THEOREM_RE.finditer(text, len(head))]
    if mains and len(text) - mains[-1] <= room:
        start = next(
            (
                match.start()
                for match in _HEADING_RE.finditer(text, len(head), mains[-1])
                if len(text) - match.start() <= room
            ),
            mains[-1],
        )
        tail = text[start:]
    else:
        tail = text[len(text) - room:]
        line_start = tail.find("\n")
        if 0 <= line_start < len(tail) // 4:
            tail = tail[line_start + 1:]
    omitted = len(text) - len(head) - len(tail)
    return (
        f"{head.rstrip()}\n\n[… {omitted:,} characters of the previous write-up "
        f"omitted here …]\n\n{tail.lstrip()}"
    )


def _attachment_context(engine: str, ws: Path) -> str:
    """The attachment block of a run's task prompt.

    A tool-using engine gets ``research_attachments.context`` (the files'
    paths, file-tool advice, text excerpts).  The tool-free engines can open
    no file: Plain gets excerpts of the files' extracted text, and the first
    task of an Ansatz CGM run lists the files, whose text each of its turns
    receives as SOURCE MATERIAL (``_prepare_cgm_sources``).  No server path
    reaches a tool-free model's provider.
    """
    if engine in _CGM_ENGINES:
        return cgm_sources.attachment_note(ws)
    if engine == "plain":
        return cgm_sources.plain_context(ws)
    return research_attachments.context(ws)


def _plain_attachments(
    run_id: str, ws: Path, *, compose: Callable[[str], str] | None = None
) -> str:
    """Plain's attachment block for this turn (``cgm_sources.plain_attachments``),
    with the chat's 📎 line saying what its model reads.  Runs in the run's
    worker, inside its error handling: a Stop or Delete while its PDFs are
    extracted (or wait for an extraction slot) raises ``StopRequested``.

    ``compose`` (a sponsored run's) turns the block into the whole problem,
    which the sponsored Kimi client refuses above
    ``sponsored_kimi_client.MAX_PROBLEM_CHARS``: the excerpts are then cut to the room
    the problem leaves, rather than failing the run.  Extracted text is
    cached, so rendering again costs no second extraction.
    """
    from agent_monitor.analyze.latex_structure import SandboxCancelled
    from agent_monitor.sponsored_kimi_client import MAX_PROBLEM_CHARS

    budget = cgm_sources.PLAIN_TOTAL_BYTES
    try:
        while True:
            rendered = cgm_sources.plain_attachments(
                ws, cancel=_stop_event(run_id), total_bytes=budget
            )
            if compose is None or budget == 0:
                break
            over = len(compose(rendered.text)) - MAX_PROBLEM_CHARS
            if over <= 0:
                break
            # Excerpt bytes are at least their characters: cutting the budget
            # by the excess (plus a margin for the per-file notes) converges.
            budget = max(0, budget - over - 256)
    except SandboxCancelled:
        raise StopRequested(run_id) from None
    if rendered.chat_line:
        _append_chat(run_id, "system", rendered.chat_line)
    return rendered.text


def _prepare_cgm_sources(
    run_id: str,
    ws: Path,
    extra_env: dict[str, str],
    *,
    task_text: str,
    message: str,
    earlier: Sequence[str] = (),
) -> None:
    """Hand this Ansatz CGM turn the text of the run's attached files.

    Each CGM turn (a first run, a follow-up, queued feedback) is a fresh
    pipeline, so each one gets SOURCE MATERIAL rendered anew from all the
    run's files, files new this turn first, in ``cgm_sources.SOURCES_FILENAME``
    (named in ``extra_env``; a run without files gets neither).  The file
    and pages ``message`` refers to are served first, else those of the
    latest of the session's ``earlier`` messages (most recent first) that
    refers to any: a page the session is about stays in view on a follow-up
    that does not name it again.  It is sized to the room the problem and
    ``task_text`` (the turn's task before ``proof_prompt``) leave in a
    strategist or worker prompt, within the route's budget.  Runs in the
    run's worker thread, never in a request thread: PDFs are extracted by a
    sandboxed child, and a Stop meanwhile stops the run.  The chat says what
    the model will read.
    """
    from agent_monitor.analyze.latex_structure import SandboxCancelled
    from agent_monitor.engines_registry import proof_prompt
    from agent_monitor.runners.math_harness_runner import source_room

    extra_env.pop(cgm_sources.SOURCES_ENV, None)
    try:
        problem = (ws / "problem.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        problem = ""
    flags = {"AGENT_MONITOR_PUBLIC_KIMI": os.environ.get("AGENT_MONITOR_PUBLIC_KIMI", ""), **extra_env}
    limit = min(
        cgm_sources.budget(flags),
        source_room(problem, proof_prompt(task_text, response_only=True)),
    )
    try:
        turn = cgm_sources.prepare_turn(
            ws, message=message, budget=limit, cancel=_stop_event(run_id), earlier=earlier
        )
    except SandboxCancelled:
        raise StopRequested(run_id) from None
    except OSError as exc:
        raise RuntimeError(
            f"The attached files could not be prepared for the model ({type(exc).__name__})"
        ) from exc
    if turn.filename:
        extra_env[cgm_sources.SOURCES_ENV] = turn.filename
    if turn.chat_line:
        _append_chat(run_id, "system", turn.chat_line)


# How a bare Continue's synthesized feedback starts (_continue_claimed_run):
# it is no message of the user's, so it is never a session problem.
_BARE_CONTINUE_PREFIX = "Continue from where the previous session stopped"
# That feedback in full: for a CGM turn (which opens no file, so it builds
# on the write-up its task carries), and for the other engines.
_BARE_CONTINUE_CGM = (
    f"{_BARE_CONTINUE_PREFIX}: build on the previous turn's write-up until every "
    "session problem has a complete, correct proof."
)
_BARE_CONTINUE_OTHER = (
    f"{_BARE_CONTINUE_PREFIX}. Review the workspace, then keep improving proof.md "
    "until every session problem has a complete, correct proof."
)


def _earlier_messages(problems: list[Any], message: str) -> list[str]:
    """The session's messages before this turn's ``message``, most recent
    first: where a follow-up finds the file and pages the session is about
    when its own message names none."""
    current = (message or "").strip()
    texts = [_problem_item_text(item) for item in problems]
    return [text for text in reversed(texts) if text and text != current]


def _cgm_follow_up_task(
    *,
    message: str,
    problems_block: str,
    proof_name: str,
    proof_excerpt: str,
    proof_chars: int | None = None,
) -> str:
    """The task of an Ansatz CGM follow-up turn.

    The turn is a fresh tool-free pass that returns a new write-up: it edits
    no file and keeps nothing of earlier turns but what this task carries.
    The feedback comes first, since the runner keeps the head and tail of a
    long task (8,000 bytes for a repair call); then the session problems and
    the previous turn's write-up to build on (``proof_excerpt``, from
    :func:`_cgm_proof_excerpt`; ``proof_chars`` is the length of the whole
    write-up when that is an excerpt of it, and the header says so).  The
    files' text comes separately as SOURCE MATERIAL.
    """
    task = (
        "This is a follow-up turn of a mathematics session; answer it with a "
        "new, complete write-up.\n"
        f"HUMAN FEEDBACK — address this now:\n{message}\n\n"
        f"SESSION PROBLEMS:\n{problems_block}\n"
    )
    if proof_excerpt:
        extent = (
            f"an excerpt of its {proof_chars:,} characters: its opening and its end, "
            "the middle omitted where marked"
            if proof_chars and proof_chars > _PROOF_EXCERPT_CHARS
            else "at most 8,000 characters"
        )
        task += (
            f"\nTHE PREVIOUS TURN'S WRITE-UP ({proof_name}, {extent}): "
            "build on it — reuse what is correct and address the feedback.\n"
            f"{proof_excerpt}\n"
        )
    return task


def _execute_continue(
    *,
    job_id: str,
    run_id: str,
    engine: str,
    message: str,
    problem_text: str,
    model: str | None,
    max_iterations: int,
    workspace: str,
    extra_env: dict[str, str] | None = None,
    owner_id: int | None = None,
    problems_recorded: bool = False,
    new_problems: Sequence[str] | None = None,
    answer_only: bool = False,
) -> None:
    """Run one follow-up turn of ``run_id`` with ``message`` as its feedback.

    ``answer_only`` (a Plain turn of easy_route on a run whose proof Ansatz
    CGM wrote): the turn answers the message and leaves proof.md unchanged;
    its answer is the turn's own (proof_turns).

    ``new_problems`` are the texts this turn's message added to the session
    problems (``send_human_message`` and ``_launch_pending_feedback`` record
    them before the turn starts; a message the session already had adds
    none).  When ``None``, the message counts as new if this turn records it,
    or if it is a queued follow-up's (``problems_recorded``).  Only those are
    tagged "← new this turn": a bare Continue, or a message the session had
    already, tags nothing.
    """
    started = time.time()
    ws = Path(workspace)
    extra_env = dict(extra_env or {})
    _apply_kimi_compatible_env(extra_env, model)
    try:
        run_data = _load_run_record(run_id)
    except FileNotFoundError:
        run_data = {"run_id": run_id, "problem_id": "continue"}
    previous_run_data = dict(run_data)
    problems = ensure_problems(run_data, run_id)
    bare = message.strip().startswith(_BARE_CONTINUE_PREFIX)
    recorded_here = False
    if (
        message
        and not problems_recorded
        and not any(_problem_item_text(p) == message.strip() for p in problems)
    ):
        if not bare:
            problems.append({"text": message.strip(), "source": "human", "ts": _now()})
            run_data["problems"] = problems
            recorded_here = True
    _write_session_problems(ws, problems)
    if new_problems is None:
        new_problems = [message] if recorded_here or (problems_recorded and not bare) else []

    proof_name, proof_text = _continuation_proof_text(ws)
    proof_excerpt = proof_text[:_PROOF_EXCERPT_CHARS]

    # The last occurrence of each text this turn added (a text is recorded
    # once; an older record may hold it twice).
    fresh = {text.strip() for text in new_problems if text and text.strip()}
    new_positions = {
        _problem_item_text(item): i for i, item in enumerate(problems, 1)
        if _problem_item_text(item) in fresh
    }
    numbered = []
    for i, item in enumerate(problems, 1):
        text = _problem_item_text(item)
        if not text:
            continue
        tag = "  ← new this turn" if new_positions.get(text) == i else ""
        numbered.append(f"{i}. {text}{tag}")
    problems_block = "\n".join(numbered) or problem_text

    response_only = engine in {"kimi", "plain"}
    workspace_instruction = (
        "Return the complete updated Markdown write-up; the application saves it automatically.\n"
        if response_only else f"WORKSPACE: {ws}\n"
    )
    revision_instruction = (
        "If the human asked to revise an existing proof, include its revised version "
        "in the returned write-up and leave the other proofs intact.\n\n"
        if response_only else
        "If the human asked to revise an existing proof, edit that proof in place "
        "and leave the others intact.\n\n"
    )
    continuation = (
        "You are continuing an informal mathematics session based on human feedback.\n"
        f"{workspace_instruction}"
        f"{proof_name} is a notebook for this WHOLE session. "
        "Do NOT delete or replace earlier proofs. "
        "If the human asked a new theorem, keep the existing write-up intact and "
        "continue with another proof that starts the same way as the first "
        "(a `# Claim` heading, then Strategy / Setup / Lemmas / Main proof). "
        "Do NOT add 'Problem N' or 'Proof N' headings — the console already shows "
        "each user request as its own problem card. "
        f"{revision_instruction}"
        f"SESSION PROBLEMS:\n{problems_block}\n\n"
    )
    if proof_excerpt:
        continuation += f"CURRENT {proof_name} (preserve this content, then extend it):\n{proof_excerpt}\n\n"
    continuation += f"HUMAN FEEDBACK — address this now:\n{message}\n"
    answer_only = bool(answer_only) and engine == "plain"
    if answer_only:
        continuation = (
            "You are answering a question in an informal mathematics session about a proof "
            "that has already been written and checked. Answer the HUMAN's question directly "
            "and completely, in Markdown with $...$ math. Do NOT rewrite, repeat or replace "
            "the proof: it stays as it is. Your answer is shown to the human as your reply.\n\n"
            f"SESSION PROBLEMS:\n{problems_block}\n\n"
            + (f"CURRENT {proof_name} (for reference only; do not reproduce it):\n{proof_excerpt}\n\n" if proof_excerpt else "")
            + f"HUMAN FEEDBACK — address this now:\n{message}\n"
        )
        extra_env["AGENT_MONITOR_PLAIN_ANSWER_ONLY"] = "1"
    if engine in _CGM_ENGINES:
        # Each CGM turn is a fresh tool-free pipeline: its own task, and the
        # files' text as SOURCE MATERIAL (_prepare_cgm_sources, below).
        continuation = _cgm_follow_up_task(
            message=message,
            problems_block=problems_block,
            proof_name=proof_name,
            proof_excerpt=_cgm_proof_excerpt(proof_text),
            proof_chars=len(proof_text),
        )
    elif engine != "plain":
        # Plain's excerpts are added once the record says running (below).
        attachment_context = _attachment_context(engine, ws)
        if attachment_context:
            continuation += "\n\n" + attachment_context

    # Only a proof run's follow-ups are turns (an analysis or a readable
    # version has no proof to keep).
    turn = (
        _proof_turn_begin(run_id, ws, answer_only=answer_only)
        if engine not in {_ANALYZE_ENGINE, _READABILITY_ENGINE}
        and str(run_data.get("run_kind") or "") not in {"analyze", "readability"}
        else None
    )
    run_data["status"] = "running"
    run_data["updated_at"] = _now()
    with _RUN_WRITE_LOCK:
        # Feedback recorded since this follow-up read the record is for the
        # next turn; keep it.
        run_data["problems"] = _problems_with_recorded(
            problems, run_id, own=message.strip() if recorded_here else None
        )
        _write_run(run_data)

    try:
        if _run_deleted(run_id):
            # Deleted while this follow-up was being prepared: no runner
            # starts (copying the profile's skills for it would bring the
            # workspace back).
            raise StopRequested(run_id)
        if engine == "plain":
            # Its PDFs may need extracting: done now that the record says
            # running (a deploy's drain sees the work), where a Stop or
            # Delete meanwhile stops the follow-up before its runner starts.
            attachment_context = _plain_attachments(run_id, ws)
            if attachment_context:
                continuation += "\n\n" + attachment_context
            if _run_deleted(run_id) or _stop_event(run_id).is_set():
                raise StopRequested(run_id)
        if engine == "improof":
            result_run = _run_improof_continuation(
                run_id=run_id,
                job_id=job_id,
                problem_id=run_data.get("problem_id") or "continue",
                problem_text=continuation,
                display_problem_text=problem_text,
                model=model,
                started=started,
                workspace=ws,
                extra_env=extra_env,
                baseline_run=previous_run_data,
            )
        elif engine in {"hermes", "ucla"}:
            # UCLA is not resumable mid-flight; its legacy refinement path
            # remains unchanged. IMProof continuations stay on IMProof above.
            result_run = _run_hermes(
                run_id=run_id,
                problem_id=run_data.get("problem_id") or "continue",
                problem_text=continuation,
                display_problem_text=problem_text,
                model=model,
                max_iterations=max_iterations,
                started=started,
                workspace=ws,
                extra_env=extra_env,
                use_subagents=bool(run_data.get("use_subagents", True)),
                subagent_model=run_data.get("subagent_model"),
                owner_id=owner_id,
            )
            if engine != "hermes":
                result_run["engine"] = engine
                result_run["source"] = engine
                result_run["trace_name"] = run_data.get("trace_name") or result_run.get("trace_name")
        else:
            if engine in _CGM_ENGINES:
                _prepare_cgm_sources(
                    run_id,
                    ws,
                    extra_env,
                    task_text=continuation,
                    # A bare Continue's feedback is the server's: its "problem"
                    # and "proof" name no file (problem.pdf, proof.tex), so the
                    # session's earlier messages decide what comes first.
                    message=(
                        "" if message.strip() in (_BARE_CONTINUE_CGM, _BARE_CONTINUE_OTHER)
                        else message
                    ),
                    earlier=_earlier_messages(problems, message),
                )
            result_run = _run_cli_engine_with_openclaude_retry(
                run_id=run_id,
                engine=engine,
                problem_id=run_data.get("problem_id") or "continue",
                problem_text=continuation,
                started=started,
                workspace=ws,
                display_problem_text=problem_text,
                extra_env=extra_env,
                requested_model=model or (run_data.get("live") or {}).get("model"),
                max_iterations=max_iterations,
                owner_id=owner_id,
                include_persona=engine not in _CGM_ENGINES,
            )
        published_before_stop = _published_before_stop(result_run)
        if result_run.get("status") == "stopped" or (
            _stop_event(run_id).is_set() and not published_before_stop
        ):
            # A Stop wins over what the stopped engine returned (IMProof
            # reports its killed workflow as failed), as on a first run.
            raise StopRequested(run_id)
        result_run = _merge_continuation_run(
            previous_run_data, result_run, job_id=job_id
        )
        result_run["job_id"] = job_id
        result_run["workspace"] = str(ws)
        result_run["use_subagents"] = bool(run_data.get("use_subagents", True))
        result_run["subagent_model"] = (
            run_data.get("subagent_model") if result_run["use_subagents"] else None
        )
        result_run["updated_at"] = _now()
        with _RUN_WRITE_LOCK:
            # Feedback sent while this follow-up ran is already in the
            # record; keep it, after this turn's own problems.
            result_run["problems"] = _problems_with_recorded(
                problems, run_id, own=message.strip() if recorded_here else None
            )
            _write_run(result_run)
        status = "finished" if result_run.get("status") != "failed" else "failed"
        _update_job(job_id, status=status, error=result_run.get("error"))
        _append_chat(
            run_id,
            "assistant",
            (result_run.get("agents") or [{}])[-1].get("output", "")[:2000] or f"Run {status}.",
        )
        # An answer-only turn left the proof as it was: nothing to derive again.
        if _root_proof_artifact(ws) and not answer_only:
            try:
                from agent_monitor import auto_pipeline

                auto_pipeline.start(
                    run_id=run_id,
                    workspace=ws,
                    owner_id=owner_id,
                    model=cgm_max.formal_model(_load_run_record(run_id), model),
                    engine=engine,
                )
            except Exception as exc:  # noqa: BLE001 - derived views are best-effort
                _append_chat(
                    run_id,
                    "system",
                    f"Automatic Lean/DAG refresh could not start: {exc}",
                )
    except StopRequested:
        try:
            with _RUN_WRITE_LOCK:  # no stale copy over a message recorded meanwhile
                stopped_run = _interrupted_continuation_record(
                    previous_run_data, _record_at_session_end(run_id), job_id=job_id
                )
                stopped_run["status"] = "stopped"
                stopped_run["updated_at"] = _now()
                _write_run(stopped_run)
        except FileNotFoundError:
            # The run was deleted, which stops its follow-up too.
            pass
        # No chat line here: stop_run has posted it, and a deleted run must not
        # get its workspace back.
        _update_job(job_id, status="stopped")
    except Exception as exc:  # noqa: BLE001
        try:
            with _RUN_WRITE_LOCK:  # no stale copy over a message recorded meanwhile
                failed_run = _interrupted_continuation_record(
                    previous_run_data, _record_at_session_end(run_id), job_id=job_id
                )
                failed_run["status"] = "failed"
                failed_run["error"] = str(exc)
                failed_run["updated_at"] = _now()
                _write_run(failed_run)
        except FileNotFoundError:
            pass
        _update_job(job_id, status="failed", error=str(exc))
        _append_chat(run_id, "assistant", f"I could not apply that feedback: {exc}")
    finally:
        # The turn ends before the feedback queued for the next one starts.
        _proof_turn_end(run_id, ws, turn)
        # This worker is done with the run (runner, record, chat, prompt
        # file): what it starts now, or a Continue, never runs beside it.
        _end_worker(job_id)
        _launch_pending_feedback(
            run_id, owner_id=owner_id, model=model, max_iterations=max_iterations
        )


def _run_improof_continuation(
    *,
    run_id: str,
    job_id: str,
    problem_id: str,
    problem_text: str,
    display_problem_text: str,
    model: str | None,
    started: float,
    workspace: Path,
    extra_env: dict[str, str] | None,
    baseline_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # A continuation is a fresh native Author/Critic workflow over the saved
    # notebook, rather than a Hermes run relabelled as IMProof.
    from agent_monitor.runners import improof as improof_runner

    prompt_path = workspace / f".improof-continue-{job_id}.txt"
    prompt_path.write_text(problem_text, encoding="utf-8")
    improof_args = None
    if model and (extra_env or {}).get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        improof_args = [
            "--component", f"cfg_codex_author.model={model}",
            "--component", f"cfg_codex_critic.model={model}",
        ]
    elif model and (extra_env or {}).get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
        improof_args = [
            "--component", f"cfg_claude_author.model={model}",
            "--component", f"cfg_claude_critic.model={model}",
        ]
        # See _execute_job_body: the Claude worker reads its model from here.
        extra_env = {**(extra_env or {}), "AGENT_MONITOR_CLAUDE_MODEL": model}
    try:
        result = improof_runner.run_problem(
            prompt_path,
            problem_id=problem_id,
            output_dir=workspace,
            extra_args=improof_args,
            extra_env=extra_env,
            research_model=model,
            on_start=lambda process: _register_proc(run_id, process),
            on_output=_live_output_flusher(
                run_id=run_id,
                engine="improof",
                problem_id=problem_id,
                problem_text=display_problem_text,
                started=started,
                workspace=workspace,
                baseline_run=baseline_run,
                continuation_job_id=job_id,
            ),
        )
    finally:
        _unregister_proc(run_id)
        prompt_path.unlink(missing_ok=True)
    return _wrap_subprocess_result(
        run_id=run_id,
        engine="improof",
        problem_id=problem_id,
        problem_text=display_problem_text,
        result=result,
        started=started,
        workspace=workspace,
    )


def _refuse_analyze_continuation(record: dict[str, Any] | None) -> None:
    """Manuscript runs (Analyze, Readability) are not conversations: no
    continuation, no chat turn."""
    from agent_monitor import analyze_runs, readability_runs

    if readability_runs.is_readability_record(record):
        raise ValueError(readability_runs.CONTINUE_REFUSAL)
    if analyze_runs.is_analyze_record(record):
        raise ValueError(analyze_runs.CONTINUE_REFUSAL)


# One node of a finished analysis is re-checked by the runner itself
# (``analyze_runner.py --recheck <node_id>``, 8 calls and 900 s, more for a
# proof checked in more parts; see ``analyze_runner.recheck_caps``) with the
# run's own route environment; the outer watchdog adds one call and
# publication.
_RECHECK_GRACE_SECONDS = 180


def _analyze_recheck_env(
    extra_env: dict[str, str], *, model: str | None, engine: str = _ANALYZE_ENGINE
) -> dict[str, str]:
    """The child environment of a re-check (or of a question about a
    readability run, ``engine``): the run's route; tool-free runner."""
    from agent_monitor.engines_registry import engine_extra_path
    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env(extra=extra_env)
    _apply_analyze_runner_env(env, extra_env)
    _apply_selected_model_env(env, engine=engine, model=model)
    if env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1" or env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
        _scrub_provider_route_env(env)
    env.setdefault("NO_COLOR", "1")
    extra = engine_extra_path(engine)
    if extra:
        env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    return env


def start_analysis_recheck(
    run_id: str,
    node_id: str,
    *,
    user: dict[str, Any] | None,
    on_exit: Any = None,
) -> dict[str, Any]:
    """Re-check one node of a finished analysis in the background.

    Returns ``{"ok": True, "status": "rechecking", "run_id", "node_id",
    "job_id"}`` at once; the outcome lands in ``run["analyze"]["recheck"]``
    (and the refreshed summary in ``run["analyze"]``).  ``on_exit`` runs once
    when the re-check is over or did not start (a guest's derived-action
    slot).  Raises ``analyze_runs.AnalyzeRequestError`` (status 400, 403, 409
    or 429), ``FileNotFoundError`` or ``ValueError``.
    """
    from agent_monitor import analyze_runs
    from agent_monitor.analyze import graph as analysis_graph
    from agent_monitor.analyze import recheck as analyze_recheck

    handed_off = False
    gate_key: str | None = None
    claimed = False
    state_written: tuple[str, str, str] | None = None
    # The job this request registered: a refusal fails that one only, never
    # the re-check already running.
    registered: str | None = None
    try:
        record = _load_run_record(run_id)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        if callable(on_exit):
            on_exit()
        raise FileNotFoundError("run not found") from None
    try:
        if not analyze_runs.is_analyze_record(record):
            raise analyze_runs.AnalyzeRequestError("Only Analyze runs have nodes to re-check")
        refusal = analyze_runs.availability_error(user)
        if refusal is not None:
            raise refusal
        if not isinstance(node_id, str) or not analysis_graph.NODE_ID_RE.fullmatch(node_id):
            raise analyze_runs.AnalyzeRequestError("node_id must be a node of this analysis")
        ws = workspace_dir(run_id)
        graph = analysis_graph.load(ws)
        if graph is None:
            raise analyze_runs.AnalyzeRequestError(
                "This analysis has no published graph to re-check yet", status=409
            )
        node = next((item for item in graph["nodes"] if item["id"] == node_id), None)
        if node is None:
            raise analyze_runs.AnalyzeRequestError("node_id must be a node of this analysis")
        if not (node.get("meta") or {}).get("checkable"):
            raise analyze_runs.AnalyzeRequestError(
                "That result cannot be re-checked: it is not a result whose proof the parser located"
            )
        if str(record.get("status") or "") == "running" or run_is_active(run_id):
            raise analyze_runs.AnalyzeRequestError(
                RECHECK_BUSY, status=409
            )
        if not _claim_launch(run_id):
            raise analyze_runs.AnalyzeRequestError(
                "A re-check of this analysis is already starting", status=409
            )
        claimed = True
        # A guest re-checks a bounded number of results per analysis (counted
        # on the record, under the launch claim, so it holds across restarts).
        requester_guest = bool(user and user.get("guest"))
        if requester_guest:
            try:
                current = _load_run_record(run_id)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                current = record
            if recheck_count(current) >= analyze_runs.GUEST_RECHECKS_PER_ANALYSIS:
                raise analyze_runs.AnalyzeRequestError(
                    analyze_runs.GUEST_RECHECK_LIMIT_MESSAGE.format(
                        limit=analyze_runs.GUEST_RECHECKS_PER_ANALYSIS
                    ),
                    status=403,
                )
        # The model the analysis selected (an account route's selector, not
        # the provider's resolved name), exactly as a continuation would.
        model = _continuation_model(record, None)
        # Never another Kimi model than Kimi K3 (an analysis started on one
        # before Analyze refused it ran as Kimi K3 under its name); nothing is
        # reserved or counted.  Kimi K3 in another case is Kimi K3.
        refusal_text = analyze_runs.kimi_model_error(analyze_runs.RUN_KIND, model, ran=True)
        if refusal_text:
            raise analyze_runs.AnalyzeRequestError(refusal_text)
        model = analyze_runs.manuscript_model(model) or model
        sponsored = str(record.get("credential_source") or "") == "sponsored_kimi"
        guest = bool(record.get("guest"))
        too_long = analyze_recheck.guest_refusal(ws, node_id) if guest else ""
        if too_long:
            # As in the analysis; nothing is reserved or counted (the runner refuses it too).
            raise analyze_runs.AnalyzeRequestError(too_long)
        if not sponsored and account_policy.member_analyze_sponsored_only(user):
            raise analyze_runs.AnalyzeRequestError(
                "This analysis ran on an API key, which members can't use on this site; "
                "start a new analysis on the included Kimi K3",
                status=403,
            )
        if sponsored:
            # The guests' share of the sponsored pool follows who asks.  The
            # key is released on failure only once this request holds it (a
            # refusal for a held slot must not free the holder's).
            analyze_runs.GATE.reserve(analyze_runs.recheck_key(run_id), run_id=run_id, guest=requester_guest)
            gate_key = analyze_runs.recheck_key(run_id)
        configured, _route = _continuation_auth_environment(
            run_data=record,
            engine=_ANALYZE_ENGINE,
            model=model,
            user=user,
            env=_user_extra_env(user, model),
        )
        _apply_bounded_run_env(configured, engine=_ANALYZE_ENGINE, guest=guest, sponsored=sponsored)
        if not sponsored and _is_kimi_model(model):
            configured["AGENT_MONITOR_KIMI_BASE_URL"] = _operator_kimi_base()
        env = _analyze_recheck_env(configured, model=model)
        # The runner's own time limit for this proof (sponsored and guest runs
        # check it in the bounded prompt budget's smaller parts).
        caps = analyze_recheck.node_caps(ws, node_id, bounded=sponsored or guest)
        timeout = caps["max_seconds"] + _analyze_api_timeout(sponsored) + _RECHECK_GRACE_SECONDS
        job_id = uuid.uuid4().hex[:10]
        label = str(node.get("label") or node_id)[:120]
        started_at = _now()
        _register_claimed_job(run_id, {
            "job_id": job_id,
            "run_id": run_id,
            "engine": _ANALYZE_ENGINE,
            "run_kind": "analyze",
            "kind": "recheck",
            "node_id": node_id,
            "status": "running",
            "created_at": started_at,
            "updated_at": started_at,
            "error": None,
            "model": model,
            "owner_id": record.get("owner_id"),
            "guest": guest,
            "auth_route": record.get("auth_route"),
            "credential_source": record.get("credential_source"),
            "problem_preview": f"Re-check · {label}",
        })
        registered = job_id
        claimed = False
        _stop_event(run_id).clear()
        _set_recheck_state(run_id, {"node_id": node_id, "label": label, "status": "running",
                                    "job_id": job_id, "started_at": started_at}, started=True)
        state_written = (job_id, label, started_at)
        _append_chat(run_id, "system", f"Re-check · {label} · started (model verifier, one result)")
        thread = threading.Thread(
            target=_execute_recheck,
            kwargs={
                "run_id": run_id, "job_id": job_id, "node_id": node_id, "label": label,
                "workspace": ws, "env": env, "timeout": timeout, "gate_key": gate_key,
                "on_exit": on_exit, "started_at": started_at, "model": model,
            },
            daemon=True,
            name=f"analyze-recheck-{job_id}",
        )
        thread.start()
        handed_off = True
        return {"ok": True, "status": "rechecking", "run_id": run_id, "node_id": node_id, "job_id": job_id}
    finally:
        if not handed_off:
            if claimed:
                _release_launch(run_id)
            if gate_key is not None:
                analyze_runs.GATE.release(gate_key, refund=True)
            with _LOCK:
                job = _JOBS.get(registered or "")
                if job is not None and job.get("status") == "running":
                    job["status"] = "failed"
            if state_written is not None:
                job_id, label, started_at = state_written
                _set_recheck_state(run_id, {
                    "node_id": node_id, "label": label, "status": "failed", "job_id": job_id,
                    "started_at": started_at, "finished_at": _now(),
                    "error": "The re-check could not start",
                })
            if callable(on_exit):
                on_exit()


# A re-check waits for the analysis, its re-check or a question about it.
RECHECK_BUSY = "Wait for the analysis (or its current re-check or question) to finish"
# Serializes every write of ``run["analyze"]["recheck"]`` (a re-check's own
# updates and the repair of a re-check that a restart interrupted), and of the
# questions about a run (``_ASK_STATE_LOCK``): on an analysis both rewrite
# ``run["analyze"]``.
_RECHECK_STATE_LOCK = threading.RLock()


def recheck_count(record: dict[str, Any] | None) -> int:
    """How many re-checks of this analysis were started (``rechecks.started``)."""
    block = (record or {}).get("analyze")
    tally = block.get("rechecks") if isinstance(block, dict) else None
    try:
        return max(0, int((tally or {}).get("started") or 0)) if isinstance(tally, dict) else 0
    except (TypeError, ValueError):
        return 0


RECHECK_INTERRUPTED = (
    "The re-check was interrupted (the server restarted); the previous verdict is kept"
)


def _recheck_worker_alive(run_id: str, job_id: object) -> bool:
    """Whether this process runs (or is starting) work for ``run_id``."""
    with _LOCK:
        if run_id in _LAUNCHING_RUNS:
            return True
        job = _JOBS.get(str(job_id or ""))
        if job is not None and job.get("run_id") == run_id and job.get("status") == "running":
            return True
        return any(
            item.get("run_id") == run_id and item.get("status") == "running" for item in _JOBS.values()
        )


def reconcile_recheck_state(run_id: str, run: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Repair a re-check that a lost worker left ``running``.

    The re-check job lives in this process only: after a restart (or a crash
    of its thread) the record would say "running" forever, with every node's
    Re-check button disabled.  The state becomes ``interrupted`` (the runner
    restores the node it was checking, or never got to change it, so the
    previous verdict stands).  Returns the current record.
    """
    if run is None:
        try:
            run = _load_run_record(run_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
    block = run.get("analyze") if isinstance(run.get("analyze"), dict) else {}
    recheck = block.get("recheck") if isinstance(block.get("recheck"), dict) else None
    if not recheck or recheck.get("status") != "running" or _recheck_worker_alive(run_id, recheck.get("job_id")):
        return run
    with _RECHECK_STATE_LOCK:
        try:
            current = _load_run_record(run_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return run
        current_block = dict(current.get("analyze") or {})
        state = current_block.get("recheck") if isinstance(current_block.get("recheck"), dict) else {}
        if (
            state.get("status") != "running"
            or state.get("job_id") != recheck.get("job_id")
            or _recheck_worker_alive(run_id, state.get("job_id"))
        ):
            return current
        current_block["recheck"] = {**state, "status": "interrupted", "finished_at": _now(),
                                    "error": RECHECK_INTERRUPTED}
        current["analyze"] = current_block
        current["updated_at"] = _now()
        _write_run(current)
    from agent_monitor import analyze_runs

    # A slot held by a lost thread of this process is returned at once.
    analyze_runs.GATE.release(analyze_runs.recheck_key(run_id))
    label = str(state.get("label") or state.get("node_id") or "")
    _append_chat(run_id, "system", f"Re-check · {label} · interrupted: {RECHECK_INTERRUPTED}")
    return current


def _set_recheck_state(
    run_id: str,
    state: dict[str, Any],
    *,
    refresh: bool = False,
    started: bool = False,
    usage: dict[str, Any] | None = None,
) -> None:
    """Record the latest re-check in ``run["analyze"]["recheck"]``.

    ``run["analyze"]["rechecks"]`` keeps the running tally across refreshes:
    re-checks ``started`` (``started=True`` counts one) and the model
    ``calls`` and tokens they spent (``usage``).
    """
    with _RECHECK_STATE_LOCK:
        try:
            run = _load_run_record(run_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        block = dict(run.get("analyze") or {})
        tally = dict(block.get("rechecks") or {}) if isinstance(block.get("rechecks"), dict) else {}
        if refresh:
            from agent_monitor.analyze import record as analyze_record

            try:
                fresh = analyze_record.run_block(workspace_dir(run_id))
            except Exception:  # noqa: BLE001 - keep the stored block
                fresh = None
            if fresh:
                # The questions about the analysis live only in the record.
                for key in ASK_KEYS:
                    if block.get(key) and key not in fresh:
                        fresh[key] = block[key]
                block = fresh
        if started:
            tally["started"] = recheck_count({"analyze": {"rechecks": tally}}) + 1
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
                tally[key] = round(float(tally.get(key) or 0) + float(value), 6)
                if key != "cost_usd":
                    tally[key] = int(tally[key])
        if tally:
            block["rechecks"] = tally
        block["recheck"] = state
        run["analyze"] = block
        run["updated_at"] = _now()
        _write_run(run)


def _recheck_outcome(output: str) -> tuple[dict[str, Any] | None, str]:
    """The runner's ``analyze.recheck`` result, or its terminal error message."""
    result: dict[str, Any] | None = None
    error = ""
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "analyze.recheck" and isinstance(event.get("result"), dict):
            result = event["result"]
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if item and item.get("type") == "error" and item.get("terminal") is True:
            error = str(item.get("message") or "")[:600]
    return result, error


def _book_recheck_usage(run_id: str, output: str, *, model: str | None, stage: str = "recheck") -> dict[str, Any]:
    """Book a re-check's model calls in the usage ledger (stage ``recheck``,
    billed like the analysis; a question about a readability run is stage
    ``ask``) and return their tally.

    The runner reports each call as a Codex-style ``turn.completed`` event,
    exactly as for the analysis itself.  The analysis's own ``totals`` stay
    its engine usage: like Lean and DAG calls, a re-check is booked apart
    (adding it to ``totals`` would book it a second time, as engine usage).
    """
    from agent_monitor import usage_ledger

    tally: dict[str, Any] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for line in str(output or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        raw = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        usage = usage_ledger.cli_usage(raw, input_includes_cache=raw.get("input_excludes_cache") is not True)
        named = event.get("model")
        call_model = named.strip() if isinstance(named, str) and named.strip() else _exact_price_model(model)
        cost = None
        try:
            # Priced as usage_ledger.record_usage does: the input includes the
            # cache reads and writes (Claude's writes cost more than input).
            cached = int(usage.get("cached_input_tokens") or 0)
            written = int(usage.get("cache_write_tokens") or 0)
            cost = _estimate_cost(
                call_model,
                input_tokens=max(0, int(usage.get("input_tokens") or 0) - cached - written),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read=cached,
                cache_write=written,
            )
        except Exception:  # noqa: BLE001 - a price estimate is best-effort
            cost = None
        usage_ledger.record_usage(model=call_model or model, usage=usage, cost_usd=cost or 0.0, stage=stage, run_id=run_id)
        tally["calls"] += 1
        tally["input_tokens"] += int(usage.get("input_tokens") or 0)
        tally["output_tokens"] += int(usage.get("output_tokens") or 0)
        tally["cost_usd"] = round(float(tally["cost_usd"]) + float(cost or 0.0), 6)
    return tally


def _watch_runner(
    run_id: str, argv: list[str], *, workspace: Path, env: dict[str, str], timeout: int, timed_out: str
) -> tuple[str, int | None, str]:
    """Run a runner process for ``run_id`` (a re-check, a question) to its
    end.  Stop and the time limit send SIGTERM first (the runner settles and
    exits 143; ``timed_out`` is then the error), and the group is killed 30 s
    later.  Returns the output, the exit code (``None``: it could not start,
    the error names why) and the error."""
    import subprocess

    try:
        proc = subprocess.Popen(
            argv, cwd=str(workspace), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return "", None, type(exc).__name__
    _register_proc(run_id, proc)
    deadline = time.monotonic() + timeout
    escalate_at: float | None = None
    output = ""
    error = ""
    try:
        while True:
            try:
                output, _ = proc.communicate(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if escalate_at is None:
                    if _stop_event(run_id).is_set():
                        # Stop sent SIGTERM: the runner settles and exits
                        # 143; the group is killed if it hangs.
                        escalate_at = now + 30
                    elif now > deadline:
                        error = timed_out
                        _terminate_registered_proc(proc, force=False)
                        escalate_at = now + 30
                elif now > escalate_at:
                    _terminate_registered_proc(proc, force=True)
                    escalate_at = now + 3600
    finally:
        _unregister_proc(run_id, proc)
    return output or "", proc.returncode, error


def _execute_recheck(
    *,
    run_id: str,
    job_id: str,
    node_id: str,
    label: str,
    workspace: Path,
    env: dict[str, str],
    timeout: int,
    gate_key: str | None,
    on_exit: Any,
    started_at: str,
    model: str | None = None,
) -> None:
    status = "failed"
    result: dict[str, Any] | None = None
    error = ""
    spent: dict[str, Any] = {}
    # A re-check that never reached a model gives its sponsored start back.
    unsent = False
    try:
        runner = Path(__file__).resolve().parent / "runners" / "analyze_runner.py"
        argv = [sys.executable, "-u", str(runner), "--recheck", node_id]
        output, returncode, error = _watch_runner(
            run_id, argv, workspace=workspace, env=env, timeout=timeout,
            timed_out=f"The re-check timed out after {timeout}s; the previous verdict is kept",
        )
        if returncode is None:
            error = f"The re-check could not start ({error})"
            unsent = True
        else:
            try:
                # Every call the re-check made is spent, whatever its outcome.
                spent = _book_recheck_usage(run_id, output, model=model)
            except Exception:  # noqa: BLE001 - accounting must not lose the outcome
                spent = {}
            result, terminal = _recheck_outcome(output)
            # Exit 2 is a configuration refusal: nothing was sent to a model.
            unsent = returncode == 2
            if returncode == 0 and result is not None:
                status = "done"
            elif returncode == 128 + int(signal.SIGTERM) or _stop_event(run_id).is_set():
                status = "stopped"
                error = error or "The re-check was stopped; nothing changed"
            else:
                error = error or terminal or f"The re-check exited with code {returncode}"
    except Exception as exc:  # noqa: BLE001 - the record must say what happened
        error = f"The re-check failed ({type(exc).__name__})"
    finally:
        state: dict[str, Any] = {
            "node_id": node_id, "label": label, "status": status, "job_id": job_id,
            "started_at": started_at, "finished_at": _now(),
        }
        if result is not None:
            state["result"] = {
                key: result.get(key)
                for key in ("ok", "node_id", "verdict", "closure", "main_closure", "calls", "reason")
            }
        if error:
            state["error"] = error[:600]
        if spent.get("calls"):
            state["usage"] = {key: spent[key] for key in ("calls", "input_tokens", "output_tokens", "cost_usd")}
        try:
            _set_recheck_state(run_id, state, refresh=True, usage=spent or None)
            if status == "done":
                verdict = str((result or {}).get("verdict") or "")
                text = {"checked": "checked by a model verifier", "not_checked": "not checked"}.get(verdict, verdict)
                _append_chat(run_id, "system", f"Re-check · {label} · {text} — a model judgement, not a certificate")
            else:
                _append_chat(run_id, "system", f"Re-check · {label} · {status}: {error}")
        except Exception:  # noqa: BLE001
            pass
        _update_job(job_id, status="finished" if status == "done" else status, error=error or None)
        if gate_key is not None:
            from agent_monitor import analyze_runs

            analyze_runs.GATE.release(gate_key, refund=unsent)
        if callable(on_exit):
            try:
                on_exit()
            except Exception:  # noqa: BLE001
                pass


# A question about a finished manuscript run (a readable version or an
# analysis) is answered by the mode's runner itself (``--ask <ask_id>``: one
# call, four with its retries, and 15 minutes) with the run's own route
# environment; the outer watchdog adds one call and the answer's publication.
_ASK_GRACE_SECONDS = 180
# Every write of a record's question state (``run[block]["ask"]`` and
# ``["asks"]``) takes the re-check state's lock: on an analysis both rewrite
# ``run["analyze"]``.
_ASK_STATE_LOCK = _RECHECK_STATE_LOCK
ASK_INTERRUPTED = "The question was interrupted (the server restarted); ask it again"
# The record blocks that hold questions, and their question state (kept when
# a block is rebuilt from the workspace).
ASK_BLOCKS = ("readability", "analyze")
ASK_KEYS = ("ask", "asks", "asks_started", "ask_usage")


class _AskMode(NamedTuple):
    """How a question about one kind of manuscript run is asked and answered."""

    kind: str
    block: str
    engine: str
    runner: str
    event: str
    timeout_variable: str
    wrong_kind: str
    unpublished: str
    node_refusal: str
    busy: str
    api_key_refusal: str


_ASK_MODES = {
    "readability": _AskMode(
        kind="readability", block="readability", engine=_READABILITY_ENGINE, runner="readability_runner.py",
        event="readability.ask", timeout_variable="AGENT_MONITOR_READABILITY_API_TIMEOUT",
        wrong_kind="Only readability runs answer questions about a paper",
        unpublished="This run has no readable version to ask about yet",
        node_refusal="node_id must be a result of this run",
        busy="Wait for the run (or the question before) to finish",
        api_key_refusal=("This run used an API key, which members can't use on this site; "
                         "start a new readability run on the included Kimi K3"),
    ),
    "analyze": _AskMode(
        kind="analyze", block="analyze", engine=_ANALYZE_ENGINE, runner="analyze_runner.py",
        event="analyze.ask", timeout_variable="AGENT_MONITOR_ANALYZE_API_TIMEOUT",
        wrong_kind="Only Analyze runs answer questions about an analysis",
        unpublished="This analysis has no published report to ask about yet",
        node_refusal="node_id must be a result of this analysis",
        busy="Wait for the analysis (or its re-check, or the question before) to finish",
        api_key_refusal=("This analysis ran on an API key, which members can't use on this site; "
                         "start a new analysis on the included Kimi K3"),
    ),
}


def ask_block_of(record: dict[str, Any] | None) -> str:
    """The block of a manuscript run's record that holds its questions."""
    from agent_monitor import analyze_runs

    return "analyze" if analyze_runs.is_analyze_record(record) else "readability"


def ask_count(record: dict[str, Any] | None) -> int:
    """How many questions were asked about this run (``asks_started``)."""
    block = (record or {}).get(ask_block_of(record))
    try:
        return max(0, int(block.get("asks_started") or 0)) if isinstance(block, dict) else 0
    except (TypeError, ValueError):
        return 0


def _ask_store(kind: str):
    if kind == "analyze":
        from agent_monitor.analyze import ask as analyze_ask

        return analyze_ask.STORE
    from agent_monitor.readability import ask as readability_ask

    return readability_ask.STORE


def _ask_published(kind: str, ws: Path) -> bool:
    """Whether the run published what questions are about (its receipt is written last)."""
    return (ws / ("analysis" if kind == "analyze" else "readability") / "receipt.json").is_file()


def _ask_node_ids(kind: str, ws: Path) -> set[str]:
    """The results a question may be about: the analysis graph's, or the paper map's."""
    if kind == "analyze":
        from agent_monitor.analyze import graph as analysis_graph

        return {node["id"] for node in (analysis_graph.load(ws) or {}).get("nodes") or []}
    from agent_monitor.readability import paper_map

    return {node["id"] for node in (paper_map.load(ws) or {}).get("nodes") or []}


def start_readability_ask(
    run_id: str,
    body: dict[str, Any],
    *,
    user: dict[str, Any] | None,
    on_exit: Any = None,
) -> dict[str, Any]:
    """Answer a question about a finished readability run in the background
    (:func:`start_manuscript_ask`)."""
    return start_manuscript_ask(run_id, body, kind="readability", user=user, on_exit=on_exit)


def start_analysis_ask(
    run_id: str,
    body: dict[str, Any],
    *,
    user: dict[str, Any] | None,
    on_exit: Any = None,
) -> dict[str, Any]:
    """Answer a question about a finished analysis in the background
    (:func:`start_manuscript_ask`)."""
    return start_manuscript_ask(run_id, body, kind="analyze", user=user, on_exit=on_exit)


def start_manuscript_ask(
    run_id: str,
    body: dict[str, Any],
    *,
    kind: str,
    user: dict[str, Any] | None,
    on_exit: Any = None,
) -> dict[str, Any]:
    """Answer a question about a finished readability run (``kind``
    ``"readability"``) or analysis (``"analyze"``) in the background.

    ``body`` is ``{question, selection?, view?, node_id?}``.  Returns
    ``{"ok": True, "status": "asking", "run_id", "ask_id", "job_id"}`` at
    once; the question waits in ``run[block]["ask"]`` and lands with its
    answer in ``run[block]["asks"]`` (the last 50; the block is
    ``"readability"`` or ``"analyze"``).  One question at a time per run, and
    none while the run (or an analysis's re-check) runs.  ``on_exit`` runs
    once when it is answered or did not start.  Raises
    ``analyze_runs.AnalyzeRequestError`` (status 400, 403, 409 or 429) or
    ``FileNotFoundError``.
    """
    from agent_monitor import analyze_runs, readability_runs
    from agent_monitor.manuscript_ask import AskError

    mode = _ASK_MODES[kind]
    analysis = kind == "analyze"
    handed_off = False
    gate_key: str | None = None
    claimed = False
    # The job this request registered: a refusal fails that one only, never
    # the question already being answered.
    registered: str | None = None
    state_written: dict[str, Any] | None = None
    try:
        record = _load_run_record(run_id)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        if callable(on_exit):
            on_exit()
        raise FileNotFoundError("run not found") from None
    try:
        if not (analyze_runs.is_analyze_record(record) if analysis
                else readability_runs.is_readability_record(record)):
            raise analyze_runs.AnalyzeRequestError(mode.wrong_kind)
        refusal = analyze_runs.ask_error(user) if analysis else readability_runs.availability_error(user)
        if refusal is not None:
            raise refusal
        store = _ask_store(kind)
        try:
            question = store.normalize(body)
        except AskError as exc:
            raise analyze_runs.AnalyzeRequestError(str(exc)) from None
        ws = workspace_dir(run_id)
        if not _ask_published(kind, ws):
            raise analyze_runs.AnalyzeRequestError(mode.unpublished, status=409)
        if question["node_id"] and question["node_id"] not in _ask_node_ids(kind, ws):
            raise analyze_runs.AnalyzeRequestError(mode.node_refusal)
        if str(record.get("status") or "") == "running" or run_is_active(run_id):
            raise analyze_runs.AnalyzeRequestError(mode.busy, status=409)
        if not _claim_launch(run_id):
            raise analyze_runs.AnalyzeRequestError(
                "A question about this run is already being answered", status=409
            )
        claimed = True
        # A guest asks a bounded number of questions per run (counted on the
        # record, under the launch claim, so it holds across restarts).
        requester_guest = bool(user and user.get("guest"))
        if requester_guest:
            try:
                current = _load_run_record(run_id)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                current = record
            if ask_count(current) >= analyze_runs.GUEST_ASKS_PER_RUN:
                message = analyze_runs.GUEST_ASK_LIMIT_MESSAGE if analysis else readability_runs.GUEST_ASK_LIMIT_MESSAGE
                raise analyze_runs.AnalyzeRequestError(
                    message.format(limit=analyze_runs.GUEST_ASKS_PER_RUN), status=403
                )
        # The model and route the run used, exactly as a re-check does (and
        # as there, never another Kimi model than Kimi K3).
        model = _continuation_model(record, None)
        refusal_text = analyze_runs.kimi_model_error(kind, model, ran=True)
        if refusal_text:
            raise analyze_runs.AnalyzeRequestError(refusal_text)
        model = analyze_runs.manuscript_model(model) or model
        sponsored = str(record.get("credential_source") or "") == "sponsored_kimi"
        guest = bool(record.get("guest"))
        if not sponsored and account_policy.member_analyze_sponsored_only(user):
            raise analyze_runs.AnalyzeRequestError(mode.api_key_refusal, status=403)
        if sponsored:
            # Sponsored model work stops when sponsored manuscript runs are
            # turned off, questions included (as re-checks and formalizing do).
            off = analyze_runs.sponsored_questions_error(guest=requester_guest, mode=kind)
            if off:
                raise analyze_runs.AnalyzeRequestError(off, status=403)
            # A slot of the sponsored pool, counted among the day's questions
            # (not its manuscript runs); the guests' share follows who asks.
            # Ours once reserved: a refusal ("held") gives back nobody's slot.
            analyze_runs.GATE.reserve(analyze_runs.ask_key(run_id), run_id=run_id, guest=requester_guest,
                                      messages=(analyze_runs.ASK_GATE_MESSAGES if analysis
                                                else readability_runs.ASK_GATE_MESSAGES),
                                      daily=analyze_runs.ASK_DAILY)
            gate_key = analyze_runs.ask_key(run_id)
        configured, _route = _continuation_auth_environment(
            run_data=record,
            engine=mode.engine,
            model=model,
            user=user,
            env=_user_extra_env(user, model),
        )
        _apply_bounded_run_env(configured, engine=mode.engine, guest=guest, sponsored=sponsored)
        if not sponsored and _is_kimi_model(model):
            configured["AGENT_MONITOR_KIMI_BASE_URL"] = _operator_kimi_base()
        env = _analyze_recheck_env(configured, model=model, engine=mode.engine)
        timeout = _ask_seconds(kind) + _analyze_api_timeout(
            sponsored, variable=mode.timeout_variable) + _ASK_GRACE_SECONDS
        ask_id = uuid.uuid4().hex[:12]
        store.write_request(ws, ask_id, question)
        job_id = uuid.uuid4().hex[:10]
        started_at = _now()
        _register_claimed_job(run_id, {
            "job_id": job_id,
            "run_id": run_id,
            "engine": mode.engine,
            "run_kind": kind,
            "kind": "ask",
            "ask_id": ask_id,
            "status": "running",
            "created_at": started_at,
            "updated_at": started_at,
            "error": None,
            "model": model,
            "owner_id": record.get("owner_id"),
            "guest": guest,
            "auth_route": record.get("auth_route"),
            "credential_source": record.get("credential_source"),
            "problem_preview": "Ask · " + " ".join(question["question"].split())[:100],
        })
        registered = job_id
        claimed = False
        _stop_event(run_id).clear()
        state = {
            "ask_id": ask_id,
            "status": "running",
            "question": question["question"],
            "selection": question["selection"],
            "view": question["view"],
            "node_id": question["node_id"],
            "job_id": job_id,
            "started_at": started_at,
        }
        _set_ask_state(run_id, state, started=True, block=mode.block)
        state_written = state
        thread = threading.Thread(
            target=_execute_ask,
            kwargs={
                "run_id": run_id, "job_id": job_id, "state": state, "workspace": ws, "env": env,
                "timeout": timeout, "gate_key": gate_key, "on_exit": on_exit, "model": model, "kind": kind,
            },
            daemon=True,
            name=f"{kind}-ask-{job_id}",
        )
        thread.start()
        handed_off = True
        return {"ok": True, "status": "asking", "run_id": run_id, "ask_id": ask_id, "job_id": job_id}
    finally:
        if not handed_off:
            if claimed:
                _release_launch(run_id)
            if gate_key is not None:
                analyze_runs.GATE.release(gate_key, refund=True)
            with _LOCK:
                job = _JOBS.get(registered or "")
                if job is not None and job.get("status") == "running":
                    job["status"] = "failed"
            if state_written is not None:
                _set_ask_state(run_id, {**state_written, "status": "failed", "finished_at": _now(),
                                        "error": "The question could not be sent"}, block=mode.block)
            if callable(on_exit):
                on_exit()


def _ask_seconds(kind: str) -> int:
    """The runner's own time limit for a question."""
    if kind == "analyze":
        from agent_monitor.runners.analyze_runner import ASK_SECONDS
    else:
        from agent_monitor.runners.readability_runner import ASK_SECONDS
    return int(ASK_SECONDS)


def _set_ask_state(
    run_id: str,
    state: dict[str, Any],
    *,
    started: bool = False,
    usage: dict[str, Any] | None = None,
    block: str = "readability",
) -> None:
    """Record a question in ``run[block]`` (``"readability"`` or
    ``"analyze"``): ``ask`` while it runs, then in ``asks`` (the last 50,
    oldest first).  ``asks_started`` counts the questions asked
    (``started=True`` counts one) and ``ask_usage`` the model calls, tokens
    and estimated cost they spent (``usage``)."""
    from agent_monitor.analyze_runs import ASKS_KEPT

    if block not in ASK_BLOCKS:
        raise ValueError(f"no questions are kept in run[{block!r}]")
    with _ASK_STATE_LOCK:
        try:
            run = _load_run_record(run_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        stored = dict(run.get(block) or {})
        if started:
            stored["asks_started"] = ask_count({"run_kind": block, block: stored}) + 1
        tally = dict(stored.get("ask_usage") or {}) if isinstance(stored.get("ask_usage"), dict) else {}
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
                tally[key] = round(float(tally.get(key) or 0) + float(value), 6)
                if key != "cost_usd":
                    tally[key] = int(tally[key])
        if tally:
            stored["ask_usage"] = tally
        if state.get("status") == "running":
            stored["ask"] = state
        else:
            current = stored.get("ask") if isinstance(stored.get("ask"), dict) else {}
            if current.get("ask_id") == state.get("ask_id"):
                stored.pop("ask", None)
            asks = [item for item in stored.get("asks") or []
                    if isinstance(item, dict) and item.get("ask_id") != state.get("ask_id")]
            stored["asks"] = (asks + [state])[-ASKS_KEPT:]
        run[block] = stored
        run["updated_at"] = _now()
        _write_run(run)


def reconcile_ask_state(run_id: str, run: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Repair a question that a lost worker left ``running`` (a restart, a
    crash of its thread), about a readability run or an analysis: it becomes
    ``interrupted``.  Returns the current record."""
    if run is None:
        try:
            run = _load_run_record(run_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
    for name in ASK_BLOCKS:
        block = run.get(name) if isinstance(run.get(name), dict) else {}
        question = block.get("ask") if isinstance(block.get("ask"), dict) else None
        if not question or question.get("status") != "running" or _recheck_worker_alive(run_id, question.get("job_id")):
            continue
        with _ASK_STATE_LOCK:
            try:
                current = _load_run_record(run_id)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return run
            state = (current.get(name) or {}).get("ask")
            if (
                not isinstance(state, dict)
                or state.get("status") != "running"
                or state.get("job_id") != question.get("job_id")
                or _recheck_worker_alive(run_id, state.get("job_id"))
            ):
                run = current
                continue
        _set_ask_state(run_id, {**state, "status": "interrupted", "finished_at": _now(), "error": ASK_INTERRUPTED},
                       block=name)
        from agent_monitor import analyze_runs

        # A slot held by a lost thread of this process is returned at once.
        analyze_runs.GATE.release(analyze_runs.ask_key(run_id))
        try:
            run = _load_run_record(run_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            run = current
    return run


def _ask_outcome(output: str, event_type: str = "readability.ask") -> tuple[dict[str, Any] | None, str]:
    """The runner's ``event_type`` result (``readability.ask``,
    ``analyze.ask``), or its terminal error message."""
    result: dict[str, Any] | None = None
    error = ""
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == event_type and isinstance(event.get("result"), dict):
            result = event["result"]
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if item and item.get("type") == "error" and item.get("terminal") is True:
            error = str(item.get("message") or "")[:600]
    return result, error


def _execute_ask(
    *,
    run_id: str,
    job_id: str,
    state: dict[str, Any],
    workspace: Path,
    env: dict[str, str],
    timeout: int,
    gate_key: str | None,
    on_exit: Any,
    model: str | None = None,
    kind: str = "readability",
) -> None:
    mode = _ASK_MODES[kind]
    status = "failed"
    result: dict[str, Any] | None = None
    error = ""
    spent: dict[str, Any] = {}
    # A question that never reached a model gives its sponsored start back.
    unsent = False
    try:
        runner = Path(__file__).resolve().parent / "runners" / mode.runner
        argv = [sys.executable, "-u", str(runner), "--ask", str(state["ask_id"])]
        output, returncode, error = _watch_runner(
            run_id, argv, workspace=workspace, env=env, timeout=timeout,
            timed_out=f"The question timed out after {timeout}s",
        )
        if returncode is None:
            error = f"The question could not be sent ({error})"
            unsent = True
        else:
            try:
                # Every call the answer took is spent, whatever its outcome.
                spent = _book_recheck_usage(run_id, output, model=model, stage="ask")
            except Exception:  # noqa: BLE001 - accounting must not lose the outcome
                spent = {}
            result, terminal = _ask_outcome(output, mode.event)
            # Exit 2 is a configuration refusal: nothing was sent to a model.
            unsent = returncode == 2
            if returncode == 0 and result is not None:
                status = "done"
            elif returncode == 128 + int(signal.SIGTERM) or _stop_event(run_id).is_set():
                status = "stopped"
                error = error or "The question was stopped"
            else:
                error = error or terminal or f"The question failed (exit code {returncode})"
    except Exception as exc:  # noqa: BLE001 - the record must say what happened
        error = f"The question failed ({type(exc).__name__})"
    finally:
        final = {**state, "status": status, "finished_at": _now()}
        if result is not None:
            final.update(
                answer=str(result.get("answer") or "")[:20_000],
                refs=[str(item)[:80] for item in (result.get("refs") or [])[:50]],
                model=str(result.get("model") or model or "")[:120],
            )
            if isinstance(result.get("part"), int):
                final["part"] = result["part"]
            about = [str(item)[:80] for item in (result.get("about") or [])[:4] if isinstance(item, str)]
            if about:
                final["about"] = about
            if result.get("carried") is True:
                final["carried"] = True
        if error:
            final["error"] = error[:600]
        if spent.get("calls"):
            final["usage"] = {key: spent[key] for key in ("calls", "input_tokens", "output_tokens", "cost_usd")}
        try:
            _set_ask_state(run_id, final, usage=spent or None, block=mode.block)
        except Exception:  # noqa: BLE001
            pass
        _update_job(job_id, status="finished" if status == "done" else status, error=error or None)
        if gate_key is not None:
            from agent_monitor import analyze_runs

            analyze_runs.GATE.release(gate_key, refund=unsent)
        if callable(on_exit):
            try:
                on_exit()
            except Exception:  # noqa: BLE001
                pass


def stop_run(run_id: str) -> dict[str, Any]:
    """Stop a running job: signal the stop event and kill any subprocess.

    An Analyze or Readability runner (or a re-check, or a question) gets
    SIGTERM first: it publishes what had settled (a re-check restores the
    node) and exits 143.  So does an Ansatz CGM runner (signalled before the
    processes of its model calls, see _stop_cgm_runner): before its
    publication it publishes nothing and exits 143; after it, the stop ends
    only its written-proof round and the run is recorded as finished.  The
    worker kills a group that does not exit within 30 s.  A Stop repeated
    meanwhile (a double click, a second tab) signals nothing more: a second
    signal could cut that wind-down short (a CGM runner's report, say).
    """
    active = _run_is_active(run_id)
    with _LOCK:
        stop = _stop_event(run_id)
        # Every start of the run (or of a continuation) clears it.
        repeated = stop.is_set()
        stop.set()
        proc = _PROCS.get(run_id)
    if repeated and (proc is not None or (active or {}).get("kind") in ("recheck", "ask")):
        # The first Stop signalled this runner (or _register_proc killed it)
        # and records the stop; its worker ends it.  A re-check or question
        # is ended by its worker whether or not its runner is registered now.
        return {"ok": True, "status": "stopped", "run_id": run_id}
    graceful = bool(active) and (
        active.get("engine") in {*_MANUSCRIPT_ENGINES, *_CGM_ENGINES}
        or active.get("kind") in ("recheck", "ask")
    )
    if proc is not None:
        if graceful and active.get("engine") in _CGM_ENGINES:
            # Recorded before the runner is signalled: stopped in its
            # written-proof round, it publishes, reports and exits while its
            # call processes are still being ended (up to a second), and its
            # worker's outcome ("finished") must be the last word.
            dropped_bookings = _record_stopped(run_id, active["job_id"])
            _stop_cgm_runner(proc)
            return {"ok": True, "status": "stopped", "run_id": run_id, "job_id": active["job_id"],
                    "dropped_bookings": dropped_bookings}
        _terminate_registered_proc(proc, force=not graceful)
    if not active:
        # Nothing runs here: a re-check (or a question) left "running" by a
        # lost worker is repaired so the run is usable again.
        before = None
        try:
            before = ((_load_run_record(run_id).get("analyze") or {}).get("recheck") or {}).get("status")
        except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
            pass
        after = ((reconcile_recheck_state(run_id) or {}).get("analyze") or {}).get("recheck") or {}
        reconcile_ask_state(run_id)
        result = {"ok": True, "status": "not_running", "run_id": run_id}
        if before == "running" and after.get("status") == "interrupted":
            result["recheck"] = "interrupted"
        return result
    if active.get("kind") in ("recheck", "ask"):
        # Stopping a re-check (or a question) changes nothing: the run keeps
        # its status and every result (a re-check restores its node).  Its
        # job stays running until its worker ends (the worker always records
        # the outcome, "stopped" here, and ends the job), so nothing new
        # starts beside a runner that is still winding down, and the
        # question or re-check is never read as lost meanwhile.  A runner
        # that registers after this Stop is killed at once (_register_proc).
        return {"ok": True, "status": "stopped", "run_id": run_id, "job_id": active["job_id"],
                active["kind"]: True}
    dropped_bookings = _record_stopped(run_id, active["job_id"])
    return {"ok": True, "status": "stopped", "run_id": run_id, "job_id": active["job_id"],
            "dropped_bookings": dropped_bookings}


def _without_dropped(problems: Any, dropped: list[dict[str, Any]]) -> list[Any] | None:
    """``problems`` without what queueing the ``dropped`` messages added
    (for each, the last human problem of its text), or ``None`` when that
    removes nothing.  A message the session already had added nothing, and
    the earlier problem of its text stays."""
    if not isinstance(problems, list):
        return None
    kept = list(problems)
    removed = False
    for entry in dropped:
        if not entry.get("problem_added"):
            continue
        for index in range(len(kept) - 1, -1, -1):
            item = kept[index]
            if (
                isinstance(item, dict)
                and str(item.get("source") or "") == "human"
                and _problem_item_text(item) == entry["content"]
            ):
                del kept[index]
                removed = True
                break
    return kept if removed else None


def _record_stopped(run_id: str, job_id: str) -> list[dict[str, Any]]:
    """The run's record, job and chat say the user stopped it.

    Feedback queued for the end of the stopped session is dropped, and the
    chat says so: a Stop starts no follow-up, and left queued it would start
    one, unasked, after the run's next session.  A dropped message leaves
    the session too: it is no longer one of its problems (problem.txt and
    problems.json follow), so no later turn, a bare Continue included,
    answers it and it no longer counts toward a member's feedback cap.  The
    files it brought stay with the run, counted as delivered, so no CGM turn
    shows them first as new.  A message sent once the stop is recorded waits
    for the stopped worker, then continues the run (see
    _launch_pending_feedback).
    """
    workspace = RUNS_DIR / "workspaces" / run_id
    with _LOCK:  # a chat message is queued either before this or after it
        _update_job(job_id, status="stopped")
        dropped = _drain_feedback_entries(workspace)
    try:
        with _RUN_WRITE_LOCK:  # no stale copy over a follow-up's final write
            run_data = _load_run_record(run_id)
            run_data["status"] = "stopped"
            run_data["updated_at"] = _now()
            problems = _without_dropped(run_data.get("problems"), dropped)
            if problems is not None:
                run_data["problems"] = problems
            _write_run(run_data)
            if problems is not None and not _run_deleted(run_id):
                _write_session_problems(workspace, problems)
    except FileNotFoundError:
        pass
    files = [ident for entry in dropped for ident in entry.get("attachment_ids") or []]
    if files and not _run_deleted(run_id):
        cgm_sources.mark_delivered(workspace, files)
    _append_chat(run_id, "system", "Run stopped by user.")
    if dropped:
        one = len(dropped) == 1
        it = "it" if one else "them"
        text = (
            f"Queued feedback ({len(dropped)} message{'' if one else 's'}) was removed from "
            f"the session: the run was stopped before {'it' if one else 'they'} ran. "
            f"Send {it} again to have {it} answered."
        )
        if files:
            text += f" The files attached to {it} stay with the run."
        _append_chat(run_id, "system", text)
    # The run starts the dropped messages booked, each with its author (the
    # console gives exactly those back, see _queue_human_feedback).
    return [
        {"author_id": entry["author_id"], "booking": entry["booking"]}
        for entry in dropped
        if entry.get("author_id") is not None and entry.get("booking") is not None
    ]


def continue_run(
    run_id: str,
    *,
    message: str | None = None,
    model: str | None = None,
    max_iterations: int = 40,
    user: dict[str, Any] | None = None,
    problems_recorded: bool = False,
    new_problems: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Resume a stopped/finished/failed run, optionally with extra guidance.

    ``new_problems``: what of ``message`` the caller added to the session
    problems for this turn (see _execute_continue)."""
    try:
        recorded = _load_run_record(run_id)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        recorded = {"engine": run_id.split("_", 1)[0]}
    _refuse_analyze_continuation(recorded)
    # Claim the run before the slow preparation (credential and account
    # checks): a second request in that window must not start a duplicate.
    if not _claim_launch(run_id):
        if run_is_stopping(run_id):
            return {"ok": False, "status": "stopping", "run_id": run_id, "error": RUN_STOPPING_ERROR}
        return {"ok": False, "status": "already_running", "run_id": run_id}
    try:
        return _continue_claimed_run(
            run_id,
            message=message,
            model=model,
            max_iterations=max_iterations,
            user=user,
            problems_recorded=problems_recorded,
            new_problems=new_problems,
        )
    except BaseException as exc:
        _release_launch(run_id)
        # Feedback queued behind this claim would wait for a harness that
        # never runs.  A queued follow-up (problems_recorded) is that feedback
        # itself, and _launch_pending_feedback puts it back.
        if isinstance(exc, Exception) and not problems_recorded:
            _start_feedback_queued_behind(run_id, max_iterations=max_iterations)
        raise


def _continue_claimed_run(
    run_id: str,
    *,
    message: str | None,
    model: str | None,
    max_iterations: int,
    user: dict[str, Any] | None,
    problems_recorded: bool,
    new_problems: Sequence[str] | None = None,
) -> dict[str, Any]:
    _stop_event(run_id).clear()

    ws = RUNS_DIR / "workspaces" / run_id
    if not ws.is_dir():
        raise FileNotFoundError("workspace not found")
    run_data = _load_run_record(run_id)
    # A run started on Ansatz CGM: this turn's engine (easy_route).
    run_data = _easy_route_follow_up(run_id, run_data, ws, message=message, user=user, model=model)
    # Stored engine identity is provenance, not a catalog alias. In particular,
    # an old UCLA continuation must stay on its explicit legacy path below and
    # must never be rewritten to ``math_harness``.
    engine = run_data.get("engine") or run_id.split("_", 1)[0]
    from agent_monitor.engines_registry import retired_engine_error

    retired_error = retired_engine_error(engine)
    if retired_error:
        raise ValueError(retired_error)
    effective_model = _member_continuation_model(user, engine, run_data, model)
    tool_free = _tool_free_follow_up(run_id, run_data, engine, effective_model)
    problems = ensure_problems(run_data, run_id)
    first_problem = _problem_item_text(problems[0]) if problems else (
        run_data.get("problem_text") or ""
    )
    feedback = (message or "").strip() or (
        _BARE_CONTINUE_CGM if engine in _CGM_ENGINES else _BARE_CONTINUE_OTHER
    )

    job_id = uuid.uuid4().hex[:10]
    continue_owner_id = (user or {}).get("id") or run_data.get("owner_id")
    continue_env, continued_route = _continuation_auth_environment(
        run_data=run_data,
        engine=engine,
        model=effective_model,
        user=user,
        env=_user_extra_env(
            user,
            *_run_provider_models(run_data, effective_model, run_data.get("subagent_model")),
        ),
    )
    continued_subagents = bool(run_data.get("use_subagents", True)) and not tool_free
    continue_env["AGENT_MONITOR_USE_SUBAGENTS"] = "1" if continued_subagents else "0"
    continued_subagent_model = run_data.get("subagent_model") if continued_subagents else None
    if continued_subagent_model:
        continue_env["AGENT_MONITOR_SUBAGENT_MODEL"] = str(continued_subagent_model)
    else:
        continue_env.pop("AGENT_MONITOR_SUBAGENT_MODEL", None)
    thread = threading.Thread(
        target=_execute_continue,
        kwargs={
            "job_id": job_id,
            "run_id": run_id,
            "engine": engine,
            "message": feedback,
            "problem_text": first_problem,
            "model": effective_model,
            "max_iterations": max_iterations,
            "workspace": str(ws),
            "extra_env": continue_env,
            "owner_id": continue_owner_id,
            "problems_recorded": problems_recorded,
            "new_problems": None if new_problems is None else list(new_problems),
            "answer_only": _easy_answer_only(ws, run_data),
        },
        daemon=True,
        name=f"continue-{run_id}-{job_id}",
    )
    _register_claimed_job(run_id, {
        "job_id": job_id,
        "run_id": run_id,
        "engine": engine,
        "problem_id": run_data.get("problem_id"),
        "status": "running",
        "created_at": _now(),
        "updated_at": _now(),
        "error": None,
        "model": effective_model,
        "kind": "continue",
        "owner_id": continue_owner_id,
        "auth_route": continued_route,
        "reasoning_effort": run_data.get("reasoning_effort", "default"),
        "speed_mode": run_data.get("speed_mode", "standard"),
        "problem_preview": _session_title(problems, first_problem),
    }, thread)
    _start_claimed_worker(run_id, job_id, thread, "Continuing run…")
    return {"ok": True, "status": "continuing", "run_id": run_id, "job_id": job_id}


_GUEST_PURGE_MAX_RECORD_BYTES = 8 * 1024 * 1024


def purge_guest_runs(*, older_than_days: float, dry_run: bool = True) -> list[str]:
    """Delete finished anonymous-guest runs older than ``older_than_days``.

    Guest identities expire after 24 hours, but their run records and
    workspaces stay on disk until removed. This operator retention helper
    selects only terminal runs recorded as guest runs (or owned by a legacy
    ``@public.local`` identity) whose last update is older than the cutoff,
    and deletes each through :func:`delete_run`. Usage history is kept. With
    ``dry_run`` (the default) it only reports the run ids it would delete.
    """
    cutoff = time.time() - max(1.0, float(older_than_days)) * 86_400
    selected: list[str] = []
    for path in sorted(_cache_dir().glob("*.json")):
        if path.name == "manifest.json":
            continue
        try:
            if path.is_symlink() or path.stat().st_size > _GUEST_PURGE_MAX_RECORD_BYTES:
                continue
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(run, dict):
            continue
        run_id = str(run.get("run_id") or "")
        if run_id != path.stem or "/" in run_id or ".." in run_id:
            continue
        guest = run.get("guest") is True or str(run.get("owner") or "").strip().lower().endswith(
            "@public.local"
        )
        if not guest or str(run.get("status") or "").lower() not in {
            "finished", "failed", "stopped", "cancelled", "interrupted",
        }:
            continue
        if _run_is_active(run_id):
            continue
        stamp = str(run.get("updated_at") or run.get("created_at") or "")
        try:
            updated = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if updated >= cutoff:
            continue
        selected.append(run_id)
        if not dry_run:
            delete_run(run_id)
    return selected


def delete_run(run_id: str) -> dict[str, Any]:
    """Delete a run, its workspace, cache JSON, manifest entry, and Hermes memory.

    A run deleted while it runs stays deleted: its worker writes no record,
    manifest entry or chat line after this (_write_run_locked, _append_chat).
    """
    if not run_id or "/" in run_id or ".." in run_id:
        raise ValueError("invalid run_id")

    cache = _cache_dir()
    workspace_path: str | None = None
    identity: dict[str, Any] = {}
    recorded: Any = None
    run_json = cache / f"{run_id}.json"
    if run_json.exists():
        try:
            recorded = json.loads(run_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            recorded = None
        if isinstance(recorded, dict):
            workspace_path = recorded.get("workspace")
            # Only what a winding-down worker's writes need (_write_run_locked,
            # usage_ledger._book_run's title).
            identity = {
                key: recorded[key]
                for key in (*_RECORD_IDENTITY_KEYS, "problem_text_preview")
                if key in recorded and not isinstance(recorded[key], (dict, list))
            }
            identity["problem_text"] = str(recorded.get("problem_text") or "")[:2000]

    removed: dict[str, Any] = {"run_id": run_id, "deleted": []}

    # Usage history outlives the run; only the trace link is retired.
    from agent_monitor import usage_ledger

    usage_ledger.mark_deleted(run_id)

    _stop_event(run_id).set()
    with _LOCK:
        proc = _PROCS.pop(run_id, None)
        # A second delete (an administrator's, the record gone) keeps the
        # identity the first one took.
        _DELETED_RUN_IDENTITY[run_id] = identity or _DELETED_RUN_IDENTITY.get(run_id) or {}
        if isinstance(recorded, dict) and any(
            worker_run == run_id and _worker_not_done(thread) for worker_run, thread in _WORKERS.values()
        ):
            # A session still running: its record lives on in memory until its
            # worker ends, for the usage it books (_record_at_session_end).
            _DELETED_RUN_LAST[run_id] = recorded
        # Beyond the cap the oldest identities go; a session still winding
        # down (_DELETED_RUN_LAST) keeps its own.
        excess = len(_DELETED_RUN_IDENTITY) - _DELETED_RUN_IDENTITY_MAX
        if excess > 0:
            for old in [
                key for key in _DELETED_RUN_IDENTITY if key != run_id and key not in _DELETED_RUN_LAST
            ][:excess]:
                del _DELETED_RUN_IDENTITY[old]
        _DELETED_RUNS.add(run_id)
        for jid in [k for k, j in _JOBS.items() if j.get("run_id") == run_id]:
            _JOBS.pop(jid, None)
    if proc is not None:
        _terminate_registered_proc(proc)
    ws = RUNS_DIR / "workspaces" / run_id

    # A record or chat write in progress ends first; later ones see the run
    # deleted and write nothing.
    with _RUN_WRITE_LOCK, _CHAT_LOCK:
        manifest_path = cache / "manifest.json"
        if manifest_path.exists():
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                payload["runs"] = [
                    r for r in (payload.get("runs") or []) if r.get("run_id") != run_id
                ]
                payload["built_at"] = _now()
                manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                removed["deleted"].append(str(manifest_path))
            except (json.JSONDecodeError, OSError):
                pass

        for path in (run_json, RUNS_DIR / f"{run_id}.json"):
            if path.exists():
                path.unlink()
                removed["deleted"].append(str(path))

        pdf = cache / "pdf" / f"{run_id}.pdf"
        if pdf.exists():
            pdf.unlink()
            removed["deleted"].append(str(pdf))

        if ws.is_dir():
            # The run's derived-views pass may be removing what it wrote after
            # the deletion at the same time (auto_pipeline): a path already
            # gone is fine, any other error still fails the deletion.  (Python
            # 3.11 reports every vanished entry to the handler; 3.12+ names it
            # onexc.)
            def _gone_already(_function, _path, error) -> None:
                error = error[1] if isinstance(error, tuple) else error
                if not isinstance(error, FileNotFoundError):
                    raise error

            if sys.version_info >= (3, 12):
                shutil.rmtree(ws, onexc=_gone_already)
            else:
                shutil.rmtree(ws, onerror=_gone_already)
            removed["deleted"].append(str(ws))

    hermes_removed = _cleanup_hermes_memory(run_id, workspace_path)
    if hermes_removed:
        removed["hermes_sessions"] = hermes_removed

    return removed
