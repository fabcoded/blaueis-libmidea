# Blaueis Gateway — Operations Guide

> Install, configure, update, debug, troubleshoot. For the protocol wire-side
> reference see `ws_protocol.md`; for the flight-recorder design see
> `flight_recorder.md`; for architecture see `architecture.md`.

---

## 1. Hardware and UART

The AC's Wi-Fi dongle port wired to the Pi's primary UART through a level
shifter. **This section is canonical** for these facts — `QUICKSTART.md`
§1–2 in blaueis-ha-midea carries the same facts at stranger depth; if the
two ever disagree, this file wins and the quickstart is corrected in the
same release.

### 1.1 The AC side — CN3

Midea indoor units expose a connector for the Wi-Fi dongle, commonly
labelled **CN3** on the indoor unit's board. Electrically it is a **5 V TTL
UART, 9600 8N1**, on four pins — not USB, even where the socket is
USB-A-shaped (sometimes keyed). On some units the socket is instead a
4-pin JST-XH header.

| CN3 pin | Signal |
|---|---|
| 1 | 5 V |
| 2 | data — the AC's TX *or* RX |
| 3 | data — the other one |
| 4 | GND |

Which of pins 2/3 carries the AC's TX line varies by unit: wire one way,
and swap the two data lines if the gateway never receives (§7,
"Gateway starts but never reaches RUNNING"). With a passive open-drain
shifter a swapped pair does no harm; correct it as soon as the log shows no
response. Identify pins before wiring:
with the unit powered and nothing connected to CN3, measure DC volts
between pins 1 and 4 — expect ~5 V. The board next to CN3 carries mains;
touch only the CN3 pins.

> **Gap.** No verified CN3 photo/pinout for a specific unit, mating
> connector part, or tested level-shifter part number yet. A BSS138-type
> bidirectional module with two or more channels should work.

### 1.2 The Pi side — primary UART

| Header pin | Signal |
|---|---|
| 8 | GPIO14 — **TX** (Pi → AC) |
| 10 | GPIO15 — **RX** (AC → Pi) |
| 6 | GND |
| 1 | 3.3 V — level shifter LV reference |
| 2 | 5 V — level shifter HV reference |

### 1.3 Wiring through the level shifter

The shifter is mandatory: the Pi's UART pins are 3.3 V and are damaged by
5 V, and the AC ignores a 3.3 V drive.

| From | Via | To |
|---|---|---|
| Pi pin 2 (5 V) | — | shifter **HV reference** |
| Pi pin 1 (3.3 V) | — | shifter **LV reference** |
| AC pin 4 (GND) | — | shifter GND **and** Pi pin 6 (GND) |
| AC pin 2 | shifter channel A | Pi pin 10 (RX) |
| AC pin 3 | shifter channel B | Pi pin 8 (TX) |
| AC pin 1 (5 V) | — | **not connected** |

### 1.4 Power rule

The AC's dongle port is rated 5 V / 300 mA; a Pi draws roughly 100–800 mA
depending on model. **Power the Pi from its own 5.1 V supply, share GND
only, and leave CN3 pin 1 unconnected.** Power sequence: wire everything
with both sides off, then power the Pi first, then the AC. If the Pi will
stay off for a long time while the AC stays on, disconnect the data lines
or switch the AC off at the mains.

### 1.5 UART exclusivity and per-model setup

The gateway needs the Pi's primary UART **exclusively** — no login
console, no Bluetooth on it, no other serial daemon.

```sh
sudo raspi-config
# Interface Options → Serial Port: login shell over serial No, serial port hardware Yes
```

- **Pi Zero W, Zero 2 W, 3, 4** — Bluetooth sits on the primary UART by
  default; the header pins get the mini UART instead. Move Bluetooth off:

  ```sh
  echo "dtoverlay=disable-bt" | sudo tee -a /boot/firmware/config.txt
  sudo systemctl disable hciuart
  ```

  After reboot, `/dev/serial0` must resolve to `ttyAMA0`, not `ttyS0`.

