#!/usr/bin/env python3
"""Duration-counter codec golden-vector test.

Reads ``fixtures/duration_counter_vectors.json`` and asserts that the
codec decodes each synthetic frame body to the recorded expected
values. Covers the 12 rsp_0xc1_group0 fields (power_on_*,
total_worked_*, current_session_*) and the 3 rsp_0xa1 fields
(current_work_*).

The fixture is regenerated from upstream-protocol research; this test
only knows the JSON shape. If a single field diverges, the failure
message names that field directly — diagnose by reading the glossary
decode entry for that field and the protocol_key in the failing
vector.

Run: python test_duration_counter_codec.py
"""

import json
import sys
from pathlib import Path

from blaueis.core.codec import decode_frame_fields, load_glossary  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "duration_counter_vectors.json"

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  [PASS] {label}")
        passed += 1
    else:
        print(f"  [FAIL] {label}: {detail}")
        failed += 1


def main():
    fixture = json.loads(_FIXTURE.read_text())
    vectors = fixture["vectors"]
    glossary = load_glossary()

    print(f"=== duration-counter codec test ({len(vectors)} vectors) ===")
    for vec in vectors:
        body = bytes.fromhex(vec["body_hex"])
        protocol_key = vec["protocol_key"]
        expected = vec["expected"]
        decoded_raw = decode_frame_fields(body, protocol_key, glossary, cap_records=None)
        decoded = {k: v.get("value") for k, v in decoded_raw.items()}

        diffs = []
        for fname, want in expected.items():
            got = decoded.get(fname)
            if got != want:
                diffs.append(f"{fname}: want={want} got={got}")
        check(f"vector {vec['name']!r}", not diffs, "; ".join(diffs))

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def test_duration_counter_codec():
    """pytest entry-point: same logic, expressed as an assertion."""
    fixture = json.loads(_FIXTURE.read_text())
    glossary = load_glossary()
    failures: list[str] = []
    for vec in fixture["vectors"]:
        body = bytes.fromhex(vec["body_hex"])
        decoded_raw = decode_frame_fields(body, vec["protocol_key"], glossary, cap_records=None)
        decoded = {k: v.get("value") for k, v in decoded_raw.items()}
        for fname, want in vec["expected"].items():
            got = decoded.get(fname)
            if got != want:
                failures.append(f"{vec['name']}.{fname}: want={want} got={got}")
    assert not failures, "\n  " + "\n  ".join(failures)


if __name__ == "__main__":
    sys.exit(main())
