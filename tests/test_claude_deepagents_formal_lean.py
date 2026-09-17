"""Offline fail-closed coverage for DeepAgents Formal Lean mode."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor.runners import deepagents_runner


_VALID_LEAN = """theorem deepagents_formal_smoke : True := by
  trivial

-- This padding makes the artifact substantive enough for the production gate.
"""


class _StructuredTool:
    @classmethod
    def from_function(cls, *, func, name: str, description: str):
        return SimpleNamespace(func=func, name=name, description=description)


def _langchain_tool_modules() -> dict[str, types.ModuleType]:
    package = types.ModuleType("langchain_core")
    tools = types.ModuleType("langchain_core.tools")
    tools.StructuredTool = _StructuredTool
    return {"langchain_core": package, "langchain_core.tools": tools}


def _deepagents_modules(create_deep_agent) -> dict[str, types.ModuleType]:
    deepagents = types.ModuleType("deepagents")
    deepagents.create_deep_agent = create_deep_agent
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


class ClaudeDeepAgentsFormalLeanTests(unittest.TestCase):
    def test_formal_prompt_uses_task_and_lean_artifact_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("Prove True.", encoding="utf-8")
            with patch.dict(
                os.environ, {"AGENT_MONITOR_CLAUDE_LEAN_MODE": "1"}, clear=True
            ):
                result = deepagents_runner._rewrite_prompt_for_virtual_fs(
                    "Write the requested result.", workspace
                )

        self.assertIn("/TASK.md", result)
        self.assertIn("/Proof.lean", result)
        self.assertIn("do not create proof.md or proof.tex", result)
        self.assertNotIn("write the final informal proof", result.lower())
        self.assertNotIn("/proof.md", result)

    def test_lean_check_uses_fixed_lake_argv_and_secret_free_env(self) -> None:
        captured: dict = {}
        completed = subprocess.CompletedProcess([], 0, stdout="compiled\n", stderr="")

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return completed

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "Proof.lean"
            source.write_text(_VALID_LEAN, encoding="utf-8")
            (workspace / "lakefile.lean").write_text("package p", encoding="utf-8")
            state: dict = {}
            env = {
                "PATH": "/opt/elan/bin:/usr/bin",
                "HOME": "/tmp/test-home",
                "LANG": "C.UTF-8",
                "LEAN_PATH": "/opt/mathlib",
                "ELAN_HOME": "/opt/elan",
                "AGENT_MONITOR_DEEPAGENTS_LEAN_TIMEOUT": "9999",
                "ANTHROPIC_API_KEY": "must-not-leak",
                "OPENAI_API_KEY": "must-not-leak",
                "AWS_SECRET_ACCESS_KEY": "must-not-leak",
                "CLAUDE_CONFIG_DIR": "/tmp/private-oauth",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.dict(sys.modules, _langchain_tool_modules()),
                patch.object(
                    deepagents_runner.shutil,
                    "which",
                    side_effect=lambda name: {
                        "lake": "/opt/elan/bin/lake",
                        "lean": "/opt/elan/bin/lean",
                    }.get(name),
                ),
                patch.object(deepagents_runner.subprocess, "run", side_effect=fake_run),
            ):
                tool = deepagents_runner._create_lean_check_tool(workspace, state)
                output = tool.func()

            self.assertEqual(
                captured["argv"],
                ["/opt/elan/bin/lake", "env", "lean", "Proof.lean"],
            )
            self.assertEqual(captured["cwd"], str(workspace))
            self.assertEqual(captured["timeout"], 300)
            self.assertFalse(captured["check"])
            self.assertEqual(
                captured["env"],
                {
                    "PATH": "/opt/elan/bin:/usr/bin",
                    "HOME": "/tmp/test-home",
                    "LANG": "C.UTF-8",
                    "LEAN_PATH": "/opt/mathlib",
                    "ELAN_HOME": "/opt/elan",
                    "NO_COLOR": "1",
                },
            )
            self.assertEqual(state["exit_code"], 0)
            self.assertEqual(state["sha256"], deepagents_runner._file_sha256(source))
            self.assertEqual(
                state["command"], ["lake", "env", "lean", "Proof.lean"]
            )
            self.assertEqual(output, "exit 0\ncompiled")

    def test_lean_check_uses_fixed_direct_lean_argv(self) -> None:
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="bad")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "Proof.lean"
            source.write_text(_VALID_LEAN, encoding="utf-8")
            expected_sha = deepagents_runner._file_sha256(source)
            state: dict = {}
            with (
                patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
                patch.dict(sys.modules, _langchain_tool_modules()),
                patch.object(
                    deepagents_runner.shutil,
                    "which",
                    side_effect=lambda name: "/usr/bin/lean" if name == "lean" else None,
                ),
                patch.object(deepagents_runner.subprocess, "run", side_effect=fake_run),
            ):
                output = deepagents_runner._create_lean_check_tool(
                    workspace, state
                ).func()

        self.assertEqual(captured["argv"], ["/usr/bin/lean", "Proof.lean"])
        self.assertEqual(state["exit_code"], 1)
        self.assertEqual(state["sha256"], expected_sha)
        self.assertEqual(state["command"], ["lean", "Proof.lean"])
        self.assertEqual(output, "exit 1\nbad")

    def test_lean_check_rejects_concurrent_edit_during_compilation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "Proof.lean"
            source.write_text(_VALID_LEAN, encoding="utf-8")
            state: dict = {}

            def fake_run(argv, **_kwargs):
                source.write_text(
                    _VALID_LEAN + "\n-- edited while Lean was running\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

            with (
                patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
                patch.dict(sys.modules, _langchain_tool_modules()),
                patch.object(
                    deepagents_runner.shutil,
                    "which",
                    side_effect=lambda name: "/usr/bin/lean" if name == "lean" else None,
                ),
                patch.object(deepagents_runner.subprocess, "run", side_effect=fake_run),
            ):
                output = deepagents_runner._create_lean_check_tool(
                    workspace, state
                ).func()

        self.assertEqual(state["exit_code"], 3)
        self.assertIsNone(state["sha256"])
        self.assertEqual(state["command"], ["lean", "Proof.lean"])
        self.assertIn("changed during compilation; result rejected", output)

    def test_compiled_artifact_gate_rejects_every_untrusted_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            candidate = workspace / "Proof.lean"
            candidate.write_text(_VALID_LEAN, encoding="utf-8")
            good_sha = deepagents_runner._file_sha256(candidate)

            self.assertTrue(
                deepagents_runner._compiled_lean_artifact_ready(
                    workspace,
                    initial_sha256=None,
                    checked_sha256=good_sha,
                    check_exit_code=0,
                )
            )
            self.assertFalse(
                deepagents_runner._compiled_lean_artifact_ready(
                    workspace,
                    initial_sha256=good_sha,
                    checked_sha256=good_sha,
                    check_exit_code=0,
                ),
                "an unchanged pre-run artifact must be stale",
            )
            self.assertFalse(
                deepagents_runner._compiled_lean_artifact_ready(
                    workspace,
                    initial_sha256=None,
                    checked_sha256=good_sha,
                    check_exit_code=1,
                ),
                "a failed compiler result must never be accepted",
            )

            candidate.write_text(_VALID_LEAN + "\n-- changed after check\n", encoding="utf-8")
            self.assertFalse(
                deepagents_runner._compiled_lean_artifact_ready(
                    workspace,
                    initial_sha256=None,
                    checked_sha256=good_sha,
                    check_exit_code=0,
                ),
                "post-check edits must invalidate the recorded SHA",
            )

            for forbidden in ("sorry", "admit"):
                candidate.write_text(
                    _VALID_LEAN + f"\nexample : True := by {forbidden}\n",
                    encoding="utf-8",
                )
                forbidden_sha = deepagents_runner._file_sha256(candidate)
                with self.subTest(forbidden=forbidden):
                    self.assertFalse(
                        deepagents_runner._compiled_lean_artifact_ready(
                            workspace,
                            initial_sha256=None,
                            checked_sha256=forbidden_sha,
                            check_exit_code=0,
                        )
                    )

            candidate.write_text(
                "theorem two_add_two : 2 + 2 = 4 := by decide\n",
                encoding="utf-8",
            )
            exact_sha = deepagents_runner._file_sha256(candidate)
            self.assertEqual(candidate.stat().st_size, 45)
            self.assertTrue(
                deepagents_runner._compiled_lean_artifact_ready(
                    workspace,
                    initial_sha256=None,
                    checked_sha256=exact_sha,
                    check_exit_code=0,
                ),
                "a compact kernel-checked theorem must pass the semantic gate",
            )

            candidate.write_text("theorem x : True := by trivial\n", encoding="utf-8")
            short_sha = deepagents_runner._file_sha256(candidate)
            self.assertLess(candidate.stat().st_size, deepagents_runner._MIN_FORMAL_LEAN_BYTES)
            self.assertFalse(
                deepagents_runner._compiled_lean_artifact_ready(
                    workspace,
                    initial_sha256=None,
                    checked_sha256=short_sha,
                    check_exit_code=0,
                ),
                "tiny artifacts must not pass the substantive gate",
            )

            candidate.unlink()
            target = workspace / "Elsewhere.lean"
            target.write_text(_VALID_LEAN, encoding="utf-8")
            candidate.symlink_to(target)
            linked_sha = deepagents_runner._file_sha256(target)
            self.assertFalse(
                deepagents_runner._compiled_lean_artifact_ready(
                    workspace,
                    initial_sha256=None,
                    checked_sha256=linked_sha,
                    check_exit_code=0,
                ),
                "symlink artifacts must be rejected",
            )

    def _run_formal_main(self, agent, *, tool_factory=None):
        created: dict = {}

        def create_deep_agent(**kwargs):
            created.update(kwargs)
            agent.created = created
            return agent

        errors: list[str] = []
        env = {
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "AGENT_MONITOR_CLAUDE_MODEL": "claude-haiku-4-5",
            "AGENT_MONITOR_CLAUDE_LEAN_MODE": "1",
        }
        normalizer = patch.object(deepagents_runner, "_normalize_changed_proof_outcome")
        auditor = patch.object(deepagents_runner, "_run_supplemental_audit")
        proof_ready = patch.object(deepagents_runner, "_proof_artifact_ready")
        with (
            patch.dict(os.environ, env, clear=True),
            patch.dict(sys.modules, _deepagents_modules(create_deep_agent)),
            patch.object(sys, "argv", ["deepagents_runner.py", "prove True"]),
            patch.object(deepagents_runner, "_install_termination_handlers"),
            patch.object(deepagents_runner, "_subagents_enabled", return_value=True),
            patch.object(deepagents_runner, "_load_user_tools", return_value=[]),
            patch.object(
                deepagents_runner,
                "_create_claude_subscription_model",
                return_value=object(),
            ) as model_factory,
            patch.object(
                deepagents_runner,
                "_create_lean_check_tool",
                side_effect=tool_factory
                or (lambda _workspace, _state: SimpleNamespace(name="lean_check")),
            ),
            normalizer as normalize,
            auditor as audit,
            proof_ready as informal_ready,
            patch.object(
                deepagents_runner,
                "emit_item",
                side_effect=lambda item: errors.append(str(item.get("message") or "")),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rc = deepagents_runner.main()
        return rc, created, errors, model_factory, normalize, audit, informal_ready

    def test_formal_main_graph_exception_fails_closed_without_informal_paths(self) -> None:
        class FailingAgent:
            def stream(self, *_args, **_kwargs):
                raise RuntimeError("offline graph failure")
                yield  # pragma: no cover

        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                result = self._run_formal_main(FailingAgent())
                proof_md_exists = Path("proof.md").exists()
            finally:
                os.chdir(previous)

        rc, created, errors, model_factory, normalize, audit, informal_ready = result
        self.assertEqual(rc, 1)
        self.assertFalse(proof_md_exists)
        self.assertTrue(any("offline graph failure" in error for error in errors))
        model_factory.assert_called_once_with(Path(tmp), "claude-haiku-4-5")
        normalize.assert_not_called()
        audit.assert_not_called()
        informal_ready.assert_not_called()
        self.assertIn("formal Lean proving agent", created["system_prompt"])
        self.assertNotIn("/proof.md", created["system_prompt"])

    def test_formal_main_never_persists_final_text_to_proof_md(self) -> None:
        AIMessage = type("AIMessage", (), {})
        message = AIMessage()
        message.id = "final"
        message.content = "# Informal fallback\n\n" + ("This must not be saved. " * 5)
        message.usage_metadata = {}
        message.tool_calls = []

        class TextOnlyAgent:
            def stream(self, *_args, **_kwargs):
                yield {"messages": [message], "files": {}}

        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                result = self._run_formal_main(TextOnlyAgent())
                proof_md_exists = Path("proof.md").exists()
            finally:
                os.chdir(previous)

        rc, _created, errors, _model_factory, normalize, audit, informal_ready = result
        self.assertEqual(rc, 1)
        self.assertFalse(proof_md_exists)
        self.assertTrue(any("lean_check was never completed" in e for e in errors))
        normalize.assert_not_called()
        audit.assert_not_called()
        informal_ready.assert_not_called()

    def test_formal_main_retries_narrative_until_compiler_bound_artifact(self) -> None:
        AIMessage = type("AIMessage", (), {})
        premature = AIMessage()
        premature.id = "premature"
        premature.content = "Everything compiled successfully."
        premature.usage_metadata = {}
        premature.tool_calls = []

        class RecoveringAgent:
            created: dict

            def __init__(self):
                self.calls = 0
                self.prompts: list[str] = []

            def stream(self, payload, *_args, **_kwargs):
                self.calls += 1
                self.prompts.append(payload["messages"][0]["content"])
                if self.calls == 1:
                    yield {"messages": [premature], "files": {}}
                    return
                Path("Proof.lean").write_text(_VALID_LEAN, encoding="utf-8")
                self.created["tools"][0].func()
                yield {"messages": [], "files": {}}

        def tool_factory(workspace: Path, state: dict):
            def check():
                state.clear()
                state.update(
                    {
                        "exit_code": 0,
                        "sha256": deepagents_runner._file_sha256(
                            workspace / "Proof.lean"
                        ),
                    }
                )
                return "exit 0"

            return SimpleNamespace(name="lean_check", func=check)

        agent = RecoveringAgent()
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                result = self._run_formal_main(
                    agent,
                    tool_factory=tool_factory,
                )
            finally:
                os.chdir(previous)

        rc, _created, errors, _model_factory, normalize, audit, informal_ready = result
        self.assertEqual(rc, 0)
        self.assertEqual(agent.calls, 2)
        self.assertIn("FORMAL LEAN RECOVERY PASS 2", agent.prompts[1])
        self.assertIn("checking an unchanged file is insufficient", agent.prompts[1])
        self.assertFalse(any(errors))
        normalize.assert_not_called()
        audit.assert_not_called()
        informal_ready.assert_not_called()

    def test_formal_main_accepts_only_latest_successful_check(self) -> None:

        class FormalAgent:
            created: dict

            def __init__(self, *, mutate_after_check: bool):
                self.mutate_after_check = mutate_after_check

            def stream(self, *_args, **_kwargs):
                Path("Proof.lean").write_text(_VALID_LEAN, encoding="utf-8")
                self.created["tools"][0].func()
                if self.mutate_after_check:
                    Path("Proof.lean").write_text(
                        _VALID_LEAN + "\n-- unchecked edit\n", encoding="utf-8"
                    )
                yield {"messages": [], "files": {}}

        def tool_factory(workspace: Path, state: dict):
            def check():
                state.clear()
                state.update(
                    {
                        "exit_code": 0,
                        "sha256": deepagents_runner._file_sha256(
                            workspace / "Proof.lean"
                        ),
                    }
                )
                return "exit 0"

            return SimpleNamespace(name="lean_check", func=check)

        outcomes: list[int] = []
        for mutate_after_check in (False, True):
            with self.subTest(mutate_after_check=mutate_after_check):
                with tempfile.TemporaryDirectory() as tmp:
                    previous = Path.cwd()
                    os.chdir(tmp)
                    try:
                        result = self._run_formal_main(
                            FormalAgent(mutate_after_check=mutate_after_check),
                            tool_factory=tool_factory,
                        )
                    finally:
                        os.chdir(previous)
                rc, _created, _errors, _model_factory, normalize, audit, informal_ready = result
                outcomes.append(rc)
                normalize.assert_not_called()
                audit.assert_not_called()
                informal_ready.assert_not_called()

        self.assertEqual(outcomes, [0, 1])


if __name__ == "__main__":
    unittest.main()
