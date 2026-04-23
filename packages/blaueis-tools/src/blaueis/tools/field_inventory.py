#!/usr/bin/env python3
"""Field-inventory CLI — "what's actually populated on this AC right now?"

Wraps :mod:`blaueis.core.inventory` with a standalone WebSocket session
to the gateway. Sends the superset of known read queries, feeds the
responses through a cap-agnostic :class:`ShadowDecoder`, and writes a
markdown report + JSON sidecar to the caller's cwd.

Intended audience: professionals + AI who want to understand a device's
populated-field landscape without standing up the full HA integration.
Home Assistant users should prefer the ``blaueis_midea.run_field_inventory``
service or the AC device's "Run field inventory scan" button — same
output, no need to wrangle PSKs by hand.

Usage::

    python -m blaueis.tools.field_inventory \\
        --host 192.168.210.30 --psk <hex> \\
        --label "cooling-20C-from-22"

See the blaueis-ha-midea devices knowledge base for interpretation
guidance and worked examples.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import websockets

# Package-relative imports work because the tools package is installed
# as editable during `pip install -e .`.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "blaueis-core" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "blaueis-client" / "src"))

from blaueis.core.codec import (  # noqa: E402
    build_frame_from_spec,  # noqa: E402
    identify_frame,
    load_glossary,
    walk_fields,  # noqa: E402
)
from blaueis.core.crypto import complete_handshake_client, create_hello, psk_to_bytes  # noqa: E402
from blaueis.core.frame import parse_frame  # noqa: E402
from blaueis.core.inventory import (  # noqa: E402
    ShadowDecoder,
    generate_compare_report,
    generate_json_sidecar,
    generate_markdown_report,
    synthesize_override_snippet,
)

# Reuse the query list + helpers from ac_probe (same tool family,
# different output).
from blaueis.tools.ac_probe import (  # noqa: E402
    B1_PROPERTY_IDS,
    build_b1_property_query,
    build_device_id_query,
    build_direct_subpage_query,
    build_group_query_raw,
)

log = logging.getLogger("field_inventory")


# ══════════════════════════════════════════════════════════════════════════
#   Query-list builder (shared shape with ac_probe)
# ══════════════════════════════════════════════════════════════════════════


def _build_query_list(glossary: dict, proto: int) -> list[tuple[str, bytes]]:
    """Produce the (label, frame_bytes) list that the scan sends. Mirrors
    ``ac_probe.probe()``'s query list — we want the same coverage so
    directly-observed fields show up the same way."""
    queries: list[tuple[str, bytes]] = []

    # Glossary-defined frames
    for fid in [
        "cmd_0xb5_extended",
        "cmd_0xb5_simple",
        "cmd_0x41",
        "cmd_0x41_group4_power",
        "cmd_0x41_group5",
        "cmd_0x41_ext",
    ]:
        spec = glossary.get("frames", {}).get(fid)
        if not spec:
            continue
        bus = spec.get("bus", ["uart", "rt"])
        if "uart" not in bus:
            continue
        try:
            frame = build_frame_from_spec(fid, glossary, proto=proto)
            queries.append((fid, frame))
        except Exception as e:
            log.debug("skip glossary frame %s: %s", fid, e)

    for sp in [0x01, 0x02]:
        queries.append((f"direct_subpage_0x{sp:02X}", build_direct_subpage_query(sp, proto=proto)))

    BATCH = 8
    for i in range(0, len(B1_PROPERTY_IDS), BATCH):
        batch = B1_PROPERTY_IDS[i : i + BATCH]
        ids = [(lo, hi) for lo, hi, _ in batch]
        labels = [lbl for _, _, lbl in batch]
        queries.append((f"B1_props_{'+'.join(labels)}", build_b1_property_query(ids, proto=proto)))

    queries.append(("device_id_0x07", build_device_id_query(proto=proto)))

    for page in [0x40, 0x42, 0x43, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F]:
        queries.append((f"group_0x{page:02X}_v21", build_group_query_raw(page, variant=0x21, proto=proto)))

    for page in [0x41, 0x43]:
        queries.append(
            (
                f"group_0x{page:02X}_v21_rt_test",
                build_group_query_raw(page, variant=0x21, proto=proto),
            )
        )

    return queries


# ══════════════════════════════════════════════════════════════════════════
#   Scan orchestrator
# ══════════════════════════════════════════════════════════════════════════


async def run_scan(args) -> int:
    """Main orchestration. Returns a POSIX exit code."""
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    stem = f"{ts}_{_slug(args.label)}_inventory"
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"

    glossary = load_glossary()
    queries = _build_query_list(glossary, args.proto)

    print(f"=== Field inventory — {args.label} ===")
    print(f"  host:    {args.host}:{args.port}")
    print(f"  queries: {len(queries)}")
    print(f"  output:  {md_path.name}")
    print()

    # ── Connect ─────────────────────────────────────────────────────────
    uri = f"ws://{args.host}:{args.port}"
    print(f"Connecting to {uri}…")
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

    # ── Drain unsolicited frames ────────────────────────────────────────
    async def recv_frame(timeout_s: float):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
            if session and not args.no_encrypt:
                return session.decrypt_json(raw)
            return json.loads(raw)
        except (TimeoutError, Exception):
            return None

    async def drain(timeout_s: float) -> list[dict]:
        frames = []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            m = await recv_frame(deadline - time.monotonic())
            if m is None:
                break
            if m.get("type") == "frame":
                frames.append(m)
        return frames

    print("Draining unsolicited frames (2s)…")
    await drain(2.0)

    # ── Connect shadow decoder + scan ───────────────────────────────────
    shadow = ShadowDecoder(glossary)
    cap_records: list[dict] = []

    def feed_to_shadow(msg: dict) -> None:
        if msg.get("type") != "frame":
            return
        hex_str = msg.get("hex", "").replace(" ", "")
        try:
            raw = bytes.fromhex(hex_str)
            body = parse_frame(raw)["body"]
            protocol_key = identify_frame(body)
        except Exception as e:
            log.debug("feed: parse failed for hex=%s: %s", hex_str[:40], e)
            return
        shadow.observe(protocol_key, body)
        # Track B5 cap records locally for the synthesizer.
        if protocol_key == "rsp_0xb5":
            try:
                from blaueis.core.codec import parse_b5_tlv

                parsed = parse_b5_tlv(body)
                for rec in parsed.get("records", []):
                    cap_records.append(rec)
            except Exception as e:
                log.debug("feed: B5 parse failed: %s", e)

    async def send_frame(frame_bytes: bytes) -> None:
        msg = {"type": "frame", "hex": frame_bytes.hex(" ")}
        if session and not args.no_encrypt:
            await ws.send(session.encrypt_json(msg))
        else:
            await ws.send(json.dumps(msg))

    for idx, (label, frame_bytes) in enumerate(queries, 1):
        print(f"[{idx:3}/{len(queries)}] {label}")
        await send_frame(frame_bytes)
        frames = await drain(args.wait)
        for f in frames:
            feed_to_shadow(f)
    print()

    # Allow a last-moment drain of trailing frames.
    print("Draining trailing frames (2s)…")
    trailing = await drain(2.0)
    for f in trailing:
        feed_to_shadow(f)

    await ws.close()

    # ── Build outputs ───────────────────────────────────────────────────
    result = shadow.snapshot(cap_records=cap_records)

    suggested = []
    if not args.no_suggest_overrides:
        walk = walk_fields(glossary)
        for fname, state in result.states.items():
            if state.classification != "populated":
                continue
            field_def = walk.get(fname)
            if field_def is None or state.frame is None or state.body is None:
                continue
            snip = synthesize_override_snippet(
                fname,
                field_def,
                state.frame,
                state.body,
                glossary,
                cap_records,
                current_value=state.value,
            )
            if snip is not None:
                suggested.append(snip)

    md = generate_markdown_report(
        result,
        glossary,
        label=args.label,
        host=args.host,
        suggested_overrides=suggested,
    )
    js = generate_json_sidecar(
        result,
        glossary,
        label=args.label,
        host=args.host,
        suggested_overrides=suggested,
    )

    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(js, indent=2, default=str), encoding="utf-8")

    populated = sum(1 for s in result.states.values() if s.classification == "populated")
    print(f"✓ populated: {populated}")
    print(f"✓ overrides suggested: {len(suggested)}")
    print(f"✓ markdown: {md_path}")
    print(f"✓ json:     {json_path}")

    # ── Compare mode ────────────────────────────────────────────────────
    if args.compare:
        compare_path = Path(args.compare).resolve()
        if not compare_path.exists():
            print(f"⚠ --compare path not found: {compare_path}", file=sys.stderr)
            return 2
        try:
            prev = json.loads(compare_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠ --compare JSON parse failed: {e}", file=sys.stderr)
            return 2
        cmp_md = generate_compare_report(prev, js)
        cmp_path = out_dir / f"{stem}_compare.md"
        cmp_path.write_text(cmp_md, encoding="utf-8")
        print(f"✓ compare:  {cmp_path}")

    return 0


def _slug(text: str) -> str:
    """Filename-safe slug from a free-form label."""
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in text).strip("-") or "unlabelled"


# ══════════════════════════════════════════════════════════════════════════
#   argparse entry
# ══════════════════════════════════════════════════════════════════════════


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scan every known query frame on a Blaueis gateway and "
        "report which fields are actually populated on the attached AC."
    )
    p.add_argument("--host", required=True, help="Gateway IP or hostname")
    p.add_argument("--port", type=int, default=8765, help="Gateway port")
    p.add_argument("--psk", help="Pre-shared key (hex); required unless --no-encrypt")
    p.add_argument("--no-encrypt", action="store_true", help="Disable encryption")
    p.add_argument("--proto", type=int, default=0x02, help="UART protocol version")
    p.add_argument(
        "--wait",
        type=float,
        default=1.5,
        help="Seconds to wait for responses per query (default 1.5)",
    )
    p.add_argument(
        "--label",
        required=True,
        help="Free-form state tag, e.g. 'cooling', 'off', 'idle'. Used in report header and filename.",
    )
    p.add_argument(
        "--compare",
        help="Path to a previous run's JSON sidecar; produces a third markdown file diffing the two runs.",
    )
    p.add_argument(
        "--output-dir",
        default=".",
        help="Where to write the markdown + JSON output (default: cwd)",
    )
    p.add_argument(
        "--no-suggest-overrides",
        action="store_true",
        help="Skip the 'Suggested overrides' section in the markdown report.",
    )
    p.add_argument("--debug", action="store_true", help="Debug logging")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    if not args.no_encrypt and not args.psk:
        print("error: --psk required unless --no-encrypt", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run_scan(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
