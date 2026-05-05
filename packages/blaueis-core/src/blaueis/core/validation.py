"""Pre-flight value validation for service calls.

Pure helpers that consult a field's glossary entry and the current
device status to decide whether a user-supplied value is acceptable
*before* the integration encodes a wire frame. Returns a structured
outcome that the calling layer (HA service handler, CLI, automation)
maps onto its own error-raising convention.

No HA imports here — the validator is glossary + status driven and
testable in isolation.

Design notes:

* The validator does **not** decide whether a field is writable —
  callers that know they're handling a control entity already know.
  The validator only answers "given the field's gates, is this value
  acceptable?".
* A field that declares no `range:` / no `values:` / no `valid_modes:`
  passes through every applicable gate as ``Ok``. The default is
  permissive; the glossary opts in to each gate per-field.
* If the current operating_mode cannot be read from ``status`` (slot
  not yet populated, integration just connected), the ``valid_modes:``
  check is skipped rather than raised — the validator cannot disagree
  with a state it doesn't know.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from blaueis.core.codec import walk_fields
from blaueis.core.query import read_field

# ── Outcome shapes ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidationOutcome:
    """Base for every outcome. ``ok`` is True only for :class:`Ok`."""

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class Ok(ValidationOutcome):
    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class FieldUnknown(ValidationOutcome):
    field_name: str


@dataclass(frozen=True)
class OutOfRange(ValidationOutcome):
    field_name: str
    value: Any
    min_value: Any
    max_value: Any


@dataclass(frozen=True)
class NotInEnum(ValidationOutcome):
    field_name: str
    value: Any
    allowed: tuple


@dataclass(frozen=True)
class ModeDisallowed(ValidationOutcome):
    field_name: str
    current_mode: str | None
    valid_modes: tuple


# ── Internal helpers ────────────────────────────────────────────────


def _operating_mode_token(status: dict, glossary: dict) -> str | None:
    """Resolve the current operating_mode to a token string (e.g. ``cool``).

    The wire-side value is an integer raw byte; ``valid_modes:`` declares
    tokens. We look up the raw → token map from
    ``operating_mode.values:`` and translate. Returns ``None`` if
    operating_mode hasn't been populated yet, or if the raw value isn't
    in the glossary's values block (unknown mode — let the caller decide
    whether to be conservative).
    """
    current = read_field(status, "operating_mode")
    if not current:
        return None
    raw = current.get("value")
    if raw is None:
        return None
    if isinstance(raw, str):
        # Already a token (older code paths sometimes return this); use it.
        return raw

    fields = walk_fields(glossary)
    op = fields.get("operating_mode") or {}
    values_block = op.get("values") or {}
    for token, vdef in values_block.items():
        if isinstance(vdef, dict) and vdef.get("raw") == raw:
            return token
    return None


def _glossary_field(glossary: dict, field_name: str) -> dict | None:
    fields = walk_fields(glossary)
    fdef = fields.get(field_name)
    return fdef if isinstance(fdef, dict) else None


def _enum_raws(values_block: dict) -> tuple:
    """Extract the tuple of allowed `raw` values from a glossary
    ``values:`` block. Skips entries without a `raw:` key."""
    out = []
    for vdef in values_block.values():
        if isinstance(vdef, dict) and "raw" in vdef:
            out.append(vdef["raw"])
    return tuple(out)


# ── Public API ──────────────────────────────────────────────────────


def validate_set(
    field_name: str,
    value: Any,
    status: dict,
    glossary: dict,
) -> ValidationOutcome:
    """Validate a proposed write against the field's glossary gates.

    Order of checks:

    1. Field exists in glossary → otherwise :class:`FieldUnknown`.
    2. ``range:`` declared and ``value`` is numeric and outside
       ``[min, max]`` → :class:`OutOfRange`.
    3. ``values:`` declared and ``value`` is not among the
       ``raw`` values listed → :class:`NotInEnum`. Booleans are skipped
       (HA's bool entity service contract handles them upstream).
    4. ``valid_modes:`` declared and the current operating mode is
       known and not in the list → :class:`ModeDisallowed`.
    5. Otherwise :class:`Ok`.

    Each gate is independently optional; an unspecified gate passes.
    """
    fdef = _glossary_field(glossary, field_name)
    if fdef is None:
        return FieldUnknown(field_name=field_name)

    rng = fdef.get("range")
    if (
        rng
        and isinstance(rng, list)
        and len(rng) == 2
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (value < rng[0] or value > rng[1])
    ):
        return OutOfRange(
            field_name=field_name,
            value=value,
            min_value=rng[0],
            max_value=rng[1],
        )

    values_block = fdef.get("values")
    if (
        values_block
        and isinstance(values_block, dict)
        and not isinstance(value, bool)
    ):
        allowed = _enum_raws(values_block)
        if allowed and value not in allowed:
            return NotInEnum(
                field_name=field_name,
                value=value,
                allowed=allowed,
            )

    valid_modes = fdef.get("valid_modes")
    if valid_modes and isinstance(valid_modes, list):
        current = _operating_mode_token(status, glossary)
        # Skip if mode is unknown — validator can't disagree with what
        # it can't see. Caller can choose to be more conservative.
        if current is not None and current not in valid_modes:
            return ModeDisallowed(
                field_name=field_name,
                current_mode=current,
                valid_modes=tuple(valid_modes),
            )

    return Ok()
