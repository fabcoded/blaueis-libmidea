"""Tests for blaueis.core.gate_eval.evaluate_offered (gate engine, G1 skeleton).

Covers: parity with is_field_visible when no `gate:` block; the power axis; the
cap-derived mode axis (only active when gate.cap_mode marks the valid_set as
operating-mode raws); and the interlock axis (fail-open when the dependency state
is unknown). No glossary `gate:` blocks exist yet, so production behaviour is
unchanged — these exercise the evaluator directly with synthetic field defs.
"""
from __future__ import annotations

import pytest

from blaueis.core.codec import load_glossary, walk_fields
from blaueis.core.gate_eval import evaluate_offered
from blaueis.core.ux_gating import is_field_visible

# ── parity: no gate block ⇒ power_on ∧ is_field_visible ──────────────────


def test_no_gate_no_ux_offered_when_powered() -> None:
    v = evaluate_offered({}, mode="cool", power_on=True)
    assert v.offered and v.blocked_by == []


def test_power_off_blocks() -> None:
    v = evaluate_offered({}, mode="cool", power_on=False)
    assert not v.offered and v.blocked_by == ["power_off"]


def test_requires_power_false_ignores_power() -> None:
    gdef = {"gate": {"requires_power": False}}
    assert evaluate_offered(gdef, mode="cool", power_on=False).offered


def test_mode_axis_matches_is_field_visible() -> None:
    gdef = {"ux": {"visible_in_modes": ["cool", "auto"]}}
    for mode in ("cool", "auto", "heat", "dry", 2, 4):
        expected = is_field_visible(gdef, current_mode=mode)
        v = evaluate_offered(gdef, mode=mode, power_on=True)
        assert v.offered == expected, mode
        if not expected:
            assert v.blocked_by == ["mode"]


# ── capability-mode axis (gate.cap_mode + active_constraints.valid_set) ───

CAP_MODE_GDEF = {"gate": {"cap_mode": {"cap_id": "0x1A"}}}


def test_cap_mode_offers_in_valid_set_modes() -> None:
    ac = {"valid_set": [2, 4]}  # cool, heat (turbo cap=1 "both")
    for mode in ("cool", "heat", 2, 4):
        assert evaluate_offered(CAP_MODE_GDEF, mode=mode, power_on=True, active_constraints=ac).offered


def test_cap_mode_blocks_outside_valid_set() -> None:
    ac = {"valid_set": [2]}  # cool_only (turbo cap=0)
    v = evaluate_offered(CAP_MODE_GDEF, mode="heat", power_on=True, active_constraints=ac)
    assert not v.offered and any(b.startswith("cap_mode:") for b in v.blocked_by)


def test_cap_mode_inactive_without_active_constraints() -> None:
    # cap_mode declared but no live constraints yet (pre-B5) ⇒ cap axis inert.
    assert evaluate_offered(CAP_MODE_GDEF, mode="heat", power_on=True).offered


def test_cap_mode_unknown_mode_fails_open() -> None:
    ac = {"valid_set": [2]}
    assert evaluate_offered(CAP_MODE_GDEF, mode=None, power_on=True, active_constraints=ac).offered


def test_cap_mode_inert_on_bool_default_valid_set() -> None:
    # Pre-B5 permissive default carries a value-domain valid_set ([False, True]),
    # NOT operating-mode raws. The cap-mode axis must stay inert (not read True→auto
    # and block cool), so the field is offered per its logical mode rule.
    ac = {"valid_set": [False, True]}
    assert evaluate_offered(CAP_MODE_GDEF, mode="cool", power_on=True, active_constraints=ac).offered
    assert evaluate_offered(CAP_MODE_GDEF, mode="heat", power_on=True, active_constraints=ac).offered


def test_cap_mode_inert_on_non_mode_int_valid_set() -> None:
    # A field-value valid_set that isn't all operating-mode raws (e.g. contains 6)
    # is not reinterpreted as modes — axis inert.
    ac = {"valid_set": [0, 6]}
    assert evaluate_offered(CAP_MODE_GDEF, mode="cool", power_on=True, active_constraints=ac).offered


def test_cap_mode_inert_on_malformed_active_constraints() -> None:
    # Non-dict / non-list inputs (e.g. a stray mock) must never yield a spurious
    # empty mode set that gates the field off — the axis stays inert.
    for bad in (object(), "x", 5, {"valid_set": "nope"}, {"valid_set": 7}):
        assert evaluate_offered(CAP_MODE_GDEF, mode="cool", power_on=True, active_constraints=bad).offered


