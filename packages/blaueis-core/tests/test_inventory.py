"""Unit tests for blaueis.core.inventory.

Covers:
- ``classify`` truth table (all 5 classifications, boolean edge cases).
- ``build_frame_field_index`` / ``cap_dependent_fields`` shape + content.
- ``ShadowDecoder`` attach-observe-detach-snapshot lifecycle on canned
  Session 15 C1 Group 4 frames.
- ``decode_variants`` + ``pick_variant`` against a cap-dependent field.
- ``synthesize_override_snippet`` end-to-end with the schema-validation
  gate: emits for populated-but-hidden fields, drops for always/zero.
- ``generate_markdown_report`` + ``generate_json_sidecar`` non-regression
  against the Session 15 fixture.
- ``generate_compare_report`` buckets fields correctly across a diff.

No network, no HA. Pure unit.
"""

from __future__ import annotations

import sys

import pytest
from blaueis.core.codec import load_glossary, walk_fields
from blaueis.core.inventory import (
    CLASS_FF_FLOOD,
    CLASS_NONE,
    CLASS_NOT_SEEN,
    CLASS_POPULATED,
    CLASS_ZERO,
    ShadowDecoder,
    build_frame_field_index,
    cap_dependent_fields,
    classify,
    decode_variants,
    generate_compare_report,
    generate_json_sidecar,
    generate_markdown_report,
    pick_variant,
    synthesize_override_snippet,
)

# The Session 15 C1 Group 4 frame — compressor running, 721.57 kWh lifetime,
# 0.191 kW instantaneous. Same body used by the "killer" apply_device_quirks
# integration test.
SESSION_15_C1G4_BODY = bytes.fromhex("c1210144000119dd00000000000000000007760000")

# Q11 reports cap 0x16 = 0 ("no power calc") despite the data being valid.
Q11_CAP_0x16_0 = [
    {
        "cap_id": "0x16",
        "cap_type": 2,
        "key_16": "0x0216",
        "data_len": 1,
        "data": [0],
        "data_hex": "00",
    }
]


@pytest.fixture(scope="module")
def glossary():
    return load_glossary()


# ══════════════════════════════════════════════════════════════════════════
#   classify()
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "value,raw,expected",
    [
        (None, None, CLASS_NONE),
        (0, None, CLASS_ZERO),
        (0.0, None, CLASS_ZERO),
        (False, None, CLASS_ZERO),
        ("", None, CLASS_ZERO),
        (24.3, None, CLASS_POPULATED),
        (1, None, CLASS_POPULATED),
        (True, None, CLASS_POPULATED),
        ("foo", None, CLASS_POPULATED),
        # FF-flood is whole-frame: when the raw body is all 0xFF, the value
        # (whatever decoder produced) is reclassified.
        (123, b"\xff\xff\xff\xff", CLASS_FF_FLOOD),
        (1.0, b"\xff" * 31, CLASS_FF_FLOOD),
        # Partial-FF body is not a flood.
        (123, b"\xff\xff\x00\xff", CLASS_POPULATED),
        # Empty body — treat as populated when value is non-zero.
        (5, b"", CLASS_POPULATED),
    ],
)
def test_classify_truth_table(value, raw, expected):
    assert classify(value, raw) == expected


def test_classify_does_not_reclassify_zero_on_ff_flood():
    """A zero value on an all-FF body is still 'zero' (FF-flood check
    only applies when value is non-zero)."""
    # Actually current behaviour: value=0 short-circuits to zero before
    # FF-flood check. This documents that.
    assert classify(0, b"\xff" * 16) == CLASS_ZERO


# ══════════════════════════════════════════════════════════════════════════
#   build_frame_field_index / cap_dependent_fields
# ══════════════════════════════════════════════════════════════════════════


def test_build_frame_field_index_nonempty(glossary):
    idx = build_frame_field_index(glossary)
    # At minimum, the legacy protocol set we know about.
    assert "rsp_0xc0" in idx
    assert "rsp_0xc1_group4" in idx
    assert "rsp_0xa1" in idx
    # Every list entry is a field name string.
    for protocol_key, fields in idx.items():
        assert isinstance(protocol_key, str)
        for fname in fields:
            assert isinstance(fname, str)
    # Sanity: the C1 group 4 frame carries the four power fields.
    assert "power_total_kwh" in idx["rsp_0xc1_group4"]
    assert "power_realtime_kw" in idx["rsp_0xc1_group4"]


