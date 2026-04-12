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


def load_all_instances():
    """Load all instance configs. Returns {name: config_dict}."""
    result = {}
    if not INSTANCES_DIR.exists():
        return result
    import yaml

    for cfg in sorted(INSTANCES_DIR.glob("*.yaml")):
        try:
            with open(cfg) as f:
                data = yaml.safe_load(f) or {}
            result[cfg.stem] = data
        except Exception:
            pass
    return result


def check_collisions(name, serial_port, ws_port):
    """Check for serial port and WebSocket port conflicts with other instances.
    Returns list of error strings. Empty = no conflicts."""
    errors = []
    for other_name, other_cfg in load_all_instances().items():
        if other_name == name:
            continue  # skip self when editing
        other_device = other_cfg.get("device", {})
        other_ws = other_cfg.get("websocket", {})
        if other_device.get("serial_port") == serial_port:
            errors.append(f"Serial port {serial_port} already used by instance '{other_name}'")
        if str(other_ws.get("port")) == str(ws_port):
            errors.append(f"WebSocket port {ws_port} already used by instance '{other_name}'")
    return errors


def write_instance_config(name, serial_port, baud, ws_port, psk, device_name, ip):
    """Write /etc/blaueis/instances/<name>.yaml using yaml.safe_dump (safe serialization)."""
    import tempfile

    import yaml

    INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    path = INSTANCES_DIR / f"{name}.yaml"

    config = {
        "schema_version": 1,
        "device": {
            "name": device_name,
            "serial_port": serial_port,
            "baud_rate": baud,
        },
        "websocket": {
            "host": "0.0.0.0",
            "port": int(ws_port),
        },
        "security": {
            "psk": psk,
        },
    }

    header = f"# Blaueis Gateway — instance: {name}\n# Service: blaueis-gateway@{name}.service\n"
    ha_block = (
        f"\n# ─── Home Assistant Integration ──────────────────────\n"
        f"# Install blaueis-ha-midea from HACS, then configure:\n"
        f"#\n"
        f"#   Host: {ip}\n"
        f"#   Port: {ws_port}\n"
        f"#   Key:  {psk}\n"
        f"#   Name: {device_name}\n"
    )

    content = header + yaml.safe_dump(config, default_flow_style=False, sort_keys=False) + ha_block

    # Atomic write: temp file + rename (survives Ctrl+C)
    fd, tmp_path = tempfile.mkstemp(dir=str(INSTANCES_DIR), suffix=".yaml.tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.rename(tmp_path, str(path))
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return path


def remove_instance(name):
    """Remove an instance: stop service, disable, delete config."""
    import subprocess

    path = INSTANCES_DIR / f"{name}.yaml"
    if not path.exists():
        print(f"  ✗ Instance '{name}' not found")
        return False

    # Show what we're about to delete
    print(f"  Instance: {name}")
    print(f"  Config:   {path}")
    print()
    print("  ⚠ The encryption key in this config will be lost.")
    print("    You will need to reconfigure the HA integration.")
    print()
    confirm = input("  Remove this instance? [y/N]: ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        return False

    # Stop and disable the systemd service
    svc = f"blaueis-gateway@{name}.service"
    subprocess.run(["systemctl", "stop", svc], capture_output=True)
    subprocess.run(["systemctl", "disable", svc], capture_output=True)

    # Delete the config
    path.unlink()
    print(f"  ✓ Removed instance '{name}'")
    return True


def set_instance_enabled(name, enabled):
    """Set enabled: true/false in an instance config."""
    import yaml

    path = INSTANCES_DIR / f"{name}.yaml"
    if not path.exists():
        print(f"  ✗ Instance '{name}' not found")
        return False

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    data["enabled"] = enabled

    import tempfile

    content = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    fd, tmp_path = tempfile.mkstemp(dir=str(INSTANCES_DIR), suffix=".yaml.tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.rename(tmp_path, str(path))
    except Exception:
        os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    state = "enabled" if enabled else "disabled"
    print(f"  ✓ Instance '{name}' {state}")
    return True


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
    parser.add_argument("--instance", help="Instance name")
    parser.add_argument("--psk", help="Pre-shared key")
    parser.add_argument("--remove", metavar="NAME", help="Remove an instance")
    parser.add_argument("--disable", metavar="NAME", help="Disable an instance")
    parser.add_argument("--enable", metavar="NAME", help="Enable an instance")
    args = parser.parse_args()

    # Handle --remove, --disable, --enable directly
    if args.remove:
        return 0 if remove_instance(args.remove) else 1
    if args.disable:
        return 0 if set_instance_enabled(args.disable, False) else 1
    if args.enable:
        return 0 if set_instance_enabled(args.enable, True) else 1

    print()
    print("─── Blaueis Gateway Setup ────────────────────────────")
    print()

    # Instance name — show existing instances if any, offer to edit or create new
    existing_data = {}  # defaults for editing (populated if user picks an existing instance)

    if not args.instance:
        existing = sorted(INSTANCES_DIR.glob("*.yaml")) if INSTANCES_DIR.exists() else []
        if existing:
            print("  Existing instances:")
            for i, cfg in enumerate(existing, 1):
                name = cfg.stem
                # Try to read device name from the config
                try:
                    import yaml

                    with open(cfg) as f:
                        data = yaml.safe_load(f) or {}
                    device = data.get("device", {}).get("name", "")
                    port = data.get("websocket", {}).get("port", "")
                    print(f"    [{i}] {name} — {device} (port {port})")
                except Exception:
                    print(f"    [{i}] {name}")
            print("    [n] Create new instance")
            print()

            choice = input("  Edit existing or create new? [n]: ").strip() or "n"
            if choice.lower() != "n":
                try:
                    idx = int(choice)
                    if 1 <= idx <= len(existing):
                        instance_name = existing[idx - 1].stem
                        print(f"  Editing: {instance_name}")
                        # Load existing values as defaults
                        with open(existing[idx - 1]) as f:
                            existing_data = yaml.safe_load(f) or {}
                    else:
                        instance_name = None
                except (ValueError, IndexError):
                    instance_name = None
            else:
                instance_name = None
        else:
            instance_name = None

        if instance_name is None:
            instance_name = ask_instance_name()
    else:
        instance_name = args.instance
        existing_data = {}
        if (INSTANCES_DIR / f"{instance_name}.yaml").exists():
            import yaml

            with open(INSTANCES_DIR / f"{instance_name}.yaml") as f:
                existing_data = yaml.safe_load(f) or {}

    # Load existing values as defaults for editing
    ex_device = existing_data.get("device", {})
    ex_ws = existing_data.get("websocket", {})
    ex_sec = existing_data.get("security", {})
    print()

    # Device name
    default_name = ex_device.get("name") or (instance_name.replace("-", " ").title() + " AC")
    device_name = ask("Device name (shown in Home Assistant)", default=default_name)
    print()

    # Serial port
    ports = detect_serial_ports()
    default_port = ex_device.get("serial_port")
    if default_port and default_port not in ports:
        ports.append(default_port)
    print("  Serial port:")
    serial_port = ask_serial_port(ports)
    baud = ex_device.get("baud_rate", 9600)
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
    psk = ask_psk(existing_psk=args.psk or ex_sec.get("psk"))
    print()

    # WebSocket port
    default_ws = str(ex_ws.get("port", 8765))
    ws_port = ask("WebSocket port", default=default_ws, validator=_validate_port)
    print()

    # Collision check — block on conflicts
    collisions = check_collisions(instance_name, serial_port, ws_port)
    if collisions:
        print("  ✗ Configuration conflicts detected:")
        for c in collisions:
            print(f"    • {c}")
        print()
        print("  Cannot create conflicting configuration. Fix the conflicts above")
        print("  or edit the other instance first.")
        return 1
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
