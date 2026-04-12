#!/usr/bin/env python3
"""Bulk B0/B1 property field tests — gates the Tier 1-4 expansion.

Validates the 36 new B0/B1 property fields wired from
mill1000/midea-msmart Finding 9 (Property Protocol §3.5):

  Tier 1 — single-byte uint8 / bool reads (21 fields, 21 properties)
  Tier 2 — single-byte temperatures using temp_offset50_half (2 fields)
  Tier 3 — 2-byte composites: 2 sub-fields per property (10 fields, 5 props)
  Tier 4 — multi-byte numerics using uint16_le / uint24_le (3 fields)

Each tier feeds a synthetic B1 frame through decode_frame_fields and
asserts the decoded value matches the byte we put in. For Tier 4 the
multi-byte values are also cross-validated against int.from_bytes
(canonical reference for byte order — same approach as
test_multibyte_encoding.py).

For settable Tier 1-3 fields we additionally assert the B0 set
command round-trips: build_b0_command_body → parse_b0b1_tlv recovers
the same value.

Run: python tools-serial/tests/test_b0b1_bulk_properties.py
"""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]

from blaueis.core.command import build_b0_command_body  # noqa: E402
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


# ── Synthetic B1 frame builder (same shape as test_b0b1_property.py) ─


def build_b1_body(properties):
    """Build a B1 body from a list of (prop_id_lo, prop_id_hi, data) tuples."""
    body = bytearray([0xB1, len(properties)])
    for lo, hi, data in properties:
        body.extend([lo, hi, 0x00, len(data)])
        body.extend(data)
    return bytes(body)


def decode_one(glossary, prop_lo, prop_hi, data):
    """Build a B1 frame with a single property and return the decoded fields dict."""
    body = build_b1_body([(prop_lo, prop_hi, bytes(data))])
    return decode_frame_fields(body, "rsp_0xb1", glossary)


def parse_b0_set_tlv(body):
    """Parse a B0 SET command body (3-byte TLV header: id_lo, id_hi, data_len).

    Mirrors the helper in test_b0b1_property.py — TX uses a 3-byte
    header (no data_type byte), while parse_b0b1_tlv handles the
    4-byte RX header. The two parsers are NOT interchangeable.
    """
    records = []
    pos = 2
    count = body[1]
    while pos < len(body) and len(records) < count:
        if pos + 3 > len(body):
            break
        prop_lo = body[pos]
        prop_hi = body[pos + 1]
        data_len = body[pos + 2]
        if pos + 3 + data_len > len(body):
            break
        data = list(body[pos + 3 : pos + 3 + data_len])
        records.append(
            {
                "property_id": f"0x{prop_lo:02X},0x{prop_hi:02X}",
                "data_len": data_len,
                "data": data,
            }
        )
        pos += 3 + data_len
    return records


# ── Tier 1: single-byte fields ────────────────────────────────────


# (field_name, prop_id_lo, prop_id_hi, raw_byte, expected_decoded_value)
TIER1_CASES = [
    ("no_wind_sense", 0x18, 0x00, 0x01, True),
    ("cool_hot_sense", 0x21, 0x00, 0x01, True),
    ("auto_prevent_straight_wind", 0x26, 0x02, 0x01, True),
    ("intelligent_wind", 0x34, 0x00, 0x01, True),
    ("child_prevent_cold_wind", 0x3A, 0x00, 0x01, True),
    ("mode_query_value", 0x41, 0x00, 0x03, 3),
    ("high_temperature_monitor", 0x47, 0x00, 0x01, True),
    ("indoor_humidity", 0x15, 0x00, 0x37, 0x37),  # 55 %
    ("little_angel", 0x1B, 0x02, 0x01, True),
    ("security", 0x29, 0x00, 0x02, 2),
    ("intelligent_control", 0x31, 0x00, 0x01, True),
    ("face_register", 0x44, 0x00, 0x01, True),
    ("even_wind", 0x4E, 0x00, 0x01, True),
    ("single_tuyere", 0x4F, 0x00, 0x01, True),
    ("prevent_straight_wind_lr", 0x58, 0x00, 0x01, True),
    ("has_icheck", 0x91, 0x00, 0x01, True),
    ("cvp", 0x98, 0x00, 0x01, True),
    ("new_wind_sense", 0xAA, 0x00, 0x01, True),
    ("comfort", 0xAD, 0x00, 0x01, True),
    ("pre_cool_hot", 0x01, 0x02, 0x01, True),
    ("body_check", 0x34, 0x02, 0x01, True),
]


