from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from agent_monitor import lean_broker


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "lean-broker"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worker = _load(
    "test_agent_monitor_lean_broker_worker",
    DEPLOY / "agent_monitor_lean_broker_worker.py",
)
prepare = _load(
    "test_agent_monitor_lean_prepare_release",
    DEPLOY / "prepare_release.py",
)


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
    raw = (
        json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    return {
        "release_id": hashlib.sha256(raw).hexdigest(),
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }


def _response(base_profile: str, source: bytes, **updates: object) -> bytes:
    value: dict[str, object] = {
        "protocol": 1,
        "profile": base_profile,
        "status": "completed",
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "error": "",
        "duration_ms": 12,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "toolchain": worker.EXPECTED_TOOLCHAIN,
        "sandbox": worker.SANDBOX_MARKER,
        **_release_fields(),
    }
    value.update(updates)
    body = json.dumps(value).encode("utf-8")
    return lean_broker._HEADER.pack(lean_broker.RESPONSE_MAGIC, len(body)) + body


class OneShotServer:
    def __init__(self, path: Path, reply: bytes | None, delay: float = 0.0):
        self.path = path
        self.reply = reply
        self.delay = delay
        self.request = b""
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.path))
            server.listen(1)
            self.ready.set()
            connection, _ = server.accept()
            with connection:
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    self.request += chunk
                if self.delay:
                    time.sleep(self.delay)
                if self.reply is not None:
                    try:
                        connection.sendall(self.reply)
                    except BrokenPipeError:
                        pass

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(2):
            raise RuntimeError("test broker did not start")

    def join(self) -> None:
        self.thread.join(2)


