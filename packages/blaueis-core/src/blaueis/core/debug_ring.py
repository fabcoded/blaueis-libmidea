"""DebugRing — in-memory rolling debug buffer (flight recorder).

Design ref: `blaueis-libmidea/docs/flight_recorder.md`.

A `logging.Handler` subclass backed by a byte-sized `collections.deque`. Every
record is serialised to JSON-encoded bytes, appended, and the oldest entries
are evicted when the total byte count exceeds the configured cap. Never writes
to disk, never propagates to parent loggers unless the caller leaves
`Logger.propagate = True` (by design: attach to a named logger with
`propagate = False` so records reach the ring only).

Records use the unified schema from flight_recorder.md §4.3 — a baseline set
of fields (ts, mono, lvl, logger, msg) plus any of the known provenance
fields attached via `extra=` on the log call.

Usage:

    import logging
    from blaueis.core.debug_ring import DebugRing, log_event

    ring = DebugRing(size_bytes=5 * 1024 * 1024)
    log = logging.getLogger("blaueis.gateway.uart")
    log.addHandler(ring)
    log.setLevel(logging.DEBUG)
    log.propagate = False

    log_event(log, logging.DEBUG, "uart_rx",
              port="uart", peer="ac", msg_id=0x41,
              len=32, hex="aa 20 ac ...")

    dump = ring.dump_jsonl()
"""
from __future__ import annotations

import collections
import json
import logging
import time
from typing import Any, Iterable

# ── Schema ────────────────────────────────────────────────────────────────

# Provenance / structured fields that the ring record schema understands.
# Anything not in this list (and not a standard LogRecord attribute) is
# dropped — callers should put ad-hoc fields in `ctx={...}`.
_KNOWN_FIELDS: tuple[str, ...] = (
    "event",       # verb-noun: uart_rx, uart_tx, ws_in, ws_out, ws_connect,
                   # ws_disconnect, state, loop, err, log
    "port",        # uart | ws | internal
    "peer",        # ac | gw:<role> | ws:<slot>
    "origin",      # who caused this transmission (same vocabulary as peer)
    "sid",         # client slot id
    "req_id",      # monotonic per-client request id (echoes the WS `ref` field)
    "msg_id",      # Midea protocol sequence byte
    "tx_seq",      # gateway-local monotonic transmit counter
    "hex",         # hex dump of a frame
    "len",         # frame byte length
    "reply_to",    # {req_id, origin, confidence} — advisory, may be null
    "ctx",         # free-form dict for fields outside this schema
)


# ── Handler ───────────────────────────────────────────────────────────────

class DebugRing(logging.Handler):
    """Byte-sized circular buffer implemented as a logging handler.

    Thread-safety: `logging.Handler.handle()` acquires `self.lock` around
    `emit`, which is sufficient for the append path. Readers call
    `snapshot()`, which copies under the same lock.

    Not a `MemoryHandler`: we do not batch-flush to a target handler. Records
    stay in memory until they are evicted by age (FIFO, byte-capped) or the
    ring is cleared / snapshotted.
    """

    def __init__(
        self,
        size_bytes: int = 5 * 1024 * 1024,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level=level)
        if size_bytes <= 0:
            raise ValueError("size_bytes must be positive")
        self._size_bytes = int(size_bytes)
        self._buf: collections.deque[bytes] = collections.deque()
        self._bytes = 0

    # ── capacity / introspection ──────────────────────────────────────

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    @property
    def byte_count(self) -> int:
        return self._bytes

    @property
    def record_count(self) -> int:
        return len(self._buf)

    # ── emit ──────────────────────────────────────────────────────────

    def emit(self, record: logging.LogRecord) -> None:
        try:
            serialised = self._serialise(record)
        except Exception:
            self.handleError(record)
            return

        self._buf.append(serialised)
        self._bytes += len(serialised)

        # Evict from the left until we fit. Keep at least one record so a
        # single outsized record does not produce an empty ring on next append.
        while self._bytes > self._size_bytes and len(self._buf) > 1:
            old = self._buf.popleft()
            self._bytes -= len(old)

    # ── serialisation ─────────────────────────────────────────────────

    def _serialise(self, record: logging.LogRecord) -> bytes:
        payload: dict[str, Any] = {
            "ts": round(record.created, 6),
            "mono": round(time.monotonic(), 6),
            "lvl": record.levelname,
            "logger": record.name,
        }

        for field in _KNOWN_FIELDS:
            if field in record.__dict__:
                payload[field] = record.__dict__[field]

        # Render the message lazily — this is the only cost we pay on emit;
        # tracebacks / heavy formatters should be avoided per docs §3.3.
        try:
            msg = record.getMessage()
        except Exception:
            msg = record.msg if isinstance(record.msg, str) else repr(record.msg)
        if msg:
            payload["msg"] = msg

        if record.exc_info:
            # Format once, stash the text; no repeat formatting at dump time.
            payload["exc"] = self.format(
                logging.LogRecord(
                    record.name, record.levelno, record.pathname, record.lineno,
                    record.msg, record.args, record.exc_info, record.funcName,
                )
            )

        encoded = json.dumps(
            payload, default=_json_default, separators=(",", ":"), ensure_ascii=False
        )
        return (encoded + "\n").encode("utf-8")

    # ── snapshot / dump / clear ───────────────────────────────────────

    def snapshot(self) -> list[bytes]:
        """Return a shallow copy of the ring, oldest → newest."""
        with self.lock:  # type: ignore[union-attr]
            return list(self._buf)

    def dump_jsonl(self) -> str:
        """Return the ring contents as one JSON object per line (newline-delimited)."""
        return b"".join(self.snapshot()).decode("utf-8")

    def dump_records(self) -> list[dict[str, Any]]:
        """Return the ring contents as a list of decoded dicts. Allocates —
        useful for diagnostics bundles, not for hot paths."""
        return [json.loads(b) for b in self.snapshot()]

    def clear(self) -> None:
        with self.lock:  # type: ignore[union-attr]
            self._buf.clear()
            self._bytes = 0


# ── Helper ────────────────────────────────────────────────────────────────

def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    msg: str = "",
    **fields: Any,
) -> None:
    """Log a structured event into the ring.

    Equivalent to `logger.log(level, msg, extra={"event": event, **fields})`
    but rejects field names that collide with LogRecord internals early so a
    typo does not get silently dropped by logging's own guard.
    """
    reserved = _RESERVED_LOGRECORD_ATTRS
    extra = {"event": event}
    for k, v in fields.items():
        if k in reserved:
            raise ValueError(
                f"field name {k!r} collides with a LogRecord attribute"
            )
        extra[k] = v
    logger.log(level, msg, extra=extra)


# LogRecord attributes as of CPython 3.11 — passing any of these in `extra`
# raises KeyError inside logging.Logger.makeRecord, so we guard up front.
_RESERVED_LOGRECORD_ATTRS: frozenset[str] = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


# ── JSON default ──────────────────────────────────────────────────────────

def _json_default(obj: Any) -> Any:
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex(" ")
    if isinstance(obj, Iterable) and not isinstance(obj, (str, dict, list, tuple)):
        return list(obj)
    return str(obj)
