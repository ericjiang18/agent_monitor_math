#!/usr/bin/env python3
"""CLI for harness-dashboard.

Usage:
    python -m harness_dashboard.cli serve [--port 4600]
    python -m harness_dashboard.cli build [--force] [--log-dir previous_log]
    python -m harness_dashboard.cli build-logs [--log-dir previous_log]
    python -m harness_dashboard.cli build-ucla [--ucla-dir UCLA/Logs/_harness_runs]
    python -m harness_dashboard.cli build-improofbench [--improof-dir IMProofBench/WorkflowRuns]
    python -m harness_dashboard.cli build-all [--ucla-dir ...] [--improof-dir ...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(prog="harness-dashboard", description="Harness pipeline monitor")
    sub = parser.add_subparsers(dest="command")

    serve_p = sub.add_parser("serve", help="Start harness dashboard HTTP server")
    serve_p.add_argument("--port", type=int, default=4600)
    serve_p.add_argument("--calls", type=str, default="monitor/calls.jsonl")
    serve_p.add_argument("--cache-dir", type=str, default="monitor/.cache")
    serve_p.add_argument("--harness-output", type=str, default="harness_0518_Final/output")

    build_p = sub.add_parser("build", help="Build harness dashboard cache")
    build_p.add_argument("--calls", type=str, default="monitor/calls.jsonl")
    build_p.add_argument("--cache-dir", type=str, default="monitor/.cache")
    build_p.add_argument("--harness-output", type=str, default="harness_0518_Final/output")
    build_p.add_argument("--log-dir", type=str, default="previous_log", help="Parse harness stdout logs from this dir")
    build_p.add_argument("--artifact-root", type=str, default="", help="Build from harness artifact dirs (e.g. UCLA/Logs/_harness_runs)")
    build_p.add_argument("--skip-logs", action="store_true", help="Skip previous_log stdout parsing")
    build_p.add_argument("--force", action="store_true")

    ucla_p = sub.add_parser("build-ucla", help="Build dashboard cache from UCLA harness artifacts only")
    ucla_p.add_argument("--ucla-dir", type=str, default="UCLA/Logs/_harness_runs")
    ucla_p.add_argument("--cache-dir", type=str, default="monitor/.cache")
    ucla_p.add_argument("--replace", action="store_true", help="Replace manifest with UCLA runs only")

    improof_p = sub.add_parser("build-improofbench", help="Build dashboard cache from IMProofBench WorkflowRuns")
    improof_p.add_argument("--improof-dir", type=str, default="IMProofBench/WorkflowRuns")
    improof_p.add_argument("--cache-dir", type=str, default="monitor/.cache")
    improof_p.add_argument("--replace", action="store_true", help="Replace manifest with IMProofBench runs only")

    all_p = sub.add_parser("build-all", help="Build UCLA + IMProofBench artifact runs into one manifest")
    all_p.add_argument("--ucla-dir", type=str, default="UCLA/Logs/_harness_runs")
    all_p.add_argument("--improof-dir", type=str, default="IMProofBench/WorkflowRuns")
    all_p.add_argument("--cache-dir", type=str, default="monitor/.cache")
    all_p.add_argument("--log-dir", type=str, default="previous_log")
    all_p.add_argument("--skip-logs", action="store_true")

    logs_p = sub.add_parser("build-logs", help="Build dashboard cache from previous_log/*.log only")
    logs_p.add_argument("--log-dir", type=str, default="previous_log")
    logs_p.add_argument("--cache-dir", type=str, default="monitor/.cache")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cache_dir = os.path.abspath(getattr(args, "cache_dir", "monitor/.cache"))
    os.environ["LLM_DASHBOARD_CACHE"] = cache_dir

    if args.command == "build":
        os.environ["LLM_MONITOR_LOG"] = os.path.abspath(args.calls)
        os.environ["HARNESS_OUTPUT_ROOT"] = os.path.abspath(args.harness_output)
        from harness_dashboard import build as build_mod
        build_mod.CALLS_JSONL = build_mod.Path(os.environ["LLM_MONITOR_LOG"])
        build_mod.CACHE_DIR = build_mod.Path(os.environ["LLM_DASHBOARD_CACHE"]) / "harness"
        build_mod.HARNESS_OUTPUT_ROOT = build_mod.Path(os.environ["HARNESS_OUTPUT_ROOT"])
        log_dir = build_mod.Path(os.path.abspath(getattr(args, "log_dir", "previous_log")))
        artifact_root = None
        if getattr(args, "artifact_root", ""):
            artifact_root = build_mod.Path(os.path.abspath(args.artifact_root))
        build_mod.rebuild(
            force=args.force,
            log_dir=log_dir,
            artifact_root=artifact_root,
            skip_logs=getattr(args, "skip_logs", False),
        )

    elif args.command == "build-ucla":
        from harness_dashboard import build as build_mod
        from harness_dashboard.artifact_builder import rebuild_from_artifacts

        cache = build_mod.Path(os.path.abspath(args.cache_dir)) / "harness"
        ucla_dir = build_mod.Path(os.path.abspath(args.ucla_dir))
        entries = rebuild_from_artifacts(ucla_dir, cache_dir=cache)
        if getattr(args, "replace", False):
            manifest_path = cache / "manifest.json"
            manifest = {"runs": entries, "built_at": entries[0].get("last_ts") if entries else None}
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            print(f"[harness_dashboard] Wrote manifest ({len(entries)} UCLA runs)")
        else:
            build_mod.merge_manifest_entries(cache, entries)

    elif args.command == "build-improofbench":
        from harness_dashboard import build as build_mod
        from harness_dashboard.improofbench_builder import rebuild_from_improof_artifacts

        cache = build_mod.Path(os.path.abspath(args.cache_dir)) / "harness"
        improof_dir = build_mod.Path(os.path.abspath(args.improof_dir))
        entries = rebuild_from_improof_artifacts(improof_dir, cache_dir=cache)
        if getattr(args, "replace", False):
            manifest_path = cache / "manifest.json"
            manifest = {"runs": entries, "built_at": entries[0].get("last_ts") if entries else None}
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            print(f"[harness_dashboard] Wrote manifest ({len(entries)} IMProofBench runs)")
        else:
            build_mod.merge_manifest_entries(cache, entries)

    elif args.command == "build-all":
        from harness_dashboard import build as build_mod
        from harness_dashboard.artifact_builder import rebuild_from_artifacts
        from harness_dashboard.improofbench_builder import rebuild_from_improof_artifacts

        cache = build_mod.Path(os.path.abspath(args.cache_dir)) / "harness"
        ucla_dir = build_mod.Path(os.path.abspath(args.ucla_dir))
        improof_dir = build_mod.Path(os.path.abspath(args.improof_dir))
        entries = []
        entries.extend(rebuild_from_artifacts(ucla_dir, cache_dir=cache))
        entries.extend(rebuild_from_improof_artifacts(improof_dir, cache_dir=cache))
        if not getattr(args, "skip_logs", False):
            log_dir = build_mod.Path(os.path.abspath(args.log_dir))
            log_entries = build_mod.rebuild_from_logs(log_dir)
            existing_ids = {e["run_id"] for e in entries}
            for le in log_entries:
                if le["run_id"] not in existing_ids:
                    entries.append(le)
        build_mod.merge_manifest_entries(cache, entries)

    elif args.command == "build-logs":
        from harness_dashboard import build as build_mod
        build_mod.CACHE_DIR = build_mod.Path(os.path.abspath(args.cache_dir)) / "harness"
        log_dir = build_mod.Path(os.path.abspath(args.log_dir))
        entries = build_mod.rebuild_from_logs(log_dir)
        manifest_path = build_mod.CACHE_DIR / "manifest.json"
        manifest = {"runs": entries, "built_at": entries[0].get("last_ts") if entries else None}
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"[harness_dashboard] Wrote manifest ({len(entries)} log runs)")

    elif args.command == "serve":
        os.environ["LLM_MONITOR_LOG"] = os.path.abspath(args.calls)
        os.environ["HARNESS_OUTPUT_ROOT"] = os.path.abspath(args.harness_output)
        os.environ["LLM_MONITOR_PORT"] = str(args.port)
        from harness_dashboard import server
        server.CACHE_DIR = server.Path(cache_dir) / "harness"
        server.LOG_PATH = os.environ["LLM_MONITOR_LOG"]
        server.HARNESS_OUTPUT_ROOT = server.Path(os.environ["HARNESS_OUTPUT_ROOT"])
        server.PORT = args.port
        server.main()


if __name__ == "__main__":
    main()
