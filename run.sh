#!/usr/bin/env bash
# run.sh — Load secrets then execute trader.py
# Called by launchd every 5 minutes.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv/bin/python"
LOG="$DIR/trader.log"

# 1. Load Telegram creds from ivsentinel env
source /Users/johnshelest/Code/.ivsentinel.env 2>/dev/null || true

# 2. Load Gemini key from Conjur
eval "$(cd /Users/johnshelest/Code/conjur-secret-manager && npm run --silent export 2>/dev/null)" || true

# 3. Run trader
echo "--- $(date) ---" >> "$LOG"
"$VENV" "$DIR/trader.py" >> "$LOG" 2>&1
