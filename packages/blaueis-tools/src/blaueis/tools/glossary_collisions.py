"""Glossary bit/byte position collision detector.

Walks every field's decode steps and builds a per-scope catalogue of
which (byte_offset, bit) slots each field claims. Each frame type has
its own scoping rule because the offset namespace differs:

  - **flat-body frames** (cmd_0x40, rsp_0xc0, rsp_0xa1, rsp_0xc1_groupN
    etc.): single body buffer; scope = frame_id.
  - **TLV property frames** (rsp_0xb1, cmd_0xb0): each TLV record has
    its own data buffer; scope = (frame_id, decode-step.property_id).
  - **TLV capability frames** (rsp_0xb5*, capability.decode): each cap
    record has its own data buffer; scope = (frame_id, capability.cap_id).

Public API:

  :func:`build_position_catalogue` — return a list of per-bit claim
  records (the "database").

  :func:`find_collisions` — return un-allowlisted same-bit collisions.

  :func:`find_shared_bytes` — return non-collision byte sharing
  (different fields, different bits, same byte) for informational
  context during signoff review.

  :func:`load_allowlist` — read a YAML allowlist file. Each entry:
  ``{scope, byte, bit, fields, reason, ...}``. Match key is
  ``(scope, byte, bit, sorted(fields))``.

The CLI lives at workspace root in ``check_field_collisions.py`` and
imports from this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

# ── Frame-scope taxonomy ──────────────────────────────────────────────
# Maps a frame_id prefix to its scoping rule. Each rule explains what
# defines an *independent byte-offset namespace*. New frame types added
# later need a one-line entry here. Order matters: more-specific
# prefixes first.

SCOPE_FLAT_BODY = "flat_body"          # scope = frame_id
SCOPE_PROPERTY_TLV = "property_tlv"    # scope = (frame_id, property_id)
SCOPE_CAPABILITY = "capability_tlv"    # scope = (frame_id, cap_id)
SCOPE_CAP_DECODE = "cap_decode"        # scope = (cap_id, sub_name)

FRAME_SCOPE_RULES: list[tuple[str, str, str]] = [
    # (prefix, scope_type, class)
    ("rsp_0xb1",      SCOPE_PROPERTY_TLV, "read"),
    ("cmd_0xb0",      SCOPE_PROPERTY_TLV, "write"),
    ("cmd_0xb1",      SCOPE_PROPERTY_TLV, "write"),
    ("rsp_0xb5_tlv",  SCOPE_CAPABILITY,   "cap"),
    ("rsp_0xb5",      SCOPE_CAPABILITY,   "cap"),
    ("cmd_0x",        SCOPE_FLAT_BODY,    "write"),
    ("rsp_0x",        SCOPE_FLAT_BODY,    "read"),
]


def classify_frame(frame_id: str) -> tuple[str, str] | None:
    """Return (scope_type, class) for a frame_id, or None if unknown."""
    for prefix, scope_type, cls in FRAME_SCOPE_RULES:
        if frame_id.startswith(prefix):
            return scope_type, cls
    return None


# ── Decode-step walker ────────────────────────────────────────────────


def _walk_decode_steps(steps) -> Iterable[dict]:
    """Yield each non-logic decode step with offset/bits set. Skips
    logic-combiner steps (which read multiple bits, don't claim them)
    and malformed entries silently."""
    if not steps:
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        if "logic" in step:
            continue
        offset = step.get("offset")
        bits = step.get("bits")
        if offset is None or not bits or len(bits) != 2:
            continue
        high, low = bits
        if not isinstance(high, int) or not isinstance(low, int):
            continue
        yield step


# ── Position catalogue (the "database") ───────────────────────────────


def build_position_catalogue(glossary: dict) -> list[dict]:
    """Walk the glossary and return one record per (scope, byte, bit) claim.

    Each record carries enough context to filter and group:

      .. code-block:: python

         {
           "field": "fan_speed",
           "field_class": "stateful_enum",
           "feature_available": "readable",
           "class": "write",                    # read | write | cap
           "frame": "cmd_0x40",
           "scope_type": "flat_body",
           "scope_key": "cmd_0x40",
           "byte": 3,
           "bit_high": 6,
           "bit_low": 0,
           "bit": 0,                            # one record per bit
           "encoding": "uint8",
           "property_id": None,
           "cap_id": None,
         }

    Per-bit expansion: a field decoding bits[6:0] of body[3] yields 7
    records (one per bit). Makes collision detection trivial — group by
    (scope_key, byte, bit) and check for >1 distinct fields.
    """
    records: list[dict] = []

    def _emit(field: str, fdef: dict, scope_type: str, cls: str,
              frame_id: str, scope_extra: str, step: dict,
              property_id: str | None = None,
              cap_id: str | None = None) -> None:
        offset = step["offset"]
        high, low = step["bits"]
        scope_key = frame_id if not scope_extra else f"{frame_id}/{scope_extra}"
        for bit in range(low, high + 1):
            records.append({
                "field": field,
                "field_class": fdef.get("field_class"),
                "feature_available": fdef.get("feature_available"),
                "class": cls,
                "frame": frame_id,
                "scope_type": scope_type,
                "scope_key": scope_key,
                "byte": offset,
                "bit_high": high,
                "bit_low": low,
                "bit": bit,
                "encoding": step.get("encoding"),
                "property_id": property_id,
                "cap_id": cap_id,
            })

    fields_root = glossary.get("fields") or {}
    for sec in ("sensor", "control"):
        for fname, fdef in (fields_root.get(sec) or {}).items():
            if not isinstance(fdef, dict):
                continue

            # protocols.* — flat-body and property-TLV frames.
            for pkey, pdef in (fdef.get("protocols") or {}).items():
                if not isinstance(pdef, dict):
                    continue
                rule = classify_frame(pkey)
                if rule is None:
                    continue
                scope_type, cls = rule
                for step in _walk_decode_steps(pdef.get("decode")):
                    if scope_type == SCOPE_PROPERTY_TLV:
                        prop = step.get("property_id")
                        if prop is None:
                            _emit(fname, fdef, scope_type, cls, pkey,
                                  scope_extra="<missing-property_id>",
                                  step=step)
                        else:
                            _emit(fname, fdef, scope_type, cls, pkey,
                                  scope_extra=f"prop={prop}", step=step,
                                  property_id=prop)
                    else:
                        _emit(fname, fdef, scope_type, cls, pkey,
                              scope_extra="", step=step)

            # capability.frames.* / capability.decode.* — scope by cap_id.
            cap = fdef.get("capability")
            if isinstance(cap, dict):
                cap_id = cap.get("cap_id") or cap.get("cap_id_16") or "<unknown_cap>"
                for fkey, fdata in (cap.get("frames") or {}).items():
                    if not isinstance(fdata, dict):
                        continue
                    for step in _walk_decode_steps(fdata.get("decode")):
                        _emit(fname, fdef, SCOPE_CAPABILITY, "cap", fkey,
                              scope_extra=f"cap={cap_id}", step=step,
                              cap_id=cap_id)
                for subname, sub in (cap.get("decode") or {}).items():
                    if not isinstance(sub, dict):
                        continue
                    for step in _walk_decode_steps(sub.get("decode")):
                        _emit(fname, fdef, SCOPE_CAP_DECODE, "cap",
                              "capability.decode",
                              scope_extra=f"cap={cap_id}/{subname}",
                              step=step, cap_id=cap_id)

    return records


# ── Collision detection ───────────────────────────────────────────────


def find_collisions(catalogue: list[dict],
                    allow: list[dict] | None = None) -> list[dict]:
    """Return un-allowlisted same-bit collisions.

    Each result entry:

      .. code-block:: python

         {
           "scope_key": "rsp_0xc0",
           "byte": 10,
           "bit": 6,
           "fields": ["dust_full", "peak_elec"],
           "class": "read",
           "frame": "rsp_0xc0",
           "scope_type": "flat_body",
         }

    Allowlist match key: ``(scope_key, byte, bit, tuple(sorted(fields)))``.
    """
    by_slot: dict[tuple[str, int, int], list[dict]] = {}
    for r in catalogue:
        key = (r["scope_key"], r["byte"], r["bit"])
        by_slot.setdefault(key, []).append(r)

    allow_keys: set[tuple[str, int, int, tuple[str, ...]]] = set()
    if allow:
        for entry in allow:
            try:
                fields_t = tuple(sorted(entry["fields"]))
                allow_keys.add(
                    (entry["scope"], entry["byte"], entry["bit"], fields_t)
                )
            except (KeyError, TypeError):
                continue

    out: list[dict] = []
    for (scope, byte, bit), recs in by_slot.items():
        owners = sorted({r["field"] for r in recs})
        if len(owners) <= 1:
            continue
        if (scope, byte, bit, tuple(owners)) in allow_keys:
            continue
        out.append({
            "scope_key": scope,
            "byte": byte,
            "bit": bit,
            "fields": owners,
            "class": recs[0]["class"],
            "frame": recs[0]["frame"],
            "scope_type": recs[0]["scope_type"],
        })
    out.sort(key=lambda r: (r["class"], r["scope_key"], r["byte"], r["bit"]))
    return out


def find_shared_bytes(catalogue: list[dict]) -> list[dict]:
    """Return non-collision byte sharing — different fields claiming
    different bits within the same byte. Informational: encoder handles
    these correctly via insert_bits(). Useful when reviewing a field's
    neighbours during signoff."""
    by_byte_owners: dict[tuple[str, int], set[str]] = {}
    bit_owners: dict[tuple[str, int], dict[int, list[str]]] = {}
    for r in catalogue:
        bk = (r["scope_key"], r["byte"])
        by_byte_owners.setdefault(bk, set()).add(r["field"])
        bit_owners.setdefault(bk, {}).setdefault(r["bit"], []).append(r["field"])

    out: list[dict] = []
    for (scope, byte), owners in by_byte_owners.items():
        owners_sorted = sorted(owners)
        if len(owners_sorted) < 2:
            continue
        per_bit = bit_owners[(scope, byte)]
        if any(len(set(v)) > 1 for v in per_bit.values()):
            continue  # has same-bit collisions; covered by find_collisions
        out.append({
            "scope_key": scope,
            "byte": byte,
            "owners": owners_sorted,
            "bit_layout": {b: sorted(set(v)) for b, v in sorted(per_bit.items())},
        })
    out.sort(key=lambda r: (r["scope_key"], r["byte"]))
    return out


# ── Allowlist loader ──────────────────────────────────────────────────


def load_allowlist(path: Path | str | None) -> list[dict]:
    """Load allowlisted-collision entries from a YAML file.

    Each entry: ``{scope: str, byte: int, bit: int, fields: [str], reason: str, ...}``.
    Returns ``[]`` for missing / empty / malformed files. Missing keys
    on individual entries are silently ignored at match time."""
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text())
    if not isinstance(data, dict):
        return []
    entries = data.get("allowed_collisions") or []
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]
