"""Fail-closed tests for exact Claude Code model attestation."""
from __future__ import annotations

import unittest

from agent_monitor.runners import claude_backend


def _process(*events: dict) -> claude_backend._ProcessResult:
    return claude_backend._ProcessResult(0, "", tuple(events))


def _init() -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "model": "claude-haiku-4-5",
        "session_id": "test-session",
    }


def _assistant(model: str, text: str = "done") -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "content": [{"type": "text", "text": text}],
        },
    }


def _result(model_usage: dict | None = None) -> dict:
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }
    if model_usage is not None:
        event["modelUsage"] = model_usage
    return event


class ExactClaudeModelAttestationTests(unittest.TestCase):
    def test_actual_assistant_and_canonical_usage_override_init(self) -> None:
        canonical = "claude-haiku-4-5-20251001"
        parsed = claude_backend._terminal_result(
            _process(
                _init(),
                _assistant(canonical),
                _result(
                    {
                        canonical: {
                            "canonicalModel": canonical,
                            "inputTokens": 2,
                        }
                    }
                ),
            )
        )
        self.assertEqual(parsed.model, canonical)
        claude_backend._require_requested_model(parsed, "claude-haiku-4-5")

    def test_mixed_assistant_models_fail_exact_attestation(self) -> None:
        parsed = claude_backend._terminal_result(
            _process(
                _init(),
                _assistant("claude-haiku-4-5-20251001"),
                _assistant("claude-sonnet-5-20260203", "secondary"),
                _result(),
            )
        )
        with self.assertRaisesRegex(
            claude_backend.ClaudeBackendError, "claude-sonnet-5"
        ):
            claude_backend._require_requested_model(parsed, "claude-haiku-4-5")

    def test_model_usage_substitution_fails_even_when_assistant_is_haiku(self) -> None:
        parsed = claude_backend._terminal_result(
            _process(
                _init(),
                _assistant("claude-haiku-4-5-20251001"),
                _result(
                    {
                        "claude-sonnet-5-20260203": {
                            "canonicalModel": "claude-sonnet-5-20260203"
                        }
                    }
                ),
            )
        )
        with self.assertRaisesRegex(
            claude_backend.ClaudeBackendError, "claude-sonnet-5"
        ):
            claude_backend._require_requested_model(parsed, "claude-haiku-4-5")

    def test_init_only_is_not_actual_model_attestation(self) -> None:
        parsed = claude_backend._terminal_result(_process(_init(), _result()))
        self.assertEqual(parsed.model, "claude-haiku-4-5")
        with self.assertRaisesRegex(
            claude_backend.ClaudeBackendError, "missing actual model telemetry"
        ):
            claude_backend._require_requested_model(parsed, "claude-haiku-4-5")


if __name__ == "__main__":
    unittest.main()
