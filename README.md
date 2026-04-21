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

- [docs/architecture.md](docs/architecture.md) — package map, dependencies, logger names.
- [docs/ws_protocol.md](docs/ws_protocol.md) — WebSocket wire reference (every frame type).
- [docs/operations.md](docs/operations.md) — install, systemd, config, update, debug, troubleshoot.
- [docs/flight_recorder.md](docs/flight_recorder.md) — rolling in-memory debug buffer (design + rationale).

> **A note on the name.** Blaueis is a small glacier in the Bavarian Alps,
> retreating year by year. Use energy responsibly — climate change is real.

Not affiliated with any commercial entity or the Berchtesgaden National
Park administration.

## Acknowledgments

Protocol knowledge in this library builds on community research, own hardware captures, and publicly available documentation.

## License

[CC0 1.0 Universal](LICENSE) — public-domain dedication. No warranty.
