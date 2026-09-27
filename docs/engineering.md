---
title: Engineering case study
description: How Ansätze preserves concurrent research edits, with a reproducible HTTP reliability check.
---

# Preserving edits when researchers work together

Two researchers can start from the same problem statement, type at the same time, and save in the opposite order. A whole-document save would let the second request erase the first person's work. A retry after a dropped response could also insert the same text twice.

**Ansätze stores character operations and applies each edit batch inside a SQLite transaction.** Stable character IDs preserve independent edits, while replaying an insertion with the same ID leaves the text unchanged. This page explains the decision and provides executable evidence of its behavior under concurrent requests.

## The engineering decision

The editor sends insertions and deletions instead of replacing the problem statement. Each insertion identifies its character, its value, and the character it follows. Deletion marks a character as deleted but retains it as an anchor for edits from clients that have not seen the deletion yet.

For example, Alice and Bob both see `ab`. Alice inserts `X` after `a`; Bob inserts `Y` after `a`. If Alice's request commits first, the stored ordering renders `aYXb`. Both insertions survive. If Alice subsequently deletes the original `a`, the text becomes `YXb`; a stale client can still insert after that deleted anchor.

| Approach considered | Consequence |
| --- | --- |
| Save the entire document | Small implementation, but stale saves can overwrite another person's edits. |
| Reject saves with an old revision | Detects conflicts, but requires people or client code to resolve them during ordinary typing. |
| Character operations with one authoritative server | Preserves independently identified edits and supports safe retries, at the cost of more metadata and serialized writes. |

The third approach fits the current single-server, plain-text research workspace. It is a server-ordered, RGA-like sequence: concurrent insertions at the same anchor are ordered by the server's insertion sequence. This is not a claim of arbitrary offline or multi-server convergence, and it cannot resolve disagreements about the mathematical meaning of an edit.

## Where reliability comes from

An edit transaction acquires SQLite's write reservation using `BEGIN IMMEDIATE`, checks membership, loads the current document, validates and applies the operations, updates the revision, and commits before the HTTP success response is sent. Another writer must wait rather than compute a replacement from the same stale database state.

Four details matter:

- **Retries preserve text.** Replaying an insertion with the same ID, value, and anchor does not duplicate it. Reusing the ID with different content is rejected. A replay still increments the document revision; idempotence here refers to document content.
- **Failed batches roll back.** If the final operation is invalid, earlier operations in that batch are not partially saved.
- **Deleted anchors remain addressable.** Tombstones preserve the position referenced by an older client's pending insertion.
- **Pending browser edits stay local until acknowledged.** The browser queues operations, retries failed batches, and avoids replacing its text with a polled snapshot while edits are pending. It uses code points for operations and converts selection positions to the browser's UTF-16 offsets.

The browser sends batches of up to 2,000 operations and polls every 1.5 seconds. This supplies shared text and member presence. It does not yet provide rich-text editing, shared cursors, or a persistent offline queue that survives closing the tab.

## Measured concurrent use

The downloadable report below records a check performed on September 9, 2026. It drives the actual project route implementation and SQLite storage through a temporary HTTP server, using synthetic identities and isolated databases. It makes no model calls and sends no traffic to the production console.

| Concurrent clients | Measured requests | Inserted characters | Replayed batches | Edit p95 | Read p95 | Duration |
| --- | --- | --- | --- | --- | --- | --- |
| 8 | 760 | 960 | 112 | 115.29 ms | 47.06 ms | 4.43 s |
| 16 | 1,520 | 1,920 | 224 | 452.03 ms | 94.62 ms | 16.75 s |

**Both trials passed with zero unexpected errors, zero lost characters, and zero duplicate characters.** All convergence, rollback, access-revocation, process-restart, and database-integrity checks passed. The measured environment was Linux x86-64 with 2 logical CPUs, Python 3.13.15, and SQLite 3.53.1.

Every trial uses 40 synchronized rounds. Each client inserts a three-character Unicode sequence at the same original anchor, reads the shared state, and repeats every third insertion batch to simulate a retry. The clients also delete the original anchor and continue inserting against it.

After the writes settle, the check verifies every expected character ID exactly once and requires every client's fetched text, IDs, and revision to agree. It also checks invalid-batch rollback, ID collisions, unauthenticated access, nonmember edits, removed-member edits, and reuse of a revoked invitation.

Finally, it kills the isolated server process **after acknowledged commits**, starts a new process against the same database, and verifies the saved text, IDs, revision, membership, and revoked access. SQLite's integrity check must return `ok`.

### Reproduce and inspect

From the repository root, using the project's Python environment:

```bash
.venv/bin/python scripts/check_collaboration_reliability.py \
  --output /tmp/ansatz-reliability-new.json
```

Use a new report filename for each run. The script fails on an unexpected response or a violated invariant, and retains its temporary databases for inspection. It never points its test server at the production database.

- [Download the measured report](/reliability/2026-09-09.json), including environment details and SHA-256 hashes of the tested code.
- [Read the executable check](/reliability/check_collaboration_reliability.py).
- Inspect the [server implementation](/reliability/projects.py) and [browser editor](/reliability/projects.js).

### What the evidence does and does not establish

These are short, local concurrency checks, not a production capacity guarantee. The test server uses the real project routes with synthetic authentication and an enlarged connection backlog. It does not exercise production login, TLS, the reverse proxy, browser typing, internet latency, or agent execution. The request timings cover the concurrent edit/read phase and exclude setup, rejection checks, and restart time. Each concurrency level was measured once; timing varies with the host.

The restart check establishes persistence across a process kill after acknowledgement. It does not establish recovery from a power failure, disk loss, or a kill in the middle of a commit. The test does not simulate a lost response; it explicitly replays requests to check the behavior such a retry relies on.

## Costs and the next decision

SQLite serializes writers across projects in this database. Each edit currently reads, renders, and rewrites the document's full character metadata, and presence polling also performs a write. The document is bounded at 20,000 visible characters and 100,000 stored nodes, including deleted characters. These constraints keep the implementation manageable but make large documents and sustained contention important future measurements.

Before expanding the supported workload, the next useful checks are a long-duration mixed read/write test near those document limits, browser disconnect/reconnect tests, and restore drills from a separate backup. Operation-log storage and safe tombstone compaction would require a new decision about what old clients may still reference.

Deployment is another boundary: the public documentation is staged before an atomic directory exchange, with the previous release retained. That protects readers from a partly built static site. It is separate from database persistence and does not itself provide zero-downtime backend deployment or backup recovery.

This case study documents the implementation and its measured behavior. It does not assign individual authorship or claim that the workload represents real researcher usage.
