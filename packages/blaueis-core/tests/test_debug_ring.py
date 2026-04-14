"""Tests for blaueis.core.debug_ring.DebugRing."""
from __future__ import annotations

import json
import logging
import threading

import pytest

from blaueis.core.debug_ring import DebugRing, log_event


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def ring() -> DebugRing:
    return DebugRing(size_bytes=4096)


@pytest.fixture
def logger(ring: DebugRing) -> logging.Logger:
    name = f"test.debug_ring.{id(ring):x}"
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.addHandler(ring)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    return lg


# ── basic append / record shape ──────────────────────────────────────────

def test_append_single_record(ring: DebugRing, logger: logging.Logger) -> None:
    logger.info("hello")
    snap = ring.snapshot()
    assert len(snap) == 1
    rec = json.loads(snap[0])
    assert rec["msg"] == "hello"
    assert rec["lvl"] == "INFO"
    assert rec["logger"] == logger.name
    assert rec["ts"] > 0
    assert rec["mono"] > 0


def test_byte_count_tracks_content(ring: DebugRing, logger: logging.Logger) -> None:
    logger.info("x")
    b = ring.byte_count
    assert b > 0
    logger.info("y")
    assert ring.byte_count > b
    assert ring.record_count == 2


def test_record_ends_with_newline(ring: DebugRing, logger: logging.Logger) -> None:
    logger.info("one")
    [raw] = ring.snapshot()
    assert raw.endswith(b"\n")


# ── schema: known fields pass through ────────────────────────────────────

def test_known_fields_propagate(ring: DebugRing, logger: logging.Logger) -> None:
    log_event(
        logger, logging.DEBUG, "uart_rx",
        port="uart", peer="ac", msg_id=0x41,
        len=32, hex="aa 20 ac 00 00 00",
        reply_to={"req_id": 7, "origin": "ws:2", "confidence": "confirmed"},
        ctx={"free_form": 123},
    )
    rec = json.loads(ring.snapshot()[0])
    assert rec["event"] == "uart_rx"
    assert rec["port"] == "uart"
    assert rec["peer"] == "ac"
    assert rec["msg_id"] == 0x41
    assert rec["len"] == 32
    assert rec["hex"] == "aa 20 ac 00 00 00"
    assert rec["reply_to"] == {
        "req_id": 7, "origin": "ws:2", "confidence": "confirmed"
    }
    assert rec["ctx"] == {"free_form": 123}


def test_unknown_extra_fields_are_dropped(ring: DebugRing, logger: logging.Logger) -> None:
    logger.info("foo", extra={"event": "loop", "random_unknown": "drop_me"})
    rec = json.loads(ring.snapshot()[0])
    assert rec["event"] == "loop"
    assert "random_unknown" not in rec


def test_log_event_rejects_reserved_attr_collision(logger: logging.Logger) -> None:
    with pytest.raises(ValueError):
        log_event(logger, logging.INFO, "loop", name="not_allowed")


def test_tx_seq_and_slot_pass_through(ring: DebugRing, logger: logging.Logger) -> None:
    log_event(logger, logging.DEBUG, "uart_tx",
              tx_seq=4711, sid=2, origin="ws:2", req_id=99)
    rec = json.loads(ring.snapshot()[0])
    assert rec["tx_seq"] == 4711
    assert rec["sid"] == 2
    assert rec["origin"] == "ws:2"
    assert rec["req_id"] == 99


# ── byte-sized eviction ───────────────────────────────────────────────────

def test_evicts_oldest_when_over_cap() -> None:
    r = DebugRing(size_bytes=512)
    lg = logging.getLogger("test.debug_ring.evict")
    lg.handlers.clear()
    lg.addHandler(r)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False

    # Each record is ~80+ bytes after JSON overhead; write many.
    for i in range(200):
        lg.info("payload-%03d", i)

    # Size stays under the cap.
    assert r.byte_count <= 512
    # Oldest have been evicted — earliest remaining record is not i=0.
    records = r.dump_records()
    assert records, "ring should not be empty"
    first_msg = records[0]["msg"]
    assert first_msg != "payload-000"
    # Latest retained record is the last one we wrote.
    assert records[-1]["msg"] == "payload-199"


def test_single_outsized_record_kept() -> None:
    r = DebugRing(size_bytes=64)
    lg = logging.getLogger("test.debug_ring.outsized")
    lg.handlers.clear()
    lg.addHandler(r)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False

    lg.info("x" * 500)  # larger than cap
    # Ring keeps the single oversize record rather than emptying itself.
    assert r.record_count == 1
    # But a subsequent small record displaces it as normal.
    lg.info("small")
    assert r.record_count == 1
    assert json.loads(r.snapshot()[0])["msg"] == "small"


# ── clear ─────────────────────────────────────────────────────────────────

def test_clear(ring: DebugRing, logger: logging.Logger) -> None:
    logger.info("a")
    logger.info("b")
    assert ring.record_count == 2
    ring.clear()
    assert ring.record_count == 0
    assert ring.byte_count == 0
    assert ring.snapshot() == []


# ── propagation off ───────────────────────────────────────────────────────

def test_does_not_propagate_to_root(ring: DebugRing, logger: logging.Logger, caplog) -> None:
    # With logger.propagate=False (set in fixture) nothing should reach caplog,
    # which attaches at the root.
    with caplog.at_level(logging.DEBUG):
        logger.info("stays_local")
    assert ring.record_count == 1
    assert "stays_local" not in caplog.text


# ── concurrency smoke test ────────────────────────────────────────────────

def test_concurrent_appends() -> None:
    r = DebugRing(size_bytes=1_000_000)
    lg = logging.getLogger("test.debug_ring.concurrent")
    lg.handlers.clear()
    lg.addHandler(r)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False

    def worker(tid: int) -> None:
        for i in range(200):
            lg.info("t%d-%d", tid, i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every record decodes — no torn writes, no partial lines.
    records = r.dump_records()
    assert len(records) == 8 * 200
    for rec in records:
        assert rec["lvl"] == "INFO"
        assert rec["msg"].startswith("t")


# ── exc_info capture ─────────────────────────────────────────────────────

def test_exception_captured(ring: DebugRing, logger: logging.Logger) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    rec = json.loads(ring.snapshot()[0])
    assert rec["msg"] == "failed"
    assert "exc" in rec
    assert "ValueError" in rec["exc"]
    assert "boom" in rec["exc"]


# ── byte dump round-trip ──────────────────────────────────────────────────

def test_dump_jsonl_round_trip(ring: DebugRing, logger: logging.Logger) -> None:
    logger.info("one")
    logger.info("two")
    text = ring.dump_jsonl()
    lines = text.strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["msg"] == "one"
    assert json.loads(lines[1])["msg"] == "two"


# ── size validation ───────────────────────────────────────────────────────

def test_rejects_zero_size() -> None:
    with pytest.raises(ValueError):
        DebugRing(size_bytes=0)


def test_rejects_negative_size() -> None:
    with pytest.raises(ValueError):
        DebugRing(size_bytes=-1)
