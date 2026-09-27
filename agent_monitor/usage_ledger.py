"""Indexed run and usage history for the admin console.

Run JSON files remain the source of truth for traces. This module keeps a
small SQLite index beside ``users.db`` so admin views can filter and aggregate
without reading every run record, and so usage survives run deletion.

Two write paths feed the ledger:

* ``record_run`` is called for every run-record write. Run ``totals`` are
  cumulative (continuations merge earlier totals in), but live heartbeats of a
  continuation can briefly carry only the new job's totals. The ledger keeps a
  per-run high-water mark and books only increases, attributed to the UTC day
  they were observed, so repeated writes never double count.
* ``record_call`` books one model call made outside the engine run (proof DAG,
  Lean, ...). Each call carries a unique key, so retries of the booking itself
  are idempotent.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator

from agent_monitor import CACHE_DIR, DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "usage.db"
METRICS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "cost_usd", "latency_s")
TERMINAL = {"finished", "failed", "stopped", "cancelled", "interrupted"}
FAILED = {"failed", "interrupted"}

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()
_OWNER_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _day(ts: str | None = None) -> str:
    value = str(ts or "")
    return value[:10] if len(value) >= 10 and value[4] == "-" else _now()[:10]


def _num(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


@contextlib.contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Open the ledger, commit on success, and always close the handle."""
    conn = _open()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _open() -> sqlite3.Connection:
    path = DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # Usage rows include account emails; never inherit a permissive umask.
    path.touch(mode=0o600, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    key = str(path)
    if key not in _INITIALIZED:
        with _INIT_LOCK:
            if key not in _INITIALIZED:
                path.chmod(0o600)
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runs_index (
                        run_id TEXT PRIMARY KEY,
                        owner_id INTEGER,
                        owner_email TEXT,
                        owner_name TEXT,
                        guest INTEGER NOT NULL DEFAULT 0,
                        engine TEXT,
                        model TEXT,
                        billing TEXT,
                        status TEXT,
                        problem_preview TEXT,
                        agent_count INTEGER NOT NULL DEFAULT 0,
                        continuations INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT,
                        updated_at TEXT,
                        finished_at TEXT,
                        error TEXT,
                        deleted_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs_index(owner_id);
                    CREATE INDEX IF NOT EXISTS idx_runs_created ON runs_index(created_at);
                    CREATE INDEX IF NOT EXISTS idx_runs_engine ON runs_index(engine);
                    CREATE INDEX IF NOT EXISTS idx_runs_status ON runs_index(status);

                    -- High-water mark of cumulative run totals already booked.
                    CREATE TABLE IF NOT EXISTS usage_marks (
                        run_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        input_tokens REAL NOT NULL DEFAULT 0,
                        output_tokens REAL NOT NULL DEFAULT 0,
                        cached_input_tokens REAL NOT NULL DEFAULT 0,
                        reasoning_tokens REAL NOT NULL DEFAULT 0,
                        cost_usd REAL NOT NULL DEFAULT 0,
                        latency_s REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY (run_id, stage)
                    );

                    -- Daily usage per run and stage; never deleted with runs.
                    CREATE TABLE IF NOT EXISTS usage_daily (
                        day TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        model TEXT NOT NULL DEFAULT '',
                        owner_id INTEGER,
                        guest INTEGER NOT NULL DEFAULT 0,
                        engine TEXT,
                        billing TEXT,
                        calls INTEGER NOT NULL DEFAULT 0,
                        input_tokens REAL NOT NULL DEFAULT 0,
                        output_tokens REAL NOT NULL DEFAULT 0,
                        cached_input_tokens REAL NOT NULL DEFAULT 0,
                        reasoning_tokens REAL NOT NULL DEFAULT 0,
                        cost_usd REAL NOT NULL DEFAULT 0,
                        latency_s REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY (day, run_id, stage, model)
                    );
                    CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_daily(day);
                    CREATE INDEX IF NOT EXISTS idx_usage_owner ON usage_daily(owner_id, day);
                    CREATE INDEX IF NOT EXISTS idx_usage_engine ON usage_daily(engine, day);

                    CREATE TABLE IF NOT EXISTS usage_calls (
                        call_key TEXT PRIMARY KEY,
                        run_id TEXT,
                        stage TEXT,
                        recorded_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT);
                    """
                )
                _INITIALIZED.add(key)
    return conn


def billing_class(run: dict[str, Any]) -> str:
    """Classify who pays for a run's tokens."""
    if str(run.get("credential_source") or "").strip() == "sponsored_kimi":
        return "sponsored"
    route = str(run.get("auth_route") or "").strip().lower()
    if not route:
        route = str((run.get("live") or {}).get("auth_mode") or "").strip().lower()
    if route in {"codex_subscription", "chatgpt_subscription", "claude_subscription"}:
        return "subscription"
    if route == "api_key":
        return "api"
    return "unknown"


def _run_model(run: dict[str, Any]) -> str:
    live = run.get("live") or {}
    return str(run.get("model") or live.get("model") or run.get("requested_model") or "").strip()[:200]


def _owner_info(owner_id: Any, run: dict[str, Any]) -> dict[str, Any]:
    """Snapshot owner identity so usage stays attributable after guest purge."""
    info = {"email": None, "name": None, "guest": bool(run.get("guest"))}
    try:
        uid = int(owner_id)
    except (TypeError, ValueError):
        info["email"] = str(run.get("owner") or "") or None
        return info
    cached = _OWNER_CACHE.get(uid)
    if cached and time.time() - cached[0] < 300:
        return {**cached[1], "guest": cached[1]["guest"] or info["guest"]}
    try:
        from agent_monitor import auth

        with contextlib.closing(auth._conn()) as conn:  # noqa: SLF001 - shared users.db helper
            row = conn.execute(
                "SELECT email, name, is_guest FROM users WHERE id=?", (uid,)
            ).fetchone()
        if row is not None:
            info = {"email": row["email"], "name": row["name"], "guest": bool(row["is_guest"]) or info["guest"]}
            _OWNER_CACHE[uid] = (time.time(), info)
    except Exception:  # noqa: BLE001 - identity is best-effort metadata
        pass
    if not info["email"]:
        info["email"] = str(run.get("owner") or "") or None
    return info


def _book(
    conn: sqlite3.Connection,
    *,
    day: str,
    run_id: str,
    stage: str,
    model: str,
    owner_id: Any,
    guest: bool,
    engine: str | None,
    billing: str,
    calls: int,
    delta: dict[str, float],
) -> None:
    if not calls and not any(delta.get(m) for m in METRICS):
        return
    conn.execute(
        f"""
        INSERT INTO usage_daily (day, run_id, stage, model, owner_id, guest, engine, billing, calls, {", ".join(METRICS)})
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {", ".join("?" for _ in METRICS)})
        ON CONFLICT(day, run_id, stage, model) DO UPDATE SET
            owner_id=excluded.owner_id, guest=excluded.guest, engine=excluded.engine,
            billing=excluded.billing, calls=calls+excluded.calls,
            {", ".join(f"{m}={m}+excluded.{m}" for m in METRICS)}
        """,
        (day, run_id, stage, model or "", owner_id, int(guest), engine, billing, calls,
         *(delta.get(m, 0.0) for m in METRICS)),
    )


def _book_run(conn: sqlite3.Connection, run: dict[str, Any], *, now: str) -> None:
    run_id = str(run.get("run_id") or "")
    owner_id = run.get("owner_id")
    owner = _owner_info(owner_id, run)
    guest = bool(owner["guest"])
    status = str(run.get("status") or "unknown")
    existing = conn.execute(
        "SELECT status, finished_at FROM runs_index WHERE run_id=?", (run_id,)
    ).fetchone()
    finished_at = existing["finished_at"] if existing else None
    if status in TERMINAL:
        if not finished_at or (existing and existing["status"] not in TERMINAL):
            finished_at = str(run.get("updated_at") or now)
    else:
        finished_at = None
    preview = str(run.get("problem_text_preview") or "").strip()
    if not preview:
        for line in str(run.get("problem_text") or "").splitlines():
            if line.strip():
                preview = line.strip()
                break
    conn.execute(
        """
        INSERT INTO runs_index (run_id, owner_id, owner_email, owner_name, guest, engine, model, billing,
            status, problem_preview, agent_count, continuations, created_at, updated_at, finished_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            owner_id=excluded.owner_id,
            owner_email=COALESCE(excluded.owner_email, runs_index.owner_email),
            owner_name=COALESCE(excluded.owner_name, runs_index.owner_name),
            guest=excluded.guest, engine=excluded.engine,
            model=CASE WHEN excluded.model != '' THEN excluded.model ELSE runs_index.model END,
            billing=CASE WHEN excluded.billing != 'unknown' THEN excluded.billing ELSE runs_index.billing END,
            status=excluded.status,
            problem_preview=CASE WHEN excluded.problem_preview != '' THEN excluded.problem_preview
                                 ELSE runs_index.problem_preview END,
            agent_count=MAX(excluded.agent_count, runs_index.agent_count),
            continuations=MAX(excluded.continuations, runs_index.continuations),
            created_at=COALESCE(runs_index.created_at, excluded.created_at),
            updated_at=excluded.updated_at, finished_at=excluded.finished_at, error=excluded.error
        """,
        (
            run_id, owner_id, owner["email"], owner["name"], int(guest), run.get("engine"),
            _run_model(run), billing_class(run), status, preview[:300],
            len(run.get("agents") or []), int(run.get("continuation_count") or 0),
            run.get("created_at") or now, run.get("updated_at") or now, finished_at,
            str(run.get("error") or "")[:500] or None,
        ),
    )

    totals = run.get("totals") or {}
    current = {m: _num(totals.get(m)) for m in METRICS}
    mark_row = conn.execute(
        "SELECT * FROM usage_marks WHERE run_id=? AND stage='engine'", (run_id,)
    ).fetchone()
    mark = {m: float(mark_row[m]) for m in METRICS} if mark_row else {m: 0.0 for m in METRICS}
    delta = {m: max(0.0, current[m] - mark[m]) for m in METRICS}
    if not any(delta.values()):
        return
    conn.execute(
        f"""
        INSERT INTO usage_marks (run_id, stage, {", ".join(METRICS)})
        VALUES (?, 'engine', {", ".join("?" for _ in METRICS)})
        ON CONFLICT(run_id, stage) DO UPDATE SET {", ".join(f"{m}=excluded.{m}" for m in METRICS)}
        """,
        (run_id, *(max(current[m], mark[m]) for m in METRICS)),
    )
    _book(
        conn, day=_day(now), run_id=run_id, stage="engine", model=_run_model(run),
        owner_id=owner_id, guest=guest, engine=run.get("engine"), billing=billing_class(run),
        calls=0, delta=delta,
    )


def record_run(run: dict[str, Any]) -> None:
    """Index one run-record write. Never raises into the run pipeline."""
    if not isinstance(run, dict) or not run.get("run_id"):
        return
    try:
        with _connect() as conn:
            _book_run(conn, run, now=_now())
    except Exception:  # noqa: BLE001 - usage indexing must not break runs
        logger.exception("usage ledger: failed to record run %s", run.get("run_id"))


def record_call(
    *,
    run_id: str,
    stage: str,
    call_key: str,
    model: str | None,
    usage: dict[str, Any] | None,
    billing: str | None = None,
    latency_s: float | None = None,
) -> None:
    """Book one model call made outside the engine run (idempotent per key)."""
    if not run_id or not call_key:
        return
    usage = usage or {}
    delta = {m: _num(usage.get(m)) for m in METRICS}
    if latency_s is not None:
        delta["latency_s"] = _num(latency_s)
    try:
        with _connect() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO usage_calls (call_key, run_id, stage, recorded_at) VALUES (?, ?, ?, ?)",
                (call_key, run_id, stage, _now()),
            ).rowcount
            if not inserted:
                return
            row = conn.execute(
                "SELECT owner_id, guest, engine, billing FROM runs_index WHERE run_id=?", (run_id,)
            ).fetchone()
            _book(
                conn, day=_day(), run_id=run_id, stage=stage, model=str(model or "")[:200],
                owner_id=row["owner_id"] if row else None, guest=bool(row["guest"]) if row else False,
                engine=row["engine"] if row else None,
                billing=billing or (row["billing"] if row else "unknown"), calls=1, delta=delta,
            )
    except Exception:  # noqa: BLE001
        logger.exception("usage ledger: failed to record %s call for %s", stage, run_id)


# ── model calls outside the engine run (DAG, Lean, ...) ─────────────────────

_CALL_CONTEXT: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "usage_call_context", default=None
)


