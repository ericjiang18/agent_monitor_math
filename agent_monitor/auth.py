"""User accounts, sessions, and per-user API keys for the Proving Console.

Storage: SQLite at data/users.db (WAL). Passwords use scrypt; session tokens
are random 256-bit values stored hashed. Google Sign-In verifies the GIS ID
token (JWT) against Google's JWKS using PyJWT — enable it by setting
GOOGLE_OAUTH_CLIENT_ID in .env.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import stat
import threading
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("agent_monitor.auth")

from agent_monitor import DATA_DIR

DB_PATH = DATA_DIR / "users.db"
SESSION_COOKIE = "pc_session"
SESSION_TTL_DAYS = 30
GUEST_SESSION_TTL_HOURS = 24

# One "@", no spaces, and a dot inside the domain.  The dot is found by a
# lookahead so a crafted text ("a@....@") is refused in linear time instead of
# being backtracked through quadratically (the match holds the GIL).
_EMAIL_RE = re.compile(r"^[^@\s]+@(?=[^@\s]+\.[^@\s])[^@\s]+$")
# The longest address mail can carry (RFC 5321 path limit, less the brackets).
MAX_EMAIL_LENGTH = 254
_INIT_LOCK = threading.Lock()
_INITIALIZED = False
_USER_ENV_CIPHERTEXT_PREFIX = "enc:v1:"
_USER_ENV_LEGACY_CIPHERTEXT_PREFIX = "enc:v0:"
_DATA_KEY_CREDENTIAL = "agent_monitor_data_key"

# A fresh account starts on the included Kimi route and the tool-free Ansatz
# CGM engine. These values belong to the account so an explicit user choice
# can replace them later without changing server-wide defaults.
_NEW_USER_DEFAULTS = {
    "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
    "AGENT_MONITOR_USE_CODEX": "0",
    "AGENT_MONITOR_USE_CLAUDE": "0",
    "AGENT_MONITOR_MODEL": "kimi-k3",
    "AGENT_MONITOR_ENGINE": "kimi",
}
# Public, non-secret values a key-less registration may have seeded in
# plaintext (current and historical defaults). ``create-admin`` runs without
# the root-only data key, so a console that already migrated its database must
# accept exactly these rows and re-encrypt them instead of failing closed.
_PLAINTEXT_SEED_VALUES: dict[str, frozenset[str]] = {
    key: frozenset({value}) for key, value in _NEW_USER_DEFAULTS.items()
}
_PLAINTEXT_SEED_VALUES["AGENT_MONITOR_ENGINE"] = frozenset({"kimi", "plain"})


class UserEnvDecryptionError(RuntimeError):
    """A stored user setting could not be authenticated and decrypted."""


def public_mode() -> bool:
    """Open access: visitors get a guest session instead of a login wall."""
    return os.environ.get("AGENT_MONITOR_PUBLIC", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _registration_override() -> bool | None:
    raw = os.environ.get("AGENT_MONITOR_REGISTRATION_OPEN")
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def registration_open() -> bool:
    """Whether an operator explicitly enabled HTTP self-registration."""
    return _registration_override() is True


# ── Registration eligibility and email verification ──────────────────────────
# AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES (e.g. ".edu") limits which addresses
# may create accounts over HTTP; AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION=1 makes
# a new account confirm its address before it can sign in. Both default off.

EMAIL_VERIFICATION_TTL = timedelta(hours=24)


class EmailVerificationRequired(PermissionError):
    """The password was right, but the account's address is not verified yet."""

    def __init__(self, email: str, message: str):
        super().__init__(message)
        self.email = email


class EmailDeliveryUnavailable(RuntimeError):
    """Sign-up needs a verification email, but outgoing mail is not configured."""


ACCESS_PAUSED_ENV = "AGENT_MONITOR_ACCESS_PAUSED_MESSAGE"
ACCESS_PAUSED_MESSAGE = (
    "Your access to this site is paused for now. Please check with the organizers or the site administrator."
)


class AccessPaused(PermissionError):
    """The password was right, but an administrator has paused this account's access."""


def access_paused_message() -> str:
    return (os.environ.get(ACCESS_PAUSED_ENV) or "").strip()[:500] or ACCESS_PAUSED_MESSAGE


def _check_access(row: sqlite3.Row) -> None:
    """Refuse a sign-in to an account whose access an administrator paused
    (an administrator's own access is never paused)."""
    if row["access_paused_at"] and not row["is_admin"]:
        raise AccessPaused(access_paused_message())


def registration_email_suffixes() -> tuple[str, ...]:
    """Domain suffixes new accounts must use, such as (".edu",); empty allows any."""
    suffixes = []
    for part in os.environ.get("AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES", "").split(","):
        value = part.strip().lower().lstrip("@")
        if value:
            suffixes.append(value if value.startswith(".") else "." + value)
    return tuple(dict.fromkeys(suffixes))


def email_allowed_for_registration(email: str) -> bool:
    suffixes = registration_email_suffixes()
    if not suffixes:
        return True
    domain = (email or "").strip().lower().rsplit("@", 1)[-1]
    return any(("." + domain).endswith(suffix) for suffix in suffixes)


def registration_email_rule() -> str:
    """The sign-up restriction as one user-facing sentence."""
    suffixes = registration_email_suffixes()
    if len(suffixes) == 1:
        return f"Sign-up is limited to {suffixes[0]} email addresses."
    return "Sign-up is limited to email addresses ending in " + " or ".join(suffixes) + "."


