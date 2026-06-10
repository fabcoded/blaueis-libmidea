#!/usr/bin/env python3
"""Extended HVAC probe — fire ALL known queries and log raw bytestreams.

Unlike ac_monitor.py (continuous scan loop), this script:
  1. Connects to the gateway
  2. Sends every known query frame one by one (with response wait)
  3. Also sends exploratory queries (B1 property probes, unknown group pages)
  4. Logs every sent query + received response as raw hex at the end of the JSON
  5. Exits when done

Queries sent (in order):
  - B5 extended + simple (capabilities)
  - C0 status query
  - C1 Group 4 power (body[1]=0x21, body[3]=0x44)
  - C1 Group 5 extended energy (body[1]=0x21, body[3]=0x45)
  - C1 extended state (optCommand=0x03, queryStat=0x02)
  - C1 direct sub-page 0x01 (14-byte short frame)
  - C1 direct sub-page 0x02 (14-byte short frame)
  - B1 property query (batch of known property IDs)
  - msg_type 0x07 device ID query
  - Exploratory: C1 group pages 0x42, 0x46..0x4F
  - Exploratory: optCommand 0x00, 0x02, 0x04, 0x05, 0x06

Usage:
    python ac_probe.py --host 192.168.210.30 --psk <hex>
    python ac_probe.py --host 192.168.210.30 --no-encrypt
"""

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent

import websockets  # noqa: E402
from blaueis.core.codec import build_frame_from_spec, load_glossary  # noqa: E402
from blaueis.core.crypto import complete_handshake_client, create_hello, psk_to_bytes  # noqa: E402
from blaueis.core.frame import build_frame, parse_frame  # noqa: E402

log = logging.getLogger("ac_probe")

# ── Scan-query builders — canonical home is blaueis.core.scan_queries ──
from blaueis.core.scan_queries import (  # noqa: E402,F401  (re-exported)
    B1_PROPERTY_IDS,
    build_b1_property_query,
    build_device_id_query,
    build_direct_subpage_query,
    build_group_query_raw,
    build_optcommand_query,
)


# ── Probe logic ─────────────────────────────────────────────────────────


