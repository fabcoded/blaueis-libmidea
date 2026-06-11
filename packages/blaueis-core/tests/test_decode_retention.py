"""Decode-retention vs exposure at the process.py value-slot gate.

Decode-retention is decoupled from exposure: ``process_data_frame`` retains a
decoded value for a confirmed-``excluded`` field so a gating interlock can read
its live state, even though the field is never exposed as an entity. Only the
pre-B5 ``capability`` / ``capability-opt`` window is still skipped — the decode is
untrusted until B5 confirms which feature owns the byte.

Phase 0 pinned the prior behaviour (``excluded`` discarded); Phase 1 flipped that
single row of ``RETAINED_BY_AVAILABILITY`` to True. Every other row is unchanged —
exposure and polling stay gated at ``available_fields`` / ``required_queries``.
"""

from __future__ import annotations

import pytest
from blaueis.core.codec import load_glossary
from blaueis.core.process import process_data_frame
from blaueis.core.query import read_field
from blaueis.core.status import build_status

C0_PROTO = "rsp_0xc0"


def _c0_body_strong_wind_on() -> bytes:
    """A C0 body with strong_wind set (anchor C0:8:5..5 → byte8 bit5 = 0x20)."""
    body = bytearray(40)
    body[8] = 0x20
    return bytes(body)


# Retention per feature_available at the process.py value-slot gate. The skip set
# is {capability, capability-opt} (pre-B5 untrusted window); everything else —
# including confirmed `excluded` — retains its decoded value.
RETAINED_BY_AVAILABILITY: dict[str, bool] = {
    "always": True,
    "readable": True,
    "readable-opt": True,
    "excluded": True,  # retained for interlock reads (not exposed/polled)
    "capability": False,  # pre-B5 untrusted — discarded
    "capability-opt": False,  # pre-B5 untrusted — discarded
}


@pytest.mark.parametrize("feature_available,retained", sorted(RETAINED_BY_AVAILABILITY.items()))
def test_retention_gate_characterization(feature_available: str, retained: bool) -> None:
    g = load_glossary()
    status = build_status(device="test", glossary=g)
    status["fields"]["strong_wind"]["feature_available"] = feature_available

    process_data_frame(status, _c0_body_strong_wind_on(), C0_PROTO, g)

    slot = read_field(status, "strong_wind")
    if retained:
        assert slot is not None and slot["value"] is True
    else:
        assert slot is None, (
            f"feature_available={feature_available!r} must stay discarded (pre-B5 "
            f"untrusted window) — only 'excluded' was decoupled from exposure"
        )


def test_excluded_value_is_retained_for_interlocks() -> None:
    """C0a/C1a — the decoupled behaviour: a confirmed-excluded field keeps its value."""
    g = load_glossary()
    status = build_status(device="test", glossary=g)
    status["fields"]["strong_wind"]["feature_available"] = "excluded"
    process_data_frame(status, _c0_body_strong_wind_on(), C0_PROTO, g)
    slot = read_field(status, "strong_wind")
    assert slot is not None and slot["value"] is True


def test_available_value_is_retained() -> None:
    """C0b — invariant Phase 1 must preserve: available field keeps its value."""
    g = load_glossary()
    status = build_status(device="test", glossary=g)
    status["fields"]["strong_wind"]["feature_available"] = "always"
    process_data_frame(status, _c0_body_strong_wind_on(), C0_PROTO, g)
    slot = read_field(status, "strong_wind")
    assert slot is not None and slot["value"] is True


def test_pre_b5_capability_value_is_discarded() -> None:
    """C0c — invariant Phase 1 must preserve: pre-B5 capability window stays skipped."""
    g = load_glossary()
    status = build_status(device="test", glossary=g)
    status["fields"]["strong_wind"]["feature_available"] = "capability"
    process_data_frame(status, _c0_body_strong_wind_on(), C0_PROTO, g)
    assert read_field(status, "strong_wind") is None
