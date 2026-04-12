#!/usr/bin/env python3
"""HVAC Shark Midea Gateway — WebSocket server + UART bridge.

Runs on a Raspberry Pi connected to the AC's UART bus via TTL converter.
Impersonates a WiFi dongle (full protocol handshake), relays frames
between a WebSocket client and the AC unit.

Usage:
    python hvac_gateway.py --config gateway.conf
    python hvac_gateway.py --config gateway.conf --no-encrypt
"""

import argparse
import asyncio
import configparser
import contextlib
import json
import logging
import os
import platform
import signal
import sys

from blaueis.core.crypto import (
    HandshakeError,
    ReplayError,
    complete_handshake_server,
    create_hello_ok,
)
from blaueis.core.frame import FrameError, validate_frame
from blaueis.gateway.uart_protocol import UartProtocol

log = logging.getLogger("hvac_gateway")

# ── Configuration ─────────────────────────────────────────────────────────


def load_config(global_path: str = None, instance_path: str = None, legacy_path: str = None) -> dict:
    """Load gateway configuration from YAML (new) or INI (legacy) files.

    New format: global_path (/etc/blaueis/gateway.yaml) + instance_path
    (/etc/blaueis/instances/<name>.yaml).

    Legacy format: single INI file (gateway.conf) — for backwards compat
    during migration.
    """
    import yaml

    config = {
        "psk": "",
        "uart_port": "/dev/serial0",
        "uart_baud": 9600,
        "ws_host": "0.0.0.0",
        "ws_port": 8765,
        "max_queue": 16,
        "frame_spacing_ms": 100,
        "stats_interval": 60,
        "fake_ip": "192.168.1.100",
        "signal_level": 4,
        "log_level": "INFO",
        "device_name": "Midea AC",
        "allow_remote_update": True,
    }

    if legacy_path:
        # Old INI format (gateway.conf)
        cfg = configparser.ConfigParser()
        cfg.read(legacy_path)
        section = cfg["gateway"] if cfg.has_section("gateway") else {}
        config.update(
            {
                "psk": section.get("psk", ""),
                "uart_port": section.get("uart_port", "/dev/serial0"),
                "uart_baud": int(section.get("uart_baud", "9600")),
                "ws_host": section.get("ws_host", "0.0.0.0"),
                "ws_port": int(section.get("ws_port", "8765")),
                "max_queue": int(section.get("max_queue", "8")),
                "frame_spacing_ms": int(section.get("frame_spacing_ms", "100")),
                "stats_interval": int(section.get("stats_interval", "60")),
                "fake_ip": section.get("fake_ip", "192.168.1.100"),
                "signal_level": int(section.get("signal_level", "4")),
                "log_level": section.get("log_level", "INFO"),
            }
        )
        return config

    # New YAML format
    if global_path and os.path.exists(global_path):
        with open(global_path, encoding="utf-8") as f:
            g = yaml.safe_load(f) or {}
        logging_cfg = g.get("logging", {})
        config["log_level"] = logging_cfg.get("level", "INFO")
        if "allow_remote_update" in g:
            config["allow_remote_update"] = bool(g["allow_remote_update"])

    if instance_path:
        with open(instance_path, encoding="utf-8") as f:
            inst = yaml.safe_load(f) or {}
        device = inst.get("device", {})
        ws = inst.get("websocket", {})
        sec = inst.get("security", {})
        config.update(
            {
                "psk": sec.get("psk", ""),
                "uart_port": device.get("serial_port", "/dev/serial0"),
                "uart_baud": device.get("baud_rate", 9600),
                "ws_host": ws.get("host", "0.0.0.0"),
                "ws_port": ws.get("port", 8765),
                "device_name": device.get("name", "Midea AC"),
            }
        )

    return config


# ── Pi stats ──────────────────────────────────────────────────────────────


