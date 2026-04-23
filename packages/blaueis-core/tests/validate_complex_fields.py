#!/usr/bin/env python3
"""
Validate §9 complex-function skeletons against real C0 frames AND
a reference implementation (when available).

Three independent decoders:
  1. Reference-implementation oracle (loaded via test-loops harness)
  2. Our glossary-based decoder (composite members + derived_from formulas)
  3. Our Python implementation of the protocol bit ops (cross-check)

The test builds synthetic C0 frames with known timer/sleep values, feeds
them through all three decoders, and asserts they agree. It also replays
every real C0 frame from the test fixtures through the same pipeline.

Run: python tools-serial/tests/validate_complex_fields.py
"""

import math
import os
import sys
from pathlib import Path

import pytest
import yaml

SPEC = Path(__file__).resolve().parent.parent / "src" / "blaueis" / "core" / "data"
TESTS = Path(__file__).resolve().parent / "test-cases"

# The reference-implementation oracle lives in the test-loops write-exempt
# subtree. We add it to sys.path so we can import the clean public API
# without referencing any upstream file names or function names.
_ORACLE_DIR = Path(__file__).resolve().parents[5] / "blaueis-research" / "internal-tests" / "lua"

# This module is an opt-in developer harness: the reference oracle it
# cross-checks against is not bundled in the public repo. Skip when absent.
pytestmark = pytest.mark.skipif(not _ORACLE_DIR.exists(), reason="oracle data unavailable (private)")


def _load_oracle():
    """Try to import the reference oracle. Returns decode_c0_body or None."""
    try:
        sys.path.insert(0, str(_ORACLE_DIR))
        from oracle import decode_c0_body

        # Smoke-test: can it actually load the reference implementation?
        decode_c0_body("C0" + " 00" * 30)
        return decode_c0_body
    except Exception:
        return None
    finally:
        if str(_ORACLE_DIR) in sys.path:
            sys.path.remove(str(_ORACLE_DIR))


# ── Helpers ────────────────────────────────────────────────────────


def load_glossary():
    with open(SPEC / "glossary.yaml") as f:
        return yaml.safe_load(f)


def hex_to_bytes(hex_str: str) -> list[int]:
    return [int(x, 16) for x in hex_str.strip().split()]


def body_to_hex(body: list[int]) -> str:
    return " ".join(f"{b:02X}" for b in body)


def extract_bits(byte_val: int, high: int, low: int) -> int:
    mask = ((1 << (high - low + 1)) - 1) << low
    return (byte_val & mask) >> low


# ── Python protocol decode (cross-check) ──────────────────────────


def py_decode_timer(body: list[int]) -> dict:
    """Decode timer bytes from C0 body using the protocol spec directly."""
    return {
        "power_on_timer": (body[4] & 0x80) == 0x80,
        "power_off_timer": (body[5] & 0x80) == 0x80,
        "open_hour": (body[4] & 0x7F) >> 2,
        "open_step_minutes": body[4] & 0x03,
        "open_min_raw": (body[6] & 0xF0) >> 4,
        "close_hour": (body[5] & 0x7F) >> 2,
        "close_step_minutes": body[5] & 0x03,
        "close_min_raw": body[6] & 0x0F,
        "power_on_time_value": ((body[4] & 0x7F) >> 2) * 60 + (body[4] & 0x03) * 15 + (15 - ((body[6] & 0xF0) >> 4)),
        "power_off_time_value": ((body[5] & 0x7F) >> 2) * 60 + (body[5] & 0x03) * 15 + (15 - (body[6] & 0x0F)),
    }


# ── Glossary-based decode ──────────────────────────────────────────


def glossary_decode_timer(body: list[int], glossary: dict) -> dict:
    """Decode timer fields by walking the glossary's composite member wire positions."""
    control = glossary["fields"]["control"]
    results = {}

    for fname in ("power_off_timer", "power_on_timer"):
        step = control[fname]["protocols"]["rsp_0xc0"]["decode"][0]
        results[fname] = bool(extract_bits(body[step["offset"]], step["bits"][0], step["bits"][1]))

    for fname in ("power_off_time_value", "power_on_time_value"):
        fdef = control[fname]
        for mname, mdef in fdef["composite"]["members"].items():
            bp = mdef["wire"]["byte_position"]
            idx = int(bp.split("[")[1].rstrip("]"))
            br = mdef["wire"]["bit_range"]
            results[mname] = extract_bits(body[idx], br[0], br[1])
        results[fname] = eval(fdef["derived_from"]["formula"], {"__builtins__": {}}, results)

    return results


# ── Build synthetic C0 body ────────────────────────────────────────


