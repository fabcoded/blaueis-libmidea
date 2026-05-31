"""Named decode-step predicates — the sanctioned cross-state escape hatch.

A glossary decode step may carry ``condition_predicate: <name>`` instead of
the two literal ``condition:`` strings (``"!= 0"`` / ``"> 0"``). The name
resolves to a pure function here that decides whether the step fires, given the
full frame ``body``, the ``step`` dict, and the step's already-extracted
``val``. This keeps cross-byte / mode-aware logic in named Python — the
"predicate registry" escape hatch ``ux_gating`` points to — rather than growing
a predicate DSL inside the YAML.

Predicates are pure ``(body, step, val) -> bool``. Return ``False`` to SKIP the
step (identical effect to a failed ``condition:``); the decoder then falls
through to the next step in the chain.
"""

from __future__ import annotations

from collections.abc import Callable

# operating_mode raw values where target_temperature is not a meaningful
# setpoint — the unit is targeting humidity, not temperature, and the OEM
# forces the temperature to 0. From the operating_mode glossary entry:
# dry = raw 3, smart_dry = raw 6.
DRY_MODES = frozenset({3, 6})


def temp_extended_setpoint_active(body: bytes, step: dict, val: int) -> bool:
    """Gate the ``body[13]`` extended-setpoint override for ``target_temperature``.

    Fires only when the override is a *real* setpoint:

    * ``val != 0`` — preserves the field's original ``condition: "!= 0"``
      sentinel (raw 0 means "no override; keep the body[2] primary").
    * mode not in :data:`DRY_MODES` — in dry / smart_dry the target temperature
      is meaningless, so the override must not run. The decoder then falls back
      to the ``body[2] & 0x0F + 16`` primary/held setpoint instead of a number
      derived from the dehumidify byte.

    ``mode`` is read from the same frame: ``body[2]`` bits[7:5].
    """
    if val == 0:
        return False
    if len(body) <= 2:
        return True
    mode = (body[2] & 0xE0) >> 5
    return mode not in DRY_MODES


# name -> pure (body, step, val) -> bool
DECODE_PREDICATES: dict[str, Callable[[bytes, dict, int], bool]] = {
    "temp_extended_setpoint_active": temp_extended_setpoint_active,
}
