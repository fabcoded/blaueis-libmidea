#!/usr/bin/env python3
"""Interactive configuration wizard for the HVAC gateway.

Usage:
    python3 configure.py                # Full interactive setup
    python3 configure.py --new-key      # Regenerate PSK only
    python3 configure.py --show         # Show current config (mask PSK)
"""

import argparse
import configparser
import glob
import os
import sys
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "gateway.conf"

DEFAULTS = {
    "ws_host": "0.0.0.0",
    "ws_port": "8765",
    "uart_port": "/dev/serial0",
    "uart_baud": "9600",
    "max_queue": "8",
    "frame_spacing_ms": "100",
    "stats_interval": "60",
    "fake_ip": "192.168.1.100",
    "signal_level": "4",
    "log_level": "INFO",
}


def prompt(label: str, default: str = "") -> str:
    """Prompt user for input with a default value."""
    suffix = f" [{default}]" if default else ""
    value = input(f"  {label}{suffix}: ").strip()
    return value or default


def detect_serial_ports() -> list[str]:
    """List available serial ports. Prefers /dev/serial0 (Pi symlink to correct UART)."""
    # /dev/serial0 is the recommended Pi UART — always correct regardless of model
    preferred = ["/dev/serial0"]
    candidates = [
        "/dev/ttyAMA0",
        "/dev/ttyS0",
        "/dev/ttyAMA1",
        "/dev/ttyUSB0",
        "/dev/ttyUSB1",
        "/dev/ttyACM0",
    ]
    found = [p for p in preferred if os.path.exists(p)]
    found.extend(p for p in candidates if os.path.exists(p) and p not in found)
    found.extend(glob.glob("/dev/serial/by-id/*"))
    return found or ["/dev/serial0"]


def generate_psk_hex() -> str:
    """Generate a new 32-byte PSK as hex string."""
    return os.urandom(32).hex()


def run_wizard() -> dict:
    """Run the interactive configuration wizard."""
    print("\n=== HVAC Gateway Configuration ===\n")
    cfg = {}

    # 1. Network
    print("[1/5] Network")
    cfg["ws_host"] = prompt("WebSocket listen address", DEFAULTS["ws_host"])
    cfg["ws_port"] = prompt("WebSocket port", DEFAULTS["ws_port"])
    print()

    # 2. UART
    print("[2/5] UART Serial")
    ports = detect_serial_ports()
    if ports:
        print(f"  Available ports: {', '.join(ports)}")
    cfg["uart_port"] = prompt("UART port", ports[0] if ports else DEFAULTS["uart_port"])
    cfg["uart_baud"] = prompt("Baud rate", DEFAULTS["uart_baud"])
    print()

    # 3. PSK
    print("[3/5] Pre-Shared Key")
    if CONFIG_FILE.exists():
        existing = configparser.ConfigParser()
        existing.read(CONFIG_FILE)
        has_psk = existing.has_option("gateway", "psk")
    else:
        has_psk = False

    if has_psk:
        regen = prompt("Regenerate PSK? (y/N)", "n").lower()
        if regen == "y":
            cfg["psk"] = generate_psk_hex()
            print(f"  \u2713 New PSK: {cfg['psk']}")
            print("  \u26a0 Copy this key to your client configuration!")
        else:
            cfg["psk"] = existing.get("gateway", "psk")
            print("  Keeping existing PSK")
    else:
        cfg["psk"] = generate_psk_hex()
        print(f"  \u2713 PSK: {cfg['psk']}")
        print("  \u26a0 Copy this key to your client configuration!")
    print()

    # 4. Fake network status
    print("[4/5] Fake Network Status (what the AC display shows)")
    cfg["fake_ip"] = prompt("IP address to report", DEFAULTS["fake_ip"])
    cfg["signal_level"] = prompt("WiFi signal level (1-4)", DEFAULTS["signal_level"])
    print()

    # 5. Options
    print("[5/5] Options")
    cfg["max_queue"] = prompt("TX queue depth", DEFAULTS["max_queue"])
    cfg["frame_spacing_ms"] = prompt("Inter-frame spacing ms", DEFAULTS["frame_spacing_ms"])
    cfg["stats_interval"] = prompt("Pi stats interval seconds", DEFAULTS["stats_interval"])
    cfg["log_level"] = prompt("Log level (DEBUG/INFO/WARNING)", DEFAULTS["log_level"])
    print()

    return cfg


def write_config(cfg: dict):
    """Write configuration to gateway.conf."""
    config = configparser.ConfigParser()
    config["gateway"] = cfg
    with open(CONFIG_FILE, "w") as f:
        config.write(f)
    print(f"Wrote {CONFIG_FILE}")


def show_config():
    """Display current configuration, masking the PSK."""
    if not CONFIG_FILE.exists():
        print(f"No configuration file found at {CONFIG_FILE}")
        return
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    print(f"\n=== Current Configuration ({CONFIG_FILE}) ===\n")
    for key, value in config["gateway"].items():
        if key == "psk":
            print(f"  {key} = {value[:8]}...{value[-8:]}")
        else:
            print(f"  {key} = {value}")
    print()


def main():
    parser = argparse.ArgumentParser(description="HVAC Gateway Configuration Wizard")
    parser.add_argument("--new-key", action="store_true", help="Regenerate PSK only")
    parser.add_argument("--show", action="store_true", help="Show current config")
    args = parser.parse_args()

    if args.show:
        show_config()
        return

    if args.new_key:
        if not CONFIG_FILE.exists():
            print("No config file exists. Run without --new-key for full setup.")
            sys.exit(1)
        config = configparser.ConfigParser()
        config.read(CONFIG_FILE)
        new_psk = generate_psk_hex()
        config["gateway"]["psk"] = new_psk
        with open(CONFIG_FILE, "w") as f:
            config.write(f)
        print(f"\u2713 New PSK: {new_psk}")
        print("\u26a0 Copy this key to your client configuration!")
        return

    cfg = run_wizard()
    write_config(cfg)


if __name__ == "__main__":
    main()
