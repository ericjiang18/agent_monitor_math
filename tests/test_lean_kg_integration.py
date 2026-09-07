from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import auto_pipeline, lean_broker, lean_kg, lean_verify, proof_graph


LEAN_SOURCE = """namespace Alpha

def statementSeed : Nat := 7

axiom statementOnly : statementSeed = 7

theorem statementAndProof : statementSeed = 7 := by rfl

theorem proofOnlySource : True := by
  exact True.intro

theorem proofOnlyConsumer : True := proofOnlySource

/- The names statementSeed and proofOnlySource occur here only as prose. -/
theorem main : True := by
  let note := "statementSeed proofOnlySource"
  exact True.intro

end Alpha

namespace Beta

def statementSeed : Nat := 8

theorem statementAndProof : statementSeed = 8 := by rfl

end Beta
"""

MATHLIB_SOURCE = """import Mathlib.Data.Int.Notation

namespace KgMathlib

theorem helper (x : Int) : x = x := by rfl

theorem main (x : Int) : x = x := helper x

end KgMathlib
"""


SECRET_ENV = {
    "OPENAI_API_KEY": "openai-secret-sentinel",
    "ANTHROPIC_API_KEY": "anthropic-secret-sentinel",
    "OPENROUTER_API_KEY": "openrouter-secret-sentinel",
    "KIMI_K3_API_KEY": "kimi-secret-sentinel",
    "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret-sentinel",
    "CLAUDE_CONFIG_DIR": "/sensitive/claude-home",
    "CODEX_HOME": "/sensitive/codex-home",
    "AGENT_MONITOR_CODEX_ACCOUNT_HOME": "/sensitive/codex-account",
    "AGENT_MONITOR_CLAUDE_ACCOUNT_HOME": "/sensitive/claude-account",
    "AWS_SESSION_TOKEN": "aws-secret-sentinel",
    "SSH_AUTH_SOCK": "/sensitive/ssh-agent",
    "HTTP_PROXY": "http://sensitive.proxy",
}


def _write_source(workspace: Path, source: str = LEAN_SOURCE) -> Path:
    lean_dir = workspace / "lean"
    lean_dir.mkdir(exist_ok=True)
    target = lean_dir / "Proof.lean"
    target.write_text(source, encoding="utf-8")
    return target


def _write_verified_cache(
    workspace: Path,
    source: str,
    *,
    version: str | None = None,
    uses_mathlib: bool = False,
    seed: str = "kg-test-release",
) -> None:
    target = workspace / "lean" / "Proof.lean"
    exact_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    identity = _release_identity(seed)
    profile = "mathlib" if uses_mathlib else "core"
    receipt = {
        "ok": True,
        "status": "verified",
        "exit_code": 0,
        "source_sha256": exact_sha,
        "toolchain": version or lean_broker.EXPECTED_TOOLCHAIN,
        "sandbox": lean_broker.SANDBOX_MARKER,
        "checker_profile": profile,
        **identity,
    }
    (workspace / lean_verify.RESULT_FILENAME).write_text(
        json.dumps({
            "status": "verified", "lean": source, "sorry_count": 0,
            "verified_source_sha256": exact_sha,
            "uses_mathlib": uses_mathlib,
            "kernel_receipt": receipt,
            "toolchain": {
                "version": version or lean_broker.EXPECTED_TOOLCHAIN,
                "checker_sandbox": lean_broker.SANDBOX_MARKER,
                "checker_profile": profile,
                **identity,
            },
        }),
        encoding="utf-8",
    )


