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

INSTALL_DIR="/opt/blaueis-gw"
CONFIG_DIR="/etc/blaueis-gw"
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

# ── Privilege setup ─────────────────────────────────
# The installer needs root for: creating system user, /opt/blaueis-gw,
# /etc/blaueis-gw, systemd units, usermod. Three scenarios:
#
#   A) Normal user with sudo → uses sudo, asks about service user
#   B) Normal user without sudo → tells them to run with sudo
#   C) Running as root (sudo bash install.sh) → works, MUST create
#      dedicated service user (gateway never runs as root)

SUDO=""
RUN_AS=""  # set later after SERVICE_USER is known — runs git/pip as the service user
MUST_CREATE_SERVICE_USER=false
INVOKING_USER=""
SUDO_KEEPALIVE_PID=""

if [ "$EUID" -eq 0 ]; then
    # Running as root — scenario C
    SUDO=""
    MUST_CREATE_SERVICE_USER=true
    INVOKING_USER="${SUDO_USER:-}"  # set by sudo, empty if su/direct root
    if [ -n "$INVOKING_USER" ]; then
        ok "Running as root (invoked by $INVOKING_USER)"
    else
        ok "Running as root"
    fi
else
    # Running as normal user — need sudo
    INVOKING_USER="$(whoami)"
    if command -v sudo &>/dev/null && sudo -v 2>/dev/null; then
        SUDO="sudo"
        ok "Sudo access confirmed for $INVOKING_USER"
        # Keep sudo alive during install
        while true; do sudo -n true; sleep 50; kill -0 "$$" || exit; done 2>/dev/null &
        SUDO_KEEPALIVE_PID=$!
        trap "kill $SUDO_KEEPALIVE_PID 2>/dev/null" EXIT
    else
        fail "No root access. Run with sudo:\n  sudo bash install.sh\n  sudo bash -c \"\$(curl -sL ...)\""
    fi
fi

# ── Parse args ──────────────────────────────────────
EXISTING_CONFIG=""
SERVICE_USER=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --config) EXISTING_CONFIG="$2"; shift 2 ;;
        --user) SERVICE_USER="$2"; shift 2 ;;
        *) warn "Unknown option: $1"; shift ;;
    esac
done

# ── Check prerequisites FIRST (fail fast before creating anything) ─
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
    $SUDO apt install python3.11 python3.11-venv"
fi
ok "Python: $($PYTHON --version)"

# pip/venv
if ! "$PYTHON" -m venv --help &>/dev/null; then
    fail "Python venv module not available. Install:
    $SUDO apt install python3.11-venv"
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

# ── Service user setup (after prereqs pass) ────────
echo ""

if [ -z "$SERVICE_USER" ]; then
    if [ "$MUST_CREATE_SERVICE_USER" = true ]; then
        SERVICE_USER="blaueis-gw"
        info "Running as root — creating dedicated 'blaueis-gw' service user (gateway never runs as root)"
    else
        echo "  Service user:"
        echo "    [1] Create 'blaueis-gw' system user (recommended)"
        echo "    [2] Run as current user ($INVOKING_USER)"
        echo ""
        read -r -p "  > " user_choice
        user_choice="${user_choice:-1}"
        echo ""
        if [ "$user_choice" = "2" ]; then
            SERVICE_USER="$INVOKING_USER"
        else
            SERVICE_USER="blaueis-gw"
        fi
    fi
fi

if [ "$SERVICE_USER" = "blaueis-gw" ]; then
    if id "blaueis-gw" &>/dev/null; then
        ok "System user 'blaueis-gw' already exists"
    else
        info "Creating system user 'blaueis-gw'..."
        $SUDO useradd --system \
            --home-dir "$INSTALL_DIR" \
            --shell /usr/sbin/nologin \
            --no-create-home \
            blaueis-gw
        ok "User blaueis-gw created (nologin shell)"
    fi
    $SUDO usermod -aG dialout blaueis-gw
    if [ -n "$INVOKING_USER" ] && [ "$INVOKING_USER" != "blaueis-gw" ] && [ "$INVOKING_USER" != "root" ]; then
        $SUDO usermod -aG blaueis-gw "$INVOKING_USER"
        ok "Added $INVOKING_USER to blaueis-gw group (for config access)"
    elif [ -z "$INVOKING_USER" ]; then
        warn "Add your normal user to the blaueis-gw group: sudo usermod -aG blaueis-gw <username>"
    fi
    ok "Service user: blaueis-gw (dedicated system user)"
