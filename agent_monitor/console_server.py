"""Unified Math Proving Console HTTP server."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import stat
import sys
import threading
import time
from collections import deque
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse, urlsplit, urlunsplit

from agent_monitor import CACHE_DIR, PROBLEMS_DIR, ROOT, RUNS_DIR
from agent_monitor import account_policy
from agent_monitor import jobs as job_manager
from agent_monitor.run_sharing import RunSharingMixin
from agent_monitor.projects import ProjectsMixin
from agent_monitor.research import ResearchMixin
from agent_monitor import projects as research_projects
from agent_monitor import attachments as research_attachments
from agent_monitor.paths import ensure_data_dirs, ensure_import_paths

ensure_import_paths()
ensure_data_dirs()

# Load API keys from Agent_Monitor/.env so all engine subprocesses inherit them.
try:
    from dotenv import load_dotenv

    env_path = Path(os.environ.get("AGENT_MONITOR_ENV_PATH") or ROOT / ".env")
    load_dotenv(env_path, override=False)
except ImportError:
    pass

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
DASHBOARD_WEB = ROOT / "monitor_core" / "harness_dashboard" / "web"
PORT = int(os.environ.get("AGENT_MONITOR_PORT", os.environ.get("LLM_MONITOR_PORT", "4600")))
# Loopback by default: the public deployment sits behind Caddy, and binding
# every interface must be an explicit operator choice.
HOST = os.environ.get("AGENT_MONITOR_HOST", "127.0.0.1").strip() or "127.0.0.1"
logger = logging.getLogger("agent_monitor.console_server")

# The deployment agent first pauses new writes at Caddy, then waits for this
# count and persisted background-work statuses to become idle. GET requests can
# also start a guest sidecar, so they are counted and paused too. Count errors: finally
# ensures a disconnected browser cannot leave deployment blocked forever.
_REQUEST_ACTIVITY_LOCK = threading.Lock()
_ACTIVE_REQUESTS = 0


def _track_request(method):
    @wraps(method)
    def tracked(*args, **kwargs):
        global _ACTIVE_REQUESTS
        with _REQUEST_ACTIVITY_LOCK:
            _ACTIVE_REQUESTS += 1
        try:
            return method(*args, **kwargs)
        finally:
            with _REQUEST_ACTIVITY_LOCK:
                _ACTIVE_REQUESTS -= 1
    return tracked


def _deployment_activity(*, exclude_current: bool = False) -> dict[str, int]:
    with _REQUEST_ACTIVITY_LOCK:
        return {"protocol": 1, "active_requests": max(0, _ACTIVE_REQUESTS - int(exclude_current))}


# Password-reset delivery must not make HTTP response timing depend on whether
# an account exists. Keep the work bounded, serialize SMTP, and rate-limit by a
# hash of the normalized address so the limiter does not retain email strings.
_PASSWORD_RESET_COOLDOWN_SECONDS = 300.0
_PASSWORD_RESET_WINDOW_SECONDS = 60.0
_PASSWORD_RESET_WINDOW_LIMIT = 30
_PASSWORD_RESET_RECENT_LIMIT = 4096
_PASSWORD_RESET_QUEUE: queue.Queue[tuple[str, str, str]] = queue.Queue(maxsize=64)
_PASSWORD_RESET_LOCK = threading.Lock()
_PASSWORD_RESET_RECENT: dict[bytes, float] = {}
_PASSWORD_RESET_WINDOW: deque[float] = deque()
_PASSWORD_RESET_WORKER_STARTED = False
_GUEST_SESSION_WINDOW_SECONDS = 60.0
_GUEST_SESSION_WINDOW: deque[float] = deque()
_GUEST_SESSION_LOCK = threading.Lock()
_GUEST_RUN_WINDOW_SECONDS = 3600.0
_GUEST_RUN_WINDOWS: dict[int, deque[float]] = {}
_GUEST_GLOBAL_RUN_WINDOW: deque[float] = deque()
# Held through both admission and job registration so simultaneous HTTP
# requests cannot all pass the same concurrency/retained-run snapshot.
_GUEST_RUN_START_LOCK = threading.RLock()
_GUEST_VERIFY_WINDOW_SECONDS = 3600.0
_GUEST_VERIFY_WINDOWS: dict[int, deque[float]] = {}
_GUEST_VERIFY_GLOBAL_WINDOW: deque[float] = deque()
_GUEST_VERIFY_ACTIVE_USERS: set[int] = set()
_GUEST_VERIFY_LOCK = threading.Lock()
_GUEST_DERIVED_WINDOW_SECONDS = 3600.0
_GUEST_DERIVED_WINDOWS: dict[int, deque[float]] = {}
_GUEST_DERIVED_GLOBAL_WINDOW: deque[float] = deque()
_GUEST_DERIVED_ACTIVE_USERS: set[int] = set()
_GUEST_DERIVED_LOCK = threading.Lock()
_GUEST_PIPELINE_BOOTSTRAP_LOCK = threading.Lock()
_GUEST_PIPELINE_BOOTSTRAPS: dict[str, tuple[int, Path]] = {}
# Per-client guest accounting (see guest_client_key): hourly windows keyed by
# client bucket, on top of the per-identity and site-wide limits, so one
# visitor minting many guest identities cannot use up everyone's allowance.
_GUEST_CLIENT_WINDOW_SECONDS = 3600.0
_GUEST_SESSION_CLIENT_WINDOWS: dict[str, deque[float]] = {}
_GUEST_RUN_CLIENT_WINDOWS: dict[str, deque[float]] = {}
_GUEST_DERIVED_CLIENT_WINDOWS: dict[str, deque[float]] = {}
# Active guest run id -> the client bucket that started it.
_GUEST_RUN_CLIENTS: dict[str, str] = {}
# Self-registered (member) accounts: new-account creation per client and
# site-wide, and hourly run starts per member (sponsored Kimi is shared).
_SIGNUP_WINDOW_SECONDS = 3600.0
_SIGNUP_CLIENT_WINDOWS: dict[str, deque[float]] = {}
_SIGNUP_GLOBAL_WINDOW: deque[float] = deque()
_SIGNUP_LOCK = threading.Lock()
_MEMBER_RUN_WINDOW_SECONDS = 3600.0
_MEMBER_RUN_WINDOWS: dict[int, deque[float]] = {}
_MEMBER_RUN_LOCK = threading.Lock()
# Every member chat message, including one that joins feedback already queued
# for a running run (which uses no run start), per member and hour.
_MEMBER_MESSAGE_WINDOWS: dict[int, deque[float]] = {}
GUEST_MAX_LEAN_CHARACTERS = 100_000
GUEST_MAX_FEEDBACK_CHARACTERS = 8_000
# A guest run request carries at most a 20,000-character problem and the small
# guest attachment allowance, so its body is capped like ordinary JSON routes
# before anything is read (members keep the 29 MiB attachment ceiling).
GUEST_MAX_REQUEST_BYTES = 1_000_000
# After refusing a request before reading its body (413, or 401/403 on an
# attachment route), read and discard at most this much of the body for at
# most this long before closing.  Closing with unread data makes the kernel
# reset the connection, which can destroy the refusal in flight (Caddy then
# answers 502 or aborts the HTTP/2 stream instead of passing on the 413).
REFUSED_BODY_DRAIN_BYTES = 32 * 1024 * 1024
REFUSED_BODY_DRAIN_SECONDS = 5.0


def _password_reset_worker() -> None:
    """Send queued reset and verification emails off the request path."""
    while True:
        kind, email, base_url = _PASSWORD_RESET_QUEUE.get()
        try:
            from agent_monitor import auth

            if kind == "verify":
                auth.send_email_verification(email, base_url)
            else:
                auth.handle_forgot_password(email, base_url)
        except Exception as exc:  # noqa: BLE001
            # Never include an address, token, or link in this log.
            logger.error("Queued %s email failed (%s)", kind, type(exc).__name__)
        finally:
            _PASSWORD_RESET_QUEUE.task_done()


def _start_password_reset_worker_locked() -> None:
    global _PASSWORD_RESET_WORKER_STARTED
    if _PASSWORD_RESET_WORKER_STARTED:
        return
    threading.Thread(
        target=_password_reset_worker,
        name="password-reset-mailer",
        daemon=True,
    ).start()
    _PASSWORD_RESET_WORKER_STARTED = True


def _enqueue_password_reset(email: str, base_url: str) -> bool:
    """Queue a generic reset attempt without performing an account lookup."""
    return _enqueue_mail_job("reset", email, base_url)


def _enqueue_email_verification(email: str, base_url: str) -> bool:
    """Queue a verification link; the worker skips unknown or verified accounts."""
    return _enqueue_mail_job("verify", email, base_url)


def _enqueue_mail_job(kind: str, email: str, base_url: str) -> bool:
    normalized = (email or "").strip().lower()
    if not normalized or len(normalized) > 320:
        return False

    key = hashlib.sha256(f"{kind}:{normalized}".encode("utf-8")).digest()
    now = time.monotonic()
    with _PASSWORD_RESET_LOCK:
        cutoff = now - _PASSWORD_RESET_WINDOW_SECONDS
        while _PASSWORD_RESET_WINDOW and _PASSWORD_RESET_WINDOW[0] <= cutoff:
            _PASSWORD_RESET_WINDOW.popleft()

        previous = _PASSWORD_RESET_RECENT.get(key)
        if previous is not None:
            if now - previous < _PASSWORD_RESET_COOLDOWN_SECONDS:
                return False
            _PASSWORD_RESET_RECENT.pop(key, None)

        if len(_PASSWORD_RESET_RECENT) >= _PASSWORD_RESET_RECENT_LIMIT:
            stale_before = now - _PASSWORD_RESET_COOLDOWN_SECONDS
            for stale_key, seen_at in list(_PASSWORD_RESET_RECENT.items()):
                if seen_at <= stale_before:
                    _PASSWORD_RESET_RECENT.pop(stale_key, None)
            if len(_PASSWORD_RESET_RECENT) >= _PASSWORD_RESET_RECENT_LIMIT:
                return False

        if len(_PASSWORD_RESET_WINDOW) >= _PASSWORD_RESET_WINDOW_LIMIT:
            return False
        try:
            _PASSWORD_RESET_QUEUE.put_nowait((kind, normalized, base_url))
        except queue.Full:
            return False

        _PASSWORD_RESET_RECENT[key] = now
        _PASSWORD_RESET_WINDOW.append(now)
        _start_password_reset_worker_locked()
        return True


def _normalize_base_path(raw: str | None) -> str:
    """Return '' or '/secret' (leading slash, no trailing slash)."""
    s = (raw or "").strip()
    if not s or s == "/":
        return ""
    if not s.startswith("/"):
        s = "/" + s
    return s.rstrip("/")


# Public URL prefix (security-through-obscurity gate in front of login).
# Example: AGENT_MONITOR_BASE_PATH=/ohwoiebrbjrbiuasoo1123k
BASE_PATH = _normalize_base_path(os.environ.get("AGENT_MONITOR_BASE_PATH"))

# Open the console without a login wall. Each visitor gets a guest session.
PUBLIC_MODE = os.environ.get("AGENT_MONITOR_PUBLIC", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Only the single remote API call baseline is guest-eligible. Local agent and
# harness engines do not provide an operating-system isolation boundary.
# Ansatz Analyze and Ansatz Readability are tool-free too; guests get them
# behind their own switches (AGENT_MONITOR_ANALYZE_GUESTS,
# AGENT_MONITOR_READABILITY_GUESTS), not through AGENT_MONITOR_GUEST_ENGINES.
GUEST_PROOF_ENGINES = frozenset({"kimi", "plain"})
GUEST_ELIGIBLE_ENGINES = GUEST_PROOF_ENGINES | {"analyze", "readability"}
GUEST_DEFAULT_ENGINES = GUEST_PROOF_ENGINES
# One Analyze per guest session per hour (and a few per client network);
# guest analyses also count against the guest run limits above.
GUEST_ANALYZE_RUNS_PER_HOUR = 1
GUEST_ANALYZE_RUNS_PER_CLIENT_PER_HOUR = 3
_GUEST_ANALYZE_WINDOWS: dict[int, deque[float]] = {}
_GUEST_ANALYZE_CLIENT_WINDOWS: dict[str, deque[float]] = {}
# Guest re-checks (each up to eight sponsored calls) belong to the guest
# Analyze allowance, not only to the generic derived-action limits: a few per
# analysis (analyze_runs.GUEST_RECHECKS_PER_ANALYSIS), per guest session and
# per client network each hour.
GUEST_RECHECKS_PER_HOUR = 2
GUEST_RECHECKS_PER_CLIENT_PER_HOUR = 4
_GUEST_RECHECK_WINDOWS: dict[int, deque[float]] = {}
_GUEST_RECHECK_CLIENT_WINDOWS: dict[str, deque[float]] = {}
# One readability run per guest session per hour (and a few per client
# network), on top of the guest run limits, as for Analyze.
GUEST_READABILITY_RUNS_PER_HOUR = 1
GUEST_READABILITY_RUNS_PER_CLIENT_PER_HOUR = 3
_GUEST_READABILITY_WINDOWS: dict[int, deque[float]] = {}
_GUEST_READABILITY_CLIENT_WINDOWS: dict[str, deque[float]] = {}
# Questions about finished manuscript runs (analyses and readable versions
# together) per account and hour (analyze_runs.ASKS_PER_HOUR), and guests'
# questions per client network and hour.
GUEST_ASKS_PER_CLIENT_PER_HOUR = 12
_ASK_WINDOWS: dict[int, deque[float]] = {}
_ASK_CLIENT_WINDOWS: dict[str, deque[float]] = {}
# Link previews (POST /api/analyze/resolve) fetch metadata from arXiv/GitHub.
ANALYZE_RESOLVES_PER_HOUR = {"guest": 10, "member": 30, "admin": 120}
GUEST_ANALYZE_RESOLVES_PER_CLIENT_PER_HOUR = 20
_ANALYZE_RESOLVE_WINDOWS: dict[int, deque[float]] = {}
_ANALYZE_RESOLVE_CLIENT_WINDOWS: dict[str, deque[float]] = {}
_ANALYZE_LOCK = threading.Lock()


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _prune_windows(windows: dict[Any, deque[float]], cutoff: float) -> None:
    for key, window in list(windows.items()):
        while window and window[0] <= cutoff:
            window.popleft()
        if not window:
            windows.pop(key, None)


def guest_client_key(peer: str | None, forwarded_for: str | None) -> str | None:
    """The client bucket for per-client guest limits, or None for the host.

    Behind the loopback reverse proxy (Caddy) the client is the last
    ``X-Forwarded-For`` hop: Caddy writes the address of the peer it accepted
    and replaces any value an untrusted client sent.  A direct, non-loopback
    peer is keyed on itself and its forwarding header is ignored.  A loopback
    caller without a forwarding header, or a loopback client address, is a
    process on this host (smoke checks, tests, the operator) and is exempt.
    IPv6 clients are bucketed by /64, the smallest block a host usually gets.
    """
    raw_peer = str(peer or "").strip()
    try:
        peer_ip = ipaddress.ip_address(raw_peer)
    except ValueError:
        peer_ip = None
    if peer_ip is not None and not peer_ip.is_loopback:
        candidate = raw_peer
    else:
        hops = [hop.strip() for hop in str(forwarded_for or "").split(",") if hop.strip()]
        if not hops:
            return None if peer_ip is not None else f"peer:{raw_peer[:64]}"
        candidate = hops[-1]
    try:
        address = ipaddress.ip_address(candidate.strip("[]"))
    except ValueError:
        # One shared bucket for anything that is not an address.
        return "unparsed"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return None
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def guest_sessions_per_client_per_hour() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_SESSIONS_PER_CLIENT_PER_HOUR",
        default=6,
        minimum=1,
        maximum=120,
    )


def guest_runs_per_client_per_hour() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_RUNS_PER_CLIENT_PER_HOUR",
        default=12,
        minimum=1,
        maximum=240,
    )


def guest_client_concurrency() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_CLIENT_CONCURRENCY", default=1, minimum=1, maximum=16
    )


def guest_derived_actions_per_client_per_hour() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_DERIVED_ACTIONS_PER_CLIENT_PER_HOUR",
        default=48,
        minimum=1,
        maximum=480,
    )


def guest_access_enabled() -> bool:
    """Whether visitors may deliberately create a restricted guest session."""
    # An explicit false value must also disable legacy auto-public mode.
    return _env_flag("AGENT_MONITOR_GUEST_ACCESS", default=True) and bool(
        guest_engine_allowlist()
    )


def guest_engine_allowlist() -> frozenset[str]:
    """Guest-safe engines selected by the operator, within a fixed ceiling."""
    raw = os.environ.get("AGENT_MONITOR_GUEST_ENGINES")
    requested = (
        set(GUEST_DEFAULT_ENGINES)
        if raw is None
        else {part.strip().lower() for part in raw.split(",") if part.strip()}
    )
    allowed = requested & GUEST_PROOF_ENGINES
    # A command override changes Plain from the reviewed single-call runner
    # into arbitrary operator-selected local code. Never expose that variant
    # through an anonymous session.
    if os.environ.get("PLAIN_CMD", "").strip():
        allowed.discard("plain")
    if os.environ.get("KIMI_PROOF_CMD", "").strip():
        allowed.discard("kimi")
    from agent_monitor import analyze_runs, readability_runs

    # Analyze and Readability follow their own switches (and an ANALYZE_CMD
    # or READABILITY_CMD override removes one).
    if analyze_runs.guests_enabled():
        allowed.add("analyze")
    if readability_runs.guests_enabled():
        allowed.add("readability")
    return frozenset(allowed)


def guest_max_iterations() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_MAX_ITERATIONS", default=8, minimum=1, maximum=20
    )


def guest_max_output_tokens() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_MAX_OUTPUT_TOKENS",
        default=4096,
        minimum=256,
        maximum=8192,
    )


def guest_runs_per_session() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_RUNS_PER_SESSION", default=10, minimum=1, maximum=10
    )


def guest_runs_per_hour() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_RUNS_PER_HOUR", default=6, minimum=1, maximum=24
    )


def guest_global_runs_per_hour() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_GLOBAL_RUNS_PER_HOUR",
        default=30,
        minimum=1,
        maximum=240,
    )


def guest_verifications_per_hour() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_VERIFICATIONS_PER_HOUR",
        default=6,
        minimum=1,
        maximum=24,
    )


def guest_global_verifications_per_hour() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_GLOBAL_VERIFICATIONS_PER_HOUR",
        default=60,
        minimum=1,
        maximum=240,
    )


def _is_operator_user(user: dict[str, Any]) -> bool:
    """Guest status always overrides a stale/corrupt admin bit."""
    return bool(user.get("is_admin")) and not bool(user.get("guest"))


def guest_policy(user: dict | None = None) -> dict[str, Any]:
    """Client-visible policy; enforcement remains entirely server-side."""
    from agent_monitor.auth import guest_runs_started
    from agent_monitor.settings import GUEST_DEFAULT_MODEL, hosted_guest_kimi_available
    used = max(guest_runs_started(int(user["id"])), job_manager.persisted_run_count(int(user["id"]))) if user and user.get("guest") else 0
    return {
        "engines": sorted(guest_engine_allowlist()),
        "default_engine": "kimi" if "kimi" in guest_engine_allowlist() else "plain",
        "default_model": GUEST_DEFAULT_MODEL,
        "runs_used": used,
        "runs_remaining": max(0, guest_runs_per_session() - used),
        "login_required": used >= guest_runs_per_session(),
        "hosted_guest_available": hosted_guest_kimi_available(),
        "max_iterations": guest_max_iterations(),
        "max_output_tokens": guest_max_output_tokens(),
        # Ansatz CGM (kimi) calls always use this bounded per-call ceiling.
        "cgm_max_output_tokens": job_manager._CGM_BOUNDED_OUTPUT_TOKENS,
        "max_lean_repairs": _guest_lean_repairs(),
        "max_feedback_repairs": _GUEST_REVISE_MAX_REPAIRS,
        "runs_per_session": guest_runs_per_session(),
        "runs_per_hour": guest_runs_per_hour(),
        "provider_verifications_per_hour": guest_verifications_per_hour(),
        "max_problem_characters": 20_000,
        "subagents": False,
        "continuation": False,
        "proof_graph_generation": True,
        "lean_actions": True,
        "formal_harness": False,
        "derived_actions_per_hour": guest_derived_actions_per_hour(),
        "provider": "kimi",
        "custom_provider_base": False,
        "attachments": research_attachments.guest_policy(),
        "analyze": _guest_analyze_summary(),
        "readability": _guest_readability_summary(),
    }


def _guest_analyze_summary() -> dict[str, Any]:
    """What a guest may analyze (the engine listing has the full policy)."""
    from agent_monitor import analyze_runs

    enabled = analyze_runs.guests_enabled()
    return {
        "enabled": enabled,
        "runs_per_hour": GUEST_ANALYZE_RUNS_PER_HOUR,
        "links": sorted(analyze_runs.GUEST_CONNECTORS),
        "paste_chars": analyze_runs.GUEST_PASTE_CHARS,
        "caps": (
            analyze_runs._caps_view(analyze_runs.effective_caps("guest", sponsored=True))
            if enabled else None
        ),
        "asks": enabled and analyze_runs.asks_enabled()
        and not analyze_runs.sponsored_questions_error(guest=True),
        "asks_per_run": analyze_runs.GUEST_ASKS_PER_RUN,
        "asks_per_hour": analyze_runs.GUEST_ASKS_PER_HOUR,
    }


def _guest_readability_summary() -> dict[str, Any]:
    """What a guest may make readable (the engine listing has the full policy)."""
    from agent_monitor import analyze_runs, readability_runs

    enabled = readability_runs.guests_enabled()
    return {
        "enabled": enabled,
        "runs_per_hour": GUEST_READABILITY_RUNS_PER_HOUR,
        "links": sorted(analyze_runs.GUEST_CONNECTORS),
        "paste_chars": analyze_runs.GUEST_PASTE_CHARS,
        "caps": (
            readability_runs._caps_view(readability_runs.effective_caps("guest", sponsored=True))
            if enabled else None
        ),
        "asks_per_run": readability_runs.GUEST_ASKS_PER_RUN,
        "asks_per_hour": readability_runs.GUEST_ASKS_PER_HOUR,
    }


def _guest_lean_repairs() -> int:
    from agent_monitor import lean_verify

    return int(lean_verify.GUEST_MAX_REPAIRS)


def _window_full(window: deque[float] | None, limit: int, now: float, span: float) -> int:
    """Seconds until ``window`` admits another event (0 = admits now)."""
    if window is None:
        return 0
    while window and window[0] <= now - span:
        window.popleft()
    if len(window) >= limit:
        return max(1, int(span - (now - window[0])))
    return 0


def _guest_mode_capacity(
    windows: dict[int, deque[float]],
    client_windows: dict[str, deque[float]],
    user_id: int,
    client: str | None,
    *,
    per_hour: int,
    per_client: int,
    refusals: tuple[str, str],
    reserve: bool,
) -> tuple[str | None, int]:
    """A manuscript mode's guest starts: ``per_hour`` per guest session and
    ``per_client`` per client network each hour (``refusals``: the session's
    and the network's message)."""
    now = time.monotonic()
    with _ANALYZE_LOCK:
        _prune_windows(windows, now - 3600.0)
        _prune_windows(client_windows, now - 3600.0)
        retry = _window_full(windows.get(user_id), per_hour, now, 3600.0)
        if retry:
            return refusals[0], retry
        if client is not None:
            retry = _window_full(client_windows.get(client), per_client, now, 3600.0)
            if retry:
                return refusals[1], retry
        if reserve:
            windows.setdefault(user_id, deque()).append(now)
            if client is not None:
                client_windows.setdefault(client, deque()).append(now)
    return None, 0


def _release_guest_mode(windows: dict[int, deque[float]], client_windows: dict[str, deque[float]],
                        user_id: int, client: str | None) -> None:
    """Return the newest guest start of a mode when the request started nothing."""
    with _ANALYZE_LOCK:
        window = windows.get(user_id)
        if window:
            window.pop()
        if client is not None:
            client_window = client_windows.get(client)
            if client_window:
                client_window.pop()


def _guest_analyze_capacity(user_id: int, client: str | None, *, reserve: bool) -> tuple[str | None, int]:
    """One Analyze per guest session per hour, a few per client network."""
    return _guest_mode_capacity(
        _GUEST_ANALYZE_WINDOWS, _GUEST_ANALYZE_CLIENT_WINDOWS, user_id, client,
        per_hour=GUEST_ANALYZE_RUNS_PER_HOUR, per_client=GUEST_ANALYZE_RUNS_PER_CLIENT_PER_HOUR,
        refusals=("Guests can start one analysis per hour; sign in for more",
                  "Too many guest analyses from your network this hour; please try again later"),
        reserve=reserve,
    )


def _release_guest_analyze(user_id: int, client: str | None) -> None:
    """Return the newest guest Analyze start when the request started nothing."""
    _release_guest_mode(_GUEST_ANALYZE_WINDOWS, _GUEST_ANALYZE_CLIENT_WINDOWS, user_id, client)


def _guest_readability_capacity(user_id: int, client: str | None, *, reserve: bool) -> tuple[str | None, int]:
    """One readability run per guest session per hour, a few per client network."""
    return _guest_mode_capacity(
        _GUEST_READABILITY_WINDOWS, _GUEST_READABILITY_CLIENT_WINDOWS, user_id, client,
        per_hour=GUEST_READABILITY_RUNS_PER_HOUR, per_client=GUEST_READABILITY_RUNS_PER_CLIENT_PER_HOUR,
        refusals=("Guests can start one readability run per hour; sign in for more",
                  "Too many guest readability runs from your network this hour; please try again later"),
        reserve=reserve,
    )


def _release_guest_readability(user_id: int, client: str | None) -> None:
    _release_guest_mode(_GUEST_READABILITY_WINDOWS, _GUEST_READABILITY_CLIENT_WINDOWS, user_id, client)


def _reserve_ask(user: dict[str, Any], client: str | None = None) -> tuple[str | None, int]:
    """Questions about finished manuscript runs (analyses and readable
    versions together) per account and hour, by role, and a guest's per
    client network (``client``) and hour."""
    from agent_monitor import analyze_runs

    role = analyze_runs.role_of(user)
    limit = analyze_runs.ASKS_PER_HOUR.get(role, analyze_runs.GUEST_ASKS_PER_HOUR)
    now = time.monotonic()
    user_id = int(user.get("id") or 0)
    with _ANALYZE_LOCK:
        _prune_windows(_ASK_WINDOWS, now - 3600.0)
        _prune_windows(_ASK_CLIENT_WINDOWS, now - 3600.0)
        retry = _window_full(_ASK_WINDOWS.get(user_id), limit, now, 3600.0)
        if retry:
            return f"You can ask {limit} questions per hour; please try again later", retry
        if client is not None:
            retry = _window_full(_ASK_CLIENT_WINDOWS.get(client), GUEST_ASKS_PER_CLIENT_PER_HOUR, now, 3600.0)
            if retry:
                return "Too many guest questions from your network this hour; please try again later", retry
        _ASK_WINDOWS.setdefault(user_id, deque()).append(now)
        if client is not None:
            _ASK_CLIENT_WINDOWS.setdefault(client, deque()).append(now)
    return None, 0


def _release_ask(user: dict[str, Any], client: str | None = None) -> None:
    """Return the newest question when the request sent nothing."""
    with _ANALYZE_LOCK:
        window = _ASK_WINDOWS.get(int(user.get("id") or 0))
        if window:
            window.pop()
        if client is not None:
            client_window = _ASK_CLIENT_WINDOWS.get(client)
            if client_window:
                client_window.pop()


def _reserve_guest_recheck(user_id: int, client: str | None) -> tuple[str | None, int]:
    """Guest re-checks per hour: a few per guest session and per client network."""
    now = time.monotonic()
    with _ANALYZE_LOCK:
        _prune_windows(_GUEST_RECHECK_WINDOWS, now - 3600.0)
        _prune_windows(_GUEST_RECHECK_CLIENT_WINDOWS, now - 3600.0)
        retry = _window_full(_GUEST_RECHECK_WINDOWS.get(user_id), GUEST_RECHECKS_PER_HOUR, now, 3600.0)
        if retry:
            return (f"Guests can re-check {GUEST_RECHECKS_PER_HOUR} results per hour; sign in for more",
                    retry)
        if client is not None:
            retry = _window_full(_GUEST_RECHECK_CLIENT_WINDOWS.get(client),
                                 GUEST_RECHECKS_PER_CLIENT_PER_HOUR, now, 3600.0)
            if retry:
                return "Too many guest re-checks from your network this hour; please try again later", retry
        _GUEST_RECHECK_WINDOWS.setdefault(user_id, deque()).append(now)
        if client is not None:
            _GUEST_RECHECK_CLIENT_WINDOWS.setdefault(client, deque()).append(now)
    return None, 0


def _release_guest_recheck(user_id: int, client: str | None) -> None:
    """Return the newest guest re-check when the request started nothing."""
    with _ANALYZE_LOCK:
        window = _GUEST_RECHECK_WINDOWS.get(user_id)
        if window:
            window.pop()
        if client is not None:
            client_window = _GUEST_RECHECK_CLIENT_WINDOWS.get(client)
            if client_window:
                client_window.pop()


def _reserve_analyze_resolve(user: dict[str, Any], client: str | None) -> tuple[str | None, int]:
    """Link previews per hour: guests 10 (and 20 per network), members 30."""
    from agent_monitor import analyze_runs

    role = analyze_runs.role_of(user)
    limit = ANALYZE_RESOLVES_PER_HOUR.get(role, 10)
    now = time.monotonic()
    user_id = int(user.get("id") or 0)
    with _ANALYZE_LOCK:
        _prune_windows(_ANALYZE_RESOLVE_WINDOWS, now - 3600.0)
        _prune_windows(_ANALYZE_RESOLVE_CLIENT_WINDOWS, now - 3600.0)
        retry = _window_full(_ANALYZE_RESOLVE_WINDOWS.get(user_id), limit, now, 3600.0)
        if retry:
            return "You have previewed many links this hour; please try again later", retry
        if role == "guest" and client is not None:
            retry = _window_full(_ANALYZE_RESOLVE_CLIENT_WINDOWS.get(client),
                                 GUEST_ANALYZE_RESOLVES_PER_CLIENT_PER_HOUR, now, 3600.0)
            if retry:
                return "Too many link previews from your network this hour; please try again later", retry
        _ANALYZE_RESOLVE_WINDOWS.setdefault(user_id, deque()).append(now)
        if role == "guest" and client is not None:
            _ANALYZE_RESOLVE_CLIENT_WINDOWS.setdefault(client, deque()).append(now)
    return None, 0


def analyze_policy(user: dict | None) -> dict[str, Any]:
    """The viewer's Analyze policy (engine listing, guest policy)."""
    from agent_monitor import analyze_runs

    return analyze_runs.viewer_policy(user, guest_runs_per_hour=GUEST_ANALYZE_RUNS_PER_HOUR)


def readability_policy(user: dict | None) -> dict[str, Any]:
    """The viewer's Readability policy (engine listing)."""
    from agent_monitor import readability_runs

    return readability_runs.viewer_policy(user, guest_runs_per_hour=GUEST_READABILITY_RUNS_PER_HOUR)


def _guest_run_capacity_error(user_id: int, client: str | None = None) -> str | None:
    from agent_monitor.auth import guest_runs_started
    if max(guest_runs_started(user_id), job_manager.persisted_run_count(user_id)) >= guest_runs_per_session():
        return f"You have used all {guest_runs_per_session()} guest runs. Sign in to continue"
    active_states = {"queued", "starting", "running", "continuing"}
    active = [
        job
        for job in job_manager.list_jobs()
        if str(job.get("status") or "").lower() in active_states
    ]
    active_run_ids = {str(job.get("run_id") or "") for job in active}
    for run_id in list(_GUEST_RUN_CLIENTS):
        if run_id not in active_run_ids:
            _GUEST_RUN_CLIENTS.pop(run_id, None)
    per_guest_limit = _bounded_env_int(
        "AGENT_MONITOR_GUEST_CONCURRENCY", default=1, minimum=1, maximum=4
    )
    if sum(job.get("owner_id") == user_id for job in active) >= per_guest_limit:
        return "Wait for your current guest run to finish before starting another"
    if client is not None and (
        sum(owner == client for owner in _GUEST_RUN_CLIENTS.values())
        >= guest_client_concurrency()
    ):
        return "Wait for the current guest run from your network to finish before starting another"
    global_limit = _bounded_env_int(
        "AGENT_MONITOR_GUEST_GLOBAL_CONCURRENCY", default=2, minimum=1, maximum=16
    )
    if sum(bool(job.get("guest")) for job in active) >= global_limit:
        return "Guest run capacity is currently full; please try again later"
    return None


def _reserve_guest_run_slot(user_id: int, client: str | None = None) -> tuple[str | None, int]:
    """Atomically enforce active, per-session, per-client and rolling guest run limits."""
    now = time.monotonic()
    cutoff = now - _GUEST_RUN_WINDOW_SECONDS
    with _GUEST_RUN_START_LOCK:
        for existing_id, window in list(_GUEST_RUN_WINDOWS.items()):
            while window and window[0] <= cutoff:
                window.popleft()
            if not window:
                _GUEST_RUN_WINDOWS.pop(existing_id, None)
        while _GUEST_GLOBAL_RUN_WINDOW and _GUEST_GLOBAL_RUN_WINDOW[0] <= cutoff:
            _GUEST_GLOBAL_RUN_WINDOW.popleft()
        _prune_windows(_GUEST_RUN_CLIENT_WINDOWS, now - _GUEST_CLIENT_WINDOW_SECONDS)

        capacity_error = _guest_run_capacity_error(user_id, client)
        if capacity_error:
            return capacity_error, 30
        user_window = _GUEST_RUN_WINDOWS.setdefault(user_id, deque())
        if len(user_window) >= guest_runs_per_hour():
            retry = max(1, int(_GUEST_RUN_WINDOW_SECONDS - (now - user_window[0])))
            return "This guest session has reached its hourly run limit", retry
        client_window = _GUEST_RUN_CLIENT_WINDOWS.get(client) if client is not None else None
        if client_window and len(client_window) >= guest_runs_per_client_per_hour():
            retry = max(1, int(_GUEST_CLIENT_WINDOW_SECONDS - (now - client_window[0])))
            return "Too many guest runs from your network this hour; please try again later", retry
        if len(_GUEST_GLOBAL_RUN_WINDOW) >= guest_global_runs_per_hour():
            retry = max(
                1,
                int(
                    _GUEST_RUN_WINDOW_SECONDS
                    - (now - _GUEST_GLOBAL_RUN_WINDOW[0])
                ),
            )
            return "Guest run capacity is currently full; please try again later", retry

        user_window.append(now)
        _GUEST_GLOBAL_RUN_WINDOW.append(now)
        if client is not None:
            _GUEST_RUN_CLIENT_WINDOWS.setdefault(client, deque()).append(now)
    return None, 0


def _release_guest_run_slot(user_id: int, client: str | None = None) -> None:
    """Give back the hourly guest run slots reserved for a start that started nothing.

    Callers hold ``_GUEST_RUN_START_LOCK`` from the reservation on, so the
    newest entries are this start's.
    """
    with _GUEST_RUN_START_LOCK:
        window = _GUEST_RUN_WINDOWS.get(user_id)
        if window:
            window.pop()
        if _GUEST_GLOBAL_RUN_WINDOW:
            _GUEST_GLOBAL_RUN_WINDOW.pop()
        if client is not None:
            client_window = _GUEST_RUN_CLIENT_WINDOWS.get(client)
            if client_window:
                client_window.pop()


def _reserve_guest_session_slot(client: str | None = None) -> tuple[bool, int]:
    """Bound anonymous account creation; existing guest sessions bypass this."""
    limit = _bounded_env_int(
        "AGENT_MONITOR_GUEST_SESSIONS_PER_MINUTE",
        default=10,
        minimum=1,
        maximum=120,
    )
    now = time.monotonic()
    with _GUEST_SESSION_LOCK:
        cutoff = now - _GUEST_SESSION_WINDOW_SECONDS
        while _GUEST_SESSION_WINDOW and _GUEST_SESSION_WINDOW[0] <= cutoff:
            _GUEST_SESSION_WINDOW.popleft()
        if client is not None:
            # Checked first so one client cannot hold the site-wide minute
            # window: it gets only a few new identities per hour.
            _prune_windows(_GUEST_SESSION_CLIENT_WINDOWS, now - _GUEST_CLIENT_WINDOW_SECONDS)
            client_window = _GUEST_SESSION_CLIENT_WINDOWS.get(client)
            if client_window and len(client_window) >= guest_sessions_per_client_per_hour():
                retry = max(1, int(_GUEST_CLIENT_WINDOW_SECONDS - (now - client_window[0])))
                return False, retry
        if len(_GUEST_SESSION_WINDOW) >= limit:
            retry = max(1, int(_GUEST_SESSION_WINDOW_SECONDS - (now - _GUEST_SESSION_WINDOW[0])))
            return False, retry
        _GUEST_SESSION_WINDOW.append(now)
        if client is not None:
            _GUEST_SESSION_CLIENT_WINDOWS.setdefault(client, deque()).append(now)
    return True, 0


def _reserve_signup_slot(client: str | None = None) -> tuple[bool, int]:
    """Bound new registered accounts per client address and site-wide."""
    per_client = _bounded_env_int(
        "AGENT_MONITOR_SIGNUPS_PER_CLIENT_PER_HOUR", default=5, minimum=1, maximum=100
    )
    site_wide = _bounded_env_int(
        "AGENT_MONITOR_SIGNUPS_PER_HOUR", default=60, minimum=1, maximum=2000
    )
    now = time.monotonic()
    cutoff = now - _SIGNUP_WINDOW_SECONDS
    with _SIGNUP_LOCK:
        while _SIGNUP_GLOBAL_WINDOW and _SIGNUP_GLOBAL_WINDOW[0] <= cutoff:
            _SIGNUP_GLOBAL_WINDOW.popleft()
        _prune_windows(_SIGNUP_CLIENT_WINDOWS, cutoff)
        client_window = _SIGNUP_CLIENT_WINDOWS.get(client) if client is not None else None
        if client_window and len(client_window) >= per_client:
            return False, max(1, int(_SIGNUP_WINDOW_SECONDS - (now - client_window[0])))
        if len(_SIGNUP_GLOBAL_WINDOW) >= site_wide:
            return False, max(1, int(_SIGNUP_WINDOW_SECONDS - (now - _SIGNUP_GLOBAL_WINDOW[0])))
        _SIGNUP_GLOBAL_WINDOW.append(now)
        if client is not None:
            _SIGNUP_CLIENT_WINDOWS.setdefault(client, deque()).append(now)
    return True, 0


def _reserve_member_run_slot(user_id: int) -> tuple[str | None, int, float | None]:
    """Bound a member's concurrent and hourly run starts (continuations count).

    Returns the refusal and its Retry-After seconds, or (None, 0, booking):
    the start's time in the member's hourly window, which gives back exactly
    this start (_release_member_run_slot)."""
    concurrency = _bounded_env_int(
        "AGENT_MONITOR_MEMBER_CONCURRENCY", default=2, minimum=1, maximum=8
    )
    per_hour = _bounded_env_int(
        "AGENT_MONITOR_MEMBER_RUNS_PER_HOUR", default=12, minimum=1, maximum=240
    )
    now = time.monotonic()
    with _MEMBER_RUN_LOCK:
        active_states = {"queued", "starting", "running", "continuing"}
        active = sum(
            job.get("owner_id") == user_id
            and str(job.get("status") or "").lower() in active_states
            for job in job_manager.list_jobs()
        )
        if active >= concurrency:
            return "Wait for one of your current runs to finish before starting another", 30, None
        window = _MEMBER_RUN_WINDOWS.setdefault(user_id, deque())
        while window and window[0] <= now - _MEMBER_RUN_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= per_hour:
            return (
                "Your account has reached its hourly run limit; please try again later",
                max(1, int(_MEMBER_RUN_WINDOW_SECONDS - (now - window[0]))),
                None,
            )
        window.append(now)
    return None, 0, now


def _member_subscription_status(kind: str) -> dict[str, Any]:
    """Login status for a member, who may not link a Codex or Claude account.

    Answered without the CLI status probe, so the member gets no account home.
    """
    status: dict[str, Any] = {
        "status": "admin_only",
        "running": False,
        "connected": False,
        "policy": "admin_only",
        "error": account_policy.subscription_notes()[kind],
    }
    if kind == "codex":
        from agent_monitor import codex_login

        status["codex_installed"] = codex_login.codex_binary() is not None
    else:
        from agent_monitor import claude_login

        status.update(
            claude_installed=claude_login.claude_binary() is not None,
            reauth_required=False,
            models=[],
        )
    return status


def _remove_member_stored_login(kind: str, user_id: int) -> None:
    """Delete a sign-in a member linked before subscriptions became admin-only.

    Only the credential file that Settings reports as stored is removed: no
    CLI runs, and no account home is created.
    """
    if kind == "codex":
        from agent_monitor import codex_login

        credentials = codex_login.account_home(user_id) / "auth.json"
    else:
        from agent_monitor import claude_login

        credentials = claude_login.account_home(user_id) / ".credentials.json"
    try:
        credentials.unlink()
    except FileNotFoundError:
        pass


def _release_member_run_slot(user_id: int, booking: float | None) -> None:
    """Give back a member's run start booked at ``booking``
    (_reserve_member_run_slot) that started nothing.  Nothing once it has
    left the hourly window: another start booked since must stay booked."""
    with _MEMBER_RUN_LOCK:
        window = _MEMBER_RUN_WINDOWS.get(user_id)
        if window and booking is not None and booking in window:
            window.remove(booking)


def _reserve_member_message(user_id: int) -> tuple[str | None, int]:
    """Bound how often a member sends run feedback, queued messages included."""
    per_hour = _bounded_env_int(
        "AGENT_MONITOR_MEMBER_MESSAGES_PER_HOUR", default=60, minimum=1, maximum=600
    )
    now = time.monotonic()
    with _MEMBER_RUN_LOCK:
        window = _MEMBER_MESSAGE_WINDOWS.setdefault(user_id, deque())
        while window and window[0] <= now - _MEMBER_RUN_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= per_hour:
            return (
                "Your account has sent its hourly limit of run feedback; please try again later",
                max(1, int(_MEMBER_RUN_WINDOW_SECONDS - (now - window[0]))),
            )
        window.append(now)
    return None, 0


def _reserve_guest_verification(user_id: int) -> tuple[str | None, int]:
    """Bound slow outbound key checks by guest and across the service."""
    now = time.monotonic()
    cutoff = now - _GUEST_VERIFY_WINDOW_SECONDS
    with _GUEST_VERIFY_LOCK:
        for existing_id, window in list(_GUEST_VERIFY_WINDOWS.items()):
            while window and window[0] <= cutoff:
                window.popleft()
            if not window:
                _GUEST_VERIFY_WINDOWS.pop(existing_id, None)
        while _GUEST_VERIFY_GLOBAL_WINDOW and _GUEST_VERIFY_GLOBAL_WINDOW[0] <= cutoff:
            _GUEST_VERIFY_GLOBAL_WINDOW.popleft()

        if user_id in _GUEST_VERIFY_ACTIVE_USERS:
            return "Wait for the current key verification to finish", 20
        global_concurrency = _bounded_env_int(
            "AGENT_MONITOR_GUEST_VERIFY_GLOBAL_CONCURRENCY",
            default=4,
            minimum=1,
            maximum=16,
        )
        if len(_GUEST_VERIFY_ACTIVE_USERS) >= global_concurrency:
            return "Guest verification capacity is currently full", 20

        user_window = _GUEST_VERIFY_WINDOWS.setdefault(user_id, deque())
        if len(user_window) >= guest_verifications_per_hour():
            retry = max(
                1,
                int(_GUEST_VERIFY_WINDOW_SECONDS - (now - user_window[0])),
            )
            return "This guest session has reached its verification limit", retry
        if len(_GUEST_VERIFY_GLOBAL_WINDOW) >= guest_global_verifications_per_hour():
            retry = max(
                1,
                int(
                    _GUEST_VERIFY_WINDOW_SECONDS
                    - (now - _GUEST_VERIFY_GLOBAL_WINDOW[0])
                ),
            )
            return "Guest verification capacity is currently full", retry

        user_window.append(now)
        _GUEST_VERIFY_GLOBAL_WINDOW.append(now)
        _GUEST_VERIFY_ACTIVE_USERS.add(user_id)
    return None, 0


def _release_guest_verification(user_id: int) -> None:
    with _GUEST_VERIFY_LOCK:
        _GUEST_VERIFY_ACTIVE_USERS.discard(user_id)


def guest_derived_actions_per_hour() -> int:
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_DERIVED_ACTIONS_PER_HOUR",
        default=24,
        minimum=1,
        maximum=60,
    )


