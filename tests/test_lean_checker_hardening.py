from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import lean_broker, lean_verify


SOURCE = "theorem main (n : Nat) : n = n := by rfl\n"


def _release_fields() -> dict[str, str]:
    toolchain_tree = "1" * 64
    mathlib_tree = "2" * 64
    identity = {
        "schema": 1,
        "lean_toolchain": lean_broker.EXPECTED_LEAN_TOOLCHAIN,
        "lean_version": lean_broker.EXPECTED_TOOLCHAIN,
        "mathlib_input_rev": lean_broker.EXPECTED_MATHLIB_INPUT_REV,
        "mathlib_rev": lean_broker.EXPECTED_MATHLIB_REV,
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }
    encoded = (
        json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    return {
        "release_id": hashlib.sha256(encoded).hexdigest(),
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }


def _broker_result(source: str, *, sandbox: str | None = None) -> dict:
    return {
        "protocol": 1,
        "profile": "core",
        "status": "completed",
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "error": "",
        "duration_ms": 5,
        "duration": 0.005,
        "timed_out": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
        "sandbox": sandbox or lean_broker.SANDBOX_MARKER,
        **_release_fields(),
    }


class LeanCheckerHardeningTests(unittest.TestCase):
    def test_cache_symlink_is_not_read_and_atomic_write_preserves_sentinel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            sentinel = workspace / "sentinel.json"
            sentinel.write_text('{"secret": true}', encoding="utf-8")
            cache = workspace / lean_verify.RESULT_FILENAME
            cache.symlink_to(sentinel)

            self.assertIsNone(lean_verify.load_cached(workspace))
            lean_verify._write_cached(workspace, {"status": "failed"})

            self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"secret": true}')
            self.assertFalse(cache.is_symlink())
            self.assertTrue(stat.S_ISREG(cache.lstat().st_mode))
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
            self.assertEqual(lean_verify.load_cached(workspace), {"status": "failed"})

    def test_cache_rejects_non_object_oversize_deep_json_and_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            cache = workspace / lean_verify.RESULT_FILENAME
            cache.write_text("[]", encoding="utf-8")
            self.assertIsNone(lean_verify.load_cached(workspace))

            cache.write_text(
                "[" * 10_000 + "0" + "]" * 10_000,
                encoding="utf-8",
            )
            self.assertIsNone(lean_verify.load_cached(workspace))

            with cache.open("wb") as stream:
                stream.truncate(lean_verify.MAX_RESULT_CACHE_BYTES + 1)
            self.assertIsNone(lean_verify.load_cached(workspace))

            cache.unlink()
            os.mkfifo(cache)
            self.assertIsNone(lean_verify.load_cached(workspace))

    def test_broker_success_produces_source_bound_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ldir = Path(raw) / "lean"
            ldir.mkdir()
            (ldir / lean_verify.PROOF_FILENAME).write_text(
                SOURCE, encoding="utf-8"
            )
            with patch.object(
                lean_broker,
                "check_source",
                return_value=_broker_result(SOURCE),
            ) as checked:
                result = lean_verify._run_lean_check(ldir, False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["sandbox"], lean_broker.SANDBOX_MARKER)
        self.assertEqual(
            result["source_sha256"],
            hashlib.sha256(SOURCE.encode()).hexdigest(),
        )
        checked.assert_called_once_with(
            SOURCE,
            uses_mathlib=False,
            timeout=100.0,
        )
        self.assertIsNotNone(lean_verify._trusted_kernel_receipt(result))

    def test_broker_failure_has_no_local_process_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ldir = Path(raw) / "lean"
            ldir.mkdir()
            (ldir / lean_verify.PROOF_FILENAME).write_text(
                SOURCE, encoding="utf-8"
            )
            with (
                patch.object(
                    lean_broker,
                    "check_source",
                    side_effect=lean_broker.LeanBrokerError("missing socket"),
                ),
                patch.object(lean_verify.subprocess, "Popen") as spawned,
            ):
                result = lean_verify._run_lean_check(ldir, False)

        self.assertFalse(result["ok"])
        self.assertIn("any local fallback", result["stderr"])
        spawned.assert_not_called()

    def test_untrusted_sandbox_marker_cannot_create_receipt(self) -> None:
        result = _broker_result(SOURCE, sandbox="systemd-dynamic-user")
        result.update({"ok": True, "status": "verified"})
        self.assertIsNone(lean_verify._trusted_kernel_receipt(result))

    def test_atomic_source_write_replaces_symlink_without_touching_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            sentinel = workspace / "sentinel.lean"
            sentinel.write_text("sentinel\n", encoding="utf-8")
            (ldir / lean_verify.PROOF_FILENAME).symlink_to(sentinel)

            def checked(check_dir: Path, uses_mathlib: bool) -> dict:
                del uses_mathlib
                source = (check_dir / lean_verify.PROOF_FILENAME).read_text(
                    encoding="utf-8"
                )
                result = _broker_result(source)
                result.update({"ok": True, "status": "verified"})
                return result

            with (
                patch.object(lean_verify, "_write_scaffold"),
                patch.object(
                    lean_verify, "_run_lean_check", side_effect=checked
                ),
            ):
                result = lean_verify._write_and_run(workspace, SOURCE, False)

            target = ldir / lean_verify.PROOF_FILENAME
            self.assertTrue(result["ok"])
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), SOURCE)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "sentinel\n")

    def test_harness_artifact_reader_is_bounded_and_no_follow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            target = ldir / lean_verify.PROOF_FILENAME
            with target.open("wb") as stream:
                stream.truncate(lean_verify.LEAN_CHECK_MAX_SOURCE_BYTES + 1)

            source, fingerprint, error = (
                lean_verify._regular_root_lean_artifact(workspace)
            )
            self.assertIsNone(source)
            self.assertIsNone(fingerprint)
            self.assertIn("source-size limit", error)

            target.unlink()
            sentinel = workspace / "sentinel.lean"
            sentinel.write_text(SOURCE, encoding="utf-8")
            target.symlink_to(sentinel)
            source, fingerprint, error = (
                lean_verify._regular_root_lean_artifact(workspace)
            )
            self.assertIsNone(source)
            self.assertIsNone(fingerprint)
            self.assertIn("safely read", error)

    def test_safe_empty_workspace_files_do_not_revive_stale_cache(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            (ldir / lean_verify.PROOF_FILENAME).write_text("", encoding="utf-8")
            (ldir / "harness.log").write_text("", encoding="utf-8")
            cached = {
                "lean": "theorem stale : True := by trivial\n",
                "log": "stale harness transcript",
            }

            self.assertEqual(lean_verify.read_source(workspace), "")
            transcript = lean_verify._harness_conversation(workspace, cached)
            self.assertEqual(transcript["events"], [])

    def test_workspace_sidecar_reads_reject_symlinks_and_use_safe_fallbacks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            secret = workspace / "secret.txt"
            secret.write_text(
                "TOP SECRET\n# References\n[1] leaked citation\n",
                encoding="utf-8",
            )
            for path in (
                workspace / "problem.txt",
                workspace / "proof.md",
                ldir / lean_verify.PROOF_FILENAME,
                ldir / "TASK.md",
                ldir / "harness.log",
            ):
                path.symlink_to(secret)

            cached_source = "theorem cached : True := by trivial\n"
            cached = {
                "status": "failed",
                "lean": cached_source,
                "log": "cached harness diagnostic",
                "attempts": [],
                "fidelity": {},
                "chat": [],
            }
            lean_verify._write_cached(workspace, cached)

            self.assertEqual(lean_verify.read_source(workspace), cached_source)
            self.assertEqual(
                lean_verify._problem_and_proof(workspace),
                ("", ""),
            )
            self.assertEqual(
                lean_verify._citations_from_proof_md(workspace),
                [],
            )
            transcript = lean_verify._harness_conversation(workspace, cached)
            rendered = "\n".join(
                str(item.get("content") or "") for item in transcript["events"]
            )
            self.assertIn("cached harness diagnostic", rendered)
            self.assertNotIn("TOP SECRET", rendered)
            summary = lean_verify.monitor_summary(workspace)
            self.assertEqual(summary["harness_prompt"], "")

    def test_workspace_sidecar_reads_reject_oversize_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            sizes = {
                workspace / "problem.txt":
                    lean_verify.MAX_WORKSPACE_PROBLEM_BYTES + 1,
                workspace / "proof.md":
                    lean_verify.MAX_WORKSPACE_SOURCE_BYTES + 1,
                ldir / lean_verify.PROOF_FILENAME:
                    lean_verify.LEAN_CHECK_MAX_SOURCE_BYTES + 1,
                ldir / "TASK.md": lean_verify.MAX_HARNESS_TASK_BYTES + 1,
                ldir / "harness.log": lean_verify.MAX_HARNESS_LOG_BYTES + 1,
            }
            for path, size in sizes.items():
                with path.open("wb") as stream:
                    stream.truncate(size)

            cached_source = "theorem cached : True := by trivial\n"
            cached = {
                "status": "failed",
                "lean": cached_source,
                "log": "bounded cached diagnostic",
                "attempts": [],
                "fidelity": {},
                "chat": [],
            }
            lean_verify._write_cached(workspace, cached)

            self.assertEqual(lean_verify.read_source(workspace), cached_source)
            self.assertEqual(
                lean_verify._problem_and_proof(workspace),
                ("", ""),
            )
            self.assertEqual(
                lean_verify._citations_from_proof_md(workspace),
                [],
            )
            transcript = lean_verify._harness_conversation(workspace, cached)
            rendered = "\n".join(
                str(item.get("content") or "") for item in transcript["events"]
            )
            self.assertIn("bounded cached diagnostic", rendered)
            summary = lean_verify.monitor_summary(workspace)
            self.assertEqual(summary["harness_prompt"], "")

    def test_isolated_codex_output_and_scaffolds_are_bounded_and_no_follow(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            ldir = workspace / lean_verify.LEAN_DIRNAME
            ldir.mkdir()
            secret = workspace / "outside-toolchain"
            secret.write_text("malicious toolchain", encoding="utf-8")
            (ldir / "lean-toolchain").symlink_to(secret)
            with (ldir / "lakefile.lean").open("wb") as stream:
                stream.truncate(lean_verify.MAX_WORKSPACE_SOURCE_BYTES + 1)
            (ldir / "lake-manifest.json").write_text("{}", encoding="utf-8")

            for output_kind in ("symlink", "oversize"):
                with self.subTest(output_kind=output_kind):
                    def fake_run(_argv, **kwargs):
                        isolated = Path(kwargs["cwd"])
                        self.assertFalse((isolated / "lean-toolchain").exists())
                        self.assertFalse((isolated / "lakefile.lean").exists())
                        self.assertEqual(
                            (isolated / "lake-manifest.json").read_text(
                                encoding="utf-8"
                            ),
                            "{}",
                        )
                        target = isolated / lean_verify.PROOF_FILENAME
                        target.unlink()
                        if output_kind == "symlink":
                            target.symlink_to(secret)
                        else:
                            with target.open("wb") as stream:
                                stream.truncate(
                                    lean_verify.LEAN_CHECK_MAX_SOURCE_BYTES + 1
                                )
                        event = {
                            "type": "item.completed",
                            "item": {
                                "id": "message",
                                "type": "agent_message",
                                "text": "finished",
                            },
                        }
                        return lean_verify.subprocess.CompletedProcess(
                            ["codex"],
                            0,
                            stdout=json.dumps(event),
                            stderr="",
                        )

                    with (
                        patch(
                            "agent_monitor.codex_login.account_login_ready",
                            return_value=True,
                        ),
                        patch(
                            "agent_monitor.codex_login.account_home",
                            return_value=workspace / "account",
                        ),
                        patch(
                            "agent_monitor.settings.codex_enabled",
                            return_value=True,
                        ),
                        patch(
                            "agent_monitor.engines_registry.build_cli_command",
                            return_value=["codex"],
                        ),
                        patch.object(
                            lean_verify,
                            "_harness_env",
                            return_value={},
                        ),
                        patch.object(
                            lean_verify.subprocess,
                            "run",
                            side_effect=fake_run,
                        ),
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "safe regular Proof\\.lean",
                        ):
                            lean_verify._codex_subscription_interaction(
                                workspace=workspace,
                                user={"id": 7},
                                prompt="Revise safely.",
                                lean_source=SOURCE,
                                edit=True,
                            )

    def test_comment_and_string_decoys_do_not_create_flags_or_declarations(
        self,
    ) -> None:
        source = """
/- outer
  /- nested -/
  axiom fake : False
  theorem invented : True := by native_decide
  set_option debug.skipKernelTC true
-/
def message : String := "axiom stringFake : False; native_decide"
/-- Real declaration.
Cites: self-derived.
-/
theorem main (n : Nat) : n = n := by rfl
"""
        flags = lean_verify._fidelity_flags(source)
        ids = {item["id"] for item in flags}
        self.assertTrue(
            {
                "axiom",
                "native_decide",
                "skip_kernel_tc",
                "trivial_conclusion",
            }.isdisjoint(ids)
        )
        declarations = lean_verify._decls_with_docs(source)
        self.assertEqual(
            [(item["kind"], item["name"]) for item in declarations],
            [("def", "message"), ("theorem", "main")],
        )
        self.assertIn("Cites: self-derived.", declarations[-1]["doc"])


if __name__ == "__main__":
    unittest.main()
