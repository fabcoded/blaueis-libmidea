"""High-level Device abstraction over the Blaueis gateway.

Wraps HvacClient + blaueis-core status/codec into an autonomous device
that manages its own connection, capability discovery, polling, and
state change notification.

Usage:
    device = Device("192.168.1.50", 8765, psk="myPassphrase")
    device.on_state_change = lambda field, value, old: print(f"{field}: {old} → {value}")
    await device.start()
    # ... device is now polling and listening autonomously ...
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


class Device:
    """Autonomous HVAC device connected via Blaueis gateway.

    Lifecycle: __init__ → start() → [running] → stop()
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

        # State
        self._glossary = load_glossary()
        self._status: dict = build_status(glossary=self._glossary)
        self._client: HvacClient | None = None
        self._registered_fields: set[str] = set()
        self._required_queries: set[str] = set()  # protocol_keys needed

        # Gateway info (populated from version/pi_status messages)
        self.gateway_info: dict = {
            "version": "unknown",
            "device_name": "Midea AC",
            "instance": "",
        }
        self.gateway_stats: dict = {}  # latest pi_status data

        # Callbacks
        self.on_state_change: Callable[[str, object, object], None] | None = None
        self.on_connected: Callable[[], None] | None = None
        self.on_disconnected: Callable[[], None] | None = None
        self.on_gateway_stats: Callable[[dict], None] | None = None

        # Tasks
        self._listen_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._running = False

    # ── Properties ──────────────────────────────────────────

    @property
    def status(self) -> dict:
        """Current glossary-driven status dict."""
        return self._status

    @property
    def glossary(self) -> dict:
        return self._glossary

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client._ws is not None

    @property
    def capabilities_received(self) -> bool:
        return self._status["meta"].get("b5_received", False)

    @property
    def available_fields(self) -> dict[str, dict]:
        """Fields confirmed available by B5 or marked 'always'/'readable'.

        Returns {field_name: {field_class, data_type, writable, feature_available, ...}}
        """
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
        """Protocol keys (frame types) needed to fulfill registered fields."""
        return frozenset(self._required_queries)

    # ── Field registration ──────────────────────────────────

    def register_fields(self, field_names: set[str] | list[str]):
        """Tell the poller which fields are needed. Updates required_queries."""
        self._registered_fields = set(field_names)
        self._recompute_queries()

    def register_all_available(self):
        """Register all B5-confirmed fields for polling."""
        self._registered_fields = set(self.available_fields.keys())
        self._recompute_queries()

    def _recompute_queries(self):
        """Deduplicate: which query frames cover the registered fields?"""
        needed = set()
        all_fields = walk_fields(self._glossary)
        for fname in self._registered_fields:
            gdef = all_fields.get(fname, {})
            protocols = gdef.get("protocols", {})
            for pkey, pdef in protocols.items():
                if pdef.get("direction") == "response":
                    # Map response key to the query that triggers it
                    query_key = self._response_to_query(pkey)
                    if query_key:
                        needed.add(query_key)
        self._required_queries = needed
        log.debug("Required queries: %s (for %d fields)", needed, len(self._registered_fields))

    @staticmethod
    def _response_to_query(response_key: str) -> str | None:
        """Map a response protocol_key to the query that triggers it."""
        if response_key == "rsp_0xc0":
            return "cmd_0x41"
        if response_key.startswith("rsp_0xc1_group"):
            # C1 groups need their specific group query
            return response_key.replace("rsp_", "cmd_")
        if response_key in ("rsp_0xb5", "rsp_0xb5_tlv"):
            return "cmd_0xb5"
        if response_key in ("rsp_0xa1",):
            return "cmd_0x41"  # A1 comes from status query
        if response_key in ("rsp_0xb1",):
            return None  # B1 is a response to B0 command, not polled
        return None

    # ── Lifecycle ───────────────────────────────────────────

    async def start(self):
        """Connect to gateway, discover capabilities, start loops."""
        if self._running:
            return

        psk_bytes = None
        if self._psk_raw and not self._no_encrypt:
            if isinstance(self._psk_raw, bytes):
                psk_bytes = self._psk_raw
            else:
                psk_bytes = psk_to_bytes(self._psk_raw)

        self._client = HvacClient(
            self.host, self.port, psk=psk_bytes, no_encrypt=self._no_encrypt
        )
        await self._client.connect()
        self._running = True

        if self.on_connected:
            self.on_connected()

        # Wire up frame reception
        self._client.add_listener(self._on_gateway_message)

        # Start listen loop
        self._listen_task = asyncio.create_task(self._listen_loop())

        # Query gateway info
        await self._query_gateway_info()

        # Query capabilities
        await self._query_capabilities()

        # Start poll loop
        self._poll_task = asyncio.create_task(self._poll_loop())

        log.info("Device started: %s:%d", self.host, self.port)

    async def stop(self):
        """Stop loops and disconnect."""
        self._running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._client:
            await self._client.close()
            self._client = None

        if self.on_disconnected:
            self.on_disconnected()

        log.info("Device stopped")

    # ── Capability discovery ────────────────────────────────

    async def _query_capabilities(self):
        """Send B5 extended + simple queries and wait for responses."""
        if not self._client:
            return

        log.info("Querying B5 capabilities...")
        await self._client.send_frame(build_cap_query_extended().hex(" "))
        await asyncio.sleep(0.5)
        await self._client.send_frame(build_cap_query_simple().hex(" "))

        # Give time for B5 responses to arrive and be processed
        for _ in range(20):
            await asyncio.sleep(0.25)
            if self._status["meta"].get("b5_received", False):
                break

        finalize_capabilities(self._status, self._glossary)

        avail = self.available_fields
        log.info("B5 complete: %d available fields", len(avail))

    # ── Frame reception ─────────────────────────────────────

    async def _query_gateway_info(self):
        """Request gateway version/name info."""
        if not self._client:
            return
        await self._client._send({"type": "version", "ref": 0})
        # Wait briefly for response
        for _ in range(10):
            await asyncio.sleep(0.2)
            if self.gateway_info.get("version") != "unknown":
                break
        log.info(
            "Gateway: %s (instance=%s, version=%s)",
            self.gateway_info.get("device_name"),
            self.gateway_info.get("instance"),
            self.gateway_info.get("version"),
        )

    def _on_gateway_message(self, msg: dict):
        """Handle all messages from the gateway."""
        msg_type = msg.get("type")

        if msg_type == "frame":
            hex_str = msg.get("hex", "")
            direction = msg.get("dir", "rx")
            if direction != "rx":
                return  # skip our own TX echoes
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
        """Decode a received frame and update status."""
        try:
            raw = bytes.fromhex(hex_str.replace(" ", ""))
            parsed = parse_frame(raw)
            body = parsed["body"]
            ts = datetime.now(UTC).isoformat()

            protocol_key = identify_frame(body)

            if protocol_key == "rsp_0xb5":
                process_b5(self._status, body, self._glossary, timestamp=ts)
            else:
                # Snapshot old values for change detection
                old_values = self._snapshot_fields()
                process_data_frame(
                    self._status, body, protocol_key, self._glossary, timestamp=ts
                )
                self._detect_changes(old_values)

        except Exception as e:
            log.debug("Frame decode error: %s", e)

    def _snapshot_fields(self) -> dict[str, object]:
        """Snapshot current field values for change detection."""
        snap = {}
        for fname in self._registered_fields:
            r = read_field(self._status, fname)
            snap[fname] = r["value"] if r else None
        return snap

    def _detect_changes(self, old_values: dict[str, object]):
        """Compare current values with snapshot and fire callbacks."""
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

    # ── Listen loop ─────────────────────────────────────────

    async def _listen_loop(self):
        """Continuously listen for gateway messages. Reconnects on failure."""
        while self._running:
            try:
                if self._client:
                    await self._client.listen()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Listen loop ended: %s", e)

            if not self._running:
                break

            # Connection lost — try to reconnect
            if self.on_disconnected:
                self.on_disconnected()
            await self._reconnect()

    async def _reconnect(self):
        """Attempt to reconnect to the gateway with backoff."""
        delays = [5, 10, 30, 60]
        for attempt, delay in enumerate(delays, 1):
            if not self._running:
                return
            log.info("Reconnecting to %s:%d (attempt %d, wait %ds)...", self.host, self.port, attempt, delay)
            await asyncio.sleep(delay)
            if not self._running:
                return
            try:
                if self._client:
                    await self._client.close()

                psk_bytes = None
                if self._psk_raw and not self._no_encrypt:
                    psk_bytes = self._psk_raw if isinstance(self._psk_raw, bytes) else psk_to_bytes(self._psk_raw)

                self._client = HvacClient(self.host, self.port, psk=psk_bytes, no_encrypt=self._no_encrypt)
                await self._client.connect()
                self._client.add_listener(self._on_gateway_message)

                if self.on_connected:
                    self.on_connected()

                log.info("Reconnected to %s:%d", self.host, self.port)
                return  # success — listen loop will resume
            except Exception as e:
                log.warning("Reconnect attempt %d failed: %s", attempt, e)

        # All attempts exhausted — keep retrying every 60s
        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                return
            try:
                if self._client:
                    await self._client.close()

                psk_bytes = None
                if self._psk_raw and not self._no_encrypt:
                    psk_bytes = self._psk_raw if isinstance(self._psk_raw, bytes) else psk_to_bytes(self._psk_raw)

                self._client = HvacClient(self.host, self.port, psk=psk_bytes, no_encrypt=self._no_encrypt)
                await self._client.connect()
                self._client.add_listener(self._on_gateway_message)

                if self.on_connected:
                    self.on_connected()

                log.info("Reconnected to %s:%d", self.host, self.port)
                return
            except Exception as e:
                log.warning("Reconnect failed: %s", e)

    # ── Poll loop ───────────────────────────────────────────

    async def _poll_loop(self):
        """Send queries for registered fields on interval."""
        try:
            # Initial status query
            await self._send_poll_queries()

            while self._running:
                await asyncio.sleep(self.poll_interval)
                if not self._running:
                    break
                try:
                    await self._send_poll_queries()
                except Exception as e:
                    log.debug("Poll failed (will retry next interval): %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Poll loop error: %s", e)

    async def _send_poll_queries(self):
        """Send the minimum set of queries to fulfill registered fields."""
        if not self._client:
            return

        queries = self._required_queries
        if not queries:
            # Default: at least send a status query
            queries = {"cmd_0x41"}

        for qkey in queries:
            frame = self._build_query_frame(qkey)
            if frame:
                await self._client.send_frame(frame.hex(" "))
                await asyncio.sleep(0.1)  # inter-frame spacing

    def _build_query_frame(self, query_key: str) -> bytes | None:
        """Build a query frame from a protocol key."""
        if query_key == "cmd_0x41":
            return build_status_query()
        if query_key == "cmd_0xb5":
            return build_cap_query_extended()
        # C1 group queries: cmd_0xc1_groupN → page 0x40+N
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

        Args:
            **changes: field_name=value pairs (e.g. power=True, target_temperature=24)

        Returns:
            Result dict from build_command_body (includes preflight errors if any).
        """
        if not self._client:
            raise RuntimeError("Device not connected")

        # Determine if any changes need 0xB0 (property set) vs 0x40
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
            result = build_command_body(
                self._status, x40_changes, self._glossary
            )
            if result["body"] is not None:
                frame = build_frame(result["body"], msg_type=0x02)
                await self._client.send_frame(frame.hex(" "))
                log.info("Sent 0x40 command: %s", x40_changes)
            results["cmd_0x40"] = result

        if b0_changes:
            result = build_b0_command_body(
                self._status, b0_changes, self._glossary
            )
            if result["body"] is not None:
                frame = build_frame(result["body"], msg_type=0x02)
                await self._client.send_frame(frame.hex(" "))
                log.info("Sent 0xB0 command: %s", b0_changes)
            results["cmd_0xb0"] = result

        return results

    # ── Field reading (convenience) ─────────────────────────

    def read(self, field_name: str) -> object | None:
        """Read a single field's current value (convenience wrapper)."""
        r = read_field(self._status, field_name)
        return r["value"] if r else None

    def read_full(self, field_name: str) -> dict | None:
        """Read a field with full metadata (value, timestamp, source, disagreements)."""
        return read_field(self._status, field_name)

    def read_all_registered(self) -> dict[str, object]:
        """Read all registered fields. Returns {name: value}."""
        return {fname: self.read(fname) for fname in self._registered_fields}
