#!/usr/bin/env bash
# Build a standalone Linux launcher binary for Archive Helper.
#
# Usage:
#   ./launchers/build_linux_exe.sh
#
# The script expects a project virtual environment at .venv.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$APP_DIR/.venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing $VENV_PYTHON. Create it first with: python3 -m venv .venv" >&2
  exit 1
fi

cd "$APP_DIR"

"$VENV_PYTHON" -m pip install --upgrade pip pyinstaller

"$VENV_PYTHON" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onefile \
  --name "ArchiveHelper" \
  --paths "$APP_DIR" \
  "$APP_DIR/rip_and_encode_gui.py"

echo
printf 'Build complete.\n'
printf 'Binary: %s\n' "$APP_DIR/dist/ArchiveHelper"
