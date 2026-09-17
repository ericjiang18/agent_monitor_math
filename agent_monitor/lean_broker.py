"""Client for the fixed-interface, socket-activated Lean checker broker.

The broker deliberately accepts only Lean source bytes.  The selected Unix
socket fixes the execution profile (Lean core or the provisioned Mathlib
mirror); callers cannot supply a command, path, environment, resource limit,
or server-side timeout.  There is intentionally no local/unsandboxed fallback.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import stat
import struct
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any


PROTOCOL_VERSION = 1
REQUEST_MAGIC = b"AMLBREQ1"
RESPONSE_MAGIC = b"AMLBRSP1"
_HEADER = struct.Struct("!8sI")

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
DEFAULT_CLIENT_TIMEOUT = 100.0
MAX_CLIENT_TIMEOUT = 120.0
SANDBOX_MARKER = "systemd-socket-dynamic-user-v1"
EXPECTED_TOOLCHAIN = (
    "Lean (version 4.14.0, x86_64-unknown-linux-gnu, "
    "commit 410fab728470, Release)"
)
EXPECTED_LEAN_TOOLCHAIN = "leanprover/lean4:v4.14.0"
EXPECTED_MATHLIB_INPUT_REV = "v4.14.0"
EXPECTED_MATHLIB_REV = "4bbdccd9c5f862bf90ff12f0a9e2c8be032b9a84"

_SOCKET_PATHS = MappingProxyType(
    {
        "core": Path("/run/agent-monitor-lean/core.sock"),
        "mathlib": Path("/run/agent-monitor-lean/mathlib.sock"),
    }
)


class LeanBrokerError(ValueError):
    """The broker is unavailable or returned an invalid response."""


def _sha256_field(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise LeanBrokerError(f"Lean broker {field} is invalid")
    return value


def _validate_release_identity(value: dict[str, Any]) -> tuple[str, str, str]:
    release_id = _sha256_field(value.get("release_id"), "release identity")
    toolchain_tree = _sha256_field(
        value.get("toolchain_tree_sha256"), "toolchain tree identity"
    )
    mathlib_tree = _sha256_field(
        value.get("mathlib_tree_sha256"), "Mathlib tree identity"
    )
    identity = {
        "schema": 1,
        "lean_toolchain": EXPECTED_LEAN_TOOLCHAIN,
        "lean_version": EXPECTED_TOOLCHAIN,
        "mathlib_input_rev": EXPECTED_MATHLIB_INPUT_REV,
        "mathlib_rev": EXPECTED_MATHLIB_REV,
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }
    encoded = (
        json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != release_id:
        raise LeanBrokerError("Lean broker release identity does not match its trees")
    return release_id, toolchain_tree, mathlib_tree


def _read_exact(sock: socket.socket, size: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        wait = deadline - time.monotonic()
        if wait <= 0:
            raise LeanBrokerError("Lean broker response timed out")
        sock.settimeout(wait)
        try:
            chunk = sock.recv(remaining)
        except TimeoutError as exc:
            raise LeanBrokerError("Lean broker response timed out") from exc
        except OSError as exc:
            raise LeanBrokerError("could not read Lean broker response") from exc
        if not chunk:
            raise LeanBrokerError("Lean broker closed an incomplete response")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _bounded_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise LeanBrokerError(f"Lean broker {field} is not text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LeanBrokerError(f"Lean broker {field} is not valid UTF-8") from exc
    if len(encoded) > MAX_TEXT_BYTES:
        raise LeanBrokerError(f"Lean broker {field} exceeded its bound")
    return value


def _validate_response(
    value: Any, *, profile: str, source_sha256: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LeanBrokerError("Lean broker response is not an object")
    if value.get("protocol") != PROTOCOL_VERSION:
        raise LeanBrokerError("Lean broker protocol version mismatch")
    if value.get("profile") != profile:
        raise LeanBrokerError("Lean broker profile mismatch")
    if value.get("source_sha256") != source_sha256:
        raise LeanBrokerError("Lean broker source attestation mismatch")
    if value.get("sandbox") != SANDBOX_MARKER:
        raise LeanBrokerError("Lean broker sandbox attestation mismatch")
    release_id, toolchain_tree, mathlib_tree = _validate_release_identity(value)

    status = value.get("status")
    if status not in {"completed", "timed_out", "error"}:
        raise LeanBrokerError("Lean broker returned an invalid status")
    exit_code = value.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        raise LeanBrokerError("Lean broker exit code is invalid")
    if status == "completed" and exit_code is None:
        raise LeanBrokerError("Lean broker completed without an exit code")
    if status == "error" and exit_code is not None:
        raise LeanBrokerError("Lean broker protocol error has an exit code")
    duration_ms = value.get("duration_ms")
    if (
        not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or duration_ms < 0
        or duration_ms > 180_000
    ):
        raise LeanBrokerError("Lean broker duration is invalid")
    toolchain = _bounded_text(value.get("toolchain"), "toolchain")
    if toolchain != EXPECTED_TOOLCHAIN:
        raise LeanBrokerError("Lean broker toolchain attestation is invalid")
    stdout = _bounded_text(value.get("stdout"), "stdout")
    stderr = _bounded_text(value.get("stderr"), "stderr")
    error = _bounded_text(value.get("error", ""), "error")
    stdout_truncated = value.get("stdout_truncated")
    stderr_truncated = value.get("stderr_truncated")
    if not isinstance(stdout_truncated, bool) or not isinstance(
        stderr_truncated, bool
    ):
        raise LeanBrokerError("Lean broker truncation flags are invalid")

    return {
        "protocol": PROTOCOL_VERSION,
        "profile": profile,
        "status": status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
        "duration_ms": duration_ms,
        "duration": duration_ms / 1000.0,
        "timed_out": status == "timed_out",
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "source_sha256": source_sha256,
        "toolchain": toolchain,
        "sandbox": SANDBOX_MARKER,
        "release_id": release_id,
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }


def _exchange(
    socket_path: Path,
    *,
    profile: str,
    source: bytes,
    timeout: float,
) -> dict[str, Any]:
    request = _HEADER.pack(REQUEST_MAGIC, len(source)) + source
    source_sha256 = hashlib.sha256(source).hexdigest()
    deadline = time.monotonic() + timeout
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(str(socket_path))
    except OSError as exc:
        try:
            client.close()
        except (OSError, UnboundLocalError):
            pass
        raise LeanBrokerError(
            f"Lean {profile} broker is unavailable at its fixed socket"
        ) from exc

    try:
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        header = _read_exact(client, _HEADER.size, deadline)
        magic, length = _HEADER.unpack(header)
        if magic != RESPONSE_MAGIC:
            raise LeanBrokerError("Lean broker response magic mismatch")
        if length < 2 or length > MAX_RESPONSE_BYTES:
            raise LeanBrokerError("Lean broker response length is invalid")
        raw = _read_exact(client, length, deadline)
    except LeanBrokerError:
        raise
    except OSError as exc:
        raise LeanBrokerError("Lean broker request failed") from exc
    finally:
        client.close()

    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise LeanBrokerError("Lean broker returned invalid JSON") from exc
    return _validate_response(
        value, profile=profile, source_sha256=source_sha256
    )


def check_source(
    source: str,
    *,
    uses_mathlib: bool = False,
    timeout: float = DEFAULT_CLIENT_TIMEOUT,
) -> dict[str, Any]:
    """Compile one Lean source through the fixed isolated broker profile."""
    if not isinstance(source, str):
        raise LeanBrokerError("Lean source must be text")
    try:
        payload = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LeanBrokerError("Lean source is not valid UTF-8") from exc
    if not payload:
        raise LeanBrokerError("Lean source is empty")
    if len(payload) > MAX_SOURCE_BYTES:
        raise LeanBrokerError("Lean source exceeded the broker limit")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or timeout <= 0
        or timeout > MAX_CLIENT_TIMEOUT
    ):
        raise LeanBrokerError("Lean broker client timeout is invalid")
    profile = "mathlib" if uses_mathlib else "core"
    return _exchange(
        _SOCKET_PATHS[profile],
        profile=profile,
        source=payload,
        timeout=float(timeout),
    )

def socket_status() -> dict[str, bool]:
    """Report usable fixed sockets without activating either service."""
    result: dict[str, bool] = {}
    for profile, path in _SOCKET_PATHS.items():
        try:
            result[profile] = stat.S_ISSOCK(
                path.lstat().st_mode
            ) and os.access(path, os.R_OK | os.W_OK)
        except OSError:
            result[profile] = False
    return result
