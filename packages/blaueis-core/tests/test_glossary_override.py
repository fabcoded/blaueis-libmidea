"""Unit tests for ``blaueis.core.glossary_override``.

Covers:
- deep_merge: scalar replace, dict recurse, list replace, None/empty.
- affected_paths: only actual changes reported, not no-op merges.
- _remove sentinel: key deleted from result when present in base.
- sanitize_override: meta stripped, OverrideMessage emitted.
- apply_override: end-to-end composition.
- exclusion gating: per-reason accept/caveat/reject classification.
"""

from __future__ import annotations

import pytest
from blaueis.core.glossary_override import (
    CAVEAT_REASONS,
    PROTECTED_KEYS,
    REJECT_REASONS,
    OverrideMessage,
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
    merged["fields"]["screen_display"]["feature_available"] = "excluded"
    assert base["fields"]["screen_display"]["feature_available"] == "always"


def test_empty_dict_override_is_noop():
    base = {"a": 1}
    merged, affected = deep_merge(base, {})
    assert merged == {"a": 1}
    assert affected == []


def test_scalar_leaf_replacement():
    base = {"fields": {"screen_display": {"feature_available": "always"}}}
    override = {"fields": {"screen_display": {"feature_available": "excluded"}}}
    merged, affected = deep_merge(base, override)
    assert merged["fields"]["screen_display"]["feature_available"] == "excluded"
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
        "fields": {"screen_display": {"feature_available": "excluded"}},
    }
    merged, _ = deep_merge(base, override)
    assert merged["fields"]["screen_display"]["description"] == "Display LED"
    assert merged["fields"]["screen_display"]["data_type"] == "bool"
    assert merged["fields"]["screen_display"]["feature_available"] == "excluded"


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
        "fields": {"x": {"feature_available": "excluded"}},
    }
    clean, messages = sanitize_override(override)
    assert "meta" not in clean
    assert "fields" in clean
    assert len(messages) == 1
    msg = messages[0]
    assert isinstance(msg, OverrideMessage)
    assert msg.code == "protected_key"
    assert msg.severity == "warning"
    assert msg.field is None
    assert "meta" in msg.message


def test_sanitize_empty_override():
    clean, messages = sanitize_override(None)
    assert clean == {}
    assert messages == []
    clean, messages = sanitize_override({})
    assert clean == {}
    assert messages == []


def test_non_protected_keys_pass_through():
    override = {"fields": {"x": 1}, "encodings": {"bcd": {"scale": 10}}}
    clean, messages = sanitize_override(override)
    assert clean == override
    assert messages == []


def test_protected_keys_set_is_frozen():
    """PROTECTED_KEYS should be immutable — the module's policy, not a
    per-call knob."""
    with pytest.raises(AttributeError):
        PROTECTED_KEYS.add("anything")  # type: ignore[attr-defined]


# ── apply_override (composition) ───────────────────────────────────────


def test_apply_override_end_to_end():
    base = {
        "meta": {"version": "1.0.0"},
        "fields": {"sensor": {"screen_display": {"feature_available": "always"}}},
    }
    override = {
        "meta": {"version": "99.0.0"},  # stripped
        "fields": {"sensor": {"screen_display": {"feature_available": "excluded"}}},
    }
    merged, affected, messages = apply_override(base, override)

    # Meta stripped → base meta preserved.
    assert merged["meta"]["version"] == "1.0.0"
    # Field leaf patched.
    assert merged["fields"]["sensor"]["screen_display"]["feature_available"] == "excluded"
    # Affected path reported for the changed leaf only.
    assert affected == ["fields.sensor.screen_display.feature_available"]
    # Message surfaced for meta strip — base field is not excluded so no
    # exclusion-gating message.
    assert len(messages) == 1
    assert messages[0].code == "protected_key"
    assert "meta" in messages[0].message


def test_apply_override_no_override():
    base = {"meta": {"version": "1.0.0"}, "fields": {}}
    merged, affected, messages = apply_override(base, None)
    assert merged == base
    assert affected == []
    assert messages == []


# ── exclusion gating ───────────────────────────────────────────────────


def _excluded_base(reasons: list[str]) -> dict:
    """Synthetic base glossary with one excluded sensor field."""
    return {
        "fields": {
            "sensor": {
                "victim": {
                    "feature_available": "excluded",
                    "excluded_reasons": list(reasons),
                    "data_type": "uint8",
                },
            },
        },
    }


