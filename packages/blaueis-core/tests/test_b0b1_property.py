#!/usr/bin/env python3
"""
B0/B1 property-protocol TLV tests.

Validates:
  1. parse_b0b1_tlv — TLV parser with synthetic B1 bodies
  2. decode_frame_fields — property-addressed decode via glossary
  3. build_b0_command_body — encode round-trip
  4. Backward compat — existing anion_ionizer field decodes from B1
  5. Invariant — every property_id in glossary decode steps is well-formed

Run: python tools-serial/tests/test_b0b1_property.py
"""

import os
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]

from blaueis.core.codec import decode_frame_fields, load_glossary, parse_b0b1_tlv  # noqa: E402
from blaueis.core.command import build_b0_command_body  # noqa: E402

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  [PASS] {label}")
        passed += 1
    else:
        print(f"  [FAIL] {label}: {detail}")
        failed += 1


# ── Synthetic B1 frame builders ────────────────────────────────────


def build_b1_body(properties: list[tuple[int, int, bytes]]) -> bytes:
    """Build a B1 body from a list of (prop_id_lo, prop_id_hi, data) tuples."""
    body = bytearray([0xB1, len(properties)])
    for lo, hi, data in properties:
        body.extend([lo, hi, 0x00, len(data)])
        body.extend(data)
    return bytes(body)


# ── Test 1: TLV parser ────────────────────────────────────────────


def test_parse_tlv():
    print("\n1. parse_b0b1_tlv")

    # Single property
    b1 = build_b1_body([(0x4B, 0x00, bytes([0x01, 0x3C, 0x1A]))])
    records = parse_b0b1_tlv(b1)
    check("single record count", len(records) == 1, f"got {len(records)}")
    check("property_id format", records[0]["property_id"] == "0x4B,0x00", f"got {records[0]['property_id']}")
    check("data_len", records[0]["data_len"] == 3, f"got {records[0]['data_len']}")
    check("data content", records[0]["data"] == [0x01, 0x3C, 0x1A], f"got {records[0]['data']}")

    # Two properties
    b1_multi = build_b1_body(
        [
            (0x4B, 0x00, bytes([0x01, 0x3C, 0x1A])),  # fresh_air
            (0x4A, 0x00, bytes([0x01, 0x00, 0x0A, 0x02])),  # aqua_wash
        ]
    )
    records = parse_b0b1_tlv(b1_multi)
    check("two records", len(records) == 2, f"got {len(records)}")
    check("second property_id", records[1]["property_id"] == "0x4A,0x00", f"got {records[1]['property_id']}")
    check("second data", records[1]["data"] == [0x01, 0x00, 0x0A, 0x02], f"got {records[1]['data']}")

    # Empty body (no records)
    b1_empty = bytes([0xB1, 0x00])
    records = parse_b0b1_tlv(b1_empty)
    check("empty body", len(records) == 0, f"got {len(records)}")

    # Truncated body (record count says 2 but only 1 fits)
    b1_trunc = build_b1_body([(0x4B, 0x00, bytes([0x01]))])
    b1_trunc = bytearray(b1_trunc)
    b1_trunc[1] = 2  # lie about record count
    records = parse_b0b1_tlv(bytes(b1_trunc))
    check("truncated body parses available", len(records) == 1, f"got {len(records)}")


# ── Test 2: decode_frame_fields with B1 ────────────────────────────