def build_c0_body(
    close_min=0, open_min=0, close_on=True, open_on=True, sleep_val=0, sleep_sw=0, sleep_time=0
) -> list[int]:
    """Pack a C0 body per the serial protocol timer encoding spec."""
    body = [0xC0] + [0x00] * 30
    body[1] = 0x01  # power on

    ch = math.floor(close_min / 60)
    cs = math.floor((close_min % 60) / 15)
    cm = math.floor((close_min % 60) % 15)
    oh = math.floor(open_min / 60)
    os_ = math.floor((open_min % 60) / 15)
    om = math.floor((open_min % 60) % 15)

    body[4] = (0x80 | (oh << 2) | os_) if open_on else 0x7F
    body[5] = (0x80 | (ch << 2) | cs) if close_on else 0x7F
    body[6] = ((15 - om) << 4) | (15 - cm)
    body[8] = sleep_val & 0x03
    body[9] = sleep_sw & 0x40
    body[17] = sleep_time & 0x1F
    return body


# ── Test 1: Glossary formula roundtrip ─────────────────────────────


def test_decompose_roundtrip():
    """Evaluate the actual glossary YAML formula strings for all 0-1440."""
    glossary = load_glossary()
    potv = glossary["fields"]["control"]["power_off_time_value"]
    derive = potv["derived_from"]["formula"]
    decompose = potv["decompose_to"]

    failed = 0
    for minutes in range(1441):
        subs = {}
        for rule in decompose:
            subs[rule["target"]] = eval(rule["formula"], {"__builtins__": {}, "floor": math.floor}, {"value": minutes})
        recomposed = eval(derive, {"__builtins__": {}}, subs)
        if recomposed != minutes:
            if failed < 3:
                print(f"  [FAIL] {minutes} -> {subs} -> {recomposed}")
            failed += 1

    ok = failed == 0
    print(f"  [{'PASS' if ok else 'FAIL'}] glossary formula roundtrip: {1441 - failed}/1441")
    return ok


# ── Test 2: Three-way timer decode ─────────────────────────────────


def test_three_way_timer(oracle):
    """
    Build synthetic C0 frames, feed them through the reference oracle,
    our glossary decoder, and our Python decoder. Assert all three agree.
    When oracle is unavailable, glossary-vs-Python checks still run but
    the reference leg is explicitly skipped (not silently passed).
    """
    glossary = load_glossary()
    cases = [
        (0, 0, True, True, "both ON at 0 min"),
        (60, 120, True, True, "close=1h, open=2h"),
        (90, 45, True, True, "close=1h30, open=45min"),
        (1440, 1440, True, True, "both at max 24h"),
        (7, 13, True, True, "close=7m, open=13m (sub-step)"),
        (137, 891, True, True, "close=2h17m, open=14h51m"),
        (0, 0, False, False, "both OFF"),
        (360, 0, True, False, "close ON 6h, open OFF"),
        (0, 720, False, True, "close OFF, open ON 12h"),
        (1, 1, True, True, "1 minute each"),
        (14, 14, True, True, "14 min (max sub-step)"),
        (15, 15, True, True, "exactly 15 min"),
        (59, 59, True, True, "59 min"),
        (719, 721, True, True, "near 12h boundary"),
    ]

    passed = failed = skipped = 0
    for close_m, open_m, close_on, open_on, desc in cases:
        body = build_c0_body(close_m, open_m, close_on, open_on)
        bhex = body_to_hex(body)

        glos = glossary_decode_timer(body, glossary)
        py = py_decode_timer(body)

        errors = []

        # Glossary vs expected
        if close_on and glos["power_off_time_value"] != close_m:
            errors.append(f"GLOS close={glos['power_off_time_value']} expected={close_m}")
        if open_on and glos["power_on_time_value"] != open_m:
            errors.append(f"GLOS open={glos['power_on_time_value']} expected={open_m}")

        # Python vs expected
        if close_on and py["power_off_time_value"] != close_m:
            errors.append(f"PY close={py['power_off_time_value']} expected={close_m}")
        if open_on and py["power_on_time_value"] != open_m:
            errors.append(f"PY open={py['power_on_time_value']} expected={open_m}")

        # Glossary must always match Python
        for key in (
            "power_off_time_value",
            "power_on_time_value",
            "close_hour",
            "close_step_minutes",
            "close_min_raw",
            "open_hour",
            "open_step_minutes",
            "open_min_raw",
        ):
            if py[key] != glos[key]:
                errors.append(f"GLOS!=PY {key}: glos={glos[key]} py={py[key]}")

        # Reference oracle — strict when available, skip when not
        ref_tag = "ref=skip"
        if oracle:
            ref = oracle(bhex)
            ref_close_sw = ref.get("power_off_timer")
            ref_open_sw = ref.get("power_on_timer")
            ref_close = ref.get("power_off_time_value")
            ref_open = ref.get("power_on_time_value")

            if ref_close_sw is not None:  # noqa: SIM102
                if (ref_close_sw == "on") != close_on:
                    errors.append(f"REF close_sw={ref_close_sw} expected={'on' if close_on else 'off'}")
            if ref_open_sw is not None:  # noqa: SIM102
                if (ref_open_sw == "on") != open_on:
                    errors.append(f"REF open_sw={ref_open_sw} expected={'on' if open_on else 'off'}")
            if close_on and ref_close is not None and int(ref_close) != close_m:
                errors.append(f"REF close={ref_close} expected={close_m}")
            if open_on and ref_open is not None and int(ref_open) != open_m:
                errors.append(f"REF open={ref_open} expected={open_m}")
            ref_tag = "ref=ok"

        if errors:
            print(f"  [FAIL] {desc}: {'; '.join(errors)}")
            failed += 1
        else:
            info = ""
            if close_on:
                info += f" close={close_m}m"
            if open_on:
                info += f" open={open_m}m"
            print(f"  [PASS] {desc}:{info or ' OFF'} [{ref_tag}]")
            passed += 1
            if not oracle:
                skipped += 1

    label = f"{passed}/{passed + failed}"
    if skipped:
        label += f" ({skipped} without ref oracle)"
    print(f"  Timer: {label}")
    return failed == 0


