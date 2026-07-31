"""User accounts, sessions, and per-user API keys for the Proving Console.

Storage: SQLite at data/users.db (WAL). Passwords use scrypt; session tokens
are random 256-bit values stored hashed. Google Sign-In verifies the GIS ID
token (JWT) against Google's JWKS using PyJWT — enable it by setting
GOOGLE_OAUTH_CLIENT_ID in .env.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_monitor import DATA_DIR

DB_PATH = DATA_DIR / "users.db"
SESSION_COOKIE = "pc_session"
SESSION_TTL_DAYS = 30

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    global _INITIALIZED
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if not _INITIALIZED:
        with _INIT_LOCK:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    name TEXT,
                    password_hash BLOB,
                    salt BLOB,
                    google_sub TEXT UNIQUE,
                    picture TEXT,
                    is_admin INTEGER DEFAULT 0,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS user_env (
                    user_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (user_id, key)
                );
                """
            )
            conn.commit()
            _INITIALIZED = True
    return conn


# ── passwords ────────────────────────────────────────────────────────────────

def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)


def _user_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"] or row["email"].split("@")[0],
        "picture": row["picture"],
        "is_admin": bool(row["is_admin"]),
    }


def register(email: str, password: str, name: str | None = None) -> dict[str, Any]:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("Please enter a valid email address")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    pw_hash = _hash_password(password, salt)
    with _conn() as conn:
        # First account becomes admin (inherits server .env keys as fallback).
        is_admin = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        try:
            cur = conn.execute(
                "INSERT INTO users (email, name, password_hash, salt, is_admin, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (email, (name or "").strip() or None, pw_hash, salt, int(is_admin), _now()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("This email is already registered") from None
        row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return _user_dict(row)  # type: ignore[return-value]


def login(email: str, password: str) -> dict[str, Any]:
    email = (email or "").strip().lower()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row is None or row["password_hash"] is None:
        raise ValueError("Incorrect email or password")
    if not hmac.compare_digest(_hash_password(password or "", row["salt"]), row["password_hash"]):
        raise ValueError("Incorrect email or password")
    return _user_dict(row)  # type: ignore[return-value]


# ── sessions ─────────────────────────────────────────────────────────────────

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (_token_hash(token), user_id, _now(), expires.isoformat()),
        )
        # opportunistic cleanup
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
    return token


def user_for_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash=? AND s.expires_at > ?",
            (_token_hash(token), _now()),
        ).fetchone()
    return _user_dict(row)


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


# ── per-user env (API keys & settings) ───────────────────────────────────────

def get_user_env(user_id: int) -> dict[str, str]:
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM user_env WHERE user_id=?", (user_id,)).fetchall()
    return {r["key"]: r["value"] or "" for r in rows}


def set_user_env(user_id: int, updates: dict[str, str], clear: list[str] | None = None) -> None:
    with _conn() as conn:
        for k in clear or []:
            conn.execute("DELETE FROM user_env WHERE user_id=? AND key=?", (user_id, k))
        for k, v in updates.items():
            if v is None or v == "":
                conn.execute("DELETE FROM user_env WHERE user_id=? AND key=?", (user_id, k))
            else:
                conn.execute(
                    "INSERT INTO user_env (user_id, key, value) VALUES (?,?,?)"
                    " ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
                    (user_id, k, str(v)),
                )


# ── Google Sign-In ───────────────────────────────────────────────────────────

_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_jwks_cache: dict[str, Any] = {"keys": None, "ts": 0.0}


def google_client_id() -> str | None:
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    return cid or None


def _google_jwks() -> dict[str, Any]:
    if _jwks_cache["keys"] is None or time.time() - _jwks_cache["ts"] > 3600:
        with urllib.request.urlopen(_GOOGLE_JWKS_URL, timeout=10) as resp:
            _jwks_cache["keys"] = json.loads(resp.read().decode("utf-8"))
            _jwks_cache["ts"] = time.time()
    return _jwks_cache["keys"]


def login_with_google(credential: str) -> dict[str, Any]:
    """Verify a Google Identity Services ID token and upsert the user."""
    cid = google_client_id()
    if not cid:
        raise ValueError("Google sign-in is not configured (missing GOOGLE_OAUTH_CLIENT_ID)")
    import jwt as pyjwt
    from jwt import PyJWK

    header = pyjwt.get_unverified_header(credential)
    key = next((k for k in _google_jwks().get("keys", []) if k.get("kid") == header.get("kid")), None)
    if key is None:
        raise ValueError("Could not verify Google token (signing key not found)")
    claims = pyjwt.decode(
        credential,
        key=PyJWK(key).key,
        algorithms=["RS256"],
        audience=cid,
        issuer=["https://accounts.google.com", "accounts.google.com"],
    )
    sub = claims["sub"]
    email = (claims.get("email") or "").lower()
    if not email or not claims.get("email_verified", False):
        raise ValueError("Google account has no verified email")
    name = claims.get("name")
    picture = claims.get("picture")
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE users SET google_sub=?, picture=COALESCE(?, picture) WHERE id=?",
                    (sub, picture, row["id"]),
                )
            else:
                is_admin = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
                conn.execute(
                    "INSERT INTO users (email, name, google_sub, picture, is_admin, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (email, name, sub, picture, int(is_admin), _now()),
                )
        else:
            conn.execute(
                "UPDATE users SET name=COALESCE(?, name), picture=COALESCE(?, picture) WHERE id=?",
                (name, picture, row["id"]),
            )
        row = conn.execute(
            "SELECT * FROM users WHERE google_sub=? OR email=?", (sub, email)
        ).fetchone()
    return _user_dict(row)  # type: ignore[return-value]
