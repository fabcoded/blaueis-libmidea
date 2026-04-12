#!/usr/bin/env python3
"""Tests for the generic multi-byte LE/BE encoding dispatch.

The codec previously only handled multi-byte values for the 4 hardcoded
power encodings (power_bcd_4, power_bcd_3, power_linear_4, power_linear_3)
which apply_encoding pattern-matched by name. Generic uint16/uint24
encodings now dispatch on a `byte_order` field declared in the
glossary's `encodings:` block.

Cross-validation strategy
-------------------------

The mill1000/midea-msmart Lua plugin's C1 decoder is only ~5 lines
(power + humidity), so it cannot serve as a reference oracle for any
of the 16-bit fields this test covers (Group 0 day counters, Group 5
fan/compressor runtime counters, Group 6 torque angle).

Instead we cross-validate against Python's `int.from_bytes` — the
canonical reference for byte-order arithmetic. Test #16 generates 64
random byte patterns and asserts our `_read_uint` and `apply_encoding`
implementations produce the same value as `int.from_bytes(b, "little")`
or `int.from_bytes(b, "big")`. This validates the encoding
implementation against a standard library that has been used by every
Python program for over a decade.

Probe-anchored field tests use real Session 15 capture bytes:
- Group 5 body[6..7] = EA 1B → indoor_fan_runtime = 7146 minutes
- Group 5 body[14..15] = 7B 07 → compressor_cumul_hours = 1915 hours
- Group 6 body[15..16] = 00 00 → torque_compensation_angle = 0
- Group 0 body[4..5] / [9..10] / [14..15] = all zero → days = 0

Usage:
    python tests/test_multibyte_encoding.py
"""

import random
import sys
from pathlib import Path


from blaueis.core.codec import _read_uint, apply_encoding, decode_frame_fields, load_glossary

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}: {detail}")


# ── Test 1-4: _read_uint helper, pure cases ──────────────────────────────


def test_read_uint_pure():
    print("\n1-4. _read_uint pure cases")
    check("uint16_le 0x1234", _read_uint(b"\x34\x12", 0, 2, "le") == 0x1234, "")
    check("uint16_be 0x1234", _read_uint(b"\x12\x34", 0, 2, "be") == 0x1234, "")
    check("uint24_le 0x123456", _read_uint(b"\x56\x34\x12", 0, 3, "le") == 0x123456, "")
    check("uint24_be 0x123456", _read_uint(b"\x12\x34\x56", 0, 3, "be") == 0x123456, "")


# ── Test 5: read at offset ───────────────────────────────────────────────


def test_read_at_offset():
    print("\n5. _read_uint at offset")
    body = b"\x00\x00\x00\x00\xea\x1b\x00\x00"
    val = _read_uint(body, 4, 2, "le")
    check("uint16_le at offset 4 = 0x1BEA", val == 0x1BEA, f"got {val:#x}")


# ── Test 6: read past end raises IndexError ──────────────────────────────


def test_read_past_end_raises():
    print("\n6. _read_uint past end raises IndexError")
    try:
        _read_uint(b"\x00", 0, 2, "le")
        check("raised IndexError", False, "no exception")
    except IndexError as e:
        check("raised IndexError", True, "")
        check("error mentions length", "body" in str(e), f"got: {e}")


# ── Test 7-8: apply_encoding dispatches to multi-byte for new encodings ─


def test_apply_encoding_dispatches_uint16_le():
    print("\n7. apply_encoding dispatches uint16_le via byte_order metadata")
    glossary = load_glossary()
    encs = glossary["encodings"]
    body = b"\x00" * 6 + b"\xea\x1b" + b"\x00" * 4
    val = apply_encoding(0xEA, "uint16_le", encs, body=body, offset=6)
    check("returns 7146 not 234", val == 7146, f"got {val}")


def test_apply_encoding_dispatches_uint16_be():
    print("\n8. apply_encoding dispatches uint16_be via byte_order metadata")
    glossary = load_glossary()
    encs = glossary["encodings"]
    body = b"\x12\x34"
    val = apply_encoding(0x12, "uint16_be", encs, body=body, offset=0)
    check("returns 0x1234", val == 0x1234, f"got {val:#x}")


