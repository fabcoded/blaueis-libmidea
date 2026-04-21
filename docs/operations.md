# Blaueis Gateway — Operations Guide

> Install, configure, update, debug, troubleshoot. For the protocol wire-side
> reference see `ws_protocol.md`; for the flight-recorder design see
> `flight_recorder.md`; for architecture see `architecture.md`.

---

## 1. Install

One-line on a Raspberry Pi (Bookworm / Bullseye):

```sh
bash -c "$(curl -sL https://raw.githubusercontent.com/fabcoded/blaueis-libmidea/main/scripts/install.sh)"
```

The installer (`scripts/install.sh`):

- Requires root (asks for `sudo`).
- Creates system user `blaueis-gw`, directories `/opt/blaueis-gw`, `/etc/blaueis-gw`.
- `git clone`s this repo into `/opt/blaueis-gw`, creates a venv, `pip install -e` for `blaueis-core` + `blaueis-gateway`.
- Installs `blaueis-gateway@.service` into systemd.
- Adds the service user to the `dialout` group (for `/dev/serial0`).
- Does **not** start a service — you must place a config and enable an instance.

Minimum Python: **3.11**.

---

## 2. systemd layout

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
sudo systemctl enable blaueis-gateway@<instance>      # start at boot
sudo systemctl status blaueis-gateway@<instance>
sudo systemctl restart blaueis-gateway@<instance>
```

Or move all instances together:

```sh
sudo systemctl start blaueis-gateway.target
```

**Crash protection:** the unit sets `StartLimitBurst=5` over 300 s. If the gateway crashes 5 times in 5 minutes, systemd marks it `failed` and stops auto-restarting — prevents spamming the AC with discovery handshakes during a crash loop. Resolve manually: `journalctl -t blaueis-gw-<instance> -n 200` → fix → `systemctl restart`.

---

## 3. Configuration reference

Two YAML files merged at startup; instance overrides global. Values apply to `UartProtocol` / `GatewayServer`.

### 3.1 Core keys (`gateway.yaml` or instance file)

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

### 3.2 Flight-recorder keys (`flight_recorder.md` §7)

| Key | Type | Default | Purpose |
|---|---|---|---|
| `debug_ring_enabled` | bool | `true` | Attach the ring handler to root at VERBOSE. |
| `debug_ring_size_mb` | int | `5` | Ring cap in MB (byte-sized eviction, not record count). |
| `slot_pool_size` | int | `8` | Max concurrent WS clients. Exhaustion → `slot_pool_full` error; no evict-oldest. |

### 3.3 Mirror keys (legacy — superseded by `subscribe`/§4.1)

| Key | Default | Note |
|---|---|---|
| `mirror_tx_gateway` | `false` | Mirror handshake / query-reply TX to WS clients |
| `mirror_tx_all` | `false` | Also mirror client-originated TX |

Both ignored when a client uses `subscribe` with `"include":["tx",...]` — the per-subscriber filter takes precedence.

### 3.4 Example instance file

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

## 4. Updating

### 4.1 Remote update (WS client, preferred)

Deploys committed code that is pushed to the remote.

```python
from blaueis.client.ws_client import HvacClient
c = HvacClient("<gateway-host>", 8765, psk=b"...")
await c.connect()
await c._send({"type": "update", "ref": 1})
# gateway git pulls, reinstalls, exits 1; systemd restarts it
```

Blocked by `allow_remote_update: false`. Requires remote commit to exist — the gateway does `git pull --ff-only`.

### 4.2 Local update (SSH, for WIP code)

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
path (`git pull`) assumes a clean checkout.

### 4.3 Manual full reinstall

```sh
ssh -i <ssh-key> hvac@<gateway-host>
cd /opt/blaueis-gw && sudo git pull
sudo -u blaueis-gw /opt/blaueis-gw/venv/bin/pip install -e packages/blaueis-core -e packages/blaueis-gateway
sudo systemctl restart blaueis-gateway@<instance>
```

---

## 5. Logs & debugging

### 5.1 Journal

```sh
sudo journalctl -t blaueis-gw-<instance> -f          # live
sudo journalctl -t blaueis-gw-<instance> -n 200      # last 200 lines
sudo journalctl -t blaueis-gw-<instance> --since "10 minutes ago"
```

Default `log_level: INFO` keeps the journal clean. Packet-level detail lives in the flight recorder (§5.3), not in the journal.

### 5.2 Inline VERBOSE

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

### 5.3 Flight recorder (preferred)

Raise log_level only if you can't get what you need from the ring. See `flight_recorder.md` §4.4.

```python
dump = await client.request_debug_dump()
# dump = {"type": "debug_dump", "record_count": N, "size_bytes": S,
#         "jsonl": "{ts, event, hex, ...}\n{...}\n"}
```

External consumers (e.g. a Home Assistant integration, a CLI client) can pull the ring by sending `{"type":"debug_dump"}` over the WS connection and attaching the returned JSONL to a bug report. See `docs/ws_protocol.md` §2.7.

---

## 6. Troubleshooting checklist

Symptoms → where to look, in order.

### Gateway won't start

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

1. `queue_frame` returning False → `max_queue` full (rare — see §3.1).
2. Ring dump: `uart_tx` with matching `req_id`? If no, queue drain stuck.
3. `uart_tx` present but no `reply_to` follows → AC ignored the command. Check frame validity with `blaueis.core.frame.parse_frame`.

### Too many disconnect / reconnect cycles

1. Ring dump: `ws_connect` / `ws_disconnect` rate.
2. HA side: check `homeassistant.log` for the integration — network issue, or PSK rotation mismatch.
3. Journal: look for UART errors; the protocol state machine re-handshakes silently on `REHANDSHAKE_MSGS` — frequent re-handshakes indicate bus noise.

### Timing weirdness

1. Ring has `tx_seq` and timestamps — extract to CSV, plot against `frame_spacing_ms`.
2. Cross-reference with `HVAC-shark-dumps/data-analysis/midea/uart/timing-analysis.md`.
3. If cadence dropped below ~70 ms post-TX: raise `frame_spacing_ms` (OEM envelope).

---

## 7. Uninstall

```sh
sudo systemctl stop blaueis-gateway@\*
sudo systemctl disable blaueis-gateway@\*
sudo rm /etc/systemd/system/blaueis-gateway@.service /etc/systemd/system/blaueis-gateway.target
sudo systemctl daemon-reload
sudo rm -rf /opt/blaueis-gw /etc/blaueis-gw
sudo userdel blaueis-gw
```

Config and data are in `/etc/blaueis-gw/`; back up before removing if you care about the PSK.
