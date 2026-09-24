from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_monitor import run_tuning, sponsored_kimi_client
from agent_monitor.public_kimi_server import PUBLIC_ENGINES, SPONSORED_ENGINES


class MathHarnessRoutingTests(unittest.TestCase):
    def test_registry_command_keeps_problem_text_out_of_process_argv(self) -> None:
        from agent_monitor.engines_registry import build_cli_command

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            problem_file = workspace / "problem.txt"
            problem_file.write_text("private mathematical statement", encoding="utf-8")
            argv = build_cli_command(
                "math_harness",
                prompt="SECRET USER PROBLEM AND PERSONA",
                workspace=workspace,
                problem_file=problem_file,
            )

        self.assertIsNotNone(argv)
        assert argv is not None
        rendered = " ".join(argv)
        self.assertNotIn("SECRET USER PROBLEM", rendered)
        self.assertNotIn("private mathematical statement", rendered)
        self.assertTrue(argv[-1].endswith("math_harness_runner.py"))

    def test_api_child_uses_only_the_run_owners_provider_credentials(self) -> None:
        from agent_monitor import jobs
        from agent_monitor.proof_graph import _resolve_llm_provider

        captured_env: dict[str, str] = {}
        real_popen = subprocess.Popen

        def capture_popen(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return real_popen(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            command = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('proof.md').write_text("
                    "'# Partial Progress\\n\\nA bounded routing fixture with enough content to be accepted as an artifact.\\n', "
                    "encoding='utf-8')"
                ),
            ]
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPENAI_API_KEY": "operator-key",
                        "OPENAI_BASE_URL": "https://operator.invalid/v1",
                        "PATH": os.environ.get("PATH", ""),
                    },
                    clear=True,
                ),
                patch(
                    "agent_monitor.engines_registry.build_cli_command",
                    return_value=command,
                ),
                patch("agent_monitor.agent_config.persona_preamble", return_value=""),
                patch.object(jobs, "_write_run"),
                patch("subprocess.Popen", side_effect=capture_popen),
            ):
                result = jobs._run_cli_engine(
                    run_id="math_harness_owned_key",
                    engine="math_harness",
                    problem_id="owned-key",
                    problem_text="Prove the routing fixture.",
                    started=time.time(),
                    workspace=workspace,
                    requested_model="openrouter:openai/gpt-5.2",
                    max_iterations=1,
                    extra_env={
                        "OPENROUTER_API_KEY": "run-owner-key",
                        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
                    },
                )

        self.assertEqual(result["status"], "finished")
        self.assertNotIn("OPENAI_API_KEY", captured_env)
        self.assertNotIn("OPENAI_BASE_URL", captured_env)
        self.assertEqual(captured_env["OPENROUTER_API_KEY"], "run-owner-key")
        provider, base, key, model = _resolve_llm_provider(
            captured_env, "openrouter:openai/gpt-5.2"
        )
        self.assertEqual(
            (provider, base, key, model),
            (
                "openrouter",
                "https://openrouter.ai/api/v1",
                "run-owner-key",
                "openai/gpt-5.2",
            ),
        )

    def test_job_environment_pins_the_server_problem_identity(self) -> None:
        from agent_monitor import jobs

        env: dict[str, str] = {}
        jobs._apply_math_harness_iteration_env(
            env,
            engine="math_harness",
            max_iterations=7,
            problem_id="server-problem-17",
        )
        self.assertEqual(env["AGENT_MONITOR_PROBLEM_ID"], "server-problem-17")

    def test_run_tuning_is_available_on_each_supported_model_route(self) -> None:
        cases = (
            ("gpt-5.6-sol", "codex_subscription", "xhigh", "fast"),
            ("gpt-5.6-sol", "api_key", "xhigh", "fast"),
            ("claude-sonnet-4-6", "claude_subscription", "max", None),
            ("kimi-k3", "api_key", "max", None),
        )

        for model, route, reasoning, speed in cases:
            with self.subTest(model=model, route=route):
                caps = run_tuning.capabilities(model, route, "math_harness")
                reasoning_ids = {item["id"] for item in caps["reasoning"]}
                speed_ids = {item["id"] for item in caps["speed"]}
                self.assertIn(reasoning, reasoning_ids)
                if speed:
                    self.assertIn(speed, speed_ids)
                else:
                    self.assertEqual(speed_ids, {"standard"})
                self.assertIn("math_harness", caps["supported_engines"])

    def test_sponsored_kimi_credential_route_includes_math_harness(self) -> None:
        self.assertTrue(sponsored_kimi_client.supports_engine("math_harness"))
        self.assertIn("math_harness", sponsored_kimi_client.supported_engines())
        self.assertIn("math_harness", SPONSORED_ENGINES)

    def test_anonymous_public_console_stays_plain_only(self) -> None:
        self.assertEqual(set(PUBLIC_ENGINES), {"plain"})
        self.assertNotIn("math_harness", PUBLIC_ENGINES)


if __name__ == "__main__":
    unittest.main()
