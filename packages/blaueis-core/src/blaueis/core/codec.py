"""Shared codec library for the Midea HVAC serial protocol.

Glossary-driven decode/encode functions used by all toolkit scripts.
Extracted from decode_frames.py and parse_b5_state.py.
"""

from pathlib import Path

import yaml

SPEC_DIR = Path(__file__).resolve().parent / "data"
GLOSSARY_PATH = SPEC_DIR / "glossary.yaml"

_glossary_cache: dict | None = None


# ── Glossary loader ───────────────────────────────────────────────────────


def load_glossary() -> dict:
    """Load and cache the serial glossary YAML."""
    global _glossary_cache
    if _glossary_cache is None:
        with open(GLOSSARY_PATH, encoding="utf-8") as f:
            _glossary_cache = yaml.safe_load(f)
    return _glossary_cache


# ── Field walkers ─────────────────────────────────────────────────────────


def walk_fields(glossary: dict) -> dict[str, dict]:
    """Flatten the nested glossary fields into {canonical_name: field_def}."""
    result = {}
    fields = glossary.get("fields", {})
    for _, category in fields.items():
        if not isinstance(category, dict):
            continue
        for key, val in category.items():
            if isinstance(val, dict) and "description" in val:
                result[key] = val
            elif isinstance(val, dict):
                for field_name, field_def in val.items():
                    if isinstance(field_def, dict) and "description" in field_def:
                        result[field_name] = field_def
    return result


def build_field_map(glossary: dict, protocol_key: str) -> list[dict]:
    """Extract all fields that have a decode array for the given protocol key."""
    result = []
    fields = glossary.get("fields", {})
    for _cat_name, cat in fields.items():
        if not isinstance(cat, dict):
            continue
        for key, val in cat.items():
            if isinstance(val, dict) and "description" in val:
                _check_field(result, key, val, protocol_key)
            elif isinstance(val, dict):
                for fname, fdef in val.items():
                    if isinstance(fdef, dict) and "description" in fdef:
                        _check_field(result, fname, fdef, protocol_key)
    return result


def _check_field(result, name, fdef, protocol_key):
    """If field has a matching protocol entry with decode array, add it.

    Match is exact — no substring containment. The earlier `(protocol_key
    in pkey)` clause caused `rsp_0xc1_group1` to spuriously match
    `rsp_0xc1_group11` and `rsp_0xc1_group12`, pulling 13 vane / group12
    fields into the group 1 decode path with stale byte offsets.
    """
    protocols = fdef.get("protocols", {})
    ploc = protocols.get(protocol_key)
    if ploc is None:
        return
    decode = ploc.get("decode")
    if not decode:
        return
    result.append(
        {
            "name": name,
            "decode": decode,
            "data_type": fdef.get("data_type"),
            "capability": fdef.get("capability"),
            "default_value": fdef.get("default_value"),
        }
    )


def build_cap_index(fields: dict[str, dict]) -> dict[str, list[str]]:
    """Map cap_id (e.g. '0x10') -> [field_names] for fields with inline capability.

    Multiple fields can share the same cap_id (e.g. 4 power/energy fields
    all use cap 0x16). Returns a list of field names per cap_id.
    """
    index: dict[str, list[str]] = {}
    for name, fdef in fields.items():
        cap = fdef.get("capability")
        if cap and "cap_id" in cap:
            index.setdefault(cap["cap_id"].lower(), []).append(name)
    return index


# ── Frame-key generation inference (data-driven, glossary lookup) ────────
#
# The protocol-key → generation map lives in the glossary under the
# top-level `protocol_generations:` dict. Adding a new frame later is one
# YAML edit — no Python change. The lookup is intentionally strict:
# unknown keys resolve to None so the user has to opt new frames in
# explicitly. mill1000/midea-msmart appendix §4.3 documents the legacy
# vs new generation split that this map encodes.


