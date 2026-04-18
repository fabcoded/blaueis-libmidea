#!/usr/bin/env python3
"""Build a 0x40 Set command frame from device status + desired changes.

Fully glossary-driven: reads all byte positions from cmd_0x40 decode arrays.
No hardcoded byte offsets — the glossary is the single source of truth.

Includes a set-command preflight check: cmd_0x40 packs many fields into
bit-shared bytes, so encoding without knowing the current state of every
sibling will silently clobber them. Before encoding, run the preflight to
verify that all siblings of any changed field have been read recently.
Hard-block by default; opt-in override with `skip_preflight=True`.

Usage:
    python build_command.py device_status.json --set target_temperature=24 operating_mode=2
    python build_command.py device_status.json --set power=false
    python build_command.py device_status.json --set power=true --skip-preflight
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from blaueis.core.codec import build_field_map, encode_field, load_glossary, walk_fields
from blaueis.core.query import read_field
from blaueis.core.ux_gating import default_for_masked_field, is_field_visible

# Default freshness window for sibling fields. Picked to be loose enough that
# a normal scan loop (15s) keeps everything fresh, tight enough that a multi-
# minute gap (e.g. dongle restart) gets caught.
DEFAULT_PREFLIGHT_THRESHOLD_SECONDS = 30.0


# ── UX mask helper ───────────────────────────────────────────────────────


def _effective_operating_mode(status: dict, changes: dict):
    """Mode used to evaluate ``ux.visible_in_modes`` for outgoing bits.

    `changes` wins: if the caller is changing mode in this very frame,
    the masker sees the NEW mode and zeroes old-mode-only fields. This
    is what prevents stale eco=1 carrying from cool into a heat-mode C3.
    """
    if "operating_mode" in changes:
        return changes["operating_mode"]
    r = read_field(status, "operating_mode")
    return r["value"] if r else None


# ── Set-command preflight helpers ────────────────────────────────────────


def _group_cmd_fields_by_byte(field_map: list[dict]) -> dict[int, list[str]]:
    """Group cmd_0x40 field names by body byte offset.

    Walks each field's first decode step and buckets by `offset`. Fields
    that share an offset will end up in the same list — these are the
    bit-packed siblings that read-modify-write must consider.

    Returns: {byte_offset: [field_name, ...]}
    """
    by_byte: dict[int, list[str]] = {}
    for field in field_map:
        decode = field.get("decode") or []
        if not decode:
            continue
        offset = decode[0].get("offset")
        if offset is None:
            continue
        by_byte.setdefault(offset, []).append(field["name"])
    return by_byte


def _group_cmd_fields_by_property(field_map: list[dict]) -> dict[str, list[str]]:
    """Group cmd_0xb0 field names by property_id.

    Property IDs identify TLV records in the B0 set frame. Multiple fields
    can share one property_id (e.g. fresh_air_switch + fresh_air_fan_speed
    both at "0x4B,0x00" with different offsets within the data buffer).

    Returns: {"0xLL,0xHH": [field_name, ...]}
    """
    by_prop: dict[str, list[str]] = {}
    for field in field_map:
        decode = field.get("decode") or []
        if not decode:
            continue
        prop_id = decode[0].get("property_id")
        if not prop_id:
            continue
        by_prop.setdefault(prop_id, []).append(field["name"])
    return by_prop


def _field_is_exempt(name: str, field_def: dict, status: dict, glossary: dict | None = None) -> bool:
    """Return True if a sibling field is exempt from the preflight check.

    Exempt fields:
      - Have a glossary `default_value` (encoder uses the default, not status).
        These are protocol-reserved bits like protocol_bit1, swing_reserved.
      - Are capability-gated to `never` (or `capability`) — process_data_frame
        skips writes for these so last_updated can never become fresh.
      - Have no rsp_* protocol entry at all (pure write-only fields like
        exchange_air and fan_speed_timer_bit). The encoder always emits
        their current_value (or 0/False if None) and there is no decode
        path that could ever populate last_updated.
      - Are not present in the status dict at all (orphaned glossary entry).
    """
    if field_def.get("default_value") is not None:
        return True
    fdef = status.get("fields", {}).get(name)
    if fdef is None:
        return True
    fa = fdef.get("feature_available")
    # 'never' = unit doesn't have this capability; 'capability' = pre-B5
    # state where decoding is suppressed by process_data_frame, so the
    # field will never have a last_updated.  Both are exempt for the same
    # reason: we can't get fresh data for them.
    if fa in ("never", "capability"):
        return True
    # Pure write-only fields: no rsp_* entry → no decode path → no
    # last_updated will ever be populated by process_data_frame.
    # Look up the glossary entry by name (field_map only carries the
    # cmd_0x40 / cmd_0xb0 protocol slice).
    if glossary is not None:
        from blaueis.core.codec import walk_fields  # local import avoids cycle

        all_fields = walk_fields(glossary)
        full_def = all_fields.get(name) or {}
        protocols = full_def.get("protocols") or {}
        has_read_path = any(p.startswith("rsp_") for p in protocols)
        if not has_read_path:
            return True
    return False


def set_command_preflight(
    field_map: list[dict],
    changes: dict,
    status: dict,
    siblings_by_position: dict,
    threshold_seconds: float,
    now: datetime | None = None,
    glossary: dict | None = None,
) -> list[dict]:
    """Pre-transmit safety check: verify it is safe to send a set command.

    cmd_0x40 packs fields into bit-shared bytes, so any sibling whose state
    is unknown or stale will get clobbered when the body is encoded. This
    is the mandatory check that callers should run before transmitting any
    set frame — analogous to an aircraft preflight inspection.

    For each field in `changes`, find its wire position (byte offset for
    cmd_0x40, property_id for cmd_0xb0), look up siblings, and check
    last_updated freshness. Returns a list of preflight errors; an empty
    list means the command is safe to send.

    The caller passes `siblings_by_position` keyed by either int (byte
    offset for cmd_0x40) or str (property_id for cmd_0xb0). We pick the
    matching position type from each changed field's decode step.

    Each error dict has:
        severity:    "error"
        field:       the stale/missing sibling
        reason:      "never_read" | "stale"
        age_seconds: float | None  (None for never_read)
        shared_with: the changed field that triggered the check
        position:    "body[N]" or "0xLL,0xHH"
    """
    if now is None:
        now = datetime.now(UTC)

    # Detect mode from the siblings map's key type. Empty maps are no-ops.
    sample_key = next(iter(siblings_by_position), None)
    is_property_mode = isinstance(sample_key, str)

    # Index field_map by name for quick lookup
    by_name = {f["name"]: f for f in field_map}

    errors: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()  # dedupe (sibling, position)

    for changed_name in changes:
        changed_field = by_name.get(changed_name)
        if changed_field is None:
            continue
        decode = changed_field.get("decode") or []
        if not decode:
            continue
        # In property mode (cmd_0xb0), position is property_id; otherwise
        # (cmd_0x40), position is byte offset.
        position = decode[0].get("property_id") if is_property_mode else decode[0].get("offset")
        if position is None:
            continue

        siblings = siblings_by_position.get(position, [])
        position_label = f"body[{position}]" if isinstance(position, int) else position

        for sibling in siblings:
            if sibling == changed_name:
                continue  # caller is overwriting this anyway
            if sibling in changes:
                continue  # also being changed in this command
            if (sibling, str(position)) in seen_pairs:
                continue
            seen_pairs.add((sibling, str(position)))

            sibling_def = by_name.get(sibling, {})
            if _field_is_exempt(sibling, sibling_def, status, glossary):
                continue

            # Read the sibling's most recent value via the priority API.
            # Preflight cares about staleness, not source provenance, so
            # we use the broadest scope (`protocol_all`) — any frame that
            # decoded this sibling counts as a recent observation.
            sibling_read = read_field(status, sibling, priority=["protocol_all"])
            last_updated = sibling_read["ts"] if sibling_read else None

            if last_updated is None:
                errors.append(
                    {
                        "severity": "error",
                        "field": sibling,
                        "reason": "never_read",
                        "age_seconds": None,
                        "shared_with": changed_name,
                        "position": position_label,
                    }
                )
                continue

            try:
                ts = datetime.fromisoformat(last_updated)
            except (TypeError, ValueError):
                # Malformed timestamp — treat as never_read for safety
                errors.append(
                    {
                        "severity": "error",
                        "field": sibling,
                        "reason": "never_read",
                        "age_seconds": None,
                        "shared_with": changed_name,
                        "position": position_label,
                    }
                )
                continue

            age = (now - ts).total_seconds()
            if age > threshold_seconds:
                errors.append(
                    {
                        "severity": "error",
                        "field": sibling,
                        "reason": "stale",
                        "age_seconds": age,
                        "shared_with": changed_name,
                        "position": position_label,
                    }
                )

    return errors


def build_command_body(
    status: dict,
    changes: dict,
    glossary: dict | None = None,
    *,
    preflight_threshold_seconds: float = DEFAULT_PREFLIGHT_THRESHOLD_SECONDS,
    skip_preflight: bool = False,
    now: datetime | None = None,
) -> dict:
    """Build a 26-byte 0x40 Set command body from status + desired changes.

    Runs `set_command_preflight` before encoding. If any sibling field of a
    changed field is stale (last_updated older than threshold) or never read
    (last_updated is None), refuses to encode and returns an empty result
    with the preflight errors populated. Pass `skip_preflight=True` to
    encode anyway — the preflight list will still carry the warnings.

    Returns:
        On success: {body, body_hex, preflight, fields_encoded}
        On preflight block: {body: None, body_hex: None, preflight: errors, fields_encoded: 0}
    """
    if glossary is None:
        glossary = load_glossary()

    encodings = glossary.get("encodings", {})
    field_map = build_field_map(glossary, "cmd_0x40")

    # ── Preflight check ─────────────────────────────────────────────
    siblings_by_byte = _group_cmd_fields_by_byte(field_map)
    preflight = set_command_preflight(
        field_map=field_map,
        changes=changes,
        status=status,
        siblings_by_position=siblings_by_byte,
        threshold_seconds=preflight_threshold_seconds,
        now=now,
        glossary=glossary,
    )
    if preflight and not skip_preflight:
        return {
            "body": None,
            "body_hex": None,
            "preflight": preflight,
            "fields_encoded": 0,
        }

    body = bytearray(26)
    body[0] = 0x40

    fields_encoded = 0

    # Full per-field glossary for UX-mask lookup. The field_map is flattened
    # and lacks the `ux` block, so we walk the raw glossary once.
    all_gdefs = walk_fields(glossary)
    effective_mode = _effective_operating_mode(status, changes)

    for field in field_map:
        name = field["name"]
        decode_steps = field["decode"]
        data_type = field["data_type"]
        default_value = field.get("default_value")

        # Determine value: desired change > current status > glossary default > type zero.
        # Glossary default_value handles protocol-reserved fields (protocol_bit1,
        # swing_reserved, ...) whose bits the spec requires to be fixed; the branch
        # is kept generic so the §A.9 grep test passes — see serial_glossary_guide.md
        # Part 4.
        if name in changes:
            value = changes[name]
        elif default_value is not None:
            value = default_value
        elif name in status.get("fields", {}):
            sibling_read = read_field(status, name)
            value = sibling_read["value"] if sibling_read else None
        else:
            value = None

        if value is None:
            value = False if data_type == "bool" else 0

        # UX mask: if this field is not in `changes` AND its ux block hides
        # it in the effective mode, force a safe default so stale state from
        # a prior mode doesn't hitchhike on the outgoing frame. Explicit
        # sets always pass through untouched (caller asked for it; the AC
        # is authoritative about whether it takes effect).
        if name not in changes:
            gdef = all_gdefs.get(name)
            if not is_field_visible(gdef, current_mode=effective_mode):
                value = default_for_masked_field(gdef)

        # Encode into body
        encode_field(body, decode_steps, data_type, value, encodings)
        fields_encoded += 1

    return {
        "body": body,
        "body_hex": body.hex(" "),
        "preflight": preflight,
        "fields_encoded": fields_encoded,
    }


def build_b0_command_body(
    status: dict,
    changes: dict,
    glossary: dict | None = None,
    *,
    preflight_threshold_seconds: float = DEFAULT_PREFLIGHT_THRESHOLD_SECONDS,
    skip_preflight: bool = False,
    now: datetime | None = None,
) -> dict:
    """Build a 0xB0 Property Set command body from status + desired changes.

    Groups changed fields by property_id, reads sibling fields from status
    to avoid clobbering, and assembles one TLV record per property.

    Runs `set_command_preflight` on property-level siblings. Most B0
    properties are independent (one field per property_id) so the preflight
    is a no-op, but multi-field properties like aqua_wash (0x4A,0x00 holds
    manual + switch + time) get the same protection as cmd_0x40 byte
    siblings.

    Returns:
        On success: {body, body_hex, preflight, fields_encoded}
        On preflight block: {body: None, body_hex: None, preflight: errors, fields_encoded: 0}
    """
    if glossary is None:
        glossary = load_glossary()

    encodings = glossary.get("encodings", {})
    field_map = build_field_map(glossary, "cmd_0xb0")

    # ── Preflight check ─────────────────────────────────────────────
    siblings_by_prop = _group_cmd_fields_by_property(field_map)
    preflight = set_command_preflight(
        field_map=field_map,
        changes=changes,
        status=status,
        siblings_by_position=siblings_by_prop,
        threshold_seconds=preflight_threshold_seconds,
        now=now,
        glossary=glossary,
    )
    if preflight and not skip_preflight:
        return {
            "body": None,
            "body_hex": None,
            "preflight": preflight,
            "fields_encoded": 0,
        }

    # Group fields by property_id.  For each property, collect every field
    # that shares it — both those being changed and siblings read from status.
    prop_fields: dict[str, list] = {}  # prop_id -> [(offset, bits, value, data_type)]

    for field in field_map:
        name = field["name"]
        decode_steps = field["decode"]
        if not decode_steps:
            continue
        step = decode_steps[0]
        prop_id = step.get("property_id")
        if not prop_id:
            continue

        # Only include properties that have at least one changed field
        prop_fields.setdefault(prop_id, [])

    # Filter to properties that have at least one changed field
    changed_props = set()
    for field in field_map:
        name = field["name"]
        if name not in changes:
            continue
        step = field["decode"][0] if field["decode"] else {}
        prop_id = step.get("property_id")
        if prop_id:
            changed_props.add(prop_id)

    # Now collect all fields for changed properties (including siblings).
    # Each entry preserves the full decode step so encode_field() can see
    # the encoding name (e.g. temp_offset50_half) and the add offset —
    # without those the round-trip drops the per-encoding transform.
    prop_data: dict[str, dict[int, tuple]] = {}  # prop_id -> {offset: (step, value, data_type)}
    for field in field_map:
        step = field["decode"][0] if field["decode"] else {}
        prop_id = step.get("property_id")
        if not prop_id or prop_id not in changed_props:
            continue

        name = field["name"]
        data_type = field["data_type"]
        offset = step.get("offset", 0)

        if name in changes:
            value = changes[name]
        elif name in status.get("fields", {}):
            sibling_read = read_field(status, name)
            value = sibling_read["value"] if sibling_read else None
        else:
            value = False if data_type == "bool" else 0

        if value is None:
            value = False if data_type == "bool" else 0

        prop_data.setdefault(prop_id, {})
        prop_data[prop_id][offset] = (step, value, data_type)

    # Assemble TLV body
    body = bytearray([0xB0, 0x00])  # tag + record count placeholder
    fields_encoded = 0

    for prop_id, offsets in sorted(prop_data.items()):
        parts = prop_id.split(",")
        lo = int(parts[0], 16)
        hi = int(parts[1], 16)
        max_offset = max(offsets.keys())
        # Buffer must be long enough to hold the widest encoding for any
        # sub-field (e.g. uint16_le occupies 2 bytes starting at offset 0).
        buf_len = max_offset + 1
        for offset, (step, _value, _dt) in offsets.items():
            enc_name = step.get("encoding")
            if enc_name:
                enc_def = encodings.get(enc_name, {})
                buf_len = max(buf_len, offset + enc_def.get("byte_count", 1))
        data = bytearray(buf_len)

        for _offset, (step, value, data_type) in offsets.items():
            # Forward the original decode step (preserving encoding/add/
            # half_bit metadata) so encode_field can apply the inverse
            # transform — stripping these keys would lose temperature
            # offsets and multi-byte encodings.
            encode_field(data, [step], data_type, value, encodings)
            fields_encoded += 1

        # B0 SET TLV format: prop_id_lo(1) + prop_id_hi(1) + data_len(1) + data(N)
        # Note: NO data_type byte in the SET path (3-byte header).
        # The RX path (B1 response) uses a 4-byte header with data_type — but
        # the manufacturer Lua's TX encoder omits data_type in SET commands.
        body.extend([lo, hi, len(data)])
        body.extend(data)

    body[1] = len(prop_data)

    return {
        "body": body,
        "body_hex": body.hex(" "),
        "preflight": preflight,
        "fields_encoded": fields_encoded,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build 0x40 Set command frame")
    parser.add_argument("status_file", help="Path to device_status.json")
    parser.add_argument("--set", nargs="+", help="Field=value pairs to change")
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the set-command preflight check and encode even if sibling state is stale",
    )
    parser.add_argument(
        "--preflight-seconds",
        type=float,
        default=DEFAULT_PREFLIGHT_THRESHOLD_SECONDS,
        help=f"Sibling staleness threshold in seconds (default: {DEFAULT_PREFLIGHT_THRESHOLD_SECONDS})",
    )
    parser.add_argument(
        "--quirks",
        action="append",
        default=[],
        metavar="PATH",
        help="Apply a device quirks YAML file before building the command. Repeatable to layer multiple files.",
    )
    args = parser.parse_args()

    with open(args.status_file, encoding="utf-8") as f:
        status = json.load(f)

    if status["meta"]["phase"] == "boot":
        print("ERROR: Cannot build command in boot phase (no device state)", file=sys.stderr)
        sys.exit(1)

    # Apply device quirks (after the loaded status is finalized).
    if args.quirks:
        from blaueis.core.quirks import apply_quirks_files

        glossary = load_glossary()
        reports = apply_quirks_files(status, args.quirks, glossary)
        for r in reports:
            src = Path(r.get("source", "<unknown>")).name
            print(f"Applied quirks: {src} ({r['name']})", file=sys.stderr)
            if r["fields_overridden"]:
                print(f"  fields: {', '.join(r['fields_overridden'])}", file=sys.stderr)
            if r["caps_synthesized"]:
                print(f"  caps: {', '.join(r['caps_synthesized'])}", file=sys.stderr)

    # Parse changes
    changes = {}
    for item in args.set or []:
        key, _, val = item.partition("=")
        # Type coercion
        if val.lower() in ("true", "false"):
            changes[key] = val.lower() == "true"
        elif "." in val:
            changes[key] = float(val)
        else:
            try:
                changes[key] = int(val)
            except ValueError:
                changes[key] = val

    result = build_command_body(
        status,
        changes,
        preflight_threshold_seconds=args.preflight_seconds,
        skip_preflight=args.skip_preflight,
    )

    if result["body"] is None:
        print("ERROR: cmd_0x40 preflight check failed:", file=sys.stderr)
        for err in result["preflight"]:
            age = f"age={err['age_seconds']:.1f}s" if err["age_seconds"] is not None else "never read"
            print(
                f"  [{err['reason']}] {err['field']} ({err['position']}, sibling of {err['shared_with']}, {age})",
                file=sys.stderr,
            )
        print("Use --skip-preflight to override (will write 0/False for unread fields).", file=sys.stderr)
        sys.exit(2)

    if result["preflight"]:
        print(
            f"WARNING: {len(result['preflight'])} preflight warning(s) (preflight skipped):",
            file=sys.stderr,
        )
        for err in result["preflight"]:
            print(f"  {err['field']}: {err['reason']}", file=sys.stderr)

    print(f"0x40 Set command body ({result['fields_encoded']} fields):")
    print(f"  {result['body_hex']}")
    print("\nBody bytes:")
    for i in range(0, 26, 8):
        chunk = result["body"][i : i + 8]
        hex_str = " ".join(f"{b:02x}" for b in chunk)
        print(f"  body[{i:2d}-{min(i + 7, 25):2d}]: {hex_str}")


if __name__ == "__main__":
    main()
