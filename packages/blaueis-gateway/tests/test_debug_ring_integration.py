"""Integration tests for the DebugRing wiring inside GatewayServer.

Covers:
  - `_on_uart_frame` emits a ring record regardless of client count.
  - `_on_uart_frame` schedules a broadcast only when clients are connected.
  - The `debug_dump` WS message returns the ring contents to the requester.
  - Slot pool exhaustion rejects new clients without touching existing ones.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from blaueis.core.debug_ring import DebugRing
from blaueis.gateway.server import GatewayServer
from blaueis.gateway.uart_protocol import VERBOSE


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_root_logger():
    """Snapshot + restore root logger state so tests don't leak handlers."""
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def ring() -> DebugRing:
    r = DebugRing(size_bytes=64 * 1024)
    r.setLevel(VERBOSE)
    root = logging.getLogger()
    # Ensure gateway's "hvac_gateway" logger records reach the ring via root.
    root.setLevel(VERBOSE)
    root.addHandler(r)
    return r


@pytest.fixture
def server(ring: DebugRing) -> GatewayServer:
    # Minimal config — no real UART, no real socket.
    config = {
        "frame_spacing_ms": 0,
        "stats_interval": 0,
        "fake_ip": "10.0.0.1",
        "uart_baud": 9600,
        "slot_pool_size": 2,
    }
    return GatewayServer(config, no_encrypt=True, debug_ring=ring)


class FakeClient:
    """Minimal stand-in for ClientConnection — captures sends."""

    def __init__(self, sid: int | None = 0):
        self.sid = sid
        self.no_encrypt = True
        self.session = None
        self.sent: list[dict] = []
        # Match ClientConnection's subscribe-filter attributes. Default to
        # "see everything" so tests don't silently lose frames via the filter.
        self.include_kinds: set[str] = {"rx", "tx", "ignored"}
        self.annotate_fields: set[str] = set()

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)

    def decrypt(self, raw: str) -> dict:
        return json.loads(raw)

    class _WS:
        remote_address = ("fake", 0)

    ws = _WS()


# ── _on_uart_frame ────────────────────────────────────────────────────────

def test_on_uart_frame_emits_ring_record_without_clients(server, ring):
    # RX frame from AC — realistic Midea UART layout.
    raw = bytes.fromhex("AA 20 AC 00 00 00 00 00 02 03 C0 01 86 66 7F 7F"
                        "00 00 80 00 00 64 44 0A 70 00 00 00 00 00 00 00 00 01")

    server._on_uart_frame(raw, ts=1.234, direction="rx")

    records = ring.dump_records()
    uart_events = [r for r in records if r.get("event") == "uart_rx"]
    assert uart_events, "expected at least one uart_rx record"
    assert len(uart_events) == 1
    rec = uart_events[0]
    assert rec["port"] == "uart"
    assert rec["peer"] == "ac"
    assert rec["len"] == len(raw)
    assert rec["hex"].startswith("aa 20 ac")
    assert rec["msg_id"] == 0xC0  # byte[10]
    assert rec["lvl"] == "VERBOSE"


@pytest.mark.asyncio
async def test_on_uart_frame_broadcasts_only_when_clients_connected(server, ring):
    """No clients → no broadcast task scheduled, but ring still captures."""
    fc = FakeClient(sid=0)

    # No clients yet — broadcast path skipped; ring path still runs.
    raw1 = bytes.fromhex("AA 20 AC 00 00 00 00 00 00 03 41 81") + bytes(20)
    server._on_uart_frame(raw1, ts=0.0, direction="tx")
    assert fc.sent == []

    # With a client connected, the next frame is broadcast.
    server._clients.add(fc)
    raw2 = bytes.fromhex("AA 20 AC 00 00 00 00 00 02 03 C1 21") + bytes(20)
    server._on_uart_frame(raw2, ts=0.1, direction="rx")
    # Yield to let the fire-and-forget broadcast task run to completion.
    await asyncio.sleep(0)

    frames = [m for m in fc.sent if m.get("type") == "frame"]
    assert len(frames) == 1
    assert frames[0]["dir"] == "rx"
    assert frames[0]["hex"].startswith("aa 20 ac")

    # Both TX and RX captured in the ring regardless of broadcast.
    events = [r.get("event") for r in ring.dump_records() if "event" in r]
    assert "uart_tx" in events
    assert "uart_rx" in events


# ── debug_dump handler ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_debug_dump_returns_ring_contents(server, ring):
    # Prime the ring with an event so the dump is non-empty.
    raw = bytes.fromhex("AA 20 AC 00 00 00 00 00 02 03 C0") + bytes(21)
    server._on_uart_frame(raw, ts=0.0, direction="rx")

    fc = FakeClient(sid=0)

    raw_msg = json.dumps({"type": "debug_dump", "ref": 42})
    await server._handle_client_message(fc, raw_msg)

    [reply] = fc.sent
    assert reply["type"] == "debug_dump"
    assert reply["ref"] == 42
    assert reply["record_count"] >= 1
    assert reply["ring_capacity_bytes"] == ring.size_bytes
    records = [json.loads(line) for line in reply["jsonl"].strip().split("\n")]
    events = [r.get("event") for r in records if "event" in r]
    assert "uart_rx" in events


@pytest.mark.asyncio
async def test_debug_dump_error_when_ring_disabled(ring):
    config = {
        "frame_spacing_ms": 0, "stats_interval": 0,
        "fake_ip": "10.0.0.1", "uart_baud": 9600, "slot_pool_size": 2,
    }
    srv = GatewayServer(config, no_encrypt=True, debug_ring=None)
    fc = FakeClient()

    raw_msg = json.dumps({"type": "debug_dump", "ref": 7})
    await srv._handle_client_message(fc, raw_msg)

    [reply] = fc.sent
    assert reply["type"] == "error"
    assert reply["ref"] == 7
    assert "disabled" in reply["msg"].lower()


# ── Slot pool integration ─────────────────────────────────────────────────

def test_slot_pool_size_honours_config(ring):
    config = {
        "frame_spacing_ms": 0, "stats_interval": 0,
        "fake_ip": "10.0.0.1", "uart_baud": 9600, "slot_pool_size": 3,
    }
    srv = GatewayServer(config, no_encrypt=True, debug_ring=ring)
    assert srv.slot_pool.size == 3
    # Acquire all, then expect exhaustion.
    srv.slot_pool.acquire()
    srv.slot_pool.acquire()
    srv.slot_pool.acquire()
    from blaueis.gateway.slot_pool import SlotPoolExhausted
    with pytest.raises(SlotPoolExhausted):
        srv.slot_pool.acquire()