def attributed(stage: str):
    """Attribute model calls made inside a run-scoped entry point to ``stage``.

    The entry point must take ``run_record`` and/or ``workspace`` keywords;
    the run id falls back to the workspace directory name.
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            record = kwargs.get("run_record") or {}
            run_id = str(record.get("run_id") or "") if isinstance(record, dict) else ""
            if not run_id and kwargs.get("workspace") is not None:
                run_id = Path(str(kwargs["workspace"])).name
            if not run_id:
                return fn(*args, **kwargs)
            token = _CALL_CONTEXT.set((run_id, stage))
            try:
                return fn(*args, **kwargs)
            finally:
                _CALL_CONTEXT.reset(token)
        return wrapper
    return decorate


def http_usage(provider: str, body: Any) -> dict[str, float]:
    """Normalize provider response usage into ledger metric names."""
    if not isinstance(body, dict):
        return {}
    if provider == "google":
        meta = body.get("usageMetadata") or {}
        return {
            "input_tokens": _num(meta.get("promptTokenCount")),
            "output_tokens": _num(meta.get("candidatesTokenCount")) + _num(meta.get("thoughtsTokenCount")),
            "cached_input_tokens": _num(meta.get("cachedContentTokenCount")),
            "reasoning_tokens": _num(meta.get("thoughtsTokenCount")),
        }
    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    if provider == "anthropic":
        return {
            "input_tokens": _num(usage.get("input_tokens")) + _num(usage.get("cache_read_input_tokens"))
            + _num(usage.get("cache_creation_input_tokens")),
            "output_tokens": _num(usage.get("output_tokens")),
            "cached_input_tokens": _num(usage.get("cache_read_input_tokens")),
        }
    in_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": _num(usage.get("input_tokens") or usage.get("prompt_tokens")),
        "output_tokens": _num(usage.get("output_tokens") or usage.get("completion_tokens")),
        "cached_input_tokens": _num((in_details or {}).get("cached_tokens") if isinstance(in_details, dict) else 0),
        "reasoning_tokens": _num((out_details or {}).get("reasoning_tokens") if isinstance(out_details, dict) else 0),
    }


def cli_usage(usage: dict[str, Any] | None, *, input_includes_cache: bool) -> dict[str, float]:
    """Normalize CLIEventParser / Claude / Codex backend usage dicts.

    Codex reports ``input_tokens`` including cached input; Claude reports
    cache reads and writes separately from ``input_tokens``.
    """
    usage = usage or {}
    cache_read = _num(usage.get("cache_read_tokens") or usage.get("cached_input_tokens"))
    cache_write = _num(usage.get("cache_write_tokens"))
    extra = 0.0 if input_includes_cache else cache_read + cache_write
    return {
        "input_tokens": _num(usage.get("input_tokens")) + extra,
        "output_tokens": _num(usage.get("output_tokens")),
        "cached_input_tokens": cache_read,
        "reasoning_tokens": _num(usage.get("reasoning_tokens")),
    }


def record_usage(
    *,
    model: str | None,
    usage: dict[str, float],
    billing: str | None = None,
    cost_usd: float | None = None,
    stage: str | None = None,
    run_id: str | None = None,
) -> None:
    """Book one out-of-run model call against the current attributed run."""
    context = _CALL_CONTEXT.get()
    run_id = run_id or (context[0] if context else None)
    stage = stage or (context[1] if context else None)
    if not run_id or not stage or not any(usage.values()):
        return
    usage = dict(usage)
    if cost_usd is None:
        try:
            from agent_monitor.jobs import _estimate_cost

            cached = int(usage.get("cached_input_tokens") or 0)
            cost_usd = _estimate_cost(
                model,
                input_tokens=max(0, int(usage.get("input_tokens") or 0) - cached),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read=cached,
            )
        except Exception:  # noqa: BLE001
            cost_usd = None
    usage["cost_usd"] = cost_usd or 0.0
    record_call(
        run_id=run_id, stage=stage, call_key=uuid.uuid4().hex,
        model=model, usage=usage, billing=billing,
    )


def mark_deleted(run_id: str) -> None:
    """Hide a deleted run's trace link while keeping its usage history."""
    try:
        with _connect() as conn:
            conn.execute("UPDATE runs_index SET deleted_at=? WHERE run_id=?", (_now(), run_id))
    except Exception:  # noqa: BLE001
        logger.exception("usage ledger: failed to mark %s deleted", run_id)


