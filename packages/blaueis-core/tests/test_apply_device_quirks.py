#!/usr/bin/env python3
"""Tests for apply_device_quirks — per-device protocol quirks engine.

Quirks describe a device's actual behaviour that the protocol can't
express. They run after process_b5 + finalize_capabilities and override
cap-derived state on a per-field basis, plus optionally synthesize fake
B5 cap records to drive encoding selection.

Library API contract being tested:
- apply_device_quirks(status, quirks, glossary) → report dict
- Pure function, no I/O
- Schema validation rejects malformed inputs

Usage:
    python tests/test_apply_device_quirks.py
"""

import sys
from pathlib import Path

import yaml
from blaueis.core.codec import decode_frame_fields, load_glossary
from blaueis.core.quirks import (
    DEVICE_QUIRKS_SCHEMA,
    apply_device_quirks,
    apply_quirks_files,
    load_device_quirks,
)
from blaueis.core.status import build_status

TESTS_DIR = Path(__file__).resolve().parent
SPEC_DIR = TESTS_DIR.parent / "src" / "blaueis" / "core" / "data"
Q11_QUIRKS = SPEC_DIR / "device_quirks" / "xtremesaveblue_q11_power.yaml"

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}: {detail}")


# ── Test 1: schema is loaded ─────────────────────────────────────────────


def test_schema_loaded():
    print("\n1. Schema loaded at import time")
    check(
        "DEVICE_QUIRKS_SCHEMA is a dict",
        isinstance(DEVICE_QUIRKS_SCHEMA, dict),
        f"got {type(DEVICE_QUIRKS_SCHEMA)}",
    )
    check(
        "schema has $schema and properties",
        "$schema" in DEVICE_QUIRKS_SCHEMA and "properties" in DEVICE_QUIRKS_SCHEMA,
        "missing required schema fields",
    )


# ── Test 2: minimal valid quirks parses ─────────────────────────────────


def test_loads_valid_quirks():
    print("\n2. Loads minimal valid quirks")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    quirks = {"name": "minimal", "feature_available": {}}
    report = apply_device_quirks(status, quirks, glossary)
    check("returns report dict", isinstance(report, dict), "")
    check("report.name = minimal", report["name"] == "minimal", f"got {report['name']}")
    check(
        "report has all required keys",
        {"fields_overridden", "caps_synthesized", "caps_skipped"}.issubset(report.keys()),
        f"got {sorted(report.keys())}",
    )


# ── Test 3: rejects unknown field ────────────────────────────────────────


def test_rejects_unknown_field():
    print("\n3. Rejects unknown field name")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    quirks = {"name": "bad", "feature_available": {"nonexistent_field": "readable"}}
    try:
        apply_device_quirks(status, quirks, glossary)
        check("raised ValueError", False, "no exception")
    except ValueError as e:
        check("raised ValueError", True, "")
        check(
            "error message mentions field name",
            "nonexistent_field" in str(e),
            f"got: {e}",
        )


# ── Test 4: schema rejects invalid feature_available value ──────────────


def test_rejects_invalid_fa_value():
    print("\n4. Schema rejects invalid feature_available value")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    quirks = {"name": "bad", "feature_available": {"power": "maybe"}}
    try:
        apply_device_quirks(status, quirks, glossary)
        check("raised ValueError", False, "no exception")
    except ValueError as e:
        check("raised ValueError", True, "")
        check(
            "error mentions schema validation",
            "schema validation" in str(e),
            f"got: {e}",
        )


# ── Test 5: schema rejects invalid cap_id format ────────────────────────


def test_rejects_invalid_cap_id():
    print("\n5. Schema rejects cap_id without 0x prefix")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    quirks = {
        "name": "bad",
        "synthesize_capabilities": [{"cap_id": "16", "data": [2]}],
    }
    try:
        apply_device_quirks(status, quirks, glossary)
        check("raised ValueError", False, "")
    except ValueError as e:
        check("raised ValueError", True, "")
        check("mentions schema validation", "schema validation" in str(e), f"got: {e}")


# ── Test 6: feature_available override applies ──────────────────────────


def test_fa_override_applies():
    print("\n6. feature_available override updates status")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    # power starts at 'always' in glossary
    original_fa = status["fields"]["power"]["feature_available"]
    quirks = {"name": "fa-test", "feature_available": {"power": "readable"}}
    report = apply_device_quirks(status, quirks, glossary)
    check(
        "status.fields.power.feature_available = readable",
        status["fields"]["power"]["feature_available"] == "readable",
        f"got {status['fields']['power']['feature_available']}",
    )
    check(
        "report.fields_overridden lists power",
        "power" in report["fields_overridden"],
        f"got {report['fields_overridden']}",
    )
    # Restore (in case test runner reuses state — defensive)
    status["fields"]["power"]["feature_available"] = original_fa


