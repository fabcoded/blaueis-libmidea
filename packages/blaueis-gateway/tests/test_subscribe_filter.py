"""Tests for the per-subscriber subscribe filter (include + annotate)."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from blaueis.core.debug_ring import DebugRing
from blaueis.gateway.server import ClientConnection, GatewayServer
from blaueis.gateway.uart_protocol import VERBOSE


@pytest.fixture(autouse=True)
def _isolate_root_logger():
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def server() -> GatewayServer:
    ring = DebugRing(size_bytes=16 * 1024)
    ring.setLevel(VERBOSE)
    root = logging.getLogger()
    root.setLevel(VERBOSE)
    root.addHandler(ring)
    config = {
        "frame_spacing_ms": 0, "stats_interval": 0, "fake_ip": "10.0.0.1",
        "uart_baud": 9600, "slot_pool_size": 4,
    }
    return GatewayServer(config, no_encrypt=True, debug_ring=ring)


class FakeWS:
    remote_address = ("fake", 0)


class FakeClient(ClientConnection):
    """Real ClientConnection, but its `send` just appends to a list."""

    def __init__(self, sid: int):
        super().__init__(FakeWS(), session=None, no_encrypt=True, sid=sid)
        self.sent: list[dict] = []

    async def send(self, msg: dict) -> None:  # type: ignore[override]
        self.sent.append(msg)


def _raw() -> bytes:
    # Valid-ish Midea frame with msg_id 0xC0 at byte[10].
    return bytes.fromhex("AA 20 AC 00 00 00 00 00 02 03 C0") + bytes(21)


# ── Default subscription ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_subscription_is_rx_only(server) -> None:
    client = FakeClient(sid=0)
    server._clients.add(client)

    server._on_uart_frame(_raw(), ts=0.0, direction="rx")
    server._on_uart_frame(_raw(), ts=0.1, direction="tx")
    await asyncio.sleep(0)

    dirs = [m["dir"] for m in client.sent if m.get("type") == "frame"]
    assert dirs == ["rx"]  # default include = ["rx"]


@pytest.mark.asyncio
async def test_default_has_no_annotation_fields(server) -> None:
    client = FakeClient(sid=0)
    server._clients.add(client)

    server._on_uart_frame(_raw(), ts=0.0, direction="rx", meta={
        "origin": "gw:handshake", "msg_id": 0xC0, "req_id": 7,
    })
    await asyncio.sleep(0)

    [frame_msg] = [m for m in client.sent if m.get("type") == "frame"]
    assert "origin" not in frame_msg
    assert "req_id" not in frame_msg
    assert "msg_id" not in frame_msg


# ── subscribe handler ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_sets_include_and_annotate(server) -> None:
    client = FakeClient(sid=0)
    raw_msg = json.dumps({
        "type": "subscribe", "ref": 1,
        "include": ["rx", "tx"],
        "annotate": ["origin", "reply_to"],
    })
    await server._handle_client_message(client, raw_msg)

    assert client.include_kinds == {"rx", "tx"}
    assert client.annotate_fields == {"origin", "reply_to"}
    [reply] = client.sent
    assert reply["type"] == "subscribed"
    assert reply["ref"] == 1
    assert reply["include"] == ["rx", "tx"]
    assert reply["annotate"] == ["origin", "reply_to"]


@pytest.mark.asyncio
async def test_subscribe_rejects_unknown_values(server) -> None:
    client = FakeClient(sid=0)
    # 'origin' is a valid annotate field but 'only_mine' is not; include
    # rejects 'provenance' — never a kind.
    raw_msg = json.dumps({
        "type": "subscribe", "ref": 2,
        "include": ["provenance"],
        "annotate": ["only_mine"],
    })
    await server._handle_client_message(client, raw_msg)

    [reply] = client.sent
    assert reply["type"] == "error"
    assert reply["ref"] == 2
    # State is unchanged on error — defaults preserved.
    assert client.include_kinds == {"rx"}
    assert client.annotate_fields == set()


@pytest.mark.asyncio
async def test_subscribe_rejects_non_list_arguments(server) -> None:
    client = FakeClient(sid=0)
    raw_msg = json.dumps({
        "type": "subscribe", "ref": 3,
        "include": "rx",  # must be a list
        "annotate": [],
    })
    await server._handle_client_message(client, raw_msg)

    [reply] = client.sent
    assert reply["type"] == "error"


# ── Subscriber behaviour after subscribe ──────────────────────────────────

@pytest.mark.asyncio
async def test_subscribed_tx_receives_tx_frames(server) -> None:
    client = FakeClient(sid=0)
    client.include_kinds = {"rx", "tx"}
    server._clients.add(client)

    server._on_uart_frame(_raw(), ts=0.0, direction="tx", meta={"origin": "ws:0"})
    await asyncio.sleep(0)

    dirs = [m["dir"] for m in client.sent]
    assert "tx" in dirs


@pytest.mark.asyncio
async def test_annotate_adds_only_requested_fields(server) -> None:
    client = FakeClient(sid=0)
    client.include_kinds = {"rx"}
    client.annotate_fields = {"origin", "reply_to"}
    server._clients.add(client)

    server._on_uart_frame(_raw(), ts=0.0, direction="rx", meta={
        "origin": "gw:handshake",
        "req_id": 42,
        "msg_id": 0xC0,
        "reply_to": {"req_id": 42, "origin": "ws:1", "confidence": "confirmed"},
    })
    await asyncio.sleep(0)

    [frame_msg] = [m for m in client.sent if m.get("type") == "frame"]
    # Requested fields present:
    assert frame_msg["origin"] == "gw:handshake"
    assert frame_msg["reply_to"]["confidence"] == "confirmed"
    # Not-requested fields absent:
    assert "req_id" not in frame_msg
    assert "msg_id" not in frame_msg


@pytest.mark.asyncio
async def test_multi_client_filters_are_independent(server) -> None:
    a = FakeClient(sid=0)
    a.include_kinds = {"rx"}
    b = FakeClient(sid=1)
    b.include_kinds = {"rx", "tx"}
    b.annotate_fields = {"origin"}
    server._clients.add(a)
    server._clients.add(b)

    server._on_uart_frame(_raw(), ts=0.0, direction="tx", meta={"origin": "ws:1"})
    server._on_uart_frame(_raw(), ts=0.1, direction="rx", meta={"origin": "gw:handshake"})
    await asyncio.sleep(0)

    # Client A saw only the RX; no annotations.
    a_frames = [m for m in a.sent if m.get("type") == "frame"]
    assert [f["dir"] for f in a_frames] == ["rx"]
    assert "origin" not in a_frames[0]

    # Client B saw both; origin annotated on both.
    b_frames = [m for m in b.sent if m.get("type") == "frame"]
    assert [f["dir"] for f in b_frames] == ["tx", "rx"]
    assert b_frames[0]["origin"] == "ws:1"
    assert b_frames[1]["origin"] == "gw:handshake"
