"""Per-field source query API.

Reads a field's value from the per-frame `sources` slot dict using a
priority list of scopes. Each scope is either a protocol-generation
token (`protocol_new`, `protocol_legacy`, `protocol_unknown`,
`protocol_all`) or a literal frame protocol_key (`rsp_0xc0`,
`rsp_0xa1`, etc.). The priority list is walked left to right; for each
entry the operator picks the slot with the newest `ts` matching that
scope. The first scope that returns a slot wins; subsequent scopes are
ignored. If no scope matches, the call returns None.

The vocabulary is intentionally small — there is no operator vocabulary,
no rule bundles, no aliases. A priority is just a list of scopes; the
implicit operator is "newest by ts".

See docs/field_query.md for the user-facing reference.
"""

from __future__ import annotations

# ── Scope vocabulary ────────────────────────────────────────────────

# Protocol-generation scopes — match by the slot's `generation` field.
GENERATION_SCOPES = {
    "protocol_new",  # generation == "new"
    "protocol_legacy",  # generation == "legacy"
    "protocol_unknown",  # generation is None
    "protocol_all",  # everything regardless of generation
}


def _slots_in_scope(sources: dict, scope: str) -> dict:
    """Return the subset of `sources` that matches the scope token.

    `scope` is either a generation token or a literal frame protocol_key.
    Unknown tokens return an empty dict — the cascade then advances.
    """
    if scope == "protocol_all":
        return sources
    if scope == "protocol_new":
        return {k: s for k, s in sources.items() if s.get("generation") == "new"}
    if scope == "protocol_legacy":
        return {k: s for k, s in sources.items() if s.get("generation") == "legacy"}
    if scope == "protocol_unknown":
        return {k: s for k, s in sources.items() if s.get("generation") is None}
    # Concrete frame key — single-slot lookup.
    if scope in sources:
        return {scope: sources[scope]}
    return {}


def _newest(slots: dict) -> tuple[str, dict] | None:
    """Return (slot_key, slot) with the newest `ts`, or None if empty.

    Slots without a `ts` field are skipped — a slot must be timestamped
    to participate in the cascade.
    """
    candidates = [(k, s) for k, s in slots.items() if s.get("ts")]
    if not candidates:
        return None
    return max(candidates, key=lambda kv: kv[1]["ts"])


# ── Public API ──────────────────────────────────────────────────────


def read_field(status: dict, name: str, priority: list[str] | None = None) -> dict | None:
    """Read a field's current value via a priority list of source scopes.

    Args:
        status: device status dict (the in-memory store).
        name: field name (e.g. "target_temperature").
        priority: ordered list of scope tokens. If None, falls back to
            the field's `default_priority`, then to the global default
            ["protocol_all"].

    Returns a dict with the resolved slot, the priority entry that
    matched, and a list of disagreeing slots:

        {
          "value": ...,
          "ts": ...,
          "source": "<protocol_key>",
          "generation": "legacy" | "new" | None,
          "scope_matched": "<priority entry>",
          "disagreements": [
            {"slot": ..., "value": ..., "ts": ..., "generation": ...},
            ...
          ],
        }

    Returns None if no scope in the priority list matches a populated
    slot, or if the field does not exist in `status`.

    `disagreements` always lists every slot in the *whole* `sources`
    dict whose value differs from the winner — the cascade scope does
    not constrain it. Callers can ignore the field, log it, or pivot
    to a different priority on the next call.
    """
    field = status.get("fields", {}).get(name)
    if field is None:
        return None

    sources = field.get("sources", {})
    if not sources:
        return None

    if priority is None:
        priority = field.get("default_priority") or ["protocol_all"]

    for scope in priority:
        in_scope = _slots_in_scope(sources, scope)
        winner = _newest(in_scope)
        if winner is None:
            continue
        slot_key, slot = winner
        return {
            "value": slot["value"],
            "ts": slot["ts"],
            "source": slot_key,
            "generation": slot.get("generation"),
            "scope_matched": scope,
            "disagreements": _list_disagreements(sources, slot_key, slot["value"]),
        }

    return None


def _list_disagreements(sources: dict, winner_key: str, winner_value) -> list[dict]:
    """Return every slot whose value differs from the winner.

    Walks the full `sources` dict — not just the matched scope — so a
    `protocol_legacy` read still surfaces a disagreeing `protocol_new`
    slot. The list is empty when every populated slot agrees.
    """
    disagreements = []
    for slot_key, slot in sources.items():
        if slot_key == winner_key:
            continue
        if slot.get("ts") is None:
            continue
        if slot.get("value") != winner_value:
            disagreements.append(
                {
                    "slot": slot_key,
                    "value": slot.get("value"),
                    "ts": slot.get("ts"),
                    "generation": slot.get("generation"),
                }
            )
    return disagreements
