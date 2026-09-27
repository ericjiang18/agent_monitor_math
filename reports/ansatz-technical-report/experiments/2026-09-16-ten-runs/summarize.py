#!/usr/bin/env python3
"""Validate ten collaboration checks and summarize their reported trial timings.

This script reads run-01.json through run-10.json beside itself and writes
summary.json, trials.csv, RESULTS.md, and results-table.tex. It never launches
an experiment. Existing derived outputs require the explicit --replace option.
Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import re
import statistics
import sys


HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[4]
HISTORICAL = ROOT / "docs/public/reliability/2026-09-09.json"
SOURCES = ("agent_monitor/projects.py", "scripts/check_collaboration_reliability.py")
CHECKS = {
    "unicode_and_deleted_anchors", "retry_idempotence", "convergence",
    "invalid_batch_rollback", "id_collision_rejected", "nonmember_rejected",
    "removed_member_rejected", "revoked_invite_rejected", "restart_persistence",
    "sqlite_integrity",
}
EXPECTED = {
    8: {"requests_measured": 760, "inserted_characters": 960,
        "replayed_batches": 112, "edit_count": 440, "read_count": 320},
    16: {"requests_measured": 1520, "inserted_characters": 1920,
         "replayed_batches": 224, "edit_count": 880, "read_count": 640},
}
OUTPUTS = ("summary.json", "trials.csv", "RESULTS.md", "results-table.tex")
METRICS = ("edit_p95_ms", "read_p95_ms", "duration_seconds")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_number(value: object, label: str) -> None:
    require(type(value) in (int, float) and math.isfinite(value) and value > 0,
            label + " must be a finite positive number")


def validate_trial(trial: dict, expected_clients: int, label: str) -> None:
    require(trial["clients"] == expected_clients, label + ": trials must occur in order 8 then 16")
    require(type(trial["rounds"]) is int and trial["rounds"] == 40, label + ": expected 40 rounds")
    for key, expected in EXPECTED[expected_clients].items():
        if key in ("edit_count", "read_count"):
            value = trial[key.split("_")[0] + "_latency"]["count"]
        else:
            value = trial[key]
        require(type(value) is int and value == expected, f"{label}: {key} must equal {expected}")
    for key in ("unexpected_errors", "lost_characters", "duplicate_characters"):
        require(type(trial[key]) is int and trial[key] == 0, label + ": nonzero or invalid " + key)
    require(set(trial["checks"]) == CHECKS, label + ": unexpected correctness-check schema")
    require(all(value is True for value in trial["checks"].values()), label + ": a correctness check failed")
    for key in ("edit_latency", "read_latency"):
        latencies = trial[key]
        for metric in ("p50_ms", "p95_ms", "max_ms"):
            positive_number(latencies[metric], f"{label}: {key}.{metric}")
        require(latencies["p50_ms"] <= latencies["p95_ms"] <= latencies["max_ms"],
                label + ": latency quantiles are out of order")
    positive_number(trial["duration_seconds"], label + ": duration_seconds")
    positive_number(trial["requests_per_second"], label + ": requests_per_second")


def collect(directory: Path) -> tuple[dict, list[dict]]:
    names = [f"run-{index:02}.json" for index in range(1, 11)]
    actual_names = {path.name for path in directory.glob("run-*.json")}
    require(actual_names == set(names), "Expected exactly run-01.json through run-10.json")
    historical = json.loads(HISTORICAL.read_text(encoding="utf-8"))
    require(set(historical["source_sha256"]) == set(SOURCES), "Unexpected historical source hash keys")
    manifest_path = directory / "experiment.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest["requested_repetitions"] == 10 and len(manifest["runs"]) == 10,
            "Experiment manifest must contain all ten requested runs")
    require(manifest["all_passed"] is True and "finished_at" in manifest and "aborted_reason" not in manifest,
            "Experiment manifest is unfinished, failed, or aborted")
    previous_finished = None
    for repetition, entry in enumerate(manifest["runs"], 1):
        require(entry["repetition"] == repetition and entry["output"] == names[repetition - 1],
                "Manifest run order differs from raw filenames")
        require(entry["exit_code"] == 0 and all(entry[key] is True for key in
                ("passed", "source_matches", "source_unchanged_after")), "A manifest run did not pass unchanged")
        started = datetime.fromisoformat(entry["started_at"])
        finished = datetime.fromisoformat(entry["finished_at"])
        require(started.utcoffset() is not None and finished.utcoffset() is not None and started < finished,
                "Invalid manifest run timestamps")
        require(previous_finished is None or previous_finished <= started, "Manifest run intervals overlap")
        previous_finished = finished
    reports, rows, start_times = [], [], []
    baseline_environment, baseline_hashes = None, None
    baseline_scope = None
    for run, name in enumerate(names, 1):
        path = directory / name
        report = json.loads(path.read_text(encoding="utf-8"))
        require(report["schema_version"] == 1, name + ": unsupported schema version")
        require(report["passed"] is True, name + ": top-level passed flag is not true")
        require(set(report["source_sha256"]) == set(SOURCES), name + ": unexpected source files")
        require(all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in report["source_sha256"].values()), name + ": malformed source digest")
        if baseline_environment is None:
            baseline_environment = report["environment"]
            baseline_hashes = report["source_sha256"]
            baseline_scope = report["scope"]
            require(set(baseline_environment) == {"python", "sqlite", "os", "architecture", "logical_cpus"},
                    name + ": unexpected environment schema")
        require(report["environment"] == baseline_environment, name + ": environment differs across runs")
        require(report["source_sha256"] == baseline_hashes, name + ": source differs across runs")
        require(report["source_sha256"] == manifest["source_sha256"], name + ": source differs from manifest")
        require(report["scope"] == baseline_scope, name + ": measurement scope differs across runs")
        timestamp = datetime.fromisoformat(report["recorded_at"])
        require(timestamp.utcoffset() is not None, name + ": recorded_at needs a timezone")
        run_entry = manifest["runs"][run - 1]
        require(datetime.fromisoformat(run_entry["started_at"]) <= timestamp <=
                datetime.fromisoformat(run_entry["finished_at"]), name + ": recorded_at is outside its manifest run interval")
        start_times.append(timestamp)
        require(len(report["trials"]) == 2, name + ": expected exactly two trials")
        reports.append({"run": run, "file": name, "sha256": digest(path),
                        "recorded_at": report["recorded_at"], "passed": True})
        for clients, trial in zip((8, 16), report["trials"]):
            validate_trial(trial, clients, f"{name}/{clients} clients")
            rows.append({
                "run": run, "file": name, "recorded_at": report["recorded_at"],
                "clients": clients, "rounds": trial["rounds"],
                "requests_measured": trial["requests_measured"],
                "inserted_characters": trial["inserted_characters"],
                "replayed_batches": trial["replayed_batches"],
                "edit_count": trial["edit_latency"]["count"],
                "edit_p50_ms": trial["edit_latency"]["p50_ms"],
                "edit_p95_ms": trial["edit_latency"]["p95_ms"],
                "edit_max_ms": trial["edit_latency"]["max_ms"],
                "read_count": trial["read_latency"]["count"],
                "read_p50_ms": trial["read_latency"]["p50_ms"],
                "read_p95_ms": trial["read_latency"]["p95_ms"],
                "read_max_ms": trial["read_latency"]["max_ms"],
                "duration_seconds": trial["duration_seconds"],
                "requests_per_second": trial["requests_per_second"],
                "unexpected_errors": 0, "lost_characters": 0, "duplicate_characters": 0,
                "all_checks_passed": True,
            })
    require(all(first < second for first, second in zip(start_times, start_times[1:])),
            "Run start timestamps are not strictly increasing")
    aggregates = {}
    for clients in (8, 16):
        selected = [row for row in rows if row["clients"] == clients]
        aggregates[str(clients)] = {
            "trials": len(selected),
            **{metric: {"median": round(statistics.median(values), 4), "min": min(values), "max": max(values)}
               for metric in METRICS for values in [[row[metric] for row in selected]]},
        }
    historical_comparison = {
        "file": str(HISTORICAL.relative_to(ROOT)), "sha256": digest(HISTORICAL),
        "recorded_at": historical["recorded_at"],
        "historical_environment": historical["environment"],
        "environment_matches": historical["environment"] == baseline_environment,
        "environment_differences": {
            key: {"historical": historical["environment"].get(key), "current": baseline_environment.get(key)}
            for key in sorted(set(historical["environment"]) | set(baseline_environment))
            if historical["environment"].get(key) != baseline_environment.get(key)
        },
        "historical_source_sha256": historical["source_sha256"],
        "source_hash_matches": {path: baseline_hashes[path] == historical["source_sha256"][path]
                                for path in SOURCES},
    }
    snapshots_match = {path: digest(directory / "source" / path) == baseline_hashes[path] for path in SOURCES}
    require(all(snapshots_match.values()), "Preserved source snapshots differ from raw reports")
    summary = {
        "schema_version": 1,
        "title": "Ten sequential collaboration reliability checks",
        "scope": baseline_scope,
        "protocol": {
            "requested_runs": 10, "reported_runs": len(reports), "total_trials": len(rows),
            "execution": "Sequential invocations; each invocation runs 8 clients then 16 clients.",
            "rounds_per_trial": 40, "warmup_runs_excluded": 0,
            "run_selection": "All ten numbered runs are included; no timing-based filtering.",
            "fresh_storage": "Each invocation creates disposable databases; each client count uses its own database.",
            "timing": "Request latency includes a new loopback HTTP connection, request/response processing, JSON decoding, and connection close. Trial duration covers concurrent edit/read rounds; setup and later correctness, rejection, and restart checks are excluded.",
            "aggregation": "Median, minimum, and maximum across ten per-run reported p95 values or durations. Request p95 values are nearest-rank quantiles rounded to 0.01 ms by the original script; durations are rounded to 0.001 s. These are not pooled request percentiles. The saved reports contain no request-level timing arrays, so a pooled p95 cannot be recomputed from them.",
            "run_start_timestamps_strictly_increasing": True,
            "sequentiality_evidence": "All ten nonoverlapping start/end intervals are verified from experiment.json; each raw report start lies within its corresponding interval.",
        },
        "environment": baseline_environment, "source_sha256": baseline_hashes,
        "working_tree_source_hash_matches": {path: digest(ROOT / path) == baseline_hashes[path] for path in SOURCES},
        "preserved_source_snapshot_hash_matches": snapshots_match,
        "experiment_manifest": {"file": manifest_path.name, "sha256": digest(manifest_path),
                                "started_at": manifest["started_at"], "finished_at": manifest["finished_at"],
                                "whole_experiment_seconds": manifest["whole_experiment_seconds"],
                                "sequential_nonoverlap_verified": True},
        "historical_comparison": historical_comparison,
        "raw_reports": reports,
        "aggregates_by_clients": aggregates,
        "totals": {key: sum(row[key] for row in rows) for key in
                   ("requests_measured", "inserted_characters", "replayed_batches", "unexpected_errors", "lost_characters", "duplicate_characters")},
        "correctness": {"all_twenty_trials_passed": True, "checks_per_trial": sorted(CHECKS),
                        "all_source_hashes_consistent": True, "all_environments_consistent": True},
        "summarizer_sha256": digest(Path(__file__).resolve()),
    }
    return summary, rows


def number(value: float) -> str:
    # The median of ten durations recorded to 0.001 s can need four decimals.
    return f"{value:.4f}".rstrip("0").rstrip(".")


def median_range(values: dict) -> str:
    return f"{number(values['median'])} [{number(values['min'])}, {number(values['max'])}]"


def markdown(summary: dict, rows: list[dict]) -> str:
    environment = summary["environment"]
    totals = summary["totals"]
    historical = summary["historical_comparison"]
    lines = [
        "# Ten sequential collaboration reliability checks", "",
        "All **10 runs / 20 trials** passed every recorded correctness check. Across both client counts, "
        f"the concurrent phases measured **{totals['requests_measured']:,} requests**, inserted "
        f"**{totals['inserted_characters']:,} characters**, and replayed **{totals['replayed_batches']:,} batches**. "
        "There were zero unexpected errors, lost characters, and duplicate characters.", "",
        "These are local collaborative-editing measurements using the real project routes and SQLite with synthetic identities. "
        "They do not measure model capability, mathematical proving performance, or production capacity.", "",
        "## Aggregate timings", "",
        "Each cell reports **median [minimum, maximum] across ten runs**. Edit/read columns aggregate each run's "
        "reported p95 latency; they are **not p95 values pooled across requests**. All ten runs are included.", "",
        "| Clients | Edit p95 (ms) | Read p95 (ms) | Concurrent-phase duration (s) |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for clients in (8, 16):
        aggregates = summary["aggregates_by_clients"][str(clients)]
        lines.append(f"| {clients} | " + " | ".join(median_range(aggregates[metric]) for metric in METRICS) + " |")
    edit_eight = number(summary["aggregates_by_clients"]["8"]["edit_p95_ms"]["median"])
    edit_sixteen = number(summary["aggregates_by_clients"]["16"]["edit_p95_ms"]["median"])
    lines += ["", "## Suggested paper paragraph", "",
              "> We repeated the isolated collaboration check ten times sequentially, running 8 clients followed by "
              "16 clients for 40 synchronized rounds in every repeat. All 20 trials passed the recorded correctness "
              f"checks, with zero unexpected errors, lost characters, or duplicate characters across {totals['requests_measured']:,} "
              f"measured requests. Median per-run edit p95 was {edit_eight} ms with 8 clients and {edit_sixteen} ms "
              "with 16 clients. The table reports medians and observed ranges across all ten repeats, with no warm-up "
              "exclusion. These values describe one host's local performance during the experiment; the correctness "
              "checks validate only the exercised scenarios. The measurements exclude production authentication, "
              "browser interaction, external networks, and model execution. Per-run p95 values are summarized "
              "without pooling request timings.", "",
              "The companion `results-table.tex` contains the replacement table body. Its caption should state: "
              "*Median [minimum, maximum] across ten sequential repeats per client count; latency statistics "
              "summarize per-run p95 values. All 20 trials passed.*", "",
              "## Protocol and timing", "",
              "Ten invocations run sequentially. Within every invocation the script runs **8 clients, then 16 clients**, "
              "with 40 synchronized rounds at each count. No warm-up run is excluded, and the condition order is fixed rather than randomized. "
              "Each invocation creates fresh disposable storage; the two client counts use separate databases.", "",
              "Each client inserts the three-character sequence `∑🙂x` per round, reads the shared state, "
              "replays every third insertion batch (rounds 0, 3, …, 39), and deletes the original anchor in round 0. "
              "Subsequent insertions continue to reference the deleted anchor.", "",
              "| Clients | Measured requests/trial | Edit requests | Read requests | Inserted characters | Replayed batches |",
              "| ---: | ---: | ---: | ---: | ---: | ---: |"]
    for clients, values in EXPECTED.items():
        lines.append(f"| {clients} | {values['requests_measured']} | {values['edit_count']} | {values['read_count']} | "
                     f"{values['inserted_characters']} | {values['replayed_batches']} |")
    lines += ["", summary["protocol"]["timing"], "", summary["protocol"]["aggregation"], "",
              "The original check also verifies Unicode/deleted-anchor behavior, content idempotence on replay, "
              "convergence after writes settle, rollback of invalid batches, identifier-collision rejection, "
              "nonmember and removed-member rejection, revoked invitations, persistence across process restart, "
              "and SQLite integrity. All ten named checks are true in every trial.", "",
              "## Every trial", "",
              "| Run | Clients | Edit p95 (ms) | Read p95 (ms) | Concurrent-phase duration (s) | Checks |",
              "| ---: | ---: | ---: | ---: | ---: | --- |"]
    for row in rows:
        lines.append(f"| [{row['run']:02}]({row['file']}) | {row['clients']} | {number(row['edit_p95_ms'])} | "
                     f"{number(row['read_p95_ms'])} | {number(row['duration_seconds'])} | Pass |")
    lines += ["", "## Machine and source identity", "",
              f"All runs report the same environment: **{environment['os']} {environment['architecture']}**, "
              f"**{environment['logical_cpus']} logical CPUs**, **Python {environment['python']}**, "
              f"and **SQLite {environment['sqlite']}**. CPU model, memory capacity, scheduler activity, and "
              "background workload are not measured by these raw reports.", "",
              f"The historical report is [`{historical['file']}`](../../../../docs/public/reliability/2026-09-09.json), "
              f"recorded at `{historical['recorded_at']}`. Historical environment matches these runs: "
              f"**{'yes' if historical['environment_matches'] else 'no'}**.", "",
              ]
    if historical["environment_differences"]:
        differences = "; ".join(f"`{key}`: historical **{values['historical']}**, current **{values['current']}**"
                                for key, values in historical["environment_differences"].items())
        lines += ["Recorded environment differences: " + differences + ".", ""]
    lines += ["| Tested source | SHA-256 in these runs | Matches historical report | Matches current file |",
              "| --- | --- | --- | --- |"]
    for source, source_hash in summary["source_sha256"].items():
        lines.append(f"| `{source}` | `{source_hash}` | "
                     f"{'Yes' if historical['source_hash_matches'][source] else 'No'} | "
                     f"{'Yes' if summary['working_tree_source_hash_matches'][source] else 'No'} |")
    lines += ["", "The historical timings are not included in the ten-run aggregates. Matching source hashes "
              "make the tested implementation identifiable; they do not establish equivalent host load or explain timing differences.", "",
              "## Limits", "",
              "- Ten repeated local runs on one host provide a small descriptive sample. The reported ranges are observed variation, not confidence intervals.",
              "- Latencies are a snapshot of this host's performance during these runs. Passing correctness checks validates the exercised scenarios, rather than guaranteeing future throughput, latency, or all possible concurrent behavior.",
              "- Raw reports retain latency summaries but no request-level timing arrays; they cannot support recomputing a pooled request p95.",
              "- Fixed 8-then-16 ordering may introduce order effects. Fresh databases do not eliminate host, filesystem, or process-cache effects.",
              "- Synthetic authentication and a loopback HTTP server with an enlarged connection backlog exclude production login, TLS, proxying, browser typing, network latency, and agent execution.",
              "- Replaying acknowledged batches tests retry content idempotence; the check does not simulate a lost HTTP response.",
              "- Restart testing kills the isolated server after acknowledged commits. It does not test power failure, disk loss, or interruption during a commit.",
              "- Workload size is 40 rounds with small documents. The experiment does not establish behavior near document limits or under sustained production contention.",
              "- Raw reports contain run-start timestamps, not run-end timestamps. The summarizer checks the runner manifest's nonoverlapping start/end intervals, each report's start within its interval, and fixed trial order.", "",
              "## Reproduce the summary", "",
              "From this directory, after all ten raw reports exist:", "", "```bash", "python3 summarize.py", "```", "",
              "Use `python3 summarize.py --replace` to regenerate the four derived files. "
              "The script validates every raw report before writing outputs; it never executes a reliability experiment. "
              "`summary.json` preserves report hashes, source identity, aggregation rules, and totals. "
              "`trials.csv` contains all 20 rows. `results-table.tex` is a standalone LaTeX `tabular` snippet; "
              "its values are median [minimum, maximum] across runs.", ""]
    return "\n".join(lines)


def latex(summary: dict) -> str:
    lines = [
        "% Ten runs per condition; cells are median [minimum, maximum].",
        "% Latency cells summarize per-run p95 values, not pooled request percentiles.",
        r"\begin{tabular}{rlll}", r"\hline",
        r"Clients & Edit p95 (ms) & Read p95 (ms) & Duration (s) \\", r"\hline",
    ]
    for clients in (8, 16):
        aggregates = summary["aggregates_by_clients"][str(clients)]
        lines.append(str(clients) + " & " + " & ".join(median_range(aggregates[metric]) for metric in METRICS) + r" \\")
    lines += [r"\hline", r"\end{tabular}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=HERE, help="Directory containing exactly ten numbered raw JSON reports")
    parser.add_argument("--replace", action="store_true", help="Replace existing derived outputs; raw reports are never modified")
    args = parser.parse_args()
    try:
        directory = args.directory.resolve()
        if not args.replace:
            require(not any((directory / name).exists() for name in OUTPUTS),
                    "Derived outputs already exist; use --replace to regenerate them")
        summary, rows = collect(directory)
        csv_buffer = io.StringIO(newline="")
        writer = csv.DictWriter(csv_buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        contents = {
            "summary.json": json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            "trials.csv": csv_buffer.getvalue(), "RESULTS.md": markdown(summary, rows),
            "results-table.tex": latex(summary),
        }
        for name, content in contents.items():
            with (directory / name).open("w" if args.replace else "x", encoding="utf-8", newline="") as target:
                target.write(content)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("Summary failed: " + str(exc), file=sys.stderr)
        return 1
    print("Validated ten runs and twenty trials; wrote " + ", ".join(OUTPUTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
