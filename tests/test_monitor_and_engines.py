from __future__ import annotations

import ast
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import auth, auto_pipeline, agent_config, console_server, jobs, lean_verify, library, monitor_overview, proof_bridge, proof_graph, settings
from agent_monitor import lean_broker
from agent_monitor.engines_registry import (
    CLI_ENGINES,
    CODEX_SUBSCRIPTION_ENGINES,
    all_engine_ids,
    build_cli_command,
    list_engines,
    proof_prompt,
)
from agent_monitor.jobs import _promote_proof_artifact
from agent_monitor.jobs import _persist_openclaw_final_answer
from agent_monitor.jobs import _persist_text_final_answer
from agent_monitor.runners.openclaude_runner import _augment_audit_prompt as augment_openclaude_audit_prompt, _codex_adapter_model, main as openclaude_main
from agent_monitor.runners.openhands_subscription_runner import OpenHandsIterationLimit, _ACPToolBudget, _acp_model_id, _cancel_acp_prompt, _codex_exec_command, _is_proof_response, _max_iterations, _reached_iteration_limit, _recoverable_activity_checkpoint, _recoverable_control_checkpoint, _usage_payload, _valid_proof_file
from agent_monitor.runners.deepseek_harness_runner import CODEX_SYSTEM_PROMPT as DEEPSEEK_CODEX_SYSTEM_PROMPT
from agent_monitor.runners.deepagents_runner import _codex_timeout_response, _codex_usage_from_jsonl, _file_sha256 as deepagents_file_sha256, _library_tool_argv as deepagents_library_tool_argv, _persist_tool_serialization_checkpoint, _recoverable_degraded_checkpoint, _recoverable_recursion_checkpoint, _recursion_limit as deepagents_recursion_limit, _run_codex_exec, _schema_python_type as deepagents_schema_python_type, _subagents_enabled, _text_only_response_schema, _text_only_tool_recovery_request, _tool_call_issues, _structured_tool_call_item_schema
from agent_monitor.runners import hermes as hermes_runner
from agent_monitor.runners import deepagents_runner
from agent_monitor.runners.hermes import close_agent as close_hermes_agent
from agent_monitor.runners.plain_runner import SYSTEM_PROMPT as PLAIN_SYSTEM_PROMPT
from agent_monitor.runners.plain_runner import _save_result as save_plain_result
from agent_monitor.runners.plain_runner import _api_artifact_issues
from agent_monitor.runners.plain_runner import _ensure_audit_disclosure
from agent_monitor.runners import improof as improof_runner
from agent_monitor.runners import improof_audit_adapter
from agent_monitor.runners import metaharness_runner
from agent_monitor.runners import openclaw_runner
from agent_monitor.runners import api_backend
from agent_monitor.runners import codex_backend
from agent_monitor import kimi_responses_proxy
from agent_monitor.runners import plain_runner
from agent_monitor.runners import ucla as ucla_runner
from agent_monitor.runners._stream import stream_subprocess
from agent_monitor.cli_events import CLIEventParser


