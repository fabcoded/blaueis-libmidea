#!/usr/bin/env python3
"""Frames-dict invariant + round-trip tests — gates TODO §6 phase 2.

Locks the top-level `frames:` dict in serial_glossary.yaml as the single
source of truth for query frame bytes. Verifies:

  - Schema-level integrity of every frame_spec entry.
  - Trigger resolution: every frames[].triggers references a real rsp_*
    key used by at least one field.
  - Reachability: every non-heartbeat rsp_* referenced by a field is
    triggered by at least one frame (the planner can refresh it).
  - Body bounds: literal bytes fit within length; bytes_at keys are in
    range; assembled_from references a cmd key used by at least one
    field with a decode array.
  - Bus enum: only `uart` / `rt` allowed.
  - Byte-identical round-trip: build_frame_from_spec() produces the
    same bytes as the legacy hardcoded builders in midea_frame.py
    for cmd_0x41 / cmd_0xb5_extended / cmd_0xb5_simple / group1 / group3.
  - Group 4/5 bug-fix regression: build_frame_from_spec(cmd_0x41_group4_power)
    must emit body[1]=0x21 (captures show this is the correct UART
    dongle frame; legacy build_group_query(page=0x44) emits body[1]=0x81
    which was never observed in any capture).
  - Planner round-trip: plan_query_cycle picks the right frames for a
    set of target fields with bus filtering.

Usage:
    python tests/test_frames_dict.py
"""

import sys
from pathlib import Path


from blaueis.core.codec import (
    build_frame_body_from_spec,
    build_frame_from_spec,
    load_glossary,
    plan_query_cycle,
    walk_fields,
)
from blaueis.core.frame import (
    build_cap_query_extended,
    build_cap_query_simple,
    build_group_query,
    build_status_query,
)

# Heartbeats are unsolicited — no query frame should trigger them.
HEARTBEAT_RSP_KEYS = {"rsp_0xa1", "rsp_0xa3", "rsp_0xa5", "rsp_0xa6"}


def _collect_field_rsp_keys(glossary: dict) -> set[str]:
    """All rsp_* keys referenced by any field's protocols or capability.frames."""
    result: set[str] = set()
    fields = walk_fields(glossary)
    for _, fdef in fields.items():
        for pkey in fdef.get("protocols") or {}:
            if pkey.startswith("rsp_"):
                result.add(pkey)
        cap = fdef.get("capability") or {}
        for pkey in cap.get("frames") or {}:
            if pkey.startswith("rsp_"):
                result.add(pkey)
    return result


def _collect_cmd_keys_any(glossary: dict) -> set[str]:
    """All cmd_* protocol keys that appear on at least one field (with or
    without a decode array). Used to validate `assembled_from` targets —
    cmd_0x40 fields carry decode arrays (bit-packed), cmd_0xb0 property
    fields do not (property ID + data), but both are valid assembly targets."""
    result: set[str] = set()
    fields = walk_fields(glossary)
    for _, fdef in fields.items():
        for pkey in fdef.get("protocols") or {}:
            if pkey.startswith("cmd_"):
                result.add(pkey)
    return result


