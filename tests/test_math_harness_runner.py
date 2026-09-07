"""Offline contract tests for the bounded Math Agent Harness adapter."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import engine_graph
from agent_monitor.runners import math_harness_runner as runner


PROBLEM = "Prove that the sum of two even integers is even."


def _proof(tag: str) -> str:
    body = (
        f"Let the two even integers be $a=2m$ and $b=2n$. {tag}. "
        "Then $a+b=2m+2n=2(m+n)$, and $m+n$ is an integer. "
        "Consequently the defining divisibility condition for evenness holds. "
        "This argument covers arbitrary integers, including zero and negative values. "
    )
    return "# Proof\n\n" + body * 3


def _plan(count: int) -> str:
    candidates = [
        {
            "label": f"Route {index}",
            "approach": f"Use independent characterization {index} of even integers.",
            "risks": f"Check integer closure in route {index}.",
        }
        for index in range(1, count + 1)
    ]
    return json.dumps(
        {"summary": "Check definitions and quantifiers.", "candidates": candidates}
    )


def _critic(verdict: str = "correct", selected: int = 1) -> str:
    if verdict == "correct":
        payload = {
            "verdict": "correct",
            "selected_candidate": selected,
            "summary": "The selected proof is complete and covers all integers.",
            "critical_errors": [],
            "gaps": [],
            "revision_instructions": "",
        }
    else:
        payload = {
            "verdict": "needs_revision",
            "selected_candidate": selected,
            "summary": "The core idea is useful but one implication needs repair.",
            "critical_errors": ["The converse was asserted without justification."],
            "gaps": ["Explain the final divisibility implication."],
            "revision_instructions": "Give the missing implication explicitly.",
        }
    return json.dumps(payload)


@contextmanager
def _in_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _api_run(
    workspace: Path,
    outputs: list[object],
    *,
    env: dict[str, str] | None = None,
    provider_sequence: list[str] | None = None,
    task: str | None = PROBLEM,
) -> tuple[int, list[dict], list[dict]]:
    calls: list[dict] = []
    events: list[dict] = []
    queue = list(outputs)
    providers = list(provider_sequence or [])

    def fake_api(system: str, user: str, *, model: str):
        calls.append({"system": system, "user": user, "model": model})
        if not queue:
            raise AssertionError("unexpected extra API call")
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        provider = providers.pop(0) if providers else "kimi"
        return SimpleNamespace(
            text=value,
            usage={"input_tokens": 7, "output_tokens": 11},
            model="kimi-k3" if provider == "kimi" else "gpt-test",
            provider=provider,
        )

    run_env = {
        "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2",
        "AGENT_MONITOR_MAX_ITERATIONS": "20",
        "AGENT_MONITOR_PROBLEM_ID": "case-1",
        **(env or {}),
    }
    with (
        _in_directory(workspace),
        patch.dict(runner.os.environ, run_env, clear=True),
        patch.object(
            runner.sys,
            "argv",
            (
                ["math_harness_runner.py"]
                if task is None
                else ["math_harness_runner.py", task]
            ),
        ),
        patch.object(runner, "codex_subscription_enabled", return_value=False),
        patch.object(runner, "claude_subscription_enabled", return_value=False),
        patch.object(runner, "api_chat", side_effect=fake_api),
        patch.object(runner, "emit", side_effect=events.append),
    ):
        returncode = runner.main()
    return returncode, calls, events


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class MathHarnessAcceptedArtifactTests(unittest.TestCase):
    def test_no_argument_uses_canonical_problem_file_without_argv_leak(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            (workspace / "problem.txt").write_text(PROBLEM, encoding="utf-8")
            returncode, calls, _events = _api_run(
                workspace,
                [_plan(2), _proof("one"), _proof("two"), _critic()],
                task=None,
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 4)
            self.assertIn(PROBLEM, calls[0]["user"])
            self.assertIn(runner.DEFAULT_TASK_DIRECTIVE, calls[0]["user"])
            receipt = json.loads(
                (
                    workspace
                    / "math_harness"
                    / "project"
                    / "engine_receipt.json"
                ).read_text()
            )
            self.assertEqual(
                receipt["problem_sha256"],
                hashlib.sha256(PROBLEM.encode()).hexdigest(),
            )
            self.assertEqual(
                receipt["task_sha256"],
                hashlib.sha256(runner.DEFAULT_TASK_DIRECTIVE.encode()).hexdigest(),
            )

    def test_exact_correct_verdict_writes_compatible_graph_and_bound_receipt(self) -> None:
        first = _proof("first candidate")
        second = _proof("selected second candidate")
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, events = _api_run(
                workspace,
                [_plan(2), first, second, _critic("correct", 2)],
            )

            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 4)
            proof_path = workspace / "proof.md"
            self.assertEqual(
                proof_path.read_text(encoding="utf-8"), second.rstrip() + "\n"
            )

            project = workspace / "math_harness" / "project"
            expected_memory = {
                "plan.jsonl",
                "proof_attempt.jsonl",
                "verification.jsonl",
                "edges.jsonl",
                "annotations.jsonl",
            }
            self.assertEqual(
                {path.name for path in (project / "global_memory").iterdir()},
                expected_memory,
            )
            self.assertEqual(
                (project / "PROBLEM.md").read_text(encoding="utf-8"),
                f"# Problem\n\n{PROBLEM}\n",
            )

            canonical = {
                "problem_id": "case-1",
                "predecessors": [],
                "glossary_introduces": {},
                "statement": PROBLEM,
                "proof": " ".join(second.split()),
            }
            expected_id = hashlib.sha256(
                json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            target_bytes = (expected_id + "\n").encode("ascii")
            self.assertEqual((project / "TARGET.md").read_bytes(), target_bytes)
            fact_path = project / "fact_graph" / "facts" / f"{expected_id}.md"
            fact_bytes = fact_path.read_bytes()
            self.assertIn(f"fact_id: {expected_id}".encode(), fact_bytes)
            self.assertIn(b"predecessors: []", fact_bytes)
            self.assertIn(b"glossary_introduces: {}", fact_bytes)
            self.assertIn(b"external_refs: []", fact_bytes)

            receipt_path = project / "engine_receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], 1)
            self.assertEqual(receipt["engine"], "math_harness")
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertEqual(receipt["critic_verdict"], "correct")
            self.assertIs(receipt["certificate"], False)
            self.assertEqual(receipt["target_fact_id"], expected_id)
            self.assertEqual(receipt["target_fact_ids"], [expected_id])
            self.assertEqual(receipt["provider"], "kimi")
            self.assertEqual(receipt["model"], "kimi-k3")
            self.assertEqual(receipt["problem_sha256"], hashlib.sha256(PROBLEM.encode()).hexdigest())
            self.assertEqual(receipt["task_sha256"], hashlib.sha256(PROBLEM.encode()).hexdigest())
            self.assertEqual(receipt["proof_sha256"], hashlib.sha256(proof_path.read_bytes()).hexdigest())
            self.assertEqual(receipt["fact_bytes_sha256"], hashlib.sha256(fact_bytes).hexdigest())
            self.assertEqual(receipt["target_sha256"], hashlib.sha256(target_bytes).hexdigest())
            self.assertEqual(receipt["fact_bytes"], len(fact_bytes))
            self.assertEqual(receipt["workflow"]["candidate_calls"], 2)
            self.assertIs(receipt["workflow"]["tools_enabled"], False)
            self.assertEqual(
                receipt["workflow"]["response_only_contract"],
                {"transport": "api_chat", "isolation_version": 1},
            )
            binding = {
                "source_sha256": receipt["source_sha256"],
                "problem_sha256": receipt["problem_sha256"],
                "task_sha256": receipt["task_sha256"],
                "proof_sha256": receipt["proof_sha256"],
                "provider": receipt["provider"],
                "model": receipt["model"],
                "verdict": receipt["verdict"],
                "critic_verdict": receipt["critic_verdict"],
                "critic_response_sha256": receipt["critic_response_sha256"],
                "target_fact_ids": receipt["target_fact_ids"],
                "target_sha256": receipt["target_sha256"],
                "fact_bytes_sha256": receipt["fact_bytes_sha256"],
                "certificate": False,
            }
            self.assertEqual(
                receipt["binding_sha256"],
                hashlib.sha256(
                    json.dumps(
                        binding,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            )
            self.assertEqual(
                receipt["source_sha256"],
                hashlib.sha256(Path(runner.__file__).read_bytes()).hexdigest(),
            )
            self.assertEqual(
                list(workspace.rglob("*receipt*.json")),
                [receipt_path],
                "the project receipt is the only authoritative receipt",
            )

            graph = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(graph["status"], "complete")
            self.assertEqual(graph["selection"]["target_ids"], [expected_id])
            self.assertEqual(graph["provenance"]["attestation"], "harness-gate-receipt")
            self.assertIs(graph["provenance"]["certificate"], False)

            from agent_monitor import jobs

            proof_before_analysis = proof_path.read_bytes()
            jobs._attach_analysis(
                {
                    "status": "finished",
                    "engine": "math_harness",
                    "model": "kimi-k3",
                    "agents": [],
                    "totals": {},
                    "pipeline": [],
                    "edges": [],
                },
                workspace,
            )
            self.assertEqual(proof_path.read_bytes(), proof_before_analysis)
            graph_after_analysis = engine_graph.build(
                workspace, {"engine": "math_harness"}
            )
            self.assertEqual(
                graph_after_analysis["provenance"]["attestation"],
                "harness-gate-receipt",
            )

            plans = _read_jsonl(project / "global_memory" / "plan.jsonl")
            attempts = _read_jsonl(project / "global_memory" / "proof_attempt.jsonl")
            verifications = _read_jsonl(project / "global_memory" / "verification.jsonl")
            edges = _read_jsonl(project / "global_memory" / "edges.jsonl")
            annotations = _read_jsonl(project / "global_memory" / "annotations.jsonl")
            self.assertEqual([row["kind"] for row in plans], ["plan", "plan"])
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[1]["status"], "verified")
            self.assertEqual(attempts[1]["fact_id"], expected_id)
            self.assertEqual(verifications[0]["fact_id"], expected_id)
            self.assertIn("promoted-to", {edge["type"] for edge in edges})
            self.assertEqual(annotations[0]["status"], "closed")

            all_files = [path for path in workspace.rglob("*") if path.is_file()]
            self.assertTrue(all_files)
            for path in all_files:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)

            event_types = [event.get("type") for event in events]
            self.assertIn("thread.started", event_types)
            self.assertTrue(
                any(
                    event.get("type") == "item.completed"
                    and (event.get("item") or {}).get("type") == "file_change"
                    for event in events
                )
            )
            self.assertIn("turn.completed", event_types)
            usage_events = [event for event in events if event.get("type") == "turn.completed"]
            self.assertEqual(len(usage_events), 4)
            self.assertEqual(
                sum(event["usage"]["input_tokens"] for event in usage_events),
                28,
            )
            self.assertEqual(
                sum(event["usage"]["output_tokens"] for event in usage_events),
                44,
            )
            self.assertEqual(
                [event["stage"] for event in usage_events],
                ["planner", "candidate-1", "candidate-2", "critic"],
            )

    def test_problem_txt_is_canonical_while_wrapped_argv_remains_task_context(self) -> None:
        wrapped_task = (
            "Your working directory is /tmp/example. Read problem.txt.\n\n"
            "PROBLEM:\n" + PROBLEM + "\n\nUse the supplied library if relevant."
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            (workspace / "problem.txt").write_text(PROBLEM + "\n", encoding="utf-8")
            returncode, calls, _events = _api_run(
                workspace,
                [_plan(2), _proof("one"), _proof("two"), _critic()],
                task=wrapped_task,
                env={"AGENT_MONITOR_PROBLEM_ID": "stable-problem-id"},
            )
            self.assertEqual(returncode, 0)
            self.assertIn(wrapped_task, calls[0]["user"])
            project = workspace / "math_harness" / "project"
            self.assertEqual(
                (project / "PROBLEM.md").read_text(encoding="utf-8"),
                f"# Problem\n\n{PROBLEM}\n",
            )
            receipt = json.loads((project / "engine_receipt.json").read_text())
            self.assertEqual(receipt["problem_id"], "stable-problem-id")
            self.assertEqual(
                receipt["problem_sha256"], hashlib.sha256(PROBLEM.encode()).hexdigest()
            )
            self.assertEqual(
                receipt["task_sha256"], hashlib.sha256(wrapped_task.encode()).hexdigest()
            )
            fact = next((project / "fact_graph" / "facts").glob("*.md")).read_text()
            self.assertIn("problem_id: stable-problem-id", fact)
            self.assertNotIn("Your working directory", fact)

    def test_archive_compatible_fact_id_is_whitespace_stable(self) -> None:
        first = runner.compute_fact_id(
            problem_id="P",
            predecessors=["bbbbbbbbbbbbbbbb", "aaaaaaaaaaaaaaaa"],
            glossary_introduces={"z": "last", "a": "first"},
            statement=" A\n B ",
            proof=" C\tD ",
        )
        second = runner.compute_fact_id(
            problem_id="P",
            predecessors=["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"],
            glossary_introduces={"a": "first", "z": "last"},
            statement="A B",
            proof="C D",
        )
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{16}$")


class MathHarnessPartialAndFailureTests(unittest.TestCase):
    def test_needs_revision_runs_once_and_remains_unattested_without_fact(self) -> None:
        revised = _proof("revised but not rechecked")
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [_plan(2), _proof("one"), _proof("two"), _critic("needs_revision"), revised],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 5)
            self.assertIn("Give the missing implication explicitly", calls[-1]["user"])
            self.assertEqual((workspace / "proof.md").read_text(), revised.rstrip() + "\n")
            project = workspace / "math_harness" / "project"
            self.assertFalse((project / "TARGET.md").exists())
            self.assertEqual(list((project / "fact_graph" / "facts").iterdir()), [])
            receipt = json.loads((project / "engine_receipt.json").read_text())
            self.assertEqual(receipt["verdict"], "unattested")
            self.assertEqual(receipt["critic_verdict"], "needs_revision")
            self.assertEqual(receipt["target_fact_ids"], [])
            self.assertIsNone(receipt["target_fact_id"])
            self.assertIsNone(receipt["fact_bytes_sha256"])
            self.assertEqual(receipt["workflow"]["revision_calls"], 1)
            self.assertEqual(
                _read_jsonl(project / "global_memory" / "annotations.jsonl")[0]["status"],
                "active",
            )

    def test_partial_continuation_removes_only_stale_target_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            accepted, _, _ = _api_run(
                workspace,
                [_plan(2), _proof("one"), _proof("two"), _critic("correct")],
            )
            self.assertEqual(accepted, 0)
            project = workspace / "math_harness" / "project"
            old_facts = list((project / "fact_graph" / "facts").glob("*.md"))
            self.assertEqual(len(old_facts), 1)

            partial, _, _ = _api_run(
                workspace,
                [
                    _plan(2),
                    _proof("new one"),
                    _proof("new two"),
                    _critic("needs_revision"),
                    _proof("new revision"),
                ],
            )
            self.assertEqual(partial, 0)
            self.assertFalse((project / "TARGET.md").exists())
            self.assertFalse(old_facts[0].exists())
            self.assertEqual(list((project / "fact_graph" / "facts").iterdir()), [])
            receipt = json.loads((project / "engine_receipt.json").read_text())
            self.assertEqual(receipt["verdict"], "unattested")
            graph = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(graph["status"], "none")
            self.assertNotEqual(
                graph["provenance"]["attestation"], "harness-gate-receipt"
            )

    def test_accepted_continuation_replaces_the_prior_generated_fact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            first_rc, _, _ = _api_run(
                workspace,
                [_plan(2), _proof("old selected"), _proof("old other"), _critic()],
            )
            self.assertEqual(first_rc, 0)
            project = workspace / "math_harness" / "project"
            old_fact = next((project / "fact_graph" / "facts").glob("*.md"))

            second_rc, _, _ = _api_run(
                workspace,
                [_plan(2), _proof("new selected"), _proof("new other"), _critic()],
            )
            self.assertEqual(second_rc, 0)
            new_facts = list((project / "fact_graph" / "facts").glob("*.md"))
            self.assertEqual(len(new_facts), 1)
            self.assertNotEqual(new_facts[0].name, old_fact.name)
            self.assertFalse(old_fact.exists())
            target = (project / "TARGET.md").read_text().strip()
            receipt = json.loads((project / "engine_receipt.json").read_text())
            self.assertEqual(target, new_facts[0].stem)
            self.assertEqual(receipt["target_fact_ids"], [target])

    def test_malformed_planner_critic_and_inconsistent_correct_write_nothing(self) -> None:
        inconsistent = json.dumps(
            {
                "verdict": "correct",
                "selected_candidate": 1,
                "summary": "claims success",
                "critical_errors": ["but has an error"],
                "gaps": [],
                "revision_instructions": "",
            }
        )
        cases = (
            ("planner", ["not-json"]),
            ("critic", [_plan(2), _proof("one"), _proof("two"), "not-json"]),
            ("capitalized", [_plan(2), _proof("one"), _proof("two"), _critic().replace("correct", "Correct", 1)]),
            ("inconsistent", [_plan(2), _proof("one"), _proof("two"), inconsistent]),
        )
        for label, outputs in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                returncode, _calls, events = _api_run(workspace, outputs)
                self.assertEqual(returncode, 1)
                self.assertFalse((workspace / "proof.md").exists())
                self.assertFalse((workspace / "math_harness").exists())
                self.assertTrue(
                    any(
                        event.get("type") == "item.completed"
                        and (event.get("item") or {}).get("type") == "error"
                        for event in events
                    )
                )

    def test_provider_error_and_provider_change_fail_without_fallback_or_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [runner.APIBackendError("provider unavailable")],
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(list(workspace.iterdir()), [])

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, _calls, _events = _api_run(
                workspace,
                [_plan(2), _proof("one"), _proof("two"), _critic()],
                provider_sequence=["kimi", "openai", "kimi", "kimi"],
            )
            self.assertEqual(returncode, 1)
            self.assertFalse((workspace / "proof.md").exists())
            self.assertFalse((workspace / "math_harness").exists())

    def test_short_pseudo_tool_and_oversized_outputs_are_rejected(self) -> None:
        cases = (
            ("short", [_plan(2), "too short"]),
            (
                "pseudo-tool",
                [_plan(2), _proof("one") + "\n<function_calls>bad</function_calls>"],
            ),
            ("oversized-plan", ["x" * (runner.MAX_PLAN_BYTES + 1)]),
        )
        for label, outputs in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                returncode, _calls, _events = _api_run(workspace, outputs)
                self.assertEqual(returncode, 1)
                self.assertEqual(list(workspace.iterdir()), [])

    def test_oversized_problem_is_rejected_before_any_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            calls: list[object] = []
            with (
                _in_directory(workspace),
                patch.object(
                    runner.sys,
                    "argv",
                    ["math_harness_runner.py", "x" * (runner.MAX_PROBLEM_BYTES + 1)],
                ),
                patch.object(runner, "api_chat", side_effect=lambda *a, **k: calls.append(a)),
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            self.assertEqual(calls, [])
            self.assertEqual(list(workspace.iterdir()), [])

    def test_oversized_problem_file_and_task_fail_before_any_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            (workspace / "problem.txt").write_text(
                "x" * (runner.MAX_PROBLEM_BYTES + 1), encoding="utf-8"
            )
            calls: list[object] = []
            with (
                _in_directory(workspace),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "api_chat", side_effect=lambda *a, **k: calls.append(a)),
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            self.assertEqual(calls, [])

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            calls = []
            with (
                _in_directory(workspace),
                patch.object(
                    runner.sys,
                    "argv",
                    ["runner", "x" * (runner.MAX_TASK_BYTES + 1)],
                ),
                patch.object(runner, "api_chat", side_effect=lambda *a, **k: calls.append(a)),
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 2)
            self.assertEqual(calls, [])


class MathHarnessProviderRoutingTests(unittest.TestCase):
    def test_codex_route_fails_closed_without_backend_isolation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=True),
                patch.object(runner, "claude_subscription_enabled", return_value=False),
                patch.object(
                    runner._codex_backend,
                    "RESPONSE_ONLY_TOOL_ISOLATION_VERSION",
                    0,
                ),
                patch.object(runner, "codex_exec") as codex,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            codex.assert_not_called()
            api.assert_not_called()
            self.assertEqual(list(workspace.iterdir()), [])

    def test_codex_backend_command_is_tool_free_and_workspace_isolated(self) -> None:
        captured: dict[str, object] = {}

        def fake_process(command, *, input, timeout, env, cwd):
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    runner._codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION + "\n",
                    "",
                )
            if command[1:] == ["debug", "models", "--bundled"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "models": [
                                {
                                    "slug": "gpt-5.6-sol",
                                    "apply_patch_tool_type": "freeform",
                                    "web_search_tool_type": "web_search",
                                    "supports_search_tool": True,
                                }
                            ]
                        }
                    ),
                    "",
                )
            captured.update(
                command=list(command),
                input=input,
                timeout=timeout,
                env=dict(env),
                cwd=cwd,
            )
            catalog_setting = next(
                item
                for item in command
                if item.startswith("model_catalog_json=")
            )
            catalog_path = Path(json.loads(catalog_setting.split("=", 1)[1]))
            captured["catalog"] = json.loads(catalog_path.read_text(encoding="utf-8"))
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("response", encoding="utf-8")
            return subprocess.CompletedProcess(
                command,
                0,
                '{"type":"turn.completed","usage":{"input_tokens":3}}\n',
                "",
            )

        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as home_raw,
            patch.object(
                runner._codex_backend.shutil,
                "which",
                return_value="/usr/local/bin/codex",
            ),
            patch.object(
                runner._codex_backend,
                "_run_codex_process",
                side_effect=fake_process,
            ),
        ):
            workspace = Path(raw)
            result = runner._codex_backend.codex_exec(
                "system",
                "user",
                model="gpt-5.6-sol",
                workspace=workspace,
                sandbox="read-only",
                enable_tools=False,
                source_env={
                    "CODEX_HOME": home_raw,
                    "PATH": os.defpath,
                    "AGENT_MONITOR_CODEX_LEGACY_LANDLOCK": "0",
                },
            )
        self.assertEqual(result.text, "response")
        command = captured["command"]
        self.assertIsInstance(command, list)
        assert isinstance(command, list)
        self.assertEqual(
            runner._codex_backend.RESPONSE_ONLY_TOOL_ISOLATION_VERSION,
            4,
        )
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--strict-config", command)
        self.assertIn("--ephemeral", command)
        for capability in (
            "apps",
            "artifact",
            "auth_elicitation",
            "browser_use",
            "browser_use_external",
            "browser_use_full_cdp_access",
            "code_mode",
            "code_mode_host",
            "computer_use",
            "goals",
            "hooks",
            "image_generation",
            "in_app_browser",
            "memories",
            "multi_agent",
            "network_proxy",
            "plugins",
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
        ):
            self.assertTrue(
                any(
                    command[index : index + 2] == ["--disable", capability]
                    for index in range(len(command) - 1)
                ),
                capability,
            )
        self.assertIn('web_search="disabled"', command)
        self.assertIn("tools.web_search=false", command)
        self.assertIn("tools.update_plan.enabled=false", command)
        self.assertIn(
            "tools.experimental_request_user_input.enabled=false",
            command,
        )
        catalog = captured["catalog"]
        self.assertIsInstance(catalog, dict)
        assert isinstance(catalog, dict)
        self.assertEqual(len(catalog["models"]), 1)
        catalog_model = catalog["models"][0]
        self.assertNotIn("apply_patch_tool_type", catalog_model)
        self.assertNotIn("web_search_tool_type", catalog_model)
        self.assertIs(catalog_model["supports_search_tool"], False)
        self.assertIs(catalog_model["supports_parallel_tool_calls"], False)
        self.assertNotIn("--cd", command)
        self.assertNotEqual(Path(str(captured["cwd"])), workspace.resolve())
        self.assertTrue(Path(str(captured["cwd"])).name.startswith("agent-monitor-codex-"))

    def test_codex_route_is_read_only_tool_free_and_never_falls_back(self) -> None:
        outputs = [_plan(1), _proof("codex"), _critic()]
        calls: list[dict] = []
        events: list[dict] = []

        def fake_codex(system: str, user: str, **kwargs):
            calls.append({"system": system, "user": user, **kwargs})
            kwargs["emit_event"](
                {"type": "item.completed", "item": {"type": "reasoning", "text": "native"}}
            )
            return SimpleNamespace(text=outputs.pop(0), usage={"input_tokens": 1})

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CODEX_MODEL": "gpt-5.6-sol",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=True),
                patch.object(runner, "claude_subscription_enabled", return_value=False),
                patch.object(runner, "codex_exec", side_effect=fake_codex),
                patch.object(runner, "claude_code_exec") as claude,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit", side_effect=events.append),
            ):
                self.assertEqual(runner.main(), 0)
            self.assertEqual(len(calls), 3)
            for call in calls:
                self.assertEqual(call["sandbox"], "read-only")
                self.assertIs(call["enable_tools"], False)
                self.assertIs(call["source_env"], runner.os.environ)
                self.assertEqual(call["model"], "gpt-5.6-sol")
            claude.assert_not_called()
            api.assert_not_called()
            receipt = json.loads(
                (workspace / "math_harness" / "project" / "engine_receipt.json").read_text()
            )
            self.assertEqual(receipt["provider"], "codex_subscription")
            self.assertEqual(
                receipt["workflow"]["response_only_contract"],
                {"transport": "codex_exec", "isolation_version": 4},
            )
            self.assertTrue(any((event.get("item") or {}).get("text") == "native" for event in events))

    def test_claude_route_is_single_turn_tool_free_and_projects_events(self) -> None:
        outputs = [_plan(1), _proof("claude"), _critic()]
        calls: list[dict] = []
        events: list[dict] = []

        def fake_claude(system: str, user: str, **kwargs):
            calls.append({"system": system, "user": user, **kwargs})
            kwargs["emit_event"](
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "claude native"}]},
                }
            )
            return SimpleNamespace(
                text=outputs.pop(0),
                usage={"output_tokens": 2},
                model="claude-sonnet-resolved",
            )

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_MODEL": "sonnet",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=False),
                patch.object(runner, "claude_subscription_enabled", return_value=True),
                patch.object(runner, "claude_code_exec", side_effect=fake_claude),
                patch.object(runner, "codex_exec") as codex,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit", side_effect=events.append),
            ):
                self.assertEqual(runner.main(), 0)
            self.assertEqual(len(calls), 3)
            for call, expected_timeout in zip(calls, (600.0, 1200.0, 600.0)):
                self.assertIs(call["enable_tools"], False)
                self.assertEqual(call["max_turns"], 1)
                self.assertEqual(call["timeout"], expected_timeout)
                self.assertIs(call["source_env"], runner.os.environ)
                self.assertNotIn("workspace", call)
            codex.assert_not_called()
            api.assert_not_called()
            receipt = json.loads(
                (workspace / "math_harness" / "project" / "engine_receipt.json").read_text()
            )
            self.assertEqual(receipt["provider"], "claude_subscription")
            self.assertEqual(receipt["model"], "claude-sonnet-resolved")
            self.assertEqual(
                receipt["workflow"]["response_only_contract"],
                {"transport": "claude_code", "isolation_version": 1},
            )
            self.assertEqual(
                runner._claude_backend.RESPONSE_ONLY_TOOL_ISOLATION_VERSION,
                1,
            )
            self.assertTrue(
                any((event.get("item") or {}).get("text") == "claude native" for event in events)
            )

    def test_claude_failure_preserves_completed_stage_usage_without_retry(self) -> None:
        calls: list[dict] = []
        events: list[dict] = []

        def fake_claude(system: str, user: str, **kwargs):
            calls.append({"system": system, "user": user, **kwargs})
            if len(calls) == 1:
                return SimpleNamespace(
                    text=_plan(1),
                    usage={"input_tokens": 13, "output_tokens": 5},
                    model="claude-fable-resolved",
                )
            raise runner.ClaudeBackendError("Claude Code timed out after 1200s")

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_MODEL": "fable",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=False),
                patch.object(runner, "claude_subscription_enabled", return_value=True),
                patch.object(runner, "claude_code_exec", side_effect=fake_claude),
                patch.object(runner, "emit", side_effect=events.append),
            ):
                self.assertEqual(runner.main(), 1)

            self.assertEqual(len(calls), 2, "a timed-out stage is never replayed")
            self.assertEqual([call["timeout"] for call in calls], [600.0, 1200.0])
            usage_events = [event for event in events if event.get("type") == "turn.completed"]
            self.assertEqual(
                usage_events,
                [
                    {
                        "type": "turn.completed",
                        "stage": "planner",
                        "usage": {
                            "input_tokens": 13,
                            "output_tokens": 5,
                            "cached_input_tokens": 0,
                        },
                    }
                ],
            )
            self.assertFalse((workspace / "proof.md").exists())

    def test_claude_stage_timeout_override_precedence_and_bounds(self) -> None:
        self.assertEqual(runner._claude_stage_timeout("candidate-2", {}), 1200.0)
        self.assertEqual(runner._claude_stage_timeout("critic", {}), 600.0)
        self.assertEqual(
            runner._claude_stage_timeout(
                "candidate-2",
                {
                    "AGENT_MONITOR_CLAUDE_TIMEOUT": "700",
                    "AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT": "900",
                    "AGENT_MONITOR_MATH_HARNESS_CLAUDE_CANDIDATE_TIMEOUT": "1500",
                },
            ),
            1500.0,
        )
        self.assertEqual(
            runner._claude_stage_timeout(
                "planner", {"AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT": "1"}
            ),
            60.0,
        )
        self.assertEqual(
            runner._claude_stage_timeout(
                "revision", {"AGENT_MONITOR_CLAUDE_TIMEOUT": "99999"}
            ),
            3600.0,
        )
        with self.assertRaisesRegex(runner.MathHarnessError, "must be a number"):
            runner._claude_stage_timeout(
                "planner", {"AGENT_MONITOR_CLAUDE_TIMEOUT": "invalid"}
            )

    def test_invalid_claude_timeout_fails_precisely_before_provider_call(self) -> None:
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_MODEL": "fable",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
                "AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT": "invalid",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=False),
                patch.object(runner, "claude_subscription_enabled", return_value=True),
                patch.object(runner, "claude_code_exec") as claude,
                patch.object(runner, "emit", side_effect=events.append),
            ):
                self.assertEqual(runner.main(), 1)

        claude.assert_not_called()
        terminal = [
            event["item"]
            for event in events
            if event.get("type") == "item.completed"
            and (event.get("item") or {}).get("terminal") is True
        ]
        self.assertEqual(len(terminal), 1)
        self.assertIn("must be a number", terminal[0]["message"])

    def test_claude_route_fails_closed_without_backend_isolation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=False),
                patch.object(runner, "claude_subscription_enabled", return_value=True),
                patch.object(
                    runner._claude_backend,
                    "RESPONSE_ONLY_TOOL_ISOLATION_VERSION",
                    0,
                ),
                patch.object(runner, "claude_code_exec") as claude,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            claude.assert_not_called()
            api.assert_not_called()
            self.assertEqual(list(workspace.iterdir()), [])

    def test_codex_error_does_not_fallback_to_api_or_claude(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            env = {
                "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
            }
            with (
                _in_directory(workspace),
                patch.dict(runner.os.environ, env, clear=True),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=True),
                patch.object(runner, "claude_subscription_enabled", return_value=False),
                patch.object(
                    runner,
                    "codex_exec",
                    side_effect=runner.CodexBackendError("account unavailable"),
                ) as codex,
                patch.object(runner, "claude_code_exec") as claude,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            codex.assert_called_once()
            claude.assert_not_called()
            api.assert_not_called()
            self.assertEqual(list(workspace.iterdir()), [])

    def test_conflicting_subscription_routes_fail_before_backend_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            with (
                _in_directory(workspace),
                patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                patch.object(runner, "codex_subscription_enabled", return_value=True),
                patch.object(runner, "claude_subscription_enabled", return_value=True),
                patch.object(runner, "codex_exec") as codex,
                patch.object(runner, "claude_code_exec") as claude,
                patch.object(runner, "api_chat") as api,
                patch.object(runner, "emit"),
            ):
                self.assertEqual(runner.main(), 1)
            codex.assert_not_called()
            claude.assert_not_called()
            api.assert_not_called()


class MathHarnessBoundsAndSecurityTests(unittest.TestCase):
    def test_candidate_count_is_bounded_by_workers_and_iterations(self) -> None:
        self.assertEqual(
            runner.candidate_count(
                {
                    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "99",
                    "AGENT_MONITOR_MAX_ITERATIONS": "2",
                }
            ),
            2,
        )
        self.assertEqual(
            runner.candidate_count(
                {
                    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "3",
                    "AGENT_MONITOR_MAX_ITERATIONS": "0",
                }
            ),
            1,
        )
        self.assertEqual(
            runner.candidate_count({"MATH_HARNESS_MAX_ITERATIONS": "1"}),
            1,
        )
        with self.assertRaises(runner.MathHarnessError):
            runner.candidate_count(
                {"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "many"}
            )

    def test_three_candidate_call_and_prompt_bounds_and_independence(self) -> None:
        proofs = [_proof("unique-A"), _proof("unique-B"), _proof("unique-C")]
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            returncode, calls, _events = _api_run(
                workspace,
                [_plan(3), *proofs, _critic("correct", 3)],
                env={
                    "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "500",
                    "AGENT_MONITOR_MAX_ITERATIONS": "3",
                },
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 5)
            self.assertTrue(
                all(len(call["user"].encode("utf-8")) <= runner.MAX_MODEL_PROMPT_BYTES for call in calls)
            )
            candidate_calls = calls[1:4]
            self.assertIn("Label: Route 1", candidate_calls[0]["user"])
            self.assertNotIn("Label: Route 2", candidate_calls[0]["user"])
            self.assertNotIn("unique-A", candidate_calls[1]["user"])
            self.assertNotIn("unique-B", candidate_calls[2]["user"])
            critic_prompt = calls[4]["user"]
            self.assertNotIn("[bounded excerpt omitted]", critic_prompt)
            for marker in ("unique-A", "unique-B", "unique-C"):
                self.assertIn(marker, critic_prompt)
            receipt = json.loads(
                (workspace / "math_harness" / "project" / "engine_receipt.json").read_text()
            )
            self.assertEqual(receipt["workflow"]["candidate_calls"], 3)
            self.assertEqual(len(receipt["calls"]), 5)

    def test_static_symlink_attacks_fail_before_any_model_call(self) -> None:
        for target_name in ("math_harness", "proof.md", "problem.txt"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside_raw:
                workspace = Path(raw)
                outside = Path(outside_raw)
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged", encoding="utf-8")
                target = workspace / target_name
                if target_name == "math_harness":
                    target.symlink_to(outside, target_is_directory=True)
                else:
                    target.symlink_to(sentinel)
                calls: list[object] = []
                with (
                    _in_directory(workspace),
                    patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                    patch.object(runner, "codex_subscription_enabled", return_value=False),
                    patch.object(runner, "claude_subscription_enabled", return_value=False),
                    patch.object(runner, "api_chat", side_effect=lambda *a, **k: calls.append(a)),
                    patch.object(runner, "emit"),
                ):
                    self.assertEqual(runner.main(), 1)
                self.assertEqual(calls, [])
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_symlinked_nested_memory_and_fact_directories_are_rejected(self) -> None:
        for branch in ("global_memory", "fact_graph"):
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside_raw:
                workspace = Path(raw)
                project = workspace / "math_harness" / "project"
                project.mkdir(parents=True)
                (project / branch).symlink_to(Path(outside_raw), target_is_directory=True)
                calls: list[object] = []
                with (
                    _in_directory(workspace),
                    patch.object(runner.sys, "argv", ["runner", PROBLEM]),
                    patch.object(runner, "codex_subscription_enabled", return_value=False),
                    patch.object(runner, "claude_subscription_enabled", return_value=False),
                    patch.object(runner, "api_chat", side_effect=lambda *a, **k: calls.append(a)),
                    patch.object(runner, "emit"),
                ):
                    self.assertEqual(runner.main(), 1)
                self.assertEqual(calls, [])

    def test_unexpected_fact_entry_fails_before_any_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            facts = (
                workspace
                / "math_harness"
                / "project"
                / "fact_graph"
                / "facts"
            )
            facts.mkdir(parents=True)
            (facts / "README.md").write_text("user material", encoding="utf-8")
            returncode, calls, _events = _api_run(
                workspace,
                [_plan(2), _proof("one"), _proof("two"), _critic()],
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(calls, [])
            self.assertEqual(
                (facts / "README.md").read_text(encoding="utf-8"),
                "user material",
            )

    def test_interrupted_adapter_fact_temp_is_cleaned_before_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            facts = (
                workspace
                / "math_harness"
                / "project"
                / "fact_graph"
                / "facts"
            )
            facts.mkdir(parents=True)
            orphan = facts / (
                ".0123456789abcdef.md.tmp-"
                "0123456789abcdef01234567"
            )
            orphan.write_bytes(b"interrupted adapter write")
            orphan.chmod(0o600)
            returncode, calls, _events = _api_run(
                workspace,
                [_plan(2), _proof("one"), _proof("two"), _critic()],
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(len(calls), 4)
            self.assertFalse(orphan.exists())
            graph = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(graph["status"], "complete")
            self.assertEqual(
                graph["provenance"]["attestation"],
                "harness-gate-receipt",
            )

    def test_corrupt_prior_fact_is_not_classified_as_adapter_owned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            first, _calls, _events = _api_run(
                workspace,
                [_plan(2), _proof("old one"), _proof("old two"), _critic()],
            )
            self.assertEqual(first, 0)
            fact = next(
                (
                    workspace
                    / "math_harness"
                    / "project"
                    / "fact_graph"
                    / "facts"
                ).glob("*.md")
            )
            original = fact.read_bytes()
            fact.write_bytes(original + b"\ncorruption\n")
            second, calls, _events = _api_run(
                workspace,
                [_plan(2), _proof("new one"), _proof("new two"), _critic()],
            )
            self.assertEqual(second, 1)
            self.assertEqual(calls, [])
            self.assertEqual(fact.read_bytes(), original + b"\ncorruption\n")

    def test_prior_receipt_with_wrong_isolation_contract_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            first, _calls, _events = _api_run(
                workspace,
                [_plan(2), _proof("old one"), _proof("old two"), _critic()],
            )
            self.assertEqual(first, 0)
            receipt_path = (
                workspace
                / "math_harness"
                / "project"
                / "engine_receipt.json"
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["workflow"]["response_only_contract"]["isolation_version"] = 99
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            second, calls, _events = _api_run(
                workspace,
                [_plan(2), _proof("new one"), _proof("new two"), _critic()],
            )
            self.assertEqual(second, 1)
            self.assertEqual(calls, [])
            self.assertEqual(
                json.loads(receipt_path.read_text())["workflow"][
                    "response_only_contract"
                ]["isolation_version"],
                99,
            )

    def test_atomic_write_stays_on_open_parent_during_directory_swap(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as outside_raw,
        ):
            workspace = Path(raw)
            project = workspace / "math_harness" / "project"
            project.mkdir(parents=True)
            moved = workspace / "math_harness" / "project-original"
            outside = Path(outside_raw)
            real_replace = os.replace
            swapped = False

            def swap_then_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    os.rename(project, moved)
                    project.symlink_to(outside, target_is_directory=True)
                return real_replace(
                    src,
                    dst,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with patch.object(runner.os, "replace", side_effect=swap_then_replace):
                runner._atomic_write(
                    workspace,
                    ("math_harness", "project", "PROBLEM.md"),
                    b"safe bytes",
                )
            self.assertFalse((outside / "PROBLEM.md").exists())
            self.assertEqual((moved / "PROBLEM.md").read_bytes(), b"safe bytes")

    def test_remove_regular_stays_on_open_parent_during_directory_swap(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as outside_raw,
        ):
            workspace = Path(raw)
            project = workspace / "math_harness" / "project"
            project.mkdir(parents=True)
            (project / "TARGET.md").write_text("generated", encoding="utf-8")
            moved = workspace / "math_harness" / "project-original"
            outside = Path(outside_raw)
            outside_target = outside / "TARGET.md"
            outside_target.write_text("sentinel", encoding="utf-8")
            real_unlink = os.unlink
            swapped = False

            def swap_then_unlink(path, *, dir_fd=None):
                nonlocal swapped
                if not swapped and path == "TARGET.md":
                    swapped = True
                    os.rename(project, moved)
                    project.symlink_to(outside, target_is_directory=True)
                return real_unlink(path, dir_fd=dir_fd)

            with patch.object(runner.os, "unlink", side_effect=swap_then_unlink):
                runner._remove_regular(
                    workspace,
                    ("math_harness", "project", "TARGET.md"),
                )
            self.assertEqual(outside_target.read_text(encoding="utf-8"), "sentinel")
            self.assertFalse((moved / "TARGET.md").exists())

    def test_prior_fact_scan_rejects_a_symlinked_project(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as outside_raw,
        ):
            workspace = Path(raw)
            (workspace / "math_harness").mkdir()
            (workspace / "math_harness" / "project").symlink_to(
                Path(outside_raw),
                target_is_directory=True,
            )
            with self.assertRaises(runner.MathHarnessError):
                runner._prior_generated_fact_ids(workspace)

    def test_runner_never_imports_archive_or_invokes_processes(self) -> None:
        source = inspect.getsource(runner)
        self.assertNotIn("from danus", source)
        self.assertNotIn("import danus", source)
        self.assertNotIn("Math_Agent_Harness-main", source)
        self.assertNotIn("import subprocess", source)
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            with (
                patch.object(subprocess, "Popen") as popen,
                patch.object(subprocess, "run") as process_run,
            ):
                returncode, _calls, _events = _api_run(
                    workspace,
                    [_plan(2), _proof("one"), _proof("two"), _critic()],
                )
            self.assertEqual(returncode, 0)
            popen.assert_not_called()
            process_run.assert_not_called()

    def test_json_contract_accepts_only_a_whole_object_or_single_fence(self) -> None:
        value = runner._json_object("```json\n{\"x\": 1}\n```", stage="test")
        self.assertEqual(value, {"x": 1})
        for malformed in (
            "prefix {\"x\": 1}",
            "{\"x\": 1} suffix",
            "```json\n{\"x\": 1}\n```\nextra",
            "[1, 2]",
        ):
            with self.subTest(payload=malformed), self.assertRaises(runner.MathHarnessError):
                runner._json_object(malformed, stage="test")


if __name__ == "__main__":
    unittest.main()