_TEST_LEAN_TOOLCHAIN_TREE = "1" * 64
_TEST_LEAN_MATHLIB_TREE = "2" * 64
_TEST_LEAN_RELEASE_ID = hashlib.sha256(
    (
        json.dumps(
            {
                "schema": 1,
                "lean_toolchain": lean_broker.EXPECTED_LEAN_TOOLCHAIN,
                "lean_version": lean_broker.EXPECTED_TOOLCHAIN,
                "mathlib_input_rev": lean_broker.EXPECTED_MATHLIB_INPUT_REV,
                "mathlib_rev": lean_broker.EXPECTED_MATHLIB_REV,
                "toolchain_tree_sha256": _TEST_LEAN_TOOLCHAIN_TREE,
                "mathlib_tree_sha256": _TEST_LEAN_MATHLIB_TREE,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
).hexdigest()


def _broker_bound_lean_check(
    lean_dir: Path,
    uses_mathlib: bool,
    *,
    ok: bool = True,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, object]:
    """Return the same source-bound receipt shape as the fixed Lean broker."""
    source = (Path(lean_dir) / lean_verify.PROOF_FILENAME).read_bytes()
    profile = "mathlib" if uses_mathlib else "core"
    return {
        "ok": ok,
        "status": "verified" if ok else "failed",
        "exit_code": 0 if ok else 1,
        "duration_s": 0.01,
        "command": ["lean-broker", profile],
        "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
        "sandbox": lean_broker.SANDBOX_MARKER,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "checker_profile": profile,
        "release_id": _TEST_LEAN_RELEASE_ID,
        "toolchain_tree_sha256": _TEST_LEAN_TOOLCHAIN_TREE,
        "mathlib_tree_sha256": _TEST_LEAN_MATHLIB_TREE,
        "stderr": stderr,
        "stdout": stdout,
    }


def _install_reconciled_audit(
    workspace: Path,
    audit: Path,
    *,
    complete_core: bool = True,
) -> None:
    """Build a real validator-backed exact-final contract for runtime tests."""
    proof = workspace / "proof.md"
    if not proof.exists():
        proof.write_text(
            "# Partial Progress\n\nA conservative audited checkpoint.\n\n"
            "ProvingConsole outcome: Partial Progress\n",
            encoding="utf-8",
        )
    digest = hashlib.sha256(proof.read_bytes()).hexdigest()
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "final-candidate.md").write_bytes(proof.read_bytes())
    output = {
        "verdict": "accept",
        "summary": "The exact frozen final candidate was checked.",
        "findings": [],
        "coverage_notes": {
            "reviewed_regions": ["complete candidate"],
            "unreviewed_or_difficult_regions": [],
            "external_checks_not_performed": [],
        },
    }
    for name in ("global.json", "decomposed.json", "verifier-output.json"):
        (audit / name).write_text(json.dumps(output), encoding="utf-8")
    (audit / "merge-map.json").write_text(
        json.dumps(
            {
                "schema_version": "ensemble-paper-audit-app.merge-map.v1",
                "findings": [],
                "counts": {
                    "global_only": 0,
                    "decomposed_only": 0,
                    "both": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    (audit / "run.json").write_text(
        json.dumps(
            {
                "run_id": audit.name,
                "complete_core": complete_core,
                "candidate_sha256": digest,
                "repair_history": [],
                "reconciliation": {
                    "schema_version": "research-proof-audit.reconciliation.v1",
                    "status": "audited_unchanged",
                    "final_artifact": {"path": "proof.md", "sha256": digest},
                    "audited_candidate": {
                        "path": "final-candidate.md",
                        "sha256": digest,
                    },
                    "reruns": {
                        "global": "global.json",
                        "decomposed": "decomposed.json",
                        "merged": "verifier-output.json",
                    },
                    "superseded_findings": [],
                },
            }
        ),
        encoding="utf-8",
    )


class MetaHarnessArtifactRegressionTests(unittest.TestCase):
    def test_internal_fenced_snippets_do_not_truncate_full_proof(self) -> None:
        proof = (
            "# Claim\n\nBefore.\n\n```text\nchecksum\n```\n\n"
            "as independently returned by\n\n```text\ncommand\n```\n\n"
            "## References\n\n[1] source\n\n∎"
        )
        self.assertEqual(metaharness_runner.extract_md(proof), proof)

    def test_single_outer_markdown_fence_is_unwrapped(self) -> None:
        wrapped = "```markdown\n# Claim\n\nComplete. ∎\n```"
        self.assertEqual(
            metaharness_runner.extract_md(wrapped),
            "# Claim\n\nComplete. ∎",
        )

    def test_meta_harness_discloses_audit_degradation_on_adapter_failure(self) -> None:
        proof = "# Partial result\n\nA lemma.\n\nProvingConsole outcome: Partial Progress"
        result = metaharness_runner.ensure_audit_disclosure(
            "CANDIDATE AUDIT GATE: run the ensemble.", proof
        )
        self.assertIn("degraded audit: Meta-Harness", result)
        self.assertEqual(result.count("ProvingConsole outcome:"), 1)
        self.assertTrue(
            result.rstrip().endswith("ProvingConsole outcome: Partial Progress")
        )

    def test_meta_harness_file_backed_audit_uses_selected_runtime(self) -> None:
        captured: dict[str, object] = {}

        def model_call(system: str, user: str):
            self.assertEqual(system, "audit system")
            self.assertEqual(user, "audit user")
            return "valid response", {"input_tokens": 3, "output_tokens": 2}

        def fake_audit(**kwargs):
            captured.update(kwargs)
            response = kwargs["call"](
                "audit system", "audit user", model=kwargs["model"], emit_event=lambda _: None
            )
            self.assertEqual(response.text, "valid response")
            self.assertEqual(response.usage["output_tokens"], 2)
            return "# Audited\n\nProvingConsole outcome: Partial Progress"

        with patch.object(metaharness_runner, "run_codex_audit", side_effect=fake_audit):
            result = metaharness_runner.complete_file_backed_audit(
                prompt="CANDIDATE AUDIT GATE: audit it.",
                candidate="# Candidate",
                model="gpt-5.6-sol",
                call=model_call,
            )

        self.assertIn("# Audited", result)
        self.assertEqual(captured["adapter"], "metaharness-response-orchestrated-audit")
        self.assertEqual(captured["runtime_label"], "Meta-Harness")
        self.assertEqual(captured["audit_prefix"], "metaharness")

    def test_meta_harness_inner_loop_defers_file_audit_to_runner(self) -> None:
        self.assertIn("do not lower", metaharness_runner.EVALUATOR_SYSTEM)
        self.assertIn("after this optimization loop", metaharness_runner.EVALUATOR_SYSTEM)
        self.assertIn("never add instructions", metaharness_runner.PROPOSER_SYSTEM)
        self.assertIn("response-only solver", metaharness_runner.PROPOSER_SYSTEM)


class ProviderRoutingRegressionTests(unittest.TestCase):
    def test_improof_audit_contract_promotion_is_bounded_and_symlink_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            native = root / "native"
            output = root / "output"
            audit = native / "audit" / "run-1"
            audit.mkdir(parents=True)
            for name in (
                "global.json",
                "decomposed.json",
                "verifier-output.json",
                "merge-map.json",
                "run.json",
            ):
                (audit / name).write_text("{}", encoding="utf-8")
            (audit / "final-candidate.md").write_text(
                "exact final candidate", encoding="utf-8"
            )
            (audit / "global-rerun.json").write_text("{}", encoding="utf-8")
            (audit / "unrelated.txt").write_text("skip", encoding="utf-8")
            (audit / "linked.json").symlink_to(audit / "global.json")

            promoted = improof_runner._promote_audit_contracts(native, output)

            self.assertEqual(len(promoted), 7)
            self.assertTrue((output / "audit/run-1/global.json").is_file())
            self.assertTrue(
                (output / "audit/run-1/final-candidate.md").is_file()
            )
            self.assertTrue(
                (output / "audit/run-1/global-rerun.json").is_file()
            )
            self.assertFalse((output / "audit/run-1/unrelated.txt").exists())
            self.assertFalse((output / "audit/run-1/linked.json").exists())

            blocked = root / "blocked-output"
            outside = root / "outside"
            blocked.mkdir()
            outside.mkdir()
            (blocked / "audit").symlink_to(outside, target_is_directory=True)
            self.assertEqual(
                improof_runner._promote_audit_contracts(native, blocked),
                [],
            )
            self.assertFalse(any(outside.iterdir()))

    def test_improof_complete_audit_requires_parseable_files_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            audit = workspace / "audit" / "improof-test"
            _install_reconciled_audit(
                workspace, audit, complete_core=False
            )
            self.assertFalse(improof_runner._audit_contracts_complete(workspace))
            manifest = json.loads(
                (audit / "run.json").read_text(encoding="utf-8")
            )
            manifest["complete_core"] = True
            (audit / "run.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            self.assertTrue(improof_runner._audit_contracts_complete(workspace))
            (audit / "global.json").write_text("{broken", encoding="utf-8")
            self.assertFalse(improof_runner._audit_contracts_complete(workspace))

    def test_improof_publishes_only_final_regular_native_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            native = root / "native"
            output = root / "output"
            (native / "solutions").mkdir(parents=True)
            output.mkdir()
            solution = native / "solutions" / "p.tex"
            solution.write_text("rigorous final solution " * 10, encoding="utf-8")
            published = improof_runner._publish_native_candidate(
                native, output, "p"
            )
            self.assertEqual(published, output / "proof.tex")
            self.assertEqual(
                (output / "proof.tex").read_text(encoding="utf-8"),
                solution.read_text(encoding="utf-8"),
            )

    def test_improof_audit_adapter_rejects_symlinked_input(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = root / "candidate.tex"
            candidate.write_text("candidate " * 20, encoding="utf-8")
            (root / "linked.tex").symlink_to(candidate)
            with patch("pathlib.Path.cwd", return_value=root):
                with self.assertRaises(OSError):
                    improof_audit_adapter._regular_workspace_file("linked.tex")

    def test_improof_codex_audit_workflow_uses_isolated_workspace_write(self) -> None:
        preset = (
            Path(__file__).resolve().parents[1]
            / "engines/improof/configs/workflows/codex_author_critic.yaml"
        ).read_text(encoding="utf-8")
        self.assertEqual(preset.count("      - workspace-write"), 2)
        self.assertNotIn("      - read-only", preset)
        self.assertIn("file-backed audit contract", preset)

    def test_improof_supplemental_audit_status_survives_run_wrapping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "proof.md").write_text(
                "# Partial Progress\n\nA conservative result.\n",
                encoding="utf-8",
            )
            run = jobs._wrap_subprocess_result(
                run_id="improof-audit-status",
                engine="improof",
                problem_id="audit-status",
                problem_text="ordinary control prompt",
                result={
                    "status": "finished",
                    "returncode": 0,
                    "workflow": "codex_author_critic",
                    "output_dir": str(workspace),
                    "supplemental_audit_status": "degraded",
                    "supplemental_audit_returncode": 1,
                    "promoted_audit_files": [],
                },
                started=time.time(),
                workspace=workspace,
            )

        recorded = run["runner_result"]
        self.assertEqual(recorded["supplemental_audit_status"], "degraded")
        self.assertEqual(recorded["supplemental_audit_returncode"], 1)
        self.assertEqual(recorded["promoted_audit_files"], [])

    def test_openclaude_audit_prompt_reserves_manifest_last_mile(self) -> None:
        ordinary = "Prove 1 + 1 = 2."
        self.assertEqual(augment_openclaude_audit_prompt(ordinary), ordinary)
        audit = "CANDIDATE AUDIT GATE: audit the candidate."
        augmented = augment_openclaude_audit_prompt(audit)
        self.assertIn("OPENCLAUDE AUDIT LAST-MILE CONTRACT", augmented)
        self.assertIn("run.json", augmented)
        self.assertIn("global_only", augmented)
        self.assertEqual(augment_openclaude_audit_prompt(augmented), augmented)

    def test_openclaude_execution_error_retries_once_after_new_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            calls: list[dict] = []

            def fake_run(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    (workspace / "proof.md").write_text(
                        "# Partial checkpoint\n\n" + "rigorous content " * 40,
                        encoding="utf-8",
                    )
                    return {
                        "run_id": "openclaude-retry",
                        "status": "failed",
                        "error": "OpenClaude exited with code 1: error_during_execution",
                        "agents": [{"trace_id": "first", "status": "failed"}],
                        "edges": [],
                        "totals": {"input_tokens": 10, "output_tokens": 2},
                    }
                return {
                    "run_id": "openclaude-retry",
                    "status": "finished",
                    "error": None,
                    "agents": [{"trace_id": "second", "status": "finished"}],
                    "edges": [],
                    "totals": {"input_tokens": 20, "output_tokens": 4},
                }

            with patch.object(jobs, "_run_cli_engine", side_effect=fake_run):
                result = jobs._run_cli_engine_with_openclaude_retry(
                    run_id="openclaude-retry",
                    engine="openclaude",
                    problem_id="p",
                    problem_text="CANDIDATE AUDIT GATE: audit it.",
                    started=time.time(),
                    workspace=workspace,
                )

            self.assertEqual(len(calls), 2)
            self.assertIn("OPENCLAUDE SAME-HARNESS RECOVERY", calls[1]["problem_text"])
            self.assertEqual(result["status"], "finished")
            self.assertEqual(result["internal_retry_count"], 1)
            self.assertEqual(result["totals"]["input_tokens"], 30)
            self.assertEqual(len(result["agents"]), 2)

    def test_openclaude_execution_error_does_not_retry_stale_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "proof.md").write_text(
                "# Existing checkpoint\n\n" + "old content " * 50,
                encoding="utf-8",
            )
            failed = {
                "run_id": "openclaude-stale",
                "status": "failed",
                "error": "OpenClaude exited with code 1: error_during_execution",
                "agents": [],
                "edges": [],
                "totals": {},
            }
            with patch.object(jobs, "_run_cli_engine", return_value=failed) as run:
                result = jobs._run_cli_engine_with_openclaude_retry(
                    run_id="openclaude-stale",
                    engine="openclaude",
                    problem_id="p",
                    problem_text="continue",
                    started=time.time(),
                    workspace=workspace,
                )

            self.assertEqual(run.call_count, 1)
            self.assertIs(result, failed)

    def test_openclaude_max_turns_retries_only_after_new_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            before = jobs._proof_checkpoint_signature(workspace)
            (workspace / "proof.md").write_text(
                "# Current proof checkpoint\n\n" + "auditable content " * 40,
                encoding="utf-8",
            )
            failed = {
                "status": "failed",
                "error": "OpenClaude exited with code 1: error_max_turns",
            }
            self.assertEqual(
                jobs._openclaude_recoverable_failure(failed["error"]),
                "error_max_turns",
            )
            self.assertTrue(
                jobs._openclaude_execution_retry_needed(
                    engine="openclaude",
                    result=failed,
                    before_signature=before,
                    workspace=workspace,
                )
            )
            current = jobs._proof_checkpoint_signature(workspace)
            self.assertFalse(
                jobs._openclaude_execution_retry_needed(
                    engine="openclaude",
                    result=failed,
                    before_signature=current,
                    workspace=workspace,
                )
            )

    def test_openclaude_repeated_tool_stop_retries_once_after_new_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            calls: list[dict] = []

            def fake_run(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    (workspace / "proof.md").write_text(
                        "# Current proof checkpoint\n\n" + "auditable content " * 40,
                        encoding="utf-8",
                    )
                    return {
                        "run_id": "openclaude-edit-retry",
                        "status": "failed",
                        "error": (
                            "OpenClaude exited with code 1: Stopped: repeated tool "
                            "failures detected.\n\nThe path proof.md failed 3 times."
                        ),
                        "agents": [{"trace_id": "first", "status": "failed"}],
                        "edges": [],
                        "totals": {"input_tokens": 10, "output_tokens": 2},
                    }
                return {
                    "run_id": "openclaude-edit-retry",
                    "status": "finished",
                    "error": None,
                    "agents": [{"trace_id": "second", "status": "finished"}],
                    "edges": [],
                    "totals": {"input_tokens": 20, "output_tokens": 4},
                }

            with patch.object(jobs, "_run_cli_engine", side_effect=fake_run):
                result = jobs._run_cli_engine_with_openclaude_retry(
                    run_id="openclaude-edit-retry",
                    engine="openclaude",
                    problem_id="p",
                    problem_text="CANDIDATE AUDIT GATE: audit it.",
                    started=time.time(),
                    workspace=workspace,
                )

            self.assertEqual(len(calls), 2)
            self.assertIn("use a fresh safe", calls[1]["problem_text"])
            self.assertEqual(result["status"], "finished")
            self.assertEqual(
                result["internal_retry_reason"],
                "repeated_tool_failures_after_new_checkpoint",
            )

    def test_shared_audit_contract_is_explicit_and_idempotent(self) -> None:
        ordinary = "Prove a finite lemma."
        self.assertEqual(jobs._augment_research_audit_prompt(ordinary), ordinary)
        audit = "CANDIDATE AUDIT GATE: audit this candidate."
        augmented = jobs._augment_research_audit_prompt(audit)
        self.assertIn("audit-output-validator.sh", augmented)
        self.assertIn("verifier-output.json", augmented)
        self.assertIn("global_only", augmented)
        self.assertIn("degraded audit:", augmented)
        self.assertEqual(jobs._augment_research_audit_prompt(augmented), augmented)

    def test_codex_backend_uses_read_only_landlock_on_linux(self) -> None:
        captured: dict = {}

        def fake_run(command, **kwargs):
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION + "\n",
                    "",
                )
            if command[1:] == ["debug", "models", "--bundled"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"models": [{"slug": "gpt-5.6-sol"}]}),
                    "",
                )
            captured["command"] = command
            captured["env"] = kwargs["env"]
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text("# Result\n\nRead-only command succeeded.", encoding="utf-8")
            stdout = json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 11, "output_tokens": 3},
                }
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_HOME": "/tmp/codex-test",
                    "AGENT_MONITOR_PYTHON": "/srv/venv/bin/python",
                },
                clear=True,
            ),
            patch.object(codex_backend.sys, "platform", "linux"),
            patch.object(
                codex_backend.shutil, "which", return_value="/usr/local/bin/codex"
            ),
            patch.object(codex_backend, "_run_codex_process", side_effect=fake_run),
        ):
            result = codex_backend.codex_exec("system", "user")

        command = captured["command"]
        self.assertEqual(
            command[command.index("--enable") + 1], "use_legacy_landlock"
        )
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertNotIn("danger-full-access", command)
        self.assertEqual(
            captured["env"]["AGENT_MONITOR_PYTHON"], "/srv/venv/bin/python"
        )
        self.assertEqual(result.usage, {"input_tokens": 11, "output_tokens": 3})

    def test_codex_backend_landlock_compatibility_can_be_disabled(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"AGENT_MONITOR_CODEX_LEGACY_LANDLOCK": "0"},
                clear=True,
            ),
            patch.object(codex_backend.sys, "platform", "linux"),
        ):
            self.assertEqual(
                codex_backend._read_only_sandbox_args(),
                ["--sandbox", "read-only"],
            )

    def test_codex_backend_suppresses_only_landlock_deprecation(self) -> None:
        notice = {
            "type": "item.completed",
            "item": {
                "type": "error",
                "message": "[features].use_legacy_landlock is deprecated",
            },
        }
        self.assertTrue(codex_backend._legacy_landlock_notice(notice))
        self.assertFalse(
            codex_backend._legacy_landlock_notice(
                {
                    "type": "item.completed",
                    "item": {"type": "error", "message": "boom"},
                }
            )
        )

    def test_plain_api_artifact_rejects_pseudo_tool_xml(self) -> None:
        self.assertEqual(_api_artifact_issues("# Clean proof\n\nDone. ∎"), [])
        issues = _api_artifact_issues(
            '<function_calls><invoke name="bash"><parameter name="args">x'
        )
        self.assertIn("<function_calls>", issues)
        self.assertIn("<invoke ", issues)
        self.assertIn("<parameter ", issues)
        self.assertIn(
            "missing level-one final heading",
            _api_artifact_issues("I will analyze this first.\n\n# Result"),
        )

    def test_plain_discloses_file_backed_audit_was_degraded(self) -> None:
        proof = (
            "# Partial result\n\nA bounded lemma.\n\n"
            "ProvingConsole outcome: Partial Progress"
        )
        result = _ensure_audit_disclosure(
            "CANDIDATE AUDIT GATE: read research-proof-audit", proof
        )
        self.assertIn("degraded audit: single-call Plain baseline", result)
        self.assertTrue(
            result.rstrip().endswith("ProvingConsole outcome: Partial Progress")
        )
        self.assertEqual(result.count("ProvingConsole outcome:"), 1)

    def test_plain_outcome_contract_is_fail_closed(self) -> None:
        missing = plain_runner._ensure_outcome_contract("# Result\n\nA lemma.")
        self.assertTrue(
            missing.rstrip().endswith("ProvingConsole outcome: Partial Progress")
        )
        explicit = "# Result\n\nProvingConsole outcome: Known/Open Status\n"
        self.assertEqual(plain_runner._ensure_outcome_contract(explicit), explicit)
        conflicting = plain_runner._ensure_outcome_contract(
            "ProvingConsole outcome: Solved\n\n"
            "ProvingConsole outcome: Counterexample\n"
        )
        self.assertEqual(conflicting.count("ProvingConsole outcome:"), 1)
        self.assertIn("ProvingConsole outcome: Partial Progress", conflicting)
        nonstandard = plain_runner._ensure_outcome_contract(
            "# Result\n\nProvingConsole: Known/Open Status\n"
        )
        self.assertNotIn("ProvingConsole: Known/Open Status", nonstandard)
        self.assertTrue(
            nonstandard.rstrip().endswith(
                "ProvingConsole outcome: Known/Open Status"
            )
        )
        heading = plain_runner._ensure_outcome_contract(
            "# Result\n\n## ProvingConsole Outcome\n\n**Counterexample**\n"
        )
        self.assertNotIn("## ProvingConsole Outcome", heading)
        self.assertTrue(
            heading.rstrip().endswith("ProvingConsole outcome: Counterexample")
        )

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                save_plain_result(
                    "End with ProvingConsole outcome: <label>.", "# Result\n\nA lemma."
                )
                saved = Path("proof.md").read_text(encoding="utf-8")
            finally:
                os.chdir(old_cwd)
        self.assertIn("ProvingConsole outcome: Partial Progress", saved)

    def test_plain_api_retries_pseudo_tool_markup_once_and_sums_usage(self) -> None:
        dirty = api_backend.APIResult(
            text="<function_calls><invoke name=\"bash\"></invoke></function_calls>",
            usage={"input_tokens": 10, "output_tokens": 2},
            model="claude-haiku-4-5",
            provider="anthropic",
        )
        clean = api_backend.APIResult(
            text="# Counterexample\n\nThe claim is false. ∎",
            usage={"input_tokens": 14, "output_tokens": 4},
            model="claude-haiku-4-5",
            provider="anthropic",
        )
        events: list[dict] = []
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                with (
                    patch.dict(os.environ, {"PLAIN_MODEL": "claude-haiku-4-5"}, clear=True),
                    patch.object(sys, "argv", ["plain_runner.py", "refute this"]),
                    patch.object(plain_runner, "api_chat", side_effect=[dirty, clean]) as call_api,
                    patch.object(plain_runner, "emit", side_effect=events.append),
                ):
                    self.assertEqual(plain_runner.main(), 0)
                self.assertEqual(call_api.call_count, 2)
                self.assertEqual(Path("proof.md").read_text(), clean.text)
            finally:
                os.chdir(previous)
        completed = [event for event in events if event.get("type") == "turn.completed"]
        self.assertEqual(
            completed[-1]["usage"], {"input_tokens": 24, "output_tokens": 6}
        )

    def test_selected_api_model_is_routed_to_each_cli_runner(self) -> None:
        for engine, key, value in (
            ("plain", "PLAIN_MODEL", "claude-haiku-4-5"),
            ("metaharness", "METAHARNESS_MODEL", "claude-haiku-4-5"),
            ("deepagents", "DEEPAGENTS_MODEL", "anthropic:claude-haiku-4-5"),
            ("openclaude", "AGENT_MONITOR_OPENCLAUDE_MODEL", "claude-haiku-4-5"),
            ("openclaw", "AGENT_MONITOR_OPENCLAW_MODEL", "anthropic/claude-haiku-4-5"),
        ):
            env: dict[str, str] = {}
            jobs._apply_selected_model_env(
                env, engine=engine, model="claude-haiku-4-5"
            )
            self.assertEqual(env["AGENT_MONITOR_SELECTED_MODEL"], "claude-haiku-4-5")
            self.assertEqual(env[key], value)

        subscription_env = {"AGENT_MONITOR_CODEX_SUBSCRIPTION": "1"}
        jobs._apply_selected_model_env(
            subscription_env, engine="plain", model="gpt-5.6-sol"
        )
        self.assertEqual(subscription_env["AGENT_MONITOR_CODEX_MODEL"], "gpt-5.6-sol")
        self.assertNotIn("PLAIN_MODEL", subscription_env)

    def test_anthropic_api_backend_uses_messages_and_reports_usage(self) -> None:
        response = {
            "content": [{"type": "text", "text": "verified counterexample"}],
            "usage": {"input_tokens": 17, "output_tokens": 5},
        }
        with (
            patch.dict(
                os.environ,
                {
                    "ANTHROPIC_API_KEY": "test-anthropic-secret",
                    "ANTHROPIC_BASE_URL": "https://api.anthropic.test",
                },
                clear=True,
            ),
            patch("agent_monitor.settings._http_json", return_value=(200, response)) as request,
        ):
            result = api_backend.api_chat(
                "system contract", "prove or refute", model="claude-haiku-4-5"
            )
        self.assertEqual(result.text, "verified counterexample")
        self.assertEqual(result.provider, "anthropic")
        self.assertEqual(result.usage, {"input_tokens": 17, "output_tokens": 5})
        args, kwargs = request.call_args
        self.assertEqual(args[0], "https://api.anthropic.test/v1/messages")
        self.assertEqual(kwargs["payload"]["system"], "system contract")
        self.assertEqual(kwargs["payload"]["messages"][0]["content"], "prove or refute")

    def test_kimi_api_backend_uses_chat_completions_and_reports_reasoning_usage(self) -> None:
        response = {
            "choices": [{"message": {"content": "sqrt(3) is irrational"}}],
            "usage": {
                "prompt_tokens": 19,
                "completion_tokens": 11,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 7},
            },
        }
        with (
            patch.dict(
                os.environ,
                {
                    "KIMI_API_KEY": "test-kimi-secret",
                    "KIMI_API_BASE": "https://kimi.example/v1",
                    "KIMI_REASONING_EFFORT": "max",
                },
                clear=True,
            ),
            patch("agent_monitor.settings._http_json", return_value=(200, response)) as request,
        ):
            result = api_backend.api_chat(
                "system contract", "prove sqrt(3) irrational", model="kimi-k3"
            )

        self.assertEqual(result.provider, "kimi")
        self.assertEqual(result.model, "kimi-k3")
        self.assertEqual(result.usage, {
            "input_tokens": 19,
            "output_tokens": 11,
            "reasoning_tokens": 7,
            "cache_read_tokens": 3,
        })
        args, kwargs = request.call_args
        self.assertEqual(args[0], "https://kimi.example/v1/chat/completions")
        self.assertEqual(kwargs["payload"]["reasoning_effort"], "max")
        self.assertEqual(kwargs["payload"]["messages"][0]["role"], "system")
        self.assertNotIn("output_config", kwargs["payload"])
        self.assertNotIn("context_management", kwargs["payload"])

    def test_kimi_model_uses_its_provider_when_openai_is_also_configured(self) -> None:
        response = {"choices": [{"message": {"content": "KIMI_OK"}}]}
        with patch("agent_monitor.settings._http_json", return_value=(200, response)) as request:
            chosen, content, provider = proof_graph._call_llm(
                {
                    "OPENAI_API_KEY": "openai-test",
                    "KIMI_API_KEY": "kimi-test",
                    "KIMI_API_BASE": "https://kimi.example/v1/",
                },
                "kimi-k3",
                "ping",
            )

        self.assertEqual((chosen, content, provider), ("kimi-k3", "KIMI_OK", "kimi"))
        self.assertEqual(request.call_args.args[0], "https://kimi.example/v1/chat/completions")
        self.assertEqual(request.call_args.kwargs["payload"]["model"], "kimi-k3")

    def test_kimi_harness_aliases_are_scoped_to_selected_model(self) -> None:
        env = {
            "KIMI_API_KEY": "kimi-test",
            "KIMI_API_BASE": "https://kimi.example/v1",
            "OPENAI_API_KEY": "original-openai",
            "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
            "AGENT_MONITOR_CODEX_MODEL": "gpt-codex-test",
        }
        jobs._apply_selected_model_env(env, engine="deepagents", model="kimi-k3")
        self.assertEqual(env["OPENAI_API_KEY"], "kimi-test")
        self.assertEqual(env["OPENAI_BASE_URL"], "https://kimi.example/v1")
        self.assertEqual(env["DEEPAGENTS_MODEL"], "openai:kimi-k3")
        self.assertEqual(env["AGENT_MONITOR_AUTH_MODE"], "api_key")
        self.assertNotIn("AGENT_MONITOR_CODEX_SUBSCRIPTION", env)
        self.assertNotIn("AGENT_MONITOR_CODEX_MODEL", env)

        untouched = {"OPENAI_API_KEY": "original-openai", "KIMI_API_KEY": "kimi-test"}
        jobs._apply_selected_model_env(untouched, engine="plain", model="gpt-5-mini")
        self.assertEqual(untouched["OPENAI_API_KEY"], "original-openai")
        self.assertNotIn("OPENAI_BASE_URL", untouched)

    def test_kimi_responses_proxy_sanitizes_codex_extensions_and_tools(self) -> None:
        payload, summary = kimi_responses_proxy.sanitize_responses_payload(
            {
                "model": "kimi-k3",
                "input": "prove it",
                "client_metadata": {"origin": "codex"},
                "context_management": [{"type": "compaction"}],
                "output_config": {"effort": "verbose"},
                "reasoning": {"effort": "xhigh", "summary": "auto"},
                "tools": [
                    {
                        "type": "namespace",
                        "name": "workspace",
                        "tools": [
                            {
                                "name": "exec_command",
                                "description": "Run a command",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"cmd": {"type": "string"}},
                                    "required": ["cmd"],
                                },
                                "strict": True,
                            }
                        ],
                    },
                    {"type": "web_search"},
                ],
            },
            reasoning_effort="max",
            min_output_tokens=32768,
        )

        self.assertNotIn("client_metadata", payload)
        self.assertNotIn("context_management", payload)
        self.assertNotIn("output_config", payload)
        self.assertEqual(payload["reasoning"], {"effort": "max"})
        self.assertEqual(payload["max_output_tokens"], 32768)
        self.assertEqual(summary["max_output_tokens"], 32768)
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["tools"][0]["name"], "exec_command")
        self.assertEqual(summary["forwarded_tool_names"], ["exec_command"])
        self.assertIn("web_search", summary["dropped_tool_types"])

    def test_lean_gap_count_ignores_comments_and_strings(self) -> None:
        source = '''/-- says `sorry` and admit in documentation -/
def note : String := "sorry -- not executable"
-- admit
/- outer sorry /- nested admit -/ still comment -/
theorem main : True := by
  sorry
'''
        self.assertEqual(lean_verify._count_sorry(source), 1)
        self.assertEqual(lean_verify._sorry_lines(source), [6])

    def test_metaharness_evolution_retains_read_only_output_contract(self) -> None:
        proposed = "Evolved proof strategy. " + "x" * 6000
        harness = metaharness_runner.enforce_solver_runtime_contract(proposed)
        self.assertLessEqual(len(harness), 4000)
        self.assertTrue(harness.endswith(metaharness_runner.SOLVER_RUNTIME_CONTRACT))
        self.assertIn("do not attempt filesystem writes", harness)
        self.assertIn("runner saves your response as proof.md", harness)
        self.assertIn("Return ONLY the complete informal proof", harness)

    def test_metaharness_evaluator_preserves_full_normal_proof_and_long_edges(self) -> None:
        normal = "BEGIN\n" + "proof line\n" * 1800 + "END"
        self.assertGreater(len(normal), 8000)
        self.assertEqual(metaharness_runner.evaluator_proof_excerpt(normal), normal)

        long = "THEOREM_HEAD\n" + "x" * 60000 + "\nREFERENCES_TAIL"
        excerpt = metaharness_runner.evaluator_proof_excerpt(long)
        self.assertLessEqual(len(excerpt), 50000)
        self.assertIn("THEOREM_HEAD", excerpt)
        self.assertIn("REFERENCES_TAIL", excerpt)
        self.assertIn("evaluator proof window omitted middle", excerpt)

    def test_openclaude_cli_receives_requested_turn_cap(self) -> None:
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
                    "Path('proof.md').write_text('# Partial Progress\\n\\nA sufficiently long bounded audit artifact for this runner.\\n', encoding='utf-8')"
                ),
            ]
            with (
                patch("agent_monitor.engines_registry.build_cli_command", return_value=command),
                patch("agent_monitor.agent_config.persona_preamble", return_value=""),
                patch.object(jobs, "_write_run"),
                patch("subprocess.Popen", side_effect=capture_popen),
            ):
                result = jobs._run_cli_engine(
                    run_id="openclaude_cap",
                    engine="openclaude",
                    problem_id="cap",
                    problem_text="Audit the claim.",
                    started=time.time(),
                    workspace=workspace,
                    requested_model="kimi-k3",
                    max_iterations=7,
                )

        self.assertEqual(captured_env["AGENT_MONITOR_OPENCLAUDE_MAX_TURNS"], "7")
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["model"], "kimi-k3")
        self.assertEqual(result["live"]["model"], "kimi-k3")

    def test_normalize_run_backfills_historical_top_level_model(self) -> None:
        from agent_monitor.schema import normalize_run

        from_live = normalize_run(
            {
                "run_id": "historical-live",
                "live": {"model": "kimi-k3"},
                "agents": [{"model": "older-model"}],
            },
            engine="openclaw",
        )
        from_agent = normalize_run(
            {
                "run_id": "historical-agent",
                "agents": [{"model": "older-model"}, {"model": "kimi-k3"}],
            },
            engine="improof",
        )

        self.assertEqual(from_live["model"], "kimi-k3")

    def test_openhands_cli_receives_requested_iteration_cap(self) -> None:
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
                    "Path('proof.md').write_text('# Partial Progress\n\nA sufficiently long bounded audit artifact for this runner.\n', encoding='utf-8')"
                ),
            ]
            with (
                patch("agent_monitor.engines_registry.build_cli_command", return_value=command),
                patch("agent_monitor.agent_config.persona_preamble", return_value=""),
                patch.object(jobs, "_write_run"),
                patch("subprocess.Popen", side_effect=capture_popen),
            ):
                result = jobs._run_cli_engine(
                    run_id="openhands_cap",
                    engine="openhands",
                    problem_id="cap",
                    problem_text="Audit the claim.",
                    started=time.time(),
                    workspace=workspace,
                    max_iterations=7,
                )

        self.assertEqual(captured_env["AGENT_MONITOR_OPENHANDS_MAX_ITERATIONS"], "7")
        self.assertIn(result["status"], {"finished", "failed"})

    def test_provenance_deduplicates_legacy_full_proof_copies(self) -> None:
        from harness_dashboard.latex_provenance import _dedupe_identical_contributors

        agents = [
            {
                "trace_id": f"turn-{index}",
                "role": "assistant",
                "call_seq": index,
                "_provenance_text": "# Proof\n\nThe identical complete proof artifact.",
            }
            for index in range(1, 101)
        ]

        deduped = _dedupe_identical_contributors(agents)

        self.assertEqual([agent["trace_id"] for agent in deduped], ["turn-100"])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text(
                agents[0]["_provenance_text"], encoding="utf-8"
            )
            jobs._attach_proof_provenance(agents, workspace)

        self.assertEqual(
            [agent["trace_id"] for agent in agents if agent.get("_provenance_text")],
            ["turn-100"],
        )

    def test_shared_proof_provenance_is_attached_only_to_final_contributor(self) -> None:
        agents = [
            {
                "trace_id": "turn-1",
                "role": "assistant",
                "stage_name": "turn-1 (message)",
                "output": "First draft",
            },
            {
                "trace_id": "turn-2",
                "role": "tools",
                "stage_name": "turn-2 (edit)",
                "output": "Edited proof.md",
            },
            {
                "trace_id": "summary",
                "role": "orchestrator",
                "pipeline_stage": "finalize",
                "stage_name": "session summary",
                "output": "Partial Progress",
            },
        ]
        proof_text = "# Partial Progress\n\nA sufficiently long final proof artifact.\n"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text(proof_text, encoding="utf-8")
            jobs._attach_proof_provenance(agents, workspace)

        attached = [agent for agent in agents if agent.get("_provenance_text")]
        self.assertEqual([agent["trace_id"] for agent in attached], ["summary"])
        self.assertEqual(attached[0]["_provenance_text"], proof_text)
        self.assertNotIn("--- proof.md ---", agents[0]["output"])
        self.assertIn("--- proof.md ---", attached[0]["output"])

    def test_run_record_publication_is_atomic_for_both_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(jobs, "CACHE_DIR", root / "cache"),
                patch.object(jobs, "RUNS_DIR", root / "runs"),
                patch.object(jobs, "_upsert_manifest"),
                patch("agent_monitor.jobs.os.replace", wraps=os.replace) as replace,
            ):
                cache_path = jobs._write_run(
                    {
                        "run_id": "atomic_record",
                        "status": "running",
                        "problem_text": "Prove the atomic publication claim.",
                        "agents": [{"output": "x" * 100_000}],
                    }
                )

            live_path = root / "runs" / "atomic_record.json"
            self.assertEqual(replace.call_count, 2)
            self.assertEqual(
                json.loads(cache_path.read_text(encoding="utf-8"))["run_id"],
                "atomic_record",
            )
            self.assertEqual(
                json.loads(live_path.read_text(encoding="utf-8"))["run_id"],
                "atomic_record",
            )
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_auth_connection_context_always_closes(self) -> None:
        connection = unittest.mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchall.return_value = []
        with (
            patch("agent_monitor.auth.sqlite3.connect", return_value=connection),
            patch.object(auth, "_INITIALIZED", True),
        ):
            self.assertEqual(auth.get_user_env(7), {})

        connection.__exit__.assert_called_once()
        connection.close.assert_called_once()

    def test_register_after_stop_kills_isolated_process_group(self) -> None:
        run_id = "stop_before_register"
        proc = unittest.mock.Mock()
        proc.pid = 4242
        jobs._stop_event(run_id).set()
        try:
            with (
                patch("agent_monitor.jobs.os.getpgid", return_value=4242),
                patch("agent_monitor.jobs.os.killpg") as killpg,
            ):
                jobs._register_proc(run_id, proc)

            killpg.assert_called_once_with(4242, signal.SIGKILL)
            proc.kill.assert_not_called()
        finally:
            jobs._unregister_proc(run_id)
            jobs._stop_event(run_id).clear()

    def test_registered_process_without_own_group_falls_back_to_direct_kill(self) -> None:
        proc = unittest.mock.Mock()
        proc.pid = 4242
        with (
            patch("agent_monitor.jobs.os.getpgid", return_value=99),
            patch("agent_monitor.jobs.os.killpg") as killpg,
        ):
            jobs._terminate_registered_proc(proc)

        killpg.assert_not_called()
        proc.kill.assert_called_once_with()

    def test_silent_subprocess_emits_live_heartbeat(self) -> None:
        updates: list[str] = []
        output, returncode, timed_out = stream_subprocess(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(0.6); print('finished', flush=True)",
            ],
            cwd=str(Path.cwd()),
            env=os.environ.copy(),
            timeout=5,
            on_output=updates.append,
        )

        self.assertEqual(returncode, 0)
        self.assertFalse(timed_out)
        self.assertEqual(output, "finished\n")
        self.assertTrue(updates)
        self.assertEqual(updates[0], "")
        self.assertEqual(updates[-1], "finished\n")

    def test_silent_cli_engine_refreshes_live_run(self) -> None:
        updates: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            command = [
                sys.executable,
                "-c",
                (
                    "import time; from pathlib import Path; time.sleep(2.3); "
                    "Path('proof.md').write_text('# Partial Progress\\n\\nThis checked artifact is complete enough for the runner.\\n', "
                    "encoding='utf-8')"
                ),
            ]
            with (
                patch(
                    "agent_monitor.engines_registry.build_cli_command",
                    return_value=command,
                ),
                patch(
                    "agent_monitor.agent_config.persona_preamble",
                    return_value="",
                ),
                patch.object(
                    jobs,
                    "_write_run",
                    side_effect=lambda run: updates.append(dict(run)),
                ),
            ):
                result = jobs._run_cli_engine(
                    run_id="silent_cli",
                    engine="plain",
                    problem_id="silent",
                    problem_text="INJECTED LIBRARY CONTEXT\nWait, then write the proof.",
                    display_problem_text="Wait, then write the proof.",
                    started=time.time(),
                    workspace=workspace,
                )

        running = [run for run in updates if run.get("status") == "running"]
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["problem_text"], "Wait, then write the proof.")
        self.assertTrue(
            all(
                run.get("problem_text") == "Wait, then write the proof."
                for run in running
            )
        )
        self.assertIn("INJECTED LIBRARY CONTEXT", result["agents"][0]["prompt"])
        self.assertGreaterEqual(len(running), 2)
        self.assertGreater(
            max(float((run.get("totals") or {}).get("latency_s") or 0) for run in running),
            0.25,
        )

    def test_hermes_keeps_library_context_out_of_canonical_problem(self) -> None:
        updates: list[dict] = []

        class FakeAgent:
            model = "gpt-test"
            session_input_tokens = 10
            session_output_tokens = 4
            session_estimated_cost_usd = 0.01

            def run_conversation(self, prompt):
                self.prompt = prompt
                return {
                    "completed": True,
                    "final_response": "# Partial Progress\n\nA valid bounded result.",
                    "messages": [],
                    "input_tokens": 10,
                    "output_tokens": 4,
                }

        agent = FakeAgent()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(hermes_runner, "create_agent", return_value=agent),
                patch.object(hermes_runner, "close_agent"),
                patch.object(
                    jobs, "_write_run", side_effect=lambda run: updates.append(dict(run))
                ),
                patch.object(
                    jobs, "_attach_analysis", side_effect=lambda run, _ws: run
                ),
            ):
                result = jobs._run_hermes(
                    run_id="hermes-canonical",
                    problem_id="canonical",
                    problem_text="USER LIBRARY\nCanonical theorem.",
                    display_problem_text="Canonical theorem.",
                    model="gpt-test",
                    max_iterations=2,
                    started=time.time(),
                    workspace=workspace,
                    use_subagents=False,
                )

        self.assertEqual(result["problem_text"], "Canonical theorem.")
        self.assertTrue(updates)
        self.assertTrue(
            all(run.get("problem_text") == "Canonical theorem." for run in updates)
        )
        self.assertIn("USER LIBRARY", agent.prompt)

    def test_hermes_explicit_audit_gets_one_same_session_last_mile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            class FakeAgent:
                model = "gpt-test"
                session_input_tokens = 20
                session_output_tokens = 8
                session_estimated_cost_usd = 0.02

                def __init__(self) -> None:
                    self.calls: list[tuple[str, object]] = []

                def run_conversation(self, prompt, conversation_history=None):
                    self.calls.append((prompt, conversation_history))
                    if len(self.calls) == 1:
                        proof = (
                            "# Partial Progress\n\n"
                            + "A bounded, self-derived lemma with unresolved obligations. " * 12
                            + "\n\nProvingConsole outcome: Partial Progress\n"
                        )
                        return {
                            "completed": True,
                            "final_response": "--- proof.md ---\n" + proof,
                            "messages": [{"role": "assistant", "content": "drafted"}],
                            "input_tokens": 11,
                            "output_tokens": 5,
                            "tool_call_count": 2,
                        }

                    audit = workspace / "audit" / "hermes-last-mile"
                    _install_reconciled_audit(workspace, audit)
                    return {
                        "completed": True,
                        "final_response": "Completed proof.md and the required audit artifacts.",
                        "messages": [{"role": "assistant", "content": "audited"}],
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "tool_call_count": 4,
                    }

            agent = FakeAgent()
            with (
                patch.object(hermes_runner, "create_agent", return_value=agent),
                patch.object(hermes_runner, "close_agent"),
                patch.object(jobs, "_write_run"),
                patch.object(jobs, "_attach_analysis", side_effect=lambda run, _ws: run),
            ):
                result = jobs._run_hermes(
                    run_id="hermes-audit-last-mile",
                    problem_id="audit-last-mile",
                    problem_text=(
                        "Audit a bounded lemma.\n\n"
                        "CANDIDATE AUDIT GATE: run the installed ensemble audit."
                    ),
                    display_problem_text="Audit a bounded lemma.",
                    model="gpt-test",
                    max_iterations=2,
                    started=time.time(),
                    workspace=workspace,
                    use_subagents=False,
                )
            audit_ready = jobs._research_audit_artifacts_ready(workspace)

        self.assertEqual(len(agent.calls), 2)
        self.assertIn("Mandatory audit last mile", agent.calls[1][0])
        self.assertEqual(
            agent.calls[1][1], [{"role": "assistant", "content": "drafted"}]
        )
        self.assertTrue(audit_ready)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["agents"][-1]["tool_calls"], 6)

    def test_hermes_codex_progress_is_bridged_to_live_tool_telemetry(self) -> None:
        class FakeAgent:
            model = "gpt-test"
            session_input_tokens = 10
            session_output_tokens = 4
            session_estimated_cost_usd = 0.01

            def run_conversation(self, _prompt):
                self.tool_progress_callback(
                    "tool.started", "apply_patch", "proof.md", {"path": "proof.md"}
                )
                self.tool_progress_callback(
                    "tool.completed",
                    "apply_patch",
                    None,
                    {"path": "proof.md", "status": "completed"},
                    result="Done!",
                )
                return {
                    "completed": True,
                    "final_response": "# Partial Progress\n\nA checked artifact.",
                    "messages": [],
                    "input_tokens": 10,
                    "output_tokens": 4,
                }

        agent = FakeAgent()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(hermes_runner, "create_agent", return_value=agent),
                patch.object(hermes_runner, "close_agent"),
                patch("agent_monitor.codex_login.account_home", return_value=Path(tmp)),
                patch("agent_monitor.codex_login.account_login_ready", return_value=True),
                patch.object(jobs, "_write_run"),
                patch.object(jobs, "_attach_analysis", side_effect=lambda run, _ws: run),
            ):
                result = jobs._run_hermes(
                    run_id="hermes-codex-tools",
                    problem_id="codex-tools",
                    problem_text="Prove a bounded lemma.",
                    display_problem_text="Prove a bounded lemma.",
                    model="gpt-test",
                    max_iterations=2,
                    started=time.time(),
                    workspace=workspace,
                    use_subagents=False,
                    owner_id=7,
                    extra_env={"AGENT_MONITOR_CODEX_SUBSCRIPTION": "1"},
                )

        self.assertEqual(result["live"]["tool_calls"], 1)
        self.assertEqual(result["live"]["iteration"], 1)
        self.assertEqual(result["live"]["tools_active"], [])
        self.assertEqual(result["live"]["tools_recent"][0]["name"], "apply_patch")
        self.assertEqual(result["live"]["tools_recent"][0]["state"], "done")
        self.assertEqual(result["live"]["tools_recent"][0]["result"], "Done!")
        self.assertIn("completed", result["live"]["tools_recent"][0]["args"])
        self.assertEqual(result["agents"][-1]["tool_calls"], 1)

    def test_hermes_codex_tool_mapper_handles_start_and_completion(self) -> None:
        from agent_monitor.paths import ensure_import_paths

        ensure_import_paths()
        from agent.codex_runtime import _codex_note_to_tool_progress

        item = {
            "type": "commandExecution",
            "command": "python check.py",
            "cwd": "/workspace",
        }
        started = {"method": "item/started", "params": {"item": item}}
        completed = {"method": "item/completed", "params": {"item": item}}
        expected = (
            "exec_command",
            "python check.py",
            {"command": "python check.py", "cwd": "/workspace"},
        )
        self.assertEqual(_codex_note_to_tool_progress(started), expected)
        self.assertEqual(_codex_note_to_tool_progress(completed), expected)
        self.assertIsNone(
            _codex_note_to_tool_progress(
                {"method": "item/completed", "params": {"item": {"type": "agentMessage"}}}
            )
        )
        web_item = {
            "id": "ws-1",
            "type": "webSearch",
            "query": "Erdos problem 1210",
            "action": {"type": "search", "query": "Erdos problem 1210"},
            "results": [{"url": "https://example.test/1210"}],
        }
        web_note = {"method": "item/completed", "params": {"item": web_item}}
        self.assertEqual(
            _codex_note_to_tool_progress(web_note),
            (
                "web_search",
                "Erdos problem 1210",
                {
                    "query": "Erdos problem 1210",
                    "action": {"type": "search", "query": "Erdos problem 1210"},
                },
            ),
        )

        from agent.transports.codex_event_projector import CodexEventProjector

        projected = CodexEventProjector().project(web_note)
        self.assertTrue(projected.is_tool_iteration)
        self.assertEqual(
            projected.messages[0]["tool_calls"][0]["function"]["name"],
            "web_search",
        )
        self.assertIn("example.test/1210", projected.messages[1]["content"])

    def test_improof_codex_uses_isolated_workspace_and_research_packet(self) -> None:
        captured: dict[str, object] = {}

        def fake_stream(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return "ok", 0, False

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            problem = workspace / "problem.txt"
            problem.write_text("Prove 1 = 1", encoding="utf-8")
            library_dir = workspace / "_library"
            library_dir.mkdir()
            (library_dir / "marker.txt").write_text("scoped", encoding="utf-8")
            prior_proof = "full continuation notebook\n" + "x" * 12000
            (workspace / "proof.tex").write_text(prior_proof, encoding="utf-8")
            with (
                patch(
                    "agent_monitor.runners._stream.stream_subprocess",
                    side_effect=fake_stream,
                ),
                patch.object(
                    improof_runner,
                    "_research_packet",
                    return_value="audited packet",
                ) as research_packet,
            ):
                result = improof_runner.run_problem(
                    problem,
                    problem_id="scoped",
                    output_dir=workspace,
                    extra_env={"AGENT_MONITOR_CODEX_SUBSCRIPTION": "1"},
                    research_model="gpt-test",
                )

            cmd = captured["cmd"]
            run_index = cmd.index("--run-id")
            native_run_id = cmd[run_index + 1]
            native_workspace = workspace.resolve() / native_run_id
            input_index = cmd.index("--input")
            self.assertEqual(cmd[input_index + 1], f"workspace={native_workspace}")
            self.assertEqual(
                (native_workspace / "_library" / "marker.txt").read_text(
                    encoding="utf-8"
                ),
                "scoped",
            )
            self.assertEqual(
                (native_workspace / "proof.tex").read_text(encoding="utf-8"),
                prior_proof,
            )
            self.assertEqual(result["workflow_run_dir"], str(native_workspace))
            self.assertEqual(result["status"], "finished")
            self.assertIn("research_packet=audited packet", cmd)
            research_packet.assert_called_once()

        workflow = (
            Path(__file__).parents[1]
            / "engines"
            / "improof"
            / "configs"
            / "workflows"
            / "codex_author_critic.yaml"
        ).read_text(encoding="utf-8")
        self.assertEqual(workflow.count("workspace_input: workspace"), 2)
        self.assertEqual(workflow.count("codex_sandbox: none"), 2)
        self.assertEqual(workflow.count("      - workspace-write"), 2)
        self.assertEqual(workflow.count("{research_packet}"), 2)
        self.assertNotIn("sandbox_workspace_write.network_access", workflow)
        self.assertNotIn("dangerously-bypass-approvals-and-sandbox", workflow)
        self.assertNotIn("allow_workspace_outside_run", workflow)
        self.assertIn("    - id: final_revision", workflow)
        self.assertIn("solution: $node.final_revision.answer_tex", workflow)
        self.assertIn("tex: $node.final_revision.answer_tex", workflow)

    def test_improof_research_packet_uses_typed_host_tools(self) -> None:
        planner_commands: list[list[str]] = []
        host_commands: list[list[str]] = []

        class FakePlanner:
            pid = 4242
            returncode = 0

            def __init__(self, cmd, **kwargs):
                self.cmd = cmd
                planner_commands.append(cmd)

            def communicate(self, prompt, timeout):
                output = Path(self.cmd[self.cmd.index("--output-last-message") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "requires_research": True,
                            "queries": [
                                "Erdos problem 348 complete sequences",
                                "complete sequence deletion weak completeness",
                                "subcomplete sequence deletion theorem",
                            ],
                            "source_urls": ["https://www.erdosproblems.com/348"],
                        }
                    ),
                    encoding="utf-8",
                )
                return (
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 10, "output_tokens": 3},
                        }
                    ),
                    "recoverable model-cache warning",
                )

        def fake_host_run(cmd, **kwargs):
            host_commands.append(cmd)
            return unittest.mock.Mock(
                stdout="BOUNDED_RESULT", stderr="", returncode=0
            )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tools = workspace / "_library" / "tools"
            tools.mkdir(parents=True)
            (tools / "literature-search.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (tools / "primary-source-fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            with (
                patch.object(improof_runner.subprocess, "Popen", FakePlanner),
                patch.object(
                    improof_runner.subprocess, "run", side_effect=fake_host_run
                ),
                patch.object(
                    improof_runner.shutil, "which", return_value="/usr/bin/codex"
                ),
            ):
                packet = improof_runner._research_packet(
                    problem_text="Audit Erdős problem #348.",
                    workspace=workspace,
                    env=os.environ.copy(),
                    model="gpt-test",
                )

            self.assertIn("Host-generated research packet", packet)
            self.assertIn("PLANNER_USAGE", packet)
            self.assertNotIn("PLANNER_DIAGNOSTIC", packet)
            self.assertIn("BOUNDED_RESULT", packet)
            self.assertEqual(len(host_commands), 4)
            self.assertTrue(all(cmd[0] == "bash" for cmd in host_commands))
            self.assertIn("--disable", planner_commands[0])
            self.assertIn("code_mode_host", planner_commands[0])
            self.assertEqual(
                planner_commands[0][planner_commands[0].index("--sandbox") + 1],
                "read-only",
            )

    def test_improof_research_packet_prefers_arxiv_full_text_and_keeps_edges(self) -> None:
        host_commands: list[list[str]] = []

        class FakePlanner:
            pid = 4242

            def __init__(self, cmd, **kwargs):
                self.cmd = cmd

            def communicate(self, prompt, timeout):
                output = Path(self.cmd[self.cmd.index("--output-last-message") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "requires_research": True,
                            "queries": [
                                "Erdos problem 413 omega",
                                "omega endpoint inequality",
                                "distinct prime factors endpoint",
                            ],
                            "source_urls": ["https://arxiv.org/abs/2604.15042"],
                        }
                    ),
                    encoding="utf-8",
                )
                return "", ""

        def fake_host_run(cmd, **kwargs):
            host_commands.append(cmd)
            if cmd[-1] == "https://arxiv.org/html/2604.15042":
                return unittest.mock.Mock(
                    stdout="THEOREM_HEAD\n" + "x" * 26000 + "\nSOURCE_TAIL",
                    stderr="",
                    returncode=0,
                )
            return unittest.mock.Mock(stdout="INDEX_RESULT", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tools = workspace / "_library" / "tools"
            tools.mkdir(parents=True)
            (tools / "literature-search.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (tools / "primary-source-fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            with (
                patch.object(improof_runner.subprocess, "Popen", FakePlanner),
                patch.object(
                    improof_runner.subprocess, "run", side_effect=fake_host_run
                ),
                patch.object(
                    improof_runner.shutil, "which", return_value="/usr/bin/codex"
                ),
            ):
                packet = improof_runner._research_packet(
                    problem_text="Audit Erdős problem #413.",
                    workspace=workspace,
                    env=os.environ.copy(),
                    model="gpt-test",
                )

        source_commands = [
            cmd for cmd in host_commands if cmd[-1].startswith("https://arxiv.org/")
        ]
        self.assertEqual(
            source_commands[0][-1], "https://arxiv.org/html/2604.15042"
        )
        self.assertEqual(len(source_commands), 1)
        self.assertIn("FULL_TEXT_FETCH https://arxiv.org/html/2604.15042", packet)
        self.assertIn("THEOREM_HEAD", packet)
        self.assertIn("SOURCE_TAIL", packet)
        self.assertIn("bounded capture omitted middle", packet)

    def test_improof_research_packet_falls_back_to_arxiv_abstract(self) -> None:
        source_commands: list[list[str]] = []

        class FakePlanner:
            pid = 4242

            def __init__(self, cmd, **kwargs):
                self.cmd = cmd

            def communicate(self, prompt, timeout):
                output = Path(self.cmd[self.cmd.index("--output-last-message") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "requires_research": True,
                            "queries": ["query one", "query two", "query three"],
                            "source_urls": ["https://arxiv.org/abs/2604.15042"],
                        }
                    ),
                    encoding="utf-8",
                )
                return "", ""

        def fake_host_run(cmd, **kwargs):
            if cmd[-1].startswith("https://arxiv.org/"):
                source_commands.append(cmd)
            if cmd[-1] == "https://arxiv.org/html/2604.15042":
                return unittest.mock.Mock(
                    stdout="", stderr="html unavailable", returncode=1
                )
            return unittest.mock.Mock(
                stdout="ABSTRACT_FALLBACK", stderr="", returncode=0
            )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tools = workspace / "_library" / "tools"
            tools.mkdir(parents=True)
            (tools / "literature-search.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (tools / "primary-source-fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            with (
                patch.object(improof_runner.subprocess, "Popen", FakePlanner),
                patch.object(
                    improof_runner.subprocess, "run", side_effect=fake_host_run
                ),
                patch.object(
                    improof_runner.shutil, "which", return_value="/usr/bin/codex"
                ),
            ):
                packet = improof_runner._research_packet(
                    problem_text="Audit Erdős problem #413.",
                    workspace=workspace,
                    env=os.environ.copy(),
                    model="gpt-test",
                )

        self.assertEqual(
            [cmd[-1] for cmd in source_commands],
            [
                "https://arxiv.org/html/2604.15042",
                "https://arxiv.org/abs/2604.15042",
            ],
        )
        self.assertIn("ABSTRACT_FALLBACK", packet)
        self.assertIn("SOURCE_FETCH_DIAGNOSTIC", packet)

    def test_improof_research_packet_skips_host_search_for_self_contained_task(self) -> None:
        class FakePlanner:
            pid = 4242

            def __init__(self, cmd, **kwargs):
                self.cmd = cmd

            def communicate(self, prompt, timeout):
                output = Path(self.cmd[self.cmd.index("--output-last-message") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "requires_research": False,
                            "queries": [],
                            "source_urls": [],
                        }
                    ),
                    encoding="utf-8",
                )
                return "", ""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(improof_runner.subprocess, "Popen", FakePlanner),
                patch.object(improof_runner.subprocess, "run") as host_run,
                patch.object(
                    improof_runner.shutil, "which", return_value="/usr/bin/codex"
                ),
            ):
                packet = improof_runner._research_packet(
                    problem_text=(
                        "Self-contained finite graph counterexample; "
                        "no sources needed."
                    ),
                    workspace=workspace,
                    env=os.environ.copy(),
                    model="gpt-test",
                )

        self.assertIn("RESEARCH_REQUIRED false", packet)
        self.assertNotIn("## Query", packet)
        self.assertNotIn("## Source", packet)
        host_run.assert_not_called()

    def test_ucla_selected_model_reaches_harness_environment(self) -> None:
        captured: dict[str, object] = {}

        def fake_stream(*args, **kwargs):
            captured.update(kwargs["env"])
            return "ok", 0, False

        with tempfile.TemporaryDirectory() as tmp:
            problem = Path(tmp) / "problem.txt"
            problem.write_text("Prove 1 = 1", encoding="utf-8")
            with patch(
                "agent_monitor.runners._stream.stream_subprocess",
                side_effect=fake_stream,
            ):
                result = ucla_runner.run_problem(
                    problem,
                    model="gpt-selected",
                    output_dir=tmp,
                    extra_env={"MODEL": "stale-default"},
                )

        self.assertEqual(result["status"], "finished")
        self.assertEqual(captured["MODEL"], "gpt-selected")

    def test_ucla_kimi_model_reaches_every_stage_and_disables_live_literature_search(self) -> None:
        captured: dict[str, object] = {}

        def fake_stream(*args, **kwargs):
            captured.update(kwargs["env"])
            return "ok", 0, False

        with tempfile.TemporaryDirectory() as tmp:
            problem = Path(tmp) / "problem.txt"
            problem.write_text("Prove 1 = 1", encoding="utf-8")
            with patch(
                "agent_monitor.runners._stream.stream_subprocess",
                side_effect=fake_stream,
            ):
                result = ucla_runner.run_problem(
                    problem,
                    model="kimi-k3",
                    output_dir=tmp,
                    extra_env={
                        "BENCHMARK_MODEL": "stale-benchmark",
                        "SUMMARIZE_MODEL": "stale-summary",
                        "TYPESET_MODEL": "stale-typeset",
                        "LIT_ENABLED": "1",
                    },
                )

        self.assertEqual(result["status"], "finished")
        for variable in (
            "MODEL",
            "BENCHMARK_MODEL",
            "SUMMARIZE_MODEL",
            "TYPESET_MODEL",
        ):
            self.assertEqual(captured[variable], "kimi-k3")
        self.assertEqual(captured["LIT_ENABLED"], "0")

    def test_ucla_kimi_request_adapter_strips_unsupported_responses_fields(self) -> None:
        source_path = (
            Path(__file__).parents[1] / "engines" / "ucla" / "harness_0518_Final.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        names = {
            "_is_kimi_model",
            "_normalize_response_effort",
            "_response_request_kwargs",
            "_lower_reasoning_effort",
            "_kimi_error_retry_state",
        }
        selected = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in names
        ]
        module = ast.Module(body=selected, type_ignores=[])
        namespace = {"BACKGROUND": True, "KIMI_ERROR_RETRY_LIMIT": 3}
        exec(compile(module, str(source_path), "exec"), namespace)

        build = namespace["_response_request_kwargs"]
        kimi = build("kimi-k3", "prove", "xhigh", "medium", 4096, True)
        self.assertEqual(
            kimi,
            {
                "model": "kimi-k3",
                "input": "prove",
                "max_output_tokens": 4096,
                "reasoning": {"effort": "max"},
            },
        )
        self.assertEqual(
            namespace["_lower_reasoning_effort"]("max", kimi=True), "high"
        )

        openai = build("gpt-test", "prove", "xhigh", "medium", 4096, True)
        self.assertEqual(openai["text"], {"verbosity": "medium"})
        self.assertTrue(openai["background"])
        self.assertEqual(openai["service_tier"], "priority")
        self.assertEqual(openai["tools"], [{"type": "web_search"}])

        retry_state = namespace["_kimi_error_retry_state"]
        bad_request = type("BadRequest", (), {"status_code": 400})()
        rate_limit = type("RateLimit", (), {"status_code": 429})()
        self.assertEqual(retry_state(bad_request, 1), (True, 400, True))
        self.assertEqual(retry_state(rate_limit, 1), (False, 429, False))
        self.assertEqual(retry_state(rate_limit, 3), (True, 429, False))

    def test_ucla_skip_benchmark_keeps_legacy_typeset_candidate_defined(self) -> None:
        source = (
            Path(__file__).parents[1] / "engines" / "ucla" / "harness_0518_Final.py"
        ).read_text(encoding="utf-8")
        stage = source.index("# ── Stage 8: Benchmark documentation")
        candidate = source.index("best_entry = None", stage)
        skip = source.index("if SKIP_BENCHMARK:", stage)
        legacy_typeset = source.index("if best_entry:", skip)
        self.assertLess(candidate, skip)
        self.assertLess(skip, legacy_typeset)
        self.assertIn(
            '"Verified solution found. See solution.tex."\n'
            "        if SKIP_BENCHMARK",
            source,
        )

    def test_ucla_background_response_emits_poll_status(self) -> None:
        source = (
            Path(__file__).parents[1] / "engines" / "ucla" / "harness_0518_Final.py"
        ).read_text(encoding="utf-8")
        self.assertIn("last_poll_report", source)
        self.assertIn("status={response.status}", source)
        self.assertIn("now - last_poll_report >= 30", source)

    def test_ucla_permanent_http_error_skips_backoff(self) -> None:
        import importlib.util as importlib_util
        import urllib.error

        root = Path(__file__).parents[1]
        ucla_dir = root / "engines" / "ucla"
        spec = importlib_util.spec_from_file_location(
            "ucla_deep_read_retry_test", ucla_dir / "deep_read.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib_util.module_from_spec(spec)
        sys.path.insert(0, str(ucla_dir))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(ucla_dir))

        error = urllib.error.HTTPError(
            "https://arxiv.org/pdf/missing.pdf", 404, "Not Found", {}, None
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(module, "_ssl_urlopen", side_effect=error) as urlopen,
            patch.object(module, "_curl_download", return_value=False) as curl,
            patch.object(module.time, "sleep") as sleep,
        ):
            ok = module._download(
                "https://arxiv.org/pdf/missing.pdf", Path(tmp) / "paper.pdf", timeout=1
            )

        self.assertFalse(ok)
        self.assertEqual(urlopen.call_count, 1)
        curl.assert_called_once()
        sleep.assert_not_called()

    def test_ucla_literature_reads_use_bounded_default_budgets(self) -> None:
        source = (
            Path(__file__).parents[1] / "engines" / "ucla" / "harness_0518_Final.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'LIT_SEARCH_MAX_TOKENS    = env_int ("LIT_SEARCH_MAX_TOKENS",    16000)',
            source,
        )
        self.assertIn(
            'LIT_READ_MAX_TOKENS      = env_int ("LIT_READ_MAX_TOKENS",      32000)',
            source,
        )
        self.assertIn(
            'DEEP_READ_EXTRACT_MAX_TOKENS = env_int ("DEEP_READ_EXTRACT_MAX_TOKENS", 32000)',
            source,
        )

    def test_ucla_terminal_output_truncation_adapts_and_is_bounded(self) -> None:
        source = (
            Path(__file__).parents[1] / "engines" / "ucla" / "harness_0518_Final.py"
        ).read_text(encoding="utf-8")
        self.assertIn("TERMINAL_RETRY_LIMIT", source)
        self.assertIn("class TerminalResponseExhausted", source)
        self.assertIn("effective_reasoning = _lower_reasoning_effort", source)
        self.assertIn("effective_max_tokens = min(", source)
        self.assertIn('info["terminal_status"] = response.status', source)
        self.assertIn("except TerminalResponseExhausted:", source)
        self.assertIn("unbounded identical retry loop", source)

    def test_ucla_pdf_parser_is_a_main_runtime_dependency(self) -> None:
        root = Path(__file__).parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        deep_read = (root / "engines" / "ucla" / "deep_read.py").read_text(
            encoding="utf-8"
        )
        literature = (
            root / "engines" / "ucla" / "literature_research.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"PyMuPDF>=1.24,<2"', pyproject)
        self.assertIn("def _require_pdf_parser()", deep_read)
        self.assertIn("_require_pdf_parser()", literature)

    def test_ucla_advisor_does_not_trust_failed_paper_extractions(self) -> None:
        source = (
            Path(__file__).parents[1] / "engines" / "ucla" / "harness_0518_Final.py"
        ).read_text(encoding="utf-8")
        self.assertIn("usable_count = sum(", source)
        self.assertIn("Stage 0 successfully read 0 papers", source)
        self.assertIn("Failed entries are URL leads, not paper-level evidence", source)
        self.assertNotIn("Treat those extractions as your primary source", source)

    def test_hermes_one_shot_cleanup_closes_codex_session(self) -> None:
        agent = unittest.mock.Mock()
        session = unittest.mock.Mock()
        agent._codex_session = session

        close_hermes_agent(agent, messages=[{"role": "assistant", "content": "done"}])

        agent.shutdown_memory_provider.assert_called_once()
        session.close.assert_called_once_with()
        self.assertIsNone(agent._codex_session)
        agent.close.assert_called_once_with()

    def test_hermes_monitor_codex_is_workspace_scoped_and_patch_enabled(self) -> None:
        class FakeAgent:
            def __init__(self, **kwargs):
                self.model = kwargs.get("model")

        fake_module = type("FakeRunAgent", (), {"AIAgent": FakeAgent})
        with (
            patch.object(hermes_runner, "_configure_hermes_home"),
            patch.object(hermes_runner, "ensure_import_paths"),
            patch.dict(sys.modules, {"run_agent": fake_module}),
            patch("agent_monitor.agent_config.memory_enabled", return_value=False),
        ):
            agent = hermes_runner.create_agent(
                model="gpt-test",
                codex_home="/tmp/codex-test",
                use_codex_subscription=True,
                enable_subagents=False,
            )

        self.assertTrue(agent._monitor_auto_approve_apply_patch)
        self.assertEqual(agent._monitor_codex_network_domains, [])
        self.assertEqual(agent._monitor_codex_home, "/tmp/codex-test")

    def test_hermes_scoped_network_proxy_is_explicit_opt_in(self) -> None:
        with patch.dict(
            os.environ, {"AGENT_MONITOR_HERMES_SCOPED_NETWORK": "1"}, clear=False
        ):
            self.assertIn("**.arxiv.org", hermes_runner._codex_network_domains())

    def test_hermes_codex_session_uses_scoped_network_proxy(self) -> None:
        from agent_monitor.paths import ensure_import_paths

        ensure_import_paths()
        from agent.transports.codex_app_server_session import CodexAppServerSession

        captured: dict[str, object] = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def initialize(self, **kwargs):
                captured["initialize"] = kwargs

            def request(self, method, params, timeout=None):
                captured["request"] = (method, params, timeout)
                return {"thread": {"id": "thread-test"}}

            def close(self):
                pass

        with patch.dict(
            os.environ, {"AGENT_MONITOR_CODEX_LEGACY_LANDLOCK": "0"}
        ):
            session = CodexAppServerSession(
                cwd="/tmp/run-test",
                network_domains=["**.arxiv.org", "**.crossref.org"],
                client_factory=FakeClient,
            )
            try:
                self.assertEqual(session.ensure_started(), "thread-test")
            finally:
                session.close()

        args = captured["extra_args"]
        self.assertIn("sandbox_workspace_write.network_access=true", args)
        self.assertIn("features.network_proxy.enabled=true", args)
        domains = next(
            value
            for value in args
            if value.startswith("features.network_proxy.domains=")
        )
        self.assertIn('"**.arxiv.org"="allow"', domains)
        self.assertNotIn("danger-full-access", " ".join(args))

    def test_hermes_codex_session_uses_landlock_on_linux(self) -> None:
        from agent_monitor.paths import ensure_import_paths

        ensure_import_paths()
        from agent.transports.codex_app_server_session import CodexAppServerSession

        captured: dict[str, object] = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def initialize(self, **_kwargs):
                pass

            def request(self, _method, _params, timeout=None):
                return {"thread": {"id": "thread-landlock"}}

            def close(self):
                pass

        with (
            patch(
                "agent.transports.codex_app_server_session.sys.platform",
                "linux",
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            session = CodexAppServerSession(
                cwd="/tmp/run-landlock",
                client_factory=FakeClient,
            )
            try:
                self.assertEqual(session.ensure_started(), "thread-landlock")
            finally:
                session.close()

        args = captured["extra_args"]
        self.assertEqual(
            args[args.index("--enable") + 1], "use_legacy_landlock"
        )
        self.assertIn('sandbox_mode="read-only"', args)
        self.assertNotIn('sandbox_mode="workspace-write"', args)
        self.assertFalse(
            any(
                value.startswith("sandbox_workspace_write.writable_roots=")
                for value in args
            )
        )

    def test_deepagents_subagent_toggle_parses_disabled_values(self) -> None:
        for value in ("0", "false", "NO", "off"):
            with patch.dict(os.environ, {"AGENT_MONITOR_USE_SUBAGENTS": value}):
                self.assertFalse(_subagents_enabled())
        with patch.dict(os.environ, {"AGENT_MONITOR_USE_SUBAGENTS": "1"}):
            self.assertTrue(_subagents_enabled())

    def test_deepagents_recursion_limit_is_configurable_and_bounded(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENT_MONITOR_DEEPAGENTS_RECURSION_LIMIT": "160"},
        ):
            self.assertEqual(deepagents_recursion_limit(), 160)
        with patch.dict(
            os.environ,
            {"AGENT_MONITOR_DEEPAGENTS_RECURSION_LIMIT": "9999"},
        ):
            self.assertEqual(deepagents_recursion_limit(), 800)
        with patch.dict(
            os.environ,
            {"AGENT_MONITOR_DEEPAGENTS_RECURSION_LIMIT": "invalid"},
        ):
            self.assertEqual(deepagents_recursion_limit(), 80)

    def test_deepagents_json_tools_receive_one_valid_json_envelope(self) -> None:
        document = {
            "verdict": "accept",
            "summary": "checked",
            "findings": [],
            "coverage_notes": {},
        }
        argv = deepagents_library_tool_argv(
            ["document"],
            {"document": document},
            json_arguments=True,
        )
        self.assertEqual(
            json.loads(argv[0]),
            {"document": document},
        )
        self.assertEqual(
            deepagents_schema_python_type(
                {"type": "array", "items": {"type": "string"}}
            ),
            list[str],
        )

    def test_deepagents_preserves_codex_chat_model_usage(self) -> None:
        stream = "\n".join(
            [
                "not-json",
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 120, "output_tokens": 7},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 80, "output_tokens": 3},
                    }
                ),
            ]
        )
        self.assertEqual(
            _codex_usage_from_jsonl(stream),
            {"input_tokens": 200, "output_tokens": 10, "total_tokens": 210},
        )

    def test_deepagents_rejects_missing_required_tool_arguments(self) -> None:
        tools = [{
            "type": "function",
            "function": {
                "name": "write_file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["file_path", "content"],
                },
            },
        }]
        invalid = {
            "kind": "tool_calls",
            "text": "",
            "tool_calls": [{"name": "write_file", "arguments": "{}"}],
        }
        valid = {
            **invalid,
            "tool_calls": [{
                "name": "write_file",
                "arguments": '{"file_path":"/proof.md","content":"proof"}',
            }],
        }
        self.assertEqual(
            _tool_call_issues(invalid, tools),
            ["call 1 (write_file) is missing required argument(s): file_path, content"],
        )
        self.assertEqual(_tool_call_issues(valid, tools), [])

        structured = _structured_tool_call_item_schema(tools)
        variant = structured["anyOf"][0]
        self.assertEqual(
            variant["properties"]["arguments"]["type"],
            "string",
        )
        self.assertEqual(
            _tool_call_issues(
                {
                    "kind": "tool_calls",
                    "tool_calls": [{
                        "name": "write_file",
                        "arguments": {"file_path": "/proof.md", "content": "proof"},
                    }],
                },
                tools,
            ),
            [],
        )

    def test_deepagents_text_only_recovery_forbids_a_third_tool_call(self) -> None:
        recovery = _text_only_tool_recovery_request(
            "original request",
            ["call 1 (write_file) arguments are not valid JSON"],
            '{"kind":"tool_calls"}',
        )
        self.assertIn("Do not call any tool", recovery)
        self.assertIn("kind='text'", recovery)
        self.assertIn("empty tool_calls array", recovery)
        self.assertIn("complete self-contained answer", recovery)

        schema = _text_only_response_schema()
        self.assertEqual(schema["required"], ["text"])
        self.assertNotIn("tool_calls", schema["properties"])
        self.assertFalse(schema["additionalProperties"])

    def test_deepagents_codex_timeout_kills_the_whole_process_group(self) -> None:
        proc = unittest.mock.Mock()
        proc.pid = 4242
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["codex"], 3),
            ("partial", "timed out"),
        ]
        with (
            patch("subprocess.Popen", return_value=proc) as popen,
            patch("agent_monitor.runners.deepagents_runner.os.killpg") as killpg,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_codex_exec(
                    ["codex", "exec"], request="task", env={}, timeout_seconds=3
                )

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_with(4242, signal.SIGKILL)

    def test_deepagents_codex_timeout_becomes_recoverable_nonaffirmative_result(self) -> None:
        response, stream, raw = _codex_timeout_response(
            17,
            b'{"type":"turn.completed","usage":{"input_tokens":4}}\n',
        )
        self.assertEqual(response["kind"], "text")
        self.assertEqual(response["tool_calls"], [])
        self.assertIn("exceeded 17s", response["text"])
        self.assertIn("never infer acceptance", response["text"])
        self.assertIn("turn.completed", stream)
        self.assertEqual(json.loads(raw), response)

    def test_deepagents_degraded_recovery_requires_current_concrete_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text("# stale\n" + "x" * 500, encoding="utf-8")
            initial_sha = deepagents_file_sha256(proof)
            proof.write_text(
                "# Partial Progress\n\n"
                + "A checked reduction remains useful. " * 16
                + "\n\n`degraded audit: global reviewer timed out; "
                "global.json and verifier-output.json are missing`.\n\n"
                "ProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            self.assertTrue(
                _recoverable_degraded_checkpoint(
                    workspace,
                    prompt="CANDIDATE AUDIT GATE:",
                    initial_proof_sha256=initial_sha,
                )
            )

            proof.write_text(
                "# Partial Progress\n\n"
                + "A checked reduction remains useful. " * 16
                + "\n\nThis is not a degraded audit.\n\n"
                "ProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            self.assertFalse(
                _recoverable_degraded_checkpoint(
                    workspace,
                    prompt="CANDIDATE AUDIT GATE:",
                    initial_proof_sha256=initial_sha,
                )
            )

    def test_deepagents_tool_json_failure_preserves_fail_closed_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text("# old\n" + "x" * 500, encoding="utf-8")
            initial_sha = deepagents_file_sha256(proof)
            proof.write_text(
                "# Candidate lemma\n\n" + "audited argument. " * 40
                + "\n\nProvingConsole outcome: Solved\n",
                encoding="utf-8",
            )
            audit = workspace / "audit" / "run-1"
            audit.mkdir(parents=True)
            (audit / "global.json").write_text("{}", encoding="utf-8")
            (audit / "decomposed.json").write_text("{}", encoding="utf-8")

            recovered = _persist_tool_serialization_checkpoint(
                workspace,
                prompt="CANDIDATE AUDIT GATE: audit this candidate.",
                initial_proof_sha256=initial_sha,
                error=(
                    "codex exec repeated an invalid LangChain tool response: "
                    "call 1 (write_file) arguments are not valid JSON"
                ),
            )

            self.assertTrue(recovered)
            final = proof.read_text(encoding="utf-8")
            self.assertIn("degraded audit:", final)
            self.assertIn("merge-map.json", final)
            self.assertEqual(final.count("ProvingConsole outcome:"), 1)
            self.assertTrue(
                final.rstrip().endswith("ProvingConsole outcome: Partial Progress")
            )
            self.assertFalse(
                _persist_tool_serialization_checkpoint(
                    workspace,
                    prompt="CANDIDATE AUDIT GATE:",
                    initial_proof_sha256=initial_sha,
                    error="unrelated model failure",
                )
            )

    def test_deepagents_supplemental_audit_uses_separate_validated_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Native candidate\n\nA useful but incomplete argument. " * 20
                + "\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            final = (
                "# Audited candidate\n\nThe exact gap remains explicit.\n\n"
                "ProvingConsole outcome: Partial Progress"
            )
            with (
                patch.object(
                    deepagents_runner,
                    "_recoverable_recursion_checkpoint",
                    return_value=False,
                ),
                patch(
                    "agent_monitor.runners.deepseek_harness_runner.run_codex_audit",
                    return_value=final,
                ) as audit,
                patch.object(deepagents_runner, "emit_item"),
            ):
                ran = deepagents_runner._run_supplemental_audit(
                    workspace,
                    prompt=(
                        "CANDIDATE AUDIT GATE: audit. "
                        "ProvingConsole outcome: <label>"
                    ),
                    model="openai:gpt-test",
                    subscription_mode=True,
                    initial_proof_sha256=None,
                    final_text="native finished",
                )
            self.assertTrue(ran)
            self.assertEqual(proof.read_text(encoding="utf-8").strip(), final)
            self.assertEqual(audit.call_args.kwargs["model"], "gpt-test")
            self.assertIsNone(audit.call_args.kwargs["call"])
            self.assertEqual(
                audit.call_args.kwargs["adapter"],
                "deepagents-response-orchestrated-audit",
            )

    def test_deepagents_supplemental_audit_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Candidate\n\nAn unsupported high-stakes claim. " * 20
                + "\n\nProvingConsole outcome: Solved\n",
                encoding="utf-8",
            )
            with (
                patch.object(
                    deepagents_runner,
                    "_recoverable_recursion_checkpoint",
                    return_value=False,
                ),
                patch(
                    "agent_monitor.runners.deepseek_harness_runner.run_codex_audit",
                    side_effect=ValueError("invalid merged verifier"),
                ),
                patch.object(deepagents_runner, "emit_item"),
            ):
                ran = deepagents_runner._run_supplemental_audit(
                    workspace,
                    prompt=(
                        "CANDIDATE AUDIT GATE: audit. "
                        "ProvingConsole outcome: <label>"
                    ),
                    model="anthropic:claude-test",
                    subscription_mode=False,
                    initial_proof_sha256=None,
                    final_text="native finished",
                )
            saved = proof.read_text(encoding="utf-8")
            self.assertTrue(ran)
            self.assertIn("degraded audit:", saved)
            self.assertNotIn("ProvingConsole outcome: Solved", saved)
            self.assertEqual(saved.count("ProvingConsole outcome:"), 1)
            self.assertTrue(
                saved.rstrip().endswith("ProvingConsole outcome: Partial Progress")
            )

    def test_deepagents_recursion_recovery_requires_fresh_validated_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text("# stale\n" + "x" * 500, encoding="utf-8")
            initial_sha = deepagents_file_sha256(proof)
            proof.write_text(
                "# Partial Progress\n\n"
                + "A bounded checked argument remains incomplete. " * 12
                + "\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            validator = workspace / "_library" / "tools" / "audit-output-validator.sh"
            validator.parent.mkdir(parents=True)
            validator.write_text(
                "#!/bin/bash\necho '{\"valid\":true}'\n", encoding="utf-8"
            )
            audit = workspace / "audit" / "run-1"
            _install_reconciled_audit(workspace, audit)
            recovered = _recoverable_recursion_checkpoint(
                workspace,
                prompt="CANDIDATE AUDIT GATE: audit the candidate.",
                initial_proof_sha256=initial_sha,
                final_text="Both auditors accepted the checkpoint.",
            )

            self.assertTrue(recovered)
            self.assertFalse(
                _recoverable_recursion_checkpoint(
                    workspace,
                    prompt="ordinary proof task",
                    initial_proof_sha256=initial_sha,
                    final_text="Completed the final proof and audit.",
                )
            )
            self.assertFalse(
                _recoverable_recursion_checkpoint(
                    workspace,
                    prompt="CANDIDATE AUDIT GATE: audit the candidate.",
                    initial_proof_sha256=deepagents_file_sha256(proof),
                    final_text="Completed the final proof and audit.",
                )
            )

            invalid = subprocess.CompletedProcess(
                ["validator"], 1, stdout='{"valid": false}', stderr=""
            )
            with patch(
                "agent_monitor.runners.deepagents_runner.subprocess.run",
                return_value=invalid,
            ):
                self.assertFalse(
                    _recoverable_recursion_checkpoint(
                        workspace,
                        prompt="CANDIDATE AUDIT GATE: audit the candidate.",
                        initial_proof_sha256=initial_sha,
                        final_text="Completed the final proof and audit.",
                    )
                )

    def test_openhands_subscription_events_preserve_codex_usage(self) -> None:
        parser = CLIEventParser(CLI_ENGINES["openhands"]["parser_style"])
        parser.feed(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 321, "output_tokens": 45},
                }
            )
        )
        self.assertEqual(parser.usage["input_tokens"], 321)
        self.assertEqual(parser.usage["output_tokens"], 45)

    def test_portable_persona_indexes_skills_without_expanding_bodies(self) -> None:
        with (
            patch.object(agent_config, "ensure_seeded"),
            patch.object(agent_config, "get_system_prompt", return_value="truth first"),
            patch.object(
                agent_config,
                "list_skills",
                return_value=[
                    {
                        "name": "audit",
                        "enabled": True,
                        "description": "Check pivotal claims",
                        "content": "UNIQUE-LONG-SKILL-BODY",
                    }
                ],
            ),
            patch.object(agent_config, "_skills_dir", return_value=Path("/skills")),
            patch.object(agent_config, "memory_enabled", return_value=False),
        ):
            preamble = agent_config.persona_preamble()

        self.assertIn("truth first", preamble)
        self.assertIn("/skills/audit/SKILL.md", preamble)
        self.assertNotIn("UNIQUE-LONG-SKILL-BODY", preamble)

    def test_portable_persona_materializes_skill_package_for_virtual_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_root = root / "hermes" / "skills"
            package = skill_root / "audit"
            (package / "references").mkdir(parents=True)
            (package / "SKILL.md").write_text(
                "---\nname: audit\ndescription: Check pivotal claims\n---\n\n# Audit\nBODY",
                encoding="utf-8",
            )
            (package / "references" / "check.md").write_text("reference", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            with (
                patch.object(agent_config, "HERMES_HOME", root / "hermes"),
                patch.object(agent_config, "ensure_seeded"),
                patch.object(agent_config, "get_system_prompt", return_value="truth first"),
                patch.object(agent_config, "memory_enabled", return_value=False),
            ):
                preamble = agent_config.persona_preamble(workspace=workspace)

            self.assertIn("audit: Check pivotal claims", preamble)
            self.assertIn("source: _agent/skills/audit/SKILL.md", preamble)
            self.assertNotIn("BODY", preamble)
            self.assertEqual(
                (workspace / "_agent" / "skills" / "audit" / "references" / "check.md").read_text(),
                "reference",
            )

    def test_native_harness_can_materialize_enabled_profile_skill_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "hermes" / "skills" / "audit"
            (package / "references").mkdir(parents=True)
            (package / "SKILL.md").write_text(
                "---\nname: audit\ndescription: Check pivotal claims\n---\n\n# Audit",
                encoding="utf-8",
            )
            (package / "references" / "check.md").write_text("reference", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            with (
                patch.object(agent_config, "HERMES_HOME", root / "hermes"),
                patch.object(agent_config, "ensure_seeded"),
            ):
                visible = agent_config.materialize_enabled_skills(workspace)

            self.assertEqual(visible, {"audit": "_agent/skills/audit/SKILL.md"})
            self.assertEqual(
                (workspace / "_agent" / "skills" / "audit" / "references" / "check.md").read_text(),
                "reference",
            )

    def test_lean_check_honors_timeout_without_hanging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ldir = Path(tmp)
            (ldir / "Proof.lean").write_text(
                "import time\ntime.sleep(30)\n", encoding="utf-8"
            )
            with (
                patch.object(
                    lean_broker,
                    "check_source",
                    return_value={
                        "status": "timed_out",
                        "exit_code": None,
                        "stdout": "",
                        "stderr": "",
                        "error": "fixed worker timeout",
                        "duration": 1.0,
                        "source_sha256": hashlib.sha256(
                            b"import time\ntime.sleep(30)\n"
                        ).hexdigest(),
                        "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
                        "sandbox": lean_broker.SANDBOX_MARKER,
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                    },
                ),
                patch.dict(os.environ, {"AGENT_MONITOR_LEAN_CHECK_TIMEOUT": "1"}),
            ):
                result = lean_verify._run_lean_check(ldir, uses_mathlib=False)

        self.assertFalse(result["ok"])
        self.assertIn("Lean broker timed out", result["stderr"])
        self.assertLess(result["duration_s"], 5)

    def test_lean_checks_are_serialized_by_the_shared_slot(self) -> None:
        first_entered = threading.Event()
        release_first = threading.Event()
        broker_calls: list[int] = []
        results: list[dict] = []

        def fake_check(source: str, **_kwargs):
            index = len(broker_calls)
            broker_calls.append(index)
            if index == 0:
                first_entered.set()
                release_first.wait(timeout=2)
            return {
                "status": "completed",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "error": "",
                "duration": 0.01,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
                "sandbox": lean_broker.SANDBOX_MARKER,
                "stdout_truncated": False,
                "stderr_truncated": False,
            }

        with tempfile.TemporaryDirectory() as tmp:
            ldir = Path(tmp)
            (ldir / "Proof.lean").write_text("theorem main : True := by trivial\n")
            with (
                patch.object(lean_verify, "_LEAN_CHECK_SLOTS", threading.BoundedSemaphore(1)),
                patch.object(lean_broker, "check_source", side_effect=fake_check),
            ):
                first = threading.Thread(
                    target=lambda: results.append(lean_verify._run_lean_check(ldir, False))
                )
                second = threading.Thread(
                    target=lambda: results.append(lean_verify._run_lean_check(ldir, False))
                )
                first.start()
                self.assertTrue(first_entered.wait(timeout=1))
                second.start()
                time.sleep(0.05)
                self.assertEqual(broker_calls, [0])
                release_first.set()
                first.join(timeout=2)
                second.join(timeout=2)

        self.assertEqual(broker_calls, [0, 1])
        self.assertEqual([result["status"] for result in results], ["verified", "verified"])

    def test_broad_mathlib_import_is_rejected_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ldir = Path(tmp)
            (ldir / "Proof.lean").write_text("import Mathlib\n\ntheorem main : True := by trivial\n")
            with (
                patch.object(
                    lean_verify,
                    "toolchain_status",
                    return_value={
                        "available": True, "lean": "/fake/lean", "lake": "/fake/lake",
                        "elan": None, "version": "fake",
                    },
                ),
                patch("agent_monitor.lean_verify.subprocess.Popen") as popen,
            ):
                result = lean_verify._run_lean_check(ldir, uses_mathlib=True)

        self.assertFalse(result["ok"])
        self.assertIn("Broad `import Mathlib` is disabled", result["stderr"])
        popen.assert_not_called()

    def test_lean_timeout_kills_the_entire_process_group(self) -> None:
        proc = unittest.mock.Mock()
        proc.poll.return_value = None
        proc.pid = 4321
        with patch("agent_monitor.lean_verify.os.killpg") as killpg:
            lean_verify._terminate_process_group(proc, force=True)
        killpg.assert_called_once_with(4321, signal.SIGKILL)
        proc.kill.assert_not_called()

    def test_cli_prompt_names_one_consistent_proof_artifact(self) -> None:
        prompt = proof_prompt("Prove 1 + 1 = 2.", workspace=Path("/tmp/run"))
        self.assertIn("write proof.md THERE", prompt)
        self.assertNotIn("write proof.tex THERE", prompt)

    def test_plain_prompt_separates_math_from_wrapper_operations(self) -> None:
        self.assertIn("request to prove a statement is not evidence", PLAIN_SYSTEM_PROMPT)
        self.assertIn("currently open", PLAIN_SYSTEM_PROMPT)
        self.assertIn("the wrapper saves it", PLAIN_SYSTEM_PROMPT)
        self.assertIn("Do not access or modify files", PLAIN_SYSTEM_PROMPT)
        self.assertEqual(DEEPSEEK_CODEX_SYSTEM_PROMPT, PLAIN_SYSTEM_PROMPT)

    def test_openhands_rejects_link_only_completion_message(self) -> None:
        self.assertFalse(
            _is_proof_response(
                "Completed and audited final deliverable: "
                "[proof.md](/tmp/run/proof.md). The requested classification is included."
            )
        )
        self.assertFalse(
            _is_proof_response(
                "Created [proof.md](/tmp/run/proof.md). The claim is false; the "
                "file gives a rigorous connected counterexample by attaching a "
                "pendant vertex to K4, and deleting it leaves chromatic number 4."
            )
        )
        self.assertTrue(
            _is_proof_response(
                "# Proof\n\nAssume $n=2k$. Then $n+2=2(k+1)$, so it is even. "
                "Every step is self-derived. ∎"
            )
        )

    def test_openhands_validates_artifact_content_not_just_existence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp) / "proof.md"
            proof.write_text(
                "Created [proof.md](/tmp/proof.md). The claim is false; the file "
                "contains a connected counterexample using K4 and a leaf.",
                encoding="utf-8",
            )
            self.assertFalse(_valid_proof_file(proof))
            proof.write_text(
                "# Counterexample\n\nLet G be K4 with an attached leaf x. The "
                "induced K4 forces four colors, and four colors suffice. Deleting "
                "x leaves K4, so the chromatic number remains four. Hence the "
                "universal claim is false.\n",
                encoding="utf-8",
            )
            self.assertTrue(_valid_proof_file(proof))

    def test_openhands_last_message_never_overwrites_proof_artifact(self) -> None:
        workspace = Path("/runs/r1")
        last_message = Path("/tmp/last-message.txt")
        command = _codex_exec_command("codex", workspace, last_message, "gpt-test")
        output_index = command.index("--output-last-message") + 1
        self.assertEqual(command[output_index], str(last_message))
        self.assertNotEqual(command[output_index], str(workspace / "proof.md"))

    def test_hermes_artifact_drops_stale_filesystem_failure_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = _persist_text_final_answer(
                "# Counterexample\n\nK4 plus a leaf refutes the claim.\n\n"
                "I could not create `proof.md` because every workspace write "
                "was rejected by the environment's permission layer.",
                workspace,
                engine="Hermes",
            )

            self.assertIsNotNone(proof)
            saved = proof.read_text(encoding="utf-8")
            self.assertIn("K4 plus a leaf", saved)
            self.assertNotIn("could not create", saved)

    def test_hermes_artifact_drops_stale_filesystem_failure_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = _persist_text_final_answer(
                "I couldn’t create `proof.md`: the workspace sandbox failed to "
                "initialize, and workspace access was rejected.\n\n"
                "# Counterexample\n\nK4 plus a leaf refutes the claim.",
                workspace,
                engine="Hermes",
            )

            self.assertIsNotNone(proof)
            saved = proof.read_text(encoding="utf-8")
            self.assertTrue(saved.startswith("# Counterexample"))
            self.assertIn("K4 plus a leaf", saved)
            self.assertNotIn("sandbox failed", saved)

    def test_hermes_artifact_drops_exact_bwrap_failure_from_middle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = _persist_text_final_answer(
                "# Partial Progress\n\nA valid reduction.\n\n"
                "## Source audit\n\nAttempts were blocked before process launch by "
                "`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`. "
                "A request to run outside that sandbox was denied. Accordingly, "
                "no exhaustive audit is claimed; the next check is the weighted sieve.\n\n"
                "## Conclusion\n\nThe reduction remains valid.",
                workspace,
                engine="Hermes",
            )
            saved = proof.read_text(encoding="utf-8")
            self.assertNotIn("RTM_NEWADDR", saved)
            self.assertNotIn("request to run outside", saved)
            self.assertIn("no exhaustive audit is claimed", saved)
            self.assertIn("weighted sieve", saved)
            self.assertIn("The reduction remains valid", saved)

    def test_hermes_replaces_explicitly_incomplete_stale_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stale = (
                "# Status Check\n\nThis is an incomplete shell.\n\n## References\n"
            )
            (workspace / "proof.md").write_text(stale, encoding="utf-8")
            final = (
                "The official page labels Erdős Problem #1210 **OPEN** as "
                "accessed on 2026-08-21: https://www.erdosproblems.com/1210.\n\n"
                "I did not infer any mathematical resolution. The sandbox failed "
                "while finalizing `proof.md` (`bwrap: loopback: Failed RTM_NEWADDR`). "
                "The file remains an incomplete draft.\n\n"
                "--- proof.md ---\n" + stale
            )

            proof = _persist_text_final_answer(final, workspace, engine="Hermes")

            self.assertEqual(proof, workspace / "proof.md")
            saved = proof.read_text(encoding="utf-8")
            self.assertIn("**OPEN**", saved)
            self.assertIn("erdosproblems.com/1210", saved)
            self.assertNotIn("incomplete shell", saved)
            self.assertNotIn("RTM_NEWADDR", saved)
            self.assertNotIn("incomplete draft", saved)

    def test_hermes_failed_update_cannot_finish_from_unchanged_stale_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stale = "# Partial Progress\n\n**Proof.** To be refined.\n"
            target = workspace / "proof.md"
            target.write_text(stale, encoding="utf-8")
            final = (
                "I could not update [proof.md] because the workspace sandbox "
                "failed; the notebook remains unchanged.\n\n"
                "--- proof.md ---\n" + stale
            )

            proof = _persist_text_final_answer(final, workspace, engine="Hermes")

            self.assertIsNone(proof)
            self.assertEqual(target.read_text(encoding="utf-8"), stale)

    def test_hermes_monitor_writer_promotes_changed_embedded_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stale = "# Partial Progress\n\n**Proof.** To be refined.\n"
            target = workspace / "proof.md"
            target.write_text(stale, encoding="utf-8")
            corrected = (
                "# Partial Progress\n\n**Lemma.** Every vertex has degree at "
                "least six.\n\n**Proof.** In a three-colouring of $G-v$, "
                "each colour occurs at least twice on $N(v)$; otherwise "
                "deleting the unique incident edge gives a three-colouring, "
                "contradicting edge-criticality. $\\square$\n"
            )
            final = (
                "The read-only agent completed a corrected artifact.\n\n"
                "--- proof.md ---\n" + corrected
            )

            proof = _persist_text_final_answer(final, workspace, engine="Hermes")

            self.assertEqual(proof, target)
            self.assertEqual(
                target.read_text(encoding="utf-8"), corrected.rstrip() + "\n"
            )

    def test_hermes_placeholder_artifact_cannot_finish_from_short_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stale = "# Partial Progress\n\n**Proof.** To be refined.\n"
            target = workspace / "proof.md"
            target.write_text(stale, encoding="utf-8")

            proof = _persist_text_final_answer(
                "Completed proof.md.", workspace, engine="Hermes"
            )

            self.assertIsNone(proof)
            self.assertEqual(target.read_text(encoding="utf-8"), stale)

    def test_hermes_changed_embedded_placeholder_is_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stale = "# Partial Progress\n\nOld incomplete draft.\n"
            target = workspace / "proof.md"
            target.write_text(stale, encoding="utf-8")
            changed_but_incomplete = (
                "# Partial Progress\n\nNew lemma.\n\n"
                "**Proof.** To be refined.\n"
            )
            final = (
                "I could not update [proof.md]; the notebook remains unchanged.\n\n"
                "--- proof.md ---\n" + changed_but_incomplete
            )

            proof = _persist_text_final_answer(final, workspace, engine="Hermes")

            self.assertIsNone(proof)
            self.assertEqual(target.read_text(encoding="utf-8"), stale)

    def test_hermes_keeps_existing_artifact_without_incomplete_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            expected = "# Verified Control\n\nA complete proof artifact of sufficient length.\n"
            target = workspace / "proof.md"
            target.write_text(expected, encoding="utf-8")

            proof = _persist_text_final_answer(
                "Completed proof.md and verified the result.", workspace, engine="Hermes"
            )

            self.assertEqual(proof, target)
            self.assertEqual(target.read_text(encoding="utf-8"), expected)

    def test_openhands_acp_model_id_includes_supported_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            (codex_home / "models_cache.json").write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-test",
                                "default_reasoning_level": "high",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_acp_model_id("gpt-test", codex_home), "gpt-test[high]")
            with patch.dict(
                os.environ, {"OPENHANDS_CODEX_REASONING_EFFORT": "xhigh"}
            ):
                self.assertEqual(
                    _acp_model_id("gpt-test", codex_home), "gpt-test[xhigh]"
                )
            self.assertEqual(
                _acp_model_id("gpt-test[low]", codex_home), "gpt-test[low]"
            )

    def test_openhands_iteration_cap_is_bounded(self) -> None:
        for raw, expected in (("7", 7), ("0", 1), ("999", 500), ("bad", 40)):
            with patch.dict(
                os.environ, {"AGENT_MONITOR_OPENHANDS_MAX_ITERATIONS": raw}
            ):
                self.assertEqual(_max_iterations(), expected)

    def test_openhands_iteration_event_is_detected(self) -> None:
        from types import SimpleNamespace

        self.assertTrue(
            _reached_iteration_limit([SimpleNamespace(code="MaxIterationsReached")])
        )
        self.assertFalse(_reached_iteration_limit([SimpleNamespace(code="Other")]))

    def test_openhands_acp_tool_budget_counts_ids_not_status_updates(self) -> None:
        from types import SimpleNamespace

        budget = _ACPToolBudget(2)
        self.assertFalse(budget.observe(SimpleNamespace(tool_call_id="a")))
        self.assertFalse(budget.observe(SimpleNamespace(tool_call_id="a")))
        self.assertFalse(budget.observe(SimpleNamespace(tool_call_id="b")))
        self.assertTrue(budget.observe(SimpleNamespace(tool_call_id="c")))
        self.assertFalse(budget.observe(SimpleNamespace(tool_call_id="d")))
        self.assertEqual(budget.tool_call_ids, {"a", "b", "c", "d"})
        self.assertTrue(budget.exceeded)

    def test_openhands_acp_cancel_uses_async_wrapper(self) -> None:
        import asyncio
        import inspect
        from types import SimpleNamespace

        called = []

        class Connection:
            async def cancel(self, session_id):
                called.append(session_id)

        class Executor:
            def run_async(self, fn):
                self.assert_async = inspect.iscoroutinefunction(fn)
                asyncio.run(fn())

        executor = Executor()
        _cancel_acp_prompt(
            SimpleNamespace(
                _conn=Connection(),
                _executor=executor,
                _session_id="session-7",
            )
        )
        self.assertTrue(executor.assert_async)
        self.assertEqual(called, ["session-7"])

    def test_openhands_usage_preserves_cache_and_reasoning(self) -> None:
        from types import SimpleNamespace

        self.assertEqual(
            _usage_payload(
                SimpleNamespace(
                    prompt_tokens=11,
                    completion_tokens=3,
                    cache_read_tokens=101,
                    cache_write_tokens=7,
                    reasoning_tokens=5,
                )
            ),
            {
                "input_tokens": 11,
                "output_tokens": 3,
                "cache_read_tokens": 101,
                "cache_write_tokens": 7,
                "reasoning_tokens": 5,
            },
        )

    def test_openhands_iteration_limit_does_not_fallback(self) -> None:
        from agent_monitor.runners import openhands_subscription_runner as runner

        with (
            patch.dict(
                os.environ,
                {"AGENT_MONITOR_CODEX_SUBSCRIPTION": "1"},
                clear=True,
            ),
            patch.object(sys, "argv", ["openhands_subscription_runner.py", "audit"]),
            patch.object(runner, "_ensure_sdk_python"),
            patch.object(
                runner.importlib.metadata,
                "version",
                return_value="1.21.0",
            ),
            patch.object(
                runner,
                "_require_subscription_auth",
                return_value=Path("/tmp/codex"),
            ),
            patch.object(
                runner,
                "_run_acp",
                side_effect=OpenHandsIterationLimit(
                    "Agent reached maximum iterations limit (7)."
                ),
            ),
            patch.object(runner, "_run_codex_exec") as fallback,
        ):
            self.assertEqual(runner.main(), 1)
        fallback.assert_not_called()

    def test_openhands_acp_error_recovers_valid_control_checkpoint(self) -> None:
        from agent_monitor.runners import openhands_subscription_runner as runner

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "proof.md").write_text(
                "# Partial result\n\n"
                + "A bounded statement was proved with explicit scope. " * 16
                + "\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            tools = workspace / "_library" / "tools"
            tools.mkdir(parents=True)
            for name in ("proof-sanity-check.sh", "citation-audit.sh"):
                (tools / name).write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            events: list[dict] = []

            with (
                patch.dict(
                    os.environ,
                    {"AGENT_MONITOR_CODEX_SUBSCRIPTION": "1"},
                    clear=True,
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "openhands_subscription_runner.py",
                        "CONTROL CONDITION: normal rigorous workflow",
                    ],
                ),
                patch.object(runner, "_ensure_sdk_python"),
                patch.object(runner.importlib.metadata, "version", return_value="1.21.0"),
                patch.object(
                    runner,
                    "_require_subscription_auth",
                    return_value=Path("/tmp/codex"),
                ),
                patch.object(runner.Path, "cwd", return_value=workspace),
                patch.object(runner, "_run_acp", side_effect=RuntimeError("ACP closed")),
                patch.object(runner, "_run_codex_exec") as fallback,
                patch.object(runner, "emit_item", side_effect=events.append),
            ):
                self.assertEqual(runner.main(), 0)

            fallback.assert_not_called()
            self.assertTrue(
                any("recovered it without rerunning" in event.get("text", "") for event in events)
            )

    def test_openhands_activity_recovery_requires_validated_audit_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "proof.md").write_text(
                "# Partial Progress\n\n"
                + "A bounded result was checked independently. " * 16
                + "\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            validator = workspace / "_library" / "tools" / "audit-output-validator.sh"
            validator.parent.mkdir(parents=True)
            validator.write_text("#!/bin/bash\necho '{\"valid\":true}'\n", encoding="utf-8")
            audit = workspace / "audit" / "run-1"
            _install_reconciled_audit(workspace, audit)
            verifier_text = (audit / "verifier-output.json").read_text(
                encoding="utf-8"
            )

            self.assertTrue(
                _recoverable_activity_checkpoint(
                    workspace, "CANDIDATE AUDIT GATE: audit this candidate"
                )
            )
            (audit / "verifier-output.json").unlink()
            self.assertFalse(
                _recoverable_activity_checkpoint(
                    workspace, "CANDIDATE AUDIT GATE: audit this candidate"
                )
            )
            (audit / "verifier-output.json").write_text(
                verifier_text, encoding="utf-8"
            )
            manifest_path = audit / "run.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("reconciliation")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            proof_path = workspace / "proof.md"
            proof_path.write_text(
                proof_path.read_text(encoding="utf-8").replace(
                    "# Partial Progress",
                    "# Partial Progress\n\nDegraded audit: exact-final reconciliation is unavailable.",
                    1,
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                _recoverable_activity_checkpoint(
                    workspace, "CANDIDATE AUDIT GATE: audit this candidate"
                )
            )
            proof_path.write_text(
                proof_path.read_text(encoding="utf-8").replace(
                    "ProvingConsole outcome: Partial Progress",
                    "ProvingConsole outcome: Solved",
                ),
                encoding="utf-8",
            )
            self.assertFalse(
                _recoverable_activity_checkpoint(
                    workspace, "CANDIDATE AUDIT GATE: audit this candidate"
                )
            )
            with tempfile.TemporaryDirectory() as outside_td:
                outside = Path(outside_td) / "proof.md"
                outside.write_text(
                    "# Outside\n\n" + "content " * 80
                    + "\nProvingConsole outcome: Partial Progress\n",
                    encoding="utf-8",
                )
                (workspace / "proof.md").unlink()
                (workspace / "proof.md").symlink_to(outside)
                self.assertFalse(
                    _recoverable_activity_checkpoint(
                        workspace, "CANDIDATE AUDIT GATE: audit this candidate"
                    )
                )
            self.assertFalse(
                _recoverable_activity_checkpoint(workspace, "ordinary proof")
            )

    def test_openhands_control_activity_recovery_is_conservative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Partial result\n\n"
                + "A bounded statement was proved with explicit scope. " * 16
                + "\n\n**ProvingConsole outcome: Partial Progress**\n",
                encoding="utf-8",
            )
            tools = workspace / "_library" / "tools"
            tools.mkdir(parents=True)
            for name in ("proof-sanity-check.sh", "citation-audit.sh"):
                (tools / name).write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

            prompt = "CONTROL CONDITION: normal rigorous workflow"
            self.assertTrue(_recoverable_control_checkpoint(workspace, prompt))
            self.assertFalse(
                _recoverable_control_checkpoint(
                    workspace,
                    prompt + "\nCANDIDATE AUDIT GATE: audit",
                )
            )

            proof.write_text(
                proof.read_text(encoding="utf-8").replace(
                    "Partial Progress", "Solved"
                ),
                encoding="utf-8",
            )
            self.assertFalse(_recoverable_control_checkpoint(workspace, prompt))

    def test_openhands_control_activity_recovery_requires_final_outcome_and_preflights(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Partial result\n\n"
                + "A bounded statement was proved with explicit scope. " * 16
                + "\n\nProvingConsole outcome: Known/Open Status\n\nTrailing text.\n",
                encoding="utf-8",
            )
            tools = workspace / "_library" / "tools"
            tools.mkdir(parents=True)
            for name in ("proof-sanity-check.sh", "citation-audit.sh"):
                (tools / name).write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            prompt = "CONTROL CONDITION: normal rigorous workflow"
            self.assertFalse(_recoverable_control_checkpoint(workspace, prompt))

            proof.write_text(
                proof.read_text(encoding="utf-8").replace("\n\nTrailing text.\n", "\n"),
                encoding="utf-8",
            )
            (tools / "citation-audit.sh").write_text(
                "#!/bin/bash\nexit 1\n", encoding="utf-8"
            )
            self.assertFalse(_recoverable_control_checkpoint(workspace, prompt))

    def test_openai_models_use_responses_api_and_extract_output_text(self) -> None:
        with patch("agent_monitor.settings._http_json", return_value=(
            200,
            {"output": [{"content": [{"type": "output_text", "text": "OPENAI_OK"}]}]},
        )) as request:
            chosen, content, provider = proof_graph._call_llm(
                {"OPENAI_API_KEY": "openai-test"},
                "gpt-5.6-luna",
                "ping",
            )

        self.assertEqual((chosen, content, provider), ("gpt-5.6-luna", "OPENAI_OK", "openai"))
        self.assertTrue(request.call_args.args[0].endswith("/responses"))
        self.assertEqual(request.call_args.kwargs["payload"]["input"], "ping")
        self.assertNotIn("messages", request.call_args.kwargs["payload"])

    def test_proof_graph_repairs_unescaped_latex_without_hiding_bad_json(self) -> None:
        raw = r'{"title":"TeX graph","nodes":[{"id":"n1","kind":"conclusion","label":"Exact claim","statement":"$\forall x,\ \underbrace{x}_{\text{term}} \neq 0$","citations":["self-derived"]}],"edges":[]}'
        graph = proof_graph._extract_json(raw)
        self.assertEqual(
            graph["nodes"][0]["statement"],
            r"$\forall x,\ \underbrace{x}_{\text{term}} \neq 0$",
        )
        mixed = r'{"title":"mixed","nodes":[{"id":"n1","kind":"conclusion","label":"Claim","statement":"first\nnext: $\forall x, x \neq 0$","citations":[]}],"edges":[]}'
        repaired = proof_graph._extract_json(mixed)
        self.assertEqual(
            repaired["nodes"][0]["statement"],
            "first\nnext: $\\forall x, x \\neq 0$",
        )
        already_escaped = r'{"title":"escaped","nodes":[{"id":"n1","kind":"conclusion","label":"Claim","statement":"$\\alpha + \\beta$","citations":[]}],"edges":[]}'
        preserved = proof_graph._extract_json(already_escaped)
        self.assertEqual(preserved["nodes"][0]["statement"], r"$\alpha + \beta$")
        with self.assertRaises(json.JSONDecodeError):
            proof_graph._extract_json(r'{"title":"still malformed",}')

    def test_claude_uses_anthropic_even_when_openai_key_is_also_set(self) -> None:
        with patch("agent_monitor.settings._http_json", return_value=(
            200,
            {"content": [{"type": "text", "text": "CLAUDE_OK"}]},
        )) as request:
            chosen, content, provider = proof_graph._call_llm(
                {
                    "OPENAI_API_KEY": "openai-test",
                    "ANTHROPIC_API_KEY": "anthropic-test",
                },
                "claude-sonnet-5",
                "ping",
            )

        self.assertEqual((chosen, content, provider), ("claude-sonnet-5", "CLAUDE_OK", "anthropic"))
        self.assertTrue(request.call_args.args[0].endswith("/v1/messages"))
        self.assertEqual(request.call_args.kwargs["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(request.call_args.kwargs["payload"]["messages"][0]["content"], "ping")

    def test_claude_without_anthropic_or_openrouter_key_fails_before_network(self) -> None:
        with patch("agent_monitor.settings._http_json") as request:
            with self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
                proof_graph._call_llm(
                    {"OPENAI_API_KEY": "openai-test"},
                    "claude-sonnet-5",
                    "ping",
                )
        request.assert_not_called()

    def test_anthropic_verification_does_not_treat_model_errors_as_success(self) -> None:
        with (
            patch.object(settings, "resolved_user_env", return_value={"ANTHROPIC_API_KEY": "test"}),
            patch.object(
                settings,
                "_http_json",
                return_value=(400, {"error": {"message": "model is not available"}}),
            ),
        ):
            result = settings.verify_provider("anthropic", user={"id": 7})

        self.assertFalse(result["ok"])
        self.assertIn("model is not available", result["error"])

    def test_kimi_settings_and_connection_verification(self) -> None:
        env = {
            "AGENT_MONITOR_USE_CODEX": "0",
            "KIMI_API_KEY": "kimi-test",
            "KIMI_API_BASE": "https://kimi.example/v1",
            "KIMI_REASONING_EFFORT": "high",
        }
        response = {"choices": [{"message": {"content": "KIMI_OK"}}]}
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch.object(settings, "_http_json", return_value=(200, response)) as request,
            patch("agent_monitor.codex_login.account_login_ready", return_value=False),
        ):
            current = settings.get_settings({"id": 7})
            verified = settings.verify_provider(
                "kimi",
                options={"KIMI_REASONING_EFFORT": "low"},
                user={"id": 7},
            )

        kimi = next(provider for provider in current["providers"] if provider["id"] == "kimi")
        self.assertTrue(kimi["api_key_set"])
        self.assertEqual(kimi["models"], ["kimi-k3"])
        self.assertEqual(kimi["settings"][0]["value"], "high")
        self.assertIn("kimi-k3", current["api_models"])
        self.assertTrue(verified["ok"])
        self.assertEqual(request.call_args.args[0], "https://kimi.example/v1/chat/completions")
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("medium", kimi["settings"][0]["choices"])
        self.assertNotIn("output_config", payload)
        self.assertNotIn("context_management", payload)

    def test_unselected_provider_default_keeps_openai_precedence_over_kimi(self) -> None:
        provider, _base, _key, model = proof_graph._resolve_llm_provider(
            {"OPENAI_API_KEY": "openai-test", "KIMI_API_KEY": "kimi-test"},
            None,
        )
        self.assertEqual(provider, "openai")
        self.assertEqual(model, "gpt-5-mini-2025-08-07")

    def test_deepseek_formal_harness_requires_its_own_api_key(self) -> None:
        env = {
            "AGENT_MONITOR_USE_CODEX": "0",
            "OPENAI_API_KEY": "openai-test",
            "ANTHROPIC_API_KEY": "anthropic-test",
        }
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value=env),
            patch("agent_monitor.engines_registry._cli_available", return_value=(True, "cmd")),
            patch("agent_monitor.codex_login.account_login_ready", return_value=True) as ready,
        ):
            engines = lean_verify.harness_engines({"id": 7})

        deepseek = next(item for item in engines if item["id"] == "deepseek_harness")
        codex = next(item for item in engines if item["id"] == "codex")
        self.assertFalse(deepseek["available"])
        self.assertIn("DEEPSEEK_API_KEY", deepseek["status_detail"])
        self.assertTrue(codex["available"])
        self.assertEqual(codex["auth_detail"], "OpenAI API key")
        ready.assert_not_called()


