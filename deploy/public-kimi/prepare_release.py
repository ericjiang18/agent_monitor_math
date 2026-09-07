#!/usr/bin/python3
"""Publish a reviewed public-Kimi build as an immutable root-owned release.

This program never imports or executes code from the source tree.  It accepts
only regular files/directories and same-tree symlinks to regular files, hashes
the regularized tree, and publishes it with one same-filesystem rename.

Publishing requires an independently reviewed SHA-256 tree identity.  Use
``--measure-only`` to produce a candidate identity without root privileges;
do not treat a measurement of an untrusted mutable tree as provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


INSTALL_ROOT = Path("/opt/proving-kimi-public")
RELEASES_ROOT = INSTALL_ROOT / "releases"
MANIFEST_NAME = "public-kimi-release.json"
MAX_FILES = 50_000
MAX_BYTES = 2 * 1024 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TREE_HASH_DOMAIN = b"proving-kimi-public-tree-v2\0"
_RUNTIME_PYTHON = Path("venv/bin/python")
_RUNTIME_MODULE = "agent_monitor.public_kimi_server"
_REQUIRED_PUBLIC_FILES = (
    Path("agent_monitor/public_kimi_gateway.py"),
    Path("agent_monitor/web/public_kimi.html"),
    Path("agent_monitor/web/public_kimi.css"),
    Path("agent_monitor/web/public_kimi.js"),
    Path("agent_monitor/runners/plain_runner.py"),
)
_TOP_LEVEL = frozenset({MANIFEST_NAME, "agent_monitor", "venv"})
_FORBIDDEN_COMPONENTS = frozenset(
    {".git", ".hg", ".svn", ".env", ".pytest_cache", "__pycache__"}
)
_FORBIDDEN_APP_COMPONENTS = frozenset({"tests", "test", "starter_skills"})
_FORBIDDEN_APP_BASENAMES = frozenset(
    {"users.db", "auth.db", "credentials", "secrets"}
)
_FORBIDDEN_APP_SUFFIXES = frozenset(
    {".db", ".sqlite", ".sqlite3", ".key", ".p12", ".pfx", ".pyc", ".pyo"}
)
_PUBLIC_WEB_ASSETS = frozenset(
    {"public_kimi.html", "public_kimi.css", "public_kimi.js"}
)


class PreparationError(ValueError):
    """Raised when a release cannot be measured or safely published."""


@dataclass
class _Budget:
    files: int = 0
    bytes: int = 0

    def add_file(self, size: int) -> None:
        self.files += 1
        self.bytes += size
        if self.files > MAX_FILES:
            raise PreparationError(f"release contains more than {MAX_FILES} files")
        if self.bytes > MAX_BYTES:
            raise PreparationError(f"release exceeds {MAX_BYTES} bytes")


def _relative_file(value: object, *, label: str) -> Path:
    raw = str(value or "")
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PreparationError(f"manifest {label} must be a clean relative path")
    return path


def _read_regular(path: Path, *, limit: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PreparationError(f"cannot open regular file: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise PreparationError(f"invalid or oversized regular file: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        if len(result) > limit:
            raise PreparationError(f"oversized regular file: {path}")
        return result
    finally:
        os.close(fd)


def validate_manifest(source: Path) -> dict[str, object]:
    """Validate the fail-closed runtime identity stored in a staging tree."""
    try:
        value = json.loads(
            _read_regular(source / MANIFEST_NAME, limit=16 * 1024).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparationError("public release manifest is invalid JSON") from exc
    if not isinstance(value, dict):
        raise PreparationError("public release manifest must be an object")
    allowed_keys = {
        "schema_version",
        "python",
        "module",
        "model",
        "engine_allowlist",
        "auto_pipeline",
        "source_revision",
    }
    unknown = set(value) - allowed_keys
    if unknown:
        raise PreparationError(
            "public release manifest contains unknown fields: "
            + ", ".join(sorted(unknown))
        )
    if value.get("schema_version") != 1:
        raise PreparationError("public release manifest schema_version must be 1")
    if value.get("model") != "kimi-k3":
        raise PreparationError("public release model must be exactly kimi-k3")
    if value.get("engine_allowlist") != ["plain"]:
        raise PreparationError(
            "public release engine_allowlist must be exactly plain"
        )
    if value.get("auto_pipeline") is not False:
        raise PreparationError("public release must disable the automatic Lean/DAG pipeline")

    python_path = _relative_file(value.get("python"), label="python")
    if python_path != _RUNTIME_PYTHON:
        raise PreparationError("public release python must be exactly venv/bin/python")
    module = str(value.get("module") or "")
    if module != _RUNTIME_MODULE:
        raise PreparationError(
            "public release module must be exactly agent_monitor.public_kimi_server"
        )
    module_path = Path(*module.split(".")).with_suffix(".py")
    for path, label, executable in (
        (source / python_path, "python", True),
        (source / module_path, "module", False),
    ):
        try:
            info = path.lstat()
        except OSError as exc:
            raise PreparationError(f"manifest {label} is missing") from exc
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise PreparationError(f"manifest {label} must be a regular file")
        if executable and not (info.st_mode & 0o111):
            raise PreparationError("manifest python must be executable")
    for relative in _REQUIRED_PUBLIC_FILES:
        path = source / relative
        try:
            info = path.lstat()
        except OSError as exc:
            raise PreparationError(f"required public release file is missing: {relative}") from exc
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise PreparationError(
                f"required public release file must be regular: {relative}"
            )
    revision = value.get("source_revision")
    if revision is not None and (
        not isinstance(revision, str)
        or len(revision) > 160
        or any(ord(char) < 0x20 for char in revision)
    ):
        raise PreparationError("manifest source_revision is invalid")
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_release_path(relative: Path, *, is_directory: bool) -> None:
    """Reject private application assets before hashing or copying them.

    The virtual environment is intentionally treated as an opaque dependency
    tree after the global secret/backup checks; package-internal test
    directories and certificate bundles are common and are not application
    data.
    """
    parts = relative.parts
    if not parts or parts == (".",):
        return
    top = parts[0]
    lowered = tuple(part.casefold() for part in parts)
    for part in lowered:
        if (
            part in _FORBIDDEN_COMPONENTS
            or part.startswith(".env.")
            or part.endswith(".orig")
            or part.endswith(".rej")
        ):
            raise PreparationError(f"forbidden release path: {relative}")
    if top not in _TOP_LEVEL:
        raise PreparationError(
            f"release path is outside the fixed root policy: {relative}"
        )
    if top == MANIFEST_NAME:
        if len(parts) != 1 or is_directory:
            raise PreparationError(
                "public release manifest must be a top-level regular file"
            )
        return
    if len(parts) == 1 and not is_directory:
        raise PreparationError(
            f"release application root must be a directory: {relative}"
        )
    if top != "agent_monitor":
        return
    app_parts = lowered[1:]
    if any(part in _FORBIDDEN_APP_COMPONENTS for part in app_parts):
        raise PreparationError(
            f"application test/skill path is forbidden: {relative}"
        )
    basename = lowered[-1]
    if basename in _FORBIDDEN_APP_BASENAMES:
        raise PreparationError(
            f"application private-data path is forbidden: {relative}"
        )
    if not is_directory and Path(basename).suffix in _FORBIDDEN_APP_SUFFIXES:
        raise PreparationError(
            f"application private-data file is forbidden: {relative}"
        )
    if len(parts) >= 2 and lowered[1] == "web":
        if len(parts) != 3 or is_directory or parts[2] not in _PUBLIC_WEB_ASSETS:
            if len(parts) > 2:
                raise PreparationError(
                    f"non-public web asset is forbidden: {relative}"
                )


def _copy_regular(
    source: Path,
    destination: Path | None,
    *,
    expected_device: int,
    expected_inode: int,
    executable: bool,
    digest: "hashlib._Hash",
    relative: Path,
    budget: _Budget,
) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise PreparationError(f"source file changed or is linked: {source}") from exc
    destination_fd = -1
    try:
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != expected_device
            or before.st_ino != expected_inode
        ):
            raise PreparationError(f"source file changed or crossed a mount: {source}")
        budget.add_file(before.st_size)
        if destination is not None:
            destination_fd = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        file_digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            file_digest.update(chunk)
            if destination_fd >= 0:
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise PreparationError("release copy made no progress")
                    view = view[written:]
        after = os.fstat(source_fd)
        if (
            copied != before.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            raise PreparationError(f"source file changed during copy: {source}")
        # Hash a framed, fixed-length record only after the source remained
        # stable.  Size plus a fixed 32-byte content digest prevents one file's
        # bytes from being reinterpreted as a following path record.
        digest.update(b"F\0" + relative.as_posix().encode("utf-8") + b"\0")
        digest.update(b"X\0" if executable else b"R\0")
        digest.update(before.st_size.to_bytes(8, "big", signed=False))
        digest.update(file_digest.digest())
        if destination_fd >= 0:
            os.fchmod(destination_fd, 0o555 if executable else 0o444)
            os.fchown(destination_fd, 0, 0)
            os.fsync(destination_fd)
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)


def _walk(
    source: Path,
    destination: Path | None,
    *,
    source_root: Path,
    source_device: int,
    digest: "hashlib._Hash",
    relative: Path,
    budget: _Budget,
) -> None:
    before = source.lstat()
    if not stat.S_ISDIR(before.st_mode) or before.st_dev != source_device:
        raise PreparationError(f"directory changed or crossed a mount: {source}")
    if destination is not None:
        destination.mkdir(mode=0o700)
        os.chown(destination, 0, 0)
    digest.update(b"D\0" + relative.as_posix().encode("utf-8") + b"\0")
    try:
        entries = sorted(os.scandir(source), key=lambda item: item.name)
    except OSError as exc:
        raise PreparationError(f"cannot scan source directory: {source}") from exc
    for entry in entries:
        src = Path(entry.path)
        dst = destination / entry.name if destination is not None else None
        rel = relative / entry.name
        info = entry.stat(follow_symlinks=False)
        if info.st_dev != source_device:
            raise PreparationError(f"cross-device source entry is forbidden: {src}")
        if stat.S_ISDIR(info.st_mode):
            _validate_release_path(rel, is_directory=True)
            _walk(
                src,
                dst,
                source_root=source_root,
                source_device=source_device,
                digest=digest,
                relative=rel,
                budget=budget,
            )
        elif stat.S_ISREG(info.st_mode):
            _validate_release_path(rel, is_directory=False)
            _copy_regular(
                src,
                dst,
                expected_device=source_device,
                expected_inode=info.st_ino,
                executable=bool(info.st_mode & 0o111),
                digest=digest,
                relative=rel,
                budget=budget,
            )
        elif stat.S_ISLNK(info.st_mode):
            try:
                target = src.resolve(strict=True)
                target_info = target.lstat()
            except OSError as exc:
                raise PreparationError(f"broken source symlink: {src}") from exc
            if (
                not _inside(target, source_root)
                or not stat.S_ISREG(target_info.st_mode)
                or target_info.st_dev != source_device
            ):
                raise PreparationError(
                    f"escaping, directory, or cross-device symlink is forbidden: {src}"
                )
            _validate_release_path(rel, is_directory=False)
            _copy_regular(
                target,
                dst,
                expected_device=source_device,
                expected_inode=target_info.st_ino,
                executable=bool(target_info.st_mode & 0o111),
                digest=digest,
                relative=rel,
                budget=budget,
            )
        else:
            raise PreparationError(f"special source entry is forbidden: {src}")
    after = source.lstat()
    if (after.st_dev, after.st_ino, after.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
    ):
        raise PreparationError(f"source directory changed during copy: {source}")
    if destination is not None:
        directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fchmod(directory_fd, 0o555)
            os.fchown(directory_fd, 0, 0)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _source_root(source: Path) -> Path:
    if source.is_symlink():
        raise PreparationError("release source root must not be a symlink")
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise PreparationError("release source root does not exist") from exc
    if not resolved.is_dir() or resolved == Path("/"):
        raise PreparationError("release source must be a non-root directory")
    install = INSTALL_ROOT.resolve(strict=False)
    if _inside(resolved, install) or _inside(install, resolved):
        raise PreparationError("release source and install root must be separate")
    return resolved


def _measure_validated_root(root: Path, destination: Path | None = None) -> str:
    validate_manifest(root)
    digest = hashlib.sha256()
    digest.update(_TREE_HASH_DOMAIN)
    _walk(
        root,
        destination,
        source_root=root,
        source_device=root.lstat().st_dev,
        digest=digest,
        relative=Path("."),
        budget=_Budget(),
    )
    return digest.hexdigest()


def measure_tree(source: Path, destination: Path | None = None) -> str:
    return _measure_validated_root(_source_root(source), destination)


def _require_digest(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise PreparationError("expected tree identity must be lowercase SHA-256")
    return value


def _ensure_root_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise PreparationError(f"install directory is invalid: {path}")
    os.chown(path, 0, 0)
    os.chmod(path, 0o755)


def prepare(source: Path, *, expected_tree_sha256: str) -> str:
    if os.geteuid() != 0:
        raise PreparationError("release publication must run as root")
    expected = _require_digest(expected_tree_sha256)
    root = _source_root(source)
    validate_manifest(root)
    measured = measure_tree(root)
    if not hmac_compare(measured, expected):
        raise PreparationError(
            f"source tree identity mismatch: expected {expected}, measured {measured}"
        )
    _ensure_root_directory(INSTALL_ROOT)
    _ensure_root_directory(RELEASES_ROOT)
    release = RELEASES_ROOT / expected
    if release.exists():
        if release.is_symlink() or not release.is_dir():
            raise PreparationError("existing release path is invalid")
        if _measure_validated_root(release) != expected:
            raise PreparationError("existing release does not match its identity")
        return expected

    container = Path(tempfile.mkdtemp(prefix=".prepare-", dir=RELEASES_ROOT))
    temporary = container / "release"
    try:
        copied = measure_tree(root, temporary)
        if not hmac_compare(copied, expected):
            raise PreparationError("source changed between measurement and publication")
        os.chown(temporary, 0, 0)
        # Moving a directory between parents requires owner-write permission
        # on the directory on this host.  Publish it root-owned and non-writable
        # to the service (0755), then remove root's write bit immediately after
        # the atomic rename.  A crash between these calls still leaves a
        # root-owned tree that the DynamicUser cannot modify.
        os.chmod(temporary, 0o755)
        os.replace(temporary, release)
        temporary = Path()
        os.chmod(release, 0o555)
        parent_fd = os.open(RELEASES_ROOT, os.O_RDONLY | os.O_DIRECTORY)
        container.rmdir()
        container = Path()
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if temporary != Path() and temporary.exists():
            _remove_private_tree(temporary)
        if container != Path() and container.exists():
            container.rmdir()
    return expected


def hmac_compare(left: str, right: str) -> bool:
    # Kept local so this preparer imports no application/source-tree modules.
    import hmac

    return hmac.compare_digest(left, right)


def _remove_private_tree(root: Path) -> None:
    """Remove only the preparer's explicit private temporary tree."""
    entries = list(root.rglob("*"))
    # A failed copy may already have sealed nested directories 0555. Restore
    # owner-only traversal/write permission before unlinking their children.
    root.chmod(0o700)
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                entry.chmod(0o700)
        except FileNotFoundError:
            pass
    for entry in sorted(entries, key=lambda item: len(item.parts), reverse=True):
        try:
            if entry.is_dir() and not entry.is_symlink():
                entry.rmdir()
            else:
                entry.chmod(0o600, follow_symlinks=False)
                entry.unlink()
        except FileNotFoundError:
            pass
    root.rmdir()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-tree-sha256")
    parser.add_argument("--measure-only", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.measure_only:
            if args.expected_tree_sha256:
                raise PreparationError(
                    "--expected-tree-sha256 is not used with --measure-only"
                )
            result = measure_tree(args.source)
        else:
            if not args.expected_tree_sha256:
                raise PreparationError(
                    "--expected-tree-sha256 is required for publication"
                )
            result = prepare(
                args.source,
                expected_tree_sha256=args.expected_tree_sha256,
            )
    except PreparationError as exc:
        print(f"prepare_release: {exc}", file=os.sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
