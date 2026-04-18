"""Tests for UART TX/RX correlation and provenance threading."""
from __future__ import annotations

import time

import pytest
from blaueis.gateway.uart_protocol import (
    HEURISTIC_WINDOW,
    UartProtocol,
    _frame_msg_id,
)


def _frame(msg_id: int) -> bytes:
    """Build a minimal valid-ish frame with the given byte-10 msg_id."""
    body = bytes([0xAA, 0x20, 0xAC, 0, 0, 0, 0, 0, 0, 0x03, msg_id])
    return body + bytes(34 - len(body))


# ── msg_id extractor ─────────────────────────────────────────────────────

def test_frame_msg_id_happy() -> None:
    assert _frame_msg_id(_frame(0x41)) == 0x41
    assert _frame_msg_id(_frame(0xC0)) == 0xC0


def test_frame_msg_id_rejects_short_and_wrong_sync() -> None:
    assert _frame_msg_id(b"\xAA\x01") is None
    assert _frame_msg_id(b"\x55" + _frame(0x41)[1:]) is None


# ── Callback meta + correlation ───────────────────────────────────────────

def test_tx_emits_meta_with_origin_req_id_tx_seq() -> None:
    proto = UartProtocol(config={"frame_spacing_ms": 0})
    captured: list[tuple[bytes, float, str, dict]] = []
    proto.set_on_frame(lambda raw, ts, d="rx", meta=None: captured.append((raw, ts, d, meta or {})))

    proto._forward_to_client(_frame(0x41), direction="tx", origin="ws:2", req_id=99)

    [(_, _, d, meta)] = captured
    assert d == "tx"
    assert meta["origin"] == "ws:2"
    assert meta["req_id"] == 99
    assert meta["msg_id"] == 0x41
    assert meta["tx_seq"] == 1  # first TX in a fresh protocol


def test_rx_after_matching_tx_is_confirmed() -> None:
    proto = UartProtocol(config={"frame_spacing_ms": 0})
    records: list[dict] = []
    proto.set_on_frame(lambda raw, ts, d="rx", meta=None: records.append(meta or {}))

    proto._forward_to_client(_frame(0x41), direction="tx", origin="ws:2", req_id=99)
    proto._forward_to_client(_frame(0x41), direction="rx")

    rx_meta = records[-1]
    assert rx_meta["reply_to"] == {
        "req_id": 99, "origin": "ws:2", "confidence": "confirmed",
    }


def test_rx_with_different_msg_id_takes_heuristic() -> None:
    proto = UartProtocol(config={"frame_spacing_ms": 0})
    records: list[dict] = []
    proto.set_on_frame(lambda raw, ts, d="rx", meta=None: records.append(meta or {}))

    proto._forward_to_client(_frame(0x41), direction="tx", origin="ws:1", req_id=7)
    # RX with a mismatched msg_id but within the window → heuristic.
    proto._forward_to_client(_frame(0x42), direction="rx")

    rt = records[-1]["reply_to"]
    assert rt["confidence"] == "heuristic"
    assert rt["origin"] == "ws:1"
    assert rt["req_id"] == 7


def test_rx_with_no_outstanding_tx_is_unsolicited() -> None:
    proto = UartProtocol(config={"frame_spacing_ms": 0})
    records: list[dict] = []
    proto.set_on_frame(lambda raw, ts, d="rx", meta=None: records.append(meta or {}))

    proto._forward_to_client(_frame(0xC0), direction="rx")

    assert "reply_to" not in records[-1]


def test_heuristic_decays_outside_window(monkeypatch) -> None:
    proto = UartProtocol(config={"frame_spacing_ms": 0})
    records: list[dict] = []
    proto.set_on_frame(lambda raw, ts, d="rx", meta=None: records.append(meta or {}))

    # Record a TX, then force time forward beyond the heuristic window.
    proto._forward_to_client(_frame(0x41), direction="tx", origin="ws:0", req_id=1)

    real_monotonic = time.monotonic
    offset = HEURISTIC_WINDOW + 0.1
    monkeypatch.setattr(
        "blaueis.gateway.uart_protocol.time.monotonic",
        lambda: real_monotonic() + offset,
    )

    proto._forward_to_client(_frame(0x99), direction="rx")  # no match

    assert "reply_to" not in records[-1]


def test_same_msg_id_retx_overwrites_outstanding() -> None:
    """If the gateway re-TXes the same msg_id, the newer TX is what any
    subsequent RX should correlate to."""
    proto = UartProtocol(config={"frame_spacing_ms": 0})
    records: list[dict] = []
    proto.set_on_frame(lambda raw, ts, d="rx", meta=None: records.append(meta or {}))

    proto._forward_to_client(_frame(0x41), direction="tx", origin="ws:0", req_id=1)
    proto._forward_to_client(_frame(0x41), direction="tx", origin="ws:1", req_id=2)
    proto._forward_to_client(_frame(0x41), direction="rx")

    rt = records[-1]["reply_to"]
    assert rt["origin"] == "ws:1"
    assert rt["req_id"] == 2
    assert rt["confidence"] == "confirmed"


# ── queue_frame provenance ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_frame_stores_origin_and_req_id() -> None:
    proto = UartProtocol(config={"frame_spacing_ms": 0, "max_queue": 4})
    ok = await proto.queue_frame(_frame(0xC0), origin="ws:3", req_id=12)
    assert ok
    frame, origin, req_id = proto._tx_queue.get_nowait()
    assert origin == "ws:3"
    assert req_id == 12
    assert _frame_msg_id(frame) == 0xC0


@pytest.mark.asyncio
async def test_queue_frame_default_origin_when_unspecified() -> None:
    proto = UartProtocol(config={"frame_spacing_ms": 0, "max_queue": 4})
    assert await proto.queue_frame(_frame(0xB1))
    _, origin, req_id = proto._tx_queue.get_nowait()
    assert origin == "ws:unknown"
    assert req_id is None
