"""UX-layer visibility helpers.

Evaluates ``ux.visible_in_modes`` / ``ux.hardware_flag`` from a glossary
field against the current device state. **Advisory only** — the evaluator
never gates wire behaviour (``flight_recorder.md §1.1`` stateless
invariant). Consumers:

- HA entity ``available`` properties — UI mask
- ``blaueis.core.command.build_command_body`` — normalise stale bits in
  outgoing C3 frames so mode-incompatible values don't hitchhike

The rule set is intentionally small: a flat ``visible_in_modes`` list and
an optional ``hardware_flag`` indirection for sensors that physically
don't exist on the SKU. Anything more structured is deliberately not
supported — if a rule needs cross-state predicates, it belongs in Python
(named, in a predicate registry) rather than growing a DSL here.
"""
from __future__ import annotations

from typing import Any

# Keep this tiny and self-contained. The mode-name ↔ int translation is
# duplicated in HA's const.py; the two must stay in sync, but the lookup
# here accepts either form so callers can pass whichever is handy.
_MODE_INT_TO_NAME: dict[int, str] = {
    1: "auto",
    2: "cool",
    3: "dry",
    4: "heat",
    5: "fan_only",
}


def is_field_visible(
    field_gdef: dict | None,
    *,
    current_mode: int | str | None,
    caps: dict | None = None,
) -> bool:
    """Return False only when the field's ``ux`` block masks it.

    Fields without a ``ux`` key — i.e. every glossary entry today — are
    always visible. Absence is permissive; the caller never needs to
    special-case it.

    Parameters
    ----------
    field_gdef:
        The glossary entry for the field (as returned by ``walk_fields``).
        ``None`` or empty → always visible.
    current_mode:
        Either an int (``2``) or a mode name (``"cool"``). ``None`` means
        mode unknown; we fail open (visible) and let the next poll correct
        the state.
    caps:
        Dict of B5-derived capability flags, e.g.
        ``{"b5_has_pm25_sensor": False}``. Missing keys read as falsy.
    """
    if not field_gdef:
        return True
    ux = field_gdef.get("ux") or {}
    if not ux:
        return True

    # Hardware flag — masks permanently when the SKU lacks the sensor.
    hw_flag = ux.get("hardware_flag")
    if hw_flag is not None and not (caps or {}).get(hw_flag):
        return False

    # Mode list — absent means no mode gating.
    modes = ux.get("visible_in_modes")
    if modes is None:
        return True
    if current_mode is None:
        # Mode not yet known (pre-first-poll). Fail open — the entity
        # will refresh as soon as operating_mode lands in the status DB.
        return True
    if current_mode in modes:
        return True
    name = _MODE_INT_TO_NAME.get(current_mode) if isinstance(current_mode, int) else None
    return name in modes


def default_for_masked_field(field_gdef: dict | None) -> Any:
    """Safe default to encode when a UX-masked field is NOT in `changes`.

    Used by the command builder to zero out stale bits. Preference order:

    1. Glossary-declared ``default_value`` (already used elsewhere for
       protocol-reserved bits).
    2. Type-zero (``False`` / ``0`` / ``""``).
    """
    if not field_gdef:
        return 0
    dv = field_gdef.get("default_value")
    if dv is not None:
        return dv
    dt = field_gdef.get("data_type", "")
    if dt == "bool":
        return False
    if dt in ("uint8", "uint16", "int8", "int16", "float"):
        return 0
    return 0
