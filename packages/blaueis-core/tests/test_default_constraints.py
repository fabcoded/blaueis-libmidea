#!/usr/bin/env python3
"""Default constraint + readable promotion tests — gates TODO §2 + §3.

Five validation tests (V1-V4 from the plan, plus one regression check):

  V1 — Cold-boot decode (gates §2 promotion). Confirm the 11
       capability-to-readable promoted fields decode their value from a
       single C0 frame BEFORE any B5 has arrived. Without the §2 promotion
       these fields would remain at current_value=null because
       process_data_frame skipped them.

  V2 — Default constraints applied at boot (gates §3). Confirm the new
       capability.default blocks populate active_constraints at boot for
       readable fields. Without §3 active_constraints would be null.

  V3 — Default → cap upgrade replacement (gates §3 + §2). Confirm that
       when a B5 capability arrives, the cap-resolved constraints
       OVERWRITE the boot defaults — they do not merge. The cap reading
       is authoritative.

  V4 — capability-only fields finalize to never (gates §2). Confirm
       fields with no rsp_0xc0.decode that did NOT get promoted still
       finalize to 'never' when their cap is missing from B5, AND that
       promoted readable fields are not regressed by finalize_capabilities.

  V0 — Sanity: distribution after the conservative-migration sensor
       demotion (always=18, readable=52, capability=18, never=3) —
       locks the current data state in place.

Usage:
    python tests/test_default_constraints.py
"""

import sys
from collections import Counter
from pathlib import Path

import yaml
from blaueis.core.codec import load_glossary, walk_fields
from blaueis.core.process import finalize_capabilities, process_raw_frame
from blaueis.core.query import read_field
from blaueis.core.status import build_status

REPO_TESTCASES = Path(__file__).resolve().parent / "test-cases"
B5_FIXTURE = REPO_TESTCASES / "xtremesaveblue_s1" / "b5_frames.yaml"
C0_FIXTURE = REPO_TESTCASES / "xtremesaveblue_s11_frames" / "c0_frames.yaml"

# Eleven fields promoted from `capability` to `readable` (decode pre-B5
# from rsp_0xc0; cap only refines write constraints).
PROMOTED_FIELDS = [
    "temperature_unit",
    "swing_vertical",
    "swing_horizontal",
    "eco_mode",
    "turbo_mode",
    "fan_speed",
    "frost_protection",
    "auxiliary_heat_level",
    "silky_cool",
    "screen_display",
    "humidity_setpoint",
]


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def process_frames(status, frames_data, glossary):
    for frame in frames_data["frames"]:
        body = bytes.fromhex(frame["body_hex"].replace(" ", "").replace("\n", ""))
        process_raw_frame(status, body, glossary, timestamp=str(frame.get("timestamp", "")))


