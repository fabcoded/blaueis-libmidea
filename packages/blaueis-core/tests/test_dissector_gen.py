#!/usr/bin/env python3
"""
Verify that the generated dissector Lua tables produce the same decode
results as the Python codec for all C0 test frames.

This doesn't run Lua — it re-implements the generated registry's decode
logic in Python and compares against midea_codec.decode_frame_fields().
If these match, the Lua (which uses the same registry data) will also match.

Run: python tools-serial/tests/test_dissector_gen.py
"""

import os
import sys
from pathlib import Path

import yaml

TOOLS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent / "test-cases"
SPEC = Path(__file__).resolve().parents[2] / "spec"
DISSECTOR = Path(__file__).resolve().parents[4] / "tools" / "dissector"

from blaueis.core.codec import build_field_map, decode_frame_fields, load_glossary  # noqa: E402


def extract_bits(byte_val, bits):
    high, low = bits
    mask = ((1 << (high - low + 1)) - 1) << low
    return (byte_val & mask) >> low


def simulate_registry_decode(body: bytes, glossary: dict) -> dict:
    """
    Simulate the generated Lua registry decode in Python.

    This mirrors the logic in the generated glossary_decode_registry Lua
    function, using the same field registry data that the generator emits.
    """
    field_map = build_field_map(glossary, "rsp_0xc0")
    encodings = glossary.get("encodings", {})
    result = {}

    for field in field_map:
        name = field["name"]
        decode = field["decode"]
        dtype = field["data_type"]

        if not decode:
            continue

        # Skip composite/derived fields (deferred in generated Lua)
        fdef = _find_field_def(glossary, name)
        if fdef and fdef.get("field_class") == "complex_function" and fdef.get("composite"):
            continue

        # Skip property_id fields (B1 registry, not C0)
        if decode[0].get("property_id"):
            continue

        val = None

        # Logic combiner
        if "logic" in decode[0]:
            if decode[0]["logic"] == "or":
                val = False
                for src in decode[0].get("sources", []):
                    if src["offset"] < len(body):
                        sv = extract_bits(body[src["offset"]], src["bits"])
                        if sv != 0:
                            val = True
            result[name] = val
            continue

        # Walk decode steps (first match wins)
        for step in decode:
            offset = step["offset"]
            bits = step["bits"]

            if offset >= len(body):
                continue

            raw = extract_bits(body[offset], bits)

            # Condition
            cond = step.get("condition")
            if cond:
                if cond == "!= 0" and raw == 0:
                    continue
                if cond == "> 0" and raw <= 0:
                    continue

            val = raw

            # Add
            add = step.get("add")
            if add is not None:
                val += add

            # Half bit
            hb = step.get("half_bit")
            if hb and hb["offset"] < len(body):  # noqa: SIM102
                if body[hb["offset"]] & (1 << hb["bit"]):
                    val += 0.5

            # Encoding
            enc = step.get("encoding")
            if enc and enc in encodings:
                edef = encodings[enc]
                off = edef.get("offset", 0)
                sc = edef.get("scale", 1.0)
                if sc != 0:
                    val = (val - off) * sc

            # Tenths nibble
            tn = step.get("tenths_nibble")
            if tn and tn.get("offset", 0) < len(body):
                raw_tn = body[tn["offset"]]
                if tn.get("nibble") == "high":  # noqa: SIM108
                    nibble = (raw_tn >> 4) & 0x0F
                else:
                    nibble = raw_tn & 0x0F
                val += nibble / 10

            # Bool coercion
            if dtype == "bool" and bits[0] == bits[1]:
                val = bool(val)

            break

        if val is not None:
            result[name] = val

    return result


def _find_field_def(glossary, name):
    for cat in ("control", "sensor"):
        if name in glossary["fields"][cat]:
            return glossary["fields"][cat][name]
    return None


def main():
    glossary = load_glossary()
    passed = failed = 0

    # Load all C0 test frames
    c0_files = sorted(TESTS.rglob("c0_frames.yaml"))
    print(f"Verifying generated registry against {len(c0_files)} C0 fixture files\n")

    for c0_file in c0_files:
        with open(c0_file) as f:
            data = yaml.safe_load(f)

        frames = data.get("frames", [])
        session = data.get("session", c0_file.parent.name)
        print(f"--- {session} ({len(frames)} frames) ---")

        for frame in frames:
            body_hex = frame["body_hex"]
            body = bytes(int(x, 16) for x in body_hex.strip().split())
            name = frame.get("name", "?")

            # Decode via Python codec
            codec_result = decode_frame_fields(body, "rsp_0xc0", glossary)

            # Decode via simulated registry
            reg_result = simulate_registry_decode(body, glossary)

            # Compare
            mismatches = []
            for fname, reg_val in reg_result.items():
                codec_entry = codec_result.get(fname)
                if codec_entry is None:
                    continue
                codec_val = codec_entry["value"]

                # Normalize for comparison
                if isinstance(reg_val, float) and isinstance(codec_val, float):
                    if abs(reg_val - codec_val) > 0.01:
                        mismatches.append(f"{fname}: reg={reg_val:.1f} codec={codec_val:.1f}")
                elif reg_val != codec_val:
                    mismatches.append(f"{fname}: reg={reg_val} codec={codec_val}")

            if mismatches:
                print(f"  [FAIL] {name}: {'; '.join(mismatches[:5])}")
                failed += 1
            else:
                print(f"  [PASS] {name} ({len(reg_result)} fields)")
                passed += 1

    # Verify glossary code is injected in the dissector between markers
    dissector_file = DISSECTOR / "HVAC-shark_mid-xye.lua"
    if dissector_file.exists():
        dissector_src = dissector_file.read_text(encoding="utf-8")
        has_start = "GLOSSARY-GEN-START" in dissector_src
        has_end = "GLOSSARY-GEN-END" in dissector_src
        has_c0 = "GLOSSARY_C0" in dissector_src
        if has_start and has_end and has_c0:
            print(f"\n  [PASS] Glossary code injected inline in {dissector_file.name}")
            passed += 1
        else:
            print(f"\n  [FAIL] Glossary markers or tables missing in {dissector_file.name}")
            failed += 1
    else:
        print("\n  [FAIL] Dissector file not found")
        failed += 1

    print(f"\nResults: {passed} passed, {failed} failed / {passed + failed} total")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[2])
    main()