def test_valid_set_not_reinterpreted_without_cap_mode_marker() -> None:
    # Without gate.cap_mode, a valid_set is NOT treated as modes (the B2 trap):
    # a field-value valid_set must not gate the mode axis.
    gdef = {}  # no gate block
    ac = {"valid_set": [2]}  # would wrongly mean "cool only" if misread as modes
    assert evaluate_offered(gdef, mode="heat", power_on=True, active_constraints=ac).offered


# ── interlock axis (cross-feature live state, fail-open if unknown) ───────

INTERLOCK_GDEF = {"gate": {"interlocks": [{"field": "ptc_state", "at": "C0:9:4..3", "blocks_when": "nonzero"}]}}


def test_interlock_blocks_when_dependency_active() -> None:
    v = evaluate_offered(INTERLOCK_GDEF, mode="cool", power_on=True, field_states={"ptc_state": 1})
    assert not v.offered and v.blocked_by == ["interlock:ptc_state"]


def test_interlock_clear_when_dependency_off() -> None:
    assert evaluate_offered(INTERLOCK_GDEF, mode="cool", power_on=True, field_states={"ptc_state": 0}).offered


def test_interlock_fails_open_when_state_unknown() -> None:
    # dependency absent (cap-absent / not decoded) ⇒ vacuously satisfied (B3).
    assert evaluate_offered(INTERLOCK_GDEF, mode="cool", power_on=True, field_states={}).offered
    assert evaluate_offered(INTERLOCK_GDEF, mode="cool", power_on=True).offered


def test_blocks_when_zero_variant() -> None:
    gdef = {"gate": {"interlocks": [{"field": "x", "at": "C0:1:0..0", "blocks_when": "zero"}]}}
    assert not evaluate_offered(gdef, mode="cool", power_on=True, field_states={"x": 0}).offered
    assert evaluate_offered(gdef, mode="cool", power_on=True, field_states={"x": 1}).offered


def test_multiple_axes_accumulate_in_blocked_by() -> None:
    gdef = {"ux": {"visible_in_modes": ["cool"]}, "gate": {"interlocks": [{"field": "y", "at": "C0:1:0..0"}]}}
    v = evaluate_offered(gdef, mode="heat", power_on=False, field_states={"y": 1})
    assert set(v.blocked_by) == {"power_off", "mode", "interlock:y"}


# ── G6: interlock mode guard (mode-multiplexed dependency bits) ───────────

MODE_GUARDED_GDEF = {"gate": {"interlocks": [
    {"field": "ptc", "at": "C0:9:4..3", "blocks_when": "nonzero", "modes": ["heat", "auto"]}]}}


def test_interlock_mode_guard_applies_inside_modes() -> None:
    v = evaluate_offered(MODE_GUARDED_GDEF, mode="heat", power_on=True, field_states={"ptc": 1})
    assert not v.offered and v.blocked_by == ["interlock:ptc"]


def test_interlock_mode_guard_inactive_outside_modes() -> None:
    # In cool the guarded bit means something else (mode-mux) → interlock skipped
    # even when the raw value is set, so a neighbour's bit can't spuriously block.
    assert evaluate_offered(MODE_GUARDED_GDEF, mode="cool", power_on=True, field_states={"ptc": 1}).offered
    assert evaluate_offered(MODE_GUARDED_GDEF, mode="dry", power_on=True, field_states={"ptc": 1}).offered


def test_interlock_mode_guard_unknown_mode_skips() -> None:
    # mode unknown → can't confirm applicability → skip (fail open).
    assert evaluate_offered(MODE_GUARDED_GDEF, mode=None, power_on=True, field_states={"ptc": 1}).offered


def test_strong_wind_elecheat_interlock_grounded() -> None:
    """Real strong_wind gate: blocked in heat while auxiliary_heat_level is on,
    but the mode guard keeps it offered in cool regardless (bit is eco there)."""
    sw = walk_fields(load_glossary())["strong_wind"]
    # heat + PTC engaged → boost gated off
    assert not evaluate_offered(sw, mode="heat", power_on=True,
                                field_states={"auxiliary_heat_level": 1}).offered
    # heat + no PTC (our unit) → offered
    assert evaluate_offered(sw, mode="heat", power_on=True,
                            field_states={"auxiliary_heat_level": 0}).offered
    # cool: mode guard inactive → offered even if the (eco) bit reads set
    assert evaluate_offered(sw, mode="cool", power_on=True,
                            field_states={"auxiliary_heat_level": 1}).offered


# ── G2: real turbo_mode gate block (cap-mode axis live in the glossary) ───


