# IMProofBench (vendored)

Source: https://github.com/1stproof/batch-2/tree/main/batch-2-submissions/improofbench

This is the ProofStack / mathagents Author–Critic workflow used for FirstProof batch-2.

## Quick run (from this directory)

```bash
# from Agent_Monitor root, with uv:
cd engines/improof
uv sync   # or: uv pip install -e .

uv run python scripts/run_workflow.py \
  --workflow configs/workflows/author_critic.yaml \
  --problem "Prove that there are infinitely many primes."
```

Via Agent Monitor:

```bash
export IMPROOF_ENTRY="$(pwd)/engines/improof/scripts/run_workflow.py"
agent-monitor run improof problems/ucla/[First_Proof]Example_problem.txt
```

See upstream `README.md` for API keys and workflow presets.