else
    if ! id -nG "$SERVICE_USER" | grep -qw dialout; then
        $SUDO usermod -aG dialout "$SERVICE_USER"
    fi
    ok "Service user: $SERVICE_USER (current user)"
fi

# Set RUN_AS for git/pip commands that should run as the service user
if [ "$SERVICE_USER" != "$(whoami)" ]; then
    RUN_AS="$SUDO -u $SERVICE_USER"
else
    RUN_AS=""
fi

# ── Clone or update repo ────────────────────────────
echo ""
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Existing installation found at $INSTALL_DIR"
    cd "$INSTALL_DIR"
    $RUN_AS git pull --ff-only -q 2>/dev/null \
        || git pull --ff-only -q 2>/dev/null \
        || { git fetch origin main --depth 50 -q; git reset --hard origin/main -q; }
    ok "Repository updated"
else
    info "Cloning blaueis-libmidea to $INSTALL_DIR..."
    # Clean up if the directory exists but isn't a git repo
    # (e.g., useradd created it as a home dir in a previous failed run)
    if [ -d "$INSTALL_DIR" ]; then
        if [ "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
            warn "$INSTALL_DIR exists and is not empty — removing stale directory"
        fi
        $SUDO rm -rf "$INSTALL_DIR"
    fi
    $SUDO mkdir -p "$INSTALL_DIR"
    $SUDO chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    $RUN_AS git clone --depth 50 "$REPO_URL" "$INSTALL_DIR" 2>/dev/null \
        || git clone --depth 50 "$REPO_URL" "$INSTALL_DIR"
    $SUDO chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    ok "Cloned to $INSTALL_DIR (owner: $SERVICE_USER)"
fi

# ── Mark repo as safe for all users (vcs-versioning needs git) ──
# pip editable installs trigger setuptools_scm / vcs-versioning which
# calls git to determine the package version. If the installing user
# differs from the repo owner, git refuses with "dubious ownership".
git config --global --add safe.directory "$INSTALL_DIR"
$RUN_AS git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true

# ── Create virtualenv + install ─────────────────────
info "Setting up Python environment..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
    $RUN_AS "$PYTHON" -m venv "$INSTALL_DIR/venv" 2>/dev/null \
        || "$PYTHON" -m venv "$INSTALL_DIR/venv"
fi
$RUN_AS "$INSTALL_DIR/venv/bin/pip" install -q \
    -e packages/blaueis-core -e packages/blaueis-gateway 2>/dev/null \
    || "$INSTALL_DIR/venv/bin/pip" install -q -e packages/blaueis-core -e packages/blaueis-gateway
$SUDO chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
ok "Python packages installed"

# ── Add service user to dialout (if not already done above) ─
if [ "$SERVICE_USER" != "blaueis-gw" ]; then
    if ! id -nG "$SERVICE_USER" | grep -qw dialout; then
        $SUDO usermod -aG dialout "$SERVICE_USER"
        warn "You may need to log out and back in for dialout group change"
    else
        ok "User $SERVICE_USER is in dialout group"
    fi
fi

# ── Create config directory ─────────────────────────
$SUDO mkdir -p "$CONFIG_DIR/instances"
if [ "$SERVICE_USER" = "blaueis-gw" ]; then
    $SUDO chown -R "blaueis-gw:blaueis-gw" "$CONFIG_DIR"
    $SUDO chmod 750 "$CONFIG_DIR" "$CONFIG_DIR/instances"
    # PSK files should not be world-readable
    $SUDO chmod 640 "$CONFIG_DIR/instances/"*.yaml 2>/dev/null || true
else
    $SUDO chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"
fi

# ── Install systemd units ──────────────────────────
info "Installing systemd service..."
# Write the service file with the correct user
SERVICE_TEMPLATE="$INSTALL_DIR/packages/blaueis-gateway/systemd/blaueis-gateway@.service"
# Inject the actual service user into the template
sed "s/^User=.*/User=$SERVICE_USER/" "$SERVICE_TEMPLATE" > /tmp/blaueis-gateway@.service
$SUDO mv /tmp/blaueis-gateway@.service /etc/systemd/system/blaueis-gateway@.service
$SUDO cp "$INSTALL_DIR/packages/blaueis-gateway/systemd/blaueis-gateway.target" /etc/systemd/system/
$SUDO systemctl daemon-reload
ok "Systemd units installed (User=$SERVICE_USER)"

# ── UART warning ───────────────────────────────────
echo ""
echo -e "${YELLOW}─── Important: Serial Port Exclusivity ──────────────${NC}"
echo ""
echo "  The Blaueis gateway needs exclusive access to the UART serial port."
echo "  Make sure no other service is using it:"
echo ""
echo "    • Pi serial console (getty) — disable with:"
echo "        sudo raspi-config → Interface Options → Serial Port"
echo "        → Login shell: No, Hardware: Yes"
echo ""
echo "    • Bluetooth on Pi 3/4/5 (shares the PL011 UART) — disable with:"
echo "        Add 'dtoverlay=disable-bt' to /boot/config.txt"
echo "        sudo systemctl disable hciuart"
echo ""
echo "    • Other UART services (GPS daemons, Zigbee bridges, etc.)"
echo "        Check: sudo lsof /dev/serial0 /dev/ttyAMA0 2>/dev/null"
echo ""
echo "  If unsure, search: 'raspberry pi disable serial console uart'"
echo ""

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

# Fix config ownership (wizard may have run as root)
$SUDO chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"
if [ "$SERVICE_USER" = "blaueis-gw" ]; then
    $SUDO chmod 750 "$CONFIG_DIR" "$CONFIG_DIR/instances"
    $SUDO chmod 640 "$CONFIG_DIR/instances/"*.yaml 2>/dev/null || true
fi

# ── Enable and start ────────────────────────────────
echo ""
# Find which instances have configs — skip disabled ones
for cfg in "$CONFIG_DIR/instances/"*.yaml; do
    if [ -f "$cfg" ]; then
        name=$(basename "$cfg" .yaml)
        # Check enabled flag (default: true if not set)
        enabled=$(python3 -c "
import yaml, sys
with open('$cfg') as f:
    d = yaml.safe_load(f) or {}
print(d.get('enabled', True))
" 2>/dev/null || echo "True")
        if [ "$enabled" = "False" ]; then
            warn "Instance $name is disabled (enabled: false in config)"
            continue
        fi
        $SUDO systemctl enable "blaueis-gateway@${name}" 2>/dev/null
        $SUDO systemctl start "blaueis-gateway@${name}" 2>/dev/null
        ok "Started blaueis-gateway@${name}"
    fi
done

# ── Install helper scripts ──────────────────────────
$SUDO ln -sf "$INSTALL_DIR/scripts/blaueis-configure" /usr/local/bin/blaueis-gw-configure
$SUDO ln -sf "$INSTALL_DIR/scripts/blaueis-update" /usr/local/bin/blaueis-gw-update
$SUDO chmod +x /usr/local/bin/blaueis-gw-configure /usr/local/bin/blaueis-gw-update

# ── Done ────────────────────────────────────────────
echo ""
echo -e "${GREEN}─── Blaueis Gateway Installed ────────────────────────${NC}"
echo ""
echo "  Commands:"
echo "    systemctl status blaueis-gateway@<name>    # check status"
echo "    blaueis-gw-configure                          # add/edit instance"
echo "    blaueis-gw-update                             # check for updates"
echo ""
echo "  Config: $CONFIG_DIR/"
echo "  Logs:   journalctl -u 'blaueis-gateway@*' -f"
echo ""
