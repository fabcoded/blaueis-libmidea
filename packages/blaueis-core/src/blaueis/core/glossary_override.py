"""Glossary override support — merge user-supplied patches into the base
glossary for per-device testing and debugging.

The base glossary is a read-only singleton loaded from ``glossary.yaml``.
An override is a partial glossary — the SAME schema, any subset of keys.
Overrides let a user flip a single leaf (e.g.
``fields.screen_display.capability.values.supported.feature_available``
from ``always`` to ``excluded``) without rewriting the whole entry.

Merge semantics:

- **Deep merge.** Scalar leaves in the override replace scalars in the base.
  Dict nodes are recursed. Lists are replaced wholesale (there is no
  semantic way to "patch" a list without a key schema).
- **``_remove: true`` sentinel.** A dict node with ``_remove: true``
  causes the corresponding key in the base to be deleted from the merge
  result. Use sparingly — the schema validator runs on the merged result
  and will reject removals that break required-key constraints.
- **Meta is protected.** The override's ``meta`` subtree is silently
  stripped before merging (recorded in ``messages``). Users never need
  to touch ``meta`` and doing so usually indicates a mistaken
  whole-glossary paste.
- **Excluded fields are gated by reason.** If the override targets a
  field whose base ``feature_available == "excluded"``, the field's
  ``excluded_reasons`` list determines the outcome (worst-wins): any
  reason in the reject set strips the override for that field; any
  reason in the caveat set keeps the merge but emits a warning;
  otherwise the merge proceeds as ``info``. See
  ``docs/exclusion_reasons.md`` for the full contract.

The third element of ``apply_override``'s return tuple is a list of
``OverrideMessage`` dataclasses — structured records the integration
renders as the user-facing status block on textarea submission. Every
non-fatal user-visible event flows through this channel: protected-key
strips, exclusion gating outcomes, future codes for unknown-field
warnings, etc.

This module does **no** schema validation — that's the caller's job
(see ``test_schema_validation.py`` for the schema + ``jsonschema``
library usage). Separation keeps this module free of the
``jsonschema`` dep and lets unit tests focus on merge semantics.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Literal

__all__ = [
    "deep_merge",
    "sanitize_override",
    "apply_override",
    "OverrideMessage",
    "PROTECTED_KEYS",
    "REMOVE_SENTINEL",
    "REJECT_REASONS",
    "CAVEAT_REASONS",
]

# Top-level override keys we refuse to merge. Stripped silently with a
# message surfaced back to the caller (severity=warning, code=protected_key).
PROTECTED_KEYS: frozenset[str] = frozenset({"meta"})

# Sentinel that, when present as a key's value, deletes the key from
# the merged result. Must be the exact Python literal ``True`` paired
# with the key name; any other value of ``_remove`` is treated as a
# normal leaf and merged through.
REMOVE_SENTINEL = "_remove"

# Reason buckets driving exclusion gating. See docs/exclusion_reasons.md
# for the full taxonomy. Worst-wins on a list: any REJECT reason → reject;
# else any CAVEAT reason → caveat; else accept.
REJECT_REASONS: frozenset[str] = frozenset({"protocol_inert", "unknown_semantic", "unsafe_write"})
CAVEAT_REASONS: frozenset[str] = frozenset(
    {
        "decode_unverified",
        "unknown_technical_background",
        "never_observed",
        "never_tested_write",
    }
)


@dataclass(frozen=True)
class OverrideMessage:
    """Structured event surfaced from override processing.

    The integration's config-flow handler renders these as the inline
    status block the user sees on textarea submission. ``code`` is
    machine-readable (drives icon / color / template selection);
    ``message`` is the prose. ``field`` is the dotted path the message
    is about, or ``None`` for top-level events (protected-key strips).
    ``reasons`` is the field's ``excluded_reasons`` list when the code
    is exclusion-related; empty for other codes.
    """

    severity: Literal["info", "warning", "error"]
    code: str
    field: str | None
    reasons: list[str]
    message: str


def deep_merge(
    base: dict[str, Any],
    override: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Deep-merge ``override`` into a copy of ``base``.

    Returns ``(merged, affected_paths)`` where:

    - ``merged`` is a new dict — ``base`` is never mutated, ``override``
      is never mutated.
    - ``affected_paths`` lists the dotted paths of leaves whose value
      in ``merged`` differs from ``base``. Used for user-facing
      "fields changed" messages (G9, G12).

    If ``override`` is ``None`` or empty, returns ``(deepcopy(base), [])``.

    The caller is responsible for schema-validating ``merged``.
    """
    merged = copy.deepcopy(base)
    if not override:
        return merged, []
    affected: list[str] = []
    _merge_in_place(merged, override, path="", affected=affected)
    return merged, affected


def sanitize_override(
    override: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[OverrideMessage]]:
    """Return ``(clean_override, messages)`` after stripping protected
    top-level keys from ``override``.

    Does not deep-copy — it returns a new top-level dict with the
    protected keys omitted, but retains references to nested structures
    from the input. Call ``deep_merge`` downstream; ``deep_merge`` does
    its own deep-copy so this shallow omission is safe.

    If ``override`` is ``None``/empty, returns ``({}, [])``.
    """
    if not override:
        return {}, []
    messages: list[OverrideMessage] = []
    clean: dict[str, Any] = {}
    for k, v in override.items():
        if k in PROTECTED_KEYS:
            messages.append(
                OverrideMessage(
                    severity="warning",
                    code="protected_key",
                    field=None,
                    reasons=[],
                    message=(f"Ignoring protected top-level key: {k!r} (overrides must not modify {k})"),
                )
            )
            continue
        clean[k] = v
    return clean, messages


