"""
notify.py — Trade alerts.

macOS  → iMessage via AppleScript (original behaviour)
Linux  → stdout log + optional HTTP webhook (NOTIFY_WEBHOOK_URL env var)

The webhook receives a POST with JSON:  {"text": "<message>"}
Point it at any receiver: ntfy.sh, Slack incoming webhook, custom endpoint, etc.
"""
from __future__ import annotations
import json
import os
import sys

IMESSAGE_TO = "+13126232322"


# ── Platform dispatch ─────────────────────────────────────────────────────────

def _send(msg: str) -> None:
    clean = (
        msg
        .replace("<b>", "").replace("</b>", "")
        .replace("<i>", "").replace("</i>", "")
    )
    if sys.platform == "darwin":
        _send_imessage(clean)
    else:
        _send_linux(clean)


def _send_imessage(msg: str) -> None:
    import subprocess
    script = (
        f'tell application "Messages"\n'
        f'  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy "{IMESSAGE_TO}" of targetService\n'
        f'  send "{_escape(msg)}" to targetBuddy\n'
        f'end tell'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True, capture_output=True, timeout=10,
        )
    except Exception as e:
        print(f"[notify] iMessage failed: {e}\n{msg}", flush=True)


def _send_linux(msg: str) -> None:
    """Log to stdout (captured by K8s) + POST to webhook if configured."""
    print(f"[notify] {msg}", flush=True)

    webhook_url = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return
    try:
        import urllib.request
        data = json.dumps({"text": msg}).encode()
        req  = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[notify] webhook POST failed: {e}", flush=True)


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── Public API ────────────────────────────────────────────────────────────────

def send(msg: str) -> None:
    _send(msg)

def trade_buy(sym: str, qty: int, price: float, score: int, regime: str) -> None:
    value = qty * price
    _send(
        f"🟢 BUY {sym}\n"
        f"{qty} shares @ ${price:.2f}  (${value:,.0f})\n"
        f"Score: {score}/100 · Regime: {regime}"
    )

def trade_sell(sym: str, qty: int, price: float, pnl: float, reason: str) -> None:
    emoji = "🔴" if pnl < 0 else "💰"
    pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
    _send(
        f"{emoji} SELL {sym}\n"
        f"{qty} shares @ ${price:.2f}  P&L: {pnl_str}\n"
        f"Reason: {reason}"
    )

def kill_switch(daily_pnl: float, reason: str) -> None:
    _send(
        f"🚨 KILL SWITCH TRIGGERED\n"
        f"Daily P&L: ${daily_pnl:,.2f}\nReason: {reason}\n"
        f"No more trades today."
    )

def eod_summary(total_value: float, daily_pnl: float, trades_today: int) -> None:
    emoji = "📈" if daily_pnl >= 0 else "📉"
    _send(
        f"{emoji} EOD Summary\n"
        f"Portfolio: ${total_value:,.2f}\n"
        f"Daily P&L: ${daily_pnl:+,.2f}\n"
        f"Trades today: {trades_today}"
    )
