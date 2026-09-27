### 6.4. A solved problem: irrationality of the square root of eight

To complement the finite progress above, an actual saved run asks for a complete
proof that $\sqrt{8}$ is irrational. Run
`kimi_adhoc_026cb7ed_a1118f831d`, registered under Kimi, was created on 15 September
2026. Its saved artifacts include an informal parity argument and a Lean proof of
the exact standard statement `Irrational (Real.sqrt 8)`.

**Theorem.** The real number $\sqrt{8}$ is irrational.

**Proof.** If an integer is odd, its square has the form
$(2k+1)^2=2(2k^2+2k)+1$ and is odd. Thus an integer with an even square is even.
Suppose $\sqrt{8}=p/q$ for positive coprime integers $p,q$. Then $p^2=8q^2$, so
$p=2a$ for an integer $a$. It follows that $a^2=2q^2$, so $a=2b$ for an integer
$b$. Substitution gives $q^2=2b^2$, so $q$ is even. Both $p$ and $q$ are therefore
divisible by $2$, contradicting coprimality. $\square$

The formal proof uses a different decomposition: it establishes
$\sqrt{8}=2\sqrt{2}$ and invokes Mathlib's `irrational_sqrt_two`. The saved
verification record from 15 September reports success with no detected admissions
and an affirmative fidelity audit. An independent check on **20 September 2026**
compiled the identical source with **Lean 4.14.0**. The recorded source hash
matches the copied file, and `#print axioms main` reports
`[propext, Classical.choice, Quot.sound]`, with no `sorryAx`. These are the
reported logical dependencies; the result is not axiom-free. The reproduction
uses the installed Mathlib libraries and does not independently rebuild them.

The saved informal output ends with a “Partial Progress” label, whereas its
argument and the checked formal theorem establish this target completely. The
example therefore also illustrates why outcome labels should be read alongside
the exact claim and checker output. It demonstrates a solved elementary task and
the reuse of a known theorem, without implying new discovery or a measured
success rate. The sanitized evidence bundle is in `examples/sqrt-eight/`.
