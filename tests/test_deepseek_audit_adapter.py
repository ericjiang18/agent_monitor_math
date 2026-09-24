from __future__ import annotations

import json
import http.client
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import Mock, patch

from agent_monitor.research_audit import reconciliation_complete
from agent_monitor.runners import deepseek_harness_runner as runner
from agent_monitor.runners import kimi_chat_completions_proxy
from agent_monitor.runners.codex_backend import CodexResult


def valid_verifier(summary: str) -> dict:
    return {
        "verdict": "accept",
        "summary": summary,
        "findings": [],
        "coverage_notes": {
            "reviewed_regions": ["complete candidate"],
            "unreviewed_or_difficult_regions": [],
            "external_checks_not_performed": ["none requested"],
        },
    }


def install_audit_skill(workspace: Path) -> None:
    source = (
        Path(runner.__file__).resolve().parents[1]
        / "starter_skills"
        / "research-proof-audit"
    )
    shutil.copytree(
        source,
        workspace / "_agent" / "skills" / "research-proof-audit",
    )


class DeepSeekAuditAdapterTests(unittest.TestCase):
    def test_final_audit_metrics_do_not_confuse_completion_with_acceptance(self) -> None:
        self.assertEqual(
            runner._final_audit_metrics(
                {"verdict": "major_revision", "findings": [{"finding_id": "F1"}]}
            ),
            {
                "final_audit_verdict": "major_revision",
                "open_finding_count": 1,
                "audit_accepts_final": False,
            },
        )
        self.assertTrue(
            runner._final_audit_metrics(valid_verifier("accepted"))[
                "audit_accepts_final"
            ]
        )

    def test_merge_map_validator_recomputes_counts_and_rejects_duplicate_sources(self) -> None:
        valid = {
            "schema_version": "ensemble-paper-audit-app.merge-map.v1",
            "findings": [
                {
                    "finding_id": "F1",
                    "sources": [
                        {"pass": "global", "finding_id": "G1"},
                        {"pass": "decomposed", "finding_id": "D1"},
                    ],
                }
            ],
            "counts": {"global_only": 0, "decomposed_only": 0, "both": 1},
        }
        self.assertTrue(runner._valid_merge_map(valid))

        wrong_counts = json.loads(json.dumps(valid))
        wrong_counts["counts"]["both"] = 0
        self.assertFalse(runner._valid_merge_map(wrong_counts))

        duplicate_source = json.loads(json.dumps(valid))
        duplicate_source["findings"][0]["sources"].append(
            {"pass": "global", "finding_id": "G1"}
        )
        self.assertFalse(runner._valid_merge_map(duplicate_source))

    def test_audit_gesture_runs_three_passes_and_adjudication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            install_audit_skill(workspace)
            global_pass = valid_verifier("Global evidence.")
            decomposed_pass = valid_verifier("Claim ledger.")
            refuter_pass = valid_verifier("Boundary check.")
            merged = valid_verifier("All validated passes reconciled.")
            merge_envelope = {
                "verifier_output": merged,
                "merge_map": {
                    "schema_version": "ensemble-paper-audit-app.merge-map.v1",
                    "findings": [],
                    "counts": {"global_only": 0, "decomposed_only": 0, "both": 0},
                },
            }
            results = [
                CodexResult("Candidate proof.\n", {"input_tokens": 1, "output_tokens": 1}),
                CodexResult(json.dumps(global_pass), {}),
                CodexResult(json.dumps(decomposed_pass), {}),
                CodexResult(json.dumps(refuter_pass), {}),
                CodexResult(json.dumps(merge_envelope), {}),
                CodexResult("# Final\n\nRepaired.\n\nProvingConsole outcome: Partial Progress\n", {}),
                CodexResult(json.dumps(global_pass), {}),
                CodexResult(json.dumps(decomposed_pass), {}),
                CodexResult(json.dumps(refuter_pass), {}),
                CodexResult(json.dumps(merge_envelope), {}),
            ]
            fake = Mock(side_effect=results)
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                with patch.object(runner, "codex_exec", fake):
                    code = runner.run_codex(
                        "CANDIDATE AUDIT GATE: Read "
                        "_agent/skills/research-proof-audit/SKILL.md before finalizing."
                    )
            finally:
                os.chdir(previous)
            self.assertEqual(code, 0)
            self.assertEqual(fake.call_count, 10)
            self.assertIn("ProvingConsole outcome: Partial Progress", (workspace / "proof.md").read_text())
            audit_dirs = list((workspace / "audit").iterdir())
            self.assertEqual(len(audit_dirs), 1)
            audit = audit_dirs[0]
            for name in (
                "frozen-task.md",
                "frozen-candidate.md",
                "global.json",
                "decomposed.json",
                "refuter.json",
                "final-candidate.md",
                "global-rerun.json",
                "decomposed-rerun.json",
                "refuter-rerun.json",
                "verifier-output.json",
                "merge-map.json",
                "run.json",
            ):
                self.assertTrue((audit / name).is_file(), name)
            pass_prompts = [call.args[1] for call in fake.call_args_list[1:4]]
            self.assertTrue(all("EXACT ORIGINAL TASK" in prompt for prompt in pass_prompts))
            self.assertTrue(all("research-proof-audit/SKILL.md" in prompt for prompt in pass_prompts))
            self.assertTrue(all("VERIFIER OUTPUT SCHEMA" in prompt for prompt in pass_prompts))
            self.assertIn("MERGE PROTOCOL", fake.call_args_list[4].args[1])
            self.assertTrue(
                all("Do not call it Plain" in prompt for prompt in pass_prompts)
            )
            adjudication_prompt = fake.call_args_list[5].args[1]
            self.assertIn("RUNNER AUDIT STATUS", adjudication_prompt)
            self.assertIn('"complete_core": true', adjudication_prompt)
            self.assertIn("remove stale claims", adjudication_prompt)
            manifest = json.loads((audit / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["passes"], ["global", "decomposed", "refuter"])
            self.assertEqual(
                manifest["validated_passes"], ["decomposed", "global", "refuter"]
            )
            self.assertTrue(manifest["complete_core"])
            self.assertEqual(manifest["errors"], {})
            self.assertEqual(manifest["repairs"], {})
            self.assertEqual(
                manifest["independence"], "fresh_context_serial_passes"
            )
            self.assertFalse(manifest["degraded_independence"])
            self.assertTrue(manifest["reconciliation_ready"])
            self.assertEqual(manifest["final_audit_verdict"], "accept")
            self.assertEqual(manifest["open_finding_count"], 0)
            self.assertTrue(manifest["audit_accepts_final"])
            self.assertEqual(
                manifest["reconciliation"]["status"], "repaired_revalidated"
            )
            self.assertTrue(reconciliation_complete(workspace, audit))
            self.assertEqual(
                (audit / "final-candidate.md").read_bytes(),
                (workspace / "proof.md").read_bytes(),
            )

    def test_invalid_structured_passes_cannot_preserve_solved_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            install_audit_skill(workspace)
            fake = Mock(
                side_effect=[
                    CodexResult("not json", {}),
                    CodexResult("still not json", {}),
                    CodexResult("also not json", {}),
                    CodexResult("still invalid", {}),
                    CodexResult("invalid refuter", {}),
                    CodexResult("invalid refuter repair", {}),
                    CodexResult(
                        "# Final\n\nUnsupported.\n\n"
                        "ProvingConsole outcome: Solved\n",
                        {},
                    ),
                    CodexResult("invalid final global", {}),
                    CodexResult("invalid final decomposed", {}),
                    CodexResult("invalid final refuter", {}),
                ]
            )
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                final = runner.run_codex_audit(
                    prompt="audit exact problem",
                    candidate="candidate",
                    model="gpt-5.6-sol",
                    call=fake,
                )
            finally:
                os.chdir(previous)
            self.assertNotIn("ProvingConsole outcome: Solved", final)
            self.assertIn("ProvingConsole outcome: Partial Progress", final)
            audit = next((workspace / "audit").iterdir())
            self.assertFalse((audit / "verifier-output.json").exists())
            manifest = json.loads((audit / "run.json").read_text(encoding="utf-8"))
            self.assertFalse(manifest["complete_core"])
            self.assertFalse(manifest["reconciliation_ready"])
            self.assertIn("merge", manifest["errors"])
            self.assertIn("merge", manifest["rerun_errors"])
            self.assertEqual(fake.call_count, 10)

    def test_malformed_outer_pass_is_repaired_without_accepting_nested_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            install_audit_skill(workspace)
            valid = valid_verifier("valid")
            malformed = (
                '{"verdict":"accept","summary":"broken","findings":'
                '[{"location":{"section":"s","quote":"q"}}],"coverage_notes":'
            )
            merge_envelope = {
                "verifier_output": valid,
                "merge_map": {
                    "schema_version": "ensemble-paper-audit-app.merge-map.v1",
                    "findings": [],
                    "counts": {"global_only": 0, "decomposed_only": 0, "both": 0},
                },
            }
            fake = Mock(
                side_effect=[
                    CodexResult(json.dumps(valid), {}),
                    CodexResult(malformed, {}),
                    CodexResult(json.dumps(valid_verifier("repaired")), {}),
                    CodexResult(json.dumps(valid), {}),
                    CodexResult(json.dumps(merge_envelope), {}),
                    CodexResult(
                        "# Final\n\nAudited.\n\nProvingConsole outcome: Partial Progress\n",
                        {},
                    ),
                    CodexResult(json.dumps(valid), {}),
                    CodexResult(json.dumps(valid), {}),
                    CodexResult(json.dumps(valid), {}),
                    CodexResult(json.dumps(merge_envelope), {}),
                ]
            )
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                final = runner.run_codex_audit(
                    prompt="audit exact problem",
                    candidate="candidate",
                    model="gpt-5.6-sol",
                    call=fake,
                )
            finally:
                os.chdir(previous)
            audit = next((workspace / "audit").iterdir())
            manifest = json.loads((audit / "run.json").read_text(encoding="utf-8"))
            invalid_preserved = (audit / "decomposed.invalid.md").is_file()
            repaired_written = (audit / "decomposed.json").is_file()
        self.assertIn("Partial Progress", final)
        self.assertTrue(invalid_preserved)
        self.assertTrue(repaired_written)
        self.assertTrue(manifest["complete_core"])
        self.assertTrue(manifest["reconciliation_ready"])
        self.assertTrue(manifest["repairs"]["decomposed"]["succeeded"])
        self.assertEqual(manifest["errors"], {})
        self.assertEqual(manifest["rerun_errors"], {})

    def test_extractor_never_promotes_a_nested_json_fragment(self) -> None:
        malformed = (
            '{"verdict":"accept","findings":'
            '[{"location":{"section":"nested","quote":"q"}}]'
        )
        with self.assertRaisesRegex(ValueError, "required top-level keys"):
            runner._extract_json_object(
                malformed,
                required_keys={"verdict", "summary", "findings", "coverage_notes"},
            )

    def test_unrequested_run_stays_a_single_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake = Mock(return_value=CodexResult("# Proof\n\nDone.\n", {}))
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                with patch.object(runner, "codex_exec", fake):
                    code = runner.run_codex("Prove the elementary statement.")
            finally:
                os.chdir(previous)
            self.assertEqual(code, 0)
            self.assertEqual(fake.call_count, 1)
            self.assertFalse((workspace / "audit").exists())

    def test_passive_library_skill_listing_does_not_trigger_audit(self) -> None:
        prompt = (
            "Enabled skill: research-proof-audit "
            "(source: _agent/skills/research-proof-audit/SKILL.md).\n"
            "CONTROL CONDITION: use the normal workflow."
        )
        self.assertFalse(runner.audit_requested(prompt))
        self.assertTrue(runner.audit_requested("Use $research-proof-audit now."))
        self.assertTrue(runner.audit_requested("CANDIDATE AUDIT GATE: audit now."))

    def test_explicit_outcome_contract_is_fail_closed_without_triggering_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake = Mock(return_value=CodexResult("# Result\n\nA bounded lemma.", {}))
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                with patch.object(runner, "codex_exec", fake):
                    code = runner.run_codex(
                        "CONTROL CONDITION: End with ProvingConsole outcome: <label>."
                    )
            finally:
                os.chdir(previous)
            self.assertEqual(code, 0)
            self.assertEqual(fake.call_count, 1)
            self.assertFalse((workspace / "audit").exists())
            self.assertTrue(
                (workspace / "proof.md")
                .read_text(encoding="utf-8")
                .rstrip()
                .endswith("ProvingConsole outcome: Partial Progress")
            )

    def test_symlinked_audit_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            workspace = Path(tmp)
            (workspace / "audit").symlink_to(Path(outside), target_is_directory=True)
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                with self.assertRaises(OSError):
                    runner.run_codex_audit(
                        prompt="audit",
                        candidate="candidate",
                        model="gpt-5.6-sol",
                        call=Mock(),
                    )
            finally:
                os.chdir(previous)


class DeepSeekNativeKimiTests(unittest.TestCase):
    @staticmethod
    def _exhaustion_events(
        *,
        content: list[dict] | None = None,
        reason: str = "max-tokens",
    ) -> list[str]:
        return [
            json.dumps({"type": "turn/start", "data": {"turn": 1}}),
            json.dumps(
                {
                    "type": "assistant/message",
                    "data": {
                        "turn": 1,
                        "step": 1,
                        "usage": {
                            "inputTokens": 10_882,
                            "outputTokens": 32_768,
                        },
                        "message": {
                            "role": "assistant",
                            "source": {
                                "model": "kimi-k3",
                                "provider": "flashflame",
                            },
                            "content": (
                                content
                                if content is not None
                                else [{"type": "reasoning", "text": "hidden"}]
                            ),
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn/end",
                    "data": {"turn": 1, "reason": {"kind": reason}},
                }
            ),
        ]

    def test_terminal_detector_requires_reasoning_only_kimi_max_tokens(self) -> None:
        detected = runner._terminal_kimi_exhaustion(self._exhaustion_events())
        self.assertIsNotNone(detected)
        self.assertEqual(detected["output_tokens"], 32_768)

        with_text = self._exhaustion_events(
            content=[
                {"type": "reasoning", "text": "hidden"},
                {"type": "text", "text": "final answer"},
            ]
        )
        self.assertIsNone(runner._terminal_kimi_exhaustion(with_text))

        with_tool = self._exhaustion_events()
        with_tool.insert(
            -1,
            json.dumps(
                {"type": "tool/call", "data": {"turn": 1, "name": "write"}}
            ),
        )
        self.assertIsNone(runner._terminal_kimi_exhaustion(with_tool))
        self.assertIsNone(
            runner._terminal_kimi_exhaustion(
                self._exhaustion_events(reason="error")
            )
        )
        newer_open_turn = [
            *self._exhaustion_events(),
            json.dumps({"type": "turn/start", "data": {"turn": 2}}),
        ]
        self.assertIsNone(runner._terminal_kimi_exhaustion(newer_open_turn))

    def test_newest_changed_session_is_the_only_log_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".dsh"
            old = home / "sessions" / "old" / "session.jsonl.zstd"
            newest = home / "sessions" / "new" / "session.jsonl.zstd"
            newest.parent.mkdir(parents=True)
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            newest.write_bytes(b"new")
            before = {old.resolve(): (1, 3), newest.resolve(): (2, 3)}
            after = {old.resolve(): (1, 3), newest.resolve(): (3, 3)}

            def decode(_command: list[str], **kwargs: object) -> Mock:
                kwargs["stdout"].write(
                    "\n".join(self._exhaustion_events()).encode()
                )
                return Mock(returncode=0)

            with (
                patch.object(runner, "_dsh_session_snapshot", return_value=after),
                patch.object(runner.shutil, "which", return_value="/usr/bin/zstdcat"),
                patch.object(runner.subprocess, "run", side_effect=decode) as launch,
            ):
                detected = runner._new_kimi_exhaustion(home, before)

        self.assertIsNotNone(detected)
        self.assertEqual(
            launch.call_args.args[0],
            ["/usr/bin/zstdcat", "--", str(newest.resolve())],
        )

    def test_selected_kimi_model_is_not_replaced_by_deepseek(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
                "DEEPSEEK_HARNESS_MODEL": "deepseek-v4-flash",
            },
            clear=True,
        ):
            self.assertEqual(runner.selected_model(), "kimi-k3")

    def test_selected_kimi_requires_its_own_credential(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"AGENT_MONITOR_SELECTED_MODEL": "kimi-k3"},
                clear=True,
            ),
            patch.object(runner, "dsh_command") as command,
            patch.object(runner, "emit_item") as emit_item,
        ):
            code = runner.run_api("prove")

        self.assertEqual(code, 2)
        command.assert_not_called()
        self.assertIn("KIMI_API_KEY", emit_item.call_args.args[0]["message"])

    def test_kimi_uses_isolated_pi_ai_route_without_persisting_secret(self) -> None:
        secret = "test-only-kimi-secret-never-persist"
        completed = Mock(returncode=0, stdout="# Proof\n\nDone.\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                with (
                    patch.dict(
                        os.environ,
                        {
                            "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
                            "KIMI_API_KEY": secret,
                            "KIMI_API_BASE": "https://kimi.example/v1/",
                            "KIMI_REASONING_EFFORT": "high",
                        },
                        clear=True,
                    ),
                    patch.object(runner, "dsh_command", return_value=["dsh"]),
                    patch.object(runner.subprocess, "run", return_value=completed) as launch,
                    patch.object(runner, "emit"),
                    patch.object(runner, "emit_item"),
                ):
                    code = runner.run_api("prove sqrt(3) irrational")
            finally:
                os.chdir(previous)

            settings_path = workspace / ".dsh" / "settings.yaml"
            settings_text = settings_path.read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertNotIn(secret, settings_text)
            self.assertIn("provider: flashflame", settings_text)
            self.assertIn("apiKeyEnv: KIMI_API_KEY", settings_text)
            self.assertIn("api: openai-completions", settings_text)
            self.assertRegex(
                settings_text,
                r'baseURL: "http://127\.0\.0\.1:\d+/v1"',
            )
            self.assertIn("model: kimi-k3", settings_text)
            self.assertIn("reasoningEffort: high", settings_text)
            self.assertIn("maxTokens: 32768", settings_text)
            for effort in ("low: low", "high: high", "max: max"):
                self.assertIn(effort, settings_text)
            self.assertEqual(settings_path.stat().st_mode & 0o777, 0o600)

            call = launch.call_args
            self.assertEqual(
                call.args[0],
                ["dsh", "--profile", "headless", "prove sqrt(3) irrational"],
            )
            self.assertNotEqual(call.kwargs["env"]["KIMI_API_KEY"], secret)
            self.assertTrue(call.kwargs["env"]["KIMI_API_KEY"])
            self.assertEqual(call.kwargs["env"]["DSH_HOME"], str(workspace / ".dsh"))
            self.assertEqual(
                call.kwargs["env"]["NODE_OPTIONS"],
                "--max-old-space-size=1024",
            )

    def test_reasoning_exhaustion_retries_once_high_and_preserves_checkpoint(self) -> None:
        secret = "test-only-kimi-secret-never-persist"
        calls: list[tuple[list[str], str, str]] = []

        def launch(command: list[str], **kwargs: object) -> Mock:
            settings = (Path.cwd() / ".dsh" / "settings.yaml").read_text(
                encoding="utf-8"
            )
            calls.append(
                (
                    command,
                    settings,
                    str(dict(kwargs)["env"]["KIMI_API_KEY"]),
                )
            )
            if len(calls) == 1:
                return Mock(returncode=1, stdout="", stderr="")
            Path("proof.md").write_text(
                "# Partial Progress\n\n"
                "A checkpointed lemma and its rigorous proof survive provider cutoff.\n",
                encoding="utf-8",
            )
            return Mock(returncode=0, stdout="Checkpoint saved.", stderr="")

        exhaustion = {
            "reason_kind": "max-tokens",
            "output_tokens": 32_768,
            "saw_reasoning": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                with (
                    patch.dict(
                        os.environ,
                        {
                            "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
                            "KIMI_API_KEY": secret,
                        },
                        clear=True,
                    ),
                    patch.object(runner, "dsh_command", return_value=["dsh"]),
                    patch.object(runner.subprocess, "run", side_effect=launch),
                    patch.object(
                        runner,
                        "_new_kimi_exhaustion",
                        side_effect=[exhaustion, None],
                    ),
                    patch.object(runner, "emit"),
                    patch.object(runner, "emit_item"),
                ):
                    code = runner.run_api("prove the claim")
                proof = Path("proof.md").read_text(encoding="utf-8")
            finally:
                os.chdir(previous)

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][-1], "prove the claim")
        self.assertIn("RECOVERY MODE", calls[1][0][-1])
        self.assertIn("Immediately create or update ./proof.md", calls[1][0][-1])
        self.assertIn("reasoningEffort: max", calls[0][1])
        self.assertIn("reasoningEffort: high", calls[1][1])
        self.assertNotEqual(calls[0][2], secret)
        self.assertNotEqual(calls[1][2], secret)
        self.assertNotEqual(calls[0][2], calls[1][2])
        self.assertIn("checkpointed lemma", proof)
        self.assertNotIn("Checkpoint saved", proof)

    def test_second_reasoning_exhaustion_emits_precise_safe_diagnostic(self) -> None:
        exhaustion = {
            "reason_kind": "max-tokens",
            "output_tokens": 32_768,
            "saw_reasoning": True,
        }
        completed = Mock(returncode=1, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                with (
                    patch.dict(
                        os.environ,
                        {
                            "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
                            "KIMI_API_KEY": "fake-secret",
                        },
                        clear=True,
                    ),
                    patch.object(runner, "dsh_command", return_value=["dsh"]),
                    patch.object(runner.subprocess, "run", return_value=completed) as launch,
                    patch.object(
                        runner,
                        "_new_kimi_exhaustion",
                        side_effect=[exhaustion, exhaustion],
                    ),
                    patch.object(runner, "emit"),
                    patch.object(runner, "emit_item") as emit_item,
                ):
                    code = runner.run_api("prove")
            finally:
                os.chdir(previous)

        self.assertEqual(code, 1)
        self.assertEqual(launch.call_count, 2)
        messages = [
            call.args[0].get("message", "") for call in emit_item.call_args_list
        ]
        diagnostic = next(
            message for message in messages if "both attempts" in message
        )
        self.assertIn("reasoning only and no final text or tool calls", diagnostic)
        self.assertIn("not an API-key authentication failure", diagnostic)
        self.assertNotIn("fake-secret", diagnostic)

    def test_auth_or_configuration_error_is_not_retried(self) -> None:
        completed = Mock(
            returncode=1,
            stdout="",
            stderr="401 invalid API key",
        )
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                with (
                    patch.dict(
                        os.environ,
                        {
                            "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
                            "KIMI_API_KEY": "fake-secret",
                        },
                        clear=True,
                    ),
                    patch.object(runner, "dsh_command", return_value=["dsh"]),
                    patch.object(runner.subprocess, "run", return_value=completed) as launch,
                    patch.object(runner, "_new_kimi_exhaustion", return_value=None),
                    patch.object(runner, "emit"),
                    patch.object(runner, "emit_item") as emit_item,
                ):
                    code = runner.run_api("prove")
            finally:
                os.chdir(previous)

        self.assertEqual(code, 1)
        self.assertEqual(launch.call_count, 1)
        self.assertIn("401 invalid API key", emit_item.call_args.args[0]["message"])

    def test_deepseek_settings_route_is_unchanged(self) -> None:
        self.assertEqual(
            runner._dsh_settings_document("deepseek-v4-flash"),
            "agent-default-model:\n"
            "  provider: deepseek-official\n"
            "  model: deepseek-v4-flash\n",
        )

    def test_invalid_kimi_reasoning_effort_fails_closed_to_max(self) -> None:
        with patch.dict(
            os.environ,
            {"KIMI_REASONING_EFFORT": "xhigh"},
            clear=True,
        ):
            document = runner._dsh_settings_document("kimi-k3")
        self.assertIn("reasoningEffort: max", document)
        self.assertNotIn("reasoningEffort: xhigh", document)


class KimiChatCompletionsProxyTests(unittest.TestCase):
    class _Upstream:
        def __init__(self, body: bytes) -> None:
            self.status = 200
            self.code = 200
            self.headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            }
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self, _size: int) -> bytes:
            body, self._body = self._body, b""
            return body

    def test_sanitizer_rewrites_developer_and_drops_unsupported_fields(self) -> None:
        payload, summary = kimi_chat_completions_proxy.sanitize_chat_payload(
            {
                "model": "flashflame/kimi-k3",
                "messages": [
                    {"role": "developer", "content": "Follow the proof contract."},
                    {"role": "user", "content": "Prove it."},
                ],
                "output_config": {"verbosity": "high"},
                "context_management": [{"type": "compaction"}],
                "service_tier": "priority",
                "reasoning": {"effort": "medium"},
                "max_completion_tokens": 4096,
            },
            reasoning_effort="high",
        )

        self.assertEqual(
            [message["role"] for message in payload["messages"]],
            ["system", "user"],
        )
        self.assertEqual(payload["model"], "kimi-k3")
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["max_tokens"], 4096)
        for field in (
            "output_config",
            "context_management",
            "service_tier",
            "reasoning",
            "max_completion_tokens",
        ):
            self.assertNotIn(field, payload)
        self.assertEqual(summary["rewritten_developer_messages"], 1)

    def test_proxy_forwards_bearer_auth_and_only_sanitized_json(self) -> None:
        secret = "fake-loopback-bearer"
        response_body = b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        with (
            patch.object(
                kimi_chat_completions_proxy.urlrequest,
                "urlopen",
                return_value=self._Upstream(response_body),
            ) as upstream,
            kimi_chat_completions_proxy.KimiChatCompletionsProxy(
                upstream_base="https://kimi.example/v1",
                reasoning_effort="max",
                upstream_api_key=secret,
                local_token="local-one-run-token",
            ) as proxy,
        ):
            parsed = urlsplit(proxy.base_url)
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
            request_body = json.dumps(
                {
                    "model": "kimi-k3",
                    "messages": [
                        {"role": "developer", "content": "system contract"},
                        {"role": "user", "content": "prove"},
                    ],
                    "verbosity": "high",
                    "reasoning_effort": "medium",
                }
            )
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=request_body,
                headers={
                    "Authorization": "Bearer local-one-run-token",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            summary = proxy.last_summary

        self.assertEqual(response.status, 200)
        forwarded = upstream.call_args.args[0]
        self.assertEqual(forwarded.full_url, "https://kimi.example/v1/chat/completions")
        self.assertEqual(forwarded.get_header("Authorization"), "Bearer " + secret)
        forwarded_body = json.loads(forwarded.data)
        self.assertEqual(forwarded_body["messages"][0]["role"], "system")
        self.assertEqual(forwarded_body["reasoning_effort"], "max")
        self.assertNotIn("verbosity", forwarded_body)
        self.assertNotIn(secret, json.dumps(summary))

    def test_proxy_rejects_missing_auth_without_contacting_upstream(self) -> None:
        with (
            patch.object(kimi_chat_completions_proxy.urlrequest, "urlopen") as upstream,
            kimi_chat_completions_proxy.KimiChatCompletionsProxy(
                upstream_base="https://kimi.example/v1",
                reasoning_effort="max",
                upstream_api_key="fake-upstream-key",
                local_token="local-one-run-token",
            ) as proxy,
        ):
            parsed = urlsplit(proxy.base_url)
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
            body = json.dumps(
                {"model": "kimi-k3", "messages": [{"role": "user", "content": "x"}]}
            )
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()

        self.assertEqual(response.status, 401)
        upstream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
