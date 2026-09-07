from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor.runners import openclaw_runner


class KimiOpenClawRuntimeTests(unittest.TestCase):
    def test_outcome_footer_accepts_sentence_punctuation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Result\n\nA complete proof.\n\n"
                "ProvingConsole outcome: Solved.\n",
                encoding="utf-8",
            )

            self.assertTrue(
                openclaw_runner._normalize_requested_outcome(
                    workspace,
                    "End with ProvingConsole outcome: <label>.",
                )
            )

            saved = proof.read_text(encoding="utf-8")
            self.assertEqual(saved.count("ProvingConsole outcome:"), 1)
            self.assertTrue(
                saved.endswith("ProvingConsole outcome: Solved\n")
            )

    def test_outcome_footer_removes_duplicates_and_fails_conflicts_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Result\n\nA candidate.\n\n"
                "ProvingConsole outcome: Solved.\n\n"
                "**ProvingConsole outcome: Counterexample!**\n\n"
                "ProvingConsole outcome: Solved\n",
                encoding="utf-8",
            )

            openclaw_runner._normalize_requested_outcome(
                workspace,
                "End with ProvingConsole outcome: <label>.",
            )

            saved = proof.read_text(encoding="utf-8")
            self.assertEqual(saved.count("ProvingConsole outcome:"), 1)
            self.assertNotIn("Counterexample", saved)
            self.assertTrue(
                saved.endswith("ProvingConsole outcome: Partial Progress\n")
            )

    def test_outcome_footer_without_verdict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Result\n\nAn incomplete but substantive candidate argument "
                "with no declared outcome.\n",
                encoding="utf-8",
            )

            openclaw_runner._normalize_requested_outcome(
                workspace,
                "End with ProvingConsole outcome: <label>.",
            )

            saved = proof.read_text(encoding="utf-8")
            self.assertEqual(saved.count("ProvingConsole outcome:"), 1)
            self.assertTrue(
                saved.endswith("ProvingConsole outcome: Partial Progress\n")
            )

    def test_kimi_model_detection_does_not_capture_other_models(self) -> None:
        self.assertTrue(openclaw_runner._is_kimi_api_model("openai/kimi-k3"))
        self.assertTrue(openclaw_runner._is_kimi_api_model("kimi:kimi-k3"))
        self.assertFalse(openclaw_runner._is_kimi_api_model("openai/gpt-5.5"))
        self.assertFalse(openclaw_runner._is_kimi_api_model("kimi-k2.6"))

    def test_runtime_uses_disposable_env_referenced_provider_config(self) -> None:
        fake_key = "test-only-kimi-key-never-persist"
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {
                "KIMI_API_KEY": fake_key,
                "KIMI_API_BASE": "https://kimi.test.example/v1/",
                "KIMI_REASONING_EFFORT": "high",
            },
            clear=True,
        ):
            workspace = Path(td)
            with openclaw_runner._openclaw_api_model_runtime(
                workspace, "openai/kimi-k3"
            ) as selected:
                state_dir = Path(os.environ["OPENCLAW_STATE_DIR"])
                config_path = state_dir / "openclaw.json"
                raw = config_path.read_text(encoding="utf-8")
                config = json.loads(raw)
                provider = config["models"]["providers"]["flashflame"]

                self.assertEqual(selected, "flashflame/kimi-k3")
                self.assertEqual(provider["api"], "openai-completions")
                self.assertEqual(provider["apiKey"], "${KIMI_API_KEY}")
                self.assertEqual(
                    provider["baseUrl"], "https://kimi.test.example/v1"
                )
                self.assertEqual(provider["models"][0]["id"], "kimi-k3")
                self.assertEqual(provider["models"][0]["maxTokens"], 32_768)
                self.assertEqual(
                    config["agents"]["defaults"]["models"]
                    ["flashflame/kimi-k3"]["params"]["extra_body"]
                    ["reasoning_effort"],
                    "high",
                )
                self.assertNotIn(fake_key, raw)
                self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
                self.assertIn("OPENCLAW_GATEWAY_TOKEN", os.environ)

            self.assertFalse(state_dir.exists())
            self.assertNotIn("OPENCLAW_STATE_DIR", os.environ)
            self.assertNotIn("OPENCLAW_GATEWAY_TOKEN", os.environ)

    def test_non_kimi_runtime_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {}, clear=True
        ):
            with openclaw_runner._openclaw_api_model_runtime(
                Path(td), "openai/gpt-5.5"
            ) as selected:
                self.assertEqual(selected, "openai/gpt-5.5")
                self.assertNotIn("OPENCLAW_STATE_DIR", os.environ)

    def test_main_routes_kimi_through_custom_provider(self) -> None:
        captured: dict[str, object] = {}

        def fake_call(argv: list[str]) -> int:
            captured["argv"] = argv
            config_path = Path(os.environ["OPENCLAW_STATE_DIR"]) / "openclaw.json"
            captured["config"] = json.loads(config_path.read_text(encoding="utf-8"))
            return 0

        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {
                "KIMI_API_KEY": "test-only-kimi-key-never-persist",
                "KIMI_API_BASE": "https://kimi.test.example/v1",
                "AGENT_MONITOR_OPENCLAW_MODEL": "openai/kimi-k3",
            },
            clear=True,
        ), patch.object(sys, "argv", ["openclaw_runner.py", "prove", "run-1"]), patch.object(
            openclaw_runner.shutil, "which", return_value="/fake/openclaw"
        ), patch.object(
            openclaw_runner.subprocess, "call", side_effect=fake_call
        ), patch.object(
            openclaw_runner, "_normalize_requested_outcome"
        ), patch.object(
            openclaw_runner, "_run_supplemental_audit"
        ):
            previous = Path.cwd()
            os.chdir(td)
            try:
                self.assertEqual(openclaw_runner.main(), 0)
            finally:
                os.chdir(previous)

        argv = captured["argv"]
        assert isinstance(argv, list)
        self.assertEqual(argv[argv.index("--model") + 1], "flashflame/kimi-k3")
        config = captured["config"]
        assert isinstance(config, dict)
        self.assertEqual(
            config["models"]["providers"]["flashflame"]["api"],
            "openai-completions",
        )
        self.assertNotIn("OPENCLAW_STATE_DIR", os.environ)


if __name__ == "__main__":
    unittest.main()
