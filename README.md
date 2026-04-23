# blaueis-libmidea

Midea air conditioner serial protocol library — codec, state management,
gateway daemon, async client, and CLI tools.

**Requires Python 3.11+.** Status: active, feature-complete. Target platform
for the gateway daemon is a Raspberry Pi on the Wi-Fi dongle UART bus at
9600 8N1; the library itself is platform-neutral.

## Packages

| Package | What it is |
|---|---|
| `blaueis-core` | Pure-Python codec, CRC/checksum, glossary-driven field decode, AES-256-GCM crypto, `DebugRing` handler. No I/O. |
| `blaueis-gateway` | Async UART↔WebSocket bridge. Runs on the Pi as a systemd service. Flight-recorder, slot pool, correlation engine. |
| `blaueis-client` | Async WebSocket client + `Device` wrapper (connection supervision, B5 capability discovery, status-DB survives reconnects). |
| `blaueis-tools` | CLI utilities — capture replay, inspection. |

## Quick start

**On a Raspberry Pi** (install the gateway daemon):

```sh
bash -c "$(curl -sL https://raw.githubusercontent.com/fabcoded/blaueis-libmidea/main/scripts/install.sh)"
```

See [docs/operations.md](docs/operations.md) for config + systemd details.

**On a development machine** (use the client library):

```sh
git clone https://github.com/fabcoded/blaueis-libmidea.git
cd blaueis-libmidea
pip install -e packages/blaueis-core -e packages/blaueis-client
```

## Running tests

Each package has its own pytest root — cd in before running:

```sh
cd packages/blaueis-core    && python3 -m pytest      # 61
cd packages/blaueis-gateway && python3 -m pytest      # 43
cd packages/blaueis-client  && python3 -m pytest      # 147
```

## Documentation

- [`packages/blaueis-core/src/blaueis/core/data/glossary.yaml`](packages/blaueis-core/src/blaueis/core/data/glossary.yaml) — field glossary: every field the library reads or writes, with units, ranges, availability, and capability gating. Self-documenting — read it directly; the file header explains the schema.
- [docs/architecture.md](docs/architecture.md) — package map, dependencies, logger names.
- [docs/ws_protocol.md](docs/ws_protocol.md) — WebSocket wire reference (every frame type).
- [docs/operations.md](docs/operations.md) — install, systemd, config, update, debug, troubleshoot.
- [docs/flight_recorder.md](docs/flight_recorder.md) — rolling in-memory debug buffer (design + rationale).
- [docs/status_db.md](docs/status_db.md) — status dictionary schema and merge logic.

> **A note on the name.** Blaueis is a small glacier in the Bavarian Alps,
> retreating year by year. Use energy responsibly — climate change is real.

Not affiliated with any commercial entity or the Berchtesgaden National
Park administration.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — covers setup, test expectations, the citation rule, and what good PRs look like.

## Related projects in this ecosystem

Blaueis is an umbrella of open-protocol HVAC tooling. Sibling repos:

- [blaueis-ha-midea](https://github.com/fabcoded/blaueis-ha-midea) — Home Assistant custom integration that consumes this library via a gateway.
- [blaueis-esphome](https://github.com/fabcoded/blaueis-esphome) — ESPHome external component porting the gateway protocol to ESP32 (placeholder, not yet implemented).
- [blaueis-hvacshark](https://github.com/fabcoded/blaueis-hvacshark) — Wireshark Lua dissector, live-capture dongle, and protocol specifications.
- [blaueis-hvacshark-traces](https://github.com/fabcoded/blaueis-hvacshark-traces) — capture sessions and offline-analysis scripts used to derive the protocol.

## Acknowledgments

Protocol knowledge in this library builds on community research, own hardware captures, and publicly available documentation. A deep thank you to the open-source and home-automation community — especially the contributors around **Home Assistant** and the broader maker community — for their tireless research work and for publishing their findings openly.

Community projects that materially informed this work:

- [dudanov/MideaUART](https://github.com/dudanov/MideaUART) — ESP/Arduino library for Midea UART, especially thorough on 0xC0 / 0x40 frame decoding.
- [chemelli74/midea-local](https://github.com/chemelli74/midea-local) — Python client for the Midea LAN protocol; deep capability (B5) reference.
- [reneklootwijk/node-mideahvac](https://github.com/reneklootwijk/node-mideahvac) — Node.js driver covering multiple Midea product families.
- [NeoAcheron/midea-ac-py](https://github.com/NeoAcheron/midea-ac-py) — early Python Midea AC implementation and historical reference.
- [wuwentao/midea_ac_lan](https://github.com/wuwentao/midea_ac_lan) — HA integration covering broad Midea device coverage.
- Countless forum threads, GitHub issues, and pull requests in the HA and ESPHome communities.

If you believe your work is referenced here without proper attribution, or you have licensing concerns, please open an issue — we will respond promptly.

## License

[CC0 1.0 Universal](LICENSE) — public-domain dedication. No warranty.