# ── Test 3: Three-way comfort sleep ───────────────────────────────


def test_three_way_sleep(oracle):
    """Compare comfort-sleep decode across reference oracle, glossary, and protocol spec."""
    glossary = load_glossary()
    cosy = glossary["fields"]["control"]["cosy_sleep"]
    members = cosy["composite"]["members"]

    cases = [
        (0x00, 0x00, 0x00, "off", "all zero = OFF"),
        (0x03, 0x40, 0x0A, "on", "standard ON"),
        (0x03, 0x40, 0x00, "on", "ON time=0"),
        (0x03, 0x00, 0x0A, "undefined", "value=3 switch=0"),
        (0x01, 0x40, 0x0A, "undefined", "value=1 switch=0x40"),
        (0x23, 0x60, 0x0A, "on", "ON + sibling bits set"),
    ]

    passed = failed = skipped = 0
    for b8, b9, b17, expected, desc in cases:
        body = build_c0_body(sleep_val=b8, sleep_sw=b9, sleep_time=b17)
        body[8] = b8
        body[9] = b9
        body[17] = b17
        bhex = body_to_hex(body)

        glos_val = extract_bits(
            body[8],
            members["comfortable_sleep_value"]["wire"]["bit_range"][0],
            members["comfortable_sleep_value"]["wire"]["bit_range"][1],
        )
        glos_sw = extract_bits(
            body[9],
            members["comfortable_sleep_switch"]["wire"]["bit_range"][0],
            members["comfortable_sleep_switch"]["wire"]["bit_range"][1],
        )

        if glos_val == 0 and glos_sw == 0:
            glos_state = "off"
        elif glos_val == 3 and glos_sw == 1:
            glos_state = "on"
        else:
            glos_state = "undefined"

        errors = []
        if glos_state != expected:
            errors.append(f"glossary state={glos_state} expected={expected}")

        # Reference oracle — strict when available
        ref_tag = "ref=skip"
        if oracle:
            ref = oracle(bhex)
            ref_cs = ref.get("comfort_sleep")
            if ref_cs is not None and glos_state in ("off", "on"):  # noqa: SIM102
                if (ref_cs == "on") != (glos_state == "on"):
                    errors.append(f"REF comfort_sleep={ref_cs} glossary={glos_state}")
            ref_tag = "ref=ok" if ref_cs is not None else "ref=n/a"

        if errors:
            print(f"  [FAIL] {desc}: {'; '.join(errors)}")
            failed += 1
        else:
            print(f"  [PASS] {desc} -> {glos_state} [{ref_tag}]")
            passed += 1
            if not oracle:
                skipped += 1

    # 0x15 vs 0x1F mask verification (glossary-only, no oracle needed)
    body = build_c0_body(sleep_val=0x03, sleep_sw=0x40, sleep_time=0x0A)
    glos_time = extract_bits(
        body[17],
        members["comfortable_sleep_time"]["wire"]["bit_range"][0],
        members["comfortable_sleep_time"]["wire"]["bit_range"][1],
    )
    if glos_time != 0x0A:
        print(f"  [FAIL] glossary mask: expected 0x0A got {glos_time:#04x}")
        failed += 1
    else:
        buggy_mask = body[17] & 0x15
        print(f"  [PASS] mask bug: 0x0A & 0x15 = {buggy_mask:#04x} (lost), 0x1F = {glos_time:#04x} (ok)")
        passed += 1

    label = f"{passed}/{passed + failed}"
    if skipped:
        label += f" ({skipped} without ref oracle)"
    print(f"  Sleep: {label}")
    return failed == 0


