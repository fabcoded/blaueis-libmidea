"""B0/B1 per-property status-byte handling.

The third byte of every B0/B1 TLV is the firmware's per-property status
code. Both rejected statuses (``0x10`` / ``0x11`` / ``0x12``) and an
empty payload (``status=0x00, data_len=0``) mean "the byte at the
position the codec would decode is not a value" — decoding either would
raise IndexError (empty payload) or stamp garbage onto a glossary field.

These tests verify:
    * ``parse_b0b1_tlv`` exposes ``status``, ``outcome``, ``is_readable``
      with the correct classification per status / data_len pair.
    * ``decode_frame_fields`` skips non-readable TLVs and surfaces them
      via ``rejections_out``.
    * Mixed frames (one OK + one rejected) decode the OK field and
      drop only the rejected one.
    * A real two-property B1 reply (one OK byte + one empty payload)
      parses to the expected per-record outcomes.
"""

from __future__ import annotations

import pytest
from blaueis.core.codec import (
    B0B1_STATUS_FAILED,
    B0B1_STATUS_IN_PROGRESS,
    B0B1_STATUS_INVALID_ATTR,
    B0B1_STATUS_OK,
    B0B1_STATUS_VALUE_ERROR,
    classify_b0b1_record,
    decode_frame_fields,
    load_glossary,
    parse_b0b1_tlv,
)
from blaueis.core.process import process_data_frame
from blaueis.core.status import build_status


def _build_b1(props: list[tuple[int, int, int, bytes]]) -> bytes:
    """Synthesise a B1 reply body from explicit per-record fields.

    Each tuple is ``(prop_lo, prop_hi, status, data)`` — passing status
    explicitly so a test can craft any of the five outcome shapes.
    ``data_len`` is taken from ``len(data)``.
    """
    body = bytearray([0xB1, len(props)])
    for lo, hi, status, data in props:
        body.extend([lo, hi, status, len(data)])
        body.extend(data)
    return bytes(body)


# ── classify_b0b1_record ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "data_len", "expected"),
    [
        (B0B1_STATUS_OK, 1, "ok"),
        (B0B1_STATUS_OK, 5, "ok"),
        (B0B1_STATUS_OK, 0, "empty"),
        (B0B1_STATUS_IN_PROGRESS, 0, "in_progress"),
        (B0B1_STATUS_IN_PROGRESS, 4, "in_progress"),
        (B0B1_STATUS_FAILED, 0, "rejected"),
        (B0B1_STATUS_INVALID_ATTR, 0, "rejected"),
        (B0B1_STATUS_VALUE_ERROR, 0, "rejected"),
        (0x40, 0, "unknown"),
        (0xFF, 1, "unknown"),
    ],
)
def test_classifier(status: int, data_len: int, expected: str) -> None:
    assert classify_b0b1_record(status, data_len) == expected


# ── parse_b0b1_tlv exposes new fields ───────────────────────────────


def test_parse_records_ok() -> None:
    body = _build_b1([(0x4B, 0x00, B0B1_STATUS_OK, bytes([0x01]))])
    [rec] = parse_b0b1_tlv(body)
    assert rec["status"] == B0B1_STATUS_OK
    assert rec["outcome"] == "ok"
    assert rec["is_readable"] is True
    assert rec["data_len"] == 1
    assert rec["data"] == [0x01]


def test_parse_records_empty_payload() -> None:
    body = _build_b1([(0x24, 0x02, B0B1_STATUS_OK, b"")])
    [rec] = parse_b0b1_tlv(body)
    assert rec["outcome"] == "empty"
    assert rec["is_readable"] is False
    assert rec["data_len"] == 0


@pytest.mark.parametrize(
    "status",
    [B0B1_STATUS_FAILED, B0B1_STATUS_INVALID_ATTR, B0B1_STATUS_VALUE_ERROR],
)
def test_parse_records_rejected(status: int) -> None:
    body = _build_b1([(0x4B, 0x00, status, b"")])
    [rec] = parse_b0b1_tlv(body)
    assert rec["outcome"] == "rejected"
    assert rec["is_readable"] is False
    assert rec["status"] == status


def test_parse_two_property_capture_one_ok_one_empty() -> None:
    """A captured two-property B1 reply where 0x022C carries a one-byte
    value and 0x0224 returns ``status=0x00, data_len=0``. Verifies that
    the parser distinguishes the ok and empty outcomes on the same
    frame and that the cursor advances correctly past a zero-length
    payload."""

    wire = bytes.fromhex("aa18ac00000000000803b1022c0200010024020000000061c8")
    body = wire[10:-2]
    records = parse_b0b1_tlv(body)
    assert len(records) == 2
    by_id = {r["property_id"]: r for r in records}

    prop_022c = by_id["0x2C,0x02"]
    assert prop_022c["outcome"] == "ok"
    assert prop_022c["is_readable"] is True
    assert prop_022c["data"] == [0x00]

    prop_0224 = by_id["0x24,0x02"]
    assert prop_0224["outcome"] == "empty"
    assert prop_0224["is_readable"] is False
    assert prop_0224["data_len"] == 0


