"""Tests for blaueis.core.gate_anchors — base-constraint B1 (bit-position anchor).

These pin every gate/interlock reference to BOTH a field name and a physical wire
address, so a rename or a decode-offset edit fails CI instead of silently re-aiming
a gate at the wrong bit. Phase-0 skeleton: the seed registry + the drift detectors.
"""
from __future__ import annotations

from blaueis.core.codec import load_glossary
from blaueis.core.gate_anchors import (
    GATE_ANCHORS,
    collect_glossary_gate_anchors,
    field_addresses,
    verify_all_anchors,
    verify_gate_anchors,
)


def test_seed_anchors_all_hold() -> None:
    """Every seeded anchor resolves to its declared address in the live glossary."""
    problems = verify_gate_anchors(load_glossary())
    assert problems == [], "\n".join(problems)


def test_turbo_wiring_distinction_is_machine_checkable() -> None:
    """The Turbo bug, generalised: strong_wind and turbo_mode are DIFFERENT bits.

    The boost ("Turbo") control acts on strong_wind (C0:8:5); our preset wrote
    turbo_mode (C0:10:1). A name-only reference hid that; the anchor makes it explicit.
    """
    g = load_glossary()
    assert field_addresses(g, "strong_wind", "C0") == ["C0:8:5..5"]
    assert field_addresses(g, "turbo_mode", "C0") == ["C0:10:1..1"]
    assert field_addresses(g, "strong_wind", "C0") != field_addresses(g, "turbo_mode", "C0")


def test_per_protocol_address_differs() -> None:
    """eco_mode writes at W40:9:7 but reads at C0:9:4 — anchors are protocol-qualified."""
    g = load_glossary()
    assert field_addresses(g, "eco_mode", "C0") == ["C0:9:4..4"]
    assert field_addresses(g, "eco_mode", "W40") == ["W40:9:7..7"]


def test_b1_property_address_form() -> None:
    """B1-property fields anchor on their property id, not a byte offset."""
    g = load_glossary()
    assert field_addresses(g, "jet_cool", "B1") == ["B1:0x67,0x00:0..0"]


def test_rename_is_caught() -> None:
    """An anchor naming a field that no longer exists is reported as unresolved."""
    problems = verify_gate_anchors(load_glossary(), {"strong_wind_RENAMED": "C0:8:5..5"})
    assert len(problems) == 1
    assert "unresolved" in problems[0]


def test_bit_position_drift_is_caught() -> None:
    """An anchor pointing at the wrong bit of an existing field is reported as drift."""
    problems = verify_gate_anchors(load_glossary(), {"strong_wind": "C0:8:4..4"})
    assert len(problems) == 1
    assert "drift" in problems[0]


def test_wrong_protocol_is_caught() -> None:
    """An anchor on a protocol the field has no decode for is reported, not ignored."""
    problems = verify_gate_anchors(load_glossary(), {"natural_wind": "B1:0x42,0x00:0..0"})
    assert len(problems) == 1
    assert "unresolved" in problems[0]


def test_registry_is_non_empty() -> None:
    """Guard against the skeleton silently shrinking to a vacuous pass."""
    assert len(GATE_ANCHORS) >= 7


# ── gate-block anchor collection (G1: ready for when fields opt in) ───────


def test_no_gate_blocks_in_glossary_yet() -> None:
    """No field declares gate.interlocks yet — collection is empty (and inert)."""
    assert collect_glossary_gate_anchors(load_glossary()) == {}


def test_collect_gate_anchors_from_synthetic_glossary() -> None:
    g = {"fields": {"control": {"turbo_mode": {
        "description": "x",
        "gate": {"interlocks": [{"field": "ptc_state", "at": "C0:9:4..3"}]},
    }}}}
    assert collect_glossary_gate_anchors(g) == {"ptc_state": "C0:9:4..3"}


def test_verify_all_anchors_holds_on_live_glossary() -> None:
    """Seed anchors + (currently empty) gate-block anchors all resolve."""
    assert verify_all_anchors(load_glossary()) == []
