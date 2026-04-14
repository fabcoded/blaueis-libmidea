"""Tests for Device._apply_optimistic — optimistic status write + callback."""
from __future__ import annotations

import logging

from blaueis.client.device import Device
from blaueis.core.query import read_field, write_field


def _fresh_device() -> Device:
    """Bare Device instance without network init — good enough to exercise
    the optimistic path, which only touches `_status` and `on_state_change`."""
    d = Device.__new__(Device)
    d._status = {"fields": {}, "meta": {}}
    d.on_state_change = None
    return d


def test_writes_new_value_into_status() -> None:
    d = _fresh_device()
    d._apply_optimistic({"eco_mode": True})
    r = read_field(d._status, "eco_mode")
    assert r is not None
    assert r["value"] is True


def test_fires_state_change_callback() -> None:
    d = _fresh_device()
    events: list[tuple] = []
    d.on_state_change = lambda f, new, old: events.append((f, new, old))

    d._apply_optimistic({"eco_mode": True, "target_temperature": 22})

    names = {e[0] for e in events}
    assert "eco_mode" in names and "target_temperature" in names
    for f, new, old in events:
        assert old is None  # status started empty


def test_no_callback_when_value_unchanged() -> None:
    d = _fresh_device()
    write_field(d._status, "eco_mode", True, ts=1.0)
    events: list[tuple] = []
    d.on_state_change = lambda f, new, old: events.append(f)

    d._apply_optimistic({"eco_mode": True})   # same value
    assert events == []


def test_callback_exception_does_not_break_other_fields() -> None:
    d = _fresh_device()
    caught: list[str] = []

    def cb(field: str, new, old):
        caught.append(field)
        if field == "eco_mode":
            raise RuntimeError("explode")

    d.on_state_change = cb
    d._apply_optimistic({"eco_mode": True, "target_temperature": 22})
    # Both callbacks were attempted despite the first one raising.
    assert set(caught) == {"eco_mode", "target_temperature"}
    # Both values landed in status.
    assert read_field(d._status, "eco_mode")["value"] is True
    assert read_field(d._status, "target_temperature")["value"] == 22


def test_optimistic_slot_loses_to_newer_real_slot() -> None:
    """The optimistic slot ts is an ISO string (matching the decoder
    convention); a subsequent real response with a newer ISO ts must
    win the read. Mixed float/string ts would raise in _newest.max()."""
    from datetime import UTC, datetime
    d = _fresh_device()
    d._apply_optimistic({"eco_mode": True})

    # Simulate a real AC response arriving a minute later — ISO string ts
    # is what process_data_frame actually writes.
    later_iso = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
    write_field(d._status, "eco_mode", False,
                source="rsp_0xc0", generation="legacy",
                ts=later_iso)

    r = read_field(d._status, "eco_mode")
    assert r["value"] is False
    assert r["source"] == "rsp_0xc0"
