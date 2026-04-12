#!/bin/bash
# Blaueis Gateway Installer
#
# Install:
#   bash -c "$(curl -sL https://raw.githubusercontent.com/fabcoded/blaueis-libmidea/main/scripts/install.sh)"
#
# Or download first:
#   wget -O /tmp/install.sh https://raw.githubusercontent.com/fabcoded/blaueis-libmidea/main/scripts/install.sh
#   bash /tmp/install.sh
#
# With existing config:
#   bash install.sh --config /path/to/existing.yaml
#
set -e

INSTALL_DIR="/opt/blaueis"
CONFIG_DIR="/etc/blaueis"
REPO_URL="https://github.com/fabcoded/blaueis-libmidea.git"
MIN_PYTHON="3.11"

# ── Colors ──────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

echo ""
echo -e "${BLUE}─── Blaueis Gateway Installer ───────────────────────${NC}"
echo ""

# ── Sudo check ─────────────────────────────────────
# The installer needs sudo for: creating /opt/blaueis, /etc/blaueis,
# installing systemd units, adding user to dialout group.
# Cache credentials once upfront so we don't prompt mid-install.
if [ "$EUID" -eq 0 ]; then
    fail "Do not run as root. Run as your normal user — the script uses sudo where needed."
fi

if ! command -v sudo &>/dev/null; then
    fail "sudo not found. Install it first: apt install sudo"
fi

info "This installer needs sudo for system setup (directories, systemd, groups)."
info "You may be prompted for your password once."
echo ""
if ! sudo -v; then
    fail "Could not obtain sudo. Check your permissions."
fi

# Keep sudo alive during the install (refresh every 50s in background)
while true; do sudo -n true; sleep 50; kill -0 "$$" || exit; done 2>/dev/null &
SUDO_KEEPALIVE_PID=$!
trap "kill $SUDO_KEEPALIVE_PID 2>/dev/null" EXIT

# ── Parse args ──────────────────────────────────────
EXISTING_CONFIG=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --config) EXISTING_CONFIG="$2"; shift 2 ;;
        *) warn "Unknown option: $1"; shift ;;
    esac
done

# ── Check prerequisites ─────────────────────────────
info "Checking prerequisites..."

# Python version
PYTHON=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    fail "Python >= $MIN_PYTHON not found. Install it first:
    sudo apt install python3.11 python3.11-venv"
fi
ok "Python: $($PYTHON --version)"

# pip/venv
if ! "$PYTHON" -m venv --help &>/dev/null; then
    fail "Python venv module not available. Install:
    sudo apt install python3.11-venv"
fi

# git
if ! command -v git &>/dev/null; then
    fail "git not found. Install: sudo apt install git"
fi
ok "git: $(git --version | head -1)"

# Serial port check
SERIAL_PORTS=()
for port in /dev/serial0 /dev/ttyAMA0 /dev/ttyUSB0 /dev/ttyUSB1 /dev/ttyACM0; do
    if [ -e "$port" ]; then
        SERIAL_PORTS+=("$port")
    fi
done
if [ ${#SERIAL_PORTS[@]} -eq 0 ]; then
    warn "No serial ports found. The wizard will ask for the port path."
else
    ok "Serial ports: ${SERIAL_PORTS[*]}"
fi

# ── Clone or update repo ────────────────────────────
echo ""
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Existing installation found at $INSTALL_DIR"
    cd "$INSTALL_DIR"
    git fetch --tags -q
    ok "Repository updated"
else
    info "Cloning blaueis-libmidea to $INSTALL_DIR..."
    sudo mkdir -p "$INSTALL_DIR"
    sudo chown "$(whoami):$(whoami)" "$INSTALL_DIR"
    git clone --depth 50 "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    ok "Cloned to $INSTALL_DIR"
fi

# ── Create virtualenv + install ─────────────────────
info "Setting up Python environment..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
    "$PYTHON" -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install -q -e packages/blaueis-core -e packages/blaueis-gateway
ok "Python packages installed"

# ── Add user to dialout group ───────────────────────
if ! groups | grep -q dialout; then
    info "Adding $(whoami) to dialout group for serial port access..."
    sudo usermod -aG dialout "$(whoami)"
    warn "You may need to log out and back in for group change to take effect"
else
    ok "User $(whoami) is in dialout group"
fi

# ── Create config directory ─────────────────────────
sudo mkdir -p "$CONFIG_DIR/instances"
sudo chown -R "$(whoami):$(whoami)" "$CONFIG_DIR"

# ── Install systemd units ──────────────────────────
info "Installing systemd service..."
sudo cp "$INSTALL_DIR/packages/blaueis-gateway/systemd/blaueis-gateway@.service" /etc/systemd/system/
sudo cp "$INSTALL_DIR/packages/blaueis-gateway/systemd/blaueis-gateway.target" /etc/systemd/system/
sudo systemctl daemon-reload
ok "Systemd units installed"

# ── Run wizard ──────────────────────────────────────
echo ""
if [ -n "$EXISTING_CONFIG" ]; then
    info "Importing config from $EXISTING_CONFIG..."
    cp "$EXISTING_CONFIG" "$CONFIG_DIR/instances/"
    INSTANCE_NAME=$(basename "$EXISTING_CONFIG" .yaml)
    ok "Imported as instance: $INSTANCE_NAME"
else
    "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/scripts/blaueis-configure"
fi

# ── Enable and start ────────────────────────────────
echo ""
# Find which instances have configs
for cfg in "$CONFIG_DIR/instances/"*.yaml; do
    if [ -f "$cfg" ]; then
        name=$(basename "$cfg" .yaml)
        sudo systemctl enable "blaueis-gateway@${name}" 2>/dev/null
        sudo systemctl start "blaueis-gateway@${name}" 2>/dev/null
        ok "Started blaueis-gateway@${name}"
    fi
done

# ── Install helper scripts ──────────────────────────
sudo ln -sf "$INSTALL_DIR/scripts/blaueis-configure" /usr/local/bin/blaueis-configure
sudo ln -sf "$INSTALL_DIR/scripts/blaueis-update" /usr/local/bin/blaueis-update
sudo chmod +x /usr/local/bin/blaueis-configure /usr/local/bin/blaueis-update

# ── Done ────────────────────────────────────────────
echo ""
echo -e "${GREEN}─── Blaueis Gateway Installed ────────────────────────${NC}"
echo ""
echo "  Commands:"
echo "    systemctl status blaueis-gateway@<name>    # check status"
echo "    blaueis-configure                          # add/edit instance"
echo "    blaueis-update                             # check for updates"
echo ""
echo "  Config: $CONFIG_DIR/"
echo "  Logs:   journalctl -u 'blaueis-gateway@*' -f"
echo ""
