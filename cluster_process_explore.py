"""Monte Carlo for the mass-biased left-moving cluster process.

Initial condition: one unit particle at each site 1,...,n.  Reports total
jumps and the number of clusters crossing each edge.  A Fenwick tree makes
mass-biased sampling O(log n) per jump.
"""
from __future__ import annotations

import argparse
import math
import random
import statistics


class Fenwick:
    def __init__(self, n: int) -> None:
        self.n = n
        self.tree = [0] * (n + 1)

    def add(self, i: int, delta: int) -> None:
        while i <= self.n:
            self.tree[i] += delta
            i += i & -i

    def select(self, rank: int) -> int:
        """Site containing zero-based rank in the mass distribution."""
        i = 0
        bit = 1 << (self.n.bit_length() - 1)
        while bit:
            j = i + bit
            if j <= self.n and self.tree[j] <= rank:
                rank -= self.tree[j]
                i = j
            bit >>= 1
        return i + 1


def trial(n: int, rng: random.Random) -> tuple[int, list[int]]:
    masses = [0] + [1] * n
    bit = Fenwick(n)
    for i in range(1, n + 1):
        bit.add(i, 1)
    remaining = n
    jumps = 0
    crossings = [0] * n  # index x-1 is edge x -> x-1
    while remaining:
        x = bit.select(rng.randrange(remaining))
        mass = masses[x]
        masses[x] = 0
        bit.add(x, -mass)
        crossings[x - 1] += 1
        if x == 1:
            remaining -= mass
        else:
            masses[x - 1] += mass
            bit.add(x - 1, mass)
        jumps += 1
    assert jumps == sum(crossings)
    return jumps, crossings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("n", type=int)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    results = [trial(args.n, rng) for _ in range(args.trials)]
    jumps = [x[0] for x in results]
    mean = statistics.mean(jumps)
    print(f"n={args.n} trials={args.trials} mean={mean:.6g}")
    print(f"mean/(n log n)={mean/(args.n*math.log(args.n)):.6g}")
    print(f"mean/n^(3/2)={mean/(args.n**1.5):.6g}")
    selected = sorted({0, args.n // 4, args.n // 2, 3 * args.n // 4, args.n - 1})
    for edge in selected:
        cmean = statistics.mean(row[1][edge] for row in results)
        print(f"edge {edge + 1}->{edge}: mean crossings={cmean:.6g}")


if __name__ == "__main__":
    main()
