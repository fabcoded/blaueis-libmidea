#!/usr/bin/env python3
"""Boundary / centre / clamping coverage for fields previously tested
at value=0 only.

Audit motivation: process_tests.yaml had 20 zero-only assertions across
20 distinct fields. Three are now covered by the duration-counter
oracle (power_on_time / total_worked_time / current_session_time;
14 vectors with min/max/centre). Seven are excluded "dead sensor"
fields per docs/disabled_fields.md §1 and won't surface anything
regardless of test coverage. The remaining 10 fields had ONLY a
zero-byte input exercising the decoder — this file extends each with:

  * 1 min boundary  (all-zero, restated for completeness)
  * 1 max boundary  (uint8: 0xFF, uint16: 0xFFFF — proves no truncation)
  * 1 approximate centre  (uint8: 0x80, uint16: 0x8000)
  * 1+ edge / asymmetry probe  (low/high byte split for LE16,
                                bit-0-only for bools, alternating bit
                                patterns for bitmasks)

For ``defrost_status`` (the one field with a protocol-justified value
range — 0..3 enum), an explicit out-of-range case (0xFF) is added.
With the new ``range: [0, 3]`` declaration in glossary, the codec's
post-decode range gate emits ``suppression='out_of_range'`` and value
becomes ``None``.

Oracle source per case:
  - Single-byte u8 fields: glossary decode → raw byte == value.
  - uint16_le field: ``int.from_bytes(bytes, 'little')`` is the
    Python stdlib reference.
  - bool from bit 0: literal mask ``raw & 0x01``.
  - enum out-of-range: codec range gate (codec.py:414-425).

Run::
    python tests/test_field_boundary_coverage.py
"""

import sys
from pathlib import Path

from blaueis.core.codec import decode_frame_fields, load_glossary  # noqa: E402

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  [PASS] {label}")
        passed += 1
    else:
        print(f"  [FAIL] {label}: {detail}")
        failed += 1


# ── Body builders ────────────────────────────────────────────────────


def build_c1_body(group: int, payload: dict, length: int = 21) -> bytes:
    """Build a synthetic rsp_0xc1_groupN body. ``payload`` maps byte
    offset to value. Header bytes [0..3] are pre-filled per the
    identify_frame contract: 0xC1, 0x21, 0x01, group_low_nibble.
    """
    body = bytearray(length)
    body[0] = 0xC1
    body[1] = 0x21
    body[2] = 0x01
    body[3] = group & 0x0F
    for off, val in payload.items():
        body[off] = val & 0xFF
    return bytes(body)


def build_b1_body(prop_lo: int, prop_hi: int, data) -> bytes:
    """Build a synthetic rsp_0xb1 body with one property TLV (4-byte
    header: prop_lo, prop_hi, data_type=0, data_len)."""
    body = bytearray([0xB1, 1])
    body.extend([prop_lo, prop_hi, 0x00, len(data)])
    body.extend(data)
    return bytes(body)


# ── Case registry ────────────────────────────────────────────────────
#
# Each entry: (field_name, frame_protocol_key, body_builder_fn, cases)
#   - body_builder_fn(raw_bytes_or_int) -> bytes
#   - cases: list of (label, raw_input, expected_value_or_special)
#     special: ("__suppression__", suppression_kind) for clamp tests


def c0(offset):
    """rsp_0xc0 body — 25 bytes; we pre-fill body[0]=0xC0, then the test
    sets the field's offset. Other offsets stay 0; some adjacent fields
    may flag as sentinel-suppressed but we only assert on the target."""
    def builder(raw):
        body = bytearray(25)
        body[0] = 0xC0
        body[offset] = raw & 0xFF
        return bytes(body)
    return builder


def c1g1(offset):
    return lambda raw: build_c1_body(1, {offset: raw}, length=21)


def c1g2(offset):
    return lambda raw: build_c1_body(2, {offset: raw}, length=21)


def c1g3(offset):
    return lambda raw: build_c1_body(3, {offset: raw}, length=21)


def c1g5(offset):
    return lambda raw: build_c1_body(5, {offset: raw}, length=21)


def c1g6(offset):
    return lambda raw: build_c1_body(6, {offset: raw}, length=21)


def c1g6_le16(offset):
    """Multi-byte LE16: payload spans offset and offset+1."""
    def builder(raw):
        body = build_c1_body(6, {}, length=21)
        body = bytearray(body)
        body[offset] = raw & 0xFF
        body[offset + 1] = (raw >> 8) & 0xFF
        return bytes(body)
    return builder


