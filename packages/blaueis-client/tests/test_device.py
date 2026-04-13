"""Tests for Device class — lifecycle, field registration, frame processing.

No real network — uses mocked HvacClient transport.

Usage:  python -m pytest packages/blaueis-client/tests/test_device.py -v
"""

import asyncio
import json

import pytest

from blaueis.client.device import Device
from blaueis.client.ws_client import HvacClient
from blaueis.core.frame import parse_frame
from blaueis.core.codec import identify_frame

from tests.conftest import B5_EXTENDED_HEX, B5_SIMPLE_HEX, C0_STATUS_HEX, MockWebSocket


# ── Helpers ─────────────────────────────────────────────────


def _make_device(**kwargs) -> Device:
    """Create a Device with defaults for testing."""
    return Device(
        host="127.0.0.1",
        port=8765,
        no_encrypt=True,
        poll_interval=999,  # effectively disable auto-polling in tests
        **kwargs,
    )


async def _inject_ws(device: Device, ws: MockWebSocket):
    """Inject a mock WebSocket into the device's client, skipping real connect."""
    client = HvacClient(device.host, device.port, no_encrypt=True)
    client._ws = ws
    device._client = client
    client.add_listener(device._on_gateway_message)


# ── Init tests ──────────────────────────────────────────────


def test_init_defaults():
    d = _make_device()
    assert d.host == "127.0.0.1"
    assert d.port == 8765
    assert d.connected is False
    assert d.capabilities_received is False
    # 'always' and 'readable' fields are available before B5;
    # only 'capability' fields need B5 confirmation
    avail = d.available_fields
    assert len(avail) > 0
    assert all(
        f["feature_available"] in ("always", "readable")
        for f in avail.values()
    )


def test_init_status_has_fields():
    d = _make_device()
    assert "fields" in d.status
    assert "meta" in d.status
    assert d.status["meta"]["phase"] == "boot"


# ── Frame processing tests ──────────────────────────────────


def test_process_b5_frames():
    """B5 frames should update capabilities and available fields."""
    d = _make_device()

    # Process B5 extended
    d._process_frame(B5_EXTENDED_HEX)
    assert d._status["meta"].get("b5_received") is True

    # Process B5 simple
    d._process_frame(B5_SIMPLE_HEX)

    # Finalize
    from blaueis.core.process import finalize_capabilities
    finalize_capabilities(d._status, d._glossary)

    avail = d.available_fields
    assert len(avail) > 0
    # These should be available on any Midea AC
    assert "power" in avail
    assert "operating_mode" in avail
    assert "target_temperature" in avail
    assert "indoor_temperature" in avail


def test_process_c0_frame():
    """C0 frame should populate status fields."""
    d = _make_device()

    # Need B5 first to gate fields
    d._process_frame(B5_EXTENDED_HEX)
    d._process_frame(B5_SIMPLE_HEX)
    from blaueis.core.process import finalize_capabilities
    finalize_capabilities(d._status, d._glossary)

    # Now process C0
    d._process_frame(C0_STATUS_HEX)

    # Read fields
    assert d.read("power") is False  # AC is off in our capture
    assert d.read("operating_mode") == 4  # heat
    assert d.read("target_temperature") == 22
    assert d.read("indoor_temperature") == pytest.approx(21.1, abs=0.5)


def test_process_c0_without_b5():
    """C0 frame should still decode always-available fields without B5."""
    d = _make_device()
    d._process_frame(C0_STATUS_HEX)

    # 'always' fields should decode even without B5
    assert d.read("power") is False
    assert d.read("target_temperature") == 22


def test_tx_frames_ignored():
    """TX direction frames should not be processed."""
    d = _make_device()
    msg = {
        "type": "frame",
        "hex": C0_STATUS_HEX,
        "ts": 1.0,
        "dir": "tx",
    }
    d._on_gateway_message(msg)
    # No status change — power should be None (never read)
    assert d.read("power") is None


# ── Change detection tests ──────────────────────────────────


def test_state_change_callback():
    """on_state_change should fire when field values change."""
    d = _make_device()
    d._process_frame(B5_EXTENDED_HEX)
    d._process_frame(B5_SIMPLE_HEX)
    from blaueis.core.process import finalize_capabilities
    finalize_capabilities(d._status, d._glossary)

    # Register fields we care about
    d.register_fields(["power", "target_temperature"])

    changes = []
    d.on_state_change = lambda field, new, old: changes.append((field, new, old))

    # First C0 — transition from None to actual values
    d._process_frame(C0_STATUS_HEX)

    # Should have changes for both registered fields
    changed_fields = {c[0] for c in changes}
    assert "power" in changed_fields
    assert "target_temperature" in changed_fields


# ── Field registration tests ────────────────────────────────


def test_register_fields_computes_queries():
    d = _make_device()
    d.register_fields(["power", "operating_mode", "target_temperature"])
    # All these come from C0, which needs cmd_0x41
    assert "cmd_0x41" in d.required_queries


def test_register_all_available():
    d = _make_device()
    d._process_frame(B5_EXTENDED_HEX)
    d._process_frame(B5_SIMPLE_HEX)
    from blaueis.core.process import finalize_capabilities
    finalize_capabilities(d._status, d._glossary)

    d.register_all_available()
    assert len(d._registered_fields) > 10
    assert "cmd_0x41" in d.required_queries


# ── Read convenience ────────────────────────────────────────


def test_read_returns_none_for_unknown():
    d = _make_device()
    assert d.read("nonexistent_field") is None


def test_read_full_returns_metadata():
    d = _make_device()
    d._process_frame(C0_STATUS_HEX)
    r = d.read_full("power")
    assert r is not None
    assert "value" in r
    assert "source" in r
    assert "ts" in r
    assert r["source"] == "rsp_0xc0"


def test_read_all_registered():
    d = _make_device()
    d._process_frame(C0_STATUS_HEX)
    d.register_fields(["power", "target_temperature", "indoor_temperature"])
    result = d.read_all_registered()
    assert "power" in result
    assert "target_temperature" in result
    assert result["power"] is False
    assert result["target_temperature"] == 22


# ── Query deduplication ─────────────────────────────────────


def test_response_to_query_mapping():
    assert Device._response_to_query("rsp_0xc0") == "cmd_0x41"
    assert Device._response_to_query("rsp_0xb5") == "cmd_0xb5"
    assert Device._response_to_query("rsp_0xb1") is None
    assert Device._response_to_query("rsp_0xa1") == "cmd_0x41"


# ── Available fields structure ──────────────────────────────


def test_available_fields_structure():
    d = _make_device()
    d._process_frame(B5_EXTENDED_HEX)
    d._process_frame(B5_SIMPLE_HEX)
    from blaueis.core.process import finalize_capabilities
    finalize_capabilities(d._status, d._glossary)

    avail = d.available_fields
    # Check structure of a known field
    power = avail.get("power")
    assert power is not None
    assert "field_class" in power
    assert "data_type" in power
    assert "writable" in power
    assert "feature_available" in power
    assert power["data_type"] == "bool"
