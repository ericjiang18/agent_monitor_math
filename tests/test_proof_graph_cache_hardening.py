from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import lean_broker, lean_kg, lean_verify, proof_graph


LEAN_SOURCE = """def seed : Nat := 1

theorem both : seed = 1 := by rfl
"""


def _node(node_id: str, *, label: str | None = None) -> dict[str, object]:
    return {
        "id": node_id,
        "kind": "conclusion",
        "label": label or node_id,
        "statement": label or node_id,
        "citations": [],
    }


def _graph(
    title: str,
    *,
    edge_label: str | None = None,
    provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    nodes = [_node("seed"), _node("both")]
    edges = (
        [{"from": "seed", "to": "both", "label": edge_label}]
        if edge_label
        else []
    )
    result: dict[str, object] = {
        "title": title,
        "nodes": nodes,
        "edges": edges,
    }
    if provenance is not None:
        result["provenance"] = provenance
    return result


class FormalGraphCacheHardeningTests(unittest.TestCase):
    def _write_cache(
        self, workspace: Path, *, model: str, graph: dict[str, object]
    ) -> None:
        proof_graph._write_cached(
            workspace,
            proof_graph.LEAN_GRAPH_FILENAME,
            {
                "generated_at": 1,
                "model": model,
                "source": "Proof.lean",
                "source_sha256": lean_kg.normalized_source_sha256(
                    LEAN_SOURCE
                ),
                "kind": "formal",
                "status": "parsed",
                "provenance": {"kind": "forged-top-level"},
                "knowledge_graph": {"status": "ok", "forged": True},
                "graph": graph,
                "compiler_graph": _graph(
                    "forged compiler graph",
                    edge_label="proof uses",
                    provenance={"kind": "lean-compiler"},
                ),
            },
        )

    def test_cached_compiler_primary_fails_closed_without_current_snapshot(
        self,
    ) -> None:
        stale = _graph(
            "stale compiler primary",
            edge_label="proof uses",
            provenance={
                "kind": "lean-compiler",
                "producer": "agent-monitor-lean-env-forged",
            },
        )
        structural = _graph("fresh structural parse")
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            self._write_cache(workspace, model="lean-compiler", graph=stale)
            with (
                patch.object(
                    proof_graph, "_lean_source", return_value=LEAN_SOURCE
                ),
                patch.object(
                    proof_graph,
                    "_compiler_snapshot_for_verified_source",
                    return_value=None,
                ),
                patch.object(
                    lean_kg,
                    "snapshot_summary",
                    return_value={"status": "unavailable"},
                ),
                patch.object(
                    proof_graph,
                    "graph_from_lean",
                    return_value=structural,
                ) as rebuild,
            ):
                result = proof_graph.load_or_parse_formal(workspace)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["graph"], structural)
        self.assertEqual(result["model"], "lean-parse")
        self.assertEqual(result["knowledge_graph"], {"status": "unavailable"})
        self.assertNotIn("compiler_graph", result)
        self.assertNotIn("provenance", result)
        rebuild.assert_called_once_with(LEAN_SOURCE, kg_snapshot=None)

    def test_current_attestation_replaces_every_cached_compiler_field(
        self,
    ) -> None:
        stale = _graph(
            "stale compiler primary",
            edge_label="type mentions",
            provenance={"kind": "lean-compiler"},
        )
        snapshot = {"status": "ok", "attested": True}
        refreshed = _graph(
            "fresh compiler primary",
            edge_label="proof uses",
            provenance={"kind": "lean-compiler", "producer": lean_kg.PRODUCER},
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            self._write_cache(workspace, model="lean-compiler", graph=stale)
            with (
                patch.object(
                    proof_graph, "_lean_source", return_value=LEAN_SOURCE
                ),
                patch.object(
                    proof_graph,
                    "_compiler_snapshot_for_verified_source",
                    return_value=snapshot,
                ),
                patch.object(
                    lean_kg,
                    "snapshot_summary",
                    return_value={"status": "ok", "fresh": True},
                ),
                patch.object(
                    proof_graph,
                    "graph_from_lean",
                    return_value=refreshed,
                ),
            ):
                result = proof_graph.load_or_parse_formal(workspace)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["graph"], refreshed)
        self.assertEqual(result["model"], "lean-compiler")
        self.assertEqual(
            result["knowledge_graph"], {"status": "ok", "fresh": True}
        )
        self.assertNotIn("compiler_graph", result)
        self.assertNotIn("provenance", result)

    def test_logical_primary_keeps_only_fresh_recomputed_compiler_graph(
        self,
    ) -> None:
        logical = _graph("logical model graph", edge_label="uses")
        snapshot = {"status": "ok", "attested": True}
        refreshed = _graph(
            "fresh compiler sidecar",
            edge_label="type mentions",
            provenance={"kind": "lean-compiler", "producer": lean_kg.PRODUCER},
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            self._write_cache(workspace, model="model-graph", graph=logical)
            with (
                patch.object(
                    proof_graph, "_lean_source", return_value=LEAN_SOURCE
                ),
                patch.object(
                    proof_graph,
                    "_compiler_snapshot_for_verified_source",
                    return_value=snapshot,
                ),
                patch.object(
                    lean_kg,
                    "snapshot_summary",
                    return_value={"status": "ok", "fresh": True},
                ),
                patch.object(
                    proof_graph,
                    "graph_from_lean",
                    return_value=refreshed,
                ),
            ):
                result = proof_graph.load_or_parse_formal(workspace)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["graph"]["title"], "logical model graph")
        self.assertEqual(result["model"], "model-graph")
        self.assertEqual(result["compiler_graph"], refreshed)
        self.assertEqual(
            result["knowledge_graph"], {"status": "ok", "fresh": True}
        )
        self.assertNotIn("provenance", result)


