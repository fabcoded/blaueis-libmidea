"""Tests for Device's ``glossary_overrides`` kwarg (G2).

Covers the wire-up between ``apply_override`` and ``StatusDB`` via
``Device.__init__``: the override must be merged into a per-instance
patched glossary view, and downstream consumers (walk_fields,
available_fields, field_gdef) must see the patched values.

We don't start the client here — construction is enough to verify the
glossary plumbing. No WebSocket, no handshake.
"""

from __future__ import annotations

import pytest

from blaueis.client.device import Device


def _get_field_feature_available(device: Device, field_name: str) -> str | None:
    """Pull ``feature_available`` from the device's patched glossary."""
    gdef = device.field_gdef(field_name)
    if gdef is None:
        return None
    return gdef.get("feature_available")


def test_no_override_uses_base_glossary():
    """Sanity: without overrides, ``screen_display.feature_available``
    should be whatever the on-disk glossary declares (currently
    ``readable``, but we just assert it's not ``never``)."""
    device = Device(host="127.0.0.1", port=65535, psk="0" * 32)
    fa = _get_field_feature_available(device, "screen_display")
    assert fa is not None
    assert fa != "never"
    assert device.glossary_override_affected == []


def test_override_flips_feature_available_to_never():
    """Override sets screen_display's feature_available to never —
    the patched glossary reflects this, and the affected-paths list
    records the leaf that changed."""
    override = {
        "fields": {
            "control": {
                "screen_display": {
                    "feature_available": "never",
                },
            },
        },
    }
    device = Device(
        host="127.0.0.1",
        port=65535,
        psk="0" * 32,
        glossary_overrides=override,
    )
    fa = _get_field_feature_available(device, "screen_display")
    assert fa == "never"
    assert (
        "fields.control.screen_display.feature_available"
        in device.glossary_override_affected
    )


def test_override_does_not_mutate_base_glossary():
    """A first Device with an override must not leak the patched view
    into a second Device built without overrides. Each Device gets its
    own patched copy; the global glossary stays pristine."""
    override = {"fields": {"control": {"screen_display": {"feature_available": "never"}}}}
    dev_patched = Device(
        host="127.0.0.1", port=65535, psk="0" * 32,
        glossary_overrides=override,
    )
    dev_clean = Device(host="127.0.0.1", port=65536, psk="0" * 32)

    assert _get_field_feature_available(dev_patched, "screen_display") == "never"
    # Second device uses the same on-disk glossary — unpatched.
    assert _get_field_feature_available(dev_clean, "screen_display") != "never"


def test_meta_override_silently_stripped():
    """Overrides of the protected ``meta`` block must be dropped without
    failing. The rest of the override must still apply."""
    override = {
        "meta": {"version": "99.0.0"},   # stripped
        "fields": {"control": {"screen_display": {"feature_available": "never"}}},
    }
    device = Device(
        host="127.0.0.1", port=65535, psk="0" * 32,
        glossary_overrides=override,
    )
    # meta.version should come from the on-disk glossary, not "99.0.0".
    assert device.glossary["meta"]["version"] != "99.0.0"
    # But the fields override still applied.
    assert _get_field_feature_available(device, "screen_display") == "never"


def test_empty_override_dict_equivalent_to_none():
    """An empty dict is treated the same as None — no override, no
    affected paths."""
    device = Device(
        host="127.0.0.1", port=65535, psk="0" * 32,
        glossary_overrides={},
    )
    assert device.glossary_override_affected == []


def test_override_list_of_affected_is_a_copy():
    """``glossary_override_affected`` returns a list that callers can
    mutate without affecting the Device's internal state."""
    override = {"fields": {"control": {"screen_display": {"feature_available": "never"}}}}
    device = Device(
        host="127.0.0.1", port=65535, psk="0" * 32,
        glossary_overrides=override,
    )
    paths = device.glossary_override_affected
    paths.append("tamper")
    assert "tamper" not in device.glossary_override_affected
