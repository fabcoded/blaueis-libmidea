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
        - {field: auxiliary_heat_level, at: 'C0:9:4..3', blocks_when: nonzero, modes: [heat, auto]}
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


def _is_mode_raw(x: object) -> bool:
    """True iff x is an operating-mode raw (int 1-5, excluding bool)."""
    return isinstance(x, int) and not isinstance(x, bool) and x in MODE_INT_TO_NAME


def _cap_mode_set(gate: dict, active_constraints: dict | None) -> set[str] | None:
    """Mode NAMES the active capability permits, or None when no cap-mode gate applies.

    Honoured only when ``gate.cap_mode`` is declared AND the live
    ``active_constraints.valid_set`` is genuinely *operating-mode raws* (ints 1-5).
    The pre-B5 permissive default carries a value-domain valid_set (e.g. turbo's
    ``[False, True]`` on/off); reinterpreting that as modes would wrongly gate
    everything to ``auto`` (True→1). So when the valid_set is not all mode-raws —
    pre-B5, or a field-value constraint (the value-vs-mode axis trap) — the cap-mode
    axis stays inert and the field falls back to its logical mode rule.
    """
    if not gate.get("cap_mode") or not isinstance(active_constraints, dict):
        return None
    valid_set = active_constraints.get("valid_set")
    if not isinstance(valid_set, (list, tuple)) or not valid_set:
        return None
    if not all(_is_mode_raw(r) for r in valid_set):
        return None
    return {MODE_INT_TO_NAME[r] for r in valid_set}


def _mode_fork_set(gate: dict, cap_values: dict | None) -> set[str] | None:
    """Mode NAMES from the first matching ``gate.mode_forks`` entry, or None.

    A fork maps a capability byte value to an explicit mode set the way a
    ``valid_set`` cannot (e.g. eco cool-only when 0x12==1 vs cool/auto/dry when
    ==2). Returns None when no fork is declared or none matches the unit's cap
    bytes — leaving the logical mode rule unrestricted (fail open).
    """
    forks = gate.get("mode_forks") or []
    if not forks or not isinstance(cap_values, dict):
        return None
    for fork in forks:
        cap_id = str(fork.get("cap_id", "")).lower()
        if cap_id and cap_values.get(cap_id) == fork.get("when_raw"):
            modes = fork.get("modes")
            return set(modes) if isinstance(modes, (list, tuple)) else None
    return None


def evaluate_offered(
    field_gdef: dict | None,
    *,
    mode: int | str | None,
    power_on: bool,
    active_constraints: dict | None = None,
    field_states: dict | None = None,
    caps: dict | None = None,
    cap_values: dict | None = None,
) -> GateVerdict:
    """Evaluate whether a field should be offered, across all gate axes.

    Parameters mirror the live status a caller already has: ``mode`` /
    ``power_on`` from the decoded status, ``active_constraints`` from the field's
    cap-derived constraints (``status['fields'][f]['active_constraints']``),
    ``field_states`` a {name: value} map for interlock dependencies (their now-
    retained decoded values), ``caps`` the B5 flag bitmap for ``hardware_flag``,
    ``cap_values`` a {cap_id: raw_byte} map of the unit's B5 caps for mode forks.
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
    # Cap-derived mode set = intersection of every declared cap restriction:
    # the cap_mode valid_set and the matching mode_fork. None ⇒ that axis inert.
    cap_modes = None
    for restriction in (_cap_mode_set(gate, active_constraints), _mode_fork_set(gate, cap_values)):
        if restriction is not None:
            cap_modes = restriction if cap_modes is None else (cap_modes & restriction)
    if cap_modes is not None:
        name = _mode_name(mode)
        # name is None → mode unknown (pre-first-poll); fail open, matching is_field_visible.
        if name is not None and name not in cap_modes:
            blocked.append(f"cap_mode:{name}∉{sorted(cap_modes)}")

    # ── interlock axis: cross-feature live state (B1 fail-open if unknown) ──
    for il in gate.get("interlocks") or []:
        # Optional mode guard: an interlock that reads a mode-multiplexed bit
        # (e.g. auxiliary_heat_level shares C0:9 bit4 with eco_mode) is only
        # meaningful in the modes where that bit carries the dependency's value.
        # Outside those modes the interlock is inactive (the bit means something
        # else), so skip it rather than misread a neighbour field's bit.
        il_modes = il.get("modes")
        if il_modes is not None:
            name = _mode_name(mode)
            if name is None or name not in il_modes:
                continue
        fname = il.get("field")
        state = (field_states or {}).get(fname) if fname else None
        if state is None:
            # dependency absent / cap-absent / not yet decoded → vacuously satisfied
            continue
        blocks_when = il.get("blocks_when", "nonzero")
        if blocks_when in ("nonzero", "truthy") and state or blocks_when in ("zero", "off") and not state:
            blocked.append(f"interlock:{fname}")

    # ── mutex axis: declared via gate.mutex_group; the existing breeze cascade
    #    enforces the forced-off siblings. Nothing to evaluate here (G1). ──

    return GateVerdict(offered=not blocked, blocked_by=blocked)