class CompilerEdgeKindTests(unittest.TestCase):
    def test_same_declaration_pair_keeps_proof_and_type_edges(self) -> None:
        declarations = [
            {"kind": "def", "name": "seed", "line": 1, "doc": ""},
            {"kind": "theorem", "name": "both", "line": 3, "doc": ""},
        ]
        snapshot = {
            "schema_version": lean_kg.SCHEMA_VERSION,
            "producer": lean_kg.PRODUCER,
            "status": "ok",
            "sandbox": next(iter(lean_kg.TRUSTED_SANDBOXES)),
            "source_sha256": hashlib.sha256(LEAN_SOURCE.encode()).hexdigest(),
            "normalized_source_sha256": lean_kg.normalized_source_sha256(
                LEAN_SOURCE
            ),
            "toolchain": "Lean test toolchain",
            "mathlib_fingerprint": "",
            "proof_edge_count": 1,
            "statement_edge_count": 1,
            "dependency_truncated_count": 0,
            "declarations": [
                {
                    "matched": True,
                    "line": 1,
                    "qualified_name": "Test.seed",
                    "local_proof_uses": [],
                    "local_statement_mentions": [],
                },
                {
                    "matched": True,
                    "line": 3,
                    "qualified_name": "Test.both",
                    "local_proof_uses": ["Test.seed"],
                    "local_statement_mentions": ["Test.seed"],
                },
            ],
        }
        with patch.object(
            lean_verify, "_decls_with_docs", return_value=declarations
        ):
            graph = proof_graph.graph_from_lean(
                LEAN_SOURCE, kg_snapshot=snapshot
            )

        pair_edges = [
            edge
            for edge in graph["edges"]
            if edge["from"] == "seed" and edge["to"] == "both"
        ]
        self.assertEqual(
            [edge["label"] for edge in pair_edges],
            ["proof uses", "type mentions"],
        )
        self.assertEqual(graph["provenance"]["proof_edges"], 1)
        self.assertEqual(graph["provenance"]["statement_edges"], 1)
        self.assertEqual(graph["provenance"]["omitted_proof_edges"], 0)
        self.assertEqual(graph["provenance"]["omitted_statement_edges"], 0)


class LeanBrokerDecodeHardeningTests(unittest.TestCase):
    class _Client:
        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, _path: str) -> None:
            pass

        def sendall(self, _payload: bytes) -> None:
            pass

        def shutdown(self, _how: int) -> None:
            pass

        def close(self) -> None:
            pass

    def test_recursive_json_decode_is_wrapped_as_broker_error(self) -> None:
        body = b"{}"
        header = lean_broker._HEADER.pack(lean_broker.RESPONSE_MAGIC, len(body))
        with (
            patch.object(
                lean_broker.socket, "socket", return_value=self._Client()
            ),
            patch.object(
                lean_broker, "_read_exact", side_effect=[header, body]
            ),
            patch.object(
                lean_broker.json,
                "loads",
                side_effect=RecursionError("nested JSON limit"),
            ),
        ):
            with self.assertRaisesRegex(
                lean_broker.LeanBrokerError, "invalid JSON"
            ) as caught:
                lean_broker._exchange(
                    Path("/fixed/core.sock"),
                    profile="core",
                    source=b"example : True := by trivial\n",
                    timeout=1.0,
                )
        self.assertIsInstance(caught.exception.__cause__, RecursionError)


if __name__ == "__main__":
    unittest.main()
