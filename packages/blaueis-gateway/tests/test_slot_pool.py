"""Tests for blaueis.gateway.slot_pool.SlotPool."""
from __future__ import annotations

import threading

import pytest

from blaueis.gateway.slot_pool import SlotPool, SlotPoolExhausted


def test_acquires_lowest_free() -> None:
    p = SlotPool(size=4)
    assert p.acquire() == 0
    assert p.acquire() == 1
    assert p.acquire() == 2
    assert p.in_use_count == 3


def test_release_returns_slot_to_pool() -> None:
    p = SlotPool(size=4)
    a, b, c = p.acquire(), p.acquire(), p.acquire()
    assert (a, b, c) == (0, 1, 2)
    p.release(b)
    # Lowest free is now the just-released slot 1.
    assert p.acquire() == 1


def test_reuse_after_full_cycle() -> None:
    p = SlotPool(size=2)
    s0 = p.acquire()
    s1 = p.acquire()
    p.release(s0)
    p.release(s1)
    # Pool is empty; new acquire starts from 0 again.
    assert p.acquire() == 0
    assert p.acquire() == 1


def test_exhausted_raises() -> None:
    p = SlotPool(size=2)
    p.acquire()
    p.acquire()
    with pytest.raises(SlotPoolExhausted):
        p.acquire()


def test_release_unknown_slot_is_noop() -> None:
    p = SlotPool(size=4)
    # No slots acquired yet — releasing anything should not raise and
    # should not affect the pool state.
    p.release(3)
    assert p.in_use_count == 0
    assert p.acquire() == 0


def test_snapshot_reflects_in_use() -> None:
    p = SlotPool(size=4)
    p.acquire()
    p.acquire()
    assert p.snapshot() == [0, 1]
    p.release(0)
    assert p.snapshot() == [1]


def test_rejects_zero_size() -> None:
    with pytest.raises(ValueError):
        SlotPool(size=0)


def test_thread_safe_acquire() -> None:
    p = SlotPool(size=100)
    acquired: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            s = p.acquire()
            with lock:
                acquired.append(s)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 10 threads × 10 acquires = 100; pool size is 100, so all unique.
    assert len(acquired) == 100
    assert len(set(acquired)) == 100
    assert p.in_use_count == 100


def test_reconnect_pattern_reuses_slot() -> None:
    """Typical lifecycle: client connects, disconnects, reconnects — slot
    reused. Ring timestamps distinguish the sessions, not the slot id."""
    p = SlotPool(size=8)
    s_first = p.acquire()
    p.release(s_first)
    s_second = p.acquire()
    assert s_first == s_second == 0
