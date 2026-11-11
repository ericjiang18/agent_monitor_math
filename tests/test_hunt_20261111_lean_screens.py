"""Lean text screens (lean_verify._fidelity_flags): one pasted source must
not hold the console for minutes, and making the screens fast must not change
what they flag -- the cheat rules are checked against a verbatim copy of the
patterns as they stood before, over a corpus that exercises every rule."""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import lean_verify

ROOT = Path(__file__).resolve().parents[1]

# ── The cheat rules as they stood before the screens were made linear ──
# (a verbatim copy; the new code must flag exactly what these flag).
OLD_KW = (
    r"(?<![A-Za-z_.'«!?\u0391-\u03a9\u03b1-\u03fb\u1d62-\u1d6a"
    r"\u1f00-\u1ffe\u2080-\u209c\u2100-\u214f\U0001d49c-\U0001d59f])"
)
OLD_CORE_TYPES = r"(?:ℕ|ℤ|ℚ|ℝ|ℂ|Nat|Int|Rat|Real|Complex)\b"
OLD_CORE_CLASSES = (
    r"(LE|LT|Add|Mul|Sub|Div|Neg|Pow|Dvd|Mod|HAdd|HMul|HSub|HDiv|HPow|HMod|OfNat|"
    r"Preorder|PartialOrder|LinearOrder|Decidable|One|Zero|Inv|Abs|Max|Min|Ord|BEq|"
    r"DecidableEq|Semiring|Ring|Field|CommRing|NatCast|IntCast)"
)
OLD_CHEAT_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        OLD_KW + r"axiom\s+([^\s:({\[]+)",
        "axiom",
        "major",
        "declares `axiom {m}` — an unproved axiom makes anything reachable",
    ),
    (
        r"set_option\s+debug\.skipKernelTC\s+true",
        "skip_kernel_tc",
        "major",
        "`set_option debug.skipKernelTC true` turns off the kernel check",
    ),
    (
        OLD_KW + r"unsafe\b",
        "unsafe",
        "major",
        "`unsafe` declarations are not kernel-checked",
    ),
    (
        r"(?:@\[|" + OLD_KW + r"attribute\s*\[)\s*(?:[^\]]*,\s*)?(?:local\s+|scoped\s+)?implemented_by",
        "implemented_by",
        "major",
        "`@[implemented_by]` swaps in unverified code",
    ),
    (
        r"(?:@\[|" + OLD_KW + r"attribute\s*\[)\s*(?:[^\]]*,\s*)?extern\b",
        "extern",
        "major",
        "`@[extern]` binds a declaration to unverified native code",
    ),
    (
        OLD_KW + r"opaque\s+([^\s:({\[]+)",
        "opaque",
        "major",
        "`opaque {m}` asserts a value exists without defining it",
    ),
    (
        r"\bnative_decide\b|\bofReduceBool\b|\bLean\.ofReduceBool\b",
        "native_decide",
        "major",
        "`native_decide` trusts the compiler, not the kernel",
    ),
    (
        OLD_KW + r"(run_cmd|run_tac|run_elab|run_meta)(?![\w'])",
        "metaprogram",
        "major",
        "`{m}` runs a metaprogram inside the file; it can alter what compiles",
    ),
    (
        # `#` never continues an identifier, so these match wherever they
        # stand (`open Nat#exit` is `open Nat` followed by `#exit`).
        r"(#eval|#exit|#guard_msgs)(?![\w'!?])",
        "metaprogram",
        "major",
        "`{m}` runs a metaprogram, stops elaboration or hides Lean's own "
        "messages; it can alter what the checker sees",
    ),
    (
        OLD_KW + r"(elab|elab_rules|macro|macro_rules|syntax|declare_syntax_cat|binder_predicate)\b",
        "custom_syntax",
        "major",
        "`{m}` defines new syntax or elaboration; proofs may bypass the kernel's view",
    ),
    (
        OLD_KW + r"(initialize|builtin_initialize)\b",
        "initialize",
        "major",
        "`{m}` runs code when the file is loaded",
    ),
    (
        r"\badmitGoal\b|\bLean\.Elab\.admitGoal\b|\bsorryAx\b",
        "hidden_admit",
        "major",
        "a goal is admitted without a visible `sorry`",
    ),
    (
        r"\bLean\.(?:Elab|Meta|Parser|Environment|MonadEnv|Kernel|Compiler)\b|"
        r"\b(?:CommandElabM|TermElabM|TacticM|MetaM|CoreM|MonadEnv|modifyEnv|addDecl)\b",
        "meta_api",
        "major",
        "uses Lean's metaprogramming API; a proof file has no business changing the environment",
    ),
    (
        OLD_KW + r"instance\b[^:\n]*:\s*" + OLD_CORE_CLASSES + r"\s+" + OLD_CORE_TYPES,
        "core_instance",
        "major",
        "redefines the standard `{m}` instance on a built-in number type",
    ),
    (
        r"@\[[^\]]*\binstance\b[^\]]*\]\s*(?:(?:private|protected|noncomputable)\s+)*"
        r"(?:def|abbrev)\b[^:\n]*:\s*" + OLD_CORE_CLASSES + r"\s+" + OLD_CORE_TYPES,
        "core_instance",
        "major",
        "registers a replacement `{m}` instance on a built-in number type",
    ),
    (
        OLD_KW + r"attribute\s*\[[^\]]*\binstance\b",
        "instance_attribute",
        "minor",
        "`attribute [instance]` registers an instance — confirm it does not change a standard structure",
    ),
    (
        OLD_KW + r"(notation3?|infix|infixl|infixr|prefix|postfix)\b",
        "custom_notation",
        "minor",
        "custom `{m}` — confirm it does not shadow a standard symbol",
    ),
    (
        r"(?m)(?::|→)\s*True\s*(?::=|$)",
        "trivial_conclusion",
        "major",
        "a statement concludes `True`, which says nothing",
    ),
    (
        r"\(\s*[A-Za-z_][^:()]*:\s*False\s*\)",
        "false_hypothesis",
        "major",
        "a hypothesis is `False`, so the theorem is vacuous",
    ),
    (
        OLD_KW + r"(?:def|abbrev|inductive|structure)\s+"
        r"(Odd|Even|Prime|Irrational|Rational|Dvd|Divides|Divisible|Coprime|IsSquare"
        r"|Sqrt|Gcd|Lcm|Continuous|Differentiable|Deriv|Integral|Limit|Converges)\b",
        "handrolled_notion",
        "minor",
        "hand-rolled `{m}` — confirm it matches the standard notion",
    ),
)


