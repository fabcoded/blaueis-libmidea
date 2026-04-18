"""Tests for HvacClient — WebSocket client with mock transport.

No real network — MockWebSocket feeds canned responses.

Usage (standalone):  python -m pytest packages/blaueis-client/tests/test_ws_client.py -v
"""

import json

import pytest
from blaueis.client.ws_client import HvacClient
from blaueis.core.crypto import (
    complete_handshake_server,
    create_hello_ok,
    generate_psk,
)

# ── Mock WebSocket ──────────────────────────────────────────────────────


class MockWebSocket:
    """Fake websockets connection — records sends, feeds canned receives."""

    def __init__(self, recv_messages: list[str] | None = None):
        self._recv_queue: list[str] = list(recv_messages or [])
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str):
        self.sent.append(data)

    async def recv(self) -> str:
        if not self._recv_queue:
            raise Exception("connection closed")
        return self._recv_queue.pop(0)

    async def close(self):
        self.closed = True

    def feed(self, msg: str):
        self._recv_queue.append(msg)


# ── Tests ───────────────────────────────────────────────────────────────


def test_next_ref_monotonic():
    c = HvacClient("localhost", 8765)
    assert c._next_ref() == 1
    assert c._next_ref() == 2
    assert c._next_ref() == 3


def test_init_defaults():
    c = HvacClient("10.0.0.1", 9999, psk=b"\x00" * 16)
    assert c.host == "10.0.0.1"
    assert c.port == 9999
    assert c.psk == b"\x00" * 16
    assert c.no_encrypt is False
    assert c._ws is None
    assert c._session is None
    assert c.gw_session.next_req_id == 0
    assert c.gw_session.sid is None


@pytest.mark.asyncio
async def test_close_clears_state():
    c = HvacClient("localhost", 8765, no_encrypt=True)
    c._ws = MockWebSocket()
    c._session = "fake_session"
    await c.close()
    assert c._ws is None
    assert c._session is None


@pytest.mark.asyncio
async def test_send_plaintext():
    c = HvacClient("localhost", 8765, no_encrypt=True)
    ws = MockWebSocket()
    c._ws = ws
    msg = {"type": "ping"}
    await c._send(msg)
    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0]) == msg


@pytest.mark.asyncio
async def test_recv_plaintext():
    c = HvacClient("localhost", 8765, no_encrypt=True)
    expected = {"type": "pong"}
    ws = MockWebSocket([json.dumps(expected)])
    c._ws = ws
    result = await c._recv()
    assert result == expected


@pytest.mark.asyncio
async def test_send_frame_shape_and_ref():
    c = HvacClient("localhost", 8765, no_encrypt=True)
    ws = MockWebSocket()
    c._ws = ws
    ref1 = await c.send_frame("AA BB CC")
    ref2 = await c.send_frame("DD EE FF")
    assert ref1 == 1
    assert ref2 == 2
    msg1 = json.loads(ws.sent[0])
    assert msg1 == {"type": "frame", "hex": "AA BB CC", "ref": 1}
    msg2 = json.loads(ws.sent[1])
    assert msg2 == {"type": "frame", "hex": "DD EE FF", "ref": 2}


@pytest.mark.asyncio
async def test_send_ping():
    c = HvacClient("localhost", 8765, no_encrypt=True)
    ws = MockWebSocket()
    c._ws = ws
    await c.send_ping()
    assert json.loads(ws.sent[0]) == {"type": "ping"}


@pytest.mark.asyncio
async def test_listen_frame_dispatch():
    frame_msg = {"type": "frame", "hex": "AA 0B", "ts": 1.5}
    c = HvacClient("localhost", 8765, no_encrypt=True)
    ws = MockWebSocket([json.dumps(frame_msg)])
    c._ws = ws

    received = []
    c.on_frame = lambda hex_str, ts: received.append((hex_str, ts))
    await c.listen()

    assert len(received) == 1
    assert received[0] == ("AA 0B", 1.5)


@pytest.mark.asyncio
async def test_listen_pi_status_dispatch():
    stats_msg = {"type": "pi_status", "cpu_percent": 42}
    c = HvacClient("localhost", 8765, no_encrypt=True)
    ws = MockWebSocket([json.dumps(stats_msg)])
    c._ws = ws

    received = []
    c.on_pi_status = lambda msg: received.append(msg)
    await c.listen()

    assert len(received) == 1
    assert received[0]["cpu_percent"] == 42


@pytest.mark.asyncio
async def test_listen_ack_no_crash():
    c = HvacClient("localhost", 8765, no_encrypt=True)
    ws = MockWebSocket([json.dumps({"type": "ack", "ref": 1, "status": "queued"})])
    c._ws = ws
    # No callback set — should not crash
    await c.listen()


@pytest.mark.asyncio
async def test_listen_error_no_crash():
    c = HvacClient("localhost", 8765, no_encrypt=True)
    ws = MockWebSocket([json.dumps({"type": "error", "ref": 1, "msg": "bad frame"})])
    c._ws = ws
    await c.listen()


@pytest.mark.asyncio
async def test_listen_pong_no_crash():
    c = HvacClient("localhost", 8765, no_encrypt=True)
    ws = MockWebSocket([json.dumps({"type": "pong"})])
    c._ws = ws
    await c.listen()


@pytest.mark.asyncio
async def test_add_listener_fires_for_all():
    msgs = [
        {"type": "frame", "hex": "AA", "ts": 0},
        {"type": "pong"},
        {"type": "ack", "ref": 1, "status": "ok"},
    ]
    c = HvacClient("localhost", 8765, no_encrypt=True)
    ws = MockWebSocket([json.dumps(m) for m in msgs])
    c._ws = ws

    raw_received = []
    c.add_listener(lambda m: raw_received.append(m))
    await c.listen()

    assert len(raw_received) == 3
    assert [m["type"] for m in raw_received] == ["frame", "pong", "ack"]


@pytest.mark.asyncio
async def test_listen_connection_close_exits():
    c = HvacClient("localhost", 8765, no_encrypt=True)
    ws = MockWebSocket([])  # empty — recv will raise immediately
    c._ws = ws
    # Should exit cleanly, not raise
    await c.listen()


@pytest.mark.asyncio
async def test_encrypted_send_recv():
    """Verify encrypted path uses session.encrypt_json / decrypt_json."""
    psk = generate_psk()
    c = HvacClient("localhost", 8765, psk=psk)

    # Simulate completed handshake by setting up real crypto sessions
    from blaueis.core.crypto import complete_handshake_client, create_hello

    hello_msg, client_rand = create_hello()
    hello_ok_msg, server_rand = create_hello_ok()
    server_session = complete_handshake_server(psk, hello_msg, server_rand)
    client_session = complete_handshake_client(psk, client_rand, hello_ok_msg)

    c._session = client_session
    c.no_encrypt = False

    # Send encrypted
    ws = MockWebSocket()
    c._ws = ws
    await c._send({"type": "ping"})
    assert len(ws.sent) == 1
    # The sent data should be a ciphertext string, not plain JSON
    assert ws.sent[0] != json.dumps({"type": "ping"})
    # Server should be able to decrypt it
    decrypted = server_session.decrypt_json(ws.sent[0])
    assert decrypted == {"type": "ping"}

    # Receive encrypted — server sends, client decrypts
    server_encrypted = server_session.encrypt_json({"type": "pong"})
    ws.feed(server_encrypted)
    result = await c._recv()
    assert result == {"type": "pong"}
