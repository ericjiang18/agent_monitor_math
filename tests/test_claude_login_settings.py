from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import claude_login, console_server, settings


class ClaudeLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        with claude_login._lock:
            claude_login._states.clear()
        with claude_login._cache_lock:
            claude_login._auth_cache.clear()

    def test_account_environment_is_private_and_scrubs_api_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user_8"
            claude_login._ensure_account_home(home)
            env = claude_login.account_environment(
                home,
                {
                    "PATH": "/bin",
                    "ANTHROPIC_API_KEY": "secret",
                    "ANTHROPIC_AUTH_TOKEN": "token",
                    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-access",
                    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "oauth-refresh",
                    "CLAUDE_CODE_OAUTH_SCOPES": "user:inference",
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    "AWS_ACCESS_KEY_ID": "aws",
                },
            )
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(home))
            self.assertEqual(env["HOME"], str(home))
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_SCOPES", env)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
            self.assertNotIn("AWS_ACCESS_KEY_ID", env)

    def test_auth_status_whitelists_metadata_and_never_returns_tokens(self) -> None:
        raw = {
            "loggedIn": True,
            "authMethod": "oauth",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
            "accessToken": "must-not-leak",
        }
        completed = SimpleNamespace(stdout=json.dumps(raw), returncode=0)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(claude_login, "claude_binary", return_value="/bin/claude"),
            patch.object(claude_login.subprocess, "run", return_value=completed),
        ):
            result = claude_login._run_auth_status(Path(tmp))
        self.assertTrue(result["loggedIn"])
        self.assertEqual(result["subscriptionType"], "max")
        self.assertNotIn("accessToken", result)
        self.assertNotIn("must-not-leak", json.dumps(result))

    def test_api_key_auth_is_not_treated_as_subscription(self) -> None:
        with patch.object(
            claude_login,
            "_auth_status",
            return_value={"loggedIn": True, "authMethod": "apiKey"},
        ):
            self.assertFalse(claude_login.account_login_ready(Path("/tmp/claude")))

    def test_access_expiry_does_not_override_a_valid_refresh_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "expired-but-normal",
                            "expiresAt": 1_700_000_000_000,
                            "refreshToken": "must-not-leak",
                            "refreshTokenExpiresAt": 1_900_000_000_000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(
                    claude_login,
                    "_auth_status",
                    return_value={"loggedIn": True, "authMethod": "oauth"},
                ),
                patch.object(claude_login.time, "time", return_value=1_800_000_000.0),
            ):
                self.assertTrue(claude_login.account_login_ready(home))

    def test_final_oauth_failure_marker_is_fail_closed_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "must-not-leak",
                            "expiresAt": 1_700_000_000_000,
                            "refreshToken": "also-secret",
                            "refreshTokenExpiresAt": 1_900_000_000_000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            auth = {
                "loggedIn": True,
                "authMethod": "oauth",
                "apiProvider": "firstParty",
                "subscriptionType": "max",
            }
            with (
                patch.object(claude_login, "account_home", return_value=home),
                patch.object(claude_login, "_auth_status", return_value=auth),
                patch.object(claude_login, "claude_binary", return_value="/bin/claude"),
                patch.object(claude_login.time, "time", return_value=1_800_000_000.0),
            ):
                claude_login.mark_reauth_required(
                    home, reason="oauth_refresh_failed"
                )
                self.assertFalse(claude_login.account_login_ready(home))
                result = claude_login.status(7)
                marker = home / claude_login._REAUTH_MARKER_NAME
                self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
                marker_payload = marker.read_text(encoding="utf-8")
                claude_login.clear_reauth_required(home)
                self.assertTrue(claude_login.account_login_ready(home))
        self.assertFalse(result["connected"])
        self.assertTrue(result["reauth_required"])
        self.assertTrue(result["auth_expired"])
        self.assertEqual(result["models"], [])
        self.assertNotIn("expiresAt", result)
        self.assertNotIn("must-not-leak", json.dumps(result))
        self.assertNotIn("also-secret", json.dumps(result))
        self.assertNotIn("must-not-leak", marker_payload)
        self.assertNotIn("also-secret", marker_payload)

    def test_successful_official_login_clears_reauth_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "refreshToken": "secret",
                            "refreshTokenExpiresAt": 1_900_000_000_000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            auth = {"loggedIn": True, "authMethod": "oauth"}
            proc = SimpleNamespace(stdout=[], wait=lambda timeout: 0)
            with (
                patch.object(claude_login, "_auth_status", return_value=auth),
                patch.object(claude_login.time, "time", return_value=1_800_000_000.0),
            ):
                claude_login.mark_reauth_required(home)
                with claude_login._lock:
                    claude_login._states[7] = {"status": "running", "running": True}
                claude_login._reader(proc, home, 7)
            self.assertFalse((home / claude_login._REAUTH_MARKER_NAME).exists())
            with claude_login._lock:
                self.assertEqual(claude_login._states[7]["status"], "success")

    def test_expired_refresh_token_is_fail_closed_without_model_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "refreshToken": "secret",
                            "refreshTokenExpiresAt": 1_700_000_000_000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(
                    claude_login,
                    "_auth_status",
                    return_value={"loggedIn": True, "authMethod": "oauth"},
                ),
                patch.object(claude_login.time, "time", return_value=1_800_000_000.0),
            ):
                self.assertFalse(claude_login.account_login_ready(home))

    def test_malformed_refresh_metadata_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text("not json", encoding="utf-8")
            with patch.object(
                claude_login,
                "_auth_status",
                return_value={"loggedIn": True, "authMethod": "oauth"},
            ):
                self.assertFalse(claude_login.account_login_ready(home))

    def test_oauth_code_is_only_written_to_stdin(self) -> None:
        stdin = io.StringIO()
        proc = SimpleNamespace(stdin=stdin)
        with claude_login._lock:
            claude_login._states[17] = {
                "status": "running",
                "running": True,
                "proc": proc,
                "output": "should-not-leak",
            }
        with patch.object(
            claude_login,
            "_augment",
            side_effect=lambda _uid, snapshot: claude_login._public_state(snapshot),
        ):
            result = claude_login.submit_code(17, "one-time-secret")
        self.assertEqual(stdin.getvalue(), "one-time-secret\n")
        self.assertNotIn("one-time-secret", json.dumps(result))
        self.assertNotIn("output", result)
        with claude_login._lock:
            self.assertNotIn("one-time-secret", json.dumps(claude_login._states[17], default=str))


