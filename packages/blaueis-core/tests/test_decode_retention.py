"""Characterization tests for the decode-retention gate (process.py value slot).

Phase 0 of the decode-decoupling plan: pin the CURRENT behaviour so the Phase 1
change is provable. Today ``process_data_frame`` discards a decoded value when the
field's ``feature_available`` is ``excluded`` / ``capability`` / ``capability-opt``
— so a hidden field has no retained value and an interlock reading it gets None.

Phase 1 will flip ONLY the ``excluded`` row (retain confirmed-hidden values so a
gate can read them) and KEEP the pre-B5 ``capability`` / ``capability-opt`` skip
(decode untrusted until B5 confirms the byte's owner). When that lands, exactly the
``excluded`` expectation in ``RETAINED_TODAY`` moves True; every other row stays.
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


# Retention per feature_available at the process.py value-slot gate, as it stands
# TODAY. The skip set is {excluded, capability, capability-opt}; everything else is
# retained. Phase 1 flips `excluded` -> True and nothing else.
RETAINED_TODAY: dict[str, bool] = {
    "always": True,
    "readable": True,
    "readable-opt": True,
    "excluded": False,        # <-- Phase 1 flips THIS to True
    "capability": False,      # pre-B5 untrusted — stays discarded
    "capability-opt": False,  # pre-B5 untrusted — stays discarded
}


@pytest.mark.parametrize("feature_available,retained", sorted(RETAINED_TODAY.items()))
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
            f"feature_available={feature_available!r} currently discards the decoded "
            f"value; if this now retains, it is the Phase-1 change — update RETAINED_TODAY"
        )


def test_excluded_value_is_discarded_today() -> None:
    """C0a — the load-bearing baseline Phase 1 flips: excluded => no retained value."""
    g = load_glossary()
    status = build_status(device="test", glossary=g)
    status["fields"]["strong_wind"]["feature_available"] = "excluded"
    process_data_frame(status, _c0_body_strong_wind_on(), C0_PROTO, g)
    assert read_field(status, "strong_wind") is None


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