def backfill(cache_dir: Path | None = None, *, force: bool = False) -> dict[str, int]:
    """Import existing run JSON once, attributing usage to each job's day."""
    cache = cache_dir or (CACHE_DIR / "harness")
    with _connect() as conn:
        if not force and conn.execute("SELECT 1 FROM ledger_meta WHERE key='backfilled_at'").fetchone():
            return {"imported": 0, "skipped": 0}
    imported = skipped = 0
    for path in sorted(cache.glob("*.json")):
        if path.name == "manifest.json":
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue
        if not isinstance(run, dict) or not run.get("run_id"):
            skipped += 1
            continue
        try:
            with _connect() as conn:
                if conn.execute(
                    "SELECT 1 FROM usage_marks WHERE run_id=? AND stage='engine'", (run["run_id"],)
                ).fetchone():
                    skipped += 1
                    continue
                _backfill_run(conn, run)
            imported += 1
        except Exception:  # noqa: BLE001
            logger.exception("usage ledger: backfill failed for %s", path.name)
            skipped += 1
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ledger_meta (key, value) VALUES ('backfilled_at', ?)", (_now(),)
        )
    return {"imported": imported, "skipped": skipped}


def _backfill_run(conn: sqlite3.Connection, run: dict[str, Any]) -> None:
    # Book each continuation job on the day it ran when lineage allows it;
    # the final _book_run call then books only what lineage did not cover.
    lineage = [item for item in run.get("session_lineage") or [] if isinstance(item, dict)]
    run_id = str(run["run_id"])
    booked = {m: 0.0 for m in METRICS}
    if lineage:
        owner = _owner_info(run.get("owner_id"), run)
        for item in lineage:
            totals = item.get("totals") or {}
            delta = {m: _num(totals.get(m)) for m in METRICS}
            _book(
                conn, day=_day(item.get("updated_at") or run.get("created_at")), run_id=run_id,
                stage="engine", model=_run_model(run), owner_id=run.get("owner_id"),
                guest=bool(owner["guest"]), engine=run.get("engine"), billing=billing_class(run),
                calls=0, delta=delta,
            )
            for m in METRICS:
                booked[m] += delta[m]
        conn.execute(
            f"INSERT OR REPLACE INTO usage_marks (run_id, stage, {', '.join(METRICS)}) "
            f"VALUES (?, 'engine', {', '.join('?' for _ in METRICS)})",
            (run_id, *(booked[m] for m in METRICS)),
        )
    _book_run(conn, run, now=str(run.get("updated_at") or run.get("created_at") or _now()))


