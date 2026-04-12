"""Midea UART dongle protocol state machine.

Implements the dongle-side protocol per midea_uart_protocol_reference.md:
  DISCOVER → MODEL → ANNOUNCE → RUNNING

The AC is stateless — re-handshaking is safe at any time.

Key principle: ALL frames received from the AC are forwarded to the client
(for debugging/monitoring), THEN handled locally (respond to queries,
trigger re-handshake, etc.).
"""

import asyncio
import logging
import time

from blaueis.core.frame import (
    FrameError,
    build_frame,
    build_model_query,
    build_network_init,
    build_network_status_response,
    build_sn_query,
    build_version_response,
    parse_frame,
)

# Custom VERBOSE level (5) — below DEBUG (10), for raw UART hex dumps
VERBOSE = 5
logging.addLevelName(VERBOSE, "VERBOSE")

log = logging.getLogger("uart_protocol")

# State constants
DISCOVER = "discover"
MODEL = "model"
ANNOUNCE = "announce"
RUNNING = "running"

# Timeouts
DISCOVER_TIMEOUT = 1.0  # 1s wait for SN response (async serial needs more headroom)
MODEL_TIMEOUT = 1.0  # 1s wait for model response
RETRY_DELAY = 0.3  # 300ms between discover retries
SILENCE_TIMEOUT = 120.0  # 120s silence → re-discover
# No proactive polling — client sends queries when needed
NET_STATUS_INTERVAL = 120.0  # 2min between network status reports

# MSG types the AC sends that require a local response
AC_QUERY_RESPOND = {
    0x13,  # firmware version query
    0x63,  # network status request
    0x87,  # version info request
}

# MSG types that are version info requests (same response)
VERSION_INFO_MSGS = {0x13, 0x87}

# MSG types that trigger re-handshake
REHANDSHAKE_MSGS = {0x82, 0x83}

# MSG types to silently ignore (no response, no forward)
IGNORE_MSGS = {0x61}  # time sync — dongle doesn't respond


