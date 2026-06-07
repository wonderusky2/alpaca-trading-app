#!/usr/bin/env bash
# setup.sh — One-time install. Run once, then launchd takes over.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "▶ Creating venv..."
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"
echo "▶ Making scripts executable..."
chmod +x "$DIR/run.sh" "$DIR/trader.py"
echo "▶ Installing launchd agent..."
cp "$DIR/com.johnshelest.robinhoodtrader.plist" \
   ~/Library/LaunchAgents/com.johnshelest.robinhoodtrader.plist
launchctl load ~/Library/LaunchAgents/com.johnshelest.robinhoodtrader.plist
echo ""
echo "✅ Done. Trader runs every 5 min. Logs at $DIR/trader.log"
echo ""
echo "Useful commands:"
echo "  Tail logs:   tail -f $DIR/trader.log"
echo "  Stop trader: launchctl unload ~/Library/LaunchAgents/com.johnshelest.robinhoodtrader.plist"
echo "  Start again: launchctl load ~/Library/LaunchAgents/com.johnshelest.robinhoodtrader.plist"
echo "  Test run now: bash $DIR/run.sh"
