"""Bit-position anchors for gate / interlock references — base-constraint B1.

A gate or interlock term names a field **and** its physical wire address, never
the name alone. The name is what reads the retained value; the address is where
that value lives on the wire. The two must agree or it is a bug.

This module resolves a field's wire address from the glossary ``decode`` blocks
and verifies a registry of anchors still resolves to the declared address — so a
field rename, a reused name, or a decode-offset edit fails CI instead of silently
re-aiming a gate at the wrong bit. It is the static, pre-deploy half of the
constraint (the gate evaluator enforces the same anchors at load time).

Skeleton scope: the seed registry below pins the interlock fields the gate model
depends on. When the structured ``gate:`` block lands, its per-term ``at:`` anchors
feed straight into :func:`verify_gate_anchors` alongside these.

Canonical address forms (high bit first, matching the glossary ``bits: [hi, lo]``):
    frame-offset   ``<CODE>:<offset>:<hi>..<lo>``     e.g. ``C0:8:5..5``
    B1 property    ``B1:<property_id>:<hi>..<lo>``     e.g. ``B1:0x67,0x00:0..0``

Note a field can sit at *different* addresses per protocol — ``eco_mode`` is
written at ``W40:9:7..7`` but read back at ``C0:9:4..4`` — so an anchor is always
protocol-qualified; the status-read protocol is the one a gate predicate consults.
"""
from __future__ import annotations

from blaueis.core.codec import walk_fields

# Short code <-> glossary protocol key. Only the frames an anchor can name.
# (rsp_0xc1 group variants would need their own codes; not used by the seed set.)
_CODE_PROTO: dict[str, str] = {
    "C0": "rsp_0xc0",
    "C1": "rsp_0xc1",
    "B1": "rsp_0xb1",
    "W40": "cmd_0x40",
    "WB0": "cmd_0xb0",
}


def _fmt_bits(bits: list[int]) -> str:
    hi, lo = bits
    return f"{hi}..{lo}"


def field_addresses(glossary: dict, field: str, proto_code: str) -> list[str]:
    """Canonical wire addresses for ``field`` under the ``proto_code`` protocol.

    Returns one address per ``decode`` step (usually one). Empty list if the
    field, the protocol entry, or its ``decode`` block is absent — the caller
    treats "no address" as drift, not as a silently-satisfied gate.
    """
    proto_key = _CODE_PROTO.get(proto_code)
    if proto_key is None:
        return []
    fdef = walk_fields(glossary).get(field)
    if not fdef:
        return []
    ploc = fdef.get("protocols", {}).get(proto_key)
    if not ploc:
        return []
    out: list[str] = []
    for step in ploc.get("decode") or []:
        bits = step.get("bits")
        if not bits:
            continue
        pid = step.get("property_id")
        locus = pid if pid is not None else step.get("offset")
        out.append(f"{proto_code}:{locus}:{_fmt_bits(bits)}")
    return out


# Seed anchor registry — {field: expected status-read address}. Grounded against
# the live glossary. Do NOT edit an entry to make a failing check pass: a mismatch
# means a rename/decode drift to fix at the source, which is the whole point.
GATE_ANCHORS: dict[str, str] = {
    "strong_wind": "C0:8:5..5",        # the boost ("Turbo") control acts here
    "turbo_mode": "C0:10:1..1",        # distinct bit; preset mis-targets this (wiring bug)
    "eco_mode": "C0:9:4..4",           # status read; cmd writes W40:9:7..7 (per-protocol differs)
    "sleep_mode": "C0:10:0..0",
    "natural_wind": "C0:9:1..1",
    "jet_cool": "B1:0x67,0x00:0..0",
    "breeze_away": "B1:0x42,0x00:7..0",
}


def collect_glossary_gate_anchors(glossary: dict) -> dict[str, str]:
    """Anchors declared in field ``gate.interlocks[].at`` → ``{dep_field: at}``.

    Each interlock names a dependency field and the wire address its value lives
    at; this surfaces those so :func:`verify_gate_anchors` checks them alongside
    the seed registry. Empty until fields opt into ``gate:`` blocks.
    """
    found: dict[str, str] = {}
    for fdef in walk_fields(glossary).values():
        for il in (fdef.get("gate") or {}).get("interlocks") or []:
            dep, at = il.get("field"), il.get("at")
            if dep and at:
                found[dep] = at
    return found


def verify_all_anchors(glossary: dict) -> list[str]:
    """Verify the seed registry AND every glossary gate-block anchor."""
    return verify_gate_anchors(glossary, {**GATE_ANCHORS, **collect_glossary_gate_anchors(glossary)})


def verify_gate_anchors(
    glossary: dict, anchors: dict[str, str] | None = None
) -> list[str]:
    """Check each anchored field still resolves to its declared wire address.

    Returns a list of human-readable problems (empty => every anchor holds).
    A problem is reported when the field is missing (rename), has no decode for
    the anchor's protocol, or resolves to a different bit-position (drift).
    """
    anchors = GATE_ANCHORS if anchors is None else anchors
    problems: list[str] = []
    for field, expected in anchors.items():
        proto_code = expected.split(":", 1)[0]
        resolved = field_addresses(glossary, field, proto_code)
        if not resolved:
            problems.append(
                f"{field}: anchor {expected!r} unresolved — field missing or no "
                f"{proto_code} decode (rename / decode drift?)"
            )
        elif expected not in resolved:
            problems.append(
                f"{field}: anchor {expected!r} != resolved {resolved} (bit-position drift)"
            )
    return problems