def email_verification_required() -> bool:
    raw = os.environ.get("AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def email_delivery_configured() -> bool:
    """Whether outgoing mail for reset and verification links is configured."""
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except (TypeError, ValueError):
        return False
    return bool(
        (os.environ.get("SMTP_SERVER") or "").strip()
        and (os.environ.get("SMTP_USERNAME") or "").strip()
        and (os.environ.get("SMTP_PASSWORD") or "")
        and 1 <= port <= 65535
    )


def _check_verified(row: sqlite3.Row) -> None:
    """Refuse a sign-in by an unverified account while verification is required."""
    if not email_verification_required():
        return
    if row["is_admin"] or row["is_guest"] or row["email_verified_at"]:
        return
    email = str(row["email"])
    if not email_allowed_for_registration(email):
        raise PermissionError(
            registration_email_rule() + " This account cannot be verified; sign up again with an eligible address."
        )
    raise EmailVerificationRequired(
        email, f"Confirm your email address to finish signing up. We sent a link to {email}."
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_new_user_defaults(conn: sqlite3.Connection, user_id: int) -> None:
    """Seed one new account without overwriting any explicit user setting."""
    # Keep the historical no-key development behavior, while ensuring the
    # production data-key path stores these defaults encrypted like all other
    # per-user settings. Invalid configured keys still fail closed here.
    encrypt = _dedicated_fernet() is not None or _legacy_fernet() is not None
    conn.executemany(
        "INSERT OR IGNORE INTO user_env (user_id, key, value) VALUES (?,?,?)",
        [
            (int(user_id), key, _encrypt_val(value) if encrypt else value)
            for key, value in _NEW_USER_DEFAULTS.items()
        ],
    )


def _migrate_user_roles(conn: sqlite3.Connection) -> None:
    """Add an explicit guest role and backfill generated legacy guests."""
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "is_guest" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN is_guest INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "UPDATE users SET is_guest=1, is_admin=0"
        " WHERE is_guest=0 AND email GLOB 'guest+*@public.local'"
        " AND password_hash IS NULL AND google_sub IS NULL"
    )
    # Guest is always the least-privileged role, including after manual edits
    # or legacy first-account bootstrap behavior.
    conn.execute("UPDATE users SET is_admin=0 WHERE is_guest=1 AND is_admin!=0")


def _migrate_access_pause(conn: sqlite3.Connection) -> None:
    """Add the time an administrator paused an account's access (NULL: on),
    when the account last signed in (kept after its sessions end), the group
    an administrator put it in (a field test's participants, say), and
    whether an administrator gave it full access (account_policy)."""
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    for column, kind in (("access_paused_at", "TEXT"), ("last_sign_in_at", "TEXT"), ("account_group", "TEXT"),
                         ("full_access", "INTEGER NOT NULL DEFAULT 0")):
        if column in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {kind}")
        except sqlite3.OperationalError as exc:
            # Another process (the console, the command line) added it first.
            if "duplicate column name" not in str(exc):
                raise


def full_access_by_default() -> bool:
    """Whether new member accounts start with full access (account_policy):
    yes unless ``AGENT_MONITOR_FULL_ACCESS_DEFAULT`` is 0/false/off/no.
    Guests never have it; an administrator can take it back per account."""
    value = os.environ.get("AGENT_MONITOR_FULL_ACCESS_DEFAULT", "1").strip().lower()
    return value not in ("0", "false", "off", "no")


def _migrate_full_access_default(conn: sqlite3.Connection) -> None:
    """Give every existing member account full access, once, when full
    access is the default.  Recorded in ``auth_migrations`` so an account an
    administrator later takes it back from stays without it."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS auth_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if not full_access_by_default():
        return
    cur = conn.execute(
        "INSERT OR IGNORE INTO auth_migrations (name, applied_at) VALUES ('full_access_default', ?)",
        (_now(),),
    )
    if cur.rowcount:
        conn.execute("UPDATE users SET full_access=1 WHERE is_guest=0 AND full_access=0")


def _migrate_email_verification(conn: sqlite3.Connection) -> None:
    """Add verification state; accounts that predate it count as verified."""
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "email_verified_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN email_verified_at TEXT")
        conn.execute(
            "UPDATE users SET email_verified_at=COALESCE(created_at, ?) WHERE is_guest=0",
            (_now(),),
        )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS email_verifications ("
        " token_hash TEXT PRIMARY KEY,"
        " user_id INTEGER NOT NULL,"
        " expires_at TEXT NOT NULL,"
        " FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)"
    )


def _conn() -> sqlite3.Connection:
    global _INITIALIZED
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # SQLite otherwise creates the database according to the caller's umask,
    # which can leave account records and encrypted credentials readable.
    DB_PATH.touch(mode=0o600, exist_ok=True)
    DB_PATH.chmod(0o600)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if not _INITIALIZED:
        with _INIT_LOCK:
            if _INITIALIZED:
                return conn
            try:
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
                    is_guest INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT,
                    email_verified_at TEXT
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
                CREATE TABLE IF NOT EXISTS guest_usage (user_id INTEGER PRIMARY KEY, runs_started INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS password_resets (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS guest_usage (
                    user_id INTEGER PRIMARY KEY,
                    runs_started INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user_id
                    ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expires_at
                    ON sessions(expires_at);
                """
                )
                # The role index must be created after upgrading an existing
                # users table, which does not yet have the is_guest column.
                _migrate_user_roles(conn)
                _migrate_email_verification(conn)
                _migrate_access_pause(conn)
                _migrate_full_access_default(conn)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_users_is_guest ON users(is_guest)"
                )
                conn.commit()
                _migrate_user_env_encryption(conn)
                _INITIALIZED = True
            except Exception:
                conn.close()
                raise
    return conn


