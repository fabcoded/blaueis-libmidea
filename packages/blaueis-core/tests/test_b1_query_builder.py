"""Tests for blaueis.core.frame.build_b1_property_query."""
from __future__ import annotations

import pytest
from blaueis.core.frame import build_b1_property_query, parse_frame, validate_frame


def test_single_prop() -> None:
    frame = build_b1_property_query([(0x42, 0x00)])
    validate_frame(frame)
    parsed = parse_frame(frame)
    assert parsed["msg_type"] == 0x03
    # body: 0xB1, count=1, 0x42, 0x00
    assert parsed["body"][0] == 0xB1
    assert parsed["body"][1] == 0x01
    assert parsed["body"][2] == 0x42
    assert parsed["body"][3] == 0x00


def test_multiple_props() -> None:
    pairs = [(0x15, 0x00), (0x39, 0x00), (0x42, 0x00), (0x48, 0x00)]
    frame = build_b1_property_query(pairs)
    parsed = parse_frame(frame)
    assert parsed["body"][0] == 0xB1
    assert parsed["body"][1] == len(pairs)
    for i, (lo, hi) in enumerate(pairs):
        assert parsed["body"][2 + i * 2] == lo
        assert parsed["body"][3 + i * 2] == hi


def test_empty_rejects() -> None:
    with pytest.raises(ValueError):
        build_b1_property_query([])


def test_large_batch_still_valid() -> None:
    # 16 pairs is a typical batch size — still well under the length ceiling.
    pairs = [(i, 0) for i in range(16)]
    frame = build_b1_property_query(pairs)
    validate_frame(frame)
    parsed = parse_frame(frame)
    assert parsed["body"][1] == 16


def test_honours_appliance_proto_sub() -> None:
    frame = build_b1_property_query([(0x42, 0)], appliance=0xA1, proto=3, sub=4)
    parsed = parse_frame(frame)
    assert parsed["appliance"] == 0xA1
    assert parsed["proto"] == 3
    assert parsed["sub"] == 4
