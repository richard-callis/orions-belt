#!/usr/bin/env bash
# Orion's Belt — Linux/macOS entry point.
# Installs (first run only) then starts the app, every time. All the actual
# install/start logic lives in install.py — this just picks a python3 and
# hands off to it.

set -euo pipefail

# Run from the directory containing this script (works from anywhere).
cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ not found on PATH. Install it from python.org." >&2
    exit 1
fi

exec "$PYTHON" install.py
