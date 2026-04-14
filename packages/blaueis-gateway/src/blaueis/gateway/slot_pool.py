"""Slot pool — fixed small pool of client slot IDs with reuse on release.

Design ref: `blaueis-libmidea/docs/flight_recorder.md` §4.6.

Allocates the lowest-free slot number on acquire; returns it to the pool on
release. Not a monotonic counter — a reconnecting client may receive the same
slot number as a previous connection. Ring-record timestamps disambiguate.
"""
from __future__ import annotations

import threading


class SlotPoolExhausted(Exception):
    """Raised when `acquire()` is called with no free slots."""


class SlotPool:
    """Fixed-size pool of integer slot ids in `range(size)`.

    Thread-safe. `acquire()` returns the lowest free slot; `release(slot)`
    puts it back in the pool. Attempting to release a slot that is not in
    use is a no-op — callers can safely `release()` on a disconnect path
    without checking whether `acquire()` succeeded.
    """

    def __init__(self, size: int = 8) -> None:
        if size <= 0:
            raise ValueError("pool size must be positive")
        self._size = int(size)
        self._in_use: set[int] = set()
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        return self._size

    @property
    def in_use_count(self) -> int:
        with self._lock:
            return len(self._in_use)

    def acquire(self) -> int:
        """Return the lowest free slot id. Raises `SlotPoolExhausted` if full."""
        with self._lock:
            for slot in range(self._size):
                if slot not in self._in_use:
                    self._in_use.add(slot)
                    return slot
            raise SlotPoolExhausted(
                f"all {self._size} slots in use"
            )

    def release(self, slot: int) -> None:
        """Return a slot to the pool. Unknown slot ids are silently ignored."""
        with self._lock:
            self._in_use.discard(slot)

    def snapshot(self) -> list[int]:
        """Return a sorted list of currently-held slot ids."""
        with self._lock:
            return sorted(self._in_use)