# ── Test 7: synthesized cap appends to capabilities_raw ────────────────


def test_synthesized_cap_appended():
    print("\n7. Synthesized cap appended to capabilities_raw")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    # build_status starts with empty capabilities_raw
    assert status.get("capabilities_raw") == []
    quirks = {
        "name": "syn-test",
        "synthesize_capabilities": [{"cap_id": "0x16", "data": [4]}],
    }
    report = apply_device_quirks(status, quirks, glossary)
    caps = status["capabilities_raw"]
    check("capabilities_raw has 1 entry", len(caps) == 1, f"got {len(caps)}")
    check("cap_id = 0x16", caps[0]["cap_id"] == "0x16", f"got {caps[0]['cap_id']}")
    check("data = [4]", caps[0]["data"] == [4], f"got {caps[0]['data']}")
    check("data_len = 1", caps[0]["data_len"] == 1, f"got {caps[0]['data_len']}")
    check(
        "report.caps_synthesized lists 0x16",
        "0x16" in report["caps_synthesized"],
        f"got {report['caps_synthesized']}",
    )


# ── Test 8: real cap wins over synthesis (default) ──────────────────────


def test_real_cap_wins_over_synthesis():
    print("\n8. Real device cap wins over synthesis (default)")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    # Pre-populate as if a real B5 frame had set cap 0x16=0
    status["capabilities_raw"] = [
        {
            "cap_id": "0x16",
            "cap_type": 2,
            "key_16": "0x0216",
            "data_len": 1,
            "data": [0],
            "data_hex": "00",
        }
    ]
    quirks = {
        "name": "syn-loses",
        "synthesize_capabilities": [{"cap_id": "0x16", "data": [4]}],
    }
    report = apply_device_quirks(status, quirks, glossary)
    check(
        "real cap 0x16=0 still present",
        status["capabilities_raw"][0]["data"] == [0],
        f"got {status['capabilities_raw'][0]['data']}",
    )
    check(
        "report.caps_skipped lists 0x16",
        "0x16" in report["caps_skipped"],
        f"got {report['caps_skipped']}",
    )
    check(
        "caps_synthesized empty",
        report["caps_synthesized"] == [],
        f"got {report['caps_synthesized']}",
    )


# ── Test 9: force=true overrides real cap ───────────────────────────────


def test_force_overrides_real_cap():
    print("\n9. force=true overrides real device cap")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    status["capabilities_raw"] = [
        {
            "cap_id": "0x16",
            "cap_type": 2,
            "key_16": "0x0216",
            "data_len": 1,
            "data": [0],
            "data_hex": "00",
        }
    ]
    quirks = {
        "name": "force-wins",
        "synthesize_capabilities": [{"cap_id": "0x16", "data": [4], "force": True}],
    }
    report = apply_device_quirks(status, quirks, glossary)
    check(
        "real cap 0x16 replaced with [4]",
        status["capabilities_raw"][0]["data"] == [4],
        f"got {status['capabilities_raw'][0]['data']}",
    )
    check(
        "report.caps_synthesized includes 0x16",
        "0x16" in report["caps_synthesized"],
        f"got {report['caps_synthesized']}",
    )


# ── Test 10: multiple quirks compose ────────────────────────────────────


def test_multiple_quirks_compose():
    print("\n10. Multiple quirks compose, last writer wins on conflicts")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    q1 = {"name": "q1", "feature_available": {"power": "readable"}}
    q2 = {"name": "q2", "feature_available": {"power": "always", "follow_me": "readable"}}
    apply_device_quirks(status, q1, glossary)
    check("after q1: power=readable", status["fields"]["power"]["feature_available"] == "readable", "")
    apply_device_quirks(status, q2, glossary)
    check("after q2: power=always (q2 overrode)", status["fields"]["power"]["feature_available"] == "always", "")
    check(
        "after q2: follow_me=readable",
        status["fields"]["follow_me"]["feature_available"] == "readable",
        "",
    )


# ── Test 11: load_device_quirks loads YAML from disk ────────────────────


def test_load_device_quirks_helper():
    print("\n11. load_device_quirks loads YAML file")
    quirks = load_device_quirks(Q11_QUIRKS)
    check("loaded dict", isinstance(quirks, dict), f"got {type(quirks)}")
    check(
        "name = XtremeSaveBlue Q11 power monitoring",
        "Q11" in quirks.get("name", ""),
        f"got {quirks.get('name')}",
    )
    check(
        "has 4 feature_available overrides",
        len(quirks.get("feature_available", {})) == 4,
        f"got {len(quirks.get('feature_available', {}))}",
    )
    check(
        "synthesize_capabilities has cap 0x16=[4]",
        any(c["cap_id"] == "0x16" and c["data"] == [4] for c in quirks.get("synthesize_capabilities", [])),
        "",
    )


