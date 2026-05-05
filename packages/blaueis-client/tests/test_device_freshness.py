"""Device.is_fresh() — single-timestamp staleness window.

Pre-first-ingest the device is treated as not fresh (callers fall
through to the ``connected`` check for the boot phase). Once
``meta.last_ingest_at`` is populated, ``is_fresh()`` returns True
within ``poll_interval × staleness_factor`` and False outside it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from blaueis.client.device import Device


def _make_device(**kwargs) -> Device:
    return Device(
        host="127.0.0.1",
        port=8765,
        no_encrypt=True,
        poll_interval=10.0,
        **kwargs,
    )


def _set_ingest_at(device: Device, dt: datetime) -> None:
    device.status["meta"]["last_ingest_at"] = dt.isoformat()


# ── Pre-first-ingest ─────────────────────────────────────────────────


def test_is_fresh_false_before_first_ingest() -> None:
    d = _make_device()
    assert d.last_ingest_at is None
    assert d.is_fresh() is False


# ── Within window ───────────────────────────────────────────────────


def test_is_fresh_true_within_window() -> None:
    d = _make_device()
    _set_ingest_at(d, datetime.now(UTC) - timedelta(seconds=5))
    assert d.is_fresh() is True  # 5 s < 10 s × 2


# ── Outside window ──────────────────────────────────────────────────


def test_is_fresh_false_outside_window() -> None:
    d = _make_device()
    _set_ingest_at(d, datetime.now(UTC) - timedelta(seconds=30))
    assert d.is_fresh() is False  # 30 s > 10 s × 2


# ── Custom factor ───────────────────────────────────────────────────


def test_is_fresh_custom_staleness_factor() -> None:
    d = _make_device()
    _set_ingest_at(d, datetime.now(UTC) - timedelta(seconds=25))
    assert d.is_fresh(staleness_factor=2.0) is False  # 25 > 20
    assert d.is_fresh(staleness_factor=3.0) is True  # 25 < 30


# ── Robustness ──────────────────────────────────────────────────────


def test_is_fresh_handles_naive_timestamp() -> None:
    """Older code paths might persist a naive ISO string. Treat as UTC."""
    d = _make_device()
    naive = (datetime.now(UTC) - timedelta(seconds=5)).replace(tzinfo=None)
    d.status["meta"]["last_ingest_at"] = naive.isoformat()
    assert d.is_fresh() is True


def test_is_fresh_handles_malformed_timestamp() -> None:
    """A garbage string in the slot must not raise — return False."""
    d = _make_device()
    d.status["meta"]["last_ingest_at"] = "not-a-timestamp"
    assert d.is_fresh() is False