def get_pi_stats() -> dict:
    """Collect Raspberry Pi system stats."""
    stats = {
        "type": "pi_status",
        "uptime_s": 0,
        "cpu_percent": 0,
        "ram_total_mb": 0,
        "ram_used_mb": 0,
        "temp_c": 0,
        "platform": platform.machine(),
    }
    try:
        with open("/proc/uptime") as f:
            stats["uptime_s"] = int(float(f.read().split()[0]))
    except (FileNotFoundError, ValueError):
        pass
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
            stats["cpu_percent"] = round(load1 * 100 / max(os.cpu_count() or 1, 1), 1)
    except (FileNotFoundError, ValueError):
        pass
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    mem[parts[0]] = int(parts[1])
            total = mem.get("MemTotal:", 0)
            avail = mem.get("MemAvailable:", 0)
            stats["ram_total_mb"] = total // 1024
            stats["ram_used_mb"] = (total - avail) // 1024
    except (FileNotFoundError, ValueError):
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            stats["temp_c"] = round(int(f.read().strip()) / 1000, 1)
    except (FileNotFoundError, ValueError):
        pass
    try:
        st = os.statvfs("/")
        stats["disk_total_mb"] = (st.f_blocks * st.f_frsize) // (1024 * 1024)
        stats["disk_free_mb"] = (st.f_bavail * st.f_frsize) // (1024 * 1024)
        stats["disk_used_mb"] = stats["disk_total_mb"] - stats["disk_free_mb"]
    except OSError:
        stats["disk_total_mb"] = 0
        stats["disk_free_mb"] = 0
        stats["disk_used_mb"] = 0
    return stats


# ── WebSocket handler ─────────────────────────────────────────────────────


class ClientConnection:
    """One connected WebSocket client with its own crypto session."""

    def __init__(self, ws, session=None, no_encrypt=False):
        self.ws = ws
        self.session = session
        self.no_encrypt = no_encrypt

    async def send(self, msg: dict):
        try:
            if self.session and not self.no_encrypt:
                await self.ws.send(self.session.encrypt_json(msg))
            else:
                await self.ws.send(json.dumps(msg))
        except Exception as e:
            log.warning("Failed to send to %s: %s", self.ws.remote_address, e)

    def decrypt(self, raw_msg: str) -> dict:
        if self.session and not self.no_encrypt:
            return self.session.decrypt_json(raw_msg)
        return json.loads(raw_msg)


