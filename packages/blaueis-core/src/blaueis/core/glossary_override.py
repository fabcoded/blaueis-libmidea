"""Glossary override support — merge user-supplied patches into the base
glossary for per-device testing and debugging.

The base glossary is a read-only singleton loaded from ``glossary.yaml``.
An override is a partial glossary — the SAME schema, any subset of keys.
Overrides let a user flip a single leaf (e.g.
``fields.screen_display.capability.values.supported.feature_available``
from ``always`` to ``never``) without rewriting the whole entry.

Merge semantics:

- **Deep merge.** Scalar leaves in the override replace scalars in the base.
  Dict nodes are recursed. Lists are replaced wholesale (there is no
  semantic way to "patch" a list without a key schema).
- **``_remove: true`` sentinel.** A dict node with ``_remove: true``
  causes the corresponding key in the base to be deleted from the merge
  result. Use sparingly — the schema validator runs on the merged result
  and will reject removals that break required-key constraints.
- **Meta is protected.** The override's ``meta`` subtree is silently
  stripped before merging (returned in ``warnings``). Users never need
  to touch ``meta`` and doing so usually indicates a mistaken
  whole-glossary paste.

This module does **no** schema validation — that's the caller's job
(see ``test_schema_validation.py`` for the schema + ``jsonschema``
library usage). Separation keeps this module free of the
``jsonschema`` dep and lets unit tests focus on merge semantics.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable

__all__ = [
    "deep_merge",
    "sanitize_override",
    "apply_override",
    "PROTECTED_KEYS",
    "REMOVE_SENTINEL",
]

# Top-level override keys we refuse to merge. Stripped silently with a
# warning surfaced back to the caller.
PROTECTED_KEYS: frozenset[str] = frozenset({"meta"})

# Sentinel that, when present as a key's value, deletes the key from
# the merged result. Must be the exact Python literal ``True`` paired
# with the key name; any other value of ``_remove`` is treated as a
# normal leaf and merged through.
REMOVE_SENTINEL = "_remove"


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
) -> tuple[dict[str, Any], list[str]]:
    """Return ``(clean_override, warnings)`` after stripping protected
    top-level keys from ``override``.

    Does not deep-copy — it returns a new top-level dict with the
    protected keys omitted, but retains references to nested structures
    from the input. Call ``deep_merge`` downstream; ``deep_merge`` does
    its own deep-copy so this shallow omission is safe.

    If ``override`` is ``None``/empty, returns ``({}, [])``.
    """
    if not override:
        return {}, []
    warnings: list[str] = []
    clean: dict[str, Any] = {}
    for k, v in override.items():
        if k in PROTECTED_KEYS:
            warnings.append(
                f"Ignoring protected top-level key: {k!r} "
                f"(overrides must not modify {k})"
            )
            continue
        clean[k] = v
    return clean, warnings


def apply_override(
    base: dict[str, Any],
    override: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Sanitize + merge in one call.

    Returns ``(merged, affected_paths, warnings)``. Does not validate
    against a schema; the caller chains schema validation after this.
    """
    clean, warnings = sanitize_override(override)
    merged, affected = deep_merge(base, clean)
    return merged, affected, warnings


# ── internals ──────────────────────────────────────────────────────────


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
