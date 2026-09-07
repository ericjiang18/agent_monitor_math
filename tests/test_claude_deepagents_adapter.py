"""Offline coverage for DeepAgents' Claude subscription chat adapter."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor.runners import deepagents_runner


_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write one virtual file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}


class ClaudeDeepAgentsContractTests(unittest.TestCase):
    def test_strict_parser_accepts_typed_call_and_rejects_extra_argument(self) -> None:
        accepted = deepagents_runner._parse_claude_deepagents_response(
            json.dumps(
                {
                    "kind": "tool_calls",
                    "text": "",
                    "tool_calls": [
                        {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "/proof.md", "content": "proof"}
                            ),
                        }
                    ],
                }
            ),
            [_TOOL],
        )
        self.assertEqual(accepted["tool_calls"][0]["name"], "write_file")

        with self.assertRaisesRegex(ValueError, "unexpected field"):
            deepagents_runner._parse_claude_deepagents_response(
                json.dumps(
                    {
                        "kind": "tool_calls",
                        "text": "",
                        "tool_calls": [
                            {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": "/proof.md",
                                        "content": "proof",
                                        "outside": True,
                                    }
                                ),
                            }
                        ],
                    }
                ),
                [_TOOL],
            )

    def test_strict_parser_canonicalizes_schema_valid_object_arguments(self) -> None:
        accepted = deepagents_runner._parse_claude_deepagents_response(
            json.dumps(
                {
                    "kind": "tool_calls",
                    "text": "",
                    "tool_calls": [
                        {
                            "name": "write_file",
                            "arguments": {
                                "path": "/proof.md",
                                "content": "proof",
                            },
                        }
                    ],
                }
            ),
            [_TOOL],
        )
        canonical = accepted["tool_calls"][0]["arguments"]
        self.assertIsInstance(canonical, str)
        self.assertEqual(
            json.loads(canonical),
            {"path": "/proof.md", "content": "proof"},
        )

        with self.assertRaisesRegex(ValueError, "unexpected field"):
            deepagents_runner._parse_claude_deepagents_response(
                json.dumps(
                    {
                        "kind": "tool_calls",
                        "text": "",
                        "tool_calls": [
                            {
                                "name": "write_file",
                                "arguments": {
                                    "path": "/proof.md",
                                    "content": "proof",
                                    "outside": True,
                                },
                            }
                        ],
                    }
                ),
                [_TOOL],
            )

    def test_text_recovery_contract_is_exact(self) -> None:
        parsed = deepagents_runner._parse_claude_deepagents_response(
            '{"text":"self-contained fallback"}', [], text_only=True
        )
        self.assertEqual(parsed["kind"], "text")
        with self.assertRaisesRegex(ValueError, "exactly"):
            deepagents_runner._parse_claude_deepagents_response(
                '{"text":"x","tool_calls":[]}', [], text_only=True
            )

    def test_parser_accepts_only_a_single_whole_json_fence(self) -> None:
        payload = json.dumps(
            {"kind": "text", "text": "complete proof", "tool_calls": []}
        )
        parsed = deepagents_runner._parse_claude_deepagents_response(
            f"```json\n{payload}\n```", []
        )
        self.assertEqual(parsed["text"], "complete proof")

        for invalid in (
            f"preface\n```json\n{payload}\n```",
            f"```javascript\n{payload}\n```",
            f"```json\n{payload}\n```\ntrailer",
        ):
            with self.subTest(invalid=invalid[:20]):
                with self.assertRaisesRegex(
                    ValueError, "one exact JSON object"
                ):
                    deepagents_runner._parse_claude_deepagents_response(
                        invalid, []
                    )

    def test_codex_and_claude_routes_fail_before_model_import(self) -> None:
        output = io.StringIO()
        with (
            patch.object(deepagents_runner, "_install_termination_handlers"),
            patch.object(sys, "argv", ["deepagents_runner.py", "prove"]),
            patch.dict(
                os.environ,
                {
                    "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                },
                clear=True,
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(deepagents_runner.main(), 2)
        self.assertIn("cannot both be active", output.getvalue())

    def test_isolated_langchain_adapter_repair_usage_and_no_tool_access(self) -> None:
        root = Path(__file__).resolve().parents[1]
        python = root / "engines" / "deepagents" / ".venv" / "bin" / "python"
        if not python.is_file():
            self.skipTest("DeepAgents isolated environment is not installed")
        script = textwrap.dedent(
            r'''
            import json
            import os
            import tempfile
            from pathlib import Path

            from langchain_core.messages import HumanMessage, ToolMessage
            from deepagents import create_deep_agent
            from deepagents.backends.filesystem import FilesystemBackend

            from agent_monitor.runners import claude_backend, deepagents_runner

            canonical = "claude-haiku-4-5-20251001"
            tool = {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write one virtual file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                },
            }
            calls = []
            responses = [
                json.dumps({
                    "kind": "tool_calls",
                    "text": "",
                    "tool_calls": [{"name": "write_file", "arguments": "{}"}],
                }),
                json.dumps({
                    "kind": "tool_calls",
                    "text": "",
                    "tool_calls": [{
                        "name": "write_file",
                        "arguments": json.dumps({
                            "path": "/proof.md",
                            "content": "complete proof",
                        }),
                    }],
                }),
            ]

            def fake_exec(system, user, **kwargs):
                calls.append((system, user, kwargs))
                return claude_backend.ClaudeResult(
                    text=responses.pop(0),
                    usage={"input_tokens": 2, "output_tokens": 3},
                    model=canonical,
                    cost_usd=None,
                    events=(),
                    terminal_event={},
                )

            claude_backend.claude_exec = fake_exec
            with tempfile.TemporaryDirectory() as workspace:
                os.environ["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"] = "1"
                os.environ["AGENT_MONITOR_CLAUDE_MODEL"] = "claude-haiku-4-5"
                os.environ["CLAUDE_CONFIG_DIR"] = workspace
                model = deepagents_runner._create_claude_subscription_model(
                    Path(workspace), "anthropic:claude-haiku-4-5"
                )
                assert model._get_ls_params()["ls_provider"] == "anthropic"
                assert model._get_ls_params()["ls_model_name"] == "claude-haiku-4-5"
                graph = create_deep_agent(
                    model=model,
                    tools=[],
                    backend=FilesystemBackend(
                        root_dir=workspace, virtual_mode=True
                    ),
                )
                assert graph is not None
                result = model._generate(
                    [HumanMessage(content="write the proof")], tools=[tool]
                )
                message = result.generations[0].message
                assert len(calls) == 2, calls
                assert message.tool_calls[0]["name"] == "write_file"
                assert message.tool_calls[0]["args"]["path"] == "/proof.md"
                assert message.usage_metadata == {
                    "input_tokens": 4,
                    "output_tokens": 6,
                    "total_tokens": 10,
                }
                assert message.response_metadata["provider"] == "anthropic"
                assert message.response_metadata["requested_model"] == (
                    "claude-haiku-4-5"
                )
                for _, _, kwargs in calls:
                    assert kwargs["model"] == "claude-haiku-4-5"
                    assert kwargs["enable_tools"] is False
                    assert kwargs["workspace"] == Path(workspace)
                    assert kwargs["source_env"] is os.environ

                calls.clear()
                responses.extend([
                    "not-json",
                    json.dumps({
                        "kind": "tool_calls",
                        "text": "",
                        "tool_calls": [{
                            "name": "write_file",
                            "arguments": "{}",
                        }],
                    }),
                    json.dumps({"text": "safe text fallback"}),
                ])
                recovered = model._generate(
                    [HumanMessage(content="answer directly")], tools=[tool]
                ).generations[0].message
                assert len(calls) == 3, calls
                assert recovered.content == "safe text fallback"
                assert recovered.tool_calls == []
                assert recovered.response_metadata["attempts"] == 3

                calls.clear()
                responses.extend([
                    json.dumps({
                        "kind": "text",
                        "text": "Proof.lean is complete and compiles.",
                        "tool_calls": [],
                    }),
                    json.dumps({
                        "kind": "tool_calls",
                        "text": "",
                        "tool_calls": [{
                            "name": "write_file",
                            "arguments": {
                                "path": "/proof.md",
                                "content": "formal artifact",
                            },
                        }],
                    }),
                ])
                os.environ["AGENT_MONITOR_CLAUDE_LEAN_MODE"] = "1"
                formal = model._generate(
                    [HumanMessage(content="formalize and compile")], tools=[tool]
                ).generations[0].message
                assert len(calls) == 2, calls
                assert formal.tool_calls[0]["name"] == "write_file"
                assert formal.tool_calls[0]["args"] == {
                    "path": "/proof.md",
                    "content": "formal artifact",
                }
                assert 'tool_choice: "required"' in calls[0][1]
                assert "kind=text is still invalid" in calls[1][1]

                calls.clear()
                responses.append(json.dumps({
                    "kind": "text",
                    "text": "The actual Lean checker passed.",
                    "tool_calls": [],
                }))
                completed = model._generate(
                    [
                        HumanMessage(content="formalize and compile"),
                        ToolMessage(
                            content="exit 0\ncompiled",
                            tool_call_id="call_lean",
                            name="lean_check",
                        ),
                    ],
                    tools=[tool],
                ).generations[0].message
                assert len(calls) == 1, calls
                assert completed.content == "The actual Lean checker passed."
                assert completed.tool_calls == []
                assert 'tool_choice: null' in calls[0][1]
                os.environ.pop("AGENT_MONITOR_CLAUDE_LEAN_MODE", None)
            '''
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root)
        completed = subprocess.run(
            [str(python), "-c", script],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
