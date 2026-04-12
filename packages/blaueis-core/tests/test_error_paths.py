"""Tests for error paths — malformed input handling across blaueis-core.

Verifies that bad inputs raise the correct exceptions and don't crash.

Usage:  python -m pytest packages/blaueis-core/tests/test_error_paths.py -v
"""

import pytest

from blaueis.core.frame import FrameError, build_frame, parse_frame, validate_frame
from blaueis.core.codec import load_glossary


# ── parse_frame error paths ─────────────────────────────────────────────


def test_parse_frame_truncated():
    with pytest.raises(FrameError, match="too short"):
        parse_frame(b"\xAA\x0B\xAC")


def test_parse_frame_wrong_start_byte():
    # 13 bytes but starts with 0x55 instead of 0xAA
    with pytest.raises(FrameError, match="Invalid start byte"):
        parse_frame(b"\x55" + b"\x00" * 12)


def test_parse_frame_length_mismatch():
    # Start byte OK, but declared length exceeds data
    with pytest.raises(FrameError, match="truncated"):
        parse_frame(b"\xAA\xFF\xAC" + b"\x00" * 10)


def test_parse_frame_empty():
    with pytest.raises(FrameError):
        parse_frame(b"")


def test_parse_frame_single_byte():
    with pytest.raises(FrameError):
        parse_frame(b"\xAA")


# ── validate_frame error paths ──────────────────────────────────────────


def test_validate_frame_bad_crc():
    # Build a valid frame, then corrupt the CRC byte (second-to-last)
    frame = bytearray(build_frame(body=b"\x41\x00", msg_type=0x03))
    frame[-2] ^= 0xFF  # flip CRC
    # Recalculate checksum so only CRC fails
    from blaueis.core.frame import frame_checksum
    frame[-1] = frame_checksum(frame)
    with pytest.raises(FrameError, match="CRC"):
        validate_frame(bytes(frame))


def test_validate_frame_bad_checksum():
    # Build a valid frame, then corrupt the checksum (last byte)
    frame = bytearray(build_frame(body=b"\x41\x00", msg_type=0x03))
    frame[-1] ^= 0xFF  # flip checksum
    with pytest.raises(FrameError, match="[Cc]hecksum"):
        validate_frame(bytes(frame))


# ── build_frame ─────────────────────────────────────────────────────────


def test_build_frame_empty_body():
    """Empty body produces a frame below the 13-byte parse minimum.

    This is expected: real protocol frames always have at least 1 body byte.
    build_frame doesn't reject it, but parse_frame will.
    """
    frame = build_frame(body=b"", msg_type=0x03)
    assert frame[0] == 0xAA
    assert len(frame) == 12  # header(10) + CRC(1) + CHK(1), no body
    with pytest.raises(FrameError, match="too short"):
        parse_frame(frame)


def test_build_frame_round_trip():
    """build_frame → parse_frame should preserve msg_type and body."""
    body = b"\xC0\x01\x02\x03"
    frame = build_frame(body=body, msg_type=0x02, appliance=0xAC)
    parsed = parse_frame(frame)
    assert parsed["body"] == body
    assert parsed["msg_type"] == 0x02
    assert parsed["crc_ok"] is True
    assert parsed["checksum_ok"] is True


# ── load_glossary ───────────────────────────────────────────────────────


def test_load_glossary_succeeds():
    glossary = load_glossary()
    assert isinstance(glossary, dict)
    assert "fields" in glossary


def test_load_glossary_has_frames():
    glossary = load_glossary()
    assert "frames" in glossary
