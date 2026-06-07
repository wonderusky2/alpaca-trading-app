#!/usr/bin/env bash
# build_app.sh — Build Alpaca Paper Trader.app with py2app
# Usage: bash build_app.sh [--install]
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_APP="$DIR/venv_app"
VENV_SERVER="$DIR/venv_server"
APP_NAME="Alpaca Paper Trader"
DIST_APP="$DIR/dist/$APP_NAME.app"
INSTALL_DIR="/Applications/$APP_NAME.app"

echo "==> Alpaca Paper Trader — py2app build"

# ── venv_server — runtime env for server.py (uses full system Python) ─────────
SYSTEM_PYTHON="/opt/homebrew/bin/python3.11"
echo "==> Setting up server venv (system Python)…"
"$SYSTEM_PYTHON" -m venv "$VENV_SERVER"
"$VENV_SERVER/bin/pip" install --quiet --upgrade pip
"$VENV_SERVER/bin/pip" install --quiet \
  filelock requests google-generativeai pandas \
  alpaca-py flask flask-cors

# ── venv_app — py2app build env (menu bar only, no heavy deps) ───────────────
if [ ! -d "$VENV_APP" ]; then
  echo "==> Creating build venv…"
  python3 -m venv "$VENV_APP"
fi

source "$VENV_APP/bin/activate"

echo "==> Installing build dependencies…"
pip install --quiet --upgrade pip
pip install --quiet py2app rumps

# ── Clean previous build ──────────────────────────────────────────────────────
echo "==> Cleaning previous build artefacts…"
rm -rf "$DIR/build" "$DIR/dist"

# ── Build ─────────────────────────────────────────────────────────────────────
echo "==> Building $APP_NAME.app…"
cd "$DIR"
python setup_app.py py2app 2>&1

deactivate

echo "==> Build complete: $DIST_APP"

# ── Optional install ─────────────────────────────────────────────────────────
if [[ "${1:-}" == "--install" ]]; then
  echo "==> Installing to /Applications/…"
  rm -rf "$INSTALL_DIR"
  cp -r "$DIST_APP" "$INSTALL_DIR"
  echo "==> Installed: $INSTALL_DIR"
  echo "==> Launching…"
  open "$INSTALL_DIR"
else
  echo ""
  echo "To install to /Applications and launch:"
  echo "  bash build_app.sh --install"
  echo ""
  echo "To run directly from dist (no install):"
  echo "  open \"$DIST_APP\""
fi