def guest_runs_started(user_id: int) -> int:
    """Lifetime starts for this guest identity, independent of run deletion."""
    with _conn() as conn:
        row = conn.execute("SELECT runs_started FROM guest_usage WHERE user_id=?", (user_id,)).fetchone()
    return int(row[0]) if row else 0



def reserve_guest_run(user_id: int, limit: int, *, previous_runs: int = 0) -> bool:
    """Reserve one use atomically, including runs predating the usage ledger."""
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO guest_usage(user_id, runs_started) VALUES (?, ?)",
                     (user_id, max(0, previous_runs)))
        result = conn.execute("UPDATE guest_usage SET runs_started=runs_started+1 WHERE user_id=? AND runs_started<?",
                              (user_id, limit))
    return result.rowcount == 1



def release_guest_run(user_id: int) -> None:
    """Refund a reservation only if synchronous job validation failed."""
    with _conn() as conn:
        conn.execute("UPDATE guest_usage SET runs_started=MAX(0, runs_started-1) WHERE user_id=?", (user_id,))


# ── passwords ────────────────────────────────────────────────────────────────

def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)


def _authenticated_user(conn: sqlite3.Connection, email: str, password: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row is None or row["password_hash"] is None:
        raise ValueError("Incorrect email or password")
    if not hmac.compare_digest(_hash_password(password or "", row["salt"]), row["password_hash"]):
        raise ValueError("Incorrect email or password")
    return row


def _insert_session(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    ttl: timedelta | None = None,
) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + (
        ttl if ttl is not None else timedelta(days=SESSION_TTL_DAYS)
    )
    now = _now()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (_token_hash(token), user_id, now, expires.isoformat()),
    )
    conn.execute("UPDATE users SET last_sign_in_at=? WHERE id=? AND is_guest=0", (now, user_id))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
    _purge_expired_guest_accounts(conn)
    return token


def _user_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    is_guest = bool(row["is_guest"])
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"] or row["email"].split("@")[0],
        "picture": row["picture"],
        "is_admin": bool(row["is_admin"]) and not is_guest,
        "guest": is_guest,
        # On by default (full_access_by_default); an administrator can take
        # it back (account_policy.has_full_access).
        "full_access": bool("full_access" in row.keys() and row["full_access"]) and not is_guest,
    }


def is_operator_account(user_id: int | None) -> bool:
    """Whether ``user_id`` is a registered administrator (never a guest).

    Fails closed: an unknown id or an unreadable account database is not an
    operator.
    """
    if user_id is None:
        return False
    try:
        with closing(_conn()) as conn:
            row = conn.execute(
                "SELECT is_admin, is_guest FROM users WHERE id=?", (int(user_id),)
            ).fetchone()
    except Exception:  # noqa: BLE001 - fail closed
        return False
    return bool(row is not None and row["is_admin"] and not row["is_guest"])


def is_full_access_account(user_id: int | None) -> bool:
    """Whether ``user_id`` is a registered account an administrator gave full
    access (never a guest).  Fails closed, like :func:`is_operator_account`."""
    if user_id is None:
        return False
    try:
        with closing(_conn()) as conn:
            row = conn.execute(
                "SELECT full_access, is_guest FROM users WHERE id=?", (int(user_id),)
            ).fetchone()
    except Exception:  # noqa: BLE001 - fail closed
        return False
    return bool(row is not None and row["full_access"] and not row["is_guest"])


def may_use_own_subscriptions(user_id: int | None) -> bool:
    """Whether runs of ``user_id`` may use the Claude Code and Codex
    subscriptions that account linked itself: an administrator, or an
    account given full access.  The stored role decides, so a sign-in linked
    by an account without it is never used."""
    return is_operator_account(user_id) or is_full_access_account(user_id)


def _registered_user_exists(conn: sqlite3.Connection) -> bool:
    """Return whether a password or federated account already exists."""
    return (
        conn.execute(
            "SELECT 1 FROM users"
            " WHERE password_hash IS NOT NULL OR google_sub IS NOT NULL LIMIT 1"
        ).fetchone()
        is not None
    )


