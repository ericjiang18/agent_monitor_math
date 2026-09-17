from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import lean_broker, lean_verify


_TOOLCHAIN_TREE = "1" * 64
_MATHLIB_TREE = "2" * 64
_IDENTITY = {
    "schema": 1,
    "lean_toolchain": lean_broker.EXPECTED_LEAN_TOOLCHAIN,
    "lean_version": lean_broker.EXPECTED_TOOLCHAIN,
    "mathlib_input_rev": lean_broker.EXPECTED_MATHLIB_INPUT_REV,
    "mathlib_rev": lean_broker.EXPECTED_MATHLIB_REV,
    "toolchain_tree_sha256": _TOOLCHAIN_TREE,
    "mathlib_tree_sha256": _MATHLIB_TREE,
}
_RELEASE_ID = hashlib.sha256(
    (
        json.dumps(_IDENTITY, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
).hexdigest()
_CHECKED = {
    "ok": True,
    "status": "verified",
    "exit_code": 0,
    "duration_s": 0.01,
    "command": ["lean-broker", "core"],
    "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
    "sandbox": lean_broker.SANDBOX_MARKER,
    "checker_profile": "core",
    "release_id": _RELEASE_ID,
    "toolchain_tree_sha256": _TOOLCHAIN_TREE,
    "mathlib_tree_sha256": _MATHLIB_TREE,
    "stderr": "",
    "stdout": "",
}


def _checked_source(source: str) -> dict:
    result = dict(_CHECKED)
    payload = (source.rstrip() + "\n").encode("utf-8")
    result["source_sha256"] = hashlib.sha256(payload).hexdigest()
    return result


def _checked_write(_workspace: Path, source: str, _uses_mathlib: bool) -> dict:
    return _checked_source(source)


class FormalHarnessHardeningTests(unittest.TestCase):
    def tearDown(self) -> None:
        with lean_verify._HARNESS_LOCK:
            lean_verify._HARNESS_JOBS.clear()
        with lean_verify._FORMAL_OPERATIONS_GUARD:
            lean_verify._FORMAL_OPERATIONS.clear()
        with lean_verify._FORMAL_IO_LOCKS_GUARD:
            lean_verify._FORMAL_IO_LOCKS.clear()

    @staticmethod
    def _finish(
        workspace: Path,
        *,
        initial_fingerprint: dict | None,
        exit_code: int | None,
        stopped: bool = False,
        log: str = "child diagnostics",
    ) -> dict:
        return lean_verify._harness_finish(
            workspace=workspace,
            engine="deepagents",
            label="DeepAgents",
            log=log,
            exit_code=exit_code,
            elapsed=0.2,
            user=None,
            run_record=None,
            model=None,
            problem="Prove True.",
            informal="Trivial.",
            audit=False,
            initial_fingerprint=initial_fingerprint,
            stopped=stopped,
        )

    @staticmethod
    def _source(workspace: Path, text: str) -> Path:
        ldir = workspace / lean_verify.LEAN_DIRNAME
        ldir.mkdir(exist_ok=True)
        target = ldir / lean_verify.PROOF_FILENAME
        target.write_text(text, encoding="utf-8")
        return target

    def test_nonzero_child_cannot_promote_compiling_fresh_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = self._source(workspace, "theorem old : True := by trivial\n")
            _old, initial, _reason = lean_verify._regular_root_lean_artifact(workspace)
            target.write_text("theorem fresh : True := by trivial\n", encoding="utf-8")
            with (
                patch.object(
                    lean_verify, "_write_and_run", side_effect=_checked_write
                ),
                patch.object(lean_verify, "toolchain_status", return_value={}),
            ):
                result = self._finish(
                    workspace,
                    initial_fingerprint=initial,
                    exit_code=1,
                    log="structured-response failure after writing the file",
                )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["harness"]["outer_check_ok"])
        self.assertTrue(result["harness"]["artifact_fresh"])
        self.assertFalse(result["harness"]["artifact_accepted"])
        self.assertIn("theorem fresh", result["lean"])
        self.assertIn("child process exited 1", result["notes"])
        self.assertIn("artifact rejected", result["log"])

    def test_exit_zero_cannot_promote_unchanged_pre_run_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._source(workspace, "theorem stale : True := by trivial\n")
            _old, initial, _reason = lean_verify._regular_root_lean_artifact(workspace)
            with (
                patch.object(
                    lean_verify, "_write_and_run", side_effect=_checked_write
                ),
                patch.object(lean_verify, "toolchain_status", return_value={}),
            ):
                result = self._finish(
                    workspace,
                    initial_fingerprint=initial,
                    exit_code=0,
                )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["harness"]["artifact_fresh"])
        self.assertFalse(result["harness"]["artifact_accepted"])
        self.assertIn("unchanged", result["notes"])

    def test_symlink_artifact_is_rejected_without_following_or_compiling_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            outside = workspace / "outside.lean"
            outside.write_text("theorem outside : True := by trivial\n", encoding="utf-8")
            (ldir / lean_verify.PROOF_FILENAME).symlink_to(outside)
            (workspace / lean_verify.RESULT_FILENAME).write_text(
                json.dumps({"lean": "theorem cached : True := by trivial\n"}),
                encoding="utf-8",
            )
            with (
                patch.object(lean_verify, "_write_and_run") as checked,
                patch.object(lean_verify, "toolchain_status", return_value={}),
            ):
                result = self._finish(
                    workspace,
                    initial_fingerprint=None,
                    exit_code=0,
                )

        checked.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["harness"]["artifact_regular"])
        self.assertIn("symbolic link", result["notes"])
        self.assertIn("theorem cached", result["lean"])
        self.assertEqual(result["log"], "child diagnostics")

    def test_exit_zero_fresh_regular_outer_checked_artifact_can_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = self._source(workspace, "theorem old : True := by trivial\n")
            _old, initial, _reason = lean_verify._regular_root_lean_artifact(workspace)
            target.write_text("theorem fresh : True := by trivial\n", encoding="utf-8")
            with (
                patch.object(
                    lean_verify, "_write_and_run", side_effect=_checked_write
                ),
                patch.object(lean_verify, "toolchain_status", return_value={}),
            ):
                result = self._finish(
                    workspace,
                    initial_fingerprint=initial,
                    exit_code=0,
                )

        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["harness"]["artifact_accepted"])
        self.assertTrue(result["harness"]["outer_check_ok"])

    def test_reservation_collapses_path_aliases_but_not_other_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "proof-a"
            workspace.mkdir()
            alias = root / "proof-a-alias"
            alias.symlink_to(workspace, target_is_directory=True)
            other = root / "proof-b"
            other.mkdir()

            token = lean_verify._reserve_formal_operation(workspace, "generate")
            self.assertEqual(
                lean_verify._harness_key(workspace),
                lean_verify._harness_key(alias),
            )
            with self.assertRaisesRegex(ValueError, "already running"):
                lean_verify._reserve_formal_operation(alias, "check")
            other_token = lean_verify._reserve_formal_operation(other, "check")
            lean_verify._release_formal_operation(other, other_token)
            lean_verify._release_formal_operation(workspace, token)

    def test_sync_and_background_guards_release_after_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaisesRegex(ValueError, "No Proof.lean"):
                lean_verify.compile_or_check(workspace=workspace)
            token = lean_verify._reserve_formal_operation(workspace, "after-sync-error")
            lean_verify._release_formal_operation(workspace, token)

            with self.assertRaisesRegex(ValueError, "Unknown harness"):
                lean_verify.start_harness(
                    workspace=workspace,
                    run_record=None,
                    user=None,
                    engine="not-a-harness",
                )
            token = lean_verify._reserve_formal_operation(workspace, "worker")
            with patch.object(
                lean_verify,
                "_harness_worker",
                side_effect=RuntimeError("worker failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "worker failed"):
                    lean_verify._harness_worker_guarded(
                        operation_token=token,
                        workspace=workspace,
                    )
            released = lean_verify._reserve_formal_operation(workspace, "after-worker")
            lean_verify._release_formal_operation(workspace, released)

    def test_write_and_check_rejects_source_mutated_during_kernel_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()

            def mutate(_ldir: Path, *, uses_mathlib: bool) -> dict:
                del uses_mathlib
                result = _checked_source(
                    (_ldir / lean_verify.PROOF_FILENAME).read_text(
                        encoding="utf-8"
                    )
                )
                (_ldir / lean_verify.PROOF_FILENAME).write_text(
                    "theorem replaced : True := by trivial\n",
                    encoding="utf-8",
                )
                return result

            with (
                patch.object(lean_verify, "_write_scaffold"),
                patch.object(lean_verify, "_run_lean_check", side_effect=mutate),
            ):
                result = lean_verify._write_and_run(
                    workspace,
                    "theorem expected : True := by trivial\n",
                    False,
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("changed while Lean was checking", result["stderr"])


    def test_missing_exit_and_stopped_run_cannot_verify_fresh_artifact(self) -> None:
        cases = (
            (None, False, "failed"),
            (0, True, "stopped"),
        )
        for exit_code, stopped, expected_status in cases:
            with self.subTest(exit_code=exit_code, stopped=stopped):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    target = self._source(
                        workspace,
                        "theorem old : True := by trivial\n",
                    )
                    _old, initial, _reason = (
                        lean_verify._regular_root_lean_artifact(workspace)
                    )
                    target.write_text(
                        "theorem fresh : True := by trivial\n",
                        encoding="utf-8",
                    )
                    with (
                        patch.object(
                            lean_verify,
                            "_write_and_run",
                            side_effect=_checked_write,
                        ),
                        patch.object(lean_verify, "toolchain_status", return_value={}),
                    ):
                        result = self._finish(
                            workspace,
                            initial_fingerprint=initial,
                            exit_code=exit_code,
                            stopped=stopped,
                        )

                self.assertEqual(result["status"], expected_status)
                self.assertTrue(result["harness"]["outer_check_ok"])
                self.assertFalse(result["harness"]["artifact_accepted"])


    def test_shared_derived_tools_are_not_advertised_as_the_selected_harness(self) -> None:
        package = Path(lean_verify.__file__).resolve().parent
        pipeline = (package / "auto_pipeline.py").read_text(encoding="utf-8")
        console = (package / "web" / "console.html").read_text(encoding="utf-8")

        self.assertNotIn('"single_harness": True', pipeline)
        self.assertNotIn('"harness": selected', pipeline)
        self.assertIn('"proof_engine": selected', pipeline)
        self.assertNotIn("Only this agent harness is used", console)
        self.assertIn("Derived · shared Lean/DAG tools", console)


if __name__ == "__main__":
    unittest.main()
