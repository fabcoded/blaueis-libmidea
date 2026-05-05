"""Pre-decode sentinel + post-decode range value suppression.

The codec gains two optional gates declared in the per-field glossary
entry:

    sentinel_values: [<bytes>]   # pre-decode, byte-level
    range:          [lo, hi]    # post-decode, physical units

Both are independently optional. A field with neither key keeps today's
behaviour bit-for-bit. A field with only ``sentinel_values:`` (e.g. the
two temperature fields after this change) gets pre-decode suppression
but no post-decode range gate. A field with only ``range:`` gets the
range tripwire.

These tests verify:
    * Sentinel byte → ``{value: None, raw: <byte>, suppression: 'sentinel'}``;
      range gate never runs even if the byte would have decoded to an
      in-range number.
    * Out-of-range decoded value → ``{value: None, raw: <number>,
      suppression: 'out_of_range'}``.
    * Absent keys = today's behaviour (no suppression dict, normal value).
    * ``process_data_frame`` overwrites prior ``value`` with ``None`` on a
      suppressed read, stamps a sibling ``suppression`` slot, and bumps
      ``meta.frame_counts.{protocol}_{reason}_suppressions``.
    * Real glossary install: a synthetic C0 body whose ``body[11]`` is
      ``0xFF`` suppresses ``indoor_temperature`` via the sentinel path.
"""

from __future__ import annotations

import pytest
from blaueis.core.codec import (
    build_field_map,
    decode_field,
    decode_frame_fields,
    load_glossary,
)
from blaueis.core.process import process_data_frame
from blaueis.core.status import build_status

# ── decode_field, no glossary required ──────────────────────────────


_TEMP_DECODE = [{"offset": 11, "bits": [7, 0], "encoding": "temp_offset50_half"}]
_TEMP_ENCODINGS = {
    "temp_offset50_half": {
        "formula": "(raw - 50) / 2.0",
        "scale": 0.5,
        "offset": 50,
    }
}


def _body_with(byte_at_11: int) -> bytes:
    """Synth body that's long enough for offset 11 — pad to 16."""
    return bytes(11) + bytes([byte_at_11]) + bytes(4)


@pytest.mark.parametrize("sentinel_byte", [0x00, 0xFF])
def test_sentinel_byte_suppresses_pre_decode(sentinel_byte: int) -> None:
    body = _body_with(sentinel_byte)
    result = decode_field(
        "indoor_temperature",
        _TEMP_DECODE,
        "float",
        body,
        _TEMP_ENCODINGS,
        sentinel_values=[0x00, 0xFF],
    )
    assert result["value"] is None
    assert result["raw"] == sentinel_byte
    assert result["suppression"] == "sentinel"


def test_normal_byte_decodes_when_sentinel_set_present() -> None:
    body = _body_with(0x55)  # 85 → (85-50)/2 = 17.5
    result = decode_field(
        "indoor_temperature",
        _TEMP_DECODE,
        "float",
        body,
        _TEMP_ENCODINGS,
        sentinel_values=[0x00, 0xFF],
    )
    assert result["value"] == 17.5
    assert "suppression" not in result


def test_no_sentinel_no_range_means_no_gates() -> None:
    """A field that declares neither key behaves exactly as today's
    decode path — no suppression dict, value flows through."""
    for b in (0x00, 0xFF, 0x55):
        result = decode_field(
            "fictional_field",
            _TEMP_DECODE,
            "float",
            _body_with(b),
            _TEMP_ENCODINGS,
        )
        assert "suppression" not in result
        # Even 0x00 / 0xFF decode to real numbers when no sentinel is set.
        assert result["value"] is not None


def test_range_gate_suppresses_post_decode() -> None:
    body = _body_with(0xFE)  # (254-50)/2 = 102.0 °C — outside [-40, 70]
    result = decode_field(
        "indoor_temperature",
        _TEMP_DECODE,
        "float",
        body,
        _TEMP_ENCODINGS,
        value_range=[-40.0, 70.0],
    )
    assert result["value"] is None
    assert result["raw"] == 102.0
    assert result["suppression"] == "out_of_range"