def register(
    email: str,
    password: str,
    name: str | None = None,
    *,
    require_open: bool = False,
) -> dict[str, Any]:
    # Reject a closed public registration route before the intentionally
    # expensive scrypt operation. Recheck after taking the write lock below.
    if require_open and _registration_override() is not True:
        raise PermissionError("Account registration is currently closed")
    email = (email or "").strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(email):
        raise ValueError("Please enter a valid email address")
    # A new address must be one plain mailbox that mail reaches exactly as
    # written: "a@b.example(x.edu" or "x<c@d.example>" would pass the pattern
    # (and a domain rule) while their mail goes elsewhere.  Only new accounts
    # are held to this; stored addresses keep signing in as they are.
    from agent_monitor.accounts_admin import usable_address

    if not usable_address(email):
        raise ValueError("Please enter a valid email address")
    if email.endswith("@public.local"):
        raise ValueError("Please enter a non-reserved email address")
    if require_open and not email_allowed_for_registration(email):
        raise ValueError(registration_email_rule())
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters")
    if require_open and email_verification_required() and not email_delivery_configured():
        raise EmailDeliveryUnavailable(
            "Sign-up needs email verification, which is not set up on this site yet. Please try again later."
        )
    salt = secrets.token_bytes(16)
    pw_hash = _hash_password(password, salt)
    with closing(_conn()) as conn, conn:
        # Serialize first-admin selection and ignore anonymous guest rows.
        conn.execute("BEGIN IMMEDIATE")
        if require_open and _registration_override() is not True:
            raise PermissionError("Account registration is currently closed")
        is_admin = not _registered_user_exists(conn)
        try:
            cur = conn.execute(
                "INSERT INTO users"
                " (email, name, password_hash, salt, is_admin, is_guest, created_at, email_verified_at,"
                " full_access)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    email,
                    # Bounded like every other name an account is given.
                    (name or "").strip()[:120].strip() or None,
                    pw_hash,
                    salt,
                    int(is_admin),
                    0,
                    _now(),
                    # HTTP sign-ups start unverified; operator-created accounts do not.
                    None if require_open else _now(),
                    int(full_access_by_default()),
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError("This email is already registered") from None
        _seed_new_user_defaults(conn, int(cur.lastrowid))
        row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return _user_dict(row)  # type: ignore[return-value]


def create_guest_session(*, replace_account_token: str | None = None) -> tuple[dict[str, Any], str]:
    """Atomically create one isolated guest identity and 24-hour session."""
    email = f"guest+{secrets.token_hex(8)}@public.local"
    with closing(_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO users"
            " (email, name, password_hash, salt, is_admin, is_guest, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (email, "Guest", None, None, 0, 1, _now()),
        )
        row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        token = _insert_session(
            conn,
            int(cur.lastrowid),
            ttl=timedelta(hours=GUEST_SESSION_TTL_HOURS),
        )
        if replace_account_token:
            # Revoke only this browser's account session after guest creation
            # succeeds, in the same transaction. Any failure rolls both back.
            conn.execute(
                "DELETE FROM sessions WHERE token_hash=? AND user_id IN"
                " (SELECT id FROM users WHERE is_guest=0)",
                (_token_hash(replace_account_token),),
            )
    account = _user_dict(row)
    assert account is not None
    return account, token


def login(email: str, password: str) -> dict[str, Any]:
    email = (email or "").strip().lower()
    with closing(_conn()) as conn, conn:
        row = _authenticated_user(conn, email, password)
    _check_verified(row)
    _check_access(row)
    return _user_dict(row)  # type: ignore[return-value]


def login_and_create_session(email: str, password: str) -> tuple[dict[str, Any], str]:
    """Authenticate and mint a session atomically with password resets."""
    email = (email or "").strip().lower()
    with closing(_conn()) as conn, conn:
        # A reset also takes this write lock. An old-password login that began
        # first therefore creates its session before the reset deletes all
        # sessions; one that begins later verifies only the new password.
        conn.execute("BEGIN IMMEDIATE")
        row = _authenticated_user(conn, email, password)
        _check_verified(row)
        _check_access(row)
        token = _insert_session(conn, int(row["id"]))
    account = _user_dict(row)
    assert account is not None
    return account, token


# ── sessions ─────────────────────────────────────────────────────────────────

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int) -> str:
    with closing(_conn()) as conn, conn:
        return _insert_session(conn, user_id)


def user_for_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with closing(_conn()) as conn, conn:
        row = conn.execute(
            "SELECT u.*, s.expires_at AS session_expires_at"
            " FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash=?",
            (_token_hash(token),),
        ).fetchone()
        if row is not None and str(row["session_expires_at"]) <= _now():
            user_id = int(row["id"])
            conn.execute(
                "DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),)
            )
            if bool(row["is_guest"]):
                conn.execute("DELETE FROM guest_usage WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM password_resets WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM user_env WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                conn.execute(
                    "DELETE FROM users WHERE id=? AND is_guest=1", (user_id,)
                )
            row = None
        if (
            row is not None
            and email_verification_required()
            and not row["is_admin"]
            and not row["is_guest"]
            and not row["email_verified_at"]
        ):
            row = None
        if row is not None and row["access_paused_at"] and not row["is_admin"]:
            # Paused by an administrator: signed out until access is on again.
            row = None
    return _user_dict(row)


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with closing(_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        token_hash = _token_hash(token)
        session = conn.execute(
            "SELECT s.user_id, u.is_guest FROM sessions s"
            " JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (token_hash,),
        ).fetchone()
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        if session is not None and bool(session["is_guest"]):
            user_id = int(session["user_id"])
            # Ending a guest session is intentionally destructive: delete the
            # identity and every encrypted provider setting attached to it.
            conn.execute("DELETE FROM guest_usage WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM password_resets WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM user_env WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM users WHERE id=? AND is_guest=1", (user_id,))


def _purge_expired_guest_accounts(conn: sqlite3.Connection) -> int:
    """Delete expired/orphan guest identities and encrypted settings.

    Cleanup runs inside session-creation transactions. Guest run artifacts
    remain subject to the ordinary run-retention policy.
    """
    now = _now()
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    rows = conn.execute(
        "SELECT u.id FROM users u WHERE u.is_guest=1"
        " AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=u.id)"
    ).fetchall()
    user_ids = [int(row["id"]) for row in rows]
    for user_id in user_ids:
        conn.execute("DELETE FROM guest_usage WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM password_resets WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_env WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=? AND is_guest=1", (user_id,))
    return len(user_ids)


# ── per-user env (API keys & settings) ───────────────────────────────────────

def _fernet_from_exact_key(material: bytes, *, source: str) -> Fernet:
    """Build a cipher from an exact Fernet key or a raw 32-byte key."""
    key = base64.urlsafe_b64encode(material) if len(material) == 32 else material.strip()
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{source} is not a valid Fernet key") from exc


def _read_data_key_file(path: Path, *, optional: bool) -> bytes | None:
    """Read one small regular key file without following a replaceable symlink."""
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if optional:
            return None
        raise RuntimeError("user-environment data key file is missing") from None
    except OSError as exc:
        raise RuntimeError("cannot open user-environment data key file") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 4096:
            raise RuntimeError("user-environment data key file has invalid type or size")
        material = bytearray()
        while len(material) <= 4096:
            chunk = os.read(fd, min(4097 - len(material), 4096))
            if not chunk:
                break
            material.extend(chunk)
        if not material or len(material) > 4096:
            raise RuntimeError("user-environment data key file has invalid size")
        return bytes(material)
    finally:
        os.close(fd)


def _dedicated_fernet() -> Fernet | None:
    """Load the database key from a file credential, never from a child env value."""
    explicit = os.environ.get("AGENT_MONITOR_DATA_KEY_FILE", "").strip()
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    credential_path = (
        Path(credential_dir) / _DATA_KEY_CREDENTIAL if credential_dir else None
    )

    if explicit and credential_path is not None:
        if os.path.abspath(explicit) != os.path.abspath(credential_path):
            # A populated systemd credential and an override are ambiguous even
            # if one of them is currently missing.
            credential_material = _read_data_key_file(credential_path, optional=True)
            if credential_material is not None:
                raise RuntimeError("conflicting user-environment data key files")

    if explicit:
        material = _read_data_key_file(Path(explicit), optional=False)
    elif credential_path is not None:
        material = _read_data_key_file(credential_path, optional=True)
    else:
        material = None

    env_key = os.environ.get("AGENT_MONITOR_DATA_ENCRYPTION_KEY", "").strip()
    if material is not None and env_key:
        raise RuntimeError("conflicting file and environment user-environment data keys")
    if material is not None:
        return _fernet_from_exact_key(material, source="user-environment data key file")
    if env_key:
        return _fernet_from_exact_key(
            env_key.encode("ascii"), source="AGENT_MONITOR_DATA_ENCRYPTION_KEY"
        )
    return None


def _legacy_fernet() -> Fernet | None:
    """Return the former app-secret cipher only for migration compatibility."""
    secret = os.environ.get("AGENT_MONITOR_SECRET_KEY", "").strip()
    if not secret:
        return None
    material = secret.encode("utf-8")
    try:
        return Fernet(material)
    except (TypeError, ValueError):
        # Historical releases derived a Fernet key from any nonempty app
        # secret. Reproduce that only to decrypt and immediately rewrap old
        # rows; new writes always use the dedicated data credential.
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))