def infer_generation(frame_key: str, glossary: dict | None = None) -> str | None:
    """Return 'legacy', 'new', or None for a glossary protocol key.

    Reads the glossary's `protocol_generations` dict — the single source
    of truth for the legacy/new split. Unknown keys (not in the dict)
    return None, matching the behaviour required by the priority-list
    semantics in field_query.read_field(): a slot with `generation =
    None` is excluded from `protocol_legacy` and `protocol_new` scopes
    and only matched by `protocol_unknown` or `protocol_all`.

    The `glossary` argument is optional for backwards compatibility with
    callers that don't have a glossary handy — in that case the function
    falls back to a load_glossary() call.
    """
    if glossary is None:
        glossary = load_glossary()
    return glossary.get("protocol_generations", {}).get(frame_key)


# ── Bit/byte extraction ──────────────────────────────────────────────────


def extract_bits(byte_val: int, bits: list[int]) -> int:
    """Extract bits[high:low] from a byte value."""
    high, low = bits
    mask = ((1 << (high - low + 1)) - 1) << low
    return (byte_val & mask) >> low


def insert_bits(byte_val: int, bits: list[int], value: int) -> int:
    """Insert value into bits[high:low] of byte_val. Reverse of extract_bits."""
    high, low = bits
    mask = ((1 << (high - low + 1)) - 1) << low
    return (byte_val & ~mask) | ((value << low) & mask)


def bcd(b: int) -> int:
    """Midea pseudo-BCD: each nibble as decimal digit (allows A-F)."""
    return ((b >> 4) & 0xF) * 10 + (b & 0xF)


# ── Encoding ──────────────────────────────────────────────────────────────


def _read_uint(body: bytes, offset: int, n_bytes: int, order: str) -> int:
    """Read an n-byte unsigned integer from body[offset:offset+n_bytes].

    Used by multi-byte encodings that carry a `byte_order` field
    ('le' or 'be'). Pure delegation to int.from_bytes — Python's stdlib
    is the canonical reference for byte-order arithmetic.
    """
    if offset + n_bytes > len(body):
        raise IndexError(f"uint{n_bytes * 8}_{order} read at offset {offset} past end of {len(body)}-byte body")
    if order == "le":
        return int.from_bytes(body[offset : offset + n_bytes], "little")
    return int.from_bytes(body[offset : offset + n_bytes], "big")


def apply_encoding(raw, enc_name: str, encodings: dict, body: bytes | None = None, offset: int = 0):
    """Apply a named encoding formula to a raw value or multi-byte range."""
    enc = encodings.get(enc_name, {})

    enc_byte_count = enc.get("byte_count", 1)
    if body is not None and enc_byte_count > 1:
        o = offset
        if "bcd" in enc_name:
            if enc_byte_count == 4:
                return bcd(body[o]) * 10000 + bcd(body[o + 1]) * 100 + bcd(body[o + 2]) + bcd(body[o + 3]) / 100
            elif enc_byte_count == 3:
                return bcd(body[o]) + bcd(body[o + 1]) / 100 + bcd(body[o + 2]) / 10000
        if "linear" in enc_name:
            if enc_byte_count == 4:
                return (body[o] * 16777216 + body[o + 1] * 65536 + body[o + 2] * 256 + body[o + 3]) / 100.0
            elif enc_byte_count == 3:
                return (body[o] * 65536 + body[o + 1] * 256 + body[o + 2]) / 10000.0
        # Generic uint{16,24,32}_{le,be} dispatch via byte_order metadata.
        # Encodings declare byte_count + byte_order in the glossary; the
        # decoder reads the bytes and returns the integer. No name pattern
        # matching, no formula evaluation.
        byte_order = enc.get("byte_order")
        if byte_order in ("le", "be"):
            return _read_uint(body, offset, enc_byte_count, byte_order)
        return float(raw)

    enc_offset = enc.get("offset")
    scale = enc.get("scale")
    if enc_offset is not None and scale is not None:
        return (raw - enc_offset) / (1.0 / scale)
    return float(raw)