# ── Test 9: glossary encodings loadable ─────────────────────────────────


def test_glossary_encodings_loadable():
    print("\n9. Glossary encodings block loads new entries")
    glossary = load_glossary()
    encs = glossary["encodings"]
    for name, expected in [
        ("uint16_le", ("le", 2)),
        ("uint16_be", ("be", 2)),
        ("uint24_le", ("le", 3)),
        ("uint24_be", ("be", 3)),
    ]:
        spec = encs.get(name, {})
        ok = spec.get("byte_order") == expected[0] and spec.get("byte_count") == expected[1]
        check(f"{name}: byte_order={expected[0]}, byte_count={expected[1]}", ok, f"got {spec}")


# ── Test 10-12: real probe-anchored field decoding ──────────────────────


def test_indoor_fan_runtime_from_real_frame():
    print("\n10. indoor_fan_runtime decodes from real S15 c1g5 frame")
    glossary = load_glossary()
    body = bytes.fromhex("c1 21 01 45 00 55 ea 1b 4b 24 00 00 00 0b 7b 07 00 b7 88 00 00".replace(" ", ""))
    decoded = decode_frame_fields(body, "rsp_0xc1_group5", glossary)
    val = decoded.get("indoor_fan_runtime", {}).get("value")
    check("indoor_fan_runtime = 7146 (was 234 with low-byte-only decode)", val == 7146, f"got {val}")


def test_compressor_cumul_hours_from_real_frame():
    print("\n11. compressor_cumul_hours decodes from real S15 c1g5 frame")
    glossary = load_glossary()
    body = bytes.fromhex("c1 21 01 45 00 55 ea 1b 4b 24 00 00 00 0b 7b 07 00 b7 88 00 00".replace(" ", ""))
    decoded = decode_frame_fields(body, "rsp_0xc1_group5", glossary)
    val = decoded.get("compressor_cumul_hours", {}).get("value")
    check("compressor_cumul_hours = 1915 (was 123)", val == 1915, f"got {val}")


def test_torque_compensation_angle_from_real_frame():
    print("\n12. torque_compensation_angle decodes from real S15 c1g6 frame")
    glossary = load_glossary()
    body = bytes.fromhex("c1 21 01 46 34 b1 1e 00 10 16 ff 03 02 00 02 00 00 00 00 00 00".replace(" ", ""))
    decoded = decode_frame_fields(body, "rsp_0xc1_group6", glossary)
    val = decoded.get("torque_compensation_angle", {}).get("value")
    check("torque_compensation_angle = 0 (LE16, both bytes 0)", val == 0, f"got {val}")


# ── Test 13-15: synthetic BE16 day counter decoding ─────────────────────


def test_power_on_days_be16():
    print("\n13. power_on_days BE16 from crafted body[4..5]=00 64")
    glossary = load_glossary()
    # body[4..5] = 0x00 0x64 → BE16 = 0x0064 = 100
    body = bytes(4) + bytes([0x00, 0x64]) + bytes(15)
    decoded = decode_frame_fields(body, "rsp_0xc1_group0", glossary)
    val = decoded.get("power_on_days", {}).get("value")
    check("power_on_days = 100", val == 100, f"got {val}")


def test_total_worked_days_be16():
    print("\n14. total_worked_days BE16 from crafted body[9..10]=01 2C")
    glossary = load_glossary()
    # body[9..10] = 0x01 0x2C → BE16 = 0x012C = 300
    body = bytes(9) + bytes([0x01, 0x2C]) + bytes(10)
    decoded = decode_frame_fields(body, "rsp_0xc1_group0", glossary)
    val = decoded.get("total_worked_days", {}).get("value")
    check("total_worked_days = 300", val == 300, f"got {val}")


def test_current_session_days_be16():
    print("\n15. current_session_days BE16 from crafted body[14..15]=00 0F")
    glossary = load_glossary()
    # body[14..15] = 0x00 0x0F → BE16 = 0x000F = 15
    body = bytes(14) + bytes([0x00, 0x0F]) + bytes(5)
    decoded = decode_frame_fields(body, "rsp_0xc1_group0", glossary)
    val = decoded.get("current_session_days", {}).get("value")
    check("current_session_days = 15", val == 15, f"got {val}")


