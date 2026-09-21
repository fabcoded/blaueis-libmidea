# Changelog

Notable changes to blaueis-libmidea — `blaueis-core`, `blaueis-client` and
`blaueis-gateway`, released together under one version number. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [semantic versioning](https://semver.org/), with breaking changes
allowed in any 0.x release ([`docs/versioning.md`](docs/versioning.md)).

## [Unreleased]

## [0.1.0]

First release. Released under the MIT License; requires Python 3.11 or newer.

### Added

- **Gateway for the Raspberry Pi.** `scripts/install.sh` installs the UART ↔
  WebSocket bridge as a systemd service, pinned to the latest published GitHub
  release (`--ref <tag|branch>` picks another version). A setup wizard
  (`blaueis-gw configure`) creates and edits instances, several air
  conditioners can run side by side, and the instances start again after a
  reboot. `blaueis-gw status`, `logs` and `uninstall` cover day-to-day
  operation. See [`docs/operations.md`](docs/operations.md).
- **Gateway update and rollback.** `sudo blaueis-gw update` reports whether a
  newer release exists; `--apply` installs it, `--ref <tag>` installs a chosen
  version, `--rollback` returns to the previous one. A failed update restores
  the previous checkout. A client can also trigger an update over the
  WebSocket unless `allow_remote_update` is off.
- **Session protocol v2.** Every WebSocket session is AES-256-GCM encrypted
  with a pre-shared key: a separate key and nonce prefix per direction, the
  passphrase stretched with scrypt, and key confirmation while connecting, so
  a wrong key fails the connect instead of surfacing later. The gateway caps
  unauthenticated connections and the number of concurrent clients (slot
  pool). Peers on another protocol version are refused during the handshake.
  See [`docs/ws_protocol.md`](docs/ws_protocol.md).
- **Version reporting.** The gateway reports the tag it runs (`v0.1.0` at a
  release checkout) in reply to a `version` request and in every `pi_status`
  broadcast; `blaueis-gw --version` shows the checkout. See
  [`docs/versioning.md`](docs/versioning.md).
- **Flight recorder.** The gateway keeps the last ~5 MB of frames, state
  changes and errors in memory and writes nothing to the journal unless asked.
  A client pulls the ring with `debug_dump`, so a consumer can merge it
  with its own `DebugRing` (provided by `blaueis-core`) into one diagnostics
  bundle. See [`docs/flight_recorder.md`](docs/flight_recorder.md).
- **Glossary-driven codec.** The field glossary
  (`blaueis/core/data/glossary.yaml`) describes every field the library reads
  or writes — encoding, unit, range, description and the capabilities and modes
  that make it available. Frames are decoded and built from it. Capability
  discovery reads the unit's own capability report, and fields the unit does
  not support, or that do not apply in the current mode, are not offered. See
  [`docs/feature_gating.md`](docs/feature_gating.md) and
  [`docs/status_db.md`](docs/status_db.md).
- **Client library for Home Assistant and other consumers.** `HvacClient`
  speaks the gateway's protocol; `Device` adds a supervised connection with
  automatic reconnect, capability discovery once per session, a status
  database that survives reconnects, cyclic polling derived from the glossary
  and a freshness check (`is_fresh()`). A confirmed wrong key raises
  `AuthenticationError` and stops the reconnect loop, calling `on_auth_failed`;
  transient handshake failures keep retrying.
- **Packages on PyPI.** `blaueis-core`, `blaueis-client` and `blaueis-gateway`
  are published to PyPI from tagged releases. `blaueis-tools` (glossary lint
  and collision check, field inventory, capture and probe utilities) stays in
  the repository.

[Unreleased]: https://github.com/fabcoded/blaueis-libmidea/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/fabcoded/blaueis-libmidea/releases/tag/v0.1.0
