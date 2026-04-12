"""Sandboxed numeric formula evaluator for glossary derived_from blocks.

The glossary vocabulary introduced by the conservative migration pass carries
`derived_from.formula` strings (e.g. ``"(raw - 50) * 0.5"``) that describe how
a displayed field is computed from its raw inputs. This module evaluates those
strings against a supplied input dict, with a tight whitelist of allowed AST
nodes so untrusted glossary data cannot import modules, call functions, or
reach attributes.

Allowed:
  - Numeric literals (int, float)
  - Variable names bound in the ``inputs`` dict
  - Unary +/-
  - Binary +, -, *, /, //, %, **
  - Parentheses

Rejected (raises FormulaError):
  - Any attribute access, subscripting, call, lambda, comprehension
  - Any name not provided in ``inputs``
  - Division by zero (propagates as ZeroDivisionError wrapped in FormulaError)

This is intentionally smaller than simpleeval: the glossary formulas are all
plain arithmetic over integer raws, and avoiding a third-party dependency
keeps the tools-serial directory importable on the Pi without extra setup.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Mapping

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class FormulaError(ValueError):
    """Raised when a formula is malformed, uses a forbidden construct, or
    references an input that was not supplied."""


def evaluate(formula: str, inputs: Mapping[str, float]) -> float:
    """Evaluate ``formula`` against ``inputs`` and return the numeric result.

    >>> evaluate("(raw - 50) * 0.5", {"raw": 74})
    12.0
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"syntax error in formula {formula!r}: {exc}") from exc

    try:
        return _eval_node(tree.body, inputs)
    except FormulaError:
        raise
    except ZeroDivisionError as exc:
        raise FormulaError(f"division by zero in formula {formula!r}") from exc
    except Exception as exc:
        raise FormulaError(f"error evaluating {formula!r}: {exc}") from exc


def _eval_node(node: ast.AST, inputs: Mapping[str, float]) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int | float)) and not isinstance(node.value, bool):
            return node.value
        raise FormulaError(f"non-numeric constant: {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id not in inputs:
            raise FormulaError(f"unknown input {node.id!r}")
        value = inputs[node.id]
        if not isinstance(value, (int | float)) or isinstance(value, bool):
            raise FormulaError(f"input {node.id!r} is not numeric: {value!r}")
        return value

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise FormulaError(f"operator {op_type.__name__} not allowed")
        left = _eval_node(node.left, inputs)
        right = _eval_node(node.right, inputs)
        return _BIN_OPS[op_type](left, right)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY_OPS:
            raise FormulaError(f"unary operator {op_type.__name__} not allowed")
        return _UNARY_OPS[op_type](_eval_node(node.operand, inputs))

    raise FormulaError(f"AST node {type(node).__name__} not allowed")
