"""decode_predicates registry + the target_temperature dry-mode fix.

The body[13] extended-setpoint override on rsp_0xc0 must NOT fire in
dry / smart_dry mode — target_temperature is meaningless there (the OEM
forces it to 0), so a mode-blind override would surface a plausible-but-
wrong setpoint derived from the dehumidify byte. The fix gates step1 with
the ``temp_extended_setpoint_active`` predicate; this verifies the gate and
that non-dry modes are untouched.

It also verifies the half-degree (body[2] bit4 = +0.5) survives the body[13]
override: the firmware sets that bit regardless of which byte holds the
integer setpoint, so both decode steps carry ``half_bit`` and the override
must not drop it.
"""

from __future__ import annotations

from blaueis.core.codec import decode_frame_fields, load_glossary
from blaueis.core.decode_predicates import (
    DRY_MODES,
    temp_extended_setpoint_active,
)

GLOSSARY = load_glossary()

# operating_mode raws: auto=1, cool=2, dry=3, heat=4, fan=5, smart_dry=6
NON_DRY = (1, 2, 4, 5)


def _c0(mode_raw: int, b13: int, b2_low: int = 9, half: bool = False) -> bytes:
    """A C0 body: body[2] = mode<<5 | half<<4 | setpoint-nibble; body[13] override byte.
    b2_low=9 -> primary setpoint 9+16 = 25.0; half sets body[2] bit4 (+0.5)."""
    b = bytearray(32)
    b[0] = 0xC0
    b[2] = (mode_raw << 5) | (0x10 if half else 0) | (b2_low & 0x0F)
    b[13] = b13
    return bytes(b)


def _target(mode_raw: int, b13: int, b2_low: int = 9, half: bool = False):
    return decode_frame_fields(_c0(mode_raw, b13, b2_low, half), "rsp_0xc0", GLOSSARY)["target_temperature"]["value"]


# ── predicate unit behaviour ────────────────────────────────────────


def test_predicate_dry_modes_constant():
    assert {3, 6} == DRY_MODES


def test_predicate_false_in_dry_and_smart_dry_for_any_value():
    for mode in (3, 6):
        for v in (1, 18, 31):
            body = bytearray(32)
            body[2] = mode << 5
            assert temp_extended_setpoint_active(bytes(body), {}, v) is False


def test_predicate_false_on_zero_value_any_mode():
    # sentinel preserved: val==0 never fires, regardless of mode
    for mode in (1, 2, 3, 4, 5, 6):
        body = bytearray(32)
        body[2] = mode << 5
        assert temp_extended_setpoint_active(bytes(body), {}, 0) is False


def test_predicate_true_for_nonzero_in_non_dry():
    for mode in NON_DRY:
        body = bytearray(32)
        body[2] = mode << 5
        assert temp_extended_setpoint_active(bytes(body), {}, 5) is True


# ── end-to-end decode: the bug fix ──────────────────────────────────


def test_dry_mode_suppresses_override_falls_back_to_primary():
    # The bug: without the gate, these body[13] values would surface as
    # 20.0/27.0/30.0/17.0. Fixed: they fall back to body[2] -> 25.0.
    for b13 in (0x28, 0x2F, 0x32, 0x65):  # 40/47/50/101-ish
        assert _target(3, b13) == 25.0, f"dry body[13]={b13:#04x}"


def test_smart_dry_mode_suppresses_override():
    assert _target(6, 0x32) == 25.0


def test_non_dry_override_still_fires_no_regression():
    assert _target(2, 0x04) == 16.0  # cool, 4+12
    assert _target(4, 0x12) == 30.0  # heat, 18+12
    assert _target(1, 0x06) == 18.0  # auto, 6+12


def test_sentinel_zero_falls_back_in_every_mode():
    for mode in (1, 2, 3, 4, 5, 6):
        assert _target(mode, 0x00) == 25.0, f"mode {mode}"


# ── half-degree (body[2] bit4) survives the body[13] override ────────
# The firmware sets the half bit in body[2] regardless of which byte holds
# the integer setpoint, so the body[13] override must not drop it.


def test_non_dry_override_keeps_half_degree():
    # cool: body[13]=0x0B -> 11+12=23, + body[2] bit4 -> 23.5 (was 23.0 pre-fix)
    assert _target(2, 0x0B, half=True) == 23.5  # cool
    assert _target(4, 0x0C, half=True) == 24.5  # heat (12+12=24, +0.5)
    assert _target(1, 0x0B, half=True) == 23.5  # auto


def test_non_dry_override_no_half_when_bit_clear():
    # Regression guard: +0.5 only when body[2] bit4 is actually set.
    assert _target(2, 0x0B, half=False) == 23.0  # cool, whole degree
    assert _target(4, 0x0C, half=False) == 24.0  # heat


def test_dry_primary_keeps_half_degree():
    # Dry suppresses the override -> body[2] primary (b2_low=9 -> 25) + half -> 25.5.
    assert _target(3, 0x32, half=True) == 25.5
    assert _target(6, 0x32, half=True) == 25.5
