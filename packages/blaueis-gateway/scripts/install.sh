#!/bin/bash
# HVAC Gateway Installer
#
# Usage:
#   ./install.sh              — install deps + register systemd daemon + start
#   ./install.sh --temporary  — install deps only (run manually with ./run.sh)
#   ./install.sh --uninstall  — stop + disable + remove systemd unit
#
# IMPORTANT — Raspberry Pi UART prerequisites:
#   The serial console and Bluetooth may block UART communication.
#   Before using this gateway, ensure BOTH are disabled:
#   1. Serial console on UART: remove 'console=serial0,115200' from /boot/firmware/cmdline.txt
#   2. Serial login shell: sudo raspi-config → Interface Options → Serial Port
#      → "login shell over serial?" = No, "serial port hardware enabled?" = Yes
#   3. Bluetooth on UART (Pi 3/4/5): add 'dtoverlay=disable-bt' to /boot/firmware/config.txt
#      OR use miniuart-bt overlay to move BT to the mini UART
#   Reboot after changes. Verify with: ls -la /dev/serial0

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="hvac-gateway"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

case "${1:-install}" in
  install|--install)
    echo "=== HVAC Gateway Installation ==="
    echo ""

    # Install Python dependencies
    echo "[1/3] Installing Python dependencies..."
    pip3 install --break-system-packages websockets pyserial pyserial-asyncio cryptography 2>/dev/null \
      || pip3 install websockets pyserial pyserial-asyncio cryptography
    echo "  Done."
    echo ""

    # Run config wizard if no config exists
    if [ ! -f "$SCRIPT_DIR/gateway.conf" ]; then
      echo "[2/3] No configuration found. Running setup wizard..."
      python3 "$SCRIPT_DIR/configure.py"
    else
      echo "[2/3] Configuration found at $SCRIPT_DIR/gateway.conf"
      echo "  (Run 'python3 configure.py' to reconfigure)"
    fi
    echo ""

    # Install systemd unit
    echo "[3/3] Installing systemd service..."
    if [ "$(id -u)" -ne 0 ]; then
      echo "  Need sudo for systemd installation."
      SUDO="sudo"
    else
      SUDO=""
    fi

    $SUDO bash -c "sed 's|{{SCRIPT_DIR}}|$SCRIPT_DIR|g; s|{{USER}}|$(whoami)|g' \
      '$SCRIPT_DIR/hvac-gateway.service' > '$SERVICE_FILE'"
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "$SERVICE_NAME"
    $SUDO systemctl start "$SERVICE_NAME"
    echo "  Done."
    echo ""
    echo "=== Gateway installed and started ==="
    echo "  Check status:  systemctl status $SERVICE_NAME"
    echo "  View logs:     journalctl -u $SERVICE_NAME -f"
    echo "  Reconfigure:   python3 $SCRIPT_DIR/configure.py"
    echo "  Uninstall:     $0 --uninstall"
    ;;

  --temporary)
    echo "=== Installing dependencies only ==="
    pip3 install --break-system-packages websockets pyserial pyserial-asyncio cryptography 2>/dev/null \
      || pip3 install websockets pyserial pyserial-asyncio cryptography
    echo ""

    if [ ! -f "$SCRIPT_DIR/gateway.conf" ]; then
      echo "No configuration found. Running setup wizard..."
      python3 "$SCRIPT_DIR/configure.py"
    fi
    echo ""
    echo "Run temporarily with:"
    echo "  $SCRIPT_DIR/run.sh"
    echo "  or: python3 $SCRIPT_DIR/hvac_gateway.py --config $SCRIPT_DIR/gateway.conf"
    ;;

  --uninstall)
    echo "=== Uninstalling HVAC Gateway ==="
    if [ "$(id -u)" -ne 0 ]; then
      SUDO="sudo"
    else
      SUDO=""
    fi
    $SUDO systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    $SUDO systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    $SUDO rm -f "$SERVICE_FILE"
    $SUDO systemctl daemon-reload
    echo "  Systemd service removed."
    echo "  Config file kept at: $SCRIPT_DIR/gateway.conf"
    echo "  To fully remove: rm -rf $SCRIPT_DIR"
    ;;

  *)
    echo "Usage: $0 [--install|--temporary|--uninstall]"
    exit 1
    ;;
esac
