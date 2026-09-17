from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_monitor import console_server, engine_graph, proof_graph
from agent_monitor.runners import math_harness_runner


def _fact_text(
    *,
    problem_id: str,
    statement: str,
    proof: str,
    predecessors: tuple[str, ...] = (),
    glossary: dict[str, str] | None = None,
    author: str = "worker-1",
    intuition: str = "",
) -> tuple[str, str]:
    glossary = glossary or {}
    canonical = {
        "problem_id": problem_id,
        "predecessors": sorted(predecessors),
        "glossary_introduces": dict(
            sorted((str(key), str(value)) for key, value in glossary.items())
        ),
        "statement": " ".join(statement.split()),
        "proof": " ".join(proof.split()),
    }
    fact_id = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    lines = [
        "---",
        f"fact_id: {fact_id}",
        f"problem_id: {problem_id}",
        f"author: {author}",
        f"predecessors: [{', '.join(predecessors)}]",
    ]
    if glossary:
        lines.append("glossary_introduces:")
        lines.extend(f"  {key}: {glossary[key]}" for key in sorted(glossary))
    else:
        lines.append("glossary_introduces: {}")
    lines.extend(
        [
            "external_refs: []",
            "---",
            "",
            "## statement",
            statement,
            "",
            "## proof",
            proof,
        ]
    )
    if intuition:
        lines.extend(["", "## intuition", intuition])
    return fact_id, "\n".join(lines) + "\n"


def _project(workspace: Path) -> tuple[Path, Path]:
    project = workspace / "math_harness" / "project"
    facts = project / "fact_graph" / "facts"
    facts.mkdir(parents=True)
    return project, facts


