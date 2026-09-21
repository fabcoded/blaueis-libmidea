"""Reconnect ordering: a post-connect handshake that outlives its link.

After a reconnect, ``_post_connect_init`` runs as a separate task and
waits up to INITIAL_STATUS_TIMEOUT for the first C0. If the link drops
again inside that window, the stale handshake must not fire
``on_connected`` after the drop's ``on_disconnected`` — otherwise the
consumer reports "connected" while the gateway is down.

No real network: ``HvacClient.connect`` is replaced by a fake transport
that either attaches a MockWebSocket or raises (gateway down).
"""

from __future__ import annotations

import asyncio

import blaueis.client.device as dev_mod
import pytest
from blaueis.client.device import Device
from blaueis.client.ws_client import HvacClient

from tests.conftest import MockWebSocket


class FakeGateway:
    """Stands in for the gateway's TCP endpoint: up or down."""

    def __init__(self) -> None:
        self.up = True
        self.sockets: list[MockWebSocket] = []

    async def connect(self, client: HvacClient) -> None:
        if not self.up:
            raise OSError("connection refused")
        ws = MockWebSocket()
        self.sockets.append(ws)
        client._ws = ws


@pytest.fixture
def gateway(monkeypatch) -> FakeGateway:
    gw = FakeGateway()

    async def fake_connect(self: HvacClient) -> None:
        await gw.connect(self)

    monkeypatch.setattr(HvacClient, "connect", fake_connect)
    monkeypatch.setattr(dev_mod, "INITIAL_STATUS_TIMEOUT", 0.2)
    monkeypatch.setattr(dev_mod, "RECONNECT_DELAYS", [0])
    return gw


def _device(events: list[str]) -> Device:
    d = Device(host="127.0.0.1", port=8765, no_encrypt=True, poll_interval=999)
    d.on_connected = lambda: events.append("connected")
    d.on_disconnected = lambda: events.append("disconnected")
    d._running = True
    return d


async def _stop(d: Device, *tasks: asyncio.Task) -> None:
    d._running = False
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_link_drop_during_post_connect_never_reports_connected(gateway):
    """Live sequence: Reconnected → drop → gateway stays down. The first
    reconnect's handshake times out later and must stay silent."""
    events: list[str] = []
    d = _device(events)

    await d._reconnect()  # gateway up: reconnects, spawns the handshake
    stale = d._post_connect_task
    assert stale is not None and not stale.done()
    await asyncio.sleep(0)  # handshake sent its C0 query and is waiting

    gateway.up = False  # link drops again; gateway stays down
    retry = asyncio.create_task(d._reconnect())
    await asyncio.sleep(0.4)  # well past INITIAL_STATUS_TIMEOUT

    assert stale.cancelled()
    assert events == ["disconnected", "disconnected"]
    await _stop(d, retry)


async def test_inline_post_connect_skips_on_connected_after_drop(gateway):
    """start() awaits the handshake inline (not as ``_post_connect_task``),
    so cancellation cannot reach it — the link generation must."""
    events: list[str] = []
    d = _device(events)
    await d._connect()

    handshake = asyncio.create_task(d._post_connect_init())
    await asyncio.sleep(0)
    gateway.up = False
    retry = asyncio.create_task(d._reconnect())
    await handshake  # times out after INITIAL_STATUS_TIMEOUT

    assert events == ["disconnected"]
    await _stop(d, retry)


async def test_reconnect_after_drop_reports_connected_once(gateway):
    """The handshake of the link that actually came back still fires."""
    events: list[str] = []
    d = _device(events)

    await d._reconnect()
    await asyncio.sleep(0)
    await d._reconnect()  # dropped and immediately back
    await d._post_connect_task  # current link's handshake times out → fires

    assert events == ["disconnected", "disconnected", "connected"]
    await _stop(d)


async def test_client_is_published_only_after_its_handshake(gateway, monkeypatch):
    """While the new connection's handshake is in flight, pollers and
    writers must still see the old client (and skip), not the new one."""
    events: list[str] = []
    d = _device(events)
    await d._connect()
    old = d.client
    await old.close()  # link lost

    gate = asyncio.Event()
    connecting: list[HvacClient] = []

    async def slow_connect(self: HvacClient) -> None:
        connecting.append(self)
        await gate.wait()
        await gateway.connect(self)

    monkeypatch.setattr(HvacClient, "connect", slow_connect)
    task = asyncio.create_task(d._connect())
    while not connecting:
        await asyncio.sleep(0)

    assert d.client is old
    assert d.connected is False
    await d._send_poll_queries()  # skipped: no live connection published
    assert gateway.sockets[0].sent == []

    gate.set()
    await task
    assert d.client is connecting[0]
    await _stop(d)
