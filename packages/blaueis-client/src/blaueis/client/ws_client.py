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
import time
from dataclasses import dataclass, field
from typing import Any

from blaueis.core.crypto import (
    complete_handshake_client,
    create_hello,
)
from blaueis.core.debug_ring import log_event

log = logging.getLogger("hvac_client")


# Ring/log levels (match the gateway's VERBOSE=5)
_VERBOSE = 5


@dataclass
class GatewaySession:
    """Per-connection state assigned by the gateway at connect time.

    `sid` is the gateway-assigned slot id (see flight_recorder.md §4.6). It is
    unknown until the `hello` message arrives from the gateway; before that,
    outgoing commands omit `sid`. The ring timestamps disambiguate sessions
    when a slot is reused after reconnect.
    """

    sid: int | None = None
    pool_size: int | None = None
    connected_at: float = 0.0
    connected_wall: float = 0.0
    server_time_at_connect: float = 0.0
    next_req_id: int = 0

    def next_ref(self) -> int:
        self.next_req_id += 1
        return self.next_req_id


class HvacClient:
    """Async WebSocket client for the HVAC gateway."""

    def __init__(self, host: str, port: int, psk: bytes | None = None, no_encrypt: bool = False):
        self.host = host
        self.port = port
        self.psk = psk
        self.no_encrypt = no_encrypt
        self._ws = None
        self._session = None
        self._listeners: list = []
        self.on_frame = None  # callback(hex_str, timestamp)
        self.on_pi_status = None  # callback(stats_dict)

        # Gateway slot session — populated by the `hello` message.
        self.gw_session = GatewaySession()

        # Futures awaiting one-shot replies, keyed by ref.
        self._pending_replies: dict[int, asyncio.Future] = {}

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

        self.gw_session.connected_at = time.monotonic()
        self.gw_session.connected_wall = time.time()

    async def close(self):
        """Close the WebSocket connection."""
        # Cancel any outstanding reply futures so awaiters wake up.
        for fut in self._pending_replies.values():
            if not fut.done():
                fut.cancel()
        self._pending_replies.clear()
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
        # Ring event — omit from regular log flow (VERBOSE, propagate=False).
        log_event(
            log, _VERBOSE, "ws_out",
            port="ws", peer=f"ws:{self.gw_session.sid}" if self.gw_session.sid is not None else "ws:?",
            sid=self.gw_session.sid,
            req_id=msg.get("ref"),
            ctx={"type": msg.get("type")},
        )

    async def _recv(self) -> dict:
        """Receive and decrypt a message from the gateway."""
        raw = await self._ws.recv()
        if self._session and not self.no_encrypt:
            msg = self._session.decrypt_json(raw)
        else:
            msg = json.loads(raw)
        log_event(
            log, _VERBOSE, "ws_in",
            port="ws", peer=f"ws:{self.gw_session.sid}" if self.gw_session.sid is not None else "ws:?",
            sid=self.gw_session.sid,
            req_id=msg.get("ref"),
            ctx={"type": msg.get("type")},
        )
        return msg

    def _next_ref(self) -> int:
        return self.gw_session.next_ref()

    async def send_frame(self, frame_hex: str) -> int:
        """Send a raw Midea frame (hex string). Returns reference ID."""
        ref = self._next_ref()
        payload = {"type": "frame", "hex": frame_hex, "ref": ref}
        if self.gw_session.sid is not None:
            # Echo our assigned slot so the gateway can sanity-check it
            # (§4.7 tech debt — gateway trusts its socket→slot map today
            # and only records the field; verification comes later).
            payload["sid"] = self.gw_session.sid
        await self._send(payload)
        return ref

    async def send_ping(self):
        """Send keepalive ping."""
        await self._send({"type": "ping"})

    # ── Subscribe filter (flight_recorder.md §4.1) ─────────────────

    async def send_subscribe(
        self,
        include: list[str] | None = None,
        annotate: list[str] | None = None,
        *,
        timeout: float = 5.0,
    ) -> dict:
        """Set the per-connection subscribe filter and await confirmation.

        `include` defaults to ["rx"]; `annotate` defaults to [].
        Raises asyncio.TimeoutError if the gateway does not reply.
        """
        ref = self._next_ref()
        msg = {
            "type": "subscribe", "ref": ref,
            "include": list(include) if include is not None else ["rx"],
            "annotate": list(annotate) if annotate is not None else [],
        }
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_replies[ref] = fut
        try:
            await self._send(msg)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_replies.pop(ref, None)

    # ── Debug dump (flight_recorder.md §4.4) ──────────────────────

    async def request_debug_dump(self, *, timeout: float = 10.0) -> dict:
        """Pull the gateway's flight-recorder ring over the wire.

        Returns the `debug_dump` reply as a dict with keys
        `jsonl`, `record_count`, `size_bytes`, `ring_capacity_bytes`.
        Raises asyncio.TimeoutError if the gateway does not reply.
        """
        ref = self._next_ref()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_replies[ref] = fut
        try:
            await self._send({"type": "debug_dump", "ref": ref})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_replies.pop(ref, None)

    # ── Listen loop ────────────────────────────────────────────────

    async def listen(self):
        """Listen for messages from the gateway. Dispatches to callbacks."""
        try:
            while self._ws:
                msg = await self._recv()
                msg_type = msg.get("type")

                if msg_type == "hello":
                    self._handle_hello(msg)
                elif msg_type == "frame" and self.on_frame:
                    self.on_frame(msg.get("hex", ""), msg.get("ts", 0))
                elif msg_type == "pi_status" and self.on_pi_status:
                    self.on_pi_status(msg)
                elif msg_type == "ack":
                    log.debug("ACK ref=%s status=%s", msg.get("ref"), msg.get("status"))
                elif msg_type == "error":
                    log.warning("Error ref=%s: %s", msg.get("ref"), msg.get("msg"))
                elif msg_type == "pong":
                    log.debug("Pong received")
                elif msg_type in ("subscribed", "debug_dump"):
                    ref = msg.get("ref")
                    fut = self._pending_replies.get(ref) if ref is not None else None
                    if fut is not None and not fut.done():
                        fut.set_result(msg)

                # Route reply-carrying errors back to awaiting futures too, so
                # `request_debug_dump` fails fast instead of timing out.
                if msg_type == "error" and msg.get("ref") in self._pending_replies:
                    fut = self._pending_replies[msg["ref"]]
                    if not fut.done():
                        fut.set_exception(
                            RuntimeError(f"gateway error: {msg.get('msg', '')}")
                        )

                for listener in self._listeners:
                    listener(msg)

        except Exception as e:
            log.info("Listen ended: %s", e)

    def _handle_hello(self, msg: dict) -> None:
        self.gw_session.sid = msg.get("sid")
        self.gw_session.pool_size = msg.get("pool_size")
        self.gw_session.server_time_at_connect = float(msg.get("server_time", 0.0))
        log_event(
            log, _VERBOSE, "ws_connect",
            port="ws", peer=f"ws:{self.gw_session.sid}",
            sid=self.gw_session.sid,
            ctx={
                "pool_size": self.gw_session.pool_size,
                "server_time": self.gw_session.server_time_at_connect,
                "wall": self.gw_session.connected_wall,
            },
        )
        log.info(
            "Gateway assigned sid=%s (pool size %s)",
            self.gw_session.sid, self.gw_session.pool_size,
        )

    def add_listener(self, callback):
        """Add a raw message listener (receives all messages)."""
        self._listeners.append(callback)
