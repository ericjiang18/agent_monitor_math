#!/usr/bin/env python3
"""Migrate Agent Monitor user settings without printing stored values."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


def main() -> int:
    credential_dir = Path(os.environ["CREDENTIALS_DIRECTORY"]).resolve()
    env_path = credential_dir / "agent_monitor_env"
    data_key_path = credential_dir / "agent_monitor_data_key"
    if not env_path.is_file() or not data_key_path.is_file():
        raise RuntimeError("required systemd credentials are unavailable")

    # Pin the environment credential before importing settings/auth. The
    # production server performs the same load in console_server startup.
    os.environ["AGENT_MONITOR_ENV_PATH"] = str(env_path)
    os.environ["AGENT_MONITOR_REQUIRE_DATA_KEY"] = "1"
    from dotenv import load_dotenv

    if not load_dotenv(env_path, override=False):
        raise RuntimeError("encrypted environment credential could not be loaded")

    from agent_monitor import auth

    expected_db = (
        Path(os.environ["AGENT_MONITOR_DATA_DIR"]).resolve() / "users.db"
    )
    if auth.DB_PATH.resolve() != expected_db:
        raise RuntimeError("refusing to migrate an unexpected database path")

    with auth._conn() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM user_env").fetchone()[0])
        encrypted = int(
            conn.execute(
                "SELECT COUNT(*) FROM user_env WHERE value LIKE 'enc:v1:%'"
            ).fetchone()[0]
        )
        if encrypted != total:
            raise RuntimeError("not every user setting was migrated")
        for row in conn.execute("SELECT value FROM user_env"):
            auth._decrypt_val(str(row[0] or ""))
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise RuntimeError("database quick_check failed after migration")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # Aggregate metadata only; never print keys, ciphertext, or plaintext.
    print(json.dumps({"ok": True, "rows": total, "encrypted": encrypted}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