def test_tier1_single_byte():
    print("\n1. Tier 1 — single-byte property reads")
    glossary = load_glossary()
    for name, lo, hi, raw, expected in TIER1_CASES:
        decoded = decode_one(glossary, lo, hi, [raw])
        actual = decoded.get(name, {}).get("value")
        check(
            f"{name} (prop 0x{lo:02X},0x{hi:02X}, raw=0x{raw:02X}) → {expected!r}",
            actual == expected,
            f"got {actual!r}",
        )

    # Also probe the false / 0 case for one bool to confirm it isn't always-True
    decoded = decode_one(glossary, 0x18, 0x00, [0x00])
    check(
        "no_wind_sense raw=0x00 → False",
        decoded.get("no_wind_sense", {}).get("value") is False,
        f"got {decoded.get('no_wind_sense')}",
    )


# ── Tier 2: temperature properties (raw - 50) / 2 ─────────────────


def test_tier2_temperature():
    print("\n2. Tier 2 — temperature properties (temp_offset50_half)")
    glossary = load_glossary()

    # mito_cool_temp: raw 78 → (78-50)/2 = 14.0 °C
    decoded = decode_one(glossary, 0x8D, 0x00, [78])
    actual = decoded.get("mito_cool_temp", {}).get("value")
    check(
        "mito_cool_temp raw=78 → 14.0 °C",
        actual == 14.0,
        f"got {actual}",
    )

    # mito_cool_temp: raw 100 → 25.0 °C
    decoded = decode_one(glossary, 0x8D, 0x00, [100])
    actual = decoded.get("mito_cool_temp", {}).get("value")
    check(
        "mito_cool_temp raw=100 → 25.0 °C",
        actual == 25.0,
        f"got {actual}",
    )

    # mito_heat_temp: raw 90 → 20.0 °C
    decoded = decode_one(glossary, 0x8E, 0x00, [90])
    actual = decoded.get("mito_heat_temp", {}).get("value")
    check(
        "mito_heat_temp raw=90 → 20.0 °C",
        actual == 20.0,
        f"got {actual}",
    )

    # Half-step: raw 81 → 15.5 °C
    decoded = decode_one(glossary, 0x8E, 0x00, [81])
    actual = decoded.get("mito_heat_temp", {}).get("value")
    check(
        "mito_heat_temp raw=81 → 15.5 °C",
        actual == 15.5,
        f"got {actual}",
    )


# ── Tier 3: 2-byte composite properties ───────────────────────────


# (prop_lo, prop_hi, [data], expected = {field: value, ...})
TIER3_CASES = [
    (0x4C, 0x00, [0x05, 0x02], {"extreme_wind_value": 5, "extreme_wind_level": 2}),
    (0x59, 0x00, [0x07, 0x03], {"wind_around_value": 7, "wind_around_ud_mode": 3}),
    (0x8F, 0x00, [0x1E, 0x02], {"dr_time_minutes": 30, "dr_time_hours": 2}),
    (0xE3, 0x00, [0x05, 0x01], {"ieco_number": 5, "ieco_switch": True}),
    (0x27, 0x02, [0x01, 0x42], {"remote_control_lock": True, "remote_control_value": 0x42}),
]


def test_tier3_composites():
    print("\n3. Tier 3 — 2-byte composite properties")
    glossary = load_glossary()
    for lo, hi, data, expected in TIER3_CASES:
        decoded = decode_one(glossary, lo, hi, data)
        for fname, exp_val in expected.items():
            actual = decoded.get(fname, {}).get("value")
            check(
                f"prop 0x{lo:02X},0x{hi:02X} {data} → {fname} == {exp_val!r}",
                actual == exp_val,
                f"got {actual!r}",
            )

    # Negative half: ieco_switch off → False (lower bit only)
    decoded = decode_one(glossary, 0xE3, 0x00, [0x09, 0x00])
    check(
        "ieco_switch raw=[0x09, 0x00] → False (and ieco_number == 9)",
        decoded.get("ieco_switch", {}).get("value") is False and decoded.get("ieco_number", {}).get("value") == 9,
        f"got {decoded}",
    )


# ── Tier 4: multi-byte numerics ──────────────────────────────────


