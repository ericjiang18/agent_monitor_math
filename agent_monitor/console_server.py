"""Unified Math Proving Console HTTP server."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_monitor import CACHE_DIR, PROBLEMS_DIR, ROOT, RUNS_DIR
from agent_monitor import jobs as job_manager
from agent_monitor.paths import ensure_data_dirs, ensure_import_paths

ensure_import_paths()
ensure_data_dirs()

# Load API keys from Agent_Monitor/.env so all engine subprocesses inherit them.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
DASHBOARD_WEB = ROOT / "monitor_core" / "harness_dashboard" / "web"
PORT = int(os.environ.get("AGENT_MONITOR_PORT", os.environ.get("LLM_MONITOR_PORT", "4600")))


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


def _with_base(path: str) -> str:
    """Prefix an app-absolute path with BASE_PATH for redirects / cookies."""
    if not path.startswith("/"):
        path = "/" + path
    return f"{BASE_PATH}{path}" if BASE_PATH else path


def _inject_base(html: str) -> str:
    """Inject window.__PC_BASE__ so frontends rewrite absolute /api URLs."""
    snip = (
        "<script>"
        f"window.__PC_BASE__={json.dumps(BASE_PATH)};"
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


def _app_path(raw_path: str) -> tuple[str | None, str]:
    """Strip BASE_PATH from the request path.

    Returns (app_path, error). error is 'not_found' when the public path is
    outside the secret prefix (except bare /health for local ops).
    """
    path = raw_path or "/"
    if not BASE_PATH:
        return path, ""
    if path == "/health":
        return path, ""
    if path == BASE_PATH or path.startswith(BASE_PATH + "/"):
        rest = path[len(BASE_PATH) :] or "/"
        return rest, ""
    return None, "not_found"


def _cache_harness() -> Path:
    return Path(os.environ.get("LLM_DASHBOARD_CACHE", str(CACHE_DIR))) / "harness"


def _run_record_for(run_id: str) -> dict | None:
    """The cached run record for a run, or None if it isn't readable."""
    rec_path = _cache_harness() / f"{run_id}.json"
    if not rec_path.exists():
        return None
    try:
        return json.loads(rec_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


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
    if not str(ws).startswith(str(WORKSPACES_ROOT)):
        return None
    return ws if ws.is_dir() else None


def _list_workspace_files(ws: Path) -> list[dict]:
    out = []
    for p in sorted(ws.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        rel = str(p.relative_to(ws))
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

    tex_path = (ws / tex_rel).resolve()
    if not str(tex_path).startswith(str(ws)) or not tex_path.exists():
        return None
    if not _shutil.which("pdflatex"):
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
        for _ in range(2):
            try:
                _subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", "doc.tex"],
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
_PUBLIC_PATHS = {"/login", "/health", "/api/auth/login", "/api/auth/register", "/api/auth/google", "/api/auth/me"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        print(f"[console] {self.address_string()} {fmt % args}")

    def _send(self, code: int, body: str, ctype: str = "application/json", headers: dict[str, str] | None = None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

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

        host = (self.headers.get("Host") or "").split(":")[0]
        secure = "" if host in {"localhost", "127.0.0.1"} else " Secure;"
        cookie_path = BASE_PATH or "/"
        if token is None:
            return f"{SESSION_COOKIE}=; Path={cookie_path}; Max-Age=0; HttpOnly;{secure} SameSite=Lax"
        max_age = SESSION_TTL_DAYS * 86400
        return f"{SESSION_COOKIE}={token}; Path={cookie_path}; Max-Age={max_age}; HttpOnly;{secure} SameSite=Lax"

    def _require_user(self, path: str) -> dict | None:
        """Return the user, or answer 401/redirect and return None."""
        user = self._current_user()
        if user:
            return user
        login = _with_base("/login")
        if path.startswith("/api/"):
            self._send(401, json.dumps({"error": "not signed in", "login": login}))
        else:
            self.send_response(302)
            self.send_header("Location", login)
            self.end_headers()
        return None

    def _send_html(self, html: str, code: int = 200):
        self._send(code, _inject_base(html), "text/html; charset=utf-8")

    def _resolve_path(self) -> str | None:
        """Parse URL path, enforce BASE_PATH gate, return app-relative path."""
        parsed = urlparse(self.path)
        app_path, err = _app_path(parsed.path)
        if err:
            self._send(404, "Not Found", "text/plain; charset=utf-8")
            return None
        return app_path

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

        if path == "/login":
            html_path = WEB_DIR / "login.html"
            self._send_html(html_path.read_text(encoding="utf-8"))
            return

        if path == "/api/auth/me":
            from agent_monitor.auth import google_client_id

            self._send(
                200,
                json.dumps({"user": self._current_user(), "google_client_id": google_client_id()}),
            )
            return

        if path == "/health":
            self._send(200, json.dumps({"ok": True, "service": "agent-monitor-console"}))
            return

        if path.startswith("/static/"):
            rel = path.removeprefix("/static/")
            for base in (WEB_DIR, DASHBOARD_WEB):
                f = base / rel
                if f.exists() and f.is_file():
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
            from agent_monitor import proof_graph

            rid = path.removeprefix("/api/run/").removesuffix("/proof_graph")
            ws = _workspace_for_run(rid)
            if not ws or not self._owns_run(rid, user):
                self._send(404, json.dumps({"error": "run not found"}))
                return
            kind = (qs.get("kind") or ["informal"])[0].strip().lower()
            if kind not in {"informal", "formal"}:
                kind = "informal"
            if kind == "formal":
                cached = proof_graph.load_or_parse_formal(ws)
            else:
                cached = proof_graph.load_cached(ws, kind="informal")
            self._send(200, json.dumps(cached or {"status": "none", "kind": kind}, ensure_ascii=False))
            return

        if path == "/api/lean/status":
            from agent_monitor import lean_verify

            self._send(
                200,
                json.dumps(
                    {
                        **lean_verify.toolchain_status(),
                        "engines": lean_verify.harness_engines(),
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
            cached = lean_verify.load_cached(ws) or {"status": "none"}
            # Backfill citations for older caches (from Lean docstring / proof.md).
            if cached.get("lean") and not cached.get("citations"):
                cached["citations"] = lean_verify._merge_citations(
                    lean_verify._citations_from_lean(str(cached.get("lean") or "")),
                    lean_verify._citations_from_proof_md(ws),
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
        target = (ws / rel).resolve()
        if not rel or not str(target).startswith(str(ws)):
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
                if path == "/api/auth/register":
                    account = auth.register(
                        str(body.get("email") or ""),
                        str(body.get("password") or ""),
                        name=body.get("name"),
                    )
                elif path == "/api/auth/login":
                    account = auth.login(str(body.get("email") or ""), str(body.get("password") or ""))
                else:
                    account = auth.login_with_google(str(body.get("credential") or ""))
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
                return
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": f"Sign-in failed: {exc}"}, ensure_ascii=False))
                return
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

        user = self._require_user(path)
        if user is None:
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
            from agent_monitor import proof_graph

            rid = path.removeprefix("/api/run/").removesuffix("/proof_graph")
            ws = _workspace_for_run(rid)
            if not ws or not self._owns_run(rid, user):
                self._send(404, json.dumps({"error": "run not found"}))
                return
            run_record = None
            rec_path = _cache_harness() / f"{rid}.json"
            if rec_path.exists():
                try:
                    run_record = json.loads(rec_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    run_record = None
            try:
                result = proof_graph.generate(
                    workspace=ws,
                    run_record=run_record,
                    user=user,
                    model=(str(body.get("model") or "").strip() or None),
                    kind=str(body.get("kind") or "informal"),
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
                            entry["content"] = (ws / f["path"]).read_text(encoding="utf-8", errors="replace")
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
            data = dash._load_run(run_id)
            if data:
                from agent_monitor.schema import normalize_run

                data = normalize_run(data, engine=data.get("engine") or "hermes")
                self._send(200, json.dumps(data, ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "run not found"}))
            return

        if path == "/api/call_detail":
            tid = qs.get("trace_id", [""])[0]
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
    root = f"http://localhost:{port}{BASE_PATH or ''}"
    print(f"Unified Math Proving Console -> {root}/")
    if BASE_PATH:
        print(f"  public base path         -> {BASE_PATH}")
    print(f"  classic monitor           -> {root}/monitor")
    print(f"  cache: {_cache_harness()}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
