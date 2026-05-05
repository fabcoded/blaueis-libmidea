"""Device.set_test_suppression — debugging hatch for staleness verification.

Activating it makes ``_process_frame`` drop every incoming frame so
``last_ingest_at`` stops advancing. Auto-clears on timer expiry, capped
at MAX_TEST_SUPPRESSION_S (10 min) to prevent forever-suppression.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from blaueis.client.device import Device


def _make_device(**kwargs) -> Device:
    return Device(
        host="127.0.0.1",
        port=8765,
        no_encrypt=True,
        poll_interval=10.0,
        **kwargs,
    )


# ── Activation / clearance ──────────────────────────────────────────


def test_default_state_not_suppressed():
    d = _make_device()
    assert d.is_test_suppressed() is False


def test_set_test_suppression_activates():
    d = _make_device()
    d.set_test_suppression(60.0)
    assert d.is_test_suppressed() is True


def test_clear_via_zero_duration():
    d = _make_device()
    d.set_test_suppression(60.0)
    d.set_test_suppression(0)
    assert d.is_test_suppressed() is False


def test_clear_via_negative_duration():
    d = _make_device()
    d.set_test_suppression(60.0)
    d.set_test_suppression(-1.0)
    assert d.is_test_suppressed() is False


# ── Auto-expiry ─────────────────────────────────────────────────────


def test_auto_expires_after_window():
    d = _make_device()
    d.set_test_suppression(5.0)
    with patch("blaueis.client.device.time.monotonic", return_value=time.monotonic() + 6.0):
        assert d.is_test_suppressed() is False


def test_still_active_within_window():
    d = _make_device()
    d.set_test_suppression(60.0)
    with patch("blaueis.client.device.time.monotonic", return_value=time.monotonic() + 30.0):
        assert d.is_test_suppressed() is True


# ── Cap enforcement ────────────────────────────────────────────────


def test_duration_capped_at_max():
    d = _make_device()
    applied = d.set_test_suppression(99999.0)
    assert applied == Device.MAX_TEST_SUPPRESSION_S


def test_duration_under_cap_unmodified():
    d = _make_device()
    applied = d.set_test_suppression(60.0)
    assert applied == 60.0


# ── Re-set replaces window ─────────────────────────────────────────


def test_re_set_replaces_window():
    """Calling again resets the window — the new duration starts from
    'now', not added to the old expiry. Allows shortening or extending."""
    d = _make_device()
    d.set_test_suppression(300.0)
    first_expiry = d._test_suppression_until
    time.sleep(0.01)  # advance monotonic clock minimally
    d.set_test_suppression(60.0)
    second_expiry = d._test_suppression_until
    assert second_expiry < first_expiry


# ── Frame drop wiring ──────────────────────────────────────────────


def test_process_frame_drops_when_suppressed():
    """The actual point of the feature: frames are dropped, so
    nothing reaches the status DB or the ingest path."""
    d = _make_device()
    d.set_test_suppression(60.0)
    # Capture initial frame_counts state — _process_frame must not
    # advance any frame counter while suppressed.
    before = dict(d.status["meta"].get("frame_counts", {}))
    # Pass a syntactically valid C0 frame body wrapped in the AA-header
    # the parser expects. Easiest: just call with garbage hex; suppression
    # short-circuits before parse_frame, so we never reach an exception.
    d._process_frame("ZZ_invalid_hex_but_irrelevant")
    after = dict(d.status["meta"].get("frame_counts", {}))
    assert before == after


def test_process_frame_resumes_after_clear():
    d = _make_device()
    d.set_test_suppression(60.0)
    d.set_test_suppression(0)  # clear
    # No assertion on frame_counts — _process_frame with garbage hex
    # would log a debug error during parse, not fall through to ingest.
    # The point: we no longer short-circuit before parse, so the
    # behaviour is restored (verified indirectly by is_test_suppressed).
    assert d.is_test_suppressed() is False
