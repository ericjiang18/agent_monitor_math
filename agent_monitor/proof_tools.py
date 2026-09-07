"""Deterministic, bounded tools for mathematical proof audits.

The functions in this module are the trusted server-side implementations behind
the Library's "Try tool" UI.  They deliberately accept a small expression
language rather than Python code: no attributes, imports, comprehensions, file
access, network access, or mutation are available.
"""
from __future__ import annotations

import ast
import hashlib
import itertools
import json
import math
import re
import runpy
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable


MAX_EXPRESSION_CHARS = 600
MAX_EXPRESSIONS = 50
MAX_AST_NODES = 220
MAX_INTEGER_BITS = 250_000
MAX_CASES = 200_000
MAX_TOOL_SECONDS = 8.0
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z")


class ToolInputError(ValueError):
    """Raised for invalid or over-budget tool input."""


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError(f"{label} must be an integer")
    if value.bit_length() > MAX_INTEGER_BITS:
        raise ToolInputError(f"{label} exceeds the exact-integer size limit")
    return value


def _bounded(value: Any) -> Any:
    integers: tuple[int, ...]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        integers = (value,)
    elif isinstance(value, Fraction):
        integers = (value.numerator, value.denominator)
    else:
        return value
    if any(abs(item).bit_length() > MAX_INTEGER_BITS for item in integers):
        raise ToolInputError("exact result exceeds the configured size limit")
    return value