def _release_identity(seed: str = "kg-test-release") -> dict[str, str]:
    toolchain_tree = hashlib.sha256(f"toolchain:{seed}".encode()).hexdigest()
    mathlib_tree = hashlib.sha256(f"mathlib:{seed}".encode()).hexdigest()
    canonical = {
        "schema": 1,
        "lean_toolchain": lean_broker.EXPECTED_LEAN_TOOLCHAIN,
        "lean_version": lean_broker.EXPECTED_TOOLCHAIN,
        "mathlib_input_rev": lean_broker.EXPECTED_MATHLIB_INPUT_REV,
        "mathlib_rev": lean_broker.EXPECTED_MATHLIB_REV,
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }
    encoded = (
        json.dumps(canonical, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    return {
        "release_id": hashlib.sha256(encoded).hexdigest(),
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }


def _compiler_rows() -> list[dict]:
    def row(name, kind, statement=(), proof=()):
        return {
            "name": name,
            "kind": kind,
            "statementDeps": list(statement),
            "proofDeps": list(proof),
            "statementTruncated": False,
            "proofTruncated": False,
        }

    return [
        row("Alpha.statementSeed", "def"),
        row(
            "Alpha.statementOnly", "axiom",
            statement=("Alpha.statementSeed",),
        ),
        row(
            "Alpha.statementAndProof", "theorem",
            statement=("Alpha.statementSeed",),
            proof=("Alpha.statementSeed",),
        ),
        row("Alpha.proofOnlySource", "theorem"),
        row(
            "Alpha.proofOnlyConsumer", "theorem",
            proof=("Alpha.proofOnlySource",),
        ),
        row("Alpha.main", "theorem"),
        row("Beta.statementSeed", "def"),
        row(
            "Beta.statementAndProof", "theorem",
            statement=("Beta.statementSeed",),
            proof=("Beta.statementSeed",),
        ),
    ]


def _broker_result(
    program: str,
    *,
    marker: str,
    rows: list[dict] | None = None,
    uses_mathlib: bool = False,
    seed: str = "kg-test-release",
) -> dict:
    return {
        "profile": "mathlib" if uses_mathlib else "core",
        "status": "completed",
        "exit_code": 0,
        "stdout": (
            f"AGENT_MONITOR_LEAN_KG::{marker}::"
            + json.dumps(_compiler_rows() if rows is None else rows)
            + "\n"
        ),
        "stderr": "",
        "error": "",
        "duration": 0.01,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "source_sha256": hashlib.sha256(program.encode()).hexdigest(),
        "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
        "sandbox": lean_broker.SANDBOX_MARKER,
        **_release_identity(seed),
    }


def _raw_rows(snapshot: dict) -> list[dict]:
    return [
        {
            "name": declaration["qualified_name"],
            "kind": declaration["compiler_kind"],
            "statementDeps": declaration["statement_dependencies"],
            "proofDeps": declaration["proof_dependencies"],
            "statementTruncated": declaration[
                "statement_dependencies_truncated"
            ],
            "proofTruncated": declaration[
                "proof_dependencies_truncated"
            ],
        }
        for declaration in snapshot.get("declarations") or []
        if declaration.get("matched")
    ]


class LeanKgIntegrationTests(unittest.TestCase):
    """Offline integration coverage through the fixed broker interface."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools = {
            "available": True,
            "version": lean_broker.EXPECTED_TOOLCHAIN,
        }
        cls._real_tmp = tempfile.TemporaryDirectory()
        cls.real_workspace = Path(cls._real_tmp.name)
        _write_source(cls.real_workspace)
        _write_verified_cache(
            cls.real_workspace,
            LEAN_SOURCE,
            version=lean_broker.EXPECTED_TOOLCHAIN,
        )
        marker = "0123456789abcdef01234567"

        def checked(program: str, **_kwargs):
            return _broker_result(program, marker=marker)

        with (
            patch.dict(os.environ, SECRET_ENV, clear=False),
            patch.object(lean_kg.secrets, "token_hex", return_value=marker),
            patch.object(lean_broker, "check_source", side_effect=checked),
        ):
            cls.snapshot = lean_kg.extract_snapshot(
                cls.real_workspace, force=True, timeout=90
            )
        if cls.snapshot.get("status") != "ok":
            raise AssertionError(f"broker KG extraction failed: {cls.snapshot}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._real_tmp.cleanup()

    def _snapshot_copy(self) -> dict:
        return copy.deepcopy(self.snapshot)

    def _workspace_with_snapshot(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        workspace = Path(tmp.name)
        _write_source(workspace)
        _write_verified_cache(
            workspace, LEAN_SOURCE, version=str(self.tools["version"])
        )
        lean_kg._write(workspace, self._snapshot_copy())
        return tmp, workspace

    def test_real_lean_414_extraction_distinguishes_statement_and_proof_edges(self) -> None:
        snapshot = self.snapshot
        self.assertEqual(snapshot["schema_version"], lean_kg.SCHEMA_VERSION)
        self.assertEqual(snapshot["producer"], lean_kg.PRODUCER)
        self.assertIn("version 4.14.0", snapshot["toolchain"])
        self.assertEqual(snapshot["declaration_count"], 8)
        self.assertEqual(snapshot["matched_declaration_count"], 8)

        declarations = {
            item["qualified_name"]: item for item in snapshot["declarations"]
        }
        statement_only = declarations["Alpha.statementOnly"]
        self.assertEqual(
            statement_only["local_statement_mentions"], ["Alpha.statementSeed"]
        )
        self.assertEqual(statement_only["local_proof_uses"], [])

        proof_only = declarations["Alpha.proofOnlyConsumer"]
        self.assertEqual(proof_only["local_statement_mentions"], [])
        self.assertEqual(
            proof_only["local_proof_uses"], ["Alpha.proofOnlySource"]
        )

        both = declarations["Alpha.statementAndProof"]
        self.assertEqual(
            both["local_statement_mentions"], ["Alpha.statementSeed"]
        )
        self.assertEqual(both["local_proof_uses"], ["Alpha.statementSeed"])

    def test_real_extraction_matches_same_leaf_names_to_their_namespaces(self) -> None:
        declarations = {
            item["qualified_name"]: item for item in self.snapshot["declarations"]
        }
        self.assertIn("Alpha.statementSeed", declarations)
        self.assertIn("Beta.statementSeed", declarations)
        self.assertEqual(
            declarations["Alpha.statementAndProof"]["local_proof_uses"],
            ["Alpha.statementSeed"],
        )
        self.assertEqual(
            declarations["Beta.statementAndProof"]["local_proof_uses"],
            ["Beta.statementSeed"],
        )
        self.assertNotIn(
            "Beta.statementSeed",
            declarations["Alpha.statementAndProof"]["local_proof_uses"],
        )

    def test_dependency_truncation_is_explicit_in_snapshot_and_dag(self) -> None:
        names, truncated = lean_kg._names(
            [f"Dependency.{index}" for index in range(
                lean_kg.MAX_DEPENDENCIES + 17
            )]
        )
        self.assertTrue(truncated)
        self.assertEqual(len(names), lean_kg.MAX_DEPENDENCIES)

        tmp, workspace = self._workspace_with_snapshot()
        self.addCleanup(tmp.cleanup)
        snapshot = self._snapshot_copy()
        snapshot["declarations"][0]["proof_dependencies_truncated"] = True
        snapshot["dependency_truncated_count"] = sum(
            int(item["statement_dependencies_truncated"])
            + int(item["proof_dependencies_truncated"])
            for item in snapshot["declarations"]
        )
        lean_kg._write(workspace, snapshot)
        loaded = lean_kg.load_snapshot(
            workspace, source=LEAN_SOURCE, toolchain=self.tools["version"]
        )
        self.assertIsNotNone(loaded)
        graph = proof_graph.graph_from_lean(
            LEAN_SOURCE, kg_snapshot=loaded
        )
        self.assertEqual(
            graph["provenance"]["dependency_truncated_count"], 1
        )

        rows = _raw_rows(self._snapshot_copy())
        rows[0]["statementTruncated"] = True
        normalized = lean_kg._normalize(LEAN_SOURCE, rows)
        self.assertTrue(normalized[0]["statement_dependencies_truncated"])
        missing = copy.deepcopy(rows)
        missing[0].pop("proofTruncated")
        with self.assertRaisesRegex(ValueError, "truncation metadata"):
            lean_kg._normalize(LEAN_SOURCE, missing)

    def test_real_expression_depth_exhaustion_sets_truncation_flag(self) -> None:
        if not lean_broker.socket_status().get("core"):
            self.skipTest("fixed core broker socket is not provisioned")
        source = (
            "set_option maxRecDepth 5000\naxiom deep : "
            + ("Nat → " * 825)
            + "Nat\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "lean").mkdir()
            raw = lean_kg._run(workspace, source, False, 90)
        row = next(item for item in raw["rows"] if item["name"] == "deep")
        self.assertIs(row["statementTruncated"], True)
        self.assertIs(row["proofTruncated"], False)
        normalized = lean_kg._normalize(source, raw["rows"])
        self.assertTrue(normalized[0]["statement_dependencies_truncated"])

    def test_graph_cache_replaces_symlink_atomically_and_rejects_oversize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            sentinel = workspace / "sentinel"
            sentinel.write_text("untouched", encoding="utf-8")
            target = workspace / proof_graph.LEAN_GRAPH_FILENAME
            target.symlink_to(sentinel)
            self.assertIsNone(
                proof_graph.load_cached(workspace, kind="formal")
            )
            cached = {
                "kind": "formal",
                "graph": {
                    "title": "safe",
                    "nodes": [{
                        "id": "main", "kind": "conclusion",
                        "label": "main", "statement": "True",
                        "citations": [],
                    }],
                    "edges": [],
                },
            }
            proof_graph._write_cached(
                workspace, proof_graph.LEAN_GRAPH_FILENAME, cached
            )
            self.assertFalse(target.is_symlink())
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), "untouched"
            )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            loaded = proof_graph.load_cached(workspace, kind="formal")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["kind"], "formal")
            self.assertEqual(loaded["graph"], cached["graph"])
            target.write_text("[]", encoding="utf-8")
            self.assertIsNone(
                proof_graph.load_cached(workspace, kind="formal")
            )
            target.write_bytes(
                b"x" * (proof_graph.MAX_GRAPH_CACHE_BYTES + 1)
            )
            self.assertIsNone(
                proof_graph.load_cached(workspace, kind="formal")
            )

    def test_recursive_json_caches_fail_closed(self) -> None:
        nested = ("[" * 5000 + "0" + "]" * 5000).encode()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / proof_graph.LEAN_GRAPH_FILENAME).write_bytes(nested)
            (workspace / lean_kg.ARTIFACT_FILENAME).write_bytes(nested)
            self.assertIsNone(
                proof_graph.load_cached(workspace, kind="formal")
            )
            self.assertIsNone(lean_kg.load_snapshot(workspace))
            (workspace / auto_pipeline.STATE_FILENAME).write_bytes(nested)
            self.assertIsNone(auto_pipeline.load_state(workspace))

    def test_pipeline_state_and_signature_do_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            sentinel = root / "sentinel"
            sentinel.write_text("DO-NOT-EXPOSE", encoding="utf-8")
            (workspace / "proof.md").symlink_to(sentinel)
            (workspace / auto_pipeline.STATE_FILENAME).symlink_to(sentinel)
            self.assertEqual(
                auto_pipeline._source_signature(workspace, None), ""
            )
            self.assertIsNone(auto_pipeline.load_state(workspace))
            auto_pipeline._write_state(workspace, {"status": "safe"})
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), "DO-NOT-EXPOSE"
            )
            self.assertFalse(
                (workspace / auto_pipeline.STATE_FILENAME).is_symlink()
            )
            self.assertEqual(
                auto_pipeline.load_state(workspace)["status"], "safe"
            )

    def test_workspace_text_reader_rejects_file_and_parent_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            secret = outside / "secret"
            secret.write_text("DO-NOT-EXPOSE", encoding="utf-8")
            (workspace / "proof.md").symlink_to(secret)
            (workspace / "problem.txt").symlink_to(secret)
            (outside / "Proof.lean").write_text(
                "theorem leaked : True := by trivial\n", encoding="utf-8"
            )
            (workspace / "lean").symlink_to(outside, target_is_directory=True)
            for name in ("proof.md", "problem.txt", "lean/Proof.lean"):
                with self.subTest(name=name):
                    self.assertEqual(
                        proof_graph._read_workspace_text(
                            workspace, name, limit=proof_graph.MAX_SOURCE_BYTES
                        ),
                        "",
                    )

    def test_graph_input_bounds_fail_before_quadratic_fallback(self) -> None:
        too_many = "\n".join(
            f"def decl{index} : Nat := {index}"
            for index in range(proof_graph.MAX_LEAN_DECLARATIONS + 1)
        )
        with self.assertRaisesRegex(ValueError, "too many declarations"):
            proof_graph.graph_from_lean(too_many)
        with self.assertRaisesRegex(ValueError, "too many or invalid nodes"):
            proof_graph._sanitize({
                "nodes": [
                    {"id": f"n{index}", "kind": "step"}
                    for index in range(proof_graph.MAX_GRAPH_NODES + 1)
                ],
                "edges": [],
            })

    def test_workspace_cannot_forge_or_replay_an_unattested_compiler_snapshot(self) -> None:
        tmp, workspace = self._workspace_with_snapshot()
        self.addCleanup(tmp.cleanup)
        self.assertIsNotNone(
            lean_kg.load_snapshot(
                workspace, source=LEAN_SOURCE,
                toolchain=self.tools["version"],
            )
        )
        path = workspace / lean_kg.ARTIFACT_FILENAME
        forged = self._snapshot_copy()
        forged["declarations"][0]["compiler_kind"] = "forged"
        path.write_text(
            json.dumps(forged, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self.assertIsNone(
            lean_kg.load_snapshot(
                workspace, source=LEAN_SOURCE,
                toolchain=self.tools["version"],
            )
        )
        forged["status"] = "error"
        forged["error"] = "forged retry suppression"
        forged["declarations"] = []
        path.write_text(
            json.dumps(forged, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self.assertIsNone(
            lean_kg.load_snapshot(
                workspace, source=LEAN_SOURCE,
                toolchain=self.tools["version"],
            )
        )
        lean_kg._write(workspace, self._snapshot_copy())
        with lean_kg._ATTESTATION_LOCK:
            lean_kg._ATTESTED_SNAPSHOTS.pop(str(path.resolve()), None)
        self.assertIsNone(
            lean_kg.load_snapshot(
                workspace, source=LEAN_SOURCE,
                toolchain=self.tools["version"],
            )
        )

    def test_broker_release_change_invalidates_compiler_snapshot(self) -> None:
        tmp, workspace = self._workspace_with_snapshot()
        self.addCleanup(tmp.cleanup)
        self.assertIsNotNone(
            lean_kg.load_snapshot(
                workspace, source=LEAN_SOURCE,
                toolchain=self.tools["version"],
            )
        )
        _write_verified_cache(
            workspace,
            LEAN_SOURCE,
            version=self.tools["version"],
            seed="changed-release",
        )
        self.assertIsNone(
            lean_kg.load_snapshot(
                workspace, source=LEAN_SOURCE,
                toolchain=self.tools["version"],
            )
        )

    def test_real_narrow_mathlib_extraction_uses_fixed_broker(self) -> None:
        if not lean_broker.socket_status().get("mathlib"):
            self.skipTest("fixed Mathlib broker socket is not provisioned")
        raw = lean_kg._run(Path("/ignored"), MATHLIB_SOURCE, True, 90)
        self.assertEqual(raw["checker_profile"], "mathlib")
        self.assertEqual(raw["sandbox"], lean_broker.SANDBOX_MARKER)
        declarations = {
            item["qualified_name"]: item
            for item in lean_kg._normalize(MATHLIB_SOURCE, raw["rows"])
        }
        self.assertEqual(
            declarations["KgMathlib.main"]["local_proof_uses"],
            ["KgMathlib.helper"],
        )

    def test_broker_request_carries_no_api_claude_codex_or_host_secrets(self) -> None:
        marker = "0123456789abcdef01234567"
        captured: dict[str, object] = {}

        def checked(program: str, **kwargs):
            captured.update({"program": program, **kwargs})
            return _broker_result(program, marker=marker)

        with (
            patch.dict(os.environ, SECRET_ENV, clear=False),
            patch.object(lean_kg.secrets, "token_hex", return_value=marker),
            patch.object(lean_broker, "check_source", side_effect=checked),
        ):
            lean_kg._run(Path("/ignored"), LEAN_SOURCE, False, 90)
        self.assertEqual(set(captured), {"program", "uses_mathlib", "timeout"})
        program = str(captured["program"])
        self.assertFalse(any(secret in program for secret in SECRET_ENV.values()))

    def test_source_gate_rejects_executable_escape_hatches_but_ignores_lexical_decoys(self) -> None:
        rejected = {
            "run_cmd": "run_cmd do IO.println \"not allowed\"\n",
            "initialize": "initialize IO.println \"not allowed\"\n",
            "run_tac": (
                "example : True := by\n"
                "  run_tac Lean.logInfo \"not allowed\"\n"
                "  trivial\n"
            ),
            "eval": "#eval (1 + 1)\n",
            "skip_kernel": (
                "set_option debug.skipKernelTC true in\n"
                "theorem main : True := by trivial\n"
            ),
            "native_decide": (
                "theorem main : (1 : Nat) = 1 := by native_decide\n"
            ),
            "broad_mathlib": (
                "import Mathlib\n"
                "theorem main : True := by trivial\n"
            ),
            "local_import": (
                "import Local.Project\n"
                "theorem main : True := by trivial\n"
            ),
        }
        for name, source in rejected.items():
            with self.subTest(name=name):
                self.assertTrue(lean_kg._unsafe_source_reason(source))

        lexical_decoys = r'''import Mathlib.Data.Nat.Prime
/- run_cmd do IO.println "comment"
   /- initialize; run_tac; #eval 7; import Mathlib -/
   import Local.Project
-/
-- run_cmd do IO.println "line comment"
def note : String :=
  "run_cmd initialize run_tac #eval import Mathlib import Local.Project"
theorem main : True := by trivial
'''
        self.assertEqual(lean_kg._unsafe_source_reason(lexical_decoys), "")

    def test_kg_adapter_contains_no_local_systemd_execution_path(self) -> None:
        module_source = Path(lean_kg.__file__).read_text(encoding="utf-8")
        self.assertNotIn("systemd-run", module_source)
        self.assertNotIn("/usr/bin/sudo", module_source)
        self.assertNotIn("_sandbox_command", module_source)
        self.assertEqual(self.snapshot["sandbox"], lean_broker.SANDBOX_MARKER)

    def test_verified_malicious_run_cmd_is_rejected_before_worker_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            sentinel = workspace / "host-sentinel"
            sentinel.write_text("original", encoding="utf-8")
            source = f'''run_cmd do
  IO.FS.writeFile "{sentinel.as_posix()}" "overwritten"
theorem main : True := by trivial
'''
            _write_source(workspace, source)
            _write_verified_cache(
                workspace, source, version=str(self.tools["version"])
            )
            with patch.object(lean_broker, "check_source") as broker:
                result = lean_kg.extract_snapshot(
                    workspace, source=source, force=True
                )
            self.assertEqual(result["status"], "error")
            self.assertIn("run_cmd", result["error"])
            broker.assert_not_called()
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), "original"
            )

    def test_workspace_path_is_not_part_of_the_fixed_broker_request(self) -> None:
        marker = "0123456789abcdef01234567"
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            sentinel = workspace / "sentinel"
            sentinel.write_text("original", encoding="utf-8")
            captured: dict[str, object] = {}

            def checked(program: str, **kwargs):
                captured.update({"program": program, **kwargs})
                return _broker_result(program, marker=marker)

            with (
                patch.object(lean_kg.secrets, "token_hex", return_value=marker),
                patch.object(lean_broker, "check_source", side_effect=checked),
            ):
                lean_kg._run(workspace, LEAN_SOURCE, False, 10)
            self.assertNotIn(str(workspace), str(captured["program"]))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "original")

    def test_duplicate_leaf_name_graph_is_acyclic_and_has_no_own_label_edge(self) -> None:
        graph = proof_graph.graph_from_lean(
            LEAN_SOURCE, kg_snapshot=self._snapshot_copy()
        )
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        self.assertEqual(list(labels.values()).count("statementSeed"), 2)
        self.assertEqual(
            list(labels.values()).count("statementAndProof"), 2
        )
        adjacency: dict[str, set[str]] = {
            node_id: set() for node_id in labels
        }
        for edge in graph["edges"]:
            source, target = edge["from"], edge["to"]
            self.assertNotEqual(source, target)
            self.assertNotEqual(labels[source], labels[target])
            adjacency[source].add(target)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            self.assertNotIn(node_id, visiting, f"cycle through {node_id}")
            if node_id in visited:
                return
            visiting.add(node_id)
            for target in adjacency[node_id]:
                visit(target)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in labels:
            visit(node_id)

    def test_graph_sanitizer_drops_the_edge_that_would_close_a_cycle(self) -> None:
        graph = proof_graph._sanitize({
            "title": "cycle probe",
            "nodes": [
                {"id": "a", "kind": "assumption", "label": "a"},
                {"id": "b", "kind": "step", "label": "b"},
                {"id": "c", "kind": "conclusion", "label": "c"},
            ],
            "edges": [
                {"from": "a", "to": "b", "label": "first"},
                {"from": "b", "to": "c", "label": "second"},
                {"from": "c", "to": "a", "label": "closing"},
            ],
        })
        self.assertEqual(
            [(edge["from"], edge["to"]) for edge in graph["edges"]],
            [("a", "b"), ("b", "c")],
        )
        self.assertNotIn(
            "closing", [edge["label"] for edge in graph["edges"]]
        )

    def test_compiler_graph_uses_exact_edges_and_ignores_comment_and_text_names(self) -> None:
        graph = proof_graph.graph_from_lean(
            LEAN_SOURCE, kg_snapshot=self._snapshot_copy()
        )
        edges = {
            (edge["from"], edge["to"], edge["label"])
            for edge in graph["edges"]
        }
        self.assertIn(
            ("statementSeed", "statementOnly", "type mentions"), edges
        )
        self.assertIn(
            ("proofOnlySource", "proofOnlyConsumer", "proof uses"), edges
        )
        self.assertIn(
            ("statementSeed", "statementAndProof", "proof uses"), edges
        )
        self.assertIn(
            ("statementSeed_2", "statementAndProof_2", "proof uses"), edges
        )
        self.assertFalse(any(edge[1] == "main" for edge in edges), edges)
        self.assertEqual(graph["provenance"]["kind"], "lean-compiler")
        self.assertEqual(graph["provenance"]["matched_declarations"], 8)

    def test_obsolete_local_worker_helpers_are_absent(self) -> None:
        self.assertFalse(hasattr(lean_kg, "_safe_env"))
        self.assertFalse(hasattr(lean_kg, "_sandbox_command"))
        self.assertFalse(hasattr(lean_kg, "_bounded"))

    def test_snapshot_cache_is_deterministic_and_avoids_a_second_extraction(self) -> None:
        tmp, workspace = self._workspace_with_snapshot()
        self.addCleanup(tmp.cleanup)
        before = (workspace / lean_kg.ARTIFACT_FILENAME).read_bytes()
        with (
            patch.object(lean_verify, "toolchain_status", return_value=self.tools),
            patch.object(
                lean_kg, "_run", side_effect=AssertionError("cache miss")
            ) as run,
        ):
            cached = lean_kg.extract_snapshot(workspace, source=LEAN_SOURCE)
        self.assertEqual(cached, self.snapshot)
        run.assert_not_called()
        lean_kg._write(workspace, self._snapshot_copy())
        self.assertEqual(
            before, (workspace / lean_kg.ARTIFACT_FILENAME).read_bytes()
        )

    def test_corrupt_wrong_schema_wrong_toolchain_and_stale_cache_are_rejected(self) -> None:
        cases: list[tuple[str, object]] = []
        bad_schema = self._snapshot_copy()
        bad_schema["schema_version"] = lean_kg.SCHEMA_VERSION + 1
        cases.append(("schema", bad_schema))
        bad_producer = self._snapshot_copy()
        bad_producer["producer"] = "untrusted-producer"
        cases.append(("producer", bad_producer))
        bad_status = self._snapshot_copy()
        bad_status["status"] = "verified"
        cases.append(("status", bad_status))
        bad_declarations = self._snapshot_copy()
        bad_declarations["declarations"] = {"not": "a list"}
        cases.append(("declarations", bad_declarations))

        for name, value in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                _write_source(workspace)
                lean_kg._write(workspace, value)  # type: ignore[arg-type]
                self.assertIsNone(
                    lean_kg.load_snapshot(
                        workspace,
                        source=LEAN_SOURCE,
                        toolchain=self.tools["version"],
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_source(workspace)
            (workspace / lean_kg.ARTIFACT_FILENAME).write_text("{broken", encoding="utf-8")
            self.assertIsNone(lean_kg.load_snapshot(workspace, source=LEAN_SOURCE))

        tmp, workspace = self._workspace_with_snapshot()
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(
            lean_kg.load_snapshot(
                workspace, source=LEAN_SOURCE, toolchain="Lean wrong-version"
            )
        )
        changed = LEAN_SOURCE + "\ntheorem later : True := by trivial\n"
        self.assertIsNone(lean_kg.load_snapshot(workspace, source=changed))

    def test_raw_source_sha_mismatch_is_rejected_even_if_normalized_sha_matches(self) -> None:
        tmp, workspace = self._workspace_with_snapshot()
        self.addCleanup(tmp.cleanup)
        value = self._snapshot_copy()
        value["source_sha256"] = "0" * 64
        lean_kg._write(workspace, value)
        self.assertIsNone(
            lean_kg.load_snapshot(
                workspace,
                source=LEAN_SOURCE,
                toolchain=self.tools["version"],
            )
        )

    def test_symlink_artifact_and_symlink_source_are_rejected_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_source(workspace)
            outside = workspace / "outside.json"
            outside.write_text(json.dumps(self.snapshot), encoding="utf-8")
            (workspace / lean_kg.ARTIFACT_FILENAME).symlink_to(outside)
            self.assertIsNone(lean_kg.load_snapshot(workspace, source=LEAN_SOURCE))

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            lean_dir = workspace / "lean"
            lean_dir.mkdir()
            outside = workspace / "outside.lean"
            outside.write_text(LEAN_SOURCE, encoding="utf-8")
            (lean_dir / "Proof.lean").symlink_to(outside)
            with patch.object(lean_kg, "_run") as run:
                result = lean_kg.extract_snapshot(workspace, source=LEAN_SOURCE)
            self.assertEqual(result["status"], "error")
            self.assertIn("regular file", result["error"])
            run.assert_not_called()

    def test_requested_source_mismatch_is_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_source(workspace)
            with patch.object(lean_kg, "_run") as run:
                result = lean_kg.extract_snapshot(
                    workspace,
                    source=LEAN_SOURCE.replace("True", "False", 1),
                    force=True,
                )
            self.assertEqual(result["status"], "error")
            self.assertIn("differs", result["error"])
            run.assert_not_called()

    def test_unverified_and_stale_checker_records_cannot_trigger_extraction(self) -> None:
        for status, checked_source in (
            ("incomplete", LEAN_SOURCE),
            ("failed", LEAN_SOURCE),
            ("verified", LEAN_SOURCE + "\n-- stale checker copy\n"),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                _write_source(workspace)
                (workspace / lean_verify.RESULT_FILENAME).write_text(
                    json.dumps({"status": status, "lean": checked_source}),
                    encoding="utf-8",
                )
                with (
                    patch.object(lean_kg, "load_snapshot") as load,
                    patch.object(lean_kg, "extract_snapshot") as extract,
                ):
                    result = proof_graph._compiler_snapshot_for_verified_source(
                        workspace, LEAN_SOURCE
                    )
                self.assertIsNone(result)
                load.assert_not_called()
                extract.assert_not_called()

    def test_source_mutation_during_extraction_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = _write_source(workspace)
            _write_verified_cache(
                workspace, LEAN_SOURCE, version=str(self.tools["version"])
            )

            def mutate(*_args, **_kwargs):
                target.write_text(
                    LEAN_SOURCE + "\ntheorem changed : True := by trivial\n",
                    encoding="utf-8",
                )
                return {
                    "rows": _raw_rows(self.snapshot),
                    "duration_s": 0.01,
                    "toolchain": self.tools["version"],
                }

            with (
                patch.object(lean_verify, "toolchain_status", return_value=self.tools),
                patch.object(lean_kg, "_run", side_effect=mutate),
            ):
                result = lean_kg.extract_snapshot(
                    workspace, source=LEAN_SOURCE, force=True
                )
            self.assertEqual(result["status"], "error")
            self.assertIn("changed during extraction", result["error"])
            self.assertEqual(result["declarations"], [])

    def test_corrupt_extractor_output_fails_closed_and_is_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_source(workspace)
            _write_verified_cache(
                workspace, LEAN_SOURCE, version=str(self.tools["version"])
            )
            with (
                patch.object(lean_verify, "toolchain_status", return_value=self.tools),
                patch.object(
                    lean_kg, "_run", side_effect=ValueError("invalid KG JSON")
                ),
            ):
                result = lean_kg.extract_snapshot(
                    workspace, source=LEAN_SOURCE, force=True
                )
            self.assertEqual(result["status"], "error")
            self.assertIn("invalid KG JSON", result["error"])
            graph = proof_graph.graph_from_lean(LEAN_SOURCE, kg_snapshot=result)
            self.assertNotIn("provenance", graph)

    def test_empty_or_unmatched_extractor_output_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_source(workspace)
            _write_verified_cache(
                workspace, LEAN_SOURCE, version=str(self.tools["version"])
            )
            with (
                patch.object(lean_verify, "toolchain_status", return_value=self.tools),
                patch.object(
                    lean_kg,
                    "_run",
                    return_value={
                        "rows": [],
                        "duration_s": 0.01,
                        "toolchain": self.tools["version"],
                    },
                ),
            ):
                result = lean_kg.extract_snapshot(
                    workspace, source=LEAN_SOURCE, force=True
                )
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["declarations"], [])

    def test_snapshot_with_wrong_normalized_hash_has_no_compiler_provenance(self) -> None:
        stale = self._snapshot_copy()
        stale["normalized_source_sha256"] = hashlib.sha256(b"other").hexdigest()
        graph = proof_graph.graph_from_lean(LEAN_SOURCE, kg_snapshot=stale)
        self.assertNotIn("provenance", graph)

    def test_load_or_parse_formal_reports_compiler_provenance(self) -> None:
        tmp, workspace = self._workspace_with_snapshot()
        self.addCleanup(tmp.cleanup)
        (workspace / lean_verify.RESULT_FILENAME).write_text(
            json.dumps(
                {
                    "status": "verified",
                    "lean": LEAN_SOURCE,
                    "verified_source_sha256": hashlib.sha256(
                        (workspace / "lean" / "Proof.lean").read_bytes()
                    ).hexdigest(),
                    "uses_mathlib": False,
                    "toolchain": {"version": self.tools["version"]},
                }
            ),
            encoding="utf-8",
        )
        _write_verified_cache(
            workspace, LEAN_SOURCE, version=self.tools["version"]
        )
        with patch.object(lean_verify, "toolchain_status", return_value=self.tools):
            result = proof_graph.load_or_parse_formal(workspace)
        self.assertEqual(result["model"], "lean-compiler")
        self.assertEqual(result["knowledge_graph"]["status"], "ok")
        self.assertEqual(result["graph"]["provenance"]["kind"], "lean-compiler")
        self.assertEqual(
            result["source_sha256"], lean_kg.normalized_source_sha256(LEAN_SOURCE)
        )

    def test_formal_dag_cache_is_invalidated_when_lean_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            changed = LEAN_SOURCE + "\ntheorem later : True := by trivial\n"
            _write_source(workspace, changed)
            stale_graph = {
                "source_sha256": lean_kg.normalized_source_sha256(LEAN_SOURCE),
                "model": "stale-model",
                "graph": {
                    "title": "stale",
                    "nodes": [{"id": "stale", "kind": "conclusion"}],
                    "edges": [],
                },
            }
            (workspace / proof_graph.LEAN_GRAPH_FILENAME).write_text(
                json.dumps(stale_graph), encoding="utf-8"
            )
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps({"status": "verified", "lean": LEAN_SOURCE}),
                encoding="utf-8",
            )
            with patch.object(lean_kg, "extract_snapshot") as extract:
                result = proof_graph.load_or_parse_formal(workspace)
            self.assertNotEqual(result["model"], "stale-model")
            self.assertEqual(result["model"], "lean-parse")
            self.assertNotEqual(result["graph"]["title"], "stale")
            self.assertEqual(
                result["source_sha256"],
                lean_kg.normalized_source_sha256(changed),
            )
            extract.assert_not_called()

    def test_automatic_formal_dag_uses_compiler_cache_without_any_model_route(self) -> None:
        tmp, workspace = self._workspace_with_snapshot()
        self.addCleanup(tmp.cleanup)
        (workspace / lean_verify.RESULT_FILENAME).write_text(
            json.dumps(
                {
                    "status": "verified",
                    "lean": LEAN_SOURCE,
                    "verified_source_sha256": hashlib.sha256(
                        (workspace / "lean" / "Proof.lean").read_bytes()
                    ).hexdigest(),
                    "uses_mathlib": False,
                    "toolchain": {"version": self.tools["version"]},
                }
            ),
            encoding="utf-8",
        )
        _write_verified_cache(
            workspace, LEAN_SOURCE, version=self.tools["version"]
        )
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={}),
            patch.object(lean_verify, "toolchain_status", return_value=self.tools),
            patch.object(proof_graph, "_call_llm", side_effect=ValueError("no API")),
            patch.object(proof_graph, "_run_claude_prompt") as claude,
            patch.object(proof_graph, "_run_codex_prompt") as codex,
        ):
            result = proof_graph.generate(
                workspace=workspace,
                run_record={"auth_route": "api"},
                user=None,
                kind="formal",
                allow_codex_fallback=False,
            )
        claude.assert_not_called()
        codex.assert_not_called()
        self.assertEqual(result["model"], "lean-compiler")
        self.assertEqual(result["graph"]["provenance"]["kind"], "lean-compiler")


if __name__ == "__main__":
    unittest.main()
