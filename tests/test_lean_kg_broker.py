from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import lean_broker, lean_kg, lean_verify


SOURCE = """namespace BrokerKg

theorem helper : True := by trivial
theorem main : True := helper

end BrokerKg
"""

ROWS = [
    {
        "name": "BrokerKg.helper",
        "kind": "theorem",
        "statementDeps": ["True"],
        "proofDeps": ["True.intro"],
        "statementTruncated": False,
        "proofTruncated": False,
    },
    {
        "name": "BrokerKg.main",
        "kind": "theorem",
        "statementDeps": ["True"],
        "proofDeps": ["BrokerKg.helper"],
        "statementTruncated": False,
        "proofTruncated": False,
    },
]


def _identity(seed: str = "release") -> dict[str, str]:
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


def _broker_result(
    program: str,
    *,
    marker: str,
    profile: str = "core",
    rows: list[dict] | None = None,
    seed: str = "release",
) -> dict:
    return {
        "protocol": lean_broker.PROTOCOL_VERSION,
        "profile": profile,
        "status": "completed",
        "exit_code": 0,
        "stdout": (
            f"AGENT_MONITOR_LEAN_KG::{marker}::"
            + json.dumps(ROWS if rows is None else rows, separators=(",", ":"))
            + "\n"
        ),
        "stderr": "",
        "error": "",
        "duration_ms": 8,
        "duration": 0.008,
        "timed_out": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "source_sha256": hashlib.sha256(program.encode()).hexdigest(),
        "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
        "sandbox": lean_broker.SANDBOX_MARKER,
        **_identity(seed),
    }


def _write_verified_cache(
    workspace: Path,
    source: str,
    *,
    uses_mathlib: bool = False,
    seed: str = "release",
) -> None:
    exact_sha = hashlib.sha256(source.encode()).hexdigest()
    profile = "mathlib" if uses_mathlib else "core"
    identity = _identity(seed)
    receipt = {
        "ok": True,
        "status": "verified",
        "exit_code": 0,
        "source_sha256": exact_sha,
        "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
        "sandbox": lean_broker.SANDBOX_MARKER,
        "checker_profile": profile,
        **identity,
    }
    toolchain = {
        "available": True,
        "version": lean_broker.EXPECTED_TOOLCHAIN,
        "checker_sandbox": lean_broker.SANDBOX_MARKER,
        "checker_profile": profile,
        **identity,
    }
    (workspace / lean_verify.RESULT_FILENAME).write_text(
        json.dumps(
            {
                "status": "verified",
                "lean": source,
                "verified_source_sha256": exact_sha,
                "uses_mathlib": uses_mathlib,
                "kernel_receipt": receipt,
                "toolchain": toolchain,
            }
        ),
        encoding="utf-8",
    )


