# Ten sequential collaboration reliability checks

All **10 runs / 20 trials** passed every recorded correctness check. Across both client counts, the concurrent phases measured **22,800 requests**, inserted **28,800 characters**, and replayed **3,360 batches**. There were zero unexpected errors, lost characters, and duplicate characters.

These are local collaborative-editing measurements using the real project routes and SQLite with synthetic identities. They do not measure model capability, mathematical proving performance, or production capacity.

## Aggregate timings

Each cell reports **median [minimum, maximum] across ten runs**. Edit/read columns aggregate each run's reported p95 latency; they are **not p95 values pooled across requests**. All ten runs are included.

| Clients | Edit p95 (ms) | Read p95 (ms) | Concurrent-phase duration (s) |
| ---: | ---: | ---: | ---: |
| 8 | 136.955 [114.75, 138.56] | 19.27 [11.57, 23.84] | 5.1065 [4.794, 5.302] |
| 16 | 548.255 [546.02, 643.26] | 47.01 [37.84, 74.91] | 20.9455 [20.427, 22.715] |

## Suggested paper paragraph

> We repeated the isolated collaboration check ten times sequentially, running 8 clients followed by 16 clients for 40 synchronized rounds in every repeat. All 20 trials passed the recorded correctness checks, with zero unexpected errors, lost characters, or duplicate characters across 22,800 measured requests. Median per-run edit p95 was 136.955 ms with 8 clients and 548.255 ms with 16 clients. The table reports medians and observed ranges across all ten repeats, with no warm-up exclusion. These values describe one host's local performance during the experiment; the correctness checks validate only the exercised scenarios. The measurements exclude production authentication, browser interaction, external networks, and model execution. Per-run p95 values are summarized without pooling request timings.

The companion `results-table.tex` contains the replacement table body. Its caption should state: *Median [minimum, maximum] across ten sequential repeats per client count; latency statistics summarize per-run p95 values. All 20 trials passed.*

## Protocol and timing

Ten invocations run sequentially. Within every invocation the script runs **8 clients, then 16 clients**, with 40 synchronized rounds at each count. No warm-up run is excluded, and the condition order is fixed rather than randomized. Each invocation creates fresh disposable storage; the two client counts use separate databases.

Each client inserts the three-character sequence `∑🙂x` per round, reads the shared state, replays every third insertion batch (rounds 0, 3, …, 39), and deletes the original anchor in round 0. Subsequent insertions continue to reference the deleted anchor.

| Clients | Measured requests/trial | Edit requests | Read requests | Inserted characters | Replayed batches |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 760 | 440 | 320 | 960 | 112 |
| 16 | 1520 | 880 | 640 | 1920 | 224 |

Request latency includes a new loopback HTTP connection, request/response processing, JSON decoding, and connection close. Trial duration covers concurrent edit/read rounds; setup and later correctness, rejection, and restart checks are excluded.

Median, minimum, and maximum across ten per-run reported p95 values or durations. Request p95 values are nearest-rank quantiles rounded to 0.01 ms by the original script; durations are rounded to 0.001 s. These are not pooled request percentiles. The saved reports contain no request-level timing arrays, so a pooled p95 cannot be recomputed from them.

The original check also verifies Unicode/deleted-anchor behavior, content idempotence on replay, convergence after writes settle, rollback of invalid batches, identifier-collision rejection, nonmember and removed-member rejection, revoked invitations, persistence across process restart, and SQLite integrity. All ten named checks are true in every trial.

## Every trial

