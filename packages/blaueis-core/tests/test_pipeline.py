#!/usr/bin/env python3
"""Pipeline integration tests — build_status → process frames → verify state.

Reads a pipeline.yaml fixture with ordered steps (B5, frame) and assertions.
Each step processes a frame and checks dot-path assertions against the status.

Usage:
    python test_pipeline.py tests/pipeline_xtremesaveblue/pipeline.yaml
"""

import sys
from pathlib import Path

from pathlib import Path

import yaml
from blaueis.core.status import build_status
from blaueis.core.query import read_field
from blaueis.core.codec import load_glossary
from blaueis.core.process import finalize_capabilities, process_raw_frame

# Field-state shortcut keys recognised in fixture assertions. Walking
# `fields.<name>.<key>` for any of these resolves via read_field() with
# the field's default priority — no per-fixture priority knob, the
# fixture is asking "what does the default-priority reader see?".
FIELD_SHORTCUT_KEYS = {"value", "source", "ts", "generation", "scope_matched"}


def resolve_dot_path(obj, path, status=None):
    """Navigate a nested dict/list using dot-separated keys.

    When `status` is provided and the path matches `fields.<name>.<key>`
    where <key> is a field shortcut, the lookup is routed through
    field_query.read_field() and the corresponding result key is
    returned. Other paths fall through to a plain dict/list walk.
    """
    keys = path.split(".")
    if status is not None and len(keys) >= 3 and keys[0] == "fields" and keys[2] in FIELD_SHORTCUT_KEYS:
        field_name = keys[1]
        result = read_field(status, field_name)
        if result is None:
            return None
        value = result.get(keys[2])
        # Allow further drilling, e.g. disagreements.0.slot
        for k in keys[3:]:
            if isinstance(value, dict):
                value = value.get(k)
            elif isinstance(value, list) and k.isdigit():
                value = value[int(k)]
            else:
                return None
        return value

    for key in keys:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and key.isdigit():
            obj = obj[int(key)]
        else:
            return None
    return obj


def values_match(actual, expected):
    """Compare values with float tolerance."""
    if isinstance(expected, float) and isinstance(actual, int | float):
        return abs(actual - expected) < 0.01
    if isinstance(expected, list) and isinstance(actual, list):
        if len(actual) != len(expected):
            return False
        return all(values_match(a, e) for a, e in zip(actual, expected, strict=False))
    return actual == expected


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <pipeline.yaml>", file=sys.stderr)
        sys.exit(1)

    pipeline_path = Path(sys.argv[1])

    with open(pipeline_path, encoding="utf-8") as f:
        pipeline = yaml.safe_load(f)

    glossary = load_glossary()
    status = build_status(device=pipeline.get("device", "test"), glossary=glossary)

    passed = 0
    failed = 0

    for step_idx, step in enumerate(pipeline["steps"], 1):
        action = step["action"]
        source_path = (pipeline_path.parent / step["source"]).resolve()

        with open(source_path, encoding="utf-8") as f:
            source_data = yaml.safe_load(f)

        print(f"  Step {step_idx}: {action} from {step['source']}")

        if action == "b5":
            for frame in source_data["frames"]:
                hex_str = frame["body_hex"].replace(" ", "").replace("\n", "")
                body = bytes.fromhex(hex_str)
                process_raw_frame(status, body, glossary, timestamp=str(frame.get("timestamp", "")))
            finalize_capabilities(status, glossary)
        elif action == "frame":
            index = step.get("index", 0)
            frame = source_data["frames"][index]
            hex_str = frame["body_hex"].replace(" ", "").replace("\n", "")
            body = bytes.fromhex(hex_str)
            process_raw_frame(status, body, glossary, timestamp=str(frame.get("timestamp", "")))

        # Check assertions
        for path, expected in step.get("assert", {}).items():
            actual = resolve_dot_path(status, path, status=status)
            if values_match(actual, expected):
                passed += 1
            else:
                failed += 1
                print(f"    [FAIL] {path}: expected={expected}, got={actual}")

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
