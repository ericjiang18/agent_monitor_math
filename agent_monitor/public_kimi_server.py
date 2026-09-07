"""Standalone, anonymous, rate-limited Kimi proof service.

This is deliberately not a mode of ``console_server``.  It has a separate
listener, data root, credential boundary, UI, and four-operation API.  The
browser can use only the response-only Plain harness vetted for this public
surface; provider, model, key, endpoint, tools, continuations, settings, and
account routes are not request parameters.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from agent_monitor import DATA_DIR, ROOT
from agent_monitor.public_kimi_gateway import PublicKimiGateway


MODEL_ID = "kimi-k3"
PUBLIC_ENGINES = ("plain",)
SPONSORED_ENGINES = frozenset(
    {
        "codex",
        "deepagents",
        "deepseek_harness",
        "formal",
        "improof",
        "math_harness",
        "metaharness",
        "openclaude",
        "openclaw",
        "openhands",
    }
)
SESSION_COOKIE = "public_kimi_session"
_SESSION_RE = re.compile(r"^[a-f0-9]{64}$")
_SPONSOR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_RUN_RE = re.compile(r"^[a-z0-9_]{1,180}$")
_SECRET_RE = re.compile(r"(?i)\b(?:sk[-_][a-z0-9._-]{8,}|bearer\s+[a-z0-9._-]{16,})")
_TERMINAL = frozenset({"finished", "failed", "stopped", "cancelled", "error"})
_ACTIVE = frozenset({"queued", "running", "stopping"})
_REQUEST_BODY_TIMEOUT_SECONDS = 10.0
_REQUEST_BODY_CHUNK_BYTES = 16 * 1024
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


@dataclass(frozen=True)
class PublicLimits:
    problem_characters: int = 12_000
    request_bytes: int = 32 * 1024
    max_iterations: int = 8
    active_runs: int = 1
    global_active_runs: int = 2
    requests_per_hour: int = 5
    global_requests_per_hour: int = 20
    artifact_ttl_hours: int = 24
    max_output_tokens: int = 8192
    job_timeout_seconds: int = 600
    upstream_timeout_seconds: int = 225
    gateway_calls_per_hour: int = 120
    gateway_concurrent_calls: int = 4
    sponsor_request_bytes: int = 2 * 1024 * 1024
    sponsor_token_ttl_seconds: int = 7200
    # One original-console user can legitimately select every advertised
    # harness plus the shared Formal/Lean-DAG scope during a debugging hour.
    # Keep the per-token and global gateway call caps as the cost boundary,
    # while making the issuance limit internally consistent with that matrix.
    sponsor_tokens_per_ip: int = 24
    sponsor_token_starts_per_hour: int = 20
    sponsor_calls_per_token: int = 80

    def public(self) -> dict[str, int]:
        return {
            "problem_characters": self.problem_characters,
            "request_bytes": self.request_bytes,
            "active_runs": self.active_runs,
            "global_active_runs": self.global_active_runs,
            "requests_per_hour": self.requests_per_hour,
            "global_requests_per_hour": self.global_requests_per_hour,
            "artifact_ttl_hours": self.artifact_ttl_hours,
            "max_output_tokens": self.max_output_tokens,
        }


class PublicAPIError(RuntimeError):
    def __init__(self, status: int, message: str, *, retry_after: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class SubmissionLimiter:
    """In-process admission control; the gateway separately caps model calls."""

    def __init__(self, limits: PublicLimits) -> None:
        self.limits = limits
        self._lock = threading.Lock()
        self._global: deque[float] = deque()
        self._sessions: dict[str, deque[float]] = defaultdict(deque)
        self._ips: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def _prune(values: deque[float], now: float) -> None:
        while values and now - values[0] >= 3600:
            values.popleft()

    def admit(self, session_key: str, ip_key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(self._global, now)
            for mapping in (self._sessions, self._ips):
                for key, values in list(mapping.items()):
                    self._prune(values, now)
                    if not values:
                        mapping.pop(key, None)
            if len(self._global) >= self.limits.global_requests_per_hour:
                retry = max(1, int(3600 - (now - self._global[0])))
                raise PublicAPIError(
                    429, "Public run quota reached; try again later", retry_after=retry
                )
            session_values = self._sessions.get(session_key)
            ip_values = self._ips.get(ip_key)
            for values in (session_values, ip_values):
                if values is not None and len(values) >= self.limits.requests_per_hour:
                    retry = max(1, int(3600 - (now - values[0])))
                    raise PublicAPIError(
                        429, "Public run quota reached; try again later", retry_after=retry
                    )
            # Insert visitor keys only after every quota check succeeds.  The
            # global cap plus old-key pruning bounds both maps against a flood
            # of fresh denied cookies.
            self._global.append(now)
            self._sessions[session_key].append(now)
            self._ips[ip_key].append(now)


@dataclass
class _SponsorSession:
    engine: str
    ip_key: str
    expires_at: float
    calls: int = 0


class SponsorSessionStore:
    """Short-lived proxy credentials; the upstream provider key never leaves."""

    def __init__(self, limits: PublicLimits) -> None:
        self.limits = limits
        self._lock = threading.Lock()
        self._tokens: dict[str, _SponsorSession] = {}
        self._issues: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _prune(self, now: float) -> None:
        for digest, session in list(self._tokens.items()):
            if session.expires_at <= now:
                self._tokens.pop(digest, None)
        for key, values in list(self._issues.items()):
            while values and now - values[0] >= 3600:
                values.popleft()
            if not values:
                self._issues.pop(key, None)

    def issue(self, *, engine: str, ip: str) -> tuple[str, int]:
        if engine not in SPONSORED_ENGINES:
            raise PublicAPIError(400, "Unsupported sponsored Kimi harness")
        now = time.monotonic()
        ip_key = _key_hash(ip)
        with self._lock:
            self._prune(now)
            issues = self._issues.get(ip_key)
            if issues is not None and len(issues) >= self.limits.sponsor_token_starts_per_hour:
                retry = max(1, int(3600 - (now - issues[0])))
                raise PublicAPIError(
                    429, "Sponsored Kimi session quota reached", retry_after=retry
                )
            active = sum(
                1 for session in self._tokens.values() if session.ip_key == ip_key
            )
            if active >= self.limits.sponsor_tokens_per_ip:
                raise PublicAPIError(
                    429, "Too many sponsored Kimi sessions", retry_after=30
                )
            token = secrets.token_urlsafe(48)
            self._tokens[self._token_hash(token)] = _SponsorSession(
                engine=engine,
                ip_key=ip_key,
                expires_at=now + self.limits.sponsor_token_ttl_seconds,
            )
            self._issues[ip_key].append(now)
            return token, self.limits.sponsor_token_ttl_seconds

    def authorize(self, token: str, *, charge: bool) -> str:
        candidate = str(token or "").strip()
        if not _SPONSOR_TOKEN_RE.fullmatch(candidate):
            raise PublicAPIError(401, "Sponsored Kimi authorization required")
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            session = self._tokens.get(self._token_hash(candidate))
            if session is None:
                raise PublicAPIError(401, "Sponsored Kimi session expired")
            if charge and session.calls >= self.limits.sponsor_calls_per_token:
                raise PublicAPIError(
                    429, "Sponsored Kimi call quota reached", retry_after=60
                )
            if charge:
                session.calls += 1
            return session.engine


def _bounded_regular_file(path: Path, *, max_bytes: int = 16_384) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 1 or info.st_size > max_bytes:
            raise RuntimeError("Kimi credential must be a bounded regular file")
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise RuntimeError("Kimi credential file is too large")
    return raw.decode("utf-8").strip()


def _parse_env_file(path: Path) -> dict[str, str]:
    allowed = {"KIMI_API_KEY", "KIMI_API_BASE", "KIMI_REASONING_EFFORT"}
    result: dict[str, str] = {}
    for line in _bounded_regular_file(path).splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        key, separator, value = candidate.partition("=")
        key = key.strip()
        if not separator or key not in allowed:
            raise RuntimeError("Dedicated public Kimi env contains an unsupported key")
        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def _upstream_credentials() -> tuple[str, str]:
    """Read LoadCredential first, then the tightly scoped deployment fallback."""
    from agent_monitor.kimi_k3 import DEFAULT_BASE_URL

    key = ""
    base = ""
    credential_dir = str(os.environ.get("CREDENTIALS_DIRECTORY") or "").strip()
    if credential_dir:
        directory = Path(credential_dir)
        key_path = directory / "kimi_api_key"
        if key_path.exists():
            key = _bounded_regular_file(key_path, max_bytes=4096)
        base_path = directory / "kimi_api_base"
        if base_path.exists():
            base = _bounded_regular_file(base_path, max_bytes=2048)
    if not key:
        fallback_path = str(os.environ.get("PUBLIC_KIMI_ENV_PATH") or "").strip()
        fallback = _parse_env_file(Path(fallback_path)) if fallback_path else {}
        key = str(fallback.get("KIMI_API_KEY") or os.environ.get("KIMI_API_KEY") or "").strip()
        base = str(
            base
            or fallback.get("KIMI_API_BASE")
            or os.environ.get("PUBLIC_KIMI_UPSTREAM_BASE")
            or os.environ.get("KIMI_API_BASE")
            or ""
        ).strip()
    if len(key) < 16 or any(character.isspace() for character in key):
        raise RuntimeError("A valid Kimi LoadCredential is required")
    base = (base or DEFAULT_BASE_URL).rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise RuntimeError("The public Kimi upstream must be a plain HTTPS base URL")
    return key, base


_SCRUB_ENV = {
    "CREDENTIALS_DIRECTORY",
    "PUBLIC_KIMI_ENV_PATH",
    "PUBLIC_KIMI_UPSTREAM_BASE",
    "OPENAI_API_KEY", "OPENAI_API_KEYS", "CODEX_API_KEY",
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "AGENT_MONITOR_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "KIMI_API_KEY", "KIMI_API_BASE", "KIMI_BASE_URL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE",
    "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
    "GEMINI_API_KEY", "GEMINI_API_BASE", "GOOGLE_API_KEY",
    "GROQ_API_KEY", "GROQ_API_BASE",
    "TOGETHERAI_API_KEY", "TOGETHERAI_API_BASE",
    "XAI_API_KEY", "XAI_API_BASE", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "CODEX_HOME", "CLAUDE_CONFIG_DIR", "AGENT_MONITOR_CODEX_SUBSCRIPTION",
    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION", "AGENT_MONITOR_MODEL",
    "AGENT_MONITOR_OPENAI_MODEL", "PLAIN_MODEL", "METAHARNESS_MODEL",
    "PLAIN_CMD", "METAHARNESS_CMD",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
}


def _install_worker_environment(data_dir: Path, *, token: str, base_url: str) -> Path:
    """Replace all provider/account state with a loopback-only Kimi route."""
    generated_secret_suffixes = ("_API_KEY", "_API_KEYS", "_AUTH_TOKEN")
    inherited = set(_SCRUB_ENV)
    inherited.update(
        name
        for name in os.environ
        if name.endswith(generated_secret_suffixes) or name.endswith("_CMD")
    )
    for name in inherited:
        os.environ.pop(name, None)
    runtime = data_dir / ".public-kimi-runtime.env"
    content = (
        f"KIMI_API_KEY={token}\n"
        f"KIMI_API_BASE={base_url}\n"
        "KIMI_REASONING_EFFORT=high\n"
        "AGENT_MONITOR_ACCOUNT_RUNTIME=api\n"
        "AGENT_MONITOR_USE_CODEX=0\n"
        "AGENT_MONITOR_USE_CLAUDE=0\n"
    )
    temporary = runtime.with_name(f".{runtime.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, runtime)
    runtime.chmod(0o600)
    os.environ.update(
        {
            "AGENT_MONITOR_ENV_PATH": str(runtime),
            "KIMI_API_KEY": token,
            "KIMI_API_BASE": base_url,
            "KIMI_REASONING_EFFORT": "high",
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
            "AGENT_MONITOR_USE_CODEX": "0",
            "AGENT_MONITOR_USE_CLAUDE": "0",
            "AGENT_MONITOR_PUBLIC": "1",
            "AGENT_MONITOR_PUBLIC_KIMI": "1",
            "AGENT_MONITOR_DISABLE_AUTO_PIPELINE": "1",
            "AGENT_MONITOR_CLI_TIMEOUT": "600",
            "AGENT_MONITOR_API_MAX_TOKENS": "8192",
        }
    )
    return runtime


def _owner_id(token: str) -> int:
    value = int.from_bytes(hashlib.sha256(token.encode("ascii")).digest()[:8], "big")
    return (value & ((1 << 63) - 1)) or 1


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


class PublicKimiApplication:
    def __init__(
        self,
        *,
        jobs: Any,
        data_dir: Path,
        redactions: tuple[str, ...],
        limits: PublicLimits | None = None,
        engine_catalog: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.jobs = jobs
        self.data_dir = data_dir.resolve()
        self.limits = limits or PublicLimits()
        self.redactions = tuple(value for value in redactions if value)
        self.limiter = SubmissionLimiter(self.limits)
        self.sponsor_sessions = SponsorSessionStore(self.limits)
        self.metadata_dir = self.data_dir / "public-runs"
        self.metadata_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.engine_catalog = engine_catalog or self._default_engine_catalog
        self._start_lock = threading.Lock()
        self._expiry_lock = threading.Lock()
        self._last_expiry_check = 0.0
        self._expire_artifacts()

    @staticmethod
    def _default_engine_catalog() -> list[dict[str, Any]]:
        from agent_monitor.engines_registry import list_engines

        return list_engines()

    def harnesses(self) -> list[dict[str, str]]:
        available = {
            str(item.get("id") or ""): item
            for item in self.engine_catalog()
            if item.get("available")
        }
        labels = {"plain": "Plain"}
        descriptions = {"plain": "One bounded Kimi proof call without tools"}
        return [
            {"id": engine, "label": labels[engine], "description": descriptions[engine]}
            for engine in PUBLIC_ENGINES
            if engine in available
        ]

    def config(self) -> dict[str, Any]:
        self._maybe_expire_artifacts()
        return {
            "service": "public-kimi",
            "model": {"id": MODEL_ID, "label": "Kimi K3"},
            "harnesses": self.harnesses(),
            "limits": self.limits.public(),
        }

    def _meta_path(self, run_id: str) -> Path:
        if not _RUN_RE.fullmatch(run_id):
            raise PublicAPIError(404, "Run not found")
        return self.metadata_dir / f"{run_id}.json"

    def _load_meta(self, run_id: str, owner_id: int) -> dict[str, Any]:
        path = self._meta_path(run_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise PublicAPIError(404, "Run not found") from None
        if not isinstance(value, dict) or value.get("owner_id") != owner_id:
            raise PublicAPIError(404, "Run not found")
        return value

    def _all_meta(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for path in self.metadata_dir.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(item, dict):
                found.append(item)
        return found

    def _active_counts(self, *, owner_id: int, ip_key: str) -> tuple[int, int, int]:
        status_by_run = {
            str(item.get("run_id") or ""): str(item.get("status") or "").lower()
            for item in self.jobs.list_jobs()
        }
        owner_active = ip_active = global_active = 0
        for meta in self._all_meta():
            status = status_by_run.get(str(meta.get("run_id") or ""), "")
            if status not in _ACTIVE:
                continue
            global_active += 1
            owner_active += int(meta.get("owner_id") == owner_id)
            ip_active += int(meta.get("ip_key") == ip_key)
        return owner_active, ip_active, global_active

    def start(self, *, owner_id: int, session_key: str, ip: str, body: Mapping[str, Any]) -> dict[str, Any]:
        self._maybe_expire_artifacts()
        unknown = set(body) - {"engine", "problem"}
        if unknown:
            raise PublicAPIError(400, "Only engine and problem are accepted")
        engine = str(body.get("engine") or "").strip().lower()
        if engine not in {item["id"] for item in self.harnesses()}:
            raise PublicAPIError(400, "Unsupported public harness")
        problem = body.get("problem")
        if not isinstance(problem, str) or not problem.strip():
            raise PublicAPIError(400, "Problem must be nonempty text")
        problem = problem.strip()
        if len(problem) > self.limits.problem_characters:
            raise PublicAPIError(413, "Problem is too long")
        ip_key = _key_hash(ip)
        with self._start_lock:
            return self._start_admitted(
                owner_id=owner_id,
                session_key=session_key,
                ip_key=ip_key,
                engine=engine,
                problem=problem,
            )

    def _start_admitted(
        self,
        *,
        owner_id: int,
        session_key: str,
        ip_key: str,
        engine: str,
        problem: str,
    ) -> dict[str, Any]:
        """Atomically check capacity, start the job, and publish ownership."""
        owner_active, ip_active, global_active = self._active_counts(
            owner_id=owner_id, ip_key=ip_key
        )
        if owner_active >= self.limits.active_runs or ip_active >= self.limits.active_runs:
            raise PublicAPIError(429, "One public run is already active for this visitor", retry_after=5)
        if global_active >= self.limits.global_active_runs:
            raise PublicAPIError(429, "The public Kimi service is at capacity", retry_after=5)
        self.limiter.admit(session_key, ip_key)
        user = {"id": owner_id, "email": None, "is_admin": True}
        try:
            job = self.jobs.start_job(
                engine=engine,
                problem_text=problem,
                model=MODEL_ID,
                max_iterations=self.limits.max_iterations,
                user=user,
                use_subagents=False,
                subagent_model=None,
                auth_route="api_key",
            )
        except Exception as exc:  # noqa: BLE001
            raise PublicAPIError(400, self.redact(str(exc) or "Run could not start")) from None
        run_id = str(job.get("run_id") or "")
        if not _RUN_RE.fullmatch(run_id):
            raise PublicAPIError(500, "Run could not start")
        meta = {
            "run_id": run_id,
            "job_id": str(job.get("job_id") or ""),
            "owner_id": owner_id,
            "ip_key": ip_key,
            "engine": engine,
            "model": MODEL_ID,
            "created_epoch": time.time(),
        }
        try:
            _atomic_json(self._meta_path(run_id), meta)
        except OSError:
            try:
                self.jobs.stop_run(run_id)
            except Exception:  # noqa: BLE001
                pass
            raise PublicAPIError(500, "Run metadata could not be secured") from None
        return {"run_id": run_id, "status": "running", "engine": engine, "model": MODEL_ID}

    def redact(self, value: object) -> str:
        text = str(value or "")
        for secret in self.redactions:
            text = text.replace(secret, "[redacted]")
        return _SECRET_RE.sub("[redacted]", text)

    def _record(self, run_id: str) -> dict[str, Any]:
        from agent_monitor import CACHE_DIR, RUNS_DIR

        for path in (CACHE_DIR / "harness" / f"{run_id}.json", RUNS_DIR / f"{run_id}.json"):
            try:
                if path.stat().st_size > 16 * 1024 * 1024:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
        return {}

    def status(self, *, run_id: str, owner_id: int) -> dict[str, Any]:
        self._maybe_expire_artifacts()
        meta = self._load_meta(run_id, owner_id)
        try:
            self.jobs.reconcile_run_status(run_id)
        except Exception:  # noqa: BLE001
            pass
        record = self._record(run_id)
        status = str(record.get("status") or "queued").lower()
        if status == "interrupted":
            status = "failed"
            record["error"] = record.get("error") or "Run interrupted before completion"
        live = record.get("live") if isinstance(record.get("live"), dict) else {}
        actual_model = str((live or {}).get("model") or "").strip()
        if actual_model and actual_model.lower().rsplit("/", 1)[-1].rsplit(":", 1)[-1] != MODEL_ID:
            status = "failed"
            error = "Runtime model attestation failed"
        elif record.get("auth_route") not in {None, "", "api_key"}:
            status = "failed"
            error = "Runtime route attestation failed"
        else:
            error = self.redact(record.get("error"))
        proof = ""
        from agent_monitor import RUNS_DIR

        proof_path = RUNS_DIR / "workspaces" / run_id / "proof.md"
        try:
            if proof_path.is_file() and not proof_path.is_symlink():
                proof = self.redact(proof_path.read_text(encoding="utf-8", errors="replace")[:300_000])
        except OSError:
            proof = ""
        events: list[dict[str, Any]] = []
        for item in (record.get("agents") or [])[-60:]:
            if not isinstance(item, dict):
                continue
            output = self.redact(item.get("output"))[-4000:]
            stage = str(item.get("stage_name") or "")
            role = str(item.get("role") or "agent")
            events.append(
                {
                    "stage": stage,
                    "role": role,
                    "title": stage or role,
                    "status": str(item.get("status") or ""),
                    "output": output,
                    "text": output,
                }
            )
        return {
            "run_id": run_id,
            "status": status,
            "engine": str(meta["engine"]),
            "model": MODEL_ID,
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "error": error or None,
            "proof": proof,
            "events": events,
        }

    def stop(self, *, run_id: str, owner_id: int) -> dict[str, str]:
        self._load_meta(run_id, owner_id)
        try:
            result = self.jobs.stop_run(run_id)
        except Exception as exc:  # noqa: BLE001
            raise PublicAPIError(400, self.redact(str(exc) or "Run could not stop")) from None
        return {"run_id": run_id, "status": str(result.get("status") or "stopping")}

    def _expire_artifacts(self) -> None:
        cutoff = time.time() - self.limits.artifact_ttl_hours * 3600
        for path in self.metadata_dir.glob("*.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
                created = float(meta.get("created_epoch") or 0)
                run_id = str(meta.get("run_id") or "")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if created >= cutoff or not _RUN_RE.fullmatch(run_id):
                continue
            try:
                self.jobs.delete_run(run_id)
            except Exception:  # noqa: BLE001
                continue
            try:
                path.unlink()
            except OSError:
                pass

    def _maybe_expire_artifacts(self) -> None:
        now = time.monotonic()
        if now - self._last_expiry_check < 900:
            return
        with self._expiry_lock:
            now = time.monotonic()
            if now - self._last_expiry_check < 900:
                return
            self._expire_artifacts()
            self._last_expiry_check = now


def _normalize_base_path(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value or value == "/":
        return ""
    if not value.startswith("/"):
        value = "/" + value
    value = value.rstrip("/")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]+", value):
        raise RuntimeError("PUBLIC_KIMI_BASE_PATH contains unsupported characters")
    return value


class PublicKimiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        app: PublicKimiApplication,
        base_path: str,
        *,
        gateway_base_url: str = "",
        gateway_token: str = "",
    ) -> None:
        super().__init__(address, PublicKimiHandler)
        self.app = app
        self.base_path = base_path
        self.gateway_base_url = gateway_base_url.rstrip("/")
        self.gateway_token = gateway_token
        host, port = self.server_address
        self.base_url_for_sponsored = f"http://{host}:{port}/internal/v1"


class PublicKimiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def public_server(self) -> PublicKimiHTTPServer:
        return self.server  # type: ignore[return-value]

    def _path(self) -> str | None:
        path = urlparse(self.path).path
        base = self.public_server.base_path
        if base:
            if path != base and not path.startswith(base + "/"):
                return None
            path = path[len(base):] or "/"
        return path

    def _session(self) -> tuple[str, bool]:
        cookie = SimpleCookie()
        try:
            cookie.load(str(self.headers.get("Cookie") or ""))
            token = str(cookie.get(SESSION_COOKIE).value if cookie.get(SESSION_COOKIE) else "")
        except Exception:  # noqa: BLE001
            token = ""
        if _SESSION_RE.fullmatch(token):
            return token, False
        return secrets.token_hex(32), True

    def _headers(self, *, content_type: str, length: int, token: str, new: bool, retry: int = 0) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if self.close_connection:
            self.send_header("Connection", "close")
        if retry:
            self.send_header("Retry-After", str(retry))
        if new:
            path = self.public_server.base_path or "/"
            secure = "; Secure" if os.environ.get("PUBLIC_KIMI_COOKIE_SECURE") == "1" else ""
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={token}; Path={path}; Max-Age=86400; HttpOnly; SameSite=Strict{secure}",
            )

    def _json(self, status: int, payload: Mapping[str, Any], *, token: str, new: bool, retry: int = 0) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._headers(content_type="application/json; charset=utf-8", length=len(body), token=token, new=new, retry=retry)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: PublicAPIError, *, token: str, new: bool) -> None:
        self._json(exc.status, {"error": exc.message}, token=token, new=new, retry=exc.retry_after)

    def _same_origin(self) -> bool:
        origin = str(self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        return urlparse(origin).netloc.lower() == str(self.headers.get("Host") or "").lower()

    def _client_ip(self) -> str:
        """Trust the Caddy-overwritten client header only from loopback."""
        peer = str(self.client_address[0])
        try:
            loopback = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        forwarded = str(self.headers.get("X-Public-Client-IP") or "").strip()
        if loopback and forwarded and "," not in forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return peer

    def _read_json(self, limit: int) -> dict[str, Any]:
        media_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        if media_type != "application/json":
            raise PublicAPIError(415, "application/json is required")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 1 or length > limit:
            raise PublicAPIError(413 if length > limit else 400, "Invalid request size")
        deadline = time.monotonic() + _REQUEST_BODY_TIMEOUT_SECONDS
        previous_timeout = self.connection.gettimeout()
        body = bytearray()
        try:
            while len(body) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close_connection = True
                    raise PublicAPIError(408, "Request body timed out")
                self.connection.settimeout(remaining)
                try:
                    chunk = self.rfile.read1(
                        min(_REQUEST_BODY_CHUNK_BYTES, length - len(body))
                    )
                except socket.timeout:
                    self.close_connection = True
                    raise PublicAPIError(408, "Request body timed out") from None
                if not chunk:
                    self.close_connection = True
                    raise PublicAPIError(400, "Incomplete request body")
                body.extend(chunk)
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                self.close_connection = True
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PublicAPIError(400, "Request body must be valid JSON") from None
        if not isinstance(value, dict):
            raise PublicAPIError(400, "Request body must be an object")
        return value

    def _loopback_peer(self) -> bool:
        try:
            return ipaddress.ip_address(str(self.client_address[0])).is_loopback
        except ValueError:
            return False

    def _bearer_token(self) -> str:
        authorization = str(self.headers.get("Authorization") or "").strip()
        prefix = "Bearer "
        return authorization[len(prefix):].strip() if authorization.startswith(prefix) else ""

    def _internal_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        retry: int = 0,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if retry:
            self.send_header("Retry-After", str(retry))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _internal_error(self, exc: PublicAPIError) -> None:
        self._internal_json(
            exc.status,
            {"error": {"message": exc.message}},
            retry=exc.retry_after,
        )

    def _forward_sponsored_request(self, upstream_path: str) -> None:
        app = self.public_server.app
        engine = app.sponsor_sessions.authorize(self._bearer_token(), charge=True)
        if upstream_path == "/responses" and engine not in {"codex", "formal"}:
            raise PublicAPIError(403, "Responses transport requires a Codex or Formal token")
        payload = self._read_json(app.limits.sponsor_request_bytes)
        if not self.public_server.gateway_base_url or not self.public_server.gateway_token:
            raise PublicAPIError(503, "Sponsored Kimi gateway unavailable")
        request = urlrequest.Request(
            self.public_server.gateway_base_url + upstream_path,
            data=json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.public_server.gateway_token,
                "Content-Type": "application/json",
                "Accept": str(self.headers.get("Accept") or "application/json"),
            },
            method="POST",
        )
        try:
            upstream = urlrequest.urlopen(
                request,
                timeout=app.limits.upstream_timeout_seconds + 5,
            )
        except urlerror.HTTPError as exc:
            upstream = exc
        except (OSError, urlerror.URLError):
            raise PublicAPIError(502, "Sponsored Kimi gateway unavailable") from None
        with upstream:
            status = int(getattr(upstream, "status", upstream.code))
            content_type = str(upstream.headers.get("Content-Type") or "application/json")
            streaming = "text/event-stream" in content_type.lower()
            if streaming:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.close_connection = True
                read = getattr(upstream, "read1", None)
                if not callable(read):
                    read = upstream.read
                total = 0
                while True:
                    chunk = read(16 * 1024)
                    if not chunk:
                        return
                    total += len(chunk)
                    if total > 16 * 1024 * 1024:
                        return
                    self.wfile.write(chunk)
                    self.wfile.flush()
            body = upstream.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise PublicAPIError(502, "Sponsored Kimi response too large")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            retry_after = str(upstream.headers.get("Retry-After") or "").strip()
            if retry_after.isdigit() and len(retry_after) <= 6:
                self.send_header("Retry-After", retry_after)
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        if path == "/internal/v1/models":
            if not self._loopback_peer():
                self._internal_json(404, {"error": {"message": "Not found"}})
                return
            try:
                self.public_server.app.sponsor_sessions.authorize(
                    self._bearer_token(), charge=False
                )
                self._internal_json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {"id": MODEL_ID, "object": "model", "owned_by": "kimi"}
                        ],
                    },
                )
            except PublicAPIError as exc:
                self._internal_error(exc)
            return
        token, new = self._session()
        if path == "/healthz":
            self._json(
                200,
                {"ok": True, "service": "public-kimi"},
                token=token,
                new=new,
            )
            return
        if path == "/":
            page = ROOT / "agent_monitor" / "web" / "public_kimi.html"
            try:
                body_text = page.read_text(encoding="utf-8")
            except OSError:
                self._json(503, {"error": "Public UI unavailable"}, token=token, new=new)
                return
            base = html.escape(self.public_server.base_path, quote=True)
            body_text = re.sub(
                r'(<meta\s+name=["\']pk-base["\']\s+content=["\'])[^"\']*(["\'])',
                rf"\g<1>{base}\g<2>",
                body_text,
                count=1,
                flags=re.IGNORECASE,
            )
            body = body_text.encode("utf-8")
            self.send_response(200)
            self._headers(content_type="text/html; charset=utf-8", length=len(body), token=token, new=new)
            self.end_headers()
            self.wfile.write(body)
            return
        static_files = {
            "/static/public_kimi.css": ("public_kimi.css", "text/css; charset=utf-8"),
            "/static/public_kimi.js": ("public_kimi.js", "application/javascript; charset=utf-8"),
        }
        if path in static_files:
            filename, content_type = static_files[path]
            try:
                body = (ROOT / "agent_monitor" / "web" / filename).read_bytes()
            except OSError:
                self._json(404, {"error": "Not found"}, token=token, new=new)
                return
            self.send_response(200)
            self._headers(content_type=content_type, length=len(body), token=token, new=new)
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/config":
            self._json(200, self.public_server.app.config(), token=token, new=new)
            return
        prefix = "/api/runs/"
        if path and path.startswith(prefix) and "/" not in path[len(prefix):]:
            try:
                result = self.public_server.app.status(
                    run_id=path[len(prefix):], owner_id=_owner_id(token)
                )
                self._json(200, result, token=token, new=new)
            except PublicAPIError as exc:
                self._error(exc, token=token, new=new)
            return
        self._json(404, {"error": "Not found"}, token=token, new=new)

    def do_POST(self) -> None:  # noqa: N802
        path = self._path()
        if path in {
            "/internal/sessions",
            "/internal/v1/chat/completions",
            "/internal/v1/responses",
        }:
            if not self._loopback_peer():
                self._internal_json(404, {"error": {"message": "Not found"}})
                return
            try:
                if path == "/internal/sessions":
                    body = self._read_json(1024)
                    if set(body) != {"engine"}:
                        raise PublicAPIError(400, "Only engine is accepted")
                    engine = str(body.get("engine") or "").strip().lower()
                    issued, ttl = self.public_server.app.sponsor_sessions.issue(
                        engine=engine,
                        ip=self._client_ip(),
                    )
                    self._internal_json(
                        201,
                        {
                            "api_token": issued,
                            "base_url": self.public_server.base_url_for_sponsored,
                            "model": MODEL_ID,
                            "expires_in": ttl,
                            "engine": engine,
                        },
                    )
                else:
                    self._forward_sponsored_request(
                        "/responses"
                        if path == "/internal/v1/responses"
                        else "/chat/completions"
                    )
            except PublicAPIError as exc:
                self._internal_error(exc)
            return
        token, new = self._session()
        if not self._same_origin():
            self._json(403, {"error": "Cross-origin requests are not allowed"}, token=token, new=new)
            return
        try:
            if path == "/api/runs":
                body = self._read_json(self.public_server.app.limits.request_bytes)
                result = self.public_server.app.start(
                    owner_id=_owner_id(token),
                    session_key=_key_hash(token),
                    ip=self._client_ip(),
                    body=body,
                )
                self._json(202, result, token=token, new=new)
                return
            prefix = "/api/runs/"
            suffix = "/stop"
            if path and path.startswith(prefix) and path.endswith(suffix):
                run_id = path[len(prefix):-len(suffix)]
                if not run_id or "/" in run_id:
                    raise PublicAPIError(404, "Run not found")
                if int(self.headers.get("Content-Length") or "0"):
                    body = self._read_json(1024)
                    if body:
                        raise PublicAPIError(400, "Stop request body must be empty")
                result = self.public_server.app.stop(run_id=run_id, owner_id=_owner_id(token))
                self._json(200, result, token=token, new=new)
                return
            raise PublicAPIError(404, "Not found")
        except PublicAPIError as exc:
            self._error(exc, token=token, new=new)

    def do_OPTIONS(self) -> None:  # noqa: N802
        token, new = self._session()
        self._json(405, {"error": "Method not allowed"}, token=token, new=new)

    do_PUT = do_OPTIONS
    do_PATCH = do_OPTIONS
    do_DELETE = do_OPTIONS


def serve() -> None:
    configured_data = str(os.environ.get("PUBLIC_KIMI_DATA_DIR") or "").strip()
    agent_data = str(os.environ.get("AGENT_MONITOR_DATA_DIR") or "").strip()
    if not configured_data or Path(configured_data).resolve() != DATA_DIR or Path(agent_data).resolve() != DATA_DIR:
        raise RuntimeError(
            "Set PUBLIC_KIMI_DATA_DIR and AGENT_MONITOR_DATA_DIR to the same isolated directory"
        )
    if DATA_DIR == (ROOT / "data").resolve():
        raise RuntimeError("The public Kimi service refuses the private/default data directory")
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA_DIR.chmod(0o700)
    upstream_key, upstream_base = _upstream_credentials()
    local_token = secrets.token_urlsafe(48)
    limits = PublicLimits()
    gateway = PublicKimiGateway(
        upstream_base=upstream_base,
        upstream_api_key=upstream_key,
        local_token=local_token,
        reasoning_effort="high",
        max_output_tokens=limits.max_output_tokens,
        max_concurrency=limits.gateway_concurrent_calls,
        calls_per_hour=limits.gateway_calls_per_hour,
        # api_chat gives its HTTP client 240 seconds.  Return a bounded gateway
        # failure first instead of leaving an upstream request running after
        # the worker has already abandoned it.
        upstream_timeout=limits.upstream_timeout_seconds,
    )
    with gateway:
        _install_worker_environment(DATA_DIR, token=local_token, base_url=gateway.base_url)
        # Import only after the real key and systemd credential directory have
        # been removed from the process environment inherited by workers.
        from agent_monitor import jobs
        from agent_monitor.paths import ensure_data_dirs

        ensure_data_dirs()
        jobs.reconcile_stale_running_runs(min_age_seconds=0)
        app = PublicKimiApplication(
            jobs=jobs,
            data_dir=DATA_DIR,
            redactions=(upstream_key, upstream_base, local_token, gateway.base_url),
            limits=limits,
        )
        host = str(os.environ.get("PUBLIC_KIMI_HOST") or "127.0.0.1").strip()
        port = int(os.environ.get("PUBLIC_KIMI_PORT") or 4610)
        base_path = _normalize_base_path(os.environ.get("PUBLIC_KIMI_BASE_PATH"))
        server = PublicKimiHTTPServer(
            (host, port),
            app,
            base_path,
            gateway_base_url=gateway.base_url,
            gateway_token=local_token,
        )
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            server.server_close()


if __name__ == "__main__":
    serve()