def b1_one_byte(prop_lo, prop_hi):
    """B1 property frame with a single data byte at data[0]."""
    return lambda raw: build_b1_body(prop_lo, prop_hi, [raw & 0xFF])


def b1_0x39(raw):
    """self_clean: B1 property 0x39,0x00, data byte 0."""
    return build_b1_body(0x39, 0x00, [raw & 0xFF])


SUPPR = "__suppression__"


CASES = [
    # ── defrost_status: enum 0..3 with range gate ────────────────
    ("defrost_status", "rsp_0xc1_group5", c1g5(10), [
        ("min (inactive)",     0x00, 0),
        ("max valid (ending)", 0x03, 3),
        ("centre (defrosting)", 0x02, 2),
        ("edge (starting)",    0x01, 1),
        ("clamp 0xFF",         0xFF, (SUPPR, "out_of_range")),
    ]),

    # ── ad_calibration_voltage: u8 raw (×16 doc-only scaling) ────
    ("ad_calibration_voltage", "rsp_0xc1_group6", c1g6(18), [
        ("min",         0x00, 0),
        ("max",         0xFF, 255),
        ("centre",      0x80, 128),
        ("edge 0x42",   0x42, 66),
    ]),

    # ── pfc_peak_current: u8 ─────────────────────────────────────
    ("pfc_peak_current", "rsp_0xc1_group6", c1g6(13), [
        ("min",         0x00, 0),
        ("max",         0xFF, 255),
        ("centre",      0x80, 128),
        ("edge 0x42",   0x42, 66),
    ]),

    # ── torque_compensation_value: u8 raw (×8 doc-only scaling) ──
    ("torque_compensation_value", "rsp_0xc1_group6", c1g6(17), [
        ("min",         0x00, 0),
        ("max",         0xFF, 255),
        ("centre",      0x80, 128),
        ("edge 0x10",   0x10, 16),
    ]),

    # ── total_error_count: u8 counter ────────────────────────────
    ("total_error_count", "rsp_0xc1_group6", c1g6(7), [
        ("min",         0x00, 0),
        ("max",         0xFF, 255),
        ("centre",      0x80, 128),
        ("edge 0x01",   0x01, 1),
    ]),

    # ── indoor_fault_flags_1: u8 bitmask ─────────────────────────
    ("indoor_fault_flags_1", "rsp_0xc1_group2", c1g2(6), [
        ("min (no flags)",       0x00, 0),
        ("max (all flags)",      0xFF, 255),
        ("alternating 0x55",     0x55, 85),
        ("high bit only 0x80",   0x80, 128),
    ]),

    # ── indoor_fault_flags_2: u8 bitmask ─────────────────────────
    ("indoor_fault_flags_2", "rsp_0xc1_group2", c1g2(7), [
        ("min",                  0x00, 0),
        ("max",                  0xFF, 255),
        ("alternating 0xAA",     0xAA, 170),
        ("low bit only 0x01",    0x01, 1),
    ]),

    # ── indoor_fault_flags_3: u8 bitmask ─────────────────────────
    ("indoor_fault_flags_3", "rsp_0xc1_group2", c1g2(8), [
        ("min",                  0x00, 0),
        ("max",                  0xFF, 255),
        ("low nibble 0x0F",      0x0F, 15),
        ("high nibble 0xF0",     0xF0, 240),
    ]),

    # ── torque_compensation_angle: u16 LE — also LE byte-order regression ─
    ("torque_compensation_angle", "rsp_0xc1_group6", c1g6_le16(15), [
        ("min",                  0x0000, 0),
        ("max",                  0xFFFF, 65535),
        ("centre",               0x8000, 32768),
        ("low byte only 0x00FF", 0x00FF, 255),
        ("high byte only 0xFF00", 0xFF00, 65280),
    ]),

    # ── self_clean: bool from bit 0 of B1 prop 0x39,0x00 ─────────
    ("self_clean", "rsp_0xb1", b1_0x39, [
        ("off (0x00)",                0x00, False),
        ("on (0x01)",                 0x01, True),
        ("bit 0 clear, bit 1 set",    0x02, False),
        ("all bits set 0xFF",         0xFF, True),
    ]),

    # ── error_code: raw-pass-through diagnostic counter ─────────
    # No range gate — community precedent (midea_ac_lan and other
    # open-source Midea LAN libs) passes the byte through unsuppressed
    # so future firmware codes surface in HA rather than being silently
    # dropped. See research in commit message for the analysis.
    ("error_code", "rsp_0xc0", c0(16), [
        ("no error (0)",         0x00, 0),
        ("max documented (33)",  0x21, 33),
        ("centre (16)",          0x10, 16),
        ("edge (1: comm fail)",  0x01, 1),
        ("undocumented (0xFF)",  0xFF, 255),
    ]),

    # ── Track 1A: encoded sensor fields ─────────────────────────
    # Each field's encoding is applied by the codec; expected values
    # come from manually applying the formula at known boundary inputs.
    # Sentinel-bearing fields (indoor_/outdoor_temperature with
    # sentinel_values=[0, 255]) replace min/max boundary cases with
    # sentinel-suppression assertions.

    # ── temp_offset50_half: physical = (raw - 50) / 2.0 °C ───────
    ("indoor_temperature", "rsp_0xc0", c0(11), [
        ("sentinel raw=0",       0x00, (SUPPR, "sentinel")),
        ("sentinel raw=255",     0xFF, (SUPPR, "sentinel")),
        ("centre raw=128 → 39C", 0x80, 39.0),
        ("edge raw=100 → 25C",   100,  25.0),
    ]),
    ("outdoor_temperature", "rsp_0xc0", c0(12), [
        ("sentinel raw=0",       0x00, (SUPPR, "sentinel")),
        ("sentinel raw=255",     0xFF, (SUPPR, "sentinel")),
        ("centre raw=128 → 39C", 0x80, 39.0),
        ("edge raw=100 → 25C",   100,  25.0),
    ]),
    ("t3_outdoor_coil_temp", "rsp_0xc1_group1", c1g1(12), [
        ("min raw=0 → -25C",     0x00, -25.0),
        ("max raw=255 → 102.5C", 0xFF, 102.5),
        ("centre raw=128 → 39C", 0x80, 39.0),
        ("edge raw=100 → 25C",   100,  25.0),
    ]),
    ("t4_outdoor_ambient_temp", "rsp_0xc1_group1", c1g1(13), [
        ("min raw=0 → -25C",     0x00, -25.0),
        ("max raw=255 → 102.5C", 0xFF, 102.5),
        ("centre raw=128 → 39C", 0x80, 39.0),
        ("edge raw=100 → 25C",   100,  25.0),
    ]),
    ("mito_cool_temp", "rsp_0xb1", b1_one_byte(0x8D, 0x00), [
        ("min raw=0 → -25C",     0x00, -25.0),
        ("max raw=255 → 102.5C", 0xFF, 102.5),
        ("centre raw=128 → 39C", 0x80, 39.0),
        ("edge raw=92 → 21C",    92,   21.0),
    ]),
    ("mito_heat_temp", "rsp_0xb1", b1_one_byte(0x8E, 0x00), [
        ("min raw=0 → -25C",     0x00, -25.0),
        ("max raw=255 → 102.5C", 0xFF, 102.5),
        ("centre raw=128 → 39C", 0x80, 39.0),
        ("edge raw=110 → 30C",   110,  30.0),
    ]),

    # ── temp_offset30_half: physical = (raw - 30) / 2.0 °C ───────
    ("compensated_setpoint", "rsp_0xc1_group5", c1g5(5), [
        ("min raw=0 → -15C",     0x00, -15.0),
        ("max raw=255 → 112.5C", 0xFF, 112.5),
        ("centre raw=128 → 49C", 0x80, 49.0),
        ("edge raw=80 → 25C",    80,   25.0),
    ]),
    ("t1_indoor_coil", "rsp_0xc1_group1", c1g1(10), [
        ("min raw=0 → -15C",     0x00, -15.0),
        ("max raw=255 → 112.5C", 0xFF, 112.5),
        ("centre raw=128 → 49C", 0x80, 49.0),
        ("edge raw=80 → 25C",    80,   25.0),
    ]),
    ("t2_indoor_temp", "rsp_0xc1_group1", c1g1(11), [
        ("min raw=0 → -15C",     0x00, -15.0),
        ("max raw=255 → 112.5C", 0xFF, 112.5),
        ("centre raw=128 → 49C", 0x80, 49.0),
        ("edge raw=80 → 25C",    80,   25.0),
    ]),

    # ── temp_direct_integer: physical = raw °C ──────────────────
    ("discharge_pipe_temp", "rsp_0xc1_group1", c1g1(14), [
        ("min raw=0 → 0C",       0x00, 0.0),
        ("max raw=255 → 255C",   0xFF, 255.0),
        ("centre raw=128 → 128C", 0x80, 128.0),
        ("edge raw=80 → 80C",    80,   80.0),
    ]),

    # ── voltage_offset60: physical = raw + 60 V ─────────────────
    ("max_bus_voltage", "rsp_0xc1_group5", c1g5(17), [
        ("min raw=0 → 60V",      0x00, 60),
        ("max raw=255 → 315V",   0xFF, 315),
        ("centre raw=128 → 188V", 0x80, 188),
        ("edge raw=180 → 240V",  180,  240),
    ]),
    ("min_bus_voltage", "rsp_0xc1_group5", c1g5(18), [
        ("min raw=0 → 60V",      0x00, 60),
        ("max raw=255 → 315V",   0xFF, 315),
        ("centre raw=128 → 188V", 0x80, 188),
        ("edge raw=180 → 240V",  180,  240),
    ]),

    # ── eev_steps: physical = raw * 8 steps ──────────────────────
    ("eev_position", "rsp_0xc1_group3", c1g3(11), [
        ("min raw=0 → 0 steps",       0x00, 0),
        ("max raw=255 → 2040 steps",  0xFF, 2040),
        ("centre raw=128 → 1024",     0x80, 1024),
        ("edge raw=50 → 400 steps",   50,   400),
    ]),
    ("eev_target_angle", "rsp_0xc1_group5", c1g5(9), [
        ("min raw=0 → 0 steps",       0x00, 0),
        ("max raw=255 → 2040 steps",  0xFF, 2040),
        ("centre raw=128 → 1024",     0x80, 1024),
        ("edge raw=50 → 400 steps",   50,   400),
    ]),
]