class GatewayServer:
    """WebSocket server that bridges multiple clients to the UART protocol."""

    def __init__(self, config: dict, no_encrypt: bool = False):
        self.config = config
        self.no_encrypt = no_encrypt
        self.protocol = UartProtocol(config)
        self._clients: set[ClientConnection] = set()
        self._uart_reader = None
        self._uart_writer = None

    async def _broadcast(self, msg: dict):
        """Send a message to all connected clients."""
        for client in list(self._clients):
            await client.send(msg)

    async def _handle_client_message(self, client: ClientConnection, raw_msg: str):
        """Process a message from a WebSocket client."""
        try:
            msg = client.decrypt(raw_msg)
        except (ReplayError, json.JSONDecodeError) as e:
            log.warning("Client message error: %s", e)
            return

        msg_type = msg.get("type")
        ref = msg.get("ref")

        if msg_type == "frame":
            try:
                frame_bytes = bytes.fromhex(msg["hex"].replace(" ", ""))
                validate_frame(frame_bytes)
                ok = await self.protocol.queue_frame(frame_bytes)
                if ok:
                    await client.send({"type": "ack", "ref": ref, "status": "queued"})
                else:
                    await client.send({"type": "error", "ref": ref, "msg": "queue full"})
            except FrameError as e:
                await client.send({"type": "error", "ref": ref, "msg": str(e)})
            except (ValueError, KeyError) as e:
                await client.send({"type": "error", "ref": ref, "msg": f"invalid hex: {e}"})

        elif msg_type == "ping":
            await client.send({"type": "pong"})

    async def _ws_handler(self, websocket):
        """Handle a WebSocket connection. Multiple clients supported."""
        import websockets

        log.info("Client connected from %s", websocket.remote_address)

        # Session handshake
        session = None
        try:
            if not self.no_encrypt:
                psk = bytes.fromhex(self.config["psk"])
                hello_raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                hello = json.loads(hello_raw)
                hello_ok_msg, server_rand = create_hello_ok()
                session = complete_handshake_server(psk, hello, server_rand)
                await websocket.send(json.dumps(hello_ok_msg))
                log.info("Encrypted session established with %s", websocket.remote_address)
            else:
                log.info("Client %s connected (no encryption)", websocket.remote_address)
        except (HandshakeError, TimeoutError) as e:
            log.warning("Handshake failed for %s: %s", websocket.remote_address, e)
            return

        client = ClientConnection(websocket, session, self.no_encrypt)
        self._clients.add(client)
        log.info("Active clients: %d", len(self._clients))

        # Set up frame forwarding (once, for all clients)
        if len(self._clients) == 1:
            _bg_tasks = set()  # prevent GC of fire-and-forget tasks

            def on_uart_frame(raw_frame: bytes, timestamp: float, direction: str = "rx"):
                task = asyncio.ensure_future(
                    self._broadcast({"type": "frame", "hex": raw_frame.hex(" "), "ts": timestamp, "dir": direction})
                )
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)

            self.protocol.set_on_frame(on_uart_frame)

        try:
            async for message in websocket:
                await self._handle_client_message(client, message)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._clients.discard(client)
            if not self._clients:
                self.protocol.set_on_frame(None)
                log.info("Last client disconnected")
            else:
                log.info("Client disconnected, %d remaining", len(self._clients))

    async def _stats_loop(self):
        """Periodically send Pi stats to connected clients."""
        interval = self.config.get("stats_interval", 60)
        if interval <= 0:
            return
        while True:
            await asyncio.sleep(interval)
            stats = get_pi_stats()
            stats["protocol_state"] = self.protocol.state
            stats["appliance"] = f"0x{self.protocol.appliance:02X}"
            stats["model"] = self.protocol.model
            stats["clients"] = len(self._clients)
            if self._clients:
                await self._broadcast(stats)

    async def _debug_recap(self):
        """Every 60s, log a recap of this service's recent journal entries.

        When you open journalctl after the fact, the recap gives you
        immediate context without scrolling back. Runs separately from
        the client-facing stats broadcast.
        """
        import subprocess
        import time as _time

        # Derive the syslog identifier from the instance config path
        # /etc/blaueis-gw/instances/atelier.yaml → blaueis-gw-atelier
        instance_name = ""
        instance_path = self.config.get("_instance_path", "")
        if instance_path:
            instance_name = os.path.splitext(os.path.basename(instance_path))[0]
        syslog_id = f"blaueis-gw-{instance_name}" if instance_name else ""

        while True:
            await asyncio.sleep(60)

            proto = self.protocol
            silence_age = _time.monotonic() - proto.silence_timer if proto.silence_timer else 0

            # One-line status summary
            log.info(
                "recap state=%s clients=%d tx_queue=%d/%d last_frame=%.0fs ago",
                proto.state,
                len(self._clients),
                proto._tx_queue.qsize(),
                proto._tx_queue.maxsize,
                silence_age,
            )

            # Tail the last 10 journal entries for this service
            if syslog_id:
                try:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        [
                            "journalctl", "-t", syslog_id,
                            "-n", "10", "--no-pager", "-o", "short-iso",
                        ],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.stdout.strip():
                        for line in result.stdout.strip().splitlines():
                            log.info("  | %s", line)
                except Exception:
                    pass  # journalctl not available or timed out — skip silently

    async def _uart_loop(self):
        """Open UART and run the protocol state machine."""
        import serial_asyncio

        port = self.config["uart_port"]
        baud = self.config["uart_baud"]

        while True:
            try:
                log.info("Opening UART %s @ %d baud", port, baud)
                self._uart_reader, self._uart_writer = await serial_asyncio.open_serial_connection(
                    url=port,
                    baudrate=baud,
                )
                # Flush stale data from the serial buffer — serial_asyncio
                # does not clear the OS buffer on open, so leftover bytes
                # (often 0xFF) cause the frame reader to miss the first response
                try:
                    stale = await asyncio.wait_for(self._uart_reader.read(1024), timeout=0.1)
                    if stale:
                        log.debug("Flushed %d stale bytes from UART buffer", len(stale))
                except TimeoutError:
                    pass
                log.info("UART connected")
                await self.protocol.run(self._uart_reader, self._uart_writer)
            except PermissionError:
                log.error(
                    "UART permission denied on %s. "
                    "The service user needs to be in the 'dialout' group. Fix with:\n"
                    "  sudo usermod -aG dialout $(whoami)\n"
                    "Then restart the service:\n"
                    "  sudo systemctl restart blaueis-gateway@<instance>",
                    port,
                )
                # Don't retry rapidly on permission errors — it won't fix itself
                await asyncio.sleep(60)
            except FileNotFoundError:
                log.error(
                    "UART port %s not found. Check:\n"
                    "  • Is the serial port path correct in /etc/blaueis/instances/<name>.yaml?\n"
                    "  • Is the USB adapter plugged in?\n"
                    "  • For GPIO UART: is 'dtoverlay=disable-bt' in /boot/config.txt?\n"
                    "  • Run: ls -la %s",
                    port,
                    port,
                )
                await asyncio.sleep(30)
            except Exception as e:
                log.error("UART error: %s, reconnecting in 5s", e)
                self._uart_reader = None
                self._uart_writer = None
                await asyncio.sleep(5)

    async def run(self):
        """Start all tasks: WebSocket server + UART + stats."""
        import websockets

        host = self.config["ws_host"]
        port = self.config["ws_port"]

        log.info("Starting gateway on ws://%s:%d", host, port)

        async with websockets.serve(self._ws_handler, host, port):
            log.info("WebSocket server listening")
            await asyncio.gather(
                self._uart_loop(),
                self._stats_loop(),
                self._debug_recap(),
            )


