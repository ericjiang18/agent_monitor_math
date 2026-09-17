#!/usr/bin/python3
"""Immutable worker for the Agent Monitor Lean socket broker.

This program is installed root-owned under ``/usr/libexec``.  Each process
serves exactly one inherited systemd socket, writes the bounded request to its
private temporary filesystem, and invokes one root-provisioned command.  Lean
stdout/stderr are redirected to private regular files and are never written
directly onto the protocol socket.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


PROTOCOL_VERSION = 1
REQUEST_MAGIC = b"AMLBREQ1"
RESPONSE_MAGIC = b"AMLBRSP1"
HEADER = struct.Struct("!8sI")

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_STREAM_BYTES = 256 * 1024
REQUEST_TIMEOUT_SEC = 5
SANDBOX_MARKER = "systemd-socket-dynamic-user-v1"

EXPECTED_TOOLCHAIN = (
    "Lean (version 4.14.0, x86_64-unknown-linux-gnu, "
    "commit 410fab728470, Release)"
)
EXPECTED_LEAN_TOOLCHAIN = "leanprover/lean4:v4.14.0"
EXPECTED_MATHLIB_INPUT_REV = "v4.14.0"
EXPECTED_MATHLIB_REV = "4bbdccd9c5f862bf90ff12f0a9e2c8be032b9a84"

TRUST_ROOT = Path("/opt/agent-monitor-lean/releases")


class BrokerFailure(ValueError):
    pass


class RequestTimedOut(BrokerFailure):
    pass


@dataclass(frozen=True)
class BrokerConfig:
    profile: str
    release_root: Path
    lean: Path
    toolchain: str
    timeout_sec: float
    release_id: str = ""
    toolchain_tree_sha256: str = ""
    mathlib_tree_sha256: str = ""
    lake: Path | None = None
    mathlib: Path | None = None


def _alarm_handler(_signum: int, _frame: object) -> None:
    raise RequestTimedOut("Lean broker request body timed out")


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise BrokerFailure("incomplete Lean broker request")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_request(stream: BinaryIO) -> bytes:
    header = _read_exact(stream, HEADER.size)
    magic, length = HEADER.unpack(header)
    if magic != REQUEST_MAGIC:
        raise BrokerFailure("invalid Lean broker request magic")
    if length < 1 or length > MAX_SOURCE_BYTES:
        raise BrokerFailure("invalid Lean broker source length")
    source = _read_exact(stream, length)
    try:
        source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BrokerFailure("Lean broker source is not UTF-8") from exc
    return source


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _assert_root_owned_chain(path: Path) -> None:
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise BrokerFailure(f"trusted path contains a symlink: {current}")
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise BrokerFailure(f"trusted path is mutable: {current}")


def _assert_release_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise BrokerFailure(f"trusted release entry is not a directory: {path}")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o555:
        raise BrokerFailure(f"trusted release directory mode is invalid: {path}")


def _assert_release_file(path: Path, *, executable: bool) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BrokerFailure(f"trusted release entry is not a regular file: {path}")
    expected = 0o555 if executable else 0o444
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != expected:
        raise BrokerFailure(f"trusted release file mode is invalid: {path}")


def _strict_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise BrokerFailure(f"missing or invalid root-owned setting: {name}")
    return value


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _release_identity(release_root: Path, release_id: str) -> tuple[str, str]:
    identity_path = release_root / "IDENTITY.json"
    _assert_release_file(identity_path, executable=False)
    raw = _read_small_regular(identity_path, 4096)
    if hashlib.sha256(raw).hexdigest() != release_id:
        raise BrokerFailure("release identity file does not match its directory")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerFailure("release identity file is invalid") from exc
    if not isinstance(value, dict):
        raise BrokerFailure("release identity is not an object")
    toolchain_tree = value.get("toolchain_tree_sha256")
    mathlib_tree = value.get("mathlib_tree_sha256")
    expected = {
        "schema": 1,
        "lean_toolchain": EXPECTED_LEAN_TOOLCHAIN,
        "lean_version": EXPECTED_TOOLCHAIN,
        "mathlib_input_rev": EXPECTED_MATHLIB_INPUT_REV,
        "mathlib_rev": EXPECTED_MATHLIB_REV,
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }
    if value != expected or not _valid_sha256(toolchain_tree) or not _valid_sha256(
        mathlib_tree
    ):
        raise BrokerFailure("release identity fields are invalid")
    return str(toolchain_tree), str(mathlib_tree)


def _timeout_from_env() -> float:
    try:
        timeout = float(_strict_env("AMLB_TIMEOUT_SEC"))
    except ValueError as exc:
        raise BrokerFailure("invalid broker timeout") from exc
    if not math.isfinite(timeout) or not 1 <= timeout <= 90:
        raise BrokerFailure("broker timeout is outside the fixed safe range")
    return timeout


def load_config(profile: str) -> BrokerConfig:
    if profile not in {"core", "mathlib"}:
        raise BrokerFailure("invalid fixed broker profile")
    release_id = _strict_env("AMLB_RELEASE_ID")
    if len(release_id) != 64 or any(c not in "0123456789abcdef" for c in release_id):
        raise BrokerFailure("invalid broker release identity")
    release_root = Path(_strict_env("AMLB_RELEASE_ROOT"))
    expected_root = TRUST_ROOT / release_id
    if release_root != expected_root or release_root.resolve(strict=True) != release_root:
        raise BrokerFailure("broker release root is not the fixed trusted path")
    if TRUST_ROOT.resolve(strict=True) != TRUST_ROOT:
        raise BrokerFailure("broker trust root contains a symlink")
    _assert_root_owned_chain(TRUST_ROOT)
    _assert_release_directory(release_root)
    toolchain_tree, mathlib_tree = _release_identity(release_root, release_id)

    lean = Path(_strict_env("AMLB_LEAN"))
    expected_lean = release_root / "toolchain" / "bin" / "lean"
    if lean != expected_lean or lean.resolve(strict=True) != lean:
        raise BrokerFailure("Lean executable is not the fixed release executable")
    for path in (release_root / "toolchain", release_root / "toolchain" / "bin"):
        _assert_release_directory(path)
    _assert_release_file(lean, executable=True)

    toolchain = _strict_env("AMLB_TOOLCHAIN")
    if toolchain != EXPECTED_TOOLCHAIN:
        raise BrokerFailure("configured Lean toolchain identity is not approved")

    timeout_sec = _timeout_from_env()
    if profile == "core":
        return BrokerConfig(
            profile=profile,
            release_root=release_root,
            lean=lean,
            toolchain=toolchain,
            timeout_sec=timeout_sec,
            release_id=release_id,
            toolchain_tree_sha256=toolchain_tree,
            mathlib_tree_sha256=mathlib_tree,
        )

    lake = Path(_strict_env("AMLB_LAKE"))
    mathlib = Path(_strict_env("AMLB_MATHLIB"))
    if lake != release_root / "toolchain" / "bin" / "lake":
        raise BrokerFailure("Lake executable is not the fixed release executable")
    if mathlib != release_root / "mathlib":
        raise BrokerFailure("Mathlib is not the fixed release mirror")
    if lake.resolve(strict=True) != lake or mathlib.resolve(strict=True) != mathlib:
        raise BrokerFailure("Mathlib release paths contain a symlink")
    _assert_release_file(lake, executable=True)
    _assert_release_directory(mathlib)
    _assert_release_file(mathlib / "lake-manifest.json", executable=False)
    _assert_release_file(mathlib / "lean-toolchain", executable=False)
    return BrokerConfig(
        profile=profile,
        release_root=release_root,
        lean=lean,
        lake=lake,
        mathlib=mathlib,
        toolchain=toolchain,
        timeout_sec=timeout_sec,
        release_id=release_id,
        toolchain_tree_sha256=toolchain_tree,
        mathlib_tree_sha256=mathlib_tree,
    )


def _read_small_regular(path: Path, limit: int = 1024 * 1024) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise BrokerFailure(f"trusted metadata file is invalid: {path}")
        value = bytearray()
        while len(value) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        if len(value) > limit:
            raise BrokerFailure(f"trusted metadata file is oversized: {path}")
        return bytes(value)
    finally:
        os.close(fd)


def attest_config(config: BrokerConfig) -> None:
    try:
        checked = subprocess.run(
            [str(config.lean), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={"PATH": str(config.lean.parent), "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrokerFailure("could not attest the fixed Lean executable") from exc
    actual = (checked.stdout or checked.stderr or "").strip()
    if checked.returncode or actual != EXPECTED_TOOLCHAIN:
        raise BrokerFailure("fixed Lean executable failed exact attestation")

    if config.profile != "mathlib":
        return
    assert config.mathlib is not None
    toolchain = _read_small_regular(config.mathlib / "lean-toolchain", 256)
    if toolchain.decode("utf-8").strip() != EXPECTED_LEAN_TOOLCHAIN:
        raise BrokerFailure("Mathlib mirror uses an unapproved Lean toolchain")
    try:
        manifest = json.loads(
            _read_small_regular(config.mathlib / "lake-manifest.json").decode(
                "utf-8"
            )
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerFailure("Mathlib mirror manifest is invalid") from exc
    packages = manifest.get("packages") if isinstance(manifest, dict) else None
    if not isinstance(packages, list):
        raise BrokerFailure("Mathlib mirror manifest has no package list")
    mathlib_entry = next(
        (
            item
            for item in packages
            if isinstance(item, dict) and item.get("name") == "mathlib"
        ),
        None,
    )
    if not isinstance(mathlib_entry, dict) or (
        mathlib_entry.get("inputRev") != EXPECTED_MATHLIB_INPUT_REV
        or mathlib_entry.get("rev") != EXPECTED_MATHLIB_REV
    ):
        raise BrokerFailure("Mathlib mirror revision is not approved")


def _mathlib_git_safe_directories(config: BrokerConfig) -> tuple[Path, ...]:
    """Return only the immutable package repositories named by the manifest.

    Lake asks Git for each dependency URL even for ``lake env``.  The broker
    deliberately runs as a DynamicUser while the content-addressed release is
    root-owned, so Git otherwise treats every package as dubiously owned and
    Lake attempts an impossible re-clone into the read-only release.  These
    exact, release-bound paths are safe to mark for this one child process.
    """

    if config.profile != "mathlib" or config.mathlib is None:
        return ()
    try:
        manifest = json.loads(
            _read_small_regular(config.mathlib / "lake-manifest.json").decode(
                "utf-8"
            )
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerFailure("Mathlib mirror manifest is invalid") from exc
    packages = manifest.get("packages") if isinstance(manifest, dict) else None
    if not isinstance(packages, list) or not packages:
        raise BrokerFailure("Mathlib mirror manifest has no package list")
    roots: list[Path] = []
    seen: set[str] = set()
    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789_.-"
    )
    for item in packages:
        name = item.get("name") if isinstance(item, dict) else None
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or any(char not in allowed for char in name)
            or name in seen
        ):
            raise BrokerFailure(
                "Mathlib manifest contains an invalid package name"
            )
        seen.add(name)
        root = config.mathlib / ".lake" / "packages" / name
        _assert_release_directory(root)
        _assert_release_directory(root / ".git")
        roots.append(root)
    return tuple(roots)


def _write_source(path: Path, source: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(source)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise BrokerFailure("could not write private Lean source")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_capped(path: Path) -> tuple[str, bool]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BrokerFailure("Lean output was not a regular file")
        value = bytearray()
        while len(value) <= MAX_STREAM_BYTES:
            chunk = os.read(
                fd, min(65536, MAX_STREAM_BYTES + 1 - len(value))
            )
            if not chunk:
                break
            value.extend(chunk)
        truncated = len(value) > MAX_STREAM_BYTES or info.st_size > MAX_STREAM_BYTES
        return bytes(value[:MAX_STREAM_BYTES]).decode("utf-8", "replace"), truncated
    finally:
        os.close(fd)


def run_fixed(config: BrokerConfig, source: bytes) -> dict[str, object]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="agent-monitor-lean-") as raw_root:
        root = Path(raw_root)
        source_path = root / "Proof.lean"
        stdout_path = root / "stdout"
        stderr_path = root / "stderr"
        home = root / "home"
        cache = root / "cache"
        home.mkdir(mode=0o700)
        cache.mkdir(mode=0o700)
        _write_source(source_path, source)

        if config.profile == "mathlib":
            assert config.lake is not None and config.mathlib is not None
            command = [
                str(config.lake),
                "--dir",
                str(config.mathlib),
                "env",
                str(config.lean),
                str(source_path),
            ]
            cwd = config.mathlib
        else:
            command = [str(config.lean), str(source_path)]
            cwd = root
        env = {
            "PATH": f"{config.lean.parent}:/usr/bin:/bin",
            "HOME": str(home),
            "XDG_CACHE_HOME": str(cache),
            "TMPDIR": str(root),
            "LANG": "C.UTF-8",
        }
        if config.profile == "mathlib":
            safe_directories = _mathlib_git_safe_directories(config)
            env.update(
                {
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_COUNT": str(len(safe_directories)),
                }
            )
            for index, directory in enumerate(safe_directories):
                env[f"GIT_CONFIG_KEY_{index}"] = "safe.directory"
                env[f"GIT_CONFIG_VALUE_{index}"] = str(directory)

        timed_out = False
        with open(stdout_path, "xb", buffering=0) as stdout_file, open(
            stderr_path, "xb", buffering=0
        ) as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    close_fds=True,
                    start_new_session=True,
                )
            except OSError as exc:
                raise BrokerFailure("could not launch the fixed Lean command") from exc
            deadline = started + config.timeout_sec
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                    break
                time.sleep(0.02)
            exit_code = process.returncode

        stdout, stdout_truncated = _read_capped(stdout_path)
        stderr, stderr_truncated = _read_capped(stderr_path)
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    return {
        "status": "timed_out" if timed_out else "completed",
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "error": "Lean check exceeded the fixed broker timeout" if timed_out else "",
        "duration_ms": duration_ms,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


def _base_response(profile: str, source_sha256: str) -> dict[str, object]:
    return {
        "protocol": PROTOCOL_VERSION,
        "profile": profile,
        "status": "error",
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "error": "",
        "duration_ms": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "source_sha256": source_sha256,
        "toolchain": EXPECTED_TOOLCHAIN,
        "sandbox": SANDBOX_MARKER,
        "release_id": "",
        "toolchain_tree_sha256": "",
        "mathlib_tree_sha256": "",
    }


def encode_response(value: dict[str, object]) -> bytes:
    body = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        compact = _base_response(
            str(value.get("profile") or ""),
            str(value.get("source_sha256") or ""),
        )
        compact["error"] = "Lean broker response exceeded its fixed bound"
        compact["duration_ms"] = int(value.get("duration_ms") or 0)
        body = json.dumps(
            compact, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        raise BrokerFailure("could not encode a bounded broker response")
    return HEADER.pack(RESPONSE_MAGIC, len(body)) + body


def _write_all(stream: BinaryIO, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = stream.write(view)
        if written is None:
            written = len(view)
        if written <= 0:
            raise BrokerFailure("could not write Lean broker response")
        view = view[written:]
    stream.flush()


def serve_one(profile: str, reader: BinaryIO, writer: BinaryIO) -> int:
    source_sha256 = ""
    started = time.monotonic()
    response = _base_response(profile, source_sha256)
    try:
        if hasattr(signal, "setitimer"):
            signal.signal(signal.SIGALRM, _alarm_handler)
            signal.setitimer(signal.ITIMER_REAL, REQUEST_TIMEOUT_SEC)
        source = read_request(reader)
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)
        source_sha256 = hashlib.sha256(source).hexdigest()
        response["source_sha256"] = source_sha256
        config = load_config(profile)
        response["release_id"] = config.release_id
        response["toolchain_tree_sha256"] = config.toolchain_tree_sha256
        response["mathlib_tree_sha256"] = config.mathlib_tree_sha256
        attest_config(config)
        response.update(run_fixed(config, source))
        response["toolchain"] = config.toolchain
    except Exception as exc:  # one bounded protocol error, never a traceback
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)
        response["status"] = "error"
        response["exit_code"] = None
        response["error"] = str(exc)[:1024] or type(exc).__name__
        response["duration_ms"] = max(
            0, int((time.monotonic() - started) * 1000)
        )
    try:
        _write_all(writer, encode_response(response))
    except Exception as exc:
        print(f"Lean broker protocol write failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile", required=True, choices=("core", "mathlib"))
    args = parser.parse_args(argv)
    return serve_one(args.profile, sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
