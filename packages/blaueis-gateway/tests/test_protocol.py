#!/usr/bin/env python3
"""Tests for uart_protocol.py — dongle state machine with mock UART.

Simulates AC responses to verify state transitions, frame forwarding,
and correct handling of all MSG types.

Usage:
    python test_protocol.py
"""

import asyncio
import sys
from pathlib import Path


from blaueis.core.frame import build_frame, parse_frame
from blaueis.gateway.uart_protocol import DISCOVER, RUNNING, UartProtocol

# ── Mock UART streams ────────────────────────────────────────────────────


class MockReader:
    """Simulates UART RX with pre-loaded response frames."""

    def __init__(self):
        self._buffer = bytearray()
        self._event = asyncio.Event()

    def feed(self, data: bytes):
        self._buffer.extend(data)
        self._event.set()

    async def read(self, n: int) -> bytes:
        while len(self._buffer) < n:
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=2.0)
            except TimeoutError:
                return b""
        result = bytes(self._buffer[:n])
        del self._buffer[:n]
        return result

    async def readexactly(self, n: int) -> bytes:
        """Match asyncio.StreamReader.readexactly — wait for exactly n bytes."""
        while len(self._buffer) < n:
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=2.0)
            except TimeoutError:
                raise asyncio.IncompleteReadError(bytes(self._buffer), n) from None
        result = bytes(self._buffer[:n])
        del self._buffer[:n]
        return result


class MockWriter:
    """Captures UART TX frames for inspection."""

    def __init__(self):
        self.sent: list[bytes] = []
        self._buf = bytearray()

    def write(self, data: bytes):
        self._buf.extend(data)
        while len(self._buf) >= 2 and self._buf[0] == 0xAA:
            frame_len = self._buf[1] + 1
            if len(self._buf) >= frame_len:
                self.sent.append(bytes(self._buf[:frame_len]))
                del self._buf[:frame_len]
            else:
                break

    async def drain(self):
        pass


# ── AC simulator frames ──────────────────────────────────────────────────


def build_sn_response(sn: str = "TEST_SN_12345") -> bytes:
    body = sn.encode("ascii").ljust(32, b"\x00")
    return build_frame(body, msg_type=0x07, appliance=0xAC, proto=0x03, sub=0x00)


def build_model_response(model: int = 0xACAC) -> bytes:
    body = bytearray(20)
    body[2] = model & 0xFF
    body[3] = (model >> 8) & 0xFF
    return build_frame(bytes(body), msg_type=0xA0, appliance=0xAC, proto=0x03)


def build_c0_response() -> bytes:
    body = bytes.fromhex("C001896600000000000000623900000000000000000000000000000000")
    return build_frame(body, msg_type=0x03, appliance=0xAC, proto=0x03)


def build_0x63_query() -> bytes:
    return build_frame(bytes(20), msg_type=0x63, appliance=0xAC, proto=0x03)


def build_0x82_rehash() -> bytes:
    return build_frame(bytes([0x00]), msg_type=0x82, appliance=0xAC, proto=0x03)


def build_0x13_version_query() -> bytes:
    return build_frame(bytes([0x00]), msg_type=0x13, appliance=0xAC, proto=0x03)


def build_restart_trigger() -> bytes:
    return build_frame(bytes([0x80, 0x40]), msg_type=0x0F, appliance=0xAC, proto=0x03)


# ── Tests ─────────────────────────────────────────────────────────────────