def test_range_gate_lets_in_range_value_through() -> None:
    body = _body_with(0x80)  # (128-50)/2 = 39.0 °C
    result = decode_field(
        "indoor_temperature",
        _TEMP_DECODE,
        "float",
        body,
        _TEMP_ENCODINGS,
        value_range=[-40.0, 70.0],
    )
    assert result["value"] == 39.0
    assert "suppression" not in result


def test_sentinel_takes_precedence_over_range() -> None:
    """0x00 decodes to -25.0 °C, which IS inside [-40, 70]. The
    sentinel must fire first; the range gate must never see the
    decoded value."""
    body = _body_with(0x00)
    result = decode_field(
        "outdoor_temperature",
        _TEMP_DECODE,
        "float",
        body,
        _TEMP_ENCODINGS,
        sentinel_values=[0x00, 0xFF],
        value_range=[-40.0, 70.0],  # -25 is inside
    )
    assert result["value"] is None
    assert result["raw"] == 0x00
    assert result["suppression"] == "sentinel"


def test_range_does_not_suppress_legit_cold_climate_value() -> None:
    """Outdoor -25 °C from a non-sentinel byte is in range and must not
    be suppressed — a conservative range chosen for the field can't
    catch real cold-climate readings."""
    # No byte != 0x00 / 0xFF on this formula produces exactly -25, so we
    # test -24.5 (byte 0x01 → (1-50)/2 = -24.5) which is non-sentinel.
    body = _body_with(0x01)
    result = decode_field(
        "outdoor_temperature",
        _TEMP_DECODE,
        "float",
        body,
        _TEMP_ENCODINGS,
        sentinel_values=[0x00, 0xFF],
        value_range=[-40.0, 70.0],
    )
    assert result["value"] == -24.5
    assert "suppression" not in result


def test_bool_field_skips_range_gate() -> None:
    """A bool decoded from a single-bit field has no meaningful
    physical range; the range gate must be a no-op even if a range is
    declared (defensive)."""
    bool_decode = [{"offset": 0, "bits": [0, 0]}]
    result = decode_field(
        "some_bool",
        bool_decode,
        "bool",
        bytes([0x01]),
        {},
        value_range=[100.0, 200.0],  # nonsense for bool
    )
    assert result["value"] is True
    assert "suppression" not in result


# ── decode_frame_fields wires both gates from the field map ─────────


def test_decode_frame_fields_passes_through_sentinel_to_real_field() -> None:
    """The two temperature fields now declare sentinel_values:; a C0
    body with body[11]=0xFF must surface the suppression for
    indoor_temperature."""
    glossary = load_glossary()

    body = bytearray(25)  # C0 body is plenty long
    body[11] = 0xFF
    body[12] = 0x55  # outdoor: (85-50)/2 = 17.5 °C, not suppressed
    body[15] = 0x00

    decoded = decode_frame_fields(bytes(body), "rsp_0xc0", glossary)

    assert "indoor_temperature" in decoded
    assert decoded["indoor_temperature"]["value"] is None
    assert decoded["indoor_temperature"]["suppression"] == "sentinel"
    assert decoded["indoor_temperature"]["raw"] == 0xFF

    assert "outdoor_temperature" in decoded
    assert decoded["outdoor_temperature"]["value"] == 17.5
    assert "suppression" not in decoded["outdoor_temperature"]


def test_field_map_surfaces_sentinel_and_range_keys() -> None:
    glossary = load_glossary()
    field_map = build_field_map(glossary, "rsp_0xc0")
    by_name = {f["name"]: f for f in field_map}
    assert by_name["indoor_temperature"]["sentinel_values"] == [0, 255]
    assert by_name["indoor_temperature"]["value_range"] is None
    assert by_name["outdoor_temperature"]["sentinel_values"] == [0, 255]


# ── process_data_frame stamps the suppression slot + counter ────────


