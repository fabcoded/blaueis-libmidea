"""Tests for ac_probe.py frame builders — pure functions, no network.

Usage:  python -m pytest packages/blaueis-tools/tests/test_ac_probe.py -v
"""

from blaueis.core.frame import parse_frame
from blaueis.tools.ac_probe import (
    build_b1_property_query,
    build_device_id_query,
    build_direct_subpage_query,
    build_group_query_raw,
    build_optcommand_query,
)


def test_direct_subpage_query_0x01():
    frame = build_direct_subpage_query(0x01)
    assert frame[0] == 0xAA  # start byte
    parsed = parse_frame(frame)
    assert parsed["body"][0] == 0x41
    assert parsed["body"][1] == 0x01


def test_direct_subpage_query_0x02():
    frame = build_direct_subpage_query(0x02)
    parsed = parse_frame(frame)
    assert parsed["body"][0] == 0x41
    assert parsed["body"][1] == 0x02


def test_optcommand_query():
    frame = build_optcommand_query(0x03, 0x02)
    assert frame[0] == 0xAA
    parsed = parse_frame(frame)
    body = parsed["body"]
    assert body[0] == 0x41
    assert body[1] == 0x21
    assert body[4] == 0x03
    assert body[5] == 0xFF
    assert body[7] == 0x02


def test_group_query_raw_default_variant():
    frame = build_group_query_raw(0x44)
    parsed = parse_frame(frame)
    body = parsed["body"]
    assert body[1] == 0x81  # default variant
    assert body[3] == 0x44


def test_group_query_raw_custom_variant():
    frame = build_group_query_raw(0x46, variant=0x21)
    parsed = parse_frame(frame)
    body = parsed["body"]
    assert body[1] == 0x21
    assert body[3] == 0x46


def test_device_id_query():
    frame = build_device_id_query()
    assert frame[0] == 0xAA
    parsed = parse_frame(frame)
    assert parsed["msg_type"] == 0x07


def test_b1_property_query():
    props = [(0x15, 0x00), (0x1A, 0x00)]
    frame = build_b1_property_query(props)
    parsed = parse_frame(frame)
    body = parsed["body"]
    assert body[0] == 0xB1
    assert body[1] == 2  # count
    assert body[2] == 0x15
    assert body[3] == 0x00
    assert body[4] == 0x1A
    assert body[5] == 0x00


def test_all_frames_parseable():
    """Every builder should produce a frame that parse_frame accepts."""
    frames = [
        build_direct_subpage_query(0x01),
        build_optcommand_query(0x03, 0x02),
        build_group_query_raw(0x44),
        build_device_id_query(),
        build_b1_property_query([(0x15, 0x00)]),
    ]
    for f in frames:
        parsed = parse_frame(f)
        assert "body" in parsed