# ── decode_frame_fields skips non-readable records ──────────────────


def _glossary_property_for(field_name: str) -> str | None:
    """Best-effort lookup of the rsp_0xb1 property_id for a field, used
    to make tests robust against glossary edits. Returns the
    ``"0xLL,0xHH"`` string or None if the field has no B1 decode."""
    glossary = load_glossary()
    for cat in glossary.get("fields", {}).values():
        if not isinstance(cat, dict):
            continue
        for name, fdef in cat.items():
            if name != field_name:
                continue
            protocols = (fdef or {}).get("protocols", {}) or {}
            b1 = protocols.get("rsp_0xb1") or {}
            decode = b1.get("decode") or []
            if decode and "property_id" in decode[0]:
                return decode[0]["property_id"]
    return None


def _split_property_id(prop_str: str) -> tuple[int, int]:
    lo, hi = (int(p.strip(), 0) for p in prop_str.split(","))
    return lo & 0xFF, hi & 0xFF


def test_decode_skips_rejected_field() -> None:
    """A B1 reply with one rejected and one OK property: only the OK
    field appears in the result, and the rejection is surfaced via
    ``rejections_out``."""

    glossary = load_glossary()

    # Two stable single-field B1 properties in the public glossary —
    # picked so each property maps to exactly one glossary field, which
    # makes the assertions below unambiguous against future glossary
    # edits that might add sibling fields on shared property ids.
    ok_prop = _glossary_property_for("anion_ionizer")
    bad_prop = _glossary_property_for("buzzer")
    if not ok_prop or not bad_prop or ok_prop == bad_prop:
        pytest.skip("test requires anion_ionizer + buzzer as distinct B1 properties")
    ok_lo, ok_hi = _split_property_id(ok_prop)
    bad_lo, bad_hi = _split_property_id(bad_prop)

    body = _build_b1(
        [
            (ok_lo, ok_hi, B0B1_STATUS_OK, bytes([0x01])),
            (bad_lo, bad_hi, B0B1_STATUS_FAILED, b""),
        ]
    )

    rejections: list[dict] = []
    decoded = decode_frame_fields(body, "rsp_0xb1", glossary, rejections_out=rejections)

    assert "anion_ionizer" in decoded
    assert "buzzer" not in decoded
    rejected_fields = {r["field"]: r for r in rejections}
    assert "buzzer" in rejected_fields
    rec = rejected_fields["buzzer"]
    assert rec["status"] == B0B1_STATUS_FAILED
    assert rec["outcome"] == "rejected"
    assert rec["property_id"].lower() == bad_prop.lower()


def test_decode_skips_empty_payload_no_indexerror() -> None:
    """A B1 reply with a property whose data_len=0 must not raise
    IndexError when the decode step would have read body[offset]."""

    glossary = load_glossary()
    prop = _glossary_property_for("buzzer")
    if not prop:
        pytest.skip("glossary has no buzzer B1 property")
    lo, hi = _split_property_id(prop)

    body = _build_b1([(lo, hi, B0B1_STATUS_OK, b"")])

    rejections: list[dict] = []
    decoded = decode_frame_fields(body, "rsp_0xb1", glossary, rejections_out=rejections)

    assert "buzzer" not in decoded
    assert any(r["field"] == "buzzer" and r["outcome"] == "empty" for r in rejections)


def test_decode_without_rejections_out_drops_silently() -> None:
    """Callers that don't pass ``rejections_out`` get the same skip
    behaviour but no rejection list; today's downstream callers
    (tests using the bare 3-arg signature) keep working."""

    glossary = load_glossary()
    prop = _glossary_property_for("buzzer")
    if not prop:
        pytest.skip("glossary has no buzzer B1 property")
    lo, hi = _split_property_id(prop)
    body = _build_b1([(lo, hi, B0B1_STATUS_INVALID_ATTR, b"")])

    decoded = decode_frame_fields(body, "rsp_0xb1", glossary)
    assert "buzzer" not in decoded


# ── process_data_frame: rejection metadata, prior value preserved ───


def test_process_records_rejection_without_overwriting_prior_value() -> None:
    """A B1 frame with a rejected property must not clobber the
    ``value`` that a prior successful frame wrote into
    ``sources[protocol_key]``. The rejection lands as a sibling slot."""

    glossary = load_glossary()
    prop = _glossary_property_for("buzzer")
    if not prop:
        pytest.skip("glossary has no buzzer B1 property")
    lo, hi = _split_property_id(prop)

    status = build_status(device="test", glossary=glossary)
    buzzer = status["fields"].get("buzzer")
    if buzzer is None:
        pytest.skip("glossary has no buzzer field")
    buzzer["feature_available"] = "readable"

    # First frame: a healthy reading lands in sources[rsp_0xb1].
    ok_body = _build_b1([(lo, hi, B0B1_STATUS_OK, bytes([0x01]))])
    process_data_frame(status, ok_body, "rsp_0xb1", glossary, timestamp="t0")
    slot = buzzer["sources"]["rsp_0xb1"]
    assert slot["value"] is not None
    prior_value = slot["value"]

    # Second frame: same property comes back rejected.
    bad_body = _build_b1([(lo, hi, B0B1_STATUS_FAILED, b"")])
    process_data_frame(status, bad_body, "rsp_0xb1", glossary, timestamp="t1")

    slot = buzzer["sources"]["rsp_0xb1"]
    assert slot["value"] == prior_value, "rejection must not overwrite prior value"
    assert "rejection" in slot
    assert slot["rejection"]["outcome"] == "rejected"
    assert slot["rejection"]["status"] == B0B1_STATUS_FAILED
    assert slot["rejection"]["ts"] == "t1"

    counts = status["meta"]["frame_counts"]
    assert counts.get("rsp_0xb1_rejections", 0) >= 1


