#!/usr/bin/env python3
"""Process incoming frames and update the device status file.

Muxes frames by type (B5, C0, C1, A1), decodes using the glossary,
and updates the device_status.json with decoded values.

Usage:
    python process_frame.py device_status.json --hex "B5 08 12 02 ..."
    python process_frame.py device_status.json --frame-file b5_frames.yaml
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import yaml
from blaueis.core.codec import (
    build_cap_index,
    decode_data_cap,
    decode_enum_cap,
    decode_frame_fields,
    identify_frame,
    infer_generation,
    load_glossary,
    parse_b5_tlv,
    walk_fields,
)

log = logging.getLogger("blaueis.process")

# ── B5 capability processing ─────────────────────────────────────────────


def _apply_caps_to_fields(status: dict, records: list[dict], glossary: dict) -> None:
    """For each cap record, decode it and update every field that shares the cap_id.

    Used by both `process_b5` (real B5 frame path) and `apply_device_quirks`
    (synthetic cap path). Pure side-effect on `status`. Records must have
    the parsed B5 TLV shape: {cap_id, cap_type, key_16, data_len, data, data_hex}.
    """
    fields = walk_fields(glossary)
    cap_index = build_cap_index(fields)

    for rec in records:
        cap_id = rec["cap_id"].lower()
        cap_type = "simple" if rec["cap_type"] == 0 else "extended"
        field_names = cap_index.get((cap_id, cap_type), [])

        for field_name in field_names:
            field_def = fields.get(field_name)
            if not field_def:
                continue
            cap_def = field_def.get("capability", {})
            status_field = status["fields"].get(field_name)
            if not status_field:
                continue

            # Glossary-level "never" is sticky: B5 cap processing must
            # not escalate a field that the glossary has pinned as
            # never available. This matches how glossary-override users
            # (see glossary_override.py) flip feature_available to
            # "never" to simulate the cap-absent path — the override
            # must survive every ingress, not just the first.
            glossary_pinned_never = field_def.get("feature_available") == "never"

            # Decode the capability value using THIS field's cap definition
            if cap_def.get("values"):
                raw_val = rec["data"][0] if rec["data"] else None
                decoded = decode_enum_cap(cap_def, raw_val)

                cap_fa = decoded.get("feature_available")
                if cap_fa and not glossary_pinned_never:
                    status_field["feature_available"] = cap_fa

                ac = {}
                for k in ("valid_range", "valid_set", "step", "correction", "slider", "values", "custom_value"):
                    if k in decoded:
                        ac[k] = decoded[k]
                if ac:
                    status_field["active_constraints"] = ac

            elif cap_def.get("decode"):
                decoded = decode_data_cap(cap_def, rec["data"])

                modes = {}
                for _dk, dv in decoded.items():
                    if not isinstance(dv, dict) or dv.get("role") not in ("min", "max"):
                        continue
                    mode = dv.get("mode", "default")
                    modes.setdefault(mode, {})
                    modes[mode][dv["role"]] = dv["value"]

                step = 1.0
                for dk, dv in decoded.items():
                    if isinstance(dv, dict) and dv.get("role") == "flag" and "half_deg" in dk:
                        step = 0.5 if dv["value"] == 1 else 1.0

                if modes:
                    status_field["active_constraints"] = {
                        "by_mode": {
                            m: {"valid_range": [r.get("min"), r.get("max")], "step": step, "correction": "clamp"}
                            for m, r in modes.items()
                        }
                    }

                if not glossary_pinned_never:
                    status_field["feature_available"] = "always"


def process_b5(status: dict, body: bytes, glossary: dict, timestamp: str | None = None) -> bool:
    """Process a B5 capability frame and update the status file.

    Returns the next_frame flag: True if more B5 pages are available.
    """
    parsed = parse_b5_tlv(body)
    records = parsed["records"]
    next_frame = parsed["next_frame"]

    # Append raw records (multiple B5 queries may be needed for all caps)
    existing = status.get("capabilities_raw", [])
    new_records = [{k: v for k, v in rec.items() if k != "frame_name"} for rec in records]
    # Merge: update existing caps by cap_id, add new ones
    existing_ids = {r["cap_id"].lower() for r in existing}
    for rec in new_records:
        if rec["cap_id"].lower() in existing_ids:
            # Replace existing record with updated one
            existing = [r for r in existing if r["cap_id"].lower() != rec["cap_id"].lower()]
        existing.append(rec)
    status["capabilities_raw"] = existing

    # Decode each cap and update ALL fields that share this cap_id
    _apply_caps_to_fields(status, records, glossary)

    status["meta"]["b5_received"] = True
    status["meta"]["phase"] = "post_b5"
    status["meta"]["frame_counts"]["rsp_0xb5"] = status["meta"]["frame_counts"].get("rsp_0xb5", 0) + 1

    return next_frame


def finalize_capabilities(status: dict, glossary: dict):
    """Mark fields whose cap was never reported as 'never'.

    Call this AFTER all B5 queries are done (multiple pages may be needed).
    Checks the accumulated capabilities_raw, not just the last B5 response.
    """
    fields = walk_fields(glossary)
    all_caps = status.get("capabilities_raw", [])
    all_cap_ids = {r["cap_id"].lower() for r in all_caps}

    for field_name, status_field in status["fields"].items():
        if status_field["feature_available"] in ("capability", "capability-opt"):
            fdef = fields.get(field_name, {})
            cap_def = fdef.get("capability", {})
            cap_id = cap_def.get("cap_id", "").lower()
            if cap_id and cap_id not in all_cap_ids:
                status_field["feature_available"] = "never"


# ── Data frame processing (C0, C1, A1) ───────────────────────────────────


def process_data_frame(
    status: dict,
    body: bytes,
    protocol_key: str,
    glossary: dict,
    timestamp: str | None = None,
    field_map: list[dict] | None = None,
):
    """Process a data frame (C0/C1/A1) and update field values in the status.

    Each decoded field gets a slot under `field["sources"][protocol_key]`
    annotated with its generation (legacy / new / null). Distinct frame
    keys keep distinct slots — rsp_0xc1_group4 and rsp_0xc1_sub02 do not
    share storage. Reads go through field_query.read_field(), which
    walks the slots via a priority list of scopes.

    ``field_map`` is forwarded to :func:`decode_frame_fields`. Supply it
    from a caller-owned cache (e.g. ``StatusDB._field_map_cache``) to
    skip the O(fields) ``build_field_map`` walk on every frame. Omit it
    for one-shot / CLI callers — the decode path rebuilds automatically.
    """
    cap_records = status.get("capabilities_raw")
    rejections: list[dict] = []
    decoded = decode_frame_fields(
        body,
        protocol_key,
        glossary,
        cap_records=cap_records,
        field_map=field_map,
        rejections_out=rejections,
    )

    ts = timestamp or datetime.now(UTC).isoformat()
    generation = infer_generation(protocol_key, glossary)

    # Increment frame count BEFORE writing so the per-slot frame_no
    # reflects the current frame index.
    new_count = status["meta"]["frame_counts"].get(protocol_key, 0) + 1
    status["meta"]["frame_counts"][protocol_key] = new_count

    suppression_counts: dict[str, int] = {}
    for field_name, result in decoded.items():
        status_field = status["fields"].get(field_name)
        if not status_field:
            continue

        # Skip fields that are not available (never, or *capability* / *capability-opt*
        # not yet resolved by B5 promotion)
        if status_field["feature_available"] in ("never", "capability", "capability-opt"):
            continue

        suppression = result.get("suppression")
        slot = {
            "value": result["value"],
            "raw": result.get("raw"),
            "frame_no": new_count,
            "ts": ts,
            "generation": generation,
        }
        if suppression:
            # Sentinel and out-of-range overwrite the slot's value with
            # None — the device's *current* answer is "no data". A
            # sibling `suppression` dict carries the reason and the
            # underlying byte (sentinel) or decoded number (range) so
            # downstream consumers (HA entity attributes, diagnostics
            # bundle) can show why.
            slot["suppression"] = {
                "reason": suppression,
                "raw": result.get("raw"),
                "frame_no": new_count,
                "ts": ts,
            }
            suppression_counts[suppression] = suppression_counts.get(suppression, 0) + 1
            if suppression == "out_of_range":
                log.warning(
                    "Field %s on %s decoded %r outside declared range — possible decoder/firmware drift",
                    field_name,
                    protocol_key,
                    result.get("raw"),
                )
            else:  # sentinel
                log.debug(
                    "Field %s on %s suppressed (sentinel byte 0x%02X)",
                    field_name,
                    protocol_key,
                    result.get("raw") or 0,
                )
        status_field.setdefault("sources", {})[protocol_key] = slot

    for reason, count in suppression_counts.items():
        ck = f"{protocol_key}_{reason}_suppressions"
        status["meta"]["frame_counts"][ck] = status["meta"]["frame_counts"].get(ck, 0) + count

    # Surface per-field rejection metadata without overwriting the prior
    # good reading. The integration's write-confirmation reconcile reads
    # `sources[protocol_key]["rejection"]` to decide whether the next
    # status broadcast confirms a pending write or rejects it.
    rejection_count = 0
    for rec in rejections:
        status_field = status["fields"].get(rec["field"])
        if not status_field:
            continue
        if status_field["feature_available"] in ("never", "capability", "capability-opt"):
            continue
        slot = status_field.setdefault("sources", {}).setdefault(protocol_key, {})
        slot["rejection"] = {
            "outcome": rec["outcome"],
            "status": rec["status"],
            "data_len": rec["data_len"],
            "property_id": rec["property_id"],
            "frame_no": new_count,
            "ts": ts,
        }
        rejection_count += 1
    if rejection_count:
        rk = f"{protocol_key}_rejections"
        status["meta"]["frame_counts"][rk] = status["meta"]["frame_counts"].get(rk, 0) + rejection_count

    # Phase transition: first C0 after B5 → steady_state
    if status["meta"]["phase"] == "post_b5" and protocol_key == "rsp_0xc0":
        status["meta"]["phase"] = "steady_state"


# ── Main entry point ─────────────────────────────────────────────────────


def process_raw_frame(status: dict, body: bytes, glossary: dict, timestamp: str | None = None):
    """Identify and process a raw frame body, updating the status."""
    protocol_key = identify_frame(body)

    if protocol_key == "rsp_0xb5":
        process_b5(status, body, glossary, timestamp=timestamp)
    else:
        process_data_frame(status, body, protocol_key, glossary, timestamp=timestamp)

    return protocol_key


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Process frames and update device status")
    parser.add_argument("status_file", help="Path to device_status.json")
    parser.add_argument("--hex", help="Hex frame body to process")
    parser.add_argument("--frame-file", help="YAML frame fixture to process")
    parser.add_argument("--index", type=int, help="Frame index within fixture (default: all)")
    args = parser.parse_args()

    status_path = Path(args.status_file)
    with open(status_path, encoding="utf-8") as f:
        status = json.load(f)

    glossary = load_glossary()

    if args.hex:
        hex_str = args.hex.replace(" ", "")
        body = bytes.fromhex(hex_str)
        pkey = process_raw_frame(status, body, glossary)
        print(f"Processed: {pkey}")

    elif args.frame_file:
        with open(args.frame_file, encoding="utf-8") as f:
            frames_data = yaml.safe_load(f)

        frames = frames_data["frames"]
        if args.index is not None:
            frames = [frames[args.index]]

        for frame in frames:
            hex_str = frame["body_hex"].replace(" ", "").replace("\n", "")
            body = bytes.fromhex(hex_str)
            ts = str(frame.get("timestamp", ""))
            pkey = process_raw_frame(status, body, glossary, timestamp=ts)
            print(f"  {frame['name']}: {pkey}")

    # Write updated status
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)

    print(f"Phase: {status['meta']['phase']}")
    populated = sum(1 for f in status["fields"].values() if f.get("sources"))
    print(f"Fields with values: {populated}/{len(status['fields'])}")
    print(f"Wrote {status_path}")


if __name__ == "__main__":
    main()