def test_process_data_frame_stamps_sentinel_slot_overwriting_prior_value() -> None:
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    indoor = status["fields"]["indoor_temperature"]
    indoor["feature_available"] = "readable"

    # First frame: a healthy reading (byte 0x55 = 17.5 °C).
    good = bytearray(25)
    good[11] = 0x55
    good[12] = 0x55
    good[15] = 0x00
    process_data_frame(status, bytes(good), "rsp_0xc0", glossary, timestamp="t0")
    slot = indoor["sources"]["rsp_0xc0"]
    assert slot["value"] == 17.5
    assert "suppression" not in slot

    # Second frame: sentinel hit on indoor.
    nodata = bytearray(25)
    nodata[11] = 0x00
    nodata[12] = 0x55
    nodata[15] = 0x00
    process_data_frame(status, bytes(nodata), "rsp_0xc0", glossary, timestamp="t1")
    slot = indoor["sources"]["rsp_0xc0"]
    # Overwrite — current device answer is "no data". Prior reading is
    # gone from this slot (still in history via frame_no/ts).
    assert slot["value"] is None
    assert slot["raw"] == 0x00
    assert slot["suppression"]["reason"] == "sentinel"
    assert slot["suppression"]["raw"] == 0x00
    assert slot["suppression"]["ts"] == "t1"

    counts = status["meta"]["frame_counts"]
    assert counts.get("rsp_0xc0_sentinel_suppressions") == 1


def test_process_data_frame_recovery_clears_suppression_on_next_good_read() -> None:
    """A suppressed slot must update cleanly when the next frame
    carries a good reading — no leftover ``suppression`` key from the
    previous suppression."""
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    indoor = status["fields"]["indoor_temperature"]
    indoor["feature_available"] = "readable"

    nodata = bytearray(25)
    nodata[11] = 0xFF
    process_data_frame(status, bytes(nodata), "rsp_0xc0", glossary, timestamp="t0")
    assert "suppression" in indoor["sources"]["rsp_0xc0"]

    good = bytearray(25)
    good[11] = 0x55
    good[15] = 0x00
    process_data_frame(status, bytes(good), "rsp_0xc0", glossary, timestamp="t1")
    slot = indoor["sources"]["rsp_0xc0"]
    assert slot["value"] == 17.5
    # The slot is fully replaced on each frame, so a stale suppression
    # dict from a prior frame must NOT carry over.
    assert "suppression" not in slot


def test_process_data_frame_counter_accumulates() -> None:
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    status["fields"]["indoor_temperature"]["feature_available"] = "readable"
    status["fields"]["outdoor_temperature"]["feature_available"] = "readable"

    body = bytearray(25)
    body[11] = 0x00  # indoor sentinel
    body[12] = 0xFF  # outdoor sentinel
    body[15] = 0x00

    process_data_frame(status, bytes(body), "rsp_0xc0", glossary, timestamp="t0")
    process_data_frame(status, bytes(body), "rsp_0xc0", glossary, timestamp="t1")

    counts = status["meta"]["frame_counts"]
    # 2 frames × 2 fields suppressed each = 4 sentinel hits
    assert counts.get("rsp_0xc0_sentinel_suppressions") == 4
    # No range suppressions occurred.
    assert "rsp_0xc0_out_of_range_suppressions" not in counts


# ── read_field surfaces suppression metadata ────────────────────────


def test_read_field_surfaces_suppression_when_present() -> None:
    """A slot carrying a `suppression` sub-dict must surface it on the
    `read_field` return so HA's `extra_state_attributes` can read why
    the field is reporting None."""
    from blaueis.core.query import read_field

    status = {
        "fields": {
            "indoor_temperature": {
                "sources": {
                    "rsp_0xc0": {
                        "value": None,
                        "raw": 0xFF,
                        "ts": "t1",
                        "generation": "legacy",
                        "suppression": {
                            "reason": "sentinel",
                            "raw": 0xFF,
                            "frame_no": 1,
                            "ts": "t1",
                        },
                    }
                }
            }
        }
    }
    r = read_field(status, "indoor_temperature")
    assert r is not None
    assert r["value"] is None
    assert r["source"] == "rsp_0xc0"
    assert r["suppression"]["reason"] == "sentinel"
    assert r["suppression"]["raw"] == 0xFF


def test_read_field_omits_suppression_key_when_slot_has_none() -> None:
    """The common path — a normal slot with no suppression — must not
    introduce a stray `suppression` key on the read result."""
    from blaueis.core.query import read_field

    status = {
        "fields": {
            "indoor_temperature": {
                "sources": {
                    "rsp_0xc0": {
                        "value": 21.5,
                        "raw": 0x55,
                        "ts": "t0",
                        "generation": "legacy",
                    }
                }
            }
        }
    }
    r = read_field(status, "indoor_temperature")
    assert r is not None
    assert r["value"] == 21.5
    assert "suppression" not in r
