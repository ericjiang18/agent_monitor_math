from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import auto_pipeline, lean_verify, proof_bridge, proof_graph


class AutoPipelineOutcomeTests(unittest.TestCase):
    @staticmethod
    def _seed(workspace: Path) -> None:
        auto_pipeline._write_state(
            workspace,
            {
                "status": "running",
                "stages": {
                    "informal_dag": {"status": "done", "detail": "ready"},
                    "lean": {"status": "waiting"},
                    "formal_dag": {"status": "waiting"},
                    "coverage": {"status": "waiting"},
                },
            },
        )

    def _run_batch(
        self,
        workspace: Path,
        *,
        lean_status: str,
        write_source: bool = True,
        coverage_status: str | None = None,
        formal_fallback: bool = False,
    ):
        def generate_lean(*, workspace: Path, **_kwargs):
            if write_source:
                lean_dir = workspace / "lean"
                lean_dir.mkdir(exist_ok=True)
                (lean_dir / "Proof.lean").write_text(
                    "theorem main : True := by trivial\n",
                    encoding="utf-8",
                )
            return {"status": lean_status, "model": "offline-test"}

        graph_result = {
            "model": "structural-test",
            "source": "Proof.lean",
            "graph": {"nodes": [{"id": "main"}], "edges": []},
        }
        fallback_result = {**graph_result, "model": "lean-compiler"}
        effective_coverage = coverage_status or (
            "verified" if lean_status == "verified" else "unverified"
        )
        coverage_result = {
            "status": effective_coverage,
            "summary": {
                "verified": 1 if effective_coverage == "verified" else 0,
                "total": 1,
            },
        }
        with (
            patch.object(auto_pipeline, "_informal_worker"),
            patch.object(lean_verify, "generate", side_effect=generate_lean),
            patch.object(
                proof_graph,
                "generate",
                return_value=graph_result,
                side_effect=ValueError("semantic DAG unavailable") if formal_fallback else None,
            ) as graph,
            patch.object(
                proof_graph,
                "load_or_parse_formal",
                return_value=fallback_result,
            ),
            patch.object(proof_bridge, "build", return_value=coverage_result) as coverage,
        ):
            auto_pipeline._run_batch(
                workspace,
                {"engine": "hermes"},
                {"id": 7},
                "offline-test",
                "hermes",
            )
            auto_pipeline._finish(workspace)
        return auto_pipeline.load_state(workspace), graph, coverage

    def test_verified_is_success_without_conflating_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._seed(workspace)
            state, graph, coverage = self._run_batch(
                workspace,
                lean_status="verified",
            )

        self.assertEqual(state["stages"]["lean"]["status"], "done")
        self.assertEqual(state["stages"]["lean"]["outcome"], "success")
        self.assertEqual(state["stages"]["formal_dag"]["status"], "done")
        self.assertEqual(state["stages"]["formal_dag"]["outcome"], "success")
        self.assertTrue(state["stages"]["formal_dag"]["result"]["certificate"])
        self.assertEqual(state["stages"]["coverage"]["outcome"], "success")
        self.assertTrue(state["stages"]["coverage"]["result"]["certificate"])
        self.assertEqual(state["proof_outcome"], "success")
        self.assertEqual(state["status"], "done")
        graph.assert_called_once()
        coverage.assert_called_once()

    def test_verified_compiler_fallback_is_a_complete_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._seed(workspace)
            state, graph, coverage = self._run_batch(
                workspace,
                lean_status="verified",
                formal_fallback=True,
            )

        stage = state["stages"]["formal_dag"]
        self.assertEqual(stage["status"], "parsed")
        self.assertEqual(stage["outcome"], "success")
        self.assertEqual(stage["result"]["model"], "lean-compiler")
        self.assertTrue(stage["result"]["certificate"])
        self.assertEqual(state["status"], "done")
        graph.assert_called_once()
        coverage.assert_called_once()

    def test_incomplete_and_unfaithful_are_attention_with_structural_views(self) -> None:
        for lean_status in ("incomplete", "unfaithful"):
            with self.subTest(lean_status=lean_status), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                self._seed(workspace)
                state, graph, coverage = self._run_batch(
                    workspace,
                    lean_status=lean_status,
                )

            self.assertEqual(state["stages"]["lean"]["status"], "done")
            self.assertEqual(state["stages"]["lean"]["outcome"], "attention")
            self.assertEqual(state["stages"]["formal_dag"]["status"], "done")
            self.assertEqual(state["stages"]["formal_dag"]["outcome"], "attention")
            self.assertFalse(state["stages"]["formal_dag"]["result"]["certificate"])
            self.assertEqual(state["stages"]["coverage"]["status"], "done")
            self.assertEqual(state["stages"]["coverage"]["outcome"], "attention")
            self.assertFalse(state["stages"]["coverage"]["result"]["certificate"])
            self.assertIn(
                "not a verified certificate",
                state["stages"]["coverage"]["detail"],
            )
            self.assertNotIn(
                "informal statements formally verified",
                state["stages"]["coverage"]["detail"],
            )
            self.assertEqual(state["proof_outcome"], "attention")
            self.assertEqual(state["status"], "partial")
            graph.assert_called_once()
            coverage.assert_called_once()

    def test_failed_with_source_keeps_structural_dag_but_not_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._seed(workspace)
            state, graph, coverage = self._run_batch(
                workspace,
                lean_status="failed",
            )

        self.assertEqual(state["stages"]["lean"]["status"], "done")
        self.assertEqual(state["stages"]["lean"]["outcome"], "error")
        self.assertEqual(state["stages"]["formal_dag"]["status"], "done")
        self.assertEqual(state["stages"]["formal_dag"]["outcome"], "attention")
        self.assertFalse(state["stages"]["formal_dag"]["result"]["certificate"])
        self.assertEqual(state["stages"]["coverage"]["status"], "done")
        self.assertEqual(state["stages"]["coverage"]["outcome"], "attention")
        self.assertFalse(state["stages"]["coverage"]["result"]["certificate"])
        self.assertIn("structural only", state["stages"]["formal_dag"]["detail"])
        self.assertEqual(state["proof_outcome"], "error")
        self.assertEqual(state["status"], "partial")
        graph.assert_called_once()
        coverage.assert_called_once()

    def test_translator_exception_blocks_formal_dag_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._seed(workspace)
            with (
                patch.object(auto_pipeline, "_informal_worker"),
                patch.object(
                    lean_verify,
                    "generate",
                    side_effect=RuntimeError("translator unavailable"),
                ),
                patch.object(proof_graph, "generate") as graph,
                patch.object(proof_bridge, "build") as coverage,
            ):
                auto_pipeline._run_batch(
                    workspace,
                    {"engine": "hermes"},
                    {"id": 7},
                    "offline-test",
                    "hermes",
                )
                auto_pipeline._finish(workspace)
            state = auto_pipeline.load_state(workspace)

        self.assertEqual(state["stages"]["lean"]["status"], "error")
        self.assertEqual(state["stages"]["lean"]["outcome"], "error")
        for name in ("formal_dag", "coverage"):
            self.assertEqual(state["stages"][name]["status"], "skipped")
            self.assertEqual(state["stages"][name]["outcome"], "blocked")
            self.assertEqual(state["stages"][name]["result"]["status"], "blocked")
        self.assertEqual(state["proof_outcome"], "error")
        self.assertEqual(state["status"], "partial")
        graph.assert_not_called()
        coverage.assert_not_called()

    def test_terminal_result_without_proof_lean_blocks_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._seed(workspace)
            state, graph, coverage = self._run_batch(
                workspace,
                lean_status="verified",
                write_source=False,
            )

        self.assertEqual(state["stages"]["lean"]["status"], "done")
        self.assertEqual(state["stages"]["lean"]["outcome"], "error")
        self.assertEqual(state["stages"]["lean"]["result"]["proof_lean"], "missing")
        for name in ("formal_dag", "coverage"):
            self.assertEqual(state["stages"][name]["status"], "skipped")
            self.assertEqual(state["stages"][name]["outcome"], "blocked")
        self.assertEqual(state["proof_outcome"], "error")
        self.assertEqual(state["status"], "partial")
        graph.assert_not_called()
        coverage.assert_not_called()

    def test_finish_backfills_outcome_from_legacy_result_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            auto_pipeline._write_state(
                workspace,
                {
                    "status": "running",
                    "stages": {
                        "informal_dag": {"status": "done"},
                        "lean": {
                            "status": "done",
                            "result": {"status": "incomplete"},
                        },
                        "formal_dag": {"status": "done"},
                        "coverage": {
                            "status": "done",
                            "result": {"status": "unverified"},
                        },
                    },
                },
            )
            auto_pipeline._finish(workspace)
            state = auto_pipeline.load_state(workspace)

        self.assertEqual(state["proof_outcome"], "attention")
        self.assertEqual(state["status"], "partial")

    def test_interruption_does_not_promote_legacy_incomplete_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            auto_pipeline._write_state(
                workspace,
                {
                    "status": "running",
                    "stages": {
                        "informal_dag": {"status": "done"},
                        "lean": {
                            "status": "done",
                            "result": {"status": "incomplete"},
                        },
                        "formal_dag": {"status": "done"},
                        "coverage": {
                            "status": "done",
                            "result": {"status": "unverified"},
                        },
                    },
                },
            )
            auto_pipeline.mark_interrupted(workspace, "server restarted")
            state = auto_pipeline.load_state(workspace)

        self.assertEqual(state["proof_outcome"], "attention")
        self.assertEqual(state["status"], "partial")

    def test_manual_failed_check_with_source_refreshes_structural_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._seed(workspace)
            lean_dir = workspace / "lean"
            lean_dir.mkdir()
            (lean_dir / "Proof.lean").write_text(
                "theorem main : False := by contradiction\n",
                encoding="utf-8",
            )
            with (
                patch.object(auto_pipeline, "_run_record", return_value={"engine": "codex"}),
                patch.object(
                    lean_verify,
                    "load_cached",
                    return_value={"status": "failed", "model": "offline-test"},
                ),
                patch.object(
                    proof_graph,
                    "generate",
                    return_value={
                        "model": "structural-test",
                        "source": "Proof.lean",
                        "graph": {"nodes": [{"id": "main"}], "edges": []},
                    },
                ) as graph,
                patch.object(
                    proof_bridge,
                    "build",
                    return_value={
                        "status": "unverified",
                        "summary": {"verified": 0, "total": 1},
                    },
                ) as coverage,
            ):
                auto_pipeline._refresh_formal_once(
                    run_id="manual-failed",
                    workspace=workspace,
                    owner_id=7,
                    model="offline-test",
                    engine="codex",
                )
            state = auto_pipeline.load_state(workspace)

        self.assertEqual(state["stages"]["lean"]["outcome"], "error")
        self.assertEqual(state["stages"]["formal_dag"]["status"], "done")
        self.assertEqual(state["stages"]["formal_dag"]["outcome"], "attention")
        self.assertEqual(state["stages"]["coverage"]["status"], "done")
        self.assertEqual(state["stages"]["coverage"]["outcome"], "attention")
        self.assertEqual(state["proof_outcome"], "error")
        self.assertEqual(state["status"], "partial")
        graph.assert_called_once()
        coverage.assert_called_once()

    def test_sponsored_kimi_serializes_informal_and_lean_model_workers(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    auto_pipeline.threading,
                    "Thread",
                    side_effect=AssertionError("sponsored Kimi must not fan out"),
                ),
                patch.object(
                    auto_pipeline,
                    "_informal_worker",
                    side_effect=lambda *_args: calls.append("informal"),
                ),
                patch.object(
                    auto_pipeline,
                    "_lean_and_formal_worker",
                    side_effect=lambda *_args: calls.append("lean"),
                ),
                patch.object(
                    auto_pipeline,
                    "load_state",
                    return_value={"stages": {"lean": {"status": "error"}}},
                ),
                patch.object(auto_pipeline, "_block_formal_downstream"),
            ):
                auto_pipeline._run_batch(
                    Path(tmp),
                    {"engine": "openclaude", "credential_source": "sponsored_kimi"},
                    {"id": 70},
                    "kimi-k3",
                    "openclaude",
                )

        self.assertEqual(calls, ["informal", "lean"])

    def test_claude_subscription_serializes_informal_before_lean(self) -> None:
        calls: list[str] = []
        active = False

        def record(label: str) -> None:
            nonlocal active
            self.assertFalse(active, f"{label} overlapped another Claude worker")
            active = True
            calls.append(label)
            active = False

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    auto_pipeline.threading,
                    "Thread",
                    side_effect=AssertionError(
                        "Claude subscription derived work must not fan out"
                    ),
                ),
                patch.object(
                    auto_pipeline,
                    "_informal_worker",
                    side_effect=lambda *_args: record("informal"),
                ),
                patch.object(
                    auto_pipeline,
                    "_lean_and_formal_worker",
                    side_effect=lambda *_args: record("lean"),
                ),
                patch.object(
                    auto_pipeline,
                    "load_state",
                    return_value={"stages": {"lean": {"status": "error"}}},
                ),
                patch.object(auto_pipeline, "_block_formal_downstream"),
            ):
                auto_pipeline._run_batch(
                    Path(tmp),
                    {
                        "engine": "math_harness",
                        "auth_route": "claude_subscription",
                    },
                    {"id": 61},
                    "fable",
                    "math_harness",
                )

        self.assertFalse(active)
        self.assertEqual(calls, ["informal", "lean"])

    def test_api_and_codex_routes_keep_parallel_derived_workers(self) -> None:
        for route in ("api_key", "codex_subscription"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp:
                barrier = threading.Barrier(2)
                events: list[str] = []
                failures: list[BaseException] = []
                event_lock = threading.Lock()

                def worker(label: str) -> None:
                    with event_lock:
                        events.append(f"{label}:start")
                    try:
                        barrier.wait(timeout=1)
                    except Exception as exc:  # surfaced on the main test thread
                        failures.append(exc)
                    with event_lock:
                        events.append(f"{label}:end")

                with (
                    patch.object(
                        auto_pipeline,
                        "_informal_worker",
                        side_effect=lambda *_args: worker("informal"),
                    ),
                    patch.object(
                        auto_pipeline,
                        "_lean_and_formal_worker",
                        side_effect=lambda *_args: worker("lean"),
                    ),
                    patch.object(
                        auto_pipeline,
                        "load_state",
                        return_value={"stages": {"lean": {"status": "error"}}},
                    ),
                    patch.object(auto_pipeline, "_block_formal_downstream"),
                ):
                    auto_pipeline._run_batch(
                        Path(tmp),
                        {"engine": "math_harness", "auth_route": route},
                        {"id": 61},
                        "offline-test",
                        "math_harness",
                    )

                self.assertEqual(failures, [])
                self.assertEqual(
                    set(events[:2]),
                    {"informal:start", "lean:start"},
                )
                self.assertEqual(
                    set(events[2:]),
                    {"informal:end", "lean:end"},
                )


if __name__ == "__main__":
    unittest.main()
