#!/usr/bin/env python3
"""Schema validation tests — gates TODO §4 (cap_value feature_available) and §3 (default block).

Two responsibilities:
  1. Assert the on-disk glossary validates clean against the schema.
  2. Assert the schema actively rejects malformed inputs (missing
     feature_available on a cap_value, malformed default block, etc.)
     so that future PRs cannot remove the constraints by accident.

Usage:
    python tests/test_schema_validation.py
"""

import copy
import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

SPEC_DIR = Path(__file__).resolve().parent.parent / "src" / "blaueis" / "core" / "data"
GLOSSARY_PATH = SPEC_DIR / "glossary.yaml"
SCHEMA_PATH = SPEC_DIR / "glossary_schema.json"


def load_glossary_and_schema():
    with open(GLOSSARY_PATH, encoding="utf-8") as f:
        glossary = yaml.safe_load(f)
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    return glossary, schema


def find_first_cap_value(glossary):
    """Return (field_name, cap_value_name) for the first enum cap_value found."""
    for cat in glossary["fields"].values():
        if not isinstance(cat, dict):
            continue
        for fname, fdef in cat.items():
            if not isinstance(fdef, dict) or "description" not in fdef:
                continue
            cap = fdef.get("capability")
            if cap and cap.get("values"):
                first_value = next(iter(cap["values"]))
                return fname, first_value
    raise RuntimeError("No cap_value found in glossary")


def find_field_path(glossary, target_field_name):
    """Return (category_name, field_name) for the given field, walking the nested structure."""
    for cat_name, cat in glossary["fields"].items():
        if not isinstance(cat, dict):
            continue
        if target_field_name in cat and isinstance(cat[target_field_name], dict):
            return cat_name, target_field_name
    raise RuntimeError(f"Field {target_field_name!r} not found in glossary")


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}: {detail}")

    glossary, schema = load_glossary_and_schema()
    validator = Draft202012Validator(schema)

    # ── Test 1: unmodified glossary validates clean ──────────────
    errors = list(validator.iter_errors(glossary))
    check(
        "unmodified glossary validates clean",
        len(errors) == 0,
        detail=f"{len(errors)} errors; first: {errors[0].message if errors else ''}",
    )

    # ── Test 2: removing feature_available from a cap_value fails (§4) ──
    fname, vname = find_first_cap_value(glossary)
    print(f"  (using {fname}.capability.values.{vname} for §4 mutation tests)")

    cat_name, _ = find_field_path(glossary, fname)
    mutated = copy.deepcopy(glossary)
    cap_value = mutated["fields"][cat_name][fname]["capability"]["values"][vname]
    if "feature_available" in cap_value:
        del cap_value["feature_available"]
    errors = list(validator.iter_errors(mutated))
    check(
        "schema rejects cap_value missing feature_available",
        len(errors) > 0,
        detail="schema accepted the mutation — §4 enforcement broken",
    )

    # ── Test 3: malformed default block fails (§3) ──────────────
    # Schema: capability.default.valid_range must be an array of length 2.
    # Inject a 1-element array; must reject.
    mutated2 = copy.deepcopy(glossary)
    cap = mutated2["fields"][cat_name][fname]["capability"]
    cap["default"] = {
        "description": "test malformed default",
        "valid_range": [1],  # invalid: must have minItems=2
    }
    errors = list(validator.iter_errors(mutated2))
    check(
        "schema rejects malformed default.valid_range (length 1)",
        len(errors) > 0,
        detail="schema accepted invalid default — §3 enforcement broken",
    )

    # ── Test 4: valid default block validates clean (§3 happy path) ──
    mutated3 = copy.deepcopy(glossary)
    cap = mutated3["fields"][cat_name][fname]["capability"]
    cap["default"] = {
        "description": "test valid default",
        "valid_set": [0, 1],
        "correction": "snap_nearest",
    }
    errors = list(validator.iter_errors(mutated3))
    check(
        "schema accepts well-formed default block",
        len(errors) == 0,
        detail=f"errors: {[e.message for e in errors[:3]]}",
    )

    # ── Test 5: invalid feature_available enum value fails ──────
    mutated4 = copy.deepcopy(glossary)
    cat_first = next(iter(mutated4["fields"].values()))
    field_first = next(v for v in cat_first.values() if isinstance(v, dict) and "feature_available" in v)
    field_first["feature_available"] = "no"  # the stale TODO doc value
    errors = list(validator.iter_errors(mutated4))
    check(
        "schema rejects feature_available='no' (stale value, must be 'never')",
        len(errors) > 0,
        detail="schema accepted 'no' — §2 enum enforcement broken",
    )

    # ── Test 6: per-field default_priority override accepted ────
    mutated5 = copy.deepcopy(glossary)
    cat_first = next(iter(mutated5["fields"].values()))
    field_first = next(v for v in cat_first.values() if isinstance(v, dict) and "feature_available" in v)
    field_first["default_priority"] = ["protocol_new", "protocol_legacy"]
    errors = list(validator.iter_errors(mutated5))
    check(
        "schema accepts default_priority: ['protocol_new', 'protocol_legacy']",
        len(errors) == 0,
        detail=f"errors: {[e.message for e in errors[:3]]}",
    )

    # ── Test 7: top-level protocol_generations dict is well-shaped ──
    # The lookup table that drives infer_generation. Each value must be
    # 'legacy', 'new', or null; keys must look like a frame key.
    mutated6 = copy.deepcopy(glossary)
    mutated6.setdefault("protocol_generations", {})["rsp_0xnew"] = "ancient"
    errors = list(validator.iter_errors(mutated6))
    check(
        "schema rejects protocol_generations value 'ancient' (not in enum)",
        len(errors) > 0,
        detail="schema accepted invalid generation value",
    )

    # ── Summary ──────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