async def probe(args):
    glossary = load_glossary()

    session_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    db_path = Path.cwd() / f"{session_ts}_probe.json"

    # Build the ordered list of (label, frame_bytes) probes.
    probes: list[tuple[str, bytes]] = []

    # 1. Glossary-defined frames (UART-capable)
    for fid in [
        "cmd_0xb5_extended",
        "cmd_0xb5_simple",
        "cmd_0x41",  # C0 status
        "cmd_0x41_group4_power",  # Group 4 power (BCD)
        "cmd_0x41_group5",  # Group 5 extended energy
        "cmd_0x41_ext",  # Extended state (optCmd=0x03, queryStat=0x02)
    ]:
        spec = glossary.get("frames", {}).get(fid)
        if not spec:
            continue
        bus = spec.get("bus", ["uart", "rt"])
        if "uart" not in bus:
            continue
        frame = build_frame_from_spec(fid, glossary, proto=args.proto)
        probes.append((fid, frame))

    # 2. Direct C1 sub-page queries (14-byte, hypothesis)
    for sp in [0x01, 0x02]:
        probes.append((f"direct_subpage_0x{sp:02X}", build_direct_subpage_query(sp, proto=args.proto)))

    # 3. B1 property query — batch all known IDs
    # Split into small batches to stay within frame size limits
    BATCH = 8
    for i in range(0, len(B1_PROPERTY_IDS), BATCH):
        batch = B1_PROPERTY_IDS[i : i + BATCH]
        ids = [(lo, hi) for lo, hi, _ in batch]
        labels = [lbl for _, _, lbl in batch]
        probes.append((f"B1_props_{'+'.join(labels)}", build_b1_property_query(ids, proto=args.proto)))

    # 4. Device ID query (msg_type=0x07)
    probes.append(("device_id_0x07", build_device_id_query(proto=args.proto)))

    # 5. All group pages with 0x21 variant (confirmed working on UART)
    # 0x40=timers, 0x41=compressor(RT?), 0x42=indoor, 0x43=outdoor(RT?),
    # 0x44/0x45=already above, 0x46=diagnostics, 0x47=unknown, 0x48-0x4F=explore,
    # 0x4B=vane control (has JS decoder)
    for page in [0x40, 0x42, 0x43, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F]:
        probes.append((f"group_0x{page:02X}_v21", build_group_query_raw(page, variant=0x21, proto=args.proto)))

    # 7. Group 1 and 3 with v21 (normally R/T-only, test if UART responds with data when AC is running)
    for page in [0x41, 0x43]:
        probes.append((f"group_0x{page:02X}_v21_rt_test", build_group_query_raw(page, variant=0x21, proto=args.proto)))

    print(f"=== HVAC Probe — {len(probes)} queries ===")
    print(f"Output: {db_path.name}")
    print()

    # ── Connect ──────────────────────────────────────────────────────────
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

    async def send_frame(frame_bytes):
        msg = {"type": "frame", "hex": frame_bytes.hex(" ")}
        if session and not args.no_encrypt:
            await ws.send(session.encrypt_json(msg))
        else:
            await ws.send(json.dumps(msg))

    async def recv_frames(timeout_s: float) -> list[dict]:
        """Collect all frames arriving within timeout_s seconds."""
        frames = []
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = session.decrypt_json(raw) if session and not args.no_encrypt else json.loads(raw)
                if msg.get("type") == "frame":
                    hex_str = msg.get("hex", "")
                    frames.append(
                        {
                            "hex": hex_str,
                            "ts": time.strftime("%H:%M:%S"),
                        }
                    )
            except TimeoutError:
                break
            except Exception:
                break
        return frames

    # ── Drain unsolicited frames before probing ──────────────────────────
    print("Draining unsolicited frames (2s)...")
    unsolicited = await recv_frames(2.0)
    print(f"  {len(unsolicited)} unsolicited frames drained")
    print()

    # ── Run probes ───────────────────────────────────────────────────────
    transcript: list[dict] = []

    for idx, (label, frame_bytes) in enumerate(probes, 1):
        ts_send = time.strftime("%H:%M:%S")
        print(f"[{idx:3d}/{len(probes)}] {label}")
        print(f"  TX: {frame_bytes.hex(' ')}")

        await send_frame(frame_bytes)
        responses = await recv_frames(args.wait)

        entry = {
            "index": idx,
            "label": label,
            "tx_hex": frame_bytes.hex(" "),
            "tx_time": ts_send,
            "responses": [],
        }

        # Parse and identify each response
        for rsp in responses:
            hex_str = rsp["hex"]
            rsp_entry = {
                "rx_hex": hex_str,
                "rx_time": rsp["ts"],
            }
            try:
                parsed = parse_frame(bytes.fromhex(hex_str.replace(" ", "")))
                body = parsed["body"]
                rsp_entry["msg_type"] = f"0x{parsed['msg_type']:02X}"
                rsp_entry["body_hex"] = body.hex(" ")
                rsp_entry["body_len"] = len(body)
                if body:
                    rsp_entry["body_tag"] = f"0x{body[0]:02X}"
                    if len(body) > 2:
                        rsp_entry["body_2"] = f"0x{body[2]:02X}"
                    if len(body) > 3:
                        rsp_entry["body_3"] = f"0x{body[3]:02X}"
            except Exception as e:
                rsp_entry["parse_error"] = str(e)

            entry["responses"].append(rsp_entry)

        n_rsp = len(entry["responses"])
        if n_rsp == 0:
            print("  RX: (no response)")
        else:
            for r in entry["responses"]:
                tag = r.get("body_tag", "?")
                blen = r.get("body_len", "?")
                print(f"  RX: tag={tag} len={blen}  {r.get('body_hex', r.get('rx_hex', ''))[:80]}")

        transcript.append(entry)
        print()

        # Small inter-query delay
        await asyncio.sleep(0.15)

    # ── Collect trailing unsolicited frames ───────────────────────────────
    print("Collecting trailing unsolicited frames (3s)...")
    trailing = await recv_frames(3.0)
    if trailing:
        trailing_entry = {
            "index": "trailing",
            "label": "unsolicited_trailing",
            "tx_hex": None,
            "responses": [],
        }
        for rsp in trailing:
            hex_str = rsp["hex"]
            rsp_entry = {"rx_hex": hex_str, "rx_time": rsp["ts"]}
            try:
                parsed = parse_frame(bytes.fromhex(hex_str.replace(" ", "")))
                body = parsed["body"]
                rsp_entry["msg_type"] = f"0x{parsed['msg_type']:02X}"
                rsp_entry["body_hex"] = body.hex(" ")
                rsp_entry["body_len"] = len(body)
                if body:
                    rsp_entry["body_tag"] = f"0x{body[0]:02X}"
            except Exception as e:
                rsp_entry["parse_error"] = str(e)
            trailing_entry["responses"].append(rsp_entry)
            print(f"  RX unsolicited: tag={rsp_entry.get('body_tag', '?')} {rsp_entry.get('body_hex', '')[:80]}")
        transcript.append(trailing_entry)

    if unsolicited:
        unsolicited_entry = {
            "index": "pre_drain",
            "label": "unsolicited_pre_drain",
            "tx_hex": None,
            "responses": [],
        }
        for rsp in unsolicited:
            hex_str = rsp["hex"]
            rsp_entry = {"rx_hex": hex_str, "rx_time": rsp["ts"]}
            try:
                parsed = parse_frame(bytes.fromhex(hex_str.replace(" ", "")))
                body = parsed["body"]
                rsp_entry["msg_type"] = f"0x{parsed['msg_type']:02X}"
                rsp_entry["body_hex"] = body.hex(" ")
                rsp_entry["body_len"] = len(body)
                if body:
                    rsp_entry["body_tag"] = f"0x{body[0]:02X}"
            except Exception as e:
                rsp_entry["parse_error"] = str(e)
            unsolicited_entry["responses"].append(rsp_entry)
        transcript.append(unsolicited_entry)

    # ── Save ─────────────────────────────────────────────────────────────
    await ws.close()

    result = {
        "meta": {
            "host": args.host,
            "port": args.port,
            "timestamp": session_ts,
            "total_probes": len(probes),
            "wait_per_probe_s": args.wait,
        },
        "transcript": transcript,
    }

    # Summary
    responded = sum(1 for t in transcript if t.get("responses") and t["tx_hex"] is not None)
    silent = sum(1 for t in transcript if not t.get("responses") and t["tx_hex"] is not None)
    print()
    print(f"=== Done: {responded} responded, {silent} silent, {len(probes)} total ===")

    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Saved: {db_path}")


def main():
    parser = argparse.ArgumentParser(description="HVAC Extended Probe")
    parser.add_argument("--host", required=True, help="Gateway IP")
    parser.add_argument("--port", type=int, default=8765, help="Gateway port")
    parser.add_argument("--psk", help="Pre-shared key (hex)")
    parser.add_argument("--no-encrypt", action="store_true", help="Disable encryption")
    parser.add_argument("--proto", type=int, default=0x02, help="UART protocol version")
    parser.add_argument("--wait", type=float, default=1.5, help="Seconds to wait for response per query (default 1.5)")
    parser.add_argument("--debug", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(probe(args))


if __name__ == "__main__":
    main()