# ── admin queries ───────────────────────────────────────────────────────────

def _usage_filters(params: dict[str, str]) -> tuple[str, list[Any]]:
    clauses, args = ["1=1"], []
    if params.get("from"):
        clauses.append("u.day >= ?")
        args.append(params["from"][:10])
    if params.get("to"):
        clauses.append("u.day <= ?")
        args.append(params["to"][:10])
    if params.get("user"):
        clauses.append("u.owner_id = ?")
        args.append(int(params["user"]))
    if params.get("engine"):
        clauses.append("u.engine = ?")
        args.append(params["engine"])
    if params.get("model"):
        clauses.append("u.model = ?")
        args.append(params["model"])
    if params.get("billing"):
        clauses.append("u.billing = ?")
        args.append(params["billing"])
    guests = params.get("guests") or "include"
    if guests == "exclude":
        clauses.append("u.guest = 0")
    elif guests == "only":
        clauses.append("u.guest = 1")
    return " AND ".join(clauses), args


def _run_filters(params: dict[str, str], alias: str = "r") -> tuple[str, list[Any]]:
    clauses, args = ["1=1"], []
    if params.get("from"):
        clauses.append(f"substr({alias}.created_at, 1, 10) >= ?")
        args.append(params["from"][:10])
    if params.get("to"):
        clauses.append(f"substr({alias}.created_at, 1, 10) <= ?")
        args.append(params["to"][:10])
    if params.get("user"):
        clauses.append(f"{alias}.owner_id = ?")
        args.append(int(params["user"]))
    for key in ("engine", "model", "billing", "status"):
        if params.get(key):
            clauses.append(f"{alias}.{key} = ?")
            args.append(params[key])
    guests = params.get("guests") or "include"
    if guests == "exclude":
        clauses.append(f"{alias}.guest = 0")
    elif guests == "only":
        clauses.append(f"{alias}.guest = 1")
    if params.get("q"):
        like = f"%{params['q'].strip()}%"
        clauses.append(
            f"({alias}.run_id LIKE ? OR {alias}.problem_preview LIKE ? OR {alias}.owner_email LIKE ? "
            f"OR {alias}.owner_name LIKE ? OR {alias}.model LIKE ?)"
        )
        args.extend([like] * 5)
    if params.get("include_deleted") not in {"1", "true", "yes"}:
        clauses.append(f"{alias}.deleted_at IS NULL")
    return " AND ".join(clauses), args


