"""Tests for Device write-lock serialisation.

Covers:
- Concurrent Device.set() calls are strictly serialised.
- Device.toggle_display() emits the cmd_0x41 body[1]=0x61 frame and takes the lock.
- Exception in one set() call releases the lock for the next caller.
- Operations outside the Device API can bundle multi-frame sequences by
  taking Device.write_lock explicitly.

No real network — uses MockWebSocket.

Usage:  python -m pytest packages/blaueis-client/tests/test_write_lock.py -v
"""

import asyncio

import pytest
from blaueis.client.device import Device
from blaueis.client.ws_client import HvacClient
from blaueis.core.frame import parse_frame

from tests.conftest import MockWebSocket


def _make_device() -> Device:
    return Device(
        host="127.0.0.1",
        port=8765,
        no_encrypt=True,
        poll_interval=999,
    )


async def _inject_ws(device: Device, ws: MockWebSocket):
    client = HvacClient(device.host, device.port, no_encrypt=True)
    client._ws = ws
    device._client = client
    client.add_listener(device._on_gateway_message)


# ── Presence + type checks ──────────────────────────────────


def test_write_lock_exists_and_is_asyncio_lock():
    d = _make_device()
    assert isinstance(d.write_lock, asyncio.Lock)
    # Same instance every access (property returns the stored lock)
    assert d.write_lock is d.write_lock


def test_write_lock_initially_released():
    d = _make_device()
    assert not d.write_lock.locked()


# ── toggle_display() emits the right frame ──────────────────


@pytest.mark.asyncio
async def test_toggle_display_emits_cmd_0x41_relative_toggle():
    d = _make_device()
    ws = MockWebSocket()
    await _inject_ws(d, ws)

    await d.toggle_display()

    assert len(ws.sent) == 1
    sent_json = __import__("json").loads(ws.sent[0])
    assert sent_json.get("type") == "frame"
    frame_bytes = bytes.fromhex(sent_json["hex"].replace(" ", ""))
    parsed = parse_frame(frame_bytes)
    assert parsed["msg_type"] == 0x03
    body = parsed["body"]
    assert body[0] == 0x41
    assert body[1] == 0x61
    assert body[3] == 0xFF
    assert body[4] == 0x02
    assert body[5] == 0x00
    assert body[6] == 0x02
    assert body[7] == 0x00


@pytest.mark.asyncio
async def test_toggle_display_raises_when_not_connected():
    d = _make_device()
    with pytest.raises(RuntimeError):
        await d.toggle_display()


@pytest.mark.asyncio
async def test_toggle_display_takes_write_lock():
    d = _make_device()
    ws = MockWebSocket()
    await _inject_ws(d, ws)

    # Pre-acquire the lock from another task; toggle_display should block.
    async with d.write_lock:
        task = asyncio.create_task(d.toggle_display())
        await asyncio.sleep(0.05)
        assert not task.done(), "toggle_display should block on write_lock"
        assert len(ws.sent) == 0, "no frame should have been sent yet"
    await asyncio.wait_for(task, timeout=1.0)
    assert len(ws.sent) == 1


# ── Lock serialises concurrent writes ───────────────────────


@pytest.mark.asyncio
async def test_concurrent_toggle_display_serialised():
    """Two concurrent toggle_display() calls should emit two frames in order,
    not interleave."""
    d = _make_device()
    ws = MockWebSocket()
    await _inject_ws(d, ws)

    # Instrument: record the order lock is acquired/released via a small delay.
    send_order: list[str] = []
    original_send = ws.send

    async def slow_send(data):
        send_order.append("enter")
        await asyncio.sleep(0.02)
        await original_send(data)
        send_order.append("exit")

    ws.send = slow_send

    task1 = asyncio.create_task(d.toggle_display())
    task2 = asyncio.create_task(d.toggle_display())
    await asyncio.gather(task1, task2)

    # Strict interleaving: enter-exit-enter-exit, never enter-enter-exit-exit.
    assert send_order == ["enter", "exit", "enter", "exit"], (
        f"writes interleaved: {send_order}"
    )
    assert len(ws.sent) == 2


@pytest.mark.asyncio
async def test_lock_released_after_exception_in_send():
    """If the underlying send raises, the lock must still be released so the
    next caller can proceed."""
    d = _make_device()
    ws = MockWebSocket()
    await _inject_ws(d, ws)

    call_count = 0

    async def flaky_send(data):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("simulated send failure")
        ws.sent.append(data)

    ws.send = flaky_send

    # First call raises; second call must succeed.
    with pytest.raises(ConnectionError):
        await d.toggle_display()
    assert not d.write_lock.locked()

    await d.toggle_display()
    assert len(ws.sent) == 1


# ── External multi-frame bundling via the exposed lock ──────


@pytest.mark.asyncio
async def test_external_caller_can_bundle_sequence_under_write_lock():
    """A caller (e.g. the ingress-hook enforcer) should be able to take the
    lock to bundle a toggle → wait → set() sequence. While the external
    caller holds the lock, internal Device calls must queue."""
    d = _make_device()
    ws = MockWebSocket()
    await _inject_ws(d, ws)

    other_call_started = asyncio.Event()
    other_call_finished = asyncio.Event()

    async def other_caller():
        other_call_started.set()
        await d.toggle_display()
        other_call_finished.set()

    async with d.write_lock:
        task = asyncio.create_task(other_caller())
        await other_call_started.wait()
        await asyncio.sleep(0.05)
        assert not other_call_finished.is_set(), (
            "other caller should be blocked while external holder owns the lock"
        )
        # External caller emits a frame themselves — nothing else can interleave.
        await ws.send('{"type":"frame","hex":"aa","ts":0,"dir":"tx"}')
    await asyncio.wait_for(task, timeout=1.0)
    # Two sends total: our raw send + the toggle.
    assert len(ws.sent) == 2
    assert "aa" in ws.sent[0]
