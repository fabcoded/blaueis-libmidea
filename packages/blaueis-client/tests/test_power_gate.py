"""Tests for StatusDB._apply_power_gate — power-off write lock.

Policy: when current power is False AND the batch does not set power=True,
only `power` itself may be written. Any other field is rejected so ghost
optimistic writes don't desynchronize status from hardware (the device
ignores non-power writes while off).
"""
from __future__ import annotations

import logging

import pytest
from blaueis.client.status_db import StatusDB
from blaueis.core.query import write_field


class TestPowerGate:
    def test_power_off_blocks_non_power(self, caplog):
        db = StatusDB()
        write_field(db._status, "power", False, ts=1.0)
        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            accepted, rejected = db._apply_power_gate(
                {"target_temperature": 22.0},
            )
        assert "target_temperature" in rejected
        assert "device is off" in rejected["target_temperature"]
        assert accepted == {}

    def test_power_off_allows_power_field(self):
        db = StatusDB()
        write_field(db._status, "power", False, ts=1.0)
        accepted, rejected = db._apply_power_gate({"power": False})
        assert accepted == {"power": False}
        assert rejected == {}

    def test_power_off_but_batch_turns_on(self):
        """power=True in batch unlocks the entire batch."""
        db = StatusDB()
        write_field(db._status, "power", False, ts=1.0)
        accepted, rejected = db._apply_power_gate(
            {"power": True, "target_temperature": 22.0, "operating_mode": 2},
        )
        assert accepted == {
            "power": True, "target_temperature": 22.0, "operating_mode": 2,
        }
        assert rejected == {}

    def test_power_on_no_restriction(self):
        db = StatusDB()
        write_field(db._status, "power", True, ts=1.0)
        accepted, rejected = db._apply_power_gate(
            {"target_temperature": 22.0, "operating_mode": 2},
        )
        assert len(accepted) == 2
        assert rejected == {}

    def test_power_unknown_no_restriction(self):
        """Pre-poll state (power=None) — don't block anything."""
        db = StatusDB()
        # Fresh StatusDB; power not yet read from device
        assert db.read("power") is None
        accepted, rejected = db._apply_power_gate({"target_temperature": 22.0})
        assert accepted == {"target_temperature": 22.0}
        assert rejected == {}

    def test_power_off_with_explicit_false_in_batch_still_blocks(self):
        """power=False in batch is a no-op toward off — doesn't unlock."""
        db = StatusDB()
        write_field(db._status, "power", False, ts=1.0)
        accepted, rejected = db._apply_power_gate(
            {"power": False, "target_temperature": 22.0},
        )
        assert "target_temperature" in rejected
        assert accepted == {"power": False}

    def test_partial_batch_some_allowed(self):
        """power + temp when off: power through, temp rejected."""
        db = StatusDB()
        write_field(db._status, "power", False, ts=1.0)
        accepted, rejected = db._apply_power_gate(
            {"power": False, "fan_speed": 60},
        )
        assert accepted == {"power": False}
        assert "fan_speed" in rejected


class TestCommandIntegration:
    @pytest.mark.asyncio
    async def test_power_off_rejects_in_pipeline(self):
        db = StatusDB()
        write_field(db._status, "operating_mode", 2, ts=1.0)
        write_field(db._status, "power", False, ts=1.0)

        sent = []
        async def fake_send(frame_hex: str) -> None:
            sent.append(frame_hex)

        result = await db.command({"target_temperature": 22.0}, fake_send)
        assert "target_temperature" in result["rejected"]
        assert sent == []  # no frame went to the wire

    @pytest.mark.asyncio
    async def test_power_on_and_temp_in_same_batch(self):
        db = StatusDB()
        write_field(db._status, "operating_mode", 2, ts=1.0)
        write_field(db._status, "power", False, ts=1.0)

        sent = []
        async def fake_send(frame_hex: str) -> None:
            sent.append(frame_hex)

        result = await db.command(
            {"power": True, "target_temperature": 22.0}, fake_send,
        )
        assert result["expanded"].get("power") is True
        assert result["expanded"].get("target_temperature") == 22.0
        assert result["rejected"] == {}