def resolve_capability_encoding(capability: dict | None, cap_records: list | None) -> str | None:
    """If field has a capability with encoding on its values, resolve active encoding from device caps."""
    if not capability or not cap_records:
        return None
    cap_id = capability.get("cap_id", "").lower()
    for rec in cap_records:
        if rec.get("cap_id", "").lower() == cap_id:
            raw_val = rec.get("data", [None])[0] if rec.get("data") else None
            if raw_val is None:
                return None
            for _vname, vdef in capability.get("values", {}).items():
                if vdef.get("raw") == raw_val and "encoding" in vdef:
                    return vdef["encoding"]
            return None
    return None


def reverse_encoding(value, enc_name: str, encodings: dict) -> int:
    """Reverse a named encoding formula: decoded value → raw integer."""
    enc = encodings.get(enc_name, {})
    enc_offset = enc.get("offset")
    scale = enc.get("scale")
    if enc_offset is not None and scale is not None:
        return int(value * (1.0 / scale) + enc_offset)
    return int(value)


def encode_field(body: bytearray, decode_steps: list, data_type: str, value, encodings: dict):
    """Encode a value into a frame body using glossary decode steps (reversed).

    Writes into body in-place. Handles bool, uint8, enum, and float types.
    For multi-step decode chains, uses only the first step (the primary encoding).
    """
    if not decode_steps or value is None:
        return

    step = decode_steps[0]

    # Logic combiner fields (OR) — set each source bit
    if "logic" in step:
        bool_val = 1 if value else 0
        for src in step.get("sources", []):
            if src["offset"] < len(body):
                body[src["offset"]] = insert_bits(body[src["offset"]], src["bits"], bool_val)
        return

    offset = step["offset"]
    bits = step["bits"]

    if offset >= len(body):
        return

    # Convert value to raw integer for bit insertion
    if data_type == "bool":
        raw = 1 if value else 0
    elif step.get("encoding"):
        raw = reverse_encoding(value, step["encoding"], encodings)
    elif step.get("add") is not None:
        # Reverse the add: raw = value - add
        raw = int(value - step["add"])
    else:
        raw = int(value)

    # Insert into body
    body[offset] = insert_bits(body[offset], bits, raw)

    # Handle half_bit (0.5°C flag)
    hb = step.get("half_bit")
    if hb and hb["offset"] < len(body):
        has_half = isinstance(value, float) and (value % 1) != 0
        body[hb["offset"]] = insert_bits(body[hb["offset"]], [hb["bit"], hb["bit"]], 1 if has_half else 0)


# ── Field decoder ─────────────────────────────────────────────────────────


def decode_field(
    name: str,
    decode_steps: list,
    data_type: str,
    body: bytes,
    encodings: dict,
    capability: dict | None = None,
    cap_records: list | None = None,
) -> dict:
    """Decode a field from a frame body using the glossary decode array.

    Generic decoder -- no field-name-specific logic. Processes the decode
    chain in priority order; first step whose condition is met wins.
    """
    if not decode_steps:
        return {"value": None, "note": "no decode steps"}

    try:
        for step in decode_steps:
            if "logic" in step:
                sources = step.get("sources", [])
                if step["logic"] == "or":
                    result = False
                    for src in sources:
                        if src["offset"] >= len(body):
                            continue
                        val = extract_bits(body[src["offset"]], src["bits"])
                        if val != 0:
                            result = True
                    return {"value": result}
                continue

            offset = step["offset"]
            bits = step["bits"]

            if offset >= len(body):
                continue

            val = extract_bits(body[offset], bits)

            cond = step.get("condition")
            if cond and ((cond == "!= 0" and val == 0) or (cond == "> 0" and val <= 0)):
                continue

            add = step.get("add")
            if add is not None:
                val = val + add

            hb = step.get("half_bit")
            if hb and hb["offset"] < len(body) and body[hb["offset"]] & (1 << hb["bit"]):
                val = val + 0.5

            enc_name = step.get("encoding")
            if enc_name == "capability":
                enc_name = resolve_capability_encoding(capability, cap_records)
            if enc_name:
                val = apply_encoding(val, enc_name, encodings, body=body, offset=offset)

            # Post-encoding fractional add from a separate byte's nibble.
            # Used by C0 indoor/outdoor temperature: the integer half-degree
            # base lives in body[11]/body[12] and the 0.1 °C tenths fraction
            # lives in the high/low nibble of body[15]. Cross-validated
            # against the manufacturer cloud plugin's C0 decoder — see
            # midea-msmart-mill1000.md Finding 17.
            tn = step.get("tenths_nibble")
            if tn and tn.get("offset", 0) < len(body):
                raw = body[tn["offset"]]
                nibble = (raw >> 4) & 0x0F if tn.get("nibble") == "high" else raw & 0x0F
                val = val + nibble / 10

            if data_type == "bool" and bits[0] == bits[1]:
                val = bool(val)

            return {"value": val}

        return {"value": None, "note": "no decode step matched"}
    except (IndexError, KeyError) as e:
        return {"value": None, "error": str(e)}


