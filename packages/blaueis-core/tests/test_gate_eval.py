"""Tests for blaueis.core.gate_eval.evaluate_offered (gate engine, G1 skeleton).

Covers: parity with is_field_visible when no `gate:` block; the power axis; the
cap-derived mode axis (only active when gate.cap_mode marks the valid_set as
operating-mode raws); and the interlock axis (fail-open when the dependency state
is unknown). No glossary `gate:` blocks exist yet, so production behaviour is
unchanged — these exercise the evaluator directly with synthetic field defs.
"""
from __future__ import annotations

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
