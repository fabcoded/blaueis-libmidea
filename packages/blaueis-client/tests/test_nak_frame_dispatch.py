"""Wire-envelope NAK dispatch in Device._process_frame.

The AC mainboard signals that a frame was rejected at the wire-envelope
layer using ``msg_type ∈ {0x06, 0x0A}``. The body of such a frame is
*not* a status reply, even when the first byte happens to match a
status-frame tag (0xC0/0xA1/0xB0/0xB1/0xB5/0xC1). The dispatcher must
divert these frames before any body-level decode runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from blaueis.client.device import NAK_MSG_TYPES, Device
from blaueis.core.frame import build_frame


def _bare_device() -> Device:
    """Construct a Device skeleton with the minimum state ``_process_frame``
    and ``_handle_nak_frame`` touch. Bypasses ``__init__`` to avoid
    pulling in the full HvacClient / StatusDB stack."""

    dev = Device.__new__(Device)
    dev._status = {"meta": {"frame_counts": {}}, "fields": {}}
    dev._frame_observers = []
    dev._glossary = {}
    dev._db = SimpleNamespace()
    dev._b5_state = "idle"
    dev._b5_response_event = None
    dev._b5_next_frame = False
    dev._follow_me_shadow = None
    dev._initial_status_event = None
    return dev


def _wire_hex(body: bytes, msg_type: int) -> str:
    return build_frame(body, msg_type=msg_type).hex()


# ── NAK_MSG_TYPES sanity ─────────────────────────────────────────────


def test_nak_msg_types_include_0x06_and_0x0a() -> None:
    assert 0x06 in NAK_MSG_TYPES
    assert 0x0A in NAK_MSG_TYPES


# ── NAK dispatch — body[0] valid status tag, must NOT reach ingest ──


@pytest.mark.parametrize("nak_msg_type", [0x06, 0x0A])
def test_nak_frame_does_not_reach_data_path(nak_msg_type: int) -> None:
    """A NAK whose body would otherwise be classified as a status
    reply (body[0]=0xC0) must skip ``identify_frame`` and the
    asyncio.create_task ingest path."""

    dev = _bare_device()

    # Body that LOOKS like a C0 status reply: tag 0xC0 + zero padding.
    # If the dispatcher were to call identify_frame(body) on this, it
    # would route the bytes to process_data_frame as if the AC were
    # reporting fresh state — a worst-case mis-decode.
    body = bytes([0xC0]) + bytes(20)
    hex_str = _wire_hex(body, msg_type=nak_msg_type)

    observed: list[tuple[str, bytes]] = []
    dev._frame_observers.append(lambda key, b: observed.append((key, b)))

    # Patch asyncio.create_task so we can detect any accidental ingest.
    with patch("blaueis.client.device.asyncio.create_task") as fake_task:
        dev._process_frame(hex_str)

    assert fake_task.call_count == 0, "NAK frame must not be enqueued for ingest"
    # Counter incremented for this NAK kind.
    sentinel = f"nak_0x{nak_msg_type:02X}"
    assert dev._status["meta"]["frame_counts"][sentinel] == 1
    # last_nak captures the body verbatim for diagnostics.
    last = dev._status["meta"]["last_nak"]
    assert last["msg_type"] == nak_msg_type
    assert last["body_hex"] == body.hex()
    # Observers see the sentinel key, not a status-frame protocol_key.
    assert observed == [(sentinel, body)]


def test_nak_counter_accumulates_across_frames() -> None:
    """Repeated NAKs of the same type bump the per-msg_type counter."""

    dev = _bare_device()
    body = bytes([0xC0]) + bytes(20)
    hex_str = _wire_hex(body, msg_type=0x06)

    with patch("blaueis.client.device.asyncio.create_task"):
        dev._process_frame(hex_str)
        dev._process_frame(hex_str)
        dev._process_frame(hex_str)

    assert dev._status["meta"]["frame_counts"]["nak_0x06"] == 3
    # The 0x0A counter is untouched.
    assert "nak_0x0A" not in dev._status["meta"]["frame_counts"]


def test_observer_exception_does_not_stop_dispatch() -> None:
    """If a frame observer raises on the NAK fan-out, subsequent
    observers still receive the event and the counter still increments
    — same defensive contract as the data-path observer fan-out."""

    dev = _bare_device()
    body = bytes([0xC0])
    hex_str = _wire_hex(body, msg_type=0x06)

    seen: list[str] = []

    def boom(key: str, b: bytes) -> None:
        raise RuntimeError("boom")

    def good(key: str, b: bytes) -> None:
        seen.append(key)

    dev._frame_observers.extend([boom, good])

    with patch("blaueis.client.device.asyncio.create_task"):
        dev._process_frame(hex_str)

    assert seen == ["nak_0x06"]
    assert dev._status["meta"]["frame_counts"]["nak_0x06"] == 1


# ── Regression: non-NAK frame still goes to the data path ───────────


def test_normal_status_frame_still_dispatches_to_data_path() -> None:
    """A wire frame with the standard status-reply msg_type (0x03) and
    body[0]=0xC0 must still route through identify_frame /
    asyncio.create_task — i.e. the NAK gate does not regress the
    happy path."""

    dev = _bare_device()
    body = bytes([0xC0]) + bytes(20)
    hex_str = _wire_hex(body, msg_type=0x03)

    observed: list[tuple[str, bytes]] = []
    dev._frame_observers.append(lambda key, b: observed.append((key, b)))

    with patch("blaueis.client.device.asyncio.create_task") as fake_task:
        dev._process_frame(hex_str)

    # Observer saw the real protocol key, not a NAK sentinel.
    assert observed and observed[0][0] == "rsp_0xc0"
    # The data-path coroutine was scheduled.
    assert fake_task.call_count == 1
    # No NAK counter was created.
    assert "nak_0x06" not in dev._status["meta"]["frame_counts"]
    assert "nak_0x0A" not in dev._status["meta"]["frame_counts"]
    assert "last_nak" not in dev._status["meta"]