def _reserve_guest_derived_action(user_id: int, client: str | None = None) -> tuple[str | None, int]:
    """Bound Formal/DAG work (and its background refresh) independently from key checks."""
    now = time.monotonic()
    cutoff = now - _GUEST_DERIVED_WINDOW_SECONDS
    with _GUEST_DERIVED_LOCK:
        for existing_id, window in list(_GUEST_DERIVED_WINDOWS.items()):
            while window and window[0] <= cutoff:
                window.popleft()
            if not window:
                _GUEST_DERIVED_WINDOWS.pop(existing_id, None)
        while _GUEST_DERIVED_GLOBAL_WINDOW and _GUEST_DERIVED_GLOBAL_WINDOW[0] <= cutoff:
            _GUEST_DERIVED_GLOBAL_WINDOW.popleft()
        _prune_windows(_GUEST_DERIVED_CLIENT_WINDOWS, now - _GUEST_CLIENT_WINDOW_SECONDS)
        if user_id in _GUEST_DERIVED_ACTIVE_USERS:
            return "Wait for your current Formal or DAG action to finish", 20
        concurrency = _bounded_env_int(
            "AGENT_MONITOR_GUEST_DERIVED_GLOBAL_CONCURRENCY",
            default=2,
            minimum=1,
            maximum=8,
        )
        if len(_GUEST_DERIVED_ACTIVE_USERS) >= concurrency:
            return "Guest Formal and DAG capacity is currently full", 20
        global_limit = _bounded_env_int(
            "AGENT_MONITOR_GUEST_DERIVED_GLOBAL_ACTIONS_PER_HOUR",
            default=120,
            minimum=1,
            maximum=480,
        )
        # Check the global ceiling before allocating a new visitor bucket.
        if len(_GUEST_DERIVED_GLOBAL_WINDOW) >= global_limit:
            retry = max(1, int(_GUEST_DERIVED_WINDOW_SECONDS - (now - _GUEST_DERIVED_GLOBAL_WINDOW[0])))
            return "Guest Formal and DAG capacity is currently full", retry
        user_window = _GUEST_DERIVED_WINDOWS.get(user_id)
        if user_window and len(user_window) >= guest_derived_actions_per_hour():
            retry = max(1, int(_GUEST_DERIVED_WINDOW_SECONDS - (now - user_window[0])))
            return "This guest session has reached its hourly Formal and DAG limit", retry
        client_window = _GUEST_DERIVED_CLIENT_WINDOWS.get(client) if client is not None else None
        if client_window and len(client_window) >= guest_derived_actions_per_client_per_hour():
            retry = max(1, int(_GUEST_CLIENT_WINDOW_SECONDS - (now - client_window[0])))
            return "Too many guest Formal and DAG actions from your network this hour", retry
        _GUEST_DERIVED_WINDOWS.setdefault(user_id, deque()).append(now)
        _GUEST_DERIVED_GLOBAL_WINDOW.append(now)
        if client is not None:
            _GUEST_DERIVED_CLIENT_WINDOWS.setdefault(client, deque()).append(now)
        _GUEST_DERIVED_ACTIVE_USERS.add(user_id)
    return None, 0


def _release_guest_derived_action(user_id: int) -> None:
    with _GUEST_DERIVED_LOCK:
        _GUEST_DERIVED_ACTIVE_USERS.discard(user_id)


class _GuestDerivedSlot:
    """One admitted guest Formal/DAG action; released exactly once.

    The slot stays held while any model work the action scheduled is still
    running (the formal DAG refresh after a Lean action runs on a background
    thread), so the per-guest and site-wide concurrency limits cover it.
    """

    def __init__(self, user_id: int) -> None:
        self.user_id = int(user_id)
        self.handed_off = False
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        _release_guest_derived_action(self.user_id)


_MANUAL_MAX_REPAIRS = 6
_REVISE_MAX_REPAIRS = 3
# lean_verify.revise_with_feedback clamps every guest feedback revision to one
# repair round; mirror it here so the console never promises more.
_GUEST_REVISE_MAX_REPAIRS = 1


def _bounded_repairs(
    requested: object,
    *,
    default: int,
    cap: int,
    guest: bool,
    guest_cap: int | None = None,
) -> int:
    """Clamp a manual Lean repair budget; guests never exceed the guest cap."""
    from agent_monitor import lean_verify

    if requested is None:
        value = default
    else:
        if isinstance(requested, bool):
            raise ValueError("max_repairs must be an integer")
        try:
            value = int(requested)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("max_repairs must be an integer") from None
    if guest:
        cap = min(
            cap,
            lean_verify.GUEST_MAX_REPAIRS if guest_cap is None else guest_cap,
        )
    return max(0, min(value, cap))


