#!/usr/bin/env python3
"""Category boundary audit — gates TODO §1.

Asserts that the control/sensor split in the glossary is consistent
with each field's protocol entries:

  - control: must have at least one cmd_* entry with direction='command'.
    Otherwise it is read-only and belongs in sensor.
  - sensor: must NOT have any cmd_* entry with direction='command'. A
    field with a settable command belongs in control.

We deliberately check on direction='command' rather than on a fixed
key prefix list because some fields (e.g. screen_display) are settable
through a dedicated sub-command frame (cmd_0x41_display) instead of
cmd_0x40 / cmd_0xb0.

This is a one-shot validator, not a refactor — if it passes, §1 is
done correctly. If it fails, the report names the offending fields so
the data can be moved.

Usage:
    python tests/test_category_boundary.py
"""

import sys

from blaueis.core.codec import load_glossary


def is_settable_protocol_entry(key: str, ploc: dict) -> bool:
    """A protocol entry is 'settable' if it is a command frame.

    Recognised by either:
      - direction == 'command' (authoritative), or
      - key starts with 'cmd_' (fallback for entries missing direction).

    Query frames (cmd_0x41 with direction='query') are NOT counted —
    they trigger a response, they do not modify state.
    """
    direction = ploc.get("direction")
    if direction == "command":
        return True
    # Defensive fallback — should not happen for well-formed entries.
    return direction is None and key.startswith("cmd_")


def main():
    glossary = load_glossary()
    fields = glossary.get("fields", {})

    passed = 0
    failed = 0
    misplaced_in_sensor: list[tuple[str, list[str]]] = []
    misplaced_in_control: list[str] = []

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}: {detail}")

    # Top-level structure: only control + sensor
    top_keys = set(fields.keys())
    check(
        "fields has only control + sensor categories",
        top_keys == {"control", "sensor"},
        f"got {top_keys}",
    )

    # Audit sensor: no settable command entries allowed
    sensor = fields.get("sensor", {})
    for fname, fdef in sensor.items():
        if not isinstance(fdef, dict) or "description" not in fdef:
            continue
        protos = fdef.get("protocols", {}) or {}
        settable = [k for k, ploc in protos.items() if isinstance(ploc, dict) and is_settable_protocol_entry(k, ploc)]
        if settable:
            misplaced_in_sensor.append((fname, settable))

    check(
        "no sensor field has a settable command entry (settable means wrong category)",
        len(misplaced_in_sensor) == 0,
        f"misplaced: {misplaced_in_sensor}",
    )

    # Audit control: every field should have at least one settable command
    control = fields.get("control", {})
    for fname, fdef in control.items():
        if not isinstance(fdef, dict) or "description" not in fdef:
            continue
        protos = fdef.get("protocols", {}) or {}
        has_settable = any(is_settable_protocol_entry(k, ploc) for k, ploc in protos.items() if isinstance(ploc, dict))
        if not has_settable:
            misplaced_in_control.append(fname)

    check(
        "every control field has at least one settable command entry",
        len(misplaced_in_control) == 0,
        f"read-only fields in control: {misplaced_in_control}",
    )

    # Field counts (sanity check)
    sensor_fields = [n for n, v in sensor.items() if isinstance(v, dict) and "description" in v]
    control_fields = [n for n, v in control.items() if isinstance(v, dict) and "description" in v]
    total = len(sensor_fields) + len(control_fields)
    check(
        "total field count == 200 (118 sensor + 82 control)",
        total == 200,
        f"got {total} (sensor={len(sensor_fields)}, control={len(control_fields)})",
    )

    print()
    print(f"  control: {len(control_fields)} fields")
    print(f"  sensor:  {len(sensor_fields)} fields")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {passed + failed} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
