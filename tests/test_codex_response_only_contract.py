from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_monitor.runners import codex_backend


class CodexResponseOnlyContractTests(unittest.TestCase):
    def test_subscription_turn_disables_tools_and_uses_empty_backend_cwd(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION + "\n",
                    stderr="",
                )
            if command[1:] == ["debug", "models", "--bundled"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "models": [
                                {
                                    "slug": "gpt-5.6-sol",
                                    "apply_patch_tool_type": "freeform",
                                }
                            ]
                        }
                    ),
                    stderr="",
                )
            captured["command"] = list(command)
            captured["cwd"] = kwargs.get("cwd")
            captured["env"] = dict(kwargs.get("env") or {})
            config = next(
                token
                for token in command
                if token.startswith("model_catalog_json=")
            )
            catalog_path = Path(json.loads(config.split("=", 1)[1]))
            captured["catalog"] = json.loads(catalog_path.read_text(encoding="utf-8"))
            captured["catalog_mode"] = stat.S_IMODE(catalog_path.stat().st_mode)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text("A response-only proof result.", encoding="utf-8")
            event = json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            )
            return subprocess.CompletedProcess(command, 0, stdout=event, stderr="")

        with tempfile.TemporaryDirectory() as exposed:
            with (
                patch.object(
                    codex_backend.shutil, "which", return_value="/bin/true"
                ),
                patch.object(
                    codex_backend, "_run_codex_process", side_effect=fake_run
                ),
            ):
                result = codex_backend.codex_exec(
                    "system",
                    "user",
                    model="gpt-5.6-sol",
                    workspace=exposed,
                    enable_tools=False,
                    source_env={
                        "CODEX_HOME": "/tmp/account-codex-home",
                        "PATH": "/usr/bin:/bin",
                        "OPENAI_API_KEY": "must-not-leak",
                        "ANTHROPIC_API_KEY": "must-not-leak",
                        "KIMI_API_KEY": "must-not-leak",
                    },
                )

        command = captured["command"]
        assert isinstance(command, list)
        disabled = {
            command[index + 1]
            for index, token in enumerate(command[:-1])
            if token == "--disable"
        }
        self.assertTrue(
            {
                "apps",
                "artifact",
                "auth_elicitation",
                "browser_use",
                "browser_use_external",
                "browser_use_full_cdp_access",
                "code_mode",
                "code_mode_host",
                "computer_use",
                "enable_mcp_apps",
                "goals",
                "hooks",
                "image_generation",
                "in_app_browser",
                "memories",
                "multi_agent",
                "network_proxy",
                "plugins",
                "plugin_sharing",
                "remote_plugin",
                "request_permissions_tool",
                "skill_mcp_dependency_install",
                "skill_search",
                "standalone_web_search",
                "tool_call_mcp_elicitation",
                "tool_suggest",
                "view_image",
                "workspace_dependencies",
                "shell_tool",
                "unified_exec",
            }.issubset(disabled)
        )
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--strict-config", command)
        self.assertIn('web_search="disabled"', command)
        self.assertIn("tools.web_search=false", command)
        self.assertIn("tools.update_plan.enabled=false", command)
        self.assertIn("tools.experimental_request_user_input.enabled=false", command)
        self.assertNotIn("--cd", command)
        self.assertNotEqual(captured["cwd"], exposed)
        self.assertFalse(Path(str(captured["cwd"])).exists())
        child_env = captured["env"]
        assert isinstance(child_env, dict)
        self.assertFalse(
            any(
                str(name).endswith(("_API_KEY", "_API_KEYS", "_AUTH_TOKEN"))
                for name in child_env
            )
        )
        self.assertEqual(captured["catalog_mode"], 0o600)
        self.assertEqual(
            captured["catalog"],
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "include_apps_usage_instructions": False,
                        "include_plugin_usage_instructions": False,
                        "include_skills_usage_instructions": False,
                        "supports_search_tool": False,
                        "supports_parallel_tool_calls": False,
                    }
                ]
            },
        )
        self.assertEqual(codex_backend.RESPONSE_ONLY_TOOL_ISOLATION_VERSION, 4)
        self.assertEqual(result.text, "A response-only proof result.")

    def test_installed_cli_sends_zero_tools_to_loopback_provider(self) -> None:
        codex = shutil.which("codex")
        if not codex:
            self.skipTest("installed Codex CLI is unavailable")
        captured: list[dict] = []
        captured_paths: list[str] = []
        received = threading.Event()

        class CaptureHandler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                try:
                    captured_paths.append(self.path)
                    size = int(self.headers.get("Content-Length") or "0")
                    payload = json.loads(self.rfile.read(size))
                    if isinstance(payload, dict):
                        captured.append(payload)
                finally:
                    received.set()
                body = json.dumps(
                    {"error": {"message": "offline capture complete"}}
                ).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        original_args = codex_backend._response_only_args
        base_url = f"http://127.0.0.1:{server.server_port}/v1"

        def capture_provider_args(*, enable_tools: bool) -> list[str]:
            return original_args(enable_tools=enable_tools) + [
                "-c",
                'model_provider="capture"',
                "-c",
                'model_providers.capture.name="Offline capture"',
                "-c",
                "model_providers.capture.base_url=" + json.dumps(base_url),
                "-c",
                'model_providers.capture.env_key="CODEX_HOME"',
                "-c",
                'model_providers.capture.wire_api="responses"',
                "-c",
                "model_providers.capture.requires_openai_auth=false",
            ]

        try:
            with tempfile.TemporaryDirectory() as account_home:
                with patch.object(
                    codex_backend,
                    "_response_only_args",
                    side_effect=capture_provider_args,
                ):
                    with self.assertRaises(codex_backend.CodexBackendError):
                        codex_backend.codex_exec(
                            "Return one short sentence.",
                            "Say that this is an offline transport test.",
                            model="gpt-5.6-sol",
                            timeout=20,
                            source_env={
                                "CODEX_HOME": account_home,
                                "PATH": os.environ.get("PATH", os.defpath),
                                "AGENT_MONITOR_CODEX_LEGACY_LANDLOCK": "0",
                            },
                        )
            self.assertTrue(received.wait(2), "Codex sent no loopback request")
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertTrue(captured)
        self.assertEqual(captured_paths[0], "/v1/responses")
        self.assertFalse(captured[0].get("tools"))
        self.assertIs(captured[0].get("parallel_tool_calls"), False)

    def test_response_only_mode_rejects_an_unvetted_cli_version(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(command))
            return subprocess.CompletedProcess(
                command, 0, stdout="codex-cli 999.0.0\n", stderr=""
            )

        with (
            patch.object(codex_backend.shutil, "which", return_value="/bin/true"),
            patch.object(codex_backend, "_run_codex_process", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(
                codex_backend.CodexBackendError, "requires vetted CLI"
            ):
                codex_backend.codex_exec(
                    "system",
                    "user",
                    model="gpt-5.6-sol",
                    source_env={"CODEX_HOME": "/tmp/account-codex-home"},
                )
        self.assertEqual(calls, [[str(Path("/bin/true").resolve()), "--version"]])

    def test_response_only_mode_rejects_model_missing_from_catalog(self) -> None:
        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION + "\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"models": [{"slug": "gpt-5.6-sol"}]}),
                stderr="",
            )

        with (
            patch.object(codex_backend.shutil, "which", return_value="/bin/true"),
            patch.object(codex_backend, "_run_codex_process", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(
                codex_backend.CodexBackendError, "not in the vetted bundled catalog"
            ):
                codex_backend.codex_exec(
                    "system",
                    "user",
                    model="unknown-model",
                    source_env={"CODEX_HOME": "/tmp/account-codex-home"},
                )


if __name__ == "__main__":
    unittest.main()