def test_tier4_multibyte():
    print("\n4. Tier 4 — multi-byte numeric properties")
    glossary = load_glossary()

    # pm25_value: uint16_le, [0xE8, 0x03] → 1000 µg/m³
    raw = bytes([0xE8, 0x03])
    expected_pm25 = int.from_bytes(raw, "little")
    decoded = decode_one(glossary, 0x0B, 0x02, raw)
    actual = decoded.get("pm25_value", {}).get("value")
    check(
        f"pm25_value uint16_le {list(raw)} → {expected_pm25}",
        actual == expected_pm25 == 1000,
        f"got {actual}",
    )

    # pm25_value: max 0xFFFF
    raw = bytes([0xFF, 0xFF])
    expected = int.from_bytes(raw, "little")
    decoded = decode_one(glossary, 0x0B, 0x02, raw)
    check(
        f"pm25_value uint16_le {list(raw)} → {expected}",
        decoded.get("pm25_value", {}).get("value") == expected == 65535,
        f"got {decoded.get('pm25_value')}",
    )

    # operating_time_total: uint24_le, [0x40, 0xE2, 0x01] → 0x01E240 = 123456
    raw = bytes([0x40, 0xE2, 0x01])
    expected = int.from_bytes(raw, "little")
    decoded = decode_one(glossary, 0x28, 0x02, raw)
    actual = decoded.get("operating_time_total", {}).get("value")
    check(
        f"operating_time_total uint24_le {list(raw)} → {expected}",
        actual == expected == 123456,
        f"got {actual}",
    )

    # operating_time_total: max 0xFFFFFF
    raw = bytes([0xFF, 0xFF, 0xFF])
    expected = int.from_bytes(raw, "little")
    decoded = decode_one(glossary, 0x28, 0x02, raw)
    check(
        f"operating_time_total uint24_le {list(raw)} → {expected}",
        decoded.get("operating_time_total", {}).get("value") == expected == 16777215,
        f"got {decoded.get('operating_time_total')}",
    )

    # prevent_super_cool: single byte read at offset 0
    decoded = decode_one(glossary, 0x49, 0x00, [0x42])
    actual = decoded.get("prevent_super_cool", {}).get("value")
    check(
        "prevent_super_cool raw=0x42 → 0x42",
        actual == 0x42,
        f"got {actual}",
    )


# ── Tier 5: B0 set command round-trip ────────────────────────────


# Settable fields chosen to exercise each tier's encoding path.
# (field_name, set_value, expected_round_trip_value)
ROUND_TRIP_CASES = [
    # Tier 1 — bool and uint8
    ("intelligent_wind", True, True),
    ("comfort", False, False),
    ("security", 3, 3),
    # Tier 2 — temperature
    ("mito_cool_temp", 22.0, 22.0),
    ("mito_heat_temp", 18.5, 18.5),
    # Tier 3 — composite (one field at a time; the other sub-field
    #          will encode as 0)
    ("extreme_wind_value", 5, 5),
    ("ieco_switch", True, True),
    # Tier 4 — single-byte read
    ("prevent_super_cool", 0x10, 0x10),
]


def test_tier5_round_trip():
    print("\n5. B0 set command round-trip (Tier 1-3 settable fields)")
    glossary = load_glossary()
    status = {"fields": {}}
    for name, value, expected in ROUND_TRIP_CASES:
        result = build_b0_command_body(
            status,
            {name: value},
            glossary,
            skip_preflight=True,
        )
        body = result["body"]
        if body is None:
            check(
                f"{name} round-trip — build_b0_command_body produced a body",
                False,
                f"preflight blocked: {result.get('preflight')}",
            )
            continue

        # Convert TX (B0 set, 3-byte header) into RX (B1 response,
        # 4-byte header) so decode_frame_fields can consume it.
        tx_records = parse_b0_set_tlv(bytes(body))
        rx_body = build_b1_body(
            [
                (
                    int(rec["property_id"].split(",")[0], 16),
                    int(rec["property_id"].split(",")[1], 16),
                    bytes(rec["data"]),
                )
                for rec in tx_records
            ]
        )
        decoded = decode_frame_fields(rx_body, "rsp_0xb1", glossary)
        actual = decoded.get(name, {}).get("value")
        check(
            f"{name} round-trip set→parse → {expected!r}",
            actual == expected,
            f"got {actual!r}",
        )


# ── Tier 6: invariants on the new fields ─────────────────────────


