"""Pre-flight value validation against glossary gates.

Synthetic glossary entries — these tests don't depend on the public
glossary content, so future edits to the real glossary can't make
them flap.
"""

from __future__ import annotations

import pytest
from blaueis.core.validation import (
    FieldUnknown,
    ModeDisallowed,
    NotInEnum,
    Ok,
    OutOfRange,
    validate_set,
)


def _glossary(field_name: str, fdef: dict, *, with_operating_mode: bool = False) -> dict:
    """Build a minimal glossary containing one synthetic field.

    Adds a stub ``operating_mode`` entry when the test exercises
    ``valid_modes:`` so the validator can resolve a token string.
    """
    fields: dict = {"control": {field_name: fdef}}
    if with_operating_mode:
        fields["control"]["operating_mode"] = {
            "description": "stub",
            "data_type": "uint8",
            "values": {
                "cool": {"raw": 0x40},
                "heat": {"raw": 0x80},
                "dry": {"raw": 0x60},
                "fan_only": {"raw": 0xA0},
                "auto": {"raw": 0x20},
            },
        }
    return {"fields": fields}


def _status_with_mode(raw: int | None) -> dict:
    """Status dict whose operating_mode source slot reports ``raw``."""
    return {
        "fields": {
            "operating_mode": {
                "sources": {
                    "rsp_0xc0": {
                        "value": raw,
                        "ts": "t0",
                        "generation": "legacy",
                    }
                }
            }
        }
    }


# ── Field unknown ───────────────────────────────────────────────────


def test_field_unknown() -> None:
    g = {"fields": {"control": {}}}
    out = validate_set("nonexistent", 1, {}, g)
    assert isinstance(out, FieldUnknown)
    assert out.field_name == "nonexistent"
    assert out.ok is False


# ── Range gate ──────────────────────────────────────────────────────


def test_range_value_inside_passes() -> None:
    g = _glossary(
        "target_temperature",
        {
            "description": "x",
            "data_type": "float",
            "range": [16.0, 30.5],
        },
    )
    assert validate_set("target_temperature", 22.0, {}, g).ok


@pytest.mark.parametrize("v", [15.5, 31.0, -5, 100])
def test_range_value_outside_rejected(v: float) -> None:
    g = _glossary(
        "target_temperature",
        {
            "description": "x",
            "data_type": "float",
            "range": [16.0, 30.5],
        },
    )
    out = validate_set("target_temperature", v, {}, g)
    assert isinstance(out, OutOfRange)
    assert out.field_name == "target_temperature"
    assert out.value == v
    assert out.min_value == 16.0
    assert out.max_value == 30.5


def test_range_boundary_values_inclusive() -> None:
    g = _glossary(
        "x",
        {
            "description": "x",
            "data_type": "float",
            "range": [0, 100],
        },
    )
    assert validate_set("x", 0, {}, g).ok
    assert validate_set("x", 100, {}, g).ok


def test_range_skipped_for_bool_value() -> None:
    """Booleans don't get range-checked even if a range is declared
    (defensive — no field should declare both, but sanity)."""
    g = _glossary(
        "x",
        {
            "description": "x",
            "data_type": "bool",
            "range": [0, 100],
        },
    )
    assert validate_set("x", True, {}, g).ok


def test_range_absent_means_no_check() -> None:
    g = _glossary("x", {"description": "x", "data_type": "float"})
    # Any value passes when no range is declared.
    assert validate_set("x", 10**9, {}, g).ok
    assert validate_set("x", -(10**9), {}, g).ok


def test_malformed_range_skipped() -> None:
    g = _glossary(
        "x",
        {
            "description": "x",
            "data_type": "float",
            "range": [16],
        },
    )
    assert validate_set("x", 22, {}, g).ok


# ── Enum gate ───────────────────────────────────────────────────────


def test_enum_value_in_set_passes() -> None:
    g = _glossary(
        "operating_mode",
        {
            "description": "x",
            "data_type": "uint8",
            "values": {
                "cool": {"raw": 0x40},
                "heat": {"raw": 0x80},
            },
        },
    )
    assert validate_set("operating_mode", 0x40, {}, g).ok


def test_enum_value_not_in_set_rejected() -> None:
    g = _glossary(
        "operating_mode",
        {
            "description": "x",
            "data_type": "uint8",
            "values": {
                "cool": {"raw": 0x40},
                "heat": {"raw": 0x80},
            },
        },
    )
    out = validate_set("operating_mode", 0x99, {}, g)
    assert isinstance(out, NotInEnum)
    assert out.value == 0x99
    assert out.allowed == (0x40, 0x80)


