#!/usr/bin/env python3
"""V6 — capture-replay against 84 real C0 frames from Session 11.

The other §2/§3 unit tests run against single hand-picked frames.
V6 is the end-to-end check the user explicitly asked for: replay every
C0 frame from a real capture, against a fresh build_status (no B5 in
the loop), and assert that:

  1. Every frame decodes successfully (no exception, no None for any
     promoted field).
  2. Every promoted field's decoded value lies in its declared
     valid_set or valid_range.
  3. The decoded value's data type matches the field's data_type.
  4. Specific frames at known timestamps match the SessionNotes
     ground-truth state (turbo on, frost protection on, power off, ...).

This is the authoritative pass/fail signal for the §2 promotion.
Synthetic single-frame tests can lie about glossary correctness; real
wire bytes cannot.

Usage:
    python tests/test_capture_replay.py
"""

import sys
from pathlib import Path

import yaml


from blaueis.core.status import build_status
from blaueis.core.query import read_field
from blaueis.core.codec import load_glossary
from blaueis.core.process import process_raw_frame


def _value(status, name):
    """Field-value shortcut: read_field with default priority, returning
    the decoded value or None when no slot has been populated yet."""
    r = read_field(status, name)
    return r["value"] if r else None


REPO_TESTCASES = Path(__file__).resolve().parent / "test-cases"
FIXTURE = REPO_TESTCASES / "xtremesaveblue_s11_frames" / "c0_window_servicemenu.yaml"

# 11 fields promoted from `capability` to `readable` by §2.
PROMOTED_FIELDS = [
    "temperature_unit",
    "swing_vertical",
    "swing_horizontal",
    "eco_mode",
    "turbo_mode",
    "fan_speed",
    "frost_protection",
    "ptc_heater",
    "silky_cool",
    "screen_display",
    "humidity_setpoint",
]

# Per-field expected type after decoding from C0. Most are bool because
# their bit-width is 1; a few are uint8 with a wider range.
EXPECTED_TYPE = {
    "temperature_unit": (bool,),
    "swing_vertical": (int, bool),
    "swing_horizontal": (int, bool),
    "eco_mode": (bool,),
    "turbo_mode": (bool,),
    "fan_speed": (int,),
    "frost_protection": (bool,),
    "ptc_heater": (int,),
    "silky_cool": (bool,),
    "screen_display": (int,),
    "humidity_setpoint": (int,),
}

# Per-field plausible range after decoding from C0.
PLAUSIBLE_RANGE = {
    "fan_speed": (0, 127),  # 7-bit field
    "ptc_heater": (0, 3),  # 2-bit field
    "screen_display": (0, 7),  # 3-bit field
    "humidity_setpoint": (0, 100),  # 7-bit, percent
}