def _get_fernet() -> Fernet:
    """Return the dedicated data cipher, with a temporary legacy fallback."""
    dedicated = _dedicated_fernet()
    if dedicated is not None:
        return dedicated
    legacy = _legacy_fernet()
    if legacy is not None:
        logger.warning(
            "Using AGENT_MONITOR_SECRET_KEY for user settings; configure the "
            "%s systemd credential",
            _DATA_KEY_CREDENTIAL,
        )
        return legacy
    raise RuntimeError("no user-environment encryption key is configured")


def _encrypt_val(val: str) -> str:
    if not val:
        return ""
    dedicated = _dedicated_fernet()
    if dedicated is not None:
        cipher = dedicated
        prefix = _USER_ENV_CIPHERTEXT_PREFIX
    else:
        cipher = _legacy_fernet()
        if cipher is None:
            raise RuntimeError("no user-environment encryption key is configured")
        prefix = _USER_ENV_LEGACY_CIPHERTEXT_PREFIX
        logger.warning(
            "Using AGENT_MONITOR_SECRET_KEY for user settings; configure the "
            "%s systemd credential",
            _DATA_KEY_CREDENTIAL,
        )
    token = cipher.encrypt(val.encode("utf-8")).decode("ascii")
    return prefix + token


def _decrypt_val(val: str) -> str:
    if not val:
        return ""
    dedicated = _dedicated_fernet()
    legacy = _legacy_fernet()
    if val.startswith(_USER_ENV_CIPHERTEXT_PREFIX):
        token = val[len(_USER_ENV_CIPHERTEXT_PREFIX) :]
        ciphers = [cipher for cipher in (dedicated, legacy) if cipher is not None]
    elif val.startswith(_USER_ENV_LEGACY_CIPHERTEXT_PREFIX):
        token = val[len(_USER_ENV_LEGACY_CIPHERTEXT_PREFIX) :]
        ciphers = [legacy] if legacy is not None else []
    elif val.startswith("gAAAA"):
        token = val
        ciphers = [cipher for cipher in (dedicated, legacy) if cipher is not None]
    else:
        if dedicated is not None:
            raise UserEnvDecryptionError(
                "legacy plaintext user setting remains after encryption migration"
            )
        return val

    for cipher in ciphers:
        try:
            return cipher.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError):
            continue
    raise UserEnvDecryptionError("stored user setting failed authenticated decryption")


def _decode_legacy_user_env_value(
    value: str, dedicated: Fernet, legacy: Fernet | None
) -> str:
    """Decode one pre-v1 value without treating broken ciphertext as plaintext."""
    if not value.startswith("gAAAA"):
        return value
    ciphers = [dedicated, *([legacy] if legacy is not None else [])]
    for cipher in ciphers:
        try:
            return cipher.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError):
            continue
    raise UserEnvDecryptionError(
        "an untagged encrypted user setting could not be authenticated"
    )


