from __future__ import annotations

import unittest
from unittest.mock import patch

from agent_monitor import jobs, sponsored_kimi_client
from agent_monitor.public_kimi_server import PUBLIC_ENGINES, SPONSORED_ENGINES


class SponsoredKimiCodexMainTests(unittest.TestCase):
    def test_codex_is_sponsored_but_never_public_browser_engine(self) -> None:
        self.assertTrue(sponsored_kimi_client.supports_engine("codex"))
        self.assertIn("codex", sponsored_kimi_client.supported_engines())
        self.assertIn("codex", SPONSORED_ENGINES)
        self.assertNotIn("codex", PUBLIC_ENGINES)

    def test_codex_kimi_route_receives_only_loopback_sponsor_marker(self) -> None:
        environment: dict[str, str] = {}
        with (
            patch.object(sponsored_kimi_client, "configured", return_value=True),
            patch.object(
                sponsored_kimi_client,
                "endpoint",
                return_value="http://127.0.0.1:4610",
            ),
        ):
            applied = jobs._apply_sponsored_kimi_route(
                environment,
                engine="codex",
                model="kimi-k3",
            )
        self.assertTrue(applied)
        self.assertEqual(environment["AGENT_MONITOR_SPONSORED_KIMI"], "1")
        self.assertEqual(
            environment["AGENT_MONITOR_SPONSORED_KIMI_URL"],
            "http://127.0.0.1:4610",
        )
        self.assertNotIn("KIMI_API_KEY", environment)


if __name__ == "__main__":
    unittest.main()
