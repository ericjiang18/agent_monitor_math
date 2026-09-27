---
title: "Quickstart"
description: "A complete guide to Ansätze: start a proof, explore the engines and evidence views, and reuse work across runs."
---

# Quickstart

Ansätze brings mathematical questions, proving engines, and the evidence behind their results into one workspace. This guide covers your first run and the tools you can explore afterwards.

## 1. Open the console

Choose **[Use as guest](/guest)** to try the console without creating an account. For durable account history, create an account when registration is offered or use **[Sign in](/sign-in)** with your existing email and password.

A guest’s run history is tied to a 24-hour session cookie in that browser. Clearing cookies or site data, or moving to another browser or device, loses access to that guest history. Guest work is not transferred when you later sign in, so sign in before starting work you need to retain with an account. Ending the guest session deletes its saved provider key.

If you forget your password, choose **Forgot password?** on the sign-in page. Use only the newest reset email; a reset link is single-use.

## 2. Choose how runs are billed

Guest mode defaults to **Kimi K3** with the **Kimi proof harness**: draft an argument, critique it, then refine the result. If hosted Kimi access is available, you can start immediately. Otherwise, open **Settings** and add your own Kimi API key. The tool-free **Plain** baseline is also available when enabled by the deployment.

The default guest allowance is **10 total run starts** in the browser session, with up to six starts per hour. Deleting a run does not restore a start. **Sign in after the tenth start** to continue; a deployment may offer a lower allowance. Each Kimi stage has a default 4,096-token response limit. Key verification allows six attempts per guest per hour by default.

**Claude Code and Codex require sign-in**, even when guest starts remain. Their cards stay visible in the dashboard so you can see the available options.

Signed-in accounts can use the provider routes enabled by the deployment:

- Add your own provider API key.
- Connect a supported subscription account when that option is available.

Provider keys saved through the console are encrypted at rest in the associated user record. In a guest session, those settings remain tied to the same browser cookie and can become inaccessible if that cookie is lost. Use a signed-in account for durable provider settings. The console selects the credential route associated with the model you choose and limits the normal child-process environment to that route. Signed-in local proving engines share the host's operating-system trust boundary; they are not a sandbox for untrusted code.

## 3. Start a problem

1. Enter a mathematical statement, or open [Open Problems](/open-problems), read its statement and cited progress, and select **Click to solve**. This prepares the workspace; it does not start a run automatically.
2. Pick an available engine and model. Guests start with **Kimi proof harness · Kimi K3** selected.
3. Signed-in users can set the iteration budget and optional delegation choices.
4. Select **Prove →**.

The run opens in its workspace. Read the developing proof in **Sandbox**, inspect the **Conversation**, or open **Monitor** to follow the engine’s activity.

## 4. Review and continue

The Kimi harness saves its draft, critique, and refined proof. Review the argument together with any verification output and recorded gaps. **Lean Verification** shows formal source and checker results; **DAG** offers separate engine, informal, and formal views. Guest sessions can use the Lean and proof-DAG tools with Kimi K3.

Signed-in users can add focused feedback and continue the same proof run. A guest’s main proof run finishes after its selected workflow; the guest can still inspect its evidence and use the available Lean actions. A proposed proof, a compiled file, and a verified statement have different meanings—read the reported status and remaining gaps.

## Keep your account safe

- Do not paste API keys into problem statements or chat messages.
- Do not share password-reset links; the token grants temporary access to reset your password.
- Sign out on shared computers.
- Rotate a provider key immediately if you believe it has been exposed.

## Proving engines

Availability depends on the deployment and the account’s connected providers. Each run has its own workspace.

| Engine | Workflow |
| --- | --- |
| **Kimi proof harness** | Kimi K3 drafts, critiques, and refines an argument in three tool-free stages; the default guest workflow. |
| **UCLA** | Literature, advisor, solver, and verification stages. |
| **IMProof** | An author–critic proving workflow based on ProofStack. |
| **Hermes** | An embedded agent harness for reasoning and tools. |
| **DeepAgents** | A LangGraph agent with a dedicated runtime. |
| **Meta-Harness** | A solver, evaluator, and proposer loop. |
| **OpenClaw** | An embedded local agent. |
| **Codex, Claude Code / OpenClaude, OpenHands** | Coding-agent integrations when available. Claude Code and Codex require sign-in. |
| **Plain** | A single-model-call baseline. |

## Trace views and proof graphs

The monitor records agent activity, token usage, and cost as the run progresses:

- **Map view** shows a free-form graph of agent activity.
- **Pipeline view** groups that activity into the workflow’s stages.
- The workspace’s **DAG** tab separates the engine’s execution graph from the informal proof and formal Lean structure.
- **Lean Verification** records source, compilation or checking results, and any remaining proof gaps.

Use the graph to understand dependencies and the checker output to inspect formal evidence. An edge in a graph alone does not certify a mathematical argument.

## Reusable memory, skills, and tools

Signed-in workspaces can use a shared library to carry useful material into future runs:

- **Memory** keeps context and summaries from earlier work.
- **Skills** provide reusable strategies and instructions.
- **Tools** provide executable scripts to compatible engines.

Enabled library items are copied into a run’s `_library/` directory and included in its prompt context. A library is reusable context; a run’s workspace preserves that run’s artifacts and evidence.

## Project layout

For developers working with a local checkout:

```text
agent_monitor/          # Console, runners, and orchestration
monitor_core/           # Dashboard and token/cost tracking
engines/                # Embedded engines and their environments
problems/               # Local mathematical statements
data/                   # Runtime runs, logs, caches, and library
```

## Keep exploring

- Browse [Open Problems](/open-problems) for a sourced question to investigate.
- Explore the [research DAG](/dag) and inspect a [worked example](/example-run).
- Read [News](/news) for mathematical developments, model releases, and research updates.
- See [Engineering & reliability](/engineering) for implementation details.
