#!/usr/bin/env python3
"""Unit tests for process_frame.py — targeted checks on frame processing.

Each test starts with a fresh status. Tests B5 upgrades, capability gating,
phase transitions, and value decoding in isolation.

Usage:
    python test_process_frame.py tests/process_frame_tests/process_tests.yaml
"""

import sys
from pathlib import Path

import yaml
from blaueis.core.codec import load_glossary
from blaueis.core.process import finalize_capabilities, process_raw_frame
from blaueis.core.query import read_field
from blaueis.core.status import build_status

# Field-state shortcut keys recognised in fixture assertions. When the
# top-level assertion key matches one of these, the runner routes the
# lookup through field_query.read_field() with the field's default
# priority — fixtures stay decoupled from the underlying `sources` slot
# layout.
FIELD_SHORTCUT_KEYS = {"value", "source", "ts", "generation", "scope_matched"}


def resolve_dot_path(obj, path):
    """Navigate a nested dict/list using dot-separated keys."""
    for key in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and key.isdigit():
            obj = obj[int(key)]
        else:
            return None
    return obj


def resolve_field_assertion(status: dict, field_name: str, path: str):
    """Resolve a field-level fixture assertion path against `status`.

    Top-level shortcut keys (`value`, `source`, `ts`, `generation`,
    `scope_matched`) route through read_field(); everything else falls
    through to a direct dict walk into the field-state.
    """
    keys = path.split(".")
    if keys[0] in FIELD_SHORTCUT_KEYS:
        result = read_field(status, field_name)
        if result is None:
            return None
        value = result.get(keys[0])
        for k in keys[1:]:
            if isinstance(value, dict):
                value = value.get(k)
            elif isinstance(value, list) and k.isdigit():
                value = value[int(k)]
            else:
                return None
        return value
    return resolve_dot_path(status["fields"].get(field_name, {}), path)


def values_match(actual, expected):
    """Compare values with float tolerance."""
    if isinstance(expected, float) and isinstance(actual, int | float):
        return abs(actual - expected) < 0.01
    if isinstance(expected, list) and isinstance(actual, list):
        if len(actual) != len(expected):
            return False
        return all(values_match(a, e) for a, e in zip(actual, expected, strict=False))
    return actual == expected


def load_and_process_b5(status, glossary, source_path):
    """Load a B5 fixture and process all frames, then finalize capabilities."""
    with open(source_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for frame in data["frames"]:
        hex_str = frame["body_hex"].replace(" ", "").replace("\n", "")
        process_raw_frame(status, bytes.fromhex(hex_str), glossary)
    finalize_capabilities(status, glossary)


def load_and_process_frame(status, glossary, source_path, index=0):
    """Load a frame fixture and process one frame."""
    with open(source_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    frame = data["frames"][index]
    hex_str = frame["body_hex"].replace(" ", "").replace("\n", "")
    process_raw_frame(status, bytes.fromhex(hex_str), glossary)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <process_tests.yaml>", file=sys.stderr)
        sys.exit(1)

    tests_path = Path(sys.argv[1])

    with open(tests_path, encoding="utf-8") as f:
        test_data = yaml.safe_load(f)

    glossary = load_glossary()
    passed = 0
    failed = 0

    for i, test in enumerate(test_data["tests"], 1):
        name = test["name"]
        action = test["action"]
        status = build_status(device="test", glossary=glossary)

        source_path = (tests_path.parent / test["source"]).resolve()
        index = test.get("index", 0)

        # Execute action
        if action == "b5":
            load_and_process_b5(status, glossary, source_path)
        elif action == "frame_no_b5":
            load_and_process_frame(status, glossary, source_path, index)
        elif action == "b5_then_frame":
            b5_path = (tests_path.parent / test["b5_source"]).resolve()
            load_and_process_b5(status, glossary, b5_path)
            load_and_process_frame(status, glossary, source_path, index)

        # Check field-level assertions
        if "check_field" in test and "assert" in test:
            field_name = test["check_field"]
            for path, expected in test["assert"].items():
                actual = resolve_field_assertion(status, field_name, path)
                if values_match(actual, expected):
                    passed += 1
                else:
                    failed += 1
                    print(f"  [FAIL] #{i} {name}: {field_name}.{path} = {actual}, expected {expected}")

        # Check meta-level assertions
        if "assert_meta" in test:
            for path, expected in test["assert_meta"].items():
                actual = resolve_dot_path(status["meta"], path)
                if values_match(actual, expected):
                    passed += 1
                else:
                    failed += 1
                    print(f"  [FAIL] #{i} {name}: meta.{path} = {actual}, expected {expected}")

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