# ── Test 12: KILLER — Q11 quirks unlock real power decode ───────────────


def test_q11_power_quirks_real_decode():
    """Load the bundled Q11 quirks file and process a real C1 Group 4
    frame from Session 15. The 4 power fields should decode with the
    expected values (721.57 kWh, 0.191 kW)."""
    print("\n12. KILLER: Q11 quirks unlock real power decode end-to-end")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)

    # Apply Q11 quirks (which forces fa=readable + synthesizes cap 0x16=4)
    quirks = load_device_quirks(Q11_QUIRKS)
    report = apply_device_quirks(status, quirks, glossary)
    check(
        "report.fields_overridden has 4 power fields",
        len(report["fields_overridden"]) == 4,
        f"got {report['fields_overridden']}",
    )
    check(
        "report.caps_synthesized has 0x16",
        "0x16" in report["caps_synthesized"],
        f"got {report['caps_synthesized']}",
    )

    # Now process the real S15 group4 frame
    fixture_path = TESTS_DIR / "test-cases" / "session15_probe" / "c1g4_frames.yaml"
    with open(fixture_path, encoding="utf-8") as f:
        fixture = yaml.safe_load(f)

    body_hex = fixture["frames"][0]["body_hex"].replace(" ", "")
    body = bytes.fromhex(body_hex)

    # Use decode_frame_fields directly to bypass process_data_frame's
    # capability gating. The encoding selection (linear vs BCD) comes
    # from the synthesized cap 0x16=4 → power_linear_4.
    decoded = decode_frame_fields(
        body,
        "rsp_0xc1_group4",
        glossary,
        cap_records=status.get("capabilities_raw"),
    )

    total = decoded.get("total_power_kwh", {}).get("value")
    realtime = decoded.get("realtime_power_kw", {}).get("value")

    check(
        "total_power_kwh decoded with linear encoding",
        total is not None,
        f"got {total}",
    )
    check(
        "total_power_kwh ~= 721.57 kWh",
        total is not None and abs(total - 721.57) < 0.01,
        f"got {total}",
    )
    check(
        "realtime_power_kw decoded",
        realtime is not None,
        f"got {realtime}",
    )
    check(
        "realtime_power_kw ~= 0.191 kW",
        realtime is not None and abs(realtime - 0.191) < 0.001,
        f"got {realtime}",
    )


# ── Test 13: library API is pure (no file I/O in apply_device_quirks) ──


def test_library_api_is_pure():
    """apply_device_quirks should accept only dicts and not touch the
    filesystem. This is the contract for the future midea-protocol-lib."""
    print("\n13. Library API is pure (no I/O in apply_device_quirks)")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    quirks = {
        "name": "pure-test",
        "feature_available": {"power": "readable"},
        "synthesize_capabilities": [{"cap_id": "0x16", "data": [4]}],
    }
    # If apply_device_quirks tried to open any file at runtime, this would
    # fail because we're passing dicts only.
    report = apply_device_quirks(status, quirks, glossary)
    check("apply_device_quirks accepts dicts only", isinstance(report, dict), "")
    check(
        "no Path or str args needed",
        report["caps_synthesized"] == ["0x16"],
        f"got {report}",
    )


# ── Test 14: apply_quirks_files convenience helper ──────────────────────


def test_apply_quirks_files_helper():
    print("\n14. apply_quirks_files loads + applies a list of paths")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    reports = apply_quirks_files(status, [Q11_QUIRKS], glossary)
    check("returns list of 1 report", len(reports) == 1, f"got {len(reports)}")
    check("report has source path", "source" in reports[0], "missing source key")
    check(
        "Q11 fields overridden",
        len(reports[0]["fields_overridden"]) == 4,
        f"got {reports[0]['fields_overridden']}",
    )


# ── Main ──────────────────────────────────────────────────────────────────


def main():
    test_schema_loaded()
    test_loads_valid_quirks()
    test_rejects_unknown_field()
    test_rejects_invalid_fa_value()
    test_rejects_invalid_cap_id()
    test_fa_override_applies()
    test_synthesized_cap_appended()
    test_real_cap_wins_over_synthesis()
    test_force_overrides_real_cap()
    test_multiple_quirks_compose()
    test_load_device_quirks_helper()
    test_q11_power_quirks_real_decode()
    test_library_api_is_pure()
    test_apply_quirks_files_helper()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {passed + failed} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
