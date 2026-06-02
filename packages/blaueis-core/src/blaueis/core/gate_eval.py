"""Three-axis gate evaluator — capability ∧ mode ∧ interlock ∧ power ∧ ¬mutex.

Composes the existing logical-mode rule (``ux_gating.is_field_visible``) with the
capability-derived mode axis (the active cap value's ``valid_set``) and the runtime
interlock axis (cross-feature live state, B1-anchored). A field with no ``gate:``
block reduces to today's behaviour: ``power_on ∧ is_field_visible`` — so wiring this
in is parity-preserving until a field opts in.

Advisory only, like ``ux_gating``: this drives entity availability / UI offering,
never wire behaviour. The wire path stays stateless.

Schema (all keys optional; see gating-audit design doc):
    gate:
      requires_power: true            # default true
      cap_mode: {cap_id: '0x1A'}      # active cap's valid_set IS operating-mode raws
      interlocks:                     # B1 dual-key cross-feature gates
        - {field: ptc_state, at: 'C0:9:4..3', blocks_when: nonzero}
      mutex_group: breeze             # declared; the existing cascade enforces it
"""
from __future__ import annotations

from dataclasses import dataclass

from blaueis.core.ux_gating import MODE_INT_TO_NAME, is_field_visible


@dataclass
class GateVerdict:
    """Result of a gate evaluation. ``offered`` is the AND of every axis;
    ``blocked_by`` names each failing axis (empty iff offered)."""

    offered: bool
    blocked_by: list[str]


def _mode_name(mode: int | str | None) -> str | None:
    if isinstance(mode, str):
        return mode
    if isinstance(mode, int):
        return MODE_INT_TO_NAME.get(mode)
    return None


def _cap_mode_set(gate: dict, active_constraints: dict | None) -> set[str] | None:
    """Mode NAMES the active capability permits, or None when no cap-mode gate applies.

    Only honoured when ``gate.cap_mode`` is declared — that marker is what makes the
    live ``active_constraints.valid_set`` mean *operating-mode raws* rather than
    field-value raws (the value-vs-mode axis trap). Without it we never reinterpret
    a valid_set as modes.
    """
    if not gate.get("cap_mode") or not active_constraints:
        return None
    valid_set = active_constraints.get("valid_set")
    if valid_set is None:
        return None
    return {n for n in (_mode_name(r) for r in valid_set) if n is not None}


def evaluate_offered(
    field_gdef: dict | None,
    *,
    mode: int | str | None,
    power_on: bool,
    active_constraints: dict | None = None,
    field_states: dict | None = None,
    caps: dict | None = None,
) -> GateVerdict:
    """Evaluate whether a field should be offered, across all gate axes.

    Parameters mirror the live status a caller already has: ``mode`` /
    ``power_on`` from the decoded status, ``active_constraints`` from the field's
    cap-derived constraints (``status['fields'][f]['active_constraints']``),
    ``field_states`` a {name: value} map for interlock dependencies (their now-
    retained decoded values), ``caps`` the B5 flag bitmap for ``hardware_flag``.
    """
    gdef = field_gdef or {}
    gate = gdef.get("gate") or {}
    blocked: list[str] = []

    # ── power axis ──
    if gate.get("requires_power", True) and not power_on:
        blocked.append("power_off")

    # ── mode axis: logical (visible_in_modes / hardware_flag) ∩ cap-derived ──
    if not is_field_visible(gdef, current_mode=mode, caps=caps):
        blocked.append("mode")
    cap_modes = _cap_mode_set(gate, active_constraints)
    if cap_modes is not None:
        name = _mode_name(mode)
        # name is None → mode unknown (pre-first-poll); fail open, matching is_field_visible.
        if name is not None and name not in cap_modes:
            blocked.append(f"cap_mode:{name}∉{sorted(cap_modes)}")

    # ── interlock axis: cross-feature live state (B1 fail-open if unknown) ──
    for il in gate.get("interlocks") or []:
        fname = il.get("field")
        state = (field_states or {}).get(fname) if fname else None
        if state is None:
            # dependency absent / cap-absent / not yet decoded → vacuously satisfied
            continue
        blocks_when = il.get("blocks_when", "nonzero")
        if blocks_when in ("nonzero", "truthy") and state:
            blocked.append(f"interlock:{fname}")
        elif blocks_when in ("zero", "off") and not state:
            blocked.append(f"interlock:{fname}")

    # ── mutex axis: declared via gate.mutex_group; the existing breeze cascade
    #    enforces the forced-off siblings. Nothing to evaluate here (G1). ──

    return GateVerdict(offered=not blocked, blocked_by=blocked)
