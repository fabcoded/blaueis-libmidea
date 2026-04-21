#!/usr/bin/env python3
"""Tests for midea_frame.py — CRC, checksum, frame build/parse.

Tests against real captured frames from XtremeSaveBlue Sessions 1-11.

Usage:
    python test_frame.py
"""

import sys

# Add gateway/ to path for imports
from blaueis.core.frame import (
    CRC8_TABLE,
    FrameError,
    build_display_toggle_frame,
    build_frame,
    build_network_init,
    build_network_status_response,
    build_sn_query,
    crc8,
    parse_frame,
    validate_frame,
)


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
        else:
            failed += 1
            print(f"  [FAIL] {name}: {detail}")

    # ── CRC-8 tests ──────────────────────────────────────────────
    # Simpler test: CRC of empty = 0
    check("crc8 empty", crc8(b"") == 0)
    # CRC of single byte
    check("crc8 single 0x41", crc8(b"\x41") == CRC8_TABLE[0x41])

    # ── Frame build + parse round-trip ────────────────────────────
    # Build a simple frame, parse it back
    test_body = bytes([0x41, 0x81, 0x00, 0xFF])
    frame = build_frame(test_body, msg_type=0x03, appliance=0xAC)

    check("frame starts with AA", frame[0] == 0xAA)
    check("frame appliance is AC", frame[2] == 0xAC)
    check("frame msg_type is 03", frame[9] == 0x03)
    check("frame sync = len ^ type", frame[3] == (frame[1] ^ frame[2]))

    parsed = parse_frame(frame)
    check("round-trip msg_type", parsed["msg_type"] == 0x03)
    check("round-trip body", parsed["body"] == test_body)
    check("round-trip crc_ok", parsed["crc_ok"])
    check("round-trip checksum_ok", parsed["checksum_ok"])
    check("round-trip appliance", parsed["appliance"] == 0xAC)

    # ── Validate catches corruption ──────────────────────────────
    corrupted = bytearray(frame)
    corrupted[11] ^= 0xFF  # flip a body byte
    parsed_bad = parse_frame(bytes(corrupted))
    check("corrupted crc fails", not parsed_bad["crc_ok"])

    try:
        validate_frame(bytes(corrupted))
        check("validate raises on bad CRC", False, "no exception raised")
    except FrameError:
        check("validate raises on bad CRC", True)

    # ── Frame too short ──────────────────────────────────────────
    try:
        parse_frame(bytes([0xAA, 0x05]))
        check("short frame raises", False, "no exception")
    except FrameError:
        check("short frame raises", True)

    # ── Bad start byte ───────────────────────────────────────────
    try:
        parse_frame(bytes([0x55, 0x0D]) + bytes(11))
        check("bad start byte raises", False, "no exception")
    except FrameError:
        check("bad start byte raises", True)

    # ── Convenience builders ─────────────────────────────────────
    sn = build_sn_query()
    sn_parsed = validate_frame(sn)
    check("SN query msg_type=0x07", sn_parsed["msg_type"] == 0x07)
    check("SN query appliance=0xFF", sn_parsed["appliance"] == 0xFF)
    check("SN query body[0]=0x00", sn_parsed["body"][0] == 0x00)

    net_init = build_network_init(ip=(192, 168, 1, 100))
    ni_parsed = validate_frame(net_init)
    check("net init msg_type=0x0D", ni_parsed["msg_type"] == 0x0D)
    # IP little-endian: 192.168.1.100 → body[4]=100, body[5]=1, body[6]=168, body[7]=192
    check("net init IP LE byte[4]=100", ni_parsed["body"][4] == 100)
    check("net init IP LE byte[7]=192", ni_parsed["body"][7] == 192)

    # ── Network status response body ─────────────────────────────
    ns_body = build_network_status_response(ip=(192, 168, 179, 4), signal=4, connected=True)
    check("net status len=20", len(ns_body) == 20)
    check("net status connected", ns_body[0] == 0x01)
    check("net status IP LE", ns_body[3:7] == bytes([4, 179, 168, 192]))
    check("net status signal=4", ns_body[8] == 4)

    # ── Build frame with custom header fields ────────────────────
    custom = build_frame(bytes([0x00]), msg_type=0x07, appliance=0xFF, proto=0x03, sub=0x02, seq=0x05)
    cp = validate_frame(custom)
    check("custom proto=3", cp["proto"] == 0x03)
    check("custom sub=2", cp["sub"] == 0x02)
    check("custom seq=5", cp["seq"] == 0x05)

    # ── Display-toggle frame (cmd_0x41 body[1]=0x61) ─────────────
    dt = build_display_toggle_frame()
    dt_parsed = validate_frame(dt)
    check("display-toggle msg_type=0x03", dt_parsed["msg_type"] == 0x03)
    check("display-toggle appliance=0xAC", dt_parsed["appliance"] == 0xAC)
    dt_body = dt_parsed["body"]
    check("display-toggle body[0]=0x41", dt_body[0] == 0x41)
    check("display-toggle body[1]=0x61", dt_body[1] == 0x61)
    check("display-toggle body[2]=0x00", dt_body[2] == 0x00)
    check("display-toggle body[3]=0xFF", dt_body[3] == 0xFF)
    check("display-toggle body[4]=0x02", dt_body[4] == 0x02)
    check("display-toggle body[5]=0x00", dt_body[5] == 0x00)
    check("display-toggle body[6]=0x02", dt_body[6] == 0x02)
    check("display-toggle body[7]=0x00", dt_body[7] == 0x00)
    check("display-toggle body length 21", len(dt_body) == 21)
    # bit-level decode of body[1]
    check("display-toggle body[1] bit 6 set", (dt_body[1] & 0x40) != 0)
    check("display-toggle body[1] bit 5 set", (dt_body[1] & 0x20) != 0)
    check("display-toggle body[1] bit 7 clear", (dt_body[1] & 0x80) == 0)
    check("display-toggle body[1] bit 0 set", (dt_body[1] & 0x01) != 0)
    # CRC + checksum already covered by validate_frame

    # ── Summary ──────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