# The Lean toolchain status a guest may read (GET /api/lean/status): broker
# availability and the fixed pinned toolchain, never host paths.
_GUEST_LEAN_STATUS_FIELDS = (
    "available",
    "mathlib_available",
    "version",
    "checker_backend",
    "checker_sandbox",
)


def _guest_formal_options() -> dict[str, Any]:
    """Guests use the hosted API, with no local harness discovery or fallback."""
    from agent_monitor.settings import GUEST_DEFAULT_MODEL

    return {
        "engines": [],
        "harness_auth_route": "api_key",
        "harness_models": [GUEST_DEFAULT_MODEL],
        "harness_model": GUEST_DEFAULT_MODEL,
        "harness_engine": "",
        "harness_available": False,
    }


def _bootstrap_guest_pipeline(
    user: dict[str, Any], run_id: str, workspace: Path, run_record: dict[str, Any],
) -> dict[str, Any] | None:
    """Derive an old guest proof once when its owner opens that completed run."""
    from agent_monitor import auto_pipeline, sponsored_kimi_client
    from agent_monitor.settings import GUEST_DEFAULT_MODEL, hosted_guest_kimi_available

    state = auto_pipeline.load_state(workspace)
    if (
        state is not None
        or not user.get("guest")
        or auto_pipeline.is_readability_run(run_record)
        or not run_record.get("guest")
        or run_record.get("owner_id") != user.get("id")
        or str(run_record.get("status") or "").lower() != "finished"
        or os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "").strip() == "1"
    ):
        return state
    # The existing bounded reader also recognizes a substantial final answer
    # for successful legacy runs that did not persist a proof.md file.
    if not auto_pipeline._source_signature(workspace, run_record):
        return None
    if not hosted_guest_kimi_available() or not sponsored_kimi_client.configured():
        return None
    with _GUEST_PIPELINE_BOOTSTRAP_LOCK:
        # Polling or simultaneous tabs must never queue a second sidecar.
        state = auto_pipeline.load_state(workspace)
        if state is not None:
            return state
        for active_id, (_owner, active_workspace) in list(_GUEST_PIPELINE_BOOTSTRAPS.items()):
            active_state = auto_pipeline.load_state(active_workspace) or {}
            if active_state.get("status") != "running":
                _GUEST_PIPELINE_BOOTSTRAPS.pop(active_id, None)
        owner_id = int(user["id"])
        if len(_GUEST_PIPELINE_BOOTSTRAPS) >= 2 or any(
            owner == owner_id for owner, _ws in _GUEST_PIPELINE_BOOTSTRAPS.values()
        ):
            return None
        error, _retry_after = _reserve_guest_derived_action(owner_id)
        if error:
            return None
        try:
            state = auto_pipeline.start(
                run_id=run_id,
                workspace=workspace,
                owner_id=owner_id,
                model=GUEST_DEFAULT_MODEL,
                engine=str(run_record.get("engine") or "kimi"),
            )
            if state.get("status") == "running":
                _GUEST_PIPELINE_BOOTSTRAPS[run_id] = (owner_id, workspace)
            return state
        except Exception as exc:  # noqa: BLE001
            logger.warning("Guest pipeline bootstrap failed (%s)", type(exc).__name__)
            return None
        finally:
            _release_guest_derived_action(owner_id)


def _guest_run_completed(run_id: str, owner_id: int, workspace: Path) -> None:
    """Start a finished guest run's bounded derived pipeline immediately.

    Uses exactly the open-time bootstrap (sponsored Kimi only, derived-action
    rate limit, at most two concurrent guest sidecars); opening the run later
    remains the fallback when capacity was full at completion time.
    """
    record = _run_record_for(run_id)
    ws = _workspace_for_run(run_id)
    if not record or ws is None or ws != Path(workspace).resolve():
        return
    _bootstrap_guest_pipeline(
        {"id": int(owner_id), "guest": True}, run_id, ws, record
    )


job_manager.set_guest_completion_hook(_guest_run_completed)


def _with_base(path: str, base_path: str | None = None) -> str:
    """Prefix an app-absolute path for redirects and browser navigation."""
    if not path.startswith("/"):
        path = "/" + path
    prefix = BASE_PATH if base_path is None else _normalize_base_path(base_path)
    return f"{prefix}{path}" if prefix else path


def _inject_base(html: str, base_path: str | None = None) -> str:
    """Inject window.__PC_BASE__ so frontends rewrite absolute /api URLs."""
    prefix = BASE_PATH if base_path is None else _normalize_base_path(base_path)
    snip = (
        "<script>"
        f"window.__PC_BASE__={json.dumps(prefix)};"
        f"window.__PC_PUBLIC__={json.dumps(PUBLIC_MODE)};"
        "(function(){"
        "var B=window.__PC_BASE__||'';"
        "if(!B)return;"
        "function u(p){"
        "if(typeof p!=='string')return p;"
        "if(/^https?:\\/\\//i.test(p)||p.startsWith('//')||p.startsWith('data:')||p.startsWith('blob:'))return p;"
        "if(p===B||p.startsWith(B+'/'))return p;"
        "if(p.startsWith('/'))return B+p;"
        "return p;"
        "}"
        "window.__pcUrl=u;"
        "var _f=window.fetch.bind(window);"
        "window.fetch=function(input,init){"
        "if(typeof input==='string')input=u(input);"
        "else if(input&&typeof Request!=='undefined'&&input instanceof Request)"
        "input=new Request(u(input.url),input);"
        "return _f(input,init);"
        "};"
        "})();"
        "</script>"
    )
    lower = html.lower()
    idx = lower.find("<head>")
    if idx >= 0:
        insert_at = idx + len("<head>")
        return html[:insert_at] + snip + html[insert_at:]
    return snip + html


_PROOF_PREFIX_RE = re.compile(r"^(/proof-[0-9a-fA-F]+)(?=/|$)")


def _app_path(raw_path: str, configured_base_path: str) -> tuple[str | None, str]:
    """Resolve an app path and its effective mount prefix.

    A configured base path remains a strict access gate. When no base path is
    configured, a component-bounded ``/proof-<hex>`` prefix is also supported
    so reverse proxies can mount the same app without mutating process-global
    state in this threaded server.
    """
    path = raw_path or "/"
    base_path = _normalize_base_path(configured_base_path)
    if base_path:
        if path == "/health":
            return path, ""
        if path == base_path or path.startswith(base_path + "/"):
            return path[len(base_path) :] or "/", base_path
        return None, ""

    match = _PROOF_PREFIX_RE.match(path)
    if match:
        prefix = match.group(1)
        return path[len(prefix) :] or "/", prefix
    return path, ""


