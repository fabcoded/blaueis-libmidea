# Flight Recorder — Rolling In-Memory Debug Buffer

> **Status: Design, not yet implemented.** This document specifies the mechanism
> so it is discoverable and not reinvented. Numbers marked **[Hypothesis]** are
> from desktop-scale benchmarks extrapolated to Pi; confirm on target before
> relying on them. When built, code lives in `blaueis-core` (handler), wired
> from `blaueis-gateway` (server-side) and `blaueis-ha-midea` (HA-side).

Cross-refs: `blaueis-gateway/src/blaueis/gateway/uart_protocol.py` (`_forward_to_client`, `set_on_frame`) · `blaueis-gateway/src/blaueis/gateway/server.py:320-337` (existing `frame` WS broadcast, dir-tagged) · HA diagnostics platform (`async_get_config_entry_diagnostics`).

---

## 1. Purpose

Capture every UART TX/RX frame, loop tick, state transition, and error on both sides (gateway + HA integration) at VERBOSE/DEBUG level, keep the last **~5 MB** in RAM, **never** write to journald / `homeassistant.log` unless the user explicitly dumps it. One-click bundle for bug reports via HA's "Download Diagnostics".

Replaces the alternative of "set logger to DEBUG system-wide", which floods journald, triggers its rate limiter (`RateLimitBurst=10000/30s` can silently drop frames), accelerates SD-card wear, and pollutes other integrations' logs.

### 1.1 Stateless processing invariant — **load-bearing**

Every valid frame received from the AC is fully processed into the status DB, **regardless** of whether it matches a pending request, which client (if any) prompted it, or whether its contents agree with the last command we sent. The gateway does not retry on disagreement; the AC's reply is authoritative. FollowMe is the one continuous-loop exception — stateless at the frame level, but requires a periodic TX cadence; it is logged with `origin: "gw:followme"` for traceability.

**Consequence for this mechanism:** provenance fields (`origin`, `req_id`, `reply_to`, client slot id) are **diagnostic metadata only**. They are recorded in the ring and optionally delivered over WS, but they never gate processing, never filter frames, and never cause a drop. We do not know that all device variants correlate requests/replies identically — filtering by provenance would silently hide data from a quirky unit.

This sentence exists to stop a future contributor from "helpfully" adding retry / reconcile / filter-by-origin logic.

---

## 2. When it pays off — and when it does not

| Scenario | Flight recorder | Global DEBUG |
|---|---|---|
| Intermittent disconnect / once-a-day bug | **Best** — ring holds the last N min on failure | Needs days of log retention |
| Post-mortem after crash / assert / reconnect | **Best** — dump ring from shutdown hook | Log may be rotated away |
| Correlating HA ↔ gateway timings | **Best** — sync timestamps, dump both | Two log streams, clock skew |
| Timing-sensitive UART debugging | **Best** — no disk I/O jitter | `fsync` perturbs the thing being measured |
| Deterministic bug reproducible in unit test | Overkill | Fine |
| Slow drift over hours / days | **Insufficient** — ring window too short | Needs on-disk rotation |
| Bug in uninstrumented code path | **Useless** — only shows what was `log.*`-ed | Same limitation |
| "Why is it slow?" | Wrong tool | Wrong tool (use a profiler) |

Rule of thumb: if the failure window is shorter than the ring window and the code path is instrumented, the recorder wins decisively. Otherwise reach for the usual tools.

---

## 3. Cost — CPU, RAM, wire

All numbers **[Hypothesis]** unless noted; orders-of-magnitude only. Pi Zero 2 is the worst-case target.

### 3.1 Per-record CPU

| Path | x86 desktop | Pi 4 (est.) | Pi Zero 2 (est.) |
|---|---|---|---|
| `isEnabledFor(DEBUG)` false (gated-out) | ~0.1 µs | ~0.3 µs | ~0.5 µs |
| Deque-only handler (format + append) | ~5–15 µs | ~15–50 µs | ~30–100 µs |
| StreamHandler → journald | ~50–200 µs | ~100–400 µs | ~500–1000 µs |

At the observed ~10 Hz UART frame rate, even worst-case Pi Zero 2 cost is **<0.1% of one core** for the ring path. **[Confirmed]** at design level: deque-only is 1-2 orders of magnitude cheaper than journald emit.

**Avoid in hot path**: `%(funcName)s`, `%(lineno)d`, `findCaller` — these walk the stack frame. Set `logging.logThreads = logging.logProcesses = logging.logMultiprocessing = False` at startup.