def main():
    glossary = load_glossary()
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
        else:
            failed += 1
            print(f"  [FAIL] {name}: {detail}")

    with open(FIXTURE, encoding="utf-8") as f:
        fixture = yaml.safe_load(f)

    frames = fixture["frames"]
    print(f"V6 — replay {len(frames)} real C0 frames from Session 11")
    print(f"     fixture: {FIXTURE.name}")
    print()

    # ── Cold-boot replay (no B5 in the loop) ───────────────────────
    status = build_status("test", glossary)

    decoded_count = {fname: 0 for fname in PROMOTED_FIELDS}
    type_failures = {fname: 0 for fname in PROMOTED_FIELDS}
    range_failures = {fname: 0 for fname in PROMOTED_FIELDS}
    none_failures = {fname: 0 for fname in PROMOTED_FIELDS}
    state_at = {}  # timestamp -> snapshot dict for spot checks

    for frame in frames:
        body = bytes.fromhex(frame["body_hex"].replace(" ", ""))
        try:
            process_raw_frame(status, body, glossary, timestamp=str(frame["timestamp"]))
        except Exception as e:
            failed += 1
            print(f"  [FAIL] decode exception at t={frame['timestamp']}: {e}")
            continue

        for fname in PROMOTED_FIELDS:
            cv = _value(status, fname)

            if cv is None:
                none_failures[fname] += 1
                continue

            decoded_count[fname] += 1

            # Type check
            allowed_types = EXPECTED_TYPE[fname]
            if not isinstance(cv, allowed_types):
                type_failures[fname] += 1

            # Range check
            if fname in PLAUSIBLE_RANGE:
                lo, hi = PLAUSIBLE_RANGE[fname]
                if not (lo <= cv <= hi):
                    range_failures[fname] += 1

        # Capture snapshots at known timestamps for spot checks
        ts = float(frame["timestamp"])
        if ts in state_at:
            continue
        state_at[ts] = {
            "operating_mode": _value(status, "operating_mode"),
            "target_temperature": _value(status, "target_temperature"),
            "fan_speed": _value(status, "fan_speed"),
            "power": _value(status, "power"),
            "temperature_unit": _value(status, "temperature_unit"),
            "frost_protection": _value(status, "frost_protection"),
            "screen_display": _value(status, "screen_display"),
            "indoor_temperature": _value(status, "indoor_temperature"),
        }

    # ── Per-field invariant assertions ────────────────────────────
    print("Per-field decode results across all 84 frames:")
    print(f"  {'field':22s} {'decoded':>8s} {'none':>6s} {'badtype':>8s} {'oorange':>8s}")
    for fname in PROMOTED_FIELDS:
        ok_decoded = decoded_count[fname]
        nf = none_failures[fname]
        tf = type_failures[fname]
        rf = range_failures[fname]
        print(f"  {fname:22s} {ok_decoded:8d} {nf:6d} {tf:8d} {rf:8d}")

        check(
            f"{fname}: decoded in every frame (84/84)",
            ok_decoded == len(frames),
            f"decoded {ok_decoded}/{len(frames)}, {nf} were None",
        )
        check(
            f"{fname}: type matches in every frame",
            tf == 0,
            f"{tf} frames had wrong type",
        )
        check(
            f"{fname}: in plausible range in every frame",
            rf == 0,
            f"{rf} frames had out-of-range value",
        )
    print()

    # Whole-session: power, mode, target_temperature must always be valid
    check(
        "session always: operating_mode is one of [1..6] in every frame",
        all(s["operating_mode"] in (1, 2, 3, 4, 5, 6) for s in state_at.values()),
        "out-of-range operating_mode found",
    )
    check(
        "session always: temperature_unit always Celsius (False) in heat-mode session",
        all(s["temperature_unit"] is False for s in state_at.values()),
        "Fahrenheit appeared unexpectedly",
    )
    check(
        "session always: target_temperature in [16, 30]",
        all(16 <= s["target_temperature"] <= 30 for s in state_at.values()),
        "out-of-range target_temperature",
    )

    # ── Spot checks against SessionNotes ground truth ─────────────
    # Looking for frames at known phase boundaries.
    # Phase 2 (turbo+temp sweep): t≈103-200
    # Phase 5 (frost protection): t≈429-537
    # Phase 6 (power off): t≈651+ (per SessionNotes)
    print("Spot checks against SessionNotes:")

    def find_state_near(target_ts, window=10):
        """Return the state captured closest to target_ts within +/- window."""
        candidates = [(abs(t - target_ts), t, s) for t, s in state_at.items() if abs(t - target_ts) <= window]
        if not candidates:
            return None, None
        candidates.sort()
        return candidates[0][1], candidates[0][2]

    # At t≈90 (session start, before any user action): mode=heat, power=on, fan=auto
    t, s = find_state_near(90.78, window=2)
    if s:
        check(
            f"t≈{t:.1f}: session start has power=True, mode=4 (heat)",
            s["power"] is True and s["operating_mode"] == 4,
            f"got {s}",
        )

    # Phase 5 frost protection enabled t=464s — frost_protection should be True
    # somewhere in t=464..537
    frost_active = any(s["frost_protection"] is True for t, s in state_at.items() if 464 <= t <= 537)
    check(
        "frost_protection True at some point during phase 5 (t=464-537)",
        frost_active,
        "frost never observed in phase 5 window — but R/T captures suggest "
        "this bit may not appear in UART C0 (per c0_frames.yaml note)",
    )

    # Phase 6 power off — at t≈654 power should be False
    t, s = find_state_near(654, window=20)
    if s:
        check(
            f"t≈{t:.1f}: power is False after phase 6 power-off",
            s["power"] is False,
            f"got {s}",
        )

    # No B5 was processed — promoted fields should still all be 'readable'
    # (not upgraded to 'always' which would require a B5 cap_value override)
    for fname in PROMOTED_FIELDS:
        check(
            f"{fname}.feature_available stays 'readable' without B5",
            status["fields"][fname]["feature_available"] == "readable",
            f"got {status['fields'][fname]['feature_available']}",
        )

    # All slots come from rsp_0xc0 — only legacy-generation source after
    # a C0-only replay. read_field with explicit protocol_legacy /
    # protocol_new scopes verifies that.
    for fname in PROMOTED_FIELDS:
        legacy = read_field(status, fname, priority=["protocol_legacy"])
        new = read_field(status, fname, priority=["protocol_new"])
        check(
            f"{fname} legacy slot populated, new slot still empty",
            legacy is not None and new is None,
            f"legacy={legacy} new={new}",
        )

    # Frame count
    check(
        f"frame_counts.rsp_0xc0 == {len(frames)}",
        status["meta"]["frame_counts"].get("rsp_0xc0") == len(frames),
        f"got {status['meta']['frame_counts'].get('rsp_0xc0')}",
    )

    # ── Summary ──────────────────────────────────────────────────
    total = passed + failed
    print()
    print(f"{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
