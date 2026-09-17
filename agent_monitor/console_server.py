"""Unified Math Proving Console HTTP server."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import stat
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse, urlsplit, urlunsplit

from agent_monitor import CACHE_DIR, PROBLEMS_DIR, ROOT, RUNS_DIR
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
HOST = os.environ.get("AGENT_MONITOR_HOST", "0.0.0.0").strip() or "0.0.0.0"
logger = logging.getLogger("agent_monitor.console_server")

# Password-reset delivery must not make HTTP response timing depend on whether
# an account exists. Keep the work bounded, serialize SMTP, and rate-limit by a
# hash of the normalized address so the limiter does not retain email strings.
_PASSWORD_RESET_COOLDOWN_SECONDS = 300.0
_PASSWORD_RESET_WINDOW_SECONDS = 60.0
_PASSWORD_RESET_WINDOW_LIMIT = 30
_PASSWORD_RESET_RECENT_LIMIT = 4096
_PASSWORD_RESET_QUEUE: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=64)
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
GUEST_MAX_LEAN_CHARACTERS = 100_000
GUEST_MAX_FEEDBACK_CHARACTERS = 8_000


def _password_reset_worker() -> None:
    while True:
        email, base_url = _PASSWORD_RESET_QUEUE.get()
        try:
            from agent_monitor.auth import handle_forgot_password

            handle_forgot_password(email, base_url)
        except Exception as exc:  # noqa: BLE001
            # Never include an address, reset token, or link in this log.
            logger.error("Queued password reset request failed (%s)", type(exc).__name__)
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
    normalized = (email or "").strip().lower()
    if not normalized or len(normalized) > 320:
        return False

    key = hashlib.sha256(normalized.encode("utf-8")).digest()
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
            _PASSWORD_RESET_QUEUE.put_nowait((normalized, base_url))
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
GUEST_ELIGIBLE_ENGINES = frozenset({"kimi", "plain"})
GUEST_DEFAULT_ENGINES = GUEST_ELIGIBLE_ENGINES


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
    allowed = requested & GUEST_ELIGIBLE_ENGINES
    # A command override changes Plain from the reviewed single-call runner
    # into arbitrary operator-selected local code. Never expose that variant
    # through an anonymous session.
    if os.environ.get("PLAIN_CMD", "").strip():
        allowed.discard("plain")
    if os.environ.get("KIMI_PROOF_CMD", "").strip():
        allowed.discard("kimi")
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
    }


def _guest_run_capacity_error(user_id: int) -> str | None:
    from agent_monitor.auth import guest_runs_started
    if max(guest_runs_started(user_id), job_manager.persisted_run_count(user_id)) >= guest_runs_per_session():
        return f"You have used all {guest_runs_per_session()} guest runs. Sign in to continue"
    active_states = {"queued", "starting", "running", "continuing"}
    active = [
        job
        for job in job_manager.list_jobs()
        if str(job.get("status") or "").lower() in active_states
    ]
    per_guest_limit = _bounded_env_int(
        "AGENT_MONITOR_GUEST_CONCURRENCY", default=1, minimum=1, maximum=4
    )
    if sum(job.get("owner_id") == user_id for job in active) >= per_guest_limit:
        return "Wait for your current guest run to finish before starting another"
    global_limit = _bounded_env_int(
        "AGENT_MONITOR_GUEST_GLOBAL_CONCURRENCY", default=2, minimum=1, maximum=16
    )
    if sum(bool(job.get("guest")) for job in active) >= global_limit:
        return "Guest run capacity is currently full; please try again later"
    return None


def _reserve_guest_run_slot(user_id: int) -> tuple[str | None, int]:
    """Atomically enforce active, per-session, and rolling guest run limits."""
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

        capacity_error = _guest_run_capacity_error(user_id)
        if capacity_error:
            return capacity_error, 30
        user_window = _GUEST_RUN_WINDOWS.setdefault(user_id, deque())
        if len(user_window) >= guest_runs_per_hour():
            retry = max(1, int(_GUEST_RUN_WINDOW_SECONDS - (now - user_window[0])))
            return "This guest session has reached its hourly run limit", retry
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
    return None, 0


def _reserve_guest_session_slot() -> tuple[bool, int]:
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
        if len(_GUEST_SESSION_WINDOW) >= limit:
            retry = max(1, int(_GUEST_SESSION_WINDOW_SECONDS - (now - _GUEST_SESSION_WINDOW[0])))
            return False, retry
        _GUEST_SESSION_WINDOW.append(now)
    return True, 0


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


def _reserve_guest_derived_action(user_id: int) -> tuple[str | None, int]:
    """Bound synchronous Formal/DAG work independently from key checks."""
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
        _GUEST_DERIVED_WINDOWS.setdefault(user_id, deque()).append(now)
        _GUEST_DERIVED_GLOBAL_WINDOW.append(now)
        _GUEST_DERIVED_ACTIVE_USERS.add(user_id)
    return None, 0


def _release_guest_derived_action(user_id: int) -> None:
    with _GUEST_DERIVED_LOCK:
        _GUEST_DERIVED_ACTIVE_USERS.discard(user_id)


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
    """Resolve a relative path only when its real target stays in ``root``."""
    if not relative or Path(relative).is_absolute():
        return None
    resolved_root = root.resolve()
    try:
        target = (resolved_root / relative).resolve()
    except (OSError, RuntimeError):
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


def _sandbox_preview(run_id: str, data: dict | None) -> dict:
    """Proof.md if it exists, otherwise the latest non-tool agent writes.

    The sandbox tab used to wait for a separate /files round-trip and stayed
    blank for the whole run. Folding the draft into /api/run makes preview
    follow the same payload the HITL panel already uses.
    """
    data = data or {}
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
    live_text = "\n\n---\n\n".join(writes[-12:]) if writes else longest
    text = proof_md or live_text
    source = "proof.md" if proof_md else ("live" if live_text else "")
    return {
        "text": text,
        "source": source,
        "has_proof_md": bool(proof_md),
        "has_proof_tex": bool(proof_tex),
        "revision": proof_revision,
        "mtime": proof_mtime,
    }


def _list_workspace_files(ws: Path) -> list[dict]:
    out = []
    for p in sorted(ws.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
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
        try:
            if tectonic:
                _subprocess.run(
                    [tectonic, "--outfmt", "pdf", "doc.tex"],
                    cwd=work,
                    capture_output=True,
                    timeout=90,
                    check=False,
                )
            else:
                for _ in range(2):
                    _subprocess.run(
                        [pdflatex, "-interaction=nonstopmode", "doc.tex"],
                        cwd=work,
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


# Paths reachable without a session.
_PUBLIC_PATHS = {
    "/login",
    "/reset-password",
    "/health",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/google",
    "/api/auth/guest",
    "/api/auth/me",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/verify-reset-token",
}


class Handler(RunSharingMixin, ProjectsMixin, ResearchMixin, BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        print(f"[console] {self.address_string()} {fmt % args}")

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
        # framing, MIME sniffing, base-tag rewriting, and plugin objects.
        out.setdefault("X-Content-Type-Options", "nosniff")
        out.setdefault("Referrer-Policy", "no-referrer")
        out.setdefault("X-Frame-Options", "SAMEORIGIN")
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

        available, retry_after = _reserve_guest_session_slot()
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
        error, retry_after = _reserve_guest_derived_action(int(user["id"]))
        if error:
            self._send(
                429,
                json.dumps({"error": error}),
                headers={"Retry-After": str(retry_after)},
            )
            return False
        return True

    def _deny_run_compilation(self, user: dict, run_id: str) -> bool:
        """Never execute TeX originating from an anonymous guest run."""
        if user.get("guest"):
            return self._deny_guest(user, "server-side proof compilation")
        record = _run_record_for(run_id) or {}
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

    def _send_html(self, html: str, code: int = 200):
        self._send(
            code,
            _inject_base(html, self._request_base_path()),
            "text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "CDN-Cache-Control": "no-store",
            },
        )

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

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, "")

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = self._resolve_path()
        if path is None:
            return
        qs = parse_qs(parsed.query)

        if self._research_route(path, "GET") or self._sharing_get(path) or self._projects_route(path, "GET"):
            return

        if path.rstrip("/") in ("/login", "/reset-password"):
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
            self._send_html(html_path.read_text(encoding="utf-8"))
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
            from agent_monitor.auth import google_client_id, registration_open

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
                    }
                ),
                headers={"Cache-Control": "no-store"},
            )
            return

        if path == "/health":
            self._send(200, json.dumps({"ok": True, "service": "agent-monitor-console"}))
            return

        if path.startswith("/static/"):
            rel = path.removeprefix("/static/")
            f = _static_asset(rel)
            if f is not None:
                ctype = "text/css" if f.suffix == ".css" else "application/javascript" if f.suffix == ".js" else "application/octet-stream"
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
            # Soft-redirect note: keep classic monitor available
            self._send_html(html)
            return

        if path == "/api/engines":
            from agent_monitor.engines_registry import list_engines

            engines = list_engines()
            if user.get("guest"):
                allowed = guest_engine_allowlist()
                engines = [{**engine, "requires_login": engine.get("id") not in allowed} for engine in engines]
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
            from agent_monitor import codex_login

            self._send(200, json.dumps(codex_login.status(user["id"]), ensure_ascii=False))
            return

        if path == "/api/settings/codex/login/poll":
            if self._deny_guest(user, "account connections"):
                return
            from agent_monitor import codex_login

            self._send(200, json.dumps(codex_login.poll_login(user["id"]), ensure_ascii=False))
            return

        if path == "/api/settings/claude/status":
            if self._deny_guest(user, "account connections"):
                return
            from agent_monitor import claude_login

            self._send(200, json.dumps(claude_login.status(user["id"]), ensure_ascii=False))
            return

        if path == "/api/settings/claude/login/poll":
            if self._deny_guest(user, "account connections"):
                return
            from agent_monitor import claude_login

            self._send(200, json.dumps(claude_login.poll_login(user["id"]), ensure_ascii=False))
            return

        if path == "/api/library":
            if self._deny_guest(user, "the shared library"):
                return
            from agent_monitor import library

            self._send(200, json.dumps(library.get_library(), ensure_ascii=False))
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
            if kind not in {"informal", "formal", "engine"}:
                kind = "informal"
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
                        cached = proof_graph.load_cached(ws, kind="informal")
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            self._send(200, json.dumps(cached or {"status": "none", "kind": kind}, ensure_ascii=False))
            return

        if path == "/api/lean/status":
            from agent_monitor import lean_verify

            if user.get("guest"):
                formal_options = _guest_formal_options()
            else:
                model_options = lean_verify.harness_model_options(user)
                engines = lean_verify.harness_engines(
                    user, model=model_options["selected"]
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
                        **lean_verify.toolchain_status(),
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
            run_record = _run_record_for(rid)
            cached = lean_verify.load_cached(ws) or {"status": "none"}
            try:
                if user.get("guest"):
                    cached.update(_guest_formal_options())
                else:
                    model_options = lean_verify.harness_model_options(user, run_record)
                    engines = lean_verify.harness_engines(user, run_record)
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
                cached["status"] = "failed"
                cached["notes"] = (
                    "Harness run did not finish — the server may have restarted. "
                    "Press Compile/Check on the source below, or run it again."
                )
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

    def do_POST(self):  # noqa: N802
        path = self._resolve_path()
        if path is None:
            return
        # Authenticate before accepting the larger attachment request bodies.
        accepts_attachments = path == "/api/runs" or bool(re.fullmatch(r"/api/runs/[^/]+/chat", path))
        upload_user = self._require_user(path) if accepts_attachments else None
        if accepts_attachments and upload_user is None:
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, json.dumps({"error": "invalid content length"}))
            return
        max_body = research_attachments.MAX_REQUEST_BYTES if accepts_attachments else 1_000_000
        if length < 0 or length > max_body:
            self.close_connection = True
            self._send(413, json.dumps({"error": "request body too large"}))
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
            except PermissionError as exc:
                self._send(403, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": f"Sign-in failed: {exc}"}, ensure_ascii=False))
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
            rel = str(body.get("path") or "proof.md").strip()
            if rel != "proof.md":
                self._send(400, json.dumps({"error": "Only proof.md is editable in Sandbox"}))
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
            if self._deny_guest(user, "the shared agent profile"):
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
            if self._deny_guest(user, "the shared agent profile"):
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
            if self._deny_guest(user, "the shared agent profile"):
                return
            from agent_monitor import agent_config

            try:
                agent_config.save_memory(str(body.get("name") or ""), str(body.get("content") or ""))
                self._send(200, json.dumps({"ok": True}))
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
            return

        if path.startswith("/api/run/") and path.endswith("/proof_graph"):
            rid = path.removeprefix("/api/run/").removesuffix("/proof_graph")
            ws = _workspace_for_run(rid)
            if not ws or not self._owns_run(rid, user):
                self._send(404, json.dumps({"error": "run not found"}))
                return
            run_record = _run_record_for(rid)
            kind = str(body.get("kind") or "informal").strip().lower()
            model = str(body.get("model") or "").strip() or None
            if user.get("guest"):
                from agent_monitor.settings import GUEST_DEFAULT_MODEL

                model = GUEST_DEFAULT_MODEL
                if kind not in {"informal", "formal", "engine"}:
                    self._send(400, json.dumps({"error": "Unknown proof graph kind"}))
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
            from agent_monitor import lean_verify

            rid = path.removeprefix("/api/run/").removesuffix("/lean_verify")
            ws = _workspace_for_run(rid)
            if not ws or not self._owns_run(rid, user):
                self._send(404, json.dumps({"error": "run not found"}))
                return
            action = str(body.get("action") or "verify").strip().lower()
            model = str(body.get("model") or "").strip() or None
            lean_src = body.get("lean")
            if isinstance(lean_src, str):
                lean_arg: str | None = lean_src
            else:
                lean_arg = None
            feedback = str(body.get("message") or body.get("feedback") or "")
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
            if not self._admit_guest_derived_action(user, ws):
                return
            try:
                if action in {"compile", "check"}:
                    result = lean_verify.compile_or_check(
                        workspace=ws, lean=lean_arg, mode=action
                    )
                elif action == "harness":
                    result = lean_verify.start_harness(
                        workspace=ws,
                        run_record=_run_record_for(rid),
                        user=user,
                        engine=str(body.get("engine") or "codex").strip().lower(),
                        model=model,
                        lean=lean_arg,
                    )
                elif action == "stop_harness":
                    result = lean_verify.stop_harness(ws)
                elif action == "audit":
                    result = lean_verify.audit_current(
                        workspace=ws,
                        run_record=_run_record_for(rid),
                        user=user,
                        model=model,
                        lean=lean_arg,
                    )
                elif action == "revise":
                    repairs = body.get("max_repairs")
                    result = lean_verify.revise_with_feedback(
                        workspace=ws,
                        user=user,
                        run_record=_run_record_for(rid),
                        message=feedback,
                        model=model,
                        lean=lean_arg,
                        max_repairs=max(0, min(int(repairs) if repairs is not None else 1, 1 if user.get("guest") else 3)),
                    )
                else:
                    repairs = body.get("max_repairs")
                    result = lean_verify.generate(
                        workspace=ws,
                        run_record=_run_record_for(rid),
                        user=user,
                        model=model,
                        max_repairs=max(0, min(int(repairs) if repairs is not None else 2, 1 if user.get("guest") else 4)),
                    )
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"Lean verification failed: {exc}"}, ensure_ascii=False))
                return
            finally:
                if user.get("guest"):
                    _release_guest_derived_action(int(user["id"]))
            if action in {"compile", "check", "audit", "revise"} and str(
                result.get("status") or ""
            ) != "running":
                from agent_monitor import auto_pipeline

                run_record = _run_record_for(rid) or {}
                auto_pipeline.refresh_formal(
                    run_id=rid,
                    workspace=ws,
                    owner_id=user.get("id"),
                    model=model or result.get("model"),
                    engine=str(run_record.get("engine") or "unknown"),
                )
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        if path == "/api/runs":
            try:
                research_projects.prepare_run(body, user)
            except (PermissionError, ValueError) as exc:
                self._send(403, json.dumps({"error": str(exc)}))
                return
            engine = str(body.get("engine") or ("kimi" if user.get("guest") else "")).lower()
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
                    return research_projects.start_run(body.get("project_id"), user["id"], body.get("project_task"), lambda: job_manager.start_job(
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
                    ))

                if user.get("guest"):
                    # Keep admission and start_job's synchronous job/run
                    # registration atomic with respect to other guest starts.
                    # The runner itself executes on its own worker thread.
                    with _GUEST_RUN_START_LOCK:
                        capacity_error, retry_after = _reserve_guest_run_slot(
                            int(user["id"])
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
                            self._send(403, json.dumps({"error": "Guest limit reached. Sign in to continue",
                                                       "login_required": True, "guest_policy": guest_policy(user)}))
                            return
                        try:
                            job = start()
                        except Exception:
                            release_guest_run(int(user["id"]))
                            raise
                        job = {**job, "guest_policy": guest_policy(user)}
                else:
                    job = start()
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            self._send(200, json.dumps(job))
            return

        if path.startswith("/api/runs/") and path.endswith("/stop"):
            run_id = path.removeprefix("/api/runs/").removesuffix("/stop")
            if not run_id or "/" in run_id or not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "not found"}))
                return
            try:
                result = job_manager.stop_run(run_id)
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}))
                return
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        if path.startswith("/api/runs/") and path.endswith("/continue"):
            if self._deny_guest(user, "run continuation"):
                return
            run_id = path.removeprefix("/api/runs/").removesuffix("/continue")
            if not run_id or "/" in run_id or not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "not found"}))
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
                self._send(404, json.dumps({"error": str(exc)}))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}))
                return
            self._send(200, json.dumps(result, ensure_ascii=False))
            return

        if path.startswith("/api/runs/") and path.endswith("/chat"):
            if self._deny_guest(user, "run continuation"):
                return
            run_id = path.removeprefix("/api/runs/").removesuffix("/chat")
            if not run_id or "/" in run_id or not self._owns_run(run_id, user):
                self._send(404, json.dumps({"error": "not found"}))
                return
            try:
                result = job_manager.send_human_message(
                    run_id,
                    str(body.get("message") or ""),
                    model=body.get("model"),
                    max_iterations=int(body.get("max_iterations") or 40),
                    user=user,
                    attachments=body.get("attachments"),
                )
            except (ValueError, FileNotFoundError) as exc:
                self._send(400, json.dumps({"error": str(exc)}))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}))
                return
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
            if self._deny_guest(user, "the shared library"):
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
            if self._deny_guest(user, "the shared library"):
                return
            from agent_monitor import library

            self._send(200, json.dumps(library.update_settings(body), ensure_ascii=False))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def do_DELETE(self):  # noqa: N802
        path = self._resolve_path()
        if path is None:
            return

        if self._research_route(path, "DELETE", {}) or self._sharing_write(path, "DELETE", {}) or self._projects_route(path, "DELETE", {}):
            return
        user = self._require_user(path)
        if user is None:
            return

        if path.startswith("/api/agent/skills/"):
            if self._deny_guest(user, "the shared agent profile"):
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
            if self._deny_guest(user, "the shared agent profile"):
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
            if self._deny_guest(user, "the shared library"):
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
                data["sandbox_preview"] = _sandbox_preview(run_id, data)
                ws = _workspace_for_run(run_id)
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


def main(port: int | None = None):
    port = port or PORT
    ensure_data_dirs()
    recovered = job_manager.reconcile_stale_running_runs(min_age_seconds=0)
    if recovered:
        print(f"[recovery] marked {len(recovered)} orphaned run(s) interrupted: {', '.join(recovered)}")
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
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
