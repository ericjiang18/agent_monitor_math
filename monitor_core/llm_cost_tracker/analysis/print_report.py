#!/usr/bin/env python3
"""Print token consumption report in the format of arXiv 2604.22750.

Usage:
    # Single result file
    python analysis/print_report.py --results-file results/airline/.../file.json

    # Auto-merge partitioned retail files (glob pattern)
    python analysis/print_report.py --results-file "results_cached/retail/user_claude-sonnet-4-5-20250929-v1/tool-calling_claude-3-5-haiku-20241022-v1_0601021947_*.json"

    # All files in a directory (one report per model-domain combo)
    python analysis/print_report.py --results-dir results results_cached

    # List available result files
    python analysis/print_report.py --list
"""
import argparse
import glob
import json
import re
import sys
from collections import Counter
from math import comb
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_cost_tracker.metrics import count_invalid_calls


def load_results(paths: list[str]) -> list[dict]:
    """Load and merge results from one or more JSON files."""
    data = []
    for p in paths:
        with open(p) as f:
            data.extend(json.load(f))
    return data


def extract_model_name(filename: str) -> str:
    """Extract human-readable model name from filename."""
    stem = Path(filename).stem
    # tool-calling_claude-3-5-haiku-20241022-v1_0601015620
    # tool-calling_claude-haiku-4-5-20251001-v1_0601025405_0-30
    parts = stem.split("_")
    # Find model: skip strategy prefix, stop before timestamp/partition
    model_parts = []
    for p in parts[1:]:
        if re.match(r"^\d{10}$", p) or re.match(r"^\d+-\d+$", p):
            break
        model_parts.append(p)
    return "-".join(model_parts) if model_parts else stem


def extract_domain(filepath: str) -> str:
    for part in Path(filepath).parts:
        if part in ("retail", "airline"):
            return part
    return "unknown"


MODEL_DISPLAY = {
    "claude-3-5-haiku-20241022-v1": "Claude 3.5 Haiku",
    "claude-haiku-4-5-20251001-v1": "Claude 4.5 Haiku",
    "claude-3-5-sonnet-20241022-v2": "Claude 3.5 Sonnet",
    "claude-sonnet-4-5-20250929-v1": "Claude 4.5 Sonnet",
    "claude-opus-4-5-20251014-v1": "Claude 4.5 Opus",
}


