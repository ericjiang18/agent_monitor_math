"""Trusted runtime checks for file-backed research-proof audit contracts.

The audit workspace is agent-controlled.  Runtime acceptance therefore calls
the server-owned reconciliation validator from the installed application,
rather than executing a copy that the agent could have modified in its run
directory.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


_RECONCILIATION_VALIDATOR = (
    Path(__file__).resolve().parent
    / "starter_skills"
    / "research-proof-audit"
    / "scripts"
    / "validate_reconciliation.py"
)


def validate_workspace_reconciliation(
    workspace: Path,
    audit_directory: Path,
    *,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Return the trusted reconciliation certificate for one audit directory.

    Unsafe paths, symbolic-link traversal, validator failures, timeouts, and
    malformed output all return a non-affirmative result.  Callers must never
    infer completion from a missing ``valid`` key.
    """
    try:
        workspace_path = Path(workspace)
        audit_path = Path(audit_directory)
        if workspace_path.is_symlink() or not workspace_path.is_dir():
            raise ValueError("workspace must be a regular directory")
        if audit_path.is_symlink() or not audit_path.is_dir():
            raise ValueError("audit directory must be a regular directory")
        workspace_root = workspace_path.resolve(strict=True)
        audit_root = audit_path.resolve(strict=True)
        relative = audit_root.relative_to(workspace_root)
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("audit directory must be a child of the workspace")
        if relative.parts[0] != "audit":
            raise ValueError("audit directory must be under workspace/audit")
        cursor = workspace_path
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("audit directory must not traverse symbolic links")
        validator = _RECONCILIATION_VALIDATOR
        if validator.is_symlink() or not validator.is_file():
            raise ValueError("server-owned reconciliation validator is unavailable")
        completed = subprocess.run(
            [sys.executable, str(validator), relative.as_posix()],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout_seconds), 120)),
            check=False,
        )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("reconciliation validator returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("reconciliation validator returned a non-object")
        payload = dict(payload)
        payload["returncode"] = completed.returncode
        if completed.stderr.strip():
            payload["stderr"] = completed.stderr.strip()[-4000:]
        if completed.returncode != 0:
            payload["valid"] = False
        return payload
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {
            "status": "error",
            "valid": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def reconciliation_complete(workspace: Path, audit_directory: Path) -> bool:
    """Return true only for an affirmative server-owned certificate."""
    result = validate_workspace_reconciliation(workspace, audit_directory)
    return result.get("valid") is True and result.get("returncode") == 0


def workspace_reconciliation_status(workspace: Path) -> dict[str, Any]:
    """Summarize bounded audit directories for Monitor/export payloads."""
    root = Path(workspace)
    audit_root = root / "audit"
    if audit_root.is_symlink() or not audit_root.is_dir():
        return {"status": "missing", "valid": False, "audits": []}
    audits: list[dict[str, Any]] = []
    try:
        directories = [
            path
            for path in audit_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        ]
        directories.sort(
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        directories = directories[:8]
    except OSError as exc:
        return {
            "status": "error",
            "valid": False,
            "audits": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            continue
        result = validate_workspace_reconciliation(
            root, directory, timeout_seconds=3
        )
        bounded = {
            "directory": str(directory.relative_to(root)),
            "valid": result.get("valid") is True,
            "status": result.get("status"),
            "reconciliation_status": result.get("reconciliation_status"),
            "certificate_sha256": result.get("certificate_sha256"),
            "independence_status": result.get("independence_status"),
            "final_audit_verdict": result.get("final_audit_verdict"),
            "open_finding_count": result.get("open_finding_count"),
            "audit_accepts_final": result.get("audit_accepts_final") is True,
            "errors": list(result.get("errors") or [])[:20],
        }
        if result.get("error"):
            bounded["error"] = str(result["error"])[-2000:]
        audits.append(bounded)
        if bounded["valid"] and bounded["independence_status"] == "independent":
            return {
                "status": "validated",
                "valid": True,
                "audit_accepts_final": bounded["audit_accepts_final"],
                "open_finding_count": bounded["open_finding_count"],
                "audits": audits,
            }
    if any(item["valid"] for item in audits):
        accepted = next(item for item in audits if item["valid"])
        return {
            "status": "validated_degraded_independence",
            "valid": True,
            "audit_accepts_final": accepted["audit_accepts_final"],
            "open_finding_count": accepted["open_finding_count"],
            "audits": audits,
        }
    return {"status": "unreconciled", "valid": False, "audits": audits}
