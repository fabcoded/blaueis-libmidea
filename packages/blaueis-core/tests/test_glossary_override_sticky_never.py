"""Tests for the "glossary-pinned never is sticky" invariant (G11).

When the glossary declares ``fields.X.feature_available: never`` — either
in the on-disk glossary or via a user override — that value must survive
every subsequent B5 ingress. Before G11, ``_apply_caps_to_fields`` would
decode the cap value and overwrite status[fields][X][feature_available]
with the cap-value's FA (e.g. ``always``), erasing the override on the
first frame that referenced the cap.
"""

from __future__ import annotations

from blaueis.core.process import _apply_caps_to_fields


def _minimal_glossary_with_screen_display(field_fa: str) -> dict:
    """Build a minimal glossary containing only screen_display, with the
    field-level feature_available set to ``field_fa``. The cap block's
    values.supported.feature_available stays ``always`` so the test can
    observe whether B5 promotion happens."""
    return {
        "fields": {
            "control": {
                "screen_display": {
                    "description": "Display LED",
                    "data_type": "bool",
                    "field_class": "stateful_bool",
                    "confidence": "consistent",
                    "feature_available": field_fa,
                    "capability": {
                        "cap_id": "0x24",
                        "cap_id_16": "0x0224",
                        "cap_type": "extended",
                        "description": "test",
                        "values": {
                            "supported": {
                                "raw": 1,
                                "feature_available": "always",
                                "description": "supported",
                            },
                            "not_supported": {
                                "raw": 0,
                                "feature_available": "never",
                                "description": "not supported",
                            },
                        },
                    },
                },
            },
        },
    }


def _minimal_status(field_fa: str) -> dict:
    """Starting status: screen_display initialised from glossary field FA."""
    return {
        "fields": {
            "screen_display": {
                "feature_available": field_fa,
                "sources": {},
            },
        },
        "capabilities_raw": [],
    }


# ── Baseline: without pinning, cap promotion works as designed ────────


def test_b5_cap_supported_promotes_unpinned_field_to_always():
    """Baseline check — when the glossary DOES NOT pin feature_available
    to never, a B5 record with value=supported promotes the field to
    `always` (current behaviour)."""
    glossary = _minimal_glossary_with_screen_display(field_fa="capability")
    status = _minimal_status(field_fa="capability")
    records = [{
        "cap_id": "0x24",
        "cap_type": 1,       # 1 → extended (matches 'extended' in glossary)
        "data": [0x01],      # value raw=1 → "supported"
    }]
    _apply_caps_to_fields(status, records, glossary)
    assert status["fields"]["screen_display"]["feature_available"] == "always"


# ── The invariant: pinned 'never' survives promotion ──────────────────


def test_b5_cap_supported_does_not_escalate_pinned_never():
    """The core G11 assertion: when the glossary pins the field to
    `never` (via an override or direct edit), a B5 record advertising
    the cap as supported MUST NOT escalate the field back to `always`.
    The override is sticky."""
    glossary = _minimal_glossary_with_screen_display(field_fa="never")
    status = _minimal_status(field_fa="never")
    records = [{
        "cap_id": "0x24",
        "cap_type": 1,
        "data": [0x01],      # cap value says "supported" (never→always)
    }]
    _apply_caps_to_fields(status, records, glossary)
    assert status["fields"]["screen_display"]["feature_available"] == "never"


def test_b5_cap_not_supported_keeps_pinned_never():
    """Cap value saying "not_supported" already maps to never — pinned
    state unchanged, trivially consistent."""
    glossary = _minimal_glossary_with_screen_display(field_fa="never")
    status = _minimal_status(field_fa="never")
    records = [{
        "cap_id": "0x24",
        "cap_type": 1,
        "data": [0x00],      # cap value says "not_supported"
    }]
    _apply_caps_to_fields(status, records, glossary)
    assert status["fields"]["screen_display"]["feature_available"] == "never"
