# Blaueis — versioning & compatibility policy

> How blaueis-libmidea, blaueis-ha-midea, and blaueis-esphome stay
> compatible across releases. The handshake-level wire details live in
> [`ws_protocol.md`](ws_protocol.md) §2.5 / §3.10; this file is the
> *policy* layer above those.

---

## 1. Two version axes

Every gateway / client pair tracks two independent values:

| Axis | Type | When it changes | Negotiation |
|---|---|---|---|
| `protocol_version` | integer | Wire-format break (frame envelope, encryption, message shape) | Both sides MUST agree on the integer; mismatch = refuse to connect |
| `software_version` | semver | Any feature or bugfix release | Advisory; clients adapt via a static feature table |

Protocol bumps are rare and disruptive. Software bumps are routine.

## 2. Handshake exchange

The crypto `hello` / `hello_ok` envelope (encoded by `blaueis.core.crypto`)
carries both values in addition to the keys:

```json
{ "type": "hello", "client_pub": "...", "client_rand": "...",
  "protocol_version": 1, "client_version": "0.4.0" }

{ "type": "hello_ok", "server_pub": "...", "server_rand": "...", "mac": "...",
  "protocol_version": 1, "gateway_version": "0.3.0", "gateway_type": "pi",
  "min_client_version": "0.2.0" }
```

After handshake both sides know:

- `protocol_version` — integer, must match
- `gateway_version` / `client_version` — semver, advisory
- `gateway_type` — `"pi"` or `"esphome"`; lets clients show the right
  update instructions (`blaueis-update.sh` vs. ESPHome dashboard)
- `min_client_version` — gateway's floor; client below this should warn
  but is not refused unless the protocol_version also breaks

## 3. Feature negotiation — static table, not capability exchange

Clients keep a hardcoded `gateway_version → feature-set` table:

```python
GATEWAY_FEATURES = {
    "0.1.0": {"basic_frame_passthrough"},
    "0.2.0": {"basic_frame_passthrough", "heartbeat_forwarding"},
    "0.3.0": {..., "b5_cap_passthrough"},
    "0.4.0": {..., "follow_me_passthrough", "c1_group_queries"},
}
```

The client picks the largest entry `≤ gateway_version` and assumes that
feature set. No per-feature capability flags exchanged on the wire —
keeps the handshake small and the server stateless.

The HA integration uses this to grey out controls that need a newer
gateway, and to display "update available" banners with type-specific
instructions.

## 4. Compatibility rules

### During beta (0.x)

Breaking changes allowed. Gateway and client should be updated together.
HA warns when versions diverge.

### After 1.0

- **`protocol_version`** stays stable. Only bumps for genuine
  wire-format breaks. Should ideally never happen.
- **Gateway WS API** is additive only. New message types added; old ones
  never removed. A v1.0 gateway works with a v1.5 client.
- **"Warn but don't break" rule:** the HA integration never refuses to
  connect to a gateway that speaks the same `protocol_version`. It may
  warn about missing features, grey out controls, or show "update
  recommended" — but basic climate control always works.

## 5. HA integration UX

| Gateway state | What user sees |
|---|---|
| Up to date | Green: "Gateway v0.4.0" in device info |
| Minor behind | Info banner: "Update available (v0.3 → v0.4). New: Follow Me." + update instructions per `gateway_type` |
| Protocol mismatch | Error: refuses connection. "Please update your gateway first." |

## 6. Version cascade — feature addition example

```
1. blaueis-libmidea v0.4.0
   ├── core: add build_follow_me_frame()
   ├── client: add device.start_follow_me(), RecurringTask
   ├── gateway: no change (frame passthrough)
   └── tag v0.4.0

2. blaueis-ha-midea v0.4.0
   ├── bump libmidea submodule
   ├── add Follow Me UI control
   ├── grey out if gateway < v0.4.0
   └── HACS release

3. blaueis-esphome — no change (passthrough)

4. Users update HA via HACS → see Follow Me
   ├── Gateway current → works
   └── Gateway old → "Follow Me requires gateway v0.4.0. [Update]"
```

## 7. Version cascade — protocol break (post-1.0 should be never)

```
1. blaueis-libmidea v2.0.0 (protocol_version 1 → 2)
2. blaueis-esphome v2.0.0 (C++ port of new handshake)
3. blaueis-ha-midea v2.0.0
4. User MUST update gateway before HA integration —
   or: integration speaks both protocol versions during a transition window
```