def print_report(data: list[dict], model: str, domain: str):
    """Print the full 5-table report for a single model-domain result set."""
    # Deduplicate (same task_id + trial keeps last)
    seen = {}
    for r in data:
        seen[(r["task_id"], r["trial"])] = r
    data = list(seen.values())

    rows = []
    for r in data:
        cm = r["info"].get("cost_metrics", {})
        input_tok = cm.get("total_all_input_tokens", 0)
        output_tok = cm.get("total_output_tokens", 0)
        # Recompute invalid calls from traj if not in pre-computed metrics
        if "invalid_calls" in cm:
            invalid = cm["invalid_calls"]
            wasted_tokens = cm.get("wasted_total_tokens", 0)
            invalid_names = cm.get("invalid_tool_names", [])
        elif "traj" in r:
            inv_info = count_invalid_calls(r["traj"], domain)
            invalid = inv_info["invalid_calls"]
            wasted_tokens = inv_info["wasted_total_tokens"]
            invalid_names = inv_info["invalid_tool_names"]
        else:
            invalid = 0
            wasted_tokens = 0
            invalid_names = []
        rows.append({
            "task_id": r["task_id"],
            "trial": r["trial"],
            "reward": r["reward"],
            "total_tokens": input_tok + output_tok,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "total_cost": cm.get("total_llm_token_cost") or 0,
            "tool_calls": cm.get("total_tool_calls", 0),
            "redundant_calls": cm.get("redundant_calls", 0),
            "invalid_calls": invalid,
            "wasted_tokens": wasted_tokens,
            "invalid_tool_names": invalid_names,
            "cache_hit_rate": cm.get("cache_hit_rate", 0),
        })
    df = pd.DataFrame(rows)

    n_tasks = df["task_id"].nunique()
    n_trials = df["trial"].nunique()
    display_model = MODEL_DISPLAY.get(model, model)

    print("=" * 72)
    print(f"  TOKEN CONSUMPTION REPORT")
    print(f"  Model: {display_model} | Domain: {domain.title()} | Strategy: tool-calling")
    print(f"  Tasks: {n_tasks} | Trials: {n_trials} | Total Runs: {len(df)}")
    print("=" * 72)

    # Pass^k
    c_per_task = {}
    for _, row in df.iterrows():
        tid = row["task_id"]
        ok = 1 if row["reward"] > 0.99 else 0
        c_per_task[tid] = c_per_task.get(tid, 0) + ok
    print(f"\n  Pass^k:")
    for k in range(1, n_trials + 1):
        val = sum(comb(c, k) / comb(n_trials, k) for c in c_per_task.values()) / len(c_per_task)
        print(f"    k={k}: {val:.3f}")

    # Table 1: Token Consumption Summary
    print(f"\nTable 1: Token Consumption Statistics (per run)")
    print("-" * 72)
    header = f'{"Metric":<25} {"Mean":>12} {"Median":>12} {"Std":>12} {"Min":>10} {"Max":>10}'
    print(header)
    print("-" * 72)
    for col, label in [("total_tokens", "Total Tokens"), ("input_tokens", "Input Tokens"), ("output_tokens", "Output Tokens")]:
        s = df[col]
        print(f"{label:<25} {s.mean():>12,.0f} {s.median():>12,.0f} {s.std():>12,.0f} {s.min():>10,.0f} {s.max():>10,.0f}")
    print("-" * 72)
    io_ratio = df["input_tokens"].mean() / max(df["output_tokens"].mean(), 1)
    inp_pct = df["input_tokens"].mean() / max(df["total_tokens"].mean(), 1) * 100
    out_pct = df["output_tokens"].mean() / max(df["total_tokens"].mean(), 1) * 100
    print(f'{"Input/Output Ratio":<25} {io_ratio:>12.1f}x')
    print(f'{"Input Token %":<25} {inp_pct:>12.1f}%')
    print(f'{"Output Token %":<25} {out_pct:>12.1f}%')
    print(f'{"Avg Cache Hit Rate":<25} {df["cache_hit_rate"].mean():>12.1%}')

    # Table 2: Cost Statistics
    print(f"\nTable 2: Cost Statistics (per run, USD)")
    print("-" * 72)
    print(f'{"Metric":<25} {"Mean":>12} {"Median":>12} {"Std":>12} {"Min":>10} {"Max":>10}')
    print("-" * 72)
    c = df["total_cost"]
    print(f'{"Total Cost":<25} {"$"+f"{c.mean():.4f}":>12} {"$"+f"{c.median():.4f}":>12} {"$"+f"{c.std():.4f}":>12} {"$"+f"{c.min():.4f}":>10} {"$"+f"{c.max():.4f}":>10}')
    print("-" * 72)

    # Table 3: Accuracy vs Token Consumption
    print(f"\nTable 3: Accuracy vs Token Consumption")
    print("-" * 72)
    success = df[df["reward"] > 0.99]
    failure = df[df["reward"] < 0.01]
    n_s, n_f = len(success), len(failure)
    print(f'{"":25} {"Success (n="+str(n_s)+")":>20} {"Failure (n="+str(n_f)+")":>20} {"Ratio":>10}')
    print("-" * 72)
    if n_s > 0 and n_f > 0:
        for col, label in [("total_tokens", "Mean Total Tokens"), ("input_tokens", "Mean Input Tokens"),
                           ("output_tokens", "Mean Output Tokens"), ("total_cost", "Mean Cost"), ("tool_calls", "Mean Tool Calls")]:
            s_val = success[col].mean()
            f_val = failure[col].mean()
            ratio = f_val / s_val if s_val > 0 else float("inf")
            if col == "total_cost":
                print(f'{label:<25} {"$"+f"{s_val:.4f}":>20} {"$"+f"{f_val:.4f}":>20} {ratio:>10.2f}x')
            elif col == "tool_calls":
                print(f"{label:<25} {s_val:>20.1f} {f_val:>20.1f} {ratio:>10.2f}x")
            else:
                print(f"{label:<25} {s_val:>20,.0f} {f_val:>20,.0f} {ratio:>10.2f}x")
    else:
        print("  (insufficient data for comparison)")

    # Table 4: Variability
    print(f"\nTable 4: Intra-Task Variability (across {n_trials} trials per task)")
    print("-" * 72)
    task_stats = df.groupby("task_id")["total_tokens"].agg(["mean", "std", "min", "max", "count"])
    task_stats["cv"] = task_stats["std"] / task_stats["mean"].clip(lower=1)
    task_stats["max_min_ratio"] = task_stats["max"] / task_stats["min"].clip(lower=1)
    multi = task_stats[task_stats["count"] > 1]
    print(f'{"Metric":<35} {"Value":>15}')
    print("-" * 72)
    if len(multi) > 0:
        print(f'{"Mean CV across tasks":<35} {multi["cv"].mean():>15.3f}')
        print(f'{"Median CV across tasks":<35} {multi["cv"].median():>15.3f}')
        print(f'{"Mean Max/Min ratio":<35} {multi["max_min_ratio"].mean():>15.2f}x')
        print(f'{"Max Max/Min ratio":<35} {multi["max_min_ratio"].max():>15.2f}x')
        print(f'{"Tasks with CV > 0.3":<35} {(multi["cv"] > 0.3).sum():>12} / {len(multi)}')

    # Table 5: Accuracy by Token Quartile
    print(f"\nTable 5: Accuracy by Token Consumption Quartile")
    print("-" * 72)
    df["quartile"] = pd.qcut(df["total_tokens"], q=4, labels=["Q1 (lowest)", "Q2", "Q3", "Q4 (highest)"], duplicates="drop")
    q_stats = df.groupby("quartile", observed=True).agg(
        n=("reward", "count"),
        accuracy=("reward", "mean"),
        mean_tokens=("total_tokens", "mean"),
        mean_cost=("total_cost", "mean"),
    )
    print(f'{"Quartile":<15} {"N":>5} {"Accuracy":>10} {"Mean Tokens":>14} {"Mean Cost":>12}')
    print("-" * 72)
    for idx, row in q_stats.iterrows():
        cost_str = "$" + f"{row['mean_cost']:.4f}"
        print(f'{str(idx):<15} {int(row["n"]):>5} {row["accuracy"]:>10.3f} {row["mean_tokens"]:>14,.0f} {cost_str:>12}')
    print("-" * 72)
    all_cost_str = "$" + f"{df['total_cost'].mean():.4f}"
    print(f'{"ALL":<15} {len(df):>5} {df["reward"].mean():>10.3f} {df["total_tokens"].mean():>14,.0f} {all_cost_str:>12}')

    # Table 6: Tool Call Quality
    print(f"\nTable 6: Tool Call Quality")
    print("-" * 72)
    total_calls = df["tool_calls"].sum()
    total_invalid = df["invalid_calls"].sum()
    total_redundant = df["redundant_calls"].sum()
    total_wasted = df["wasted_tokens"].sum()
    runs_with_invalid = (df["invalid_calls"] > 0).sum()
    # Collect all invalid tool names
    all_invalid_names: list[str] = []
    for names in df["invalid_tool_names"]:
        all_invalid_names.extend(names)
    print(f'{"Metric":<35} {"Value":>15}')
    print("-" * 72)
    print(f'{"Total Tool Calls":<35} {total_calls:>15,}')
    print(f'{"Invalid (Hallucinated) Calls":<35} {total_invalid:>15,}')
    print(f'{"Redundant (Consecutive Dup) Calls":<35} {total_redundant:>15,}')
    print(f'{"Runs with Invalid Calls":<35} {runs_with_invalid:>12} / {len(df)}')
    invalid_rate = total_invalid / total_calls * 100 if total_calls > 0 else 0
    print(f'{"Invalid Call Rate":<35} {invalid_rate:>14.2f}%')
    print(f'{"Wasted Tokens (from invalid)":<35} {total_wasted:>15,}')
    if all_invalid_names:
        name_counts = Counter(all_invalid_names).most_common(5)
        print(f'{"Top Hallucinated Tool Names:":<35}')
        for name, cnt in name_counts:
            print(f'  {name:<33} {cnt:>15}')
    print("-" * 72)

    print()


