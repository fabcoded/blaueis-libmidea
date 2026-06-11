#!/usr/bin/env python3
"""Unit tests for build_status.py — validates the initial device status structure.

No fixture needed — tests against the glossary directly.

Usage:
    python test_build_status.py
"""

import sys
from collections import Counter

from blaueis.core.codec import load_glossary
from blaueis.core.status import build_status


def main():
    glossary = load_glossary()
    status = build_status(device="test_device", glossary=glossary)

    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}: {detail}")

    fields = status["fields"]
    meta = status["meta"]

    # Meta checks
    check("meta.phase == boot", meta["phase"] == "boot", f"got {meta['phase']}")
    check("meta.b5_received == false", meta["b5_received"] is False)
    check("meta.device set", meta["device"] == "test_device")
    check("meta.frame_counts empty", meta["frame_counts"] == {})
    check("meta.glossary_version present", "glossary_version" in meta)

    # Field count — 108 baseline + 44 Session 15 known + 8 unknown (G7+G12) +
    # 1 jet_cool + 3 Group 0 day counters + 36 bulk B0/B1 properties (Tier 1-4).
    check("total fields == 198", len(fields) == 198, f"got {len(fields)}")

    # Writable count: every field with at least one cmd_* protocol entry
    # whose direction is 'command'. This is the same predicate the
    # category boundary audit uses (test_category_boundary.py); it
    # includes screen_display (cmd_0x41_display sub-cmd) and every
    # cmd_0xb0-only field, neither of which the legacy
    # 'cmd_0x40 in protocols' check picked up.
    writable = [n for n, f in fields.items() if f["writable"]]
    check(
        "writable fields == 83 (matches control category count)",
        len(writable) == 83,
        f"got {len(writable)}: {sorted(writable)}",
    )

    # feature_available distribution — snapshot of the current glossary (sums to
    # 198): always 11, readable 63, capability 76, excluded 38, readable-opt 10.
    # The extensive exclusion work (vane fields, group-unknown bytes,
    # decode-unverified/never-observed, etc.) moved many fields
    # readable -> excluded/capability over time; every excluded field carries a
    # schema-enforced excluded_reasons entry.
    fa_counts = Counter(f["feature_available"] for f in fields.values())
    check("always == 11", fa_counts["always"] == 11, f"got {fa_counts.get('always', 0)}")
    check("readable == 63", fa_counts["readable"] == 63, f"got {fa_counts.get('readable', 0)}")
    check("capability == 76", fa_counts["capability"] == 76, f"got {fa_counts.get('capability', 0)}")
    check("excluded == 38", fa_counts["excluded"] == 38, f"got {fa_counts.get('excluded', 0)}")

    # Required keys on every field. The new shape is flat: sources +
    # default_priority back the per-frame storage; the rest are
    # field-level metadata + constraint envelopes. No more from_legacy /
    # from_new / overlay / current_value mirror keys.
    required_keys = {
        "feature_available",
        "data_type",
        "writable",
        "sources",
        "default_priority",
        "active_constraints",
        "global_constraints",
    }
    missing_keys = []
    for name, f in fields.items():
        missing = required_keys - set(f.keys())
        if missing:
            missing_keys.append(f"{name}: {missing}")
    check("all fields have required keys", len(missing_keys) == 0, f"missing: {missing_keys[:3]}")

    # Per-frame source storage — every field starts with an empty
    # sources dict and the global default priority list. Slots are
    # populated lazily by process_data_frame as frames decode the field.
    sources_problems = []
    for name, f in fields.items():
        if f["sources"] != {}:
            sources_problems.append(f"{name}.sources non-empty at boot: {f['sources']}")
        if f["default_priority"] != ["protocol_all"]:
            sources_problems.append(f"{name}.default_priority = {f['default_priority']}")
    check(
        "all sources dicts empty + default_priority == ['protocol_all'] at boot",
        len(sources_problems) == 0,
        f"problems: {sources_problems[:3]}",
    )

    # Legacy keys must be GONE — fail loudly if any field still carries
    # current_value / last_updated / source_frame / from_legacy / etc.
    legacy_keys = {"current_value", "last_updated", "source_frame", "last_raw", "from_legacy", "from_new", "overlay"}
    leaks = []
    for name, f in fields.items():
        leaked = legacy_keys & set(f.keys())
        if leaked:
            leaks.append(f"{name}: {leaked}")
    check("no legacy bucket / mirror keys remain", len(leaks) == 0, f"leaks: {leaks[:3]}")

    # Specific fields have global_constraints
    check(
        "target_temperature has global_constraints",
        len(fields["target_temperature"]["global_constraints"]) >= 2,
        f"got {len(fields['target_temperature']['global_constraints'])}",
    )
    check(
        "fan_speed has global_constraints",
        len(fields["fan_speed"]["global_constraints"]) >= 1,
        f"got {len(fields['fan_speed']['global_constraints'])}",
    )

    # capabilities_raw is empty list
    check("capabilities_raw empty", status["capabilities_raw"] == [])

    # Never fields
    never_fields = [n for n, f in fields.items() if f["feature_available"] == "excluded"]
    expected_never = {
        "current_session_time",
        "current_work_time",
        "fan_speed_timer_bit",
        "group12_unknown_byte4",
        "group12_unknown_byte6",
        "group7_unknown_byte10",
        "group7_unknown_byte11",
        "group7_unknown_byte5",
        "group7_unknown_byte6",
        "group7_unknown_byte7",
        "group7_unknown_byte8",
        "humidity_actual",
        "humidity_measured",
        "ipm_module_temp",
        "local_body_sense",
        "night_light",
        "outdoor_return_air_temp",
        "peak_elec",
        "pm25_concentration",
        "power_off_time_value",
        "power_off_timer",
        "power_on_time",
        "power_on_time_value",
        "power_on_timer",
        "protocol_bit1",
        "rate_select",
        "total_worked_time",
        "vane_lr_angle",
        "vane_lr_lower",
        "vane_lr_status",
        "vane_lr_upper",
        "vane_top_status",
        "vane_ud_angle",
        "vane_ud_cool_lower",
        "vane_ud_cool_upper",
        "vane_ud_heat_lower",
        "vane_ud_heat_upper",
        "vane_ud_status",
    }
    check("never fields correct", set(never_fields) == expected_never, f"got {never_fields}")

    # Summary
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
