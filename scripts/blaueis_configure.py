#!/usr/bin/env python3
"""Blaueis Gateway — interactive setup wizard.

Creates or updates an instance config in /etc/blaueis/instances/<name>.yaml.
Also creates the global config /etc/blaueis/gateway.yaml if it doesn't exist.

Usage:
    blaueis-configure                           # interactive
    blaueis-configure --instance bedroom        # add/edit specific instance
    blaueis-configure --instance bedroom --psk "mySecretKey123"  # with existing key
"""

import argparse
import hashlib
import os
import re
import secrets
import socket
import string
from pathlib import Path

CONFIG_DIR = Path("/etc/blaueis")
INSTANCES_DIR = CONFIG_DIR / "instances"
GLOBAL_CONFIG = CONFIG_DIR / "gateway.yaml"

PSK_ALPHABET = string.ascii_letters + string.digits  # a-zA-Z0-9
PSK_LENGTH = 44  # ≥256 bits entropy from 62-char alphabet
PSK_MIN_LENGTH = 12

INSTANCE_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$")


def detect_serial_ports():
    """Find available serial ports."""
    ports = []
    for path in ["/dev/serial0", "/dev/ttyAMA0", "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0"]:
        if os.path.exists(path):
            ports.append(path)
    return ports


def detect_ip():
    """Get the Pi's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"


def generate_psk():
    """Generate a 44-char alphanumeric key (≥256 bits entropy)."""
    return "".join(secrets.choice(PSK_ALPHABET) for _ in range(PSK_LENGTH))


def validate_psk(psk):
    """Validate PSK length. Returns the string or raises."""
    if len(psk) < PSK_MIN_LENGTH:
        raise ValueError(f"Key too short ({len(psk)} chars). Minimum {PSK_MIN_LENGTH}.")
    return psk


def psk_to_key(psk):
    """SHA-256 the PSK string into a 32-byte AES-256 key."""
    return hashlib.sha256(psk.encode("utf-8")).digest()


def ask(prompt, default=None, validator=None):
    """Ask the user for input with optional default and validation."""
    while True:
        if default:
            raw = input(f"  {prompt} [{default}]: ").strip()
            if not raw:
                raw = default
        else:
            raw = input(f"  {prompt}: ").strip()

        if validator:
            try:
                return validator(raw)
            except ValueError as e:
                print(f"  ✗ {e}")
                continue
        return raw


def ask_serial_port(ports):
    """Ask user to select or enter a serial port."""
    if ports:
        print("  Available serial ports:")
        for i, p in enumerate(ports, 1):
            print(f"    [{i}] {p}")
        print(f"    [{len(ports) + 1}] Enter custom path")
        print()

        while True:
            choice = input("  Select port [1]: ").strip() or "1"
            try:
                idx = int(choice)
                if 1 <= idx <= len(ports):
                    return ports[idx - 1]
                if idx == len(ports) + 1:
                    return input("  Custom serial port path: ").strip()
            except ValueError:
                pass
            print("  ✗ Invalid choice")
    else:
        return input("  Serial port path (e.g. /dev/serial0): ").strip()


def ask_instance_name(default=None):
    """Ask for a valid instance name."""

    def validate(name):
        if not INSTANCE_NAME_RE.match(name):
            raise ValueError("Use lowercase letters, digits, hyphens only (e.g. living-room)")
        if (INSTANCES_DIR / f"{name}.yaml").exists():
            confirm = input(f"  Instance '{name}' already exists. Overwrite? [y/N]: ").strip().lower()
            if confirm != "y":
                raise ValueError("Cancelled")
        return name

    return ask("Instance name (e.g. living-room, bedroom)", default=default, validator=validate)


def ask_psk(existing_psk=None):
    """Ask for PSK or auto-generate."""
    if existing_psk:
        try:
            validate_psk(existing_psk)
            print(f"  Using provided key ({len(existing_psk)} chars)")
            return existing_psk
        except ValueError as e:
            print(f"  ✗ Provided key rejected: {e}")

    print("  Enter a passphrase (min 12 characters),")
    print("  or press Enter to auto-generate:")
    raw = input("  > ").strip()

    if not raw:
        psk = generate_psk()
        print(f"  Auto-generated: {psk}")
        return psk

    validate_psk(raw)
    return raw


def write_global_config():
    """Write /etc/blaueis/gateway.yaml if it doesn't exist."""
    if GLOBAL_CONFIG.exists():
        return

    content = """# Blaueis Gateway — global configuration
schema_version: 1

logging:
  level: INFO

remote_management:
  allow_remote_control: true
  update_channel: stable
  update_check_interval: 86400
  # Updates are NEVER applied automatically. The check reports to HA;
  # the user triggers the actual update via HA UI or blaueis-update.
"""
    GLOBAL_CONFIG.write_text(content)


