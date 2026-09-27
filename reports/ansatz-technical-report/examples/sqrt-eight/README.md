# Solved example: irrationality of the real square root of eight

This evidence bundle preserves the formal source of an actual saved application
run, `kimi_adhoc_026cb7ed_a1118f831d`, and independently checks it. The registered
engine is Kimi; the stored model label is `kimi-k3`. No new model call was made.

The original run began on **15 September 2026 at 22:22:49 UTC** and its last run
update was at **22:26:27 UTC**. The saved verification record is dated
**15 September 2026 at 22:29:50 UTC** and reports a successful Lean check, no
detected admissions, and an affirmative statement-fidelity audit. The source hash
in that record matches `Proof.lean` exactly.

The independent local check on **20 September 2026** compiled the exact source
with Lean **4.14.0**, commit prefix `410fab728470`. Its axiom report is
`[propext, Classical.choice, Quot.sound]`; the proof is **not axiom-free**. The
output contains no `sorryAx`. The check uses already installed Mathlib libraries;
it does not rebuild or independently certify the toolchain or the imported
library artifacts.

## Mathematical statement and complete human proof

**Theorem.** The real number $\sqrt{8}$ is irrational.

**Proof.** An integer with an even square is even: the square of an odd integer
$2k+1$ is $2(2k^2+2k)+1$, which is odd. Suppose that $\sqrt{8}=p/q$ for positive
coprime integers $p,q$. Then $p^2=8q^2$, so $p$ is even; write $p=2a$. Substitution
gives $a^2=2q^2$, so $a$ is even; write $a=2b$. Substitution now gives
$q^2=2b^2$, so $q$ is even. Hence $p$ and $q$ share the factor $2$, contradicting
coprimality. Therefore $\sqrt{8}$ is irrational. $\square$

This is a concise editorial restatement of the parity proof in the saved
`proof.md`. The stored informal output includes additional lemmas about the real
square root and ends with a “Partial Progress” label. That label is not used as a
correctness criterion here: the mathematical proof above is complete, and the
checked Lean source proves the exact standard real-number statement.

The formal source follows a shorter route: it proves
$\sqrt{8}=2\sqrt{2}$ and uses Mathlib's `irrational_sqrt_two`. It therefore
demonstrates reuse of an established theorem, rather than formalization of each
step of the informal parity argument. This single elementary example establishes
neither new mathematical discovery nor a success rate or engine ranking.

## Files and reproduction

- `Proof.lean`: byte-for-byte copy of the saved formal source.
- `AxiomAudit.lean`: that exact source followed by `#check main` and
  `#print axioms main`.
- `provenance.json`: selected historical metadata and original-artifact hashes;
  no account identities, credentials, or conversation logs are copied.
- `fresh-check.json`: independent compilation and axiom-check output.
- `check.py`: standard-library-only reproduction script; makes no model calls or
  network requests and does not install dependencies.
- `report-excerpt.md`: report-ready subsection.

From the repository root, point the script to an existing prebuilt Mathlib project
compatible with Lean 4.14.0:

```bash
python3 reports/ansatz-technical-report/examples/sqrt-eight/check.py \
  --mathlib-project /path/to/prebuilt/mathlib-project \
  --output /tmp/sqrt-eight-check-new.json
```

The project used for the retained check was
`/home/ubuntu/.cache/agent-monitor/mathlib-v4.14.0`. Its Mathlib source revision was
`4bbdccd9c5f862bf90ff12f0a9e2c8be032b9a84`. This revision identifies the local source
checkout, not an independent rebuild of its precompiled libraries. Running the
script does not modify the original application run.
