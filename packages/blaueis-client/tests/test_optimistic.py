"""Tests for StatusDB optimistic write path — value persistence + callback dispatch."""
from __future__ import annotations

from blaueis.client.status_db import StatusDB
from blaueis.core.query import read_field, write_field


def _fresh_db() -> StatusDB:
    """StatusDB instance for optimistic write testing."""
    return StatusDB()


def test_writes_new_value_into_status() -> None:
    db = _fresh_db()
    db._apply_optimistic({"eco_mode": True})
    r = read_field(db._status, "eco_mode")
    assert r is not None
    assert r["value"] is True


def test_fires_state_change_callback() -> None:
    db = _fresh_db()
    events: list[tuple] = []
    db.on_state_change = lambda f, new, old: events.append((f, new, old))

    db._apply_optimistic({"eco_mode": True, "target_temperature": 22})
    db._flush_events()

    names = {e[0] for e in events}
    assert "eco_mode" in names and "target_temperature" in names
    for _f, _new, old in events:
        assert old is None  # status started empty


def test_no_callback_when_value_unchanged() -> None:
    db = _fresh_db()
    write_field(db._status, "eco_mode", True, ts=1.0)
    events: list[tuple] = []
    db.on_state_change = lambda f, new, old: events.append(f)

    db._apply_optimistic({"eco_mode": True})   # same value
    db._flush_events()
    assert events == []


def test_callback_exception_does_not_break_other_fields() -> None:
    db = _fresh_db()
    caught: list[str] = []

    def cb(field: str, new, old):
        caught.append(field)
        if field == "eco_mode":
            raise RuntimeError("explode")

    db.on_state_change = cb
    db._apply_optimistic({"eco_mode": True, "target_temperature": 22})
    db._flush_events()
    # Both callbacks were attempted despite the first one raising.
    assert set(caught) == {"eco_mode", "target_temperature"}
    # Both values landed in status.
    assert read_field(db._status, "eco_mode")["value"] is True
    assert read_field(db._status, "target_temperature")["value"] == 22


def test_optimistic_slot_loses_to_newer_real_slot() -> None:
    """The optimistic slot ts is an ISO string (matching the decoder
    convention); a subsequent real response with a newer ISO ts must
    win the read. Mixed float/string ts would raise in _newest.max()."""
    from datetime import UTC, datetime
    db = _fresh_db()
    db._apply_optimistic({"eco_mode": True})

    # Simulate a real AC response arriving a minute later — ISO string ts
    # is what process_data_frame actually writes.
    later_iso = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
    write_field(db._status, "eco_mode", False,
                source="rsp_0xc0", generation="legacy",
                ts=later_iso)

    r = read_field(db._status, "eco_mode")
    assert r["value"] is False
    assert r["source"] == "rsp_0xc0"
