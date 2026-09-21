"""Undecodable client messages close the connection with 1008, no traceback.

Seen live: a client sent a plaintext message into an established
encrypted session; ``Session.decrypt`` raised ``KeyError: 'c'``, which
escaped ``_handle_client_message`` and websockets closed with 1011
(internal error) after logging a traceback.
"""

from __future__ import annotations

import json
import logging

import pytest
import websockets
from blaueis.client.ws_client import HvacClient
from blaueis.core.crypto import (
    complete_handshake_client,
    complete_handshake_server,
    create_hello,
    create_hello_ok,
    generate_psk,
    psk_to_bytes,
)
from blaueis.gateway import server as server_mod
from blaueis.gateway.server import ClientConnection, GatewayServer

PEER = ("192.0.2.7", 51234)


def _config(**extra) -> dict:
    return {
        "frame_spacing_ms": 0,
        "stats_interval": 0,
        "fake_ip": "10.0.0.1",
        "uart_baud": 9600,
        "slot_pool_size": 4,
        **extra,
    }


class FakeWS:
    remote_address = PEER

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed_with: tuple[int, str] | None = None

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


def _session_pair(psk: bytes):
    """(client_session, server_session) sharing one handshake."""
    hello, client_rand = create_hello()
    hello_ok, server_rand = create_hello_ok()
    server = complete_handshake_server(psk, hello, server_rand)
    client = complete_handshake_client(psk, client_rand, hello_ok)
    return client, server


def _bad_base64(client_session) -> str:
    env = json.loads(client_session.encrypt_json({"type": "ping"}))
    env["ct"] = "not base64!"
    return json.dumps(env)


BAD_MESSAGES = {
    # the live case: plaintext message inside an encrypted session
    "plaintext_in_session": lambda cs, other: json.dumps({"type": "frame", "hex": "AA", "ref": 1}),
    "not_json": lambda cs, other: "garbage{",
    "json_list": lambda cs, other: "[1, 2]",
    "counter_not_int": lambda cs, other: json.dumps({"c": "x", "ct": "", "tag": ""}),
    "bad_base64": lambda cs, other: _bad_base64(cs),
    "wrong_key": lambda cs, other: other.encrypt_json({"type": "ping"}),
}


@pytest.fixture
def server() -> GatewayServer:
    return GatewayServer(_config(), no_encrypt=False)


@pytest.mark.parametrize("kind", sorted(BAD_MESSAGES))
async def test_undecodable_message_closes_1008_with_one_warning(server, caplog, kind) -> None:
    psk = generate_psk()
    client_session, server_session = _session_pair(psk)
    other_session, _ = _session_pair(generate_psk())
    ws = FakeWS()
    client = ClientConnection(ws, server_session, no_encrypt=False, sid=0)

    with caplog.at_level(logging.DEBUG):
        await server._handle_client_message(client, BAD_MESSAGES[kind](client_session, other_session))

    assert client.rejected is True
    assert ws.closed_with is not None and ws.closed_with[0] == 1008
    assert ws.sent == []  # nothing processed, no reply
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert str(PEER) in warnings[0].getMessage()
    assert warnings[0].exc_info is None  # no traceback


async def test_replayed_message_closes_1008(server) -> None:
    client_session, server_session = _session_pair(generate_psk())
    ws = FakeWS()
    client = ClientConnection(ws, server_session, no_encrypt=False, sid=0)
    envelope = client_session.encrypt_json({"type": "ping"})

    await server._handle_client_message(client, envelope)
    assert client.rejected is False
    await server._handle_client_message(client, envelope)

    assert client.rejected is True
    assert ws.closed_with[0] == 1008


async def test_wrong_key_reason_is_auth_failure(server) -> None:
    _, server_session = _session_pair(generate_psk())
    other_session, _ = _session_pair(generate_psk())
    ws = FakeWS()
    client = ClientConnection(ws, server_session, no_encrypt=False, sid=0)

    await server._handle_client_message(client, other_session.encrypt_json({"type": "ping"}))

    assert ws.closed_with == (1008, "auth failure")


async def test_valid_message_still_handled(server) -> None:
    client_session, server_session = _session_pair(generate_psk())
    ws = FakeWS()
    client = ClientConnection(ws, server_session, no_encrypt=False, sid=0)

    await server._handle_client_message(client, client_session.encrypt_json({"type": "ping"}))

    assert client.rejected is False
    assert ws.closed_with is None
    assert len(ws.sent) == 1


async def test_plaintext_into_live_session_closes_1008_end_to_end(caplog) -> None:
    """Real websockets server and client over loopback: the plaintext
    message gets a 1008 close, and no handler traceback is logged."""
    passphrase = "test passphrase"
    server = GatewayServer(_config(psk=passphrase), no_encrypt=False)

    with caplog.at_level(logging.WARNING):
        async with websockets.serve(server._ws_handler, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            client = HvacClient("127.0.0.1", port, psk=psk_to_bytes(passphrase))
            await client.connect()
            ws = client._ws

            await ws.send(json.dumps({"type": "frame", "hex": "AA", "ref": 1}))
            with pytest.raises(websockets.exceptions.ConnectionClosed) as ei:
                await ws.recv()
            await ws.wait_closed()

    assert ei.value.rcvd is not None
    assert ei.value.rcvd.code == 1008
    assert not server._clients
    assert server.slot_pool.in_use_count == 0
    gw_warnings = [r for r in caplog.records if r.name == server_mod.log.name and r.levelno >= logging.WARNING]
    assert len(gw_warnings) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
