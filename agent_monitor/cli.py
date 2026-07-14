"""agent-monitor CLI — serve dashboard, build cache, run engines."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _bootstrap() -> None:
    # Allow running as `python -m agent_monitor.cli` before install
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from agent_monitor.paths import ensure_data_dirs, ensure_import_paths

    ensure_import_paths()
    ensure_data_dirs()


def _default_cache() -> Path:
    from agent_monitor import CACHE_DIR

    return CACHE_DIR


def cmd_serve(args: argparse.Namespace) -> None:
    from agent_monitor import CACHE_DIR, ENGINES_DIR, ROOT
    from agent_monitor.paths import ensure_data_dirs

    ensure_data_dirs()
    cache = Path(args.cache_dir).resolve() if args.cache_dir else CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["LLM_DASHBOARD_CACHE"] = str(cache)
    os.environ["LLM_MONITOR_PORT"] = str(args.port)
    os.environ["AGENT_MONITOR_PORT"] = str(args.port)
    os.environ["HARNESS_OUTPUT_ROOT"] = str(
        Path(args.harness_output).resolve()
        if args.harness_output
        else ENGINES_DIR / "ucla"
    )
    calls = Path(args.calls).resolve() if args.calls else ROOT / "data" / "calls.jsonl"
    os.environ["LLM_MONITOR_LOG"] = str(calls)
    if not calls.exists():
        calls.parent.mkdir(parents=True, exist_ok=True)
        calls.touch()

    from agent_monitor.console_server import main as console_main

    print(f"Unified Math Proving Console -> http://localhost:{args.port}")
    print(f"  classic monitor           -> http://localhost:{args.port}/monitor")
    print(f"  cache: {cache / 'harness'}")
    print(f"  root:  {ROOT}")
    console_main(port=args.port)


def cmd_build(args: argparse.Namespace) -> None:
    from agent_monitor import CACHE_DIR, ENGINES_DIR, RUNS_DIR
    from agent_monitor.builders import hermes_builder
    from agent_monitor.schema import normalize_run

    cache = Path(args.cache_dir).resolve() if args.cache_dir else CACHE_DIR
    harness_cache = cache / "harness"
    harness_cache.mkdir(parents=True, exist_ok=True)
    os.environ["LLM_DASHBOARD_CACHE"] = str(cache)

    entries: list[dict] = []

    # Keep existing demo / prior runs unless --replace
    manifest_path = harness_cache / "manifest.json"
    if manifest_path.exists() and not args.replace:
        try:
            prev = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries.extend(prev.get("runs") or [])
        except (json.JSONDecodeError, OSError):
            pass

    # Hermes runner outputs
    hermes_entries = hermes_builder.build_all(harness_cache, RUNS_DIR)
    entries = _merge_entries(entries, hermes_entries)

    # IMProof WorkflowRuns
    improof_dir = Path(args.improof_dir).resolve() if args.improof_dir else (
        ENGINES_DIR / "improof" / "WorkflowRuns"
    )
    if improof_dir.exists():
        from harness_dashboard.build import merge_manifest_entries
        from harness_dashboard.improofbench_builder import rebuild_from_improof_artifacts

        improof_entries = rebuild_from_improof_artifacts(improof_dir, cache_dir=harness_cache)
        for e in improof_entries:
            e["engine"] = "improof"
            # stamp engine into run json
            run_path = harness_cache / f"{e['run_id']}.json"
            if run_path.exists():
                try:
                    data = json.loads(run_path.read_text(encoding="utf-8"))
                    data = normalize_run(data, engine="improof")
                    run_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                except (json.JSONDecodeError, OSError):
                    pass
        entries = _merge_entries(entries, improof_entries)
        print(f"[agent-monitor] IMProof runs: {len(improof_entries)}")

    # UCLA artifacts (optional path)
    if args.ucla_dir:
        ucla_dir = Path(args.ucla_dir).resolve()
        if ucla_dir.exists():
            from harness_dashboard.artifact_builder import rebuild_from_artifacts

            ucla_entries = rebuild_from_artifacts(ucla_dir, cache_dir=harness_cache)
            for e in ucla_entries:
                e["engine"] = "ucla"
                run_path = harness_cache / f"{e['run_id']}.json"
                if run_path.exists():
                    try:
                        data = json.loads(run_path.read_text(encoding="utf-8"))
                        data = normalize_run(data, engine="ucla")
                        run_path.write_text(
                            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    except (json.JSONDecodeError, OSError):
                        pass
            entries = _merge_entries(entries, ucla_entries)
            print(f"[agent-monitor] UCLA runs: {len(ucla_entries)}")

    # Ensure demo improof sample stays tagged
    demo = harness_cache / "improof_prob_001.json"
    if demo.exists():
        try:
            data = normalize_run(json.loads(demo.read_text(encoding="utf-8")), engine="improof")
            demo.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            if not any(e.get("run_id") == "improof_prob_001" for e in entries):
                entries.append(
                    {
                        "run_id": "improof_prob_001",
                        "engine": "improof",
                        "trace_name": data.get("trace_name") or "improof_prob_001",
                        "source": "demo",
                        "agent_count": len(data.get("agents") or []),
                        "total_cost_usd": (data.get("totals") or {}).get("cost_usd"),
                    }
                )
        except (json.JSONDecodeError, OSError):
            pass

    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "runs": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[agent-monitor] Wrote manifest ({len(entries)} runs) -> {manifest_path}")


def _merge_entries(existing: list[dict], new_entries: list[dict]) -> list[dict]:
    by_id = {e.get("run_id"): e for e in existing if e.get("run_id")}
    for e in new_entries:
        rid = e.get("run_id")
        if rid:
            by_id[rid] = {**(by_id.get(rid) or {}), **e}
    return list(by_id.values())


def cmd_problems(args: argparse.Namespace) -> None:
    from agent_monitor import PROBLEMS_DIR

    manifest = PROBLEMS_DIR / "manifest.json"
    if not manifest.exists():
        print("No problems/manifest.json — run repo setup first.")
        sys.exit(1)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    problems = data.get("problems") or []
    if args.source:
        problems = [p for p in problems if p.get("source") == args.source]
    print(f"{len(problems)} problems")
    for p in problems[: args.limit]:
        print(f"  [{p.get('source')}] {p.get('problem_id')}  {p.get('statement_path')}")


def cmd_run(args: argparse.Namespace) -> None:
    from agent_monitor import PROBLEMS_DIR

    problem_path = Path(args.problem)
    if not problem_path.exists():
        candidate = PROBLEMS_DIR / args.problem
        if candidate.exists():
            problem_path = candidate
        else:
            # try manifest lookup by id
            man = PROBLEMS_DIR / "manifest.json"
            if man.exists():
                for p in json.loads(man.read_text(encoding="utf-8")).get("problems") or []:
                    if p.get("problem_id") == args.problem:
                        problem_path = PROBLEMS_DIR / p["statement_path"]
                        break
    if not problem_path.exists():
        print(f"Problem not found: {args.problem}")
        sys.exit(1)

    text = problem_path.read_text(encoding="utf-8", errors="replace")
    engine = args.engine
    if engine == "hermes":
        from agent_monitor.runners import hermes as hermes_runner

        run = hermes_runner.run_problem(
            text,
            problem_id=args.problem_id or problem_path.stem,
            model=args.model,
            max_iterations=args.max_iterations,
        )
        print(json.dumps({"run_id": run.get("run_id"), "engine": "hermes", "status": "ok"}, indent=2))
    elif engine == "ucla":
        from agent_monitor.runners import ucla as ucla_runner

        result = ucla_runner.run_problem(problem_path, problem_id=args.problem_id)
        print(json.dumps(result, indent=2)[:4000])
    elif engine == "improof":
        from agent_monitor.runners import improof as improof_runner

        result = improof_runner.run_problem(problem_path, problem_id=args.problem_id)
        print(json.dumps(result, indent=2)[:4000])
    else:
        print(f"Unknown engine: {engine}")
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    _bootstrap()
    from agent_monitor import CACHE_DIR, ENGINES_DIR

    parser = argparse.ArgumentParser(
        prog="agent-monitor",
        description="Unified informal math proving console (UCLA / IMProof / Hermes)",
    )
    sub = parser.add_subparsers(dest="command")

    serve_p = sub.add_parser("serve", help="Start pipeline dashboard")
    serve_p.add_argument("--port", type=int, default=int(os.environ.get("AGENT_MONITOR_PORT", "4600")))
    serve_p.add_argument("--cache-dir", type=str, default=str(CACHE_DIR))
    serve_p.add_argument("--calls", type=str, default="")
    serve_p.add_argument("--harness-output", type=str, default="")

    build_p = sub.add_parser("build", help="Build unified run cache / manifest")
    build_p.add_argument("--cache-dir", type=str, default=str(CACHE_DIR))
    build_p.add_argument(
        "--improof-dir",
        type=str,
        default=str(ENGINES_DIR / "improof" / "WorkflowRuns"),
    )
    build_p.add_argument("--ucla-dir", type=str, default="", help="Optional UCLA _harness_runs root")
    build_p.add_argument("--replace", action="store_true", help="Replace manifest instead of merge")

    probs = sub.add_parser("problems", help="List bundled problems")
    probs.add_argument("--source", type=str, default="")
    probs.add_argument("--limit", type=int, default=50)

    run_p = sub.add_parser("run", help="Run a problem with selected engine")
    run_p.add_argument("engine", choices=["hermes", "ucla", "improof"])
    run_p.add_argument("problem", help="Problem id or path to statement file")
    run_p.add_argument("--problem-id", type=str, default="")
    run_p.add_argument("--model", type=str, default="")
    run_p.add_argument("--max-iterations", type=int, default=60)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "build":
        cmd_build(args)
    elif args.command == "problems":
        cmd_problems(args)
    elif args.command == "run":
        cmd_run(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