def test_enum_skipped_for_bool() -> None:
    """Boolean fields: HA service contract handles bool conversion;
    enum gate would over-fire on True/False."""
    g = _glossary(
        "x",
        {
            "description": "x",
            "data_type": "bool",
            "values": {"on": {"raw": 1}, "off": {"raw": 0}},
        },
    )
    # True doesn't equal 1 in the same comparison if bools are excluded;
    # validator should let it through and let HA upstream handle.
    assert validate_set("x", True, {}, g).ok


def test_enum_block_without_raw_keys_does_not_constrain() -> None:
    """A `values:` block with descriptive entries but no `raw:` keys
    (rare, but seen in glossary entries that document but don't enum)
    must not reject any value — there's nothing to check against."""
    g = _glossary(
        "x",
        {
            "description": "x",
            "data_type": "uint8",
            "values": {"cool": {"description": "Cooling mode"}},
        },
    )
    assert validate_set("x", 99, {}, g).ok


# ── Mode gate ───────────────────────────────────────────────────────


def test_valid_modes_accepts_when_current_mode_listed() -> None:
    g = _glossary(
        "eco_mode",
        {"description": "x", "data_type": "bool", "valid_modes": ["cool", "auto"]},
        with_operating_mode=True,
    )
    status = _status_with_mode(0x40)  # cool
    assert validate_set("eco_mode", True, status, g).ok


def test_valid_modes_rejects_when_current_mode_not_listed() -> None:
    g = _glossary(
        "eco_mode",
        {"description": "x", "data_type": "bool", "valid_modes": ["cool", "auto"]},
        with_operating_mode=True,
    )
    status = _status_with_mode(0x80)  # heat
    out = validate_set("eco_mode", True, status, g)
    assert isinstance(out, ModeDisallowed)
    assert out.current_mode == "heat"
    assert out.valid_modes == ("cool", "auto")


def test_valid_modes_skipped_when_mode_unknown() -> None:
    """When operating_mode hasn't been read yet, validator can't
    disagree — let the wire write proceed and the firmware judge."""
    g = _glossary(
        "eco_mode",
        {"description": "x", "data_type": "bool", "valid_modes": ["cool"]},
        with_operating_mode=True,
    )
    status = {"fields": {}}  # no operating_mode slot
    assert validate_set("eco_mode", True, status, g).ok


def test_valid_modes_skipped_when_raw_outside_glossary_values() -> None:
    """If the firmware reports a raw mode byte the glossary doesn't
    document, the token resolution returns None and the gate is
    skipped (same conservative posture)."""
    g = _glossary(
        "eco_mode",
        {"description": "x", "data_type": "bool", "valid_modes": ["cool"]},
        with_operating_mode=True,
    )
    status = _status_with_mode(0xFE)  # not in values block
    assert validate_set("eco_mode", True, status, g).ok


def test_valid_modes_absent_means_no_check() -> None:
    g = _glossary("x", {"description": "x", "data_type": "bool"}, with_operating_mode=True)
    status = _status_with_mode(0x80)
    assert validate_set("x", True, status, g).ok


# ── Combined gates ──────────────────────────────────────────────────


def test_range_fires_before_mode_when_value_out_of_range() -> None:
    """Range and mode could both fail — the validator picks one in the
    documented order. Range is cheaper and more specific so it fires
    first; the user gets the most actionable message."""
    g = _glossary(
        "target_temperature",
        {
            "description": "x",
            "data_type": "float",
            "range": [16.0, 30.5],
            "valid_modes": ["cool"],
        },
        with_operating_mode=True,
    )
    status = _status_with_mode(0x80)  # heat — would fail mode gate too
    out = validate_set("target_temperature", 99.0, status, g)
    assert isinstance(out, OutOfRange)


def test_in_range_value_still_subject_to_mode_gate() -> None:
    g = _glossary(
        "target_temperature",
        {
            "description": "x",
            "data_type": "float",
            "range": [16.0, 30.5],
            "valid_modes": ["cool"],
        },
        with_operating_mode=True,
    )
    status = _status_with_mode(0x80)  # heat
    out = validate_set("target_temperature", 22.0, status, g)
    assert isinstance(out, ModeDisallowed)


def test_ok_outcome_truthy() -> None:
    g = _glossary("x", {"description": "x", "data_type": "float"})
    out = validate_set("x", 22, {}, g)
    assert out.ok is True
    assert isinstance(out, Ok)
