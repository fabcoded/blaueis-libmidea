#!/usr/bin/env python3
"""Live HVAC monitor with scan loop.

Two concurrent async tasks:

  RECEIVER: continuously processes ALL incoming frames from the gateway.
    - B5 capability responses -> process_b5 (appends caps to database)
    - C0/C1/A1 data frames -> process_data_frame (decodes available fields)
    - Fields with feature_available='always' decode before B5 is known
    - Fields with 'capability'/'never' are skipped

  SENDER: manages a scan queue that evolves through phases:
    1. BOOT: send C0 status query (power, mode, temps -- always-available)
    2. CAPS: if caps unknown, add B5 queries to front of queue
    3. RESOLVED: after caps finalized, rebuild queue from glossary
       (only queries that carry available fields)
    4. RUNNING: every 15s send full scan queue
    5. Every 30min: re-query B5 caps (AC might have power-cycled)

Usage:
    python ac_monitor.py --host 192.168.210.30 --psk <hex>
    python ac_monitor.py --host 192.168.210.30 --no-encrypt --interval 15
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

# ac-monitor/ -> examples/ -> raspi-midea/ -> tools/ -> HVAC-shark/
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
# Order matters: tools-serial midea_frame.py is canonical (full builders).
# Gateway has a stripped-down midea_frame.py with only handshake builders.
# Insert tools-serial LAST so it ends up FIRST in sys.path.

import websockets  # noqa: E402
from blaueis.core.quirks import apply_quirks_files  # noqa: E402
from blaueis.core.command import build_command_body  # noqa: E402
from blaueis.core.status import build_status  # noqa: E402
from blaueis.core.query import read_field  # noqa: E402
from blaueis.core.crypto import complete_handshake_client, create_hello, psk_to_bytes  # noqa: E402
from blaueis.core.codec import (  # noqa: E402
    build_frame_from_spec,
    build_scan_queue,
    detect_dead_frames,
    load_glossary,
    walk_fields,
)
from blaueis.core.frame import build_frame, parse_frame  # noqa: E402
from blaueis.core.process import finalize_capabilities, process_b5, process_data_frame  # noqa: E402

log = logging.getLogger("ac_monitor")

CAP_RESCAN_INTERVAL = 1800  # 30 minutes


def save_status(status: dict, db_path: Path):
    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)


def build_query_table(status: dict, glossary: dict) -> dict[str, list[str]]:
    """Which response frames carry which available fields.

    Used by the sender task to report coverage in the 'capabilities
    resolved' summary line. The planner no longer consumes this — it
    walks `frames[].triggers` directly via plan_query_cycle().
    """
    fields_def = walk_fields(glossary)
    table = {}
    for name, fdef in fields_def.items():
        fa = status["fields"].get(name, {}).get("feature_available", "always")
        if fa == "never":
            continue
        for pkey, ploc in fdef.get("protocols", {}).items():
            if pkey.startswith("rsp_") and ploc.get("decode"):
                table.setdefault(pkey, []).append(name)
    return table


def identify_body(body: bytes) -> str | None:
    """Identify frame type from body[0]. Handles responses AND heartbeats."""
    if not body:
        return None
    tag = body[0]
    if tag == 0xC0 or tag == 0xA0:  # A0 is status echo (same layout as C0)
        return "rsp_0xc0"
    if tag == 0xC1 and len(body) > 3:
        # body[3] carries the group page selector (echoed from query).
        # Low nibble gives the group number (body[3] & 0x0F).
        return f"rsp_0xc1_group{body[3] & 0x0F}"
    if tag == 0xB5:
        return "rsp_0xb5"
    if tag == 0xB1:
        return "rsp_0xb1"
    if tag == 0xA1:
        return "rsp_0xa1"
    # A3, A5, A6: heartbeat types — no glossary decode yet, skip
    return None


# ── Receiver task ─────────────────────────────────────────────────────────


async def receiver_task(ws, session, status, glossary, db_path, prev_values, no_encrypt):
    """Continuously receive and process ALL frames from the gateway."""
    b5_count = 0
    frame_count = 0

    while True:
        try:
            raw = await ws.recv()
            msg = session.decrypt_json(raw) if session and not no_encrypt else json.loads(raw)
        except Exception:
            break

        if msg.get("type") != "frame":
            continue

        hex_str = msg.get("hex", "")
        try:
            parsed = parse_frame(bytes.fromhex(hex_str.replace(" ", "")))
            body = parsed["body"]
        except Exception:
            continue

        rsp_key = identify_body(body)
        if not rsp_key:
            continue

        frame_count += 1

        if rsp_key == "rsp_0xb5":
            process_b5(status, body, glossary)
            b5_count += 1
            cap_count = len(status.get("capabilities_raw", []))
            log.info("B5 #%d: %d caps total", b5_count, cap_count)
            save_status(status, db_path)
            continue

        process_data_frame(status, body, rsp_key, glossary)

        # Print changes
        changed = False
        for name in sorted(status["fields"]):
            r = read_field(status, name)
            if r is None:
                continue
            val = r["value"]
            prev = prev_values.get(name)
            if prev != val:
                src = r["source"]
                if prev is not None:
                    print(f"  {name:30s} = {prev} -> {val}  ({src})")
                else:
                    print(f"  {name:30s} = {val}  ({src})")
                prev_values[name] = val
                changed = True

        if changed:
            populated = sum(1 for f in status["fields"].values() if f.get("sources"))
            print(f"  [{populated}/{len(status['fields'])} fields, #{frame_count} {rsp_key}]")
        # Always save (to keep frame_counts/timestamps current)
        save_status(status, db_path)


# ── Sender task (scan loop) ──────────────────────────────────────────────


async def sender_task(ws, session, status, glossary, db_path, interval, no_encrypt, proto, bus, quirks_paths=None):
    """Scan loop: build and send query queue based on database state.

    All frame construction goes through build_scan_queue() (a pure
    function) which consumes the glossary's top-level `frames:` dict.
    No hardcoded byte offsets or frame IDs in this function —
    see §A.9 codec contract in serial_glossary_guide.md Part 4.
    """

    async def send_frame(frame_bytes):
        msg = {"type": "frame", "hex": frame_bytes.hex(" ")}
        if session and not no_encrypt:
            await ws.send(session.encrypt_json(msg))
        else:
            await ws.send(json.dumps(msg))

    caps_finalized = False
    last_cap_scan = 0.0
    scan_count = 0
    # Track which frame IDs the AC won't respond to on this bus.
    # Populated after scan_count >= 2 by _detect_dead_frames().
    dead_frames: set[str] = set()

    while True:
        scan_count += 1
        need_caps = not caps_finalized or (time.monotonic() - last_cap_scan > CAP_RESCAN_INTERVAL)

        # ── Build this cycle's scan queue (pure function) ────────
        scan_queue = build_scan_queue(
            status=status,
            glossary=glossary,
            bus=bus,
            caps_finalized=caps_finalized,
            need_caps=need_caps,
            dead_frames=dead_frames,
            proto=proto,
        )

        # ── Send scan queue ──────────────────────────────────────
        labels = [label for label, _ in scan_queue]
        ts = time.strftime("%H:%M:%S")
        print(f"--- Scan #{scan_count} @ {ts} [{bus}]: {', '.join(labels)} ---")

        for _label, frame in scan_queue:
            await send_frame(frame)
            await asyncio.sleep(0.3)  # inter-frame spacing

        # ── Wait for responses to arrive in receiver ─────────────
        await asyncio.sleep(2.0)

        # ── Finalize caps if B5 arrived ──────────────────────────
        if need_caps and status["meta"].get("b5_received"):
            finalize_capabilities(status, glossary)
            # Apply device quirks after every cap rescan so the overrides
            # stick across periodic re-scans (CAP_RESCAN_INTERVAL).
            _apply_and_print_quirks(status, glossary, quirks_paths)
            last_cap_scan = time.monotonic()

            if not caps_finalized:
                caps_finalized = True
                fa = Counter(f["feature_available"] for f in status["fields"].values())
                caps_total = len(status.get("capabilities_raw", []))
                print(
                    f"  Capabilities resolved ({caps_total} caps): "
                    f"always={fa.get('always', 0)}, never={fa.get('never', 0)}"
                )

                # Rebuild and show query table for the operator summary.
                query_table = build_query_table(status, glossary)
                triggered_rsps: set[str] = set()
                for spec in (glossary.get("frames") or {}).values():
                    frame_bus = spec.get("bus") or ["uart", "rt"]
                    if bus in frame_bus:
                        triggered_rsps.update(spec.get("triggers") or [])
                queryable = [k for k in query_table if k in triggered_rsps]
                unsolicited = [k for k in query_table if k not in queryable]
                print(f"  Query table: {len(queryable)} queryable, {len(unsolicited)} unsolicited")
                for pkey in sorted(queryable):
                    print(f"    {pkey:20s} -> {len(query_table[pkey])} fields")
                for pkey in sorted(unsolicited):
                    print(f"    {pkey:20s} -> {len(query_table[pkey])} fields (unsolicited)")

            save_status(status, db_path)

        # ── Detect dead group frames (sent but no response) ──────
        if caps_finalized and scan_count >= 2:
            frame_counts = status["meta"].get("frame_counts", {})
            newly_dead = detect_dead_frames(glossary, frame_counts, bus) - dead_frames
            for fid in newly_dead:
                log.info("dropping %s from scan (AC does not respond on %s bus)", fid, bus)
            dead_frames |= newly_dead

        # ── Wait until next scan ─────────────────────────────────
        await asyncio.sleep(interval)


# ── Quirks helper ─────────────────────────────────────────────────────────


def _apply_and_print_quirks(status, glossary, quirks_paths):
    """Load and apply each quirks file, printing a one-line summary per file."""
    if not quirks_paths:
        return
    reports = apply_quirks_files(status, quirks_paths, glossary)
    for r in reports:
        src = r.get("source", "<unknown>")
        print(f"Applied quirks: {Path(src).name} ({r['name']})")
        if r["fields_overridden"]:
            print(f"  feature_available overrides: {', '.join(r['fields_overridden'])}")
        if r["caps_synthesized"]:
            print(f"  synthesized capabilities: {', '.join(r['caps_synthesized'])}")
        if r["caps_skipped"]:
            print(f"  skipped (real cap wins): {', '.join(r['caps_skipped'])}")
    print()


# ── One-shot set command ──────────────────────────────────────────────────


def _parse_changes(items: list[str]) -> dict:
    """Parse `field=value` strings from --set into a typed changes dict."""
    changes: dict = {}
    for item in items:
        key, _, val = item.partition("=")
        if not key or not val:
            raise ValueError(f"--set entry must be field=value, got {item!r}")
        if val.lower() in ("true", "false"):
            changes[key.strip()] = val.lower() == "true"
        elif "." in val:
            try:
                changes[key.strip()] = float(val)
                continue
            except ValueError:
                pass
            changes[key.strip()] = val
        else:
            try:
                changes[key.strip()] = int(val)
            except ValueError:
                changes[key.strip()] = val
    return changes


async def _connect(args):
    """Open ws connection + (optional) handshake. Returns (ws, session)."""
    uri = f"ws://{args.host}:{args.port}"
    print(f"Connecting to {uri}...")
    ws = await asyncio.wait_for(websockets.connect(uri), timeout=5.0)

    session = None
    if not args.no_encrypt:
        psk = psk_to_bytes(args.psk)
        hello_msg, client_rand = create_hello()
        await ws.send(json.dumps(hello_msg))
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        session = complete_handshake_client(psk, client_rand, reply)
        print("Session established (AES-256-GCM)")
    else:
        print("Connected (no encryption)")
    return ws, session


async def _send_frame(ws, session, frame_bytes: bytes, no_encrypt: bool) -> None:
    msg = {"type": "frame", "hex": frame_bytes.hex(" ")}
    if session and not no_encrypt:
        await ws.send(session.encrypt_json(msg))
    else:
        await ws.send(json.dumps(msg))


async def _drain_for(ws, session, status, glossary, no_encrypt, seconds: float) -> int:
    """Read incoming frames for `seconds`, processing each into status.

    Returns the number of frames processed. Used to populate `last_updated`
    on every readable field before running the preflight check.
    """
    deadline = time.monotonic() + seconds
    n = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except TimeoutError:
            break
        try:
            msg = session.decrypt_json(raw) if session and not no_encrypt else json.loads(raw)
        except Exception:
            continue
        if msg.get("type") != "frame":
            continue
        try:
            parsed = parse_frame(bytes.fromhex(msg.get("hex", "").replace(" ", "")))
            body = parsed["body"]
        except Exception:
            continue
        rsp_key = identify_body(body)
        if not rsp_key:
            continue
        if rsp_key == "rsp_0xb5":
            process_b5(status, body, glossary)
        else:
            process_data_frame(status, body, rsp_key, glossary)
        n += 1
    return n


async def run_set_oneshot(args):
    """One-shot set command.

    1. Connect + handshake
    2. Query B5 (capabilities) and C0 (current state)
    3. Drain responses to populate every field's last_updated
    4. Build the cmd_0x40 / cmd_0xb0 body via build_command_body
       (set_command_preflight runs automatically)
    5. On preflight pass: send the set frame, wait for the C0 echo, exit 0
    6. On preflight fail: print errors, exit 2
    """
    glossary = load_glossary()
    status = build_status(device=args.host, glossary=glossary)

    try:
        changes = _parse_changes(args.set)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"=== HVAC Set Command ({len(changes)} change(s)) ===")
    for k, v in changes.items():
        print(f"  {k} = {v}")
    print()

    ws, session = await _connect(args)
    try:
        # 1. Cap query (B5 extended + simple)
        for fid in ("cmd_0xb5_extended", "cmd_0xb5_simple"):
            frame = build_frame_from_spec(fid, glossary, proto=args.proto)
            await _send_frame(ws, session, frame, args.no_encrypt)
            await asyncio.sleep(0.2)
        print("Draining B5 responses (2s)...")
        await _drain_for(ws, session, status, glossary, args.no_encrypt, 2.0)
        finalize_capabilities(status, glossary)
        cap_count = len(status.get("capabilities_raw", []))
        print(f"  Resolved {cap_count} capabilities")

        # 1b. Apply device quirks (after finalize_capabilities so they
        # override cap-derived state)
        _apply_and_print_quirks(status, glossary, args.quirks)

        # 2. Status query (C0) — populates a `sources` slot for every readable field
        c0_query = build_frame_from_spec("cmd_0x41", glossary, proto=args.proto)
        await _send_frame(ws, session, c0_query, args.no_encrypt)
        print("Draining C0 response (2s)...")
        await _drain_for(ws, session, status, glossary, args.no_encrypt, 2.0)

        populated = sum(1 for f in status["fields"].values() if f.get("sources"))
        print(f"  Populated sources on {populated} fields")
        print()

        # 3. Build the command (preflight runs inside build_command_body)
        # Decide between cmd_0x40 and cmd_0xb0 based on which protocol the
        # changed fields belong to. For mixed sets, build both.
        from blaueis.core.codec import walk_fields as _walk

        all_fields = _walk(glossary)
        cmd_0x40_changes: dict = {}
        cmd_0xb0_changes: dict = {}
        for name, value in changes.items():
            fdef = all_fields.get(name)
            if fdef is None:
                print(f"ERROR: unknown field {name!r}", file=sys.stderr)
                return 2
            protos = fdef.get("protocols") or {}
            if "cmd_0x40" in protos:
                cmd_0x40_changes[name] = value
            elif "cmd_0xb0" in protos:
                cmd_0xb0_changes[name] = value
            else:
                print(f"ERROR: field {name!r} has no settable protocol", file=sys.stderr)
                return 2

        async def _build_send(builder, changes_subset, tag):
            if not changes_subset:
                return True
            print(f"Building {tag} ({len(changes_subset)} field(s))...")
            result = builder(
                status,
                changes_subset,
                glossary,
                preflight_threshold_seconds=args.preflight_seconds,
                skip_preflight=args.skip_preflight,
            )
            if result["body"] is None:
                print(f"ERROR: {tag} preflight check failed:", file=sys.stderr)
                for err in result["preflight"]:
                    age = f"age={err['age_seconds']:.1f}s" if err["age_seconds"] is not None else "never read"
                    print(
                        f"  [{err['reason']}] {err['field']} ({err['position']}, "
                        f"sibling of {err['shared_with']}, {age})",
                        file=sys.stderr,
                    )
                print("Use --skip-preflight to override.", file=sys.stderr)
                return False
            if result["preflight"]:
                print(f"WARNING: {len(result['preflight'])} preflight warning(s) (preflight skipped)")
                for err in result["preflight"]:
                    print(f"  {err['field']}: {err['reason']}")
            # Wrap body in UART frame envelope
            msg_type = 0x02
            frame = build_frame(
                body=bytes(result["body"]),
                msg_type=msg_type,
                appliance=0xAC,
                proto=args.proto,
            )
            print(f"  TX: {frame.hex(' ')}")
            await _send_frame(ws, session, frame, args.no_encrypt)
            return True

        from blaueis.core.command import build_b0_command_body

        if not await _build_send(build_command_body, cmd_0x40_changes, "cmd_0x40"):
            return 2
        if not await _build_send(build_b0_command_body, cmd_0xb0_changes, "cmd_0xb0"):
            return 2

        # 4. Wait for the C0 echo + verify
        print("\nWaiting for C0 echo (2s)...")
        await _drain_for(ws, session, status, glossary, args.no_encrypt, 2.0)

        print("\nVerification:")
        ok = True
        for name, expected in changes.items():
            r = read_field(status, name)
            actual = r["value"] if r else None
            mark = "OK" if actual == expected else "MISMATCH"
            if actual != expected:
                ok = False
            print(f"  {mark:8s} {name}: expected={expected}, got={actual}")
        return 0 if ok else 1
    finally:
        await ws.close()


# ── Main ──────────────────────────────────────────────────────────────────


async def run_monitor(args):
    glossary = load_glossary()
    status = build_status(device=args.host, glossary=glossary)

    session_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    db_path = Path(__file__).resolve().parent / f"{session_ts}_out.json"
    save_status(status, db_path)

    print("=== HVAC Live Monitor ===")
    print(f"Glossary: {len(status['fields'])} fields")
    print(f"Database: {db_path.name}")
    print(f"Scan interval: {args.interval}s, cap rescan: {CAP_RESCAN_INTERVAL}s")
    print()

    uri = f"ws://{args.host}:{args.port}"
    print(f"Connecting to {uri}...")
    ws = await asyncio.wait_for(websockets.connect(uri), timeout=5.0)

    session = None
    if not args.no_encrypt:
        psk = psk_to_bytes(args.psk)
        hello_msg, client_rand = create_hello()
        await ws.send(json.dumps(hello_msg))
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        session = complete_handshake_client(psk, client_rand, reply)
        print("Session established (AES-256-GCM)")
    else:
        print("Connected (no encryption)")
    print()

    prev_values = {}

    try:
        await asyncio.gather(
            receiver_task(ws, session, status, glossary, db_path, prev_values, args.no_encrypt),
            sender_task(
                ws,
                session,
                status,
                glossary,
                db_path,
                args.interval,
                args.no_encrypt,
                args.proto,
                args.bus,
                quirks_paths=args.quirks,
            ),
        )
    except KeyboardInterrupt:
        print("\nStopped.")
    except websockets.exceptions.ConnectionClosed:
        print("\nConnection closed.")
    finally:
        await ws.close()
        save_status(status, db_path)
        print(f"Saved: {db_path.name}")


def main():
    parser = argparse.ArgumentParser(description="HVAC Live Monitor")
    parser.add_argument("--host", required=True, help="Gateway IP")
    parser.add_argument("--port", type=int, default=8765, help="Gateway port")
    parser.add_argument("--psk", help="Pre-shared key (hex)")
    parser.add_argument("--no-encrypt", action="store_true", help="Disable encryption")
    parser.add_argument("--interval", type=int, default=15, help="Scan interval seconds")
    parser.add_argument("--proto", type=int, default=0x02, help="UART protocol version")
    parser.add_argument(
        "--bus",
        choices=["uart", "rt"],
        default="uart",
        help="Physical bus the dongle is wired to. Controls which frames the "
        "planner can send: groups 4/5 power queries are UART-only, "
        "groups 1/3 telemetry are R/T-only. See glossary TODO §8.",
    )
    parser.add_argument("--debug", action="store_true", help="Debug logging")
    parser.add_argument(
        "--set",
        nargs="+",
        metavar="FIELD=VALUE",
        help="One-shot mode: set field(s) instead of running the monitor loop. "
        "Performs B5 + C0 query first, then runs set_command_preflight, then sends the set frame.",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the set-command preflight check (one-shot --set mode only)",
    )
    parser.add_argument(
        "--preflight-seconds",
        type=float,
        default=30.0,
        help="Preflight staleness threshold in seconds (one-shot --set mode only, default 30)",
    )
    parser.add_argument(
        "--quirks",
        action="append",
        default=[],
        metavar="PATH",
        help="Apply a device quirks YAML file after capability finalisation. "
        "Repeatable to layer multiple files. Used by both monitor and one-shot --set modes.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.set:
        sys.exit(asyncio.run(run_set_oneshot(args)))
    asyncio.run(run_monitor(args))


if __name__ == "__main__":
    main()