- **Pi 5** — `/dev/serial0` is the 3-pin debug header, not header pins
  8/10. Enable the header UART instead:

  ```sh
  echo "dtoverlay=uart0-pi5" | sudo tee -a /boot/firmware/config.txt
  ```

  and select `/dev/ttyAMA0` in the installer wizard (§2).

- **Other 40-pin-header models** — no extra step.

Minimum OS: Raspberry Pi OS **Bookworm** (Python 3.11+; Bullseye ships
3.9 and will not run the gateway). Reboot after any of the above before
continuing to install.

---

## 2. Install

One-line on a Raspberry Pi (Bookworm or newer — see §1.5):

```sh
bash -c "$(curl -sL https://raw.githubusercontent.com/fabcoded/blaueis-libmidea/main/scripts/install.sh)"
```

The installer (`scripts/install.sh`):

- Requires root (asks for `sudo`).
- Creates system user `blaueis-gw`, directories `/opt/blaueis-gw`, `/etc/blaueis-gw`.
- `git clone`s this repo into `/opt/blaueis-gw` **pinned to the latest GitHub
  release** (see *Which version gets installed* below), creates a venv,
  `pip install -e` for `blaueis-core` + `blaueis-gateway`.
- Installs `blaueis-gateway@.service` and `blaueis-gateway.target` into systemd.
- Adds the service user to the `dialout` group (for `/dev/serial0`).
- Runs the setup wizard (or imports `--config <file>`), then enables
  `blaueis-gateway.target` and every enabled instance and starts them (an
  instance that is already running on a re-run is restarted).

Options: `--config <file>` (import an existing instance file), `--user <name>`
(service user), `--ref <tag|branch>` (install a specific version).

**Which version gets installed.** The installer asks the GitHub Releases API
for the latest published release — the same source of truth HACS uses for
the integration — and clones that tag (`git clone --depth 1 --branch <tag>`).
Pre-releases and drafts are not "latest"; install one with `--ref`:

```sh
sudo bash install.sh --ref v0.1.0rc1   # a tag
sudo bash install.sh --ref main        # a branch (development code)
```

- **No release published yet** (the API answers 404): the installer warns and
  falls back to `main`. This is what happens today, until the first release
  exists.
- **The API cannot be reached** (offline, rate limit — 60 unauthenticated
  requests per hour per IP): the installer stops before creating anything and
  asks for `--ref`. It never guesses.

The installed version is `git describe` of the checkout (`blaueis-gw --version`).
The ref the checkout is on is recorded in `/opt/blaueis-gw/.update-state`,
which `blaueis-gw update --rollback` reads (§5). Re-running the installer on
an existing install moves the checkout to the resolved ref and records the
previous one the same way.

Minimum Python: **3.11**.

---

## 3. systemd layout

```
/etc/systemd/system/
  blaueis-gateway@.service           ← template unit, one service per instance
  blaueis-gateway.target             ← aggregate — starts all instances

/opt/blaueis-gw/
  packages/blaueis-core/             ← editable install
  packages/blaueis-gateway/
  venv/                              ← python virtualenv

/etc/blaueis-gw/
  gateway.yaml                       ← global defaults
  instances/
    atelier.yaml                     ← per-AC instance config
    guest.yaml                       ← another AC on another UART
```

Start / stop / status:

```sh
sudo systemctl start blaueis-gateway@<instance>
sudo systemctl enable blaueis-gateway@<instance>      # add to the target
sudo systemctl status blaueis-gateway@<instance>
sudo systemctl restart blaueis-gateway@<instance>
```

Or move all instances together:

```sh
sudo systemctl start blaueis-gateway.target
```

**Start at boot needs the target enabled.** The instance unit is
`WantedBy=blaueis-gateway.target`, and only the target is
`WantedBy=multi-user.target`. Enabling an instance links it into the
target's wants; nothing starts at boot unless the target itself is
enabled:

```sh
sudo systemctl enable blaueis-gateway.target          # once per host
systemctl is-enabled blaueis-gateway.target           # must print "enabled"
```

