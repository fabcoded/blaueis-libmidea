"""Tests for StatusDB mode gate and mutex expansion logic.

Tests the glossary-driven enforcement at the field level:
- Mode gate: visible_in_modes check before accepting writes
- Forward pass: mutual_exclusion.when_on.forces expansion
- Reverse pass: mode change clears incompatible fields
- Protocol split: x40 vs b0 routing

These test internal methods directly — no lock, no I/O.

Usage:  python -m pytest packages/blaueis-client/tests/test_mutex_forces.py -v
"""
from __future__ import annotations

from blaueis.client.status_db import StatusDB
from blaueis.core.codec import walk_fields
from blaueis.core.query import write_field

# ── Helpers ──────────────────────────────────────────────────


def _db_with_mode(mode_val: int) -> tuple[StatusDB, dict]:
    """StatusDB with operating_mode pre-set. Returns (db, all_fields)."""
    db = StatusDB()
    write_field(db._status, "operating_mode", mode_val, ts=1.0)
    return db, walk_fields(db._glossary)


# ── Mode gate ────────────────────────────────────────────────


class TestModeGate:
    """visible_in_modes check rejects fields in wrong mode."""

    def test_rejects_frost_protection_in_cool(self):
        db, af = _db_with_mode(2)  # cool
        accepted, rejected = db._apply_mode_gate({"frost_protection": True}, af)
        assert "frost_protection" not in accepted
        assert "frost_protection" in rejected

    def test_accepts_frost_protection_in_heat(self):
        db, af = _db_with_mode(4)  # heat
        accepted, rejected = db._apply_mode_gate({"frost_protection": True}, af)
        assert "frost_protection" in accepted
        assert len(rejected) == 0

    def test_operating_mode_always_accepted(self):
        db, af = _db_with_mode(2)
        accepted, rejected = db._apply_mode_gate({"operating_mode": 4}, af)
        assert "operating_mode" in accepted
        assert len(rejected) == 0

    def test_effective_mode_uses_new_mode(self):
        """Mode change + frost_protection in same call — gate uses NEW mode."""
        db, af = _db_with_mode(2)  # currently cool
        changes = {"operating_mode": 4, "frost_protection": True}
        accepted, rejected = db._apply_mode_gate(changes, af)
        assert "operating_mode" in accepted
        assert "frost_protection" in accepted
        assert len(rejected) == 0

    def test_mixed_accept_reject(self):
        db, af = _db_with_mode(2)  # cool
        changes = {
            "frost_protection": True,  # heat only → reject
            "eco_mode": True,          # cool/auto/dry → accept
            "turbo_mode": True,        # cool/heat → accept
        }
        accepted, rejected = db._apply_mode_gate(changes, af)
        assert "frost_protection" in rejected
        assert "eco_mode" in accepted
        assert "turbo_mode" in accepted

    def test_no_visible_in_modes_always_passes(self):
        db, af = _db_with_mode(2)
        accepted, rejected = db._apply_mode_gate({"power": True}, af)
        assert "power" in accepted
        assert len(rejected) == 0

    def test_jet_cool_only_in_cool(self):
        db_heat, af = _db_with_mode(4)  # heat
        _, rejected = db_heat._apply_mode_gate({"jet_cool": True}, af)
        assert "jet_cool" in rejected

        db_cool, af2 = _db_with_mode(2)  # cool
        accepted, _ = db_cool._apply_mode_gate({"jet_cool": True}, af2)
        assert "jet_cool" in accepted

    def test_sleep_mode_rejected_in_fan(self):
        db, af = _db_with_mode(5)  # fan
        _, rejected = db._apply_mode_gate({"sleep_mode": True}, af)
        assert "sleep_mode" in rejected

    def test_eco_mode_modes(self):
        """eco_mode visible in cool/auto/dry — rejected in heat and fan."""
        for mode, should_pass in [(2, True), (1, True), (3, True), (4, False), (5, False)]:
            db, af = _db_with_mode(mode)
            accepted, rejected = db._apply_mode_gate({"eco_mode": True}, af)
            if should_pass:
                assert "eco_mode" in accepted, f"eco_mode rejected in mode {mode}"
            else:
                assert "eco_mode" in rejected, f"eco_mode accepted in mode {mode}"


# ── Forward pass ─────────────────────────────────────────────


