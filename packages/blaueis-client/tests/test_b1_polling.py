"""Tests for Device's B1 property polling integration."""
from __future__ import annotations

import pytest

from blaueis.client.device import Device, _parse_b1_property_id
from blaueis.core.frame import parse_frame


# ── _parse_b1_property_id ────────────────────────────────────────────────

def test_parse_string() -> None:
    assert _parse_b1_property_id("0x42,0x00") == (0x42, 0x00)
    assert _parse_b1_property_id("0x0B, 0x02") == (0x0B, 0x02)


def test_parse_tuple_list() -> None:
    assert _parse_b1_property_id((0x42, 0)) == (0x42, 0)
    assert _parse_b1_property_id([0x09, 4]) == (0x09, 0x04)


def test_parse_int_hi_defaults_zero() -> None:
    assert _parse_b1_property_id(0x42) == (0x42, 0)


def test_parse_junk_returns_none() -> None:
    assert _parse_b1_property_id(None) is None
    assert _parse_b1_property_id("not a pair") is None
    assert _parse_b1_property_id({"lo": 1}) is None
    assert _parse_b1_property_id([1, 2, 3]) is None


# ── Device._compute_required_queries emits B1 batch keys ────────────────

@pytest.fixture(autouse=True)
def _restore_walk_fields():
    """Each B1 test patches walk_fields in the device module — restore after."""
    import blaueis.client.device as mod
    orig = mod.walk_fields
    yield
    mod.walk_fields = orig


def _patch_device_with_b1_fields(dev: Device, prop_ids: list[tuple[int, int]]) -> None:
    """Install enough fake state so _compute_required_queries sees B1 fields.

    Stubs the status DB and patches `walk_fields` in the device module so
    every prop_id becomes a distinct field with a readable B1 decode entry.
    The autouse fixture above restores `walk_fields` after each test.
    """
    dev._status = {"fields": {}, "meta": {}}
    fake_glossary: dict[str, dict] = {}
    for i, (lo, hi) in enumerate(prop_ids):
        fname = f"fake_b1_field_{i}"
        dev._status["fields"][fname] = {
            "feature_available": "readable",
            "data_type": "bool",
            "writable": False,
        }
        fake_glossary[fname] = {
            "field_class": "stateful_bool",
            "protocols": {
                "rsp_0xb1": {
                    "direction": "response",
                    "decode": [{"property_id": f"0x{lo:02X},0x{hi:02X}"}],
                },
            },
        }

    import blaueis.client.device as mod
    mod.walk_fields = lambda _glossary: fake_glossary
    dev._glossary = {"_stub": True}


def test_no_b1_fields_no_batch_keys() -> None:
    dev = Device.__new__(Device)
    _patch_device_with_b1_fields(dev, [])
    q = dev._compute_required_queries()
    assert not any(k.startswith("cmd_0xb1_batch_") for k in q)
    assert dev._b1_prop_ids == []


def test_single_batch_for_few_props() -> None:
    dev = Device.__new__(Device)
    _patch_device_with_b1_fields(dev, [(0x42, 0), (0x48, 0), (0x39, 0)])
    q = dev._compute_required_queries()
    assert "cmd_0xb1_batch_0" in q
    assert "cmd_0xb1_batch_1" not in q  # 3 props fit in one 8-wide batch
    # sorted by (lo, hi)
    assert dev._b1_prop_ids == [(0x39, 0), (0x42, 0), (0x48, 0)]


def test_multiple_batches_for_many_props() -> None:
    dev = Device.__new__(Device)
    props = [(i, 0) for i in range(1, 21)]  # 20 props → ceil(20/8)=3 batches
    _patch_device_with_b1_fields(dev, props)
    q = dev._compute_required_queries()
    batch_keys = sorted(k for k in q if k.startswith("cmd_0xb1_batch_"))
    assert batch_keys == ["cmd_0xb1_batch_0", "cmd_0xb1_batch_1", "cmd_0xb1_batch_2"]


def test_dedupes_same_prop_across_fields() -> None:
    dev = Device.__new__(Device)
    # Two fields, same property id → one batch, one pair.
    _patch_device_with_b1_fields(dev, [(0x42, 0), (0x42, 0)])
    dev._compute_required_queries()
    assert dev._b1_prop_ids == [(0x42, 0)]


# ── _build_query_frame dispatches to B1 builder ─────────────────────────

def test_build_query_frame_b1_batch_0() -> None:
    dev = Device.__new__(Device)
    _patch_device_with_b1_fields(dev, [(0x42, 0), (0x48, 0), (0x39, 0)])
    dev._compute_required_queries()  # populates _b1_prop_ids
    frame = dev._build_query_frame("cmd_0xb1_batch_0")
    assert frame is not None
    parsed = parse_frame(frame)
    body = parsed["body"]
    assert body[0] == 0xB1
    assert body[1] == 3
    # sorted: 0x39, 0x42, 0x48 — each followed by its hi byte (0)
    assert [body[2], body[4], body[6]] == [0x39, 0x42, 0x48]


def test_build_query_frame_b1_batch_out_of_range() -> None:
    dev = Device.__new__(Device)
    _patch_device_with_b1_fields(dev, [(0x42, 0)])
    dev._compute_required_queries()
    # batch 5 doesn't exist → None, not crash
    assert dev._build_query_frame("cmd_0xb1_batch_5") is None


def test_response_to_query_for_rsp_0xb1_returns_none() -> None:
    # rsp_0xb1 is handled by _compute_required_queries directly; the static
    # map returns None to avoid generating a static `cmd_0xb1` key.
    assert Device._response_to_query("rsp_0xb1") is None
    # Existing maps unaffected.
    assert Device._response_to_query("rsp_0xc0") == "cmd_0x41"
    assert Device._response_to_query("rsp_0xc1_group1") == "cmd_0xc1_group1"
    assert Device._response_to_query("rsp_0xb5") == "cmd_0xb5"
