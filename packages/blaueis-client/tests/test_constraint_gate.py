"""Tests for StatusDB.constraint gate — active-cap envelope clamping.

Policy: always clamp to the nearest valid value; log.warning for the
audit trail; never raise. Tests drive `_apply_constraint_gate` directly
(no lock, no I/O) and seed `active_constraints` on the status dict.

Usage: python -m pytest packages/blaueis-client/tests/test_constraint_gate.py -v
"""
from __future__ import annotations

import logging

import pytest
from blaueis.client.status_db import StatusDB
from blaueis.core.codec import walk_fields
from blaueis.core.query import write_field


def _db() -> tuple[StatusDB, dict]:
    db = StatusDB()
    return db, walk_fields(db._glossary)


def _seed_constraints(db: StatusDB, fname: str, ac: dict) -> None:
    db._status["fields"].setdefault(fname, {})["active_constraints"] = ac


class TestValidRange:
    def test_clamps_high(self, caplog):
        db, af = _db()
        _seed_constraints(db, "target_temperature", {"valid_range": [16.0, 30.0]})
        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            out = db._apply_constraint_gate(
                {"target_temperature": 40.0}, af, effective_mode=2,
            )
        assert out["target_temperature"] == 30.0
        assert any("clamped" in r.message for r in caplog.records)

    def test_clamps_low(self, caplog):
        db, af = _db()
        _seed_constraints(db, "target_temperature", {"valid_range": [16.0, 30.0]})
        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            out = db._apply_constraint_gate(
                {"target_temperature": 5.0}, af, effective_mode=2,
            )
        assert out["target_temperature"] == 16.0

    def test_in_range_passthrough(self, caplog):
        db, af = _db()
        _seed_constraints(db, "target_temperature", {"valid_range": [16.0, 30.0]})
        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            out = db._apply_constraint_gate(
                {"target_temperature": 22.0}, af, effective_mode=2,
            )
        assert out["target_temperature"] == 22.0
        assert not any("clamped" in r.message for r in caplog.records)


class TestValidSet:
    def test_snaps_to_nearest(self, caplog):
        db, af = _db()
        _seed_constraints(db, "fan_speed", {"valid_set": [20, 40, 60, 80, 102]})
        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            out = db._apply_constraint_gate({"fan_speed": 55}, af, effective_mode=2)
        assert out["fan_speed"] == 60

    def test_in_set_passthrough(self, caplog):
        db, af = _db()
        _seed_constraints(db, "fan_speed", {"valid_set": [20, 40, 60, 80, 102]})
        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            out = db._apply_constraint_gate({"fan_speed": 60}, af, effective_mode=2)
        assert out["fan_speed"] == 60
        assert not any("clamped" in r.message for r in caplog.records)

    def test_empty_set_drops_write(self, caplog):
        """Disabled caps expose valid_set=[] — drop the write entirely."""
        db, af = _db()
        _seed_constraints(db, "anion_ionizer", {"valid_set": []})
        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            out = db._apply_constraint_gate(
                {"anion_ionizer": True}, af, effective_mode=2,
            )
        # anion_ionizer is data_type=bool so it's skipped — use a non-bool
        # field to test the empty-set drop path
        assert "anion_ionizer" in out  # bool skip → unchanged

    def test_empty_set_drops_non_bool(self, caplog):
        db, af = _db()
        _seed_constraints(db, "fan_speed", {"valid_set": []})
        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            out = db._apply_constraint_gate({"fan_speed": 60}, af, effective_mode=2)
        assert "fan_speed" not in out
        assert any("dropped" in r.message for r in caplog.records)


class TestByMode:
    def test_selects_mode_envelope(self, caplog):
        """by_mode['cool'] should be used over flat envelope when mode=2."""
        db, af = _db()
        _seed_constraints(db, "target_temperature", {
            "by_mode": {
                "cool": {"valid_range": [17.0, 29.0]},
                "heat": {"valid_range": [16.0, 30.0]},
            },
        })
        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            out = db._apply_constraint_gate(
                {"target_temperature": 30.0}, af, effective_mode=2,  # cool
            )
        assert out["target_temperature"] == 29.0

    def test_selects_heat_mode_envelope(self):
        db, af = _db()
        _seed_constraints(db, "target_temperature", {
            "by_mode": {
                "cool": {"valid_range": [17.0, 29.0]},
                "heat": {"valid_range": [16.0, 30.0]},
            },
        })
        out = db._apply_constraint_gate(
            {"target_temperature": 30.0}, af, effective_mode=4,  # heat
        )
        assert out["target_temperature"] == 30.0  # 30 is valid in heat

    def test_missing_mode_falls_back_to_flat(self):
        """When by_mode lacks entry for current mode, use the top-level envelope."""
        db, af = _db()
        _seed_constraints(db, "target_temperature", {
            "valid_range": [16.0, 30.0],
            "by_mode": {"cool": {"valid_range": [17.0, 29.0]}},
        })
        out = db._apply_constraint_gate(
            {"target_temperature": 30.0}, af, effective_mode=5,  # fan — no entry
        )
        assert out["target_temperature"] == 30.0


class TestSkips:
    def test_bool_skipped(self):
        """data_type=bool fields never touched by the gate."""
        db, af = _db()
        out = db._apply_constraint_gate({"power": True}, af, effective_mode=2)
        assert out["power"] is True

    def test_empty_constraints_passthrough(self):
        """Pre-B5: active_constraints empty/None, gate is a no-op."""
        db, af = _db()
        # Force the field's active_constraints to empty — this mirrors
        # pre-B5 state for a field whose glossary has no capability.default.
        db._status["fields"]["target_temperature"]["active_constraints"] = {}
        out = db._apply_constraint_gate(
            {"target_temperature": 99.0}, af, effective_mode=2,
        )
        assert out["target_temperature"] == 99.0

    def test_unknown_field_passthrough(self):
        db, af = _db()
        out = db._apply_constraint_gate(
            {"not_a_field": 12345}, af, effective_mode=2,
        )
        assert out["not_a_field"] == 12345


class TestCommandIntegration:
    """End-to-end through command() — verify gate runs in the pipeline."""

    @pytest.mark.asyncio
    async def test_clamp_reaches_encoder(self, caplog):
        db, _ = _db()
        write_field(db._status, "operating_mode", 2, ts=1.0)  # cool
        write_field(db._status, "power", True, ts=1.0)
        _seed_constraints(db, "target_temperature", {"valid_range": [17.0, 29.0]})

        sent = []
        async def fake_send(frame_hex: str) -> None:
            sent.append(frame_hex)

        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            result = await db.command({"target_temperature": 40.0}, fake_send)

        assert result["expanded"]["target_temperature"] == 29.0
        assert any("clamped" in r.message for r in caplog.records)
