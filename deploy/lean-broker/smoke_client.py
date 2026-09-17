#!/usr/bin/python3
"""Root-owned activation smoke test for both fixed Lean broker sockets."""

from __future__ import annotations

import hashlib
import json
import socket
import struct
from pathlib import Path


REQUEST_MAGIC = b"AMLBREQ1"
RESPONSE_MAGIC = b"AMLBRSP1"
HEADER = struct.Struct("!8sI")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
EXPECTED_TOOLCHAIN = (
    "Lean (version 4.14.0, x86_64-unknown-linux-gnu, "
    "commit 410fab728470, Release)"
)
EXPECTED_LEAN_TOOLCHAIN = "leanprover/lean4:v4.14.0"
EXPECTED_MATHLIB_INPUT_REV = "v4.14.0"
EXPECTED_MATHLIB_REV = "4bbdccd9c5f862bf90ff12f0a9e2c8be032b9a84"

CASES = {
    "core": (
        Path("/run/agent-monitor-lean/core.sock"),
        "example : 1 + 1 = 2 := by decide\n",
    ),
    "mathlib": (
        Path("/run/agent-monitor-lean/mathlib.sock"),
        "import Mathlib.Data.Int.Notation\n"
        "example : (1 : Int) + 1 = 2 := by decide\n",
    ),
}


def _read_exact(client: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = client.recv(size)
        if not chunk:
            raise RuntimeError("broker closed an incomplete smoke response")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def check(profile: str, socket_path: Path, source: str) -> None:
    payload = source.encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(105)
        client.connect(str(socket_path))
        client.sendall(HEADER.pack(REQUEST_MAGIC, len(payload)) + payload)
        client.shutdown(socket.SHUT_WR)
        magic, length = HEADER.unpack(_read_exact(client, HEADER.size))
        if magic != RESPONSE_MAGIC or not 2 <= length <= MAX_RESPONSE_BYTES:
            raise RuntimeError(f"{profile} broker returned an invalid frame")
        value = json.loads(_read_exact(client, length).decode("utf-8"))
    expected_hash = hashlib.sha256(payload).hexdigest()
    required = {
        "protocol": 1,
        "profile": profile,
        "status": "completed",
        "exit_code": 0,
        "source_sha256": expected_hash,
        "toolchain": EXPECTED_TOOLCHAIN,
        "sandbox": "systemd-socket-dynamic-user-v1",
    }
    for field, expected in required.items():
        if value.get(field) != expected:
            raise RuntimeError(
                f"{profile} broker smoke failed: {field}={value.get(field)!r}; "
                f"stderr={str(value.get('stderr') or '')[:400]}"
            )
    toolchain_tree = value.get("toolchain_tree_sha256")
    mathlib_tree = value.get("mathlib_tree_sha256")
    release_id = value.get("release_id")
    for field, candidate in (
        ("toolchain_tree_sha256", toolchain_tree),
        ("mathlib_tree_sha256", mathlib_tree),
        ("release_id", release_id),
    ):
        if not isinstance(candidate, str) or len(candidate) != 64 or any(
            char not in "0123456789abcdef" for char in candidate
        ):
            raise RuntimeError(f"{profile} broker returned invalid {field}")
    identity = {
        "schema": 1,
        "lean_toolchain": EXPECTED_LEAN_TOOLCHAIN,
        "lean_version": EXPECTED_TOOLCHAIN,
        "mathlib_input_rev": EXPECTED_MATHLIB_INPUT_REV,
        "mathlib_rev": EXPECTED_MATHLIB_REV,
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }
    raw_identity = (
        json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if hashlib.sha256(raw_identity).hexdigest() != release_id:
        raise RuntimeError(f"{profile} broker release identity is inconsistent")


def main() -> int:
    for profile, (socket_path, source) in CASES.items():
        check(profile, socket_path, source)
        print(f"{profile}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
