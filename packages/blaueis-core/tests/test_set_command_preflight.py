#!/usr/bin/env python3
"""Tests for set_command_preflight in build_command_body / build_b0_command_body.

The cmd_0x40 frame packs many fields into bit-shared bytes (read-modify-write).
Sending a set command without knowing the current state of every sibling will
silently clobber them. The preflight check blocks the build if any sibling of
a changed field is stale or never read — like an aircraft preflight check
before takeoff (transmission).

Tests cover both cmd_0x40 (byte sharing) and cmd_0xb0 (property sharing).

Usage:
    python tests/test_set_command_preflight.py
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


import yaml
from blaueis.core.command import (
    _group_cmd_fields_by_byte,
    _group_cmd_fields_by_property,
    build_b0_command_body,
    build_command_body,
)
from blaueis.core.status import build_status
from blaueis.core.codec import build_field_map, decode_frame_fields, load_glossary
from blaueis.core.process import finalize_capabilities, process_raw_frame

TESTS_DIR = Path(__file__).resolve().parent

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


# ── Helpers ───────────────────────────────────────────────────────────────


def _populate_field(status: dict, name: str, value, age_seconds: float = 0.0, now: datetime | None = None) -> None:
    """Populate a field's `sources` slot at (now - age_seconds).

    Mirrors what process_data_frame does for a real frame: writes a
    `rsp_0xc0` slot annotated with `generation: legacy`. The preflight
    reads via field_query.read_field(priority=['protocol_all']) so any
    populated slot satisfies it.
    """
    if now is None:
        now = datetime.now(UTC)
    ts = (now - timedelta(seconds=age_seconds)).isoformat()
    fdef = status["fields"].get(name)
    if fdef is None:
        return
    fdef.setdefault("sources", {})["rsp_0xc0"] = {
        "value": value,
        "raw": value,
        "frame_no": 1,
        "ts": ts,
        "generation": "legacy",
    }


def _populate_all_cmd_0x40_fields(
    status: dict, glossary: dict, age_seconds: float = 0.0, now: datetime | None = None
) -> None:
    """Set every cmd_0x40 field as fresh (or aged by N seconds)."""
    field_map = build_field_map(glossary, "cmd_0x40")
    for f in field_map:
        name = f["name"]
        # Use a sensible default value
        dt = f.get("data_type", "uint8")
        default = False if dt == "bool" else 0
        _populate_field(status, name, default, age_seconds=age_seconds, now=now)


# ── Test 1: helper functions ─────────────────────────────────────────────


def test_group_helpers():
    print("\n1. Group helper functions")
    glossary = load_glossary()
    field_map_40 = build_field_map(glossary, "cmd_0x40")
    field_map_b0 = build_field_map(glossary, "cmd_0xb0")

    by_byte = _group_cmd_fields_by_byte(field_map_40)
    check(
        "cmd_0x40 grouped by byte returns dict[int, list]",
        isinstance(by_byte, dict) and all(isinstance(k, int) for k in by_byte),
        f"got {type(by_byte)}",
    )
    # body[8] is the most-shared byte (7 fields per the field inventory)
    check(
        "body[8] has at least 6 sibling fields (heavy bit-pack)",
        len(by_byte.get(8, [])) >= 6,
        f"got {by_byte.get(8)}",
    )

    by_prop = _group_cmd_fields_by_property(field_map_b0)
    check(
        "cmd_0xb0 grouped by property returns dict[str, list]",
        isinstance(by_prop, dict) and all(isinstance(k, str) for k in by_prop),
        f"got {type(by_prop)}",
    )
    # property 0x4A,0x00 (aqua_wash) has 3 writable fields (manual + switch + time)
    check(
        "property 0x4A,0x00 has at least 3 sibling fields",
        len(by_prop.get("0x4A,0x00", [])) >= 3,
        f"got {by_prop.get('0x4A,0x00')}",
    )


# ── Test 2: boot status blocks any change ─────────────────────────────────


def test_boot_status_blocks_change():
    print("\n2. Boot status blocks any cmd_0x40 change")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    # boot status: every field has last_updated=None

    result = build_command_body(status, {"power": True}, glossary)
    check(
        "body is None (preflight blocked)",
        result["body"] is None,
        f"got body={result['body']}",
    )
    check(
        "validation has errors",
        len(result["preflight"]) > 0,
        f"got {len(result['preflight'])} errors",
    )
    check(
        "all errors have severity=error",
        all(e["severity"] == "error" for e in result["preflight"]),
        "mixed severities",
    )
    # power is in body[1] sharing with buzzer/protocol_bit1/resume
    error_fields = {e["field"] for e in result["preflight"]}
    check(
        "resume is flagged as never_read sibling of power",
        "resume" in error_fields,
        f"errors on {error_fields}",
    )


# ── Test 3: skip_preflight=True bypasses preflight ─────────────────────────────────


def test_force_bypass():
    print("\n3. skip_preflight=True bypasses the preflight")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)

    result = build_command_body(status, {"power": True}, glossary, skip_preflight=True)
    check(
        "body returned despite stale state",
        result["body"] is not None,
        f"got body={result['body']}",
    )
    check(
        "validation still populated as warnings",
        len(result["preflight"]) > 0,
        "expected warnings present",
    )
    check(
        "body is 26 bytes with body[0]=0x40",
        len(result["body"]) == 26 and result["body"][0] == 0x40,
        f"got len={len(result['body']) if result['body'] else None}",
    )


# ── Test 4: single-owner byte (humidity_setpoint) needs no siblings ──────


def test_single_owner_byte():
    print("\n4. Single-owner byte requires no sibling freshness")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)

    # humidity_setpoint owns body[19] alone — changing it should pass
    # the preflight even with everything else stale.
    result = build_command_body(status, {"humidity_setpoint": 50}, glossary)
    check(
        "humidity_setpoint single-owner: no preflight errors",
        result["body"] is not None,
        f"validation: {result['preflight']}",
    )


# ── Test 5: shared byte all fresh passes ─────────────────────────────────


def test_shared_byte_all_fresh():
    print("\n5. Shared byte with all siblings fresh passes")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    now = datetime.now(UTC)

    # Populate every cmd_0x40 field as fresh (1 second old)
    _populate_all_cmd_0x40_fields(status, glossary, age_seconds=1.0, now=now)

    result = build_command_body(status, {"power": True}, glossary, now=now)
    check(
        "all-fresh state: body returned",
        result["body"] is not None,
        f"validation: {result['preflight']}",
    )
    check(
        "no validation errors",
        len(result["preflight"]) == 0,
        f"got {len(result['preflight'])}",
    )


# ── Test 6: shared byte one stale blocks ─────────────────────────────────


def test_shared_byte_stale():
    print("\n6. Shared byte with one stale sibling blocks")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    now = datetime.now(UTC)

    _populate_all_cmd_0x40_fields(status, glossary, age_seconds=1.0, now=now)
    # Make resume stale (60s > 30s default)
    _populate_field(status, "resume", False, age_seconds=60.0, now=now)

    result = build_command_body(status, {"power": True}, glossary, now=now)
    check(
        "stale sibling blocks: body is None",
        result["body"] is None,
        f"got body={result['body']}",
    )
    stale_errors = [e for e in result["preflight"] if e["reason"] == "stale"]
    check(
        "validation includes stale entry for resume",
        any(e["field"] == "resume" for e in stale_errors),
        f"errors: {result['preflight']}",
    )
    if stale_errors:
        ages = [e["age_seconds"] for e in stale_errors if e["field"] == "resume"]
        check(
            "resume age ~60s",
            ages and 55 < ages[0] < 65,
            f"age={ages}",
        )


# ── Test 7: never_read distinct from stale ───────────────────────────────


def test_never_read_distinct():
    print("\n7. never_read reason distinct from stale")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    now = datetime.now(UTC)

    _populate_all_cmd_0x40_fields(status, glossary, age_seconds=1.0, now=now)
    # Reset resume to never_read by clearing its sources slot. The
    # preflight reads via read_field — an empty sources dict resolves to
    # None, which the preflight reports as never_read.
    status["fields"]["resume"]["sources"] = {}

    result = build_command_body(status, {"power": True}, glossary, now=now)
    nr = [e for e in result["preflight"] if e["field"] == "resume"]
    check(
        "resume flagged",
        len(nr) == 1,
        f"got {len(nr)} entries for resume",
    )
    if nr:
        check(
            "reason=never_read with age_seconds=None",
            nr[0]["reason"] == "never_read" and nr[0]["age_seconds"] is None,
            f"got reason={nr[0]['reason']}, age={nr[0]['age_seconds']}",
        )


# ── Test 8: capability=never field is exempt ─────────────────────────────


def test_capability_never_exempt():
    print("\n8. Fields with feature_available=never are exempt")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    now = datetime.now(UTC)

    _populate_all_cmd_0x40_fields(status, glossary, age_seconds=1.0, now=now)
    # buzzer is at body[1] sharing with power. Mark it never (cap missing).
    status["fields"]["buzzer"]["feature_available"] = "never"
    status["fields"]["buzzer"]["last_updated"] = None  # never read

    result = build_command_body(status, {"power": True}, glossary, now=now)
    err_fields = {e["field"] for e in result["preflight"]}
    check(
        "buzzer (never) not in errors",
        "buzzer" not in err_fields,
        f"errors: {err_fields}",
    )
    check(
        "build succeeded (no other stale fields)",
        result["body"] is not None,
        f"validation: {result['preflight']}",
    )


# ── Test 9: default_value field is exempt ────────────────────────────────


def test_default_value_exempt():
    print("\n9. Fields with default_value are exempt")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    now = datetime.now(UTC)

    _populate_all_cmd_0x40_fields(status, glossary, age_seconds=1.0, now=now)
    # protocol_bit1 has default_value=1 — mark it never_read
    status["fields"]["protocol_bit1"]["last_updated"] = None

    result = build_command_body(status, {"power": True}, glossary, now=now)
    err_fields = {e["field"] for e in result["preflight"]}
    check(
        "protocol_bit1 not in errors (default_value exempt)",
        "protocol_bit1" not in err_fields,
        f"errors: {err_fields}",
    )


# ── Test 10: cmd_0xb0 multi-property sibling check ───────────────────────


def test_b0_property_siblings_stale():
    print("\n10. cmd_0xb0 property-level sibling freshness")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    now = datetime.now(UTC)

    # aqua_wash property 0x4A,0x00 holds 3 writable fields:
    # aqua_wash_manual, aqua_wash_switch, aqua_wash_time. All start as
    # feature_available=capability — promote to readable so the preflight
    # doesn't auto-exempt them.
    for name in ("aqua_wash_manual", "aqua_wash_switch", "aqua_wash_time"):
        status["fields"][name]["feature_available"] = "readable"

    # Populate two fresh, leave one stale.
    _populate_field(status, "aqua_wash_manual", False, age_seconds=1.0, now=now)
    _populate_field(status, "aqua_wash_switch", False, age_seconds=120.0, now=now)
    _populate_field(status, "aqua_wash_time", 5, age_seconds=1.0, now=now)

    # Change aqua_wash_manual — aqua_wash_switch is stale (120s > 30s)
    result = build_b0_command_body(status, {"aqua_wash_manual": True}, glossary, now=now)
    check(
        "stale aqua_wash_switch blocks the build",
        result["body"] is None,
        f"got body={result['body']}",
    )
    err = [e for e in result["preflight"] if e["field"] == "aqua_wash_switch"]
    check(
        "aqua_wash_switch flagged stale",
        len(err) == 1 and err[0]["reason"] == "stale",
        f"errors: {result['preflight']}",
    )


# ── Test 11: cmd_0xb0 independent property is unaffected ─────────────────


def test_b0_independent_property():
    print("\n11. cmd_0xb0 independent property bypasses preflight")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)

    # anion_ionizer is the sole field at property 0x1E,0x02 — no siblings
    result = build_b0_command_body(status, {"anion_ionizer": True}, glossary)
    check(
        "anion_ionizer change: body returned (no siblings)",
        result["body"] is not None,
        f"validation: {result['preflight']}",
    )


# ── Test 12: changing all siblings together passes ───────────────────────


def test_change_all_siblings():
    print("\n12. Changing all siblings together passes (no stale to check)")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)

    # Change all 3 aqua_wash writable fields together — no sibling left to validate
    result = build_b0_command_body(
        status,
        {"aqua_wash_manual": True, "aqua_wash_switch": True, "aqua_wash_time": 5},
        glossary,
    )
    check(
        "all 3 aqua_wash fields in changes: build passes",
        result["body"] is not None,
        f"validation: {result['preflight']}",
    )


# ── Test 13: validation dict schema ──────────────────────────────────────


def test_validation_schema():
    print("\n13. Validation error dict schema")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    now = datetime.now(UTC)

    result = build_command_body(status, {"power": True}, glossary, now=now)
    check("at least one validation error to inspect", len(result["preflight"]) > 0, "")
    if result["preflight"]:
        err = result["preflight"][0]
        required = {"severity", "field", "reason", "age_seconds", "shared_with", "position"}
        check(
            "error has all required keys",
            required.issubset(err.keys()),
            f"missing: {required - err.keys()}",
        )
        check(
            "shared_with == power",
            err["shared_with"] == "power",
            f"got {err['shared_with']}",
        )
        check(
            "position == body[1]",
            err["position"] == "body[1]",
            f"got {err['position']}",
        )


# ── Test 14: now parameter freezes time ──────────────────────────────────


def test_now_parameter():
    print("\n14. now parameter freezes time for deterministic ages")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    fixed_now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)

    _populate_all_cmd_0x40_fields(status, glossary, age_seconds=1.0, now=fixed_now)
    # Mark resume 45s old relative to fixed_now
    _populate_field(status, "resume", False, age_seconds=45.0, now=fixed_now)

    result = build_command_body(status, {"power": True}, glossary, now=fixed_now)
    stale = [e for e in result["preflight"] if e["field"] == "resume" and e["reason"] == "stale"]
    check(
        "resume age == 45.0 ± 0.01",
        stale and abs(stale[0]["age_seconds"] - 45.0) < 0.01,
        f"got {stale[0]['age_seconds'] if stale else None}",
    )


# ── Test 15a: end-to-end — load real frame, change one bit, verify preflight ──


def _load_frames(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["frames"]


def test_integration_real_frame_round_trip():
    """End-to-end: load real B5 + C0 fixtures, encode a change, verify the
    preflight accepts when fresh and rejects when stale.

    This is the test that proves the preflight works against real captured
    bytes, not synthetic test status dicts.
    """
    print("\n15a. Integration: real C0 fixture → encode change → preflight check")
    glossary = load_glossary()

    # 1. Build a fresh status, load B5 caps from S1, finalize.
    status = build_status(device="test", glossary=glossary)
    b5_path = TESTS_DIR / "test-cases" / "xtremesaveblue_s1" / "b5_frames.yaml"
    for frame in _load_frames(b5_path):
        body = bytes.fromhex(frame["body_hex"].replace(" ", "").replace("\n", ""))
        process_raw_frame(status, body, glossary)
    finalize_capabilities(status, glossary)

    # 2. Process a real C0 frame from Session 7 to populate last_updated
    #    for every decodable field.
    c0_path = TESTS_DIR / "test-cases" / "xtremesaveblue_s7_frames" / "c0_frames.yaml"
    c0_frames = _load_frames(c0_path)
    first_frame = c0_frames[0]
    body = bytes.fromhex(first_frame["body_hex"].replace(" ", "").replace("\n", ""))
    process_raw_frame(status, body, glossary)

    # Sanity: confirm at least one cmd_0x40 field got a sources slot.
    populated = [n for n, f in status["fields"].items() if f.get("sources") and f.get("writable")]
    check(
        f"C0 frame populated sources on writable fields ({len(populated)} fields)",
        len(populated) > 5,
        f"got {len(populated)}",
    )

    # 3. Try to encode a single-bit change (eco_mode toggle).
    #    All siblings should be fresh (just decoded), so preflight must pass.
    result = build_command_body(status, {"eco_mode": True}, glossary)
    check(
        "fresh status: eco_mode change passes preflight",
        result["body"] is not None,
        f"validation: {result['preflight']}",
    )
    check(
        "no validation errors on fresh status",
        len(result["preflight"]) == 0,
        f"got {len(result['preflight'])}",
    )

    # 4. Round-trip: decode the encoded body and verify eco_mode flipped
    #    while siblings are preserved from the C0 read.
    decoded = decode_frame_fields(
        bytes(result["body"]),
        "cmd_0x40",
        glossary,
        cap_records=status.get("capabilities_raw"),
    )
    check(
        "encoded body decodes eco_mode == True",
        decoded.get("eco_mode", {}).get("value") is True,
        f"got {decoded.get('eco_mode')}",
    )
    # power was true in the C0 frame (body[1] bit 0 = 1)
    check(
        "encoded body preserves power (sibling at body[9])",
        decoded.get("power", {}).get("value") is True,
        f"got {decoded.get('power')}",
    )
    # follow_me was true in the C0 frame (per ground_truth)
    check(
        "encoded body preserves follow_me (sibling at body[8])",
        decoded.get("follow_me", {}).get("value") is True,
        f"got {decoded.get('follow_me')}",
    )

    # 5. Bump the clock past the freshness threshold and try again.
    fixed_now = datetime.now(UTC) + timedelta(seconds=120)
    result_stale = build_command_body(status, {"eco_mode": True}, glossary, now=fixed_now)
    check(
        "after 120s without re-read: preflight blocks the build",
        result_stale["body"] is None,
        f"got body={result_stale['body']}",
    )
    check(
        "validation lists at least one stale sibling",
        any(e["reason"] == "stale" for e in result_stale["preflight"]),
        f"reasons: {[e['reason'] for e in result_stale['preflight']]}",
    )

    # 6. With skip_preflight=True, the same stale call returns the body.
    result_forced = build_command_body(status, {"eco_mode": True}, glossary, now=fixed_now, skip_preflight=True)
    check(
        "skip_preflight=True returns body even when stale",
        result_forced["body"] is not None,
        f"got body={result_forced['body']}",
    )
    check(
        "skip_preflight=True still surfaces warnings in validation",
        len(result_forced["preflight"]) > 0,
        "expected warnings",
    )


# ── Test 15b: end-to-end — refresh after stale resets the preflight ───────


def test_integration_refresh_after_stale():
    """After a stale period, processing a fresh C0 frame resets last_updated
    on all decoded fields, so the preflight passes again."""
    print("\n15b. Integration: stale → re-process C0 → preflight passes")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)

    b5_path = TESTS_DIR / "test-cases" / "xtremesaveblue_s1" / "b5_frames.yaml"
    for frame in _load_frames(b5_path):
        body = bytes.fromhex(frame["body_hex"].replace(" ", "").replace("\n", ""))
        process_raw_frame(status, body, glossary)
    finalize_capabilities(status, glossary)

    c0_path = TESTS_DIR / "test-cases" / "xtremesaveblue_s7_frames" / "c0_frames.yaml"
    c0_frames = _load_frames(c0_path)
    body = bytes.fromhex(c0_frames[0]["body_hex"].replace(" ", "").replace("\n", ""))

    # First decode at t=0
    process_raw_frame(status, body, glossary)

    # Try to send a change 120s later — must fail
    fixed_now = datetime.now(UTC) + timedelta(seconds=120)
    r1 = build_command_body(status, {"power": False}, glossary, now=fixed_now)
    check("stale at t+120s: preflight blocks", r1["body"] is None, "")

    # Re-process the same C0 frame at the new "now" — process_frame uses
    # datetime.now(UTC) internally so this writes a fresh timestamp.
    process_raw_frame(status, body, glossary)
    r2 = build_command_body(status, {"power": False}, glossary)
    check(
        "after re-process: preflight passes",
        r2["body"] is not None,
        f"preflight: {r2['preflight']}",
    )


# ── Test 15: threshold parameter ─────────────────────────────────────────


def test_threshold_parameter():
    print("\n15. preflight_threshold_seconds parameter")
    glossary = load_glossary()
    status = build_status(device="test", glossary=glossary)
    now = datetime.now(UTC)

    _populate_all_cmd_0x40_fields(status, glossary, age_seconds=1.0, now=now)
    _populate_field(status, "resume", False, age_seconds=60.0, now=now)

    # Tight threshold (10s): 60s resume should fail
    r1 = build_command_body(status, {"power": True}, glossary, preflight_threshold_seconds=10.0, now=now)
    check("threshold=10s: stale 60s blocks", r1["body"] is None, "")

    # Loose threshold (120s): 60s resume should pass
    r2 = build_command_body(status, {"power": True}, glossary, preflight_threshold_seconds=120.0, now=now)
    check("threshold=120s: stale 60s passes", r2["body"] is not None, "")


# ── Main ──────────────────────────────────────────────────────────────────


def main():
    test_group_helpers()
    test_boot_status_blocks_change()
    test_force_bypass()
    test_single_owner_byte()
    test_shared_byte_all_fresh()
    test_shared_byte_stale()
    test_never_read_distinct()
    test_capability_never_exempt()
    test_default_value_exempt()
    test_b0_property_siblings_stale()
    test_b0_independent_property()
    test_change_all_siblings()
    test_validation_schema()
    test_now_parameter()
    test_integration_real_frame_round_trip()
    test_integration_refresh_after_stale()
    test_threshold_parameter()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {passed + failed} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