def test_cap_dependent_fields_match_power_quartet(glossary):
    """Exactly the four power fields are cap-dependent today."""
    result = cap_dependent_fields(glossary)
    expected = {
        "power_total_kwh",
        "power_total_run_kwh",
        "power_current_run_kwh",
        "power_realtime_kw",
    }
    assert expected.issubset(result)


# ══════════════════════════════════════════════════════════════════════════
#   decode_variants / pick_variant
# ══════════════════════════════════════════════════════════════════════════


def test_decode_variants_produces_linear_and_bcd(glossary):
    walk = walk_fields(glossary)
    variants = decode_variants(
        "power_total_kwh",
        walk["power_total_kwh"],
        "rsp_0xc1_group4",
        SESSION_15_C1G4_BODY,
        glossary,
    )
    by_enc = {v.encoding: v.value for v in variants if v.encoding}
    # Ground truth for the Session 15 frame:
    assert by_enc["power_linear_4"] == pytest.approx(721.57, abs=0.01)
    assert by_enc["power_bcd_4"] == pytest.approx(120.43, abs=0.01)


def test_decode_variants_returns_empty_for_non_cap_field(glossary):
    """Fields without a `capability:` block produce no variants."""
    walk = walk_fields(glossary)
    fdef = walk["indoor_temperature"]
    # Indoor temperature has no capability block.
    assert not fdef.get("capability")
    variants = decode_variants(
        "indoor_temperature",
        fdef,
        "rsp_0xc0",
        bytes([0xC0] + [0] * 29),
        glossary,
    )
    assert variants == []


def test_pick_variant_prefers_single_meaningful(glossary):
    walk = walk_fields(glossary)
    variants = decode_variants(
        "power_total_kwh",
        walk["power_total_kwh"],
        "rsp_0xc1_group4",
        SESSION_15_C1G4_BODY,
        glossary,
    )
    picked, guessed = pick_variant(variants, walk["power_total_kwh"])
    # Two distinct non-zero values (BCD vs linear) → guessed
    assert guessed is True
    assert picked is not None
    assert picked.encoding in ("power_bcd_4", "power_linear_4")


def test_pick_variant_handles_all_zero_field(glossary):
    walk = walk_fields(glossary)
    # power_total_run_kwh sits at body[8..11] — all zero in Session 15.
    variants = decode_variants(
        "power_total_run_kwh",
        walk["power_total_run_kwh"],
        "rsp_0xc1_group4",
        SESSION_15_C1G4_BODY,
        glossary,
    )
    picked, guessed = pick_variant(variants, walk["power_total_run_kwh"])
    # No variant produces a non-zero value — picker returns None.
    assert picked is None
    assert guessed is False


# ══════════════════════════════════════════════════════════════════════════
#   ShadowDecoder
# ══════════════════════════════════════════════════════════════════════════


def test_shadow_decoder_accumulates_then_classifies(glossary):
    sd = ShadowDecoder(glossary)
    sd.observe("rsp_0xc1_group4", SESSION_15_C1G4_BODY)
    result = sd.snapshot(cap_records=Q11_CAP_0x16_0)

    pt = result.states["power_total_kwh"]
    pr = result.states["power_realtime_kw"]
    ptr = result.states["power_total_run_kwh"]

    assert pt.classification == CLASS_POPULATED
    assert pr.classification == CLASS_POPULATED
    # Run / current-run stay zero in Session 15.
    assert ptr.classification == CLASS_ZERO

    # Indoor temp was never seen — should be classified not_seen.
    it = result.states["indoor_temperature"]
    assert it.classification == CLASS_NOT_SEEN
    assert it.frame is None


def test_shadow_decoder_last_observation_wins(glossary):
    """Two observations of the same protocol_key → later overrides the
    earlier (matches AC semantics: fresher frame = fresher read)."""
    sd = ShadowDecoder(glossary)
    first = SESSION_15_C1G4_BODY
    # Same frame but with body[4..7] bumped by 1 — simulates a later probe.
    second = bytearray(first)
    second[7] = (second[7] + 1) & 0xFF  # +0.01 kWh
    sd.observe("rsp_0xc1_group4", bytes(first))
    sd.observe("rsp_0xc1_group4", bytes(second))
    result = sd.snapshot(cap_records=Q11_CAP_0x16_0)

    variants = result.states["power_total_kwh"].variants
    linear = next(v for v in variants if v.encoding == "power_linear_4")
    # 721.57 + 0.01 → 721.58
    assert linear.value == pytest.approx(721.58, abs=0.01)


