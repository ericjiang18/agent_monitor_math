# Section 7.2: ten repetitions

This directory records ten sequential executions of the existing collaboration
reliability workload on 16 September 2026. Each execution runs eight clients,
then sixteen clients, for forty synchronized rounds each. Every repetition uses
a fresh Python process and fresh temporary SQLite databases. All repetitions
are retained; the first is not discarded as a warmup.

## Results and evidence

- `RESULTS.md`: readable summary, individual trials, and interpretation.
- `results-table.tex`: LaTeX table for the paper, reporting median and range.
- `summary.json`: machine-readable aggregate statistics and validation.
- `trials.csv`: twenty rows, one per repetition/client-count combination.
- `run-01.json` through `run-10.json`: original benchmark output, unmodified.
- `run-01.log` through `run-10.log`: stdout and stderr, including the locations
  of retained temporary test databases.
- `experiment.json`: start/end times, commands, exit codes, and source hashes.
- `source/`: copies of the two benchmarked source files captured before the run.
- `run_experiment.py`: the sequential experiment driver.
- `summarize.py`: validation and aggregation of the ten raw reports.

The driver checks the two source hashes before and after every repetition. The
summary validates the expected request counts, operation counts, correctness
checks, environment, and source identity across the reports.

## What the timing means

Each raw report records the nearest-rank 95th percentile of individual edit or
read request times, rounded to milliseconds with two decimal places. The
summary reports the median and minimum–maximum range of those ten per-run p95
values. It does not pool individual request times; the benchmark does not retain
the underlying request-timing arrays.

`duration_seconds` measures the concurrent edit/read phase. It excludes project
setup, final invariant checks, rejection tests, and restart checks. The driver's
`whole_process_seconds` includes these additional phases and is not substituted
for the paper's duration column.

The workload uses the actual project HTTP routes with synthetic authentication,
loopback networking, and isolated storage. It makes no model calls or production
requests. These measurements describe this host during this experiment. CPU
scheduling, storage, caches, and other host activity can affect the timings.
The fixed eight-then-sixteen ordering and absence of warmup exclusion should be
retained when describing the method.

## Reproduce a fresh batch

Run from the repository root in a terminal that permits localhost sockets:

```bash
batch_dir=$(mktemp -d /tmp/ansatz-ten-runs.XXXXXX)
for repetition in 01 02 03 04 05 06 07 08 09 10; do
  .venv/bin/python scripts/check_collaboration_reliability.py \
    --output "$batch_dir/run-$repetition.json" \
    > "$batch_dir/run-$repetition.log" 2>&1 || break
done
```

This starts a new batch and preserves its logs. If a run fails, inspect it before
proceeding; do not discard the failure or present a partial batch as ten
successful repetitions. The results in this directory were obtained with
`run_experiment.py`, which also records source identity and every attempt.

The original paper's historical September 9 measurements are retained in their
original files. This directory provides a separate repeated experiment.
