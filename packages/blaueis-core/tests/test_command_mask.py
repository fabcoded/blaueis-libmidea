"""Tests for UX-mask behaviour in build_command_body.

Covers the new masking path — stale bits for fields whose ``ux`` block
excludes the current mode get forced to their default value on the
outgoing frame. Fields without a ``ux`` block, or explicitly present in
``changes``, are unaffected.
"""
from __future__ import annotations

import copy
import pytest

from blaueis.core.command import build_command_body
from blaueis.core.codec import load_glossary


# ── Helpers ─────────────────────────────────────────────────────────────

def _glossary_with_ux(base: dict, field_name: str, visible_in_modes: list) -> dict:
    """Return a deep-copied glossary with a ``ux.visible_in_modes`` block
    injected on the named field. Walks ``fields`` → ``{category}`` → field."""
    g = copy.deepcopy(base)
    for cat_name, category in g.get("fields", {}).items():
        if field_name in category:
            category[field_name].setdefault("ux", {})
            category[field_name]["ux"]["visible_in_modes"] = list(visible_in_modes)
            return g
    pytest.fail(f"field {field_name!r} not found in glossary")


def _status_with_field(glossary, field_name: str, value, mode: int, ts: float = 1000.0) -> dict:
    """Minimal status dict carrying a value for `field_name` plus the
    operating_mode. Each field's `sources` block holds one fresh slot.
    """
    from blaueis.core.query import write_field
    status: dict = {"fields": {}, "meta": {}}
    write_field(status, field_name, value, ts=ts)
    write_field(status, "operating_mode", mode, ts=ts)
    return status


# ── Tests ───────────────────────────────────────────────────────────────

def test_stale_masked_field_zeroed_on_mode_change() -> None:
    """Status=cool with eco=True. changes={mode:heat} and eco visible only
    in cool. The outgoing body must carry eco=False (masked)."""
    base = load_glossary()
    g = _glossary_with_ux(base, "eco_mode", ["cool", "auto", "dry"])
    status = _status_with_field(g, "eco_mode", True, mode=2)

    result = build_command_body(
        status, changes={"operating_mode": 4}, glossary=g, skip_preflight=True,
    )
    assert result["body"] is not None
    # Re-parse to confirm eco bit is off. We use the same glossary decode
    # path — if eco_mode was encoded True we'd see True here.
    from blaueis.core.codec import decode_frame_fields
    decoded = decode_frame_fields(bytes(result["body"]), "cmd_0x40", g)
    assert decoded["eco_mode"]["value"] is False


def test_explicit_set_of_masked_field_passes_through() -> None:
    """Even if eco is "masked" in heat, an explicit changes={eco_mode:True}
    goes on the wire — the AC is authoritative."""
    base = load_glossary()
    g = _glossary_with_ux(base, "eco_mode", ["cool"])
    status = _status_with_field(g, "eco_mode", False, mode=4)

    result = build_command_body(
        status,
        changes={"operating_mode": 4, "eco_mode": True},
        glossary=g, skip_preflight=True,
    )
    from blaueis.core.codec import decode_frame_fields
    decoded = decode_frame_fields(bytes(result["body"]), "cmd_0x40", g)
    assert decoded["eco_mode"]["value"] is True


def test_no_ux_block_unaffected() -> None:
    """Regression guard: a field without ``ux`` behaves exactly as before —
    value carries over from status into the outgoing frame."""
    base = load_glossary()
    status = _status_with_field(base, "eco_mode", True, mode=2)

    result = build_command_body(
        status, changes={"operating_mode": 2}, glossary=base, skip_preflight=True,
    )
    from blaueis.core.codec import decode_frame_fields
    decoded = decode_frame_fields(bytes(result["body"]), "cmd_0x40", base)
    # status had eco=True, no ux masking in base glossary, mode unchanged ->
    # outgoing eco must be True.
    assert decoded["eco_mode"]["value"] is True


def test_mask_uses_new_mode_when_changes_includes_operating_mode() -> None:
    """Edge case: status has mode=cool, changes say mode=heat, eco visible
    only in cool. Effective mode for masking must be the NEW mode (heat),
    so eco gets zeroed. If we accidentally used status mode, eco would
    incorrectly pass through."""
    base = load_glossary()
    g = _glossary_with_ux(base, "eco_mode", ["cool"])
    status = _status_with_field(g, "eco_mode", True, mode=2)  # cool

    result = build_command_body(
        status, changes={"operating_mode": 4}, glossary=g, skip_preflight=True,
    )
    from blaueis.core.codec import decode_frame_fields
    decoded = decode_frame_fields(bytes(result["body"]), "cmd_0x40", g)
    assert decoded["eco_mode"]["value"] is False


def test_mode_in_visible_list_passes() -> None:
    """Field whose current mode IS in visible_in_modes is not masked."""
    base = load_glossary()
    g = _glossary_with_ux(base, "eco_mode", ["cool"])
    status = _status_with_field(g, "eco_mode", True, mode=2)  # cool

    result = build_command_body(
        status, changes={"operating_mode": 2}, glossary=g, skip_preflight=True,
    )
    from blaueis.core.codec import decode_frame_fields
    decoded = decode_frame_fields(bytes(result["body"]), "cmd_0x40", g)
    assert decoded["eco_mode"]["value"] is True