def _migrate_user_env_encryption(conn: sqlite3.Connection) -> int:
    """Atomically migrate and validate every per-user setting under the data key."""
    dedicated = _dedicated_fernet()
    required = os.environ.get("AGENT_MONITOR_REQUIRE_DATA_KEY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if dedicated is None:
        if required:
            raise RuntimeError("the required user-environment data key is unavailable")
        return 0

    changed = 0
    legacy = _legacy_fernet()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT user_id, key, value FROM user_env ORDER BY user_id, key"
        ).fetchall()
        for row in rows:
            stored = str(row["value"] or "")
            if stored.startswith(_USER_ENV_CIPHERTEXT_PREFIX):
                token = stored[len(_USER_ENV_CIPHERTEXT_PREFIX) :]
                try:
                    dedicated.decrypt(token.encode("ascii")).decode("utf-8")
                    continue
                except (InvalidToken, UnicodeError):
                    if legacy is None:
                        raise UserEnvDecryptionError(
                            "stored user setting failed migration validation"
                        ) from None
                    try:
                        plaintext = legacy.decrypt(token.encode("ascii")).decode(
                            "utf-8"
                        )
                    except (InvalidToken, UnicodeError) as exc:
                        raise UserEnvDecryptionError(
                            "stored user setting failed migration validation"
                        ) from exc
            elif stored.startswith(_USER_ENV_LEGACY_CIPHERTEXT_PREFIX):
                if legacy is None:
                    raise UserEnvDecryptionError(
                        "legacy encrypted user setting has no migration key"
                    )
                token = stored[len(_USER_ENV_LEGACY_CIPHERTEXT_PREFIX) :]
                try:
                    plaintext = legacy.decrypt(token.encode("ascii")).decode("utf-8")
                except (InvalidToken, UnicodeError) as exc:
                    raise UserEnvDecryptionError(
                        "legacy encrypted user setting failed authentication"
                    ) from exc
            else:
                plaintext = _decode_legacy_user_env_value(stored, dedicated, legacy)
            encrypted = _USER_ENV_CIPHERTEXT_PREFIX + dedicated.encrypt(
                plaintext.encode("utf-8")
            ).decode("ascii")
            conn.execute(
                "UPDATE user_env SET value=? WHERE user_id=? AND key=?",
                (encrypted, row["user_id"], row["key"]),
            )
            changed += 1

        # Validate the exact rows written before making any migration durable.
        for row in conn.execute("SELECT value FROM user_env"):
            stored = str(row["value"] or "")
            if not stored.startswith(_USER_ENV_CIPHERTEXT_PREFIX):
                raise UserEnvDecryptionError(
                    "user setting remained unencrypted during migration"
                )
            token = stored[len(_USER_ENV_CIPHERTEXT_PREFIX) :]
            try:
                dedicated.decrypt(token.encode("ascii")).decode("utf-8")
            except (InvalidToken, UnicodeError) as exc:
                raise UserEnvDecryptionError(
                    "stored user setting failed post-migration validation"
                ) from exc
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if changed:
        logger.info("Encrypted %d legacy per-user settings", changed)
    return changed


def _is_plaintext_seed(key: str, value: str) -> bool:
    """A key-less registration's public default, safe to accept and rewrap."""
    return (
        bool(value)
        and not value.startswith(
            (_USER_ENV_CIPHERTEXT_PREFIX, _USER_ENV_LEGACY_CIPHERTEXT_PREFIX, "gAAAA")
        )
        and value in _PLAINTEXT_SEED_VALUES.get(key, frozenset())
    )


def get_user_env(user_id: int) -> dict[str, str]:
    """Retrieve user API keys from SQLite and decrypt them in memory.

    Plaintext rows are otherwise refused once a data key exists. The single
    exception is an exact public registration default seeded without the key
    (``agent-monitor create-admin`` runs without the root-only credential):
    it is returned and re-encrypted in place, so a running console never
    raises ``UserEnvDecryptionError`` for such an account.
    """
    with closing(_conn()) as conn, conn:
        rows = conn.execute("SELECT key, value FROM user_env WHERE user_id=?", (user_id,)).fetchall()
        out: dict[str, str] = {}
        rewrap: list[tuple[str, str]] = []
        seeds = [
            row for row in rows if _is_plaintext_seed(str(row["key"]), str(row["value"] or ""))
        ]
        keyed = bool(seeds) and _dedicated_fernet() is not None
        for row in rows:
            key = str(row["key"])
            stored = str(row["value"] or "")
            if keyed and _is_plaintext_seed(key, stored):
                out[key] = stored
                rewrap.append((key, stored))
                continue
            out[key] = _decrypt_val(stored)
        for key, value in rewrap:
            conn.execute(
                "UPDATE user_env SET value=? WHERE user_id=? AND key=? AND value=?",
                (_encrypt_val(value), user_id, key, value),
            )
    return out