# ── Test 16: cross-validation against Python stdlib (64 random patterns) ─


def test_cross_validate_against_stdlib():
    """For 64 random byte patterns, decode via apply_encoding AND
    int.from_bytes from Python stdlib. Both must agree.

    int.from_bytes is the canonical reference for byte-order arithmetic
    (used by every Python program since 2014). Matching it is functionally
    equivalent to cross-validating against any other endian implementation.
    """
    print("\n16. Cross-validate against Python stdlib (64 random patterns)")
    glossary = load_glossary()
    encs = glossary["encodings"]
    rng = random.Random(0xC0FFEE)  # deterministic seed

    mismatches_le16 = 0
    mismatches_be16 = 0
    mismatches_le24 = 0
    mismatches_be24 = 0
    n_each = 16

    for _ in range(n_each):
        pair = bytes([rng.randint(0, 255), rng.randint(0, 255)])
        body = bytes(8) + pair + bytes(8)
        offset = 8
        if apply_encoding(0, "uint16_le", encs, body=body, offset=offset) != int.from_bytes(pair, "little"):
            mismatches_le16 += 1
        if apply_encoding(0, "uint16_be", encs, body=body, offset=offset) != int.from_bytes(pair, "big"):
            mismatches_be16 += 1

    for _ in range(n_each):
        triple = bytes([rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)])
        body = bytes(8) + triple + bytes(8)
        offset = 8
        if apply_encoding(0, "uint24_le", encs, body=body, offset=offset) != int.from_bytes(triple, "little"):
            mismatches_le24 += 1
        if apply_encoding(0, "uint24_be", encs, body=body, offset=offset) != int.from_bytes(triple, "big"):
            mismatches_be24 += 1

    check(
        f"uint16_le: {n_each} patterns match int.from_bytes(little)",
        mismatches_le16 == 0,
        f"{mismatches_le16} mismatches",
    )
    check(
        f"uint16_be: {n_each} patterns match int.from_bytes(big)", mismatches_be16 == 0, f"{mismatches_be16} mismatches"
    )
    check(
        f"uint24_le: {n_each} patterns match int.from_bytes(little)",
        mismatches_le24 == 0,
        f"{mismatches_le24} mismatches",
    )
    check(
        f"uint24_be: {n_each} patterns match int.from_bytes(big)", mismatches_be24 == 0, f"{mismatches_be24} mismatches"
    )


# ── Test 17: existing BCD/linear paths still work (regression) ──────────


def test_existing_bcd_linear_paths_unchanged():
    print("\n17. Existing power_bcd_4 / power_linear_4 paths unaffected")
    glossary = load_glossary()
    encs = glossary["encodings"]
    # Real S15 c1g4 bytes for total_power_kwh: 00 01 19 DD
    body = b"\x00\x01\x19\xdd"
    linear = apply_encoding(0, "power_linear_4", encs, body=body, offset=0)
    check("power_linear_4 = 721.57", abs(linear - 721.57) < 0.01, f"got {linear}")
    # Same bytes via BCD: bcd(0)*10000 + bcd(1)*100 + bcd(0x19) + bcd(0xDD)/100
    #                  = 0 + 100 + 19 + 1.43 = 120.43
    bcd = apply_encoding(0, "power_bcd_4", encs, body=body, offset=0)
    check("power_bcd_4 = 120.43", abs(bcd - 120.43) < 0.01, f"got {bcd}")


# ── Main ──────────────────────────────────────────────────────────────────


def main():
    test_read_uint_pure()
    test_read_at_offset()
    test_read_past_end_raises()
    test_apply_encoding_dispatches_uint16_le()
    test_apply_encoding_dispatches_uint16_be()
    test_glossary_encodings_loadable()
    test_indoor_fan_runtime_from_real_frame()
    test_compressor_cumul_hours_from_real_frame()
    test_torque_compensation_angle_from_real_frame()
    test_power_on_days_be16()
    test_total_worked_days_be16()
    test_current_session_days_be16()
    test_cross_validate_against_stdlib()
    test_existing_bcd_linear_paths_unchanged()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {passed + failed} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