class LeanKgBrokerTests(unittest.TestCase):
    def test_run_uses_only_fixed_broker_and_persists_release_identity(self) -> None:
        marker = "0123456789abcdef01234567"
        captured: dict[str, object] = {}

        def checked(program: str, **kwargs):
            captured.update({"program": program, **kwargs})
            return _broker_result(program, marker=marker)

        with (
            patch.object(lean_kg.secrets, "token_hex", return_value=marker),
            patch.object(lean_broker, "check_source", side_effect=checked) as broker,
        ):
            result = lean_kg._run(Path("/untrusted/workspace"), SOURCE, False, 77)

        broker.assert_called_once()
        self.assertEqual(captured["uses_mathlib"], False)
        self.assertEqual(captured["timeout"], 77.0)
        self.assertIn("namespace AgentMonitorKg_", str(captured["program"]))
        self.assertEqual(result["rows"], ROWS)
        self.assertEqual(result["checker_profile"], "core")
        self.assertEqual(result["sandbox"], lean_broker.SANDBOX_MARKER)
        self.assertEqual(
            {key: result[key] for key in _identity()}, _identity()
        )

    def test_broker_failure_has_no_local_execution_fallback(self) -> None:
        with patch.object(
            lean_broker,
            "check_source",
            side_effect=lean_broker.LeanBrokerError("missing socket"),
        ) as broker:
            with self.assertRaisesRegex(ValueError, "no local fallback"):
                lean_kg._run(Path("/untrusted/workspace"), SOURCE, False, 40)
        broker.assert_called_once()
        module_source = Path(lean_kg.__file__).read_text(encoding="utf-8")
        self.assertNotIn("systemd-run", module_source)
        self.assertNotIn("/usr/bin/sudo", module_source)
        self.assertNotIn("toolchain_status()", module_source)
        self.assertNotIn("_mathlib_project()", module_source)

    def test_run_rejects_nonzero_truncated_and_invalid_provenance(self) -> None:
        marker = "0123456789abcdef01234567"

        def result(**updates):
            program = "import Lean\n" + SOURCE.rstrip() + lean_kg._instrumentation(
                marker
            )
            value = _broker_result(program, marker=marker)
            value.update(updates)
            return value

        cases = {
            "nonzero": result(exit_code=1, stderr="compile failed"),
            "truncated": result(stdout_truncated=True),
            "sandbox": result(sandbox="untrusted"),
            "profile": result(profile="mathlib"),
            "source": result(source_sha256="0" * 64),
            "release": result(release_id="0" * 64),
        }
        for name, broker_result in cases.items():
            with self.subTest(name=name), patch.object(
                lean_kg.secrets, "token_hex", return_value=marker
            ), patch.object(
                lean_broker, "check_source", return_value=broker_result
            ):
                with self.assertRaises(ValueError):
                    lean_kg._run(Path("/untrusted/workspace"), SOURCE, False, 40)

    def test_snapshot_cache_is_bound_to_checker_release_and_tree_hashes(self) -> None:
        marker = "0123456789abcdef01234567"
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            (workspace / "lean").mkdir()
            (workspace / "lean" / "Proof.lean").write_text(
                SOURCE, encoding="utf-8"
            )
            _write_verified_cache(workspace, SOURCE)

            def checked(program: str, **_kwargs):
                return _broker_result(program, marker=marker)

            with (
                patch.object(lean_kg.secrets, "token_hex", return_value=marker),
                patch.object(lean_broker, "check_source", side_effect=checked),
            ):
                snapshot = lean_kg.extract_snapshot(
                    workspace, source=SOURCE, force=True
                )

            self.assertEqual(snapshot["status"], "ok", snapshot)
            self.assertEqual(snapshot["checker_profile"], "core")
            self.assertEqual(
                {key: snapshot[key] for key in _identity()}, _identity()
            )
            self.assertIsNotNone(
                lean_kg.load_snapshot(
                    workspace,
                    source=SOURCE,
                    toolchain=lean_broker.EXPECTED_TOOLCHAIN,
                )
            )

            _write_verified_cache(workspace, SOURCE, seed="new-release")
            self.assertIsNone(
                lean_kg.load_snapshot(
                    workspace,
                    source=SOURCE,
                    toolchain=lean_broker.EXPECTED_TOOLCHAIN,
                )
            )

    def test_mathlib_profile_is_selected_by_the_fixed_socket_route(self) -> None:
        marker = "0123456789abcdef01234567"

        def checked(program: str, **kwargs):
            self.assertIs(kwargs["uses_mathlib"], True)
            return _broker_result(program, marker=marker, profile="mathlib")

        with (
            patch.object(lean_kg.secrets, "token_hex", return_value=marker),
            patch.object(lean_broker, "check_source", side_effect=checked),
        ):
            result = lean_kg._run(Path("/ignored"), SOURCE, True, 200)
        self.assertEqual(result["checker_profile"], "mathlib")

    def test_real_broker_kg_smoke_when_provisioned(self) -> None:
        sockets = lean_broker.socket_status()
        if not sockets.get("core"):
            self.skipTest("fixed core broker socket is not provisioned")
        result = lean_kg._run(Path("/ignored"), SOURCE, False, 90)
        self.assertEqual(result["checker_profile"], "core")
        self.assertTrue(any(row.get("name") == "BrokerKg.main" for row in result["rows"]))


if __name__ == "__main__":
    unittest.main()