def set_user_env(user_id: int, updates: dict[str, str], clear: list[str] | None = None) -> None:
    """Encrypt user API keys before writing to SQLite."""
    with closing(_conn()) as conn, conn:
        for k in clear or []:
            conn.execute("DELETE FROM user_env WHERE user_id=? AND key=?", (user_id, k))
        for k, v in updates.items():
            if v is None or v == "":
                conn.execute("DELETE FROM user_env WHERE user_id=? AND key=?", (user_id, k))
            else:
                encrypted_v = _encrypt_val(str(v))
                conn.execute(
                    "INSERT INTO user_env (user_id, key, value) VALUES (?,?,?)"
                    " ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
                    (user_id, k, encrypted_v),
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


def login_with_google(
    credential: str, *, require_open_for_new: bool = False
) -> dict[str, Any]:
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
    if email.endswith("@public.local"):
        raise ValueError("Google account uses a reserved email address")
    name = claims.get("name")
    picture = claims.get("picture")
    with closing(_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if row is not None:
                # Google has verified this address, so it also verifies the account.
                conn.execute(
                    "UPDATE users SET google_sub=?, picture=COALESCE(?, picture),"
                    " email_verified_at=COALESCE(email_verified_at, ?) WHERE id=?",
                    (sub, picture, _now(), row["id"]),
                )
            else:
                if require_open_for_new and _registration_override() is not True:
                    raise PermissionError("Account registration is currently closed")
                if require_open_for_new and not email_allowed_for_registration(email):
                    raise PermissionError(registration_email_rule())
                is_admin = not _registered_user_exists(conn)
                cur = conn.execute(
                    "INSERT INTO users"
                    " (email, name, google_sub, picture, is_admin, is_guest, created_at, email_verified_at,"
                    " full_access)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (email, name, sub, picture, int(is_admin), 0, _now(), _now(), int(full_access_by_default())),
                )
                _seed_new_user_defaults(conn, int(cur.lastrowid))
        else:
            conn.execute(
                "UPDATE users SET name=COALESCE(?, name), picture=COALESCE(?, picture),"
                " email_verified_at=CASE WHEN email=? THEN COALESCE(email_verified_at, ?)"
                " ELSE email_verified_at END WHERE id=?",
                (name, picture, email, _now(), row["id"]),
            )
        row = conn.execute(
            "SELECT * FROM users WHERE google_sub=? OR email=?", (sub, email)
        ).fetchone()
        if row is not None:
            _check_access(row)
    return _user_dict(row)  # type: ignore[return-value]

def _reset_expiry(value: str) -> datetime | None:
    try:
        expires_at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at.astimezone(timezone.utc)


def _validated_reset_base(raw_url: str, base_path: str = "") -> str | None:
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
    path = parsed.path.rstrip("/")
    mount = (base_path or "").strip().rstrip("/")
    if mount and not mount.startswith("/"):
        mount = "/" + mount
    if not path and mount:
        path = mount
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", "")).rstrip("/")


def verify_reset_token(raw_token: str) -> dict[str, Any]:
    """Verify if a reset token is valid and return associated user info."""
    if not raw_token:
        return {"valid": False, "error": "Missing reset token"}
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with closing(_conn()) as conn, conn:
        row = conn.execute(
            "SELECT pr.user_id, pr.expires_at, u.email FROM password_resets pr "
            "JOIN users u ON u.id = pr.user_id WHERE pr.token_hash=?",
            (token_hash,),
        ).fetchone()
        if not row:
            return {"valid": False, "error": "Invalid or expired reset token"}
        expires_at = _reset_expiry(row["expires_at"])
        if expires_at is None:
            conn.execute("DELETE FROM password_resets WHERE token_hash=?", (token_hash,))
            return {"valid": False, "error": "Invalid reset token"}
        if datetime.now(timezone.utc) >= expires_at:
            conn.execute("DELETE FROM password_resets WHERE token_hash=?", (token_hash,))
            return {"valid": False, "error": "Reset link has expired"}
        return {"valid": True, "email": row["email"]}


def handle_forgot_password(email: str, base_app_url: str | None = None) -> str | None:
    """Generate a reset token and send an email if the user exists. Returns the token if created."""
    email = (email or "").strip().lower()
    if not email:
        return None
    with closing(_conn()) as conn, conn:
        user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()

    if not user:
        # Silently return to prevent email enumeration attacks
        return None

    if base_app_url:
        base_app_url = _validated_reset_base(base_app_url)
    else:
        base_app_url = _validated_reset_base(
            os.environ.get("AGENT_MONITOR_APP_URL", ""),
            os.environ.get("AGENT_MONITOR_BASE_PATH", ""),
        )
    if not base_app_url:
        logger.error("[AUTH] No valid application URL; password reset email not sent")
        return None

    # Generate a cryptographically secure random token
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    # Store the hashed token, purging prior tokens for this user
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM password_resets WHERE user_id=?", (user["id"],))
        conn.execute(
            "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user["id"], expires_at.isoformat()),
        )

    # Send the email
    reset_link = f"{base_app_url}/reset-password?token={raw_token}"
    if not _send_reset_email(email, reset_link):
        # Do not leave an undelivered credential active. The hash predicate
        # avoids deleting a newer token created by a concurrent request.
        with closing(_conn()) as conn, conn:
            conn.execute("DELETE FROM password_resets WHERE token_hash=?", (token_hash,))
        return None
    return raw_token


def _send_reset_email(to_email: str, reset_link: str) -> bool:
    return _send_email(
        to_email,
        "Password Reset Request",
        "Click the link to reset your password. This link expires in 1 hour.\n\n"
        f"{reset_link}\n\nIf you did not ask to reset your password, you can ignore this email.",
        purpose="password reset",
    )


def _send_email(to_email: str, subject: str, body: str, *, purpose: str, from_name: str | None = None,
                reply_to: str | None = None) -> bool:
    """Send one email with authenticated, certificate-verified STARTTLS
    (``from_name`` shown with the site's sender address, replies to
    ``reply_to``).  Never raises: False when the mail was not accepted."""
    from email.utils import formataddr, parseaddr

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        sender = os.environ.get("SMTP_FROM_EMAIL", "noreply@example.com")
        display = " ".join(str(from_name or "").split())[:120]
        # SMTP_FROM_EMAIL may be "Name <address>" itself: the name given here replaces its own.
        msg["From"] = formataddr((display, parseaddr(sender)[1] or sender)) if display else sender
        msg["To"] = to_email
        if reply_to and _EMAIL_RE.match(reply_to.strip()):
            msg["Reply-To"] = reply_to.strip()
        msg.set_content(body)
    except Exception as exc:  # noqa: BLE001
        logger.error("[AUTH] %s email could not be written for %s (%s)", purpose.capitalize(), to_email,
                     type(exc).__name__)
        return False

    server = (os.environ.get("SMTP_SERVER") or "").strip()
    user = (os.environ.get("SMTP_USERNAME") or "").strip()
    password = os.environ.get("SMTP_PASSWORD") or ""
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except (TypeError, ValueError):
        logger.error("[AUTH] SMTP_PORT is invalid; %s email not sent", purpose)
        return False

    if not server or not (1 <= port <= 65535) or not user or not password:
        logger.warning("[AUTH] SMTP is not fully configured; %s email not sent", purpose)
        return False

    try:
        tls_context = ssl.create_default_context()
        with smtplib.SMTP(server, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls(context=tls_context)
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
        logger.info("[AUTH] %s email accepted by SMTP for %s", purpose.capitalize(), to_email)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[AUTH] %s email delivery failed for %s (%s)",
            purpose.capitalize(),
            to_email,
            type(exc).__name__,
        )
        return False


# ── Email verification links ─────────────────────────────────────────────────

def email_verification_pending(user_id: int) -> bool:
    """Whether this account still has to confirm its address."""
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT is_admin, is_guest, email_verified_at FROM users WHERE id=?", (int(user_id),)
        ).fetchone()
    return bool(row is not None and not row["is_admin"] and not row["is_guest"] and not row["email_verified_at"])


def send_email_verification(email: str, base_app_url: str | None = None) -> str | None:
    """Email a fresh verification link to an unverified account.

    Returns the raw token when an email was accepted, otherwise None (unknown,
    verified or guest account, no application URL, or delivery failure).
    """
    email = (email or "").strip().lower()
    if base_app_url:
        base_app_url = _validated_reset_base(base_app_url)
    else:
        base_app_url = _validated_reset_base(
            os.environ.get("AGENT_MONITOR_APP_URL", ""),
            os.environ.get("AGENT_MONITOR_BASE_PATH", ""),
        )
    if not base_app_url:
        logger.error("[AUTH] No valid application URL; verification email not sent")
        return None
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT id, is_guest, email_verified_at FROM users WHERE email=?", (email,)
        ).fetchone()
    if row is None or row["is_guest"] or row["email_verified_at"]:
        return None
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + EMAIL_VERIFICATION_TTL
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM email_verifications WHERE user_id=?", (row["id"],))
        conn.execute(
            "INSERT INTO email_verifications (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, row["id"], expires_at.isoformat()),
        )
    link = f"{base_app_url}/verify-email?token={raw_token}"
    body = (
        "Welcome to Ansätze.\n\n"
        "Confirm that this is your email address to finish creating your account:\n\n"
        f"{link}\n\n"
        "The link expires in 24 hours. If you did not sign up for Ansätze, you can ignore this email."
    )
    if not _send_email(email, "Confirm your email address for Ansätze", body, purpose="verification"):
        with closing(_conn()) as conn, conn:
            conn.execute("DELETE FROM email_verifications WHERE token_hash=?", (token_hash,))
        return None
    return raw_token