# ── B5 TLV parser ────────────────────────────────────────────────────────


def parse_b5_tlv(body: bytes) -> list[dict]:
    """Parse a B5 response body into TLV records.

    Body layout: body[0]=0xB5, body[1]=record_count, body[2..]=records.
    Each record: cap_id(1) + cap_type(1) + data_len(1) + data(N).
    """
    if body[0] != 0xB5:
        raise ValueError(f"Not a B5 body: starts with 0x{body[0]:02X}")

    record_count = body[1]
    records = []
    pos = 2

    while pos < len(body) and len(records) < record_count:
        if pos + 3 > len(body):
            break
        cap_id = body[pos]
        cap_type = body[pos + 1]
        data_len = body[pos + 2]
        if pos + 3 + data_len > len(body):
            break
        data = body[pos + 3 : pos + 3 + data_len]
        key_16 = f"0x{cap_type:02X}{cap_id:02X}"
        records.append(
            {
                "cap_id": f"0x{cap_id:02X}",
                "cap_type": cap_type,
                "key_16": key_16,
                "data_len": data_len,
                "data": list(data),
                "data_hex": data.hex(),
            }
        )
        pos += 3 + data_len

    return records


def parse_b0b1_tlv(body: bytes) -> list[dict]:
    """Parse a B0/B1 property frame body into TLV records.

    Body layout: body[0]=0xB0 or 0xB1, body[1]=record_count, body[2..]=records.
    Each record: prop_id_lo(1) + prop_id_hi(1) + data_type(1) + data_len(1) + data(N).
    Cursor advances by (4 + data_len) per record.
    """
    record_count = body[1]
    records = []
    pos = 2

    while pos < len(body) and len(records) < record_count:
        if pos + 4 > len(body):
            break
        prop_id_lo = body[pos]
        prop_id_hi = body[pos + 1]
        data_type = body[pos + 2]
        data_len = body[pos + 3]
        if pos + 4 + data_len > len(body):
            break
        data = body[pos + 4 : pos + 4 + data_len]
        prop_key = f"0x{prop_id_lo:02X},0x{prop_id_hi:02X}"
        records.append(
            {
                "property_id": prop_key,
                "data_type": data_type,
                "data_len": data_len,
                "data": list(data),
            }
        )
        pos += 4 + data_len

    return records


# ── Capability decoders ───────────────────────────────────────────────────


def decode_enum_cap(cap_def: dict, raw_value: int) -> dict:
    """Decode an enum-mapped capability: look up raw value in cap_def.values."""
    values = cap_def.get("values", {})
    for sym_name, vdef in values.items():
        if vdef.get("raw") == raw_value:
            result = {"decoded_key": sym_name, "raw": raw_value}
            for k in ("valid_range", "valid_set", "step", "correction", "ui_gate", "feature_available"):
                if k in vdef:
                    result[k] = vdef[k]
            return result
    return {"decoded_key": None, "raw": raw_value, "note": "value not in glossary"}