def apply_override(
    base: dict[str, Any],
    override: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str], list[OverrideMessage]]:
    """Sanitize + gate-by-reason + merge in one call.

    Returns ``(merged, affected_paths, messages)``:

    - ``merged`` — the patched glossary view. Excluded fields whose
      reasons land in the reject bucket are NOT merged through (the
      override patch for those fields is dropped before the merge).
    - ``affected_paths`` — leaves whose value changed; reflects the
      post-gate merge (rejected fields contribute zero affected paths).
    - ``messages`` — the structured event list (protected_key,
      excluded_accepted, excluded_caveat, excluded_rejected).

    Does not validate against a schema; the caller chains schema
    validation after this.
    """
    clean, messages = sanitize_override(override)
    clean, gating_messages = _gate_excluded(base, clean)
    messages.extend(gating_messages)
    merged, affected = deep_merge(base, clean)
    return merged, affected, messages


# ── internals ──────────────────────────────────────────────────────────


def _classify_reasons(reasons: Iterable[str]) -> Literal["accepted", "caveat", "rejected"]:
    """Worst-wins outcome for a list of excluded_reasons."""
    rs = set(reasons)
    if rs & REJECT_REASONS:
        return "rejected"
    if rs & CAVEAT_REASONS:
        return "caveat"
    return "accepted"


def _gate_excluded(
    base: dict[str, Any],
    override: dict[str, Any],
) -> tuple[dict[str, Any], list[OverrideMessage]]:
    """Walk override; for each field-level patch on a base field whose
    ``feature_available == 'excluded'``, classify by reasons and decide
    accept / caveat / reject.

    Rejected patches are stripped from the override (the field's entry
    is removed; deeper structure under it is untouched in the base).
    Accepted and caveat patches pass through unchanged. Returns the
    cleaned override plus one ``OverrideMessage`` per gated field.

    No-op for overrides that don't carry a ``fields`` block.
    """
    if not isinstance(override, dict):
        return override, []
    field_patches = override.get("fields")
    if not isinstance(field_patches, dict):
        return override, []
    base_fields = base.get("fields", {}) if isinstance(base, dict) else {}
    cleaned = copy.deepcopy(override)
    messages: list[OverrideMessage] = []
    for cat_name, cat_patch in field_patches.items():
        if not isinstance(cat_patch, dict):
            continue
        base_cat = base_fields.get(cat_name) if isinstance(base_fields, dict) else None
        if not isinstance(base_cat, dict):
            continue
        for field_name in list(cat_patch.keys()):
            base_field = base_cat.get(field_name)
            if not isinstance(base_field, dict):
                continue
            if base_field.get("feature_available") != "excluded":
                continue
            reasons_raw = base_field.get("excluded_reasons") or []
            reasons = [r for r in reasons_raw if isinstance(r, str)]
            outcome = _classify_reasons(reasons)
            field_path = f"fields.{cat_name}.{field_name}"
            if outcome == "rejected":
                # Strip the patch for this field from cleaned override.
                del cleaned["fields"][cat_name][field_name]
                messages.append(
                    OverrideMessage(
                        severity="error",
                        code="excluded_rejected",
                        field=field_path,
                        reasons=list(reasons),
                        message=(
                            f"override rejected: {field_name!r} is excluded for "
                            f"{', '.join(reasons) or '<no reasons>'} — at least one "
                            f"reason cannot be overridden via YAML"
                        ),
                    )
                )
            elif outcome == "caveat":
                messages.append(
                    OverrideMessage(
                        severity="warning",
                        code="excluded_caveat",
                        field=field_path,
                        reasons=list(reasons),
                        message=(
                            f"override accepted with caveat: {field_name!r} is excluded "
                            f"for {', '.join(reasons)} — surfaced behaviour may be "
                            f"unverified"
                        ),
                    )
                )
            else:  # "accepted"
                messages.append(
                    OverrideMessage(
                        severity="info",
                        code="excluded_accepted",
                        field=field_path,
                        reasons=list(reasons),
                        message=(
                            f"override accepted: {field_name!r} is excluded for {', '.join(reasons) or '<no reasons>'}"
                        ),
                    )
                )
    return cleaned, messages


def _merge_in_place(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    path: str,
    affected: list[str],
) -> None:
    """Recursively merge ``source`` into ``target`` (mutating target).

    ``path`` tracks the dotted key path so we can report which leaves
    were affected. ``affected`` accumulates those paths.
    """
    for key, src_val in source.items():
        sub_path = f"{path}.{key}" if path else key

        # Handle remove sentinel — delete key from target if present.
        if isinstance(src_val, dict) and src_val.get(REMOVE_SENTINEL) is True:
            if key in target:
                del target[key]
                affected.append(sub_path)
            continue

        if key not in target:
            # Adding a new key — entire subtree counts as affected at
            # every leaf (for accurate G9 markers).
            target[key] = copy.deepcopy(src_val)
            _mark_leaves(sub_path, src_val, affected)
            continue

        tgt_val = target[key]
        if isinstance(tgt_val, dict) and isinstance(src_val, dict):
            _merge_in_place(tgt_val, src_val, path=sub_path, affected=affected)
        else:
            # Scalar / list / type-mismatch: replace outright, but only
            # record as affected if the value actually changed.
            if tgt_val != src_val:
                target[key] = copy.deepcopy(src_val)
                affected.append(sub_path)


def _mark_leaves(path: str, value: Any, affected: list[str]) -> None:
    """Walk a value subtree, appending the dotted path of every leaf to
    ``affected``. Lists are treated as leaves at the list level."""
    if isinstance(value, dict):
        if not value:
            affected.append(path)
            return
        for k, v in value.items():
            _mark_leaves(f"{path}.{k}", v, affected)
    else:
        affected.append(path)