def _write_bound_receipt(
    workspace: Path,
    project: Path,
    *,
    fact_id: str,
    fact_payload: str,
    problem: str,
    proof: str,
) -> dict[str, object]:
    target_payload = f"{fact_id}\n".encode("ascii")
    proof_payload = proof.rstrip().encode("utf-8") + b"\n"
    problem_payload = problem.strip().encode("utf-8")
    fact_bytes = fact_payload.encode("utf-8")
    (project / "TARGET.md").write_bytes(target_payload)
    (project / "PROBLEM.md").write_bytes(b"# Problem\n\n" + problem_payload + b"\n")
    (workspace / "proof.md").write_bytes(proof_payload)
    receipt: dict[str, object] = {
        "schema_version": 1,
        "engine": "math_harness",
        "source": "agent-monitor-math-harness-response-adapter@1",
        "source_sha256": "1" * 64,
        "task_sha256": "2" * 64,
        "critic_response_sha256": "3" * 64,
        "semantics": "model-critic",
        "verdict": "accepted",
        "critic_verdict": "correct",
        "critic": "response-only-model-critic",
        "certificate": False,
        "problem_id": "demo",
        "problem_sha256": hashlib.sha256(problem_payload).hexdigest(),
        "proof_sha256": hashlib.sha256(proof_payload).hexdigest(),
        "provider": "offline-test",
        "model": "offline-test-model",
        "requested_model": "offline-test-model",
        "transport": "api_chat",
        "target_fact_id": fact_id,
        "target_fact_ids": [fact_id],
        "target_sha256": hashlib.sha256(target_payload).hexdigest(),
        "fact_path": f"fact_graph/facts/{fact_id}.md",
        "fact_bytes": len(fact_bytes),
        "fact_bytes_sha256": hashlib.sha256(fact_bytes).hexdigest(),
        "workflow": {
            "tools_enabled": False,
            "response_only_contract": {"transport": "api_chat", "isolation_version": 1},
        },
        "calls": [{"stage": "critic", "model": "offline-test-model"}],
    }
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
        "certificate": receipt["certificate"],
    }
    receipt["binding_sha256"] = hashlib.sha256(
        json.dumps(
            binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    (project / "engine_receipt.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    return receipt


def _write_runner_generation(
    workspace: Path, *, accepted: bool = True
) -> Path:
    runner = math_harness_runner
    problem = "Show that every even integer is divisible by two."
    plans = [
        {"label": "definition", "approach": "Unfold evenness.", "risks": "None."},
        {"label": "divisibility", "approach": "Use a witness.", "risks": "Check signs."},
    ]
    candidates = [
        "If n is even, n = 2k for an integer k.",
        "Take the integer witness k from the definition of evenness.",
    ]
    replies = [
        ("planner", runner.Reply("planner", {}, "offline", "offline-model")),
        (
            "candidate-1",
            runner.Reply(candidates[0], {}, "offline", "offline-model"),
        ),
        (
            "candidate-2",
            runner.Reply(candidates[1], {}, "offline", "offline-model"),
        ),
        ("critic", runner.Reply("critic", {}, "offline", "offline-model")),
    ]
    if accepted:
        critic = runner.Critic("correct", 2, "Accepted.", (), (), "")
        final_proof = candidates[1]
        revised = False
    else:
        critic = runner.Critic(
            "needs_revision",
            2,
            "A bounded revision remains unattested.",
            ("One critical error remains.",),
            ("One justification is missing.",),
            "Repair the missing justification.",
        )
        final_proof = "A bounded revised proof that remains unattested."
        revised = True
        replies.append(
            ("revision", runner.Reply(final_proof, {}, "offline", "offline-model"))
        )
    previous = Path.cwd()
    os.chdir(workspace)
    try:
        runner._write_artifacts(
            problem=problem,
            task=problem,
            plan_summary="Two bounded approaches.",
            plans=plans,
            candidates=candidates,
            critic=critic,
            final_proof=final_proof,
            revised=revised,
            route=runner.Route("offline", "offline-model", "api_chat"),
            replies=replies,
            prior_fact_ids=(),
        )
    finally:
        os.chdir(previous)
    return workspace / "math_harness" / "project"


class EngineGraphAdapterTests(unittest.TestCase):
    def test_workflow_view_projects_real_runner_memory_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project = _write_runner_generation(workspace)
            run = {"run_id": "workflow-1", "engine": "math_harness"}

            facts = engine_graph.rebuild(workspace, run, view="facts")
            workflow = engine_graph.rebuild(workspace, run, view="workflow")
            automatic = engine_graph.build(workspace, run, view="auto")

            self.assertEqual(facts["graph_role"], "fact-dependency")
            self.assertEqual(workflow["graph_role"], "workflow")
            self.assertEqual(automatic, workflow)
            self.assertEqual(workflow["status"], "complete")
            self.assertEqual(workflow["selection"]["target_ids"], ["goal:root"])
            self.assertNotEqual(workflow["source_sha256"], facts["source_sha256"])
            self.assertIs(workflow["provenance"]["proof_logic"], False)
            self.assertIs(workflow["provenance"]["certificate"], False)
            self.assertEqual(
                workflow["provenance"]["attestation"], "unattested"
            )
            self.assertIs(
                workflow["provenance"]["memory_receipt_bound"], False
            )
            self.assertEqual(
                workflow["provenance"]["response_only_transport"], "api_chat"
            )
            self.assertEqual(
                workflow["provenance"]["response_only_isolation_version"], 1
            )
            workflow_kinds = {
                node["meta"]["workflow_kind"]
                for node in workflow["graph"]["nodes"]
            }
            self.assertEqual(
                workflow_kinds,
                {"goal", "plan", "proof_attempt", "verification", "fact"},
            )
            edge_types = {edge["label"] for edge in workflow["graph"]["edges"]}
            self.assertTrue(
                {
                    "addresses",
                    "supports",
                    "promoted-to",
                    "critic-input",
                    "critic-verdict",
                }.issubset(edge_types)
            )

            fact_cache = workspace / engine_graph.CACHE_FILENAME
            workflow_cache = workspace / engine_graph.WORKFLOW_CACHE_FILENAME
            self.assertTrue(fact_cache.is_file())
            self.assertTrue(workflow_cache.is_file())
            self.assertEqual(stat.S_IMODE(workflow_cache.stat().st_mode), 0o600)
            self.assertEqual(
                engine_graph.load_cached(workspace, view="workflow"), workflow
            )
            self.assertEqual(engine_graph.load_cached(workspace), facts)

            before_hash = workflow["source_sha256"]
            plan_path = project / "global_memory" / "plan.jsonl"
            rows = [
                json.loads(line)
                for line in plan_path.read_text(encoding="utf-8").splitlines()
            ]
            rows[0]["evidence"] += "\nA cache-refresh note."
            plan_path.write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    for row in rows
                ),
                encoding="utf-8",
            )
            refreshed = engine_graph.load_or_build(
                workspace, run, view="auto"
            )
            self.assertNotEqual(refreshed["source_sha256"], before_hash)
            self.assertEqual(
                engine_graph.load_cached(workspace, view="workflow"), refreshed
            )


    def test_workflow_view_preserves_rejected_revision_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_runner_generation(workspace, accepted=False)

            workflow = engine_graph.build(
                workspace,
                {"run_id": "workflow-rejected", "engine": "math_harness"},
                view="workflow",
            )

            self.assertEqual(workflow["graph_role"], "workflow")
            self.assertEqual(workflow["status"], "partial")
            self.assertEqual(
                workflow["provenance"]["critic_verdict"], "needs_revision"
            )
            self.assertEqual(
                workflow["provenance"]["attestation"], "unattested"
            )
            self.assertNotIn(
                "fact",
                {
                    node["meta"]["workflow_kind"]
                    for node in workflow["graph"]["nodes"]
                },
            )
            goal = next(
                node
                for node in workflow["graph"]["nodes"]
                if node["id"] == "goal:root"
            )
            self.assertEqual(goal["meta"]["status"], "active")
            self.assertIn(
                "refines",
                {edge["label"] for edge in workflow["graph"]["edges"]},
            )

    def test_workflow_files_endpoints_and_cache_fail_closed(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project = _write_runner_generation(workspace)
            memory = project / "global_memory"
            outside = workspace / "outside.jsonl"
            edge_path = memory / "edges.jsonl"
            outside.write_bytes(edge_path.read_bytes())
            edge_path.unlink()
            edge_path.symlink_to(outside)
            with self.assertRaisesRegex(
                engine_graph.EngineGraphError, "unsafe engine graph file"
            ):
                engine_graph.build(
                    workspace, {"engine": "math_harness"}, view="workflow"
                )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project = _write_runner_generation(workspace)
            memory = project / "global_memory"
            (memory / "unexpected.jsonl").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                engine_graph.EngineGraphError, "unexpected"
            ):
                engine_graph.build(
                    workspace, {"engine": "math_harness"}, view="workflow"
                )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project = _write_runner_generation(workspace)
            edge_path = project / "global_memory" / "edges.jsonl"
            rows = [
                json.loads(line)
                for line in edge_path.read_text(encoding="utf-8").splitlines()
            ]
            rows[0]["dst"] = "missing:endpoint"
            edge_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                engine_graph.EngineGraphError, "invalid workflow edge"
            ):
                engine_graph.build(
                    workspace, {"engine": "math_harness"}, view="workflow"
                )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_runner_generation(workspace)
            with (
                patch.object(engine_graph, "MAX_WORKFLOW_FILE_BYTES", 8),
                self.assertRaisesRegex(
                    engine_graph.EngineGraphError, "size limit"
                ),
            ):
                engine_graph.build(
                    workspace, {"engine": "math_harness"}, view="workflow"
                )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_runner_generation(workspace)
            victim = workspace / "workflow-victim.json"
            victim.write_text("unchanged", encoding="utf-8")
            cache = workspace / engine_graph.WORKFLOW_CACHE_FILENAME
            cache.symlink_to(victim)
            result = engine_graph.rebuild(
                workspace, {"engine": "math_harness"}, view="workflow"
            )
            self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse(cache.is_symlink())
            self.assertEqual(
                engine_graph.load_cached(workspace, view="workflow"), result
            )


    def test_math_harness_maps_target_dependencies_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project, facts = _project(workspace)
            first_id, first = _fact_text(
                problem_id="demo",
                statement="A base lemma.",
                proof="This follows directly.",
                glossary={"A": "the base object"},
            )
            target_statement = "The target theorem."
            target_proof = "Apply the base lemma."
            second_id, second = _fact_text(
                problem_id="demo",
                statement=target_statement,
                proof=target_proof,
                predecessors=(first_id,),
                intuition="Build on the base case.",
            )
            (facts / f"{first_id}.md").write_text(first, encoding="utf-8")
            (facts / f"{second_id}.md").write_text(second, encoding="utf-8")
            _write_bound_receipt(
                workspace,
                project,
                fact_id=second_id,
                fact_payload=second,
                problem=target_statement,
                proof=target_proof,
            )

            result = engine_graph.rebuild(
                workspace, {"run_id": "run-1", "engine": "math_harness"}
            )

            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["graph_role"], "fact-dependency")
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["selection"]["target_ids"], [second_id])
            self.assertTrue(result["selection"]["complete_closure"])
            by_id = {node["id"]: node for node in result["graph"]["nodes"]}
            self.assertEqual(by_id[first_id]["kind"], "lemma")
            self.assertEqual(by_id[second_id]["kind"], "conclusion")
            self.assertEqual(
                result["graph"]["edges"],
                [{"from": first_id, "to": second_id, "label": "fact dependency"}],
            )
            self.assertEqual(
                result["provenance"]["attestation"], "harness-gate-receipt"
            )
            self.assertIs(result["provenance"]["certificate"], False)
            self.assertIs(result["provenance"]["lean_verified"], False)
            self.assertEqual(
                result["provenance"]["response_only_transport"], "api_chat"
            )
            self.assertEqual(
                result["provenance"]["response_only_isolation_version"], 1
            )
            cache = workspace / engine_graph.CACHE_FILENAME
            self.assertTrue(cache.is_file())
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
            self.assertEqual(engine_graph.load_cached(workspace), result)

            # The root proof is what the formal pipeline consumes.  Editing it
            # after receipt publication must immediately revoke attestation.
            (workspace / "proof.md").write_text("A different proof.\n", encoding="utf-8")
            stale = engine_graph.load_or_build(
                workspace, {"run_id": "run-1", "engine": "math_harness"}
            )
            self.assertEqual(stale["provenance"]["attestation"], "unattested")
            self.assertEqual(stale["provenance"]["receipt_status"], "invalid")
            self.assertEqual(engine_graph.load_cached(workspace), stale)

    def test_math_harness_without_facts_falls_back_to_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            result = engine_graph.build(
                workspace,
                {
                    "run_id": "run-live",
                    "engine": "math_harness",
                    "agents": [
                        {
                            "trace_id": "planner",
                            "stage_name": "Planner",
                            "status": "running",
                        }
                    ],
                    "edges": [],
                },
            )

            self.assertEqual(result["graph_role"], "execution")
            self.assertIs(result["provenance"]["proof_logic"], False)
            self.assertEqual(result["graph"]["nodes"][0]["id"], "planner")

    def test_missing_target_is_partial_and_never_infers_a_conclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project, facts = _project(workspace)
            fact_id, payload = _fact_text(
                problem_id="demo", statement="A terminal fact.", proof="Proof."
            )
            (facts / f"{fact_id}.md").write_text(payload, encoding="utf-8")
            # A copied receipt cannot attest a project with no explicit TARGET.
            (project / "engine_receipt.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "engine": "math_harness",
                        "verdict": "accepted",
                        "critic_verdict": "correct",
                        "target_fact_ids": [fact_id],
                        "certificate": False,
                    }
                ),
                encoding="utf-8",
            )

            result = engine_graph.build(workspace, {"engine": "math_harness"})

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["selection"]["target_source"], "unset")
            self.assertEqual(result["selection"]["target_ids"], [])
            self.assertEqual(result["graph"]["nodes"][0]["kind"], "lemma")
            self.assertEqual(result["provenance"]["attestation"], "unattested")

    def test_upstream_external_refs_accept_authors_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _, facts = _project(workspace)
            fact_id, payload = _fact_text(
                problem_id="demo",
                statement="A cited fact.",
                proof="Use the cited theorem.",
            )
            refs = [
                {
                    "key": "HL26",
                    "authors": ["Han", "Liu"],
                    "title": "On X",
                    "arxiv": "2603.03817",
                    "year": 2026,
                    "venue": "arXiv",
                    "doi": "10.1000/example",
                    "cited_for": "Theorem 1.2",
                }
            ]
            payload = payload.replace(
                "external_refs: []",
                "external_refs: " + json.dumps(refs, ensure_ascii=False),
            )
            (facts / f"{fact_id}.md").write_text(
                payload, encoding="utf-8"
            )

            result = engine_graph.build(workspace, {"engine": "math_harness"})

            citation = result["graph"]["nodes"][0]["citations"][0]
            for expected in ("HL26", "Han, Liu", "On X", "2026", "Theorem 1.2"):
                with self.subTest(expected=expected):
                    self.assertIn(expected, citation)

    def test_fact_proof_preserves_schema_named_markdown_headings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _, facts = _project(workspace)
            proof = (
                "Opening.\n\n## Proof\nNested proof heading.\n\n"
                "## Statement\nStill proof text.\n\n## Intuition\nAlso proof text."
            )
            fact_id, payload = _fact_text(
                problem_id="demo", statement="Heading safety.", proof=proof
            )
            (facts / f"{fact_id}.md").write_text(payload, encoding="utf-8")

            result = engine_graph.build(workspace, {"engine": "math_harness"})

            node_proof = result["graph"]["nodes"][0]["meta"]["proof_excerpt"]
            self.assertEqual(node_proof, proof)
            self.assertIn("## Intuition\nAlso proof text.", node_proof)

    def test_content_ids_predecessors_targets_and_cycles_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project, facts = _project(workspace)
            fact_id, payload = _fact_text(
                problem_id="demo", statement="A fact.", proof="Proof."
            )
            (facts / f"{fact_id}.md").write_text(
                payload.replace("Proof.", "Altered proof."), encoding="utf-8"
            )
            with self.assertRaisesRegex(engine_graph.EngineGraphError, "content id mismatch"):
                engine_graph.build(workspace, {"engine": "math_harness"})

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project, facts = _project(workspace)
            unknown = "f" * 16
            fact_id, payload = _fact_text(
                problem_id="demo",
                statement="Dependent.",
                proof="Proof.",
                predecessors=(unknown,),
            )
            (facts / f"{fact_id}.md").write_text(payload, encoding="utf-8")
            (project / "TARGET.md").write_text(fact_id + "\n", encoding="utf-8")
            with self.assertRaisesRegex(engine_graph.EngineGraphError, "unknown predecessor"):
                engine_graph.build(workspace, {"engine": "math_harness"})

        cyclic = {
            "a": engine_graph._Fact("a", "p", "w", ("b",), "s", "p", {}, "", ()),
            "b": engine_graph._Fact("b", "p", "w", ("a",), "s", "p", {}, "", ()),
        }
        with self.assertRaisesRegex(engine_graph.EngineGraphError, "cycle"):
            engine_graph._topological_order(cyclic)

    def test_nofollow_rejects_fact_and_target_symlinks(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project, facts = _project(workspace)
            fact_id, payload = _fact_text(
                problem_id="demo", statement="A fact.", proof="Proof."
            )
            outside = workspace / "outside.md"
            outside.write_text(payload, encoding="utf-8")
            (facts / f"{fact_id}.md").symlink_to(outside)
            with self.assertRaisesRegex(engine_graph.EngineGraphError, "unsafe engine graph file"):
                engine_graph.build(workspace, {"engine": "math_harness"})

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project, facts = _project(workspace)
            fact_id, payload = _fact_text(
                problem_id="demo", statement="A fact.", proof="Proof."
            )
            (facts / f"{fact_id}.md").write_text(payload, encoding="utf-8")
            outside = workspace / "outside-target.md"
            outside.write_text(fact_id + "\n", encoding="utf-8")
            (project / "TARGET.md").symlink_to(outside)
            with self.assertRaisesRegex(engine_graph.EngineGraphError, "unsafe engine graph file"):
                engine_graph.build(workspace, {"engine": "math_harness"})

    def test_generic_engine_is_an_execution_trace_not_proof_logic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = {
                "run_id": "generic-1",
                "engine": "improof",
                "agents": [
                    {
                        "trace_id": "author",
                        "stage_name": "Author",
                        "role": "prover",
                        "status": "finished",
                        "model": "test-model",
                        "output": "Drafted a proof.",
                        "call_seq": "9" * 5000,
                    },
                    {
                        "trace_id": "critic",
                        "stage_name": "Critic",
                        "status": "finished",
                    },
                ],
                "edges": [{"from": "author", "to": "critic", "type": "review"}],
            }

            result = engine_graph.rebuild(Path(tmp), run)

            self.assertEqual(result["graph_role"], "execution")
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["provenance"]["semantics"], "agent-execution")
            self.assertIs(result["provenance"]["proof_logic"], False)
            self.assertIs(result["provenance"]["certificate"], False)
            self.assertEqual(
                result["graph"]["edges"],
                [{"from": "author", "to": "critic", "label": "review"}],
            )
            self.assertEqual(result["graph"]["nodes"][0]["meta"]["call_seq"], 1)

            run["status"] = "finished"
            complete = engine_graph.build(Path(tmp), run)
            self.assertEqual(complete["status"], "complete")
            self.assertNotEqual(complete["source_sha256"], result["source_sha256"])

            empty = engine_graph.build(
                Path(tmp),
                {
                    "run_id": "generic-empty",
                    "engine": "improof",
                    "status": "running",
                },
            )
            self.assertEqual(empty["status"], "none")

    def test_unexpected_fact_entry_and_stale_receipt_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project, facts = _project(workspace)
            fact_id, payload = _fact_text(
                problem_id="demo", statement="A fact.", proof="Proof."
            )
            (facts / f"{fact_id}.md").write_text(payload, encoding="utf-8")
            (facts / "README.md").write_text("not a fact", encoding="utf-8")
            with self.assertRaisesRegex(engine_graph.EngineGraphError, "unexpected entry"):
                engine_graph.build(workspace, {"engine": "math_harness"})

            (facts / "README.md").unlink()
            valid_receipt = _write_bound_receipt(
                workspace,
                project,
                fact_id=fact_id,
                fact_payload=payload,
                problem="A fact.",
                proof="Proof.",
            )

            codex_receipt = json.loads(json.dumps(valid_receipt))
            codex_receipt["transport"] = "codex_exec"
            codex_receipt["workflow"]["response_only_contract"] = {
                "transport": "codex_exec",
                "isolation_version": 4,
            }
            (project / "engine_receipt.json").write_text(
                json.dumps(codex_receipt), encoding="utf-8"
            )
            codex_graph = engine_graph.build(
                workspace, {"engine": "math_harness"}
            )
            self.assertEqual(
                codex_graph["provenance"]["attestation"],
                "harness-gate-receipt",
            )
            self.assertEqual(
                codex_graph["provenance"]["response_only_isolation_version"], 4
            )

            extra_id, extra = _fact_text(
                problem_id="demo", statement="Disconnected.", proof="Separate."
            )
            (facts / f"{extra_id}.md").write_text(extra, encoding="utf-8")
            disconnected = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(len(disconnected["graph"]["nodes"]), 2)
            self.assertEqual(
                disconnected["provenance"]["attestation"], "unattested"
            )
            (facts / f"{extra_id}.md").unlink()

            invalid_isolation = json.loads(json.dumps(valid_receipt))
            invalid_isolation["workflow"]["response_only_contract"][
                "isolation_version"
            ] = 2
            (project / "engine_receipt.json").write_text(
                json.dumps(invalid_isolation), encoding="utf-8"
            )
            isolated = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(
                isolated["provenance"]["attestation"], "unattested"
            )

            # A copied/minimal receipt with the right target is not sufficient.
            (project / "engine_receipt.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "engine": "math_harness",
                        "verdict": "accepted",
                        "critic_verdict": "correct",
                        "target_fact_id": fact_id,
                        "target_fact_ids": [fact_id],
                        "certificate": False,
                    }
                ),
                encoding="utf-8",
            )
            copied = engine_graph.build(workspace, {"engine": "math_harness"})
            self.assertEqual(copied["provenance"]["attestation"], "unattested")

            for field in (
                "problem_sha256",
                "proof_sha256",
                "target_sha256",
                "fact_bytes_sha256",
            ):
                with self.subTest(field=field):
                    corrupted = dict(valid_receipt)
                    corrupted[field] = "0" * 64
                    (project / "engine_receipt.json").write_text(
                        json.dumps(corrupted), encoding="utf-8"
                    )
                    result = engine_graph.build(
                        workspace, {"engine": "math_harness"}
                    )
                    self.assertEqual(
                        result["provenance"]["attestation"], "unattested"
                    )
                    self.assertEqual(
                        result["provenance"]["receipt_status"], "invalid"
                    )

    def test_cache_symlink_is_not_followed_or_overwritten_through(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            victim = workspace / "victim.json"
            victim.write_text("do not change", encoding="utf-8")
            cache = workspace / engine_graph.CACHE_FILENAME
            cache.symlink_to(victim)
            self.assertIsNone(engine_graph.load_cached(workspace))

            result = engine_graph.rebuild(
                workspace,
                {"run_id": "r", "engine": "plain", "agents": [], "edges": []},
            )

            self.assertEqual(victim.read_text(encoding="utf-8"), "do not change")
            self.assertFalse(cache.is_symlink())
            self.assertEqual(engine_graph.load_cached(workspace), result)


class RunRecordReaderSecurityTests(unittest.TestCase):
    def test_run_record_reader_is_bounded_nofollow_and_requires_an_object(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache" / "harness"
            runs = root / "runs"
            cache.mkdir(parents=True)
            runs.mkdir()

            def read(run_id: str) -> dict | None:
                with (
                    patch.object(console_server, "_cache_harness", return_value=cache),
                    patch.object(console_server, "RUNS_DIR", runs),
                ):
                    return console_server._run_record_for(run_id)

            (cache / "valid.json").write_text('{"engine":"plain"}', encoding="utf-8")
            self.assertEqual(read("valid"), {"engine": "plain"})
            self.assertIsNone(read("../valid"))

            (cache / "list.json").write_text("[]", encoding="utf-8")
            self.assertIsNone(read("list"))
            (cache / "deep.json").write_text(
                "[" * 1100 + "0" + "]" * 1100, encoding="utf-8"
            )
            self.assertIsNone(read("deep"))

            (cache / "large.json").write_text('{"large":"123456789"}', encoding="utf-8")
            with patch.object(console_server, "_MAX_RUN_RECORD_BYTES", 8):
                self.assertIsNone(read("large"))

            outside = root / "outside.json"
            outside.write_text('{"engine":"stolen"}', encoding="utf-8")
            (cache / "linked.json").symlink_to(outside)
            self.assertIsNone(read("linked"))

            real_parent = root / "real-harness"
            real_parent.mkdir()
            (real_parent / "parentlink.json").write_text(
                '{"engine":"stolen"}', encoding="utf-8"
            )
            parent_link = root / "linked-harness"
            parent_link.symlink_to(real_parent, target_is_directory=True)
            with (
                patch.object(console_server, "_cache_harness", return_value=parent_link),
                patch.object(console_server, "RUNS_DIR", runs),
            ):
                self.assertIsNone(console_server._run_record_for("parentlink"))


class EngineGraphRouteTests(unittest.TestCase):
    @staticmethod
    def _handler(path: str, body: dict | None = None) -> console_server.Handler:
        handler = console_server.Handler.__new__(console_server.Handler)
        handler.path = path
        raw = json.dumps(body or {}).encode("utf-8")
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler._resolve_path = Mock(return_value=path.split("?", 1)[0])
        handler._require_user = Mock(return_value={"id": 7, "is_admin": False})
        handler._owns_run = Mock(return_value=True)
        handler._send = Mock()
        return handler

    def test_post_engine_graph_is_offline_and_never_calls_proof_graph_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            handler = self._handler(
                "/api/run/run-1/proof_graph", {"kind": "engine", "model": "ignored"}
            )
            expected = {"kind": "engine", "status": "none"}
            with (
                patch.object(console_server, "_workspace_for_run", return_value=workspace),
                patch.object(
                    console_server,
                    "_run_record_for",
                    return_value={"run_id": "run-1", "engine": "plain"},
                ),
                patch.object(engine_graph, "rebuild", return_value=expected) as rebuild,
                patch.object(proof_graph, "generate") as model_graph,
            ):
                console_server.Handler.do_POST(handler)

            rebuild.assert_called_once()
            self.assertEqual(rebuild.call_args.kwargs["view"], "auto")
            model_graph.assert_not_called()
            self.assertEqual(handler._send.call_args.args[0], 200)
            self.assertEqual(json.loads(handler._send.call_args.args[1]), expected)

    def test_get_engine_graph_forwards_explicit_workflow_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            handler = self._handler(
                "/api/run/run-1/proof_graph?kind=engine&view=workflow"
            )
            expected = {"kind": "engine", "status": "complete"}
            with (
                patch.object(
                    console_server, "_workspace_for_run", return_value=workspace
                ),
                patch.object(
                    console_server,
                    "_run_record_for",
                    return_value={"run_id": "run-1", "engine": "math_harness"},
                ),
                patch.object(
                    engine_graph, "load_or_build", return_value=expected
                ) as load,
            ):
                console_server.Handler.do_GET(handler)

            self.assertEqual(load.call_args.kwargs["view"], "workflow")
            self.assertEqual(handler._send.call_args.args[0], 200)


    def test_get_engine_graph_remains_ownership_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            handler = self._handler("/api/run/foreign/proof_graph?kind=engine")
            handler._owns_run = Mock(return_value=False)
            with (
                patch.object(console_server, "_workspace_for_run", return_value=workspace),
                patch.object(engine_graph, "load_or_build") as load,
            ):
                console_server.Handler.do_GET(handler)

            load.assert_not_called()
            self.assertEqual(handler._send.call_args.args[0], 404)


if __name__ == "__main__":
    unittest.main()