def decode_data_cap(cap_def: dict, data: list[int]) -> dict:
    """Decode a data-carried capability using cap_def.decode rules."""
    decode_rules = cap_def.get("decode", {})
    decoded = {}
    for field_name, rule in decode_rules.items():
        offset = rule.get("offset", 0)
        if offset >= len(data):
            decoded[field_name] = {"value": None, "note": "offset beyond data"}
            continue
        raw = data[offset]
        formula = rule.get("formula", "raw")
        if formula == "raw * 0.5":
            value = raw * 0.5
        elif formula == "bit0":
            value = raw & 0x01
        else:
            value = raw
        decoded[field_name] = {
            "raw": raw,
            "value": value,
            "role": rule.get("role"),
            "mode": rule.get("mode"),
            "unit": rule.get("unit"),
        }
    return decoded


# ── Frame muxer ───────────────────────────────────────────────────────────


PROTOCOL_KEY_MAP = {
    "0xC0 Status Response": "rsp_0xc0",
    "0xC1 Group 0": "rsp_0xc1_group0",
    "0xC1 Group 1": "rsp_0xc1_group1",
    "0xC1 Group 2": "rsp_0xc1_group2",
    "0xC1 Group 3": "rsp_0xc1_group3",
    "0xC1 Group 4": "rsp_0xc1_group4",
    "0xC1 Group 5": "rsp_0xc1_group5",
    "0xC1 Group 6": "rsp_0xc1_group6",
    "0xC1 Group 7": "rsp_0xc1_group7",
    "0xC1 Group 11": "rsp_0xc1_group11",
    "0xC1 Group 12": "rsp_0xc1_group12",
    "0xC1 Sub-page 0x02": "rsp_0xc1_sub02",
    "0xA1 Heartbeat": "rsp_0xa1",
    "0xB1 Property Response": "rsp_0xb1",
}


def identify_frame(body: bytes) -> str:
    """Identify frame type from body byte 0 and return the protocol key."""
    tag = body[0]
    if tag == 0xB5:
        return "rsp_0xb5"
    if tag == 0xC0:
        return "rsp_0xc0"
    if tag == 0xC1:
        # Group page is in body[3] (the page selector echoed from the query).
        # body[1]=0x21 (variant), body[2]=0x01 (sub-cmd). The low nibble of
        # body[3] & 0x0F gives the group number.
        # Exception: body[2]=0x01/0x02 with body[1]!=0x21 could be a sub-page
        # response (§4.3) — but those are rare and handled separately.
        group = body[3] & 0x0F if len(body) > 3 else 0
        return f"rsp_0xc1_group{group}"
    if tag == 0xA1:
        return "rsp_0xa1"
    if tag == 0xB1:
        return "rsp_0xb1"
    if tag == 0xB0:
        return "rsp_0xb0"
    raise ValueError(f"Unknown frame tag: 0x{tag:02X}")


def decode_frame_fields(
    body: bytes, protocol_key: str, glossary: dict, cap_records: list | None = None
) -> dict[str, dict]:
    """Decode all fields from a frame body for the given protocol key.

    For B0/B1 property frames, TLV records are parsed first. Fields whose
    decode steps carry a ``property_id`` are matched to the corresponding
    TLV record; ``offset``/``bits`` then apply relative to the record's
    data bytes rather than the frame body.

    Returns {field_name: {"value": decoded_value, "raw": raw_value}}.
    """
    encodings = glossary.get("encodings", {})
    field_map = build_field_map(glossary, protocol_key)

    # Pre-parse TLV for property-protocol frames
    record_by_prop: dict[str, bytes] | None = None
    if protocol_key in ("rsp_0xb1", "rsp_0xb0", "cmd_0xb0"):
        records = parse_b0b1_tlv(body)
        record_by_prop = {r["property_id"].lower(): bytes(r["data"]) for r in records}

    result = {}
    for field in field_map:
        decode_steps = field["decode"]

        # Determine the effective byte array for this field
        effective_body = body
        if record_by_prop is not None and decode_steps:
            prop_id = decode_steps[0].get("property_id")
            if prop_id:
                prop_data = record_by_prop.get(prop_id.lower())
                if prop_data is None:
                    continue  # property not present in this frame
                effective_body = prop_data

        decoded = decode_field(
            field["name"],
            decode_steps,
            field["data_type"],
            effective_body,
            encodings,
            capability=field.get("capability"),
            cap_records=cap_records,
        )
        if decoded.get("value") is not None:
            result[field["name"]] = decoded
    return result


