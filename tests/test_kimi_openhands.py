from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent_monitor.runners import openhands_subscription_runner as runner


def _proof(*, references: str | None = "- self-derived — elementary argument") -> str:
    text = (
        "# Proof\n\nWe prove the claim by contradiction. Assume the contrary and "
        "derive an impossible divisibility conclusion, so the original claim "
        "follows. Every inference is justified by the preceding hypotheses, "
        "and the contradiction eliminates the only alternative. Therefore the "
        "stated conclusion holds. This completes the rigorous argument.\n"
    )
    if references is not None:
        text += "\n## References\n\n" + references + "\n"
    return text


class KimiOpenHandsTests(unittest.TestCase):
    @staticmethod
    def _fake_conversation(
        run_effect,
        conversations: list[object],
    ):
        class FakeConversation:
            def __init__(self) -> None:
                self.messages: list[str] = []
                self.run_count = 0

            def send_message(self, message: str) -> None:
                self.messages.append(message)

            def run(self) -> None:
                self.run_count += 1
                run_effect(self.run_count)

        @contextmanager
        def fake_context(_workspace: Path):
            conversation = FakeConversation()
            conversations.append(conversation)
            yield conversation

        return fake_context

    def test_model_detection_is_scoped_to_kimi_k3(self) -> None:
        self.assertTrue(runner._is_kimi_api_model("openai/kimi-k3"))
        self.assertTrue(runner._is_kimi_api_model("openai:kimi-k3"))
        self.assertFalse(runner._is_kimi_api_model("openai/gpt-5.5"))
        self.assertFalse(runner._is_kimi_api_model("kimi-k2.6"))

    def test_reference_contract_matches_body_and_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proof = Path(td) / "proof.md"
            proof.write_text(
                _proof(references="[1] Euclid's lemma — divisibility")
                .replace("derive an impossible", "use Euclid's lemma [1] and derive an impossible"),
                encoding="utf-8",
            )
            self.assertEqual(runner._proof_reference_issues(proof), [])

            proof.write_text(_proof(references=None), encoding="utf-8")
            self.assertIn("no References heading", runner._proof_reference_issues(proof)[0])

            proof.write_text(
                _proof(references="[2] Unused source — not cited"),
                encoding="utf-8",
            )
            self.assertTrue(
                any(
                    "uncited reference entries: [2]" in issue
                    for issue in runner._proof_reference_issues(proof)
                )
            )

    def test_clean_first_run_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            conversations: list[object] = []

            def complete(_run_count: int) -> None:
                (workspace / "proof.md").write_text(_proof(), encoding="utf-8")

            with patch.object(
                runner,
                "_kimi_sdk_conversation",
                self._fake_conversation(complete, conversations),
            ):
                self.assertEqual(runner._run_kimi_native_api("prove", workspace), 0)
            self.assertEqual(len(conversations), 1)
            conversation = conversations[0]
            self.assertEqual(conversation.run_count, 1)
            self.assertEqual(len(conversation.messages), 1)
            self.assertIn("root\n  file ./proof.md", conversation.messages[0])
            self.assertNotIn("security_risk field", conversation.messages[0])

    def test_missing_references_get_exactly_one_bounded_repair(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            conversations: list[object] = []

            def complete(_run_count: int) -> None:
                (workspace / "proof.md").write_text(
                    _proof(references=None), encoding="utf-8"
                )

            def repair(*, prompt: str, workspace: Path, issues: list[str]):
                self.assertEqual(prompt, "prove")
                self.assertTrue(
                    any("no References heading" in issue for issue in issues)
                )
                (workspace / "proof.md").write_text(_proof(), encoding="utf-8")
                return {"input_tokens": 10, "output_tokens": 20}

            with patch.object(
                runner,
                "_kimi_sdk_conversation",
                self._fake_conversation(complete, conversations),
            ), patch.object(
                runner, "_write_kimi_artifact_repair", side_effect=repair
            ) as repair_call, patch.object(runner, "emit_item") as emit, patch.object(
                runner, "emit"
            ) as emit_raw:
                self.assertEqual(runner._run_kimi_native_api("prove", workspace), 0)

            self.assertEqual(len(conversations), 1)
            conversation = conversations[0]
            self.assertEqual(conversation.run_count, 1)
            self.assertEqual(len(conversation.messages), 1)
            repair_call.assert_called_once()
            self.assertIn("tool-free artifact continuation", emit.call_args_list[0].args[0]["text"])
            self.assertEqual(
                emit_raw.call_args.args[0]["usage"],
                {"input_tokens": 10, "output_tokens": 20},
            )

    def test_failed_repair_returns_clear_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            conversations: list[object] = []

            def incomplete(_run_count: int) -> None:
                (workspace / "proof.md").write_text(
                    _proof(references=None), encoding="utf-8"
                )

            with patch.object(
                runner,
                "_kimi_sdk_conversation",
                self._fake_conversation(incomplete, conversations),
            ), patch.object(
                runner,
                "_write_kimi_artifact_repair",
                side_effect=RuntimeError("continuation failed"),
            ), patch.object(runner, "emit_item") as emit:
                self.assertEqual(runner._run_kimi_native_api("prove", workspace), 1)

            self.assertEqual(len(conversations), 1)
            self.assertEqual(conversations[0].run_count, 1)
            error = emit.call_args_list[-1].args[0]
            self.assertEqual(error["type"], "error")
            self.assertIn("after one bounded repair", error["message"])
            self.assertIn("continuation failed", error["message"])

    def test_agent_error_after_valid_new_artifact_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            conversations: list[object] = []

            def complete_then_raise(_run_count: int) -> None:
                (workspace / "proof.md").write_text(_proof(), encoding="utf-8")
                raise RuntimeError("agent loop stopped after final write")

            with patch.object(
                runner,
                "_kimi_sdk_conversation",
                self._fake_conversation(complete_then_raise, conversations),
            ), patch.object(runner, "emit_item") as emit:
                self.assertEqual(runner._run_kimi_native_api("prove", workspace), 0)

            self.assertEqual(conversations[0].run_count, 1)
            messages = [call.args[0].get("text", "") for call in emit.call_args_list]
            self.assertTrue(any("recovered that completed artifact" in text for text in messages))

    def test_preexisting_unchanged_artifact_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "proof.md").write_text(_proof(), encoding="utf-8")
            conversations: list[object] = []

            def no_write(_run_count: int) -> None:
                return None

            with patch.object(
                runner,
                "_kimi_sdk_conversation",
                self._fake_conversation(no_write, conversations),
            ), patch.object(
                runner, "_write_kimi_artifact_repair", return_value={}
            ), patch.object(runner, "emit_item") as emit:
                self.assertEqual(runner._run_kimi_native_api("prove", workspace), 1)

            self.assertEqual(conversations[0].run_count, 1)
            self.assertIn(
                "not created or updated",
                emit.call_args_list[-1].args[0]["message"],
            )

    def test_artifact_continuation_atomically_installs_valid_proof(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text("incomplete", encoding="utf-8")
            (workspace / "problem.txt").write_text("Prove the claim.", encoding="utf-8")

            with patch.object(
                runner,
                "_kimi_text_completion",
                return_value=(_proof(), {"input_tokens": 4, "output_tokens": 8}),
            ) as completion:
                usage = runner._write_kimi_artifact_repair(
                    prompt="prove",
                    workspace=workspace,
                    issues=["proof.md is incomplete"],
                )

            self.assertEqual(usage, {"input_tokens": 4, "output_tokens": 8})
            self.assertEqual(runner._proof_reference_issues(proof), [])
            self.assertEqual(list(workspace.glob(".proof-kimi-repair-*.md")), [])
            self.assertIn("proof.md is incomplete", completion.call_args.args[0])

    def test_invalid_continuation_preserves_existing_draft(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text("existing draft", encoding="utf-8")

            with patch.object(
                runner,
                "_kimi_text_completion",
                return_value=("too short", {}),
            ):
                with self.assertRaisesRegex(RuntimeError, "continuation was rejected"):
                    runner._write_kimi_artifact_repair(
                        prompt="prove",
                        workspace=workspace,
                        issues=["proof.md is incomplete"],
                    )

            self.assertEqual(proof.read_text(encoding="utf-8"), "existing draft")
            self.assertEqual(list(workspace.glob(".proof-kimi-repair-*.md")), [])

    def test_markdown_fence_is_removed_only_when_it_wraps_entire_response(self) -> None:
        fenced = "```markdown\n# Proof\n\nBody\n```"
        self.assertEqual(runner._strip_markdown_fence(fenced), "# Proof\n\nBody")
        self.assertEqual(
            runner._strip_markdown_fence("Preface\n" + fenced),
            "Preface\n" + fenced,
        )

    def test_text_completion_uses_kimi_chat_wire_contract(self) -> None:
        response_body = json.dumps(
            {
                "choices": [{"message": {"content": _proof()}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 11},
            }
        ).encode()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, limit: int) -> bytes:
                self_limit = 4_000_001
                if limit != self_limit:
                    raise AssertionError(f"unexpected read limit: {limit}")
                return response_body

        with patch.dict(
            os.environ,
            {
                "KIMI_API_KEY": "unit-secret",
                "KIMI_API_BASE": "https://example.test/v1/",
                "KIMI_REASONING_EFFORT": "max",
            },
        ), patch.object(
            runner.urllib_request,
            "urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            text, usage = runner._kimi_text_completion("Write the proof.")

        self.assertEqual(text, _proof().strip())
        self.assertEqual(usage["input_tokens"], 7)
        self.assertEqual(usage["output_tokens"], 11)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer unit-secret")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "kimi-k3")
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(payload["max_tokens"], 16_000)
        self.assertNotIn("tools", payload)
        self.assertNotIn("output_config", payload)

    def test_main_routes_only_kimi_api_mode_to_bounded_runner(self) -> None:
        with patch.dict(
            os.environ,
            {"LLM_MODEL": "openai/kimi-k3", "LLM_API_KEY": "test-key"},
            clear=True,
        ), patch.object(sys, "argv", ["runner", "prove"]), patch.object(
            runner, "_which", return_value="/fake/openhands"
        ), patch.object(
            runner, "_ensure_sdk_python"
        ), patch.object(
            runner, "_run_kimi_native_api", return_value=0
        ) as run:
            self.assertEqual(runner.main(), 0)
        run.assert_called_once_with("prove", Path.cwd().resolve())


if __name__ == "__main__":
    unittest.main()
