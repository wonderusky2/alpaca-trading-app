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
    """Log to stdout (captured by K8s) + POST to webhook + fire APNs push."""
    print(f"[notify] {msg}", flush=True)

    webhook_url = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
    if webhook_url:
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

    # APNs push via the server's push endpoint (#41)
    # server.py runs in the same pod on port 5001 — call it directly
    _send_push_via_server(msg)


def _send_push_via_server(msg: str) -> None:
    """Fire an APNs push by calling the local server's push endpoint."""
    api_key = os.environ.get("LAB_API_KEY", "").strip()
    if not api_key:
        return   # no key = dev mode, skip push
    try:
        import urllib.request
        # Build a short title from the first line
        lines = msg.strip().splitlines()
        title = lines[0][:50] if lines else "Alpaca Agent"
        body  = "\n".join(lines[1:])[:100].strip() if len(lines) > 1 else ""
        payload = json.dumps({"title": title, "body": body or title}).encode()
        req = urllib.request.Request(
            "http://localhost:5001/api/lab/push/send",
            data=payload,
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass   # push is best-effort — never block the trading loop


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── Reason descriptions (human-readable) ─────────────────────────────────────

_REASON_TEXT = {
    "regime_flip":      "regime flipped against position",
    "signal_reversal":  "signal score dropped below threshold",
    "loss_stop":        "hit hard loss stop",
    "below_day_open":   "price fell below today's open",
    "below_ema21":      "price crossed below EMA21",
    "below_vwap":       "price fell >1% below VWAP",
    "profit_giveback":  "gave back too much of peak profit",
    "max_holding_days": "hit max hold duration",
    "partial_profit":   "partial scale-out at profit target",
    "breakeven_stop":   "remainder hit breakeven — free trade complete",
    "velocity_stop":    "massive red candle (velocity stop)",
    "eod_flat":         "end-of-day flatten before market close",
    "pyramid_add":      "adding to winner (pyramid in)",
}

_LESSON_TEXT = {
    "regime_flip":      "Regime flipped → exit immediately. Don't fight the tape.",
    "signal_reversal":  "Score went weak — conviction gone, no reason to hold.",
    "loss_stop":        "Hard stop hit. Cut it, move on.",
    "below_day_open":   "Broke below day open — intraday thesis failed.",
    "below_ema21":      "Below EMA21 means trend has turned. Don't fight.",
    "below_vwap":       "Below VWAP = sellers in control. Step aside.",
    "profit_giveback":  "Peak profit given back past threshold — lock it next time sooner.",
    "max_holding_days": "Max hold reached. Stale thesis = time to free up capital.",
    "partial_profit":   "Scaled out 50% at target. Remainder rides to breakeven stop.",
    "breakeven_stop":   "Breakeven stop triggered. Banked the partial gain, no loss.",
    "velocity_stop":    "Got out of the way of a big red candle. Saved capital.",
    "eod_flat":         "EOD flatten. Never hold through close.",
}


# ── Public API ────────────────────────────────────────────────────────────────

def send(msg: str) -> None:
    _send(msg)

def trade_buy(sym: str, qty: int, price: float, score: int, regime: str,
              label: str = "BUY") -> None:
    value = qty * price
    _send(
        f"🟢 {label} {sym}\n"
        f"{qty} shares @ ${price:.2f}  (${value:,.0f})\n"
        f"Score: {score}/100 · Regime: {regime}"
    )

def trade_pyramid(sym: str, qty: int, price: float, score: int, regime: str,
                  pnl_pct: float, entry_price: float, add_num: int) -> None:
    value = qty * price
    _send(
        f"📈 PYRAMID ADD #{add_num}: {sym}\n"
        f"Adding {qty} shares @ ${price:.2f}  (${value:,.0f})\n"
        f"Position already up {pnl_pct:+.2f}% (entry ${entry_price:.2f})\n"
        f"Score: {score}/100 · Regime: {regime}\n"
        f"Strategy: scaling into a winner"
    )

def trade_sell(sym: str, qty: int, price: float, pnl: float, reason: str,
               entry_price: float = 0.0, pnl_pct: float = 0.0,
               hold_hours: float = 0.0) -> None:
    emoji = "🔴" if pnl < 0 else "💰"
    pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
    reason_desc = _REASON_TEXT.get(reason, reason)
    lesson = _LESSON_TEXT.get(reason, "")

    entry_line = f"Entry: ${entry_price:.2f} → Exit: ${price:.2f}  ({pnl_pct:+.2f}%)\n" if entry_price > 0 else ""
    hold_line  = f"Held: {hold_hours:.1f}h\n" if hold_hours > 0 else ""
    lesson_line = f"📌 {lesson}" if lesson else ""

    _send(
        f"{emoji} SELL {sym} — {reason_desc.upper()}\n"
        f"{qty} shares @ ${price:.2f}  P&L: {pnl_str}\n"
        f"{entry_line}"
        f"{hold_line}"
        f"{lesson_line}"
    )

def tick_summary(regime: str, positions: list[tuple[str, float, float]],
                 equity: float, daily_pnl: float) -> None:
    """Periodic heartbeat — sent once per tick so you always know the state."""
    pos_lines = "\n".join(
        f"  {sym}: {pnl_pct:+.2f}% (${pnl_dollar:+,.0f})"
        for sym, pnl_pct, pnl_dollar in positions
    ) or "  (no open positions)"
    daily_emoji = "📈" if daily_pnl >= 0 else "📉"
    _send(
        f"📊 Tick summary · Regime: {regime}\n"
        f"{daily_emoji} Daily P&L: ${daily_pnl:+,.0f}  Equity: ${equity:,.0f}\n"
        f"Open positions:\n{pos_lines}"
    )

def session_open(regime: str, equity: float, n_signals: int) -> None:
    _send(
        f"🔔 RHT tick — Regime: {regime}\n"
        f"Equity: ${equity:,.0f}  ·  {n_signals} conviction signal(s) found"
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
