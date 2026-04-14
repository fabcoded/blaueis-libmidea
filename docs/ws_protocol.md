# Blaueis Gateway — WebSocket Protocol

> Wire reference. Every message is a JSON object with a mandatory `type`
> field. When encryption is enabled, every *payload* is AES-256-GCM-wrapped
> by `blaueis.core.crypto`; the wrapping is transparent to the schemas below.
> `ref` is a caller-chosen monotonic id; the gateway echoes it on replies.

Cross-refs: `blaueis-gateway/src/blaueis/gateway/server.py` (`_handle_client_message`) · `blaueis-client/src/blaueis/client/ws_client.py` · `docs/flight_recorder.md` (§4.1 / §4.4 / §4.6).

---

## 1. Lifecycle (happy path)

```
client: open TCP                                        gateway: accept
client: { type:"hello", ... }             ──────▶       gateway: crypto handshake
                                          ◀──────       gateway: { type:"hello_ok", ... }
                                          ◀──────       gateway: { type:"hello", sid:N, ... }
client: { type:"subscribe", ... }         ──────▶ *
                                          ◀──────       gateway: { type:"subscribed", ... }
client: { type:"frame", hex:"...", ref:1, sid:N } ─▶    gateway: writes to UART
                                          ◀──────       gateway: { type:"ack", ref:1, status:"queued" }
                                          ◀──────       gateway: { type:"frame", dir:"rx", hex:"..." }
...
client: { type:"debug_dump", ref:42 }     ──────▶ *
                                          ◀──────       gateway: { type:"debug_dump", ref:42, jsonl:"..." }
client: close WS                                        gateway: release slot
```

`*` = introduced by the flight-recorder feature; older clients may skip.

---

## 2. Client → gateway

### 2.1 Crypto handshake `hello` / reply `hello_ok`

Structure handled by `blaueis.core.crypto.create_hello` / `complete_handshake_server`. Details in `crypto.py`; not reproduced here. Skipped entirely when the server runs with `--no-encrypt`.

### 2.2 `subscribe` — per-socket filter (§4.1)

```json
{"type":"subscribe","ref":1,
 "include":["rx","tx","ignored"],
 "annotate":["origin","req_id","msg_id","tx_seq","reply_to"]}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `include` | list\<str\> | `["rx"]` | Event kinds delivered as `frame`. Valid: `rx`, `tx`, `ignored`. |
| `annotate` | list\<str\> | `[]` | Provenance fields attached to each `frame` delivery. Valid: `origin`, `req_id`, `msg_id`, `tx_seq`, `reply_to`. |

Validated server-side. Unknown values → `type:"error"`, state unchanged. Never filters by provenance (§1.1 stateless invariant).

### 2.3 `frame` — send a Midea frame to the AC

```json
{"type":"frame","hex":"AA 20 AC ...","ref":123,"sid":2}
```

| Field | Required | Notes |
|---|---|---|
| `hex` | yes | Full frame bytes in hex (spaces OK). Validated via `validate_frame` before queueing. |
| `ref` | recommended | Monotonic per-client. Used as `req_id` in the ring and echoed on `ack`. |
| `sid` | optional | Advisory — the gateway's `hello`-assigned slot. §4.7 tech debt: not verified in v1. |

Gateway replies with `ack` (queued) or `error` (invalid hex / queue full).

### 2.4 `ping`

```json
{"type":"ping"}
```
Reply: `{"type":"pong"}`. No `ref`.

### 2.5 `version`

```json
{"type":"version"}
```
Reply:
```json
{"type":"version","version":"abc1234","device_name":"Midea AC","instance":"atelier"}
```

### 2.6 `logs` — last N journal lines

```json
{"type":"logs","ref":3,"n":50}
```

Returns the last `n` (capped at 100) journal entries for the service unit:
```json
{"type":"logs","ref":3,"lines":["...","..."]}
```

### 2.7 `debug_dump` — pull the flight-recorder ring (§4.4)

```json
{"type":"debug_dump","ref":42}
```

Reply:
```json
{"type":"debug_dump","ref":42,
 "jsonl":"{\"ts\":...}\n{\"ts\":...}\n",
 "record_count":35124,
 "size_bytes":4980123,
 "ring_capacity_bytes":5242880}
