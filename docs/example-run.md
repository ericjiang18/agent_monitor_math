---
title: A worked research example
description: Explore Erdős–Straus partial results, exact computations, and a Lean dependency graph while keeping the general conjecture open.
---
<script setup>
import LeanShowcase from './.vitepress/theme/LeanShowcase.vue'
import { withBase } from 'vitepress'
</script>

# An open problem, with inspectable progress

The **Erdős–Straus conjecture** asks whether every integer n ≥ 2 admits positive integers x, y, z with **4/n = 1/x + 1/y + 1/z**. See [Erdős Problem #242](https://www.erdosproblems.com/242) for the problem and current references.

This is a **curated worked example**, not a captured multi-agent run or a claimed solution. Its Python computations and finite Lean proofs were actually executed. The planner, explorer, and reviewer roles show how collaborators can divide a research task; no model calls, token counts, or costs are fabricated.

<a :href="withBase('/example-run-viewer.html')">Open the interactive example run →</a>

## What the example contains

- **A partial algebraic result:** for n = 2m, choose denominators (m, 2m, 2m). This handles even n. The general identity is explained in the findings but is not formalized in this Lean file.
- **An exact search:** Python’s rational arithmetic checks positive witnesses for each n from 2 through 500.
- **Five Lean proofs:** concrete witnesses for n = 2, 3, 5, 7, 13 and their conjunction compile under Lean 4.14.0. The theorem `checked_cases` depends on no axioms.
- **An explicit gap:** none of those finite checks establishes the universal conjecture. The general goal remains disconnected from the certified finite-case DAG.

<LeanShowcase />

## Reproduce the evidence

From the repository root, with Lean available:

```sh
python3 scripts/build_erdos_example.py
```

This runs the exact-arithmetic search, checks `agent_monitor/examples/ErdosStraus.lean`, and regenerates the public example JSON. It uses no account data or model API calls.

## Work on a problem together

Open **Collaborative projects** from the proving console. Create a project, enter the problem and research notes, and use **Invite collaborators** to copy an editor invitation. Invitees sign in and explicitly accept it.

Members edit the same document; changes merge and sync automatically. The member list shows who is active. Each person can select an engine and model, assign an agent task, and launch a run using their own account configuration. Runs receive a fixed copy of the shared problem at launch and appear in **Project results** with their author and status.

Project members can inspect and continue project runs, including their artifacts and prompts. Invite only people who should have that access. The owner can revoke unused invitations or remove members; removing a member also invalidates the existing invite link. Account credentials remain private.

## Share a result without editing access

Inside a run, select **Share**. Review the sharing description, confirm that the selected snapshot may be shared, and choose **Create / update link**, then **Copy link**. The recipient can read it without signing in.

The snapshot includes the problem, proof text, available agent summaries, saved DAGs, and Lean source. It excludes account settings, chat history, attachments, and the rest of the workspace. It stays fixed until its owner updates it. **Revoke link** prevents future access, and deleting the source run also disables the share. Revocation cannot remove copies already saved by recipients.

Public run links are read-only. Project invitations grant collaborative editing access; they are separate controls.

For the editing design, tradeoffs, and reproducible concurrency results, read the [engineering case study](/engineering).