# One snippet per rule (and near misses), placed at the start, the end, the
# middle, inside an attribute that is never closed and after one that is.
SNIPPETS = (
    "axiom foo : False", "axiom", "my_axiom x", "set_option debug.skipKernelTC true",
    "unsafe def f := 1", "@[implemented_by g] def f := 1", "@[simp, implemented_by g] def f",
    "@[ simp ,  local implemented_by g]", "attribute [scoped implemented_by g] f",
    "attribute[simp, implemented_by g] f", "@[simp] def implemented_by := 1",
    '@[extern "c_f"] opaque f : Nat', "@[simp,extern] def f", "@[externs] def f",
    "attribute [extern] f", "opaque c : Nat", "native_decide", "Lean.ofReduceBool",
    "run_cmd pure ()", "#eval 1", "#exit", "#guard_msgs in", "macro x : term => x",
    "syntax foo", "initialize foo", "admitGoal", "sorryAx", "Lean.Elab.Command",
    "CommandElabM", "instance : Add ℕ := ⟨fun a b => a⟩", "instance foo : Mul Nat := sorry",
    "instance : Add MyType := x", "instance\n : Add ℕ", "@[instance] def foo : Add ℕ := x",
    "@[reducible, instance] private noncomputable def foo : LE Nat := x",
    "@[instance]\nabbrev foo (n : Nat) : DecidableEq ℤ := x", "@[instance] def foo : Add ℕ\n",
    "@[simp] def foo : Add ℕ := x", "@[instance 100] theorem t : Add ℕ", "@[instance] def f",
    "attribute [instance] foo", "attribute [local instance] foo", "attribute [simp] instance",
    "notation \"x\" => 1", "infixl:65 \" +' \" => f", "theorem t : True := trivial",
    "theorem t : 1 = 1 → True", "theorem t (h : False) : 1 = 2", "theorem t ( x y : False )",
    "def Prime (n : Nat) := n > 1", "structure Continuous where", "def PrimeLike := 1",
)
TOKENS = (
    "@[", "@[ ", "attribute", "attribute [", "attribute[", "[", "]", ",", ", ", " ", "  ",
    "\n", "instance", "instance ", "extern", "implemented_by", "local ", "scoped ", "def",
    "def ", "abbrev ", "private ", "noncomputable ", "protected ", " x", "x", ":", " : ",
    "Add", " Add", "DecidableEq", " ℕ", " Nat", "Natural", "False", "(", ")", "(h ", "True",
    " := ", "axiom ", "foo", "simp", "_", "@[instance] ", "theorem t", "-- note\n",
    '"s"', "/- c -/", "«", "'", "1", ".", "@[simp, ", "my", "→",
)


