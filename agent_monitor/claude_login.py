"""Per-user Claude Code subscription authentication.

Claude Code's official ``claude auth login --claudeai`` command opens an
OAuth URL and then waits for the authorization code on stdin.  This module
drives that command in the background for the Settings UI while keeping each
signed-in Proving Console user's Claude configuration in a separate private
directory.

Only the authenticated session's numeric user id may be passed into these
functions.  No OAuth credential, authorization code, CLI output, or token is
ever included in a response payload.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from agent_monitor import CACHE_DIR

ACCOUNT_HOME_ROOT = CACHE_DIR.parent / "claude_home" / "users"

DEFAULT_CLAUDE_MODELS = (
    "sonnet",
    "opus",
    "fable",
    "claude-haiku-4-5",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https?://[^\s<>]+")
_AUTH_CACHE_SECONDS = 5.0
_MAX_CREDENTIAL_FILE_BYTES = 64 * 1024
_REAUTH_MARKER_NAME = ".agent-monitor-reauth-required.json"

# Account mode must not silently fall through to a metered API key or a cloud
# provider inherited by the server process.  Keep this list intentionally
# broader than the variables used by Proving Console itself.
_PROVIDER_ENV_VARS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
}

_lock = threading.Lock()
_cache_lock = threading.Lock()
_states: dict[int, dict[str, Any]] = {}
_auth_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _user_key(user_id: int) -> int:
    value = int(user_id)
    if value < 0:
        raise ValueError("invalid user id")
    return value


def _state_for(user_id: int) -> dict[str, Any]:
    """Caller must hold ``_lock``."""
    return _states.setdefault(_user_key(user_id), {"status": "idle", "running": False})


def account_home(user_id: int) -> Path:
    """Return this user's isolated ``CLAUDE_CONFIG_DIR``."""
    root = os.environ.get("AGENT_MONITOR_CLAUDE_ACCOUNT_HOME")
    base = Path(root).expanduser() if root else ACCOUNT_HOME_ROOT
    return base / f"user_{_user_key(user_id)}"


def _ensure_account_home(home: Path) -> None:
    home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        home.parent.chmod(0o700)
    except OSError:
        pass
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)


