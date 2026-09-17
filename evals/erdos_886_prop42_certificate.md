# Independent certificate for the Erdős–Rosenfeld Proposition 4.2 audit

Audit date: 2026-08-22 UTC.

This certificate concerns only the printed auxiliary construction in Erdős–Rosenfeld,
*The factor-difference set of integers*, Acta Arith. 79 (1997), 353–359,
doi:10.4064/aa-79-4-353-359. It is not a solution of Erdős Problem #886.

Let

\[
N_a=\prod_{j=0}^7(a+j),\qquad D_4=16a^2+112a+120.
\]

Exact low-to-high coefficient multiplication gives

\[
D_4^4-16^4N_a=
4096(50625+108360a+82512a^2+30128a^3+6072a^4+672a^5+32a^6).
\]

Every displayed coefficient is positive. Thus the printed claim
\(D_4\le16N_a^{1/4}\) fails for every positive integer \(a\). At the stated
boundary \(a=5\),

\[
N_5=19{,}958{,}400,\quad D_4=1080,\quad
D_4^4-16^4N_5=52{,}495{,}257{,}600>0.
\]

This does not invalidate the weaker four-larger-divisor window: that claim concerns
the one-sided distance \(A_i-\sqrt{N_a}\), not the full factor difference \(A_i-B_i\).

## Separate minimum-difference boundary defect

Trial division gives

\[
N_5=2^7 3^4 5^2\cdot7\cdot11,
\]

so \(N_5\) has 480 divisors and 240 unordered factor pairs. Exhaustive enumeration
of all 240 pairs finds the final ten pairs below the square root to be

\[
\begin{array}{r|r|r}
B&A&A-B\\\hline
3850&5184&1334\\
3960&5040&1080\\
4032&4950&918\\
4050&4928&878\\
4158&4800&642\\
4200&4752&552\\
4224&4725&501\\
4320&4620&300\\
4400&4536&136\\
4455&4480&25
\end{array}
\]

The largest divisor below \(\sqrt{N_5}\) is 4455, paired with 4480. Hence
\(d_0(N_5)=25\), not \(16\cdot5+56=136\).

For \(a\ge6\), put

\[
A_1=(a+1)(a+2)(a+4)(a+7),\quad
B_1=a(a+3)(a+5)(a+6),
\]

so \(A_1B_1=N_a\) and \(A_1-B_1=16a+56=:D_1\). Exact expansion yields

\[
2(A_1+B_1)-1-D_1^2
=4a^4+56a^3-4a^2-1400a-3025.
\]

With \(b=a-6\), the right-hand side is

\[
4b^4+152b^3+1868b^2+8056b+5711>0.
\]

If another factor pair had a smaller nonnegative factor difference, its integer
factor sum would be at most \(A_1+B_1-1\). The difference-of-squares identity
\(s^2-D^2=4N_a\) would then force its squared factor difference to be at most
\(D_1^2-(2(A_1+B_1)-1)<0\), a contradiction. Therefore
\(d_0(N_a)=16a+56\) for every integer \(a\ge6\), while \(a=5\) is the exact
boundary failure above.

## Reproduction code

The following standard-library calculation generated the polynomial and complete
factor-pair certificate; no floating-point arithmetic is used.

```python
from math import isqrt

def conv(xs, ys):
    out = [0] * (len(xs) + len(ys) - 1)
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            out[i + j] += x * y
    return out

N = [1]
for j in range(8):
    N = conv(N, [j, 1])
D = [120, 112, 16]
D4 = conv(conv(D, D), conv(D, D))
delta = [D4[i] - (16**4 * N[i] if i < len(N) else 0)
         for i in range(len(D4))]
assert delta == [4096*x for x in
                 [50625, 108360, 82512, 30128, 6072, 672, 32, 0, 0]]

n5 = 1
for x in range(5, 13):
    n5 *= x
pairs = [(d, n5 // d, n5 // d - d)
         for d in range(1, isqrt(n5) + 1) if n5 % d == 0]
assert n5 == 19_958_400
assert len(pairs) == 240
assert min(pairs, key=lambda row: row[2]) == (4455, 4480, 25)
```
