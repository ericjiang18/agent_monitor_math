#!/usr/bin/env python3
"""Extract per-turn token data from benchmark result JSONs into a flat CSV.

Reads result files (EnvRunResult JSON arrays) and extracts per-turn token
usage from the `_usage` field attached to each assistant message in the
trajectory. Also handles legacy results that lack `_usage` by falling back
to task-level totals divided evenly (marked with is_estimated=True).

Output CSV columns:
    model, domain, task_id, trial, turn, input_tokens, output_tokens,
    cache_read_input_tokens, cache_write_input_tokens, total_tokens,
    cumulative_input_tokens, cumulative_output_tokens, cumulative_total_tokens,
    tool_name, reward, is_estimated

Usage:
    python analysis/extract_turn_data.py --results-dir results_cached --output analysis/turn_data.csv
    python analysis/extract_turn_data.py --results-file results_cached/retail/.../file.json --output analysis/turn_data.csv
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


RESPOND_ACTION_NAME = "respond"


def extract_model_from_filename(filename: str) -> str:
    """Extract model short name from result filename.

    Example: tool-calling_claude-haiku-4-5-20251001-v1_0527173223.json
             -> claude-haiku-4-5-20251001-v1
    """
    stem = Path(filename).stem
    # Remove strategy prefix and timestamp suffix
    parts = stem.split("_")
    # Strategy is first part (tool-calling, react, act, few-shot)
    # Timestamp is last part (digits)
    # Model is everything in between
    if len(parts) >= 3 and parts[-1].isdigit():
        return "_".join(parts[1:-1])
    if stem.endswith("_MERGED"):
        return "_".join(parts[1:-1])
    return "_".join(parts[1:])


def extract_domain_from_path(filepath: Path) -> str:
    """Extract domain (retail/airline) from the file path."""
    for part in filepath.parts:
        if part in ("retail", "airline"):
            return part
    return "unknown"


def get_tool_name_from_message(msg: Dict[str, Any]) -> Optional[str]:
    """Extract tool name from an assistant message."""
    if msg.get("tool_calls"):
        return msg["tool_calls"][0]["function"]["name"]
    # Text-based action parsing (react/act)
    content = msg.get("content", "")
    if content and "Action:" in content:
        action_str = content.split("Action:")[-1].strip()
        try:
            parsed = json.loads(action_str)
            name = parsed.get("name", "")
            if name:
                return name
        except (json.JSONDecodeError, AttributeError):
            pass
    return RESPOND_ACTION_NAME


def extract_turns_from_result(result: Dict[str, Any], model: str, domain: str) -> List[Dict[str, Any]]:
    """Extract per-turn rows from a single EnvRunResult."""
    task_id = result["task_id"]
    trial = result["trial"]
    reward = result["reward"]
    traj = result.get("traj", [])

    if not traj:
        return []

    # Find assistant messages (these are the "turns")
    assistant_msgs = [(i, msg) for i, msg in enumerate(traj) if msg.get("role") == "assistant"]

    if not assistant_msgs:
        return []

    # Check if we have per-turn _usage data
    has_usage = any(msg.get("_usage") for _, msg in assistant_msgs)

    rows = []
    cum_input = 0
    cum_output = 0
    cum_total = 0

    if has_usage:
        for turn_idx, (_, msg) in enumerate(assistant_msgs):
            usage = msg.get("_usage", {})
            input_tok = usage.get("input_tokens", 0)
            output_tok = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            cache_write = usage.get("cache_write_input_tokens", 0)
            total = input_tok + output_tok + cache_read + cache_write

            cum_input += input_tok + cache_read + cache_write
            cum_output += output_tok
            cum_total += total

            rows.append({
                "model": model,
                "domain": domain,
                "task_id": task_id,
                "trial": trial,
                "turn": turn_idx,
                "input_tokens": input_tok,
                "output_tokens": output_tok,
                "cache_read_input_tokens": cache_read,
                "cache_write_input_tokens": cache_write,
                "total_tokens": total,
                "cumulative_input_tokens": cum_input,
                "cumulative_output_tokens": cum_output,
                "cumulative_total_tokens": cum_total,
                "tool_name": get_tool_name_from_message(msg),
                "reward": reward,
                "is_estimated": False,
            })
    else:
        # Legacy: distribute task-level totals evenly across turns
        cost_metrics = result.get("info", {}).get("cost_metrics", {})
        total_input = cost_metrics.get("total_input_tokens", 0) or 0
        total_output = cost_metrics.get("total_output_tokens", 0) or 0
        total_cache_read = cost_metrics.get("total_cache_read_input_tokens", 0) or 0
        total_cache_write = cost_metrics.get("total_cache_write_input_tokens", 0) or 0
        n_turns = len(assistant_msgs)

        for turn_idx, (_, msg) in enumerate(assistant_msgs):
            # Simple even distribution for legacy data
            input_tok = total_input // n_turns
            output_tok = total_output // n_turns
            cache_read = total_cache_read // n_turns
            cache_write = total_cache_write // n_turns
            total = input_tok + output_tok + cache_read + cache_write

            cum_input += input_tok + cache_read + cache_write
            cum_output += output_tok
            cum_total += total

            rows.append({
                "model": model,
                "domain": domain,
                "task_id": task_id,
                "trial": trial,
                "turn": turn_idx,
                "input_tokens": input_tok,
                "output_tokens": output_tok,
                "cache_read_input_tokens": cache_read,
                "cache_write_input_tokens": cache_write,
                "total_tokens": total,
                "cumulative_input_tokens": cum_input,
                "cumulative_output_tokens": cum_output,
                "cumulative_total_tokens": cum_total,
                "tool_name": get_tool_name_from_message(msg),
                "reward": reward,
                "is_estimated": True,
            })

    return rows


def process_file(filepath: Path) -> List[Dict[str, Any]]:
    """Process a single results JSON file."""
    model = extract_model_from_filename(filepath.name)
    domain = extract_domain_from_path(filepath)

    with open(filepath) as f:
        results = json.load(f)

    all_rows = []
    for result in results:
        rows = extract_turns_from_result(result, model, domain)
        all_rows.extend(rows)

    return all_rows


def find_result_files(results_dir: Path) -> List[Path]:
    """Find all result JSON files in a directory tree."""
    files = []
    for p in results_dir.rglob("*.json"):
        # Skip non-result files
        if p.name.startswith("."):
            continue
        # Only include files that look like result files (contain task_id, reward, traj)
        try:
            with open(p) as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) > 0 and "task_id" in data[0]:
                files.append(p)
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
    return sorted(files)


def main():
    parser = argparse.ArgumentParser(description="Extract per-turn token data from result JSONs")
    parser.add_argument("--results-dir", type=Path, help="Directory containing result JSON files (recursive)")
    parser.add_argument("--results-file", type=Path, help="Single result JSON file")
    parser.add_argument("--output", type=Path, default=Path("analysis/turn_data.csv"), help="Output CSV path")
    parser.add_argument("--merged-only", action="store_true", help="Only process *_MERGED.json files")
    args = parser.parse_args()

    if not args.results_dir and not args.results_file:
        print("Error: provide --results-dir or --results-file", file=sys.stderr)
        sys.exit(1)

    files = []
    if args.results_file:
        files = [args.results_file]
    else:
        files = find_result_files(args.results_dir)
        if args.merged_only:
            files = [f for f in files if "MERGED" in f.name]

    if not files:
        print("No result files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(files)} result file(s)...")

    all_rows = []
    for filepath in files:
        print(f"  {filepath.relative_to(filepath.parents[2]) if len(filepath.parts) > 3 else filepath}")
        rows = process_file(filepath)
        all_rows.extend(rows)
        print(f"    -> {len(rows)} turn records")

    if not all_rows:
        print("No turn data extracted.", file=sys.stderr)
        sys.exit(1)

    # Write CSV
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(all_rows[0].keys())
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\n✅ Wrote {len(all_rows)} rows to {args.output}")
    # Summary
    models = sorted(set(r["model"] for r in all_rows))
    domains = sorted(set(r["domain"] for r in all_rows))
    estimated = sum(1 for r in all_rows if r["is_estimated"])
    print(f"   Models: {models}")
    print(f"   Domains: {domains}")
    print(f"   Estimated (legacy, no per-turn data): {estimated}/{len(all_rows)} rows")


if __name__ == "__main__":
    main()