def main():
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

    glossary = load_glossary()
    frames = glossary.get("frames") or {}

    # ── Invariant 1: frames dict exists and is non-empty ────────────
    check("frames dict exists and is non-empty", len(frames) > 0, detail=f"got {len(frames)} frames")

    # ── Invariant 2: every frame has a body ─────────────────────────
    missing_body = [fid for fid, spec in frames.items() if not spec.get("body")]
    check("every frame has body", not missing_body, detail=f"missing: {missing_body}")

    # ── Invariant 3: every triggers entry resolves to a real rsp_* key ──
    real_rsp = _collect_field_rsp_keys(glossary)
    stale_triggers = []
    for fid, spec in frames.items():
        for t in spec.get("triggers") or []:
            if t not in real_rsp:
                stale_triggers.append((fid, t))
    check(
        "all triggers resolve to real rsp_* keys",
        not stale_triggers,
        detail=f"stale: {stale_triggers[:5]}",
    )

    # ── Invariant 4: reachability — every non-heartbeat rsp_* is triggered ──
    triggered = set()
    for spec in frames.values():
        triggered.update(spec.get("triggers") or [])
    unreachable = real_rsp - triggered - HEARTBEAT_RSP_KEYS
    check(
        "every non-heartbeat rsp_* is reachable",
        not unreachable,
        detail=f"unreachable: {sorted(unreachable)}",
    )

    # ── Invariant 5: body bytes fit within length ───────────────────
    body_overflow = []
    for fid, spec in frames.items():
        body = spec["body"]
        if "bytes" in body:
            length = int(body["length"])
            n = len(body["bytes"])
            if n > length:
                body_overflow.append((fid, n, length))
    check(
        "literal bytes fit within declared length",
        not body_overflow,
        detail=f"overflow: {body_overflow}",
    )

    # ── Invariant 6: bytes_at keys are in range ─────────────────────
    bytes_at_range_errors = []
    for fid, spec in frames.items():
        body = spec["body"]
        if "bytes_at" in body:
            length = int(body["length"])
            for k in body["bytes_at"]:
                idx = int(k)
                if idx < 0 or idx >= length:
                    bytes_at_range_errors.append((fid, idx, length))
    check(
        "bytes_at keys are within [0, length)",
        not bytes_at_range_errors,
        detail=f"range errors: {bytes_at_range_errors}",
    )

    # ── Invariant 7: assembled_from references a real cmd_* key ─────
    real_cmd = _collect_cmd_keys_any(glossary)
    bad_assembled = []
    for fid, spec in frames.items():
        body = spec["body"]
        if "assembled_from" in body:
            key = body["assembled_from"]
            if key not in real_cmd:
                bad_assembled.append((fid, key))
    check(
        "assembled_from targets a real cmd_* key used by at least one field",
        not bad_assembled,
        detail=f"orphans: {bad_assembled}",
    )

    # ── Invariant 8: bus values in enum ─────────────────────────────
    allowed_buses = {"uart", "rt"}
    bad_bus = []
    for fid, spec in frames.items():
        for b in spec.get("bus") or []:
            if b not in allowed_buses:
                bad_bus.append((fid, b))
    check("all bus values in {uart, rt}", not bad_bus, detail=f"bad: {bad_bus}")

    # ── Invariant 9: msg_type is a byte ─────────────────────────────
    bad_msg_type = []
    for fid, spec in frames.items():
        mt = spec.get("msg_type", 0x03)
        if not isinstance(mt, int) or not (0 <= mt <= 255):
            bad_msg_type.append((fid, mt))
    check("msg_type is a valid byte", not bad_msg_type, detail=f"bad: {bad_msg_type}")

    # ── Invariant 10: no redundant cmd_0x41* marker entries (TODO §7) ──
    # After §7 lands, coverage of cmd_0x41* query paths is implied by the
    # top-level frames: dict plus the field's rsp_* protocol entry. Any
    # field that carries a cmd_0x41* protocol entry without a decode
    # array AND without direction='command' is a pure pointer marker —
    # dead weight the planner should reach through the frames dict.
    # The one exception kept is screen_display.cmd_0x41_display: it is
    # semantically a SET via sub-cmd 0x61, not a query, and the rename
    # that would make this explicit is tracked in TODO §13.
    marker_survivors = []
    for name, fdef in walk_fields(glossary).items():
        for pkey, ploc in (fdef.get("protocols") or {}).items():
            if not pkey.startswith("cmd_0x41"):
                continue
            if ploc.get("decode"):
                continue
            if ploc.get("direction") == "command":
                continue
            marker_survivors.append((name, pkey))
    check(
        "no pure-marker cmd_0x41* protocol entries remain (TODO §7)",
        not marker_survivors,
        detail=f"survivors: {marker_survivors}",
    )

    # ── Invariant 11: every field's query-path is reachable via frames: ──
    # Companion to §7: if a field decodes from rsp_0xc0 / rsp_0xc1_* /
    # rsp_0xc1_sub02, the frames dict must carry a frame whose triggers
    # list includes that rsp_*. This is how the planner figures out
    # "how do I refresh this field?" without walking any per-field cmd_*
    # marker entries.
    query_trigger_gap = []
    query_rsps = {
        "rsp_0xc0",
        "rsp_0xc1_group0",
        "rsp_0xc1_group1",
        "rsp_0xc1_group2",
        "rsp_0xc1_group3",
        "rsp_0xc1_group4",
        "rsp_0xc1_group5",
        "rsp_0xc1_group6",
        "rsp_0xc1_group7",
        "rsp_0xc1_group11",
        "rsp_0xc1_group12",
        "rsp_0xc1_sub02",
    }
    for name, fdef in walk_fields(glossary).items():
        for pkey in fdef.get("protocols") or {}:
            if pkey not in query_rsps:
                continue
            if pkey not in triggered:
                query_trigger_gap.append((name, pkey))
                break
    check(
        "every field's decoded rsp_* is reachable via frames[].triggers",
        not query_trigger_gap,
        detail=f"unreachable: {query_trigger_gap[:5]}",
    )

    # ── Round-trip 1: cmd_0x41 byte-identical to build_status_query() ──
    new_cmd_0x41 = build_frame_from_spec("cmd_0x41", glossary)
    old_cmd_0x41 = build_status_query()
    check(
        "build_frame_from_spec(cmd_0x41) matches build_status_query()",
        new_cmd_0x41 == old_cmd_0x41,
        detail=f"new={new_cmd_0x41.hex()} old={old_cmd_0x41.hex()}",
    )

    # ── Round-trip 2: cmd_0xb5_extended matches build_cap_query_extended() ──
    new_b5e = build_frame_from_spec("cmd_0xb5_extended", glossary)
    old_b5e = build_cap_query_extended()
    check(
        "build_frame_from_spec(cmd_0xb5_extended) matches legacy builder",
        new_b5e == old_b5e,
        detail=f"new={new_b5e.hex()} old={old_b5e.hex()}",
    )

    # ── Round-trip 3: cmd_0xb5_simple matches build_cap_query_simple() ──
    new_b5s = build_frame_from_spec("cmd_0xb5_simple", glossary)
    old_b5s = build_cap_query_simple()
    check(
        "build_frame_from_spec(cmd_0xb5_simple) matches legacy builder",
        new_b5s == old_b5s,
        detail=f"new={new_b5s.hex()} old={old_b5s.hex()}",
    )

    # ── Round-trip 4: cmd_0x41_group1 matches build_group_query(page=0x41) ──
    new_g1 = build_frame_from_spec("cmd_0x41_group1", glossary)
    old_g1 = build_group_query(page=0x41)
    check(
        "build_frame_from_spec(cmd_0x41_group1) matches legacy group query",
        new_g1 == old_g1,
        detail=f"new={new_g1.hex()} old={old_g1.hex()}",
    )

    # ── Round-trip 5: cmd_0x41_group3 matches build_group_query(page=0x43) ──
    new_g3 = build_frame_from_spec("cmd_0x41_group3", glossary)
    old_g3 = build_group_query(page=0x43)
    check(
        "build_frame_from_spec(cmd_0x41_group3) matches legacy group query",
        new_g3 == old_g3,
        detail=f"new={new_g3.hex()} old={old_g3.hex()}",
    )

    # ── Bug fix regression 1: group 4 power has body[1]=0x21 ────────
    g4 = build_frame_body_from_spec(frames["cmd_0x41_group4_power"], glossary)
    check(
        "cmd_0x41_group4_power body[1] == 0x21 (bug-fix regression)",
        g4[1] == 0x21,
        detail=f"got 0x{g4[1]:02X} (legacy hardcoded 0x81 for every page — fixed in phase 2)",
    )
    check(
        "cmd_0x41_group4_power body[3] == 0x44",
        g4[3] == 0x44,
        detail=f"got 0x{g4[3]:02X}",
    )
    # After phase 2, build_group_query is a thin wrapper that routes through
    # build_frame_from_spec. Both paths must now emit the same bytes. This
    # test locks in the fix: the legacy entry point no longer produces the
    # pre-phase-2 buggy body[1]=0x81.
    legacy_g4_full = build_group_query(page=0x44)
    legacy_g4_body_byte1 = legacy_g4_full[10 + 1]  # body starts at offset 10 in UART frame
    check(
        "legacy build_group_query(page=0x44) now also emits body[1]=0x21 (phase 2 wrapper)",
        legacy_g4_body_byte1 == 0x21,
        detail=f"legacy wrapper emitted 0x{legacy_g4_body_byte1:02X}",
    )

    # ── Bug fix regression 2: group 5 has body[1]=0x21 ──────────────
    g5 = build_frame_body_from_spec(frames["cmd_0x41_group5"], glossary)
    check(
        "cmd_0x41_group5 body[1] == 0x21 (captures show UART uses 0x21 for group 5 too)",
        g5[1] == 0x21,
        detail=f"got 0x{g5[1]:02X}",
    )

    # ── Bytes_at regression: cmd_0x41_ext lays out 0x41/0x81/0x03/0x02 at right positions ──
    ext = build_frame_body_from_spec(frames["cmd_0x41_ext"], glossary)
    check(
        "cmd_0x41_ext body[0]=0x41, body[1]=0x81, body[4]=0x03, body[7]=0x02",
        ext[0] == 0x41 and ext[1] == 0x81 and ext[4] == 0x03 and ext[7] == 0x02,
        detail=f"got {ext[:8].hex()}",
    )
    check(
        "cmd_0x41_ext other slots zero-filled (e.g. body[2]=0 body[3]=0 body[5]=0)",
        ext[2] == 0 and ext[3] == 0 and ext[5] == 0 and ext[6] == 0,
        detail=f"body[2:7]={ext[2:7].hex()}",
    )

    # ── Planner test 1: indoor_temperature on UART picks cmd_0x41 ───
    p1 = plan_query_cycle(["indoor_temperature"], glossary, bus="uart")
    check(
        "plan({indoor_temperature}, uart) = [cmd_0x41]",
        p1 == ["cmd_0x41"],
        detail=f"got {p1}",
    )

    # ── Planner test 2: realtime_power_kw on UART picks group4_power ──
    # The field decodes from rsp_0xc1_group4 and rsp_0xb5_tlv. The planner
    # picks the first matching frame in YAML order — cmd_0x41_group4_power
    # comes before cmd_0xb5_extended in serial_glossary.yaml.
    p2 = plan_query_cycle(["realtime_power_kw"], glossary, bus="uart")
    check(
        "plan({realtime_power_kw}, uart) includes cmd_0x41_group4_power",
        "cmd_0x41_group4_power" in p2,
        detail=f"got {p2}",
    )

    # ── Planner test 3: t1_indoor_coil reachable on both buses ────────
    # t1_indoor_coil decodes from rsp_0xc1_group1, triggered by
    # cmd_0x41_group1 (bus=[uart, rt] — confirmed working on UART with
    # body[1]=0x21 in Session 15 probe).
    p3 = plan_query_cycle(["t1_indoor_coil"], glossary, bus="uart")
    check(
        "plan({t1_indoor_coil}, uart) includes cmd_0x41_group1 (v21 works on UART)",
        "cmd_0x41_group1" in p3,
        detail=f"got {p3}",
    )

    p4 = plan_query_cycle(["t1_indoor_coil"], glossary, bus="rt")
    check(
        "plan({t1_indoor_coil}, rt) = [cmd_0x41_group1]",
        p4 == ["cmd_0x41_group1"],
        detail=f"got {p4}",
    )

    # ── Planner test 4: multi-field dedup + ordering ────────────────
    p5 = plan_query_cycle(
        ["indoor_temperature", "realtime_power_kw", "total_power_kwh"],
        glossary,
        bus="uart",
    )
    check(
        "plan for two power fields + temp dedups to 2 unique frames",
        set(p5) == {"cmd_0x41", "cmd_0x41_group4_power"},
        detail=f"got {p5}",
    )
    # The order must place cmd_0x41 (C0 status, priority 1) before
    # cmd_0x41_group4_power (group queries, priority 2).
    check(
        "plan output is priority-ordered (C0 before group queries)",
        p5.index("cmd_0x41") < p5.index("cmd_0x41_group4_power"),
        detail=f"got {p5}",
    )

    # ── Planner test 5: realistic "all control fields" target on UART ──
    # The live monitor's real target set is roughly "everything settable
    # the user can see plus the headline telemetry". This test exercises
    # the planner against that shape and locks the expected output so a
    # future field addition that would bloat the scan queue is caught.
    all_control = [
        name
        for name, fdef in walk_fields(glossary).items()
        if any(ploc.get("direction") == "command" for ploc in (fdef.get("protocols") or {}).values())
    ]
    key_telemetry = [
        "indoor_temperature",
        "outdoor_temperature",
        "realtime_power_kw",
        "total_power_kwh",
        "t1_indoor_coil",  # forces group1 path on R/T
    ]
    p_uart = plan_query_cycle(all_control + key_telemetry, glossary, bus="uart")
    check(
        f"plan({len(all_control)} control + 4 telemetry, uart) is non-empty",
        len(p_uart) > 0,
        detail=f"got {p_uart}",
    )
    # Must include cmd_0x41 (C0 status refresh — covers most control fields)
    check(
        "UART plan includes cmd_0x41 (C0 status — covers most control fields)",
        "cmd_0x41" in p_uart,
        detail=f"got {p_uart}",
    )
    # Must include cmd_0x41_group4_power (power telemetry)
    check(
        "UART plan includes cmd_0x41_group4_power (power telemetry)",
        "cmd_0x41_group4_power" in p_uart,
        detail=f"got {p_uart}",
    )
    # All group frames are now bus-agnostic (body[1]=0x21 works on both
    # buses — confirmed Session 15 probe). UART plan should include group1
    # because t1_indoor_coil is in key_telemetry.
    check(
        "UART plan includes cmd_0x41_group1 (v21 works on UART — Session 15)",
        "cmd_0x41_group1" in p_uart,
        detail=f"got {p_uart}",
    )

    # ── Planner test 6: same target set on R/T ──────────────────────
    p_rt = plan_query_cycle(all_control + key_telemetry, glossary, bus="rt")
    check(
        f"plan({len(all_control)} control + 4 telemetry, rt) is non-empty",
        len(p_rt) > 0,
        detail=f"got {p_rt}",
    )
    # R/T bus still uses cmd_0x41 for C0 status (frame is bus-agnostic).
    check(
        "R/T plan includes cmd_0x41 (C0 status is bus-agnostic)",
        "cmd_0x41" in p_rt,
        detail=f"got {p_rt}",
    )
    # R/T plan should include group1 because t1_indoor_coil et al. need it —
    # but only if a target field decodes from rsp_0xc1_group1.
    has_group1_field = any(
        "rsp_0xc1_group1" in (walk_fields(glossary).get(n) or {}).get("protocols") or {}
        for n in all_control + key_telemetry
    )
    if has_group1_field:
        check(
            "R/T plan includes cmd_0x41_group1 when a target decodes from rsp_0xc1_group1",
            "cmd_0x41_group1" in p_rt,
            detail=f"got {p_rt}",
        )
    # All group frames are now bus-agnostic. R/T plan should include
    # group4_power because realtime_power_kw is in key_telemetry.
    check(
        "R/T plan includes cmd_0x41_group4_power (bus-agnostic after Session 15)",
        "cmd_0x41_group4_power" in p_rt,
        detail=f"got {p_rt}",
    )

    # ── Planner test 7: empty target set returns empty plan ─────────
    p_empty = plan_query_cycle([], glossary, bus="uart")
    check(
        "plan([], uart) is empty",
        p_empty == [],
        detail=f"got {p_empty}",
    )

    # ── Summary ─────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