def _canonical_app_base(raw_url: str, request_base_path: str) -> str | None:
    """Validate the configured public app URL and attach its mount if needed."""
    value = (raw_url or "").strip()
    if not value or any(ord(ch) < 32 for ch in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    path = _normalize_base_path(parsed.path)
    if not path:
        path = _normalize_base_path(request_base_path)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", "")).rstrip("/")


def _cache_harness() -> Path:
    return Path(os.environ.get("LLM_DASHBOARD_CACHE", str(CACHE_DIR))) / "harness"


_MAX_RUN_RECORD_BYTES = 4_000_000


def _read_run_record_at(parent: Path, name: str) -> dict | None:
    """Bounded, no-follow read of one run record beneath an expected parent."""
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(os.fspath(parent), directory_flags)
    except OSError:
        return None
    try:
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except OSError:
            return None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_RUN_RECORD_BYTES:
                return None
            chunks: list[bytes] = []
            remaining = _MAX_RUN_RECORD_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(fd)
            if len(payload) > _MAX_RUN_RECORD_BYTES:
                return None
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                return None
        except OSError:
            return None
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (
        json.JSONDecodeError,
        RecursionError,
        UnicodeDecodeError,
        ValueError,
    ):
        return None
    return value if isinstance(value, dict) else None


def _derived_record_for(run_id: str) -> dict | None:
    """The run record as Lean and graph work sees it.

    A Max run (Claude Code + Codex) does derived work on its worker route, the
    linked Codex account, and a Max (API) run on its worker's API model and
    key, both at high effort; a Codex-subscription or API-key run made at
    extra-high or maximum effort derives at high too.  See cgm_max.formal_view.
    """
    from agent_monitor import cgm_max

    return cgm_max.formal_view(_run_record_for(run_id))


def _run_record_for(run_id: str) -> dict | None:
    """The bounded cached run record for a validated run id, if readable."""
    if (
        not run_id
        or len(run_id) > 240
        or run_id in {".", ".."}
        or ".." in run_id
        or "/" in run_id
        or "\\" in run_id
        or not all(char.isascii() and (char.isalnum() or char in "_.-") for char in run_id)
    ):
        return None
    name = f"{run_id}.json"
    for parent in (_cache_harness(), RUNS_DIR):
        value = _read_run_record_at(Path(parent), name)
        if value is not None:
            return value
    return None


def _preferred_formal_harness_engine(
    run_record: dict | None,
    engines: list[dict],
) -> str:
    """Choose a route-compatible Formal harness without reviving stale state."""
    recorded = str((run_record or {}).get("engine") or "").strip()
    available = [
        str(item.get("id") or "")
        for item in engines
        if item.get("available") and str(item.get("id") or "")
    ]
    if recorded in available:
        return recorded
    return available[0] if available else ""


def _pipeline_may_run_lean(workspace: Path) -> bool:
    """Whether the run's automatic pipeline is running with formal stages on."""
    from agent_monitor import auto_pipeline

    state = auto_pipeline.load_state(workspace) or {}
    lean = (state.get("stages") or {}).get("lean") or {}
    return state.get("status") == "running" and lean.get("status") != "off"


WORKSPACES_ROOT = (RUNS_DIR / "workspaces").resolve()
_TEXT_SUFFIXES = {".tex", ".txt", ".md", ".py", ".json", ".yaml", ".yml", ".log", ".sty", ".bib"}

_PREVIEW_CACHE: dict[str, str] = {}


def _problem_preview_for(run_id: str | None, cache_dir: Path) -> str:
    """Backfill a short problem title for manifest entries written before
    problem_preview existed. Reads the run's problem.txt / run record once."""
    if not run_id or "/" in run_id or ".." in run_id:
        return ""
    if run_id in _PREVIEW_CACHE:
        return _PREVIEW_CACHE[run_id]
    text = ""
    ws = _workspace_for_run(run_id)
    if ws and (ws / "problem.txt").exists():
        try:
            text = (ws / "problem.txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if not text:
        rec = cache_dir / f"{run_id}.json"
        if rec.exists():
            try:
                run = json.loads(rec.read_text(encoding="utf-8"))
                text = run.get("problem_text_preview") or ""
                if not text:
                    agents = run.get("agents") or []
                    text = str(agents[0].get("prompt") or "") if agents else ""
            except (json.JSONDecodeError, OSError):
                text = ""
    preview = next((ln.strip()[:140] for ln in text.splitlines() if ln.strip()), "")
    _PREVIEW_CACHE[run_id] = preview
    return preview


def _workspace_for_run(run_id: str) -> Path | None:
    """Resolve a run's workspace dir, guarding against path escape."""
    if not run_id or "/" in run_id or ".." in run_id:
        return None
    ws = (WORKSPACES_ROOT / run_id).resolve()
    root = str(WORKSPACES_ROOT)
    if ws != WORKSPACES_ROOT and not str(ws).startswith(root + os.sep):
        return None
    return ws if ws.is_dir() else None


def _file_revision(path: Path) -> str:
    if not path.is_file():
        return ""
    path_stat = path.stat()
    return f"{path_stat.st_mtime_ns}:{path_stat.st_size}"


def _contained_path(root: Path, relative: str) -> Path | None:
    """Resolve a relative path only when its real target stays in ``root``.

    Rejects absolute paths and embedded NUL bytes before touching the
    filesystem, then requires the fully resolved target to stay under ``root``
    so a symlink cannot lead out of it.
    """
    if not relative or "\x00" in relative or Path(relative).is_absolute():
        return None
    resolved_root = root.resolve()
    try:
        target = (resolved_root / relative).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return target if target.is_relative_to(resolved_root) else None


def _workspace_target(workspace: Path, relative: str) -> Path | None:
    """Resolve a path only when its real target stays inside ``workspace``."""
    return _contained_path(workspace, relative)


def _static_asset(relative: str) -> Path | None:
    """Return a real file from an approved static root, never a traversal."""
    for root in (WEB_DIR, DASHBOARD_WEB):
        target = _workspace_target(root, relative)
        if target is not None and target.is_file():
            return target
    return None


def _save_sandbox_proof(
    workspace: Path,
    *,
    content: str,
    expected_revision: str = "",
    force: bool = False,
) -> tuple[int, dict]:
    """Conflict-safe, atomic proof.md save used by the Notion-style editor."""
    encoded = content.encode("utf-8")
    if len(encoded) > 400_000:
        return 413, {"error": "proof.md is limited to 400 KB"}
    target = workspace / "proof.md"
    current_revision = _file_revision(target) or "missing"
    if expected_revision and expected_revision != current_revision and not force:
        current = ""
        if target.is_file():
            current = target.read_text(encoding="utf-8", errors="replace")[:400_000]
        return 409, {
            "error": "proof.md changed since editing began",
            "conflict": True,
            "revision": current_revision,
            "content": current,
        }
    tmp = workspace / f".proof.md.{os.getpid()}.{id(content)}.tmp"
    try:
        tmp.write_bytes(encoded)
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    target_stat = target.stat()
    return 200, {
        "ok": True,
        "path": "proof.md",
        "revision": _file_revision(target),
        "mtime": target_stat.st_mtime,
        "size": target_stat.st_size,
    }


def _live_writes(data: dict) -> str:
    """The latest non-tool agent writes of a run (its live Sandbox text)."""
    writes: list[str] = []
    longest = ""
    for agent in data.get("agents") or []:
        out = str(agent.get("output") or "").strip()
        if not out:
            continue
        if len(out) > len(longest):
            longest = out
        role = str(agent.get("role") or "").lower()
        stage = str(agent.get("pipeline_stage") or "").lower()
        if role in {"tools", "tool", "exec"} or stage == "act":
            continue
        if out.lower().startswith("edited:"):
            continue
        writes.append(out)
    return "\n\n---\n\n".join(writes[-12:]) if writes else longest


def _https_link(value: object) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith("https://") and len(text) <= 2000 else None


# The Analyze runner's own progress notes start with this prefix ("Analyze ·
# Check · Lemma 3.7 · error — …"); a bare call stage after it ("map",
# "check-3", "check-3-low", "segment-1-retry") only marks a model call, as
# does one a refusal fallback's note names ("check-3 · declined first by
# <model>", math_harness_runner._emit_call_usage).
_DECLINED_FIRST = r"(?: · declined first by .+)?"
_ANALYZE_NOTE_PREFIX = "Analyze · "
_ANALYZE_CALL_RE = re.compile(r"(?:map|segment-\d+|check-\d+)(?:-retry)?(?:-low)?" + _DECLINED_FIRST)
_ANALYZE_NOW_CHECKING = "Check · now checking "
ANALYZE_PROGRESS_LINES = 600


# The Readability runner's notes start with "Readability · "; a bare call
# stage after it ("guide", "rewrite-3", "check-2-retry-low") marks a call
# (with or without a refusal fallback's "· declined first by <model>").
_READABILITY_NOTE_PREFIX = "Readability · "
_READABILITY_CALL_RE = re.compile(
    r"(?:guide|ask|(?:rewrite|check|repair|recheck)-\d+)(?:-retry)?(?:-low)?" + _DECLINED_FIRST
)
_READABILITY_NOW_WORKING = " · now working on "


def _analysis_progress_text(data: dict) -> str:
    """The live Sandbox of an Analyze run: the runner's progress notes and its
    public error messages, one per line, without the ``Analyze · `` prefix.

    Never a verifier's or paper map's raw JSON reply, a model-call marker or
    the process log (the session summary node): those are model output and
    plumbing, shown in the Monitor and the process log only."""
    return _progress_text(data, prefix=_ANALYZE_NOTE_PREFIX, call_re=_ANALYZE_CALL_RE,
                          in_flight=lambda line: line.startswith(_ANALYZE_NOW_CHECKING))


def _readability_progress_text(data: dict) -> str:
    """The live Sandbox of a readability run, as for an analysis: its notes
    without the ``Readability · `` prefix (a note may quote a bounded piece
    of a parsed reply, as Analyze's do), never a raw reply or a call marker."""
    return _progress_text(data, prefix=_READABILITY_NOTE_PREFIX, call_re=_READABILITY_CALL_RE,
                          in_flight=lambda line: _READABILITY_NOW_WORKING in line)


def _progress_text(data: dict, *, prefix: str, call_re: re.Pattern, in_flight) -> str:
    """A manuscript runner's progress notes (``prefix`` removed, call markers
    dropped) and public error messages; an ``in_flight`` line ("now
    checking …") only while it is the last one."""
    lines: list[str] = []
    for agent in data.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        output = str(agent.get("output") or "").strip()
        if not output:
            continue
        if output.startswith(prefix):
            note = " ".join(output[len(prefix):].split())
            if note and not call_re.fullmatch(note):
                lines.append(note[:600])
        elif str(agent.get("stage_name") or "").endswith("(error)"):
            # The runner's terminal error ("The source could not be used: …").
            message = " ".join(output.split())
            if message:
                lines.append(message[:600])
    if not lines and str(data.get("status") or "") not in ("", "running"):
        error = " ".join(str(data.get("error") or "").split())
        if error:
            lines.append(error[:600])
    # "Now checking …" matters only for the checks still in flight (the last line).
    lines = [line for index, line in enumerate(lines) if not in_flight(line) or index == len(lines) - 1]
    return "\n".join(lines[-ANALYZE_PROGRESS_LINES:])


def _analysis_preview(run_id: str, data: dict) -> dict:
    """The Sandbox of an Analyze run: ``analysis/report.md`` (live progress
    until it exists).  Never anything under ``analysis/source/``; the owner
    opens the manuscript through ``source_url`` (the link) or ``source_file``
    (a pasted or uploaded file, via /api/workspace/<id>/file)."""
    report = ""
    revision = "missing"
    mtime = 0.0
    source_file = None
    ws = _workspace_for_run(run_id)
    if ws:
        path = _workspace_target(ws, "analysis/report.md")
        if path is not None and path.is_file():
            try:
                report = path.read_text(encoding="utf-8", errors="replace")[:400_000]
                revision = _file_revision(path)
                mtime = path.stat().st_mtime
            except OSError:
                report = ""
        manuscript = _workspace_target(ws, "analysis/manuscript.json")
        try:
            root_file = (
                json.loads(manuscript.read_text(encoding="utf-8")).get("root_file")
                if manuscript is not None and manuscript.is_file() and manuscript.stat().st_size < 4_000_000
                else None
            )
        except (OSError, ValueError, AttributeError):
            root_file = None
        if isinstance(root_file, str) and root_file and ".." not in root_file.split("/"):
            candidate = _workspace_target(ws, f"analysis/source/{root_file}")
            if candidate is not None and candidate.is_file():
                source_file = f"analysis/source/{root_file}"
    block = data.get("analyze") if isinstance(data.get("analyze"), dict) else {}
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    live = "" if report else _analysis_progress_text(data)
    return {
        "kind": "analysis",
        "text": report or live,
        "source": "analysis/report.md" if report else ("live" if live else ""),
        "has_report": bool(report),
        "has_proof_md": False,
        "has_proof_tex": False,
        "editable": False,
        "revision": revision,
        "mtime": mtime,
        "source_url": _https_link(source.get("resolved_url") or source.get("input_url")),
        "source_file": source_file,
        "title": str(source.get("title") or "")[:300],
    }


def _workspace_bytes(ws: Path, relative: str, limit: int) -> bytes | None:
    """At most ``limit`` bytes of a regular workspace file, or ``None``."""
    path = _workspace_target(ws, relative)
    if path is None or not path.is_file():
        return None
    try:
        with path.open("rb") as stream:
            return stream.read(limit)
    except OSError:
        return None


def _writeup_preview(ws: Path) -> dict | None:
    """The written proof of an Ansatz CGM run (``writeup.md``), if one exists.

    Its status and the check's findings come from ``writeup.json`` only when
    that sidecar describes exactly this file; ``fresh`` says whether it was
    written for the current proof.md and engine receipt (a continuation or a
    Sandbox edit of proof.md leaves it stale).  Only proof.md is editable.
    """
    from agent_monitor.cgm import writeup as cgm_writeup

    path = _workspace_target(ws, "writeup.md")
    payload = _workspace_bytes(ws, "writeup.md", 400_000)
    if path is None or payload is None:
        return None
    view = cgm_writeup.describe(
        payload,
        _workspace_bytes(ws, "math_harness/project/writeup.json", 256_000),
        proof=_workspace_bytes(ws, "proof.md", 400_000),
        receipt=_workspace_bytes(ws, "math_harness/project/engine_receipt.json", 64_000),
    )
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return {
        "text": payload.decode("utf-8", errors="replace"),
        "status": view["status"],
        "fresh": view["fresh"],
        "path": "writeup.md",
        "revision": _file_revision(path),
        "mtime": mtime,
        "check": view["check"],
        "model": view["model"],
        "provider": view["provider"],
    }


def _json_file(path: Path | None, limit: int = 4_000_000) -> dict:
    """A small JSON object of a run workspace (``{}`` when unreadable)."""
    try:
        if path is None or path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _readability_preview(run_id: str, data: dict) -> dict:
    """The Sandbox of a readability run: ``readability/readable.md`` (live
    progress until it exists), with what the views beside it need: whether
    the original text is there, the notation (at most 300 rows), the parts
    and their checks, and the author's macros.  Never anything under
    ``readability/source/``: the owner opens the stored source file through
    ``source_file`` (/api/workspace/<id>/file) and the reading-order text
    through ``original_path``."""
    from agent_monitor.readability import paper_map, report

    readable = ""
    revision = "missing"
    mtime = 0.0
    source_file = None
    original_available = False
    notation = None
    macros: dict = {}
    parts: list[dict] = []
    ws = _workspace_for_run(run_id)
    if ws:
        path = _workspace_target(ws, "readability/readable.md")
        if path is not None and path.is_file():
            try:
                with path.open("rb") as handle:
                    readable = report.clip(handle.read(report.VIEW_BYTES).decode("utf-8", errors="replace"))
                revision = _file_revision(path)
                mtime = path.stat().st_mtime
            except OSError:
                readable = ""
        original = _workspace_target(ws, "readability/original.txt")
        original_available = original is not None and original.is_file()
        manuscript = _json_file(_workspace_target(ws, "readability/manuscript.json"))
        root_file = manuscript.get("root_file")
        if isinstance(root_file, str) and root_file and ".." not in root_file.split("/"):
            candidate = _workspace_target(ws, f"readability/source/{root_file}")
            if candidate is not None and candidate.is_file():
                source_file = f"readability/source/{root_file}"
        raw_macros = manuscript.get("macros") if isinstance(manuscript.get("macros"), dict) else {}
        macros = {str(name)[:80]: str(body)[:400] for name, body in list(raw_macros.items())[:400]
                  if str(name).startswith("\\")}
        notation = paper_map.load_notation(ws, rows=300)
        check = _json_file(_workspace_target(ws, "readability/check.json"))
        for part in (check.get("parts") or [])[:400]:
            if not isinstance(part, dict) or not isinstance(part.get("index"), int):
                continue
            parts.append({
                "index": part["index"],
                "title": str(part.get("title") or "")[:200],
                "kind": "references" if part.get("kind") == "references" else "text",
                "status": str(part.get("status") or "")[:20],
                "reason": str(part.get("reason") or "")[:400],
                "lines": [int(value) for value in (part.get("lines") or [])[:2] if isinstance(value, int)],
                "repaired": part.get("repaired") is True,
                # Every issue the check found: the major ones are also marked
                # in the text, the minor ones are the part's check notes.
                "issues": [{key: str(issue.get(key) or "")[:600] for key in ("severity", "location", "problem", "fix")}
                           for issue in (part.get("issues") or [])[:12] if isinstance(issue, dict)],
                "missing": [str(node)[:80] for node in (part.get("missing") or [])[:40]],
            })
    block = data.get("readability") if isinstance(data.get("readability"), dict) else {}
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    summary = block.get("summary") if isinstance(block.get("summary"), dict) else {}
    live = "" if readable else _readability_progress_text(data)
    return {
        "kind": "readability",
        "text": readable or live,
        "source": "readability/readable.md" if readable else ("live" if live else ""),
        "has_readable": bool(readable),
        "has_proof_md": False,
        "has_proof_tex": False,
        "editable": False,
        "revision": revision,
        "mtime": mtime,
        "original_available": original_available,
        "original_path": "readability/original.txt" if original_available else None,
        "source_url": _https_link(source.get("resolved_url") or source.get("input_url")),
        "source_file": source_file,
        "title": str(source.get("title") or "")[:300],
        "audience": str(block.get("audience") or ""),
        "math_fidelity": source.get("math_fidelity"),
        "parts": {key: int(summary.get(key) or 0)
                  for key in ("parts", "rewritten", "faithful", "issues", "unchecked", "not_rewritten", "kept")},
        "check": parts,
        "notation": notation or {"rows": [], "terms": [], "macros_used": []},
        "macros": macros,
    }


_GUEST_RATINGS: dict[int, list[float]] = {}
_GUEST_RATINGS_LOCK = threading.Lock()
GUEST_RATINGS_PER_HOUR = 30


def _guest_rating_allowed(user: dict) -> bool:
    """A guest's Auto ratings are bounded (an anonymous session must not spend
    the judge's key at will): GUEST_RATINGS_PER_HOUR per guest, then Auto
    takes its default."""
    try:
        guest_id = int(user.get("id"))
    except (TypeError, ValueError):
        return False
    now = time.time()
    with _GUEST_RATINGS_LOCK:
        recent = [t for t in _GUEST_RATINGS.get(guest_id, []) if now - t < 3600]
        if len(recent) >= GUEST_RATINGS_PER_HOUR:
            _GUEST_RATINGS[guest_id] = recent
            return False
        recent.append(now)
        _GUEST_RATINGS[guest_id] = recent
        if len(_GUEST_RATINGS) > 10_000:  # forget the idle
            for key in [k for k, v in _GUEST_RATINGS.items() if not v or now - v[-1] >= 3600]:
                _GUEST_RATINGS.pop(key, None)
        return True


def _not_math_refusal(user: dict, text: object, *, run_id: str = "") -> str:
    """easy_route's refusal of a request that is clearly not mathematics, for
    a signed-in account with the judge on ("" otherwise, and on any doubt)."""
    from agent_monitor import easy_route

    if not user or not easy_route.enabled():
        return ""
    request = str(text or "").strip()
    if not request:
        return ""
    problem = ""
    if run_id:
        try:
            record = _run_record_for(run_id) or {}
            problems = job_manager.ensure_problems(record, run_id)
            problem = job_manager._problem_item_text(problems[0]) if problems else str(record.get("problem_text") or "")
        except Exception:  # noqa: BLE001 - context only
            problem = ""
    return easy_route.topic_refusal(request, problem=problem)


_AUTO_MODE_LABELS = {
    "informal": "Informal only",
    "formal": "Informal + formalization",
    "analyze": "Analyze",
    "readability": "Readability",
}


def _note_auto_mode(job: dict, body: dict) -> None:
    """A run started in Auto mode says in its thread which mode was chosen."""
    mode = str((body or {}).get("auto_mode") or "").strip().lower()
    run_id = str((job or {}).get("run_id") or "")
    if mode not in _AUTO_MODE_LABELS or not run_id:
        return
    from agent_monitor import easy_route

    try:
        job_manager._append_chat(
            run_id, "system", f"Auto mode: {_AUTO_MODE_LABELS[mode]} (chosen by {easy_route.model()})."
        )
    except Exception:  # noqa: BLE001 - the note is informational
        return


def _sandbox_preview(run_id: str, data: dict | None) -> dict:
    """Proof.md if it exists, otherwise the latest non-tool agent writes.

    The sandbox tab used to wait for a separate /files round-trip and stayed
    blank for the whole run. Folding the draft into /api/run makes preview
    follow the same payload the HITL panel already uses.  An Analyze run
    previews its report instead (it has no proof), and a readability run its
    readable version.  An Ansatz CGM run with a written proof also carries
    it as ``writeup`` (see ``_writeup_preview``).
    """
    data = data or {}
    from agent_monitor import analyze_runs, readability_runs

    if readability_runs.is_readability_record(data):
        return _readability_preview(run_id, data)
    if analyze_runs.is_analyze_record(data):
        return _analysis_preview(run_id, data)
    proof_md = ""
    proof_revision = "missing"
    proof_mtime = 0.0
    proof_tex = False
    ws = _workspace_for_run(run_id)
    if ws:
        md_path = _workspace_target(ws, "proof.md")
        if md_path is not None and md_path.is_file():
            try:
                proof_md = md_path.read_text(encoding="utf-8", errors="replace")[:400_000]
                proof_revision = _file_revision(md_path)
                proof_mtime = md_path.stat().st_mtime
            except OSError:
                proof_md = ""
        proof_tex = any(
            (target := _workspace_target(ws, name)) is not None and target.is_file()
            for name in ("proof.tex", "proof.pdf")
        )
    live_text = _live_writes(data)
    text = proof_md or live_text
    source = "proof.md" if proof_md else ("live" if live_text else "")
    preview = {
        "text": text,
        "source": source,
        "has_proof_md": bool(proof_md),
        "has_proof_tex": bool(proof_tex),
        "revision": proof_revision,
        "mtime": proof_mtime,
    }
    from agent_monitor.engine_graph import CGM_GRAPH_ENGINES

    if ws and str(data.get("engine") or "").strip().lower() in CGM_GRAPH_ENGINES:
        writeup = _writeup_preview(ws)
        if writeup is not None:
            preview["writeup"] = writeup
    return preview


def _refresh_analyze_block(data: dict, ws: Path | None) -> None:
    """An Analyze record shows ``run["analyze"]`` (never ``run["analysis"]``,
    the Monitor block).  A finished analysis whose stored block lacks its
    summary (written before a restart, say) is completed from the workspace;
    a running one is left as stored."""
    from agent_monitor import analyze_runs

    if not analyze_runs.is_analyze_record(data):
        return
    data["run_kind"] = "analyze"
    block = data.get("analyze") if isinstance(data.get("analyze"), dict) else {}
    data["analyze"] = block
    if ws is None or block.get("summary") or str(data.get("status") or "") == "running":
        return
    from agent_monitor.analyze import record as analyze_record

    try:
        fresh = analyze_record.run_block(ws)
    except Exception:  # noqa: BLE001 - the stored block stays
        fresh = None
    if fresh:
        for key in ("recheck", "rechecks", *job_manager.ASK_KEYS):
            if block.get(key):
                fresh[key] = block[key]
        data["analyze"] = fresh


def _refresh_readability_block(data: dict, ws: Path | None) -> None:
    """A readability record shows ``run["readability"]``.  A finished run
    whose stored block lacks its summary (written before a restart, say) is
    completed from the workspace, keeping its questions; a running one is
    left as stored."""
    from agent_monitor import readability_runs

    if not readability_runs.is_readability_record(data):
        return
    data["run_kind"] = "readability"
    block = data.get("readability") if isinstance(data.get("readability"), dict) else {}
    data["readability"] = block
    if ws is None or block.get("summary") or str(data.get("status") or "") == "running":
        return
    from agent_monitor.readability import record as readability_record

    try:
        fresh = readability_record.run_block(ws)
    except Exception:  # noqa: BLE001 - the stored block stays
        fresh = None
    if fresh:
        for key in job_manager.ASK_KEYS:
            if block.get(key):
                fresh[key] = block[key]
        data["readability"] = fresh


def _list_workspace_files(ws: Path) -> list[dict]:
    """The run's files for its owner's file list and export: never what is
    internal, a dot-file or anything in a dot-directory (the attachments'
    extracted-text cache ``attachments/.text/``, scratch space)."""
    out = []
    resolved_ws = ws.resolve()
    for p in sorted(resolved_ws.rglob("*")):
        if p.is_symlink() or not p.is_file() or p.name.startswith("."):
            continue
        try:
            p.resolve().relative_to(resolved_ws)
            rel = str(p.relative_to(resolved_ws))
        except (OSError, RuntimeError, ValueError):
            continue
        if any(part.startswith(".") for part in p.relative_to(resolved_ws).parts):
            continue
        rel = str(p.relative_to(ws))
        if _workspace_target(ws, rel) is None:
            continue
        # skip bulky binary intermediates except pdf
        if p.suffix in {".aux", ".out", ".synctex.gz", ".fls", ".fdb_latexmk"}:
            continue
        out.append(
            {
                "path": rel,
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
                "kind": "pdf" if p.suffix == ".pdf" else ("text" if p.suffix in _TEXT_SUFFIXES else "binary"),
            }
        )
    out.sort(key=lambda f: -f["mtime"])
    return out


def _compile_workspace_pdf(ws: Path, tex_rel: str) -> Path | None:
    """Compile a workspace .tex to PDF (cached beside it as .preview.pdf)."""
    import shutil as _shutil
    import subprocess as _subprocess
    import tempfile as _tempfile

    tex_path = _workspace_target(ws, tex_rel)
    if tex_path is None or not tex_path.is_file():
        return None
    from agent_monitor.engines_registry import which_tool

    pdflatex = which_tool("pdflatex")
    tectonic = which_tool("tectonic")
    if not pdflatex and not tectonic:
        return None
    out_pdf = tex_path.with_suffix(".preview.pdf")
    if out_pdf.exists() and out_pdf.stat().st_mtime >= tex_path.stat().st_mtime:
        return out_pdf
    tex = tex_path.read_text(encoding="utf-8", errors="replace")
    if "\\documentclass" not in tex:
        return None
    with _tempfile.TemporaryDirectory(prefix="console_latex_") as tmp:
        work = Path(tmp)
        (work / "doc.tex").write_text(tex, encoding="utf-8")
        # The document is a run's: TeX expands environment variables in file
        # names, so it compiles without the server's secrets.
        from agent_monitor.subprocess_env import child_process_env

        tex_env = child_process_env()
        try:
            if tectonic:
                _subprocess.run(
                    [tectonic, "--outfmt", "pdf", "doc.tex"],
                    cwd=work,
                    env=tex_env,
                    capture_output=True,
                    timeout=90,
                    check=False,
                )
            else:
                for _ in range(2):
                    _subprocess.run(
                        [pdflatex, "-interaction=nonstopmode", "doc.tex"],
                        cwd=work,
                        env=tex_env,
                        capture_output=True,
                        timeout=90,
                        check=False,
                    )
        except (OSError, _subprocess.TimeoutExpired):
            return None
        pdf = work / "doc.pdf"
        if pdf.exists() and pdf.stat().st_size > 0:
            _shutil.copy2(pdf, out_pdf)
            return out_pdf
    return None


# Content types an HTML form can submit cross-site without a CORS preflight.
_FORM_CONTENT_TYPES = frozenset(
    {"application/x-www-form-urlencoded", "multipart/form-data", "text/plain"}
)

# Paths reachable without a session.
_PUBLIC_PATHS = {
    "/login",
    "/reset-password",
    "/verify-email",
    "/health",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/google",
    "/api/auth/guest",
    "/api/auth/me",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/verify-reset-token",
    "/api/auth/verify-email",
    "/api/auth/resend-verification",
}


class Handler(RunSharingMixin, ProjectsMixin, ResearchMixin, BaseHTTPRequestHandler):
    # Socket timeout for each blocking read/write: a client that stalls a
    # request (or its body) loses the connection instead of pinning a thread
    # and its buffers forever.  Longer than Caddy's 2-minute upstream
    # keep-alive, so the proxy always retires idle connections first.
    timeout = 300

    def log_message(self, fmt, *args):  # noqa: A003
        print(f"[console] {self.address_string()} {fmt % args}", flush=True)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # The client (or Caddy) went away before the reply was written;
            # nothing more can be sent on this connection, and a traceback
            # per disconnect only buries real errors in the journal.
            self.close_connection = True

    def _linger_after_refusal(self, declared_length: int) -> None:
        """Close gracefully after answering before the request body was read.

        The reply is already written: half-close our side so the peer sees the
        complete response, then read and discard (bounded in bytes and time)
        whatever of the declared body is still arriving, so the final close
        does not turn into a connection reset that truncates the reply.
        """
        self.close_connection = True
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return
        remaining = min(max(0, int(declared_length or 0)), REFUSED_BODY_DRAIN_BYTES)
        deadline = time.monotonic() + REFUSED_BODY_DRAIN_SECONDS
        try:
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(min(left, 1.0))
                chunk = self.rfile.read1(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass

    def log_request(self, code="-", size="-"):
        """Log request paths without query strings that may contain reset tokens."""
        path = urlparse(self.path).path
        path = re.sub(r"/(?:api/)?shared/[^/]+|/invite/[^/]+", "/private-link/[redacted]", path)
        self.log_message('"%s %s %s" %s %s', self.command, path, self.request_version, code, size)

    def _send(self, code: int, body: str, ctype: str = "application/json", headers: dict[str, str] | None = None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in self._merge_headers(headers).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _is_https(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _merge_headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        out = dict(headers or {})
        # These directives do not constrain the console's existing inline
        # scripts/styles, but close the useful browser-level attack surfaces:
        # framing, MIME sniffing, base-tag rewriting, plugin objects, and
        # ambient device access.
        out.setdefault("X-Content-Type-Options", "nosniff")
        out.setdefault("Referrer-Policy", "no-referrer")
        out.setdefault("X-Frame-Options", "SAMEORIGIN")
        out.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        out.setdefault(
            "Content-Security-Policy",
            "frame-ancestors 'self'; base-uri 'self'; object-src 'none'",
        )
        pending = getattr(self, "_pending_session_cookie", None)
        if pending and "Set-Cookie" not in out:
            out["Set-Cookie"] = self._session_cookie_header(
                pending,
                guest=bool(getattr(self, "_pending_session_guest", False)),
            )
            self._pending_session_cookie = None
            self._pending_session_guest = False
        return out

    def _issue_guest(self, *, replace_account_token: str | None = None) -> dict | None:
        from agent_monitor import auth

        available, retry_after = _reserve_guest_session_slot(self._guest_client_key())
        if not available:
            self._send(
                429,
                json.dumps(
                    {
                        "ok": False,
                        "error": "Guest capacity is busy; please try again shortly",
                    }
                ),
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "Retry-After": str(retry_after),
                },
            )
            return None
        try:
            if replace_account_token:
                account, token = auth.create_guest_session(replace_account_token=replace_account_token)
            else:
                account, token = auth.create_guest_session()
        except Exception as exc:  # noqa: BLE001
            logger.error("Guest session creation failed (%s)", type(exc).__name__)
            self._send(
                503,
                json.dumps(
                    {
                        "ok": False,
                        "error": "Guest access is temporarily unavailable",
                    }
                ),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
            return None
        self._pending_session_cookie = token
        self._pending_session_guest = True
        return account

    # ── auth helpers ─────────────────────────────────────────────────────
    def _session_token(self) -> str | None:
        from http.cookies import SimpleCookie

        from agent_monitor.auth import SESSION_COOKIE

        raw = self.headers.get("Cookie") or ""
        try:
            jar = SimpleCookie(raw)
        except Exception:  # noqa: BLE001
            return None
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _current_user(self) -> dict | None:
        from agent_monitor import auth

        user = auth.user_for_token(self._session_token())
        if user and user.get("guest") and not guest_access_enabled():
            return None
        return user

    def _accounts_payload(self, user: dict) -> dict:
        """``GET /api/admin/accounts``: every registered account and its access,
        and what an invitation needs (whether the site can send email, the
        sign-in address, the operator as sender, the default message)."""
        from agent_monitor import accounts_admin, auth

        accounts = accounts_admin.list_accounts()
        return {
            "accounts": accounts,
            "groups": sorted({account["group"] for account in accounts if account["group"]}, key=str.casefold),
            # Other ways in that a pause does not close: a new sign-up (with
            # another address, or another Google account) and guest use.
            "open_doors": {"sign_ups": auth.registration_open(), "guests": guest_access_enabled()},
            "email_available": auth.email_delivery_configured(),
            "sign_in_url": accounts_admin.sign_in_url(self._request_base_path()),
            "sender": {"name": str(user.get("name") or ""), "email": str(user.get("email") or "")},
            "defaults": {"subject": accounts_admin.DEFAULT_SUBJECT, "message": accounts_admin.DEFAULT_MESSAGE,
                         "reset_subject": accounts_admin.RESET_SUBJECT,
                         "reset_message": accounts_admin.RESET_MESSAGE,
                         "password_length": accounts_admin.DEFAULT_PASSWORD_LENGTH,
                         "placeholders": list(accounts_admin.PLACEHOLDERS)},
        }

    def _admin_accounts_post(self, path: str, body: dict, user: dict) -> None:
        """``POST /api/admin/accounts`` (create from a pasted list, optionally
        emailing each new person their sign-in details; with ``preview``,
        only what it would do), ``/access`` (turn access on or off; with
        ``others: "off"`` and a ``group``, everyone else in the group off),
        ``/full-access`` (give accounts full access, or take it back),
        ``/group`` (put accounts in a group) and ``/password`` (a new random
        password).  The passwords are in this response only."""
        from agent_monitor import accounts_admin, auth

        sender_name = " ".join(str(body.get("sender_name") or user.get("name") or user.get("email") or "").split())[:120]
        reply_to = str(user.get("email") or "") or None
        subject = str(body.get("subject") or "")[:200] or None
        message = str(body.get("message") or "")[:10_000]
        message = message if message.strip() else None
        send_email = body.get("send_email") is True
        url = accounts_admin.sign_in_url(self._request_base_path())
        email_available = auth.email_delivery_configured()
        refusal = None
        if send_email and not email_available:
            refusal = "This site cannot send email (SMTP is not set up); send the details yourself"
        elif send_email and not url:
            refusal = ("Emails need the site's address (AGENT_MONITOR_APP_URL), which is not set; "
                       "send the details yourself")
        elif send_email and path == "/api/admin/accounts" and message and "{password}" not in message:
            refusal = "The message must contain {password}: without it nobody could sign in"
        if refusal:
            self._send(400, json.dumps({"error": refusal}), headers={"Cache-Control": "no-store"})
            return
        try:
            if path == "/api/admin/accounts":
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("Paste one person per line: 'Name - email', 'Name <email>' or 'email'")
                preview = body.get("preview") is True
                group, group_is_new = accounts_admin.resolve_group(body.get("group"))
                results = accounts_admin.create_accounts(accounts_admin.parse_people(text),
                                                         password_length=body.get("password_length"), preview=preview,
                                                         group=group,
                                                         full_access=body["full_access"]
                                                         if isinstance(body.get("full_access"), bool) else None)
                for result in results:
                    if send_email and not preview and result["status"] == "created":
                        result["email_sent"] = accounts_admin.send_invite(
                            email=result["email"], name=result["name"], password=result["password"], url=url,
                            sender_name=sender_name, reply_to=reply_to, subject=subject, message=message)
                if not preview:
                    logger.info("[ADMIN] %s created %d account(s)", user.get("email"),
                                sum(1 for result in results if result["status"] == "created"))
                payload: dict = {"results": results, "email_available": email_available, "group": group,
                                 "group_is_new": group_is_new}
                stopped = next((result["error"] for result in results if result["status"] == "not_processed"), None)
                if stopped:
                    payload["stopped"] = stopped
            elif path == "/api/admin/accounts/access":
                emails = body.get("emails")
                access = body.get("access")
                others = body.get("others")
                if not isinstance(emails, list) or not all(isinstance(email, str) for email in emails):
                    raise ValueError("emails must be a list of email addresses")
                if access not in ("on", "off"):
                    raise ValueError('access must be "on" or "off"')
                if others not in (None, "off"):
                    raise ValueError('others may only be "off"')
                payload = accounts_admin.set_access(emails, on=access == "on", others_off=others == "off",
                                                    group=body.get("group"))
                logger.info("[ADMIN] %s turned access %s for %d account(s)%s", user.get("email"), access,
                            len(payload["updated"]),
                            f" and off for {len(payload['turned_off'])} other(s)" if others == "off" else "")
            elif path == "/api/admin/accounts/full-access":
                emails = body.get("emails")
                state = body.get("full_access")
                if not isinstance(emails, list) or not all(isinstance(email, str) for email in emails):
                    raise ValueError("emails must be a list of email addresses")
                if state not in ("on", "off"):
                    raise ValueError('full_access must be "on" or "off"')
                payload = accounts_admin.set_full_access(emails, on=state == "on")
                logger.info("[ADMIN] %s turned full access %s for %d account(s)", user.get("email"), state,
                            len(payload["updated"]))
            elif path == "/api/admin/accounts/group":
                emails = body.get("emails")
                if not isinstance(emails, list) or not all(isinstance(email, str) for email in emails):
                    raise ValueError("emails must be a list of email addresses")
                payload = accounts_admin.set_group(emails, body.get("group"))
                logger.info("[ADMIN] %s set the group of %d account(s)", user.get("email"), len(payload["updated"]))
            else:
                email = str(body.get("email") or "").strip().lower()
                password = accounts_admin.new_password(email, password_length=body.get("password_length"))
                payload = {"email": email, "password": password}
                if send_email:
                    # A new password has its own message (the add form's says the account was created).
                    account = next((item for item in accounts_admin.list_accounts() if item["email"] == email), {})
                    payload["email_sent"] = accounts_admin.send_invite(
                        email=email, name=str(account.get("name") or ""), password=password, url=url,
                        sender_name=sender_name, reply_to=reply_to, subject=accounts_admin.RESET_SUBJECT,
                        message=accounts_admin.RESET_MESSAGE)
                logger.info("[ADMIN] %s set a new password for one account", user.get("email"))
        except ValueError as exc:
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False), headers={"Cache-Control": "no-store"})
            return
        self._send(200, json.dumps(payload, ensure_ascii=False), headers={"Cache-Control": "no-store"})

    def _require_operator(self, user: dict) -> bool:
        """Admin-only endpoints: enforced here, never by the UI alone."""
        if _is_operator_user(user):
            return True
        self._send(403, json.dumps({"error": "admin access required"}), headers={"Cache-Control": "no-store"})
        return False

    def _deny_guest(self, user: dict, capability: str) -> bool:
        """Reject shared control-plane actions for anonymous sessions."""
        if not user.get("guest"):
            return False
        self._send(
            403,
            json.dumps(
                {
                    "ok": False,
                    "error": f"Sign in to access {capability}",
                }
            ),
            headers={"Cache-Control": "no-store"},
        )
        return True

    def _deny_member(
        self, user: dict, capability: str, *, explanation: str | None = None
    ) -> bool:
        """Reject host-level capabilities for self-registered accounts
        (not for one an administrator gave full access)."""
        if not account_policy.is_restricted(user):
            return False
        self._send(
            403,
            json.dumps(
                {
                    "ok": False,
                    "error": explanation
                    or f"{capability} is limited to the site administrator",
                }
            ),
            headers={"Cache-Control": "no-store"},
        )
        return True

    # The run start this request booked for a member (_admit_member_run).
    _member_booking: float | None = None

    def _fetch_linked_papers(self, body: dict, text: object, run_id: str | None = None):
        """Papers linked in a signed-in account's problem or message, added
        to ``body["attachments"]`` as uploads (agent_monitor.link_sources).
        Returns what was fetched, for :meth:`_report_linked_papers`; never
        raises (a link that cannot be read is reported, not refused)."""
        from agent_monitor import link_sources

        if not isinstance(text, str) or not text.strip() or not link_sources.find_links(text):
            return None
        uploads = body.get("attachments")
        if uploads is not None and not isinstance(uploads, list):
            return None  # start_job refuses the malformed list itself
        known: dict[str, str] = {}
        run_files: list[dict] = []
        if run_id:
            workspace = RUNS_DIR / "workspaces" / run_id
            known = link_sources.read_known(workspace)
            try:
                run_files = research_attachments.list_files(workspace)
            except (OSError, ValueError):
                run_files = []
        try:
            fetched = link_sources.fetch_links(text, existing=uploads or [], known=known, run_files=run_files)
        except Exception:  # noqa: BLE001 - never block the run on a link
            logger.exception("linked paper fetch failed")
            return None
        if fetched.items:
            body["attachments"] = list(uploads or []) + fetched.items
        return fetched

    @staticmethod
    def _report_linked_papers(run_id: object, fetched) -> None:
        """Say in the run's conversation which links were read (or not)."""
        from agent_monitor import link_sources

        if fetched is None or not run_id:
            return
        try:
            for note in fetched.notes:
                job_manager._append_chat(str(run_id), "system", note)
            link_sources.remember(RUNS_DIR / "workspaces" / str(run_id), fetched.read)
        except Exception:  # noqa: BLE001
            logger.exception("linked paper report failed")

    def _admit_member_run(self, user: dict) -> bool:
        self._member_booking = None
        if not account_policy.is_member(user):
            return True
        error, retry_after, booking = _reserve_member_run_slot(int(user["id"]))
        if error:
            self._send(429, json.dumps({"error": error}), headers={"Retry-After": str(retry_after)})
            return False
        self._member_booking = booking
        return True

    def _release_member_run(self, user: dict) -> None:
        """Return an admitted member start when the request started nothing."""
        if account_policy.is_member(user):
            _release_member_run_slot(int(user["id"]), self._member_booking)

    def _admit_member_message(self, user: dict) -> bool:
        if not account_policy.is_member(user):
            return True
        error, retry_after = _reserve_member_message(int(user["id"]))
        if error:
            self._send(429, json.dumps({"error": error}), headers={"Retry-After": str(retry_after)})
            return False
        return True

    def _admit_guest_derived_action(self, user: dict, workspace: Path) -> bool:
        if not user.get("guest"):
            return True
        from agent_monitor import auto_pipeline

        state = auto_pipeline.load_state(workspace) or {}
        if state.get("status") == "running":
            self._send(
                409,
                json.dumps({"error": "Wait for the automatic Formal and DAG pipeline to finish"}),
                headers={"Retry-After": "20"},
            )
            return False
        error, retry_after = _reserve_guest_derived_action(
            int(user["id"]), self._guest_client_key()
        )
        if error:
            self._send(
                429,
                json.dumps({"error": error}),
                headers={"Retry-After": str(retry_after)},
            )
            return False
        return True

    def _guest_client_key(self) -> str | None:
        peer = self.client_address[0] if self.client_address else None
        return guest_client_key(peer, self.headers.get("X-Forwarded-For"))

    def _deny_run_compilation(self, user: dict, run_id: str) -> bool:
        """Never execute TeX originating from an anonymous guest run, or a
        third-party manuscript (an Analyze run's sources are never compiled)."""
        if user.get("guest"):
            return self._deny_guest(user, "server-side proof compilation")
        if self._deny_member(user, "Server-side compilation"):
            return True
        record = _run_record_for(run_id) or {}
        from agent_monitor import analyze_runs, readability_runs

        if analyze_runs.is_manuscript_record(record):
            self._send(
                403,
                json.dumps(
                    {
                        "ok": False,
                        "error": (
                            readability_runs.COMPILE_REFUSAL
                            if readability_runs.is_readability_record(record)
                            else "Analyze runs are never compiled: their TeX is a third-party manuscript"
                        ),
                    }
                ),
                headers={"Cache-Control": "no-store"},
            )
            return True
        owner = str(record.get("owner") or "").strip().lower()
        if record.get("guest") or owner.endswith("@public.local"):
            self._send(
                403,
                json.dumps(
                    {
                        "ok": False,
                        "error": "Server-side compilation is disabled for guest-generated artifacts",
                    }
                ),
                headers={"Cache-Control": "no-store"},
            )
            return True
        return False

    # ── Analyze (run_kind "analyze"; see agent_monitor/analyze_runs.py) ──

    def _analysis_lean_request(
        self, ws: Path, action: str, body: dict, *, user: dict | None = None, record: dict | None = None
    ) -> dict | None:
        """Validate a Lean action on an Analyze run; Verify returns the
        statement-only inputs for the selected ``targets`` (default: the main
        results).  Formalizing is model work, so the Analyze switches apply
        (compiling or checking existing Lean is not)."""
        from agent_monitor import analyze_runs
        from agent_monitor.analyze import graph as analysis_graph

        if action in {"compile", "check"}:
            return None
        if action not in {"verify", "generate"}:
            raise analyze_runs.AnalyzeRequestError(
                "An analysis formalizes the statements of selected results (Verify with targets), "
                "or compiles and checks its Lean source"
            )
        if body.get("prove") not in (None, False):
            raise analyze_runs.AnalyzeRequestError(
                "Analyze formalizes statements only; proofs are not attempted"
            )
        refusal = analyze_runs.derived_work_error(user, record)
        if refusal is not None:
            raise refusal
        graph = analysis_graph.load(ws)
        if graph is None:
            raise analyze_runs.AnalyzeRequestError(
                "The analysis has not published its graph yet", status=409
            )
        targets = body.get("targets")
        chosen = (
            analyze_runs.default_targets(graph)
            if targets is None
            else analyze_runs.validate_targets(graph, targets)
        )
        if not chosen:
            raise analyze_runs.AnalyzeRequestError("Select results in the DAG to formalize")
        return analyze_runs.lean_inputs(graph, chosen)

    def _send_analyze_error(self, exc: Exception) -> None:
        status = int(getattr(exc, "status", 400) or 400)
        retry_after = getattr(exc, "retry_after", None)
        headers = {"Cache-Control": "no-store"}
        if retry_after:
            headers["Retry-After"] = str(int(retry_after))
        self._send(status, json.dumps({"error": str(exc)}, ensure_ascii=False), headers=headers)

    def _refuse_analyze_continuation(self, run_id: str) -> bool:
        """Continue and chat do not apply to a manuscript run (an analysis or
        a readability run; 400, plain message)."""
        from agent_monitor import analyze_runs, readability_runs

        record = _run_record_for(run_id)
        if readability_runs.is_readability_record(record):
            self._send(400, json.dumps({"error": readability_runs.CONTINUE_REFUSAL}))
            return True
        if analyze_runs.is_analyze_record(record):
            self._send(400, json.dumps({"error": analyze_runs.CONTINUE_REFUSAL}))
            return True
        return False

    def _start_analyze_run(self, body: dict, user: dict) -> None:
        """``POST /api/runs`` with ``run_kind: "analyze"``.

        Validation by role first (plain 400s), then the sponsored capacity
        (429) and the guest and member run admission, then ``start_job``.
        """
        from agent_monitor import analyze_runs

        guest = bool(user.get("guest"))
        requested_model = body.get("model") or None
        try:
            refusal = analyze_runs.availability_error(user)
            if refusal is not None:
                raise refusal
            analyze_runs.normalize_request(body, user)
            if requested_model is not None and not isinstance(requested_model, str):
                raise analyze_runs.AnalyzeRequestError("model must be text")
            if guest:
                from agent_monitor.settings import validate_guest_model

                if requested_model is not None:
                    try:
                        requested_model = validate_guest_model(requested_model)
                    except ValueError as exc:
                        raise analyze_runs.AnalyzeRequestError(str(exc)) from None
                capacity = analyze_runs.GATE.capacity_error(guest=True)
                if capacity is not None:
                    raise capacity
        except analyze_runs.AnalyzeRequestError as exc:
            self._send_analyze_error(exc)
            return

        def start() -> dict:
            return research_projects.start_run(
                body.get("project_id"),
                user["id"],
                body.get("project_task"),
                # An analysis never receives the project's document, so the
                # tool-free rule for project proofs does not apply.
                lambda _tool_free: job_manager.start_job(
                    engine="analyze",
                    run_kind="analyze",
                    source=body.get("source"),
                    analyze=body.get("analyze"),
                    attachments=body.get("attachments"),
                    # Analyze formalizes only when asked (statements only).
                    formalize=body.get("formalize") is True,
                    model=requested_model,
                    user=user,
                    use_subagents=False,
                    auth_route=(str(body.get("auth_route") or "").strip() or None),
                    reasoning_effort=(str(body.get("reasoning_effort") or "").strip() or None),
                    speed_mode=(str(body.get("speed_mode") or "").strip() or None),
                ),
            )

        try:
            if guest:
                from agent_monitor.auth import release_guest_run, reserve_guest_run

                user_id = int(user["id"])
                client = self._guest_client_key()
                with _GUEST_RUN_START_LOCK:
                    error, retry_after = _guest_analyze_capacity(user_id, client, reserve=False)
                    if error:
                        self._send(429, json.dumps({"error": error}), headers={"Retry-After": str(retry_after)})
                        return
                    capacity_error, retry_after = _reserve_guest_run_slot(user_id, client)
                    if capacity_error:
                        self._send(
                            429,
                            json.dumps({"error": capacity_error, "guest_policy": guest_policy(user),
                                        "login_required": guest_policy(user)["login_required"]}),
                            headers={"Retry-After": str(retry_after)},
                        )
                        return
                    if not reserve_guest_run(user_id, guest_runs_per_session(),
                                             previous_runs=job_manager.persisted_run_count(user_id)):
                        _release_guest_run_slot(user_id, client)
                        self._send(403, json.dumps({"error": "Guest limit reached. Sign in to continue",
                                                   "login_required": True, "guest_policy": guest_policy(user)}))
                        return
                    _guest_analyze_capacity(user_id, client, reserve=True)
                    try:
                        job = start()
                    except Exception:
                        release_guest_run(user_id)
                        _release_guest_analyze(user_id, client)
                        _release_guest_run_slot(user_id, client)
                        raise
                    if client is not None and job.get("run_id"):
                        _GUEST_RUN_CLIENTS[str(job["run_id"])] = client
                    job = {**job, "guest_policy": guest_policy(user)}
            else:
                member_error = account_policy.member_run_error(
                    user, "analyze", model=requested_model, auth_route=body.get("auth_route")
                )
                if member_error:
                    self._send(403, json.dumps({
                        "error": member_error,
                        "allowed_engines": sorted(account_policy.member_engine_allowlist()),
                    }))
                    return
                if not self._admit_member_run(user):
                    return
                try:
                    job = start()
                except Exception:
                    self._release_member_run(user)
                    raise
        except analyze_runs.AnalyzeRequestError as exc:
            self._send_analyze_error(exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return
        _note_auto_mode(job, body)
        self._send(200, json.dumps(job))

    def _start_readability_run(self, body: dict, user: dict) -> None:
        """``POST /api/runs`` with ``run_kind: "readability"``.

        As for an analysis: validation by role first (plain 400s), then the
        sponsored capacity (429) and the guest and member run admission, then
        ``start_job``.
        """
        from agent_monitor import readability_runs
        from agent_monitor.analyze_runs import AnalyzeRequestError

        guest = bool(user.get("guest"))
        requested_model = body.get("model") or None
        try:
            refusal = readability_runs.availability_error(user)
            if refusal is not None:
                raise refusal
            readability_runs.normalize_request(body, user)
            if requested_model is not None and not isinstance(requested_model, str):
                raise AnalyzeRequestError("model must be text")
            if guest:
                from agent_monitor.settings import validate_guest_model

                if requested_model is not None:
                    try:
                        requested_model = validate_guest_model(requested_model)
                    except ValueError as exc:
                        raise AnalyzeRequestError(str(exc)) from None
                from agent_monitor import analyze_runs

                capacity = analyze_runs.GATE.capacity_error(guest=True, messages=readability_runs.GATE_MESSAGES)
                if capacity is not None:
                    raise capacity
        except AnalyzeRequestError as exc:
            self._send_analyze_error(exc)
            return

        def start() -> dict:
            return research_projects.start_run(
                body.get("project_id"),
                user["id"],
                body.get("project_task"),
                # A readability run never receives the project's document, so
                # the tool-free rule for project proofs does not apply.
                lambda _tool_free: job_manager.start_job(
                    engine="readability",
                    run_kind="readability",
                    source=body.get("source"),
                    readability=body.get("readability"),
                    attachments=body.get("attachments"),
                    formalize=False,
                    model=requested_model,
                    user=user,
                    use_subagents=False,
                    auth_route=(str(body.get("auth_route") or "").strip() or None),
                    reasoning_effort=(str(body.get("reasoning_effort") or "").strip() or None),
                    speed_mode=(str(body.get("speed_mode") or "").strip() or None),
                ),
            )

        try:
            if guest:
                from agent_monitor.auth import release_guest_run, reserve_guest_run

                user_id = int(user["id"])
                client = self._guest_client_key()
                with _GUEST_RUN_START_LOCK:
                    error, retry_after = _guest_readability_capacity(user_id, client, reserve=False)
                    if error:
                        self._send(429, json.dumps({"error": error}), headers={"Retry-After": str(retry_after)})
                        return
                    capacity_error, retry_after = _reserve_guest_run_slot(user_id, client)
                    if capacity_error:
                        self._send(
                            429,
                            json.dumps({"error": capacity_error, "guest_policy": guest_policy(user),
                                        "login_required": guest_policy(user)["login_required"]}),
                            headers={"Retry-After": str(retry_after)},
                        )
                        return
                    if not reserve_guest_run(user_id, guest_runs_per_session(),
                                             previous_runs=job_manager.persisted_run_count(user_id)):
                        _release_guest_run_slot(user_id, client)
                        self._send(403, json.dumps({"error": "Guest limit reached. Sign in to continue",
                                                   "login_required": True, "guest_policy": guest_policy(user)}))
                        return
                    _guest_readability_capacity(user_id, client, reserve=True)
                    try:
                        job = start()
                    except Exception:
                        release_guest_run(user_id)
                        _release_guest_readability(user_id, client)
                        _release_guest_run_slot(user_id, client)
                        raise
                    if client is not None and job.get("run_id"):
                        _GUEST_RUN_CLIENTS[str(job["run_id"])] = client
                    job = {**job, "guest_policy": guest_policy(user)}
            else:
                member_error = account_policy.member_run_error(
                    user, "readability", model=requested_model, auth_route=body.get("auth_route")
                )
                if member_error:
                    self._send(403, json.dumps({
                        "error": member_error,
                        "allowed_engines": sorted(account_policy.member_engine_allowlist()),
                    }))
                    return
                if not self._admit_member_run(user):
                    return
                try:
                    job = start()
                except Exception:
                    self._release_member_run(user)
                    raise
        except AnalyzeRequestError as exc:
            self._send_analyze_error(exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return
        _note_auto_mode(job, body)
        self._send(200, json.dumps(job))

    def _start_manuscript_ask(self, rid: str, body: dict, user: dict, kind: str) -> None:
        """``POST /api/run/<id>/readability/ask`` (``kind`` ``"readability"``)
        or ``/analysis/ask`` (``"analyze"``) ``{question, selection?, view?,
        node_id?}`` (the run's owner or the operator): 202 ``{ask_id}``; the
        answer lands in ``run["readability"]["asks"]`` or
        ``run["analyze"]["asks"]``."""
        from agent_monitor import analyze_runs, readability_runs
        from agent_monitor.analyze_runs import AnalyzeRequestError

        analysis = kind == "analyze"
        record = _run_record_for(rid)
        if not (analyze_runs.is_analyze_record(record) if analysis else readability_runs.is_readability_record(record)):
            self._send(400, json.dumps({"error": "Only Analyze runs answer questions about an analysis" if analysis
                                        else "Only readability runs answer questions about a paper"}))
            return
        if user.get("guest") and job_manager.ask_count(record) >= analyze_runs.GUEST_ASKS_PER_RUN:
            message = analyze_runs.GUEST_ASK_LIMIT_MESSAGE if analysis else readability_runs.GUEST_ASK_LIMIT_MESSAGE
            self._send(403, json.dumps({
                "error": message.format(limit=analyze_runs.GUEST_ASKS_PER_RUN),
                "login_required": True,
            }))
            return
        client = self._guest_client_key() if user.get("guest") else None
        error, retry_after = _reserve_ask(user, client)
        if error:
            self._send(429, json.dumps({"error": error}), headers={"Retry-After": str(retry_after)})
            return
        start = job_manager.start_analysis_ask if analysis else job_manager.start_readability_ask
        try:
            result = start(rid, body if isinstance(body, dict) else {}, user=user)
        except AnalyzeRequestError as exc:
            _release_ask(user, client)
            self._send_analyze_error(exc)
            return
        except FileNotFoundError:
            _release_ask(user, client)
            self._send(404, json.dumps({"error": "run not found"}))
            return
        except ValueError as exc:
            _release_ask(user, client)
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return
        except Exception as exc:  # noqa: BLE001
            _release_ask(user, client)
            self._send(500, json.dumps({"error": f"The question could not be sent: {exc}"}, ensure_ascii=False))
            return
        self._send(202, json.dumps(result, ensure_ascii=False))

    def _paper_map_payload(self, rid: str, ws: Path) -> dict:
        """``kind=paper-map``: a readability run's paper map (deterministic,
        sanitized and bounded; no model call)."""
        from agent_monitor.readability import paper_map

        graph = paper_map.load(ws)
        if graph is None:
            return {"status": "none", "kind": "paper-map"}
        block = (_run_record_for(rid) or {}).get("readability")
        summary = (block or {}).get("summary") if isinstance(block, dict) else None
        return {
            "status": "ready",
            "kind": "paper-map",
            "schema": graph["schema"],
            "source": "readability/paper_map.json",
            "certificate": False,
            "basis": "manuscript structure (labels and references) with a model's reading guide; not a certificate",
            "summary": {
                "nodes": len(graph["nodes"]),
                "edges": len(graph["edges"]),
                "main_results": [node["id"] for node in graph["nodes"] if node["main"]],
                "parts": summary if isinstance(summary, dict) else None,
            },
            "graph": {"title": graph["title"], "nodes": graph["nodes"], "edges": graph["edges"]},
            "macros": graph["macros"],
        }

    def _resolve_analyze_link(self, body: dict, user: dict) -> None:
        """``POST /api/analyze/resolve {url}``: metadata only, at most 10 s.
        Shared by the Analyze and Readability source boxes: open while either
        mode is."""
        from agent_monitor import analyze_runs, readability_runs

        refusal = analyze_runs.availability_error(user)
        if refusal is not None and readability_runs.availability_error(user) is None:
            refusal = None
        if refusal is not None:
            self._send_analyze_error(refusal)
            return
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            self._send(400, json.dumps({"error": "Paste a link to preview"}))
            return
        if len(url) > analyze_runs.MAX_URL_CHARS:
            self._send(400, json.dumps({"error": "The link is too long"}))
            return
        # A link refused without any lookup (mistyped, half-typed, member-only)
        # is answered at once and does not count against the preview limit.
        refusal = analyze_runs.preview_refusal(url.strip(), role=analyze_runs.role_of(user))
        if refusal is not None:
            self._send(200, json.dumps(refusal, ensure_ascii=False), headers={"Cache-Control": "no-store"})
            return
        error, retry_after = _reserve_analyze_resolve(user, self._guest_client_key())
        if error:
            self._send(429, json.dumps({"error": error}), headers={"Retry-After": str(retry_after)})
            return
        result = analyze_runs.resolve_link(url.strip(), role=analyze_runs.role_of(user))
        self._send(200, json.dumps(result, ensure_ascii=False), headers={"Cache-Control": "no-store"})

    def _start_analysis_recheck(self, rid: str, body: dict, user: dict) -> None:
        """``POST /api/run/<id>/analysis/recheck {node_id}`` (owner or admin)."""
        from agent_monitor import analyze_runs

        node_id = body.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            self._send(400, json.dumps({"error": "node_id is required"}))
            return
        ws = _workspace_for_run(rid)
        if ws is None:
            self._send(404, json.dumps({"error": "run not found"}))
            return
        record = _run_record_for(rid)
        if not analyze_runs.is_analyze_record(record):
            self._send(400, json.dumps({"error": "Only Analyze runs have nodes to re-check"}))
            return
        if str((record or {}).get("status") or "") == "running" or job_manager.run_is_active(rid):
            # Refused before a guest's re-check, derived action or a member's
            # start is spent (jobs refuses it again under its launch claim).
            self._send(409, json.dumps({"error": job_manager.RECHECK_BUSY}))
            return
        slot: _GuestDerivedSlot | None = None
        member_admitted = False
        guest_window: tuple[int, str | None] | None = None
        if user.get("guest"):
            # A guest re-check spends sponsored calls from the guest Analyze
            # allowance: a few per analysis (jobs re-checks this under the
            # launch claim), per hour and per network, then the guest derived
            # limits like any Formal or DAG action.
            if job_manager.recheck_count(record) >= analyze_runs.GUEST_RECHECKS_PER_ANALYSIS:
                self._send(403, json.dumps({
                    "error": analyze_runs.GUEST_RECHECK_LIMIT_MESSAGE.format(
                        limit=analyze_runs.GUEST_RECHECKS_PER_ANALYSIS),
                    "login_required": True,
                }))
                return
            user_id, client = int(user["id"]), self._guest_client_key()
            error, retry_after = _reserve_guest_recheck(user_id, client)
            if error:
                self._send(429, json.dumps({"error": error}), headers={"Retry-After": str(retry_after)})
                return
            guest_window = (user_id, client)
            if not self._admit_guest_derived_action(user, ws):
                _release_guest_recheck(*guest_window)
                return
            slot = _GuestDerivedSlot(int(user["id"]))
        elif account_policy.is_member(user):
            # It spends the member's key or sponsored budget like a run start.
            if not self._admit_member_run(user):
                return
            member_admitted = True

        def refused() -> None:
            # Nothing started: the member's hourly start and the guest's
            # re-check allowance come back.
            if member_admitted:
                self._release_member_run(user)
            if guest_window is not None:
                _release_guest_recheck(*guest_window)

        try:
            result = job_manager.start_analysis_recheck(
                rid, node_id.strip(), user=user, on_exit=slot.release if slot is not None else None
            )
        except analyze_runs.AnalyzeRequestError as exc:
            refused()
            self._send_analyze_error(exc)
            return
        except FileNotFoundError:
            refused()
            self._send(404, json.dumps({"error": "run not found"}))
            return
        except ValueError as exc:
            refused()
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return
        except Exception as exc:  # noqa: BLE001
            refused()
            self._send(500, json.dumps({"error": f"The re-check could not start: {exc}"}, ensure_ascii=False))
            return
        self._send(200, json.dumps(result, ensure_ascii=False))

    def _analysis_graph_payload(self, rid: str, ws: Path, kind: str) -> dict:
        """``kind=analysis``: the manuscript graph; ``kind=informal``: its
        deterministic projection (no model call either way)."""
        from agent_monitor import proof_graph
        from agent_monitor.analyze import graph as analysis_graph

        record = _run_record_for(rid) or {}
        if kind == "informal":
            return proof_graph.load_or_build_informal(ws, record) or {"status": "none", "kind": "informal"}
        graph = analysis_graph.load(ws)
        if graph is None:
            return {"status": "none", "kind": "analysis"}
        block = record.get("analyze") if isinstance(record.get("analyze"), dict) else {}
        return {
            "status": "ready",
            "kind": "analysis",
            "schema": graph.get("schema"),
            "generated_at": graph.get("generated_at"),
            "source": "analysis/analysis_graph.json",
            "basis": analysis_graph.BASIS,
            "certificate": False,
            "summary": block.get("summary"),
            "graph": graph,
        }

    def _session_cookie_header(
        self, token: str | None, *, guest: bool = False
    ) -> str:
        """Set-Cookie value; token=None clears the cookie."""
        from agent_monitor.auth import (
            GUEST_SESSION_TTL_HOURS,
            SESSION_COOKIE,
            SESSION_TTL_DAYS,
        )

        # Only mark Secure on HTTPS. Using Host!=localhost used to drop cookies
        # for anyone opening the console over plain HTTP on a public IP.
        secure = " Secure;" if self._is_https() else ""
        cookie_path = self._request_base_path() or "/"
        if token is None:
            return f"{SESSION_COOKIE}=; Path={cookie_path}; Max-Age=0; HttpOnly;{secure} SameSite=Lax"
        max_age = (
            GUEST_SESSION_TTL_HOURS * 3600 if guest else SESSION_TTL_DAYS * 86400
        )
        return f"{SESSION_COOKIE}={token}; Path={cookie_path}; Max-Age={max_age}; HttpOnly;{secure} SameSite=Lax"

    def _require_user(self, path: str) -> dict | None:
        """Return the user, or answer 401/redirect and return None."""
        user = self._current_user()
        if user:
            return user
        if PUBLIC_MODE and guest_access_enabled():
            return self._issue_guest()
        login = _with_base("/login", self._request_base_path())
        if path.startswith("/api/"):
            self._send(401, json.dumps({"error": "not signed in", "login": login}))
        else:
            self.send_response(302)
            self.send_header("Location", login)
            self.end_headers()
        return None

    def _send_html(self, html: str, code: int = 200, headers: dict[str, str] | None = None):
        self._send(
            code,
            _inject_base(html, self._request_base_path()),
            "text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "CDN-Cache-Control": "no-store",
                **(headers or {}),
            },
        )

    def _request_base_path(self) -> str:
        return getattr(self, "_resolved_base_path", BASE_PATH)

    def _resolve_path(self) -> str | None:
        """Parse the URL, enforce the configured gate, and record its mount."""
        app_path, request_base_path = _app_path(urlparse(self.path).path, BASE_PATH)
        self._resolved_base_path = request_base_path
        if app_path is None:
            self._send(404, "Not Found", "text/plain; charset=utf-8")
            return None
        return app_path

    def _request_base_path(self) -> str:
        return getattr(self, "_resolved_base_path", BASE_PATH)

    def _password_reset_base_url(self) -> str | None:
        """Return a non-user-controlled base URL for links sent by email."""
        configured = os.environ.get("AGENT_MONITOR_APP_URL", "").strip()
        if configured:
            base_url = _canonical_app_base(configured, self._request_base_path())
            if base_url is None:
                logger.error("AGENT_MONITOR_APP_URL is invalid; password reset email not sent")
            return base_url

        # Local development can work without extra configuration. Public
        # deployments must set AGENT_MONITOR_APP_URL so Host headers cannot
        # poison emailed reset links.
        host = (self.headers.get("Host") or "").strip()
        try:
            parsed_host = urlsplit(f"//{host}")
        except ValueError:
            parsed_host = None
        if parsed_host and parsed_host.hostname in {"localhost", "127.0.0.1", "::1"}:
            scheme = "https" if self._is_https() else "http"
            return _canonical_app_base(
                f"{scheme}://{host}",
                self._request_base_path(),
            )
        logger.error("AGENT_MONITOR_APP_URL is required for public password reset email")
        return None

    def _owns_run(self, run_id: str, user: dict) -> bool:
        if research_projects.can_read_run(run_id, user.get("id")):
            return True
        owner = job_manager.run_owner(run_id)
        if owner is None:
            return _is_operator_user(user)  # legacy runs belong to the operator
        return owner == user.get("id") or _is_operator_user(user)

    def _is_run_owner(self, run_id: str, user: dict) -> bool:
        """Owner or operator only: project membership grants access, not deletion."""
        owner = job_manager.run_owner(run_id)
        if owner is None:
            return _is_operator_user(user)
        return owner == user.get("id") or _is_operator_user(user)

    def _deny_run_change(self, run_id: str, user: dict, change: str) -> bool:
        """Refuse (403) a change to a run that ``user`` may only read.

        Project membership grants read access only: changing a run, or
        spending compute and credentials on it, stays with its owner and the
        operator.  Callers check ``_owns_run`` (404) first.
        """
        if self._is_run_owner(run_id, user):
            return False
        self._send(
            403,
            json.dumps({"error": f"Only the member who started this run can {change}."}),
        )
        return True

    def _send_bytes(self, code: int, body: bytes, ctype: str, *, download_name: str | None = None, headers: dict[str, str] | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if download_name:
            fallback = re.sub(r'[^A-Za-z0-9_.-]', '_', download_name)
            self.send_header("Content-Disposition", f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(download_name, safe="")}')
        for k, v in self._merge_headers(headers).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _reject_cross_origin_write(self) -> bool:
        """Refuse a state-changing request that a browser sent from elsewhere.

        Runs before any body is read, for every POST and DELETE (including the
        unauthenticated cookie-setting auth routes, where SameSite=Lax gives
        no protection). A browser always labels such requests: a foreign
        ``Origin`` or ``Sec-Fetch-Site: cross-site``/``same-site``. HTML forms
        can only send the three form content types, which no console client
        uses. Non-browser clients (no Origin, no Sec-Fetch-Site) are unaffected.
        """
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        origin = self.headers.get("Origin")
        foreign = site in {"cross-site", "same-site"}
        if origin is not None:
            try:
                origin_host = urlsplit(origin.strip()).netloc.lower()
            except ValueError:
                origin_host = ""
            host = (self.headers.get("Host") or "").strip().lower()
            if not origin_host or origin_host != host:
                foreign = True
        if foreign:
            self.close_connection = True
            self._send(
                403,
                json.dumps({"error": "Cross-origin requests are not accepted; use the console"}),
                headers={"Cache-Control": "no-store"},
            )
            return True
        if (
            self.headers.get("Content-Type") is not None
            and self.headers.get_content_type() in _FORM_CONTENT_TYPES
        ):
            self.close_connection = True
            self._send(
                415,
                json.dumps({"error": "Expected application/json"}),
                headers={"Cache-Control": "no-store"},
            )
            return True
        return False

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, "")

    @_track_request
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = self._resolve_path()
        if path is None:
            return
        qs = parse_qs(parsed.query)

        if self._research_route(path, "GET") or self._sharing_get(path) or self._projects_route(path, "GET"):
            return

        if path.rstrip("/") in ("/login", "/reset-password", "/verify-email"):
            if (
                path.rstrip("/") == "/login"
                and PUBLIC_MODE
                and guest_access_enabled()
            ):
                self.send_response(302)
                self.send_header("Location", _with_base("/", self._request_base_path()))
                self.end_headers()
                return
            html_path = WEB_DIR / "login.html"
            # Sign in with Google identifies the page by its origin, which a
            # no-referrer policy withholds; cross-origin requests still get only
            # the origin, never the path or a token in the query string.
            self._send_html(
                html_path.read_text(encoding="utf-8"),
                headers={"Referrer-Policy": "strict-origin-when-cross-origin"},
            )
            return

        if path == "/api/auth/verify-reset-token":
            from agent_monitor.auth import verify_reset_token

            token = (qs.get("token") or [""])[0].strip()
            res = verify_reset_token(token)
            status = 200 if res.get("valid") else 400
            self._send(
                status,
                json.dumps(res),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
            return

        if path == "/api/auth/me":
            from agent_monitor.auth import (
                email_delivery_configured,
                email_verification_required,
                google_client_id,
                registration_email_suffixes,
                registration_open,
            )

            user = self._current_user()
            if user is None and PUBLIC_MODE and guest_access_enabled():
                user = self._issue_guest()
                if user is None:
                    return
            self._send(
                200,
                json.dumps(
                    {
                        "user": user,
                        "google_client_id": google_client_id(),
                        "public": PUBLIC_MODE,
                        "guest_access": guest_access_enabled(),
                        "guest_policy": guest_policy(user),
                        "registration_open": registration_open(),
                        "registration_email_suffixes": list(registration_email_suffixes()),
                        "email_verification_required": email_verification_required(),
                        "password_reset_available": email_delivery_configured(),
                    }
                ),
                headers={"Cache-Control": "no-store"},
            )
            return

        if path == "/health":
            self._send(200, json.dumps({"ok": True, "service": "agent-monitor-console", "deployment": _deployment_activity(exclude_current=True)}))
            return

        if path.startswith("/static/"):
            rel = path.removeprefix("/static/")
            f = _static_asset(rel)
            if f is not None:
                ctype = {".css": "text/css", ".js": "application/javascript", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf"}.get(f.suffix, "application/octet-stream")
                self._send_bytes(200, f.read_bytes(), ctype)
                return
            self._send(404, json.dumps({"error": "not found"}))
            return

        user = self._require_user(path)
        if user is None:
            return

        attachment_route = re.fullmatch(r"/api/runs/([^/]+)/attachments/([a-f0-9]{32})", path)
        if attachment_route:
            run_id, attachment_id = attachment_route.groups()
            if not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "not found"}))
                return
            workspace = RUNS_DIR / "workspaces" / run_id
            item = next((item for item in research_attachments.list_files(workspace)
                         if item["id"] == attachment_id), None)
            if item is None:
                self._send(404, json.dumps({"error": "not found"}))
                return
            file = research_attachments.file_path(workspace, item)
            if not file.is_file():
                self._send(404, json.dumps({"error": "file not found"}))
                return
            self._send_bytes(200, file.read_bytes(), item["type"],
                             download_name=item["name"], headers={"Cache-Control": "private, no-store"})
            return

        if path in ("/", "/index.html", "/console"):
            html_path = WEB_DIR / "console.html"
            self._send_html(html_path.read_text(encoding="utf-8"))
            return

        if path in ("/monitor", "/dashboard"):
            html = (DASHBOARD_WEB / "index.html").read_text(encoding="utf-8")
            self._send_html(html)
            return

        if path == "/api/engines":
            from agent_monitor import sponsored_kimi_client
            from agent_monitor.engines_registry import list_engines

            engines = sponsored_kimi_client.with_degraded_notice(list_engines(), user)
            if user.get("guest"):
                allowed = guest_engine_allowlist()
                # Signing up gives a member account, so an engine members
                # cannot run is administrator-only, not "after sign-in".
                after_sign_in = allowed | account_policy.member_engine_allowlist()
                engines = [
                    {
                        **engine,
                        "requires_login": engine.get("id") not in allowed,
                        # A guest, and the member it becomes, bills an API key.
                        "auth_modes": [mode for mode in engine.get("auth_modes") or [] if mode == "api_key"],
                    }
                    if engine.get("id") in after_sign_in
                    else account_policy.admin_only_engine_view(engine)
                    for engine in engines
                ]
            elif account_policy.is_restricted(user):
                # Local harnesses are administrator-only; the rest bill an API
                # key (the CGM harness only the member's own, never Kimi K3).
                engines = [account_policy.member_engine_view(engine) for engine in engines]
            elif account_policy.has_full_access(user):
                engines = [account_policy.full_access_engine_view(engine) for engine in engines]
            # The Analyze and Readability engines carry this viewer's policy
            # for their mode, so the browser offers a mode only as this
            # server accepts it.
            engines = [
                {**engine, "analyze": analyze_policy(user)}
                if engine.get("id") == "analyze"
                else {**engine, "readability": readability_policy(user)}
                if engine.get("id") == "readability"
                else engine
                for engine in engines
            ]
            self._send(200, json.dumps({"engines": engines}))
            return

        if path == "/api/monitor/overview":
            if self._deny_guest(user, "the operator overview"):
                return
            from agent_monitor.monitor_overview import build_overview

            self._send(200, json.dumps(build_overview(user), ensure_ascii=False))
            return

        if path == "/api/problems":
            man = PROBLEMS_DIR / "manifest.json"
            if man.exists():
                self._send(200, man.read_text(encoding="utf-8"))
            else:
                self._send(200, json.dumps({"problems": []}))
            return

        if path == "/api/jobs":
            # Everyone's workspace lists only their own jobs; operators see
            # other accounts through the admin view or with scope=all.
            scope_all = (qs.get("scope") or [""])[0] == "all" and _is_operator_user(user)
            owner = None if scope_all else user["id"]
            self._send(200, json.dumps({"jobs": job_manager.list_jobs(owner_id=owner)}))
            return

        if path == "/api/admin/accounts":
            if not self._require_operator(user):
                return
            self._send(200, json.dumps(self._accounts_payload(user), ensure_ascii=False),
                       headers={"Cache-Control": "no-store"})
            return

        if path in ("/api/admin/usage", "/api/admin/runs"):
            if not self._require_operator(user):
                return
            from agent_monitor import usage_ledger

            params = {key: (values[0] if values else "") for key, values in qs.items()}
            try:
                payload = (
                    usage_ledger.usage_summary(params)
                    if path == "/api/admin/usage"
                    else usage_ledger.list_runs(params)
                )
            except ValueError as exc:
                self._send(400, json.dumps({"error": f"bad filter: {exc}"}))
                return
            self._send(200, json.dumps(payload, ensure_ascii=False), headers={"Cache-Control": "no-store"})
            return

        if path == "/api/agent/config":
            if self._deny_guest(user, "the shared agent profile"):
                return
            from agent_monitor import agent_config

            self._send(200, json.dumps(agent_config.get_agent_config(), ensure_ascii=False))
            return

        if path == "/api/settings":
            from agent_monitor.settings import get_settings

            payload = get_settings(user)
            if user.get("guest"):
                payload["max_iterations"] = guest_max_iterations()
                payload["guest_policy"] = guest_policy(user)
            self._send(200, json.dumps(payload, ensure_ascii=False))
            return

        if path == "/api/settings/codex/status":
            if self._deny_guest(user, "account connections"):
                return
            if account_policy.is_restricted(user):
                self._send(200, json.dumps(_member_subscription_status("codex"), ensure_ascii=False))
                return
            from agent_monitor import codex_login

            self._send(200, json.dumps(codex_login.status(user["id"]), ensure_ascii=False))
            return

        if path == "/api/settings/codex/login/poll":
            if self._deny_guest(user, "account connections"):
                return
            if account_policy.is_restricted(user):
                self._send(200, json.dumps(_member_subscription_status("codex"), ensure_ascii=False))
                return
            from agent_monitor import codex_login

            self._send(200, json.dumps(codex_login.poll_login(user["id"]), ensure_ascii=False))
            return

        if path == "/api/settings/claude/status":
            if self._deny_guest(user, "account connections"):
                return
            if account_policy.is_restricted(user):
                self._send(200, json.dumps(_member_subscription_status("claude"), ensure_ascii=False))
                return
            from agent_monitor import claude_login

            self._send(200, json.dumps(claude_login.status(user["id"]), ensure_ascii=False))
            return

        if path == "/api/settings/claude/login/poll":
            if self._deny_guest(user, "account connections"):
                return
            if account_policy.is_restricted(user):
                self._send(200, json.dumps(_member_subscription_status("claude"), ensure_ascii=False))
                return
            from agent_monitor import claude_login

            self._send(200, json.dumps(claude_login.poll_login(user["id"]), ensure_ascii=False))
            return

        if path == "/api/library":
            if self._deny_guest(user, "the shared library"):
                return
            from agent_monitor import library

            payload = library.get_library()
            if not _is_operator_user(user):
                # Members read the shared profile, not records of the
                # operator's own runs (each quotes its problem and answer).
                payload = library.without_run_memories(payload)
            self._send(200, json.dumps(payload, ensure_ascii=False))
            return

        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/")
            job = job_manager.get_job(job_id)
            if not job or (
                job.get("owner_id") != user["id"] and not _is_operator_user(user)
            ):
                self._send(404, json.dumps({"error": "job not found"}))
                return
            self._send(200, json.dumps(job))
            return

        if path.startswith("/api/runs/") and path.endswith("/chat"):
            run_id = path.removeprefix("/api/runs/").removesuffix("/chat")
            if not run_id or "/" in run_id or not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "not found"}))
                return
            try:
                self._send(200, json.dumps({"messages": job_manager.list_chat(run_id)}, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                self._send(404, json.dumps({"error": str(exc)}))
            return

        if path.startswith("/api/workspace/"):
            self._handle_workspace(path, qs, user)
            return

        if path.startswith("/api/run/") and path.endswith("/proof_graph"):
            rid = path.removeprefix("/api/run/").removesuffix("/proof_graph")
            ws = _workspace_for_run(rid)
            if not ws or not self._owns_run(rid, user):
                self._send(404, json.dumps({"error": "run not found"}))
                return
            kind = (qs.get("kind") or ["informal"])[0].strip().lower()
            if kind not in {"informal", "formal", "engine", "analysis", "paper-map"}:
                kind = "informal"
            from agent_monitor import analyze_runs, readability_runs

            if analyze_runs.is_analyze_record(_run_record_for(rid)) and kind in {"informal", "analysis"}:
                self._send(200, json.dumps(self._analysis_graph_payload(rid, ws, kind), ensure_ascii=False))
                return
            if kind == "analysis":
                self._send(400, json.dumps({"error": "This run has no manuscript analysis"}))
                return
            if kind == "paper-map":
                if not readability_runs.is_readability_record(_run_record_for(rid)):
                    self._send(400, json.dumps({"error": "This run has no paper map"}))
                    return
                self._send(200, json.dumps(self._paper_map_payload(rid, ws), ensure_ascii=False))
                return
            try:
                if kind == "engine":
                    from agent_monitor import engine_graph

                    view = (qs.get("view") or ["auto"])[0].strip().lower() or "auto"
                    cached = engine_graph.load_or_build(
                        ws, _run_record_for(rid), view=view
                    )
                else:
                    from agent_monitor import proof_graph

                    if kind == "formal":
                        cached = proof_graph.load_or_parse_formal(ws)
                    else:
                        # A CGM fact DAG yields the informal DAG without a
                        # model call; otherwise only the cached graph is read.
                        cached = proof_graph.load_or_build_informal(ws)
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            self._send(200, json.dumps(cached or {"status": "none", "kind": kind}, ensure_ascii=False))
            return

        if path == "/api/lean/status":
            from agent_monitor import lean_verify

            toolchain = lean_verify.toolchain_status()
            if user.get("guest"):
                # Read-only and host-neutral: whether the sandboxed Lean broker
                # is installed and which fixed toolchain it pins, never local
                # paths or harness discovery (and no guest action is spent).
                toolchain = {
                    key: toolchain[key]
                    for key in _GUEST_LEAN_STATUS_FIELDS
                    if key in toolchain
                }
                formal_options = _guest_formal_options()
            else:
                model_options = lean_verify.harness_model_options(user)
                # The coding-agent harness is the operator's alone (the POST
                # refuses it to members), so a member is offered none.
                engines = (
                    []
                    if account_policy.is_restricted(user)
                    else lean_verify.harness_engines(user, model=model_options["selected"])
                )
                formal_options = {
                    "engines": engines,
                    "harness_auth_route": model_options["auth_route"],
                    "harness_models": model_options["models"],
                    "harness_model": model_options["selected"],
                }
            self._send(
                200,
                json.dumps(
                    {
                        **toolchain,
                        **formal_options,
                    },
                    ensure_ascii=False,
                ),
            )
            return

        if path.startswith("/api/run/") and path.endswith("/lean_verify"):
            from agent_monitor import lean_verify

            rid = path.removeprefix("/api/run/").removesuffix("/lean_verify")
            ws = _workspace_for_run(rid)
            if not ws or not self._owns_run(rid, user):
                self._send(404, json.dumps({"error": "run not found"}))
                return
            run_record = _derived_record_for(rid)
            cached = lean_verify.load_cached(ws) or {"status": "none"}
            try:
                if user.get("guest"):
                    cached.update(_guest_formal_options())
                else:
                    model_options = lean_verify.harness_model_options(user, run_record)
                    engines = (
                        []
                        if account_policy.is_restricted(user)
                        else lean_verify.harness_engines(user, run_record)
                    )
                    cached["engines"] = engines
                    cached["harness_auth_route"] = model_options["auth_route"]
                    cached["harness_models"] = model_options["models"]
                    cached["harness_model"] = model_options["selected"]
                    cached["harness_engine"] = _preferred_formal_harness_engine(
                        run_record, engines
                    )
            except ValueError as exc:
                cached["engines"] = []
                cached["harness_models"] = []
                cached["harness_engine"] = ""
                cached["harness_route_error"] = str(exc)
            # Reconcile both old and current caches: this backfills source
            # anchors, removes harmless Markdown duplicates, and drops legacy
            # inequality fragments that were once misread as references.
            if cached.get("lean"):
                cached["citations"] = lean_verify._reconcile_citations(
                    cached.get("citations"),
                    str(cached.get("lean") or ""),
                    workspace=ws,
                )
            if cached.get("status") not in (None, "none") and "toolchain" not in cached:
                cached["toolchain"] = lean_verify.toolchain_status()
            job = lean_verify.harness_job(ws)
            cached["harness_running"] = bool(job)
            if job:
                cached["harness"] = {**(cached.get("harness") or {}), **job}
            elif cached.get("status") == "running":
                # The worker died without writing a final record (restart, OOM).
                # Persist that too: a running record counts as active work.
                lean_verify.reconcile_orphaned_harness(ws)
                cached["status"] = "failed"
                cached["notes"] = lean_verify.HARNESS_INTERRUPTED_NOTE
            self._send(200, json.dumps(cached, ensure_ascii=False))
            return

        # Delegate classic dashboard APIs
        if path in ("/api/runs", "/api/rebuild") or path.startswith("/api/run/") or path == "/api/call_detail":
            self._proxy_dashboard_get(path, qs, user)
            return

        self._send(404, json.dumps({"error": "not found"}))

    def _handle_workspace(self, path: str, qs: dict, user: dict):
        # /api/workspace/{run_id}/files | /file?path= | /pdf?path=
        rest = path.removeprefix("/api/workspace/")
        parts = rest.split("/", 1)
        run_id = parts[0]
        action = parts[1] if len(parts) > 1 else "files"
        ws = _workspace_for_run(run_id)
        if not ws or not self._owns_run(run_id, user):
            self._send(404, json.dumps({"error": "workspace not found", "run_id": run_id}))
            return

        if action == "files":
            self._send(200, json.dumps({"run_id": run_id, "files": _list_workspace_files(ws)}))
            return

        if action == "pdf" and self._deny_run_compilation(user, run_id):
            return

        rel = (qs.get("path", [""])[0] or "").strip()
        target = _workspace_target(ws, rel) if rel else None
        if target is None:
            self._send(400, json.dumps({"error": "bad path"}))
            return

        if action == "file":
            if not target.exists() or not target.is_file():
                self._send(404, json.dumps({"error": "file not found"}))
                return
            if target.suffix == ".pdf":
                dl = (qs.get("download", [""])[0] or "").strip() in {"1", "true", "yes"}
                self._send_bytes(
                    200,
                    target.read_bytes(),
                    "application/pdf",
                    download_name=target.name if dl else None,
                )
                return
            text = target.read_text(encoding="utf-8", errors="replace")
            self._send(
                200,
                json.dumps(
                    {
                        "path": rel,
                        "mtime": target.stat().st_mtime,
                        "revision": _file_revision(target),
                        "size": target.stat().st_size,
                        "content": text[:400_000],
                    },
                    ensure_ascii=False,
                ),
            )
            return

        if action == "pdf":
            pdf = _compile_workspace_pdf(ws, rel)
            if pdf and pdf.exists():
                dl = (qs.get("download", [""])[0] or "").strip() in {"1", "true", "yes"}
                name = Path(rel).with_suffix(".pdf").name if dl else None
                self._send_bytes(200, pdf.read_bytes(), "application/pdf", download_name=name)
            else:
                self._send(404, json.dumps({"error": "pdf compile failed or pdflatex missing"}))
            return

        self._send(404, json.dumps({"error": "unknown workspace action"}))

    @_track_request
    def do_POST(self):  # noqa: N802
        path = self._resolve_path()
        if path is None:
            return
        if self._reject_cross_origin_write():
            return
        # Authenticate before accepting the larger attachment request bodies.
        is_chat = bool(re.fullmatch(r"/api/runs/[^/]+/chat", path))
        accepts_attachments = path == "/api/runs" or is_chat
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = None
        upload_user = self._require_user(path) if accepts_attachments else None
        if accepts_attachments and upload_user is None:
            self._linger_after_refusal(length or 0)
            return
        if is_chat and upload_user.get("guest"):
            # Refused before the (up to 29 MiB) body is read or parsed.
            self.close_connection = True
            self._deny_guest(upload_user, "run continuation")
            self._linger_after_refusal(length or 0)
            return
        if length is None:
            self.close_connection = True
            self._send(
                400,
                json.dumps({"error": "invalid content length"}),
                headers={"Connection": "close"},
            )
            return
        if accepts_attachments and not upload_user.get("guest"):
            max_body = research_attachments.MAX_REQUEST_BYTES
        elif accepts_attachments:
            max_body = GUEST_MAX_REQUEST_BYTES
        else:
            max_body = 1_000_000
        if length < 0 or length > max_body:
            self.close_connection = True
            self._send(
                413,
                json.dumps({"error": "request body too large"}),
                headers={"Connection": "close"},
            )
            self._linger_after_refusal(length)
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, json.dumps({"error": "invalid json"}))
            return
        if not isinstance(body, dict):
            self._send(400, json.dumps({"error": "JSON body must be an object"}))
            return

        if self._research_route(path, "POST", body) or self._sharing_write(path, "POST", body) or self._projects_route(path, "POST", body):
            return

        if path == "/api/auth/guest":
            if not guest_access_enabled():
                self._send(
                    403,
                    json.dumps({"ok": False, "error": "Guest access is disabled"}),
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )
                return
            current = self._current_user()
            explicit_switch = (
                body.get("switch_account") is True
                and self.headers.get("X-Guest-Intent") == "homepage"
                and self.headers.get_content_type() == "application/json"
            )
            if current is not None:
                if current.get("guest"):
                    self._send(
                        200,
                        json.dumps({"ok": True, "user": current}),
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                    )
                    return
                elif not explicit_switch:
                    self._send(
                        409,
                        json.dumps({"ok": False, "error": "Already signed in"}),
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                    )
                    return
            account = self._issue_guest(
                replace_account_token=self._session_token() if current and explicit_switch else None
            )
            if account is None:
                return
            self._send(
                200,
                json.dumps({"ok": True, "user": account}),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
            return

        if path in ("/api/auth/register", "/api/auth/login", "/api/auth/google"):
            from agent_monitor import auth

            prior_token = self._session_token()
            # Inspect the cookie directly for cleanup. `_current_user()` hides
            # existing guests when access has just been disabled, but a
            # successful account sign-in should still retire that abandoned
            # guest identity and its encrypted key.
            prior_user = auth.user_for_token(prior_token)
            try:
                token = None
                if path == "/api/auth/register":
                    if auth.registration_open():
                        admitted, retry_after = _reserve_signup_slot(self._guest_client_key())
                        if not admitted:
                            self._send(
                                429,
                                json.dumps({"error": "Too many new accounts right now; please try again later"}),
                                headers={"Retry-After": str(retry_after)},
                            )
                            return
                    account = auth.register(
                        str(body.get("email") or ""),
                        str(body.get("password") or ""),
                        name=body.get("name"),
                        require_open=True,
                    )
                elif path == "/api/auth/login":
                    account, token = auth.login_and_create_session(
                        str(body.get("email") or ""),
                        str(body.get("password") or ""),
                    )
                else:
                    account = auth.login_with_google(
                        str(body.get("credential") or ""),
                        require_open_for_new=True,
                    )
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except auth.EmailVerificationRequired as exc:
                self._send(
                    403,
                    json.dumps({"error": str(exc), "verification_required": True, "email": exc.email}, ensure_ascii=False),
                )
                return
            except PermissionError as exc:
                self._send(403, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except auth.EmailDeliveryUnavailable as exc:
                self._send(503, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": f"Sign-in failed: {exc}"}, ensure_ascii=False))
                return
            if (
                path == "/api/auth/register"
                and auth.email_verification_required()
                and auth.email_verification_pending(int(account["id"]))
            ):
                # The account exists but cannot sign in until its address is confirmed.
                base_url = self._password_reset_base_url()
                if base_url:
                    _enqueue_email_verification(account["email"], base_url)
                self._send(
                    200,
                    json.dumps(
                        {"ok": True, "verification_required": True, "email": account["email"]},
                        ensure_ascii=False,
                    ),
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )
                return
            if token is None:
                token = auth.create_session(int(account["id"]))
            if prior_user and prior_user.get("guest"):
                auth.destroy_session(prior_token)
            self._send(
                200,
                json.dumps({"ok": True, "user": account}, ensure_ascii=False),
                headers={"Set-Cookie": self._session_cookie_header(token)},
            )
            return

        if path == "/api/auth/logout":
            from agent_monitor import auth

            auth.destroy_session(self._session_token())
            self._send(200, json.dumps({"ok": True}), headers={"Set-Cookie": self._session_cookie_header(None)})
            return
        if path == "/api/auth/verify-email":
            from agent_monitor import auth

            try:
                result = auth.verify_email(str(body.get("token") or "").strip())
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )
                return
            self._send(
                200,
                json.dumps(result, ensure_ascii=False),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
            return

        if path == "/api/auth/resend-verification":
            email = (body.get("email") or "").strip().lower()
            base_url = self._password_reset_base_url()
            if email and base_url:
                _enqueue_email_verification(email, base_url)
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "message": "If that account still needs verification, a new link is on its way. It can take a few minutes; check your spam folder too.",
                    }
                ),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
            return

        if path == "/api/auth/forgot-password":
            email = (body.get("email") or "").strip().lower()
            base_url = self._password_reset_base_url()
            if email and base_url:
                _enqueue_password_reset(email, base_url)
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "success": True,
                        "message": "If that email exists, a reset link has been sent.",
                    }
                ),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
            return

        if path == "/api/auth/reset-password":
            from agent_monitor.auth import handle_reset_password

            token = (body.get("token") or "").strip()
            new_password = str(body.get("new_password") or "")

            if not token or not new_password:
                self._send(
                    400,
                    json.dumps({"ok": False, "success": False, "error": "Missing required fields"}),
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )
                return
            if len(new_password) < 8:
                self._send(
                    400,
                    json.dumps(
                        {
                            "ok": False,
                            "success": False,
                            "error": "Password must be at least 8 characters",
                        }
                    ),
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )
                return

            success = handle_reset_password(token, new_password)
            if not success:
                self._send(
                    400,
                    json.dumps(
                        {
                            "ok": False,
                            "success": False,
                            "error": "Invalid or expired reset token",
                        }
                    ),
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )
                return

            self._send(
                200,
                json.dumps({"ok": True, "success": True, "message": "Password updated successfully"}),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
            return
        user = upload_user if accepts_attachments else self._require_user(path)
        if user is None:
            return

        if path in ("/api/admin/accounts", "/api/admin/accounts/access", "/api/admin/accounts/full-access",
                    "/api/admin/accounts/group", "/api/admin/accounts/password"):
            if not self._require_operator(user):
                return
            self._admin_accounts_post(path, body if isinstance(body, dict) else {}, user)
            return

        if path == "/api/analyze/resolve":
            self._resolve_analyze_link(body, user)
            return

        if path.startswith("/api/run/") and path.endswith("/analysis/recheck"):
            rid = path.removeprefix("/api/run/").removesuffix("/analysis/recheck")
            if not rid or "/" in rid or not self._owns_run(rid, user):
                self._send(404, json.dumps({"error": "run not found"}))
                return
            if self._deny_run_change(rid, user, "re-check its results"):
                return
            self._start_analysis_recheck(rid, body, user)
            return

        for suffix, kind in (("/readability/ask", "readability"), ("/analysis/ask", "analyze")):
            if path.startswith("/api/run/") and path.endswith(suffix):
                rid = path.removeprefix("/api/run/").removesuffix(suffix)
                if not rid or "/" in rid or not self._owns_run(rid, user):
                    self._send(404, json.dumps({"error": "run not found"}))
                    return
                # Project members may read the questions and answers, not ask.
                if self._deny_run_change(rid, user, "ask about it"):
                    return
                self._start_manuscript_ask(rid, body, user, kind)
                return

        if path.startswith("/api/library/tools/") and path.endswith("/run"):
            if self._deny_guest(user, "trusted library tools"):
                return
            from urllib.parse import unquote

            from agent_monitor import library

            item_id = unquote(
                path.removeprefix("/api/library/tools/").removesuffix("/run")
            ).strip("/")
            if not item_id or "/" in item_id or ".." in item_id:
                self._send(404, json.dumps({"error": "tool not found"}))
                return
            try:
                result = library.run_trusted_tool(item_id, body.get("arguments"))
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"Tool execution failed: {exc}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        if path.startswith("/api/workspace/") and path.endswith("/file"):
            if self._deny_guest(user, "workspace editing"):
                return
            rest = path.removeprefix("/api/workspace/")
            run_id, separator, action = rest.partition("/")
            ws = _workspace_for_run(run_id)
            if action != "file" or not separator or not ws or not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "workspace not found"}))
                return
            if self._deny_run_change(run_id, user, "edit its proof"):
                return
            rel = str(body.get("path") or "proof.md").strip()
            if rel != "proof.md":
                self._send(400, json.dumps({"error": "Only proof.md is editable in Sandbox"}))
                return
            from agent_monitor import analyze_runs, readability_runs

            record = _run_record_for(run_id)
            if readability_runs.is_readability_record(record):
                self._send(400, json.dumps({"error": readability_runs.EDIT_REFUSAL}))
                return
            if analyze_runs.is_analyze_record(record):
                self._send(400, json.dumps({
                    "error": "An analysis has no editable proof; its report is generated from the manuscript checks",
                }))
                return
            content = body.get("content")
            if not isinstance(content, str):
                self._send(400, json.dumps({"error": "content must be text"}))
                return
            code, result = _save_sandbox_proof(
                ws,
                content=content,
                expected_revision=str(body.get("expected_revision") or ""),
                force=body.get("force") is True,
            )
            if code == 200:
                from agent_monitor import auto_pipeline

                auto_pipeline.mark_stale(ws, "proof.md was edited in Sandbox")
            self._send(code, json.dumps(result, ensure_ascii=False))
            return

        if path == "/api/agent/config":
            if not self._require_operator(user):
                return
            from agent_monitor import agent_config

            try:
                if "system_prompt" in body:
                    agent_config.save_system_prompt(str(body.get("system_prompt") or ""))
                if "memory_enabled" in body:
                    agent_config.set_memory_enabled(bool(body.get("memory_enabled")))
                self._send(200, json.dumps({"ok": True}))
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return

        if path == "/api/agent/skills":
            if not self._require_operator(user):
                return
            from agent_monitor import agent_config

            try:
                name = str(body.get("name") or "")
                if "content" in body:
                    agent_config.save_skill(name, str(body.get("content") or ""))
                if "enabled" in body:
                    agent_config.set_skill_enabled(name, bool(body.get("enabled")))
                self._send(200, json.dumps({"ok": True, "skills": agent_config.list_skills()}, ensure_ascii=False))
            except (ValueError, FileNotFoundError) as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return

        if path == "/api/agent/memory":
            if not self._require_operator(user):
                return
            from agent_monitor import agent_config

            try:
                agent_config.save_memory(str(body.get("name") or ""), str(body.get("content") or ""))
                self._send(200, json.dumps({"ok": True}))
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return

        if path.startswith("/api/run/") and path.endswith("/proof_graph"):
            # Guests may refresh DAGs under the bounded guest branch below
            # (Kimi only, derived-action limits, global concurrency).
            from agent_monitor import proof_graph

            rid = path.removeprefix("/api/run/").removesuffix("/proof_graph")
            ws = _workspace_for_run(rid)
            if not ws or not self._owns_run(rid, user):
                self._send(404, json.dumps({"error": "run not found"}))
                return
            if self._deny_run_change(rid, user, "rebuild its graphs"):
                return
            run_record = _derived_record_for(rid)
            kind = str(body.get("kind") or "informal").strip().lower()
            model = str(body.get("model") or "").strip() or None
            from agent_monitor import analyze_runs, readability_runs

            if readability_runs.is_readability_record(run_record) and kind != "engine":
                # The paper map is deterministic: a refresh never spends a
                # model call, and no proof DAG is drawn from a rewrite.
                if kind != "paper-map":
                    self._send(400, json.dumps({"error": "A readability run has a paper map, not a proof DAG"}))
                    return
                self._send(200, json.dumps(self._paper_map_payload(rid, ws), ensure_ascii=False))
                return
            if kind == "paper-map":
                self._send(400, json.dumps({"error": "This run has no paper map"}))
                return
            if analyze_runs.is_analyze_record(run_record) and kind in {"informal", "analysis"}:
                # The manuscript structure is deterministic: a refresh never
                # spends a model call (or a guest's derived action).
                self._send(200, json.dumps(self._analysis_graph_payload(rid, ws, kind), ensure_ascii=False))
                return
            if kind == "analysis":
                self._send(400, json.dumps({"error": "This run has no manuscript analysis"}))
                return
            if (
                kind != "engine"
                and job_manager.tool_free_run(rid, run_record or {})
                and job_manager._route_from_record(run_record) != "api_key"
            ):
                # A tool-free project run: as for its Lean actions, no graph
                # model call off its API route (a subscription agent can read
                # the server's files). The engine view calls no model.
                self._send(403, json.dumps({"error": account_policy.PROJECT_TOOL_FREE_ERROR}))
                return
            if user.get("guest"):
                from agent_monitor.settings import GUEST_DEFAULT_MODEL

                model = GUEST_DEFAULT_MODEL
                if kind not in {"informal", "formal", "engine"}:
                    self._send(400, json.dumps({"error": "Unknown proof graph kind"}))
                    return
                if kind == "engine":
                    # Model-free and deterministic: the same view GET returns,
                    # so a guest refresh never spends a Formal/DAG action.
                    from agent_monitor import engine_graph

                    try:
                        result = engine_graph.load_or_build(
                            ws,
                            run_record,
                            view=str(body.get("view") or "auto").strip().lower() or "auto",
                        )
                    except ValueError as exc:
                        self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                        return
                    self._send(200, json.dumps(result, ensure_ascii=False))
                    return
            if not self._admit_guest_derived_action(user, ws):
                return
            try:
                if kind == "engine":
                    from agent_monitor import engine_graph

                    result = engine_graph.rebuild(
                        ws,
                        run_record,
                        view=str(body.get("view") or "auto").strip().lower() or "auto",
                    )
                else:
                    from agent_monitor import proof_graph

                    result = proof_graph.generate(
                        workspace=ws,
                        run_record=run_record,
                        user=user,
                        model=model,
                        kind=kind,
                    )
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"Graph generation failed: {exc}"}, ensure_ascii=False))
                return
            finally:
                if user.get("guest"):
                    _release_guest_derived_action(int(user["id"]))
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        if path.startswith("/api/run/") and path.endswith("/lean_verify"):
            # Guests get the documented bounded actions (Verify, Compile,
            # Check, Audit, feedback revision) under the guest branch below;
            # coding-agent Formal harnesses stay sign-in only.
            rid = path.removeprefix("/api/run/").removesuffix("/lean_verify")
            ws = _workspace_for_run(rid)
            if not ws or not self._owns_run(rid, user):
                self._send(404, json.dumps({"error": "run not found"}))
                return
            if self._deny_run_change(rid, user, "run Lean actions on it"):
                return
            action = str(body.get("action") or "verify").strip().lower()
            model = str(body.get("model") or "").strip() or None
            lean_src = body.get("lean")
            if isinstance(lean_src, str):
                lean_arg: str | None = lean_src
            else:
                lean_arg = None
            feedback = str(body.get("message") or body.get("feedback") or "")
            if action in {"harness", "stop_harness"} and self._deny_member(
                user, "The local formal harness"
            ):
                return
            analysis_lean = None
            from agent_monitor import analyze_runs, readability_runs

            run_record = _run_record_for(rid)
            if readability_runs.is_readability_record(run_record):
                # A rewrite of someone's manuscript is not a proof to formalize.
                self._send(400, json.dumps({"error": "Readability runs have no proof to formalize in Lean"}))
                return
            if action not in {"compile", "check", "stop_harness"}:
                # Model work on a member's run billed to a subscription or Max
                # (started before subscriptions became administrator-only)
                # would bill that sign-in and could fall back to Codex.
                # Compile and Check call no model; _lean_verify_action skips
                # the Formal refresh after them on such a run.
                # A Max (API) run's Lean and graph work is ordinary own-key API
                # work on its worker model (cgm_max.formal_view), which any
                # member may do: only a follow-up re-runs Max (API) itself.
                refusal = account_policy.member_continuation_error(
                    user, job_manager._route_from_record(run_record)
                )
                if refusal:
                    self._send(403, json.dumps({"error": refusal}, ensure_ascii=False))
                    return
                # A tool-free project run: no coding-agent harness, and no
                # model work off its API route (the Codex fallback included).
                if job_manager.tool_free_run(rid, run_record or {}) and (
                    action == "harness"
                    or job_manager._route_from_record(run_record) != "api_key"
                ):
                    self._send(403, json.dumps({"error": account_policy.PROJECT_TOOL_FREE_ERROR}))
                    return
            if analyze_runs.is_analyze_record(run_record):
                # An analysis formalizes the STATEMENTS of selected nodes on the
                # server's Lean 4.14 checker (proofs are not attempted), or
                # compiles/checks the Lean source; nothing reads a report as a proof.
                try:
                    analysis_lean = self._analysis_lean_request(
                        ws, action, body, user=user, record=run_record
                    )
                except analyze_runs.AnalyzeRequestError as exc:
                    self._send_analyze_error(exc)
                    return
            if user.get("guest"):
                from agent_monitor.settings import GUEST_DEFAULT_MODEL

                model = GUEST_DEFAULT_MODEL
                if action in {"harness", "stop_harness"}:
                    self._deny_guest(user, "the local formal harness")
                    return
                if action not in {"verify", "generate", "compile", "check", "audit", "revise"}:
                    self._send(400, json.dumps({"error": "Unknown Lean action"}))
                    return
                if len(lean_arg or "") > GUEST_MAX_LEAN_CHARACTERS:
                    self._send(400, json.dumps({"error": "Lean source is too large for a guest action"}))
                    return
                if len(feedback) > GUEST_MAX_FEEDBACK_CHARACTERS:
                    self._send(400, json.dumps({"error": "Feedback is too large for a guest action"}))
                    return
            if action == "harness" and _pipeline_may_run_lean(ws):
                # Its Lean stage would fail on the harness's reservation, or
                # regenerate over the harness's Proof.lean. Stop is never refused.
                self._send(
                    409,
                    json.dumps({"error": "Wait for the automatic Formal and DAG pipeline to finish"}),
                    headers={"Retry-After": "20"},
                )
                return
            if not self._admit_guest_derived_action(user, ws):
                return
            slot = _GuestDerivedSlot(int(user["id"])) if user.get("guest") else None
            try:
                self._lean_verify_action(
                    rid, ws, user, action, model, lean_arg, feedback, body, slot,
                    analysis_lean=analysis_lean,
                    analysis_run=analyze_runs.is_analyze_record(run_record),
                )
            finally:
                # Released here unless the formal refresh took ownership.
                if slot is not None and not slot.handed_off:
                    slot.release()
            return

        if path == "/api/route-request":
            # Auto mode (easy_route): is the new request mathematics, and which
            # mode fits it. Signed-in accounts only, with the judge on.
            from agent_monitor import easy_route

            if not easy_route.enabled() or (user.get("guest") and not _guest_rating_allowed(user)):
                # Off, or a guest past its ratings this hour: Auto takes its default.
                self._send(200, json.dumps({"enabled": False}))
                return
            rated = easy_route.rate_request(
                str(body.get("problem_text") or "")[:20_000], has_source=bool(body.get("has_source"))
            )
            self._send(200, json.dumps({
                "enabled": True,
                "math": bool(rated.get("math")),
                "mode": rated.get("mode") or "",
                "label": easy_route.MODE_LABELS.get(rated.get("mode") or "", ""),
                "message": rated.get("reason") if not rated.get("math") else "",
                "model": rated.get("model"),
            }, ensure_ascii=False))
            return
        if path == "/api/runs":
            from agent_monitor import analyze_runs

            try:
                run_kind = analyze_runs.run_kind_of(body)
            except analyze_runs.AnalyzeRequestError as exc:
                self._send(exc.status, json.dumps({"error": str(exc)}))
                return
            try:
                research_projects.prepare_run(body, user)
            except (PermissionError, ValueError) as exc:
                self._send(403, json.dumps({"error": str(exc)}))
                return
            if run_kind == "analyze":
                self._start_analyze_run(body, user)
                return
            if run_kind == "readability":
                self._start_readability_run(body, user)
                return
            engine = str(body.get("engine") or ("kimi" if user.get("guest") else "")).lower()
            if engine == "analyze":
                self._send(
                    400,
                    json.dumps({
                        "error": "Ansatz Analyze runs in the Analyze mode; reload the console and choose Analyze",
                    }),
                )
                return
            if engine == "readability":
                self._send(
                    400,
                    json.dumps({
                        "error": "Ansatz Readability runs in the Readability mode; reload the console and choose "
                                 "Readability",
                    }),
                )
                return
            # Mathematics only (easy_route): a request the judge is sure is not
            # mathematics starts nothing (before any run start is reserved).
            refusal = _not_math_refusal(user, body.get("problem_text"))
            if refusal:
                self._send(400, json.dumps({"error": refusal, "not_math": True}, ensure_ascii=False))
                return
            requested_model = body.get("model") or None
            requested_subagent_model = (
                str(body.get("subagent_model") or "").strip() or None
            )
            try:
                max_iterations = int(body.get("max_iterations") or 40)
            except (TypeError, ValueError):
                self._send(400, json.dumps({"error": "max_iterations must be an integer"}))
                return
            if user.get("guest"):
                from agent_monitor.settings import validate_guest_model

                if engine not in guest_engine_allowlist():
                    self._send(
                        403,
                        json.dumps(
                            {
                                "error": "That engine requires a signed-in account",
                                "allowed_engines": sorted(guest_engine_allowlist()),
                            }
                        ),
                    )
                    return
                problem_text = body.get("problem_text")
                if problem_text is not None and not isinstance(problem_text, str):
                    self._send(400, json.dumps({"error": "problem_text must be text"}))
                    return
                if len(problem_text or "") > 20_000:
                    self._send(400, json.dumps({"error": "Guest problems are limited to 20,000 characters"}))
                    return
                try:
                    # Multibyte text can fit the character limit yet exceed the
                    # CGM byte limit; refuse it before a run slot is reserved.
                    job_manager._check_cgm_problem_size(engine, problem_text or "")
                    # Likewise the (text-only, small) guest attachment allowance.
                    research_attachments.validate(body.get("attachments"), guest=True)
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                    return
                if requested_model is not None:
                    try:
                        requested_model = validate_guest_model(requested_model)
                    except ValueError as exc:
                        self._send(400, json.dumps({"error": str(exc)}))
                        return
                requested_subagent_model = None
                max_iterations = max(1, min(max_iterations, guest_max_iterations()))
            try:
                def start() -> dict:
                    # An Ansatz CGM proof the judge rates easy runs on Plain (easy_route).
                    return research_projects.start_run(body.get("project_id"), user["id"], body.get("project_task"), lambda tool_free: job_manager.start_job_routed(
                        engine=engine,
                        problem_id=body.get("problem_id"),
                        problem_text=body.get("problem_text"),
                        model=requested_model,
                        max_iterations=max_iterations,
                        max_output_tokens=(
                            guest_max_output_tokens() if user.get("guest") else None
                        ),
                        user=user,
                        use_subagents=(
                            body.get("use_subagents") is not False
                            and not user.get("guest")
                        ),
                        subagent_model=requested_subagent_model,
                        attachments=body.get("attachments"),
                        auth_route=(str(body.get("auth_route") or "").strip() or None),
                        reasoning_effort=(
                            str(body.get("reasoning_effort") or "").strip() or None
                        ),
                        speed_mode=(
                            str(body.get("speed_mode") or "").strip() or None
                        ),
                        # Only an explicit false turns automatic formalization off.
                        formalize=body.get("formalize") is not False,
                        tool_free=tool_free,
                    ))

                if user.get("guest"):
                    # Keep admission and start_job's synchronous job/run
                    # registration atomic with respect to other guest starts.
                    # The runner itself executes on its own worker thread.
                    client = self._guest_client_key()
                    with _GUEST_RUN_START_LOCK:
                        capacity_error, retry_after = _reserve_guest_run_slot(
                            int(user["id"]), client
                        )
                        if capacity_error:
                            self._send(
                                429,
                                json.dumps({"error": capacity_error, "guest_policy": guest_policy(user), "login_required": guest_policy(user)["login_required"]}),
                                headers={"Retry-After": str(retry_after)},
                            )
                            return
                        from agent_monitor.auth import reserve_guest_run, release_guest_run
                        if not reserve_guest_run(int(user["id"]), guest_runs_per_session(),
                                                 previous_runs=job_manager.persisted_run_count(int(user["id"]))):
                            _release_guest_run_slot(int(user["id"]), client)
                            self._send(403, json.dumps({"error": "Guest limit reached. Sign in to continue",
                                                       "login_required": True, "guest_policy": guest_policy(user)}))
                            return
                        try:
                            job = start()
                        except Exception:
                            # A refused start (no key, bad input) starts
                            # nothing, so it costs no hourly run either.
                            release_guest_run(int(user["id"]))
                            _release_guest_run_slot(int(user["id"]), client)
                            raise
                        if client is not None and job.get("run_id"):
                            _GUEST_RUN_CLIENTS[str(job["run_id"])] = client
                        job = {**job, "guest_policy": guest_policy(user)}
                else:
                    requested_model = account_policy.member_run_model(
                        user, engine, requested_model
                    )
                    member_error = account_policy.member_run_error(
                        user,
                        engine,
                        model=requested_model,
                        auth_route=body.get("auth_route"),
                    )
                    if member_error:
                        self._send(
                            403,
                            json.dumps(
                                {
                                    "error": member_error,
                                    "allowed_engines": sorted(account_policy.member_engine_allowlist()),
                                }
                            ),
                        )
                        return
                    if not self._admit_member_run(user):
                        return
                    try:
                        # Papers linked in the problem are read for the model.
                        linked = self._fetch_linked_papers(body, body.get("problem_text"))
                        job = start()
                    except Exception:
                        # A refused start (no key, bad input) starts nothing.
                        self._release_member_run(user)
                        raise
                    self._report_linked_papers(job.get("run_id"), linked)
            except account_policy.ProjectPolicyError as exc:
                # The project's tool-free rule.
                self._send(403, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            _note_auto_mode(job, body)
            self._send(200, json.dumps(job))
            return

        if path.startswith("/api/runs/") and path.endswith("/stop"):
            run_id = path.removeprefix("/api/runs/").removesuffix("/stop")
            if not run_id or "/" in run_id or not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "not found"}))
                return
            if self._deny_run_change(run_id, user, "stop it"):
                return
            try:
                result = job_manager.stop_run(run_id)
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}))
                return
            # A member's queued feedback the Stop dropped started nothing:
            # the run start it booked is given back (that one, if still booked).
            for dropped in result.pop("dropped_bookings", None) or []:
                _release_member_run_slot(int(dropped["author_id"]), dropped["booking"])
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        if path.startswith("/api/runs/") and path.endswith("/continue"):
            if self._deny_guest(user, "run continuation"):
                return
            run_id = path.removeprefix("/api/runs/").removesuffix("/continue")
            if not run_id or "/" in run_id or not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "not found"}))
                return
            if self._deny_run_change(run_id, user, "continue it"):
                return
            if self._refuse_analyze_continuation(run_id):
                return
            # A repeated Continue on a running run starts nothing, so it is
            # answered before a member's run start is reserved.  So is one
            # that comes while a stopped session still winds down: it would
            # run beside that session's worker.
            if job_manager.run_is_stopping(run_id):
                self._send(409, json.dumps({"ok": False, "status": "stopping", "run_id": run_id,
                                            "error": job_manager.RUN_STOPPING_ERROR}))
                return
            if job_manager.run_is_active(run_id):
                self._send(200, json.dumps({"ok": False, "status": "already_running", "run_id": run_id}))
                return
            if not self._admit_member_run(user):
                return
            try:
                result = job_manager.continue_run(
                    run_id,
                    message=body.get("message"),
                    model=body.get("model"),
                    max_iterations=int(body.get("max_iterations") or 40),
                    user=user,
                )
            except FileNotFoundError as exc:
                self._release_member_run(user)
                self._send(404, json.dumps({"error": str(exc)}))
                return
            except account_policy.ProjectPolicyError as exc:
                # The project's tool-free rule.
                self._release_member_run(user)
                self._send(403, json.dumps({"error": str(exc)}))
                return
            except ValueError as exc:
                # A refused continuation (retired engine, member policy, a
                # disconnected account) is the caller's error, as for /chat.
                self._release_member_run(user)
                self._send(400, json.dumps({"error": str(exc)}))
                return
            except Exception as exc:  # noqa: BLE001
                self._release_member_run(user)
                self._send(500, json.dumps({"error": str(exc)}))
                return
            if not result.get("ok"):
                # Another request started this run's continuation first (or
                # a Stop came first, and its session still winds down).
                self._release_member_run(user)
            self._send(409 if result.get("status") == "stopping" else 200, json.dumps(result, ensure_ascii=False))
            return

        if path.startswith("/api/runs/") and path.endswith("/chat"):
            if self._deny_guest(user, "run continuation"):
                return
            run_id = path.removeprefix("/api/runs/").removesuffix("/chat")
            if not run_id or "/" in run_id or not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "not found"}))
                return
            if self._deny_run_change(run_id, user, "send it feedback"):
                return
            if self._refuse_analyze_continuation(run_id):
                return
            # Mathematics only (easy_route): a message the judge is sure is not
            # mathematics (with the run's problem as context) is not sent.
            refusal = _not_math_refusal(user, body.get("message"), run_id=run_id)
            if refusal:
                self._send(400, json.dumps({"error": refusal, "not_math": True}, ensure_ascii=False))
                return
            if not self._admit_member_message(user):
                return
            # Every message may start or book a continuation, so a member's
            # run start is reserved first; send_human_message decides under
            # the job lock whether it only joined feedback its author already
            # queued for a running run, and only then is the start returned.
            if not self._admit_member_run(user):
                return
            # Papers linked in the message are read for the model (once per run).
            linked = self._fetch_linked_papers(body, body.get("message"), run_id)
            try:
                result = job_manager.send_human_message(
                    run_id,
                    str(body.get("message") or ""),
                    model=body.get("model"),
                    max_iterations=int(body.get("max_iterations") or 40),
                    user=user,
                    attachments=body.get("attachments"),
                    # Queued, the message keeps its start: a Stop that drops
                    # it gives back exactly that one.
                    booking=self._member_booking,
                )
            except account_policy.ProjectPolicyError as exc:
                # The project's tool-free rule.
                self._release_member_run(user)
                self._send(403, json.dumps({"error": str(exc)}))
                return
            except (ValueError, FileNotFoundError) as exc:
                self._release_member_run(user)
                self._send(400, json.dumps({"error": str(exc)}))
                return
            except Exception as exc:  # noqa: BLE001
                self._release_member_run(user)
                self._send(500, json.dumps({"error": str(exc)}))
                return
            if result.get("joined_follow_up"):
                # It rides on a follow-up this account already paid for.
                self._release_member_run(user)
            self._report_linked_papers(run_id, linked)
            if linked is not None and linked.notes and isinstance(result.get("messages"), list):
                result["messages"] = job_manager.list_chat(run_id)
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        if path == "/api/settings/verify":
            from agent_monitor.settings import verify_provider

            guest_verification_reserved = False
            try:
                if user.get("guest"):
                    capacity_error, retry_after = _reserve_guest_verification(
                        int(user["id"])
                    )
                    if capacity_error:
                        self._send(
                            429,
                            json.dumps({"error": capacity_error}),
                            headers={"Retry-After": str(retry_after)},
                        )
                        return
                    guest_verification_reserved = True
                result = verify_provider(
                    str(body.get("provider") or ""),
                    api_key=body.get("api_key"),
                    base_url=body.get("base_url"),
                    options=body.get("options") if isinstance(body.get("options"), dict) else None,
                    user=user,
                )
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}))
                return
            finally:
                if guest_verification_reserved:
                    _release_guest_verification(int(user["id"]))
            code = 200 if result.get("ok") else 400
            self._send(code, json.dumps(result, ensure_ascii=False))
            return

        if path == "/api/settings":
            from agent_monitor.settings import save_settings

            try:
                result = save_settings(body, user=user)
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": str(exc)}))
                return
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        if path in (
            "/api/settings/codex/login/start",
            "/api/settings/codex/login/cancel",
            "/api/settings/codex/logout",
        ):
            if self._deny_guest(user, "account connections"):
                return
            if path.endswith("/start") and self._deny_member(
                user,
                "Connecting a Codex subscription",
                explanation=account_policy.subscription_notes()["codex"],
            ):
                return
            if account_policy.is_restricted(user):
                # A member links no account, so there is no login to cancel;
                # logout only removes a sign-in stored before the policy.
                if path.endswith("/logout"):
                    _remove_member_stored_login("codex", int(user["id"]))
                self._send(200, json.dumps(_member_subscription_status("codex"), ensure_ascii=False))
                return
            # Every operation is scoped to user["id"] from the authenticated
            # session — never from the request body — so a user can only
            # ever start, poll, cancel, or disconnect their *own* Codex
            # account login, same isolation as per-user API keys.
            from agent_monitor import codex_login

            if path.endswith("/start"):
                result = codex_login.start_login(user["id"])
            elif path.endswith("/cancel"):
                result = codex_login.cancel_login(user["id"])
            else:
                result = codex_login.logout(user["id"])
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        if path in (
            "/api/settings/claude/login/start",
            "/api/settings/claude/login/code",
            "/api/settings/claude/login/cancel",
            "/api/settings/claude/logout",
        ):
            if self._deny_guest(user, "account connections"):
                return
            if path.endswith(("/start", "/code")) and self._deny_member(
                user,
                "Connecting a Claude subscription",
                explanation=account_policy.subscription_notes()["claude"],
            ):
                return
            if account_policy.is_restricted(user):
                # As for the member's status, claude_login is never reached:
                # its CLI probes create an account home.  Logout only removes
                # a sign-in stored before the policy.
                if path.endswith("/logout"):
                    _remove_member_stored_login("claude", int(user["id"]))
                self._send(200, json.dumps(_member_subscription_status("claude"), ensure_ascii=False))
                return
            from agent_monitor import claude_login

            try:
                if path.endswith("/code"):
                    result = claude_login.submit_code(user["id"], body.get("code"))
                elif path.endswith("/start"):
                    result = claude_login.start_login(user["id"])
                elif path.endswith("/cancel"):
                    result = claude_login.cancel_login(user["id"])
                else:
                    result = claude_login.logout(user["id"])
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        if path == "/api/library":
            if not self._require_operator(user):
                return
            from agent_monitor import library

            try:
                item = library.upsert_item(body)
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}))
                return
            self._send(200, json.dumps(item, ensure_ascii=False))
            return

        if path == "/api/library/settings":
            if not self._require_operator(user):
                return
            from agent_monitor import library

            self._send(200, json.dumps(library.update_settings(body), ensure_ascii=False))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def _lean_verify_action(
        self,
        rid: str,
        ws: Path,
        user: dict,
        action: str,
        model: str | None,
        lean_arg: str | None,
        feedback: str,
        body: dict,
        slot: "_GuestDerivedSlot | None",
        *,
        analysis_lean: dict | None = None,
        analysis_run: bool = False,
    ) -> None:
        """Run one admitted Lean action and answer the request.

        A guest's derived-action ``slot`` moves to the formal DAG refresh the
        action schedules, which releases it when its model work is done.
        ``analysis_lean`` (an Analyze run's Verify) formalizes the statements
        of the selected nodes only.
        """
        from agent_monitor import auto_pipeline, lean_verify

        # A run deleted meanwhile stops this action's Lean work at its next
        # model call, probe, compile or write (nothing recreates its files).
        stop_token = lean_verify.stop_when(lambda: auto_pipeline._run_gone(ws))
        try:
            if action in {"compile", "check"}:
                # An analysis's Lean states results; checking it again never
                # turns it into a proof attempt.
                result = lean_verify.compile_or_check(
                    workspace=ws, lean=lean_arg, mode=action,
                    statements_only=True if analysis_run else None,
                )
            elif action == "harness":
                result = lean_verify.start_harness(
                    workspace=ws,
                    run_record=_derived_record_for(rid),
                    user=user,
                    engine=str(body.get("engine") or "codex").strip().lower(),
                    model=model,
                    lean=lean_arg,
                    run_id=rid,
                )
            elif action == "stop_harness":
                result = lean_verify.stop_harness(ws)
            elif action == "audit":
                result = lean_verify.audit_current(
                    workspace=ws,
                    run_record=_derived_record_for(rid),
                    user=user,
                    model=model,
                    lean=lean_arg,
                )
            elif action == "revise":
                repairs = body.get("max_repairs")
                result = lean_verify.revise_with_feedback(
                    workspace=ws,
                    user=user,
                    run_record=_derived_record_for(rid),
                    message=feedback,
                    model=model,
                    lean=lean_arg,
                    max_repairs=_bounded_repairs(
                        repairs,
                        default=1,
                        cap=_REVISE_MAX_REPAIRS,
                        guest=bool(user.get("guest")),
                        guest_cap=_GUEST_REVISE_MAX_REPAIRS,
                    ),
                )
            else:
                repairs = body.get("max_repairs")
                overrides = (
                    {
                        "guidance": analysis_lean["guidance"],
                        "problem_override": analysis_lean["problem"],
                        "proof_override": analysis_lean["proof"],
                        "prove": False,
                        "targets": analysis_lean["targets"],
                        "source_label": "analysis/analysis_graph.json",
                    }
                    if analysis_lean
                    else {}
                )
                result = lean_verify.generate(
                    workspace=ws,
                    run_record=_derived_record_for(rid),
                    user=user,
                    model=model,
                    max_repairs=_bounded_repairs(
                        repairs,
                        default=lean_verify.AUTO_MAX_REPAIRS,
                        cap=_MANUAL_MAX_REPAIRS,
                        guest=bool(user.get("guest")),
                    ),
                    **overrides,
                )
        except lean_verify.SessionStopped:
            self._send(410, json.dumps({"error": "This run was deleted; its Lean work stopped."}, ensure_ascii=False))
            return
        except ValueError as exc:
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return
        except Exception as exc:  # noqa: BLE001
            self._send(500, json.dumps({"error": f"Lean verification failed: {exc}"}, ensure_ascii=False))
            return
        finally:
            lean_verify.stop_reset(stop_token)

        # Verify is how an informal-only run is formalized later; its formal
        # stages then leave "off" exactly as after a manual check.
        refreshes = action in {"compile", "check", "audit", "revise"} or (
            action == "verify" and auto_pipeline.formal_stages_off(ws)
        )
        if refreshes and str(result.get("status") or "") != "running":
            run_record = _derived_record_for(rid) or {}
            route = job_manager._route_from_record(run_record)
            # The refresh may ask a model on the run's recorded route, so a
            # member's run off the API route (billed to a subscription or Max,
            # reached here by Compile or Check), and a tool-free project run
            # off it, keep their Formal stages.
            if route == "api_key" or (
                not account_policy.is_restricted(user)
                and not job_manager.tool_free_run(rid, run_record)
            ):
                auto_pipeline.refresh_formal(
                    run_id=rid,
                    workspace=ws,
                    owner_id=user.get("id"),
                    model=model or result.get("model"),
                    engine=str(run_record.get("engine") or "unknown"),
                    on_exit=slot.release if slot is not None else None,
                )
                if slot is not None:
                    slot.handed_off = True
        self._send(200, json.dumps(result, ensure_ascii=False))

    @_track_request
    def do_DELETE(self):  # noqa: N802
        path = self._resolve_path()
        if path is None:
            return
        if self._reject_cross_origin_write():
            return

        if self._research_route(path, "DELETE", {}) or self._sharing_write(path, "DELETE", {}) or self._projects_route(path, "DELETE", {}):
            return
        user = self._require_user(path)
        if user is None:
            return

        if path.startswith("/api/agent/skills/"):
            if not self._require_operator(user):
                return
            from urllib.parse import unquote

            from agent_monitor import agent_config

            name = unquote(path.removeprefix("/api/agent/skills/"))
            try:
                agent_config.delete_skill(name)
                self._send(200, json.dumps({"ok": True}))
            except (ValueError, FileNotFoundError) as exc:
                self._send(404, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return

        if path.startswith("/api/agent/memory/"):
            if not self._require_operator(user):
                return
            from urllib.parse import unquote

            from agent_monitor import agent_config

            name = unquote(path.removeprefix("/api/agent/memory/"))
            try:
                agent_config.save_memory(name, "")
                self._send(200, json.dumps({"ok": True}))
            except ValueError as exc:
                self._send(404, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return

        if path.startswith("/api/library/"):
            if not self._require_operator(user):
                return
            from agent_monitor import library

            item_id = path.removeprefix("/api/library/")
            if not item_id or "/" in item_id:
                self._send(404, json.dumps({"error": "not found"}))
                return
            ok = library.delete_item(item_id)
            self._send(200 if ok else 404, json.dumps({"deleted": ok, "id": item_id}))
            return

        if path.startswith("/api/runs/"):
            run_id = path.removeprefix("/api/runs/")
            if not run_id or "/" in run_id or not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "not found"}))
                return
            if self._deny_run_change(run_id, user, "delete it"):
                return
            try:
                result = job_manager.delete_run(run_id)
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}))
                return
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def _proxy_dashboard_get(self, path: str, qs: dict, user: dict):
        """Reuse harness_dashboard.server handlers for run JSON APIs."""
        import harness_dashboard.server as dash

        dash.CACHE_DIR = _cache_harness()
        dash.MANIFEST_PATH = dash.CACHE_DIR / "manifest.json"

        if path == "/api/runs":
            job_manager.reconcile_stale_running_runs()
            payload: dict = {"runs": []}
            if dash.MANIFEST_PATH.exists():
                try:
                    payload = json.loads(dash.MANIFEST_PATH.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    payload = {"runs": []}
            # Operators keep ownerless legacy runs in their own list; other
            # accounts' runs live in the admin view (or scope=all).
            scope_all = (qs.get("scope") or [""])[0] == "all" and _is_operator_user(user)
            if not scope_all:
                payload["runs"] = [
                    r for r in (payload.get("runs") or [])
                    if r.get("owner_id") == user["id"]
                    or (r.get("owner_id") is None and _is_operator_user(user))
                ]
            for entry in payload.get("runs") or []:
                if not entry.get("problem_preview"):
                    entry["problem_preview"] = _problem_preview_for(entry.get("run_id"), dash.CACHE_DIR)
            self._send(200, json.dumps(payload, ensure_ascii=False))
            return

        if path == "/api/rebuild":
            self._send(200, json.dumps({"ok": True, "note": "use agent-monitor build"}))
            return

        route_run_id: str | None = None
        route_action: str | None = None
        if path.startswith("/api/run/"):
            tail = path.removeprefix("/api/run/")
            route_run_id, separator, action = tail.partition("/")
            route_action = action if separator else None
            # Ownership and loading must operate on exactly the same ID.
            # Reject extra path components instead of checking only the first
            # component and later handing a traversal-shaped ID to a loader.
            if (
                not route_run_id
                or (route_action is not None and (not route_action or "/" in route_action))
                or not self._owns_run(route_run_id, user)
            ):
                self._send(404, json.dumps({"error": "run not found"}))
                return

        if route_action == "export":
            run_id = route_run_id
            assert run_id is not None
            data = dash._load_run(run_id)
            if not data:
                self._send(404, json.dumps({"error": "run not found"}))
                return
            from datetime import datetime, timezone

            bundle: dict = {"run": data, "exported_at": datetime.now(timezone.utc).isoformat()}
            try:
                bundle["chat"] = job_manager.list_chat(run_id)
            except Exception:  # noqa: BLE001
                bundle["chat"] = []
            ws = _workspace_for_run(run_id)
            files: list[dict] = []
            if ws:
                for f in _list_workspace_files(ws):
                    entry = dict(f)
                    if f["kind"] == "text" and f["size"] <= 400_000:
                        try:
                            target = _workspace_target(ws, f["path"])
                            if target is not None and target.is_file():
                                entry["content"] = target.read_text(
                                    encoding="utf-8", errors="replace"
                                )
                        except OSError:
                            pass
                    files.append(entry)
            bundle["workspace_files"] = files
            body = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
            self._send_bytes(200, body, "application/json", download_name=f"{run_id}_logs.json")
            return

        if route_action == "final_latex":
            run_id = route_run_id
            assert run_id is not None
            if self._deny_run_compilation(user, run_id):
                return
            data = dash._load_run(run_id)
            if not data:
                self._send(404, json.dumps({"error": "run not found"}))
                return
            self._send(200, json.dumps(dash._final_latex_payload(run_id, data), ensure_ascii=False))
            return

        if route_action == "final.tex":
            run_id = route_run_id
            assert run_id is not None
            data = dash._load_run(run_id)
            tex = dash._read_final_tex(data) if data else None
            if tex:
                self._send(200, tex, "text/plain; charset=utf-8")
            else:
                self._send(404, json.dumps({"error": "final proof not found"}))
            return

        if route_action == "final.pdf":
            run_id = route_run_id
            assert run_id is not None
            if self._deny_run_compilation(user, run_id):
                return
            from harness_dashboard.latex_provenance import (
                compile_pdf,
                pdf_cache_path,
                resolve_latex_path,
            )
            data = dash._load_run(run_id)
            if not data:
                self._send(404, json.dumps({"error": "run not found"}))
                return
            tex_path, _ = resolve_latex_path(data)
            if not tex_path:
                self._send(404, json.dumps({"error": "no latex source"}))
                return
            cache_pdf = pdf_cache_path(dash.CACHE_DIR, run_id)
            if not compile_pdf(tex_path, cache_pdf):
                self._send(404, json.dumps({"error": "pdf compile failed or not latex"}))
                return
            self._send_bytes(200, cache_pdf.read_bytes(), "application/pdf")
            return

        if route_run_id is not None:
            run_id = route_run_id
            if route_action is not None:
                self._send(404, json.dumps({"error": "not found"}))
                return
            job_manager.reconcile_run_status(run_id)
            data = dash._load_run(run_id) or _run_record_for(run_id)
            if data:
                from agent_monitor.schema import normalize_run

                data = normalize_run(data, engine=data.get("engine") or "hermes")
                data["problems"] = job_manager.ensure_problems(data, run_id)
                if job_manager.run_is_stopping(run_id):
                    # Recorded stopped while its worker still winds down: the
                    # console offers Continue once this is gone.
                    data["stopping"] = True
                ws = _workspace_for_run(run_id)
                _refresh_analyze_block(data, ws)
                _refresh_readability_block(data, ws)
                data["sandbox_preview"] = _sandbox_preview(run_id, data)
                # Each turn's proof, for one proof card per turn (proof_turns).
                from agent_monitor import proof_turns

                data["proof_turns"] = proof_turns.summary(ws)
                if ws:
                    from agent_monitor import auto_pipeline, lean_verify, proof_bridge

                    data["lean_verification"] = lean_verify.monitor_summary(ws)
                    data["auto_pipeline"] = _bootstrap_guest_pipeline(user, run_id, ws, data)
                    data["proof_coverage"] = proof_bridge.load_cached(ws)
                else:
                    data["auto_pipeline"] = None
                    data["proof_coverage"] = None
                    data["lean_verification"] = {
                        "status": "none",
                        "has_result": False,
                        "attempt_count": 0,
                        "attempts": [],
                        "chat": [],
                        "harness_running": False,
                    }
                self._send(200, json.dumps(data, ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "run not found"}))
            return

        if path == "/api/call_detail":
            tid = qs.get("trace_id", [""])[0]
            run_id = tid.split("::", 1)[0]
            if not tid or not run_id or not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "call not found"}))
                return
            rnd = int(qs.get("round", ["1"])[0])
            detail = dash._call_detail(tid, rnd)
            if detail:
                self._send(200, json.dumps(detail, ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "call not found"}))
            return

        self._send(404, json.dumps({"error": "not found"}))