class LeanBrokerClientTests(unittest.TestCase):
    def test_source_only_frame_and_validated_response(self) -> None:
        source = "example : True := by trivial\n"
        payload = source.encode()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "core.sock"
            server = OneShotServer(path, _response("core", payload))
            server.start()
            with patch.object(
                lean_broker,
                "_SOCKET_PATHS",
                {"core": path, "mathlib": Path(raw) / "mathlib.sock"},
            ):
                result = lean_broker.check_source(source)
            server.join()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)
        magic, length = lean_broker._HEADER.unpack(
            server.request[: lean_broker._HEADER.size]
        )
        self.assertEqual(magic, lean_broker.REQUEST_MAGIC)
        self.assertEqual(length, len(payload))
        self.assertEqual(server.request[lean_broker._HEADER.size :], payload)

    def test_mathlib_profile_is_selected_only_by_fixed_socket(self) -> None:
        source = "import Mathlib.Data.Int.Notation\n"
        payload = source.encode()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "mathlib.sock"
            server = OneShotServer(path, _response("mathlib", payload))
            server.start()
            with patch.object(
                lean_broker,
                "_SOCKET_PATHS",
                {"core": Path(raw) / "core.sock", "mathlib": path},
            ):
                result = lean_broker.check_source(source, uses_mathlib=True)
            server.join()
        self.assertEqual(result["profile"], "mathlib")

    def test_no_unsandboxed_fallback_when_socket_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            lean_broker,
            "_SOCKET_PATHS",
            {
                "core": Path(raw) / "missing.sock",
                "mathlib": Path(raw) / "also-missing.sock",
            },
        ):
            with self.assertRaisesRegex(lean_broker.LeanBrokerError, "unavailable"):
                lean_broker.check_source("example : True := by trivial\n")

    def test_source_and_client_timeout_bounds(self) -> None:
        with self.assertRaisesRegex(lean_broker.LeanBrokerError, "exceeded"):
            lean_broker.check_source("x" * (lean_broker.MAX_SOURCE_BYTES + 1))
        for timeout in (
            0,
            lean_broker.MAX_CLIENT_TIMEOUT + 1,
            True,
            float("nan"),
        ):
            with self.assertRaisesRegex(lean_broker.LeanBrokerError, "timeout"):
                lean_broker.check_source("x", timeout=timeout)  # type: ignore[arg-type]

    def test_response_attestations_are_required(self) -> None:
        source = b"example : True := by trivial\n"
        cases = (
            {"profile": "mathlib"},
            {"source_sha256": "0" * 64},
            {"sandbox": "not-isolated"},
            {"toolchain": "Lean 4.14-ish"},
            {"stdout_truncated": "false"},
            {"status": "completed", "exit_code": None},
            {"status": "error", "exit_code": 1},
            {"stdout": "\ud800"},
            {"release_id": "0" * 64},
            {"toolchain_tree_sha256": "not-a-sha"},
        )
        for index, update in enumerate(cases):
            with self.subTest(update=update), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / f"core-{index}.sock"
                server = OneShotServer(path, _response("core", source, **update))
                server.start()
                with patch.object(
                    lean_broker,
                    "_SOCKET_PATHS",
                    {"core": path, "mathlib": Path(raw) / "mathlib.sock"},
                ):
                    with self.assertRaises(lean_broker.LeanBrokerError):
                        lean_broker.check_source(source.decode())
                server.join()

    def test_oversized_or_stalled_response_fails_closed(self) -> None:
        source = b"example : True := by trivial\n"
        oversized = lean_broker._HEADER.pack(
            lean_broker.RESPONSE_MAGIC, lean_broker.MAX_RESPONSE_BYTES + 1
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "oversized.sock"
            server = OneShotServer(path, oversized)
            server.start()
            with patch.object(
                lean_broker,
                "_SOCKET_PATHS",
                {"core": path, "mathlib": Path(raw) / "mathlib.sock"},
            ):
                with self.assertRaisesRegex(lean_broker.LeanBrokerError, "length"):
                    lean_broker.check_source(source.decode())
            server.join()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "stalled.sock"
            server = OneShotServer(path, None, delay=0.2)
            server.start()
            with patch.object(
                lean_broker,
                "_SOCKET_PATHS",
                {"core": path, "mathlib": Path(raw) / "mathlib.sock"},
            ):
                with self.assertRaisesRegex(lean_broker.LeanBrokerError, "timed out"):
                    lean_broker.check_source(source.decode(), timeout=0.05)
            server.join()

    def test_socket_status_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            real = Path(raw) / "real.sock"
            alias = Path(raw) / "alias.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(real))
                alias.symlink_to(real)
                with patch.object(
                    lean_broker,
                    "_SOCKET_PATHS",
                    {"core": real, "mathlib": alias},
                ):
                    self.assertEqual(
                        lean_broker.socket_status(),
                        {"core": True, "mathlib": False},
                    )

    def test_socket_status_rejects_inaccessible_socket(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "core.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                with (
                    patch.object(
                        lean_broker,
                        "_SOCKET_PATHS",
                        {"core": path},
                    ),
                    patch.object(lean_broker.os, "access", return_value=False),
                ):
                    self.assertEqual(
                        lean_broker.socket_status(), {"core": False}
                    )


def _decode_worker_frame(value: bytes) -> dict[str, object]:
    magic, length = worker.HEADER.unpack(value[: worker.HEADER.size])
    if magic != worker.RESPONSE_MAGIC:
        raise AssertionError("bad worker response magic")
    body = value[worker.HEADER.size :]
    if len(body) != length:
        raise AssertionError("worker emitted more or less than one frame")
    decoded = json.loads(body.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise AssertionError("worker response is not an object")
    return decoded


class LeanBrokerWorkerTests(unittest.TestCase):
    def test_one_protocol_frame_never_contains_raw_lean_output(self) -> None:
        source = b"example : True := by trivial\n"
        request = worker.HEADER.pack(worker.REQUEST_MAGIC, len(source)) + source
        writer = io.BytesIO()
        config = worker.BrokerConfig(
            profile="core",
            release_root=Path("/fixed"),
            lean=Path("/fixed/lean"),
            toolchain=worker.EXPECTED_TOOLCHAIN,
            timeout_sec=60,
            **_release_fields(),
        )
        result = {
            "status": "completed",
            "exit_code": 0,
            "stdout": "LEAN-RAW-OUTPUT",
            "stderr": "",
            "error": "",
            "duration_ms": 1,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        with (
            patch.object(worker, "load_config", return_value=config),
            patch.object(worker, "attest_config"),
            patch.object(worker, "run_fixed", return_value=result),
        ):
            self.assertEqual(worker.serve_one("core", io.BytesIO(request), writer), 0)
        value = writer.getvalue()
        decoded = _decode_worker_frame(value)
        self.assertEqual(decoded["stdout"], "LEAN-RAW-OUTPUT")
        self.assertNotIn(b"LEAN-RAW-OUTPUT", value[: worker.HEADER.size])
        self.assertEqual(decoded["source_sha256"], hashlib.sha256(source).hexdigest())

    def test_request_cannot_select_command_profile_or_path(self) -> None:
        source = (
            b'{"profile":"mathlib","argv":["/bin/sh"],'
            b'"path":"/etc/shadow"}\n'
        )
        request = worker.HEADER.pack(worker.REQUEST_MAGIC, len(source)) + source
        self.assertEqual(worker.read_request(io.BytesIO(request)), source)

    def test_malformed_request_returns_one_bounded_error_frame(self) -> None:
        writer = io.BytesIO()
        code = worker.serve_one("core", io.BytesIO(b"bad"), writer)
        self.assertEqual(code, 0)
        self.assertLessEqual(len(writer.getvalue()), worker.MAX_RESPONSE_BYTES + 12)
        decoded = _decode_worker_frame(writer.getvalue())
        self.assertEqual(decoded["status"], "error")
        self.assertEqual(decoded["source_sha256"], "")

    def test_release_identity_file_binds_both_tree_digests(self) -> None:
        fields = _release_fields()
        identity = {
            "schema": 1,
            "lean_toolchain": worker.EXPECTED_LEAN_TOOLCHAIN,
            "lean_version": worker.EXPECTED_TOOLCHAIN,
            "mathlib_input_rev": worker.EXPECTED_MATHLIB_INPUT_REV,
            "mathlib_rev": worker.EXPECTED_MATHLIB_REV,
            "toolchain_tree_sha256": fields["toolchain_tree_sha256"],
            "mathlib_tree_sha256": fields["mathlib_tree_sha256"],
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "IDENTITY.json").write_text(
                json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
            )
            with patch.object(worker, "_assert_release_file"):
                self.assertEqual(
                    worker._release_identity(root, fields["release_id"]),
                    (
                        fields["toolchain_tree_sha256"],
                        fields["mathlib_tree_sha256"],
                    ),
                )
                with self.assertRaisesRegex(worker.BrokerFailure, "does not match"):
                    worker._release_identity(root, "0" * 64)

    def test_fixed_runner_truncates_output(self) -> None:
        config = worker.BrokerConfig(
            profile="core",
            release_root=Path(sys.prefix),
            lean=Path(sys.executable),
            toolchain=worker.EXPECTED_TOOLCHAIN,
            timeout_sec=5,
        )
        source = (
            "import sys\n"
            f"sys.stdout.write('x' * {worker.MAX_STREAM_BYTES + 4096})\n"
        ).encode()
        result = worker.run_fixed(config, source)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(str(result["stdout"])), worker.MAX_STREAM_BYTES)
        self.assertIs(result["stdout_truncated"], True)

    def test_fixed_runner_kills_timed_out_process_group(self) -> None:
        config = worker.BrokerConfig(
            profile="core",
            release_root=Path(sys.prefix),
            lean=Path(sys.executable),
            toolchain=worker.EXPECTED_TOOLCHAIN,
            timeout_sec=0.08,
        )
        started = time.monotonic()
        result = worker.run_fixed(config, b"import time\ntime.sleep(10)\n")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["error"])

    def test_fixed_runner_passes_no_source_controlled_argv(self) -> None:
        marker = "--profile=mathlib /etc/shadow"
        config = worker.BrokerConfig(
            profile="core",
            release_root=Path(sys.prefix),
            lean=Path(sys.executable),
            toolchain=worker.EXPECTED_TOOLCHAIN,
            timeout_sec=5,
        )
        source = f"import sys\nprint(sys.argv)\n# {marker}\n".encode()
        result = worker.run_fixed(config, source)
        self.assertEqual(result["exit_code"], 0)
        output = str(result["stdout"])
        self.assertNotIn(marker, output)
        self.assertNotIn("/etc/shadow", output)
    def test_mathlib_runner_marks_only_manifest_packages_safe_for_git(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            mathlib = root / "mathlib"
            packages = mathlib / ".lake" / "packages"
            for name in ("mathlib", "batteries"):
                (packages / name / ".git").mkdir(parents=True)
            (mathlib / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {"name": "mathlib"},
                            {"name": "batteries"},
                        ]
                    }
                )
            )
            config = worker.BrokerConfig(
                profile="mathlib",
                release_root=root,
                lean=Path(sys.executable),
                lake=Path(sys.executable),
                mathlib=mathlib,
                toolchain=worker.EXPECTED_TOOLCHAIN,
                timeout_sec=5,
            )
            captured: dict[str, object] = {}

            class FinishedProcess:
                pid = os.getpid()
                returncode = 0

                @staticmethod
                def poll() -> int:
                    return 0

            def fake_popen(
                *args: object, **kwargs: object
            ) -> FinishedProcess:
                captured.update(kwargs)
                return FinishedProcess()

            with patch.object(
                worker, "_assert_release_directory"
            ), patch.object(
                worker.subprocess, "Popen", side_effect=fake_popen
            ):
                result = worker.run_fixed(
                    config, b"example : True := by trivial\n"
                )

        self.assertEqual(result["exit_code"], 0)
        child_env = captured["env"]
        self.assertIsInstance(child_env, dict)
        assert isinstance(child_env, dict)
        self.assertEqual(child_env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(child_env["GIT_CONFIG_KEY_0"], "safe.directory")
        self.assertEqual(
            child_env["GIT_CONFIG_VALUE_0"], str(packages / "mathlib")
        )
        self.assertEqual(
            child_env["GIT_CONFIG_VALUE_1"], str(packages / "batteries")
        )
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("ANTHROPIC_API_KEY", child_env)


    def test_config_rejects_nonfixed_executable_even_if_env_is_root_owned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trust = Path(raw) / "releases"
            release_id = "a" * 64
            release = trust / release_id
            (release / "toolchain" / "bin").mkdir(parents=True)
            (release / "toolchain" / "bin" / "lean").write_text("x")
            env = {
                "AMLB_RELEASE_ID": release_id,
                "AMLB_RELEASE_ROOT": str(release),
                "AMLB_LEAN": "/bin/sh",
                "AMLB_TOOLCHAIN": worker.EXPECTED_TOOLCHAIN,
                "AMLB_TIMEOUT_SEC": "60",
            }
            with patch.object(worker, "TRUST_ROOT", trust), patch.dict(
                os.environ, env, clear=True
            ), patch.object(worker, "_assert_root_owned_chain"), patch.object(
                worker, "_assert_release_directory"
            ), patch.object(
                worker, "_release_identity", return_value=("1" * 64, "2" * 64)
            ):
                with self.assertRaisesRegex(worker.BrokerFailure, "fixed release"):
                    worker.load_config("core")


class LeanBrokerReleasePreparationTests(unittest.TestCase):
    def test_regularizes_internal_file_symlink_and_sets_immutable_modes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "source"
            destination = Path(raw) / "destination"
            source.mkdir()
            target = source / "library.so.1"
            target.write_bytes(b"library")
            os.chmod(target, 0o755)
            (source / "library.so").symlink_to("library.so.1")
            with patch.object(prepare.os, "chown"), patch.object(
                prepare.os, "fchown"
            ):
                digest = prepare.copy_tree_regularized(source, destination)
                measured = prepare.measure_tree_regularized(source)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertEqual(measured, digest)
            self.assertFalse((destination / "library.so").is_symlink())
            self.assertEqual((destination / "library.so").read_bytes(), b"library")
            self.assertEqual(
                (destination / "library.so").stat().st_mode & 0o777, 0o555
            )
            self.assertEqual(destination.stat().st_mode & 0o777, 0o555)

    def test_rejects_escaping_symlink_and_special_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root / "outside"
            outside.write_text("secret")
            source = root / "source"
            source.mkdir()
            (source / "escape").symlink_to(outside)
            with patch.object(prepare.os, "chown"), patch.object(
                prepare.os, "fchown"
            ):
                with self.assertRaisesRegex(prepare.PreparationError, "symlink"):
                    prepare.copy_tree_regularized(source, root / "destination")
            (source / "escape").unlink()
            os.mkfifo(source / "fifo")
            with patch.object(prepare.os, "chown"), patch.object(
                prepare.os, "fchown"
            ):
                with self.assertRaisesRegex(prepare.PreparationError, "special"):
                    prepare.copy_tree_regularized(source, root / "destination2")

    def test_source_root_symlink_is_rejected_before_release_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            toolchain = root / "toolchain"
            mathlib = root / "mathlib"
            (toolchain / "bin").mkdir(parents=True)
            mathlib.mkdir()
            alias = root / "toolchain-alias"
            alias.symlink_to(toolchain, target_is_directory=True)
            with self.assertRaisesRegex(prepare.PreparationError, "real directory"):
                prepare.validate_source_identity(alias, mathlib)

    def test_exact_mathlib_revision_is_required_without_executing_lean(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            toolchain = root / "toolchain"
            mathlib = root / "mathlib"
            (toolchain / "bin").mkdir(parents=True)
            mathlib.mkdir()
            for name in ("lean", "lake"):
                path = toolchain / "bin" / name
                path.write_text("not executed")
                os.chmod(path, 0o755)
            (mathlib / "lean-toolchain").write_text(prepare.EXPECTED_LEAN_TOOLCHAIN)
            manifest = {
                "packages": [
                    {
                        "name": "mathlib",
                        "inputRev": prepare.EXPECTED_MATHLIB_INPUT_REV,
                        "rev": prepare.EXPECTED_MATHLIB_REV,
                    }
                ]
            }
            (mathlib / "lake-manifest.json").write_text(json.dumps(manifest))
            with patch("subprocess.run", side_effect=AssertionError("must not execute")):
                prepare.validate_source_identity(toolchain, mathlib)
            manifest["packages"][0]["rev"] = "0" * 40
            (mathlib / "lake-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(prepare.PreparationError, "not exact"):
                prepare.validate_source_identity(toolchain, mathlib)


class LeanBrokerUnitInvariantTests(unittest.TestCase):
    def test_worker_units_enforce_required_isolation(self) -> None:
        required = (
            "DynamicUser=yes",
            "NoNewPrivileges=yes",
            "PrivateNetwork=yes",
            "PrivatePIDs=yes",
            "ProtectHome=yes",
            "InaccessiblePaths=/run",
            "RestrictAddressFamilies=none",
            "SystemCallFilter=~@network-io",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "StandardInput=socket",
            "StandardOutput=socket",
            "ProtectSystem=strict",
            "MemoryMax=",
            "RuntimeMaxSec=",
            "TasksMax=",
        )
        for profile in ("core", "mathlib"):
            path = DEPLOY / f"agent-monitor-lean-{profile}@.service"
            text = path.read_text()
            with self.subTest(profile=profile):
                for item in required:
                    self.assertIn(item, text)
                self.assertNotIn("sudo", text)
                self.assertNotIn("systemd-run", text)
                self.assertIn(f"--profile={profile}", text)

    def test_socket_units_are_root_owned_group_gated_and_accept_one_worker(self) -> None:
        for profile in ("core", "mathlib"):
            text = (DEPLOY / f"agent-monitor-lean-{profile}.socket").read_text()
            with self.subTest(profile=profile):
                self.assertIn("Accept=yes", text)
                self.assertIn("SocketUser=root", text)
                self.assertIn("SocketGroup=agent-monitor-lean", text)
                self.assertIn("SocketMode=0660", text)
                self.assertIn("MaxConnections=8", text)

    def test_application_dropin_is_installed_only_after_both_smokes(self) -> None:
        installer = (DEPLOY / "install.sh").read_text()
        smoke = installer.index("/usr/libexec/agent-monitor-lean-broker-smoke")
        dropin = installer.index("lean-broker.conf")
        self.assertLess(smoke, dropin)
        self.assertNotIn("restart agent-monitor", installer)
        dropin_text = (DEPLOY / "agent-monitor-lean-client.conf").read_text()
        self.assertIn("NoNewPrivileges=yes", dropin_text)
        self.assertIn("SupplementaryGroups=agent-monitor-lean", dropin_text)

    def test_install_defaults_to_no_activation(self) -> None:
        installer = (DEPLOY / "install.sh").read_text()
        self.assertIn("activate=0", installer)
        self.assertIn("if [[ ${activate} -eq 0 ]]", installer)
        self.assertFalse(
            any(line.lstrip().startswith("sudo ") for line in installer.splitlines())
        )
        self.assertIn("--expected-toolchain-tree-sha256", installer)
        self.assertIn("--expected-mathlib-tree-sha256", installer)


if __name__ == "__main__":
    unittest.main()
