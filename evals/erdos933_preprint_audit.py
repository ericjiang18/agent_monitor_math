#!/usr/bin/env python3
"""Finite consistency audit for Zenodo 18652007 (not a proof of #933)."""

from __future__ import annotations

import argparse
import math
from fractions import Fraction


def smooth_part_23_of_consecutive_product(n: int) -> tuple[int, int, int]:
    left, right, smooth = n, n + 1, 1
    for prime in (2, 3):
        while left % prime == 0:
            left //= prime
            smooth *= prime
        while right % prime == 0:
            right //= prime
            smooth *= prime
    return smooth, left, right


def valuation(value: int, prime: int) -> int:
    exponent = 0
    while value % prime == 0:
        value //= prime
        exponent += 1
    return exponent


def log_fraction_bounds(value: Fraction, terms: int = 40) -> tuple[Fraction, Fraction]:
    # Exact rational lower/upper bounds for log(value), for 1 <= value <= 2.
    # With z=(value-1)/(value+1), use
    #   log(value) = 2 * sum_{j>=0} z^(2j+1)/(2j+1).
    # All terms are nonnegative and the omitted tail is at most
    #   2*z^(2*terms+1) / ((2*terms+1)*(1-z^2)).
    if not Fraction(1) <= value <= Fraction(2):
        raise ValueError("value must lie in [1, 2]")
    z = (value - 1) / (value + 1)
    z_squared = z * z
    power = z
    partial = Fraction(0)
    for index in range(terms):
        partial += power / (2 * index + 1)
        power *= z_squared
    lower = 2 * partial
    remainder = 2 * power / ((2 * terms + 1) * (1 - z_squared))
    return lower, lower + remainder


LOG2_LOWER, LOG2_UPPER = log_fraction_bounds(Fraction(2))


def log_integer_bounds(value: int) -> tuple[Fraction, Fraction]:
    # Exact rational bounds after reducing value to [1,2) by powers of 2.
    exponent = value.bit_length() - 1
    reduced = Fraction(value, 1 << exponent)
    reduced_lower, reduced_upper = log_fraction_bounds(reduced)
    return (
        exponent * LOG2_LOWER + reduced_lower,
        exponent * LOG2_UPPER + reduced_upper,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10_000_000)
    args = parser.parse_args()
    if args.limit < 2:
        raise SystemExit("--limit must be at least 2")

    claimed = 3.0 / math.log(2.0)
    maximum = -1.0
    maximizers: list[int] = []
    exact_maximizers: list[int] = []
    exact_strict_case_values: list[int] = []
    exact_nontrivial_cases = 0
    exact_strict_cases = 0
    for n in range(2, args.limit + 1):
        smooth, _, _ = smooth_part_23_of_consecutive_product(n)
        ratio = smooth / (n * math.log(n))
        if ratio > maximum + 1e-14:
            maximum = ratio
            maximizers = [n]
        elif abs(ratio - maximum) <= 1e-14:
            maximizers.append(n)

        # To compare R(n) with 3/log(2), cross-multiply positive logarithms:
        #     smooth*log(2) <= 3*n*log(n).
        # If smooth <= 3*n this follows immediately from log(2) <= log(n),
        # with equality only at n=2. Only the remaining finite cases need
        # interval bounds; every endpoint below is an exact Fraction.
        if smooth < 3 * n:
            continue
        if smooth == 3 * n:
            # Equality also needs log(n)=log(2), hence n=2.
            if n == 2:
                exact_maximizers.append(n)
            continue
        exact_nontrivial_cases += 1
        exponent = n.bit_length() - 1
        if n == 2**exponent and smooth == 3 * exponent * n:
            # Both sides are exactly exponent*3*n*log(2).
            exact_maximizers.append(n)
            continue
        log_n_lower, _ = log_integer_bounds(n)
        exact_margin = 3 * n * log_n_lower - smooth * LOG2_UPPER
        assert exact_margin > 1
        exact_strict_case_values.append(n)
        exact_strict_cases += 1

    # Exact algebraic check of the paper's own third equality case:
    # n=512=2^9, v_3(513)=3, hence smooth=512*27 and
    # smooth/(n log n)=27/(9 log 2)=3/log 2.
    smooth_512, residual_left, residual_right = smooth_part_23_of_consecutive_product(512)
    assert (smooth_512, residual_left, residual_right) == (13_824, 1, 19)
    assert abs(smooth_512 / (512 * math.log(512)) - claimed) < 1e-14

    # n=14 is a concrete configuration omitted by the paper's purportedly
    # exhaustive pure-power/gcd-one cases: 14=2*7 and 15=3*5.
    assert smooth_part_23_of_consecutive_product(14) == (6, 7, 5)

    # The public counterexample is an exact mixed-cofactor configuration.
    counterexample = 1_487_503_359
    assert counterexample == 3**14 * 311
    assert counterexample + 1 == 2**15 * 45_395
    assert math.gcd(311 * 45_395, 6) == 1
    counter_smooth, counter_left, counter_right = (
        smooth_part_23_of_consecutive_product(counterexample)
    )
    assert (counter_smooth, counter_left, counter_right) == (
        2**15 * 3**14,
        311,
        45_395,
    )
    # A human-auditable strict comparison that avoids trusting decimals:
    # n < 2^31 and 3*(311*31) < 2^15 imply R(n) > 3/log(2).
    assert counterexample < 2**31
    assert 3 * 311 * 31 < 2**15

    # Page 5, section 4.4.2 of the checksum-identified PDF states, for even x,
    # v_3(2^x-1)=v_3(x). Its first boundary x=2 already refutes this formula:
    # v_3(3)=1 whereas v_3(2)=0. The standard correct identity is
    # v_3(2^x-1)=1+v_3(x) for even positive x.
    assert valuation(2**2 - 1, 3) == 1
    assert valuation(2, 3) == 0
    for exponent in range(2, 100, 2):
        assert valuation(2**exponent - 1, 3) == 1 + valuation(exponent, 3)

    print(f"limit={args.limit}")
    print(f"maximum={maximum:.15f}")
    print(f"3/log(2)={claimed:.15f}")
    print(f"maximizers={maximizers}")
    print(f"exact_finite_maximizers={exact_maximizers}")
    print(
        "exact_log_certificate="
        f"nontrivial_cases:{exact_nontrivial_cases},"
        f"strict_cases:{exact_strict_cases},series_terms:40"
    )
    print(f"exact_strict_cases={exact_strict_case_values}")
    print("exact_strict_margin_lower_bound=1")
    print("uncovered_example=n=14, residuals=(7,5), smooth_part=6")
    print(
        "universal_bound_counterexample="
        "n=1487503359=3^14*311, n+1=2^15*45395"
    )
    print("page5_lte_boundary=x=2, v3(2^x-1)=1, claimed_v3(x)=0")
    print("scope=finite consistency check only; no limsup conclusion")


if __name__ == "__main__":
    main()