def test_decode_b1():
    print("\n2. decode_frame_fields with B1 property frames")
    glossary = load_glossary()

    # Fresh air: switch=ON (0x01), fan_speed=60 (0x3C), temp=26 (0x1A)
    b1 = build_b1_body([(0x4B, 0x00, bytes([0x01, 0x3C, 0x1A]))])
    decoded = decode_frame_fields(b1, "rsp_0xb1", glossary)

    check("fresh_air_switch decoded", "fresh_air_switch" in decoded, f"keys: {list(decoded.keys())}")
    if "fresh_air_switch" in decoded:
        check(
            "fresh_air_switch == True",
            decoded["fresh_air_switch"]["value"] is True,
            f"got {decoded['fresh_air_switch']['value']}",
        )
    check("fresh_air_fan_speed decoded", "fresh_air_fan_speed" in decoded)
    if "fresh_air_fan_speed" in decoded:
        check(
            "fresh_air_fan_speed == 60",
            decoded["fresh_air_fan_speed"]["value"] == 0x3C,
            f"got {decoded['fresh_air_fan_speed']['value']}",
        )
    check("fresh_air_temp decoded", "fresh_air_temp" in decoded)
    if "fresh_air_temp" in decoded:
        check(
            "fresh_air_temp == 26",
            decoded["fresh_air_temp"]["value"] == 0x1A,
            f"got {decoded['fresh_air_temp']['value']}",
        )

    # Water washing: manual=1, switch=ON, time=10, stage=2
    b1_ww = build_b1_body([(0x4A, 0x00, bytes([0x01, 0x01, 0x0A, 0x02]))])
    decoded_ww = decode_frame_fields(b1_ww, "rsp_0xb1", glossary)

    check("aqua_wash_switch decoded", "aqua_wash_switch" in decoded_ww)
    if "aqua_wash_switch" in decoded_ww:
        check(
            "aqua_wash_switch == True",
            decoded_ww["aqua_wash_switch"]["value"] is True,
            f"got {decoded_ww['aqua_wash_switch']['value']}",
        )
    check("aqua_wash_time decoded", "aqua_wash_time" in decoded_ww)
    if "aqua_wash_time" in decoded_ww:
        check(
            "aqua_wash_time == 10",
            decoded_ww["aqua_wash_time"]["value"] == 0x0A,
            f"got {decoded_ww['aqua_wash_time']['value']}",
        )
    check("aqua_wash_stage decoded", "aqua_wash_stage" in decoded_ww)
    if "aqua_wash_stage" in decoded_ww:
        check(
            "aqua_wash_stage == 2",
            decoded_ww["aqua_wash_stage"]["value"] == 0x02,
            f"got {decoded_ww['aqua_wash_stage']['value']}",
        )

    # Both properties in one frame
    b1_both = build_b1_body(
        [
            (0x4B, 0x00, bytes([0x01, 0x3C, 0x1A])),
            (0x4A, 0x00, bytes([0x00, 0x00, 0x05, 0x03])),
        ]
    )
    decoded_both = decode_frame_fields(b1_both, "rsp_0xb1", glossary)
    check(
        "both properties decode",
        "fresh_air_switch" in decoded_both and "aqua_wash_stage" in decoded_both,
        f"keys: {sorted(decoded_both.keys())}",
    )

    # Property not in frame → field not in output
    b1_only_ww = build_b1_body([(0x4A, 0x00, bytes([0x00, 0x00, 0x00, 0x00]))])
    decoded_only = decode_frame_fields(b1_only_ww, "rsp_0xb1", glossary)
    check(
        "missing property skipped",
        "fresh_air_switch" not in decoded_only,
        f"unexpectedly found: {[k for k in decoded_only if 'fresh' in k]}",
    )


# ── Test 3: Backward compat — anion_ionizer ───────────────────────


def test_backward_compat():
    print("\n3. Backward compatibility — existing B0 field (anion_ionizer)")
    glossary = load_glossary()

    # B1 response with anion_ionizer ON
    b1 = build_b1_body([(0x1E, 0x02, bytes([0x01]))])
    decoded = decode_frame_fields(b1, "rsp_0xb1", glossary)
    check("anion_ionizer decoded", "anion_ionizer" in decoded, f"keys: {list(decoded.keys())}")
    if "anion_ionizer" in decoded:
        check(
            "anion_ionizer == True",
            decoded["anion_ionizer"]["value"] is True,
            f"got {decoded['anion_ionizer']['value']}",
        )

    # anion_ionizer OFF
    b1_off = build_b1_body([(0x1E, 0x02, bytes([0x00]))])
    decoded_off = decode_frame_fields(b1_off, "rsp_0xb1", glossary)
    if "anion_ionizer" in decoded_off:
        check(
            "anion_ionizer OFF == False",
            decoded_off["anion_ionizer"]["value"] is False,
            f"got {decoded_off['anion_ionizer']['value']}",
        )


# ── B0 SET TLV parser (3-byte header, TX format) ─────────────────


def parse_b0_set_tlv(body: bytes) -> list[dict]:
    """Parse a B0 SET command body (3-byte TLV header: id_lo, id_hi, data_len).

    IMPORTANT: B0 SET uses a DIFFERENT header format than B0/B1 responses.
    SET: [id_lo, id_hi, data_len] = 3-byte header, data at +3.
    Response: [id_lo, id_hi, data_type, data_len] = 4-byte header, data at +4.
    Using parse_b0b1_tlv (RX parser) on TX output will silently misparse!
    """
    records = []
    pos = 2  # skip B0 tag + count
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


# ── Test 4: Encode round-trip ─────────────────────────────────────


def test_encode_roundtrip():
    print("\n4. build_b0_command_body encode round-trip")
    glossary = load_glossary()
    status = {"fields": {}}

    # Encode fresh_air ON + fan_speed 60
    result = build_b0_command_body(
        status,
        {"fresh_air_switch": True, "fresh_air_fan_speed": 60},
        glossary,
    )
    body = result["body"]
    check("B0 tag", body[0] == 0xB0, f"got 0x{body[0]:02X}")
    check("record count >= 1", body[1] >= 1, f"got {body[1]}")

    # Parse back using the TX parser (3-byte header), NOT the RX parser
    records = parse_b0_set_tlv(bytes(body))
    check("round-trip records >= 1", len(records) >= 1, f"got {len(records)}")

    # Find the 0x4B property
    fa_rec = [r for r in records if r["property_id"] == "0x4B,0x00"]
    check("0x4B property present", len(fa_rec) == 1, f"found {len(fa_rec)}")
    if fa_rec:
        data = fa_rec[0]["data"]
        check("switch byte == 1", (data[0] & 0x01) == 1, f"got {data[0]:#04x}")
        check("fan_speed byte == 60", data[1] == 60, f"got {data[1]}")


