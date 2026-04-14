"""Tests for blaueis.core.ux_gating.is_field_visible + default_for_masked_field."""
from __future__ import annotations

import pytest

from blaueis.core.ux_gating import default_for_masked_field, is_field_visible


# ── Pass-through cases (no ux block, no mode block) ──────────────────────

def test_no_glossary_entry_visible() -> None:
    assert is_field_visible(None, current_mode="cool") is True


def test_empty_glossary_entry_visible() -> None:
    assert is_field_visible({}, current_mode="cool") is True


def test_glossary_without_ux_block_visible() -> None:
    gdef = {"field_class": "stateful_bool", "protocols": {}}
    assert is_field_visible(gdef, current_mode="heat") is True


def test_ux_without_visible_in_modes_visible() -> None:
    gdef = {"ux": {"something_else": 1}}
    assert is_field_visible(gdef, current_mode="heat") is True


# ── visible_in_modes behaviour ──────────────────────────────────────────

def test_mode_string_in_list() -> None:
    gdef = {"ux": {"visible_in_modes": ["cool", "auto", "dry"]}}
    assert is_field_visible(gdef, current_mode="cool") is True
    assert is_field_visible(gdef, current_mode="auto") is True


def test_mode_string_not_in_list() -> None:
    gdef = {"ux": {"visible_in_modes": ["cool"]}}
    assert is_field_visible(gdef, current_mode="heat") is False


def test_mode_int_resolved_via_name_table() -> None:
    gdef = {"ux": {"visible_in_modes": ["cool"]}}
    assert is_field_visible(gdef, current_mode=2) is True   # cool
    assert is_field_visible(gdef, current_mode=4) is False  # heat


def test_mode_int_if_int_in_list() -> None:
    # List authors can also write ints if they prefer.
    gdef = {"ux": {"visible_in_modes": [2, 1]}}
    assert is_field_visible(gdef, current_mode=2) is True
    assert is_field_visible(gdef, current_mode="heat") is False


def test_unknown_mode_fail_open() -> None:
    gdef = {"ux": {"visible_in_modes": ["cool"]}}
    assert is_field_visible(gdef, current_mode=None) is True


# ── hardware_flag behaviour ─────────────────────────────────────────────

def test_hardware_flag_truthy_cap() -> None:
    gdef = {"ux": {"hardware_flag": "b5_has_pm25_sensor",
                   "visible_in_modes": ["cool", "heat", "auto", "dry", "fan_only"]}}
    assert is_field_visible(gdef, current_mode="cool",
                            caps={"b5_has_pm25_sensor": True}) is True


def test_hardware_flag_falsy_cap_masks_permanently() -> None:
    gdef = {"ux": {"hardware_flag": "b5_has_pm25_sensor"}}
    assert is_field_visible(gdef, current_mode="cool",
                            caps={"b5_has_pm25_sensor": False}) is False


def test_hardware_flag_missing_cap_masks() -> None:
    # No caps dict — we fail CLOSED on hardware (safer than showing a broken entity).
    gdef = {"ux": {"hardware_flag": "b5_has_pm25_sensor"}}
    assert is_field_visible(gdef, current_mode="cool", caps=None) is False


def test_hardware_and_mode_both_checked() -> None:
    # Hardware absent short-circuits even if mode would pass.
    gdef = {"ux": {"hardware_flag": "b5_sensor", "visible_in_modes": ["cool"]}}
    assert is_field_visible(gdef, current_mode="cool",
                            caps={"b5_sensor": False}) is False


# ── default_for_masked_field ────────────────────────────────────────────

def test_default_uses_glossary_default_value() -> None:
    gdef = {"default_value": 42, "data_type": "uint8"}
    assert default_for_masked_field(gdef) == 42


def test_default_type_zero_bool() -> None:
    gdef = {"data_type": "bool"}
    assert default_for_masked_field(gdef) is False


def test_default_type_zero_numeric() -> None:
    gdef = {"data_type": "uint8"}
    assert default_for_masked_field(gdef) == 0


def test_default_none_gdef() -> None:
    assert default_for_masked_field(None) == 0