def write_instance_config(name, serial_port, baud, ws_port, psk, device_name, ip):
    """Write /etc/blaueis/instances/<name>.yaml."""
    INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    path = INSTANCES_DIR / f"{name}.yaml"

    content = f"""# Blaueis Gateway — instance: {name}
# Service: blaueis-gateway@{name}.service
schema_version: 1

device:
  name: {device_name}
  serial_port: {serial_port}
  baud_rate: {baud}

websocket:
  host: 0.0.0.0
  port: {ws_port}

security:
  psk: {psk}

# ─── Home Assistant Integration ────���─────────────────
# Install blaueis-ha-midea from HACS, then configure:
#
#   Host: {ip}
#   Port: {ws_port}
#   Key:  {psk}
#   Name: {device_name}
"""
    path.write_text(content)
    return path


def test_uart(serial_port, baud):
    """Quick UART test — try to open the port."""
    try:
        import serial

        ser = serial.Serial(serial_port, baud, timeout=1)
        ser.close()
        return True
    except ImportError:
        # pyserial not installed yet during initial setup — skip test
        return None
    except Exception as e:
        print(f"  ✗ UART test failed: {e}")
        return False


def _validate_port(p):
    port = int(p)
    if not 1024 <= port <= 65535:
        raise ValueError("Port must be 1024-65535")
    return str(port)


def main():
    parser = argparse.ArgumentParser(description="Blaueis Gateway Setup Wizard")
    parser.add_argument("--instance", help="Instance name (skip interactive prompt)")
    parser.add_argument("--psk", help="Pre-shared key (skip interactive prompt)")
    args = parser.parse_args()

    print()
    print("─── Blaueis Gateway Setup ────────────────────────────")
    print()

    # Instance name
    instance_name = args.instance or ask_instance_name()
    print()

    # Device name
    device_name = ask("Device name (shown in Home Assistant)", default=instance_name.replace("-", " ").title() + " AC")
    print()

    # Serial port
    ports = detect_serial_ports()
    print("  Serial port:")
    serial_port = ask_serial_port(ports)
    baud = 9600
    print()

    # Test UART
    uart_ok = test_uart(serial_port, baud)
    if uart_ok is True:
        print(f"  ✓ UART test passed ({serial_port} @ {baud})")
    elif uart_ok is False:
        print("  ✗ UART test failed — check port and permissions")
        print("    Make sure you're in the dialout group: sudo usermod -aG dialout $(whoami)")
    else:
        print("  ○ UART test skipped (pyserial not yet installed)")
    print()

    # PSK
    print("  Encryption key:")
    psk = ask_psk(existing_psk=args.psk)
    print()

    # WebSocket port
    ws_port = ask("WebSocket port", default="8765", validator=_validate_port)
    print()

    # Detect IP
    ip = detect_ip()

    # Write configs
    write_global_config()
    config_path = write_instance_config(instance_name, serial_port, baud, ws_port, psk, device_name, ip)

    # Summary
    print("─── Configuration Saved ──────────────────────────────")
    print()
    print(f"  Instance: {instance_name}")
    print(f"  Config:   {config_path}")
    print(f"  Gateway:  {ip}:{ws_port}")
    print(f"  Serial:   {serial_port} @ {baud}")
    print()
    print("─── Home Assistant Setup ─────────────────────────────")
    print()
    print("  Install blaueis-ha-midea from HACS, then configure:")
    print()
    print(f"    Host: {ip}")
    print(f"    Port: {ws_port}")
    print(f"    Key:  {psk}")
    print(f"    Name: {device_name}")
    print()
    print(f"  These values are also in {config_path}")
    print()


if __name__ == "__main__":
    main()