def account_environment(
    home: Path,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment for Claude subscription commands, with API routes removed."""
    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env(base)
    for name in _PROVIDER_ENV_VARS:
        env.pop(name, None)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    # A private HOME also prevents a subprocess from discovering another
    # account's legacy config, keychain fallback, plugins, or project history.
    env["HOME"] = str(home)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    return env


def claude_binary() -> str | None:
    from agent_monitor.engines_registry import which_tool

    return which_tool("claude")


def _run_auth_status(home: Path) -> dict[str, Any]:
    binary = claude_binary()
    if not binary:
        return {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}
    _ensure_account_home(home)
    try:
        completed = subprocess.run(
            [binary, "auth", "status", "--json"],
            env=account_environment(home),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
            umask=0o077,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}
    try:
        payload = json.loads(completed.stdout or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # Whitelist harmless metadata.  Future CLI versions may add credentials to
    # the JSON payload; those fields must never cross this module boundary.
    return {
        "loggedIn": bool(payload.get("loggedIn")),
        "authMethod": str(payload.get("authMethod") or "none"),
        "apiProvider": str(payload.get("apiProvider") or "firstParty"),
        **(
            {"subscriptionType": str(payload["subscriptionType"])}
            if payload.get("subscriptionType")
            else {}
        ),
    }


def _auth_status(home: Path, *, force: bool = False) -> dict[str, Any]:
    key = str(home)
    now = time.monotonic()
    with _cache_lock:
        cached = _auth_cache.get(key)
    if not force and cached and now - cached[0] < _AUTH_CACHE_SECONDS:
        return dict(cached[1])
    payload = _run_auth_status(home)
    with _cache_lock:
        _auth_cache[key] = (time.monotonic(), dict(payload))
    return payload


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    # Current Claude Code credentials use epoch milliseconds. Accept epoch
    # seconds too so a harmless format change cannot invert readiness.
    return parsed / 1000.0 if parsed > 100_000_000_000 else parsed


def _refresh_credential_state(
    home: Path, *, now: float | None = None
) -> dict[str, bool]:
    """Inspect only local refresh-expiry metadata, never credential values.

    An expired access token is normal and must not force reauthentication: the
    official CLI rotates it with the refresh token. A known-expired or
    malformed refresh credential cannot recover, so it is safe to fail closed.
    Missing files remain compatible with CLI versions backed by a keychain.
    """

    result = {"present": False, "invalid": False, "expired": False}
    path = home / ".credentials.json"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return result
    except OSError:
        result.update(present=True, invalid=True)
        return result
    result["present"] = True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CREDENTIAL_FILE_BYTES:
            result["invalid"] = True
            return result
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            payload = json.load(stream)
    except (OSError, RecursionError, UnicodeError, TypeError, ValueError):
        result["invalid"] = True
        return result
    finally:
        if fd >= 0:
            os.close(fd)
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict):
        result["invalid"] = True
        return result
    refresh_token = oauth.get("refreshToken")
    refresh_expires = _timestamp(oauth.get("refreshTokenExpiresAt"))
    if not isinstance(refresh_token, str) or not refresh_token.strip() or refresh_expires is None:
        result["invalid"] = True
        return result
    current = time.time() if now is None else float(now)
    result["expired"] = refresh_expires <= current
    return result


def _reauth_marker(home: str | Path) -> Path:
    return Path(home) / _REAUTH_MARKER_NAME


def _has_reauth_marker(home: Path) -> bool:
    try:
        return stat.S_ISREG(_reauth_marker(home).lstat().st_mode)
    except OSError:
        return False


def mark_reauth_required(home: str | Path, *, reason: str = "oauth_refresh_failed") -> None:
    """Persist a secret-free marker after a final, unrecoverable OAuth 401."""

    config_home = Path(home)
    _ensure_account_home(config_home)
    marker = _reauth_marker(config_home)
    safe_reason = (
        reason
        if reason in {"oauth_expired", "oauth_refresh_failed", "oauth_unusable"}
        else "oauth_unusable"
    )
    temporary = config_home / (
        f".{_REAUTH_MARKER_NAME}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(temporary, flags, 0o600)
        payload = json.dumps(
            {"version": 1, "reason": safe_reason, "marked_at": int(time.time())},
            separators=(",", ":"),
        ).encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, marker)
        marker.chmod(0o600)
        try:
            directory_fd = os.open(
                config_home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # The marker already exists atomically; directory fsync is an
            # additional crash-durability guarantee where supported.
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def clear_reauth_required(home: str | Path) -> None:
    """Clear only Proving Console's marker after login or a successful turn."""

    try:
        _reauth_marker(home).unlink()
    except FileNotFoundError:
        pass


def _account_state(
    home: Path,
    auth: Mapping[str, Any] | None = None,
    *,
    ignore_marker: bool = False,
) -> dict[str, bool]:
    payload = dict(auth) if auth is not None else _auth_status(home)
    method = str(payload.get("authMethod") or "").strip().lower().replace("_", "")
    subscription_login = bool(payload.get("loggedIn")) and method not in {"apikey", "api"}
    refresh = _refresh_credential_state(home) if subscription_login else {}
    local_failure = bool(refresh.get("invalid") or refresh.get("expired"))
    marked = False if ignore_marker else _has_reauth_marker(home)
    reauth_required = marked or (subscription_login and local_failure)
    return {
        "connected": subscription_login and not reauth_required,
        "reauth_required": reauth_required,
        "auth_expired": bool(marked or refresh.get("expired")),
    }


def account_login_ready(home: Path) -> bool:
    """True only for usable account OAuth, never API or marked-stale auth."""
    return _account_state(home)["connected"]


def account_reauth_required(home: Path) -> bool:
    """True when this account must reconnect before subscription routing."""
    return _account_state(home)["reauth_required"]


def available_models(user_id: int) -> list[str]:
    """Stable Claude Code account aliases accepted by the official CLI."""
    return list(DEFAULT_CLAUDE_MODELS) if account_login_ready(account_home(user_id)) else []


def _public_state(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "running",
        "url",
        "awaiting_code",
        "code_submitted",
        "started_at",
        "error",
    }
    return {key: value for key, value in snapshot.items() if key in allowed}


def _augment(user_id: int, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    home = account_home(user_id)
    auth = _auth_status(home)
    account = _account_state(home, auth)
    connected = account["connected"]
    return {
        **_public_state(snapshot),
        "connected": connected,
        "reauth_required": account["reauth_required"],
        "auth_expired": account["auth_expired"],
        "claude_installed": claude_binary() is not None,
        "models": list(DEFAULT_CLAUDE_MODELS) if connected else [],
        "auth_method": auth.get("authMethod") or "none",
        "api_provider": auth.get("apiProvider") or "firstParty",
        **(
            {"subscription_type": auth["subscriptionType"]}
            if auth.get("subscriptionType")
            else {}
        ),
    }


def status(user_id: int) -> dict[str, Any]:
    with _lock:
        snapshot = dict(_state_for(user_id))
    return _augment(user_id, snapshot)


def poll_login(user_id: int) -> dict[str, Any]:
    return status(user_id)


def _reader(proc: subprocess.Popen[str], home: Path, user_id: int) -> None:
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = _ANSI_RE.sub("", raw_line.rstrip("\r\n"))
            with _lock:
                st = _state_for(user_id)
                if st.get("status") != "running":
                    break
                urls = _URL_RE.findall(line)
                if urls:
                    st["url"] = urls[-1].rstrip(").,\"")
                    st["awaiting_code"] = True
                if "paste code" in line.lower():
                    st["awaiting_code"] = True
    except (OSError, ValueError):
        pass
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1

    auth = _auth_status(home, force=True)
    connected = _account_state(home, auth, ignore_marker=True)["connected"]
    if rc == 0 and connected:
        clear_reauth_required(home)
    with _lock:
        st = _state_for(user_id)
        if st.get("status") == "cancelled":
            return
        st.pop("proc", None)
        st["running"] = False
        st["awaiting_code"] = False
        if rc == 0 and connected:
            st["status"] = "success"
            st.pop("error", None)
        else:
            st["status"] = "error"
            st["error"] = f"claude auth login exited with code {rc}"


def start_login(user_id: int) -> dict[str, Any]:
    user_id = _user_key(user_id)
    with _lock:
        st = _state_for(user_id)
        if st.get("status") == "running":
            snapshot = dict(st)
        else:
            binary = claude_binary()
            if not binary:
                st.clear()
                st.update(
                    {
                        "status": "error",
                        "running": False,
                        "error": "Claude Code CLI not installed",
                    }
                )
                return _augment(user_id, st)
            home = account_home(user_id)
            _ensure_account_home(home)
            try:
                proc = subprocess.Popen(
                    [binary, "auth", "login", "--claudeai"],
                    env=account_environment(home),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    umask=0o077,
                )
            except OSError as exc:
                st.clear()
                st.update({"status": "error", "running": False, "error": str(exc)})
                return _augment(user_id, st)
            st.clear()
            st.update(
                {
                    "status": "running",
                    "running": True,
                    "url": None,
                    "awaiting_code": False,
                    "code_submitted": False,
                    "started_at": time.time(),
                    "proc": proc,
                }
            )
            snapshot = dict(st)
            threading.Thread(
                target=_reader,
                args=(proc, home, user_id),
                daemon=True,
                name=f"claude-login-{user_id}",
            ).start()
    return _augment(user_id, snapshot)


def submit_code(user_id: int, code: str) -> dict[str, Any]:
    """Send the one-time OAuth code to this user's active login process."""
    value = str(code or "").strip()
    if not value or len(value) > 4096 or "\n" in value or "\r" in value:
        raise ValueError("A valid Claude authorization code is required")
    with _lock:
        st = _state_for(user_id)
        proc: subprocess.Popen[str] | None = st.get("proc")
        if st.get("status") != "running" or proc is None or proc.stdin is None:
            raise ValueError("No Claude login is waiting for an authorization code")
        try:
            proc.stdin.write(value + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ValueError("Claude login is no longer accepting a code") from exc
        st["code_submitted"] = True
        st["awaiting_code"] = False
        snapshot = dict(st)
    # ``value`` is deliberately never stored, logged, or returned.
    return _augment(user_id, snapshot)


def cancel_login(user_id: int) -> dict[str, Any]:
    with _lock:
        st = _state_for(user_id)
        proc: subprocess.Popen[str] | None = st.get("proc")
        st.clear()
        st.update({"status": "cancelled", "running": False})
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                proc.terminate()
            except OSError:
                pass
    return _augment(user_id, {"status": "cancelled", "running": False})


def logout(user_id: int) -> dict[str, Any]:
    """Disconnect this user's account through the official Claude CLI."""
    user_id = _user_key(user_id)
    with _lock:
        running = _state_for(user_id).get("status") == "running"
    if running:
        cancel_login(user_id)

    home = account_home(user_id)
    binary = claude_binary()
    error = ""
    if binary and home.exists():
        try:
            subprocess.run(
                [binary, "auth", "logout"],
                env=account_environment(home),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                umask=0o077,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = str(exc)
    with _cache_lock:
        _auth_cache.pop(str(home), None)
    with _lock:
        st = _state_for(user_id)
        st.clear()
        st.update({"status": "idle", "running": False})
    auth = _auth_status(home, force=True)
    method = str(auth.get("authMethod") or "").strip().lower().replace("_", "")
    officially_connected = bool(auth.get("loggedIn")) and method not in {"apikey", "api"}
    if not officially_connected:
        clear_reauth_required(home)
    result = _augment(user_id, {"status": "idle", "running": False})
    result["ok"] = not officially_connected
    if error and officially_connected:
        result["error"] = error
    return result
