#!/usr/bin/python3
"""Prepare an immutable, root-owned Lean 4.14 + Mathlib broker release.

No source-tree executable is run by this program.  Symlinks that resolve to a
regular file inside the same source tree are copied as regular files; every
other symlink, special entry, mount crossing, and concurrent inode replacement
is rejected.  Publication is one same-filesystem rename into a content-addressed
release directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path


INSTALL_ROOT = Path("/opt/agent-monitor-lean")
RELEASES_ROOT = INSTALL_ROOT / "releases"
ENV_ROOT = Path("/etc/agent-monitor/lean-broker")

EXPECTED_LEAN_TOOLCHAIN = "leanprover/lean4:v4.14.0"
EXPECTED_MATHLIB_INPUT_REV = "v4.14.0"
EXPECTED_MATHLIB_REV = "4bbdccd9c5f862bf90ff12f0a9e2c8be032b9a84"
EXPECTED_TOOLCHAIN = (
    "Lean (version 4.14.0, x86_64-unknown-linux-gnu, "
    "commit 410fab728470, Release)"
)


class PreparationError(ValueError):
    pass


def _read_regular(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise PreparationError(f"metadata file is invalid: {path}")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise PreparationError(f"metadata file is oversized: {path}")
        return bytes(data)
    finally:
        os.close(fd)


def validate_source_identity(toolchain: Path, mathlib: Path) -> None:
    for root, label in ((toolchain, "toolchain"), (mathlib, "Mathlib")):
        if root.is_symlink() or not root.is_dir() or root.resolve() != root:
            raise PreparationError(f"{label} source root must be a real directory")
    lean = toolchain / "bin" / "lean"
    lake = toolchain / "bin" / "lake"
    if not lean.is_file() or not os.access(lean, os.X_OK):
        raise PreparationError("Lean 4.14 executable is unavailable")
    if not lake.is_file() or not os.access(lake, os.X_OK):
        raise PreparationError("Lake executable is unavailable")
    try:
        lean_toolchain = _read_regular(mathlib / "lean-toolchain", 256)
        manifest = json.loads(
            _read_regular(mathlib / "lake-manifest.json", 1024 * 1024).decode(
                "utf-8"
            )
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparationError("Mathlib identity metadata is invalid") from exc
    if lean_toolchain.decode("utf-8").strip() != EXPECTED_LEAN_TOOLCHAIN:
        raise PreparationError("Mathlib does not target exact Lean v4.14.0")
    packages = manifest.get("packages") if isinstance(manifest, dict) else None
    mathlib_entry = next(
        (
            item
            for item in packages or []
            if isinstance(item, dict) and item.get("name") == "mathlib"
        ),
        None,
    )
    if not isinstance(mathlib_entry, dict) or (
        mathlib_entry.get("inputRev") != EXPECTED_MATHLIB_INPUT_REV
        or mathlib_entry.get("rev") != EXPECTED_MATHLIB_REV
    ):
        raise PreparationError("Mathlib mirror is not exact v4.14.0/4bbdccd9")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _copy_open_file(
    source: Path,
    destination: Path | None,
    *,
    expected_device: int,
    expected_inode: int | None,
    executable: bool,
    digest: "hashlib._Hash",
    relative: Path,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    try:
        source_info = os.fstat(source_fd)
        if (
            not stat.S_ISREG(source_info.st_mode)
            or source_info.st_dev != expected_device
            or (
                expected_inode is not None
                and source_info.st_ino != expected_inode
            )
        ):
            raise PreparationError(f"source entry changed or crossed a mount: {source}")
        destination_fd = -1
        if destination is not None:
            destination_fd = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        try:
            digest.update(b"F\0" + relative.as_posix().encode("utf-8") + b"\0")
            digest.update(b"X\0" if executable else b"R\0")
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if destination_fd >= 0:
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_fd, view)
                        if written <= 0:
                            raise PreparationError("release copy made no progress")
                        view = view[written:]
            if destination_fd >= 0:
                os.fsync(destination_fd)
                os.fchmod(destination_fd, 0o555 if executable else 0o444)
                os.fchown(destination_fd, 0, 0)
        finally:
            if destination_fd >= 0:
                os.close(destination_fd)
    finally:
        os.close(source_fd)


def _copy_directory(
    source: Path,
    destination: Path | None,
    *,
    source_root: Path,
    source_device: int,
    digest: "hashlib._Hash",
    relative: Path,
) -> None:
    before = source.lstat()
    if not stat.S_ISDIR(before.st_mode) or before.st_dev != source_device:
        raise PreparationError(f"directory crossed a mount or changed: {source}")
    if destination is not None:
        destination.mkdir(mode=0o700)
        os.chown(destination, 0, 0)
    digest.update(b"D\0" + relative.as_posix().encode("utf-8") + b"\0")
    try:
        entries = sorted(os.scandir(source), key=lambda item: item.name)
    except OSError as exc:
        raise PreparationError(f"could not scan source directory: {source}") from exc
    for entry in entries:
        source_path = Path(entry.path)
        destination_path = destination / entry.name if destination is not None else None
        child_relative = relative / entry.name
        info = entry.stat(follow_symlinks=False)
        if info.st_dev != source_device:
            raise PreparationError(f"cross-device source entry is forbidden: {source_path}")
        if stat.S_ISDIR(info.st_mode):
            _copy_directory(
                source_path,
                destination_path,
                source_root=source_root,
                source_device=source_device,
                digest=digest,
                relative=child_relative,
            )
        elif stat.S_ISREG(info.st_mode):
            _copy_open_file(
                source_path,
                destination_path,
                expected_device=source_device,
                expected_inode=info.st_ino,
                executable=bool(info.st_mode & 0o111),
                digest=digest,
                relative=child_relative,
            )
        elif stat.S_ISLNK(info.st_mode):
            try:
                target = source_path.resolve(strict=True)
                target_info = target.lstat()
            except OSError as exc:
                raise PreparationError(f"broken source symlink: {source_path}") from exc
            if (
                not _inside(target, source_root)
                or not stat.S_ISREG(target_info.st_mode)
                or target_info.st_dev != source_device
            ):
                raise PreparationError(
                    f"escaping, directory, or cross-device symlink is forbidden: {source_path}"
                )
            _copy_open_file(
                target,
                destination_path,
                expected_device=source_device,
                expected_inode=target_info.st_ino,
                executable=bool(target_info.st_mode & 0o111),
                digest=digest,
                relative=child_relative,
            )
        else:
            raise PreparationError(f"special source entry is forbidden: {source_path}")
    after = source.lstat()
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise PreparationError(f"source directory changed during copy: {source}")
    if destination is not None:
        directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fchmod(directory_fd, 0o555)
            os.fchown(directory_fd, 0, 0)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def copy_tree_regularized(source: Path, destination: Path) -> str:
    if source.is_symlink():
        raise PreparationError("release source root must not be a symlink")
    source = source.resolve(strict=True)
    if source.is_symlink() or not source.is_dir():
        raise PreparationError("release source must be a real directory")
    source_info = source.lstat()
    digest = hashlib.sha256()
    _copy_directory(
        source,
        destination,
        source_root=source,
        source_device=source_info.st_dev,
        digest=digest,
        relative=Path("."),
    )
    return digest.hexdigest()


def measure_tree_regularized(source: Path) -> str:
    """Hash the exact regularized tree without executing it or writing a copy."""
    if source.is_symlink():
        raise PreparationError("release source root must not be a symlink")
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise PreparationError("release source must be a real directory")
    source_info = source.lstat()
    digest = hashlib.sha256()
    _copy_directory(
        source,
        None,
        source_root=source,
        source_device=source_info.st_dev,
        digest=digest,
        relative=Path("."),
    )
    return digest.hexdigest()


def _atomic_write(path: Path, value: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chown(path.parent, 0, 0)
    temporary_fd, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(temporary_fd, mode)
        os.fchown(temporary_fd, 0, 0)
        view = memoryview(value)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise PreparationError("configuration write made no progress")
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _environment(release_id: str, profile: str) -> bytes:
    release = RELEASES_ROOT / release_id
    values = {
        "AMLB_RELEASE_ID": release_id,
        "AMLB_RELEASE_ROOT": str(release),
        "AMLB_LEAN": str(release / "toolchain" / "bin" / "lean"),
        "AMLB_TOOLCHAIN": EXPECTED_TOOLCHAIN,
        "AMLB_TIMEOUT_SEC": "60" if profile == "core" else "90",
    }
    if profile == "mathlib":
        values.update(
            {
                "AMLB_LAKE": str(release / "toolchain" / "bin" / "lake"),
                "AMLB_MATHLIB": str(release / "mathlib"),
            }
        )
    return "".join(
        f"{name}={json.dumps(value)}\n" for name, value in values.items()
    ).encode("utf-8")


def _require_digest(value: str, label: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise PreparationError(f"{label} pin is not a lowercase SHA-256 digest")
    return value


def prepare(
    toolchain: Path,
    mathlib: Path,
    *,
    expected_toolchain_tree_sha256: str,
    expected_mathlib_tree_sha256: str,
) -> str:
    if os.geteuid() != 0:
        raise PreparationError("release preparation must run as root")
    validate_source_identity(toolchain, mathlib)
    expected_toolchain_tree_sha256 = _require_digest(
        expected_toolchain_tree_sha256, "toolchain tree"
    )
    expected_mathlib_tree_sha256 = _require_digest(
        expected_mathlib_tree_sha256, "Mathlib tree"
    )
    toolchain = toolchain.resolve(strict=True)
    mathlib = mathlib.resolve(strict=True)
    for source in (toolchain, mathlib):
        if _inside(source, INSTALL_ROOT) or _inside(INSTALL_ROOT, source):
            raise PreparationError("source and immutable install roots must be separate")
    RELEASES_ROOT.mkdir(parents=True, exist_ok=True)
    os.chown(INSTALL_ROOT, 0, 0)
    os.chown(RELEASES_ROOT, 0, 0)
    os.chmod(INSTALL_ROOT, 0o555)
    os.chmod(RELEASES_ROOT, 0o555)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=RELEASES_ROOT))
    try:
        toolchain_digest = copy_tree_regularized(toolchain, staging / "toolchain")
        mathlib_digest = copy_tree_regularized(mathlib, staging / "mathlib")
        if toolchain_digest != expected_toolchain_tree_sha256:
            raise PreparationError("toolchain tree does not match its trusted pin")
        if mathlib_digest != expected_mathlib_tree_sha256:
            raise PreparationError("Mathlib tree does not match its trusted pin")
        identity = {
            "schema": 1,
            "lean_toolchain": EXPECTED_LEAN_TOOLCHAIN,
            "lean_version": EXPECTED_TOOLCHAIN,
            "mathlib_input_rev": EXPECTED_MATHLIB_INPUT_REV,
            "mathlib_rev": EXPECTED_MATHLIB_REV,
            "toolchain_tree_sha256": toolchain_digest,
            "mathlib_tree_sha256": mathlib_digest,
        }
        identity_bytes = (
            json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        release_id = hashlib.sha256(identity_bytes).hexdigest()
        _atomic_write(staging / "IDENTITY.json", identity_bytes, 0o444)
        os.chmod(staging, 0o555)
        os.chown(staging, 0, 0)
        destination = RELEASES_ROOT / release_id
        if destination.exists():
            existing = _read_regular(destination / "IDENTITY.json", 4096)
            if existing != identity_bytes:
                raise PreparationError("content-addressed release identity collision")
            shutil.rmtree(staging)
        else:
            os.rename(staging, destination)
        _atomic_write(ENV_ROOT / "core.env", _environment(release_id, "core"), 0o444)
        _atomic_write(
            ENV_ROOT / "mathlib.env", _environment(release_id, "mathlib"), 0o444
        )
        os.chmod(ENV_ROOT, 0o555)
        os.chown(ENV_ROOT, 0, 0)
        return release_id
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain", required=True, type=Path)
    parser.add_argument("--mathlib", required=True, type=Path)
    parser.add_argument("--expected-toolchain-tree-sha256")
    parser.add_argument("--expected-mathlib-tree-sha256")
    parser.add_argument("--measure-only", action="store_true")
    args = parser.parse_args(argv)
    validate_source_identity(args.toolchain, args.mathlib)
    if args.measure_only:
        print(
            json.dumps(
                {
                    "toolchain_tree_sha256": measure_tree_regularized(
                        args.toolchain
                    ),
                    "mathlib_tree_sha256": measure_tree_regularized(args.mathlib),
                },
                sort_keys=True,
            )
        )
        return 0
    if not args.expected_toolchain_tree_sha256 or not args.expected_mathlib_tree_sha256:
        parser.error(
            "immutable publication requires both independently reviewed tree pins"
        )
    release_id = prepare(
        args.toolchain,
        args.mathlib,
        expected_toolchain_tree_sha256=args.expected_toolchain_tree_sha256,
        expected_mathlib_tree_sha256=args.expected_mathlib_tree_sha256,
    )
    print(release_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
