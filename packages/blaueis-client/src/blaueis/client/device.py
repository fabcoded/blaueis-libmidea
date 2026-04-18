"""High-level Device abstraction over the Blaueis gateway.

Wraps HvacClient + blaueis-core status/codec into an autonomous device
that manages its own connection, capability discovery, polling, and
state change notification.

Architecture:
    Status database (self._status) persists across connection drops.
    B5 capabilities are loaded once and never cleared.
    A supervisor task ensures listen + poll loops are always running.
    If a loop dies, the supervisor restarts it without touching state.

Usage:
    device = Device("192.168.1.50", 8765, psk="myPassphrase")
    device.on_state_change = lambda field, value, old: print(f"{field}: {old} → {value}")
    await device.start()
    print(device.available_fields)
    await device.set(power=True, target_temperature=24)
    await device.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from blaueis.client.status_db import StatusDB
from blaueis.client.ws_client import HvacClient
from blaueis.core.codec import (
    identify_frame,
    walk_fields,
)
from blaueis.core.crypto import psk_to_bytes
from blaueis.core.frame import (
    build_cap_query_extended,
    build_cap_query_simple,
    build_status_query,
    parse_frame,
)
from blaueis.core.process import (
    finalize_capabilities,
    process_b5,
)
from blaueis.core.query import read_field

log = logging.getLogger("blaueis.device")

# Fields that fold into the HA climate entity — never exposed as standalone.
CLIMATE_FIELDS = frozenset({"operating_mode", "target_temperature", "fan_speed"})

DEFAULT_POLL_INTERVAL = 15.0  # seconds
SUPERVISOR_CHECK_INTERVAL = 5.0  # seconds
RECONNECT_DELAYS = [5, 10, 30, 60]  # initial backoff sequence
RECONNECT_FOREVER_INTERVAL = 60  # after exhausting backoff


def _parse_b1_property_id(raw) -> tuple[int, int] | None:
    """Normalise a glossary ``property_id`` into a ``(lo, hi)`` tuple.

    The glossary writes property ids as ``"0x42,0x00"`` (string) in the
    protocol decode blocks. Accepts strings, tuples, lists, or ints (in
    which case ``hi`` defaults to 0).
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 2:
            return None
        try:
            return (int(parts[0], 0) & 0xFF, int(parts[1], 0) & 0xFF)
        except ValueError:
            return None
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return (int(raw[0]) & 0xFF, int(raw[1]) & 0xFF)
        except (ValueError, TypeError):
            return None
    if isinstance(raw, int):
        return (raw & 0xFF, 0)
    return None