def main():
    glossary = load_glossary()
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

    # ── V0: Distribution sanity ────────────────────────────────────
    print("\n  V0 — distribution after §2 promotion")
    status = build_status("test", glossary)
    fields = status["fields"]
    fa = Counter(f["feature_available"] for f in fields.values())
    check("always == 14", fa["always"] == 14, f"got {fa['always']}")
    check("readable == 120", fa["readable"] == 120, f"got {fa['readable']}")
    check("capability == 63", fa["capability"] == 63, f"got {fa['capability']}")
    check("never == 3", fa["never"] == 3, f"got {fa['never']}")

    # All 11 promoted fields are now `readable`
    for fname in PROMOTED_FIELDS:
        check(
            f"{fname} promoted to readable",
            fields[fname]["feature_available"] == "readable",
            f"got {fields[fname]['feature_available']}",
        )

    # ── V1: Cold-boot decode of promoted fields ────────────────────
    print("\n  V1 — cold-boot decode (no B5 yet)")
    status = build_status("test", glossary)
    c0_data = load_yaml(C0_FIXTURE)
    # Process only the FIRST C0 frame, no B5 first
    body = bytes.fromhex(c0_data["frames"][0]["body_hex"].replace(" ", ""))
    process_raw_frame(status, body, glossary)

    gt = c0_data["frames"][0]["ground_truth"]
    cold_boot_decoded = []
    for fname in PROMOTED_FIELDS:
        r = read_field(status, fname)
        actual = r["value"] if r else None
        expected = gt.get(fname)
        ok = actual == expected
        if ok:
            cold_boot_decoded.append(fname)
        else:
            check(
                f"{fname} decoded pre-B5 from C0[0]",
                ok,
                f"expected {expected!r}, got {actual!r}",
            )
    check(
        f"all 11 promoted fields decode pre-B5 (got {len(cold_boot_decoded)}/11)",
        len(cold_boot_decoded) == 11,
        f"missing: {set(PROMOTED_FIELDS) - set(cold_boot_decoded)}",
    )

    # ── V2: Default constraints at boot ────────────────────────────
    print("\n  V2 — capability.default applied at boot")
    status = build_status("test", glossary)
    fields = status["fields"]

    # fan_speed: discrete set [20, 40, 60, 80, 102] before B5
    fs_ac = fields["fan_speed"]["active_constraints"]
    check(
        "fan_speed default valid_set == [20, 40, 60, 80, 102]",
        fs_ac is not None and fs_ac.get("valid_set") == [20, 40, 60, 80, 102],
        f"got {fs_ac}",
    )

    # operating_mode: standard 5 modes
    om_ac = fields["operating_mode"]["active_constraints"]
    check(
        "operating_mode default valid_set == [1, 2, 3, 4, 5]",
        om_ac is not None and om_ac.get("valid_set") == [1, 2, 3, 4, 5],
        f"got {om_ac}",
    )

    # target_temperature: by_mode default 17-30°C 1°C steps
    tt_ac = fields["target_temperature"]["active_constraints"]
    cool_default = (tt_ac or {}).get("by_mode", {}).get("cool")
    check(
        "target_temperature.by_mode.cool default valid_range == [17, 30]",
        cool_default is not None and cool_default.get("valid_range") == [17, 30],
        f"got {cool_default}",
    )
    check(
        "target_temperature.by_mode.cool default step == 1.0",
        cool_default is not None and cool_default.get("step") == 1.0,
        f"got {cool_default}",
    )

    # temperature_unit: bool default
    tu_ac = fields["temperature_unit"]["active_constraints"]
    check(
        "temperature_unit default valid_set == [false, true]",
        tu_ac is not None and tu_ac.get("valid_set") == [False, True],
        f"got {tu_ac}",
    )

    # eco_mode: bool default
    em_ac = fields["eco_mode"]["active_constraints"]
    check(
        "eco_mode default valid_set == [false, true]",
        em_ac is not None and em_ac.get("valid_set") == [False, True],
        f"got {em_ac}",
    )

    # Every readable field that carries a `capability:` block must also
    # carry a `capability.default:` block — that is what build_status
    # consumes at boot before B5 arrives. Fields without a capability
    # block (pure sensors demoted to readable by the §10 partial
    # default-deny migration) are exempt: no capability = no constraints
    # to default.
    all_defs = walk_fields(load_glossary())
    readable_with_cap = [
        n
        for n, f in fields.items()
        if f["feature_available"] == "readable" and (all_defs.get(n) or {}).get("capability")
    ]
    no_default = [n for n in readable_with_cap if fields[n]["active_constraints"] is None]
    check(
        f"every readable field with a capability block has capability.default ({len(readable_with_cap)} checked)",
        len(no_default) == 0,
        f"missing default: {no_default}",
    )

    # Capability-only and always fields stay None at boot (they don't
    # get a boot default because they're either decoded directly or not
    # decodable at all yet).
    cap_with_default = [
        n for n, f in fields.items() if f["feature_available"] == "capability" and f["active_constraints"] is not None
    ]
    check(
        "capability-level fields have no boot default",
        len(cap_with_default) == 0,
        f"unexpected default: {cap_with_default}",
    )

    # ── V3: Cap upgrade overwrites default ────────────────────────
    print("\n  V3 — B5 cap upgrade replaces default")
    status = build_status("test", glossary)
    pre_b5_fan = status["fields"]["fan_speed"]["active_constraints"]
    process_frames(status, load_yaml(B5_FIXTURE), glossary)
    finalize_capabilities(status, glossary)

    fan_post = status["fields"]["fan_speed"]["active_constraints"]
    # Cap 0x10 = 1 (stepless) on Q11 → valid_range [0, 102], step 1
    check(
        "fan_speed.active_constraints replaced by cap (valid_range present)",
        fan_post is not None and "valid_range" in fan_post,
        f"got {fan_post}",
    )
    check(
        "fan_speed.active_constraints.valid_range == [0, 102]",
        fan_post is not None and fan_post.get("valid_range") == [0, 102],
        f"got {fan_post}",
    )
    check(
        "fan_speed default valid_set replaced (no longer present)",
        fan_post is not None and "valid_set" not in fan_post,
        f"still has valid_set: {fan_post}",
    )
    check(
        "fan_speed pre-B5 default was discrete set",
        pre_b5_fan is not None and pre_b5_fan.get("valid_set") == [20, 40, 60, 80, 102],
        f"got pre={pre_b5_fan}",
    )

    # target_temperature cap 0x25 → by_mode 16.0..30.0 (replaces default 17..30)
    tt_post = status["fields"]["target_temperature"]["active_constraints"]
    cool_post = (tt_post or {}).get("by_mode", {}).get("cool")
    check(
        "target_temperature.by_mode.cool replaced by cap data (16.0)",
        cool_post is not None and cool_post.get("valid_range") == [16.0, 30.0],
        f"got {cool_post}",
    )

    # fan_speed feature_available upgraded to 'always' by cap_value
    check(
        "fan_speed.feature_available upgraded to always",
        status["fields"]["fan_speed"]["feature_available"] == "always",
        f"got {status['fields']['fan_speed']['feature_available']}",
    )

    # ── V4: finalize_capabilities preserves readable ──────────────
    print("\n  V4 — finalize_capabilities preserves readable, marks unresolved capability as never")
    status = build_status("test", glossary)
    process_frames(status, load_yaml(B5_FIXTURE), glossary)
    finalize_capabilities(status, glossary)

    # Capability-only fields with no cap in B5 → never. Note that
    # anion_ionizer cap 0x1E IS in Session 1 B5 (Q11 supports it), so
    # it gets upgraded to 'always' — not a finalize-sweep candidate.
    # breeze_away (cap 0x33) and buzzer (cap 0x2c) genuinely are NOT
    # in S1 caps and have no rsp_0xc0 decode — those are the ones
    # finalize_capabilities() must mark as never.
    check(
        "breeze_away (no rsp_0xc0, cap 0x33 NOT in S1) → never",
        status["fields"]["breeze_away"]["feature_available"] == "never",
        f"got {status['fields']['breeze_away']['feature_available']}",
    )
    check(
        "buzzer (no rsp_0xc0, cap 0x2c NOT in S1) → never",
        status["fields"]["buzzer"]["feature_available"] == "never",
        f"got {status['fields']['buzzer']['feature_available']}",
    )
    # And conversely: anion_ionizer cap 0x1E IS in S1 → upgraded to always.
    check(
        "anion_ionizer (cap 0x1E IS in S1) → always (cap-driven upgrade)",
        status["fields"]["anion_ionizer"]["feature_available"] == "always",
        f"got {status['fields']['anion_ionizer']['feature_available']}",
    )

    # Promoted readable fields whose cap IS in S1 must NOT be regressed to never.
    # Cap 0x15 (swing) is in S1 with value 1 = both → upgrades to always.
    # Cap 0x22 (temperature_unit) is in S1 with value 0 = changeable → upgrades to always.
    check(
        "swing_vertical (readable, cap 0x15 in S1) NOT regressed",
        status["fields"]["swing_vertical"]["feature_available"] != "never",
        f"got {status['fields']['swing_vertical']['feature_available']}",
    )
    check(
        "temperature_unit (readable, cap 0x22 in S1) NOT regressed",
        status["fields"]["temperature_unit"]["feature_available"] != "never",
        f"got {status['fields']['temperature_unit']['feature_available']}",
    )
    check(
        "fan_speed (readable, cap 0x10 in S1) NOT regressed",
        status["fields"]["fan_speed"]["feature_available"] != "never",
        f"got {status['fields']['fan_speed']['feature_available']}",
    )

    # Negative case: a readable field whose cap is NOT in B5 should keep
    # the readable level (NOT downgrade to never), because the receiver
    # can still decode it from rsp_0xc0. auxiliary_heat_level cap 0x19 = 0
    # in S1 (per Session 1 B5: '0x19=0 → not supported on Q11'), so its
    # cap_value 'not_supported' has feature_available: never. After cap
    # upgrade auxiliary_heat_level becomes 'never' — that's the correct
    # cap-driven state, not a finalize-sweep regression.
    aux_state = status["fields"]["auxiliary_heat_level"]["feature_available"]
    check(
        "auxiliary_heat_level state after S1 (cap=0 → never) is cap-driven, not regression",
        aux_state == "never",
        f"got {aux_state}",
    )

    # ── Summary ──────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