class EngineRegistryTests(unittest.TestCase):
    def test_cli_timeout_uses_audit_safe_deepagents_default_and_overrides(self) -> None:
        self.assertEqual(jobs._cli_timeout_seconds("deepagents", {}), 10_800)
        self.assertEqual(jobs._cli_timeout_seconds("math_harness", {}), 7_200)
        self.assertEqual(
            jobs._cli_timeout_seconds(
                "math_harness",
                {
                    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2",
                    "AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT": "3600",
                },
            ),
            18_300,
        )
        self.assertEqual(jobs._cli_timeout_seconds("codex", {}), 3_600)
        self.assertEqual(jobs._cli_timeout_seconds("claude", {}), 3_600)
        self.assertEqual(
            jobs._cli_timeout_seconds(
                "claude", {"AGENT_MONITOR_CLAUDE_TIMEOUT": "4200"}
            ),
            4_500,
        )
        self.assertEqual(
            jobs._cli_timeout_seconds(
                "claude", {"AGENT_MONITOR_CLAUDE_TIMEOUT": "4200.25"}
            ),
            4_501,
        )
        self.assertEqual(
            jobs._cli_timeout_seconds(
                "claude", {"AGENT_MONITOR_CLAUDE_TIMEOUT": "invalid"}
            ),
            3_600,
        )
        self.assertEqual(
            jobs._cli_timeout_seconds(
                "claude",
                {
                    "AGENT_MONITOR_CLAUDE_TIMEOUT": "4200",
                    "AGENT_MONITOR_CLI_TIMEOUT": "5000",
                },
            ),
            5_000,
        )
        self.assertEqual(
            jobs._cli_timeout_seconds(
                "deepagents",
                {
                    "AGENT_MONITOR_CLI_TIMEOUT": "4200",
                    "AGENT_MONITOR_DEEPAGENTS_TIMEOUT": "7200",
                },
            ),
            7_200,
        )
        self.assertEqual(
            jobs._cli_timeout_seconds(
                "deepagents", {"AGENT_MONITOR_DEEPAGENTS_TIMEOUT": "invalid"}
            ),
            10_800,
        )
        self.assertEqual(
            jobs._cli_timeout_seconds("codex", {"AGENT_MONITOR_CLI_TIMEOUT": "2"}),
            60,
        )
        self.assertEqual(
            jobs._cli_timeout_seconds(
                "deepagents", {"AGENT_MONITOR_SPONSORED_KIMI": "1"}
            ),
            6_600,
        )
        self.assertEqual(jobs._cli_timeout_seconds("codex", {"AGENT_MONITOR_SPONSORED_KIMI": "1"}), 3_600)


    def test_openclaude_error_result_preserves_terminal_subtype(self) -> None:
        parser = CLIEventParser("openclaude")
        parser.feed(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": True,
                    "num_turns": 60,
                    "result": "",
                    "usage": {},
                }
            )
        )

        self.assertEqual(parser.terminal_error, "error_max_turns")
        self.assertIn("error_max_turns", parser.output())
        self.assertEqual(
            jobs._cli_failure_reason(
                label="OpenClaude",
                returncode=1,
                timed_out=False,
                timeout_s=3600,
                proof_missing=False,
                detail=parser.terminal_error,
            ),
            "OpenClaude exited with code 1: error_max_turns",
        )

    def test_trusted_terminal_item_preserves_math_harness_failure_detail(self) -> None:
        parser = CLIEventParser("codex")
        parser.feed(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "error",
                        "message": (
                            "candidate-1 provider call failed: "
                            "Claude Code timed out after 1200s"
                        ),
                        "terminal": True,
                    },
                }
            )
        )
        self.assertEqual(
            parser.terminal_error,
            "candidate-1 provider call failed: Claude Code timed out after 1200s",
        )
        self.assertIn("candidate-1", parser.output())

    def test_untrusted_error_item_does_not_become_terminal_detail(self) -> None:
        parser = CLIEventParser("codex")
        parser.feed(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "error", "message": "arbitrary CLI text"},
                }
            )
        )
        self.assertIsNone(parser.terminal_error)

    def test_cli_failure_reason_is_actionable(self) -> None:
        self.assertEqual(
            jobs._cli_failure_reason(
                label="OpenClaude",
                returncode=1,
                timed_out=False,
                timeout_s=3600,
                proof_missing=False,
            ),
            "OpenClaude exited with code 1",
        )
        self.assertIn(
            "no proof artifact",
            jobs._cli_failure_reason(
                label="OpenClaude",
                returncode=0,
                timed_out=False,
                timeout_s=3600,
                proof_missing=True,
            ),
        )

    def test_openclaude_subscription_uses_codex_provider_not_api_mode(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                    "CODEX_HOME": "/tmp/codex-home",
                    "CLAUDE_CODE_USE_OPENAI": "1",
                    "OPENAI_BASE_URL": "https://api.openai.com/v1",
                    "OPENAI_API_FORMAT": "responses",
                    "AGENT_MONITOR_OPENCLAUDE_MAX_TURNS": "7",
                },
                clear=True,
            ),
            patch.object(sys, "argv", ["openclaude_runner.py", "prove 1 = 1"]),
            patch(
                "agent_monitor.runners.openclaude_runner.shutil.which",
                return_value="/bin/openclaude",
            ),
            patch(
                "agent_monitor.runners.openclaude_runner.os.execv",
                side_effect=RuntimeError("captured"),
            ) as execute,
        ):
            with self.assertRaisesRegex(RuntimeError, "captured"):
                openclaude_main()
            argv = execute.call_args.args[1]
            self.assertNotIn("CLAUDE_CODE_USE_OPENAI", os.environ)
            self.assertNotIn("OPENAI_BASE_URL", os.environ)
            self.assertNotIn("OPENAI_API_FORMAT", os.environ)
        self.assertEqual(argv[argv.index("--provider") + 1], "openai")
        self.assertEqual(argv[argv.index("--model") + 1], "codexplan")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "7")

    def test_every_engine_reports_health_and_auth(self) -> None:
        engines = list_engines()
        self.assertEqual({engine["id"] for engine in engines}, all_engine_ids())
        self.assertEqual(len(engines), len(all_engine_ids()))
        for engine in engines:
            self.assertIn(engine["health"], {"ready", "setup_required"})
            self.assertIsInstance(engine["auth_modes"], list)
            self.assertIsInstance(engine["subscription_supported"], bool)

    def test_python_wrappers_use_the_active_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = build_cli_command(
                "plain",
                prompt="Prove 1 + 1 = 2.",
                workspace=root,
                problem_file=root / "problem.txt",
            )
        self.assertIsNotNone(command)
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(
            jobs._user_extra_env(None)["AGENT_MONITOR_PYTHON"], sys.executable
        )

    def test_subscription_registry_matches_implemented_transports(self) -> None:
        self.assertTrue({"hermes", "openclaude"}.issubset(CODEX_SUBSCRIPTION_ENGINES))
        self.assertNotIn("ucla", CODEX_SUBSCRIPTION_ENGINES)

    def test_new_harness_auth_and_safety_contracts(self) -> None:
        by_id = {engine["id"]: engine for engine in list_engines()}
        self.assertEqual(_codex_adapter_model("gpt-5.6-terra"), "codexplan")
        self.assertEqual(
            _codex_adapter_model("not-a-codex-model"), "codexplan"
        )
        self.assertEqual(
            by_id["openclaude"]["supported_models"], ["gpt-5.6-sol"]
        )


        self.assertEqual(
            by_id["deepseek_harness"]["auth_modes"],
            ["codex_subscription", "api_key"],
        )
        self.assertEqual(by_id["danus"]["auth_modes"], ["codex_subscription"])
        self.assertFalse(by_id["danus"]["available"])
        self.assertEqual(by_id["danus"]["risk_level"], "high")

    def test_deepseek_wrapper_uses_the_active_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            command = build_cli_command(
                "deepseek_harness",
                prompt="Prove 2 is even.",
                workspace=workspace,
                problem_file=workspace / "problem.txt",
            )
        if command is not None:
            self.assertEqual(command[0], sys.executable)

    def test_nested_harness_proof_is_promoted_for_the_console(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            nested = workspace / "native" / "agent"
            nested.mkdir(parents=True)
            answer = nested / "answer.tex"
            answer.write_text(
                "\\documentclass{article}\n\\begin{document}\nA complete proof.\n\\end{document}\n"
            )
            promoted = _promote_proof_artifact(workspace)
            self.assertEqual(promoted, workspace / "proof.tex")
            self.assertEqual(promoted.read_text(), answer.read_text())


    def test_newer_nested_continuation_replaces_stale_root_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stale = workspace / "proof.tex"
            stale.write_text("old root proof\n" + "x" * 80)
            os.utime(stale, (1, 1))
            nested = workspace / "console-follow-up"
            nested.mkdir()
            answer = nested / "answer.tex"
            answer.write_text("new native continuation\n" + "y" * 80)

            promoted = _promote_proof_artifact(workspace)

            self.assertEqual(promoted, stale)
            self.assertEqual(promoted.read_text(), answer.read_text())

class ImproofDisplayRegressionTests(unittest.TestCase):
    def test_running_agent_start_is_visible_before_model_call(self) -> None:
        from agent_monitor.paths import ensure_import_paths

        ensure_import_paths()
        from harness_dashboard.improofbench_builder import build_run_from_improof_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"
            run_dir.mkdir()
            rows = [
                {
                    "kind": "dag.loop_iteration_started",
                    "agent": "DAGWorkflow",
                    "payload": {"iteration": 2},
                },
                {
                    "kind": "agent.start",
                    "agent": "cfg_codex_author",
                    "call_id": "live-author",
                    "ts": "2026-08-19T00:00:00Z",
                    "payload": {"input": {"problem": "Prove the claim."}},
                },
            ]
            (run_dir / "events.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            built = build_run_from_improof_artifacts("improof_live", run_dir)

        self.assertIsNotNone(built)
        self.assertEqual(len(built["agents"]), 1)
        agent = built["agents"][0]
        self.assertEqual(agent["role"], "author")
        self.assertEqual(agent["round_id"], 2)
        self.assertEqual(agent["status"], "running")
        self.assertIn("Prove the claim", agent["prompt"])

    def test_live_heartbeat_preserves_cached_rich_agents(self) -> None:
        rich = {
            "agents": [
                {
                    "trace_id": "improof_live::author-0",
                    "stage_name": "author-0",
                    "role": "author",
                    "pipeline_stage": "author",
                    "status": "running",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_input_tokens": 0,
                }
            ],
            "totals": {"agents": 1, "input_tokens": 0, "output_tokens": 0},
        }
        writes: list[dict] = []
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(jobs, "_artifact_agents", return_value=rich) as enrich,
            patch.object(jobs, "_write_run", side_effect=lambda run: writes.append(run)),
        ):
            flush = jobs._live_output_flusher(
                run_id="improof_live",
                engine="improof",
                problem_id="live",
                problem_text="Prove the claim.",
                started=time.time(),
                workspace=Path(tmp),
            )
            flush("")
            flush("heartbeat")

        self.assertEqual(enrich.call_count, 1)
        self.assertEqual([run["agents"][0]["role"] for run in writes], ["author", "author"])

    def test_live_improof_continuation_keeps_prior_monitor_nodes(self) -> None:
        rich = {
            "agents": [
                {
                    "trace_id": "improof_live::author-0",
                    "role": "author",
                    "status": "running",
                    "round_id": 0,
                }
            ],
            "totals": {"agents": 1, "input_tokens": 0, "output_tokens": 0},
        }
        baseline = {
            "run_id": "improof_live",
            "status": "finished",
            "agents": [
                {
                    "trace_id": "improof_live::prior-0",
                    "role": "critic",
                    "status": "finished",
                    "round_id": 0,
                }
            ],
            "totals": {"agents": 1, "input_tokens": 10, "output_tokens": 2},
        }
        writes: list[dict] = []
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(jobs, "_artifact_agents", return_value=rich),
            patch.object(jobs, "_write_run", side_effect=lambda run: writes.append(run)),
        ):
            flush = jobs._live_output_flusher(
                run_id="improof_live",
                engine="improof",
                problem_id="live",
                problem_text="Revise the proof.",
                started=time.time(),
                workspace=Path(tmp),
                baseline_run=baseline,
                continuation_job_id="follow-live",
            )
            flush("")

        self.assertEqual(
            [agent["role"] for agent in writes[0]["agents"]],
            ["critic", "author"],
        )
        self.assertEqual(writes[0]["status"], "running")
        self.assertEqual(writes[0]["continuation_count"], 1)
        self.assertEqual(writes[0]["totals"]["agents"], 2)

    def test_configured_codex_agents_keep_roles_rounds_outputs_and_edges(self) -> None:
        from agent_monitor.paths import ensure_import_paths

        ensure_import_paths()
        from harness_dashboard.improofbench_builder import build_run_from_improof_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260819-000000"
            agents_dir = run_dir / "agents"
            agents_dir.mkdir(parents=True)
            rows = []
            for round_id in (0, 1):
                rows.append({
                    "kind": "dag.loop_iteration_started",
                    "agent": "DAGWorkflow",
                    "payload": {"node": "loop", "iteration": round_id},
                })
                for role, call_id, output in (
                    ("author", f"a{round_id}", {"status": "done", "answer_tex": f"Solution round {round_id}"}),
                    ("critic", f"c{round_id}", {"status": "done", "critique": f"Critique round {round_id}"}),
                ):
                    agent_name = f"cfg_codex_{role}"
                    start = f"2026-08-19T00:00:0{round_id}Z"
                    end = f"2026-08-19T00:00:0{round_id + 1}Z"
                    input_data = {
                        "problem": "Prove a public smoke theorem.",
                        "round": round_id,
                        "previous_solution": "Earlier solution" if round_id else "",
                        "previous_critique": "Earlier critique" if round_id else "",
                    }
                    rows.extend((
                        {"kind": "agent.start", "agent": agent_name, "call_id": call_id, "ts": start, "payload": {"input": input_data}},
                        {"kind": "model.call", "agent": agent_name, "parent_call_id": call_id, "ts": end, "payload": {"model": "gpt-test", "in_tokens": 10, "out_tokens": 5, "cost_usd": 0.001}},
                        {"kind": "agent.end", "agent": agent_name, "call_id": call_id, "ts": end, "payload": {"output": output}},
                    ))
                    folder = agents_dir / f"{agent_name}-c{round_id}-{call_id}"
                    folder.mkdir()
                    (folder / "input.json").write_text(json.dumps(input_data), encoding="utf-8")
                    (folder / "output.json").write_text(json.dumps(output), encoding="utf-8")
            (run_dir / "events.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            (run_dir / "run-metadata.json").write_text(
                json.dumps({"config_snapshot": {"problem_id": "adhoc-smoke"}}), encoding="utf-8"
            )

            built = build_run_from_improof_artifacts("improof_adhoc_smoke", run_dir)

        self.assertIsNotNone(built)
        agents = built["agents"]
        self.assertEqual([a["role"] for a in agents], ["author", "critic", "author", "critic"])
        self.assertEqual([a["round_id"] for a in agents], [0, 0, 1, 1])
        self.assertTrue(all(a["status"] == "finished" for a in agents))
        self.assertTrue(all(a["latency_s"] == 1.0 for a in agents))
        self.assertIn("Solution round 0", agents[0]["output"])
        self.assertIn("Critique round 0", agents[1]["output"])
        self.assertIn("Earlier critique", agents[2]["prompt"])
        self.assertEqual([edge["type"] for edge in built["edges"]], ["verify", "refine", "verify"])
        self.assertEqual(built["stages_present"], ["author", "critic"])