# ── Frame spec builder + query planner (TODO §6 phase 2) ──────────────────
#
# Generic frame builder that consumes the top-level `frames:` dict from the
# glossary. Replaces the hardcoded builders in midea_frame.py. Per the §A.9
# codec contract (serial_glossary_guide.md Part 4), the glossary is the
# single source of truth for frame bytes: no literal byte offsets or field
# names in Python.


def build_frame_body_from_spec(
    frame_spec: dict,
    glossary: dict,
    status: dict | None = None,
    changes: dict | None = None,
) -> bytes:
    """Materialise the body bytes of a frame from its `frames.<id>` entry.

    Supports three body shapes from the schema's `frame_body` oneOf:
      - {length, bytes: [...]}        — literal, zero-padded to length
      - {length, bytes_at: {i: b}}    — sparse placement, rest zero-filled
      - {length?, assembled_from: k}  — build_command.build_command_body
                                        walks all cmd_k fields (for cmd_0x40,
                                        cmd_0xb0, etc. set frames). Requires
                                        `status` and optionally `changes`.

    Raises ValueError on malformed/unknown body shape. Does NOT wrap in the
    UART envelope — use build_frame_from_spec() for that.
    """
    body_spec = frame_spec.get("body") or {}

    if "bytes" in body_spec:
        length = int(body_spec["length"])
        bytes_list = body_spec["bytes"]
        if len(bytes_list) > length:
            raise ValueError(f"frame body bytes ({len(bytes_list)}) exceed declared length ({length})")
        body = bytearray(length)
        for i, b in enumerate(bytes_list):
            body[i] = int(b) & 0xFF
        return bytes(body)

    if "bytes_at" in body_spec:
        length = int(body_spec["length"])
        body = bytearray(length)
        for k, v in body_spec["bytes_at"].items():
            idx = int(k)
            if idx < 0 or idx >= length:
                raise ValueError(f"bytes_at index {idx} out of range for length {length}")
            body[idx] = int(v) & 0xFF
        return bytes(body)

    if "assembled_from" in body_spec:
        # Deferred import to avoid a top-level circular dependency:
        # build_command.py already imports from midea_codec, and we only
        # need it here for set-frame assembly.
        from blaueis.core.command import build_command_body

        if status is None:
            raise ValueError("frame_spec with assembled_from requires a current-state status dict")
        result = build_command_body(status, changes or {}, glossary)
        body = result["body"]
        declared_length = body_spec.get("length")
        if declared_length is not None and len(body) != int(declared_length):
            raise ValueError(
                f"assembled body length {len(body)} differs from declared length {declared_length} in frame_spec"
            )
        return bytes(body)

    raise ValueError(f"unrecognised frame_body shape: {sorted(body_spec.keys())}")


def build_frame_from_spec(
    frame_id: str,
    glossary: dict,
    appliance: int = 0xAC,
    proto: int = 0,
    sub: int = 0,
    seq: int = 0,
    status: dict | None = None,
    changes: dict | None = None,
) -> bytes:
    """Build a complete UART frame (header + body + CRC + checksum) from a
    frames[frame_id] entry.

    Looks up the spec in glossary['frames'], materialises the body via
    build_frame_body_from_spec, then delegates to midea_frame.build_frame
    for the envelope + CRC. The msg_type defaults to 0x03 unless the spec
    declares otherwise.
    """
    frames = glossary.get("frames") or {}
    if frame_id not in frames:
        raise KeyError(f"frame_id {frame_id!r} not found in glossary.frames")
    spec = frames[frame_id]

    body = build_frame_body_from_spec(spec, glossary, status=status, changes=changes)
    msg_type = spec.get("msg_type", 0x03)

    # Deferred import: midea_frame imports nothing from midea_codec, so this
    # is safe at call time. Placed here to keep the module top importable on
    # the Pi without pulling in the frame envelope helper for unit tests
    # that only exercise the body builder.
    from blaueis.core.frame import build_frame

    return build_frame(
        body=body,
        msg_type=msg_type,
        appliance=appliance,
        proto=proto,
        sub=sub,
        seq=seq,
    )