class Device:
    """Autonomous HVAC device connected via Blaueis gateway.

    The status database (self._status) is the single source of truth.
    It persists across connection drops and loop restarts.
    B5 capabilities are loaded once during start() and never cleared.

    Lifecycle: __init__ → start() → [running, supervised] → stop()
    """

    def __init__(
        self,
        host: str,
        port: int = 8765,
        psk: str | bytes | None = None,
        no_encrypt: bool = False,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ):
        self.host = host
        self.port = port
        self._psk_raw = psk
        self._no_encrypt = no_encrypt
        self.poll_interval = poll_interval

        # ── Status database (persists across reconnects) ───
        self._db = StatusDB()
        self._glossary = self._db.glossary
        self._status = self._db.status

        # ── Gateway info (populated from version/pi_status) ─
        self.gateway_info: dict = {
            "version": "unknown",
            "device_name": "Midea AC",
            "instance": "",
        }
        self.gateway_stats: dict = {}

        # ── Connection state ───────────────────────────────
        self._client: HvacClient | None = None
        self._psk_bytes: bytes | None = None  # computed once in start()

        # ── Callbacks ──────────────────────────────────────
        # on_state_change is delegated to StatusDB — see property below
        self.on_connected: Callable[[], None] | None = None
        self.on_disconnected: Callable[[], None] | None = None
        self.on_gateway_stats: Callable[[dict], None] | None = None

        # ── B5 state machine ───────────────────────────────
        self._b5_state: str = "idle"  # idle | waiting | done
        self._b5_next_frame: bool = False  # set by frame handler
        self._b5_page: int = 0  # current page being queried
        self._b5_response_event: asyncio.Event | None = None

        # ── Follow Me shadow register ─────────────────────
        self._follow_me_shadow: dict | None = None  # {"celsius": float}

        # ── Task management ────────────────────────────────
        self._running = False
        self._supervisor_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None

    # ── Properties ──────────────────────────────────────────

    @property
    def on_state_change(self):
        return self._db.on_state_change

    @on_state_change.setter
    def on_state_change(self, callback):
        self._db.on_state_change = callback

    @property
    def status(self) -> dict:
        """Current glossary-driven status dict (persists across reconnects)."""
        return self._status

    @property
    def glossary(self) -> dict:
        return self._glossary

    @property
    def connected(self) -> bool:
        """True if WebSocket is alive and listen loop is running."""
        if not self._client or not self._client._ws:
            return False
        return bool(self._listen_task and not self._listen_task.done())

    @property
    def capabilities_received(self) -> bool:
        return self._status["meta"].get("b5_received", False)

    def field_gdef(self, name: str) -> dict | None:
        """Return the full glossary definition for a field (or None)."""
        return walk_fields(self._glossary).get(name)

    def caps_bitmap(self) -> dict:
        """B5-derived capability flags, keyed by name. Stub today — returns
        an empty dict; UX-gating code that reads `hardware_flag` falls
        conservatively (masked) when a flag is absent. Parsing individual
        bits out of the B5 response is a separate, deferred change."""
        return {}

    @property
    def available_fields(self) -> dict[str, dict]:
        """Fields confirmed available by B5 or marked 'always'/'readable'."""
        result = {}
        all_fields = walk_fields(self._glossary)
        for name, fdata in self._status["fields"].items():
            fa = fdata.get("feature_available", "never")
            if fa in ("never", "capability"):
                continue
            gdef = all_fields.get(name, {})
            result[name] = {
                "field_class": gdef.get("field_class", "unknown"),
                "data_type": fdata.get("data_type", "unknown"),
                "writable": fdata.get("writable", False),
                "feature_available": fa,
                "active_constraints": fdata.get("active_constraints"),
            }
        return result

    # ── Follow Me shadow register API ─────────────────────

    def set_follow_me_shadow(self, celsius: float) -> None:
        """Arm Follow Me shadow — all subsequent cmd_0x41 polls carry the temp."""
        celsius = max(0.0, min(50.0, float(celsius)))
        self._follow_me_shadow = {"celsius": celsius}
        log.debug("Follow Me shadow armed: %.1f°C", celsius)

    def clear_follow_me_shadow(self) -> None:
        """Disarm — cmd_0x41 reverts to standard status query."""
        self._follow_me_shadow = None
        log.debug("Follow Me shadow cleared")

    @property
    def follow_me_shadow_active(self) -> bool:
        return self._follow_me_shadow is not None

    @property
    def required_queries(self) -> frozenset[str]:
        """Compute required queries from the database. No caching — always fresh."""
        return frozenset(self._compute_required_queries())

    # ── Query computation (from database) ───────────────────

    # Max number of B1 property ids per query frame. The probe tool uses 8;
    # real OEM dongles bundle ~16–20. Below the body-length ceiling either way.
    _B1_BATCH_SIZE = 8

    def _compute_required_queries(self) -> set[str]:
        """Scan the status database for available fields and deduplicate
        the query frames needed to populate them.

        This is the single source of truth — no external registration needed.
        """
        needed: set[str] = set()
        all_fields = walk_fields(self._glossary)
        b1_prop_ids: list[tuple[int, int]] = []
        b1_seen: set[tuple[int, int]] = set()

        for fname, fdata in self._status["fields"].items():
            fa = fdata.get("feature_available", "never")
            if fa in ("never", "capability"):
                continue  # not confirmed available
            gdef = all_fields.get(fname, {})
            protocols = gdef.get("protocols", {})
            for pkey, pdef in protocols.items():
                if pdef.get("direction") != "response":
                    continue
                if pkey == "rsp_0xb1":
                    for entry in pdef.get("decode", []) or []:
                        pair = _parse_b1_property_id(entry.get("property_id"))
                        if pair is not None and pair not in b1_seen:
                            b1_seen.add(pair)
                            b1_prop_ids.append(pair)
                    continue
                query_key = self._response_to_query(pkey)
                if query_key:
                    needed.add(query_key)

        # Register one query key per B1 batch — _build_query_frame resolves
        # each to the right slice of the sorted prop_id list.
        if b1_prop_ids:
            self._b1_prop_ids = sorted(b1_prop_ids)
            n_batches = (len(self._b1_prop_ids) + self._B1_BATCH_SIZE - 1) // self._B1_BATCH_SIZE
            for i in range(n_batches):
                needed.add(f"cmd_0xb1_batch_{i}")
        else:
            self._b1_prop_ids = []
        return needed

    @staticmethod
    def _response_to_query(response_key: str) -> str | None:
        if response_key == "rsp_0xc0":
            return "cmd_0x41"
        if response_key.startswith("rsp_0xc1_group"):
            return response_key.replace("rsp_", "cmd_")
        if response_key in ("rsp_0xb5", "rsp_0xb5_tlv"):
            return "cmd_0xb5"
        if response_key in ("rsp_0xa1",):
            return "cmd_0x41"
        # rsp_0xb1 is handled by _compute_required_queries directly because
        # the query carries a list of prop_ids extracted from the glossary —
        # no static command key maps to it.
        return None

    # ── Lifecycle ───────────────────────────────────────────

    async def start(self):
        """Connect, discover B5 capabilities (once), start supervisor."""
        if self._running:
            return

        # Compute PSK once
        if self._psk_raw and not self._no_encrypt:
            if isinstance(self._psk_raw, bytes):
                self._psk_bytes = self._psk_raw
            else:
                self._psk_bytes = psk_to_bytes(self._psk_raw)

        # Initial connection
        await self._connect()
        self._running = True

        # Start listen loop immediately (needed for B5 response reception)
        self._listen_task = asyncio.create_task(self._listen_loop())

        # Query gateway info + B5 capabilities (once, results persist in _status)
        await self._query_gateway_info()
        await self._query_capabilities()

        # Start poll loop (queries derived from database each cycle)
        self._poll_task = asyncio.create_task(self._poll_loop())

        # Start supervisor (restarts loops if they die)
        self._supervisor_task = asyncio.create_task(self._supervisor())

        log.info("Device started: %s:%d (%d available fields)",
                 self.host, self.port, len(self.available_fields))

    async def stop(self):
        """Stop supervisor and all loops, disconnect."""
        self._running = False

        for task in [self._supervisor_task, self._poll_task, self._listen_task]:
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self._supervisor_task = None
        self._poll_task = None
        self._listen_task = None

        if self._client:
            await self._client.close()
            self._client = None

        if self.on_disconnected:
            self.on_disconnected()

        log.info("Device stopped")

    # ── Connection management ──────────────────────────────

    async def _connect(self):
        """Establish WebSocket connection and wire up message handler."""
        self._client = HvacClient(
            self.host, self.port, psk=self._psk_bytes, no_encrypt=self._no_encrypt
        )
        await self._client.connect()
        self._client.add_listener(self._on_gateway_message)
        if self.on_connected:
            self.on_connected()

    async def _reconnect(self):
        """Reconnect WebSocket with backoff. Does NOT re-query B5 or wipe status."""
        if self.on_disconnected:
            self.on_disconnected()

        # Backoff sequence, then retry every 60s forever
        delays = list(RECONNECT_DELAYS)
        attempt = 0
        while self._running:
            attempt += 1
            delay = delays.pop(0) if delays else RECONNECT_FOREVER_INTERVAL
            log.info("Reconnecting to %s:%d (attempt %d, wait %ds)...",
                     self.host, self.port, attempt, delay)
            await asyncio.sleep(delay)

            if not self._running:
                return

            try:
                if self._client:
                    with contextlib.suppress(Exception):
                        await self._client.close()
                await self._connect()
                log.info("Reconnected to %s:%d", self.host, self.port)
                return
            except Exception as e:
                log.warning("Reconnect attempt %d failed: %s", attempt, e)

    # ── Supervisor ─────────────────────────────────────────

    async def _supervisor(self):
        """Ensure listen and poll loops are always running.

        Checks every SUPERVISOR_CHECK_INTERVAL seconds. If a loop task
        has exited (done/cancelled/exception), restarts it. The status
        database is never touched — loops just resume writing to it.
        """
        try:
            while self._running:
                # Start or restart listen loop
                if self._listen_task is None or self._listen_task.done():
                    if self._listen_task and self._listen_task.done():
                        # Log why it died
                        exc = self._listen_task.exception() if not self._listen_task.cancelled() else None
                        if exc:
                            log.warning("Supervisor: listen loop died (%s), restarting", exc)
                        else:
                            log.info("Supervisor: listen loop exited, restarting")
                    self._listen_task = asyncio.create_task(self._listen_loop())

                # Start or restart poll loop
                if self._poll_task is None or self._poll_task.done():
                    if self._poll_task and self._poll_task.done():
                        exc = self._poll_task.exception() if not self._poll_task.cancelled() else None
                        if exc:
                            log.warning("Supervisor: poll loop died (%s), restarting", exc)
                        else:
                            log.info("Supervisor: poll loop exited, restarting")
                    self._poll_task = asyncio.create_task(self._poll_loop())

                await asyncio.sleep(SUPERVISOR_CHECK_INTERVAL)

        except asyncio.CancelledError:
            raise

    # ── Listen loop ─────────────────────────────────────────

    async def _listen_loop(self):
        """Listen for gateway messages. On disconnect, reconnect and resume.

        The status database is never cleared — reconnection just
        re-establishes the WebSocket so frames can flow again.
        """
        while self._running:
            try:
                if self._client and self._client._ws:
                    await self._client.listen()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Listen ended: %s", e)

            if not self._running:
                break

            # Connection lost — reconnect (no B5, no status wipe)
            await self._reconnect()

    # ── Poll loop ───────────────────────────────────────────

    async def _poll_loop(self):
        """Send queries on interval. Tolerates individual failures."""
        while self._running:
            try:
                await self._send_poll_queries()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("Poll failed (will retry): %s", e)

            # Wait for next cycle
            await asyncio.sleep(self.poll_interval)

    async def _send_poll_queries(self):
        """Send minimum query set derived from the status database."""
        if not self._client or not self._client._ws:
            log.debug("Poll skipped: no connection")
            return  # skip this cycle, try next

        queries = self._compute_required_queries() or {"cmd_0x41"}
        log.debug("Poll: %d queries", len(queries))

        sent = 0
        for qkey in sorted(queries):
            if not self._running:
                break
            if not self._client or not self._client._ws:
                log.warning("Poll aborted: connection lost mid-cycle")
                break
            frame = self._build_query_frame(qkey)
            if frame:
                try:
                    await self._client.send_frame(frame.hex(" "))
                    sent += 1
                except Exception as e:
                    log.warning("Send failed for %s: %s", qkey, e)
                    break  # connection likely dead, stop sending
                await asyncio.sleep(0.1)  # inter-frame spacing
        log.debug("Poll sent %d/%d queries", sent, len(queries))

    # ── Capability discovery (once) ─────────────────────────

    async def _query_capabilities(self):
        """Query B5 capabilities using response-driven state machine.

        Sends B5 page 0 (extended), waits for response, checks next_frame
        flag. If set, sends page 1 (simple), waits, repeats. Finalizes
        when next_frame=0 or timeout. Called once during start().

        Other frames (C0 heartbeats, etc.) arriving during the wait are
        processed normally by the listen loop — only B5 responses drive
        the state machine.
        """
        if not self._client:
            return
        if self.capabilities_received:
            log.info("B5 capabilities already loaded, skipping")
            return

        log.info("Querying B5 capabilities...")
        b5_queries = [build_cap_query_extended, build_cap_query_simple]
        page = 0

        while page < len(b5_queries):
            # Set up state machine for this page
            self._b5_state = "waiting"
            self._b5_next_frame = False
            self._b5_response_event = asyncio.Event()

            # Send query
            try:
                frame = b5_queries[page]()
                await self._client.send_frame(frame.hex(" "))
                log.debug("B5 page %d query sent", page)
            except Exception as e:
                log.warning("B5 page %d send failed: %s", page, e)
                break

            # Wait for response (driven by _process_frame setting the event)
            try:
                await asyncio.wait_for(self._b5_response_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("B5 page %d response timeout", page)
                break

            log.debug("B5 page %d received (next_frame=%s)", page, self._b5_next_frame)

            if not self._b5_next_frame:
                break  # no more pages
            page += 1

        self._b5_state = "done"
        self._b5_response_event = None
        finalize_capabilities(self._status, self._glossary)
        log.info("B5 complete: %d pages, %d available fields",
                 page + 1, len(self.available_fields))

    async def _query_gateway_info(self):
        """Request gateway version/name. Called once during start()."""
        if not self._client:
            return
        try:
            await self._client._send({"type": "version", "ref": 0})
        except Exception as e:
            log.warning("Gateway info query failed: %s", e)
            return

        for _ in range(10):
            await asyncio.sleep(0.2)
            if self.gateway_info.get("version") != "unknown":
                break

        log.info("Gateway: %s (instance=%s, version=%s)",
                 self.gateway_info.get("device_name"),
                 self.gateway_info.get("instance"),
                 self.gateway_info.get("version"))

    # ── Frame reception (writes to database) ────────────────

    def _on_gateway_message(self, msg: dict):
        """Handle all messages from the gateway."""
        msg_type = msg.get("type")

        if msg_type == "frame":
            hex_str = msg.get("hex", "")
            direction = msg.get("dir", "rx")
            if direction != "rx":
                return
            self._process_frame(hex_str)

        elif msg_type == "version":
            self.gateway_info["version"] = msg.get("version", "unknown")
            if "device_name" in msg:
                self.gateway_info["device_name"] = msg["device_name"]
            if "instance" in msg:
                self.gateway_info["instance"] = msg["instance"]

        elif msg_type == "ack":
            log.debug("ACK ref=%s status=%s", msg.get("ref"), msg.get("status"))

        elif msg_type == "error":
            log.warning("Gateway error ref=%s: %s", msg.get("ref"), msg.get("msg"))

        elif msg_type == "pi_status":
            self.gateway_stats = msg
            if "device_name" in msg:
                self.gateway_info["device_name"] = msg["device_name"]
            if "instance" in msg:
                self.gateway_info["instance"] = msg["instance"]
            if self.on_gateway_stats:
                try:
                    self.on_gateway_stats(msg)
                except Exception:
                    log.exception("on_gateway_stats callback error")

    def _process_frame(self, hex_str: str):
        """Decode a received frame and route to the status database.

        B5 capability frames are processed synchronously (one-time
        bootstrap). Data frames (C0/C1/B1) are routed through StatusDB's
        async ingest for lock-protected processing.
        """
        try:
            raw = bytes.fromhex(hex_str.replace(" ", ""))
            parsed = parse_frame(raw)
            body = parsed["body"]
            ts = datetime.now(UTC).isoformat()

            protocol_key = identify_frame(body)

            log.debug("RX %s (%dB)", protocol_key, len(body))

            if protocol_key == "rsp_0xb5":
                next_frame = process_b5(self._status, body, self._glossary, timestamp=ts)
                if self._b5_state == "waiting":
                    self._b5_next_frame = next_frame
                    if self._b5_response_event:
                        self._b5_response_event.set()
            else:
                asyncio.create_task(
                    self._db.ingest(
                        body, protocol_key,
                        timestamp=ts,
                        available_fields=self.available_fields,
                    )
                )
                if protocol_key == "rsp_0xc0" and self._follow_me_shadow is not None:
                    fm = read_field(self._status, "follow_me")
                    fm_val = fm["value"] if fm else None
                    log.debug("TRACE rsp_0xc0 follow_me=%s (shadow active)", fm_val)

        except Exception as e:
            log.debug("Frame decode error: %s", e)

    # ── Query frame builder ────────────────────────────────

    def _build_query_frame(self, query_key: str) -> bytes | None:
        if query_key == "cmd_0x41":
            if self._follow_me_shadow is not None:
                frame = self._build_follow_me_query(self._follow_me_shadow["celsius"])
                log.debug(
                    "TRACE cmd_0x41 → Follow Me frame (%dB): %s",
                    len(frame), frame.hex(" "),
                )
                return frame
            return build_status_query()
        if query_key == "cmd_0xb5":
            return build_cap_query_extended()
        if query_key.startswith("cmd_0xc1_group"):
            try:
                from blaueis.core.frame import build_group_query
                group_num = int(query_key.split("group")[1])
                page = 0x40 + group_num
                return build_group_query(page=page)
            except (ValueError, ImportError):
                log.warning("Cannot build query for %s", query_key)
                return None
        if query_key.startswith("cmd_0xb1_batch_"):
            try:
                from blaueis.core.frame import build_b1_property_query
                batch = int(query_key.rsplit("_", 1)[1])
                start = batch * self._B1_BATCH_SIZE
                end = start + self._B1_BATCH_SIZE
                pairs = getattr(self, "_b1_prop_ids", [])[start:end]
                if not pairs:
                    return None
                return build_b1_property_query(pairs)
            except (ValueError, ImportError):
                log.warning("Cannot build B1 query for %s", query_key)
                return None
        log.debug("Unknown query key: %s", query_key)
        return None

    def _build_follow_me_query(self, celsius: float) -> bytes:
        """Build a Follow Me poll frame by patching the standard status query.

        Takes the glossary-spec cmd_0x41 frame (body[1]=0x81, body[3]=0xFF)
        and overwrites body[4]=0x01 (optCommand) and body[5]=T*2+50.
        """
        base = build_status_query()
        frame = bytearray(base)
        celsius = max(0.0, min(50.0, float(celsius)))
        raw = int(round(celsius * 2 + 50))
        frame[14] = 0x01  # body[4] = optCommand Follow Me
        frame[15] = raw   # body[5] = temperature
        from blaueis.core.frame import crc8, frame_checksum
        body_end = len(frame) - 2
        frame[-2] = crc8(frame[10:body_end])
        frame[-1] = frame_checksum(frame)
        return bytes(frame)

    # ── Commands ────────────────────────────────────────────

    async def set(self, **changes) -> dict:
        """Send a set command to the AC.

        Mode-gates fields via visible_in_modes, expands mutual_exclusion
        forces, builds frames, sends atomically, and optimistic-writes.
        See docs/status_db.md for the full protocol.

        Raises RuntimeError if not connected.
        """
        if not self._client or not self._client._ws:
            raise RuntimeError("Device not connected")

        return await self._db.command(
            changes,
            send_fn=self._client.send_frame,
        )

    # ── Field reading (convenience) ─────────────────────────

    def read(self, field_name: str) -> object | None:
        r = read_field(self._status, field_name)
        return r["value"] if r else None

    def read_full(self, field_name: str) -> dict | None:
        return read_field(self._status, field_name)

    def read_all_available(self) -> dict[str, object]:
        """Read all available fields. Returns {name: value}."""
        return {fname: self.read(fname) for fname in self.available_fields}