class ClaudeAccountRuntimeSettingsTests(unittest.TestCase):
    def test_account_runtime_is_mutually_exclusive_and_legacy_compatible(self) -> None:
        self.assertEqual(settings.account_runtime(env={"AGENT_MONITOR_USE_CODEX": "0"}), "api")
        self.assertEqual(settings.account_runtime(env={"AGENT_MONITOR_USE_CODEX": "1"}), "codex")
        self.assertEqual(settings.account_runtime(env={"AGENT_MONITOR_USE_CLAUDE": "1"}), "claude")
        env = {
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
            "AGENT_MONITOR_USE_CODEX": "1",
            "AGENT_MONITOR_USE_CLAUDE": "1",
        }
        self.assertEqual(settings.account_runtime(env=env), "api")
        self.assertFalse(settings.codex_enabled(env=env))
        self.assertFalse(settings.claude_enabled(env=env))

    def test_claude_runtime_exposes_only_account_aliases_for_claude(self) -> None:
        env = {"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"}
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch("agent_monitor.codex_login.account_login_ready", return_value=True),
            patch("agent_monitor.codex_login.available_models", return_value=["gpt-account"]),
            patch("agent_monitor.claude_login.account_login_ready", return_value=True),
        ):
            result = settings.get_settings({"id": 7, "is_admin": False})
        self.assertEqual(result["account_runtime"], "claude")
        self.assertTrue(result["claude_enabled"])
        self.assertFalse(result["codex_enabled"])
        self.assertTrue(result["claude_connected"])
        self.assertEqual(
            result["claude_models"],
            ["sonnet", "opus", "fable", "claude-haiku-4-5"],
        )
        self.assertEqual(result["codex_models"], ["gpt-account"])
        self.assertEqual(result["model_presets"], result["claude_models"])
        self.assertEqual(result["models_by_runtime"]["codex"], ["gpt-account"])
        self.assertFalse(result["api_providers_active"])
        self.assertEqual(result["auth_mode"], "claude_subscription")

    def test_expired_claude_runtime_stays_selected_but_not_ready(self) -> None:
        env = {"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"}
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch("agent_monitor.codex_login.account_login_ready", return_value=False),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
            patch(
                "agent_monitor.claude_login.account_reauth_required",
                return_value=True,
            ),
        ):
            result = settings.get_settings({"id": 7, "is_admin": False})
        self.assertEqual(result["account_runtime"], "claude")
        self.assertEqual(result["auth_mode"], "claude_subscription")
        self.assertFalse(result["runtime_ready"])
        self.assertFalse(result["claude_connected"])
        self.assertTrue(result["claude_reauth_required"])
        self.assertEqual(result["claude_models"], [])
        self.assertEqual(result["model_presets"], [])

    def test_save_runtime_writes_mutually_exclusive_flags(self) -> None:
        with (
            patch("agent_monitor.auth.set_user_env") as set_env,
            patch.object(settings, "get_settings", return_value={"account_runtime": "claude"}),
        ):
            settings.save_settings({"account_runtime": "claude"}, user={"id": 9})
        set_env.assert_called_once_with(
            9,
            {
                "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
                "AGENT_MONITOR_USE_CODEX": "0",
                "AGENT_MONITOR_USE_CLAUDE": "1",
            },
            clear=[],
        )

    def test_conflicting_legacy_toggles_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot both"):
            settings.save_settings({"codex_enabled": True, "claude_enabled": True}, user={"id": 9})


class ClaudeSettingsRouteTests(unittest.TestCase):
    def test_code_endpoint_uses_authenticated_user_not_body_user_id(self) -> None:
        body = json.dumps({"user_id": 999, "code": "oauth-once"}).encode()
        handler = object.__new__(console_server.Handler)
        handler.path = "/api/settings/claude/login/code"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._resolve_path = lambda: "/api/settings/claude/login/code"
        handler._require_user = lambda _path: {"id": 42, "is_admin": False}
        sent: list[tuple[int, dict]] = []
        handler._send = lambda code, payload, *args, **kwargs: sent.append((code, json.loads(payload)))
        with patch.object(claude_login, "submit_code", return_value={"status": "running"}) as submit:
            handler.do_POST()
        submit.assert_called_once_with(42, "oauth-once")
        self.assertEqual(sent[0][0], 200)


if __name__ == "__main__":
    unittest.main()
