#!/usr/bin/env python3
"""Safely promote Candidate S plus the Candidate T delta into a live SOUL.md."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

DEFAULT_MARKER = "Reserve the final quarter of the available budget"
DEFAULT_MAX_BYTES = 20_000
EVALS_DIR = Path(__file__).resolve().parent


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _body_without_title(text: str, *, source: str) -> str:
    title, separator, body = (text or "").partition("\n")
    if not separator or not title.startswith("# ") or not body.strip():
        raise ValueError(f"{source} must start with one Markdown title and a body")
    return body.strip() + "\n"


def build_candidate(candidate_s: str, candidate_t_delta: str) -> tuple[str, str]:
    current = _body_without_title(candidate_s, source="Candidate S")
    delta = _body_without_title(candidate_t_delta, source="Candidate T delta")
    promoted = current.rstrip() + "\n\n" + delta
    return current, promoted


def promote(
    *,
    candidate_s_path: Path,
    candidate_t_delta_path: Path,
    live_path: Path,
    apply: bool = False,
    expected_output_sha256: str = "",
    marker: str = DEFAULT_MARKER,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, object]:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if live_path.is_symlink() or not live_path.is_file():
        raise ValueError("live SOUL.md must be an existing regular non-symlink file")
    candidate_s = candidate_s_path.read_text(encoding="utf-8")
    delta = candidate_t_delta_path.read_text(encoding="utf-8")
    current_expected, promoted = build_candidate(candidate_s, delta)
    current_live = live_path.read_text(encoding="utf-8")
    current_sha = _sha256(current_live)
    expected_current_sha = _sha256(current_expected)
    if current_live != current_expected:
        raise ValueError(
            "live SOUL.md changed; compare-and-swap refused "
            f"(expected {expected_current_sha}, observed {current_sha})"
        )
    promoted_bytes = len(promoted.encode("utf-8"))
    if promoted_bytes > max_bytes:
        raise ValueError(
            f"promoted prompt is {promoted_bytes} bytes; limit is {max_bytes}"
        )
    marker_count = promoted.count(marker)
    if marker_count != 1 or marker in current_live:
        raise ValueError(
            "Candidate T marker must be absent from live and occur exactly once "
            f"after promotion (observed {marker_count})"
        )
    promoted_sha = _sha256(promoted)
    if expected_output_sha256 and promoted_sha != expected_output_sha256:
        raise ValueError(
            "promoted prompt hash mismatch "
            f"(expected {expected_output_sha256}, observed {promoted_sha})"
        )

    result: dict[str, object] = {
        "status": "dry-run",
        "live_path": str(live_path),
        "expected_current_sha256": expected_current_sha,
        "candidate_t_sha256": promoted_sha,
        "candidate_t_bytes": promoted_bytes,
        "marker_count": marker_count,
    }
    if not apply:
        return result

    # Recheck immediately before the atomic replace so a concurrent UI edit is
    # not silently lost between planning and application.
    if live_path.read_text(encoding="utf-8") != current_expected:
        raise ValueError("live SOUL.md changed during promotion; write refused")
    mode = stat.S_IMODE(live_path.stat().st_mode)
    temporary = live_path.with_name(f".{live_path.name}.promote-{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(promoted)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, live_path)
        directory_fd = os.open(live_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    if _sha256(live_path.read_text(encoding="utf-8")) != promoted_sha:
        raise RuntimeError("post-write SOUL.md hash verification failed")
    result["status"] = "applied"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--candidate-s",
        type=Path,
        default=EVALS_DIR / "system_prompt_candidate_s.md",
    )
    parser.add_argument(
        "--candidate-t-delta",
        type=Path,
        default=EVALS_DIR / "system_prompt_candidate_t_delta.md",
    )
    parser.add_argument("--live", type=Path, required=True)
    parser.add_argument("--expected-output-sha256", default="")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = promote(
        candidate_s_path=args.candidate_s,
        candidate_t_delta_path=args.candidate_t_delta,
        live_path=args.live,
        apply=args.apply,
        expected_output_sha256=args.expected_output_sha256,
        max_bytes=args.max_bytes,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
