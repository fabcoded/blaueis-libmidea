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
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent

import websockets  # noqa: E402
from blaueis.core.crypto import complete_handshake_client, create_hello  # noqa: E402
from blaueis.core.codec import build_frame_from_spec, load_glossary  # noqa: E402
from blaueis.core.frame import build_frame, parse_frame  # noqa: E402

log = logging.getLogger("ac_probe")

# ── Frame builders for queries not in the glossary ──────────────────────


def build_direct_subpage_query(subpage: int, appliance: int = 0xAC, proto: int = 0) -> bytes:
    """14-byte direct C1 sub-page query (§3.1.4.4).

    body[0]=0x41, body[1]=subpage (0x01 or 0x02). Hypothesis.
    """
    body = bytes([0x41, subpage & 0xFF])
    return build_frame(body=body, msg_type=0x03, appliance=appliance, proto=proto)


def build_optcommand_query(opt_cmd: int, query_stat: int = 0x00, appliance: int = 0xAC, proto: int = 0) -> bytes:
    """24-byte optCommand query (§3.1.4.3).

    body[0]=0x41, body[1]=0x21, body[4]=optCommand, body[5]=0xFF,
    body[7]=queryStat.
    """
    body = bytearray(24)
    body[0] = 0x41
    body[1] = 0x21
    body[4] = opt_cmd & 0xFF
    body[5] = 0xFF
    body[7] = query_stat & 0xFF
    return build_frame(body=bytes(body), msg_type=0x03, appliance=appliance, proto=proto)


def build_group_query_raw(page: int, variant: int = 0x81, appliance: int = 0xAC, proto: int = 0) -> bytes:
    """Generic group page query — allows arbitrary page + variant byte."""
    body = bytearray(24)
    body[0] = 0x41
    body[1] = variant
    body[2] = 0x01
    body[3] = page & 0xFF
    return build_frame(body=bytes(body), msg_type=0x03, appliance=appliance, proto=proto)


def build_device_id_query(appliance: int = 0xAC, proto: int = 0) -> bytes:
    """msg_type=0x07 device ID / SN query (§5.6)."""
    return build_frame(body=bytes([0x00]), msg_type=0x07, appliance=appliance, proto=proto)


def build_b1_property_query(prop_ids: list[tuple[int, int]], appliance: int = 0xAC, proto: int = 0) -> bytes:
    """B1 property query — body[0]=0xB1, body[1]=count, then (lo, hi) pairs."""
    body = bytearray()
    body.append(0xB1)
    body.append(len(prop_ids))
    for lo, hi in prop_ids:
        body.append(lo & 0xFF)
        body.append(hi & 0xFF)
    return build_frame(body=bytes(body), msg_type=0x03, appliance=appliance, proto=proto)


# ── Known B1 property IDs to probe ─────────────────────────────────────

# All known B0/B1 property IDs from community protocol research (§3.5),
# organised by tranche so the response analysis is easy to read.
# Format: (lo, hi, label). The probe iterates this list in order and
# bundles BATCH (8) IDs per request frame.
#
# A device that does not implement a property typically replies with
# data_len=0 for that prop_id rather than dropping it. The interesting
# signal in the response is therefore not "any reply" but "data_len > 0
# AND data bytes look plausible".
B1_PROPERTY_IDS = [
    # ── Pre-existing properties (probed in previous sessions) ─────────
    (0x15, 0x00, "indoor_humidity"),
    (0x3F, 0x00, "error_code_query"),
    (0x41, 0x00, "mode_query"),
    (0x1A, 0x00, "tone_buzzer"),
    (0x18, 0x00, "no_wind_sense"),
    (0x32, 0x00, "wind_straight_avoid"),
    (0x39, 0x00, "self_clean"),
    (0x42, 0x00, "prevent_straight_wind"),
    (0x48, 0x00, "rate_select"),
    (0x09, 0x00, "wind_swing_ud_angle"),
    (0x0A, 0x00, "wind_swing_lr_angle"),
    (0x0B, 0x02, "pm25_value"),
    (0x28, 0x02, "operating_time"),
    (0x91, 0x00, "has_icheck"),
    (0x4B, 0x00, "fresh_air"),
    (0xAD, 0x00, "comfort"),
    (0xE3, 0x00, "ieco_switch"),
    (0x47, 0x00, "high_temperature_monitor"),
    # ── Tier 1 bool / uint8 properties (bulk add 2026-04-11) ──────────
    (0x21, 0x00, "cool_hot_sense"),
    (0x26, 0x02, "auto_prevent_straight_wind"),
    (0x34, 0x00, "intelligent_wind"),
    (0x3A, 0x00, "child_prevent_cold_wind"),
    (0x1B, 0x02, "little_angel"),
    (0x29, 0x00, "security"),
    (0x31, 0x00, "intelligent_control"),
    (0x44, 0x00, "face_register"),
    (0x4E, 0x00, "even_wind"),
    (0x4F, 0x00, "single_tuyere"),
    (0x58, 0x00, "prevent_straight_wind_lr"),
    (0x98, 0x00, "cvp"),
    (0xAA, 0x00, "new_wind_sense"),
    (0x01, 0x02, "pre_cool_hot"),
    (0x34, 0x02, "body_check"),
    # ── Tier 2 mito_*_temp (temp_offset50_half) ───────────────────────
    (0x8D, 0x00, "mito_cool_temp"),
    (0x8E, 0x00, "mito_heat_temp"),
    # ── Tier 3 2-byte composites ──────────────────────────────────────
    (0x4C, 0x00, "extreme_wind"),
    (0x59, 0x00, "wind_around"),
    (0x8F, 0x00, "dr_time"),
    (0x27, 0x02, "remote_control_lock"),
    # ── Tier 4 multi-byte numerics ────────────────────────────────────
    (0x49, 0x00, "prevent_super_cool"),
    # ── Tier 5 deferred properties (composite/string-shaped) ──────────
    (0x09, 0x04, "filter_level"),
    (0x20, 0x00, "voice_control"),
    (0x24, 0x00, "volume_control"),
    (0x90, 0x00, "cool_heat_amount"),
    (0xE0, 0x00, "ieco_frame"),
    (0xAB, 0x00, "indoor_unit_code"),
    (0xAC, 0x00, "outdoor_unit_code"),
    (0x51, 0x00, "parent_control"),
    (0x25, 0x02, "temperature_ranges"),
    (0x30, 0x02, "main_horizontal_guide_strip"),
    (0x31, 0x02, "sup_horizontal_guide_strip"),
]


# ── Probe logic ─────────────────────────────────────────────────────────


async def probe(args):
    glossary = load_glossary()

    session_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    db_path = Path(__file__).resolve().parent / f"{session_ts}_probe.json"

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
        psk = bytes.fromhex(args.psk)
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