def test_gating_unnecessary_automation_accepted_clean():
    """unnecessary_automation alone → accepted (info, no caveat)."""
    base = _excluded_base(["unnecessary_automation"])
    override = {
        "fields": {"sensor": {"victim": {"feature_available": "always"}}},
    }
    merged, affected, messages = apply_override(base, override)
    # Patch passes through.
    assert merged["fields"]["sensor"]["victim"]["feature_available"] == "always"
    # One info-level message.
    assert len(messages) == 1
    msg = messages[0]
    assert msg.code == "excluded_accepted"
    assert msg.severity == "info"
    assert msg.field == "fields.sensor.victim"
    assert msg.reasons == ["unnecessary_automation"]


def test_gating_never_observed_caveat():
    """never_observed → caveat (warning), patch still applied."""
    base = _excluded_base(["never_observed"])
    override = {
        "fields": {"sensor": {"victim": {"feature_available": "readable"}}},
    }
    merged, _affected, messages = apply_override(base, override)
    # Patch passes through despite caveat.
    assert merged["fields"]["sensor"]["victim"]["feature_available"] == "readable"
    assert len(messages) == 1
    msg = messages[0]
    assert msg.code == "excluded_caveat"
    assert msg.severity == "warning"
    assert msg.reasons == ["never_observed"]


def test_gating_protocol_inert_rejected():
    """protocol_inert → rejected (error). Patch is dropped."""
    base = _excluded_base(["protocol_inert"])
    override = {
        "fields": {"sensor": {"victim": {"feature_available": "always"}}},
    }
    merged, affected, messages = apply_override(base, override)
    # Patch is stripped — base value preserved in merge.
    assert merged["fields"]["sensor"]["victim"]["feature_available"] == "excluded"
    # Affected paths reflect the post-gate merge: nothing changed.
    assert affected == []
    assert len(messages) == 1
    msg = messages[0]
    assert msg.code == "excluded_rejected"
    assert msg.severity == "error"
    assert msg.reasons == ["protocol_inert"]


def test_gating_unknown_semantic_rejected():
    """unknown_semantic also lands in the reject set."""
    base = _excluded_base(["unknown_semantic"])
    override = {
        "fields": {"sensor": {"victim": {"feature_available": "readable"}}},
    }
    merged, _affected, messages = apply_override(base, override)
    assert merged["fields"]["sensor"]["victim"]["feature_available"] == "excluded"
    assert messages[0].code == "excluded_rejected"


def test_gating_worst_wins_caveat_plus_clean():
    """[unnecessary_automation, never_tested_write] → caveat (worst wins)."""
    base = _excluded_base(["unnecessary_automation", "never_tested_write"])
    override = {
        "fields": {"sensor": {"victim": {"feature_available": "always"}}},
    }
    merged, _affected, messages = apply_override(base, override)
    # Patch still applied (caveat does not strip).
    assert merged["fields"]["sensor"]["victim"]["feature_available"] == "always"
    assert len(messages) == 1
    msg = messages[0]
    assert msg.code == "excluded_caveat"
    assert msg.severity == "warning"
    assert set(msg.reasons) == {"unnecessary_automation", "never_tested_write"}


def test_gating_worst_wins_reject_dominates_caveat():
    """[never_observed, protocol_inert] → rejected (reject dominates caveat)."""
    base = _excluded_base(["never_observed", "protocol_inert"])
    override = {
        "fields": {"sensor": {"victim": {"feature_available": "always"}}},
    }
    merged, _affected, messages = apply_override(base, override)
    assert merged["fields"]["sensor"]["victim"]["feature_available"] == "excluded"
    assert messages[0].code == "excluded_rejected"


def test_gating_non_excluded_field_silent():
    """Override on a non-excluded field emits no exclusion message."""
    base = {
        "fields": {
            "sensor": {
                "victim": {"feature_available": "readable", "data_type": "uint8"},
            },
        },
    }
    override = {
        "fields": {"sensor": {"victim": {"feature_available": "readable-opt"}}},
    }
    _merged, _affected, messages = apply_override(base, override)
    # No exclusion-gating message for non-excluded base field.
    assert messages == []


def test_gating_buckets_match_documented_split():
    """Sanity-check the reason buckets stay aligned with the documented
    contract: reject ∩ caveat must be empty; unnecessary_automation is in
    neither (i.e. lands in the implicit 'accept clean' bucket)."""
    assert REJECT_REASONS.isdisjoint(CAVEAT_REASONS)
    assert "unnecessary_automation" not in REJECT_REASONS
    assert "unnecessary_automation" not in CAVEAT_REASONS
    # The full enum coverage of REJECT ∪ CAVEAT ∪ {unnecessary_automation}
    # should match the schema enum (8 values).
    full = REJECT_REASONS | CAVEAT_REASONS | {"unnecessary_automation"}
    assert len(full) == 8
