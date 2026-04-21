"""Unit tests for ``blaueis.core.glossary_override``.

Covers:
- deep_merge: scalar replace, dict recurse, list replace, None/empty.
- affected_paths: only actual changes reported, not no-op merges.
- _remove sentinel: key deleted from result when present in base.
- sanitize_override: meta stripped with warning, everything else passes.
- apply_override: end-to-end composition.
"""

from __future__ import annotations

import pytest

from blaueis.core.glossary_override import (
    PROTECTED_KEYS,
    apply_override,
    deep_merge,
    sanitize_override,
)


# ── deep_merge ─────────────────────────────────────────────────────────


def test_empty_override_returns_base_copy():
    base = {"fields": {"screen_display": {"feature_available": "always"}}}
    merged, affected = deep_merge(base, None)
    assert merged == base
    assert affected == []
    # Must be a copy, not the same object.
    merged["fields"]["screen_display"]["feature_available"] = "never"
    assert base["fields"]["screen_display"]["feature_available"] == "always"


def test_empty_dict_override_is_noop():
    base = {"a": 1}
    merged, affected = deep_merge(base, {})
    assert merged == {"a": 1}
    assert affected == []


def test_scalar_leaf_replacement():
    base = {"fields": {"screen_display": {"feature_available": "always"}}}
    override = {"fields": {"screen_display": {"feature_available": "never"}}}
    merged, affected = deep_merge(base, override)
    assert merged["fields"]["screen_display"]["feature_available"] == "never"
    assert affected == ["fields.screen_display.feature_available"]


def test_nested_merge_preserves_sibling_keys():
    """Merging one leaf must not erase sibling keys."""
    base = {
        "fields": {
            "screen_display": {
                "description": "Display LED",
                "feature_available": "always",
                "data_type": "bool",
            },
        },
    }
    override = {
        "fields": {"screen_display": {"feature_available": "never"}},
    }
    merged, _ = deep_merge(base, override)
    assert merged["fields"]["screen_display"]["description"] == "Display LED"
    assert merged["fields"]["screen_display"]["data_type"] == "bool"
    assert merged["fields"]["screen_display"]["feature_available"] == "never"


def test_adding_new_field():
    base = {"fields": {"screen_display": {"data_type": "bool"}}}
    override = {"fields": {"new_field": {"data_type": "int", "feature_available": "always"}}}
    merged, affected = deep_merge(base, override)
    assert "new_field" in merged["fields"]
    assert merged["fields"]["new_field"]["data_type"] == "int"
    # Both leaves of the newly-added subtree should be reported.
    assert set(affected) == {
        "fields.new_field.data_type",
        "fields.new_field.feature_available",
    }


def test_list_replacement_not_concatenation():
    """Lists are replaced wholesale — no merging semantics."""
    base = {"fields": {"x": {"values": ["a", "b", "c"]}}}
    override = {"fields": {"x": {"values": ["z"]}}}
    merged, affected = deep_merge(base, override)
    assert merged["fields"]["x"]["values"] == ["z"]
    assert affected == ["fields.x.values"]


def test_noop_merge_reports_no_affected():
    """If override value equals base value, nothing is reported."""
    base = {"fields": {"x": {"feature_available": "always"}}}
    override = {"fields": {"x": {"feature_available": "always"}}}
    _, affected = deep_merge(base, override)
    assert affected == []


def test_type_mismatch_replaces():
    """Override dict-vs-scalar type mismatch: scalar wins."""
    base = {"a": {"b": 1}}
    override = {"a": 42}
    merged, affected = deep_merge(base, override)
    assert merged["a"] == 42
    assert affected == ["a"]


def test_base_is_never_mutated():
    base = {"a": {"b": 1}}
    override = {"a": {"b": 2}}
    deep_merge(base, override)
    assert base == {"a": {"b": 1}}


def test_override_is_never_mutated():
    base = {"a": {"b": 1}}
    override = {"a": {"b": 2}}
    deep_merge(base, override)
    assert override == {"a": {"b": 2}}


# ── _remove sentinel ───────────────────────────────────────────────────


def test_remove_sentinel_deletes_key():
    base = {
        "fields": {
            "screen_display": {"feature_available": "always"},
            "other_field": {"feature_available": "always"},
        },
    }
    override = {"fields": {"other_field": {"_remove": True}}}
    merged, affected = deep_merge(base, override)
    assert "other_field" not in merged["fields"]
    assert "screen_display" in merged["fields"]
    assert affected == ["fields.other_field"]


def test_remove_sentinel_on_missing_key_is_noop():
    """Asking to remove a key that doesn't exist is silently a no-op —
    the affected list stays empty."""
    base = {"fields": {"a": 1}}
    override = {"fields": {"nonexistent": {"_remove": True}}}
    _, affected = deep_merge(base, override)
    assert affected == []


def test_remove_false_is_not_a_sentinel():
    """Only ``_remove: True`` is a sentinel. ``_remove: false`` or other
    values are treated as a normal leaf and merged through."""
    base = {"fields": {"x": {"feature_available": "always"}}}
    override = {"fields": {"x": {"_remove": False}}}
    merged, _ = deep_merge(base, override)
    # _remove becomes a normal leaf on x.
    assert merged["fields"]["x"]["_remove"] is False
    assert merged["fields"]["x"]["feature_available"] == "always"


# ── sanitize_override ──────────────────────────────────────────────────


def test_meta_stripped_with_warning():
    override = {
        "meta": {"version": "99.0.0"},
        "fields": {"x": {"feature_available": "never"}},
    }
    clean, warnings = sanitize_override(override)
    assert "meta" not in clean
    assert "fields" in clean
    assert len(warnings) == 1
    assert "meta" in warnings[0]


def test_sanitize_empty_override():
    clean, warnings = sanitize_override(None)
    assert clean == {}
    assert warnings == []
    clean, warnings = sanitize_override({})
    assert clean == {}
    assert warnings == []


def test_non_protected_keys_pass_through():
    override = {"fields": {"x": 1}, "encodings": {"bcd": {"scale": 10}}}
    clean, warnings = sanitize_override(override)
    assert clean == override
    assert warnings == []


def test_protected_keys_set_is_frozen():
    """PROTECTED_KEYS should be immutable — the module's policy, not a
    per-call knob."""
    with pytest.raises(AttributeError):
        PROTECTED_KEYS.add("anything")  # type: ignore[attr-defined]


# ── apply_override (composition) ───────────────────────────────────────


def test_apply_override_end_to_end():
    base = {
        "meta": {"version": "1.0.0"},
        "fields": {"screen_display": {"feature_available": "always"}},
    }
    override = {
        "meta": {"version": "99.0.0"},           # stripped
        "fields": {"screen_display": {"feature_available": "never"}},
    }
    merged, affected, warnings = apply_override(base, override)

    # Meta stripped → base meta preserved.
    assert merged["meta"]["version"] == "1.0.0"
    # Field leaf patched.
    assert merged["fields"]["screen_display"]["feature_available"] == "never"
    # Affected path reported for the changed leaf only.
    assert affected == ["fields.screen_display.feature_available"]
    # Warning surfaced for meta.
    assert len(warnings) == 1
    assert "meta" in warnings[0]


def test_apply_override_no_override():
    base = {"meta": {"version": "1.0.0"}, "fields": {}}
    merged, affected, warnings = apply_override(base, None)
    assert merged == base
    assert affected == []
    assert warnings == []