class TestForwardPass:
    """mutual_exclusion.when_on.forces expansion."""

    def test_frost_protection_forces_siblings(self):
        db, af = _db_with_mode(4)
        expanded = db._expand_mutex_forces({"frost_protection": True}, af)
        assert expanded["frost_protection"] is True
        assert expanded["turbo_mode"] == 0
        assert expanded["eco_mode"] == 0
        assert expanded["strong_wind"] == 0
        assert expanded["sleep_mode"] == 0

    def test_eco_mode_forces(self):
        db, af = _db_with_mode(2)
        expanded = db._expand_mutex_forces({"eco_mode": True}, af)
        assert expanded["turbo_mode"] == 0
        assert expanded["strong_wind"] == 0
        assert expanded["no_wind_sense"] == 0
        assert expanded["jet_cool"] == 0
        assert expanded["frost_protection"] == 0

    def test_caller_explicit_overrides_forces(self):
        """Caller-set value takes precedence over forced value."""
        db, af = _db_with_mode(4)
        expanded = db._expand_mutex_forces(
            {"frost_protection": True, "turbo_mode": True}, af,
        )
        assert expanded["turbo_mode"] is True  # caller wins over forces

    def test_no_expansion_when_turning_off(self):
        db, af = _db_with_mode(4)
        expanded = db._expand_mutex_forces({"frost_protection": False}, af)
        assert expanded == {"frost_protection": False}

    def test_power_false_no_expansion(self):
        db, af = _db_with_mode(2)
        expanded = db._expand_mutex_forces({"power": False}, af)
        assert expanded == {"power": False}

    def test_transitive_no_wind_sense(self):
        """no_wind_sense → breezeless=1 (truthy) → breezeless's own forces."""
        db, af = _db_with_mode(2)
        expanded = db._expand_mutex_forces({"no_wind_sense": True}, af)
        # Direct from no_wind_sense
        assert expanded["breezeless"] == 1
        assert expanded["eco_mode"] == 0
        assert expanded["strong_wind"] == 0
        assert expanded["turbo_mode"] == 0
        assert expanded["swing_vertical"] == 0
        # Transitive from breezeless=1
        assert expanded["breeze_away"] == 0
        assert expanded["breeze_mild"] == 0
        assert expanded["jet_cool"] == 0
        assert expanded["swing_horizontal"] == 0

    def test_empty_forces_pass_through(self):
        db, af = _db_with_mode(2)
        expanded = db._expand_mutex_forces({"power": True}, af)
        assert expanded == {"power": True}

    def test_depth_cap_terminates(self):
        """Synthetic chain beyond depth cap stops at 10 levels."""
        db = StatusDB()
        synth = {}
        for i in range(15):
            forces = {f"chain_{i+1}": 1} if i < 14 else {}
            synth[f"chain_{i}"] = {
                "data_type": "bool",
                "mutual_exclusion": {"when_on": {"forces": forces}},
            }
        expanded = db._expand_mutex_forces({"chain_0": True}, synth)
        assert "chain_0" in expanded
        assert "chain_10" in expanded
        assert "chain_11" not in expanded
        assert len(expanded) == 11


# ── Reverse pass ─────────────────────────────────────────────


class TestReversePass:
    """Mode change clears fields not visible in the new mode."""

    def test_cool_clears_frost_protection(self):
        db, af = _db_with_mode(4)
        write_field(db._status, "frost_protection", True, ts=1.0)
        expanded = db._expand_mutex_forces({"operating_mode": 2}, af)
        assert expanded["operating_mode"] == 2
        assert expanded["frost_protection"] is False

    def test_heat_keeps_frost_protection(self):
        """Staying in heat doesn't clear frost_protection."""
        db, af = _db_with_mode(4)
        write_field(db._status, "frost_protection", True, ts=1.0)
        expanded = db._expand_mutex_forces({"operating_mode": 4}, af)
        # frost_protection is visible in heat → not cleared
        assert "frost_protection" not in expanded or expanded.get("frost_protection") is True

    def test_does_not_clear_off_fields(self):
        """Fields already OFF aren't added to expanded by reverse pass."""
        db, af = _db_with_mode(2)
        # eco_mode is OFF in status
        expanded = db._expand_mutex_forces({"operating_mode": 4}, af)
        assert "eco_mode" not in expanded

    def test_no_reverse_without_mode_change(self):
        """Without operating_mode in changes, reverse pass doesn't run."""
        db, af = _db_with_mode(4)
        write_field(db._status, "frost_protection", True, ts=1.0)
        # sleep_mode forces don't include frost_protection
        expanded = db._expand_mutex_forces({"sleep_mode": True}, af)
        assert "frost_protection" not in expanded

    def test_fan_clears_eco(self):
        """eco_mode visible in [cool,auto,dry] — cleared when switching to fan."""
        db, af = _db_with_mode(2)
        write_field(db._status, "eco_mode", True, ts=1.0)
        expanded = db._expand_mutex_forces({"operating_mode": 5}, af)
        assert expanded["eco_mode"] is False

    def test_fan_clears_sleep(self):
        """sleep_mode visible in [cool,heat,dry,auto] — cleared in fan."""
        db, af = _db_with_mode(2)
        write_field(db._status, "sleep_mode", True, ts=1.0)
        expanded = db._expand_mutex_forces({"operating_mode": 5}, af)
        assert expanded["sleep_mode"] is False


# ── Protocol split ───────────────────────────────────────────


class TestProtocolSplit:
    """Fields route to cmd_0x40 or cmd_0xb0 based on glossary protocols."""

    def test_frost_protection_forces_all_x40(self):
        af = walk_fields(StatusDB()._glossary)
        changes = {
            "frost_protection": True,
            "turbo_mode": 0,
            "eco_mode": 0,
            "strong_wind": 0,
            "sleep_mode": 0,
        }
        x40, b0 = StatusDB._split_by_protocol(changes, af)
        assert len(b0) == 0
        assert set(x40.keys()) == set(changes.keys())

    def test_breeze_fields_route_to_b0(self):
        af = walk_fields(StatusDB()._glossary)
        changes = {"breeze_away": True, "breeze_mild": 0, "breezeless": 0}
        x40, b0 = StatusDB._split_by_protocol(changes, af)
        assert "breeze_away" in b0
        assert "breeze_mild" in b0
        assert "breezeless" in b0
        assert len(x40) == 0

    def test_no_wind_sense_expansion_spans_protocols(self):
        db, af = _db_with_mode(2)
        expanded = db._expand_mutex_forces({"no_wind_sense": True}, af)
        x40, b0 = StatusDB._split_by_protocol(expanded, af)
        assert len(x40) > 0, "should have x40 fields"
        assert len(b0) > 0, "should have b0 fields"
        assert "no_wind_sense" in b0
        assert "turbo_mode" in x40
