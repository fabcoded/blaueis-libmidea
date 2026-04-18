"""Tests for StatusDB._apply_feature_gate — feature_available='never' rejection.

Policy: if B5 capability processing marked a field as feature_available=never,
the device does not support the feature. Reject the write with a stable reason
string; never raise.
"""
from __future__ import annotations

import logging

import pytest
from blaueis.client.status_db import StatusDB
from blaueis.core.query import write_field


class TestFeatureGate:
    def test_never_rejects(self, caplog):
        db = StatusDB()
        db._status["fields"].setdefault("breezeless", {})["feature_available"] = "never"
        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            accepted, rejected = db._apply_feature_gate({"breezeless": True})
        assert "breezeless" in rejected
        assert "breezeless" not in accepted
        assert "not supported" in rejected["breezeless"]

    def test_always_accepts(self):
        db = StatusDB()
        db._status["fields"].setdefault("breezeless", {})["feature_available"] = "always"
        accepted, rejected = db._apply_feature_gate({"breezeless": True})
        assert accepted == {"breezeless": True}
        assert rejected == {}

    def test_capability_accepts(self):
        db = StatusDB()
        db._status["fields"].setdefault("breezeless", {})["feature_available"] = "capability"
        accepted, rejected = db._apply_feature_gate({"breezeless": True})
        assert accepted == {"breezeless": True}
        assert rejected == {}

    def test_missing_key_accepts(self):
        """Pre-B5 state: feature_available unset → default 'always' → accept."""
        db = StatusDB()
        db._status["fields"].setdefault("power", {}).pop("feature_available", None)
        accepted, rejected = db._apply_feature_gate({"power": True})
        assert accepted == {"power": True}
        assert rejected == {}

    def test_unknown_field_accepts(self):
        """Status has no entry for the field — treat as 'always' (accept)."""
        db = StatusDB()
        accepted, rejected = db._apply_feature_gate({"not_a_field": 1})
        assert accepted == {"not_a_field": 1}


class TestCommandIntegration:
    @pytest.mark.asyncio
    async def test_never_blocks_command(self, caplog):
        db = StatusDB()
        write_field(db._status, "operating_mode", 2, ts=1.0)
        write_field(db._status, "power", True, ts=1.0)
        db._status["fields"].setdefault("breezeless", {})["feature_available"] = "never"

        sent = []
        async def fake_send(frame_hex: str) -> None:
            sent.append(frame_hex)

        with caplog.at_level(logging.WARNING, logger="blaueis.device"):
            result = await db.command({"breezeless": True}, fake_send)

        assert "breezeless" in result["rejected"]
        assert "breezeless" not in result["expanded"]

    @pytest.mark.asyncio
    async def test_never_does_not_block_other_fields(self):
        """Mixed batch: never-field rejected, others pass through."""
        db = StatusDB()
        write_field(db._status, "operating_mode", 2, ts=1.0)
        write_field(db._status, "power", True, ts=1.0)
        db._status["fields"].setdefault("breezeless", {})["feature_available"] = "never"

        sent = []
        async def fake_send(frame_hex: str) -> None:
            sent.append(frame_hex)

        result = await db.command(
            {"breezeless": True, "target_temperature": 22.0}, fake_send,
        )
        assert "breezeless" in result["rejected"]
        assert result["expanded"].get("target_temperature") == 22.0