_SUMS = ", ".join(f"SUM(u.{m}) AS {m}" for m in METRICS) + ", SUM(u.calls) AS calls"


def _rows(cursor: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    out = []
    for row in cursor:
        item = dict(row)
        for m in METRICS:
            if m in item:
                item[m] = round(float(item[m] or 0), 6 if m == "cost_usd" else 3)
        out.append(item)
    return out


def usage_summary(params: dict[str, str]) -> dict[str, Any]:
    """Aggregate usage and run outcomes for the admin overview."""
    where, args = _usage_filters(params)
    run_params = {k: v for k, v in params.items() if k not in {"q", "status"}}
    run_params["include_deleted"] = "1"  # deleted runs still count as usage
    run_where, run_args = _run_filters(run_params)
    base = f"FROM usage_daily u WHERE {where}"
    with _connect() as conn:
        totals = dict(conn.execute(f"SELECT {_SUMS} {base}", args).fetchone())
        by_billing = _rows(conn.execute(f"SELECT u.billing AS key, {_SUMS} {base} GROUP BY u.billing ORDER BY cost_usd DESC", args))
        by_day = _rows(conn.execute(f"SELECT u.day AS key, {_SUMS} {base} GROUP BY u.day ORDER BY u.day", args))
        by_engine = _rows(conn.execute(f"SELECT COALESCE(u.engine, 'unknown') AS key, {_SUMS} {base} GROUP BY key ORDER BY cost_usd DESC", args))
        by_model = _rows(conn.execute(f"SELECT CASE WHEN u.model = '' THEN 'unknown' ELSE u.model END AS key, {_SUMS} {base} GROUP BY key ORDER BY cost_usd DESC", args))
        by_stage = _rows(conn.execute(f"SELECT u.stage AS key, {_SUMS} {base} GROUP BY u.stage ORDER BY cost_usd DESC", args))
        by_user = _rows(conn.execute(
            f"""SELECT u.owner_id AS key, MAX(u.guest) AS guest, {_SUMS} {base}
                GROUP BY u.owner_id ORDER BY cost_usd DESC""", args))
        run_stats = {
            "runs": 0, "failed": 0, "active": 0, "deleted": 0,
            **dict(conn.execute(
                f"""SELECT COUNT(*) AS runs,
                        SUM(CASE WHEN r.status IN ('failed','interrupted') THEN 1 ELSE 0 END) AS failed,
                        SUM(CASE WHEN r.status NOT IN ('finished','failed','stopped','cancelled','interrupted') THEN 1 ELSE 0 END) AS active,
                        SUM(CASE WHEN r.deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted
                    FROM runs_index r WHERE {run_where}""", run_args).fetchone()),
        }
        run_counts = {
            (row["owner_id"], row["engine"]): (row["runs"], row["failed"])
            for row in conn.execute(
                f"""SELECT r.owner_id, r.engine, COUNT(*) AS runs,
                        SUM(CASE WHEN r.status IN ('failed','interrupted') THEN 1 ELSE 0 END) AS failed
                    FROM runs_index r WHERE {run_where} GROUP BY r.owner_id, r.engine""", run_args)
        }
        runs_by_day = {
            row["day"]: (row["runs"], row["failed"])
            for row in conn.execute(
                f"""SELECT substr(r.created_at, 1, 10) AS day, COUNT(*) AS runs,
                        SUM(CASE WHEN r.status IN ('failed','interrupted') THEN 1 ELSE 0 END) AS failed
                    FROM runs_index r WHERE {run_where} GROUP BY day""", run_args)
        }
        people = {
            row["owner_id"]: dict(row)
            for row in conn.execute(
                """SELECT owner_id, MAX(owner_email) AS email, MAX(owner_name) AS name, MAX(guest) AS guest,
                          MAX(updated_at) AS last_active
                   FROM runs_index GROUP BY owner_id""")
        }
        options = {
            "engines": [r[0] for r in conn.execute("SELECT DISTINCT engine FROM runs_index WHERE engine IS NOT NULL ORDER BY engine")],
            "models": [r[0] for r in conn.execute("SELECT DISTINCT model FROM usage_daily WHERE model != '' ORDER BY model")],
            "statuses": [r[0] for r in conn.execute("SELECT DISTINCT status FROM runs_index WHERE status IS NOT NULL ORDER BY status")],
        }
        backfilled = conn.execute("SELECT value FROM ledger_meta WHERE key='backfilled_at'").fetchone()

    def attach_runs(rows: list[dict[str, Any]], match) -> None:
        for row in rows:
            pairs = [value for key, value in run_counts.items() if match(key, row["key"])]
            row["runs"] = sum(p[0] for p in pairs)
            row["failed"] = sum(p[1] or 0 for p in pairs)

    attach_runs(by_user, lambda key, value: key[0] == value)
    attach_runs(by_engine, lambda key, value: (key[1] or "unknown") == value)
    for row in by_user:
        person = people.get(row["key"]) or {}
        row["email"] = person.get("email")
        row["name"] = person.get("name")
        row["guest"] = bool(row.get("guest") or person.get("guest"))
        row["last_active"] = person.get("last_active")
    # Users with runs but no recorded tokens still belong in the table.
    listed = {row["key"] for row in by_user}
    for (owner_id, _engine), (runs, failed) in run_counts.items():
        if owner_id in listed:
            continue
        listed.add(owner_id)
        person = people.get(owner_id) or {}
        pairs = [v for k, v in run_counts.items() if k[0] == owner_id]
        by_user.append({
            "key": owner_id, "email": person.get("email"), "name": person.get("name"),
            "guest": bool(person.get("guest")), "last_active": person.get("last_active"),
            "runs": sum(p[0] for p in pairs), "failed": sum(p[1] or 0 for p in pairs),
            "calls": 0, **{m: 0.0 for m in METRICS},
        })
    for row in by_day:
        runs, failed = runs_by_day.get(row["key"], (0, 0))
        row["runs"], row["failed"] = runs, failed or 0
    days_with_usage = {row["key"] for row in by_day}
    for day, (runs, failed) in runs_by_day.items():
        if day and day not in days_with_usage:
            by_day.append({"key": day, "runs": runs, "failed": failed or 0, "calls": 0, **{m: 0.0 for m in METRICS}})
    by_day.sort(key=lambda row: row["key"] or "")

    for m in METRICS:
        totals[m] = round(float(totals.get(m) or 0), 6 if m == "cost_usd" else 3)
    totals["calls"] = int(totals.get("calls") or 0)
    totals.update({k: int(v or 0) for k, v in run_stats.items()})
    return {
        "totals": totals,
        "by_billing": by_billing,
        "by_day": by_day,
        "by_user": by_user,
        "by_engine": by_engine,
        "by_model": by_model,
        "by_stage": by_stage,
        "users": sorted(
            ({"id": k, "email": v.get("email"), "name": v.get("name"), "guest": bool(v.get("guest"))}
             for k, v in people.items() if k is not None),
            key=lambda u: (u["guest"], str(u["email"] or "")),
        ),
        "options": options,
        "backfilled_at": backfilled[0] if backfilled else None,
    }


_RUN_SORTS = {
    "created": "r.created_at", "updated": "r.updated_at", "cost": "cost_usd",
    "tokens": "(input_tokens + output_tokens)", "duration": "latency_s",
}


def list_runs(params: dict[str, str]) -> dict[str, Any]:
    """Filterable, paginated list of every indexed run."""
    where, args = _run_filters(params)
    try:
        limit = max(1, min(int(params.get("limit") or 50), 200))
        offset = max(0, int(params.get("offset") or 0))
    except ValueError:
        limit, offset = 50, 0
    order = _RUN_SORTS.get(params.get("sort") or "created", "r.created_at")
    direction = "ASC" if params.get("dir") == "asc" else "DESC"
    usage_sums = ", ".join(f"COALESCE(SUM(u.{m}), 0) AS {m}" for m in METRICS)
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM runs_index r WHERE {where}", args).fetchone()[0]
        rows = _rows(conn.execute(
            f"""SELECT r.*, {usage_sums}, COALESCE(SUM(u.calls), 0) AS calls,
                       GROUP_CONCAT(DISTINCT CASE WHEN u.stage != 'engine' THEN u.stage END) AS extra_stages
                FROM runs_index r LEFT JOIN usage_daily u ON u.run_id = r.run_id
                WHERE {where}
                GROUP BY r.run_id
                ORDER BY {order} {direction}, r.run_id
                LIMIT ? OFFSET ?""",
            [*args, limit, offset],
        ))
    for row in rows:
        row["guest"] = bool(row["guest"])
        row["deleted"] = bool(row.pop("deleted_at", None))
        row["extra_stages"] = [s for s in str(row.get("extra_stages") or "").split(",") if s]
        started, ended = row.get("created_at"), row.get("finished_at")
        row["wall_s"] = _elapsed(started, ended)
    return {"runs": rows, "total": total, "limit": limit, "offset": offset}


def _elapsed(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        a = time.mktime(time.strptime(str(start)[:19], fmt))
        b = time.mktime(time.strptime(str(end)[:19], fmt))
    except ValueError:
        return None
    return max(0.0, b - a)


def start_backfill_thread() -> None:
    """Import pre-existing runs in the background on server start."""
    def work() -> None:
        try:
            result = backfill()
            if result["imported"]:
                logger.info("usage ledger: imported %s existing runs", result["imported"])
        except Exception:  # noqa: BLE001
            logger.exception("usage ledger: backfill failed")

    threading.Thread(target=work, name="usage-ledger-backfill", daemon=True).start()


if __name__ == "__main__":
    import sys

    print(json.dumps(backfill(force="--force" in sys.argv)))