def _data_key_required() -> bool:
    return os.environ.get("AGENT_MONITOR_REQUIRE_DATA_KEY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _open_account_database() -> None:
    """Open (and migrate/validate) the account database before serving.

    With ``AGENT_MONITOR_REQUIRE_DATA_KEY=1`` a missing, unreadable or invalid
    data key, or stored settings it cannot authenticate, stop the console at
    startup instead of on the first request (so a health check never reports a
    console that cannot read any account's settings).
    """
    from agent_monitor import auth

    required = _data_key_required()
    try:
        if required and auth._dedicated_fernet() is None:
            raise RuntimeError(
                "AGENT_MONITOR_REQUIRE_DATA_KEY=1 but no user-environment data key is configured"
            )
        auth._conn().close()
    except Exception as exc:  # noqa: BLE001
        if required:
            raise SystemExit(
                f"[startup] refusing to start: account database/data key check failed: {exc}"
            ) from None
        logger.warning("Account database check failed at startup (%s)", type(exc).__name__)


_GUEST_RETENTION_INTERVAL_SECONDS = 6 * 3600.0
_GUEST_RETENTION_FIRST_DELAY_SECONDS = 600.0


def guest_run_retention_days() -> int:
    """Days finished guest runs are kept; 0 (the default) keeps them all.

    The public deployment sets AGENT_MONITOR_GUEST_RUN_RETENTION_DAYS in its
    unit, so anonymous run records and workspaces do not accumulate forever.
    """
    return _bounded_env_int(
        "AGENT_MONITOR_GUEST_RUN_RETENTION_DAYS", default=0, minimum=0, maximum=3650
    )


def _purge_expired_guest_runs() -> list[str]:
    days = guest_run_retention_days()
    if days < 1:
        return []
    # In this process, so the console's own job index drops the runs too.
    deleted = job_manager.purge_guest_runs(older_than_days=days, dry_run=False)
    if deleted:
        logger.info("Guest retention: deleted %d finished guest run(s) older than %d days", len(deleted), days)
    return deleted


def _guest_retention_loop(stop: threading.Event) -> None:
    delay = _GUEST_RETENTION_FIRST_DELAY_SECONDS
    while not stop.wait(delay):
        delay = _GUEST_RETENTION_INTERVAL_SECONDS
        try:
            _purge_expired_guest_runs()
        except Exception as exc:  # noqa: BLE001 - retry on the next interval
            logger.warning("Guest retention purge failed (%s)", type(exc).__name__)


def start_guest_retention_thread() -> threading.Event | None:
    """Purge finished guest runs past the retention period every few hours."""
    if guest_run_retention_days() < 1:
        return None
    stop = threading.Event()
    threading.Thread(
        target=_guest_retention_loop, args=(stop,), name="guest-retention", daemon=True
    ).start()
    return stop


class ConsoleHTTPServer(ThreadingHTTPServer):
    """The console's HTTP server.

    ``socketserver``'s default listen backlog is 5: a burst of polling tabs
    behind Caddy overflowed it and surfaced as 502s at the edge.
    """

    request_queue_size = 128
    daemon_threads = True


def _line_buffered_output() -> None:
    """Write log lines through to journald as they happen.

    Under systemd, stdout is a socket and Python block-buffers it (8 KiB), so
    access lines arrived minutes late, in bursts, out of order with stderr,
    and were lost on every stop.  Environment=PYTHONUNBUFFERED=1 in the unit
    does the same; this keeps an older unit correct too.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def main(port: int | None = None):
    _line_buffered_output()
    port = port or PORT
    ensure_data_dirs()
    # Link previews (POST /api/analyze/resolve) share the server-wide arXiv
    # politeness slot ($AGENT_MONITOR_DATA_DIR/cache) with every Analyze runner.
    from agent_monitor.analyze import sources as analyze_sources

    analyze_sources.ARXIV_SLOT_PATH = "auto"
    _open_account_database()
    recovered = job_manager.reconcile_stale_running_runs(min_age_seconds=0)
    if recovered:
        print(f"[recovery] marked {len(recovered)} orphaned run(s) interrupted: {', '.join(recovered)}")
    from agent_monitor import lean_verify

    closed_harnesses = lean_verify.reconcile_orphaned_harnesses(WORKSPACES_ROOT)
    if closed_harnesses:
        print(
            f"[recovery] closed {len(closed_harnesses)} interrupted Formal Lean "
            f"harness record(s): {', '.join(closed_harnesses)}"
        )
    removed_logins = job_manager.remove_legacy_codex_key_homes()
    if removed_logins:
        print(f"[cleanup] removed {removed_logins} saved Codex API-key login(s) from the data directory")
    if start_guest_retention_thread() is not None:
        print(f"  guest run retention       -> {guest_run_retention_days()} days")
    root = f"http://localhost:{port}{BASE_PATH or ''}"
    print(f"Unified Math Proving Console -> {root}/")
    print(f"  bind                      -> {HOST}:{port}")
    if PUBLIC_MODE:
        print("  access                    -> public (no login; guest sessions)")
    if BASE_PATH:
        print(f"  public base path         -> {BASE_PATH}")
    print(f"  classic monitor           -> {root}/monitor")
    print(f"  cache: {_cache_harness()}")
    from agent_monitor import usage_ledger

    usage_ledger.start_backfill_thread()
    ConsoleHTTPServer((HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