def _field_response_keys(glossary: dict, field_name: str) -> set[str]:
    """Return the set of rsp_* protocol keys the given field decodes from."""
    fields = walk_fields(glossary)
    fdef = fields.get(field_name)
    if not fdef:
        return set()
    keys = set()
    for pkey, ploc in (fdef.get("protocols") or {}).items():
        if pkey.startswith("rsp_") and ploc.get("decode"):
            keys.add(pkey)
    # Include capability.frames as well — some fields only update via cap frames.
    cap = fdef.get("capability") or {}
    for pkey in cap.get("frames") or {}:
        if pkey.startswith("rsp_"):
            keys.add(pkey)
    return keys


_FRAME_PRIORITY = {
    "cmd_0xb5": 0,  # B5 cap queries first (cap gating)
    "cmd_0x41_g": 2,  # group queries last
    "cmd_0x41": 1,  # standard C0 query between
}


def _frame_priority(fid: str) -> int:
    for prefix, rank in sorted(_FRAME_PRIORITY.items(), key=lambda kv: -len(kv[0])):
        if fid.startswith(prefix):
            return rank
    return 10


def plan_query_cycle(
    fields_to_poll: list[str] | set[str],
    glossary: dict,
    bus: str | None = None,
) -> list[str]:
    """Return a deduped, priority-ordered list of frame IDs that together
    refresh every field in fields_to_poll.

    For each target field: find all rsp_* keys it decodes from, then find
    any frame whose `triggers` list intersects those rsp_* keys. If `bus`
    is supplied, skip frames whose `bus` list doesn't include it (frames
    with no `bus` entry are assumed bus-agnostic).

    The planner picks the first matching frame per field — no cost
    optimisation yet. That's deferred to TODO §13 "later features".

    Order: B5 cap queries first, C0 standard second, C1 groups last.
    """
    frames = glossary.get("frames") or {}
    needed: set[str] = set()

    for field_name in fields_to_poll:
        rsp_keys = _field_response_keys(glossary, field_name)
        if not rsp_keys:
            continue
        for fid, spec in frames.items():
            frame_bus = spec.get("bus")
            if bus is not None and frame_bus and bus not in frame_bus:
                continue
            triggers = set(spec.get("triggers") or [])
            if rsp_keys & triggers:
                needed.add(fid)
                break  # first match wins

    return sorted(needed, key=lambda fid: (_frame_priority(fid), fid))


# ── Scan-queue orchestration (consumed by ac_monitor and tests) ───────────


# Frames that are bus-agnostic and always needed regardless of planner
# output. The live-monitor wants a fresh C0 snapshot every cycle even if
# no specific field asked for it; cmd_0x41 is also the fallback query
# that covers most control fields.
ALWAYS_QUEUE_FRAMES = ("cmd_0x41",)

# Frames that refresh capability state. Sent before plan_query_cycle is
# consulted so feature_available flags are up to date when the planner
# walks field targets.
CAP_QUERY_FRAMES = ("cmd_0xb5_extended", "cmd_0xb5_simple")


def target_field_names(status: dict) -> list[str]:
    """Return the field names in `status` whose feature_available != 'never'.

    Used as the default target set for plan_query_cycle when a live
    monitor wants to refresh every available field.
    """
    return [
        name
        for name, fstate in (status or {}).get("fields", {}).items()
        if fstate.get("feature_available", "always") != "never"
    ]


