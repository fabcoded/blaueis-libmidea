#!/usr/bin/env python3
"""Device quirks engine — per-device fixes for cap-derived state.

Quirks describe a device's actual behaviour (Linux kernel-style "quirks")
and are applied after process_b5 + finalize_capabilities. Two operations:

  1. feature_available overrides — directly set
     status['fields'][name]['feature_available'] for fields whose cap
     gating is wrong on this unit.
  2. synthesize_capabilities — inject synthetic B5 TLV records into
     status['capabilities_raw'] so the existing resolve_capability_encoding()
     picks the right encoding. Useful when a device under-reports its
     capability tier (e.g. Q11 reports cap 0x16=0 "no power calc" yet
     returns valid C1 group4 power frames).

Library API contract (frozen — used by future midea-protocol-lib + HA):

    apply_device_quirks(status, quirks, glossary) -> dict
        Pure function. No I/O. The only public entry point.

    DEVICE_QUIRKS_SCHEMA: dict
        The JSON Schema as a dict (loaded once from disk).

CLI convenience (not part of the lib core):

    load_device_quirks(path) -> dict
        File loader for ac_monitor + build_command CLI use.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml
from blaueis.core.codec import walk_fields
from blaueis.core.process import _apply_caps_to_fields
from jsonschema import Draft202012Validator

# ── Schema (loaded once at import time) ──────────────────────────────────

_SCHEMA_PATH = Path(__file__).resolve().parent / "data" / "device_quirks_schema.json"


def _load_schema() -> dict:
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


DEVICE_QUIRKS_SCHEMA: dict = _load_schema()
_VALIDATOR = Draft202012Validator(DEVICE_QUIRKS_SCHEMA)


# ── Public library API (pure function — no file I/O) ────────────────────


def apply_device_quirks(
    status: dict,
    quirks: dict,
    glossary: dict,
) -> dict:
    """Apply a device quirks dict to a finalized status, in-place.

    Order of operations:

      1. Validate the quirks dict against DEVICE_QUIRKS_SCHEMA. Raise
         ValueError on schema violation.
      2. For each (field_name, fa) in quirks.get('feature_available', {}):
         - Verify the field exists in the glossary (raise ValueError if not)
         - Set status['fields'][field_name]['feature_available'] = fa
      3. For each cap_entry in quirks.get('synthesize_capabilities', []):
         - Build a synthetic TLV record matching the parse_b5_tlv shape
         - If the cap_id is already in status['capabilities_raw'] and the
           entry doesn't set force=true, skip it (real device data wins)
         - Otherwise append (or replace if force) the record
      4. Re-derive cap-gated state by calling _apply_caps_to_fields() on
         the synthesized records. The feature_available overrides from
         step 2 are then re-applied so user intent wins over re-derivation.

    Args:
        status: A status dict from build_status() + process_b5 +
            finalize_capabilities.
        quirks: A quirks dict matching DEVICE_QUIRKS_SCHEMA.
        glossary: The serial glossary dict.

    Returns:
        Report dict with structure:
            {
                "name": str,           # quirks['name']
                "fields_overridden": list[str],
                "caps_synthesized": list[str],   # cap_ids that were applied
                "caps_skipped": list[str],       # cap_ids skipped because real cap exists
            }

    Raises:
        ValueError: schema validation failure or unknown field name.
    """
    # 1. Schema validation
    errors = list(_VALIDATOR.iter_errors(quirks))
    if errors:
        msg = "device quirks failed schema validation:\n" + "\n".join(
            f"  {list(e.absolute_path)}: {e.message}" for e in errors
        )
        raise ValueError(msg)

    report: dict[str, Any] = {
        "name": quirks.get("name", "<unnamed>"),
        "fields_overridden": [],
        "caps_synthesized": [],
        "caps_skipped": [],
    }

    glossary_fields = walk_fields(glossary)

    # 2. feature_available overrides — track to re-apply after step 4
    fa_overrides: dict[str, str] = quirks.get("feature_available", {}) or {}
    for field_name, fa in fa_overrides.items():
        if field_name not in glossary_fields:
            raise ValueError(f"device quirks references unknown field {field_name!r} in feature_available block")
        status_field = status["fields"].get(field_name)
        if status_field is None:
            raise ValueError(f"device quirks references field {field_name!r} that is missing from status['fields']")
        status_field["feature_available"] = fa
        report["fields_overridden"].append(field_name)

    # 3. Synthesize capabilities — append/replace records in capabilities_raw
    caps_raw = status.setdefault("capabilities_raw", [])
    existing_cap_ids = {r["cap_id"].lower(): i for i, r in enumerate(caps_raw)}

    synthesized_records: list[dict] = []
    for entry in quirks.get("synthesize_capabilities", []) or []:
        cap_id = entry["cap_id"]
        cap_id_lower = cap_id.lower()
        cap_type = entry.get("cap_type", 0x02)
        data = list(entry["data"])
        force = entry.get("force", False)

        if cap_id_lower in existing_cap_ids and not force:
            # Real device data wins
            report["caps_skipped"].append(cap_id)
            continue

        # Build a synthetic record matching parse_b5_tlv()'s output shape
        record = {
            "cap_id": f"0x{int(cap_id_lower, 16):02X}",
            "cap_type": cap_type,
            "key_16": f"0x{cap_type:02X}{int(cap_id_lower, 16):02X}",
            "data_len": len(data),
            "data": data,
            "data_hex": bytes(data).hex(),
        }

        if cap_id_lower in existing_cap_ids:
            # Replace in place to preserve order
            caps_raw[existing_cap_ids[cap_id_lower]] = record
        else:
            caps_raw.append(record)
            existing_cap_ids[cap_id_lower] = len(caps_raw) - 1

        synthesized_records.append(record)
        report["caps_synthesized"].append(cap_id)

    # 4. Re-derive field state from the synthesized records, then re-apply
    # the feature_available overrides so user intent wins.
    if synthesized_records:
        _apply_caps_to_fields(status, synthesized_records, glossary)
    for field_name, fa in fa_overrides.items():
        status["fields"][field_name]["feature_available"] = fa

    return report


# ── CLI convenience (not part of library core) ──────────────────────────


def load_device_quirks(path: Path | str) -> dict:
    """Load and parse a device quirks YAML file.

    This is a convenience helper for CLI tools that want file-based
    loading. The library core (apply_device_quirks) takes pre-loaded
    dicts; consumers like ac_monitor and build_command use this helper
    to load files passed via --quirks flags.
    """
    p = Path(path)
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def apply_quirks_files(
    status: dict,
    quirks_paths: list[Path | str],
    glossary: dict,
) -> list[dict]:
    """Convenience: load each path and apply in order. Returns list of reports."""
    reports = []
    for path in quirks_paths:
        quirks = load_device_quirks(path)
        # Defensive copy so the loaded dict isn't mutated by validation/apply
        report = apply_device_quirks(status, copy.deepcopy(quirks), glossary)
        report["source"] = str(path)
        reports.append(report)
    return reports