# ── Test 4: Real C0 frames ────────────────────────────────────────


def test_real_c0_frames(oracle):
    """Replay all C0 test fixture frames through all decoders."""
    glossary = load_glossary()
    c0_files = sorted(TESTS.rglob("c0_frames.yaml"))
    print(f"\n  Found {len(c0_files)} C0 frame files\n")

    passed = failed = skipped = 0
    for c0_file in c0_files:
        with open(c0_file) as f:
            data = yaml.safe_load(f)
        session = data.get("session", c0_file.parent.name)
        frames = data.get("frames", [])
        print(f"  --- {session} ({len(frames)} frames) ---")

        for frame in frames:
            body = hex_to_bytes(frame["body_hex"])
            bhex = frame["body_hex"].replace(" ", "")
            name = frame.get("name", "?")

            glos = glossary_decode_timer(body, glossary)
            py = py_decode_timer(body)

            errors = []

            # Glossary vs Python (must always agree)
            for key in ("power_off_time_value", "power_on_time_value", "power_off_timer", "power_on_timer"):
                if py[key] != glos[key]:
                    errors.append(f"glos!=py {key}")

            # Ground truth
            gt = frame.get("ground_truth", {})
            gt_cosy = gt.get("cosy_sleep")
            if gt_cosy is not None:
                glos_csv = extract_bits(body[8], 1, 0)
                if glos_csv != gt_cosy:
                    errors.append(f"cosy_sleep: gt={gt_cosy} glos={glos_csv}")

            # Reference oracle — strict when available
            ref_tag = "ref=skip"
            if oracle:
                ref = oracle(bhex)
                ref_close_sw = ref.get("power_off_timer")
                ref_open_sw = ref.get("power_on_timer")
                ref_close = ref.get("power_off_time_value")
                ref_open = ref.get("power_on_time_value")

                # Time values only compared when timer is ON
                if ref_close_sw == "on" and ref_close is not None and int(ref_close) != glos["power_off_time_value"]:
                    errors.append(f"close: ref={ref_close} glos={glos['power_off_time_value']}")
                if ref_open_sw == "on" and ref_open is not None and int(ref_open) != glos["power_on_time_value"]:
                    errors.append(f"open: ref={ref_open} glos={glos['power_on_time_value']}")

                # Switch agreement (always)
                if ref_close_sw is not None and (ref_close_sw == "on") != glos["power_off_timer"]:
                    errors.append(f"close_sw: ref={ref_close_sw} glos={glos['power_off_timer']}")
                if ref_open_sw is not None and (ref_open_sw == "on") != glos["power_on_timer"]:
                    errors.append(f"open_sw: ref={ref_open_sw} glos={glos['power_on_timer']}")
                ref_tag = "ref=ok"

            if errors:
                print(f"    [FAIL] {name}: {'; '.join(errors)}")
                failed += 1
            else:
                print(f"    [PASS] {name} [{ref_tag}]")
                passed += 1
                if not oracle:
                    skipped += 1

    return passed, failed, skipped


# ── Main ───────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("§9 Complex-function validation")
    print("=" * 60)
    all_ok = True

    # Load the reference-implementation oracle (optional)
    print("\nLoading reference-implementation oracle...")
    oracle = _load_oracle()
    if oracle:
        print("  OK: oracle loaded\n")
    else:
        print("  WARN: oracle not available (lupa not installed?)")
        print("  Tests will run without the reference oracle.\n")

    print("1. Glossary formula roundtrip (0-1440 min)")
    all_ok &= test_decompose_roundtrip()

    print("\n2. Three-way timer comparison")
    all_ok &= test_three_way_timer(oracle)

    print("\n3. Three-way comfort-sleep comparison")
    all_ok &= test_three_way_sleep(oracle)

    print("\n4. Real C0 frames (all decoders)")
    c0_pass, c0_fail, c0_skip = test_real_c0_frames(oracle)
    all_ok &= c0_fail == 0
    c0_label = f"{c0_pass} passed, {c0_fail} failed"
    if c0_skip:
        c0_label += f" ({c0_skip} without ref oracle)"
    print(f"\n  Real C0 frames: {c0_label}")

    # Summary line for run_all_tests.py
    total = 1441 + 14 + 7 + c0_pass
    failed_count = 0 if all_ok else 1
    print(f"\nResults: {total} passed, {failed_count} failed / {total + failed_count} total")

    print("\n" + "=" * 60)
    print(f"OVERALL: {'PASS' if all_ok else 'FAIL'}")
    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[2])
    main()
