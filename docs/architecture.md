# blaueis-libmidea — Architecture

> Packages, dependencies, entry points, logger names. Use this as the map
> when deciding where a change belongs.

## 1. Packages at a glance

```
packages/
├── blaueis-core/       Pure Python codec, state, commands, glossary.  No I/O.
├── blaueis-gateway/    Async UART bridge + WebSocket server.  Runs on the Pi.
├── blaueis-client/     Async WebSocket client + high-level Device wrapper.
└── blaueis-tools/      CLI / scripts (capture replay, inspection).
```

Dependency graph (strict, no back-edges):

```
blaueis-core  ←──────────────  blaueis-gateway
      ↑                              ↑
      └──────── blaueis-client ──────┘
                       ↑
                 blaueis-tools
```

`blaueis-core` has **no async, no sockets, no files** other than the glossary
YAML at import time. Everything that moves bytes lives in gateway or client.

---

## 2. `blaueis-core` — codec, state, commands

Pure, synchronous. Target: Python 3.11+.

| Module | Role |
|---|---|
| `frame.py` | CRC-8, checksum, `build_frame` / `parse_frame`, `validate_frame`, `FrameError` |
| `codec.py` | `load_glossary`, `decode_frame_fields`, `encode_field`, capability index |
| `process.py` | Raw frame → status-dict pipeline (B5 capability, C0 / C1 / A1 frames, B0 / B1 properties) |
| `status.py` | Status dictionary schema + merge logic |
| `query.py` | `read_field` — field lookup with value decoding |
| `command.py` | Command body builders for set operations (C3, B0 bulk set, B1 property set) |
| `formula.py` | Expression evaluator for glossary-driven scaling / conditions |
| `quirks.py` | Device-specific quirks (e.g. Q11 hi-byte stripping) |
| `crypto.py` | PSK → key, AES-256-GCM handshake (`create_hello`, `complete_handshake_*`) |
| `debug_ring.py` | `logging.Handler` with byte-sized deque + `log_event` helper (flight recorder) |

**Glossary source of truth:** `blaueis-hvacshark/protocols/midea/spec/serial_glossary.yaml` (loaded by `load_glossary`).

**Logger names:** modules don't install handlers; loggers created via
`logging.getLogger(__name__)` (`blaueis.core.*`). `debug_ring` exports
`log_event(logger, level, event, **fields)` for structured event records.

**Public API (imports).** See `blaueis/core/__init__.py` docstring for the
curated list.

---

## 3. `blaueis-gateway` — UART ↔ WebSocket bridge

Runs on the Raspberry Pi as a systemd service. Owns one UART device and serves
multiple WebSocket clients concurrently.

| Module | Role |
|---|---|
| `server.py` | `GatewayServer` — WS server, client lifecycle, broadcast, debug dump. Entry: `python -m blaueis.gateway.server` |
| `uart_protocol.py` | `UartProtocol` — dongle state machine (DISCOVER → MODEL → ANNOUNCE → RUNNING), outstanding-TX correlation (§4.5), frame mirroring |
| `slot_pool.py` | Fixed-size pool of client slot ids; lowest-free allocation, reuse on release (flight_recorder.md §4.6) |

**Startup path:** `main()` parses config → `logging.basicConfig` is replaced
with explicit root setup (stream handler at user level, `DebugRing` at VERBOSE)
→ `GatewayServer(config, debug_ring=...)` → `server.run()` attaches the
always-on UART tap and serves WebSockets.

**Always-on tap:** `server.py:_on_uart_frame` is attached to the protocol
unconditionally at startup — the ring captures every RX/TX regardless of
whether a WS client is connected (flight_recorder.md §4.2).

**Logger names:** `hvac_gateway` (server-level), `uart_protocol` (frame-level;
raw hex at VERBOSE=5).

**Transport:** plain `websockets.serve`. Encryption is **application-layer
AES-256-GCM** over the already-open WS, negotiated in the first round-trip
(`core.crypto.complete_handshake_server`). `--no-encrypt` bypasses for local
development.

**Config:** YAML at `/etc/blaueis-gw/gateway.yaml` (global) +
`/etc/blaueis-gw/instances/<name>.yaml` (per-AC). See `docs/operations.md` §3
for keys.

**systemd unit:** `blaueis-gateway@.service` — instanced by YAML filename.
`systemctl start blaueis-gateway@atelier` loads `instances/atelier.yaml`.

---

## 4. `blaueis-client` — async WS client + Device wrapper

Two layers, pick by need.

### 4.1 `HvacClient` (low level — `ws_client.py`)

One-to-one with the WS protocol. Connect → crypto handshake → `listen()` loop.
Stateless about AC semantics — just sends / receives framed dicts.

```python
from blaueis.client.ws_client import HvacClient
c = HvacClient(host, port, psk=psk_bytes)
await c.connect()
asyncio.create_task(c.listen())          # dispatches to on_frame / listeners
ref = await c.send_frame(query.hex())
dump = await c.request_debug_dump()       # flight recorder
await c.send_subscribe(include=["rx","tx"], annotate=["origin","reply_to"])
await c.close()
```

**`GatewaySession`** dataclass (populated from the gateway's `hello` message):
`sid`, `pool_size`, `connected_at`, `connected_wall`, `server_time_at_connect`,
`next_req_id`. `send_frame` stamps `sid` on outgoing frames (advisory; see
flight_recorder.md §4.7).

**Logger:** `hvac_client`.

### 4.2 `Device` (high level — `device.py`)

Autonomous device abstraction. Owns its own connection lifecycle
(supervisor + reconnect backoff), status database that survives reconnects,
B5 capability discovery, periodic polling, field-change callbacks.

```python
from blaueis.client.device import Device
dev = Device(host, port, psk=psk)
dev.on_state_change = lambda field, new, old: ...
await dev.start()
print(dev.available_fields)                  # B5-confirmed fields only
await dev.set(power=True, target_temperature=22)
await dev.stop()
```

**Status DB invariant:** `self._status` persists across connection drops. B5
caps loaded once, never cleared. Supervisor restarts dead loops without
touching state. Per flight_recorder.md §1.1 — every received frame is
processed regardless of correlation.

**Logger:** `blaueis.device`.

---

## 5. `blaueis-tools` — CLI utilities

Scripts that consume the client library — capture replay, status pretty-print,
glossary browser. Depends on `blaueis-client` and `blaueis-core`. Not runtime
path for production.

---

## 6. What each logger sees

| Logger | Attached by | Level (default) | Goes where |
|---|---|---|---|
| `hvac_gateway` | gateway `main()` | VERBOSE (ring) + configured (stream) | Journal + DebugRing |
| `uart_protocol` | (propagates to root) | VERBOSE records captured | DebugRing |
| `hvac_client` | HA `__init__` | VERBOSE (ring) + default (HA log) | `homeassistant.log` + HA DebugRing |
| `blaueis.device` | HA `__init__` | VERBOSE (ring) + default | Same |
| `blaueis.client.*` | HA `__init__` | VERBOSE (ring) + default | Same |
| `blaueis.core.*` | — (pure lib) | inherits | Wherever parent is attached |

See `docs/flight_recorder.md` §4 for the ring record schema.

---

## 7. Cross-cutting concerns

- **Wire protocol over WS:** `docs/ws_protocol.md`.
- **Flight recorder (debug buffer):** `docs/flight_recorder.md`.
- **Operations / install / config / update:** `docs/operations.md`.
- **HA integration (consumer of this library):** `../blaueis-ha-midea/docs/integration.md`.
