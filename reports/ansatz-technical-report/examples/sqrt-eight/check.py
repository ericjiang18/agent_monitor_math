#!/usr/bin/env python3
"""Recheck the saved source against an already installed Lean/Mathlib project.

This script does not install packages, invoke Lake, or call a model.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mathlib-project", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("sqrt-eight-check-new.json"))
    args = parser.parse_args()
    folder = Path(__file__).resolve().parent
    project = args.mathlib_project.resolve()
    libraries = sorted(p for p in project.rglob(".lake/build/lib") if p.is_dir())
    libraries += sorted(p for p in project.rglob(".lake/build/lib/lean") if p.is_dir())
    if not libraries:
        parser.error("No prebuilt Lean library directories found in the supplied project")
    env = os.environ.copy()
    env["LEAN_PATH"] = ":".join(str(p) for p in libraries)
    source = (folder / "Proof.lean").read_bytes()
    audit_source = (folder / "AxiomAudit.lean").read_bytes()
    expected_audit = source + b"\n\n#check main\n#print axioms main\n"
    if audit_source != expected_audit:
        raise SystemExit("AxiomAudit.lean must be the exact source plus the two print commands")
    provenance = json.loads((folder / "provenance.json").read_text())
    source_hash = hashlib.sha256(source).hexdigest()
    if source_hash != provenance["copied_source_sha256"]:
        raise SystemExit("Proof.lean no longer matches the preserved source hash")
    version = subprocess.run(["lean", "--version"], capture_output=True, text=True, check=True)
    mathlib_dir = project / ".lake/packages/mathlib"
    if not mathlib_dir.is_dir():
        mathlib_dir = project
    revision = subprocess.run(
        ["git", "-C", str(mathlib_dir), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    checks = []
    for filename in ["Proof.lean", "AxiomAudit.lean"]:
        start = time.monotonic()
        result = subprocess.run(
            ["lean", filename], cwd=folder, env=env,
            capture_output=True, text=True, timeout=90,
        )
        checks.append({
            "command": ["lean", filename],
            "exit_code": result.returncode,
            "duration_s": round(time.monotonic() - start, 3),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "source_sha256": hashlib.sha256((folder / filename).read_bytes()).hexdigest(),
        })
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "Independent local Lean compilation of exact saved source and separate axiom audit",
        "lean_version": version.stdout.strip(),
        "mathlib_git_revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "mathlib_source_revision_note": "Identifies the local source checkout; this check trusts the installed prebuilt libraries and does not rebuild them.",
        "source_sha256": source_hash,
        "matches_historical_verified_source": source_hash == provenance["historical_verification"]["source_sha256"],
        "checks": checks,
        "all_passed": all(item["exit_code"] == 0 for item in checks),
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
