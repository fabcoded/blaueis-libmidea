"""Tests for Device class — lifecycle, field registration, frame processing.

No real network — uses mocked HvacClient transport.

Usage:  python -m pytest packages/blaueis-client/tests/test_device.py -v
"""

import asyncio

import pytest
from blaueis.client.device import Device
from blaueis.client.ws_client import HvacClient
from blaueis.core.frame import parse_frame

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
    # 'always', 'readable', and the '-opt' soft-disabled variants are
    # available before B5; 'capability' / 'capability-opt' need B5
    # confirmation; 'excluded' is always excluded.
    avail = d.available_fields
    assert len(avail) > 0
    assert all(
        f["feature_available"] in ("always", "readable", "readable-opt")
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


async def test_process_c0_frame():
    """C0 frame should populate status fields."""
    d = _make_device()

    # Need B5 first to gate fields
    d._process_frame(B5_EXTENDED_HEX)
    d._process_frame(B5_SIMPLE_HEX)
    from blaueis.core.process import finalize_capabilities
    finalize_capabilities(d._status, d._glossary)

    # Now process C0 — ingest runs as a scheduled task
    d._process_frame(C0_STATUS_HEX)
    await asyncio.sleep(0)

    # Read fields
    assert d.read("power") is False  # AC is off in our capture
    assert d.read("operating_mode") == 4  # heat
    assert d.read("target_temperature") == 22
    assert d.read("indoor_temperature") == pytest.approx(21.1, abs=0.5)


async def test_process_c0_without_b5():
    """C0 frame should still decode always-available fields without B5."""
    d = _make_device()
    d._process_frame(C0_STATUS_HEX)
    await asyncio.sleep(0)

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


async def test_state_change_callback():
    """on_state_change should fire when field values change."""
    d = _make_device()
    d._process_frame(B5_EXTENDED_HEX)
    d._process_frame(B5_SIMPLE_HEX)
    from blaueis.core.process import finalize_capabilities
    finalize_capabilities(d._status, d._glossary)

    changes = []
    d.on_state_change = lambda field, new, old: changes.append((field, new, old))

    # First C0 — transition from None to actual values (ingest is async)
    d._process_frame(C0_STATUS_HEX)
    await asyncio.sleep(0)

    # Should have changes for available fields decoded from C0
    changed_fields = {c[0] for c in changes}
    assert "power" in changed_fields
    assert "target_temperature" in changed_fields


# ── Query computation tests (database-driven) ───────────────


def test_required_queries_from_database():
    """required_queries are derived from the status database, not registration."""
    d = _make_device()
    # Before B5: only 'always'/'readable' fields → still need cmd_0x41
    assert "cmd_0x41" in d.required_queries


def test_required_queries_include_groups_after_b5():
    d = _make_device()
    d._process_frame(B5_EXTENDED_HEX)
    d._process_frame(B5_SIMPLE_HEX)
    from blaueis.core.process import finalize_capabilities
    finalize_capabilities(d._status, d._glossary)

    queries = d.required_queries
    assert "cmd_0x41" in queries
    # C1 group fields are available → group queries needed
    assert "cmd_0xc1_group1" in queries


# ── Read convenience ────────────────────────────────────────


def test_read_returns_none_for_unknown():
    d = _make_device()
    assert d.read("nonexistent_field") is None


async def test_read_full_returns_metadata():
    d = _make_device()
    d._process_frame(C0_STATUS_HEX)
    await asyncio.sleep(0)
    r = d.read_full("power")
    assert r is not None
    assert "value" in r
    assert "source" in r
    assert "ts" in r
    assert r["source"] == "rsp_0xc0"


async def test_read_all_available():
    d = _make_device()
    d._process_frame(C0_STATUS_HEX)
    await asyncio.sleep(0)
    result = d.read_all_available()
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


# ── C1 group query frame build ──────────────────────────────


@pytest.mark.parametrize(
    "query_key,expected_page",
    [
        ("cmd_0xc1_group0", 0x40),
        ("cmd_0xc1_group1", 0x41),
        ("cmd_0xc1_group2", 0x42),
        ("cmd_0xc1_group3", 0x43),
        ("cmd_0xc1_group5", 0x45),
        ("cmd_0xc1_group6", 0x46),
        ("cmd_0xc1_group7", 0x47),
        ("cmd_0xc1_group11", 0x4B),
        ("cmd_0xc1_group12", 0x4C),
    ],
)
def test_build_c1_group_query_page_and_appliance(query_key, expected_page):
    """Regression: _build_query_frame must pass the group page as `page`,
    not positionally (which previously bound it to `appliance`)."""
    d = _make_device()
    frame = d._build_query_frame(query_key)
    assert frame is not None, f"no frame built for {query_key}"
    parsed = parse_frame(frame)
    assert parsed["appliance"] == 0xAC, (
        f"{query_key}: appliance=0x{parsed['appliance']:02X}, expected 0xAC "
        f"(page leaked into appliance arg)"
    )
    assert parsed["body"][3] == expected_page, (
        f"{query_key}: body[3]=0x{parsed['body'][3]:02X}, expected 0x{expected_page:02X}"
    )


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


# ── Frame observer hook ────────────────────────────────────


def test_frame_observer_receives_protocol_key_and_body():
    """Observer is called synchronously with the decoded body bytes."""
    d = _make_device()
    seen: list[tuple[str, bytes]] = []
    d.register_frame_observer(lambda pk, body: seen.append((pk, body)))
    d._process_frame(C0_STATUS_HEX)
    assert len(seen) == 1
    protocol_key, body = seen[0]
    assert protocol_key == "rsp_0xc0"
    assert body[0] == 0xC0


def test_frame_observer_register_is_idempotent():
    d = _make_device()
    cb = lambda pk, body: None  # noqa: E731
    d.register_frame_observer(cb)
    d.register_frame_observer(cb)  # second registration is a no-op
    assert d._frame_observers.count(cb) == 1


def test_frame_observer_unregister_works_and_is_safe_for_unknown():
    d = _make_device()
    calls = []
    cb = lambda pk, body: calls.append(pk)  # noqa: E731
    d.register_frame_observer(cb)
    d._process_frame(C0_STATUS_HEX)
    assert len(calls) == 1
    d.unregister_frame_observer(cb)
    d._process_frame(C0_STATUS_HEX)
    assert len(calls) == 1  # unchanged
    # Unregistering an unknown observer is a no-op, not an error.
    d.unregister_frame_observer(cb)


def test_frame_observer_exception_does_not_break_ingest():
    """One observer crashing must not suppress other observers or the
    normal ingest path."""
    d = _make_device()
    other = []

    def bad(pk, body):
        raise RuntimeError("boom")

    def good(pk, body):
        other.append(pk)

    d.register_frame_observer(bad)
    d.register_frame_observer(good)
    # Should not raise:
    d._process_frame(C0_STATUS_HEX)
    assert other == ["rsp_0xc0"]


def test_frame_observer_receives_bytes_copy():
    """Observer receives an immutable bytes copy — can't accidentally
    mutate the ingress body."""
    d = _make_device()
    captures = []

    def capture(pk, body):
        captures.append(body)

    d.register_frame_observer(capture)
    d._process_frame(C0_STATUS_HEX)
    assert captures and isinstance(captures[0], bytes)
    with pytest.raises(TypeError):
        captures[0][0] = 0  # type: ignore[index]


# ── Initial-status handshake (boot/reconnect gap closer) ────


async def test_ingest_and_signal_signals_on_c0_only():
    """The initial-status event fires only on rsp_0xc0 ingest, not on any
    other protocol. Other frames (B1, C1, etc.) coming in during the wait
    must NOT prematurely unblock _query_initial_status."""
    from unittest.mock import AsyncMock

    d = _make_device()
    d._db.ingest = AsyncMock()  # isolate the signal-gate from real ingest
    d._initial_status_event = asyncio.Event()

    await d._ingest_and_signal(b"", "rsp_0xb1", "ts")
    assert not d._initial_status_event.is_set()

    await d._ingest_and_signal(b"", "rsp_0xc0", "ts")
    assert d._initial_status_event.is_set()


async def test_ingest_and_signal_no_crash_when_not_armed():
    """If no _query_initial_status is in flight, the event is None and the
    helper must still ingest cleanly without dereferencing it."""
    from unittest.mock import AsyncMock

    d = _make_device()
    d._db.ingest = AsyncMock()
    assert d._initial_status_event is None  # default

    # Should not raise:
    await d._ingest_and_signal(b"", "rsp_0xc0", "ts")
    d._db.ingest.assert_awaited_once()


async def test_query_initial_status_returns_true_on_c0_ingest():
    """Happy path: send query, get C0 ingest, event is set, helper returns
    True and clears the event to leave state ready for the next reconnect."""
    d = _make_device()
    ws = MockWebSocket()
    await _inject_ws(d, ws)

    task = asyncio.create_task(d._query_initial_status())
    # Yield so the coroutine sends its frame and arms the event.
    await asyncio.sleep(0)

    assert len(ws.sent) >= 1, "expected one cmd_0x41 to be sent"
    assert d._initial_status_event is not None

    # Simulate a C0 ingest completing — _ingest_and_signal would set this.
    d._initial_status_event.set()

    result = await task
    assert result is True
    assert d._initial_status_event is None


async def test_query_initial_status_times_out_when_silent(monkeypatch):
    """Timeout fallback: if no C0 arrives within INITIAL_STATUS_TIMEOUT the
    helper returns False and clears the event so on_connected can still fire."""
    import blaueis.client.device as dev_mod

    monkeypatch.setattr(dev_mod, "INITIAL_STATUS_TIMEOUT", 0.05)

    d = _make_device()
    ws = MockWebSocket()
    await _inject_ws(d, ws)

    result = await d._query_initial_status()
    assert result is False
    assert d._initial_status_event is None


async def test_post_connect_init_fires_on_connected_after_signal():
    """on_connected fires AFTER the initial status round-trip — never before.
    This is the contract that lets HA's connected flag track real entity
    values rather than the bare WS state."""
    d = _make_device()
    ws = MockWebSocket()
    await _inject_ws(d, ws)

    order: list[str] = []
    d.on_connected = lambda: order.append("on_connected")

    task = asyncio.create_task(d._post_connect_init())
    await asyncio.sleep(0)  # let it send the query and arm the event

    # on_connected must NOT have fired yet — we're mid-handshake.
    assert order == []
    assert d._initial_status_event is not None

    # Now signal the C0 ingest.
    order.append("c0_ingested")
    d._initial_status_event.set()
    await task

    assert order == ["c0_ingested", "on_connected"]


async def test_post_connect_init_fires_on_connected_on_timeout(monkeypatch):
    """Even if the AC is silent (no C0 arrives), on_connected must still
    fire after the timeout — HA needs to learn about the connection so
    cards stop showing 'Unavailable' from the disconnect side."""
    import blaueis.client.device as dev_mod

    monkeypatch.setattr(dev_mod, "INITIAL_STATUS_TIMEOUT", 0.05)

    d = _make_device()
    ws = MockWebSocket()
    await _inject_ws(d, ws)

    fired = []
    d.on_connected = lambda: fired.append(True)

    await d._post_connect_init()
    assert fired == [True]