def find_result_groups(dirs: list[str]) -> dict[tuple[str, str], list[str]]:
    """Find all result files grouped by (model, domain)."""
    groups: dict[tuple[str, str], list[str]] = {}
    for d in dirs:
        for path in sorted(Path(d).rglob("*.json")):
            model = extract_model_name(path.name)
            domain = extract_domain(str(path))
            key = (model, domain)
            groups.setdefault(key, []).append(str(path))
    return groups


def main():
    parser = argparse.ArgumentParser(description="Print token consumption report")
    parser.add_argument("--results-file", type=str, help="Path or glob pattern for result file(s)")
    parser.add_argument("--results-dir", type=str, nargs="+", help="Directories to scan for result files")
    parser.add_argument("--list", action="store_true", help="List available result groups")
    args = parser.parse_args()

    if args.results_file:
        paths = sorted(glob.glob(args.results_file))
        if not paths:
            print(f"No files matching: {args.results_file}", file=sys.stderr)
            sys.exit(1)
        data = load_results(paths)
        model = extract_model_name(paths[0])
        domain = extract_domain(paths[0])
        print_report(data, model, domain)

    elif args.results_dir or args.list:
        dirs = args.results_dir or ["results", "results_cached"]
        groups = find_result_groups(dirs)
        if args.list:
            print("Available result groups:")
            for (model, domain), files in sorted(groups.items()):
                n_results = sum(len(json.load(open(f))) for f in files)
                print(f"  {model} | {domain} | {len(files)} file(s) | {n_results} results")
            return
        for (model, domain), files in sorted(groups.items()):
            data = load_results(files)
            print_report(data, model, domain)
            print("\n" + "=" * 72 + "\n")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
