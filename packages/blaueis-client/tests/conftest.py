"""Shared fixtures for blaueis-client tests.

Captured frames from a live Atelier Midea unit (2026-04-13).
"""

import asyncio
import json

import pytest

# ── Captured frames (hex strings) ──────────────────────────

# B5 extended: 8 capability records
B5_EXTENDED_HEX = (
    "aa 35 ac 00 00 00 00 00 02 03 b5 08 12 02 01 01"
    " 14 02 01 01 15 02 01 01 16 02 01 00 1a 02 01 01"
    " 10 02 01 01 25 02 07 20 3c 20 3c 20 3c 00 24 02"
    " 01 01 01 00 20 40"
)

# B5 simple: 9 capability records
B5_SIMPLE_HEX = (
    "aa 33 ac 00 00 00 00 00 02 03 b5 09 1e 02 01 01"
    " 13 02 01 01 22 02 01 00 19 02 01 00 39 00 01 01"
    " 42 00 01 01 09 00 01 01 0a 00 01 01 48 00 01 01"
    " 00 00 f4 10"
)

# C0 status: AC off, heat mode, 22°C target, indoor 21.1°C
C0_STATUS_HEX = (
    "aa 28 ac 00 00 00 00 00 02 03 c0 00 86 66 7f 7f"
    " 00 00 00 00 00 5c ff 0a 00 01 00 00 00 00 00 00"
    " 00 01 00 00 00 00 00 e0 36"
)


# ── Mock WebSocket ──────────────────────────────────────────


class MockWebSocket:
    """Fake websockets connection for testing."""

    def __init__(self, recv_messages: list[str] | None = None):
        self._recv_queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False
        self.remote_address = ("127.0.0.1", 12345)
        if recv_messages:
            for msg in recv_messages:
                self._recv_queue.put_nowait(msg)

    async def send(self, data: str):
        self.sent.append(data)

    async def recv(self) -> str:
        try:
            return self._recv_queue.get_nowait()
        except asyncio.QueueEmpty:
            # Block until feed() adds something, or raise on close
            return await self._recv_queue.get()

    async def close(self):
        self.closed = True
        # Unblock any waiting recv()
        self._recv_queue.put_nowait(None)

    def feed(self, msg: str):
        """Add a message to be received."""
        self._recv_queue.put_nowait(msg)

    def feed_json(self, obj: dict):
        """Add a JSON message to be received."""
        self._recv_queue.put_nowait(json.dumps(obj))

    def feed_frame(self, hex_str: str, direction: str = "rx"):
        """Add a frame message to be received."""
        self.feed_json({
            "type": "frame",
            "hex": hex_str,
            "ts": 1000.0,
            "dir": direction,
        })
