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
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from blaueis.client.ws_client import HvacClient
from blaueis.core.codec import (
    build_cap_index,
    build_field_map,
    identify_frame,
    load_glossary,
    walk_fields,
)
from blaueis.core.command import build_command_body, build_b0_command_body
from blaueis.core.crypto import psk_to_bytes
from blaueis.core.frame import (
    build_cap_query_extended,
    build_cap_query_simple,
    build_frame,
    build_status_query,
    parse_frame,
)
from blaueis.core.process import (
    finalize_capabilities,
    process_b5,
    process_data_frame,
    process_raw_frame,
)
from blaueis.core.query import read_field
from blaueis.core.status import build_status

log = logging.getLogger("blaueis.device")

# Fields that fold into the HA climate entity — never exposed as standalone.
CLIMATE_FIELDS = frozenset({"operating_mode", "target_temperature", "fan_speed"})

DEFAULT_POLL_INTERVAL = 15.0  # seconds
SUPERVISOR_CHECK_INTERVAL = 5.0  # seconds
RECONNECT_DELAYS = [5, 10, 30, 60]  # initial backoff sequence
RECONNECT_FOREVER_INTERVAL = 60  # after exhausting backoff


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
        self._glossary = load_glossary()
        self._status: dict = build_status(glossary=self._glossary)
        self._registered_fields: set[str] = set()
        self._required_queries: set[str] = set()

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
        self.on_state_change: Callable[[str, object, object], None] | None = None
        self.on_connected: Callable[[], None] | None = None
        self.on_disconnected: Callable[[], None] | None = None
        self.on_gateway_stats: Callable[[dict], None] | None = None

        # ── Task management ────────────────────────────────
        self._running = False
        self._supervisor_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None

    # ── Properties ──────────────────────────────────────────

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
        if not self._listen_task or self._listen_task.done():
            return False
        return True

    @property
    def capabilities_received(self) -> bool:
        return self._status["meta"].get("b5_received", False)

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

    @property
    def required_queries(self) -> set[str]:
        return frozenset(self._required_queries)

    # ── Field registration ──────────────────────────────────

    def register_fields(self, field_names: set[str] | list[str]):
        self._registered_fields = set(field_names)
        self._recompute_queries()

    def register_all_available(self):
        self._registered_fields = set(self.available_fields.keys())
        self._recompute_queries()

    def _recompute_queries(self):
        needed = set()
        all_fields = walk_fields(self._glossary)
        for fname in self._registered_fields:
            gdef = all_fields.get(fname, {})
            protocols = gdef.get("protocols", {})
            for pkey, pdef in protocols.items():
                if pdef.get("direction") == "response":
                    query_key = self._response_to_query(pkey)
                    if query_key:
                        needed.add(query_key)
        self._required_queries = needed
        log.debug("Required queries: %s (for %d fields)", needed, len(self._registered_fields))

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
        if response_key in ("rsp_0xb1",):
            return None
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

        # Start poll loop
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
                try:
                    await task
                except asyncio.CancelledError:
                    pass

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
                    try:
                        await self._client.close()
                    except Exception:
                        pass
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
        """Send minimum query set for registered fields."""
        if not self._client or not self._client._ws:
            return  # skip this cycle, try next

        queries = self._required_queries or {"cmd_0x41"}

        for qkey in queries:
            if not self._running:
                break
            frame = self._build_query_frame(qkey)
            if frame:
                try:
                    await self._client.send_frame(frame.hex(" "))
                except Exception as e:
                    log.debug("Send failed for %s: %s", qkey, e)
                await asyncio.sleep(0.1)  # inter-frame spacing

    # ── Capability discovery (once) ─────────────────────────

    async def _query_capabilities(self):
        """Send B5 queries and wait for responses. Called once during start()."""
        if not self._client:
            return
        if self.capabilities_received:
            log.info("B5 capabilities already loaded, skipping")
            return

        log.info("Querying B5 capabilities...")
        caps_before = len(self._status.get("capabilities_raw", []))

        # Send extended query, wait for response
        try:
            await self._client.send_frame(build_cap_query_extended().hex(" "))
        except Exception as e:
            log.warning("B5 extended send failed: %s", e)

        for _ in range(20):
            await asyncio.sleep(0.25)
            if len(self._status.get("capabilities_raw", [])) > caps_before:
                break  # got extended response

        # Send simple query, wait for response
        caps_after_ext = len(self._status.get("capabilities_raw", []))
        try:
            await self._client.send_frame(build_cap_query_simple().hex(" "))
        except Exception as e:
            log.warning("B5 simple send failed: %s", e)

        for _ in range(20):
            await asyncio.sleep(0.25)
            if len(self._status.get("capabilities_raw", [])) > caps_after_ext:
                break  # got simple response

        finalize_capabilities(self._status, self._glossary)
        log.info("B5 complete: %d available fields", len(self.available_fields))

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
        """Decode a received frame and update the status database."""
        try:
            raw = bytes.fromhex(hex_str.replace(" ", ""))
            parsed = parse_frame(raw)
            body = parsed["body"]
            ts = datetime.now(UTC).isoformat()

            protocol_key = identify_frame(body)

            if protocol_key == "rsp_0xb5":
                process_b5(self._status, body, self._glossary, timestamp=ts)
            else:
                old_values = self._snapshot_fields()
                process_data_frame(
                    self._status, body, protocol_key, self._glossary, timestamp=ts
                )
                self._detect_changes(old_values)

        except Exception as e:
            log.debug("Frame decode error: %s", e)

    def _snapshot_fields(self) -> dict[str, object]:
        snap = {}
        for fname in self._registered_fields:
            r = read_field(self._status, fname)
            snap[fname] = r["value"] if r else None
        return snap

    def _detect_changes(self, old_values: dict[str, object]):
        if not self.on_state_change:
            return
        for fname in self._registered_fields:
            r = read_field(self._status, fname)
            new_val = r["value"] if r else None
            old_val = old_values.get(fname)
            if new_val != old_val:
                try:
                    self.on_state_change(fname, new_val, old_val)
                except Exception:
                    log.exception("on_state_change callback error for %s", fname)

    # ── Query frame builder ────────────────────────────────

    def _build_query_frame(self, query_key: str) -> bytes | None:
        if query_key == "cmd_0x41":
            return build_status_query()
        if query_key == "cmd_0xb5":
            return build_cap_query_extended()
        if query_key.startswith("cmd_0xc1_group"):
            try:
                from blaueis.core.frame import build_group_query
                group_num = int(query_key.split("group")[1])
                page = 0x40 + group_num
                return build_group_query(page)
            except (ValueError, ImportError):
                log.warning("Cannot build query for %s", query_key)
                return None
        log.debug("Unknown query key: %s", query_key)
        return None

    # ── Commands ────────────────────────────────────────────

    async def set(self, **changes) -> dict:
        """Send a set command to the AC.

        Raises RuntimeError if not connected.
        """
        if not self._client or not self._client._ws:
            raise RuntimeError("Device not connected")

        all_fields = walk_fields(self._glossary)
        b0_changes = {}
        x40_changes = {}

        for fname, value in changes.items():
            gdef = all_fields.get(fname, {})
            protocols = gdef.get("protocols", {})
            if "cmd_0xb0" in protocols:
                b0_changes[fname] = value
            else:
                x40_changes[fname] = value

        results = {}

        if x40_changes:
            result = build_command_body(self._status, x40_changes, self._glossary)
            if result["body"] is not None:
                frame = build_frame(result["body"], msg_type=0x02)
                await self._client.send_frame(frame.hex(" "))
                log.info("Sent 0x40 command: %s", x40_changes)
            elif result["preflight"]:
                log.warning("Command blocked by preflight: %s", result["preflight"])
            results["cmd_0x40"] = result

        if b0_changes:
            result = build_b0_command_body(self._status, b0_changes, self._glossary)
            if result["body"] is not None:
                frame = build_frame(result["body"], msg_type=0x02)
                await self._client.send_frame(frame.hex(" "))
                log.info("Sent 0xB0 command: %s", b0_changes)
            results["cmd_0xb0"] = result

        return results

    # ── Field reading (convenience) ─────────────────────────

    def read(self, field_name: str) -> object | None:
        r = read_field(self._status, field_name)
        return r["value"] if r else None

    def read_full(self, field_name: str) -> dict | None:
        return read_field(self._status, field_name)

    def read_all_registered(self) -> dict[str, object]:
        return {fname: self.read(fname) for fname in self._registered_fields}