def verify_email(raw_token: str) -> dict[str, Any]:
    """Consume a verification link and mark its account verified."""
    if not raw_token:
        raise ValueError("This confirmation link is invalid or has already been used.")
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    error = None
    # Raise only after the transaction commits, so an expired link is deleted.
    with closing(_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT ev.user_id, ev.expires_at, u.email FROM email_verifications ev"
            " JOIN users u ON u.id=ev.user_id WHERE ev.token_hash=?",
            (token_hash,),
        ).fetchone()
        if row is None:
            error = (
                "This confirmation link is invalid or has already been used."
                " If you already confirmed your address, sign in."
            )
        else:
            expires_at = _reset_expiry(row["expires_at"])
            if expires_at is None or datetime.now(timezone.utc) >= expires_at:
                conn.execute("DELETE FROM email_verifications WHERE token_hash=?", (token_hash,))
                error = "This confirmation link has expired. Enter your email to get a new one."
            else:
                conn.execute(
                    "UPDATE users SET email_verified_at=COALESCE(email_verified_at, ?) WHERE id=?",
                    (_now(), row["user_id"]),
                )
                conn.execute("DELETE FROM email_verifications WHERE user_id=?", (row["user_id"],))
    if error:
        raise ValueError(error)
    return {"ok": True, "email": row["email"]}


def set_password(email: str, new_password: str) -> dict[str, Any]:
    """Operator path: set an account's password and sign it out everywhere."""
    email = (email or "").strip().lower()
    if len(new_password or "") < 8:
        raise ValueError("Password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    pw_hash = _hash_password(new_password, salt)
    with closing(_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM users WHERE email=? AND is_guest=0", (email,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No account uses {email}")
        conn.execute(
            "UPDATE users SET password_hash=?, salt=? WHERE id=?", (pw_hash, salt, row["id"])
        )
        conn.execute("DELETE FROM password_resets WHERE user_id=?", (row["id"],))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    return _user_dict(row)  # type: ignore[return-value]


def handle_reset_password(raw_token: str, new_password: str) -> bool:
    """Verify token, update password, and invalidate used tokens and sessions."""
    if not raw_token or not new_password or len(new_password) < 8:
        return False
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    with closing(_conn()) as conn, conn:
        # Serialize password verification/session creation with reset token
        # consumption so exactly one reset wins and no old-password login can
        # mint a session after this transaction commits.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, expires_at FROM password_resets WHERE token_hash=?",
            (token_hash,),
        ).fetchone()

        if not row:
            return False

        expires_at = _reset_expiry(row["expires_at"])
        if expires_at is None or datetime.now(timezone.utc) >= expires_at:
            conn.execute("DELETE FROM password_resets WHERE token_hash=?", (token_hash,))
            return False

        new_salt = secrets.token_bytes(16)
        new_password_hash = _hash_password(new_password, new_salt)

        conn.execute(
            "UPDATE users SET password_hash=?, salt=?,"
            " email_verified_at=COALESCE(email_verified_at, ?) WHERE id=?",
            (new_password_hash, new_salt, _now(), row["user_id"]),
        )
        conn.execute("DELETE FROM password_resets WHERE user_id=?", (row["user_id"],))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))

    return True
