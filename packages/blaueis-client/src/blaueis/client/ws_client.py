"""HVAC Gateway WebSocket client library.

Connects to the Pi gateway, handles session handshake (optional encryption),
sends commands, receives frames.

Usage as library:
    from blaueis.core.frame import build_status_query
    client = HvacClient("192.168.1.50", 8765, psk=bytes.fromhex("..."))
    await client.connect()
    await client.send_frame(build_status_query().hex())
    await client.close()
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

# Add gateway/ for shared modules

from blaueis.core.crypto import (
    complete_handshake_client,
    create_hello,
)

log = logging.getLogger("hvac_client")


class HvacClient:
    """Async WebSocket client for the HVAC gateway."""

    def __init__(self, host: str, port: int, psk: bytes | None = None, no_encrypt: bool = False):
        self.host = host
        self.port = port
        self.psk = psk
        self.no_encrypt = no_encrypt
        self._ws = None
        self._session = None
        self._ref_counter = 0
        self._listeners: list = []
        self.on_frame = None  # callback(hex_str, timestamp)
        self.on_pi_status = None  # callback(stats_dict)

    async def connect(self):
        """Connect to the gateway and perform session handshake."""
        import websockets

        uri = f"ws://{self.host}:{self.port}"
        log.info("Connecting to %s", uri)
        self._ws = await websockets.connect(uri)

        if not self.no_encrypt and self.psk:
            hello_msg, client_rand = create_hello()
            await self._ws.send(json.dumps(hello_msg))
            reply_raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            reply = json.loads(reply_raw)
            self._session = complete_handshake_client(self.psk, client_rand, reply)
            log.info("Encrypted session established")
        else:
            log.info("Connected without encryption")

    async def close(self):
        """Close the WebSocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None
            self._session = None

    async def _send(self, msg: dict):
        """Send a message to the gateway."""
        if self._session and not self.no_encrypt:
            await self._ws.send(self._session.encrypt_json(msg))
        else:
            await self._ws.send(json.dumps(msg))

    async def _recv(self) -> dict:
        """Receive and decrypt a message from the gateway."""
        raw = await self._ws.recv()
        if self._session and not self.no_encrypt:
            return self._session.decrypt_json(raw)
        return json.loads(raw)

    def _next_ref(self) -> int:
        self._ref_counter += 1
        return self._ref_counter

    async def send_frame(self, frame_hex: str) -> int:
        """Send a raw Midea frame (hex string). Returns reference ID."""
        ref = self._next_ref()
        await self._send({"type": "frame", "hex": frame_hex, "ref": ref})
        return ref

    async def send_ping(self):
        """Send keepalive ping."""
        await self._send({"type": "ping"})

    async def listen(self):
        """Listen for messages from the gateway. Dispatches to callbacks."""
        try:
            while self._ws:
                msg = await self._recv()
                msg_type = msg.get("type")

                if msg_type == "frame" and self.on_frame:
                    self.on_frame(msg.get("hex", ""), msg.get("ts", 0))
                elif msg_type == "pi_status" and self.on_pi_status:
                    self.on_pi_status(msg)
                elif msg_type == "ack":
                    log.debug("ACK ref=%s status=%s", msg.get("ref"), msg.get("status"))
                elif msg_type == "error":
                    log.warning("Error ref=%s: %s", msg.get("ref"), msg.get("msg"))
                elif msg_type == "pong":
                    log.debug("Pong received")

                for listener in self._listeners:
                    listener(msg)

        except Exception as e:
            log.info("Listen ended: %s", e)

    def add_listener(self, callback):
        """Add a raw message listener (receives all messages)."""
        self._listeners.append(callback)
