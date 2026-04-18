#!/usr/bin/env python3
"""Scan queue orchestration tests — locks build_scan_queue + detect_dead_frames.

Regression coverage for the live monitor's send path (TODO §6 final sweep).
The pure function build_scan_queue is the logic that used to live inline
in ac_monitor.py; these tests verify it against known status dicts so
future refactors can't silently change what hits the wire.

Four scan-cycle phases the live monitor passes through:

  1. Boot: caps_finalized=False, need_caps=True
       → B5 extended + B5 simple + C0 status

  2. UART resolved: caps_finalized=True, need_caps=False, bus=uart
       → C0 + cmd_0x41_ext + all group queries with fields
       (all groups are bus-agnostic since Session 15 discovery:
       body[1]=0x21 works on both buses)

  3. R/T resolved: caps_finalized=True, need_caps=False, bus=rt
       → same as UART (all frames bus-agnostic)

  4. Periodic cap rescan: caps_finalized=True, need_caps=True
       → union of boot + resolved

  + dead-frame skipping, detect_dead_frames invariants, dedup between
    the always-queue C0 and any planner-suggested cmd_0x41.

Usage:
    python tests/test_scan_queue.py
"""

import sys

from blaueis.core.codec import (
    build_scan_queue,
    detect_dead_frames,
    load_glossary,
    target_field_names,
)
from blaueis.core.status import build_status


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
    status = build_status(device="test", glossary=glossary)

    def labels(queue):
        return [lbl for lbl, _ in queue]

    # ── Phase 1: boot — B5 + C0, no groups ──────────────────────────
    q1 = build_scan_queue(
        status=status,
        glossary=glossary,
        bus="uart",
        caps_finalized=False,
        need_caps=True,
        dead_frames=set(),
    )
    q1_labels = labels(q1)
    check("boot phase: B5 extended first", q1_labels[0].startswith("B5 extended"), f"got {q1_labels}")
    check("boot phase: B5 simple second", q1_labels[1].startswith("B5 simple"), f"got {q1_labels}")
    check("boot phase: C0 third", q1_labels[2].startswith("C0"), f"got {q1_labels}")
    check("boot phase: no group queries (caps unresolved)", len(q1_labels) == 3, f"got {q1_labels}")

    # ── Phase 2: UART resolved ──────────────────────────────────────
    q2 = build_scan_queue(
        status=status,
        glossary=glossary,
        bus="uart",
        caps_finalized=True,
        need_caps=False,
        dead_frames=set(),
    )
    q2_labels = labels(q2)
    q2_fids = " ".join(q2_labels)
    check("uart resolved: no B5 queries", "B5 " not in q2_fids, f"got {q2_labels}")
    check("uart resolved: includes C0", any(lbl.startswith("C0") for lbl in q2_labels), f"got {q2_labels}")
    check("uart resolved: includes group4_power", "cmd_0x41_group4_power" in q2_fids, f"got {q2_labels}")
    check("uart resolved: includes group5", "cmd_0x41_group5" in q2_fids, f"got {q2_labels}")
    check("uart resolved: includes group1 (bus-agnostic since S15)", "cmd_0x41_group1" in q2_fids, f"got {q2_labels}")
    check("uart resolved: includes group3 (bus-agnostic since S15)", "cmd_0x41_group3" in q2_fids, f"got {q2_labels}")
    check("uart resolved: includes cmd_0x41_ext", "cmd_0x41_ext" in q2_fids, f"got {q2_labels}")

    # ── Group 4 power bug-fix regression on the actual wire bytes ───
    group4_frame = next((f for lbl, f in q2 if "group4_power" in lbl), None)
    check(
        "uart queue: group4 power frame has body[1]=0x21 (bug-fix regression)",
        group4_frame is not None and group4_frame[10 + 1] == 0x21,
        detail=f"body[1]=0x{group4_frame[10 + 1]:02X}" if group4_frame else "frame missing",
    )

    # ── Phase 3: R/T resolved ───────────────────────────────────────
    q3 = build_scan_queue(
        status=status,
        glossary=glossary,
        bus="rt",
        caps_finalized=True,
        need_caps=False,
        dead_frames=set(),
    )
    q3_labels = labels(q3)
    q3_fids = " ".join(q3_labels)
    check("rt resolved: includes C0 (bus-agnostic)", any(lbl.startswith("C0") for lbl in q3_labels), f"got {q3_labels}")
    check("rt resolved: includes group1", "cmd_0x41_group1" in q3_fids, f"got {q3_labels}")
    check("rt resolved: includes group3", "cmd_0x41_group3" in q3_fids, f"got {q3_labels}")
    check(
        "rt resolved: includes group4_power (bus-agnostic since S15)",
        "cmd_0x41_group4_power" in q3_fids,
        f"got {q3_labels}",
    )
    check("rt resolved: includes group5 (bus-agnostic since S15)", "cmd_0x41_group5" in q3_fids, f"got {q3_labels}")

    # All group frames now use body[1]=0x21 (works on both buses)
    group1_frame = next((f for lbl, f in q3 if "group1" in lbl), None)
    check(
        "rt queue: group1 frame has body[1]=0x21 (unified variant — Session 15)",
        group1_frame is not None and group1_frame[10 + 1] == 0x21,
        detail=f"body[1]=0x{group1_frame[10 + 1]:02X}" if group1_frame else "frame missing",
    )

    # ── Phase 4: periodic cap rescan ────────────────────────────────
    q4 = build_scan_queue(
        status=status,
        glossary=glossary,
        bus="uart",
        caps_finalized=True,
        need_caps=True,
        dead_frames=set(),
    )
    q4_labels = labels(q4)
    q4_fids = " ".join(q4_labels)
    has_both_b5 = "B5 extended" in q4_fids and "B5 simple" in q4_fids
    check("cap rescan: includes both B5 queries", has_both_b5, f"got {q4_labels}")
    check("cap rescan: includes C0", any(lbl.startswith("C0") for lbl in q4_labels), f"got {q4_labels}")
    check("cap rescan: includes UART group queries", "cmd_0x41_group4_power" in q4_fids, f"got {q4_labels}")
    # The ordering invariant: B5 first, then C0, then group queries
    first_b5 = q4_labels.index("B5 extended (0x00)")
    first_c0 = next(i for i, lbl in enumerate(q4_labels) if lbl.startswith("C0"))
    first_group = next(i for i, lbl in enumerate(q4_labels) if "group" in lbl)
    check(
        "cap rescan: order is B5 < C0 < group queries",
        first_b5 < first_c0 < first_group,
        detail=f"b5={first_b5} c0={first_c0} group={first_group}",
    )

    # ── dead_frames filter ──────────────────────────────────────────
    q_dead = build_scan_queue(
        status=status,
        glossary=glossary,
        bus="uart",
        caps_finalized=True,
        need_caps=False,
        dead_frames={"cmd_0x41_group4_power"},
    )
    q_dead_fids = " ".join(labels(q_dead))
    check(
        "dead_frames: group4_power excluded when dead",
        "cmd_0x41_group4_power" not in q_dead_fids,
        detail=f"got {labels(q_dead)}",
    )
    # Group 5 (also UART) should still appear.
    check(
        "dead_frames: group5 still present (not in dead set)",
        "cmd_0x41_group5" in q_dead_fids,
        detail=f"got {labels(q_dead)}",
    )

    # ── Dedup: cmd_0x41 appears only once even if planner also picks it ──
    q_all = build_scan_queue(
        status=status,
        glossary=glossary,
        bus="uart",
        caps_finalized=True,
        need_caps=False,
        dead_frames=set(),
    )
    c0_count = sum(
        1 for lbl in labels(q_all) if lbl.startswith("C0 status") or lbl == "cmd_0x41" or lbl.startswith("cmd_0x41 ")
    )
    check("dedup: cmd_0x41 / C0 appears exactly once", c0_count == 1, f"got {labels(q_all)}")

    # ── detect_dead_frames ──────────────────────────────────────────
    # All group frames are now bus-agnostic (bus=[uart, rt]).
    # Simulate: AC responded to group 4 only. All other groups should be dead.
    all_group_fids = {fid for fid in glossary.get("frames", {}) if fid.startswith("cmd_0x41_group")}
    dead = detect_dead_frames(glossary, {"rsp_0xc1_group4": 5}, bus="uart")
    check(
        "detect_dead_frames(uart, only group4 responding) marks all other groups dead",
        dead == all_group_fids - {"cmd_0x41_group4_power"},
        detail=f"got {dead}",
    )

    dead_rt = detect_dead_frames(glossary, {"rsp_0xc1_group1": 5}, bus="rt")
    check(
        "detect_dead_frames(rt, only group1 responding) marks all other groups dead",
        dead_rt == all_group_fids - {"cmd_0x41_group1"},
        detail=f"got {dead_rt}",
    )

    # No responses → all group frames marked dead on both buses
    dead_nothing = detect_dead_frames(glossary, {}, bus="uart")
    check(
        "detect_dead_frames(empty counts, uart) marks all group frames dead",
        dead_nothing == all_group_fids,
        detail=f"got {dead_nothing}",
    )

    # ── target_field_names ──────────────────────────────────────────
    targets = target_field_names(status)
    check(
        f"target_field_names returns non-never fields ({len(targets)} fields)",
        len(targets) > 0 and all(status["fields"][n]["feature_available"] != "never" for n in targets),
        detail=f"got {len(targets)} targets",
    )
    # Simulate a never field: mutate a copy
    import copy

    status2 = copy.deepcopy(status)
    first_field = next(iter(status2["fields"]))
    status2["fields"][first_field]["feature_available"] = "never"
    targets2 = target_field_names(status2)
    check(
        "target_field_names excludes feature_available=never fields",
        first_field not in targets2,
        detail=f"{first_field} should not be in targets",
    )

    # ── Summary ─────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