```

`jsonl` is newline-delimited JSON objects, one per ring record. Record schema: see `flight_recorder.md` §4.3.

If the ring is disabled in gateway config: `{"type":"error","ref":42,"msg":"debug ring disabled in gateway config"}`.

### 2.8 `update` — remote git-pull + reinstall

```json
{"type":"update","ref":4}
```

Triggers `_run_update()`: `git pull --ff-only` in `/opt/blaueis-gw`, then `pip install -e ...` for `blaueis-core` + `blaueis-gateway`. On success exits non-zero so systemd restarts the service.

Reply flow:
```json
{"type":"ack","ref":4,"status":"updating"}
{"type":"update_result","ref":4,"ok":true,"old_version":"...","new_version":"...","steps":[...]}
```

Requires `allow_remote_update: true` in gateway config (default). Disabled → `error`.

---

## 3. Gateway → client

### 3.1 `hello` — slot assignment (§4.6)

Sent unsolicited as the first message after crypto handshake completes.

```json
{"type":"hello","sid":2,"pool_size":8,"server_time":1712000000.123}
```

Client should stash `sid` + `server_time` (see `GatewaySession`). `sid` is reused after disconnect; ring timestamps disambiguate sessions.

### 3.2 `frame` — UART bus observation

```json
{"type":"frame","hex":"AA 20 AC ...","ts":1712000000.456,"dir":"rx",
 "origin":"ws:2","req_id":123,"msg_id":193,"tx_seq":4711,
 "reply_to":{"req_id":123,"origin":"ws:2","confidence":"confirmed"}}
```

| Field | Always present | Notes |
|---|---|---|
| `hex` | yes | Raw bytes observed on the wire |
| `ts` | yes | Gateway monotonic timestamp |
| `dir` | yes | `"rx"` (from AC) or `"tx"` (we sent it) |
| others | **only if subscribed with `annotate`** | Provenance — absent on default subscription |

Delivered per-subscriber according to `include`/`annotate`.

### 3.3 `ack` / `error`

```json
{"type":"ack","ref":1,"status":"queued"}
{"type":"error","ref":1,"msg":"queue full"}
{"type":"error","code":"slot_pool_full","msg":"gateway accepts max 8 concurrent clients"}
```

`code` is present on structural errors (auth failure, slot exhaustion); `msg` is always present. `ref` is present when the error is a reply to a specific request.

### 3.4 `pong` — ping reply

```json
{"type":"pong"}
```

### 3.5 `pi_status` — periodic stats broadcast

Broadcast to all clients every `stats_interval` seconds (default 60).

```json
{"type":"pi_status","uptime_s":12345,"cpu_percent":3.4,"ram_total_mb":3800,
 "ram_used_mb":820,"temp_c":46.2,"protocol_state":"running",
 "appliance":"0xAC","model":40961,"clients":2,"version":"abc1234",
 "device_name":"Midea AC","instance":"atelier",
 "disk_total_mb":...,"disk_free_mb":...,"disk_used_mb":...}
```

### 3.6 `journal` — periodic journal broadcast

Every 60s the gateway reads the last 10 journal entries and broadcasts them as:
```json
{"type":"journal","lines":["2026-04-14 ...","..."]}
```

Flight-recorder ring largely supersedes this for debug purposes; kept for compatibility.

### 3.7 `subscribed` — subscribe filter confirmation

```json
{"type":"subscribed","ref":1,"include":["rx","tx"],"annotate":["origin","reply_to"]}
```

Echoes the normalised (sorted) accepted filter state.

### 3.8 `debug_dump` reply

See §2.7.

### 3.9 `update_result`

See §2.8.

### 3.10 `version` reply

See §2.5.

### 3.11 `logs` reply

See §2.6.

---

## 4. Error taxonomy

| `code` | Meaning | Resolution |
|---|---|---|
| `slot_pool_full` | More than `slot_pool_size` concurrent clients | Close an idle client, or raise `slot_pool_size` in gateway config |
| (no code, `msg:"queue full"`) | TX queue backlog ≥ `max_queue` | Client is flooding; throttle sends |
| (no code, `msg:"invalid hex: ..."`) | `frame.hex` unparseable or fails `validate_frame` | Fix the frame bytes |
| (no code, `msg:"debug ring disabled in gateway config"`) | `debug_ring_enabled: false` | Flip the config flag + restart |
| (no code, `msg:"remote updates disabled in config"`) | `allow_remote_update: false` | Update manually via SSH |
| (no code, `msg:"unknown values: ..."`) | Bad `subscribe` args | Correct `include`/`annotate` values |

Encryption failures (bad PSK, replay) cause connection drop during handshake — no wire error; check gateway journal.

---

## 5. Encryption wrapping

When enabled, every frame (except the initial `hello` from the client) is:

```
{"iv":"<hex>","tag":"<hex>","ciphertext":"<hex>"}
```

…with the inner JSON encrypted under an AES-256-GCM key derived from the shared PSK + both-sides nonces (`blaueis.core.crypto`). Replay is rejected by sequence number. Handled transparently by `HvacClient` / `GatewayServer`.