def build_scan_queue(
    status: dict,
    glossary: dict,
    bus: str,
    caps_finalized: bool,
    need_caps: bool,
    dead_frames: set[str] | None = None,
    proto: int = 0x02,
    appliance: int = 0xAC,
    sub: int = 0,
    seq: int = 0,
) -> list[tuple[str, bytes]]:
    """Build an ordered list of (label, frame_bytes) to send this scan cycle.

    Pure function — no websocket, no side effects, no logging. Testable
    in isolation; unit tests feed it a synthetic status dict and assert
    against frame IDs. ac_monitor.py wraps this in its async send loop.

    Order:
      1. B5 capability queries (if need_caps)
      2. C0 status query (always — covers most of the control surface)
      3. Group queries from plan_query_cycle() for any remaining fields

    `dead_frames` is a set of frame IDs the AC has proven not to respond
    to on this bus — typically populated after a few scan cycles by
    `detect_dead_frames()`. Frames in the set are silently skipped.

    See serial_glossary_guide.md Part 4 (validation strategy — send path)
    and TODO §6 for the rationale.
    """
    dead = dead_frames or set()
    queue: list[tuple[str, bytes]] = []
    seen: set[str] = set()

    def _append(label: str, fid: str):
        if fid in dead or fid in seen:
            return
        try:
            fb = build_frame_from_spec(fid, glossary, appliance=appliance, proto=proto, sub=sub, seq=seq)
        except Exception:
            # Silent skip — the caller can detect the missing frame by
            # comparing the return labels to its expected set, and the
            # test_frames_dict invariants guarantee the glossary is
            # well-formed. We avoid pulling a logger dependency here so
            # this function stays import-clean for tools-serial.
            return
        queue.append((label, fb))
        seen.add(fid)

    if need_caps:
        _append("B5 extended (0x00)", "cmd_0xb5_extended")
        _append("B5 simple (0x01)", "cmd_0xb5_simple")

    for fid in ALWAYS_QUEUE_FRAMES:
        _append("C0 status", fid)

    if caps_finalized:
        targets = target_field_names(status)
        plan = plan_query_cycle(targets, glossary, bus=bus)
        # Drop cap-discovery frames from the planner output when we're
        # not in a cap-refresh cycle. The planner includes them because
        # capability-gated fields list rsp_0xb5_tlv in their
        # `capability.frames` — a valid "refresh path" that the
        # orchestration layer handles separately via need_caps above.
        if not need_caps:
            plan = [fid for fid in plan if fid not in CAP_QUERY_FRAMES]
        fields_by_name = walk_fields(glossary)
        frames = glossary.get("frames") or {}
        for fid in plan:
            triggers = set(frames.get(fid, {}).get("triggers") or [])
            # Count fields whose protocols OR capability.frames intersect
            # this frame's triggers — capability-gated fields show up via
            # the latter and the label should reflect the full coverage.
            # Skip fields whose feature_available is 'never': they are
            # discarded at decode time by process_data_frame, so showing
            # them in the operator-facing label would inflate the count
            # past what actually populates state.
            covered = 0
            for fname, fdef in fields_by_name.items():
                fa = status["fields"].get(fname, {}).get("feature_available", "always")
                if fa == "never":
                    continue
                protos = set((fdef.get("protocols") or {}).keys())
                cap_frames = set(((fdef.get("capability") or {}).get("frames") or {}).keys())
                if triggers & (protos | cap_frames):
                    covered += 1
            _append(f"{fid} ({covered}f)", fid)

    return queue


def detect_dead_frames(glossary: dict, frame_counts: dict, bus: str) -> set[str]:
    """Return the set of group query frame IDs that haven't produced any
    response given the observed frame_counts.

    Walks `frames:` for any `cmd_0x41_group*` entry on the current bus
    and checks whether any of its `triggers` rsp keys appear in
    frame_counts. If not, the AC isn't responding — the caller should
    drop it from the scan queue.

    B5 and C0 queries are never marked dead: they're mandatory for
    capability discovery and status snapshots.
    """
    dead: set[str] = set()
    for fid, spec in (glossary.get("frames") or {}).items():
        if not fid.startswith("cmd_0x41_group"):
            continue
        frame_bus = spec.get("bus") or ["uart", "rt"]
        if bus not in frame_bus:
            continue  # planner already filters
        triggers = spec.get("triggers") or []
        if not triggers:
            continue
        if not any(rsp in frame_counts for rsp in triggers):
            dead.add(fid)
    return dead
