from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "public-kimi"


class PublicKimiDeployTests(unittest.TestCase):
    def test_unit_is_separate_hardened_and_credential_bound(self) -> None:
        unit = (DEPLOY / "proving-kimi-public.service.in").read_text(encoding="utf-8")
        self.assertIn("python -P -m agent_monitor.public_kimi_server", unit)
        self.assertIn("PUBLIC_KIMI_PORT=4610", unit)
        self.assertIn("PUBLIC_KIMI_COOKIE_SECURE=1", unit)
        self.assertIn("AGENT_MONITOR_PUBLIC_KIMI=1", unit)
        self.assertIn("AGENT_MONITOR_DISABLE_AUTO_PIPELINE=1", unit)
        self.assertIn("PrivateUsers=no", unit)
        self.assertIn("RuntimeDirectory=proving-kimi-public", unit)
        self.assertIn("PUBLIC_KIMI_DATA_DIR=/run/proving-kimi-public", unit)
        self.assertNotIn("StateDirectory=", unit)
        self.assertIn("LoadCredential=kimi_api_key:", unit)
        self.assertIn("DynamicUser=yes", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ProtectHome=yes", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertRegex(unit, r"(?m)^CapabilityBoundingSet=\s*$")
        self.assertRegex(unit, r"(?m)^AmbientCapabilities=\s*$")
        self.assertNotIn("KIMI_API_KEY=", unit)
        self.assertNotIn("/home/repo/agent_monitor_math/.env", unit)
        self.assertNotIn("agent-monitor.service", unit)

    def test_caddy_allows_only_public_surface_and_preserves_session_cookie(self) -> None:
        caddy = (DEPLOY / "Caddyfile.example").read_text(encoding="utf-8")
        self.assertIn("reverse_proxy 127.0.0.1:4610", caddy)
        self.assertIn("max_size 32KiB", caddy)
        self.assertEqual(caddy.count("buffer_requests"), 2)
        self.assertIn('Strict-Transport-Security "max-age=31536000; includeSubDomains"', caddy)
        self.assertIn("header_up -Authorization", caddy)
        self.assertIn("header_up -X-API-Key", caddy)
        self.assertNotIn("header_up -Cookie", caddy)
        self.assertRegex(caddy, r"(?m)^\s*path /\s*$")
        self.assertNotRegex(caddy, r"(?m)^\s*path / /kimi")
        self.assertIn("path /api/config", caddy)
        self.assertIn("path /api/runs", caddy)
        self.assertIn("/api/runs/[A-Za-z0-9_-]{20,128}", caddy)
        self.assertNotIn("/api/settings", caddy)
        self.assertNotIn("/api/library", caddy)
        self.assertNotIn("/api/lean", caddy)

    def test_installer_is_staging_only_by_default(self) -> None:
        installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        before_activation, marker, after_activation = installer.partition(
            'if [[ ${activate} -eq 0 ]]'
        )
        self.assertIn("mktemp /tmp/proving-kimi-public.XXXXXX.service", before_activation)
        self.assertTrue(marker)
        self.assertNotIn("systemctl restart", before_activation)
        self.assertNotIn("systemctl enable", before_activation)
        self.assertNotIn("systemctl daemon-reload", before_activation)
        self.assertIn("systemctl restart proving-kimi-public.service", after_activation)
        self.assertIn("for command in systemctl curl", after_activation)
        self.assertIn("Public Kimi backend is healthy", after_activation)
        self.assertNotIn("systemctl restart agent-monitor.service", installer)
        self.assertIn(
            "[[ ! -e ${base_target} || ! ${base_source} -ef ${base_target} ]]", installer
        )
        self.assertNotIn("/etc/caddy/Caddyfile", installer)

    def test_readme_matches_implemented_v1(self) -> None:
        readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
        self.assertIn('{"engine": "...", "problem": "..."}', readme)
        self.assertIn('"engine_allowlist"', readme)
        self.assertIn("counters are in memory", readme)
        self.assertRegex(readme, r"not an\s+OS-level credential boundary")
        self.assertNotIn("{harness, problem_text}", readme)
        self.assertNotIn('"harness_allowlist"', readme)
        self.assertNotIn("persistent global daily spend", readme)

    def test_release_manifest_contract_is_exact(self) -> None:
        preparer = (DEPLOY / "prepare_release.py").read_text(encoding="utf-8")
        self.assertIn('value.get("engine_allowlist") != ["plain"]', preparer)
        self.assertIn('value.get("model") != "kimi-k3"', preparer)
        self.assertIn('value.get("auto_pipeline") is not False', preparer)
        self.assertNotIn('"codex"', preparer)


if __name__ == "__main__":
    unittest.main()
