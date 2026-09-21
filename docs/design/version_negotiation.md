# Version negotiation — design for 0.2

> **Design, not behaviour. None of this exists in code as of 0.1.0.** What
> gateway and client do today with version numbers is described in
> [`../versioning.md`](../versioning.md) §2: the handshake carries the
> protocol integer and the gateway refuses a mismatch; after connect the
> client asks for the gateway's `git describe` build string. Nothing
> compares package versions, and no feature table, minimum-version floor
> or "update available" message exists.
>
> Negotiation pays off once several gateway/integration pairs exist in the
> wild, which is after 0.1.0. It is deferred to 0.2 for that reason.

---

## 1. Axes

Every gateway / client pair would track two independent values:

| Axis | Type | When it changes | Negotiation |
|---|---|---|---|
| protocol version | integer | Wire-format break (frame envelope, encryption, message shape) | Both sides must agree on the integer; mismatch = refuse to connect (this part exists today) |
| software version | semver | Any feature or bugfix release | Advisory; clients adapt via a static feature table (design) |

## 2. Handshake exchange

The crypto `hello` / `hello_ok` envelope (encoded by `blaueis.core.crypto`)
would carry the software versions in addition to what it carries today. The
existing `version` field stays the protocol integer; the added fields are new
keys, so a peer that does not know them ignores them:

```json
{ "type": "hello", "version": 2, "client_rand": "...",
  "client_version": "0.4.0" }

{ "type": "hello_ok", "server_rand": "...",
  "gateway_version": "0.3.0", "gateway_type": "pi",
  "min_client_version": "0.2.0" }
```

After the handshake both sides would know:

- the protocol integer — must match (enforced today)
- `gateway_version` / `client_version` — semver, advisory
- `gateway_type` — `"pi"` or `"esphome"`; lets clients show the right update
  instructions (`blaueis-gw update` vs. the ESPHome dashboard)
- `min_client_version` — the gateway's floor; a client below it should warn
  but is not refused unless the protocol integer also differs

`hello` and `hello_ok` are plaintext, so anything added there is
unauthenticated; whether the versions belong in the handshake or in the first
encrypted message is an open point of this design.

## 3. Feature negotiation — static table, not capability exchange

Clients would keep a hardcoded `gateway_version → feature-set` table:

```python
GATEWAY_FEATURES = {
    "0.1.0": {"basic_frame_passthrough"},
    "0.2.0": {"basic_frame_passthrough", "heartbeat_forwarding"},
    "0.3.0": {..., "b5_cap_passthrough"},
    "0.4.0": {..., "follow_me_passthrough", "c1_group_queries"},
}
```

The client picks the largest entry `≤ gateway_version` and assumes that
feature set. No per-feature capability flags go over the wire — the handshake
stays small and the server stateless. (The version numbers above are
illustrative.)

The Home Assistant integration would use this to grey out controls that need a
newer gateway and to display "update available" banners with type-specific
instructions.

## 4. Compatibility rule for the integration

**"Warn but don't break":** the Home Assistant integration never refuses to
connect to a gateway that speaks the same protocol integer. It may warn about
missing features, grey out controls or show "update recommended" — but basic
climate control always works.

## 5. Home Assistant UX

| Gateway state | What the user sees |
|---|---|
| Up to date | Green: "Gateway v0.4.0" in device info |
| Minor behind | Info banner: "Update available (v0.3 → v0.4). New: Follow Me." plus update instructions per `gateway_type` |
| Protocol mismatch | Error: refuses the connection. "Please update your gateway first." |

The protocol-mismatch row would need the gateway to announce the refusal.
Today it closes the handshake silently on purpose (an unauthenticated
"stop retrying" signal is a denial lever, `ws_protocol.md` §2.1), so this row
depends on resolving that.

## 6. Cascade with negotiation — feature addition

```
1. blaueis-libmidea v0.4.0
   ├── core: add build_follow_me_frame()
   ├── client: add device.start_follow_me(), RecurringTask
   ├── gateway: no change (frame passthrough)
   └── tag v0.4.0

2. blaueis-ha-midea v0.4.0
   ├── sync the library, add the Follow Me UI control
   ├── grey it out if gateway < v0.4.0
   └── HACS release

3. Users update HA via HACS and see Follow Me
   ├── gateway current → works
   └── gateway old → "Follow Me requires gateway v0.4.0. [Update]"
```
