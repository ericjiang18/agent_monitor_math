from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PREPARER = ROOT / "deploy" / "public-kimi" / "prepare_release.py"


def _load_preparer():
    spec = importlib.util.spec_from_file_location("public_kimi_prepare_release", PREPARER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


preparer = _load_preparer()


class PublicKimiReleaseTests(unittest.TestCase):
    def _stage(self, root: Path) -> Path:
        (root / "venv" / "bin").mkdir(parents=True)
        python = root / "venv" / "bin" / "python"
        python.write_bytes(b"#!/bin/sh\nexit 1\n")
        python.chmod(0o755)
        (root / "agent_monitor").mkdir()
        (root / "agent_monitor" / "public_kimi_server.py").write_text(
            "# reviewed test fixture\n", encoding="utf-8"
        )
        for relative in preparer._REQUIRED_PUBLIC_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# reviewed test fixture\n", encoding="utf-8")
        (root / preparer.MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "python": "venv/bin/python",
                    "module": "agent_monitor.public_kimi_server",
                    "model": "kimi-k3",
                    "engine_allowlist": ["plain"],
                    "auto_pipeline": False,
                    "source_revision": "test-only",
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_measurement_is_stable_and_content_addressed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stage = self._stage(Path(raw))
            first = preparer.measure_tree(stage)
            second = preparer.measure_tree(stage)
            self.assertEqual(first, second)
            self.assertRegex(first, r"^[0-9a-f]{64}$")
            (stage / "agent_monitor" / "public_kimi_server.py").write_text(
                "# changed fixture\n", encoding="utf-8"
            )
            self.assertNotEqual(first, preparer.measure_tree(stage))

    def test_manifest_fails_closed_on_harness_or_pipeline_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stage = self._stage(Path(raw))
            manifest = json.loads((stage / preparer.MANIFEST_NAME).read_text())
            manifest["engine_allowlist"].append("codex")
            (stage / preparer.MANIFEST_NAME).write_text(json.dumps(manifest))
            with self.assertRaisesRegex(preparer.PreparationError, "engine_allowlist"):
                preparer.validate_manifest(stage)

            manifest["engine_allowlist"] = ["plain"]
            manifest["auto_pipeline"] = True
            (stage / preparer.MANIFEST_NAME).write_text(json.dumps(manifest))
            with self.assertRaisesRegex(preparer.PreparationError, "automatic Lean/DAG"):
                preparer.validate_manifest(stage)

    def test_manifest_binds_exact_execstart_and_public_files(self) -> None:
        cases = (
            ("module", "agent_monitor.public_kimi_gateway", "module must be exactly"),
            ("python", "venv/bin/alternate", "python must be exactly"),
        )
        for field, value, message in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                stage = self._stage(Path(raw))
                if field == "python":
                    alternate = stage / value
                    alternate.write_text("#!/bin/sh\n", encoding="utf-8")
                    alternate.chmod(0o755)
                manifest_path = stage / preparer.MANIFEST_NAME
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest[field] = value
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(preparer.PreparationError, message):
                    preparer.validate_manifest(stage)

        with tempfile.TemporaryDirectory() as raw:
            stage = self._stage(Path(raw))
            (stage / "agent_monitor/web/public_kimi.js").unlink()
            with self.assertRaisesRegex(preparer.PreparationError, "required public"):
                preparer.validate_manifest(stage)

    def test_tree_hash_records_are_unambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as left_raw, tempfile.TemporaryDirectory() as right_raw:
            left = self._stage(Path(left_raw))
            right = self._stage(Path(right_raw))
            prefix = b"opaque-prefix"
            payload = b"active-pth-payload"
            legacy_boundary = b"F\0agent_monitor/b\0R\0"
            (left / "agent_monitor/a").write_bytes(prefix + legacy_boundary + payload)
            (right / "agent_monitor/a").write_bytes(prefix)
            (right / "agent_monitor/b").write_bytes(payload)
            self.assertNotEqual(preparer.measure_tree(left), preparer.measure_tree(right))

    def test_escaping_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside:
            stage = self._stage(Path(raw))
            external = Path(outside) / "secret"
            external.write_text("must not copy", encoding="utf-8")
            os.symlink(external, stage / "escape")
            with self.assertRaisesRegex(preparer.PreparationError, "escaping"):
                preparer.measure_tree(stage)

    def test_manifest_rejects_symlinked_python(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stage = self._stage(Path(raw))
            python = stage / "venv" / "bin" / "python"
            python.unlink()
            os.symlink("../../agent_monitor/public_kimi_server.py", python)
            with self.assertRaisesRegex(preparer.PreparationError, "regular file"):
                preparer.validate_manifest(stage)

    def test_release_path_policy_rejects_private_application_assets(self) -> None:
        cases = (
            (Path(".env"), "secret=value", "forbidden release path"),
            (Path("agent_monitor/users.db"), "private", "private-data"),
            (
                Path("agent_monitor/web/console.html"),
                "private UI",
                "non-public web asset",
            ),
            (Path("tests/test_private.py"), "pass", "fixed root policy"),
        )
        for relative, content, message in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as raw:
                stage = self._stage(Path(raw))
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(preparer.PreparationError, message):
                    preparer.measure_tree(stage)

    def test_prepare_atomically_publishes_only_the_pinned_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as target:
            stage = self._stage(Path(raw))
            expected = preparer.measure_tree(stage)
            install_root = Path(target) / "install"
            releases_root = install_root / "releases"
            with (
                mock.patch.object(preparer, "INSTALL_ROOT", install_root),
                mock.patch.object(preparer, "RELEASES_ROOT", releases_root),
                mock.patch.object(preparer.os, "geteuid", return_value=0),
                mock.patch.object(preparer.os, "chown"),
                mock.patch.object(preparer.os, "fchown"),
            ):
                self.assertEqual(
                    preparer.prepare(stage, expected_tree_sha256=expected),
                    expected,
                )
                published = releases_root / expected
                self.assertEqual(
                    preparer.prepare(stage, expected_tree_sha256=expected),
                    expected,
                )
                self.assertEqual(
                    sorted(path.name for path in releases_root.iterdir()),
                    [expected],
                )


if __name__ == "__main__":
    unittest.main()
