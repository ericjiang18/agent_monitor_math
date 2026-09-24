from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_monitor import auto_pipeline, lean_verify, proof_bridge, proof_graph


class MathHarnessAutomaticFormalPipelineTests(unittest.TestCase):
    def test_completed_math_harness_proof_reaches_lean_formal_dag_and_coverage(
        self,
    ) -> None:
        calls: list[tuple[str, str | None, str]] = []

        def fake_lean_generate(**kwargs):
            workspace = Path(kwargs["workspace"])
            lean_dir = workspace / "lean"
            lean_dir.mkdir(exist_ok=True)
            (lean_dir / "Proof.lean").write_text(
                "theorem main : 1 + 1 = 2 := by decide\n",
                encoding="utf-8",
            )
            calls.append(
                (
                    "lean",
                    kwargs.get("model"),
                    str((kwargs.get("run_record") or {}).get("engine")),
                )
            )
            return {"status": "verified", "model": kwargs.get("model")}

        def fake_graph_generate(**kwargs):
            calls.append(
                (
                    str(kwargs["kind"]),
                    kwargs.get("model"),
                    str((kwargs.get("run_record") or {}).get("engine")),
                )
            )
            return {
                "model": kwargs.get("model"),
                "source": "proof.md" if kwargs["kind"] == "informal" else "Proof.lean",
                "graph": {
                    "nodes": [{"id": "main", "kind": "conclusion"}],
                    "edges": [],
                },
            }

        record = {
            "run_id": "math-harness-auto",
            "engine": "math_harness",
            "status": "finished",
            "model": "kimi-k3",
            "auth_route": "api_key",
            "credential_source": "sponsored_kimi",
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text(
                "# Proof\n\nA complete response-only Math Harness proof.\n",
                encoding="utf-8",
            )
            auto_pipeline._write_state(
                workspace,
                {
                    "version": 2,
                    "run_id": record["run_id"],
                    "proof_engine": "math_harness",
                    "status": "running",
                    "stages": {},
                },
            )
            with (
                patch.object(lean_verify, "generate", side_effect=fake_lean_generate),
                patch.object(proof_graph, "generate", side_effect=fake_graph_generate),
                patch.object(
                    proof_bridge,
                    "build",
                    return_value={
                        "status": "verified",
                        "summary": {"verified": 1, "total": 1},
                    },
                ),
            ):
                auto_pipeline._run_batch(
                    workspace,
                    record,
                    {"id": 61},
                    "kimi-k3",
                    "math_harness",
                )
                auto_pipeline._finish(workspace)
            state = auto_pipeline.load_state(workspace)

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(
            calls,
            [
                ("informal", "kimi-k3", "math_harness"),
                ("lean", "kimi-k3", "math_harness"),
                ("formal", "kimi-k3", "math_harness"),
            ],
        )
        self.assertEqual(state["proof_engine"], "math_harness")
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["proof_outcome"], "success")
        self.assertEqual(state["stages"]["lean"]["result"]["status"], "verified")
        self.assertTrue(state["stages"]["formal_dag"]["result"]["certificate"])
        self.assertTrue(state["stages"]["coverage"]["result"]["certificate"])


if __name__ == "__main__":
    unittest.main()
