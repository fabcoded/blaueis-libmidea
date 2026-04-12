#!/usr/bin/env python3
"""HVAC Gateway CLI — interactive client for the Pi gateway.

Usage:
    python cli.py --host 192.168.1.50 --psk <hex>
    python cli.py --host 192.168.1.50 --no-encrypt

Commands:
    query               Send status query (0x41), show response
    caps                Send capability query (B5)
    raw <hex>           Send raw hex frame
    monitor             Continuous frame display (Ctrl+C to stop)
    pi                  Show Pi gateway stats
    ping                Keepalive ping
    help                Show commands
    quit                Disconnect and exit
"""

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

# tools-serial has the canonical frame builders. Inserted LAST so it's
# searched FIRST in sys.path (overrides gateway's stripped-down midea_frame).

from blaueis.client.ws_client import HvacClient
from blaueis.core.frame import build_cap_query_extended, build_cap_query_simple, build_status_query

log = logging.getLogger("cli")


def format_frame(hex_str: str, ts: float) -> str:
    """Format a received frame for display."""
    clean = hex_str.replace(" ", "")
    if len(clean) < 4:
        return f"  [{ts:.3f}] {hex_str}"

    # Parse basic info
    body_start = clean[20:]  # after 10-byte header (20 hex chars)
    cmd_byte = body_start[:2] if body_start else "??"
    msg_type = clean[18:20]

    labels = {
        "c0": "C0 Status Response",
        "c1": "C1 Extended Data",
        "b5": "B5 Capabilities",
        "a1": "A1 Heartbeat",
        "a0": "A0 Heartbeat ACK",
        "41": "41 Status Query",
        "40": "40 Set Command",
    }
    label = labels.get(cmd_byte.lower(), f"CMD 0x{cmd_byte}")

    return f"  [{ts:.3f}] {label} (msg=0x{msg_type}, {len(clean) // 2}B): {hex_str}"


async def interactive_loop(client: HvacClient):
    """Run the interactive command loop."""
    # Start listener in background
    listen_task = asyncio.create_task(client.listen())

    # Set up frame display callback
    monitoring = [False]

    def on_frame(hex_str, ts):
        if monitoring[0]:
            print(format_frame(hex_str, ts))

    def on_pi_status(stats):
        print(
            f"\n  Pi: cpu={stats.get('cpu_percent', '?')}% "
            f"ram={stats.get('ram_used_mb', '?')}/{stats.get('ram_total_mb', '?')}MB "
            f"temp={stats.get('temp_c', '?')}°C "
            f"up={stats.get('uptime_s', 0) // 3600}h "
            f"state={stats.get('protocol_state', '?')}"
        )

    client.on_frame = on_frame
    client.on_pi_status = on_pi_status

    print("Connected. Type 'help' for commands.\n")

    try:
        while True:
            try:
                line = await asyncio.get_event_loop().run_in_executor(None, lambda: input("> "))
            except EOFError:
                break

            parts = line.strip().split()
            if not parts:
                continue

            cmd = parts[0].lower()

            if cmd == "quit" or cmd == "exit":
                break
            elif cmd == "help":
                print("  query    — send status query (0x41)")
                print("  caps     — send capability query (B5)")
                print("  raw <hex>— send raw hex frame")
                print("  monitor  — toggle continuous frame display")
                print("  pi       — request Pi stats")
                print("  ping     — keepalive")
                print("  quit     — exit")
            elif cmd == "query":
                print("  Sending status query...")
                await client.send_frame(build_status_query().hex(" "))
            elif cmd == "caps":
                print("  Sending capability queries (extended + simple)...")
                await client.send_frame(build_cap_query_extended().hex(" "))
                await asyncio.sleep(0.2)
                await client.send_frame(build_cap_query_simple().hex(" "))
            elif cmd == "raw":
                hex_str = " ".join(parts[1:])
                if not hex_str:
                    print("  Usage: raw AA 23 AC ...")
                    continue
                ref = await client.send_frame(hex_str)
                print(f"  Sent (ref={ref})")
            elif cmd == "monitor":
                monitoring[0] = not monitoring[0]
                print(f"  Monitor {'ON' if monitoring[0] else 'OFF'}")
            elif cmd == "pi":
                await client.send_ping()  # stats come on their own interval
                print("  Pi stats will appear on next interval")
            elif cmd == "ping":
                await client.send_ping()
                print("  Ping sent")
            else:
                print(f"  Unknown command: {cmd}. Type 'help'.")

    except KeyboardInterrupt:
        pass
    finally:
        listen_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listen_task


async def run(args):
    from blaueis.core.crypto import psk_to_bytes

    psk = psk_to_bytes(args.psk) if args.psk else None
    client = HvacClient(args.host, args.port, psk=psk, no_encrypt=args.no_encrypt)

    try:
        await client.connect()
        await interactive_loop(client)
    finally:
        await client.close()
        print("Disconnected.")


def main():
    parser = argparse.ArgumentParser(description="HVAC Gateway CLI Client")
    parser.add_argument("--host", required=True, help="Gateway hostname/IP")
    parser.add_argument("--port", type=int, default=8765, help="Gateway WebSocket port")
    parser.add_argument("--psk", help="Pre-shared key (hex)")
    parser.add_argument("--no-encrypt", action="store_true", help="Disable encryption")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