# ── Test 5: Glossary invariants for property_id ───────────────────


def test_property_id_invariants():
    print("\n5. Glossary invariants — property_id format")
    glossary = load_glossary()
    import re

    pat = re.compile(r"^0x[0-9A-Fa-f]{2},0x[0-9A-Fa-f]{2}$")

    bad = []
    prop_id_count = 0
    for cat in ("control", "sensor"):
        for name, field in glossary["fields"][cat].items():
            if not isinstance(field, dict):
                continue
            for pk, ploc in field.get("protocols", {}).items():
                for step in ploc.get("decode", []):
                    pid = step.get("property_id")
                    if pid is None:
                        continue
                    prop_id_count += 1
                    if not pat.match(pid):
                        bad.append(f"{name}.{pk}: {pid}")

    check(f"all {prop_id_count} property_id values match pattern", len(bad) == 0, f"bad: {bad}")
    check("at least 46 property_id decode steps exist", prop_id_count >= 46, f"got {prop_id_count}")


# ── Test 6: TX/RX format asymmetry guard ──────────────────────────


def test_tx_rx_asymmetry():
    """Guard against the TX/RX TLV header bug.

    B0 SET (TX) uses a 3-byte header: [id_lo, id_hi, data_len].
    B0/B1 Response (RX) uses a 4-byte header: [id_lo, id_hi, type, data_len].
    This test ensures build_b0_command_body produces 3-byte headers and that
    parse_b0b1_tlv correctly expects 4-byte headers.
    """
    print("\n6. TX/RX format asymmetry guard")
    glossary = load_glossary()
    status = {"fields": {}}

    # Build a SET command for a single 1-byte property
    result = build_b0_command_body(
        status,
        {"wind_swing_lr_angle": 50},
        glossary,
    )
    body = result["body"]

    # TX format: B0(1) + count(1) + [id_lo, id_hi, data_len, data] = 6 bytes total
    check("TX frame length == 6 (3-byte header + 1-byte data)", len(body) == 6, f"got {len(body)}: {body.hex(' ')}")

    # Verify exact byte layout matches Lua encoder
    # Lua: bodyBytes = [0x0A, 0x00, 0x01, value]
    check("TX byte 2 = id_lo (0x0A)", body[2] == 0x0A, f"got 0x{body[2]:02X}")
    check("TX byte 3 = id_hi (0x00)", body[3] == 0x00, f"got 0x{body[3]:02X}")
    check("TX byte 4 = data_len (1)", body[4] == 1, f"got {body[4]}")
    check("TX byte 5 = data (50)", body[5] == 50, f"got {body[5]}")

    # Build a synthetic B1 RESPONSE for the same property (4-byte header)
    b1_response = build_b1_body([(0x0A, 0x00, bytes([50]))])
    # B1 format: B1(1) + count(1) + [id_lo, id_hi, type, data_len, data] = 7 bytes
    check(
        "RX frame length == 7 (4-byte header + 1-byte data)",
        len(b1_response) == 7,
        f"got {len(b1_response)}: {b1_response.hex(' ')}",
    )

    # The TX and RX formats MUST differ in length for the same property
    check(
        "TX != RX length (asymmetry is real)",
        len(body) != len(b1_response),
        "DANGER: TX and RX produce same length — asymmetry may be unhandled",
    )

    # Verify parse_b0b1_tlv (RX parser) correctly handles 4-byte headers
    rx_records = parse_b0b1_tlv(b1_response)
    check("RX parser finds record", len(rx_records) == 1, f"got {len(rx_records)}")
    if rx_records:
        check("RX data == [50]", rx_records[0]["data"] == [50], f"got {rx_records[0]['data']}")

    # Verify parse_b0_set_tlv (TX parser) correctly handles 3-byte headers
    tx_records = parse_b0_set_tlv(bytes(body))
    check("TX parser finds record", len(tx_records) == 1, f"got {len(tx_records)}")
    if tx_records:
        check("TX data == [50]", tx_records[0]["data"] == [50], f"got {tx_records[0]['data']}")

    # CRITICAL: using the WRONG parser on the WRONG format must NOT silently succeed
    # (This is the exact bug we had: parse_b0b1_tlv on TX output gave wrong data)
    wrong_parse = parse_b0b1_tlv(bytes(body))
    if wrong_parse:
        wrong_data = wrong_parse[0].get("data", [])
        check(
            "WRONG parser on TX gives wrong data (mismatch detected)",
            wrong_data != [50],
            "DANGER: wrong parser gave correct data — test is not guarding properly",
        )
    else:
        check("WRONG parser on TX gives no records (safe failure)", True)


# ── Main ───────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("B0/B1 property-protocol TLV tests")
    print("=" * 60)

    test_parse_tlv()
    test_decode_b1()
    test_backward_compat()
    test_encode_roundtrip()
    test_property_id_invariants()
    test_tx_rx_asymmetry()

    print(f"\nResults: {passed} passed, {failed} failed / {passed + failed} total")
    print(f"\n{'=' * 60}")
    print(f"OVERALL: {'PASS' if failed == 0 else 'FAIL'}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[2])
    main()
