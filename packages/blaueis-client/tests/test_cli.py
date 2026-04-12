"""Tests for CLI format_frame() — pure function, no network.

Usage (standalone):  python -m pytest packages/blaueis-client/tests/test_cli.py -v
"""

from blaueis.client.cli import format_frame


def _make_hex(header_hex: str, cmd_byte: str) -> str:
    """Build a minimal hex string: 10-byte header + cmd byte in body."""
    # 10-byte header = 20 hex chars, then body starts with cmd_byte
    return header_hex.ljust(20, "0") + cmd_byte


def test_c0_label():
    hex_str = _make_hex("AA23AC00000000000003", "c0")
    result = format_frame(hex_str, 1.0)
    assert "C0 Status Response" in result


def test_b5_label():
    hex_str = _make_hex("AA23AC00000000000003", "b5")
    result = format_frame(hex_str, 2.5)
    assert "B5 Capabilities" in result


def test_a1_label():
    hex_str = _make_hex("AA23AC00000000000003", "a1")
    result = format_frame(hex_str, 0.0)
    assert "A1 Heartbeat" in result


def test_unknown_cmd_byte():
    hex_str = _make_hex("AA23AC00000000000003", "ff")
    result = format_frame(hex_str, 0.0)
    assert "CMD 0xff" in result


def test_short_hex_fallback():
    result = format_frame("AA", 3.0)
    assert "[3.000]" in result
    assert "AA" in result


def test_empty_hex():
    result = format_frame("", 0.0)
    assert "[0.000]" in result


def test_timestamp_formatting():
    hex_str = _make_hex("AA23AC00000000000003", "c0")
    result = format_frame(hex_str, 12.345)
    assert "[12.345]" in result


def test_byte_count_in_output():
    hex_str = _make_hex("AA23AC00000000000003", "c0") + "aabbcc"
    result = format_frame(hex_str, 0.0)
    # 20 hex header + 2 cmd + 6 extra = 28 hex chars = 14 bytes
    assert "14B" in result
