#!/bin/bash
# Launch Archive Helper GUI on macOS
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -x "$APP_DIR/.venv/bin/python3" ]; then
  "$APP_DIR/.venv/bin/python3" "$APP_DIR/rip_and_encode_gui.py"
elif [ -x "$APP_DIR/.venv/bin/python" ]; then
  "$APP_DIR/.venv/bin/python" "$APP_DIR/rip_and_encode_gui.py"
else
  python3 "$APP_DIR/rip_and_encode_gui.py"
fi
