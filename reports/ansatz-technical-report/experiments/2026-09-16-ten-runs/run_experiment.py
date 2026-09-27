"""Run ten sequential repetitions of the unmodified Section 7.2 workload.

Each repetition uses a fresh subprocess and the benchmark's isolated database.
All attempts are retained; failed attempts are not silently replaced.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SOURCES = ("agent_monitor/projects.py", "scripts/check_collaboration_reliability.py")


def now():
    return datetime.now(timezone.utc).isoformat()


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def save(manifest):
    temporary = HERE / "experiment.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(HERE / "experiment.json")


def main():
    if (HERE / "experiment.json").exists() or any(HERE.glob("run-*.json")):
        raise SystemExit("This experiment already has results; choose a fresh directory.")
    expected = hashes()
    for name in SOURCES:
        destination = HERE / "source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    manifest = {
        "schema_version": 1,
        "started_at": now(),
        "requested_repetitions": 10,
        "protocol": "Ten sequential fresh processes; each runs 8 then 16 clients for 40 rounds. No repetitions discarded as warmup. Existing benchmark unmodified.",
        "scope": "Temporary loopback HTTP servers, synthetic identities, isolated SQLite databases. No model calls or production requests.",
        "source_sha256": expected,
        "runs": [],
    }
    save(manifest)
    total_started = time.perf_counter()
    for repetition in range(1, 11):
        if hashes() != expected:
            manifest["aborted_reason"] = "Source changed before repetition " + str(repetition)
            save(manifest)
            raise SystemExit(manifest["aborted_reason"])
        output = HERE / f"run-{repetition:02d}.json"
        log = HERE / f"run-{repetition:02d}.log"
        command = [str(ROOT / ".venv/bin/python"), str(ROOT / SOURCES[1]), "--output", str(output)]
        entry = {"repetition": repetition, "started_at": now(), "command": command,
                 "output": output.name, "log": log.name}
        started = time.perf_counter()
        print(f"Starting repetition {repetition}/10", flush=True)
        with log.open("x") as stream:
            result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        entry.update(exit_code=result.returncode, finished_at=now(),
                     whole_process_seconds=round(time.perf_counter()-started, 3),
                     source_unchanged_after=hashes() == expected)
        if result.returncode == 0 and output.exists():
            data = json.loads(output.read_text())
            entry["passed"] = data.get("passed") is True
            entry["source_matches"] = data.get("source_sha256") == expected
            short = [{"clients": t["clients"], "edit_p95_ms": t["edit_latency"]["p95_ms"],
                      "read_p95_ms": t["read_latency"]["p95_ms"], "duration_s": t["duration_seconds"]}
                     for t in data["trials"]]
            print(f"Repetition {repetition}/10: " + json.dumps(short), flush=True)
        else:
            entry["passed"] = False
            print(f"Repetition {repetition}/10 failed: inspect {log.name}", flush=True)
        manifest["runs"].append(entry)
        save(manifest)
        if not entry["source_unchanged_after"]:
            manifest["aborted_reason"] = "Source changed during repetition " + str(repetition)
            save(manifest)
            raise SystemExit(manifest["aborted_reason"])
    manifest.update(finished_at=now(), whole_experiment_seconds=round(time.perf_counter()-total_started,3),
                    all_passed=all(r["passed"] and r.get("source_matches") for r in manifest["runs"]))
    save(manifest)
    print("Finished ten repetitions. All passed: " + str(manifest["all_passed"]), flush=True)
    if not manifest["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
