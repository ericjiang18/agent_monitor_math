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
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("agent_monitor.auth")

from agent_monitor import DATA_DIR

DB_PATH = DATA_DIR / "users.db"
SESSION_COOKIE = "pc_session"
SESSION_TTL_DAYS = 30

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GUEST_EMAIL_RE = re.compile(r"^guest\+[0-9a-f]+@public\.local$")
_INIT_LOCK = threading.Lock()
_INITIALIZED = False
_USER_ENV_CIPHERTEXT_PREFIX = "enc:v1:"
_USER_ENV_LEGACY_CIPHERTEXT_PREFIX = "enc:v0:"
_DATA_KEY_CREDENTIAL = "agent_monitor_data_key"

# A fresh account starts on the included Kimi route. These values belong to
# the account so an explicit user choice can replace them later without
# changing server-wide defaults.
_NEW_USER_DEFAULTS = {
    "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
    "AGENT_MONITOR_USE_CODEX": "0",
    "AGENT_MONITOR_USE_CLAUDE": "0",
    "AGENT_MONITOR_MODEL": "kimi-k3",
    "AGENT_MONITOR_ENGINE": "plain",
}


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
                CREATE TABLE IF NOT EXISTS user_env (
                    user_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (user_id, key)
                );

                CREATE TABLE IF NOT EXISTS password_resets (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
                )
                conn.commit()
                _migrate_user_env_encryption(conn)
                _INITIALIZED = True
            except Exception:
                conn.close()
                raise
    return conn


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


def _insert_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (_token_hash(token), user_id, _now(), expires.isoformat()),
    )
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
    return token


def _user_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"] or row["email"].split("@")[0],
        "picture": row["picture"],
        "is_admin": bool(row["is_admin"]),
        "guest": bool(_GUEST_EMAIL_RE.match(row["email"] or "")),
    }


def register(email: str, password: str, name: str | None = None) -> dict[str, Any]:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("Please enter a valid email address")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    pw_hash = _hash_password(password, salt)
    with closing(_conn()) as conn, conn:
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
        _seed_new_user_defaults(conn, int(cur.lastrowid))
        row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return _user_dict(row)  # type: ignore[return-value]


def ensure_guest_user() -> dict[str, Any]:
    """Create a unique anonymous guest (public mode)."""
    email = f"guest+{secrets.token_hex(8)}@public.local"
    with closing(_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO users (email, name, password_hash, salt, is_admin, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (email, "Guest", None, None, 0, _now()),
        )
        _seed_new_user_defaults(conn, int(cur.lastrowid))
        row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return _user_dict(row)  # type: ignore[return-value]


def login(email: str, password: str) -> dict[str, Any]:
    email = (email or "").strip().lower()
    with closing(_conn()) as conn, conn:
        row = _authenticated_user(conn, email, password)
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
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash=? AND s.expires_at > ?",
            (_token_hash(token), _now()),
        ).fetchone()
    return _user_dict(row)


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


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


def get_user_env(user_id: int) -> dict[str, str]:
    """Retrieve user API keys from SQLite and decrypt them in memory."""
    with closing(_conn()) as conn, conn:
        rows = conn.execute("SELECT key, value FROM user_env WHERE user_id=?", (user_id,)).fetchall()
    return {r["key"]: _decrypt_val(r["value"] or "") for r in rows}


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
    with closing(_conn()) as conn, conn:
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
                cur = conn.execute(
                    "INSERT INTO users (email, name, google_sub, picture, is_admin, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (email, name, sub, picture, int(is_admin), _now()),
                )
                _seed_new_user_defaults(conn, int(cur.lastrowid))
        else:
            conn.execute(
                "UPDATE users SET name=COALESCE(?, name), picture=COALESCE(?, picture) WHERE id=?",
                (name, picture, row["id"]),
            )
        row = conn.execute(
            "SELECT * FROM users WHERE google_sub=? OR email=?", (sub, email)
        ).fetchone()
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
    """Send a reset email with authenticated, certificate-verified STARTTLS."""
    msg = MIMEText(f"Click the link to reset your password. This link expires in 1 hour.\n\n{reset_link}")
    msg["Subject"] = "Password Reset Request"
    msg["From"] = os.environ.get("SMTP_FROM_EMAIL", "noreply@example.com")
    msg["To"] = to_email

    server = (os.environ.get("SMTP_SERVER") or "").strip()
    user = (os.environ.get("SMTP_USERNAME") or "").strip()
    password = os.environ.get("SMTP_PASSWORD") or ""
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except (TypeError, ValueError):
        logger.error("[AUTH] SMTP_PORT is invalid; password reset email not sent")
        return False

    if not server or not (1 <= port <= 65535) or not user or not password:
        logger.warning("[AUTH] SMTP is not fully configured; password reset email not sent")
        return False

    try:
        tls_context = ssl.create_default_context()
        with smtplib.SMTP(server, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls(context=tls_context)
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
        logger.info("[AUTH] Password reset email accepted by SMTP for %s", to_email)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[AUTH] Password reset email delivery failed for %s (%s)",
            to_email,
            type(exc).__name__,
        )
        return False


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
            "UPDATE users SET password_hash=?, salt=? WHERE id=?",
            (new_password_hash, new_salt, row["user_id"]),
        )
        conn.execute("DELETE FROM password_resets WHERE user_id=?", (row["user_id"],))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))

    return True
