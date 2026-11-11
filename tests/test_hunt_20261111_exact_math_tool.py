"""Fixes from the 2026-11-11 bug hunt of the trusted exact-math tool: every
big-integer operation is priced from its operand sizes before it runs, so one
Library "Try tool" request cannot hold the console's interpreter lock for
seconds or hours, while the documented limits keep working."""

from __future__ import annotations

import time
import unittest

from agent_monitor.proof_tools import (
    ToolInputError,
    bounded_counterexample_search,
    evaluate_exact,
    exact_math_certificate,
)


class ExactArithmeticWorkBudgetTest(unittest.TestCase):
    def test_a_powmod_with_large_exponent_and_modulus_is_refused_before_it_runs(self) -> None:
        for expression in (
            # about a second before; scaled up, the same shape runs for hours
            "powmod(3, (2**10000)**2 - 1, 2**4096 - 159) > 0",
            # a small exponent against a huge modulus: seconds of reduction
            "powmod(3, 2**64 - 1, (3**10000)**6 + 2) > 0",
        ):
            with self.subTest(expression=expression):
                started = time.monotonic()
                with self.assertRaisesRegex(ToolInputError, "work limit"):
                    evaluate_exact(expression)
                self.assertLess(time.monotonic() - started, 0.5)

    def test_many_moderate_operations_in_one_call_share_one_work_limit(self) -> None:
        # Each expression alone is allowed (about 0.2 s); together they are not.
        expression = "powmod(3, 2**4000 - 1, 2**4096 - 159) > 0"
        self.assertTrue(evaluate_exact(expression))
        with self.assertRaisesRegex(ToolInputError, "work limit"):
            exact_math_certificate({"expressions": [expression] * 6})

    def test_a_negative_power_of_a_huge_rational_is_refused_before_it_runs(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(ToolInputError, "size limit"):
            evaluate_exact("((3**10000)**15 / 1) ** -10000 > 0")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_valuation_of_a_large_power_is_exact_and_fast(self) -> None:
        started = time.monotonic()
        self.assertTrue(evaluate_exact("valuation((2**10000)**20, 2) == 200000"))
        self.assertTrue(evaluate_exact("valuation((3**10000)**10 * 10, 3) == 100000"))
        self.assertLess(time.monotonic() - started, 1.0)
        for expression, expected in (
            ("valuation(48, 2)", 4),
            ("valuation(-81, 3)", 4),
            ("valuation(7, 2)", 0),
            ("valuation(2**7 * 3, 2)", 7),
            ("valuation(12**5 * 7, 12)", 5),
            ("valuation(10**40, 10)", 40),
            ("valuation(10**40, 100)", 20),
            ("valuation(5**63 * 2, 5)", 63),
            ("valuation(5**64, 5)", 64),
            ("valuation(6, 7**50)", 0),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(evaluate_exact(expression), expected)

    def test_the_documented_limits_still_evaluate(self) -> None:
        # Each of these reaches a documented limit (factorial 5000, binomial
        # 100000, 250000-bit integers, exponent 10000) in one call of its own.
        for expression in (
            "factorial(5000) > 0",
            "binomial(100000, 50000) > 0",
            "binomial(100000, 2) == 4999950000",
            "(2**10000)**24 > (3**10000)**15",
            "(2**10000 + 1) ** 24 > 0",
            "powmod(2, 10**18, 10**9 + 7) == powmod(2, 10**18 % (10**9 + 6), 10**9 + 7)",
            "powmod(3, 2**2048 - 1, 2**2048 - 159) >= 0",
            "gcd((3**10000)**15, (3**10000)**14 * 2) == (3**10000)**14",
            "is_square((3**10000)**14)",
            "valuation(factorial(5000), 2) == 4995",
            "((2**10000)**10 + 1) / (3**10000)**6 > 1",
        ):
            with self.subTest(expression=expression):
                self.assertEqual(exact_math_certificate({"expressions": [expression]})["status"], "passed")
        result = exact_math_certificate(
            {"expressions": ["1/3 + 1/6"] + ["4455*4480 == 19958400", "binomial(12, 4) == 495"] * 24 + ["2**64 > 0"]}
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["results"][0]["value"]["exact"], "1/2")

    def test_a_counterexample_search_case_has_the_same_work_limit(self) -> None:
        with self.assertRaisesRegex(ToolInputError, "work limit"):
            bounded_counterexample_search(
                {
                    "predicate": "powmod(3, (2**10000)**2 - n, 2**4096 - 159) > 0",
                    "ranges": [{"name": "n", "start": 1, "end": 2}],
                }
            )
        result = bounded_counterexample_search(
            {
                "predicate": "powmod(2, n - 1, n) == 1 or not is_prime(n)",
                "ranges": [{"name": "n", "start": 3, "end": 2000}],
            }
        )
        self.assertEqual(result["status"], "no_counterexample_in_scope")


class ExactResultSerialisationTest(unittest.TestCase):
    def test_a_result_too_long_to_print_is_refused_with_a_clear_message(self) -> None:
        for expression in ("factorial(5000)", "7**10000", "(2**10000)**2 / 3", "1 / (3**10000)"):
            with self.subTest(expression=expression):
                with self.assertRaises(ToolInputError) as caught:
                    exact_math_certificate({"expressions": ["1 + 1", expression]})
                message = str(caught.exception)
                self.assertIn("expressions[1]", message)
                self.assertIn("decimal digits", message)
                self.assertNotIn("set_int_max_str_digits", message)

    def test_large_results_within_the_printable_size_are_returned_exactly(self) -> None:
        result = exact_math_certificate(
            {"expressions": ["10**4299", "factorial(5000) > 0", "(3**10000) % (10**4000 + 1)", "1 / 10**4299"]}
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["results"][0]["value"], 10**4299)
        self.assertEqual(result["results"][3]["value"]["denominator"], 10**4299)
        self.assertEqual(len(result["certificate_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
