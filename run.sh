#!/usr/bin/env bash
# Orion's Belt — single entry point: installs if needed, then starts.
# Run this whether it's the first launch or the hundredth — no need to
# remember to run setup.sh separately.

set -euo pipefail

# Run from the directory containing this script (works from anywhere).
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found — running first-time setup..."
    echo ""
    bash ./setup.sh
fi

source .venv/bin/activate
exec python launch.py