The installer and `blaueis-gw update` do this; `blaueis-gw status`
warns when it is missing. A gateway that runs fine until the first
reboot and then never comes back is this — a manual `systemctl start`
works without the target and hides the gap until power is lost.

**Crash protection:** the unit sets `StartLimitBurst=5` over 300 s. If the gateway crashes 5 times in 5 minutes, systemd marks it `failed` and stops auto-restarting — prevents spamming the AC with discovery handshakes during a crash loop. Resolve manually: `journalctl -t blaueis-gw-<instance> -n 200` → fix → `systemctl restart`.

---

## 4. Configuration reference

Two YAML files merged at startup; instance overrides global. Values apply to `UartProtocol` / `GatewayServer`.

### 4.1 Core keys (`gateway.yaml` or instance file)

| Key | Type | Default | Purpose |
|---|---|---|---|
| `psk` | str | `""` | Pre-shared key for AES-256-GCM. **Required** unless `--no-encrypt`. |
| `uart_port` | str | `/dev/serial0` | UART device path. |
| `uart_baud` | int | `9600` | Midea Wi-Fi dongle bus speed (don't change). |
| `ws_host` | str | `0.0.0.0` | WS bind address. |
| `ws_port` | int | `8765` | WS listen port. |
| `max_queue` | int | `16` | TX queue depth; beyond → `queue_frame` returns False. |
| `frame_spacing_ms` | int | `150` | Inter-frame sleep after each UART TX. Raised from 100 on 2026-04-14 for conservative margin (`data-analysis/midea/uart/timing-analysis.md`). |
| `stats_interval` | int | `60` | Seconds between `pi_status` broadcasts. Set to 0 to disable. |
| `fake_ip` | str | `192.168.1.100` | IP the gateway reports to the AC during ANNOUNCE. |
| `signal_level` | int | `4` | Dongle "signal level" pretended value (0–4). |
| `log_level` | str | `INFO` | Stream/journald handler level. `VERBOSE` is available (=5). |
| `device_name` | str | `Midea AC` | Human name surfaced in `pi_status` / `version`. |
| `allow_remote_update` | bool | `true` | Gate on `{"type":"update"}` WS command. |

### 4.2 Flight-recorder keys (`flight_recorder.md` §7)

| Key | Type | Default | Purpose |
|---|---|---|---|
| `debug_ring_enabled` | bool | `true` | Attach the ring handler to root at VERBOSE. |
| `debug_ring_size_mb` | int | `5` | Ring cap in MB (byte-sized eviction, not record count). |
| `slot_pool_size` | int | `8` | Max concurrent WS clients. Exhaustion → `slot_pool_full` error; no evict-oldest. |

### 4.3 Mirror keys (legacy — superseded by `subscribe` / `flight_recorder.md` §4.1)

| Key | Default | Note |
|---|---|---|
| `mirror_tx_gateway` | `false` | Mirror handshake / query-reply TX to WS clients |
| `mirror_tx_all` | `false` | Also mirror client-originated TX |

Both ignored when a client uses `subscribe` with `"include":["tx",...]` — the per-subscriber filter takes precedence.

### 4.4 Example instance file

```yaml
# /etc/blaueis-gw/instances/<instance>.yaml
psk: "xxxxxxxxxxxxxxxx"
uart_port: /dev/serial0
ws_port: 8765
device_name: "My Midea AC"
log_level: INFO
debug_ring_size_mb: 5
slot_pool_size: 8
```

Permissions: `chown blaueis-gw:blaueis-gw` + `chmod 640` — the service user needs read access, others must not.

---

## 5. Updating

All update paths follow the installer's rule: the target is the **latest
published GitHub release**; with no release published yet they fall back to
`main` with a warning (today's state, until the first release); if the
Releases API cannot be reached they change nothing and report the error.
Each applied update records the ref it left in `/opt/blaueis-gw/.update-state`
(`current_ref`, `current_sha`, `previous_ref`, `previous_sha`), which is what
`--rollback` returns to. The installed version is always `git describe` of
the checkout.

Standard path — run this on the Pi:

```sh
sudo blaueis-gw update                      # check: current vs latest release, no changes
sudo blaueis-gw update --apply              # stop instances → check out → pip → start → health check
sudo blaueis-gw update --ref v0.1.0rc1      # apply a specific tag or branch (rc tests, --ref main for dev)
sudo blaueis-gw update --rollback           # return to previous_ref from the state file
```

- `--ref` implies `--apply`. A later plain `--apply` goes back to the latest
  release; there is no sticky channel.
- `--rollback` swaps current and previous, so a second rollback goes forward
  again. It refuses when no previous ref is recorded (installs made before the
  state file existed, until their first update) — use `--ref <tag>` instead.
- The target is fetched before any instance is stopped, so a network or
  unknown-ref failure leaves the gateway running untouched. If the checkout
  fails after the stop, or `pip install` fails (the checkout then goes back to
  the previous commit and its packages are reinstalled; the state file is not
  written), the instances are started again anyway and the command exits
  non-zero. The remote update (§5.1.1) does the same and replies `ok: false`.
- Every applied update re-enables `blaueis-gateway.target` if it is not enabled.

### 5.1 Developer paths

For triggering an update without an SSH session, and for deploying code that
isn't in a release yet — uncommitted work, or a branch under test.

#### 5.1.1 Remote update (WebSocket client)

Triggers the same update from a WebSocket client, no SSH needed.

```python
from blaueis.client.ws_client import HvacClient
c = HvacClient("<gateway-host>", 8765, psk=b"...")
await c.connect()
await c._send({"type": "update", "ref": 1})
# gateway checks out the latest release, reinstalls, exits 1; systemd restarts it
```

Blocked by `allow_remote_update: false`. Always targets the latest release (or
`main` when none exists — reported as a warning in the `resolve` step of
`update_result`); a specific ref needs the local path (§5, `--ref`). Only the
instance that received the command restarts; restart other instances on the
same Pi by hand. Reply format: `ws_protocol.md` §2.8.

#### 5.1.2 Uncommitted code (SSH, for WIP)

SSH access to the Pi uses whatever key your install provisioned (PuTTY
`.ppk` keys convert to OpenSSH with `puttygen <key>.ppk -O
private-openssh -o <key>`). The default service user is `hvac`; host is
whatever you configured (e.g. `gateway.local` via mDNS, or a static IP).
`sudo` is required for anything touching `/etc/blaueis-gw/`, the
`blaueis-gw` service user's files, or `systemctl`.

For uncommitted changes:

```sh
scp -i <ssh-key> -r packages/blaueis-gateway hvac@<gateway-host>:/tmp/
ssh -i <ssh-key> hvac@<gateway-host> '
  sudo cp -r /tmp/blaueis-gateway/* /opt/blaueis-gw/packages/blaueis-gateway/
  sudo systemctl restart blaueis-gateway@<instance>
'
```

Do NOT edit files directly under `/opt/blaueis-gw/` as root — the update
paths do a forced checkout and assume a clean tree; local edits to tracked
files are discarded by the next update.

#### 5.1.3 Manual full reinstall

Re-run the installer (§2) — it moves the existing checkout to the resolved
ref, reinstalls the packages and the systemd units, and keeps
`/etc/blaueis-gw/`. Instances that are already running are restarted
(`systemctl try-restart`) so they load the new code; stopped ones are started:

```sh
sudo bash /opt/blaueis-gw/scripts/install.sh              # latest release
sudo bash /opt/blaueis-gw/scripts/install.sh --ref main   # or a specific ref
```

---

## 6. Logs & debugging

### 6.1 Journal

```sh
sudo journalctl -t blaueis-gw-<instance> -f          # live
sudo journalctl -t blaueis-gw-<instance> -n 200      # last 200 lines
sudo journalctl -t blaueis-gw-<instance> --since "10 minutes ago"
```

Default `log_level: INFO` keeps the journal clean. Packet-level detail lives in the flight recorder (§6.3), not in the journal.

### 6.2 Inline VERBOSE

For short-lived deep-dive:

```sh
sudo systemctl edit blaueis-gateway@<instance>
# add:
#   [Service]
#   Environment="BLAUEIS_LOG_LEVEL=VERBOSE"    # or restart with --verbose
```

Or run the service in the foreground:

```sh
sudo -u blaueis-gw /opt/blaueis-gw/venv/bin/python -m blaueis.gateway.server \
  --instance /etc/blaueis-gw/instances/atelier.yaml --verbose
```

### 6.3 Flight recorder (preferred)

Raise log_level only if you can't get what you need from the ring. See `flight_recorder.md` §4.4.

```python
dump = await client.request_debug_dump()
# dump = {"type": "debug_dump", "record_count": N, "size_bytes": S,
#         "jsonl": "{ts, event, hex, ...}\n{...}\n"}
```

External consumers (e.g. a Home Assistant integration, a CLI client) can pull the ring by sending `{"type":"debug_dump"}` over the WS connection and attaching the returned JSONL to a bug report. See `docs/ws_protocol.md` §2.7.

---

## 7. Troubleshooting checklist

Symptoms → where to look, in order.

### Gateway won't start

0. After a reboot or power loss, with no unit active at all:
   `systemctl is-enabled blaueis-gateway.target` — `disabled` means the
   instance was only ever started by hand. `sudo systemctl enable
   blaueis-gateway.target && sudo systemctl start blaueis-gateway.target`.
1. `sudo systemctl status blaueis-gateway@<instance>` — systemd reason.
2. `sudo journalctl -t blaueis-gw-<instance> -n 100` — startup error.
3. Check config file permissions: `ls -la /etc/blaueis-gw/instances/atelier.yaml` → must be `blaueis-gw:blaueis-gw 640`.
4. Check UART access: `sudo -u blaueis-gw ls -l /dev/serial0` → group `dialout` readable.
5. Check port free: `sudo ss -tlnp | grep 8765`.

### Gateway starts but never reaches RUNNING

1. Ring dump + look for `uart_rx` events. If none → wiring / UART device wrong.
2. If only `uart_tx` → AC not replying. Check physical wiring polarity and that the AC is powered.
3. `SILENCE_TIMEOUT` (120 s) triggers re-DISCOVER in a loop → same diagnosis.

### Client connects, no frames received

1. Did the client opt into subscription? Default is `include:["rx"]` — should work by default; explicitly `subscribe` if you modified defaults.
2. Check `slot_pool_size` — more clients than slots → `slot_pool_full`.
3. Ring dump: any `uart_rx` records? If yes but client sees nothing → WS broadcast path broken; check encryption / PSK mismatch.

### Commands don't reach the AC

1. `queue_frame` returning False → `max_queue` full (rare — see §4.1).
2. Ring dump: `uart_tx` with matching `req_id`? If no, queue drain stuck.
3. `uart_tx` present but no `reply_to` follows → AC ignored the command. Check frame validity with `blaueis.core.frame.parse_frame`.

### Too many disconnect / reconnect cycles

1. Ring dump: `ws_connect` / `ws_disconnect` rate.
2. HA side: check `homeassistant.log` for the integration — network issue, or PSK rotation mismatch.
3. Journal: look for UART errors; the protocol state machine re-handshakes silently on `REHANDSHAKE_MSGS` — frequent re-handshakes indicate bus noise.

### Timing weirdness

1. Ring has `tx_seq` and timestamps — extract to CSV, plot against `frame_spacing_ms`.
2. Cross-reference with `blaueis-hvacshark-traces/data-analysis/midea/uart/timing-analysis.md`.
3. If cadence dropped below ~70 ms post-TX: raise `frame_spacing_ms` (OEM envelope).

---

## 8. Uninstall

```sh
sudo systemctl stop blaueis-gateway@\*
sudo systemctl disable blaueis-gateway@\*
sudo rm /etc/systemd/system/blaueis-gateway@.service /etc/systemd/system/blaueis-gateway.target
sudo systemctl daemon-reload
sudo rm -rf /opt/blaueis-gw /etc/blaueis-gw
sudo userdel blaueis-gw
```

Config and data are in `/etc/blaueis-gw/`; back up before removing if you care about the PSK.
