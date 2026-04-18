"""Tests for fan mode label and custom_value propagation.

Covers:
  - decode_enum_cap extracts 'custom_value' from a cap_value definition
  - decode_enum_cap preserves 'label' fields inside the nested values dict
  - Both fields survive _apply_caps_to_fields → active_constraints
  - Real B5 (stepless cap 0x10=1) produces custom_value + labelled values
"""
from pathlib import Path

import yaml
from blaueis.core.codec import decode_enum_cap, load_glossary
from blaueis.core.process import finalize_capabilities, process_raw_frame
from blaueis.core.status import build_status

B5_FIXTURE = (
    Path(__file__).resolve().parent
    / "test-cases/xtremesaveblue_s1/b5_frames.yaml"
)


# ── Unit: decode_enum_cap ──────────────────────────────────────────────────

_CAP_DEF_WITH_LABELS = {
    "values": {
        "stepless": {
            "raw": 1,
            "feature_available": "always",
            "valid_range": [0, 102],
            "step": 1,
            "correction": "clamp",
            "custom_value": {"label": "Custom"},
            "values": {
                "ultra_low": {"raw": 1,  "label": "Ultra Low"},
                "low":       {"raw": 40, "label": "Low"},
                "auto":      {"raw": 102, "label": "Auto"},
            },
        }
    }
}


def test_decode_enum_cap_propagates_custom_value():
    result = decode_enum_cap(_CAP_DEF_WITH_LABELS, raw_value=1)
    assert result["decoded_key"] == "stepless"
    assert result.get("custom_value") == {"label": "Custom"}


def test_decode_enum_cap_preserves_labels_in_values():
    result = decode_enum_cap(_CAP_DEF_WITH_LABELS, raw_value=1)
    values = result.get("values", {})
    assert values["ultra_low"].get("label") == "Ultra Low"
    assert values["low"].get("label") == "Low"
    assert values["auto"].get("label") == "Auto"


def test_decode_enum_cap_no_custom_value_absent():
    cap_def = {
        "values": {
            "standard": {
                "raw": 5,
                "feature_available": "always",
                "valid_set": [40, 60, 80, 102],
                "values": {
                    "low":  {"raw": 40, "label": "Low"},
                    "high": {"raw": 80, "label": "High"},
                },
            }
        }
    }
    result = decode_enum_cap(cap_def, raw_value=5)
    assert "custom_value" not in result


# ── Integration: real B5 stepless cap → active_constraints ────────────────

def _load_b5_status():
    glossary = load_glossary()
    status = build_status("test", glossary)
    with open(B5_FIXTURE, encoding="utf-8") as f:
        b5_data = yaml.safe_load(f)
    for frame in b5_data["frames"]:
        body = bytes.fromhex(frame["body_hex"].replace(" ", "").replace("\n", ""))
        process_raw_frame(status, body, glossary)
    finalize_capabilities(status, glossary)
    return status


def test_stepless_cap_produces_custom_value():
    status = _load_b5_status()
    ac = status["fields"]["fan_speed"]["active_constraints"]
    assert ac is not None
    assert ac.get("custom_value") == {"label": "Custom"}


def test_stepless_cap_values_carry_labels():
    status = _load_b5_status()
    ac = status["fields"]["fan_speed"]["active_constraints"]
    values = ac.get("values", {})
    assert values["ultra_low"].get("label") == "Ultra Low"
    assert values["low"].get("label") == "Low"
    assert values["medium"].get("label") == "Medium"
    assert values["high"].get("label") == "High"
    assert values["auto"].get("label") == "Auto"


def test_stepless_cap_values_raw_unchanged():
    status = _load_b5_status()
    ac = status["fields"]["fan_speed"]["active_constraints"]
    values = ac.get("values", {})
    assert values["ultra_low"]["raw"] == 1
    assert values["low"]["raw"] == 40
    assert values["high"]["raw"] == 80
    assert values["auto"]["raw"] == 102
