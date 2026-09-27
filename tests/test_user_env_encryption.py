"""Verify versioned encryption and migration of per-user provider settings."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from agent_monitor import auth


def _raw_rows(path: Path) -> list[tuple[int, str, str]]:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT user_id, key, value FROM user_env ORDER BY user_id, key"
        ).fetchall()


@pytest.fixture()
def encrypted_store(tmp_path: Path):
    db_path = tmp_path / "users.db"
    key_path = tmp_path / "data.key"
    key_path.write_bytes(Fernet.generate_key())
    env = {
        "AGENT_MONITOR_DATA_KEY_FILE": str(key_path),
        "AGENT_MONITOR_REQUIRE_DATA_KEY": "1",
        "AGENT_MONITOR_SECRET_KEY": Fernet.generate_key().decode("ascii"),
    }
    with (
        patch.object(auth, "DB_PATH", db_path),
        patch.object(auth, "_INITIALIZED", False),
        patch.dict(
            auth.os.environ,
            {
                "AGENT_MONITOR_DATA_KEY_FILE": env["AGENT_MONITOR_DATA_KEY_FILE"],
                "AGENT_MONITOR_REQUIRE_DATA_KEY": env["AGENT_MONITOR_REQUIRE_DATA_KEY"],
                "AGENT_MONITOR_SECRET_KEY": env["AGENT_MONITOR_SECRET_KEY"],
                "CREDENTIALS_DIRECTORY": "",
                "AGENT_MONITOR_DATA_ENCRYPTION_KEY": "",
            },
        ),
    ):
        yield db_path, key_path, env
    auth._INITIALIZED = False


def test_new_values_are_tagged_encrypted_and_round_trip(encrypted_store) -> None:
    db_path, _, _ = encrypted_store
    auth.set_user_env(7, {"OPENAI_API_KEY": "test-openai-secret"})

    stored = _raw_rows(db_path)[0][2]
    assert stored.startswith("enc:v1:")
    assert "test-openai-secret" not in stored
    assert auth.get_user_env(7) == {"OPENAI_API_KEY": "test-openai-secret"}


def test_plaintext_rows_migrate_atomically_and_idempotently(encrypted_store) -> None:
    db_path, _, _ = encrypted_store
    with auth._conn() as conn:
        conn.executemany(
            "INSERT INTO user_env(user_id,key,value) VALUES(?,?,?)",
            [
                (3, "OPENAI_API_KEY", "legacy-openai-secret"),
                (3, "AGENT_MONITOR_MODEL", "gpt-test"),
            ],
        )

    auth._INITIALIZED = False
    assert auth.get_user_env(3) == {
        "OPENAI_API_KEY": "legacy-openai-secret",
        "AGENT_MONITOR_MODEL": "gpt-test",
    }
    first = _raw_rows(db_path)
    assert all(value.startswith("enc:v1:") for _, _, value in first)
    assert all("legacy-openai-secret" not in value for _, _, value in first)

    auth._INITIALIZED = False
    assert auth.get_user_env(3)["OPENAI_API_KEY"] == "legacy-openai-secret"
    assert _raw_rows(db_path) == first


def test_legacy_untagged_fernet_is_rewrapped(encrypted_store) -> None:
    db_path, _, env = encrypted_store
    legacy = Fernet(env["AGENT_MONITOR_SECRET_KEY"].encode("ascii"))
    old_token = legacy.encrypt(b"legacy-anthropic-secret").decode("ascii")
    with auth._conn() as conn:
        conn.execute(
            "INSERT INTO user_env(user_id,key,value) VALUES(?,?,?)",
            (8, "ANTHROPIC_API_KEY", old_token),
        )

    auth._INITIALIZED = False
    assert auth.get_user_env(8) == {
        "ANTHROPIC_API_KEY": "legacy-anthropic-secret"
    }
    stored = _raw_rows(db_path)[0][2]
    assert stored.startswith("enc:v1:")
    assert stored != "enc:v1:" + old_token


def test_noncanonical_historical_app_secret_can_be_migrated(encrypted_store) -> None:
    db_path, _, _ = encrypted_store
    historical_secret = "a historical passphrase, not a Fernet key"
    derived = base64.urlsafe_b64encode(
        hashlib.sha256(historical_secret.encode("utf-8")).digest()
    )
    old_token = Fernet(derived).encrypt(b"legacy-provider-secret").decode("ascii")
    with auth._conn() as conn:
        conn.execute(
            "INSERT INTO user_env(user_id,key,value) VALUES(?,?,?)",
            (13, "OPENAI_API_KEY", old_token),
        )

    auth._INITIALIZED = False
    with patch.dict(
        auth.os.environ,
        {"AGENT_MONITOR_SECRET_KEY": historical_secret},
    ):
        assert auth.get_user_env(13) == {
            "OPENAI_API_KEY": "legacy-provider-secret"
        }
    assert _raw_rows(db_path)[0][2].startswith("enc:v1:")


def test_legacy_untagged_fernet_still_decrypts_before_cutover(encrypted_store) -> None:
    _, _, env = encrypted_store
    legacy = Fernet(env["AGENT_MONITOR_SECRET_KEY"].encode("ascii"))
    old_token = legacy.encrypt(b"legacy-openai-secret").decode("ascii")
    with patch.dict(
        auth.os.environ,
        {
            "AGENT_MONITOR_DATA_KEY_FILE": "",
            "AGENT_MONITOR_REQUIRE_DATA_KEY": "0",
            "AGENT_MONITOR_DATA_ENCRYPTION_KEY": "",
            "CREDENTIALS_DIRECTORY": "",
        },
    ):
        assert auth._decrypt_val(old_token) == "legacy-openai-secret"


def test_legacy_tagged_value_is_rewrapped_under_dedicated_key(encrypted_store) -> None:
    db_path, _, env = encrypted_store
    legacy = Fernet(env["AGENT_MONITOR_SECRET_KEY"].encode("ascii"))
    legacy_token = legacy.encrypt(b"legacy-kimi-secret").decode("ascii")
    with auth._conn() as conn:
        conn.execute(
            "INSERT INTO user_env(user_id,key,value) VALUES(?,?,?)",
            (11, "KIMI_API_KEY", "enc:v1:" + legacy_token),
        )

    auth._INITIALIZED = False
    assert auth.get_user_env(11) == {"KIMI_API_KEY": "legacy-kimi-secret"}
    stored = _raw_rows(db_path)[0][2]
    assert stored.startswith("enc:v1:")
    assert stored != "enc:v1:" + legacy_token


def test_migration_returns_changed_count(encrypted_store) -> None:
    db_path, _, _ = encrypted_store
    with auth._conn() as conn:
        conn.execute(
            "INSERT INTO user_env(user_id,key,value) VALUES(?,?,?)",
            (12, "OPENAI_API_KEY", "legacy-value"),
        )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert auth._migrate_user_env_encryption(conn) == 1
        assert auth._migrate_user_env_encryption(conn) == 0


def test_corrupt_tagged_value_fails_closed_and_rolls_back(encrypted_store) -> None:
    db_path, _, _ = encrypted_store
    with auth._conn() as conn:
        conn.executemany(
            "INSERT INTO user_env(user_id,key,value) VALUES(?,?,?)",
            [
                (9, "AAA_PLAINTEXT", "legacy-plaintext"),
                (9, "ZZZ_CORRUPT", "enc:v1:not-a-fernet-token"),
            ],
        )
    before = _raw_rows(db_path)

    auth._INITIALIZED = False
    with pytest.raises(auth.UserEnvDecryptionError):
        auth.get_user_env(9)
    assert _raw_rows(db_path) == before


def test_wrong_key_fails_closed(encrypted_store) -> None:
    _, key_path, _ = encrypted_store
    auth.set_user_env(10, {"KIMI_API_KEY": "test-kimi-secret"})
    key_path.write_bytes(Fernet.generate_key())

    auth._INITIALIZED = False
    with pytest.raises(auth.UserEnvDecryptionError):
        auth.get_user_env(10)


def test_required_key_must_exist(tmp_path: Path) -> None:
    with (
        patch.object(auth, "DB_PATH", tmp_path / "missing-key.db"),
        patch.object(auth, "_INITIALIZED", False),
        patch.dict(
            auth.os.environ,
            {
                "AGENT_MONITOR_REQUIRE_DATA_KEY": "1",
                "AGENT_MONITOR_DATA_KEY_FILE": "",
                "AGENT_MONITOR_DATA_ENCRYPTION_KEY": "",
                "AGENT_MONITOR_SECRET_KEY": Fernet.generate_key().decode("ascii"),
                "CREDENTIALS_DIRECTORY": "",
            },
        ),
    ):
        with pytest.raises(RuntimeError, match="required user-environment data key"):
            auth._conn()
    auth._INITIALIZED = False


def test_systemd_credentials_directory_supplies_data_key(tmp_path: Path) -> None:
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir()
    key_path = credential_dir / "agent_monitor_data_key"
    key_path.write_bytes(Fernet.generate_key())
    with patch.dict(
        auth.os.environ,
        {
            "AGENT_MONITOR_DATA_KEY_FILE": "",
            "AGENT_MONITOR_DATA_ENCRYPTION_KEY": "",
            "CREDENTIALS_DIRECTORY": str(credential_dir),
        },
    ):
        assert auth._dedicated_fernet() is not None


def test_key_file_symlink_is_rejected(tmp_path: Path) -> None:
    real_key = tmp_path / "real.key"
    real_key.write_bytes(Fernet.generate_key())
    linked_key = tmp_path / "linked.key"
    linked_key.symlink_to(real_key)
    with patch.dict(
        auth.os.environ,
        {
            "AGENT_MONITOR_DATA_KEY_FILE": str(linked_key),
            "AGENT_MONITOR_DATA_ENCRYPTION_KEY": "",
            "CREDENTIALS_DIRECTORY": "",
        },
    ):
        with pytest.raises(RuntimeError, match="cannot open"):
            auth._dedicated_fernet()


def test_oversized_key_file_is_rejected(tmp_path: Path) -> None:
    key_path = tmp_path / "oversized.key"
    key_path.write_bytes(b"x" * 4097)
    with patch.dict(
        auth.os.environ,
        {
            "AGENT_MONITOR_DATA_KEY_FILE": str(key_path),
            "AGENT_MONITOR_DATA_ENCRYPTION_KEY": "",
            "CREDENTIALS_DIRECTORY": "",
        },
    ):
        with pytest.raises(RuntimeError, match="invalid type or size"):
            auth._dedicated_fernet()