### 3.2 RAM budget

| Storage shape | ~Bytes / record | 5 MB holds | Notes |
|---|---|---|---|
| Raw `LogRecord` object | 400–800 | ~10 k | Pins `args` graph → unbounded tail risk. **Do not use.** |
| Formatted `str` | 130–170 (for 80–120-char line) | ~35 k | Fine. |
| Pre-serialised JSON `bytes` | 150 (for 120-char line) | **~35 k** | **Preferred.** Avoids re-encode on dump; no Unicode-kind surprises. |

At 10 Hz UART + ~5 Hz loop ticks = ~15 records/s → **5 MB ≈ 35 min of history** **[Hypothesis]**. That is the *design-limiting window*, not RAM.

### 3.3 Concurrency

- `collections.deque(maxlen=N).append` is atomic under the GIL — no extra lock needed for append.
- `logging.Handler.handle()` already takes `self.lock` around `emit` — sufficient.
- Async caveat: if `emit` runs on the HA event loop, formatting must be trivial. **Do not** render tracebacks inline for every frame; defer heavy formatting to dump time.
- Free-threaded Python ≥3.13 (`--disable-gil`): deque iteration is not atomic (cpython#112050). Iterate under the handler lock at dump time. Not a concern for current HA / gateway builds.

### 3.4 SD-card wear angle

10 frames/s → ~864 k journald entries/day → ~260 MB/day logical, flash-level amplified to **~200–500 MB/day** **[Hypothesis]**. On an industrial A1/A2 card (3-10 TBW) this is decades of endurance, but real SD-death on Pis is dominated by power-loss corruption and cheap cards, not byte totals. Ring buffer sidesteps the question entirely.

---

## 4. Wire and API shape

### 4.1 Gateway → subscribers (existing, reused)

The `"type":"frame"` WS message already carries `"dir":"rx"|"tx"` (server.py:322). TX mirroring is gated by `mirror_tx_gateway` (handshake) and `mirror_tx_all` (normal) in `uart_protocol.py`. **The existing seam is reused; no new frame type is introduced.**

Change: per-subscriber filter on **event kind only**, never on provenance (§1.1). On connect, client may send:

```json
{"type":"subscribe","include":["rx","tx","ignored"],"annotate":["origin","reply_to"]}
```

- `include` — which event kinds to deliver (`rx`, `tx`, `ignored`). Default (no subscribe) = `["rx"]` non-ignored, preserving current consumer semantics.
- `annotate` — which provenance fields to attach to delivered frames. Default empty. Debug consumer opts in.

**No `only_mine` / origin filter.** Rationale: §1.1 stateless invariant — a client must always see every frame that reached it, because the status DB is authoritative and device-variant behaviour is not assumed uniform.

### 4.2 Internal tap — always on

Currently `set_on_frame` is attached only while ≥1 WS client is connected (server.py:327/337). **Change for ring buffer**: the `DebugRing` subscribes to the same internal hook unconditionally, so the recorder captures even when no WS client is listening. WS broadcast remains client-gated.

### 4.3 Ring record schema (both sides, identical)

```json
{
  "ts": 1712000000.123,
  "mono": 48123.456,
  "tx_seq": 4711,
  "lvl": "DEBUG",
  "logger": "uart_protocol",
  "event": "uart_rx",
  "port": "uart",
  "peer": "ac",
  "origin": "ws:2",
  "sid": 2,
  "req_id": 1234,
  "msg_id": 47,
  "len": 32,
  "hex": "aa 20 ...",
  "reply_to": {"req_id": 1234, "origin": "ws:2", "confidence": "confirmed"},
  "msg": "...",
  "ctx": {"msg_type": "0xc0"}
}
```

Field roles:

| Field | Meaning | Present on |
|---|---|---|
| `event` | verb-noun: `uart_rx`, `uart_tx`, `ws_in`, `ws_out`, `ws_connect`, `ws_disconnect`, `loop`, `state`, `err`, `log` | all |
| `port` | physical/logical link: `uart`, `ws`, `internal` | all (explicit for greppability) |
| `peer` | the other end: `ac`, `gw:handshake`, `gw:polling`, `gw:followme`, `ws:<slot>` | uart_*, ws_* |
| `origin` | who caused this transmission (closed vocabulary, same values as `peer` for gw/ws sources) | _tx, _out |
| `sid` | client slot id (see §4.6) | ws_*, plus uart_tx when ws-originated |
| `req_id` | client-assigned monotonic ref (reuses existing `"ref"` wire field) | ws_in and any uart_tx / uart_rx it causes |
| `msg_id` | Midea-level sequence byte extracted from the frame | uart_*, if extractable |
| `tx_seq` | gateway-local monotonic transmit counter — disambiguates ordering when two records share a timestamp | uart_tx |
| `reply_to` | best-effort correlation back to a stimulus TX; `null` for unsolicited / broadcast / status pushes | uart_rx |
| `reply_to.confidence` | `confirmed` (msg_id match) \| `heuristic` (time-window nearest) \| `unknown` (no match) | when reply_to ≠ null |

Closed `origin` vocabulary: `ac`, `gw:handshake`, `gw:polling`, `gw:followme`, `gw:keepalive`, `ws:<slot>`. Any new gateway-internal TX source must be added here explicitly so dumps have a complete enumeration.

### 4.4 Retrieval

| Side | Mechanism | Trigger |
|---|---|---|
| Gateway | WS request `{"type":"debug_dump"}` | Pull-on-demand from any authenticated client |
| HA integration | `diagnostics.py` → `async_get_config_entry_diagnostics` | "Download Diagnostics" button in UI |
| Combined bundle | HA diagnostics pulls gateway ring via WS, merges with HA ring by `ts` | One click |

Redact via `homeassistant.components.diagnostics.async_redact_data` before returning (AES keys, tokens, serials).

### 4.5 Provenance & correlation — end-to-end tracing

Two separate IDs, two separate purposes:

- **`req_id`** — monotonic uint stamped by the *HA client* on every outgoing command (reuses the existing `ref` wire field, already carried in `{"type":"frame","hex":...,"ref":N}`). Uniquely identifies an HA→gateway command and all ring records derived from it. Grep `req_id=1234` across HA ring + gateway ring to see the full life of one command.
- **`msg_id`** — Midea protocol-level sequence byte inside the frame itself. Used by the gateway to correlate an `uart_rx` back to the `uart_tx` that likely prompted it.

Correlation layers, applied in order:

1. **Protocol-level match** — `msg_id` on RX matches an entry in the gateway's outstanding-TX map. `reply_to.confidence = "confirmed"`. **[Unknown]** whether the AC echoes `msg_id` reliably across all message types; must be verified against existing Session captures before this layer can be relied on. Where it fails, fall through.
2. **Heuristic nearest-TX** — no msg_id match, but there is exactly one un-correlated TX within a time window (e.g. 500 ms). `reply_to.confidence = "heuristic"`.
3. **Unsolicited** — RX with no plausible preceding TX (status pushes, FollowMe echoes, device-initiated frames). `reply_to = null`.

All three paths **process the frame identically** (§1.1). Correlation is annotation for human readers of the dump, nothing more.

### 4.6 Client slot identity

Gateway manages a **small fixed pool of slot ids** (design default **8**). Not a monotonic counter — slots are reused as clients come and go. Ring timestamps disambiguate which session a `ws:2` record belongs to.

Lifecycle:

| Event | Gateway action | Wire |
|---|---|---|
| Client connects | Allocate lowest free slot id; record in ring as `ws_connect` with `sid`, peer addr, auth status | Send `{"type":"hello","sid":N,"server_time":...}` as first frame after auth |
| Client sends frame | Tag internal record with that socket's `sid`; if frame includes `sid` field, see §9 TODO for verification | `{"type":"frame","hex":"...","ref":N,"sid":2}` |
| Client disconnects | Free slot; record `ws_disconnect` with reason (clean/peer_reset/timeout) | — |
| Pool exhausted | Reject new connection with explicit error | `{"type":"error","code":"slot_pool_full"}` |

**HA-side tracking** (what the integration stores per config entry, in memory only, not persisted):

```python
@dataclass
class GatewaySession:
    connected_at: float         # monotonic time of WS open
    connected_wall: float       # wall-clock time.time() of WS open
    sid: int                    # slot assigned by gateway's "hello"
    server_time_at_connect: float  # gateway-reported wall time, for clock-skew detection
    next_req_id: int = 1        # monotonic per-session request counter
```

On every outgoing command, the HA client stamps `sid=self.session.sid` and `ref=self.session.next_req_id++`. Reconnect → new `hello` → possibly new `sid` → new session object. The previous session's ring entries are retained (they live in the ring, not the session dataclass) and are distinguishable by their `connected_at`.

### 4.7 Gateway-side `sid` verification — **technical debt, not v1**

In v1, the gateway derives `sid` from the WS socket's own mapping (each connection knows its assigned slot). The `sid` field sent by the HA client in each frame is redundant-but-useful for ring traceability and is **recorded as-given**, not checked.

TODO (tech debt): gateway should verify `frame.sid == socket.assigned_slot` and emit an `err` ring record + close the connection on mismatch. Catches:
- buggy/malicious client stamping the wrong slot,
- stale frames delivered after a reconnect under the same slot,
- proxy/relay scenarios we might add later.

Deferred because v1 does not forward frames between sockets, so the mapping is unambiguous from the socket alone. Revisit before adding any relay or multi-process gateway topology.

---

## 5. Why not `logging.handlers.MemoryHandler`

Designed for I/O batching: fills a list to `capacity`, then **flushes to a target handler** (wiping the list). No sliding-window eviction. There is also cpython#95804 (flushOnClose quirk on exit). A `logging.Handler` subclass with `collections.deque(maxlen=N)` is the idiomatic flight-recorder shape — **[Confirmed]** by Python docs and multiple community references.

---

## 6. Home Assistant precedent

| Integration | In-memory debug ring? | Diagnostics pattern |
|---|---|---|
| `system_log` (core) | **Yes** — last 50 warnings/errors in memory | WS API + Logs panel |
| `zwave_js` | No — enables DEBUG in JS driver, bundles driver log | State dump only |
| `zha` | No | State dump only |
| `deconz`, `mqtt` | No | State dump only |

So: ring-into-diagnostics is a **mild novelty but not un-idiomatic** — `system_log` establishes acceptance of in-process rings. Community integrations universally state-dump only; no prior art for per-integration debug rings. **[Consistent]**

HA's Platinum quality scale *requires* `async_get_config_entry_diagnostics`. This plan delivers it.

---

## 7. Configuration

Gateway `gateway.yaml`:

```yaml
debug_ring:
  enabled: true       # default true; cost is negligible
  size_mb: 5          # byte-sized cap, not line count
  level: VERBOSE      # VERBOSE | DEBUG | INFO
```

HA integration: no user-visible config; ring always on, size from `const.py`. Dump is manual via diagnostics download.

---

## 8. Non-goals

- No remote log shipping. Ring stays on-device until user pulls.
- No sampling / rate-limiting — 5 MB is small enough that filtering hides the bug you are hunting.
- No binary ringfile / mmap — plain Python `deque` + JSON lines, debuggable with `print`.
- No change to existing INFO-level journald output — operational logs keep working unchanged.
- No auto-dump on error (deferred; could be added later as an opt-in).

---

## 9. Open questions — resolve before implementation

1. **Window size default.** 35 min at 10 Hz is ample for post-mortem but useless for drift. Keep 5 MB, or make it larger by default?
2. **Auto-dump on error.** Silent pull-on-demand is the current plan. Worth an opt-in "on reconnect storm, write ring to `/var/log/blaueis-gw/dump-{ts}.jsonl`"?
3. **Subscribe-filter wire format.** `include` list, or explicit flags (`rx:true,tx:true,ignored:false`)? Latter is easier to extend.
4. **Slot pool cap.** Default 8 — enough? Exhaustion policy is **reject with error** (not evict-oldest; evict-oldest hides bugs). Revisit if multi-consumer patterns emerge.
5. **Does the AC echo `msg_id` in replies?** **[Unknown]** — must be verified against Session captures before §4.5 confidence=`confirmed` path is trusted. Catalogue per message type; the rest stay on heuristic.
6. **Unsolicited-frame whitelist.** Which message types does the AC push unprompted (status, FollowMe echoes, etc.)? Needed so §4.5 layer 2 does not heuristic-correlate them to whatever TX happened to precede.
7. **Gateway-side `sid` verification.** Tech debt per §4.7 — prioritise when we add any frame-relay or multi-process topology.

---

## 10. References

- Python docs — [logging.Handler](https://docs.python.org/3/library/logging.html), [handlers.MemoryHandler](https://docs.python.org/3/library/logging.handlers.html)
- [cpython#95804](https://github.com/python/cpython/issues/95804) — MemoryHandler flushOnClose quirk
- [cpython#112050](https://github.com/python/cpython/issues/112050) — deque thread-safety under free-threaded Python
- HA developer docs — [Integration diagnostics](https://developers.home-assistant.io/docs/core/integration/diagnostics/), [Quality scale: diagnostics](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/)
- HA core — [`system_log`](https://www.home-assistant.io/integrations/system_log/) (in-memory ring precedent)
- [journald.conf(5)](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html) — `RateLimitBurst`, `Storage`, `SystemMaxUse`
