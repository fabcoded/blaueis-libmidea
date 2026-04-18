#!/usr/bin/env python3
"""Tests for the command builder — round-trip encode→decode + byte checks.

Each test builds a device status, applies desired changes via the command
builder, then decodes the result and compares against expected values.

Usage:
    python test_command_builder.py tests/command_builder/command_tests.yaml
"""

import sys
from pathlib import Path

import yaml
from blaueis.core.codec import decode_frame_fields, load_glossary
from blaueis.core.command import build_command_body
from blaueis.core.process import finalize_capabilities, process_raw_frame
from blaueis.core.status import build_status


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <command_tests.yaml>", file=sys.stderr)
        sys.exit(1)

    tests_path = Path(sys.argv[1])

    with open(tests_path, encoding="utf-8") as f:
        test_data = yaml.safe_load(f)

    glossary = load_glossary()

    # Build a status with B5 caps for constraint resolution
    b5_path = (tests_path.parent / test_data["b5_source"]).resolve()
    base_status = build_status(device="test", glossary=glossary)
    with open(b5_path, encoding="utf-8") as f:
        b5_data = yaml.safe_load(f)
    for frame in b5_data["frames"]:
        hex_str = frame["body_hex"].replace(" ", "").replace("\n", "")
        process_raw_frame(base_status, bytes.fromhex(hex_str), glossary)
    finalize_capabilities(base_status, glossary)

    passed = 0
    failed = 0

    for i, test in enumerate(test_data["tests"], 1):
        name = test["name"]
        state = test.get("state", {})
        changes = test.get("changes", {})

        # Apply state to a copy of the base status
        import copy

        status = copy.deepcopy(base_status)
        # Seed each requested field by populating a `sources` slot, the
        # same shape process_data_frame writes for a real C0 frame.
        # build_command_body reads via field_query.read_field, which sees
        # whatever is in `sources` regardless of which frame key it came
        # from — so a synthetic rsp_0xc0 slot is enough.
        for field_name, value in state.items():
            if field_name in status["fields"]:
                status["fields"][field_name].setdefault("sources", {})["rsp_0xc0"] = {
                    "value": value,
                    "raw": value,
                    "frame_no": 1,
                    "ts": "2026-04-11T12:00:00Z",
                    "generation": "legacy",
                }
        status["meta"]["phase"] = "steady_state"

        # Build command. skip_preflight=True bypasses the set-command preflight
        # check — these round-trip encode→decode fixtures don't populate
        # last_updated for every sibling, so the preflight would block them.
        # Preflight behaviour is tested separately in test_set_command_preflight.py.
        result = build_command_body(status, changes, glossary, skip_preflight=True)
        body = result["body"]

        # Check expect_body_byte
        for offset_str, expected_hex in test.get("expect_body_byte", {}).items():
            offset = int(offset_str)
            expected = int(expected_hex, 16) if isinstance(expected_hex, str) else expected_hex
            actual = body[offset]
            if actual == expected:
                passed += 1
            else:
                failed += 1
                print(f"  [FAIL] #{i} {name}: body[{offset}] = 0x{actual:02X}, expected 0x{expected:02X}")

        # Check expect_decode (round-trip)
        if "expect_decode" in test:
            decoded = decode_frame_fields(bytes(body), "cmd_0x40", glossary, cap_records=status.get("capabilities_raw"))
            for field_name, expected in test["expect_decode"].items():
                actual = decoded.get(field_name, {}).get("value")
                ok = actual == expected
                if isinstance(expected, float) and actual is not None:
                    ok = abs(actual - expected) < 0.01
                if ok:
                    passed += 1
                else:
                    failed += 1
                    print(f"  [FAIL] #{i} {name}: {field_name} decoded={actual}, expected={expected}")

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
