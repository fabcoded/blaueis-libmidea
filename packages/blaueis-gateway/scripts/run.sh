#!/bin/bash
# Run HVAC gateway temporarily in foreground (Ctrl+C to stop)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$SCRIPT_DIR/gateway.conf" ]; then
  echo "No gateway.conf found. Run configure.py first:"
  echo "  python3 $SCRIPT_DIR/configure.py"
  exit 1
fi

exec python3 "$SCRIPT_DIR/hvac_gateway.py" --config "$SCRIPT_DIR/gateway.conf" "$@"
