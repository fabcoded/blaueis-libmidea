"""meta.last_ingest_at advances on every successful frame ingest.

Single-timestamp staleness window: consumers compute
``now - last_ingest_at`` and compare against their threshold. If the
field stops advancing, the device is silent (powered off, firmware
crash, comms partition).

The test confirms it advances for every ingest path (B5 / C0 / C1 /
A1 / B1) and survives multi-frame interleaving.
"""

from __future__ import annotations

from blaueis.core.codec import load_glossary
from blaueis.core.process import process_b5, process_data_frame
from blaueis.core.status import build_status


def _fresh_status() -> dict:
    g = load_glossary()
    return build_status(device="test", glossary=g)


# ── B5 ──────────────────────────────────────────────────────────────


def test_process_b5_sets_last_ingest_at() -> None:
    status = _fresh_status()
    assert status["meta"].get("last_ingest_at") is None
    g = load_glossary()
    # Minimum valid B5: header byte + record_count=0 + next_frame=0 + reserved=0
    process_b5(status, b"\xb5\x00\x00\x00", g, timestamp="2026-05-05T20:00:00+00:00")
    assert status["meta"]["last_ingest_at"] == "2026-05-05T20:00:00+00:00"


# ── C0 / C1 / A1 / B1 ────────────────────────────────────────────────


def _data_body() -> bytes:
    """A short body — process_data_frame will call decode_frame_fields
    which is robust against trailing-empty buffers; the meta update
    runs unconditionally regardless of decode outcome."""
    return b"\xc0" + b"\x00" * 30


def test_process_data_frame_sets_last_ingest_at_for_c0() -> None:
    status = _fresh_status()
    g = load_glossary()
    process_data_frame(
        status,
        _data_body(),
        "rsp_0xc0",
        g,
        timestamp="2026-05-05T20:00:01+00:00",
    )
    assert status["meta"]["last_ingest_at"] == "2026-05-05T20:00:01+00:00"


def test_process_data_frame_sets_last_ingest_at_for_b1() -> None:
    status = _fresh_status()
    g = load_glossary()
    process_data_frame(
        status,
        b"\xb1\x00\x00",
        "rsp_0xb1",
        g,
        timestamp="2026-05-05T20:00:02+00:00",
    )
    assert status["meta"]["last_ingest_at"] == "2026-05-05T20:00:02+00:00"


def test_last_ingest_at_advances_on_subsequent_ingests() -> None:
    """Multi-frame sequence advances the timestamp monotonically."""
    status = _fresh_status()
    g = load_glossary()
    process_data_frame(
        status,
        _data_body(),
        "rsp_0xc0",
        g,
        timestamp="2026-05-05T20:00:00+00:00",
    )
    first = status["meta"]["last_ingest_at"]
    process_data_frame(
        status,
        _data_body(),
        "rsp_0xc0",
        g,
        timestamp="2026-05-05T20:00:10+00:00",
    )
    second = status["meta"]["last_ingest_at"]
    assert first != second
    assert second > first  # ISO-8601 sorts lexicographically