def _corpus() -> list[str]:
    sources = ["", "\n", "@[", "]", "attribute [", "instance"]
    for snippet in SNIPPETS:
        sources += [
            snippet, snippet + "\n", "\n" + snippet, "theorem t : 1 = 1 := rfl\n" + snippet,
            snippet + "\ntheorem t : 1 = 1 := rfl", "@[simp " + snippet, "@[simp] " + snippet,
            "@[" * 3 + snippet + "]", "attribute [simp] foo\n" + snippet + "\n" + snippet,
            "@[simp, " + snippet + ", inline]", "x" + snippet, "." + snippet, "1" + snippet,
        ]
    rng = random.Random(20261111)
    for _ in range(3000):
        sources.append("".join(rng.choice(TOKENS) for _ in range(rng.randint(1, 24))))
    return sources


def _hits(found) -> list[tuple]:
    return [(hit.span(), hit.groups()) for hit in found]


class CheatScreenEquivalenceTest(unittest.TestCase):
    def test_the_cheat_rules_themselves_are_unchanged(self) -> None:
        self.assertEqual(lean_verify._CHEAT_RULES, OLD_CHEAT_RULES)
        rules = {pattern for pattern, *_ in OLD_CHEAT_RULES}
        self.assertEqual(len(lean_verify._CHEAT_RULE_SCANS), 5)
        self.assertLessEqual(set(lean_verify._CHEAT_RULE_SCANS), rules)

    def test_every_rule_finds_the_same_matches_as_the_original_pattern(self) -> None:
        corpus = _corpus()
        matched = set()
        for pattern, fid, *_ in OLD_CHEAT_RULES:
            for src in corpus:
                old = _hits(re.finditer(pattern, src))
                self.assertEqual(
                    _hits(lean_verify._cheat_rule_hits(pattern, src)), old, f"{fid}: {src!r}"
                )
                if old:
                    matched.add(pattern)
        # The corpus exercises every rule.
        self.assertEqual(matched, {pattern for pattern, *_ in OLD_CHEAT_RULES})

    def test_the_screens_flag_exactly_what_the_original_patterns_flag(self) -> None:
        corpus = _corpus()
        new = [lean_verify._fidelity_flags(src) for src in corpus]
        with (
            patch.object(lean_verify, "_CHEAT_RULES", OLD_CHEAT_RULES),
            patch.object(lean_verify, "_cheat_rule_hits", lambda pattern, src: re.finditer(pattern, src)),
        ):
            old = [lean_verify._fidelity_flags(src) for src in corpus]
        for src, before, after in zip(corpus, old, new):
            self.assertEqual(after, before, repr(src))
        self.assertTrue(any(flags for flags in new))


# Sources a guest may paste (the guest limit is 100,000 characters) that made
# the attribute and line rules rescan the same text from every opening.
SLOW_SOURCES = r"""
import json, sys, time
from agent_monitor import lean_verify
def fit(unit, n):
    return (unit * (n // len(unit) + 1))[:n]
sources = [
    "@[" * 6000 + " instance" * 2000,
    "@[" + " " * 30000 + "x" * 30000,
    fit("@[,", 60000),
    fit("attribute[", 60000),
    fit("instance ", 60000),
    fit("@[instance] def x ", 60000),
]
started = time.monotonic()
flags = [[flag["id"] for flag in lean_verify._fidelity_flags(src)] for src in sources]
print(json.dumps({"seconds": time.monotonic() - started, "flags": flags}))
"""


class CheatScreenSpeedTest(unittest.TestCase):
    def test_unclosed_attributes_and_long_lines_are_screened_in_linear_time(self) -> None:
        # In a child process: the old screens held the GIL for minutes.
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            done = subprocess.run(
                [sys.executable, "-B", "-c", SLOW_SOURCES],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            self.fail("screening six sources of up to 60,000 characters took over 30 s")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        result = json.loads(done.stdout)
        self.assertLess(result["seconds"], 10.0)
        self.assertNotIn("core_instance", result["flags"][0])


if __name__ == "__main__":
    unittest.main()