| Run | Clients | Edit p95 (ms) | Read p95 (ms) | Concurrent-phase duration (s) | Checks |
| ---: | ---: | ---: | ---: | ---: | --- |
| [01](run-01.json) | 8 | 136.77 | 21.86 | 5.14 | Pass |
| [01](run-01.json) | 16 | 548.32 | 63.23 | 20.427 | Pass |
| [02](run-02.json) | 8 | 138.56 | 20.2 | 5.302 | Pass |
| [02](run-02.json) | 16 | 555.81 | 43.84 | 22.715 | Pass |
| [03](run-03.json) | 8 | 115.86 | 21.43 | 4.794 | Pass |
| [03](run-03.json) | 16 | 548.7 | 74.91 | 20.86 | Pass |
| [04](run-04.json) | 8 | 137.4 | 15.25 | 5.197 | Pass |
| [04](run-04.json) | 16 | 546.87 | 63.65 | 20.8 | Pass |
| [05](run-05.json) | 8 | 119.26 | 11.57 | 4.923 | Pass |
| [05](run-05.json) | 16 | 548.19 | 45.08 | 20.577 | Pass |
| [06](run-06.json) | 8 | 115.69 | 16.8 | 4.874 | Pass |
| [06](run-06.json) | 16 | 643.26 | 37.84 | 22.047 | Pass |
| [07](run-07.json) | 8 | 137.14 | 12.79 | 5.091 | Pass |
| [07](run-07.json) | 16 | 547.07 | 43.75 | 20.999 | Pass |
| [08](run-08.json) | 8 | 114.75 | 23.61 | 4.828 | Pass |
| [08](run-08.json) | 16 | 549.12 | 47.7 | 20.892 | Pass |
| [09](run-09.json) | 8 | 137.67 | 23.84 | 5.166 | Pass |
| [09](run-09.json) | 16 | 546.26 | 65.3 | 21.006 | Pass |
| [10](run-10.json) | 8 | 137.85 | 18.34 | 5.122 | Pass |
| [10](run-10.json) | 16 | 546.02 | 46.32 | 21.697 | Pass |

## Machine and source identity

All runs report the same environment: **Linux x86_64**, **8 logical CPUs**, **Python 3.13.15**, and **SQLite 3.53.1**. CPU model, memory capacity, scheduler activity, and background workload are not measured by these raw reports.

The historical report is [`docs/public/reliability/2026-09-09.json`](../../../../docs/public/reliability/2026-09-09.json), recorded at `2026-09-09T19:54:11.614016+00:00`. Historical environment matches these runs: **no**.

Recorded environment differences: `logical_cpus`: historical **2**, current **8**.

| Tested source | SHA-256 in these runs | Matches historical report | Matches current file |
| --- | --- | --- | --- |
| `agent_monitor/projects.py` | `187addf2deca5a0ecf9381f9e5a36bfc7eb34df938165f4f1f9db6ce992dce6a` | Yes | Yes |
| `scripts/check_collaboration_reliability.py` | `fc0a71073e57eb3d0fcc0b034a150cc118fd6d32ff862b6c5d15a9e6aafc9610` | Yes | Yes |

The historical timings are not included in the ten-run aggregates. Matching source hashes make the tested implementation identifiable; they do not establish equivalent host load or explain timing differences.

## Limits

- Ten repeated local runs on one host provide a small descriptive sample. The reported ranges are observed variation, not confidence intervals.
- Latencies are a snapshot of this host's performance during these runs. Passing correctness checks validates the exercised scenarios, rather than guaranteeing future throughput, latency, or all possible concurrent behavior.
- Raw reports retain latency summaries but no request-level timing arrays; they cannot support recomputing a pooled request p95.
- Fixed 8-then-16 ordering may introduce order effects. Fresh databases do not eliminate host, filesystem, or process-cache effects.
- Synthetic authentication and a loopback HTTP server with an enlarged connection backlog exclude production login, TLS, proxying, browser typing, network latency, and agent execution.
- Replaying acknowledged batches tests retry content idempotence; the check does not simulate a lost HTTP response.
- Restart testing kills the isolated server after acknowledged commits. It does not test power failure, disk loss, or interruption during a commit.
- Workload size is 40 rounds with small documents. The experiment does not establish behavior near document limits or under sustained production contention.
- Raw reports contain run-start timestamps, not run-end timestamps. The summarizer checks the runner manifest's nonoverlapping start/end intervals, each report's start within its interval, and fixed trial order.

## Reproduce the summary

From this directory, after all ten raw reports exist:

```bash
python3 summarize.py
```

Use `python3 summarize.py --replace` to regenerate the four derived files. The script validates every raw report before writing outputs; it never executes a reliability experiment. `summary.json` preserves report hashes, source identity, aggregation rules, and totals. `trials.csv` contains all 20 rows. `results-table.tex` is a standalone LaTeX `tabular` snippet; its values are median [minimum, maximum] across runs.
