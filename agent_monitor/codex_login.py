"""ChatGPT/OpenAI-account (OAuth device-code) login for the Codex CLI engine.

By default the ``codex`` engine bills against ``OPENAI_API_KEY`` (metered API
usage). Logging Codex in with a ChatGPT/OpenAI account instead makes runs
consume that account's subscription usage (Plus/Pro/Team/Codex) rather than
per-token API cost. ``agent_monitor.jobs._ensure_codex_auth`` prefers this
login, when present, for that user's ``codex`` engine runs.

Every login is scoped to the signed-in user (own ``CODEX_HOME`` directory, own
in-memory login state) — the same isolation model as per-user API keys in
``agent_monitor.settings``/``agent_monitor.auth``. Callers must always pass the
``user_id`` from the authenticated session; nothing here accepts a caller-
supplied user id from request bodies, so one user can never see, cancel, or
disconnect another user's Codex account.

This module drives ``codex login --device-auth`` as a background subprocess
so the console's Settings UI can show the sign-in URL/code and poll for
completion without blocking the request thread.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from agent_monitor import CACHE_DIR

ACCOUNT_HOME_ROOT = CACHE_DIR.parent / "codex_home" / "users"

_URL_RE = re.compile(r"https?://\S+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CODE_RE = re.compile(r"^[A-Z0-9]{3,6}-[A-Z0-9]{3,6}$")
_MAX_OUTPUT_CHARS = 4000

_lock = threading.Lock()
_states: dict[int, dict[str, Any]] = {}


def _state_for(user_id: int) -> dict[str, Any]:
    """Caller must hold ``_lock``."""
    return _states.setdefault(user_id, {"status": "idle", "running": False})


def account_home(user_id: int) -> Path:
    """Each user gets their own CODEX_HOME — never shared across accounts."""
    root = os.environ.get("AGENT_MONITOR_CODEX_ACCOUNT_HOME")
    base = Path(root).expanduser() if root else ACCOUNT_HOME_ROOT
    return base / f"user_{user_id}"


def account_login_ready(home: Path) -> bool:
    """True if ``home`` holds a ChatGPT-account (OAuth) Codex login.

    An account login's auth.json carries a ``tokens`` bundle (id_token /
    access_token / refresh_token from the ChatGPT OAuth exchange); a bare
    API-key login is just ``{"OPENAI_API_KEY": "..."}`` with no tokens.
    """
    auth_path = home / "auth.json"
    if not auth_path.exists():
        return False
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    tokens = data.get("tokens") or {}
    return bool(tokens.get("access_token") or tokens.get("id_token"))


def codex_binary() -> str | None:
    return shutil.which("codex")


def _augment(user_id: int, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Attach install/connection info so every response shape is self-contained.

    The UI renders directly off whatever a request returns (start/poll/status
    all feed the same render function), so each of them must carry
    ``codex_installed`` / ``connected`` — leaving them out reads as "false" on
    the frontend and wrongly claims Codex isn't installed mid-login.
    """
    home = account_home(user_id)
    snapshot["connected"] = account_login_ready(home)
    snapshot["codex_installed"] = codex_binary() is not None
    snapshot["codex_home"] = str(home)
    return snapshot


def status(user_id: int) -> dict[str, Any]:
    """Snapshot of the current/last login attempt plus static install info."""
    with _lock:
        st = {k: v for k, v in _state_for(user_id).items() if k != "proc"}
    return _augment(user_id, st)


def _reader(proc: subprocess.Popen, home: Path, user_id: int) -> None:
    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            # codex CLI emits ANSI color codes even with NO_COLOR=1 set.
            line = _ANSI_RE.sub("", raw_line.rstrip("\n"))
            lines.append(line)
            text = "\n".join(lines)
            with _lock:
                st = _state_for(user_id)
                if st.get("status") != "running":
                    break  # cancelled from elsewhere
                st["output"] = text[-_MAX_OUTPUT_CHARS:]
                urls = _URL_RE.findall(text)
                if urls:
                    st["url"] = urls[-1].rstrip(").,")
                if _CODE_RE.match(line.strip()):
                    st["code"] = line.strip()
    except (OSError, ValueError):
        pass
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1
    with _lock:
        st = _state_for(user_id)
        if st.get("status") == "cancelled":
            return
        st["running"] = False
        if rc == 0 and account_login_ready(home):
            st["status"] = "success"
        else:
            st["status"] = "error"
            if rc != 0 and not st.get("error"):
                st["error"] = f"codex login exited with code {rc}"


def start_login(user_id: int) -> dict[str, Any]:
    with _lock:
        st = _state_for(user_id)
        if st.get("status") == "running":
            return _augment(user_id, {k: v for k, v in st.items() if k != "proc"})
        codex = codex_binary()
        if not codex:
            st.clear()
            st.update({"status": "error", "running": False, "error": "codex CLI not installed"})
            return _augment(user_id, dict(st))
        home = account_home(user_id)
        home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        env.setdefault("NO_COLOR", "1")
        try:
            proc = subprocess.Popen(
                [codex, "login", "--device-auth"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            st.clear()
            st.update({"status": "error", "running": False, "error": str(exc)})
            return _augment(user_id, dict(st))
        st.clear()
        st.update(
            {
                "status": "running",
                "running": True,
                "output": "",
                "url": None,
                "started_at": time.time(),
                "proc": proc,
            }
        )
        snapshot = {k: v for k, v in st.items() if k != "proc"}
    threading.Thread(target=_reader, args=(proc, home, user_id), daemon=True).start()
    return _augment(user_id, snapshot)


def cancel_login(user_id: int) -> dict[str, Any]:
    with _lock:
        st = _state_for(user_id)
        proc: subprocess.Popen | None = st.get("proc")
        st["status"] = "cancelled"
        st["running"] = False
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass
    return _augment(user_id, {"status": "cancelled", "running": False})


def poll_login(user_id: int) -> dict[str, Any]:
    with _lock:
        st = {k: v for k, v in _state_for(user_id).items() if k != "proc"}
    return _augment(user_id, st)


def logout(user_id: int) -> dict[str, Any]:
    """Remove the cached account login (does not touch API-key homes)."""
    home = account_home(user_id)
    auth_path = home / "auth.json"
    if auth_path.exists():
        auth_path.unlink()
    return _augment(user_id, {"ok": True})
