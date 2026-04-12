#!/usr/bin/env python3
"""Unit tests for formula_evaluator.evaluate.

Covers:
  - target_temperature-style affine formulas ((raw - 50) * 0.5)
  - Multi-input arithmetic
  - Rejection of forbidden constructs (call, attribute, subscript, lambda,
    boolean literals, unknown names)
  - Syntax errors
  - Division-by-zero reporting

Usage:
    python -X utf8 tests/test_formula_evaluator.py
"""

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]

from blaueis.core.formula import FormulaError, evaluate  # noqa: E402

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  [FAIL] {name}: {detail}")


def expect_value(name, formula, inputs, want):
    try:
        got = evaluate(formula, inputs)
    except Exception as exc:
        check(name, False, f"raised {type(exc).__name__}: {exc}")
        return
    check(name, got == want, f"got {got!r}, want {want!r}")


def expect_error(name, formula, inputs, substring=""):
    try:
        got = evaluate(formula, inputs)
    except FormulaError as exc:
        check(
            name,
            substring in str(exc) if substring else True,
            f"message {str(exc)!r} missing {substring!r}",
        )
        return
    except Exception as exc:
        check(name, False, f"raised {type(exc).__name__} instead of FormulaError: {exc}")
        return
    check(name, False, f"no error raised, got {got!r}")


# ── Happy path: target_temperature affine formula ─────────────────
expect_value("target_temp raw=74 -> 12.0", "(raw - 50) * 0.5", {"raw": 74}, 12.0)
expect_value("target_temp raw=32 -> -9.0", "(raw - 50) * 0.5", {"raw": 32}, -9.0)
expect_value("target_temp raw=50 -> 0.0", "(raw - 50) * 0.5", {"raw": 50}, 0.0)

# ── Arithmetic operators ──────────────────────────────────────────
expect_value("add", "a + b", {"a": 3, "b": 4}, 7)
expect_value("sub", "a - b", {"a": 10, "b": 4}, 6)
expect_value("mul", "a * b", {"a": 3, "b": 4}, 12)
expect_value("truediv", "a / b", {"a": 10, "b": 4}, 2.5)
expect_value("floordiv", "a // b", {"a": 10, "b": 4}, 2)
expect_value("mod", "a % b", {"a": 10, "b": 3}, 1)
expect_value("pow", "a ** b", {"a": 2, "b": 10}, 1024)
expect_value("unary minus", "-a + 5", {"a": 3}, 2)
expect_value("parens precedence", "(a + b) * c", {"a": 1, "b": 2, "c": 3}, 9)

# ── Multi-input composite style ───────────────────────────────────
# e.g. power = voltage * current
expect_value("power = V*I", "v * i", {"v": 230, "i": 4.5}, 230 * 4.5)

# ── Rejection: unknown input ──────────────────────────────────────
expect_error("unknown name", "missing + 1", {"raw": 5}, "unknown input 'missing'")

# ── Rejection: forbidden constructs ───────────────────────────────
expect_error("call rejected", "abs(raw)", {"raw": -1}, "not allowed")
expect_error("attribute rejected", "raw.denominator", {"raw": 1}, "not allowed")
expect_error("subscript rejected", "raw[0]", {"raw": 1}, "not allowed")
expect_error("lambda rejected", "(lambda x: x)(raw)", {"raw": 1}, "not allowed")
expect_error("bool literal rejected", "True + raw", {"raw": 1}, "non-numeric")
expect_error("string literal rejected", "'abc'", {}, "non-numeric")
expect_error("bitand rejected", "raw & 1", {"raw": 1}, "not allowed")

# ── Rejection: syntax ─────────────────────────────────────────────
expect_error("syntax error", "raw +", {"raw": 1}, "syntax error")

# ── Rejection: division by zero ───────────────────────────────────
expect_error("div by zero", "a / b", {"a": 1, "b": 0}, "division by zero")

# ── Input type check ──────────────────────────────────────────────
expect_error("bool input rejected", "flag + 1", {"flag": True}, "not numeric")

print(f"Results: {passed} passed, {failed} failed / {passed + failed} total")
sys.exit(0 if failed == 0 else 1)