# ── in_progress + unknown outcomes flow through the same skip path ──
#
# `rejected` (0x10/0x11/0x12) and `empty` (0x00 + data_len 0) are the
# common cases. `in_progress` (0x01) and `unknown` (any other byte) are
# tested separately because their semantics differ — in_progress means
# "firmware will follow up", unknown means "investigate" — and the
# integration paths need to handle both without misclassifying as ok.


def test_decode_skips_in_progress_tlv() -> None:
    """A status=0x01 (in_progress) TLV must be skipped at decode and
    surfaced via rejections_out with outcome='in_progress'."""
    glossary = load_glossary()
    prop = _glossary_property_for("buzzer")
    if not prop:
        pytest.skip("glossary has no buzzer B1 property")
    lo, hi = _split_property_id(prop)
    body = _build_b1([(lo, hi, B0B1_STATUS_IN_PROGRESS, bytes([0x01]))])

    rejections: list[dict] = []
    decoded = decode_frame_fields(body, "rsp_0xb1", glossary, rejections_out=rejections)
    assert "buzzer" not in decoded
    assert any(r["field"] == "buzzer" and r["outcome"] == "in_progress" for r in rejections)


def test_decode_skips_unknown_status_tlv() -> None:
    """A status byte outside the documented set classifies as 'unknown'
    and skips decode — the codec defers to the investigator rather
    than guessing whether to trust the data."""
    glossary = load_glossary()
    prop = _glossary_property_for("buzzer")
    if not prop:
        pytest.skip("glossary has no buzzer B1 property")
    lo, hi = _split_property_id(prop)
    # 0x40 is outside the documented status set ({0x00, 0x01, 0x10,
    # 0x11, 0x12}); the codec must classify it as ``unknown`` and skip
    # rather than guess.
    body = _build_b1([(lo, hi, 0x40, bytes([0x01]))])

    rejections: list[dict] = []
    decoded = decode_frame_fields(body, "rsp_0xb1", glossary, rejections_out=rejections)
    assert "buzzer" not in decoded
    rec = next((r for r in rejections if r["field"] == "buzzer"), None)
    assert rec is not None
    assert rec["outcome"] == "unknown"
    assert rec["status"] == 0x40


def test_process_stamps_in_progress_rejection_slot() -> None:
    """process_data_frame routes in_progress through the same rejection
    slot as failed/empty — outcome carries the actual classification so
    a downstream reconciler can tell them apart."""
    glossary = load_glossary()
    prop = _glossary_property_for("buzzer")
    if not prop:
        pytest.skip("glossary has no buzzer B1 property")
    lo, hi = _split_property_id(prop)

    status = build_status(device="test", glossary=glossary)
    buzzer = status["fields"]["buzzer"]
    buzzer["feature_available"] = "readable"

    body = _build_b1([(lo, hi, B0B1_STATUS_IN_PROGRESS, b"")])
    process_data_frame(status, body, "rsp_0xb1", glossary, timestamp="t0")

    slot = buzzer["sources"]["rsp_0xb1"]
    assert "rejection" in slot
    assert slot["rejection"]["outcome"] == "in_progress"
    assert slot["rejection"]["status"] == B0B1_STATUS_IN_PROGRESS
    assert status["meta"]["frame_counts"].get("rsp_0xb1_rejections", 0) == 1


def test_process_stamps_unknown_rejection_slot() -> None:
    glossary = load_glossary()
    prop = _glossary_property_for("buzzer")
    if not prop:
        pytest.skip("glossary has no buzzer B1 property")
    lo, hi = _split_property_id(prop)

    status = build_status(device="test", glossary=glossary)
    buzzer = status["fields"]["buzzer"]
    buzzer["feature_available"] = "readable"

    body = _build_b1([(lo, hi, 0x77, b"")])  # undocumented status
    process_data_frame(status, body, "rsp_0xb1", glossary, timestamp="t0")

    slot = buzzer["sources"]["rsp_0xb1"]
    assert "rejection" in slot
    assert slot["rejection"]["outcome"] == "unknown"
    assert slot["rejection"]["status"] == 0x77
    assert status["meta"]["frame_counts"].get("rsp_0xb1_rejections", 0) == 1
