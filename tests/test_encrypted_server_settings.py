"""Verify operator-managed settings stay immutable while user settings remain writable."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_monitor import settings


def test_server_settings_are_read_only_in_encrypted_mode() -> None:
    with patch.dict(
        settings.os.environ,
        {"AGENT_MONITOR_SERVER_SETTINGS_READ_ONLY": "1"},
        clear=False,
    ):
        with pytest.raises(PermissionError, match="operator-managed"):
            settings.save_settings({"updates": {"SMTP_PASSWORD": "new-secret"}})


def test_per_user_settings_remain_writable_in_encrypted_mode() -> None:
    user = {"id": 7, "email": "user@example.test"}
    with (
        patch.dict(
            settings.os.environ,
            {"AGENT_MONITOR_SERVER_SETTINGS_READ_ONLY": "1"},
            clear=False,
        ),
        patch("agent_monitor.auth.set_user_env") as set_user_env,
        patch.object(settings, "get_settings", return_value={"ok": True}),
    ):
        response = settings.save_settings(
            {"updates": {"OPENAI_API_KEY": "user-key"}}, user=user
        )

    set_user_env.assert_called_once()
    assert response == {"ok": True, "settings": {"ok": True}}