# ── Main ──────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Blaueis Midea Gateway")
    # New YAML config (systemd passes these)
    parser.add_argument("--global", dest="global_config", help="Path to /etc/blaueis/gateway.yaml")
    parser.add_argument("--instance", dest="instance_config", help="Path to /etc/blaueis/instances/<name>.yaml")
    # Legacy INI config (backwards compat with old gateway.conf)
    parser.add_argument("--config", help="Legacy: path to gateway.conf (INI format)")
    parser.add_argument("--no-encrypt", action="store_true", help="Disable encryption (development)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Set log level to VERBOSE (raw UART hex)")
    args = parser.parse_args()

    if not args.config and not args.instance_config:
        parser.error("Either --instance (YAML) or --config (legacy INI) is required")

    # ── Early permission checks (visible in journalctl) ──────
    config_path = args.instance_config or args.config
    try:
        if args.config:
            config = load_config(legacy_path=args.config)
        else:
            config = load_config(global_path=args.global_config, instance_path=args.instance_config)
            config["_instance_path"] = args.instance_config or ""
    except PermissionError:
        print(
            f"ERROR: Cannot read config file: {config_path}\n"
            f"  The service user does not have read access.\n"
            f"  Fix: sudo chown blaueis:blaueis {config_path}\n"
            f"       sudo chmod 640 {config_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    except FileNotFoundError:
        print(
            f"ERROR: Config file not found: {config_path}\n"
            f"  Run 'blaueis-configure' to create it, or check the\n"
            f"  instance name in the systemd service.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check UART port exists before starting the event loop
    uart_port = config.get("uart_port", "/dev/serial0")
    if not os.path.exists(uart_port):
        print(
            f"WARNING: Serial port {uart_port} does not exist.\n"
            f"  The gateway will retry once the port appears.\n"
            f"  Check: ls -la {uart_port}",
            file=sys.stderr,
        )
    elif not os.access(uart_port, os.R_OK | os.W_OK):
        print(
            f"ERROR: No read/write access to {uart_port}.\n"
            f"  Add the service user to the dialout group:\n"
            f"    sudo usermod -aG dialout $(whoami)\n"
            f"  Then restart the service.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Custom VERBOSE level (5) — below DEBUG (10)
    # See: https://docs.python.org/3/library/logging.html#logging-levels
    VERBOSE = 5
    logging.addLevelName(VERBOSE, "VERBOSE")

    if args.verbose:
        log_level = VERBOSE
    else:
        level_name = config["log_level"].upper()
        log_level = {"VERBOSE": VERBOSE}.get(level_name, getattr(logging, level_name, logging.INFO))

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    server = GatewayServer(config, no_encrypt=args.no_encrypt)

    loop = asyncio.new_event_loop()

    def shutdown(sig):
        log.info("Received %s, shutting down", sig.name)
        server.protocol.stop()
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown, sig)

    try:
        loop.run_until_complete(server.run())
    except KeyboardInterrupt:
        log.info("Keyboard interrupt, shutting down")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