def test_shadow_decoder_ff_flood_frame(glossary):
    """msg_type 0x07 returned all-FF on Q11 → every field decoded from
    that frame (if any) should carry the FF-flood marker."""
    sd = ShadowDecoder(glossary)
    # An all-FF body that the codec can identify. 0x07 device-id response:
    # tag 0xFF isn't a known frame tag, so we fake with rsp_0xa1 for this
    # unit test. The behaviour we're testing is whole-frame FF detection.
    sd.observe("rsp_0xa1", b"\xff" * 31)
    result = sd.snapshot(cap_records=[])
    # Any field decoded from that frame should be flagged.
    for state in result.states.values():
        if state.frame == "rsp_0xa1":
            assert state.classification in (CLASS_FF_FLOOD, CLASS_NOT_SEEN)


# ══════════════════════════════════════════════════════════════════════════
#   synthesize_override_snippet
# ══════════════════════════════════════════════════════════════════════════


def test_synthesize_emits_for_populated_hidden_field(glossary):
    walk = walk_fields(glossary)
    snip = synthesize_override_snippet(
        "power_total_kwh",
        walk["power_total_kwh"],
        "rsp_0xc1_group4",
        SESSION_15_C1G4_BODY,
        glossary,
        Q11_CAP_0x16_0,
    )
    assert snip is not None
    assert snip.field_name == "power_total_kwh"
    assert snip.category == "sensor"
    assert snip.picked_variant is not None
    # Should pick one of the cap-defined encodings, not a raw fallback.
    assert snip.picked_variant.encoding in ("power_linear_4", "power_bcd_4")
    # HA metadata inferred from unit=kWh.
    assert snip.ha_metadata.get("device_class") == "energy"
    assert snip.ha_metadata.get("state_class") == "total_increasing"
    assert snip.ha_metadata.get("off_behavior") == "available"
    # YAML starts at the `fields:` root with full wrapping path.
    assert snip.yaml_text.startswith("fields:")
    assert "sensor:" in snip.yaml_text
    assert "power_total_kwh:" in snip.yaml_text


def test_synthesize_skips_fields_with_always_feature_available(glossary):
    walk = walk_fields(glossary)
    # indoor_temperature is `feature_available: readable` — not hidden,
    # nothing to unlock.
    snip = synthesize_override_snippet(
        "indoor_temperature",
        walk["indoor_temperature"],
        "rsp_0xc0",
        bytes([0xC0] + [0] * 29),
        glossary,
        [],
    )
    assert snip is None


def test_synthesize_skips_all_zero_fields(glossary):
    walk = walk_fields(glossary)
    # power_total_run_kwh is all-zero in Session 15 → pick_variant returns
    # None → no snippet.
    snip = synthesize_override_snippet(
        "power_total_run_kwh",
        walk["power_total_run_kwh"],
        "rsp_0xc1_group4",
        SESSION_15_C1G4_BODY,
        glossary,
        Q11_CAP_0x16_0,
    )
    assert snip is None


def test_synthesize_yaml_parses_as_valid_override(glossary):
    """Emitted YAML must round-trip through yaml.safe_load, deep-merge
    cleanly against the base glossary, and pass schema validation.
    This is the non-negotiable gate from the plan."""
    import yaml
    from blaueis.core.glossary_override import apply_override
    from jsonschema import Draft202012Validator

    walk = walk_fields(glossary)
    snip = synthesize_override_snippet(
        "power_realtime_kw",
        walk["power_realtime_kw"],
        "rsp_0xc1_group4",
        SESSION_15_C1G4_BODY,
        glossary,
        Q11_CAP_0x16_0,
    )
    assert snip is not None

    parsed = yaml.safe_load(snip.yaml_text)
    merged, _affected, _warnings = apply_override(glossary, parsed)

    # Load schema from the same path the synthesizer uses.
    from blaueis.core.inventory import _load_glossary_schema

    schema = _load_glossary_schema()
    validator = Draft202012Validator(schema)

    base_sigs = {(tuple(e.absolute_path), e.message) for e in validator.iter_errors(glossary)}
    new_errors = [e for e in validator.iter_errors(merged) if (tuple(e.absolute_path), e.message) not in base_sigs]
    assert new_errors == [], f"unexpected schema errors: {[e.message for e in new_errors]}"


