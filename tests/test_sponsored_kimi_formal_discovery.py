from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from agent_monitor import console_server, lean_verify


class SponsoredKimiFormalDiscoveryTests(unittest.TestCase):
    def test_global_no_key_discovery_lists_kimi_formal_harnesses(self) -> None:
        user = {"id": 70, "is_admin": False}
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch("agent_monitor.settings.account_runtime", return_value="api"),
            patch(
                "agent_monitor.settings.get_settings",
                return_value={"api_models": ["kimi-k3"]},
            ),
            patch(
                "agent_monitor.engines_registry._cli_available",
                return_value=(True, "cmd"),
            ),
            patch(
                "agent_monitor.sponsored_kimi_client.configured",
                return_value=True,
            ) as configured,
        ):
            options = lean_verify.harness_model_options(user)
            engines = lean_verify.harness_engines(
                user, model=options["selected"]
            )

        self.assertEqual(options["auth_route"], "api_key")
        self.assertEqual(options["models"], ["kimi-k3"])
        self.assertEqual(options["selected"], "kimi-k3")
        by_id = {item["id"]: item for item in engines}
        for engine in ("codex", "openclaude", "openclaw", "deepagents"):
            self.assertTrue(by_id[engine]["available"])
            self.assertIn("Included Kimi K3", by_id[engine]["auth_detail"])
        self.assertFalse(by_id["openhands"]["available"])
        configured.assert_called_once()

    def test_user_kimi_key_is_preferred_without_sponsored_probe(self) -> None:
        with (
            patch(
                "agent_monitor.settings.resolved_user_env",
                return_value={"KIMI_API_KEY": "user-kimi-key"},
            ),
            patch("agent_monitor.settings.account_runtime", return_value="api"),
            patch(
                "agent_monitor.engines_registry._cli_available",
                return_value=(True, "cmd"),
            ),
            patch(
                "agent_monitor.sponsored_kimi_client.configured"
            ) as configured,
        ):
            engines = lean_verify.harness_engines(
                {"id": 71}, model="kimi-k3"
            )

        codex = next(item for item in engines if item["id"] == "codex")
        self.assertTrue(codex["available"])
        self.assertEqual(codex["auth_detail"], "Kimi K3 API key")
        configured.assert_not_called()


    def test_global_api_models_prefer_the_saved_default(self) -> None:
        user = {"id": 72, "is_admin": False}
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch("agent_monitor.settings.account_runtime", return_value="api"),
            patch(
                "agent_monitor.settings.get_settings",
                return_value={
                    "api_models": ["kimi-k3", "gpt-test"],
                    "default_model": "gpt-test",
                },
            ),
        ):
            options = lean_verify.harness_model_options(user)

    def test_global_status_returns_model_and_engine_catalog_together(self) -> None:
        user = {"id": 70, "is_admin": False}
        model_options = {
            "auth_route": "api_key",
            "models": ["kimi-k3"],
            "selected": "kimi-k3",
        }
        engines = [{"id": "codex", "available": True}]
        handler = object.__new__(console_server.Handler)
        handler.path = "/api/lean/status"
        handler._resolve_path = lambda: "/api/lean/status"
        handler._require_user = lambda _path: user
        sent: list[tuple[int, dict]] = []
        handler._send = lambda code, payload, *args, **kwargs: sent.append(
            (code, json.loads(payload))
        )

        with (
            patch.object(
                lean_verify,
                "toolchain_status",
                return_value={"available": True, "version": "Lean 4.14.0"},
            ),
            patch.object(
                lean_verify,
                "harness_model_options",
                return_value=model_options,
            ) as options,
            patch.object(
                lean_verify,
                "harness_engines",
                return_value=engines,
            ) as catalog,
        ):
            handler.do_GET()

        self.assertEqual(sent[0][0], 200)
        payload = sent[0][1]
        self.assertEqual(payload["harness_auth_route"], "api_key")
        self.assertEqual(payload["harness_models"], ["kimi-k3"])
        self.assertEqual(payload["harness_model"], "kimi-k3")
        self.assertEqual(payload["engines"], engines)
        options.assert_called_once_with(user)
        catalog.assert_called_once_with(user, model="kimi-k3")


if __name__ == "__main__":
    unittest.main()