async def run_tests():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
        else:
            failed += 1
            print(f"  [FAIL] {name}: {detail}")

    # ── Test 1: Full handshake — SN response forwarded to client ─
    proto = UartProtocol(config={"frame_spacing_ms": 0, "fake_ip": "10.0.0.1"})
    reader = MockReader()
    writer = MockWriter()
    forwarded = []
    proto.set_on_frame(lambda raw, ts, d="rx": forwarded.append((raw, d)))

    check("initial state is DISCOVER", proto.state == DISCOVER)

    async def run_discover():
        await proto._do_discover(reader, writer)

    async def feed_sn():
        await asyncio.sleep(0.05)
        reader.feed(build_sn_response("XSAVE_BLUE_Q11"))

    await asyncio.gather(run_discover(), feed_sn())

    check("after discover: state=MODEL", proto.state == "model")
    check("after discover: appliance=0xAC", proto.appliance == 0xAC)
    check("after discover: proto=3", proto.proto == 0x03)
    check("after discover: SN starts with XSAVE", proto.serial_number.startswith("XSAVE"))
    check("discover: SN response forwarded to client", len(forwarded) >= 1)
    check("discover sent 0x07 frame", len(writer.sent) >= 1)
    first_sent = parse_frame(writer.sent[0])
    check("discover sent appliance=0xFF", first_sent["appliance"] == 0xFF)

    # ── Test 2: MODEL — model response forwarded to client ───────
    reader2 = MockReader()
    writer2 = MockWriter()
    forwarded.clear()

    async def run_model():
        await proto._do_model(reader2, writer2)

    async def feed_model():
        await asyncio.sleep(0.05)
        reader2.feed(build_model_response(0xACAC))

    await asyncio.gather(run_model(), feed_model())

    check("after model: state=ANNOUNCE", proto.state == "announce")
    check("after model: model=0xACAC", proto.model == 0xACAC)
    check("model: response forwarded to client", len(forwarded) >= 1)

    # ── Test 3: ANNOUNCE ─────────────────────────────────────────
    writer3 = MockWriter()
    await proto._do_announce(writer3)

    check("after announce: state=RUNNING", proto.state == RUNNING)
    announce_parsed = parse_frame(writer3.sent[0])
    check("announce msg_type=0x0D", announce_parsed["msg_type"] == 0x0D)
    announce_body = announce_parsed["body"]
    check("announce IP byte[4]=1 (LE)", announce_body[4] == 1)
    check("announce IP byte[7]=10 (LE)", announce_body[7] == 10)

    # ── Test 4: RUNNING — 0x63 responded AND forwarded ───────────
    reader4 = MockReader()
    writer4 = MockWriter()
    forwarded.clear()

    reader4.feed(build_0x63_query())
    await proto._do_running(reader4, writer4)

    check("0x63 → responded", len(writer4.sent) == 1)
    ns_parsed = parse_frame(writer4.sent[0])
    check("0x63 response msg_type=0x63", ns_parsed["msg_type"] == 0x63)
    check("0x63 response connected=1", ns_parsed["body"][0] == 0x01)
    check("0x63 ALSO forwarded to client", len(forwarded) == 1)

    # ── Test 5: RUNNING — C0 forwarded, no response ─────────────
    reader5 = MockReader()
    writer5 = MockWriter()
    forwarded.clear()

    reader5.feed(build_c0_response())
    await proto._do_running(reader5, writer5)

    check("C0 forwarded to client", len(forwarded) == 1)
    check("no response sent for C0", len(writer5.sent) == 0)

    # ── Test 6: RUNNING — 0x82 re-handshake, forwarded + ack ────
    reader6 = MockReader()
    writer6 = MockWriter()
    forwarded.clear()

    reader6.feed(build_0x82_rehash())
    await proto._do_running(reader6, writer6)

    check("0x82 → state back to DISCOVER", proto.state == DISCOVER)
    check("0x82 → ACK sent", len(writer6.sent) == 1)
    check("0x82 forwarded to client", len(forwarded) == 1)

    # ── Test 7: RUNNING — 0x13 firmware version query → respond ──
    proto2 = UartProtocol(config={"frame_spacing_ms": 0})
    proto2.state = RUNNING
    proto2.appliance = 0xAC
    proto2.silence_timer = asyncio.get_event_loop().time()
    forwarded2 = []
    proto2.set_on_frame(lambda raw, ts, d="rx": forwarded2.append((raw, d)))

    reader7 = MockReader()
    writer7 = MockWriter()
    reader7.feed(build_0x13_version_query())
    await proto2._do_running(reader7, writer7)

    check("0x13 → version response sent", len(writer7.sent) == 1)
    check("0x13 forwarded to client", len(forwarded2) == 1)

    # ── Test 8: RUNNING — 0x0F restart trigger ───────────────────
    proto3 = UartProtocol(config={"frame_spacing_ms": 0})
    proto3.state = RUNNING
    proto3.appliance = 0xAC
    proto3.silence_timer = asyncio.get_event_loop().time()

    reader8 = MockReader()
    writer8 = MockWriter()
    reader8.feed(build_restart_trigger())
    await proto3._do_running(reader8, writer8)

    check("0x0F restart trigger → DISCOVER", proto3.state == DISCOVER)

    # ── Test 9: Silence timeout ──────────────────────────────────
    proto4 = UartProtocol(config={"frame_spacing_ms": 0})
    proto4.state = RUNNING
    proto4.silence_timer = 0  # long ago

    reader9 = MockReader()
    writer9 = MockWriter()
    await proto4._do_running(reader9, writer9)

    check("silence timeout → DISCOVER", proto4.state == DISCOVER)

    # ── Test 10: TX queue ────────────────────────────────────────
    proto5 = UartProtocol(config={"max_queue": 2, "frame_spacing_ms": 0})
    test_frame = build_frame(bytes([0x41]), msg_type=0x03, appliance=0xAC)

    ok1 = await proto5.queue_frame(test_frame)
    ok2 = await proto5.queue_frame(test_frame)
    ok3 = await proto5.queue_frame(test_frame)

    check("queue accepts frame 1", ok1)
    check("queue accepts frame 2", ok2)
    check("queue rejects frame 3 (full)", not ok3)

    # ── Test 11: DISCOVER with no response → tries 0x07 + 0x65 ──
    proto6 = UartProtocol(config={"frame_spacing_ms": 0})
    reader11 = MockReader()
    writer11 = MockWriter()
    await proto6._do_discover(reader11, writer11)

    check("discover no response: stays DISCOVER", proto6.state == DISCOVER)
    msg_types_sent = [parse_frame(f)["msg_type"] for f in writer11.sent]
    check("discover tried 0x07", 0x07 in msg_types_sent)
    check("discover tried 0x65", 0x65 in msg_types_sent)

    # ── Test 12: TX mirror — gateway-initiated frames ────────────
    proto7 = UartProtocol(config={"frame_spacing_ms": 0, "mirror_tx_gateway": True})
    proto7.state = RUNNING
    proto7.appliance = 0xAC
    proto7.silence_timer = asyncio.get_event_loop().time()
    mirrored = []
    proto7.set_on_frame(lambda raw, ts, d="rx": mirrored.append((raw, d)))

    reader12 = MockReader()
    writer12 = MockWriter()
    # Feed 0x63 query → gateway responds → response should be mirrored as "tx"
    reader12.feed(build_0x63_query())
    await proto7._do_running(reader12, writer12)

    rx_frames = [m for m in mirrored if m[1] == "rx"]
    tx_frames = [m for m in mirrored if m[1] == "tx"]
    check("mirror: 0x63 query received as rx", len(rx_frames) == 1)
    check("mirror: 0x63 response mirrored as tx", len(tx_frames) == 1)

    # ── Test 13: TX mirror off — no tx frames forwarded ──────────
    proto8 = UartProtocol(config={"frame_spacing_ms": 0, "mirror_tx_gateway": False})
    proto8.state = RUNNING
    proto8.appliance = 0xAC
    proto8.silence_timer = asyncio.get_event_loop().time()
    mirrored2 = []
    proto8.set_on_frame(lambda raw, ts, d="rx": mirrored2.append((raw, d)))

    reader13 = MockReader()
    writer13 = MockWriter()
    reader13.feed(build_0x63_query())
    await proto8._do_running(reader13, writer13)

    tx_frames2 = [m for m in mirrored2 if m[1] == "tx"]
    check("mirror off: no tx frames", len(tx_frames2) == 0)
    check("mirror off: rx still forwarded", len(mirrored2) == 1)

    # ── Summary ──────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


def main():
    return asyncio.run(run_tests())


if __name__ == "__main__":
    sys.exit(main())