def _require_integral(value: Any, label: str = "argument") -> int:
    if isinstance(value, bool):
        raise ToolInputError(f"{label} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, Fraction) and value.denominator == 1:
        return value.numerator
    raise ToolInputError(f"{label} must be an integer")


def _is_prime(value: Any) -> bool:
    """Deterministic Miller-Rabin for signed 64-bit integers."""
    n = _require_integral(value, "is_prime argument")
    if abs(n) >= 2**64:
        raise ToolInputError("is_prime is limited to unsigned 64-bit inputs")
    if n < 2:
        return False
    small = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    if n in small:
        return True
    if any(n % p == 0 for p in small):
        return False
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for base in (2, 325, 9375, 28178, 450775, 9780504, 1795265022):
        if base % n == 0:
            continue
        x = pow(base, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = (x * x) % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _valuation(value: Any, prime: Any) -> int:
    n = _require_integral(value, "valuation value")
    p = _require_integral(prime, "valuation base")
    if n == 0:
        raise ToolInputError("valuation(0, p) is not finite")
    if p <= 1:
        raise ToolInputError("valuation base must exceed 1")
    n = abs(n)
    count = 0
    while n % p == 0:
        n //= p
        count += 1
    return count


def _factorial(value: Any) -> int:
    n = _require_integral(value, "factorial argument")
    if not 0 <= n <= 5_000:
        raise ToolInputError("factorial argument must be between 0 and 5000")
    return _bounded(math.factorial(n))


def _binomial(n_value: Any, k_value: Any) -> int:
    n = _require_integral(n_value, "binomial n")
    k = _require_integral(k_value, "binomial k")
    if not 0 <= n <= 100_000 or not 0 <= k <= n:
        raise ToolInputError("binomial requires 0 <= k <= n <= 100000")
    return _bounded(math.comb(n, k))


def _lcm(*values: Any) -> int:
    if not values:
        raise ToolInputError("lcm requires at least one argument")
    return _bounded(math.lcm(*(_require_integral(v, "lcm argument") for v in values)))


def _gcd(*values: Any) -> int:
    if not values:
        raise ToolInputError("gcd requires at least one argument")
    return math.gcd(*(_require_integral(v, "gcd argument") for v in values))


def _floor(value: Any) -> int:
    if isinstance(value, Fraction):
        return value.numerator // value.denominator
    return _require_integral(value, "floor argument")


def _ceil(value: Any) -> int:
    if isinstance(value, Fraction):
        return -(-value.numerator // value.denominator)
    return _require_integral(value, "ceil argument")


def _powmod(base: Any, exponent: Any, modulus: Any) -> int:
    b = _require_integral(base, "powmod base")
    e = _require_integral(exponent, "powmod exponent")
    m = _require_integral(modulus, "powmod modulus")
    if e < 0 or m <= 0:
        raise ToolInputError("powmod requires exponent >= 0 and modulus > 0")
    return pow(b, e, m)


def _divides(divisor: Any, value: Any) -> bool:
    d = _require_integral(divisor, "divisor")
    n = _require_integral(value, "dividend")
    if d == 0:
        raise ToolInputError("zero cannot be used as a divisor")
    return n % d == 0


def _is_square(value: Any) -> bool:
    n = _require_integral(value, "is_square argument")
    return n >= 0 and math.isqrt(n) ** 2 == n


_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "gcd": _gcd,
    "lcm": _lcm,
    "factorial": _factorial,
    "binomial": _binomial,
    "floor": _floor,
    "ceil": _ceil,
    "powmod": _powmod,
    "divides": _divides,
    "is_prime": _is_prime,
    "is_square": _is_square,
    "valuation": _valuation,
}


class _ExactEvaluator(ast.NodeVisitor):
    def __init__(self, variables: dict[str, int]):
        self.variables = variables
        self.nodes = 0

    def visit(self, node: ast.AST) -> Any:  # noqa: D401
        self.nodes += 1
        if self.nodes > MAX_AST_NODES:
            raise ToolInputError("expression is too complex")
        return super().visit(node)

    def generic_visit(self, node: ast.AST) -> Any:
        raise ToolInputError(f"unsupported expression element: {type(node).__name__}")

    def visit_Expression(self, node: ast.Expression) -> Any:  # noqa: N802
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:  # noqa: N802
        if isinstance(node.value, bool):
            return node.value
        if isinstance(node.value, int):
            return _bounded(node.value)
        raise ToolInputError("only integer and boolean literals are allowed")

    def visit_Name(self, node: ast.Name) -> Any:  # noqa: N802
        if node.id in self.variables:
            return self.variables[node.id]
        raise ToolInputError(f"unknown variable: {node.id}")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:  # noqa: N802
        value = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            if not isinstance(value, bool):
                raise ToolInputError("not requires a boolean operand")
            return not value
        if isinstance(node.op, ast.UAdd):
            return _bounded(+value)
        if isinstance(node.op, ast.USub):
            return _bounded(-value)
        raise ToolInputError("unsupported unary operator")

    def visit_BinOp(self, node: ast.BinOp) -> Any:  # noqa: N802
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(left, bool) or isinstance(right, bool):
            raise ToolInputError("booleans cannot be used in arithmetic")
        if isinstance(node.op, ast.Add):
            value = left + right
        elif isinstance(node.op, ast.Sub):
            value = left - right
        elif isinstance(node.op, ast.Mult):
            value = left * right
        elif isinstance(node.op, ast.Div):
            if right == 0:
                raise ToolInputError("division by zero")
            value = Fraction(left) / Fraction(right)
        elif isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise ToolInputError("division by zero")
            value = left // right
        elif isinstance(node.op, ast.Mod):
            if right == 0:
                raise ToolInputError("modulo by zero")
            value = left % right
        elif isinstance(node.op, ast.Pow):
            exponent = _require_integral(right, "exponent")
            if abs(exponent) > 10_000:
                raise ToolInputError("absolute exponent is limited to 10000")
            base_bits = max(1, abs(_require_integral(left, "power base")).bit_length())
            if exponent > 0 and base_bits * exponent > MAX_INTEGER_BITS:
                raise ToolInputError("power result would exceed the size limit")
            if left == 0 and exponent < 0:
                raise ToolInputError("zero cannot have a negative exponent")
            value = left**exponent
        else:
            raise ToolInputError("unsupported binary operator")
        return _bounded(value)

    def visit_BoolOp(self, node: ast.BoolOp) -> bool:  # noqa: N802
        if isinstance(node.op, ast.And):
            for child in node.values:
                value = self.visit(child)
                if not isinstance(value, bool):
                    raise ToolInputError("and requires boolean operands")
                if not value:
                    return False
            return True
        if isinstance(node.op, ast.Or):
            for child in node.values:
                value = self.visit(child)
                if not isinstance(value, bool):
                    raise ToolInputError("or requires boolean operands")
                if value:
                    return True
            return False
        raise ToolInputError("unsupported boolean operator")

    def visit_Compare(self, node: ast.Compare) -> bool:  # noqa: N802
        left = self.visit(node.left)
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            right = self.visit(comparator)
            if isinstance(operator, ast.Eq):
                ok = left == right
            elif isinstance(operator, ast.NotEq):
                ok = left != right
            elif isinstance(operator, ast.Lt):
                ok = left < right
            elif isinstance(operator, ast.LtE):
                ok = left <= right
            elif isinstance(operator, ast.Gt):
                ok = left > right
            elif isinstance(operator, ast.GtE):
                ok = left >= right
            else:
                raise ToolInputError("unsupported comparison operator")
            if not ok:
                return False
            left = right
        return True

    def visit_IfExp(self, node: ast.IfExp) -> Any:  # noqa: N802
        condition = self.visit(node.test)
        if not isinstance(condition, bool):
            raise ToolInputError("conditional expression requires a boolean condition")
        return self.visit(node.body if condition else node.orelse)

    def visit_Call(self, node: ast.Call) -> Any:  # noqa: N802
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ToolInputError("function is not allowlisted")
        if node.keywords:
            raise ToolInputError("keyword arguments are not supported")
        if len(node.args) > 20:
            raise ToolInputError("too many function arguments")
        args = [self.visit(arg) for arg in node.args]
        try:
            return _bounded(_FUNCTIONS[node.func.id](*args))
        except ToolInputError:
            raise
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise ToolInputError(f"invalid {node.func.id} call: {exc}") from exc


def evaluate_exact(expression: str, variables: dict[str, int] | None = None) -> Any:
    expression = str(expression or "").strip()
    if not expression or len(expression) > MAX_EXPRESSION_CHARS:
        raise ToolInputError(f"expression must contain 1-{MAX_EXPRESSION_CHARS} characters")
    checked: dict[str, int] = {}
    for raw_name, raw_value in (variables or {}).items():
        name = str(raw_name)
        if not _NAME_RE.fullmatch(name):
            raise ToolInputError(f"invalid variable name: {name!r}")
        checked[name] = _integer(raw_value, f"variable {name}")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolInputError(f"invalid expression syntax: {exc.msg}") from exc
    return _ExactEvaluator(checked).visit(tree)


def _json_value(value: Any) -> Any:
    if isinstance(value, Fraction):
        return {
            "exact": f"{value.numerator}/{value.denominator}",
            "numerator": value.numerator,
            "denominator": value.denominator,
        }
    if isinstance(value, (bool, int, str)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(_json_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _arguments(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ToolInputError("tool arguments must be a JSON object")
    return payload


def exact_math_certificate(payload: dict[str, Any]) -> dict[str, Any]:
    args = _arguments(payload)
    allowed = {"expressions", "variables"}
    extra = set(args) - allowed
    if extra:
        raise ToolInputError("unknown arguments: " + ", ".join(sorted(extra)))
    expressions = args.get("expressions")
    if not isinstance(expressions, list) or not 1 <= len(expressions) <= MAX_EXPRESSIONS:
        raise ToolInputError(f"expressions must be a list of 1-{MAX_EXPRESSIONS} strings")
    variables = args.get("variables") or {}
    if not isinstance(variables, dict) or len(variables) > 32:
        raise ToolInputError("variables must be an object with at most 32 entries")
    started = time.monotonic()
    rows = []
    booleans = []
    for expression in expressions:
        if not isinstance(expression, str):
            raise ToolInputError("every expression must be a string")
        value = evaluate_exact(expression, variables)
        rows.append({"expression": expression, "value": _json_value(value), "type": type(value).__name__})
        if isinstance(value, bool):
            booleans.append(value)
    if booleans:
        status = "passed" if all(booleans) else "failed"
    else:
        status = "completed"
    result: dict[str, Any] = {
        "tool": "exact-math-certificate",
        "status": status,
        "method": "restricted AST with exact integers and rational Fractions",
        "variables": variables,
        "results": rows,
        "boolean_checks": len(booleans),
        "all_boolean_checks_true": all(booleans) if booleans else None,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "limitations": "This certifies only the supplied finite expressions, not an unrestricted theorem.",
    }
    result["certificate_sha256"] = _digest({"arguments": args, "result": result})
    return result


def _range_values(spec: dict[str, Any], index: int) -> tuple[str, range]:
    if not isinstance(spec, dict):
        raise ToolInputError(f"ranges[{index}] must be an object")
    extra = set(spec) - {"name", "start", "end", "step"}
    if extra:
        raise ToolInputError(f"ranges[{index}] has unknown fields: {', '.join(sorted(extra))}")
    name = str(spec.get("name") or "")
    if not _NAME_RE.fullmatch(name):
        raise ToolInputError(f"ranges[{index}].name is invalid")
    start = _integer(spec.get("start"), f"ranges[{index}].start")
    end = _integer(spec.get("end"), f"ranges[{index}].end")
    step = _integer(spec.get("step", 1), f"ranges[{index}].step")
    if step == 0:
        raise ToolInputError("range step cannot be zero")
    if (step > 0 and start > end) or (step < 0 and start < end):
        raise ToolInputError("range direction does not reach its inclusive end")
    stop = end + (1 if step > 0 else -1)
    values = range(start, stop, step)
    if not values:
        raise ToolInputError("range cannot be empty")
    return name, values


def bounded_counterexample_search(payload: dict[str, Any]) -> dict[str, Any]:
    args = _arguments(payload)
    allowed = {"predicate", "ranges", "fixed", "max_examples"}
    extra = set(args) - allowed
    if extra:
        raise ToolInputError("unknown arguments: " + ", ".join(sorted(extra)))
    predicate = args.get("predicate")
    if not isinstance(predicate, str):
        raise ToolInputError("predicate must be a string")
    raw_ranges = args.get("ranges")
    if not isinstance(raw_ranges, list) or not 1 <= len(raw_ranges) <= 4:
        raise ToolInputError("ranges must contain 1-4 inclusive integer ranges")
    ranges = [_range_values(spec, index) for index, spec in enumerate(raw_ranges)]
    names = [name for name, _ in ranges]
    if len(set(names)) != len(names):
        raise ToolInputError("range variable names must be unique")
    fixed = args.get("fixed") or {}
    if not isinstance(fixed, dict) or len(fixed) > 24:
        raise ToolInputError("fixed must be an object with at most 24 integer values")
    fixed_values: dict[str, int] = {}
    for raw_name, raw_value in fixed.items():
        name = str(raw_name)
        if not _NAME_RE.fullmatch(name) or name in names:
            raise ToolInputError(f"invalid or duplicate fixed variable: {name!r}")
        fixed_values[name] = _integer(raw_value, f"fixed variable {name}")
    max_examples = args.get("max_examples", 10)
    max_examples = _integer(max_examples, "max_examples")
    if not 1 <= max_examples <= 50:
        raise ToolInputError("max_examples must be between 1 and 50")
    total_cases = math.prod(len(values) for _, values in ranges)
    if total_cases > MAX_CASES:
        raise ToolInputError(f"cartesian search has {total_cases} cases; maximum is {MAX_CASES}")
    started = time.monotonic()
    checked = 0
    counterexample_count = 0
    examples: list[dict[str, Any]] = []
    for point in itertools.product(*(values for _, values in ranges)):
        if time.monotonic() - started > MAX_TOOL_SECONDS:
            raise ToolInputError(f"search exceeded the {MAX_TOOL_SECONDS:g}s deterministic time limit")
        variables = {**fixed_values, **dict(zip(names, point, strict=True))}
        value = evaluate_exact(predicate, variables)
        if not isinstance(value, bool):
            raise ToolInputError("predicate must evaluate to true or false")
        checked += 1
        if not value:
            counterexample_count += 1
            if len(examples) < max_examples:
                examples.append({"variables": variables, "predicate_value": False})
    result: dict[str, Any] = {
        "tool": "bounded-counterexample-search",
        "status": "counterexample_found" if counterexample_count else "no_counterexample_in_scope",
        "predicate": predicate,
        "scope": [
            {"name": name, "start": values.start, "end": values[-1], "step": values.step}
            for name, values in ranges
        ],
        "fixed": fixed_values,
        "checked_cases": checked,
        "scope_complete": checked == total_cases,
        "counterexample_count": counterexample_count,
        "counterexamples": examples,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "limitations": "No-counterexample means only that the declared finite Cartesian scope was exhausted.",
    }
    result["certificate_sha256"] = _digest({"arguments": args, "result": result})
    return result


_DOMAIN_PATTERNS = {
    "natural": r"(?:\\mathbb\s*\{?N\}?|ℕ|\bnatural(?:s| numbers?)?\b)",
    "integer": r"(?:\\mathbb\s*\{?Z\}?|ℤ|\binteger(?:s)?\b)",
    "rational": r"(?:\\mathbb\s*\{?Q\}?|ℚ|\brational(?:s| numbers?)?\b)",
    "real": r"(?:\\mathbb\s*\{?R\}?|ℝ|\breal(?:s| numbers?)?\b)",
    "complex": r"(?:\\mathbb\s*\{?C\}?|ℂ|\bcomplex(?:es| numbers?)?\b)",
    "finite": r"\bfinite\b",
    "infinite": r"\binfinite(?:ly)?\b",
    "positive": r"\bpositive\b|>\s*0",
    "nonnegative": r"\bnon[- ]?negative\b|>=?\s*0|≥\s*0",
    "connected": r"\bconnected\b",
    "simple": r"\bsimple\b",
}


def _statement_anchors(text: str) -> dict[str, Any]:
    lowered = text.lower()
    domains = sorted(name for name, pattern in _DOMAIN_PATTERNS.items() if re.search(pattern, text, re.I))
    constants = sorted(set(re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", text)))
    forall = len(re.findall(r"(?:∀|\\forall\b|\bfor\s+(?:every|all|each)\b)", lowered))
    exists = len(re.findall(r"(?:∃|\\exists\b|\bthere\s+exist(?:s)?\b)", lowered))
    relations = sorted(
        name
        for name, pattern in {
            "strict-less": r"(?<![<])<(?![=>])|<\\!",
            "less-equal": r"<=|≤|\\leq?\b",
            "strict-greater": r"(?<![>])>(?![=])",
            "greater-equal": r">=|≥|\\geq?\b",
            "equality": r"(?<![<>=!])=(?!=)",
            "divisibility": r"\\mid\b|\bdivides\b",
            "subset": r"\\subset(?:eq)?\b|⊂|⊆",
        }.items()
        if re.search(pattern, text)
    )
    return {
        "characters": len(text),
        "forall_markers": forall,
        "exists_markers": exists,
        "domains_and_qualifiers": domains,
        "numeric_constants": constants,
        "relation_kinds": relations,
        "unfinished_markers": sorted(set(re.findall(r"\b(?:sorry|admit)\b", lowered))),
    }


def statement_fidelity_audit(payload: dict[str, Any]) -> dict[str, Any]:
    args = _arguments(payload)
    allowed = {"original", "candidate", "formal"}
    extra = set(args) - allowed
    if extra:
        raise ToolInputError("unknown arguments: " + ", ".join(sorted(extra)))
    original = args.get("original")
    candidate = args.get("candidate")
    formal = args.get("formal") or ""
    if not isinstance(original, str) or not original.strip():
        raise ToolInputError("original must be a non-empty string")
    if not isinstance(candidate, str) or not candidate.strip():
        raise ToolInputError("candidate must be a non-empty string")
    if not isinstance(formal, str):
        raise ToolInputError("formal must be a string")
    if max(len(original), len(candidate), len(formal)) > 80_000:
        raise ToolInputError("each statement is limited to 80000 characters")
    original_anchors = _statement_anchors(original)
    targets = {"candidate": _statement_anchors(candidate)}
    if formal.strip():
        targets["formal"] = _statement_anchors(formal)
    issues: list[dict[str, str]] = []
    for label, anchors in targets.items():
        for key in ("domains_and_qualifiers", "numeric_constants", "relation_kinds"):
            missing = sorted(set(original_anchors[key]) - set(anchors[key]))
            if missing:
                issues.append({
                    "target": label,
                    "kind": f"missing-{key.replace('_', '-')}",
                    "detail": ", ".join(missing),
                })
        for quantifier in ("forall_markers", "exists_markers"):
            if original_anchors[quantifier] != anchors[quantifier]:
                issues.append({
                    "target": label,
                    "kind": "quantifier-marker-count",
                    "detail": f"{quantifier}: original {original_anchors[quantifier]}, {label} {anchors[quantifier]}",
                })
        if anchors["unfinished_markers"]:
            issues.append({
                "target": label,
                "kind": "unfinished-formalization",
                "detail": ", ".join(anchors["unfinished_markers"]),
            })
    result: dict[str, Any] = {
        "tool": "statement-fidelity-audit",
        "status": "review_required" if issues else "preflight_clear",
        "original_anchors": original_anchors,
        "target_anchors": targets,
        "issues": issues,
        "limitations": (
            "This is a deterministic anchor preflight, not a semantic proof of equivalence. "
            "A separate mathematical or model-based fidelity review is still required."
        ),
    }
    result["certificate_sha256"] = _digest({"arguments": args, "result": result})
    return result


def audit_output_validator(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a verifier-output.v1 object with the installed locked contract.

    The web broker sends ``{"document": ...}``, while materialized shell tools
    commonly receive the verifier object itself on stdin. Accept both shapes;
    the locked validator remains the authority for the raw object's schema and
    semantic invariants.
    """
    args = _arguments(payload)
    if "document" in args:
        extra = set(args) - {"document"}
        if extra:
            raise ToolInputError("unknown envelope arguments: " + ", ".join(sorted(extra)))
        document = args.get("document")
    else:
        document = args
    if isinstance(document, str):
        if len(document) > 200_000:
            raise ToolInputError("document string exceeds 200000 characters")
        try:
            document = json.loads(document)
        except json.JSONDecodeError as exc:
            raise ToolInputError(f"document is not valid JSON: {exc.msg}") from exc
    if not isinstance(document, dict):
        raise ToolInputError("document must be a JSON object or encoded JSON object")
    validator = (
        Path(__file__).resolve().parent
        / "starter_skills"
        / "research-proof-audit"
        / "scripts"
        / "validate_output.py"
    )
    if not validator.is_file() or validator.is_symlink():
        raise ToolInputError("installed audit validator is unavailable")
    namespace = runpy.run_path(str(validator))
    validate = namespace.get("validate_document")
    if not callable(validate):
        raise ToolInputError("installed audit validator has no callable contract")
    result = dict(validate(document))
    result["tool"] = "audit-output-validator"
    result["certificate_sha256"] = _digest(
        {"arguments": {"document": document}, "result": result}
    )
    return result


TRUSTED_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "tool_exact_math_certificate": exact_math_certificate,
    "tool_bounded_counterexample_search": bounded_counterexample_search,
    "tool_statement_fidelity_audit": statement_fidelity_audit,
    "tool_audit_output_validator": audit_output_validator,
}


def run_trusted_tool(tool_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = TRUSTED_TOOL_HANDLERS.get(str(tool_id))
    if handler is None:
        raise ToolInputError("tool has no trusted server-side runner")
    return handler(arguments)


def _read_cli_payload(raw: str) -> dict[str, Any]:
    if raw.startswith("@"):
        name = raw[1:]
        requested = Path(name)
        if (
            not name
            or requested.is_absolute()
            or "\\" in name
            or requested.suffix.lower() != ".json"
            or any(part in {"", ".", ".."} for part in requested.parts)
        ):
            raise ToolInputError("@file input must name a workspace-relative .json file")
        root = Path.cwd().resolve()
        candidate = root / requested
        cursor = root
        for part in requested.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ToolInputError("@file input must not traverse symbolic links")
        try:
            path = candidate.resolve(strict=True)
        except OSError as exc:
            raise ToolInputError("@file input must resolve to a workspace-local JSON file") from exc
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or path.suffix.lower() != ".json"
        ):
            raise ToolInputError("@file input must resolve to a workspace-local JSON file")
        with path.open("r", encoding="utf-8") as handle:
            data = handle.read(200_001)
    else:
        data = raw or sys.stdin.read(200_001)
    if len(data) > 200_000:
        raise ToolInputError("JSON input exceeds 200000 characters")
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ToolInputError(f"invalid JSON input: {exc.msg}") from exc
    return _arguments(payload)


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in {"-h", "--help"}:
        print("Usage: python -m agent_monitor.proof_tools TOOL_NAME 'JSON'\n"
              "Tools: exact-math-certificate, bounded-counterexample-search, "
              "statement-fidelity-audit, audit-output-validator")
        return 0
    name = args.pop(0)
    tool_id = {
        "exact-math-certificate": "tool_exact_math_certificate",
        "bounded-counterexample-search": "tool_bounded_counterexample_search",
        "statement-fidelity-audit": "tool_statement_fidelity_audit",
        "audit-output-validator": "tool_audit_output_validator",
    }.get(name, name)
    try:
        payload = _read_cli_payload(args[0] if args else "")
        result = run_trusted_tool(tool_id, payload)
    except (ToolInputError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
