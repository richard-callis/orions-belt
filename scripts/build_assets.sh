#!/usr/bin/env bash
# Rebuild vendored frontend assets (Tailwind CSS).
# Run this after editing templates or the Tailwind config.
#
# Requirements: node + npm in PATH
#   npm install (installs tailwindcss v3 locally)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATIC="$REPO_ROOT/app/static"
SCRIPTS="$REPO_ROOT/scripts"

echo "==> Installing Tailwind (if needed)…"
cd "$SCRIPTS"
npm install --prefer-offline 2>/dev/null || npm install --legacy-peer-deps

echo "==> Building Tailwind CSS…"
./node_modules/.bin/tailwindcss \
  -c "$SCRIPTS/tailwind.config.js" \
  -i "$SCRIPTS/tailwind.input.css" \
  -o "$STATIC/css/tailwind.css" \
  --minify

echo "==> Done. Output: app/static/css/tailwind.css ($(du -sh "$STATIC/css/tailwind.css" | cut -f1))"
