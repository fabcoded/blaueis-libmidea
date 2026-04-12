#!/usr/bin/env python3
"""Build an initial device status file from the serial glossary.

Creates device_status.json with all fields at their glossary defaults.
At boot:
  - `sources` is an empty dict; populated lazily by process_data_frame
    as each frame type decodes the field.
  - `default_priority` is the priority list field_query.read_field()
    consumes when no explicit priority is passed.
  - active_constraints is populated from capability.default for fields
    that declare one (TODO §3); null otherwise.

Usage:
    python build_status.py [--device "XtremeSaveBlue Q11"] [--output path/device_status.json]
"""

import json
from pathlib import Path

from blaueis.core.codec import load_glossary, walk_fields


def _initial_constraints(fdef: dict) -> dict | None:
    """Pull capability.default into active_constraints at boot.

    Returns None if no `default` block is defined. The default block is
    used for `readable` fields whose decoder works pre-B5 but whose write
    constraints are not yet known — until the device's capability scan
    provides field-specific constraint data, the field must fall back to
    the most permissive constraint set the protocol allows, otherwise
    valid frames decoded before the first B5 reply would be rejected.

    Once a real B5 capability arrives, process_b5() in process_frame.py
    overwrites this slot with the cap-resolved constraints.
    """
    cap = fdef.get("capability") or {}
    default = cap.get("default")
    if not default:
        return None
    out: dict = {}
    if "by_mode" in default:
        out["by_mode"] = default["by_mode"]
    for k in ("valid_set", "valid_range", "step", "correction"):
        if k in default:
            out[k] = default[k]
    return out or None


def build_status(device: str = "unknown", glossary: dict | None = None) -> dict:
    """Build an initial device status dict from glossary defaults."""
    if glossary is None:
        glossary = load_glossary()

    fields = walk_fields(glossary)
    status_fields = {}

    for name, fdef in fields.items():
        # Determine if writable: any cmd_* protocol entry with direction='command'.
        protocols = fdef.get("protocols", {}) or {}
        writable = any(isinstance(p, dict) and p.get("direction") == "command" for p in protocols.values())

        # Global constraints (always-applicable validation envelope)
        global_constraints = []
        if "constraints" in fdef:
            global_constraints = fdef["constraints"].get("global", [])

        status_fields[name] = {
            "feature_available": fdef.get("feature_available", "always"),
            "data_type": fdef.get("data_type"),
            "writable": writable,
            # Per-frame source slots — populated by process_data_frame as
            # each frame type decodes the field. Each slot is keyed by
            # the literal protocol_key and carries {value, raw, frame_no,
            # ts, generation}. read_field() walks this dict against a
            # priority list of scopes. Forever-keep, no eviction.
            "sources": {},
            # Per-field source priority. Used by read_field() when no
            # explicit priority is passed at call time. Glossary may
            # override per field; the global default is the most
            # permissive scope.
            "default_priority": fdef.get("default_priority", ["protocol_all"]),
            # Constraint envelope
            "active_constraints": _initial_constraints(fdef),
            "global_constraints": global_constraints,
        }

    return {
        "meta": {
            "device": device,
            "phase": "boot",
            "glossary_version": glossary["meta"]["version"],
            "b5_received": False,
            "frame_counts": {},
        },
        "fields": status_fields,
        "capabilities_raw": [],
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build initial device status file")
    parser.add_argument("--device", default="unknown", help="Device name")
    parser.add_argument("--output", default="device_status.json", help="Output path")
    args = parser.parse_args()

    status = build_status(device=args.device)

    output_path = Path(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)

    writable_count = sum(1 for f in status["fields"].values() if f["writable"])
    print(f"Built device status: {len(status['fields'])} fields ({writable_count} writable)")
    print(f"Phase: {status['meta']['phase']}")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