def run_cases():
    glossary = load_glossary()
    for field, protocol_key, builder, cases in CASES:
        print(f"\n{field} ({protocol_key}):")
        for label, raw, expected in cases:
            body = builder(raw)
            decoded = decode_frame_fields(body, protocol_key, glossary, cap_records=None)
            entry = decoded.get(field, {})
            actual = entry.get("value")
            suppression = entry.get("suppression")

            if isinstance(expected, tuple) and expected[0] == SUPPR:
                want_suppr = expected[1]
                check(
                    f"  raw=0x{raw:0{4 if 'angle' in field else 2}X} {label} → suppression={want_suppr!r}",
                    suppression == want_suppr and actual is None,
                    f"value={actual!r} suppression={suppression!r}",
                )
            else:
                check(
                    f"  raw=0x{raw:0{4 if 'angle' in field else 2}X} {label} → {expected!r}",
                    actual == expected and suppression is None,
                    f"value={actual!r} suppression={suppression!r}",
                )


# ── Pytest entry-point ───────────────────────────────────────────────


def test_field_boundary_coverage():
    """Pytest discoverable: same logic as ``run_cases()``, expressed
    as a single assertion. Failures list every diverging case."""
    glossary = load_glossary()
    failures: list[str] = []
    for field, protocol_key, builder, cases in CASES:
        for label, raw, expected in cases:
            body = builder(raw)
            decoded = decode_frame_fields(body, protocol_key, glossary, cap_records=None)
            entry = decoded.get(field, {})
            actual = entry.get("value")
            suppression = entry.get("suppression")

            if isinstance(expected, tuple) and expected[0] == SUPPR:
                want_suppr = expected[1]
                if not (suppression == want_suppr and actual is None):
                    failures.append(
                        f"{field}/{label}: want suppression={want_suppr!r}, "
                        f"got value={actual!r} suppression={suppression!r}"
                    )
            else:
                if actual != expected or suppression is not None:
                    failures.append(
                        f"{field}/{label}: want {expected!r}, "
                        f"got value={actual!r} suppression={suppression!r}"
                    )

    assert not failures, "\n  " + "\n  ".join(failures)


# ── Sanity: cross-validate the LE16 case against int.from_bytes ──────


def test_torque_compensation_angle_le16_against_stdlib():
    """For each LE16 case in the registry, the expected value must
    match ``int.from_bytes(body[15:17], 'little')`` — Python stdlib
    is the canonical byte-order oracle. If this drifts from the
    declared expected, the test data is wrong, not the codec."""
    failures = []
    for field, _proto, _builder, cases in CASES:
        if field != "torque_compensation_angle":
            continue
        for label, raw, expected in cases:
            lo = raw & 0xFF
            hi = (raw >> 8) & 0xFF
            stdlib = int.from_bytes(bytes([lo, hi]), "little")
            if stdlib != expected:
                failures.append(f"{label}: stdlib={stdlib} expected={expected}")
    assert not failures, "\n  " + "\n  ".join(failures)


def main():
    print("=== field boundary / clamping coverage ===")
    run_cases()
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