def test_field_invariants():
    print("\n6. Field invariants — bulk-added properties")
    glossary = load_glossary()
    fields = glossary["fields"]
    cat_index = {}
    for cat in ("control", "sensor"):
        for n, f in fields[cat].items():
            if isinstance(f, dict) and "description" in f:
                cat_index[n] = (cat, f)

    new_fields = (
        [c[0] for c in TIER1_CASES]
        + ["mito_cool_temp", "mito_heat_temp"]
        + [
            "extreme_wind_value",
            "extreme_wind_level",
            "wind_around_value",
            "wind_around_ud_mode",
            "dr_time_minutes",
            "dr_time_hours",
            "ieco_number",
            "ieco_switch",
            "remote_control_lock",
            "remote_control_value",
        ]
        + ["pm25_value", "operating_time_total", "prevent_super_cool"]
    )

    check(
        f"all 36 bulk-added fields exist in glossary (got {sum(1 for n in new_fields if n in cat_index)}/36)",
        all(n in cat_index for n in new_fields),
        f"missing: {[n for n in new_fields if n not in cat_index]}",
    )

    # Every field starts at feature_available: capability (cap-gated;
    # the quirks system promotes them per-device).
    not_capability = [n for n in new_fields if cat_index[n][1].get("feature_available") != "capability"]
    check(
        "all bulk-added fields default to feature_available: capability",
        len(not_capability) == 0,
        f"non-capability: {not_capability}",
    )

    # Sensor-categorized fields must NOT have a cmd_0xb0 protocol entry
    # (no write path → sensor; this is exactly what the category
    # boundary test enforces, but we double-check for the new fields).
    sensors_with_set = [
        n for n in new_fields if cat_index[n][0] == "sensor" and "cmd_0xb0" in cat_index[n][1].get("protocols", {})
    ]
    check(
        "no sensor-categorized bulk field has a cmd_0xb0 set entry",
        len(sensors_with_set) == 0,
        f"violators: {sensors_with_set}",
    )

    # Every field cites mill1000/midea-msmart Finding 9 in sources.
    bad_sources = []
    for n in new_fields:
        srcs = cat_index[n][1].get("sources", [])
        if not any("mill1000/midea-msmart" in str(s) for s in srcs):
            bad_sources.append(n)
    check(
        "all bulk-added fields cite mill1000/midea-msmart in sources",
        len(bad_sources) == 0,
        f"missing citation: {bad_sources}",
    )


# ── Tier 7: no property_id collisions with pre-existing fields ───


def test_no_collisions():
    print("\n7. Property-id collision audit (bulk fields vs pre-existing)")
    glossary = load_glossary()

    new_pids = {
        "0x18,0x00",
        "0x21,0x00",
        "0x26,0x02",
        "0x34,0x00",
        "0x3A,0x00",
        "0x41,0x00",
        "0x47,0x00",
        "0x15,0x00",
        "0x1B,0x02",
        "0x29,0x00",
        "0x31,0x00",
        "0x44,0x00",
        "0x4E,0x00",
        "0x4F,0x00",
        "0x58,0x00",
        "0x91,0x00",
        "0x98,0x00",
        "0xAA,0x00",
        "0xAD,0x00",
        "0x01,0x02",
        "0x34,0x02",
        "0x8D,0x00",
        "0x8E,0x00",
        "0x4C,0x00",
        "0x59,0x00",
        "0x8F,0x00",
        "0xE3,0x00",
        "0x27,0x02",
        "0x0B,0x02",
        "0x28,0x02",
        "0x49,0x00",
    }

    # Map prop_id → set of fields claiming it
    pid_owners = {}
    for cat in ("control", "sensor"):
        for n, f in glossary["fields"][cat].items():
            if not isinstance(f, dict):
                continue
            for ploc in f.get("protocols", {}).values():
                for step in ploc.get("decode", []):
                    pid = step.get("property_id")
                    if pid is None:
                        continue
                    pid_owners.setdefault(pid, set()).add(n)

    # Bulk-added prop_ids may have multiple owners ONLY if those owners
    # are sub-fields of the same composite (Tier 3). They must NOT
    # collide with previously existing canonical fields.
    composite_owners = {
        "0x4C,0x00": {"extreme_wind_value", "extreme_wind_level"},
        "0x59,0x00": {"wind_around_value", "wind_around_ud_mode"},
        "0x8F,0x00": {"dr_time_minutes", "dr_time_hours"},
        "0xE3,0x00": {"ieco_number", "ieco_switch"},
        "0x27,0x02": {"remote_control_lock", "remote_control_value"},
    }

    for pid in new_pids:
        owners = pid_owners.get(pid, set())
        expected = composite_owners.get(pid)
        if expected is not None:
            check(
                f"composite prop {pid} owned only by {sorted(expected)}",
                owners == expected,
                f"got {sorted(owners)}",
            )
        else:
            check(
                f"prop {pid} has exactly one owner",
                len(owners) == 1,
                f"got {sorted(owners)}",
            )


def main():
    test_tier1_single_byte()
    test_tier2_temperature()
    test_tier3_composites()
    test_tier4_multibyte()
    test_tier5_round_trip()
    test_field_invariants()
    test_no_collisions()

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