def test_turbo_mode_declares_cap_mode() -> None:
    tm = walk_fields(load_glossary())["turbo_mode"]
    assert tm.get("gate", {}).get("cap_mode", {}).get("cap_id") == "0x1A"


@pytest.mark.parametrize(
    "valid_set,mode,offered",
    [
        ([2, 4], "cool", True),   # cap=1 "both"      — our unit
        ([2, 4], "heat", True),   # cap=1 "both"      — our unit
        ([2], "cool", True),      # cap=0 "cool_only"
        ([2], "heat", False),     # cap=0 "cool_only" — gated off in heat
        ([4], "cool", False),     # cap=3 "heat_only" — gated off in cool
        ([4], "heat", True),      # cap=3 "heat_only"
    ],
)
def test_turbo_cap_mode_gates_against_live_caps(valid_set, mode, offered) -> None:
    """Drive the REAL turbo_mode gdef with each cap's valid_set (operating-mode raws)."""
    tm = walk_fields(load_glossary())["turbo_mode"]
    v = evaluate_offered(tm, mode=mode, power_on=True, active_constraints={"valid_set": valid_set})
    assert v.offered is offered


def test_turbo_unit_cap1_unchanged_vs_static_list() -> None:
    """Parity guard: our unit (cap=1 → [2,4]) offers exactly what visible_in_modes does."""
    tm = walk_fields(load_glossary())["turbo_mode"]
    ac = {"valid_set": [2, 4]}
    for mode in ("cool", "heat", "auto", "dry", "fan_only"):
        gated = evaluate_offered(tm, mode=mode, power_on=True, active_constraints=ac).offered
        static = is_field_visible(tm, current_mode=mode)
        assert gated == static, mode


# ── G4: mode_forks axis (eco cap-value → mode-set fork) ──────────────────

FORK_GDEF = {"ux": {"visible_in_modes": ["cool", "auto", "dry"]},
             "gate": {"mode_forks": [
                 {"cap_id": "0x12", "when_raw": 1, "modes": ["cool"]},
                 {"cap_id": "0x12", "when_raw": 2, "modes": ["cool", "auto", "dry"]},
             ]}}


def test_mode_fork_first_match_restricts() -> None:
    assert evaluate_offered(FORK_GDEF, mode="cool", power_on=True, cap_values={"0x12": 1}).offered
    v = evaluate_offered(FORK_GDEF, mode="auto", power_on=True, cap_values={"0x12": 1})
    assert not v.offered and any(b.startswith("cap_mode:") for b in v.blocked_by)


def test_mode_fork_second_match_keeps_full_set() -> None:
    for m in ("cool", "auto", "dry"):
        assert evaluate_offered(FORK_GDEF, mode=m, power_on=True, cap_values={"0x12": 2}).offered


def test_mode_fork_inert_without_match_or_caps() -> None:
    # No matching fork (raw 0) or no caps → fork axis inert ⇒ logical mode rule only.
    for cv in ({"0x12": 0}, {}, None, "notadict"):
        assert evaluate_offered(FORK_GDEF, mode="auto", power_on=True, cap_values=cv).offered


def test_eco_mode_fork_grounded_against_real_glossary() -> None:
    eco = walk_fields(load_glossary())["eco_mode"]
    # our unit (0x12=1, special eco) ⇒ cool only
    assert evaluate_offered(eco, mode="cool", power_on=True, cap_values={"0x12": 1}).offered
    assert not evaluate_offered(eco, mode="auto", power_on=True, cap_values={"0x12": 1}).offered
    assert not evaluate_offered(eco, mode="dry", power_on=True, cap_values={"0x12": 1}).offered
    # window variant (0x12=2) ⇒ cool/auto/dry
    for m in ("cool", "auto", "dry"):
        assert evaluate_offered(eco, mode=m, power_on=True, cap_values={"0x12": 2}).offered


def test_cap_mode_and_fork_intersect() -> None:
    # A field declaring both axes is gated by their intersection.
    gdef = {"gate": {"cap_mode": {"cap_id": "0x1A"},
                     "mode_forks": [{"cap_id": "0x12", "when_raw": 1, "modes": ["cool", "heat"]}]}}
    # cap_mode [2,4]=cool,heat ∩ fork [cool,heat] = cool,heat → heat offered
    assert evaluate_offered(gdef, mode="heat", power_on=True,
                            active_constraints={"valid_set": [2, 4]}, cap_values={"0x12": 1}).offered
    # cap_mode [2]=cool ∩ fork [cool,heat] = cool → heat blocked
    assert not evaluate_offered(gdef, mode="heat", power_on=True,
                                active_constraints={"valid_set": [2]}, cap_values={"0x12": 1}).offered
