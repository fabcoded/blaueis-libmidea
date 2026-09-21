# Blaueis — versioning & compatibility policy

> Which version numbers exist, what each one means, who compares them, and
> the rules for changing them. The wire details live in
> [`ws_protocol.md`](ws_protocol.md) §2.1 (handshake) and §2.5 (`version`);
> this file is the *policy* layer above them.

---

## 1. Three version values

| Value | Type | Lives in | Who compares it |
|---|---|---|---|
| Protocol version | integer | `PROTOCOL_VERSION` in `blaueis.core.crypto` (today `2`) | The gateway, during the handshake. A mismatch is refused. |
| Package version | semver | `version` in `packages/blaueis-{core,client,gateway}/pyproject.toml` (and `blaueis-tools`, which is not published) | Nobody at runtime. `pip` checks the `blaueis-core~=…` pin of client and gateway ([`releasing.md`](releasing.md)). |
| Gateway build string | free text | `git describe --tags --always --dirty` run in `/opt/blaueis-gw` when the gateway process starts | Nobody. It is reported, never compared. |

**Protocol version** counts wire-format breaks (frame envelope, encryption,
message shape) — rare and disruptive. **Package versions** move on every
release, all three published packages to the same number (§4). The **gateway
build string** says which checkout a running gateway was started from:

| Checkout | Build string |
|---|---|
| a release tag | `v0.1.0` |
| commits after a tag | `v0.1.0-5-g1a2b3c4` |
| no tag reachable | `1a2b3c4` |
| uncommitted changes | any of the above plus `-dirty` |
| `git` fails or `/opt/blaueis-gw` is missing | `unknown` |

**Spelling.** The git tag, the GitHub release and the gateway build string
carry the leading `v` (`v0.1.0`); PyPI, `pyproject.toml` and the Home
Assistant integration's manifest carry the bare form (`0.1.0`). The release
pipeline refuses a tag whose bare form differs from the package versions
([`releasing.md`](releasing.md)).

The build string is computed once, at process start
(`GW_VERSION` in `blaueis/gateway/server.py`), so a gateway that was updated
in place keeps reporting the old string until the service restarts — which the
update path does itself. `blaueis-gw --version` runs its own `git describe`
on the checkout and therefore always shows the checkout, not the process.

### Protocol version history

| Ver | Date | Break |
|---|---|---|
| 1 | initial | PSK/HKDF session crypto, one shared session key and nonce prefix for both directions. |
| 2 | 2026-06 | Direction-separated session keys and nonce prefixes; scrypt PSK stretching; the client confirms the key by decrypting the gateway's first encrypted message. v1 peers refused. |

## 2. What is exchanged at runtime

**Protocol version, in the handshake.** The client's plaintext `hello`
carries it as `"version": 2`. The gateway compares it with its own
`PROTOCOL_VERSION` (`complete_handshake_server`); on a mismatch it logs the
refusal and closes the connection **without any message on the wire** — an
unauthenticated "stop retrying" signal would hand an on-path attacker a
denial lever. The client therefore sees a connection that closed during the
handshake, treats it as transient, and retries (`ws_protocol.md` §2.1). The
gateway's `hello_ok` carries no version, and the client compares nothing.

**Gateway build string, after connect.** `Device.start()` sends
`{"type":"version"}` once (`ws_protocol.md` §2.5) and waits up to two seconds
for the reply, whose `version` field is the build string. The client keeps it
in `Device.gateway_info["version"]` (`"unknown"` until the reply arrives) and
does not ask again after a reconnect. The gateway also puts the same string
in every `pi_status` broadcast (`ws_protocol.md` §3.5); the client stores that
message in `Device.gateway_stats` but takes only `device_name` and `instance`
from it into `gateway_info`. Consumers such as the Home Assistant integration
display the string and include it in diagnostics.

**Not exchanged.** No package version travels in the handshake, there is no
minimum-version floor, no feature table, no capability list and no
"update available" signal. A mismatch of package versions between gateway and
client is not detected anywhere. The design for such an exchange is in
[`design/version_negotiation.md`](design/version_negotiation.md); nothing in
it exists in code.

## 3. Compatibility rules

### During 0.x

Breaking changes are allowed in any release, protocol-compatible or not.
**Update gateway and client together** — an instruction to the operator, not
something the code enforces. The one incompatibility the code does refuse is a
protocol-version mismatch (§2); any other break (a changed reply shape, a
removed message type) shows up as undefined behaviour, not as a clean error.

### From 1.0

- `protocol_version` stays stable and bumps only for a genuine wire-format
  break — ideally never.
- The gateway WebSocket API is additive: new message types are added, old ones
  are not removed. A 1.0 gateway works with a 1.5 client.

### Always

- A peer speaks exactly one protocol version; there is no transition window in
  which both are accepted.
- A protocol-version bump is a lockstep release of gateway and client.

## 4. Lockstep versioning

`blaueis-core`, `blaueis-client` and `blaueis-gateway` are released together
under one semver number and one tag (`vX.Y.Z`); every release bumps all three
to that value, even a package whose code did not change. Client and gateway
pin `blaueis-core` to the same minor series. `blaueis-tools` carries the same
number but is not published. The mechanics — pins, tag guard, order of
publication — are in [`releasing.md`](releasing.md).

## 5. Release cascade — a feature in the library

```
1. blaueis-libmidea vX.Y.Z
   ├── core / client: the change, e.g. a new frame builder and a Device method
   ├── gateway: no code change is needed for frame passthrough, but the
   │   version still moves in lockstep
   ├── CHANGELOG.md: entry under [Unreleased], moved to [X.Y.Z] at release
   └── tag vX.Y.Z → PyPI upload → draft GitHub release → published by hand

2. blaueis-ha-midea
   ├── syncs blaueis-core and blaueis-client into its vendored copy
   ├── adds the UI on top of the new API
   └── publishes its own tagged release through HACS

3. Users update the integration through HACS. The gateway is a separate
   step: `blaueis-gw update --apply` on the Pi, or the `update` WebSocket
   message. It follows the latest published GitHub release of this
   repository, independently of the integration.
```

If the change needs new gateway behaviour, the new integration release does
not work fully until the gateway has been updated; during 0.x the release
notes tell the operator to update both.

## 6. Release cascade — a protocol break

```
1. blaueis-libmidea: PROTOCOL_VERSION n → n+1 in blaueis.core.crypto, a
   history row in §1, a breaking entry in CHANGELOG.md, lockstep release
2. blaueis-ha-midea: sync the library, publish a HACS release
3. Operators update gateway and integration together. Until both are on the
   new version the gateway closes every connection during the handshake and
   the integration reports the gateway as unreachable.
```

There is no order in which the two updates avoid an outage: peers on different
protocol versions cannot talk. The v1 → v2 change of 2026-06 went this way.
