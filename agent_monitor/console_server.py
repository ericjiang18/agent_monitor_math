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
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

from agent_monitor import CACHE_DIR, PROBLEMS_DIR, ROOT, RUNS_DIR
from agent_monitor import jobs as job_manager
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


def _workspace_target(workspace: Path, relative: str) -> Path | None:
    """Resolve a path only when its real target stays inside ``workspace``."""
    root = workspace.resolve()
    target = (root / relative).resolve()
    return target if target.is_relative_to(root) else None


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
    "/api/auth/me",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/verify-reset-token",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        print(f"[console] {self.address_string()} {fmt % args}")

    def log_request(self, code="-", size="-"):
        """Log request paths without query strings that may contain reset tokens."""
        path = urlparse(self.path).path
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
            out["Set-Cookie"] = self._session_cookie_header(pending)
            self._pending_session_cookie = None
        return out

    def _issue_guest(self) -> dict:
        from agent_monitor import auth

        account = auth.ensure_guest_user()
        self._pending_session_cookie = auth.create_session(int(account["id"]))
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

        return auth.user_for_token(self._session_token())

    def _session_cookie_header(self, token: str | None) -> str:
        """Set-Cookie value; token=None clears the cookie."""
        from agent_monitor.auth import SESSION_COOKIE, SESSION_TTL_DAYS

        # Only mark Secure on HTTPS. Using Host!=localhost used to drop cookies
        # for anyone opening the console over plain HTTP on a public IP.
        secure = " Secure;" if self._is_https() else ""
        cookie_path = self._request_base_path() or "/"
        if token is None:
            return f"{SESSION_COOKIE}=; Path={cookie_path}; Max-Age=0; HttpOnly;{secure} SameSite=Lax"
        max_age = SESSION_TTL_DAYS * 86400
        return f"{SESSION_COOKIE}={token}; Path={cookie_path}; Max-Age={max_age}; HttpOnly;{secure} SameSite=Lax"

    def _require_user(self, path: str) -> dict | None:
        """Return the user, or answer 401/redirect and return None."""
        user = self._current_user()
        if user:
            return user
        if PUBLIC_MODE:
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
        owner = job_manager.run_owner(run_id)
        if owner is None:
            return bool(user.get("is_admin"))  # legacy runs belong to the operator
        return owner == user.get("id") or bool(user.get("is_admin"))

    def _send_bytes(self, code: int, body: bytes, ctype: str, *, download_name: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        for k, v in self._merge_headers(None).items():
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

        if path.rstrip("/") in ("/login", "/reset-password"):
            if path.rstrip("/") == "/login" and PUBLIC_MODE:
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
            from agent_monitor.auth import google_client_id

            user = self._current_user()
            if user is None and PUBLIC_MODE:
                user = self._issue_guest()
            self._send(
                200,
                json.dumps(
                    {
                        "user": user,
                        "google_client_id": google_client_id(),
                        "public": PUBLIC_MODE,
                    }
                ),
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

            self._send(200, json.dumps({"engines": list_engines()}))
            return

        if path == "/api/monitor/overview":
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
            owner = None if user.get("is_admin") else user["id"]
            self._send(200, json.dumps({"jobs": job_manager.list_jobs(owner_id=owner)}))
            return

        if path == "/api/agent/config":
            from agent_monitor import agent_config

            self._send(200, json.dumps(agent_config.get_agent_config(), ensure_ascii=False))
            return

        if path == "/api/settings":
            from agent_monitor.settings import get_settings

            self._send(200, json.dumps(get_settings(user), ensure_ascii=False))
            return

        if path == "/api/settings/codex/status":
            from agent_monitor import codex_login

            self._send(200, json.dumps(codex_login.status(user["id"]), ensure_ascii=False))
            return

        if path == "/api/settings/codex/login/poll":
            from agent_monitor import codex_login

            self._send(200, json.dumps(codex_login.poll_login(user["id"]), ensure_ascii=False))
            return

        if path == "/api/settings/claude/status":
            from agent_monitor import claude_login

            self._send(200, json.dumps(claude_login.status(user["id"]), ensure_ascii=False))
            return

        if path == "/api/settings/claude/login/poll":
            from agent_monitor import claude_login

            self._send(200, json.dumps(claude_login.poll_login(user["id"]), ensure_ascii=False))
            return

        if path == "/api/library":
            from agent_monitor import library

            self._send(200, json.dumps(library.get_library(), ensure_ascii=False))
            return

        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/")
            job = job_manager.get_job(job_id)
            if not job or (job.get("owner_id") not in (None, user["id"]) and not user.get("is_admin")):
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

            model_options = lean_verify.harness_model_options(user)
            engines = lean_verify.harness_engines(
                user, model=model_options["selected"]
            )
            self._send(
                200,
                json.dumps(
                    {
                        **lean_verify.toolchain_status(),
                        "engines": engines,
                        "harness_auth_route": model_options["auth_route"],
                        "harness_models": model_options["models"],
                        "harness_model": model_options["selected"],
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
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, json.dumps({"error": "invalid json"}))
            return

        if path in ("/api/auth/register", "/api/auth/login", "/api/auth/google"):
            from agent_monitor import auth

            try:
                token = None
                if path == "/api/auth/register":
                    account = auth.register(
                        str(body.get("email") or ""),
                        str(body.get("password") or ""),
                        name=body.get("name"),
                    )
                elif path == "/api/auth/login":
                    account, token = auth.login_and_create_session(
                        str(body.get("email") or ""),
                        str(body.get("password") or ""),
                    )
                else:
                    account = auth.login_with_google(str(body.get("credential") or ""))
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": f"Sign-in failed: {exc}"}, ensure_ascii=False))
                return
            if token is None:
                token = auth.create_session(int(account["id"]))
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
        user = self._require_user(path)
        if user is None:
            return

        if path.startswith("/api/library/tools/") and path.endswith("/run"):
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
                        model=(str(body.get("model") or "").strip() or None),
                        kind=kind,
                    )
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"Graph generation failed: {exc}"}, ensure_ascii=False))
                return
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
                        message=str(body.get("message") or body.get("feedback") or ""),
                        model=model,
                        lean=lean_arg,
                        max_repairs=max(0, min(int(repairs) if repairs is not None else 1, 3)),
                    )
                else:
                    repairs = body.get("max_repairs")
                    result = lean_verify.generate(
                        workspace=ws,
                        run_record=_run_record_for(rid),
                        user=user,
                        model=model,
                        max_repairs=max(0, min(int(repairs) if repairs is not None else 2, 4)),
                    )
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"Lean verification failed: {exc}"}, ensure_ascii=False))
                return
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
                job = job_manager.start_job(
                    engine=str(body.get("engine") or "").lower(),
                    problem_id=body.get("problem_id"),
                    problem_text=body.get("problem_text"),
                    model=body.get("model") or None,
                    max_iterations=int(body.get("max_iterations") or 40),
                    user=user,
                    use_subagents=body.get("use_subagents") is not False,
                    subagent_model=(str(body.get("subagent_model") or "").strip() or None),
                    auth_route=(str(body.get("auth_route") or "").strip() or None),
                    reasoning_effort=(
                        str(body.get("reasoning_effort") or "").strip() or None
                    ),
                    speed_mode=(str(body.get("speed_mode") or "").strip() or None),
                )
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

            try:
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
            from agent_monitor import library

            try:
                item = library.upsert_item(body)
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}))
                return
            self._send(200, json.dumps(item, ensure_ascii=False))
            return

        if path == "/api/library/settings":
            from agent_monitor import library

            self._send(200, json.dumps(library.update_settings(body), ensure_ascii=False))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def do_DELETE(self):  # noqa: N802
        path = self._resolve_path()
        if path is None:
            return

        user = self._require_user(path)
        if user is None:
            return

        if path.startswith("/api/agent/skills/"):
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
            if not user.get("is_admin"):
                payload["runs"] = [
                    r for r in (payload.get("runs") or []) if r.get("owner_id") == user["id"]
                ]
            for entry in payload.get("runs") or []:
                if not entry.get("problem_preview"):
                    entry["problem_preview"] = _problem_preview_for(entry.get("run_id"), dash.CACHE_DIR)
            self._send(200, json.dumps(payload, ensure_ascii=False))
            return

        if path == "/api/rebuild":
            self._send(200, json.dumps({"ok": True, "note": "use agent-monitor build"}))
            return

        if path.startswith("/api/run/"):
            rid = path.removeprefix("/api/run/").split("/", 1)[0]
            if not self._owns_run(rid, user):
                self._send(404, json.dumps({"error": "run not found"}))
                return

        if path.startswith("/api/run/") and path.endswith("/export"):
            run_id = path.removeprefix("/api/run/").removesuffix("/export")
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

        if path.startswith("/api/run/") and path.endswith("/final_latex"):
            run_id = path.removeprefix("/api/run/").removesuffix("/final_latex")
            data = dash._load_run(run_id)
            if not data:
                self._send(404, json.dumps({"error": "run not found"}))
                return
            self._send(200, json.dumps(dash._final_latex_payload(run_id, data), ensure_ascii=False))
            return

        if path.startswith("/api/run/") and path.endswith("/final.tex"):
            run_id = path.removeprefix("/api/run/").removesuffix("/final.tex")
            data = dash._load_run(run_id)
            tex = dash._read_final_tex(data) if data else None
            if tex:
                self._send(200, tex, "text/plain; charset=utf-8")
            else:
                self._send(404, json.dumps({"error": "final proof not found"}))
            return

        if path.startswith("/api/run/") and path.endswith("/final.pdf"):
            from harness_dashboard.latex_provenance import (
                compile_pdf,
                pdf_cache_path,
                resolve_latex_path,
            )

            run_id = path.removeprefix("/api/run/").removesuffix("/final.pdf")
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

        if path.startswith("/api/run/"):
            run_id = path.removeprefix("/api/run/")
            # strip nested suffixes already handled
            if "/" in run_id:
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
                    data["auto_pipeline"] = auto_pipeline.load_state(ws)
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
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
