from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor.runners import deepagents_runner


class KimiDeepAgentsTests(unittest.TestCase):
    def test_kimi_model_detection_is_exact(self) -> None:
        self.assertTrue(deepagents_runner._is_kimi_api_model("kimi-k3"))
        self.assertTrue(deepagents_runner._is_kimi_api_model("openai:kimi-k3"))
        self.assertTrue(deepagents_runner._is_kimi_api_model("kimi/kimi-k3"))
        self.assertFalse(deepagents_runner._is_kimi_api_model("kimi-k2.6"))
        self.assertFalse(deepagents_runner._is_kimi_api_model("gpt-5.6-sol"))

    def test_kimi_chat_model_has_explicit_endpoint_budget_and_reasoning(self) -> None:
        captured: dict = {}

        class FakeChatOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        module = types.ModuleType("langchain_openai")
        module.ChatOpenAI = FakeChatOpenAI
        with (
            patch.dict(
                os.environ,
                {
                    "KIMI_API_KEY": "test-only-kimi-key",
                    "KIMI_API_BASE": "https://kimi.example/v1/",
                    "KIMI_REASONING_EFFORT": "high",
                    "AGENT_MONITOR_DEEPAGENTS_MAX_TOKENS": "32768",
                },
                clear=True,
            ),
            patch.dict(sys.modules, {"langchain_openai": module}),
        ):
            model = deepagents_runner._create_kimi_api_model("openai:kimi-k3")

        self.assertIsInstance(model, FakeChatOpenAI)
        self.assertEqual(captured["model"], "kimi-k3")
        self.assertEqual(captured["api_key"], "test-only-kimi-key")
        self.assertEqual(captured["base_url"], "https://kimi.example/v1")
        self.assertEqual(captured["reasoning_effort"], "high")
        self.assertEqual(captured["max_completion_tokens"], 32768)
        self.assertFalse(captured["use_responses_api"])
        self.assertFalse(captured["streaming"])

    def test_kimi_chat_model_requires_its_own_key(self) -> None:
        module = types.ModuleType("langchain_openai")
        module.ChatOpenAI = object
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.dict(sys.modules, {"langchain_openai": module}),
        ):
            with self.assertRaisesRegex(RuntimeError, "KIMI_API_KEY"):
                deepagents_runner._create_kimi_api_model("kimi-k3")

    def test_kimi_max_tokens_is_bounded_and_has_safe_default(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENT_MONITOR_DEEPAGENTS_MAX_TOKENS": "invalid"},
            clear=True,
        ):
            self.assertEqual(
                deepagents_runner._kimi_max_completion_tokens(), 32768
            )
        with patch.dict(
            os.environ,
            {"AGENT_MONITOR_DEEPAGENTS_MAX_TOKENS": "10"},
            clear=True,
        ):
            self.assertEqual(deepagents_runner._kimi_max_completion_tokens(), 1024)
        with patch.dict(
            os.environ,
            {"AGENT_MONITOR_DEEPAGENTS_MAX_TOKENS": "999999"},
            clear=True,
        ):
            self.assertEqual(
                deepagents_runner._kimi_max_completion_tokens(), 128000
            )

    @staticmethod
    def _fake_deepagents_modules(agent) -> dict[str, types.ModuleType]:
        deepagents = types.ModuleType("deepagents")
        deepagents.create_deep_agent = lambda **_kwargs: agent
        backends = types.ModuleType("deepagents.backends")
        filesystem = types.ModuleType("deepagents.backends.filesystem")

        class FilesystemBackend:
            def __init__(self, **_kwargs):
                pass

        filesystem.FilesystemBackend = FilesystemBackend
        return {
            "deepagents": deepagents,
            "deepagents.backends": backends,
            "deepagents.backends.filesystem": filesystem,
        }

    def test_main_returns_nonzero_when_no_proof_artifact_exists(self) -> None:
        class EmptyAgent:
            def stream(self, *_args, **_kwargs):
                yield {"messages": [], "files": {}}

        errors: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                with (
                    patch.dict(
                        os.environ,
                        {"DEEPAGENTS_MODEL": "openai:test-model"},
                        clear=True,
                    ),
                    patch.dict(
                        sys.modules,
                        self._fake_deepagents_modules(EmptyAgent()),
                    ),
                    patch.object(sys, "argv", ["deepagents_runner.py", "prove 1=1"]),
                    patch.object(deepagents_runner, "_install_termination_handlers"),
                    patch.object(deepagents_runner, "_subagents_enabled", return_value=True),
                    patch.object(deepagents_runner, "_load_user_tools", return_value=[]),
                    patch.object(deepagents_runner, "_run_supplemental_audit", return_value=False),
                    patch.object(
                        deepagents_runner,
                        "emit_item",
                        side_effect=lambda item: errors.append(str(item.get("message") or "")),
                    ),
                ):
                    rc = deepagents_runner.main()
            finally:
                os.chdir(previous)

        self.assertEqual(rc, 1)
        self.assertTrue(any("without a non-empty proof" in item for item in errors))

    def test_main_persists_substantive_final_text_as_proof(self) -> None:
        AIMessage = type("AIMessage", (), {})
        message = AIMessage()
        message.id = "final"
        message.content = "# Proof\n\n" + ("A rigorous proof sentence. " * 4)
        message.usage_metadata = {"input_tokens": 4, "output_tokens": 8}
        message.tool_calls = []

        class FinalAgent:
            def stream(self, *_args, **_kwargs):
                yield {"messages": [message], "files": {}}

        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                with (
                    patch.dict(
                        os.environ,
                        {"DEEPAGENTS_MODEL": "openai:test-model"},
                        clear=True,
                    ),
                    patch.dict(
                        sys.modules,
                        self._fake_deepagents_modules(FinalAgent()),
                    ),
                    patch.object(sys, "argv", ["deepagents_runner.py", "prove 1=1"]),
                    patch.object(deepagents_runner, "_install_termination_handlers"),
                    patch.object(deepagents_runner, "_subagents_enabled", return_value=True),
                    patch.object(deepagents_runner, "_load_user_tools", return_value=[]),
                    patch.object(deepagents_runner, "_run_supplemental_audit", return_value=False),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    rc = deepagents_runner.main()
                proof = Path("proof.md").read_text(encoding="utf-8")
            finally:
                os.chdir(previous)

        self.assertEqual(rc, 0)
        self.assertTrue(proof.startswith("# Proof"))
        self.assertGreaterEqual(len(proof.encode("utf-8")), 40)


if __name__ == "__main__":
    unittest.main()
