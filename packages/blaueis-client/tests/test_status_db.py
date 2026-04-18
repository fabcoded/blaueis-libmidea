"""Tests for StatusDB — command integration, event handling, lock behavior.

Tests the full StatusDB workflow: locked operations, event deduplication,
callback dispatch, and read consistency.

Usage:  python -m pytest packages/blaueis-client/tests/test_status_db.py -v
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from blaueis.client.status_db import StatusDB
from blaueis.core.codec import identify_frame, walk_fields
from blaueis.core.frame import parse_frame
from blaueis.core.query import write_field

from tests.conftest import C0_STATUS_HEX

# ── Helpers ──────────────────────────────────────────────────


def _db_with_mode(mode_val: int) -> StatusDB:
    db = StatusDB()
    write_field(db._status, "operating_mode", mode_val, ts=1.0)
    return db


def _c0_body() -> tuple[bytes, str]:
    """Parse C0 test fixture into (body, protocol_key)."""
    raw = bytes.fromhex(C0_STATUS_HEX.replace(" ", ""))
    parsed = parse_frame(raw)
    return parsed["body"], identify_frame(parsed["body"])


async def _populate(db: StatusDB) -> None:
    """Ingest C0 frame to populate fields with fresh data (passes preflight)."""
    body, pkey = _c0_body()
    avail = {name: {} for name in walk_fields(db._glossary)}
    await db.ingest(body, pkey, available_fields=avail)


# ── Command integration ─────────────────────────────────────


class TestCommand:
    async def test_returns_expanded_rejected_results(self):
        db = _db_with_mode(2)  # cool
        send = AsyncMock()
        result = await db.command(
            {"frost_protection": True, "power": True},
            send_fn=send,
        )
        assert "expanded" in result
        assert "rejected" in result
        assert "results" in result
        assert "frost_protection" in result["rejected"]
        assert "power" in result["expanded"]

    async def test_optimistic_write_applied(self):
        db = _db_with_mode(4)  # heat
        send = AsyncMock()
        await db.command({"frost_protection": True}, send_fn=send)
        assert db.read("frost_protection") is True

    async def test_callbacks_fire(self):
        db = _db_with_mode(4)  # heat
        events: list[tuple] = []
        db.on_state_change = lambda f, n, o: events.append((f, n, o))
        send = AsyncMock()
        await db.command({"frost_protection": True}, send_fn=send)
        changed = {e[0] for e in events}
        assert "frost_protection" in changed

    async def test_mode_gate_blocks_and_no_frame_sent(self):
        db = _db_with_mode(2)  # cool
        send = AsyncMock()
        result = await db.command({"frost_protection": True}, send_fn=send)
        assert "frost_protection" in result["rejected"]
        assert len(result["expanded"]) == 0
        send.assert_not_called()

    async def test_expansion_forces_included(self):
        db = _db_with_mode(4)
        send = AsyncMock()
        result = await db.command({"frost_protection": True}, send_fn=send)
        exp = result["expanded"]
        assert exp["frost_protection"] is True
        assert exp["turbo_mode"] == 0
        assert exp["eco_mode"] == 0

    async def test_frame_sent_after_c0_populate(self):
        """With C0 data populated, preflight passes and frames are sent."""
        db = StatusDB()
        await _populate(db)
        send = AsyncMock()
        await db.command({"power": True}, send_fn=send)
        assert send.call_count >= 1

    async def test_all_rejected_means_empty_expanded(self):
        db = _db_with_mode(5)  # fan
        send = AsyncMock()
        result = await db.command(
            {"frost_protection": True, "jet_cool": True},
            send_fn=send,
        )
        assert len(result["expanded"]) == 0
        assert "frost_protection" in result["rejected"]
        assert "jet_cool" in result["rejected"]


# ── Ingest ───────────────────────────────────────────────────


class TestIngest:
    async def test_populates_fields(self):
        db = StatusDB()
        body, pkey = _c0_body()
        avail = {"power": {}, "target_temperature": {}, "operating_mode": {}}
        await db.ingest(body, pkey, available_fields=avail)
        assert db.read("power") is False
        assert db.read("target_temperature") == 22
        assert db.read("operating_mode") == 4

    async def test_fires_callbacks_on_change(self):
        db = StatusDB()
        events: list[tuple] = []
        db.on_state_change = lambda f, n, o: events.append((f, n, o))
        body, pkey = _c0_body()
        avail = {"power": {}, "target_temperature": {}}
        await db.ingest(body, pkey, available_fields=avail)
        changed = {e[0] for e in events}
        assert "power" in changed
        assert "target_temperature" in changed

    async def test_no_callback_on_same_value(self):
        db = StatusDB()
        body, pkey = _c0_body()
        avail = {"power": {}}
        # First ingest — sets power=False
        await db.ingest(body, pkey, available_fields=avail)

        events: list[tuple] = []
        db.on_state_change = lambda f, n, o: events.append((f, n, o))
        # Second ingest of same frame — no change
        await db.ingest(body, pkey, available_fields=avail)
        power_events = [e for e in events if e[0] == "power"]
        assert len(power_events) == 0

    async def test_no_callbacks_without_available_fields(self):
        db = StatusDB()
        events: list[tuple] = []
        db.on_state_change = lambda f, n, o: events.append((f, n, o))
        body, pkey = _c0_body()
        await db.ingest(body, pkey, available_fields=None)
        assert len(events) == 0


# ── Event deduplication ──────────────────────────────────────


class TestEventDedup:
    def test_keeps_first_old_last_new(self):
        db = StatusDB()
        events: list[tuple] = []
        db.on_state_change = lambda f, n, o: events.append((f, n, o))
        db._pending_events.append(("eco_mode", True, None))
        db._pending_events.append(("eco_mode", False, True))
        db._flush_events()
        assert len(events) == 1
        assert events[0] == ("eco_mode", False, None)

    def test_drops_no_net_change(self):
        db = StatusDB()
        events: list[tuple] = []
        db.on_state_change = lambda f, n, o: events.append((f, n, o))
        db._pending_events.append(("eco_mode", True, None))
        db._pending_events.append(("eco_mode", None, True))
        db._flush_events()
        assert len(events) == 0

    def test_multiple_fields_each_fire_once(self):
        db = StatusDB()
        events: list[tuple] = []
        db.on_state_change = lambda f, n, o: events.append((f, n, o))
        db._pending_events.append(("eco_mode", True, None))
        db._pending_events.append(("turbo_mode", True, None))
        db._pending_events.append(("eco_mode", False, True))
        db._flush_events()
        assert len(events) == 2
        by_field = {e[0]: e for e in events}
        assert by_field["eco_mode"] == ("eco_mode", False, None)
        assert by_field["turbo_mode"] == ("turbo_mode", True, None)

    def test_clears_pending_without_callback(self):
        db = StatusDB()
        db.on_state_change = None
        db._pending_events.append(("eco_mode", True, None))
        db._flush_events()
        assert len(db._pending_events) == 0

    def test_callback_exception_continues(self):
        db = StatusDB()
        caught: list[str] = []

        def cb(f, n, o):
            caught.append(f)
            if f == "eco_mode":
                raise RuntimeError("boom")

        db.on_state_change = cb
        db._pending_events.append(("eco_mode", True, None))
        db._pending_events.append(("turbo_mode", True, None))
        db._flush_events()
        assert set(caught) == {"eco_mode", "turbo_mode"}


# ── Lock behavior ────────────────────────────────────────────


class TestLockBehavior:
    async def test_lock_held_during_send(self):
        """send_fn runs inside the lock."""
        db = StatusDB()
        await _populate(db)
        lock_states: list[bool] = []

        async def check_send(frame_hex):
            lock_states.append(db._lock.locked())

        await db.command({"power": True}, send_fn=check_send)
        assert all(lock_states), "Lock should be held during send"

    async def test_callback_reads_without_deadlock(self):
        """Callbacks fire outside lock — read() during callback is safe."""
        db = _db_with_mode(4)  # heat
        read_values: list = []

        def cb(f, n, o):
            read_values.append(db.read("operating_mode"))

        db.on_state_change = cb
        send = AsyncMock()
        await db.command({"frost_protection": True}, send_fn=send)
        assert len(read_values) > 0
        assert read_values[0] == 4

    async def test_lock_not_held_during_callback(self):
        db = _db_with_mode(4)
        lock_during_cb: list[bool] = []

        def cb(f, n, o):
            lock_during_cb.append(db._lock.locked())

        db.on_state_change = cb
        send = AsyncMock()
        await db.command({"eco_mode": True}, send_fn=send)
        assert not any(lock_during_cb), "Lock should NOT be held during callbacks"

    async def test_concurrent_commands_serialize(self):
        """Two concurrent commands don't interleave their sends."""
        db = StatusDB()
        await _populate(db)
        order: list[str] = []

        async def send_a(frame_hex):
            order.append("a_start")
            await asyncio.sleep(0.02)
            order.append("a_end")

        async def send_b(frame_hex):
            order.append("b_start")
            await asyncio.sleep(0.02)
            order.append("b_end")

        await asyncio.gather(
            db.command({"power": True}, send_fn=send_a),
            db.command({"power": False}, send_fn=send_b),
        )
        # Both should have sent (commands are valid after C0 populate)
        # If both sent, verify serialization (no interleaving)
        if "a_start" in order and "b_start" in order:
            a_start = order.index("a_start")
            b_start = order.index("b_start")
            if a_start < b_start:
                assert order.index("a_end") < b_start
            else:
                assert order.index("b_end") < a_start


# ── Read ─────────────────────────────────────────────────────


class TestRead:
    def test_read_unknown_returns_none(self):
        db = StatusDB()
        assert db.read("nonexistent") is None

    def test_read_returns_value(self):
        db = StatusDB()
        write_field(db._status, "power", True, ts=1.0)
        assert db.read("power") is True

    def test_read_field_returns_metadata(self):
        db = StatusDB()
        write_field(db._status, "power", True, ts=1.0, source="test")
        r = db.read_field("power")
        assert r is not None
        assert r["value"] is True
        assert "ts" in r

    def test_read_field_unknown_returns_none(self):
        db = StatusDB()
        assert db.read_field("nonexistent") is None
