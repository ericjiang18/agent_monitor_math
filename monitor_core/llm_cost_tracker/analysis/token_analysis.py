#!/usr/bin/env python3
"""Token consumption analysis for LLM agent benchmarks (inspired by arXiv 2604.22750).

Reads the per-turn CSV produced by extract_turn_data.py and generates:
1. Input/output token ratio analysis
2. Per-turn token growth curves (context accumulation)
3. Intra-task variability across trials (stochasticity)
4. Accuracy-cost relationship (accuracy at different token budgets)
5. Phase-level token composition (early/mid/late)
6. Cross-model token efficiency comparison

Usage:
    python analysis/token_analysis.py --input analysis/turn_data.csv --output-dir analysis/output
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Optional plotting — gracefully degrade if matplotlib unavailable
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_PLOT = True
except ImportError:
    HAS_PLOT = False


def load_data(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    # Compute all-input (uncached + cache_read + cache_write) per turn
    df["all_input_tokens"] = df["input_tokens"] + df["cache_read_input_tokens"] + df["cache_write_input_tokens"]
    return df


# ─────────────────────────────────────────────────────────────────────
# 1. Input/Output Ratio
# ─────────────────────────────────────────────────────────────────────
def analysis_io_ratio(df: pd.DataFrame, output_dir: Path):
    """Analyze the ratio of input to output tokens per task."""
    # Aggregate per task-trial
    task_df = df.groupby(["model", "domain", "task_id", "trial"]).agg(
        total_input=("all_input_tokens", "sum"),
        total_output=("output_tokens", "sum"),
        total_tokens=("total_tokens", "sum"),
        reward=("reward", "first"),
    ).reset_index()
    task_df["io_ratio"] = task_df["total_input"] / task_df["total_output"].clip(lower=1)
    task_df["input_fraction"] = task_df["total_input"] / task_df["total_tokens"].clip(lower=1)

    print("\n" + "=" * 60)
    print("1. INPUT/OUTPUT TOKEN RATIO")
    print("=" * 60)
    summary = task_df.groupby(["model", "domain"]).agg(
        mean_io_ratio=("io_ratio", "mean"),
        median_io_ratio=("io_ratio", "median"),
        mean_input_fraction=("input_fraction", "mean"),
        mean_total_tokens=("total_tokens", "mean"),
    ).round(2)
    print(summary.to_string())

    if HAS_PLOT:
        fig, ax = plt.subplots(figsize=(8, 5))
        for (model, domain), grp in task_df.groupby(["model", "domain"]):
            ax.hist(grp["input_fraction"], bins=20, alpha=0.5, label=f"{model} ({domain})")
        ax.set_xlabel("Input Token Fraction (input / total)")
        ax.set_ylabel("Count")
        ax.set_title("Distribution of Input Token Fraction per Task")
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(output_dir / "io_ratio_distribution.png", dpi=150)
        plt.close(fig)

    return task_df


# ─────────────────────────────────────────────────────────────────────
# 2. Per-Turn Token Growth Curves
# ─────────────────────────────────────────────────────────────────────
def analysis_turn_growth(df: pd.DataFrame, output_dir: Path):
    """Analyze how tokens grow turn-by-turn (context accumulation)."""
    print("\n" + "=" * 60)
    print("2. PER-TURN TOKEN GROWTH")
    print("=" * 60)

    # Average token counts by turn number across all tasks
    turn_avg = df.groupby(["model", "domain", "turn"]).agg(
        mean_input=("all_input_tokens", "mean"),
        mean_output=("output_tokens", "mean"),
        mean_cumulative_input=("cumulative_input_tokens", "mean"),
        mean_cumulative_total=("cumulative_total_tokens", "mean"),
        n_tasks=("task_id", "count"),
    ).reset_index()

    # Only show turns with enough data points
    turn_avg = turn_avg[turn_avg["n_tasks"] >= 5]

    for (model, domain), grp in turn_avg.groupby(["model", "domain"]):
        print(f"\n  {model} ({domain}) — first 10 turns:")
        subset = grp[grp["turn"] < 10][["turn", "mean_input", "mean_output", "mean_cumulative_total", "n_tasks"]]
        print(subset.to_string(index=False))

    if HAS_PLOT:
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        for (model, domain), grp in turn_avg.groupby(["model", "domain"]):
            label = f"{model} ({domain})"
            axes[0].plot(grp["turn"], grp["mean_input"], marker=".", label=label, alpha=0.7)
            axes[1].plot(grp["turn"], grp["mean_cumulative_total"], marker=".", label=label, alpha=0.7)
        axes[0].set_xlabel("Turn")
        axes[0].set_ylabel("Mean Input Tokens")
        axes[0].set_title("Per-Turn Input Tokens (context growth)")
        axes[0].legend(fontsize=7)
        axes[1].set_xlabel("Turn")
        axes[1].set_ylabel("Mean Cumulative Total Tokens")
        axes[1].set_title("Cumulative Token Consumption")
        axes[1].legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(output_dir / "turn_growth_curves.png", dpi=150)
        plt.close(fig)


# ─────────────────────────────────────────────────────────────────────
# 3. Intra-Task Variability (Stochasticity)
# ─────────────────────────────────────────────────────────────────────
def analysis_variability(df: pd.DataFrame, output_dir: Path):
    """Analyze token usage variability across trials for the same task."""
    print("\n" + "=" * 60)
    print("3. INTRA-TASK VARIABILITY (across trials)")
    print("=" * 60)

    # Total tokens per task-trial
    task_df = df.groupby(["model", "domain", "task_id", "trial"]).agg(
        total_tokens=("total_tokens", "sum"),
        n_turns=("turn", "max"),
    ).reset_index()

    # Per-task stats across trials
    var_df = task_df.groupby(["model", "domain", "task_id"]).agg(
        mean_tokens=("total_tokens", "mean"),
        std_tokens=("total_tokens", "std"),
        min_tokens=("total_tokens", "min"),
        max_tokens=("total_tokens", "max"),
        n_trials=("trial", "count"),
    ).reset_index()
    var_df["cv"] = var_df["std_tokens"] / var_df["mean_tokens"].clip(lower=1)
    var_df["max_min_ratio"] = var_df["max_tokens"] / var_df["min_tokens"].clip(lower=1)

    # Only analyze tasks with multiple trials
    multi_trial = var_df[var_df["n_trials"] > 1]

    if multi_trial.empty:
        print("  No multi-trial data available for variability analysis.")
        return

    summary = multi_trial.groupby(["model", "domain"]).agg(
        mean_cv=("cv", "mean"),
        median_cv=("cv", "median"),
        mean_max_min_ratio=("max_min_ratio", "mean"),
        max_max_min_ratio=("max_min_ratio", "max"),
    ).round(3)
    print(summary.to_string())

    if HAS_PLOT:
        fig, axes = plt.subplots(1, 2, figsize=(12, 5))
        for (model, domain), grp in multi_trial.groupby(["model", "domain"]):
            label = f"{model} ({domain})"
            axes[0].hist(grp["cv"].dropna(), bins=20, alpha=0.5, label=label)
            axes[1].hist(grp["max_min_ratio"].dropna(), bins=20, alpha=0.5, label=label)
        axes[0].set_xlabel("Coefficient of Variation")
        axes[0].set_title("Token Usage CV Across Trials")
        axes[0].legend(fontsize=8)
        axes[1].set_xlabel("Max/Min Token Ratio")
        axes[1].set_title("Max/Min Ratio Across Trials")
        axes[1].legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(output_dir / "variability.png", dpi=150)
        plt.close(fig)


# ─────────────────────────────────────────────────────────────────────
# 4. Accuracy-Cost Relationship
# ─────────────────────────────────────────────────────────────────────
def analysis_accuracy_cost(df: pd.DataFrame, output_dir: Path):
    """Analyze relationship between token consumption and task success."""
    print("\n" + "=" * 60)
    print("4. ACCURACY-COST RELATIONSHIP")
    print("=" * 60)

    task_df = df.groupby(["model", "domain", "task_id", "trial"]).agg(
        total_tokens=("total_tokens", "sum"),
        reward=("reward", "first"),
    ).reset_index()
    task_df["success"] = (task_df["reward"] > 0.99).astype(int)

    for (model, domain), grp in task_df.groupby(["model", "domain"]):
        print(f"\n  {model} ({domain}):")
        # Bin by token quartiles
        grp = grp.copy()
        grp["token_quartile"] = pd.qcut(grp["total_tokens"], q=4, labels=["Q1 (low)", "Q2", "Q3", "Q4 (high)"], duplicates="drop")
        quartile_acc = grp.groupby("token_quartile", observed=True)["success"].agg(["mean", "count"])
        quartile_tokens = grp.groupby("token_quartile", observed=True)["total_tokens"].mean()
        result = pd.concat([quartile_acc, quartile_tokens], axis=1)
        result.columns = ["accuracy", "n_tasks", "mean_tokens"]
        print(result.to_string())

        # Success vs failure token comparison
        success_tokens = grp[grp["success"] == 1]["total_tokens"]
        fail_tokens = grp[grp["success"] == 0]["total_tokens"]
        print(f"    Success mean tokens: {success_tokens.mean():.0f} (n={len(success_tokens)})")
        print(f"    Failure mean tokens: {fail_tokens.mean():.0f} (n={len(fail_tokens)})")

    if HAS_PLOT:
        fig, ax = plt.subplots(figsize=(8, 5))
        for (model, domain), grp in task_df.groupby(["model", "domain"]):
            grp = grp.copy()
            grp["token_bin"] = pd.qcut(grp["total_tokens"], q=10, duplicates="drop")
            bin_acc = grp.groupby("token_bin", observed=True).agg(
                accuracy=("success", "mean"),
                mean_tokens=("total_tokens", "mean"),
            ).reset_index()
            ax.plot(bin_acc["mean_tokens"], bin_acc["accuracy"], marker="o", label=f"{model} ({domain})", alpha=0.7)
        ax.set_xlabel("Mean Total Tokens (binned)")
        ax.set_ylabel("Accuracy")
        ax.set_title("Accuracy vs Token Consumption")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(output_dir / "accuracy_cost_curve.png", dpi=150)
        plt.close(fig)


# ─────────────────────────────────────────────────────────────────────
# 5. Phase-Level Token Composition
# ─────────────────────────────────────────────────────────────────────
def analysis_phase_composition(df: pd.DataFrame, output_dir: Path):
    """Analyze token composition across conversation phases (early/mid/late)."""
    print("\n" + "=" * 60)
    print("5. PHASE-LEVEL TOKEN COMPOSITION")
    print("=" * 60)

    PHASES = ["early", "early_mid", "mid", "late_mid", "late"]

    def assign_phase(row, max_turn):
        """Assign a phase based on relative position in the conversation."""
        if max_turn == 0:
            return "early"
        frac = row["turn"] / max_turn
        if frac < 0.2:
            return "early"
        elif frac < 0.4:
            return "early_mid"
        elif frac < 0.6:
            return "mid"
        elif frac < 0.8:
            return "late_mid"
        else:
            return "late"

    # Get max turn per task-trial
    max_turns = df.groupby(["model", "domain", "task_id", "trial"])["turn"].max().reset_index()
    max_turns.columns = ["model", "domain", "task_id", "trial", "max_turn"]
    phase_df = df.merge(max_turns, on=["model", "domain", "task_id", "trial"])
    phase_df["phase"] = phase_df.apply(lambda r: assign_phase(r, r["max_turn"]), axis=1)
    phase_df["phase"] = pd.Categorical(phase_df["phase"], categories=PHASES, ordered=True)

    # Token composition by phase
    phase_summary = phase_df.groupby(["model", "domain", "phase"], observed=True).agg(
        mean_input=("all_input_tokens", "mean"),
        mean_output=("output_tokens", "mean"),
        mean_cache_read=("cache_read_input_tokens", "mean"),
        mean_total=("total_tokens", "mean"),
    ).reset_index()
    phase_summary["output_ratio"] = phase_summary["mean_output"] / phase_summary["mean_total"].clip(lower=1)

    for (model, domain), grp in phase_summary.groupby(["model", "domain"]):
        print(f"\n  {model} ({domain}):")
        print(grp[["phase", "mean_input", "mean_output", "mean_cache_read", "output_ratio"]].to_string(index=False))

    if HAS_PLOT:
        fig, ax = plt.subplots(figsize=(10, 5))
        models_domains = phase_summary.groupby(["model", "domain"]).ngroups
        width = 0.8 / max(models_domains, 1)
        x = np.arange(len(PHASES))
        for i, ((model, domain), grp) in enumerate(phase_summary.groupby(["model", "domain"])):
            grp_sorted = grp.set_index("phase").reindex(PHASES)
            ax.bar(x + i * width, grp_sorted["output_ratio"].values, width,
                   label=f"{model} ({domain})", alpha=0.8)
        ax.set_xticks(x + width * (models_domains - 1) / 2)
        ax.set_xticklabels([p.replace("_", "-").title() for p in PHASES])
        ax.set_ylabel("Output Token Ratio")
        ax.set_title("Output Token Ratio by Conversation Phase")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        fig.savefig(output_dir / "phase_composition.png", dpi=150)
        plt.close(fig)


# ─────────────────────────────────────────────────────────────────────
# 6. Cross-Model Token Efficiency
# ─────────────────────────────────────────────────────────────────────
def analysis_model_comparison(df: pd.DataFrame, output_dir: Path):
    """Compare token efficiency across models."""
    print("\n" + "=" * 60)
    print("6. CROSS-MODEL TOKEN EFFICIENCY")
    print("=" * 60)

    task_df = df.groupby(["model", "domain", "task_id", "trial"]).agg(
        total_tokens=("total_tokens", "sum"),
        total_input=("all_input_tokens", "sum"),
        total_output=("output_tokens", "sum"),
        n_turns=("turn", "max"),
        reward=("reward", "first"),
    ).reset_index()
    task_df["n_turns"] = task_df["n_turns"] + 1  # 0-indexed
    task_df["success"] = (task_df["reward"] > 0.99).astype(int)
    task_df["tokens_per_turn"] = task_df["total_tokens"] / task_df["n_turns"].clip(lower=1)

    summary = task_df.groupby(["model", "domain"]).agg(
        n_tasks=("task_id", "count"),
        accuracy=("success", "mean"),
        mean_total_tokens=("total_tokens", "mean"),
        median_total_tokens=("total_tokens", "median"),
        mean_input_tokens=("total_input", "mean"),
        mean_output_tokens=("total_output", "mean"),
        mean_turns=("n_turns", "mean"),
        mean_tokens_per_turn=("tokens_per_turn", "mean"),
    ).round(1)
    print(summary.to_string())

    # Token efficiency: accuracy per 100K tokens
    summary["accuracy_per_100k"] = (summary["accuracy"] / (summary["mean_total_tokens"] / 100_000)).round(3)
    print("\n  Token Efficiency (accuracy per 100K tokens):")
    print(summary[["accuracy", "mean_total_tokens", "accuracy_per_100k"]].to_string())

    if HAS_PLOT and task_df["model"].nunique() > 1:
        fig, axes = plt.subplots(1, 2, figsize=(12, 5))
        # Bar chart: mean tokens by model
        model_means = task_df.groupby(["model", "domain"])["total_tokens"].mean().reset_index()
        for i, (domain, grp) in enumerate(model_means.groupby("domain")):
            ax = axes[i] if model_means["domain"].nunique() > 1 else axes[0]
            ax.barh(grp["model"], grp["total_tokens"], alpha=0.7)
            ax.set_xlabel("Mean Total Tokens")
            ax.set_title(f"Token Usage by Model ({domain})")
        fig.tight_layout()
        fig.savefig(output_dir / "model_comparison.png", dpi=150)
        plt.close(fig)


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Token consumption analysis for LLM agent benchmarks")
    parser.add_argument("--input", type=Path, default=Path("analysis/turn_data.csv"), help="Input CSV from extract_turn_data.py")
    parser.add_argument("--output-dir", type=Path, default=Path("analysis/output"), help="Output directory for figures and tables")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: {args.input} not found. Run extract_turn_data.py first.", file=sys.stderr)
        sys.exit(1)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading data from {args.input}...")
    df = load_data(args.input)
    print(f"  {len(df)} turn records, {df['model'].nunique()} model(s), {df['domain'].nunique()} domain(s)")
    print(f"  Estimated (legacy) data: {df['is_estimated'].sum()} / {len(df)} rows")

    # Run all analyses
    task_df = analysis_io_ratio(df, args.output_dir)
    analysis_turn_growth(df, args.output_dir)
    analysis_variability(df, args.output_dir)
    analysis_accuracy_cost(df, args.output_dir)
    analysis_phase_composition(df, args.output_dir)
    analysis_model_comparison(df, args.output_dir)

    print("\n" + "=" * 60)
    print(f"✅ Analysis complete. Outputs in {args.output_dir}/")
    if HAS_PLOT:
        print("   Figures: io_ratio_distribution.png, turn_growth_curves.png,")
        print("            variability.png, accuracy_cost_curve.png,")
        print("            phase_composition.png, model_comparison.png")
    print("=" * 60)


if __name__ == "__main__":
    main()