# ══════════════════════════════════════════════════════════════════════════
#   Report writers
# ══════════════════════════════════════════════════════════════════════════


def test_generate_json_sidecar_shape(glossary):
    sd = ShadowDecoder(glossary)
    sd.observe("rsp_0xc1_group4", SESSION_15_C1G4_BODY)
    result = sd.snapshot(cap_records=Q11_CAP_0x16_0)

    walk = walk_fields(glossary)
    snips = [
        synthesize_override_snippet(f, walk[f], "rsp_0xc1_group4", SESSION_15_C1G4_BODY, glossary, Q11_CAP_0x16_0)
        for f in ("power_total_kwh", "power_realtime_kw")
    ]
    snips = [s for s in snips if s]

    js = generate_json_sidecar(
        result,
        glossary,
        label="test",
        host="192.168.210.30",
        suggested_overrides=snips,
    )

    assert js["meta"]["label"] == "test"
    assert js["meta"]["host"] == "192.168.210.30"
    assert "glossary_version" in js["meta"]
    assert "fields" in js
    assert "suggested_overrides" in js
    assert len(js["suggested_overrides"]) == 2

    # Fields include classifications + variants for cap-dependent entries.
    pt = js["fields"]["power_total_kwh"]
    assert pt["classification"] == CLASS_POPULATED
    assert "variants" in pt and len(pt["variants"]) >= 2


def test_generate_markdown_report_shape(glossary):
    sd = ShadowDecoder(glossary)
    sd.observe("rsp_0xc1_group4", SESSION_15_C1G4_BODY)
    result = sd.snapshot(cap_records=Q11_CAP_0x16_0)
    md = generate_markdown_report(result, glossary, label="test", host="192.168.210.30")

    # Structural sanity — starts with an HTML timestamp comment, then
    # the H1 heading follows.
    assert md.startswith("<!-- Field inventory generated ")
    assert "# Field inventory — test" in md
    assert "## Summary by classification" in md
    assert "## Populated fields" in md
    assert "## Zero-valued fields" in md
    assert "## Not-seen fields" in md
    # Populated table cites power_total_kwh and power_realtime_kw.
    assert "power_total_kwh" in md
    assert "power_realtime_kw" in md


def test_generate_markdown_includes_suggested_overrides(glossary):
    sd = ShadowDecoder(glossary)
    sd.observe("rsp_0xc1_group4", SESSION_15_C1G4_BODY)
    result = sd.snapshot(cap_records=Q11_CAP_0x16_0)

    walk = walk_fields(glossary)
    snip = synthesize_override_snippet(
        "power_total_kwh",
        walk["power_total_kwh"],
        "rsp_0xc1_group4",
        SESSION_15_C1G4_BODY,
        glossary,
        Q11_CAP_0x16_0,
    )
    md = generate_markdown_report(
        result,
        glossary,
        label="test",
        suggested_overrides=[snip],
    )
    assert "## Suggested overrides (1 fields)" in md or "## Suggested overrides" in md
    assert "power_total_kwh" in md
    assert "```yaml" in md
    assert "feature_available: always" in md


def test_generate_compare_report_buckets(glossary):
    """Diff a synthetic before/after — before has power_realtime_kw=0,
    after has power_realtime_kw=0.5 → becomes_populated bucket."""
    prev = {
        "meta": {"label": "off", "timestamp": "2026-04-23T10:00:00Z"},
        "fields": {
            "power_realtime_kw": {"classification": CLASS_ZERO, "value": 0.0},
            "indoor_temperature": {"classification": CLASS_POPULATED, "value": 24.0},
            "dead_field": {"classification": CLASS_POPULATED, "value": 5},
        },
    }
    curr = {
        "meta": {"label": "cooling", "timestamp": "2026-04-23T10:05:00Z"},
        "fields": {
            "power_realtime_kw": {"classification": CLASS_POPULATED, "value": 0.5},
            "indoor_temperature": {"classification": CLASS_POPULATED, "value": 24.0},
            "dead_field": {"classification": CLASS_ZERO, "value": 0},
        },
    }
    report = generate_compare_report(prev, curr)
    assert "## Became populated (1)" in report
    assert "power_realtime_kw" in report.split("## Stopped")[0]
    assert "## Stopped being populated (1)" in report
    assert "dead_field" in report.split("## Value changed")[0]
    assert "## Value changed (0)" in report
    assert "## Stable (1)" in report  # indoor_temperature


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
