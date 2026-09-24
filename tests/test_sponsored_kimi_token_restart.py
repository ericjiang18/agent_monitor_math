from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agent_monitor import sponsored_kimi_client


class SponsoredKimiTokenRestartTests(unittest.TestCase):
    def tearDown(self) -> None:
        with sponsored_kimi_client._credential_lock:
            sponsored_kimi_client._credential_cache.clear()

    def test_cached_token_is_reissued_after_service_loses_its_session(self) -> None:
        calls: list[str] = []
        issued = iter(("a" * 64, "b" * 64))

        def request(path: str, **_kwargs):
            calls.append(path)
            if path == "/internal/v1/models":
                raise sponsored_kimi_client.SponsoredKimiError(
                    "Sponsored Kimi session expired"
                )
            token = next(issued)
            return (
                {
                    "api_token": token,
                    "base_url": "http://127.0.0.1:4610/internal/v1",
                    "model": "kimi-k3",
                    "engine": "formal",
                    "expires_in": 7200,
                },
                "",
            )

        with (
            patch.dict(
                os.environ,
                {"AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610"},
                clear=False,
            ),
            patch.object(sponsored_kimi_client, "_request", side_effect=request),
        ):
            first = sponsored_kimi_client.issue_credentials(
                engine="formal", client_id=70, cache_key="restart-run"
            )
            second = sponsored_kimi_client.issue_credentials(
                engine="formal", client_id=70, cache_key="restart-run"
            )

        self.assertEqual(first["KIMI_API_KEY"], "a" * 64)
        self.assertEqual(second["KIMI_API_KEY"], "b" * 64)
        self.assertEqual(
            calls,
            ["/internal/sessions", "/internal/v1/models", "/internal/sessions"],
        )


if __name__ == "__main__":
    unittest.main()
