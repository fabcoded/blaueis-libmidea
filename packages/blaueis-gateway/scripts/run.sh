#!/bin/bash
# Run HVAC gateway temporarily in foreground (Ctrl+C to stop)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_DIR="$(dirname "$SCRIPT_DIR")"

if [ ! -d "/etc/blaueis-gw/instances" ] || [ -z "$(ls /etc/blaueis-gw/instances/*.yaml 2>/dev/null)" ]; then
  echo "No instance configured. Run blaueis-configure first:"
  echo "  blaueis-configure"
  exit 1
fi

exec python3 -m blaueis.gateway.server "$@"