class UartProtocol:
    """Dongle protocol state machine for Midea UART bus."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.state = DISCOVER
        self.appliance = 0xFF
        self.proto = 0x00
        self.sub = 0x00
        self.model = 0
        self.serial_number = ""
        self.silence_timer = 0.0
        self.msg_counter = 0
        self._tx_queue: asyncio.Queue = asyncio.Queue(maxsize=self.config.get("max_queue", 8))
        self._on_frame = None  # callback for frames to forward to client
        self._running = False
        # TX mirroring: forward frames WE send on UART to the client too
        # mirror_tx_gateway: mirror frames initiated by the gateway (handshake, query responses)
        # mirror_tx_all: also mirror client-originated frames relayed to UART
        self.mirror_tx_gateway = self.config.get("mirror_tx_gateway", False)
        self.mirror_tx_all = self.config.get("mirror_tx_all", False)

    @property
    def fake_ip(self) -> tuple[int, int, int, int]:
        ip_str = self.config.get("fake_ip", "192.168.1.100")
        parts = ip_str.split(".")
        return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))

    @property
    def signal_level(self) -> int:
        return self.config.get("signal_level", 4)

    def set_on_frame(self, callback):
        """Set callback for ALL frames received from AC (forwarded to client)."""
        self._on_frame = callback

    def _next_seq(self) -> int:
        self.msg_counter = (self.msg_counter + 1) & 0xFF
        return self.msg_counter

    def _forward_to_client(self, raw: bytes, direction: str = "rx"):
        """Forward a raw frame to the client callback (if connected).

        direction: "rx" = from AC, "tx" = we sent to AC (mirrored)
        """
        if self._on_frame:
            self._on_frame(raw, time.monotonic(), direction)

    # ── UART I/O helpers ──────────────────────────────────────────────

    async def _send(self, writer, frame: bytes, *, mirror: bool = True):
        """Write a frame to UART with inter-frame spacing.

        If mirror=True and mirror_tx_gateway is enabled, the frame is
        also forwarded to the client with direction="tx".
        """
        spacing = self.config.get("frame_spacing_ms", 100) / 1000.0
        log.log(VERBOSE, "UART TX (%dB): %s", len(frame), frame.hex(" "))
        writer.write(frame)
        await writer.drain()
        if mirror and self.mirror_tx_gateway:
            self._forward_to_client(frame, direction="tx")
        await asyncio.sleep(spacing)

    async def _read_one_frame(self, reader) -> bytes | None:
        """Read one frame from UART, scanning for 0xAA start.

        Note: serial_asyncio.read() may return partial data (fewer bytes
        than requested). We use readexactly() for the frame body to ensure
        we get all bytes, and read(1) only for scanning the start byte.
        """
        while True:
            byte = await reader.read(1)
            if not byte:
                return None
            if byte[0] != 0xAA:
                continue
            length_byte = await reader.read(1)
            if not length_byte:
                return None
            frame_len = length_byte[0]
            remaining = frame_len - 1
            if remaining < 0 or remaining > 250:
                continue
            # readexactly waits until all bytes arrive (unlike read which returns partial)
            try:
                rest = await reader.readexactly(remaining)
            except (asyncio.IncompleteReadError, ConnectionError):
                return None
            frame = bytes([0xAA, frame_len]) + rest
            log.log(VERBOSE, "UART RX (%dB): %s", len(frame), frame.hex(" "))
            return frame

    async def _send_and_wait(self, writer, reader, frame: bytes, timeout: float) -> tuple[dict | None, bytes | None]:
        """Send a frame and wait for response.

        Returns (parsed_frame, raw_bytes) or (None, None).
        Any non-matching frames received while waiting are forwarded to client.
        """
        await self._send(writer, frame)
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                break
            try:
                raw = await asyncio.wait_for(self._read_one_frame(reader), timeout=remaining_time)
            except TimeoutError:
                break

            if raw is None:
                break

            try:
                parsed = parse_frame(raw)
            except FrameError as e:
                log.warning("Frame error while waiting for response: %s", e)
                continue

            # Always forward to client — even during handshake
            self._forward_to_client(raw)

            # Check if this is the response we're waiting for
            # During handshake: response msg_type matches our query msg_type
            sent_parsed = parse_frame(frame)
            if parsed["msg_type"] == sent_parsed["msg_type"]:
                return parsed, raw

            # Not our response — handle AC queries that arrived during wait
            await self._handle_ac_query(parsed, writer)

        return None, None

    async def _handle_ac_query(self, parsed: dict, writer):
        """Handle an AC query that needs a local response (can arrive any time)."""
        msg = parsed["msg_type"]

        if msg == 0x63:
            ip_str = ".".join(str(x) for x in self.fake_ip)
            log.debug("AC requests network status (0x63) → responding with IP %s", ip_str)
            ns_body = build_network_status_response(
                ip=self.fake_ip,
                signal=self.signal_level,
                connected=True,
            )
            resp_frame = build_frame(
                ns_body,
                msg_type=0x63,
                appliance=self.appliance,
                proto=self.proto,
                sub=self.sub,
                seq=self._next_seq(),
            )
            await self._send(writer, resp_frame)

        elif msg in VERSION_INFO_MSGS:
            log.debug("AC requests version info (0x%02X) → responding", msg)
            resp_frame = build_version_response(self.appliance, self.proto, self.sub)
            await self._send(writer, resp_frame)

        elif msg == 0x68:
            # WiFi config query — respond with minimal config
            log.debug("AC requests WiFi config (0x68)")
            resp_frame = build_frame(
                bytes([0x00] * 20),
                msg_type=0x68,
                appliance=self.appliance,
                proto=self.proto,
                sub=self.sub,
            )
            await self._send(writer, resp_frame)

    # ── State machine ─────────────────────────────────────────────────

    async def run(self, reader, writer):
        """Main protocol loop. Runs until cancelled."""
        self._running = True
        log.info("Protocol starting, state=%s", self.state)

        try:
            while self._running:
                if self.state == DISCOVER:
                    await self._do_discover(reader, writer)
                elif self.state == MODEL:
                    await self._do_model(reader, writer)
                elif self.state == ANNOUNCE:
                    await self._do_announce(writer)
                elif self.state == RUNNING:
                    await self._do_running(reader, writer)
        except asyncio.CancelledError:
            log.info("Protocol cancelled")
        finally:
            self._running = False

    async def _do_discover(self, reader, writer):
        """DISCOVER: send SN query, learn appliance type."""
        log.info("DISCOVER: sending SN query (0x07)")
        sn_frame = build_sn_query()
        resp, raw = await self._send_and_wait(writer, reader, sn_frame, DISCOVER_TIMEOUT)

        if resp is None:
            # Try RAC SN query (0x65)
            log.info("DISCOVER: no response to 0x07, trying 0x65")
            rac_frame = build_frame(bytes(20), msg_type=0x65, appliance=0xFF)
            resp, raw = await self._send_and_wait(writer, reader, rac_frame, DISCOVER_TIMEOUT)

        if resp is None:
            log.warning("DISCOVER: no response, retrying in %.1fs", RETRY_DELAY)
            await asyncio.sleep(RETRY_DELAY)
            return  # stay in DISCOVER

        # Learn device identity from response
        self.appliance = resp["appliance"]
        self.proto = resp["proto"]
        self.sub = resp["sub"]
        body = resp["body"]
        self.serial_number = body.decode("ascii", errors="replace").rstrip("\x00") if body else ""

        log.info(
            "DISCOVER: found appliance=0x%02X proto=%d sub=%d sn=%s",
            self.appliance,
            self.proto,
            self.sub,
            self.serial_number[:32],
        )
        self.state = MODEL

    async def _do_model(self, reader, writer):
        """MODEL: query model number."""
        log.info("MODEL: sending model query (0xA0)")
        frame = build_model_query(self.appliance, self.proto, self.sub)
        resp, raw = await self._send_and_wait(writer, reader, frame, MODEL_TIMEOUT)

        if resp and len(resp["body"]) >= 4:
            self.model = resp["body"][2] | (resp["body"][3] << 8)
            log.info("MODEL: model=%d (0x%04X)", self.model, self.model)
        else:
            log.warning("MODEL: no/bad response, continuing anyway")

        self.state = ANNOUNCE

    async def _do_announce(self, writer):
        """ANNOUNCE: send network init, transition to RUNNING."""
        log.info("ANNOUNCE: sending network init (0x0D) with IP %s", ".".join(str(x) for x in self.fake_ip))
        frame = build_network_init(self.appliance, self.fake_ip, self.proto, self.sub)
        await self._send(writer, frame)
        self.silence_timer = time.monotonic()
        self.state = RUNNING
        log.info("ANNOUNCE → RUNNING")

    async def _do_running(self, reader, writer):
        """RUNNING: read frames, answer AC queries, relay ALL to client, poll periodically."""
        # No proactive polling — the AC queries us every ~60s with 0x63
        # (which keeps the silence timer alive). The client application
        # sends queries/commands via WebSocket when it needs data.

        # Check for pending TX from client
        try:
            tx_frame = self._tx_queue.get_nowait()
            # Client-originated frames: only mirror if mirror_tx_all is set
            writer.write(tx_frame)
            await writer.drain()
            if self.mirror_tx_all:
                self._forward_to_client(tx_frame, direction="tx")
            spacing = self.config.get("frame_spacing_ms", 100) / 1000.0
            await asyncio.sleep(spacing)
            log.debug("TX queued frame (%d bytes)", len(tx_frame))
        except asyncio.QueueEmpty:
            pass

        # Read one frame with short timeout
        try:
            raw = await asyncio.wait_for(self._read_one_frame(reader), timeout=1.0)
        except TimeoutError:
            # Check silence timeout
            if time.monotonic() - self.silence_timer > SILENCE_TIMEOUT:
                log.warning("RUNNING: silence >%ds, re-discovering", int(SILENCE_TIMEOUT))
                self.state = DISCOVER
                self.appliance = 0xFF
            return

        if raw is None:
            return

        self.silence_timer = time.monotonic()

        try:
            parsed = parse_frame(raw)
        except FrameError as e:
            log.warning("Invalid frame: %s", e)
            return

        msg = parsed["msg_type"]

        # ALWAYS forward to client first — client sees everything
        if msg not in IGNORE_MSGS:
            self._forward_to_client(raw)

        # Then handle locally
        if msg in REHANDSHAKE_MSGS:
            log.info("AC requests re-handshake (0x%02X), restarting", msg)
            ack = build_frame(bytes([0x00]), msg_type=msg, appliance=self.appliance)
            await self._send(writer, ack)
            self.state = DISCOVER
            self.appliance = 0xFF

        elif msg in AC_QUERY_RESPOND or msg == 0x68:
            await self._handle_ac_query(parsed, writer)

        elif msg in (0x0F, 0x11):
            # Transport data — check for restart trigger
            body = parsed["body"]
            if len(body) >= 2 and body[0] == 0x80 and body[1] == 0x40:
                log.warning("AC sent restart trigger (0x%02X body=80 40), re-discovering", msg)
                self.state = DISCOVER
                self.appliance = 0xFF

        # else: data frames (0x03/C0/C1/B5, 0x04, 0x05, 0x06, 0x0A)
        # already forwarded to client above — no local action needed

    # ── Client-facing commands ────────────────────────────────────────

    async def queue_frame(self, frame: bytes) -> bool:
        """Queue a raw frame for UART transmission. Returns False if queue full."""
        try:
            self._tx_queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            log.warning("TX queue full (%d), dropping frame", self._tx_queue.maxsize)
            return False

    def stop(self):
        """Signal the protocol to stop."""
        self._running = False
