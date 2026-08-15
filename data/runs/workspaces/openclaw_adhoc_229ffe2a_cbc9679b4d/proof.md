# Claim

In the natural numbers, with numerals and addition defined recursively below,

$$1+1=2.$$

**Status:** PROVED.

# Strategy

Use a direct definitional computation from the recursive definition of addition.

# Setup and notation

Let $0$ be the initial natural number and $S$ the successor operation. Define the numerals

$$1:=S(0), \qquad 2:=S(1).$$

Define addition recursively in its second argument by

$$a+0:=a, \qquad a+S(b):=S(a+b)$$

for all natural numbers $a,b$.

# Lemmas

## Lemma 1 (zero clause)

$1+0=1$.

**Proof.** Substitute $a=1$ into the defining equation $a+0:=a$. This is **self-derived** from the definition of addition. $\square$

# Main proof

Because $1=S(0)$, the successor clause in the definition of addition, with $a=1$ and $b=0$, gives

$$1+1=1+S(0)=S(1+0).$$

By Lemma 1,

$$S(1+0)=S(1).$$

Finally, $2:=S(1)$ by definition. Therefore

$$1+1=2.$$

Thus the claim is proved. $\square$

# Boundary cases and verification

The only recursive base case used is $1+0=1$, established in Lemma 1. Every term is a natural number; there are no denominators, roots, limits, or hidden side conditions. The proof uses only the two defining clauses for addition and the definitions of $1$ and $2$. It does not assume the target equality.

# Audit summary

Two independent checks were applied:

1. **Quantifier/definition audit:** the claim is a closed equality, and each rewrite is an instance of a displayed definition with its parameters specified.
2. **Circularity/edge-case audit:** the derivation bottoms out at the zero clause after one successor step; it neither invokes $1+1=2$ nor omits a recursive base case.

Both audits pass.

## References

No external results are used; every inferential step is definitional or **self-derived** above.