class OpenClawRegressionTests(unittest.TestCase):
    def test_transport_failure_preserves_substantive_checkpoint_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Claimed solution\n\n" + "A checked partial argument. " * 20
                + "\n\nProvingConsole outcome: Solved\n",
                encoding="utf-8",
            )
            recovered = openclaw_runner._recover_checkpointed_proof(
                workspace,
                "CANDIDATE AUDIT GATE: audit. ProvingConsole outcome: <label>",
                1,
            )
            saved = proof.read_text(encoding="utf-8")
        self.assertTrue(recovered)
        self.assertIn("degraded run:", saved)
        self.assertIn("degraded audit:", saved)
        self.assertNotIn("ProvingConsole outcome: Solved", saved)
        self.assertEqual(saved.count("ProvingConsole outcome:"), 1)
        self.assertIn("ProvingConsole outcome: Partial Progress", saved)

    def test_transport_failure_without_substantive_checkpoint_stays_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text("too short", encoding="utf-8")
            self.assertFalse(
                openclaw_runner._recover_checkpointed_proof(workspace, "prove", 1)
            )

    def test_transport_failure_does_not_reuse_unchanged_prior_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text("# Existing\n\n" + "valid old work " * 30, encoding="utf-8")
            prior = openclaw_runner._proof_sha256(proof)
            self.assertFalse(
                openclaw_runner._recover_checkpointed_proof(
                    workspace,
                    "ProvingConsole outcome: <label>",
                    1,
                    prior_sha256=prior,
                )
            )

    def test_isolated_gateway_auth_uses_reusable_random_token(self) -> None:
        config: dict = {}
        with patch.object(openclaw_runner.secrets, "token_urlsafe", return_value="run-token"):
            token = openclaw_runner._ensure_local_gateway_auth(config)
        self.assertEqual(token, "run-token")
        self.assertEqual(config["gateway"]["mode"], "local")
        self.assertEqual(config["gateway"]["auth"], {"mode": "token", "token": "run-token"})
        self.assertEqual(openclaw_runner._ensure_local_gateway_auth(config), "run-token")

    def test_openclaw_binds_only_exact_trusted_tool_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tools = workspace / "_library" / "tools"
            tools.mkdir(parents=True)
            trusted = tools / "exact-math-certificate.sh"
            trusted.write_text(
                '#!/bin/bash\nexec "$AGENT_MONITOR_PYTHON" -m agent_monitor.proof_tools '
                'exact-math-certificate "${1:-}"\n',
                encoding="utf-8",
            )
            rebound = tools / "statement-fidelity-audit.sh"
            rebound.write_text(
                "#!/bin/bash\nexec /old/venv/python -m agent_monitor.proof_tools "
                'statement-fidelity-audit "${1:-}"\n',
                encoding="utf-8",
            )
            custom = tools / "custom.sh"
            custom.write_text('exec "$AGENT_MONITOR_PYTHON" custom\n', encoding="utf-8")
            proof_tools = workspace / "trusted" / "proof tools.py"
            changed = openclaw_runner._bind_workspace_tool_python(
                workspace, "/srv/agent monitor/python", proof_tools
            )
            self.assertEqual(changed, [trusted, rebound])
            self.assertIn(
                "'/srv/agent monitor/python' "
                f"'{proof_tools.resolve()}' exact-math-certificate",
                trusted.read_text(),
            )
            self.assertIn(
                "'/srv/agent monitor/python' "
                f"'{proof_tools.resolve()}' statement-fidelity-audit",
                rebound.read_text(),
            )
            self.assertEqual(
                custom.read_text(), 'exec "$AGENT_MONITOR_PYTHON" custom\n'
            )

    def test_openclaw_audit_prompt_requires_parent_merge_artifacts(self) -> None:
        ordinary = "Prove a finite lemma."
        self.assertEqual(openclaw_runner._augment_audit_prompt(ordinary), ordinary)
        audit = "CANDIDATE AUDIT GATE: run independent passes."
        augmented = openclaw_runner._augment_audit_prompt(audit)
        self.assertIn("verifier-output.json", augmented)
        self.assertIn("merge-map.json", augmented)
        self.assertIn("Do not use sessions_spawn or sessions_yield", augmented)
        self.assertIn("degraded_independence", augmented)
        self.assertEqual(
            openclaw_runner._augment_audit_prompt(augmented), augmented
        )

    def test_openclaw_persists_terminal_candidate_before_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = workspace / "session.jsonl"
            saved = openclaw_runner._persist_terminal_candidate(
                workspace,
                (
                    session,
                    "# Candidate\n\nA substantive terminal-only mathematical argument. "
                    * 4,
                ),
            )
            self.assertEqual(saved, workspace / "proof.md")
            original = saved.read_text(encoding="utf-8")
            second = openclaw_runner._persist_terminal_candidate(
                workspace, (session, "A different terminal response. " * 5)
            )
            self.assertEqual(second, saved)
            self.assertEqual(saved.read_text(encoding="utf-8"), original)

    def test_openclaw_normalizes_heading_outcome_to_exact_footer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Result\n\nA bounded lemma.\n\n"
                "## ProvingConsole Outcome\n\n**Known/Open Status**\n",
                encoding="utf-8",
            )
            changed = openclaw_runner._normalize_requested_outcome(
                workspace,
                "CONTROL CONDITION: end with ProvingConsole outcome: <label>.",
            )
            saved = proof.read_text(encoding="utf-8")
            self.assertTrue(changed)
            self.assertNotIn("## ProvingConsole Outcome", saved)
            self.assertEqual(saved.count("ProvingConsole outcome:"), 1)
            self.assertTrue(
                saved.rstrip().endswith(
                    "ProvingConsole outcome: Known/Open Status"
                )
            )

    def test_openclaw_conflicting_outcomes_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Result\n\n"
                "ProvingConsole outcome: Solved\n\n"
                "## ProvingConsole Outcome\n\n**Counterexample**\n",
                encoding="utf-8",
            )
            openclaw_runner._normalize_requested_outcome(
                workspace,
                "End with ProvingConsole outcome: <label>.",
            )
            saved = proof.read_text(encoding="utf-8")
            self.assertNotIn("ProvingConsole outcome: Solved", saved)
            self.assertNotIn("Counterexample", saved)
            self.assertEqual(saved.count("ProvingConsole outcome:"), 1)
            self.assertTrue(
                saved.rstrip().endswith(
                    "ProvingConsole outcome: Partial Progress"
                )
            )

    def test_openclaw_supplemental_audit_is_explicit_and_uses_selected_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Candidate\n\nA frozen candidate argument. " * 20
                + "\n\nProvingConsole outcome: Partial Progress\n",
                encoding="utf-8",
            )
            final = (
                "# Audited candidate\n\nThe central obligation remains open.\n\n"
                "ProvingConsole outcome: Partial Progress"
            )
            with (
                patch.object(
                    openclaw_runner,
                    "_validated_audit_contract_exists",
                    return_value=False,
                ),
                patch(
                    "agent_monitor.runners.codex_backend.subscription_enabled",
                    return_value=True,
                ),
                patch(
                    "agent_monitor.runners.deepseek_harness_runner.run_codex_audit",
                    return_value=final,
                ) as audit,
                patch("builtins.print"),
            ):
                self.assertFalse(
                    openclaw_runner._run_supplemental_audit(
                        workspace, "ordinary proof", "gpt-test"
                    )
                )
                self.assertTrue(
                    openclaw_runner._run_supplemental_audit(
                        workspace,
                        "CANDIDATE AUDIT GATE: audit. ProvingConsole outcome: <label>",
                        "gpt-test",
                    )
                )
            self.assertEqual(proof.read_text(encoding="utf-8").strip(), final)
            self.assertEqual(audit.call_args.kwargs["model"], "gpt-test")
            self.assertIsNone(audit.call_args.kwargs["call"])
            self.assertEqual(
                audit.call_args.kwargs["adapter"],
                "openclaw-response-orchestrated-audit",
            )

    def test_openclaw_supplemental_audit_uses_api_route_when_codex_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text(
                "# Candidate\n\nA bounded partial argument. " * 20,
                encoding="utf-8",
            )
            with (
                patch.object(
                    openclaw_runner,
                    "_validated_audit_contract_exists",
                    return_value=False,
                ),
                patch(
                    "agent_monitor.runners.codex_backend.subscription_enabled",
                    return_value=False,
                ),
                patch(
                    "agent_monitor.runners.deepseek_harness_runner.run_codex_audit",
                    return_value=(
                        "# Audited\n\nStill partial.\n\n"
                        "ProvingConsole outcome: Partial Progress"
                    ),
                ) as audit,
                patch("builtins.print"),
            ):
                self.assertTrue(
                    openclaw_runner._run_supplemental_audit(
                        workspace,
                        "CANDIDATE AUDIT GATE: audit. ProvingConsole outcome: <label>",
                        "anthropic/claude-test",
                    )
                )
            self.assertIs(audit.call_args.kwargs["call"], openclaw_runner._api_audit_call)
            self.assertEqual(
                audit.call_args.kwargs["model"], "anthropic/claude-test"
            )

    def test_openclaw_does_not_trust_incomplete_native_audit_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            native = workspace / "audit" / "native"
            native.mkdir(parents=True)
            (native / "run.json").write_text(
                json.dumps({"complete_core": True}), encoding="utf-8"
            )
            self.assertFalse(
                openclaw_runner._validated_audit_contract_exists(workspace)
            )

    def test_openclaw_accepts_valid_native_contract_without_adapter_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            native = workspace / "audit" / "native"
            _install_reconciled_audit(workspace, native)
            with patch(
                "agent_monitor.runners.deepseek_harness_runner._validate_verifier_document",
                return_value=(True, "valid"),
            ) as validator:
                self.assertTrue(
                    openclaw_runner._validated_audit_contract_exists(workspace)
                )
            self.assertEqual(validator.call_count, 3)

    def test_openclaw_rejects_invalid_optional_native_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            native = workspace / "audit" / "native"
            native.mkdir(parents=True)
            for name in (
                "global.json",
                "decomposed.json",
                "verifier-output.json",
                "refuter.json",
            ):
                (native / name).write_text("{}\n", encoding="utf-8")
            (native / "merge-map.json").write_text(
                json.dumps(
                    {
                        "schema_version": "ensemble-paper-audit-app.merge-map.v1",
                        "findings": [],
                        "counts": {
                            "global_only": 0,
                            "decomposed_only": 0,
                            "both": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (native / "run.json").write_text(
                json.dumps({"run_id": "native"}), encoding="utf-8"
            )

            def validate(_directory, label, _document):
                return ("refuter" not in label, label)

            with patch(
                "agent_monitor.runners.deepseek_harness_runner._validate_verifier_document",
                side_effect=validate,
            ):
                self.assertFalse(
                    openclaw_runner._validated_audit_contract_exists(workspace)
                )

    def test_terminal_watchdog_ignores_stale_and_tool_use_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            session = state / "agents" / "main" / "sessions" / "run.jsonl"
            session.parent.mkdir(parents=True)

            def event(stop_reason: str, text: str) -> str:
                return json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "stopReason": stop_reason,
                            "content": [{"type": "text", "text": text}],
                        },
                    }
                ) + "\n"

            session.write_text(event("stop", "stale final"), encoding="utf-8")
            offsets = openclaw_runner._session_offsets(state)
            self.assertIsNone(openclaw_runner._terminal_event_since(state, offsets))

            with session.open("a", encoding="utf-8") as handle:
                handle.write(event("toolUse", "intermediate"))
            self.assertIsNone(openclaw_runner._terminal_event_since(state, offsets))

            with session.open("a", encoding="utf-8") as handle:
                handle.write(event("stop", "fresh final"))
            found = openclaw_runner._terminal_event_since(state, offsets)

        self.assertIsNotNone(found)
        self.assertEqual(found[0], session)
        self.assertEqual(found[1], "fresh final")

    def test_terminal_watchdog_ignores_subagent_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            sessions = state / "agents" / "main" / "sessions"
            sessions.mkdir(parents=True)
            parent = sessions / "parent.jsonl"
            child = sessions / "child.jsonl"
            parent.write_text("", encoding="utf-8")
            child.write_text(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "stopReason": "stop",
                            "content": [{"type": "text", "text": "child complete"}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            for path, key in (
                (parent, "agent:main:parent-run"),
                (child, "agent:main:subagent:child-run"),
            ):
                path.with_suffix(".trajectory.jsonl").write_text(
                    json.dumps({"sessionKey": key}) + "\n", encoding="utf-8"
                )
            offsets = openclaw_runner._session_offsets(
                state, "agent:main:parent-run"
            )
            self.assertIsNone(
                openclaw_runner._terminal_event_since(
                    state, offsets, "agent:main:parent-run"
                )
            )
            with parent.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "assistant",
                                "stopReason": "stop",
                                "content": [{"type": "text", "text": "parent complete"}],
                            },
                        }
                    )
                    + "\n"
                )
            found = openclaw_runner._terminal_event_since(
                state, offsets, "agent:main:parent-run"
            )
            self.assertIsNotNone(found)
            self.assertEqual(found[0], parent)
            self.assertEqual(found[1], "parent complete")

    def test_terminal_watchdog_stops_hung_process_group_after_fresh_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            script = state / "hung_agent.py"
            script.write_text(
                """import json, os, pathlib, time
root = pathlib.Path(__import__('sys').argv[1])
session = root / 'agents' / 'main' / 'sessions' / 'run.jsonl'
session.parent.mkdir(parents=True)
(root / 'pid').write_text(str(os.getpid()))
session.write_text(json.dumps({
    'type': 'message',
    'message': {
        'role': 'assistant',
        'stopReason': 'stop',
        'content': [{'type': 'text', 'text': 'complete proof'}],
    },
}) + '\\n')
time.sleep(60)
""",
                encoding="utf-8",
            )
            with patch("builtins.print") as printed:
                code = openclaw_runner._run_with_terminal_watchdog(
                    [sys.executable, str(script), str(state)],
                    state,
                    grace_s=0.1,
                )
            child_pid = int((state / "pid").read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        payload = json.loads(printed.call_args.args[0])
        self.assertTrue(payload["agentMonitorTerminalWatchdog"])
        self.assertEqual(payload["payloads"][0]["text"], "complete proof")

    def test_final_message_is_saved_when_openclaw_did_not_write_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = workspace / "session.jsonl"
            proof = "By reflexivity of equality, every object equals itself; hence 0 = 0."
            session.write_text(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": proof}],
                            "usage": {"input": 10, "output": 10},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            parser = CLIEventParser("openclaw")
            parser.feed(json.dumps({"sessionFile": str(session)}))
            parser.finalize_openclaw()

            artifact = _persist_openclaw_final_answer(parser, workspace)

            self.assertEqual(artifact, workspace / "proof.md")
            self.assertEqual(artifact.read_text(encoding="utf-8").strip(), proof)


    def test_complete_text_response_is_saved_for_fileless_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = "By induction on n, the asserted identity follows in the base and successor cases."

            artifact = _persist_text_final_answer(proof, workspace, engine="Hermes")

            self.assertEqual(artifact, workspace / "proof.md")
            self.assertEqual(artifact.read_text(encoding="utf-8").strip(), proof)


class LeanHarnessRegressionTests(unittest.TestCase):
    def test_lean_notes_strip_false_product_audit_availability_claims(self) -> None:
        raw = (
            "The exact open target remains behind one sorry "
            "(degraded audit: skill file unavailable). "
            "Audit: the main declaration is incomplete, while helper is verified."
        )
        cleaned = lean_verify._sanitize_formalization_notes(raw)
        self.assertNotIn("degraded audit", cleaned.lower())
        self.assertNotIn("skill file unavailable", cleaned.lower())
        self.assertIn("Audit: the main declaration is incomplete", cleaned)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            lean_src = "theorem helper : True := by trivial"
            lean_dir = workspace / lean_verify.LEAN_DIRNAME
            lean_dir.mkdir()
            (lean_dir / lean_verify.PROOF_FILENAME).write_text(
                lean_src, encoding="utf-8"
            )
            authoritative_check = _broker_bound_lean_check(lean_dir, False)
            with patch.object(
                lean_verify, "toolchain_status", return_value={"available": True}
            ):
                persisted = lean_verify._persist(
                    workspace,
                    lean_src=lean_src,
                    status="verified",
                    uses_mathlib=False,
                    attempts=[],
                    notes=raw,
                    authoritative_check=authoritative_check,
                )
        self.assertEqual(persisted["notes"], cleaned)

    def test_lean_citations_dedupe_source_identity_and_stop_at_outcome(self) -> None:
        lean = """/-- Exact open target.
Cites: [Adenwalla 2024, DOI 10.1016/j.disc.2024.114183] — open-status source -/
theorem main : True := by
  trivial
"""
        raw = [
            {
                "id": "7",
                "decl": "main",
                "text": (
                    "Adenwalla states the target is open; "
                    "doi:10.1016/j.disc.2024.114183"
                ),
            },
            {
                "id": "7",
                "text": "DOI10.1016/j.disc.2024.114183",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text(
                """## References

[1] doi:10.1016/j.disc.2024.114183 — full bibliography entry.

degraded audit: the baseline cannot execute file-backed passes.

ProvingConsole outcome: Partial Progress
""",
                encoding="utf-8",
            )
            reconciled = lean_verify._reconcile_citations(
                raw, lean, workspace=workspace
            )

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["id"], "1")
        self.assertEqual(reconciled[0].get("decl"), "main")
        self.assertNotIn("ProvingConsole outcome", reconciled[0]["text"])
        self.assertNotIn("degraded audit", reconciled[0]["text"])

        legacy = lean_verify._normalize_citations(
            [
                {
                    "text": (
                        "doi:10.4064/aa163-2-4 — source. degraded audit: "
                        "single call. ProvingConsole outcome: Partial Progre"
                    )
                }
            ]
        )
        self.assertEqual(legacy[0]["text"], "doi:10.4064/aa163-2-4 — source.")

    def test_lean_citations_dedupe_markdown_and_ignore_inequality_lines(self) -> None:
        lean = """import Mathlib.Data.Nat.PrimeFin
/-- Offset equivalence.
Cites: self-derived — substitution between `m` and `k = n - m` -/
theorem offsets (n : Nat) :
    (∀ k : Nat,
      1 ≤ k → k < n → k ≤ n) := by
  omega

/-- Exact open target.
Cites: [Erdos Problem #413] — exact open infinitude target -/
theorem main : True := by
  trivial
"""
        from_source = lean_verify._citations_from_lean(lean)
        self.assertEqual(len(from_source), 2)
        self.assertNotIn("< n", " ".join(c["text"] for c in from_source))

        payload = json.dumps(
            {
                "lean": lean,
                "citations": [
                    {
                        "id": "1",
                        "decl": "offsets",
                        "text": (
                            "self-derived — substitution between m and k = n - m."
                        ),
                    },
                    {
                        "id": "2",
                        "decl": "main",
                        "text": (
                            "Erdos Problem #413 — exact open infinitude target"
                        ),
                    },
                ],
            }
        )
        parsed = lean_verify._parse_model_payload(payload)
        self.assertEqual(len(parsed["citations"]), 2)
        self.assertEqual(
            {c.get("decl") for c in parsed["citations"]}, {"offsets", "main"}
        )

        legacy = [
            *parsed["citations"],
            *from_source,
            {"id": "0", "text": "< n ∧", "kind": "theorem"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps(
                    {
                        "status": "incomplete",
                        "lean": lean,
                        "citations": legacy,
                        "attempts": [],
                    }
                ),
                encoding="utf-8",
            )
            summary = lean_verify.monitor_summary(workspace)
            reconciled = lean_verify._reconcile_citations(
                legacy, lean, workspace=workspace
            )

        self.assertEqual(summary["citation_count"], 2)
        self.assertEqual(len(reconciled), 2)

    def test_statement_fidelity_fix_gets_one_bounded_compile_repair(self) -> None:
        check_outcomes = iter([True, False, True])

        def broker_check(lean_dir: Path, uses_mathlib: bool) -> dict[str, object]:
            ok = next(check_outcomes)
            return _broker_bound_lean_check(
                lean_dir,
                uses_mathlib,
                ok=ok,
                stdout="" if ok else "unsolved goals",
            )
        translated = json.dumps(
            {"lean": "theorem main (p : Prop) (h : p) : p := by exact h"}
        )
        compiled_repair = json.dumps(
            {"lean": "theorem main (p : Prop) (h : p) : p := by exact h"}
        )
        statement_fix = {
            "lean": "theorem main (p : Prop) (h : p) : p := by badTactic",
            "uses_mathlib": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "problem.txt").write_text(
                "Prove the exact claim.", encoding="utf-8"
            )
            with (
                patch("agent_monitor.settings.resolved_user_env", return_value={}),
                patch.object(
                    lean_verify,
                    "_proof_source",
                    return_value=("proof.md", "proof"),
                ),
                patch.object(
                    lean_verify,
                    "_chat",
                    side_effect=[
                        ("gpt-test", translated),
                        ("gpt-test", compiled_repair),
                    ],
                ),
                patch.object(
                    lean_verify,
                    "_run_lean_check",
                    side_effect=broker_check,
                ),
                patch.object(
                    lean_verify,
                    "_repair_statement",
                    return_value=statement_fix,
                ),
                patch.object(
                    lean_verify,
                    "_audit_faithfulness",
                    side_effect=[
                        {
                            "ok": True,
                            "faithful": False,
                            "severity": "major",
                            "issues": ["too weak"],
                        },
                        {
                            "ok": True,
                            "faithful": True,
                            "severity": "ok",
                            "issues": [],
                        },
                    ],
                ),
                patch.object(
                    lean_verify,
                    "toolchain_status",
                    return_value={"available": True},
                ),
            ):
                result = lean_verify.generate(
                    workspace=workspace,
                    run_record={},
                    user={"id": 7},
                    model="gpt-test",
                    max_repairs=2,
                )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(
            [attempt["action"] for attempt in result["attempts"]],
            ["verify", "statement-fix", "statement-repair"],
        )
        self.assertTrue(result["fidelity"]["audit"]["faithful"])

    def test_legacy_mathlib_omega_import_maps_to_lean_414_core_module(self) -> None:
        payload = lean_verify._parse_model_payload(
            '{"lean":"import Mathlib.Tactic.Omega\\n\\ntheorem main : True := by trivial"}'
        )
        self.assertIn("import Lean.Elab.Tactic.Omega", payload["lean"])
        self.assertNotIn("import Mathlib.Tactic.Omega", payload["lean"])

    def test_legacy_mathlib_nlinarith_import_maps_to_linarith_module(self) -> None:
        payload = lean_verify._parse_model_payload(
            '{"lean":"import Mathlib.Tactic.Nlinarith\\n\\nexample : True := by trivial"}'
        )
        self.assertIn("import Mathlib.Tactic.Linarith", payload["lean"])
        self.assertNotIn("import Mathlib.Tactic.Nlinarith", payload["lean"])

    def test_unbuilt_int_and_fin_imports_map_to_prebuilt_lean_414_modules(self) -> None:
        payload = lean_verify._parse_model_payload(
            json.dumps(
                {
                    "lean": "import Mathlib.Data.Int.Basic\n"
                    "import Mathlib.Data.Fin.Interval\n\n"
                    "example : True := by trivial"
                }
            )
        )
        self.assertIn("import Mathlib.Data.Int.Order.Basic", payload["lean"])
        self.assertIn("import Mathlib.Order.Fin.Basic", payload["lean"])
        self.assertNotIn("import Mathlib.Data.Int.Basic", payload["lean"])
        self.assertNotIn("import Mathlib.Data.Fin.Interval", payload["lean"])

    def test_prime_factors_import_maps_to_lean_414_finset_module(self) -> None:
        for obsolete in ("Mathlib.Data.Nat.Prime.Defs", "Mathlib.Data.Nat.Prime.Finset"):
            with self.subTest(obsolete=obsolete):
                payload = lean_verify._parse_model_payload(
                    json.dumps({"lean": f"import {obsolete}\n\ndef omega (n : Nat) := n.primeFactors.card"})
                )
                self.assertIn("import Mathlib.Data.Nat.PrimeFin", payload["lean"])
                self.assertNotIn(f"import {obsolete}", payload["lean"])

    def test_fintype_derivation_import_maps_to_deriver_module(self) -> None:
        payload = lean_verify._parse_model_payload(
            json.dumps(
                {
                    "lean": "import Mathlib.Data.Fintype.Basic\n"
                    "inductive V | a | b deriving DecidableEq, Fintype"
                }
            )
        )
        self.assertIn("import Mathlib.Tactic.DeriveFintype", payload["lean"])
        self.assertNotIn("import Mathlib.Data.Fintype.Basic", payload["lean"])

    def test_lean_prompts_require_narrow_mathlib_imports(self) -> None:
        self.assertIn("Never use the broad `import Mathlib`", lean_verify.TRANSLATE_PROMPT)
        self.assertIn("lake env lean Proof.lean", lean_verify.HARNESS_TASK)
        self.assertIn("never the\nbroad `import Mathlib`", lean_verify.HARNESS_TASK)
        self.assertIn("formalise\n  each feasible self-derived lemma", lean_verify.TRANSLATE_PROMPT)
        self.assertIn("Do not conjoin optional\n  minimality", lean_verify.TRANSLATE_PROMPT)
        self.assertIn("added nonessential conjuncts", lean_verify.REPAIR_PROMPT)

    @staticmethod
    def _write_verified_lean_cache(workspace: Path) -> None:
        (workspace / "lean").mkdir(exist_ok=True)
        source = "theorem main : True := by\n  trivial\n"
        (workspace / "lean" / "Proof.lean").write_text(source, encoding="utf-8")
        (workspace / lean_verify.RESULT_FILENAME).write_text(
            json.dumps(
                {
                    "status": "verified",
                    "action": "harness",
                    "lean": source,
                    "sorry_count": 0,
                    "attempts": [{"check": {"ok": True}}],
                    "fidelity": {"severity": "ok", "flags": [], "audit": {"ok": False}},
                    "chat": [],
                }
            ),
            encoding="utf-8",
        )

    def test_new_harness_clears_the_previous_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lean_dir = Path(tmp) / "lean"
            lean_dir.mkdir()
            log = lean_dir / "harness.log"
            log.write_text("old run output\n", encoding="utf-8")

            reset = lean_verify._reset_harness_log(lean_dir)

            self.assertEqual(reset, log)
            self.assertEqual(log.read_text(encoding="utf-8"), "")

    def test_lean_ui_scopes_async_responses_to_the_originating_run(self) -> None:
        console = (
            Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
        ).read_text(encoding="utf-8")
        self.assertIn("const actionRunId=runId", console)
        self.assertIn("encodeURIComponent(actionRunId)", console)
        self.assertIn("if(actionRunId!==runId)return", console)
        self.assertIn("const requestedRunId=runId", console)
        self.assertIn("prepareLeanHarnessStart", console)
        self.assertIn("const leanPendingRuns=new Map()", console)

    def test_monitor_summary_is_bounded_and_preserves_formal_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "lean").mkdir()
            (workspace / "lean" / "TASK.md").write_text(
                "Formalize the saved mathematical problem.", encoding="utf-8"
            )
            (workspace / "lean" / "harness.log").write_text(
                "Reading additional input from stdin...\n"
                + json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "m1", "type": "agent_message", "text": "I will formalize it."},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "c1",
                            "type": "command_execution",
                            "command": "lean Proof.lean",
                            "aggregated_output": "compiled cleanly",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps(
                    {
                        "status": "incomplete",
                        "generated_at": "2026-08-18T01:00:00Z",
                        "action": "check",
                        "lean": "theorem main : True := by\n  sorry\n",
                        "sorry_count": 1,
                        "sorry_lines": [2],
                        "attempts": [
                            {
                                "round": 0,
                                "action": "check",
                                "sorry_count": 1,
                                "uses_mathlib": False,
                                "check": {
                                    "ok": True,
                                    "status": "verified",
                                    "exit_code": 0,
                                    "duration_s": 0.25,
                                },
                                "stderr_tail": "",
                                "stdout_tail": "",
                            }
                        ],
                        "fidelity": {
                            "severity": "minor",
                            "flags": [
                                {
                                    "id": "sorry",
                                    "severity": "minor",
                                    "message": "one admitted gap",
                                    "lines": [2],
                                }
                            ],
                            "declarations": [{"kind": "theorem", "name": "main", "line": 1}],
                            "audit": {
                                "ok": True,
                                "faithful": True,
                                "severity": "minor",
                                "verdict": "The statement matches but the proof is incomplete.",
                            },
                        },
                        "chat": [
                            {
                                "role": "user",
                                "content": "Replace the sorry.",
                                "ts": "2026-08-18T01:01:00Z",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = lean_verify.monitor_summary(workspace)

        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["attempt_count"], 1)
        self.assertEqual(summary["attempts"][0]["status"], "incomplete")
        self.assertEqual(summary["fidelity"]["severity"], "minor")
        self.assertEqual(summary["chat"][0]["role"], "user")
        self.assertEqual(summary["conversation_event_count"], 3)
        self.assertEqual(summary["conversation"][1]["content"], "I will formalize it.")
        self.assertEqual(summary["conversation"][2]["command"], "lean Proof.lean")
        self.assertEqual(summary["harness_usage"]["input_tokens"], 100)
        self.assertEqual(summary["harness_prompt"], "Formalize the saved mathematical problem.")
        self.assertNotIn("lean", summary)

    def test_monitor_and_conversation_offer_formal_lean_modes(self) -> None:
        root = Path(__file__).parents[1]
        console = (root / "agent_monitor" / "web" / "console.html").read_text(
            encoding="utf-8"
        )
        monitor = (
            root / "monitor_core" / "harness_dashboard" / "web" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('data-convo-mode="formal"', console)
        self.assertIn("formalConversationHtml", console)
        self.assertIn("run.lean_verification", console)
        self.assertIn('data-monitor-mode="lean"', monitor)
        self.assertIn("renderLeanMonitor", monitor)
        self.assertIn("renderResearchAuditSection", monitor)
        self.assertIn("Review execution and proof acceptance are reported separately.", monitor)
        self.assertIn("Candidate accepted", monitor)
        self.assertIn("buildFormalLeanRun", monitor)
        self.assertIn("All Formal Events", monitor)
        self.assertIn("Solution DAG", monitor)
        self.assertNotIn("body.formal-monitor .sidebar { display: none; }", monitor)
        self.assertIn("formal verification context", monitor)
        self.assertIn("lean/TASK.md", monitor)
        self.assertIn("proving-console-view", monitor)

    def test_status_question_answers_without_rewriting_or_calling_a_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_verified_lean_cache(workspace)
            before = lean_verify.read_source(workspace)
            with patch.object(lean_verify, "_chat", side_effect=AssertionError("model must not run")):
                result = lean_verify.revise_with_feedback(
                    workspace=workspace,
                    user={"id": 7},
                    message="is this all proved?",
                    lean=before,
                )

            self.assertEqual(lean_verify.read_source(workspace), before)
            self.assertEqual([item["role"] for item in result["chat"]], ["user", "assistant"])
            self.assertIn("kernel accepted", result["chat"][-1]["content"])
            self.assertIn("fidelity audit", result["chat"][-1]["content"])
            self.assertEqual(result["interaction"]["kind"], "question")

    def test_rename_request_with_complete_proof_wording_is_an_edit(self) -> None:
        self.assertFalse(
            lean_verify._feedback_is_question(
                "Rename the theorem to addition_two and keep a complete proof."
            )
        )

    def test_status_question_does_not_certify_or_overwrite_an_unchecked_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_verified_lean_cache(workspace)
            checked = lean_verify.read_source(workspace)
            draft = checked + "\n-- unchecked editor draft\n"
            result = lean_verify.revise_with_feedback(
                workspace=workspace,
                user={"id": 7},
                message="is this all proved?",
                lean=draft,
            )

            self.assertEqual(lean_verify.read_source(workspace), checked)
            self.assertIn("differs from the last checked", result["chat"][-1]["content"])
            self.assertIn("Run Check", result["chat"][-1]["content"])
            self.assertEqual(result["interaction"]["kind"], "question")

    def test_edit_request_falls_back_to_connected_codex_and_rechecks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_verified_lean_cache(workspace)
            revised = "theorem main (p : Prop) (h : p) : p := by\n  exact h\n"
            with (
                patch.object(lean_verify, "_chat", side_effect=ValueError("No API key")),
                patch.object(
                    lean_verify,
                    "_codex_subscription_interaction",
                    return_value={
                        "answer": "Replaced the proof term and checked it.",
                        "lean": revised,
                        "model": "Codex subscription",
                    },
                ) as codex,
                patch.object(
                    lean_verify,
                    "_run_lean_check",
                    side_effect=_broker_bound_lean_check,
                ),
                patch.object(
                    lean_verify,
                    "_audit_faithfulness",
                    return_value={"ok": True, "faithful": True, "severity": "ok"},
                ),
                patch.object(
                    lean_verify,
                    "toolchain_status",
                    return_value={"available": True, "version": "Lean test"},
                ),
            ):
                result = lean_verify.revise_with_feedback(
                    workspace=workspace,
                    user={"id": 7},
                    message="replace trivial with an explicit proof term",
                )

            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["model"], "Codex subscription")
            self.assertEqual(result["lean"].strip(), revised.strip())
            self.assertTrue(codex.call_args.kwargs["edit"])
            self.assertEqual(result["chat"][-1]["role"], "assistant")

    def test_intervention_failure_is_persisted_as_an_assistant_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_verified_lean_cache(workspace)
            with (
                patch.object(lean_verify, "_chat", side_effect=ValueError("No API key")),
                patch.object(
                    lean_verify,
                    "_codex_subscription_interaction",
                    side_effect=ValueError("Codex is disconnected"),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "Codex is disconnected"):
                    lean_verify.revise_with_feedback(
                        workspace=workspace,
                        user={"id": 7},
                        message="explain why the theorem is vacuous?",
                    )

            chat=(lean_verify.load_cached(workspace) or {})["chat"]
            self.assertEqual([item["role"] for item in chat], ["user", "assistant"])
            self.assertIn("couldn't complete", chat[-1]["content"])
            self.assertIn("Codex is disconnected", chat[-1]["content"])

    def test_second_intervention_is_rejected_while_one_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_verified_lean_cache(workspace)
            lock = lean_verify._intervention_lock(workspace)
            lock.acquire()
            try:
                with self.assertRaisesRegex(ValueError, "already running"):
                    lean_verify.revise_with_feedback(
                        workspace=workspace,
                        user={"id": 7},
                        message="is this complete?",
                    )
            finally:
                lock.release()


class LibraryPlacementTests(unittest.TestCase):
    def test_context_advertises_only_materialized_library_files(self) -> None:
        tool = {
            "type": "tool",
            "name": "checker",
            "description": "read-only check",
            "content": "#!/bin/bash\ntrue\n",
            "approval_mode": "auto",
            "read_only": True,
        }

        with patch.object(
            library,
            "enabled_items",
            side_effect=lambda item_type=None: [tool] if item_type == "tool" else [],
        ):
            context = library.compose_context()

        self.assertIn("_library/tools/", context)
        self.assertIn("_library/tools.json", context)
        self.assertNotIn("_library/MEMORY.md", context)
        self.assertNotIn("_library/SKILLS.md", context)

    def test_lean_tool_detects_both_lakefile_formats(self) -> None:
        content = library._LEAN_CHECK_TOOL["content"]
        self.assertIn("lakefile.toml", content)
        self.assertIn("lakefile.lean", content)

    def test_seed_migrates_the_legacy_toml_only_lean_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            library_file = library_dir / "library.json"
            library_dir.mkdir()
            legacy = {
                **library._LEAN_CHECK_TOOL,
                "content": library._LEAN_CHECK_TOOL["content"].replace(
                    library._LEAN_MATHLIB_PROJECT_CHECK_V2,
                    library._LEAN_MATHLIB_PROJECT_CHECK_V1,
                ),
            }
            library_file.write_text(
                json.dumps({"items": [legacy], "settings": {}}), encoding="utf-8"
            )
            with (
                patch.object(library, "LIBRARY_DIR", library_dir),
                patch.object(library, "LIBRARY_FILE", library_file),
                patch.object(library, "SEED_MARKER", library_dir / ".seeded"),
            ):
                library.ensure_seeded()

            data = json.loads(library_file.read_text(encoding="utf-8"))
            migrated = next(
                item for item in data["items"] if item["id"] == "tool_lean_check"
            )
            self.assertIn(library._LEAN_MATHLIB_PROJECT_CHECK_V2, migrated["content"])
            self.assertNotIn(library._LEAN_MATHLIB_PROJECT_CHECK_V1, migrated["content"])

    def test_seed_migrates_the_legacy_latex_citation_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            library_file = library_dir / "library.json"
            library_dir.mkdir()
            legacy = {
                **library._CITATION_AUDIT_TOOL,
                "content": library._CITATION_AUDIT_TOOL["content"].replace(
                    library._CITATION_AUDIT_STRUCTURE_V2,
                    library._CITATION_AUDIT_STRUCTURE_V1,
                ),
            }
            library_file.write_text(
                json.dumps({"items": [legacy], "settings": {}}), encoding="utf-8"
            )
            with (
                patch.object(library, "LIBRARY_DIR", library_dir),
                patch.object(library, "LIBRARY_FILE", library_file),
                patch.object(library, "SEED_MARKER", library_dir / ".seeded"),
            ):
                library.ensure_seeded()

            data = json.loads(library_file.read_text(encoding="utf-8"))
            migrated = next(
                item
                for item in data["items"]
                if item["id"] == "tool_citation_audit"
            )
            self.assertIn(
                library._CITATION_AUDIT_STRUCTURE_V2, migrated["content"]
            )
            self.assertNotIn(
                library._CITATION_AUDIT_STRUCTURE_V1, migrated["content"]
            )

    def test_native_skills_are_listed_under_skills_and_a_tool_is_seeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            with (
                patch.object(library, "LIBRARY_DIR", library_dir),
                patch.object(library, "LIBRARY_FILE", library_dir / "library.json"),
                patch.object(library, "SEED_MARKER", library_dir / ".seeded"),
                patch.object(agent_config, "HERMES_HOME", root / "hermes"),
            ):
                result = library.get_library()
                skills = [item for item in result["items"] if item["type"] == "skill"]
                tools = [item for item in result["items"] if item["type"] == "tool"]
                self.assertTrue(skills)
                self.assertTrue(all(item["id"].startswith("agent_skill:") for item in skills))
                self.assertEqual(
                    {item["name"] for item in tools},
                    {
                        "proof-sanity-check",
                        "lean-check",
                        "citation-audit",
                        "primary-source-fetch",
                        "literature-search",
                        "pdf-text-extract",
                        "exact-math-certificate",
                        "bounded-counterexample-search",
                        "statement-fidelity-audit",
                        "audit-output-validator",
                    },
                )
                self.assertTrue(all(item["read_only"] for item in tools))
                self.assertTrue(all(item["approval_mode"] == "auto" for item in tools))
                self.assertTrue(all(item["input_schema"]["type"] == "object" for item in tools))
                self.assertGreater(result["counts"]["skill"], 0)
                self.assertEqual(result["counts"]["tool"], 10)

                workspace = root / "workspace"
                workspace.mkdir()
                library.materialize(workspace)
                manifest = json.loads(
                    (workspace / "_library" / "tools.json").read_text(encoding="utf-8")
                )
                self.assertEqual(len(manifest), 10)
                self.assertTrue(all(item["approval_mode"] == "auto" for item in manifest))
                sanity_tool = next(
                    item for item in manifest if item["name"] == "proof-sanity-check"
                )
                malformed = workspace / "malformed.md"
                malformed.write_bytes(b"valid line\n{\x08f1}\n")
                malformed_check = subprocess.run(
                    ["bash", str(workspace / sanity_tool["script"]), "malformed.md"],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                self.assertEqual(malformed_check.returncode, 1)
                self.assertIn("disallowed ASCII control bytes", malformed_check.stdout)
                narrative = workspace / "narrative.md"
                narrative.write_text(
                    "# Partial Progress\n\n"
                    "The upstream Lean declaration contains `sorry`, so it is not verified.\n\n"
                    "## References\n",
                    encoding="utf-8",
                )
                narrative_check = subprocess.run(
                    ["bash", str(workspace / sanity_tool["script"]), "narrative.md"],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                self.assertEqual(narrative_check.returncode, 0)
                self.assertNotIn("WARNING: unfinished proof markers", narrative_check.stdout)
                self.assertIn("no executable TODO/FIXME/sorry/admit", narrative_check.stdout)
                lean_gap = workspace / "lean-gap.md"
                lean_gap.write_text(
                    "# Incomplete helper\n\n```lean\ntheorem helper : True := by\n  sorry\n```\n",
                    encoding="utf-8",
                )
                lean_gap_check = subprocess.run(
                    ["bash", str(workspace / sanity_tool["script"]), "lean-gap.md"],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                self.assertEqual(lean_gap_check.returncode, 0)
                self.assertIn("5:  sorry", lean_gap_check.stdout)
                self.assertIn("WARNING: unfinished proof markers", lean_gap_check.stdout)
                citation_tool = next(
                    item for item in manifest if item["name"] == "citation-audit"
                )
                latex = workspace / "proof.tex"
                latex.write_text(
                    "Claim \\cite[Theorem 1]{Adenwalla}.\n"
                    "\\begin{thebibliography}{9}\n"
                    "\\bibitem{Adenwalla} Source.\n"
                    "\\end{thebibliography}\n",
                    encoding="utf-8",
                )
                citation_check = subprocess.run(
                    ["bash", str(workspace / citation_tool["script"]), "proof.tex"],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                self.assertEqual(
                    citation_check.returncode, 0, citation_check.stdout
                )
                self.assertIn(
                    "bibliography section found", citation_check.stdout
                )
                self.assertIn(
                    "all LaTeX citation keys resolve", citation_check.stdout
                )
                latex.write_text(
                    "Missing \\cite{Unknown}.\n", encoding="utf-8"
                )
                missing_check = subprocess.run(
                    ["bash", str(workspace / citation_tool["script"]), "proof.tex"],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                self.assertEqual(missing_check.returncode, 1)
                self.assertIn(
                    "without matching bibitems: Unknown", missing_check.stdout
                )
                source_tool = next(
                    item for item in manifest if item["name"] == "primary-source-fetch"
                )
                self.assertEqual(source_tool["input_schema"]["required"], ["url"])
                source_script = workspace / source_tool["script"]
                denied = subprocess.run(
                    ["bash", str(source_script), "https://example.com/not-allowed"],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                self.assertEqual(denied.returncode, 1)
                self.assertIn("not allowlisted", denied.stderr)
                source_text = source_script.read_text(encoding="utf-8")
                self.assertIn("raw.githubusercontent.com", source_text)
                self.assertIn("google-deepmind", source_text)
                denied_other_github = subprocess.run(
                    [
                        "bash",
                        str(source_script),
                        "https://github.com/untrusted/repo/blob/main/file.md",
                    ],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                self.assertEqual(denied_other_github.returncode, 1)
                self.assertIn("not allowlisted", denied_other_github.stderr)
                denied_other_raw_repo = subprocess.run(
                    [
                        "bash",
                        str(source_script),
                        "https://raw.githubusercontent.com/untrusted/repo/main/file.md",
                    ],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                self.assertEqual(denied_other_raw_repo.returncode, 1)
                self.assertIn("repository is not allowlisted", denied_other_raw_repo.stderr)

                literature_tool = next(
                    item for item in manifest if item["name"] == "literature-search"
                )
                self.assertEqual(literature_tool["input_schema"]["required"], ["query"])
                literature_script = (workspace / literature_tool["script"]).read_text(
                    encoding="utf-8"
                )
                self.assertIn("https://export.arxiv.org/api/query", literature_script)
                self.assertIn("https://api.crossref.org/works", literature_script)
                self.assertIn("ARXIV_TOTAL_RESULTS", literature_script)
                self.assertIn("CROSSREF_TOTAL_RESULTS", literature_script)
                self.assertIn("INDEX_RETRY HTTP_", literature_script)
                self.assertIn("exc.code not in {429, 502, 503, 504}", literature_script)

                help_query = subprocess.run(
                    [
                        "bash",
                        str(workspace / literature_tool["script"]),
                        "--help",
                    ],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                self.assertEqual(help_query.returncode, 0, help_query.stderr)
                self.assertIn(
                    "Usage: literature-search.sh QUERY [FROM_DATE] [TO_DATE] [LIMIT]",
                    help_query.stdout,
                )
                self.assertNotIn("SEARCH_QUERY", help_query.stdout)

                dry_env = {**os.environ, "LITERATURE_SEARCH_DRY_RUN": "1"}
                unicode_query = subprocess.run(
                    [
                        "bash",
                        str(workspace / literature_tool["script"]),
                        "Erdős Problem #684†",
                        "1900-01-01",
                        "2026-08-21",
                        "10",
                    ],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                    env=dry_env,
                )
                self.assertEqual(unicode_query.returncode, 0, unicode_query.stderr)
                self.assertIn("ARXIV_NORMALIZED_TEXT Erdos Problem #684", unicode_query.stdout)
                self.assertIn(
                    "ARXIV_PARSED_QUERY all:Erdos AND all:Problem AND all:684",
                    unicode_query.stdout,
                )
                self.assertNotIn("all%3AErd+AND+all%3As", unicode_query.stdout)

                identifier_query = subprocess.run(
                    [
                        "bash",
                        str(workspace / literature_tool["script"]),
                        "arXiv:2603.29961v2",
                        "2026-01-01",
                        "2026-08-21",
                        "10",
                    ],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                    env=dry_env,
                )
                self.assertEqual(identifier_query.returncode, 0, identifier_query.stderr)
                self.assertIn("ARXIV_PARSED_QUERY id:2603.29961v2", identifier_query.stdout)
                self.assertIn("id_list=2603.29961v2", identifier_query.stdout)
                self.assertNotIn("search_query=id%3A2603.29961", identifier_query.stdout)

                phrase_query = subprocess.run(
                    [
                        "bash",
                        str(workspace / literature_tool["script"]),
                        '"Infinite Sidon-type sets" "zero-sum linear forms"',
                        "2024-01-01",
                        "2026-08-21",
                        "10",
                    ],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                    env=dry_env,
                )
                self.assertEqual(phrase_query.returncode, 0, phrase_query.stderr)
                self.assertIn(
                    'ARXIV_PARSED_QUERY all:"Infinite Sidon-type sets" '
                    'AND all:"zero-sum linear forms"',
                    phrase_query.stdout,
                )
                self.assertIn(
                    "search_query=all%3A%22Infinite+Sidon-type+sets%22",
                    phrase_query.stdout,
                )

                math_phrase_query = subprocess.run(
                    [
                        "bash",
                        str(workspace / literature_tool["script"]),
                        '"density of A+A"',
                        "1900-01-01",
                        "2026-08-21",
                        "10",
                    ],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                    env=dry_env,
                )
                self.assertEqual(
                    math_phrase_query.returncode, 0, math_phrase_query.stderr
                )
                self.assertIn(
                    'ARXIV_PARSED_QUERY all:"density of A+A"',
                    math_phrase_query.stdout,
                )
                self.assertIn(
                    "search_query=all%3A%22density+of+A%2BA%22",
                    math_phrase_query.stdout,
                )


                pdf_tool = next(
                    item for item in manifest if item["name"] == "pdf-text-extract"
                )
                self.assertEqual(pdf_tool["input_schema"]["required"], ["path"])
                pdf_script = workspace / pdf_tool["script"]
                escaped_pdf = subprocess.run(
                    ["bash", str(pdf_script), "../outside.pdf"],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                self.assertEqual(escaped_pdf.returncode, 2)
                self.assertIn("path must stay inside", escaped_pdf.stderr)

                saved = library.upsert_item(
                    {"type": "skill", "name": "new-method", "content": "# New method\n\nUse carefully."}
                )
                self.assertEqual(saved["id"], "agent_skill:new-method")
                self.assertTrue((root / "hermes" / "skills" / "new-method" / "SKILL.md").is_file())

        console = (
            Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("ag-skill-add", console)
        self.assertIn("Skill packs live under Skills", console)


class HumanInterventionRegressionTests(unittest.TestCase):
    def test_improof_continuation_stays_on_selected_native_harness(self) -> None:
        captured: dict = {}

        def fake_run(problem_path, **kwargs):
            captured["problem_path"] = Path(problem_path)
            captured.update(kwargs)
            self.assertTrue(Path(problem_path).is_file())
            self.assertIn("HUMAN FEEDBACK", Path(problem_path).read_text())
            return {"status": "finished", "workflow": "codex_author_critic"}

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(improof_runner, "run_problem", side_effect=fake_run),
                patch.object(jobs, "_register_proc") as register,
                patch.object(jobs, "_unregister_proc") as unregister,
                patch.object(
                    jobs,
                    "_wrap_subprocess_result",
                    return_value={"status": "finished", "engine": "improof"},
                ) as wrap,
            ):
                result = jobs._run_improof_continuation(
                    run_id="improof_native_continue",
                    job_id="follow-1",
                    problem_id="195",
                    problem_text="HUMAN FEEDBACK: authenticate the bound.",
                    display_problem_text="Erdos problem 195.",
                    model="gpt-test",
                    started=10.0,
                    workspace=workspace,
                    extra_env={"AGENT_MONITOR_CODEX_SUBSCRIPTION": "1"},
                )

            self.assertEqual(result["engine"], "improof")
            self.assertEqual(captured["output_dir"], workspace)
            self.assertEqual(captured["research_model"], "gpt-test")
            self.assertEqual(
                captured["extra_args"],
                [
                    "--component", "cfg_codex_author.model=gpt-test",
                    "--component", "cfg_codex_critic.model=gpt-test",
                ],
            )
            self.assertFalse(captured["problem_path"].exists())
            unregister.assert_called_once_with("improof_native_continue")
            register.assert_not_called()
            wrap.assert_called_once()
            self.assertEqual(wrap.call_args.kwargs["engine"], "improof")

    def test_plain_continuation_ack_does_not_replace_existing_proof(self) -> None:
        prompt = (
            "CURRENT proof.md (preserve this content, then extend it):\n"
            "# Proof\n\nBy reflexivity, $0=0$.\n\n"
            "HUMAN FEEDBACK \u2014 address this now:\n"
            "Recheck it; if correct, preserve proof.md exactly and simply report "
            "that it was received."
        )
        original = "# Proof\n\nBy reflexivity, $0=0$.\n"
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp) / "proof.md"
            proof.write_text(original, encoding="utf-8")
            old_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                changed = save_plain_result(
                    prompt,
                    "## Solved\n\nContinuation received; the existing proof is "
                    "correct and left unchanged.",
                )
            finally:
                os.chdir(old_cwd)

            self.assertFalse(changed)
            self.assertEqual(proof.read_text(encoding="utf-8"), original)

    def test_plain_continuation_still_writes_a_real_revision(self) -> None:
        prompt = (
            "HUMAN FEEDBACK \u2014 address this now:\n"
            "Correct the missing boundary case; do not discard valid lemmas."
        )
        revision = "# Proof\n\nThe repaired argument handles the boundary case.\n"
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp) / "proof.md"
            proof.write_text("# Draft\n", encoding="utf-8")
            old_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                changed = save_plain_result(prompt, revision)
            finally:
                os.chdir(old_cwd)

            self.assertTrue(changed)
            self.assertEqual(proof.read_text(encoding="utf-8"), revision)

    def test_cli_continuation_passes_canonical_problem_text_to_run_record(self) -> None:
        captured: dict = {}
        record = {
            "run_id": "plain_continue",
            "engine": "plain",
            "problem_id": "continue",
            "problem_text": "Canonical theorem.",
            "problems": [
                {"text": "Canonical theorem.", "source": "initial"},
                {"text": "Add the boundary case.", "source": "human"},
            ],
            "use_subagents": False,
        }

        def fake_cli(**kwargs):
            captured.update(kwargs)
            return {
                "run_id": "plain_continue",
                "status": "finished",
                "agents": [{"output": "Updated."}],
            }

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "proof.md").write_text(
                "# Partial Progress\n\nA complete continuation artifact for derived views.\n",
                encoding="utf-8",
            )
            with (
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_write_session_problems"),
                patch.object(jobs, "_write_run"),
                patch.object(jobs, "_run_cli_engine", side_effect=fake_cli),
                patch.object(jobs, "_update_job"),
                patch.object(jobs, "_append_chat"),
                patch.object(jobs, "_launch_pending_feedback"),
                patch("agent_monitor.auto_pipeline.start") as derived,
            ):
                jobs._execute_continue(
                    job_id="job",
                    run_id="plain_continue",
                    engine="plain",
                    message="Add the boundary case.",
                    problem_text="Canonical theorem.",
                    model="gpt-test",
                    max_iterations=2,
                    workspace=tmp,
                )

        self.assertEqual(captured["display_problem_text"], "Canonical theorem.")
        self.assertEqual(captured["max_iterations"], 2)
        self.assertIn("HUMAN FEEDBACK", captured["problem_text"])
        self.assertIn("Add the boundary case.", captured["problem_text"])
        derived.assert_called_once_with(
            run_id="plain_continue",
            workspace=Path(tmp),
            owner_id=None,
            model="gpt-test",
            engine="plain",
        )

    def test_continuation_merge_preserves_prior_monitor_and_usage(self) -> None:
        previous = {
            "run_id": "plain_lineage",
            "job_id": "initial-job",
            "status": "failed",
            "created_at": "2026-08-22T00:00:00Z",
            "updated_at": "2026-08-22T00:02:00Z",
            "agents": [
                {"trace_id": "plain_lineage::turn-1", "round_id": 1},
                {"trace_id": "plain_lineage::turn-2", "round_id": 2},
            ],
            "edges": [
                {"from": "plain_lineage::turn-1", "to": "plain_lineage::turn-2"}
            ],
            "totals": {
                "agents": 2,
                "input_tokens": 100,
                "output_tokens": 20,
                "cost_usd": 1.5,
                "latency_s": 10.0,
            },
        }
        current = {
            "run_id": "plain_lineage",
            "status": "finished",
            "updated_at": "2026-08-22T00:05:00Z",
            "agents": [
                {"trace_id": "plain_lineage::turn-1", "round_id": 1},
                {"trace_id": "plain_lineage::turn-2", "round_id": 2},
            ],
            "edges": [
                {"from": "plain_lineage::turn-1", "to": "plain_lineage::turn-2"}
            ],
            "totals": {
                "agents": 2,
                "input_tokens": 40,
                "output_tokens": 10,
                "cost_usd": 0.5,
                "latency_s": 4.0,
            },
        }

        merged = jobs._merge_continuation_run(
            previous, current, job_id="follow-job"
        )

        trace_ids = [agent["trace_id"] for agent in merged["agents"]]
        self.assertEqual(len(trace_ids), 4)
        self.assertEqual(len(set(trace_ids)), 4)
        self.assertEqual(
            [agent["round_id"] for agent in merged["agents"]], [1, 2, 3, 4]
        )
        self.assertEqual(merged["totals"]["agents"], 4)
        self.assertEqual(merged["totals"]["input_tokens"], 140)
        self.assertEqual(merged["totals"]["output_tokens"], 30)
        self.assertEqual(merged["totals"]["cost_usd"], 2.0)
        self.assertEqual(merged["totals"]["latency_s"], 14.0)
        self.assertEqual(merged["continuation_count"], 1)
        self.assertEqual(
            [item["kind"] for item in merged["session_lineage"]],
            ["initial", "continue"],
        )
        self.assertEqual(merged["created_at"], previous["created_at"])
        self.assertIn(
            {
                "from": "plain_lineage::turn-2",
                "to": "plain_lineage::turn-1::continue-follow-job",
                "type": "continue",
            },
            merged["edges"],
        )

    def test_continue_inherits_recorded_model_in_job_and_runner(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "workspaces" / "plain_model").mkdir(parents=True)
            record = {
                "run_id": "plain_model",
                "engine": "plain",
                "owner_id": 61,
                "problem_id": "model",
                "problem_text": "Prove 2 = 2.",
                "problems": [{"text": "Prove 2 = 2.", "source": "initial"}],
                "live": {"model": "gpt-recorded"},
            }
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_append_chat"),
                patch.object(jobs, "_user_extra_env", return_value={}),
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                result = jobs.continue_run(
                    "plain_model", message="Check the boundary.", user={"id": 61}
                )

        self.assertEqual(captured["model"], "gpt-recorded")
        with jobs._LOCK:
            self.assertEqual(jobs._JOBS[result["job_id"]]["model"], "gpt-recorded")
            jobs._JOBS.pop(result["job_id"], None)

    def test_continuation_recovers_claude_alias_from_requested_or_legacy_model(self) -> None:
        self.assertEqual(
            jobs._continuation_model(
                {
                    "auth_route": "claude_subscription",
                    "requested_model": "fable",
                    "live": {"model": "claude-fable-5"},
                },
                None,
            ),
            "fable",
        )
        self.assertEqual(
            jobs._continuation_model(
                {
                    "auth_route": "claude_subscription",
                    "live": {"model": "gpt-5.6-sol"},
                    "agents": [{"model": "claude-opus-5"}],
                },
                None,
            ),
            "opus",
        )
        self.assertEqual(
            jobs._continuation_model(
                {
                    "auth_route": "claude_subscription",
                    "live": {"model": "claude-haiku-4-5-20251001"},
                },
                None,
            ),
            "claude-haiku-4-5",
        )

    def test_continuation_rejects_unknown_stored_claude_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported Claude account model"):
            jobs._continuation_model(
                {
                    "auth_route": "claude_subscription",
                    "live": {"model": "claude-unknown-9"},
                },
                None,
            )

    def test_continue_preserves_disabled_subagent_setting(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "workspaces" / "deepagents_toggle").mkdir(parents=True)
            record = {
                "run_id": "deepagents_toggle",
                "engine": "deepagents",
                "owner_id": 61,
                "problem_id": "toggle",
                "problem_text": "Prove 2 = 2.",
                "problems": [{"text": "Prove 2 = 2.", "source": "initial"}],
                "use_subagents": False,
                "subagent_model": "must-not-leak",
            }
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_append_chat"),
                patch.object(jobs, "_user_extra_env", return_value={}),
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                result = jobs.continue_run(
                    "deepagents_toggle", message="Continue directly.", user={"id": 61}
                )

        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_USE_SUBAGENTS"], "0")
        self.assertNotIn("AGENT_MONITOR_SUBAGENT_MODEL", captured["extra_env"])
        with jobs._LOCK:
            jobs._JOBS.pop(result["job_id"], None)

    def test_continue_restores_the_run_owners_codex_subscription(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "workspaces" / "plain_smoke").mkdir(parents=True)
            record = {
                "run_id": "plain_smoke",
                "engine": "plain",
                "owner_id": 61,
                "problem_id": "smoke",
                "problem_text": "Prove 0 = 0.",
                "problems": [{"text": "Prove 0 = 0.", "source": "initial"}],
            }
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_append_chat"),
                patch.object(jobs, "_user_extra_env", return_value={}),
                patch("agent_monitor.codex_login.account_home", return_value=Path("/tmp/codex-owner")),
                patch("agent_monitor.codex_login.account_login_ready", return_value=True),
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                result = jobs.continue_run(
                    "plain_smoke", message="Make it more explicit.", user={"id": 61}
                )

        self.assertEqual(captured["owner_id"], 61)
        self.assertEqual(captured["extra_env"]["CODEX_HOME"], "/tmp/codex-owner")
        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_CODEX_SUBSCRIPTION"], "1")
        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_AUTH_MODE"], "chatgpt_subscription")
        with jobs._LOCK:
            jobs._JOBS.pop(result["job_id"], None)

    def test_continue_uses_api_mode_when_codex_is_connected_but_disabled(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "workspaces" / "plain_api").mkdir(parents=True)
            record = {
                "run_id": "plain_api",
                "engine": "plain",
                "owner_id": 61,
                "problem_id": "api",
                "problem_text": "Prove 1 = 1.",
                "problems": [{"text": "Prove 1 = 1.", "source": "initial"}],
            }
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_append_chat"),
                patch.object(
                    jobs,
                    "_user_extra_env",
                    return_value={
                        "AGENT_MONITOR_USE_CODEX": "0",
                        "ANTHROPIC_API_KEY": "test-key",
                    },
                ),
                patch("agent_monitor.codex_login.account_login_ready", return_value=True) as ready,
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                result = jobs.continue_run(
                    "plain_api", message="Use the API model.", user={"id": 61}
                )

        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_AUTH_MODE"], "api_key")
        self.assertNotIn("AGENT_MONITOR_CODEX_SUBSCRIPTION", captured["extra_env"])
        self.assertNotIn("CODEX_HOME", captured["extra_env"])
        ready.assert_not_called()
        with jobs._LOCK:
            jobs._JOBS.pop(result["job_id"], None)

    def test_feedback_queued_during_a_run_is_applied_once_in_order(self) -> None:
        run_id = "plain_queue_smoke"
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            workspace = runs / "workspaces" / run_id
            workspace.mkdir(parents=True)
            jobs._stop_event(run_id).clear()
            with patch.object(jobs, "RUNS_DIR", runs):
                jobs._queue_human_feedback(workspace, "First correction")
                jobs._queue_human_feedback(workspace, "Second correction")
                with (
                    patch.object(jobs, "_run_is_active", return_value=None),
                    patch.object(jobs, "_append_chat"),
                    patch.object(jobs, "continue_run", return_value={"ok": True}) as resume,
                ):
                    result = jobs._launch_pending_feedback(
                        run_id, owner_id=61, model="gpt-test", max_iterations=7
                    )

            self.assertEqual(result, {"ok": True})
            resume.assert_called_once_with(
                run_id,
                message="First correction\n\nSecond correction",
                model="gpt-test",
                max_iterations=7,
                user={"id": 61},
                problems_recorded=True,
            )
            self.assertFalse((workspace / jobs._PENDING_FEEDBACK_FILENAME).exists())

    def test_active_feedback_stays_out_of_problem_file_until_continuation(self) -> None:
        run_id = "plain_queue_visibility"
        initial = "Prove that n + 0 = n for every natural number n."
        feedback = "Add a separate check of the n = 0 case."
        record = {
            "run_id": run_id,
            "engine": "plain",
            "owner_id": 61,
            "problem_text": initial,
            "problems": [{"text": initial, "source": "initial"}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            workspace = runs / "workspaces" / run_id
            workspace.mkdir(parents=True)
            (workspace / "problem.txt").write_text(initial + "\n", encoding="utf-8")
            active = {"job_id": "initial-job", "status": "running"}
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_write_run"),
                patch.object(jobs, "_append_chat"),
                patch.object(jobs, "_run_is_active", return_value=active),
            ):
                result = jobs.send_human_message(
                    run_id, feedback, model="claude-haiku-4-5", user={"id": 61}
                )

            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["job_id"], "initial-job")
            self.assertEqual(
                (workspace / "problem.txt").read_text(encoding="utf-8"),
                initial + "\n",
            )
            self.assertFalse((workspace / "problems.json").exists())
            self.assertEqual(jobs._drain_human_feedback(workspace), [feedback])
            self.assertEqual(record["problems"][-1]["text"], feedback)
            self.assertEqual(record["problems"][-1]["source"], "human")

            jobs._write_session_problems(workspace, record["problems"])
            continuation_prompt = (workspace / "problem.txt").read_text(encoding="utf-8")
            self.assertIn(initial, continuation_prompt)
            self.assertIn(feedback, continuation_prompt)

    def test_queued_feedback_does_not_create_a_combined_duplicate_problem(self) -> None:
        captured: dict = {}
        record = {
            "run_id": "plain_queue_prompt",
            "engine": "plain",
            "problem_id": "queue",
            "problem_text": "Canonical theorem.",
            "problems": [
                {"text": "Canonical theorem.", "source": "initial"},
                {"text": "First correction", "source": "human"},
                {"text": "Second correction", "source": "human"},
            ],
        }

        def fake_cli(**kwargs):
            captured.update(kwargs)
            return {
                "run_id": "plain_queue_prompt",
                "status": "finished",
                "agents": [{"output": "Updated."}],
            }

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "proof.md").write_text("# Partial Progress\n", encoding="utf-8")
            with (
                patch.object(jobs, "_load_run_record", return_value=record),
                patch.object(jobs, "_write_session_problems"),
                patch.object(jobs, "_write_run"),
                patch.object(jobs, "_run_cli_engine", side_effect=fake_cli),
                patch.object(jobs, "_update_job"),
                patch.object(jobs, "_append_chat"),
                patch.object(jobs, "_launch_pending_feedback"),
                patch("agent_monitor.auto_pipeline.start"),
            ):
                jobs._execute_continue(
                    job_id="job",
                    run_id="plain_queue_prompt",
                    engine="plain",
                    message="First correction\n\nSecond correction",
                    problem_text="Canonical theorem.",
                    model="gpt-test",
                    max_iterations=2,
                    workspace=tmp,
                    problems_recorded=True,
                )

        self.assertEqual(len(record["problems"]), 3)
        prompt = captured["problem_text"]
        self.assertIn("2. First correction", prompt)
        self.assertIn("3. Second correction", prompt)
        self.assertNotIn("4. First correction", prompt)

    def test_feedback_is_requeued_if_follow_up_cannot_start(self) -> None:
        run_id = "plain_queue_retry"
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            workspace = runs / "workspaces" / run_id
            workspace.mkdir(parents=True)
            jobs._stop_event(run_id).clear()
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "_run_is_active", return_value=None),
                patch.object(jobs, "_append_chat"),
                patch.object(jobs, "continue_run", side_effect=RuntimeError("busy")),
            ):
                jobs._queue_human_feedback(workspace, "Please retry")
                result = jobs._launch_pending_feedback(
                    run_id, owner_id=61, model=None, max_iterations=5
                )

            self.assertIsNone(result)
            self.assertEqual(jobs._drain_human_feedback(workspace), ["Please retry"])


class AuthenticationModeSettingsTests(unittest.TestCase):
    def test_newly_connected_codex_account_has_models_before_cache_exists(self) -> None:
        from agent_monitor import codex_login

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "new-account"
            home.mkdir()
            with (
                patch.object(codex_login, "account_home", return_value=home),
                patch.object(codex_login, "account_login_ready", return_value=True),
            ):
                models = codex_login.available_models(63)

        self.assertEqual(models, list(codex_login.DEFAULT_CODEX_MODELS))
        html = (console_server.WEB_DIR / "console.html").read_text(encoding="utf-8")
        self.assertIn("async function applyCodexStatus(st)", html)
        self.assertIn("if(needsModels)await loadSettings()", html)

    def test_fresh_seed_includes_validated_skill_packages(self) -> None:
        from agent_monitor.skill_seeds import (
            STARTER_SKILL_REFERENCES,
            STARTER_SKILLS,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            with patch.object(agent_config, "HERMES_HOME", home):
                agent_config.ensure_seeded()

            for name in ("proof-strategies", "literature-search"):
                package = home / "skills" / name
                self.assertEqual(
                    (package / "SKILL.md").read_text(encoding="utf-8"),
                    STARTER_SKILLS[name],
                )
                for filename, content in STARTER_SKILL_REFERENCES[name].items():
                    self.assertEqual(
                        (package / "references" / filename).read_text(encoding="utf-8"),
                        content,
                    )

    def test_hitl_feedback_is_not_used_as_sandbox_problem_or_session_title(self) -> None:
        problems = [
            {"text": "Original theorem", "source": "initial"},
            {"text": "Correct the boundary case", "source": "human"},
        ]
        self.assertEqual(jobs._session_title(problems), "Original theorem")
        html = (console_server.WEB_DIR / "console.html").read_text(encoding="utf-8")
        self.assertIn("src==='human'||src==='hitl'||src==='feedback'", html)

    def test_missing_hitl_chat_events_are_recovered_from_session_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            cache = root / "cache"
            workspace = runs / "workspaces" / "review-run"
            workspace.mkdir(parents=True)
            existing = {"role": "user", "content": "First correction", "ts": "t1"}
            (workspace / "human_chat.jsonl").write_text(
                json.dumps(existing) + "\n", encoding="utf-8"
            )
            record = {
                "run_id": "review-run",
                "updated_at": "t3",
                "problems": [
                    {"text": "Original theorem", "source": "initial"},
                    {"text": "First correction", "source": "human", "ts": "t1"},
                    {"text": "Second correction", "source": "feedback", "ts": "t2"},
                ],
            }
            runs.mkdir(exist_ok=True)
            (runs / "review-run.json").write_text(json.dumps(record), encoding="utf-8")
            with (
                patch.object(jobs, "RUNS_DIR", runs),
                patch.object(jobs, "CACHE_DIR", cache),
            ):
                chat = jobs.list_chat("review-run")

            self.assertEqual(
                [message["content"] for message in chat],
                ["First correction", "Second correction"],
            )
            self.assertTrue(chat[-1]["recovered_from_session"])
            self.assertEqual(
                len(
                    (workspace / "human_chat.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
                1,
            )

    def test_workspace_target_rejects_prefix_collision_and_external_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "run"
            sibling = root / "run_evil"
            workspace.mkdir()
            sibling.mkdir()
            (workspace / "inside.md").write_text("inside", encoding="utf-8")
            (sibling / "secret.md").write_text("outside", encoding="utf-8")

            self.assertEqual(
                console_server._workspace_target(workspace, "inside.md"),
                (workspace / "inside.md").resolve(),
            )
            self.assertIsNone(
                console_server._workspace_target(workspace, "../run_evil/secret.md")
            )
            link = workspace / "outside-link.md"
            link.symlink_to(sibling / "secret.md")
            self.assertIsNone(console_server._workspace_target(workspace, link.name))
            self.assertNotIn(
                link.name,
                {item["path"] for item in console_server._list_workspace_files(workspace)},
            )
            self.assertIsNone(console_server._compile_workspace_pdf(workspace, "."))
            with (
                patch.object(console_server, "WEB_DIR", workspace),
                patch.object(console_server, "DASHBOARD_WEB", workspace),
            ):
                self.assertEqual(
                    console_server._static_asset("inside.md"),
                    (workspace / "inside.md").resolve(),
                )
                self.assertIsNone(console_server._static_asset("../run_evil/secret.md"))
                self.assertIsNone(console_server._static_asset(link.name))

    def test_response_headers_block_framing_and_sniffing(self) -> None:
        handler = object.__new__(console_server.Handler)
        headers = handler._merge_headers(None)

        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", headers["Content-Security-Policy"])
        self.assertIn("base-uri 'self'", headers["Content-Security-Policy"])
        self.assertIn("object-src 'none'", headers["Content-Security-Policy"])

    def test_call_detail_rejects_foreign_trace_before_lookup(self) -> None:
        handler = object.__new__(console_server.Handler)
        sent: dict = {}
        handler._owns_run = lambda run_id, user: False
        handler._send = lambda code, body, *args, **kwargs: sent.update(
            code=code, body=json.loads(body)
        )
        with patch("harness_dashboard.server._call_detail") as lookup:
            handler._proxy_dashboard_get(
                "/api/call_detail",
                {"trace_id": ["foreign-run::turn-1"], "round": ["1"]},
                {"id": 7, "is_admin": False},
            )

        self.assertEqual(sent["code"], 404)
        self.assertEqual(sent["body"]["error"], "call not found")
        lookup.assert_not_called()

    def test_admin_env_write_enforces_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("OPENAI_API_KEY=old\n", encoding="utf-8")
            env_path.chmod(0o644)
            with patch.object(settings, "ENV_PATH", env_path):
                settings._write_env_file({"OPENAI_API_KEY": "replacement"})

            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            self.assertIn(
                "OPENAI_API_KEY=replacement", env_path.read_text(encoding="utf-8")
            )

    def test_auth_database_creation_enforces_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "users.db"
            db_path.touch(mode=0o644)
            with (
                patch.object(auth, "DB_PATH", db_path),
                patch.object(auth, "_INITIALIZED", False),
            ):
                conn = auth._conn()
                conn.close()

            self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)

    def test_turning_codex_off_lists_only_configured_api_provider_models(self) -> None:
        env = {
            "AGENT_MONITOR_USE_CODEX": "0",
            "ANTHROPIC_API_KEY": "test-anthropic-key-000000000",
        }
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch("agent_monitor.codex_login.account_login_ready", return_value=True),
            patch("agent_monitor.codex_login.available_models", return_value=["gpt-codex-test"]),
        ):
            result = settings.get_settings({"id": 7, "is_admin": False})

        self.assertFalse(result["codex_enabled"])
        self.assertTrue(result["codex_connected"])
        self.assertEqual(result["codex_models"], ["gpt-codex-test"])
        self.assertEqual(result["models_by_runtime"]["codex"], ["gpt-codex-test"])
        self.assertTrue(result["api_models"])
        self.assertTrue(all(model.startswith("claude-") for model in result["api_models"]))
        self.assertEqual(result["model_presets"], result["api_models"])
        self.assertEqual(result["auth_mode"], "api_key")

    def test_new_run_uses_api_mode_and_accepts_api_model_when_codex_is_off(self) -> None:
        captured: dict = {}

        class FakeThread:
            def __init__(self, *, kwargs, **_):
                captured.update(kwargs)

            def start(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            with (
                patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
                patch(
                    "agent_monitor.engines_registry.list_engines",
                    return_value=[{
                        "id": "plain",
                        "label": "Plain",
                        "available": True,
                        "auth_modes": ["codex_subscription", "api_key"],
                    }],
                ),
                patch("agent_monitor.engines_registry.supported_models", return_value=["gpt-codex-only"]),
                patch.object(jobs, "_user_extra_env", return_value={
                    "AGENT_MONITOR_USE_CODEX": "0",
                    "ANTHROPIC_API_KEY": "test-key",
                }),
                patch.object(jobs, "workspace_dir", return_value=workspace),
                patch.object(jobs, "_write_run"),
                patch("agent_monitor.codex_login.account_login_ready", return_value=True) as ready,
                patch("agent_monitor.jobs.threading.Thread", FakeThread),
            ):
                job = jobs.start_job(
                    engine="plain",
                    problem_text="Prove 2 = 2.",
                    model="claude-test-model",
                    user={"id": 7, "email": "test@example.com"},
                )

        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_AUTH_MODE"], "api_key")
        self.assertNotIn("AGENT_MONITOR_CODEX_SUBSCRIPTION", captured["extra_env"])
        self.assertEqual(captured["model"], "claude-test-model")
        ready.assert_not_called()
        with jobs._LOCK:
            jobs._JOBS.pop(job["job_id"], None)

    def test_selected_kimi_does_not_override_codex_runtime(self) -> None:
        with (
                patch("agent_monitor.engines_registry.all_engine_ids", return_value={"codex"}),
                patch(
                    "agent_monitor.engines_registry.list_engines",
                    return_value=[{
                        "id": "codex",
                        "label": "Codex CLI",
                        "available": True,
                        "auth_modes": ["codex_subscription", "api_key"],
                    }],
                ),
                patch("agent_monitor.engines_registry.supported_models", return_value=["gpt-codex-only"]),
                patch.object(jobs, "_user_extra_env", return_value={
                    "AGENT_MONITOR_ACCOUNT_RUNTIME": "codex",
                    "AGENT_MONITOR_USE_CODEX": "1",
                    "KIMI_API_KEY": "test-kimi-key",
                    "KIMI_API_BASE": "https://kimi.example/v1",
                }),
                patch.object(jobs, "_configure_auth_route") as configure,
                patch("agent_monitor.jobs.threading.Thread") as thread,
            ):
            with self.assertRaisesRegex(ValueError, "Kimi K3.*cannot use"):
                jobs.start_job(
                    engine="codex",
                    problem_text="Prove 2 = 2.",
                    model="kimi-k3",
                    user={"id": 7, "email": "test@example.com"},
                )
        configure.assert_not_called()
        thread.assert_not_called()

    def test_codex_toggle_is_saved_per_user(self) -> None:
        with (
            patch("agent_monitor.auth.set_user_env") as set_env,
            patch.object(settings, "get_settings", return_value={"codex_enabled": False}),
        ):
            result = settings.save_settings({"codex_enabled": False}, user={"id": 9})

        set_env.assert_called_once_with(
            9,
            {
                "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
                "AGENT_MONITOR_USE_CODEX": "0",
                "AGENT_MONITOR_USE_CLAUDE": "0",
            },
            clear=[],
        )
        self.assertFalse(result["settings"]["codex_enabled"])

    def test_lean_codex_fallback_respects_the_off_switch(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("agent_monitor.settings.codex_enabled", return_value=False),
        ):
            with self.assertRaisesRegex(ValueError, "Codex is turned off"):
                lean_verify._codex_subscription_interaction(
                    workspace=Path(tmp),
                    user={"id": 7},
                    prompt="Explain this proof.",
                    lean_source="theorem main : True := by trivial",
                    edit=False,
                )


class MonitorOverviewTests(unittest.TestCase):
    def test_overview_only_aggregates_owned_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "harness"
            cache.mkdir(parents=True)
            owned = {
                "run_id": "owned",
                "owner_id": 7,
                "engine": "hermes",
                "status": "finished",
                "problem_id": "demo",
                "totals": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01, "latency_s": 2},
            }
            other = {
                "run_id": "other",
                "owner_id": 8,
                "engine": "codex",
                "status": "failed",
                "totals": {"input_tokens": 900, "output_tokens": 100, "cost_usd": 9, "latency_s": 20},
            }
            (cache / "manifest.json").write_text(
                json.dumps({"runs": [{"run_id": "owned"}, {"run_id": "other"}]}), encoding="utf-8"
            )
            (cache / "owned.json").write_text(json.dumps(owned), encoding="utf-8")
            (cache / "other.json").write_text(json.dumps(other), encoding="utf-8")
            library = {
                "items": [
                    {"type": "memory", "enabled": True},
                    {"type": "skill", "enabled": False},
                    {"type": "tool", "enabled": True},
                ],
                "counts": {"memory": 1, "skill": 1, "tool": 1},
                "settings": {"auto_memory": True},
            }
            agent = {
                "memory_enabled": True,
                "memory": [{"name": "MEMORY.md"}],
                "skills": [{"name": "proof", "enabled": True}],
                "system_prompt": "careful prover",
            }
            with (
                patch.object(monitor_overview, "CACHE_DIR", Path(tmp)),
                patch("agent_monitor.library.get_library", return_value=library),
                patch("agent_monitor.agent_config.get_agent_config", return_value=agent),
                patch("agent_monitor.settings.get_settings", return_value={"configured_providers": ["openai"]}),
                patch("agent_monitor.codex_login.status", return_value={"connected": True}),
                patch("agent_monitor.engines_registry.list_engines", return_value=[]),
            ):
                result = monitor_overview.build_overview({"id": 7, "is_admin": False})

        self.assertEqual(result["runs"]["total"], 1)
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertEqual(result["usage"]["cost_usd"], 0.01)
        self.assertEqual(result["engines"][0]["engine"], "hermes")
        self.assertEqual(result["library"]["enabled"], {"memory": 1, "skill": 0, "tool": 1})
        self.assertTrue(result["auth"]["codex_connected"])


class AutoPipelineAndSandboxRegressionTests(unittest.TestCase):
    def test_failed_run_output_is_not_used_as_a_proof_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            output = "analysis trace " * 30
            failed = {"status": "failed", "agents": [{"output": output}]}
            finished = {"status": "finished", "agents": [{"output": output}]}

            self.assertEqual(auto_pipeline._source_signature(workspace, failed), "")
            self.assertTrue(
                auto_pipeline._source_signature(workspace, finished).startswith("run:")
            )

    def test_terminal_run_reconciles_an_orphaned_running_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            runs_dir = root / "runs"
            workspace = runs_dir / "workspaces" / "finished-run"
            workspace.mkdir(parents=True)
            run = {
                "run_id": "finished-run",
                "engine": "codex",
                "status": "finished",
                "updated_at": "2026-08-21T00:01:00+00:00",
                "workspace": str(workspace),
                "pipeline": [],
                "agents": [],
                "edges": [],
                "totals": {},
            }
            with (
                patch.object(jobs, "CACHE_DIR", cache_dir),
                patch.object(jobs, "RUNS_DIR", runs_dir),
                patch.object(jobs, "_run_is_active", return_value=None),
            ):
                jobs._write_run(run)
                auto_pipeline._write_state(
                    workspace,
                    {
                        "status": "running",
                        "stages": {
                            "informal_dag": {"status": "done"},
                            "lean": {"status": "running"},
                            "formal_dag": {"status": "waiting"},
                        },
                    },
                )
                result = jobs.reconcile_run_status("finished-run", min_age_seconds=0)
                pipeline = auto_pipeline.load_state(workspace)

        self.assertEqual(result["status"], "finished")
        self.assertEqual(pipeline["status"], "partial")
        self.assertEqual(pipeline["stages"]["lean"]["status"], "skipped")
        self.assertEqual(pipeline["stages"]["formal_dag"]["status"], "skipped")

    def test_global_reconciliation_discovers_terminal_run_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            runs_dir = root / "runs"
            workspace = runs_dir / "workspaces" / "finished-sidecar"
            workspace.mkdir(parents=True)
            run = {
                "run_id": "finished-sidecar",
                "engine": "plain",
                "status": "finished",
                "updated_at": "2026-08-21T00:01:00+00:00",
                "workspace": str(workspace),
                "pipeline": [],
                "agents": [],
                "edges": [],
                "totals": {},
            }
            with (
                patch.object(jobs, "CACHE_DIR", cache_dir),
                patch.object(jobs, "RUNS_DIR", runs_dir),
                patch.object(jobs, "_run_is_active", return_value=None),
            ):
                jobs._write_run(run)
                auto_pipeline._write_state(
                    workspace,
                    {
                        "status": "running",
                        "stages": {
                            "informal_dag": {"status": "done"},
                            "lean": {"status": "running"},
                            "formal_dag": {"status": "waiting"},
                        },
                    },
                )
                jobs.reconcile_stale_running_runs(min_age_seconds=0)
                pipeline = auto_pipeline.load_state(workspace)

        self.assertEqual(pipeline["status"], "partial")
        self.assertEqual(pipeline["stages"]["lean"]["status"], "skipped")
        self.assertEqual(pipeline["stages"]["formal_dag"]["status"], "skipped")

    def test_orphaned_running_run_is_reconciled_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            runs_dir = root / "runs"
            workspace = runs_dir / "workspaces" / "stale-run"
            workspace.mkdir(parents=True)
            run = {
                "run_id": "stale-run",
                "engine": "metaharness",
                "problem_id": "p1",
                "status": "running",
                "created_at": "2026-08-21T00:00:00+00:00",
                "updated_at": "2026-08-21T00:01:00+00:00",
                "workspace": str(workspace),
                "pipeline": [],
                "agents": [{"status": "running", "output": "working"}],
                "edges": [],
                "totals": {},
            }
            with (
                patch.object(jobs, "CACHE_DIR", cache_dir),
                patch.object(jobs, "RUNS_DIR", runs_dir),
                patch.object(jobs, "_run_is_active", return_value=None),
            ):
                jobs._write_run(run)
                auto_pipeline._write_state(
                    workspace,
                    {
                        "status": "running",
                        "stages": {
                            "informal_dag": {"status": "done"},
                            "lean": {"status": "done"},
                            "formal_dag": {"status": "done"},
                            "coverage": {"status": "done"},
                        },
                    },
                )
                result = jobs.reconcile_run_status("stale-run", min_age_seconds=0)
                persisted = jobs._load_run_record("stale-run")
                pipeline = auto_pipeline.load_state(workspace)
                chat = jobs.list_chat("stale-run")

        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(persisted["status"], "interrupted")
        self.assertFalse(persisted["completed"])
        self.assertTrue(persisted["interruption"]["recoverable"])
        self.assertEqual(persisted["agents"][0]["status"], "interrupted")
        self.assertEqual(pipeline["status"], "done")
        self.assertIn("press Continue", chat[-1]["content"])

    def test_reconciliation_never_interrupts_a_live_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            runs_dir = root / "runs"
            runs_dir.mkdir(parents=True)
            run = {
                "run_id": "live-run",
                "engine": "hermes",
                "problem_id": "p1",
                "status": "running",
                "updated_at": "2026-08-21T00:01:00+00:00",
                "workspace": str(runs_dir / "workspaces" / "live-run"),
                "pipeline": [],
                "agents": [],
                "edges": [],
                "totals": {},
            }
            with (
                patch.object(jobs, "CACHE_DIR", cache_dir),
                patch.object(jobs, "RUNS_DIR", runs_dir),
            ):
                jobs._write_run(run)
                with patch.object(
                    jobs, "_run_is_active", return_value={"job_id": "job-live"}
                ):
                    result = jobs.reconcile_run_status(
                        "live-run", min_age_seconds=0
                    )
                persisted = jobs._load_run_record("live-run")

        self.assertEqual(result["status"], "running")
        self.assertEqual(persisted["status"], "running")

    def test_lean_empty_state_is_animated_and_dag_is_automatic_only(self) -> None:
        html = (
            Path(console_server.__file__).parent / "web" / "console.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="lean-progress-bar"', html)
        self.assertIn('id="lean-progress-pct"', html)
        self.assertIn("function renderLeanProgress(", html)
        self.assertIn('data-stage="coverage"', html)
        self.assertNotIn('id="dag-gen"', html)
        self.assertNotIn('id="dag-model"', html)
        self.assertNotIn("function generateDag(", html)
        self.assertIn("Proof graph is generated automatically", html)
        self.assertIn(".sdot.interrupted", html)
        self.assertIn("status==='interrupted'", html)

    def test_sandbox_save_detects_conflicts_and_requires_explicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code, first = console_server._save_sandbox_proof(
                workspace,
                content="# Proof\nFirst version.\n",
                expected_revision="missing",
            )
            self.assertEqual(code, 200)

            (workspace / "proof.md").write_text("# Proof\nEngine version.\n", encoding="utf-8")
            code, conflict = console_server._save_sandbox_proof(
                workspace,
                content="# Proof\nHuman version.\n",
                expected_revision=first["revision"],
            )
            self.assertEqual(code, 409)
            self.assertTrue(conflict["conflict"])
            self.assertIn("Engine version", (workspace / "proof.md").read_text())

            code, saved = console_server._save_sandbox_proof(
                workspace,
                content="# Proof\nHuman version.\n",
                expected_revision=first["revision"],
                force=True,
            )
            self.assertEqual(code, 200)
            self.assertNotEqual(saved["revision"], first["revision"])
            self.assertIn("Human version", (workspace / "proof.md").read_text())

    def test_parallel_batch_starts_informal_and_lean_workers_together(self) -> None:
        rendezvous = threading.Barrier(2)
        observed: list[str] = []

        def worker(name: str):
            def run(*_args) -> None:
                observed.append(name + ":started")
                rendezvous.wait(timeout=2)
                observed.append(name + ":finished")
            return run

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(auto_pipeline, "_informal_worker", side_effect=worker("informal")),
            patch.object(auto_pipeline, "_lean_and_formal_worker", side_effect=worker("lean")),
        ):
            auto_pipeline._run_batch(Path(tmp), {}, {"id": 7}, "gpt-test")

        self.assertIn("informal:started", observed[:2])
        self.assertIn("lean:started", observed[:2])
        self.assertEqual(set(observed[-2:]), {"informal:finished", "lean:finished"})

    def test_pipeline_stage_failures_are_isolated(self) -> None:
        def generate(*, workspace, kind, **_kwargs):
            if kind == "informal":
                raise ValueError("informal graph failed")
            return {
                "model": "test",
                "source": "Proof.lean",
                "graph": {"nodes": [{"id": "main"}], "edges": []},
            }

        def generate_lean(*, workspace, **_kwargs):
            lean_dir = workspace / "lean"
            lean_dir.mkdir()
            (lean_dir / "Proof.lean").write_text(
                "theorem main : True := by trivial\n", encoding="utf-8"
            )
            return {"status": "verified", "model": "test"}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(proof_graph, "generate", side_effect=generate),
            patch.object(lean_verify, "generate", side_effect=generate_lean),
        ):
            workspace = Path(tmp)
            auto_pipeline._write_state(
                workspace,
                {
                    "status": "running",
                    "stages": {
                        "informal_dag": {"status": "waiting"},
                        "lean": {"status": "waiting"},
                        "formal_dag": {"status": "waiting"},
                    },
                },
            )
            auto_pipeline._run_batch(workspace, {}, {"id": 7}, "gpt-test")
            state = auto_pipeline.load_state(workspace)

        self.assertEqual(state["stages"]["informal_dag"]["status"], "error")
        self.assertEqual(state["stages"]["lean"]["status"], "done")
        self.assertEqual(state["stages"]["formal_dag"]["status"], "done")

    def test_informal_dag_falls_back_when_model_json_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = "# Proof\n\nLemma one establishes the boundary.\n\nTherefore partial progress.\n"
            (workspace / "proof.md").write_text(proof, encoding="utf-8")
            fallback = proof_graph.graph_from_text(proof)
            auto_pipeline._write_state(
                workspace,
                {"status": "running", "stages": {"informal_dag": {"status": "waiting"}}},
            )
            with patch.object(
                proof_graph, "generate", side_effect=ValueError("Invalid \\escape")
            ):
                auto_pipeline._informal_worker(
                    workspace,
                    {"engine": "deepagents"},
                    {"id": 7},
                    "gpt-test",
                    {"informal_graph": fallback},
                    "deepagents",
                )
            state = auto_pipeline.load_state(workspace)
            cached = proof_graph.load_cached(workspace)

        self.assertEqual(state["stages"]["informal_dag"]["status"], "parsed")
        self.assertEqual(cached["model"], "structural-parse")
        self.assertEqual(len(cached["graph"]["nodes"]), len(fallback["nodes"]))
        self.assertIn("Invalid", cached["fallback_reason"])

    def test_manual_lean_check_refreshes_only_formal_monitor_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text("# Proof\nVerified claim.\n", encoding="utf-8")
            lean_dir = workspace / "lean"
            lean_dir.mkdir()
            (lean_dir / "Proof.lean").write_text(
                "theorem main : True := by trivial\n", encoding="utf-8"
            )
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps({"status": "verified", "model": "gpt-test"}),
                encoding="utf-8",
            )
            auto_pipeline._write_state(
                workspace,
                {
                    "status": "done",
                    "selected_harness": "codex",
                    "stages": {
                        "informal_dag": {"status": "done", "detail": "keep me"},
                        "lean": {"status": "done", "detail": "Lean failed · codex"},
                        "formal_dag": {"status": "done", "detail": "old graph"},
                        "coverage": {"status": "done", "detail": "0/2"},
                    },
                },
            )
            with (
                patch.object(auto_pipeline, "_run_record", return_value={"engine": "codex"}),
                patch.object(
                    proof_graph,
                    "generate",
                    return_value={
                        "model": "gpt-test",
                        "source": "Proof.lean",
                        "graph": {"nodes": [{"id": "a"}, {"id": "main"}], "edges": []},
                    },
                ) as generate,
                patch.object(
                    proof_bridge,
                    "build",
                    return_value={
                        "status": "verified",
                        "summary": {"verified": 2, "total": 2},
                    },
                ) as build,
            ):
                auto_pipeline._refresh_formal_once(
                    run_id="formal-refresh-test",
                    workspace=workspace,
                    owner_id=7,
                    model="gpt-test",
                    engine="codex",
                )
            state = auto_pipeline.load_state(workspace)

        self.assertEqual(state["status"], "done")
        self.assertEqual(state["stages"]["informal_dag"]["detail"], "keep me")
        self.assertEqual(state["stages"]["lean"]["detail"], "Lean verified · shared translator/kernel · proof from codex")
        self.assertEqual(state["stages"]["formal_dag"]["detail"], "2 Lean nodes · shared formal DAG")
        self.assertEqual(
            state["stages"]["coverage"]["detail"],
            "2/2 informal statements formally verified",
        )
        self.assertEqual(generate.call_args.kwargs["kind"], "formal")
        build.assert_called_once_with(workspace, selected_harness="codex")

    def test_reconciliation_never_interrupts_a_live_formal_refresh(self) -> None:
        class LiveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            auto_pipeline._write_state(
                workspace,
                {
                    "status": "running",
                    "stages": {
                        "informal_dag": {"status": "done"},
                        "lean": {"status": "done"},
                        "formal_dag": {"status": "running"},
                        "coverage": {"status": "waiting"},
                    },
                },
            )
            key = str(workspace.resolve())
            with auto_pipeline._FORMAL_REFRESH_LOCK:
                auto_pipeline._FORMAL_REFRESH_ACTIVE[key] = LiveThread()
            try:
                changed = auto_pipeline.reconcile_orphaned(
                    workspace,
                    "worker missing",
                )
                state = auto_pipeline.load_state(workspace)
            finally:
                with auto_pipeline._FORMAL_REFRESH_LOCK:
                    auto_pipeline._FORMAL_REFRESH_ACTIVE.pop(key, None)

        self.assertFalse(changed)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["stages"]["formal_dag"]["status"], "running")

    def test_automatic_dag_never_falls_back_to_a_second_codex_harness(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(proof_graph, "_call_llm", side_effect=ValueError("no direct api")),
            patch.object(proof_graph, "_run_codex_prompt") as codex,
            patch.object(settings, "resolved_user_env", return_value={}),
        ):
            workspace = Path(tmp)
            (workspace / "proof.md").write_text(
                "# Proof\nAssume n is even. Then n = 2k. Therefore n + 2 is even.\n",
                encoding="utf-8",
            )
            result = proof_graph.generate(
                workspace=workspace,
                run_record={},
                user={"id": 7},
                model=None,
                kind="informal",
                allow_codex_fallback=False,
            )

        self.assertEqual(result["model"], "structural-parse")
        self.assertTrue(result["graph"]["nodes"])
        codex.assert_not_called()

    def test_proof_engine_is_distinguished_from_shared_derived_tools(self) -> None:
        class DummyThread:
            def is_alive(self):
                return False

            def start(self):
                return None

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(auto_pipeline.threading, "Thread", return_value=DummyThread()),
        ):
            state = auto_pipeline.start(
                run_id="selected-harness-test",
                workspace=Path(tmp),
                owner_id=7,
                model="gpt-test",
                engine="hermes",
            )

        self.assertEqual(state["proof_engine"], "hermes")
        self.assertFalse(state["single_harness"])
        self.assertNotIn("selected_harness", state)
        for stage in state["stages"].values():
            self.assertEqual(stage["proof_engine"], "hermes")
            self.assertTrue(stage["derived_executor"].startswith("shared-"))
            self.assertNotIn("harness", stage)

    def test_lean_receives_informal_dag_guidance_while_workers_run_together(self) -> None:
        observed: dict[str, object] = {}

        def generate_graph(*, workspace, kind, allow_codex_fallback, **_kwargs):
            self.assertFalse(allow_codex_fallback)
            if kind == "formal":
                graph = {
                    "title": "main",
                    "nodes": [{"id": "f1", "kind": "conclusion", "label": "main", "statement": "True"}],
                    "edges": [],
                }
                filename = proof_graph.LEAN_GRAPH_FILENAME
            else:
                graph = {
                    "title": "Parity",
                    "nodes": [{"id": "i1", "kind": "conclusion", "label": "Even conclusion", "statement": "n + 2 is even"}],
                    "edges": [],
                }
                filename = proof_graph.GRAPH_FILENAME
            result = {"model": "tool", "source": kind, "kind": kind, "graph": graph}
            (workspace / filename).write_text(json.dumps(result), encoding="utf-8")
            return result

        def generate_lean(*, workspace, guidance=None, **_kwargs):
            observed["guidance"] = guidance
            lean_dir = workspace / "lean"
            lean_dir.mkdir(exist_ok=True)
            (lean_dir / "Proof.lean").write_text("theorem main : True := by trivial\n", encoding="utf-8")
            result = {
                "status": "verified",
                "model": "tool",
                "sorry_count": 0,
                "fidelity": {"severity": "ok", "audit": {"ok": True, "faithful": True}},
            }
            (workspace / lean_verify.RESULT_FILENAME).write_text(json.dumps(result), encoding="utf-8")
            return result

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(proof_graph, "generate", side_effect=generate_graph),
            patch.object(lean_verify, "generate", side_effect=generate_lean),
        ):
            workspace = Path(tmp)
            (workspace / "proof.md").write_text(
                "# Proof\nAssume n is even. Therefore n + 2 is even.\n",
                encoding="utf-8",
            )
            auto_pipeline._write_state(
                workspace,
                {"status": "running", "selected_harness": "hermes", "stages": {}},
            )
            auto_pipeline._run_batch(
                workspace, {"engine": "hermes"}, {"id": 7}, "gpt-test", "hermes"
            )
            state = auto_pipeline.load_state(workspace)

        self.assertIn("COOPERATIVE INFORMAL DAG", str(observed.get("guidance") or ""))
        self.assertEqual(state["stages"]["coverage"]["status"], "done")
        lean_stage = state["stages"]["lean"]["result"]
        self.assertEqual(lean_stage["proof_engine"], "hermes")
        self.assertEqual(lean_stage["derived_executor"], "shared-lean-translator-kernel")
        self.assertNotIn("harness", lean_stage)

    def test_green_coverage_requires_kernel_and_faithful_audit(self) -> None:
        informal = {
            "graph": {
                "nodes": [
                    {
                        "id": "i1",
                        "kind": "conclusion",
                        "label": "Even conclusion",
                        "statement": "n + 2 is even",
                    }
                ],
                "edges": [],
            }
        }
        formal = {
            "graph": {
                "nodes": [
                    {
                        "id": "f1",
                        "kind": "conclusion",
                        "label": "main",
                        "statement": "theorem main : Even (n + 2)",
                    }
                ],
                "edges": [],
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / proof_graph.GRAPH_FILENAME).write_text(json.dumps(informal), encoding="utf-8")
            (workspace / proof_graph.LEAN_GRAPH_FILENAME).write_text(json.dumps(formal), encoding="utf-8")
            false_result = {
                "status": "unfaithful",
                "sorry_count": 0,
                "fidelity": {"severity": "major", "audit": {"ok": True, "faithful": False}},
            }
            (workspace / lean_verify.RESULT_FILENAME).write_text(json.dumps(false_result), encoding="utf-8")
            coverage = proof_bridge.build(workspace, selected_harness="hermes")
            self.assertEqual(coverage["summary"]["verified"], 0)

            true_result = {
                "status": "verified",
                "sorry_count": 0,
                "fidelity": {"severity": "ok", "audit": {"ok": True, "faithful": True}},
            }
            (workspace / lean_verify.RESULT_FILENAME).write_text(json.dumps(true_result), encoding="utf-8")
            coverage = proof_bridge.build(workspace, selected_harness="hermes")

        self.assertEqual(coverage["summary"]["verified"], 1)
        self.assertEqual(coverage["items"][0]["status"], "verified")
        self.assertEqual(coverage["items"][0]["formal_ids"], ["f1"])

    def test_incomplete_lean_preserves_only_admission_independent_helpers(self) -> None:
        lean_source = """def blockProduct : Prop := True

theorem earlier_block_longer : True := by trivial

theorem admitted_result : True := by sorry

theorem derived_consequence : True := admitted_result

theorem main : True := by sorry
"""
        informal = {
            "graph": {
                "nodes": [
                    {
                        "id": "i1",
                        "kind": "lemma",
                        "label": "Earlier block is longer",
                        "statement": "Every admissible solution has an earlier block that is longer.",
                    },
                    {
                        "id": "i2",
                        "kind": "claim",
                        "label": "Derived consequence",
                        "statement": "The derived consequence holds.",
                    },
                    {
                        "id": "i3",
                        "kind": "conclusion",
                        "label": "Open main conclusion",
                        "statement": "The unrestricted theorem holds.",
                    },
                ],
                "edges": [],
            }
        }
        formal = {"graph": proof_graph.graph_from_lean(lean_source)}
        result = {
            "status": "incomplete",
            "sorry_count": 2,
            "lean": lean_source,
            "attempts": [{"check": {"ok": True, "exit_code": 0}}],
            "fidelity": {
                "severity": "major",
                "audit": {"ok": True, "faithful": False},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / proof_graph.GRAPH_FILENAME).write_text(
                json.dumps(informal), encoding="utf-8"
            )
            (workspace / proof_graph.LEAN_GRAPH_FILENAME).write_text(
                json.dumps(formal), encoding="utf-8"
            )
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps(result), encoding="utf-8"
            )
            coverage = proof_bridge.build(workspace, selected_harness="deepseek_harness")

        items = {item["informal_id"]: item for item in coverage["items"]}
        self.assertEqual(coverage["status"], "partial")
        self.assertFalse(coverage["formal_verified"])
        self.assertEqual(coverage["summary"]["verified"], 1)
        self.assertEqual(items["i1"]["status"], "verified")
        self.assertEqual(items["i1"]["lean_declarations"], ["earlier_block_longer"])
        self.assertNotEqual(items["i2"]["status"], "verified")
        self.assertNotEqual(items["i3"]["status"], "verified")

    def test_partial_coverage_matches_forced_ap_helper_without_green_open_main(self) -> None:
        lean_source = """def ForcedAP (k : Nat) : Prop := True

theorem forcedAP_three : ForcedAP 3 := by trivial

theorem main : ForcedAP 4 := by sorry
"""
        informal = {
            "graph": {
                "nodes": [
                    {
                        "id": "proved-three",
                        "kind": "lemma",
                        "label": "Every permutation forces three",
                        "statement": "Every one-sided permutation forces a three-term progression.",
                    },
                    {
                        "id": "open-four",
                        "kind": "conclusion",
                        "label": "Exact value remains unresolved",
                        "statement": "Whether every permutation forces four remains open.",
                    },
                ],
                "edges": [],
            }
        }
        formal = {"graph": proof_graph.graph_from_lean(lean_source)}
        result = {
            "status": "incomplete",
            "sorry_count": 1,
            "lean": lean_source,
            "attempts": [{"check": {"ok": True, "exit_code": 0}}],
            "fidelity": {
                "severity": "major",
                "audit": {"ok": True, "faithful": False},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / proof_graph.GRAPH_FILENAME).write_text(
                json.dumps(informal), encoding="utf-8"
            )
            (workspace / proof_graph.LEAN_GRAPH_FILENAME).write_text(
                json.dumps(formal), encoding="utf-8"
            )
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps(result), encoding="utf-8"
            )
            coverage = proof_bridge.build(workspace, selected_harness="improof")

        items = {item["informal_id"]: item for item in coverage["items"]}
        self.assertEqual(coverage["summary"]["verified"], 1)
        self.assertEqual(items["proved-three"]["status"], "verified")
        self.assertEqual(
            items["proved-three"]["lean_declarations"], ["forcedAP_three"]
        )
        self.assertNotEqual(items["open-four"]["status"], "verified")

    def test_partial_coverage_accepts_a_distinctive_numeric_certificate(self) -> None:
        lean_source = """theorem boundary_five_certificate :
    (4455 : Int) * 4480 = 19958400 ∧ 4480 - 4455 = 25 := by norm_num

 theorem main : True := by sorry
"""
        informal = {
            "graph": {
                "nodes": [
                    {
                        "id": "exact",
                        "kind": "claim",
                        "label": "Boundary minimum at five",
                        "statement": (
                            "The exact certificate is 4455 * 4480 = 19958400 "
                            "and 4480 - 4455 = 25."
                        ),
                    },
                    {
                        "id": "near_miss",
                        "kind": "claim",
                        "label": "Boundary maximum at five",
                        "statement": "An unrelated boundary value is 25.",
                    },
                ],
                "edges": [],
            }
        }
        formal = {"graph": proof_graph.graph_from_lean(lean_source)}
        result = {
            "status": "incomplete",
            "sorry_count": 1,
            "lean": lean_source,
            "attempts": [{"check": {"ok": True, "exit_code": 0}}],
            "fidelity": {
                "severity": "major",
                "audit": {"ok": True, "faithful": False},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / proof_graph.GRAPH_FILENAME).write_text(
                json.dumps(informal), encoding="utf-8"
            )
            (workspace / proof_graph.LEAN_GRAPH_FILENAME).write_text(
                json.dumps(formal), encoding="utf-8"
            )
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps(result), encoding="utf-8"
            )
            coverage = proof_bridge.build(workspace, selected_harness="openclaude")

        items = {item["informal_id"]: item for item in coverage["items"]}
        self.assertEqual(coverage["summary"]["verified"], 1)
        self.assertEqual(items["exact"]["status"], "verified")
        self.assertEqual(
            items["exact"]["lean_declarations"], ["boundary_five_certificate"]
        )
        self.assertNotEqual(items["near_miss"]["status"], "verified")

    def test_partial_coverage_matches_exact_edge_deletion_biconditional(self) -> None:
        lean_source = """def deleteEdge (G : Nat) (x y : Nat) : Nat := G

theorem deleteEdge_three_colorable_iff_exists_same_color_of_not_colorable :
    deleteEdge 1 2 3 = 1 ↔ ∃ c : Fin 3 → Fin 3, c 2 = c 3 := by
  constructor
  · intro _
    exact ⟨fun _ => 0, rfl⟩
  · intro _
    rfl

theorem main : True := by sorry
"""
        informal = {
            "graph": {
                "nodes": [
                    {
                        "id": "exact",
                        "kind": "lemma",
                        "label": "Edge-deletion characterization",
                        "statement": (
                            "The deleted-edge graph is 3-colorable if and only if "
                            "it has a 3-coloring giving both endpoints the same color."
                        ),
                    },
                    {
                        "id": "one-way",
                        "kind": "step",
                        "label": "Edge-deletion sufficient direction",
                        "statement": (
                            "A same-color endpoint coloring is sufficient for "
                            "3-colorability after deleting the edge."
                        ),
                    },
                ],
                "edges": [],
            }
        }
        formal = {"graph": proof_graph.graph_from_lean(lean_source)}
        result = {
            "status": "incomplete",
            "sorry_count": 1,
            "lean": lean_source,
            "attempts": [{"check": {"ok": True, "exit_code": 0}}],
            "fidelity": {
                "severity": "minor",
                "audit": {"ok": True, "faithful": False},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / proof_graph.GRAPH_FILENAME).write_text(
                json.dumps(informal), encoding="utf-8"
            )
            (workspace / proof_graph.LEAN_GRAPH_FILENAME).write_text(
                json.dumps(formal), encoding="utf-8"
            )
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps(result), encoding="utf-8"
            )
            coverage = proof_bridge.build(workspace, selected_harness="hermes")

        items = {item["informal_id"]: item for item in coverage["items"]}
        self.assertEqual(coverage["summary"]["verified"], 1)
        self.assertEqual(items["exact"]["status"], "verified")
        self.assertEqual(
            items["exact"]["lean_declarations"],
            ["deleteEdge_three_colorable_iff_exists_same_color_of_not_colorable"],
        )
        self.assertNotEqual(items["one-way"]["status"], "verified")

    def test_partial_coverage_maps_exact_indexed_value_without_false_inequalities(self) -> None:
        lean_source = """def Nk (k n : Nat) : Prop := True

theorem nk_three_eq_three : Nk 3 3 := by trivial

theorem main : Nk 3 3 ∧ True := by sorry
"""
        informal = {
            "graph": {
                "nodes": [
                    {
                        "id": "exact",
                        "kind": "claim",
                        "label": "Exact value n_3",
                        "statement": "Combining both bounds gives $n_3=3$.",
                    },
                    {
                        "id": "upper",
                        "kind": "step",
                        "label": "Upper bound for n_3",
                        "statement": "The construction proves $n_3\\le 3$.",
                    },
                    {
                        "id": "wrong",
                        "kind": "claim",
                        "label": "Wrong exact value n_3",
                        "statement": "Suppose instead that $n_3=4$.",
                    },
                ],
                "edges": [],
            }
        }
        formal = {"graph": proof_graph.graph_from_lean(lean_source)}
        result = {
            "status": "incomplete",
            "sorry_count": 1,
            "lean": lean_source,
            "attempts": [{"check": {"ok": True, "exit_code": 0}}],
            "fidelity": {
                "severity": "major",
                "audit": {"ok": True, "faithful": False},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / proof_graph.GRAPH_FILENAME).write_text(
                json.dumps(informal), encoding="utf-8"
            )
            (workspace / proof_graph.LEAN_GRAPH_FILENAME).write_text(
                json.dumps(formal), encoding="utf-8"
            )
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps(result), encoding="utf-8"
            )
            coverage = proof_bridge.build(workspace, selected_harness="codex")

        items = {item["informal_id"]: item for item in coverage["items"]}
        self.assertEqual(coverage["summary"]["verified"], 1)
        self.assertEqual(items["exact"]["status"], "verified")
        self.assertEqual(
            items["exact"]["lean_declarations"], ["nk_three_eq_three"]
        )
        self.assertNotEqual(items["upper"]["status"], "verified")
        self.assertNotEqual(items["wrong"]["status"], "verified")

    def test_edited_proof_invalidates_old_green_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / proof_graph.GRAPH_FILENAME).write_text(
                json.dumps(
                    {
                        "graph": {
                            "nodes": [
                                {
                                    "id": "i1",
                                    "kind": "conclusion",
                                    "label": "Conclusion",
                                    "statement": "The claim holds",
                                }
                            ],
                            "edges": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            (workspace / proof_graph.LEAN_GRAPH_FILENAME).write_text(
                json.dumps(
                    {
                        "graph": {
                            "nodes": [
                                {
                                    "id": "f1",
                                    "kind": "conclusion",
                                    "label": "main",
                                    "statement": "theorem main : True",
                                }
                            ],
                            "edges": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps(
                    {
                        "status": "verified",
                        "sorry_count": 0,
                        "fidelity": {
                            "severity": "ok",
                            "audit": {"ok": True, "faithful": True},
                        },
                    }
                ),
                encoding="utf-8",
            )
            proof_bridge.build(workspace, selected_harness="hermes")
            (workspace / "proof.md").write_text("# Edited proof\nDifferent claim.\n", encoding="utf-8")
            stale = proof_bridge.load_cached(workspace)

        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["summary"]["verified"], 0)
        self.assertFalse(any(item["status"] == "verified" for item in stale["items"]))

    def test_automatic_pipeline_preserves_exact_recorded_model(self) -> None:
        self.assertEqual(auto_pipeline._api_model("gpt-5.6-sol"), "gpt-5.6-sol")
        self.assertEqual(auto_pipeline._api_model("opus"), "opus")
        self.assertIsNone(auto_pipeline._api_model("codex"))
        self.assertEqual(auto_pipeline._api_model("claude-sonnet-5"), "claude-sonnet-5")


class Erdos933ExactCertificateTests(unittest.TestCase):
    def test_bounded_audit_emits_exact_certificate(self) -> None:
        script = Path(__file__).resolve().parents[1] / "evals" / "erdos933_preprint_audit.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--limit", "512"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("exact_finite_maximizers=[2, 8, 512]", completed.stdout)
        self.assertIn(
            "exact_log_certificate=nontrivial_cases:9,strict_cases:7,series_terms:40",
            completed.stdout,
        )
        self.assertIn("exact_strict_cases=[3, 15, 27, 63, 80, 243, 255]", completed.stdout)
        self.assertIn("exact_strict_margin_lower_bound=1", completed.stdout)
        self.assertIn("scope=finite consistency check only", completed.stdout)
        smaller = subprocess.run(
            [sys.executable, str(script), "--limit", "100"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("exact_finite_maximizers=[2, 8]", smaller.stdout)


if __name__ == "__main__":
    unittest.main()
