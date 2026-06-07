#!/usr/bin/env python3
"""
trader.py — robinhood-trader main loop (Alpaca paper trading).
Runs every 5 min via launchd. Headless, no UI required.

Secrets loaded from environment (Conjur via run.sh).
Orders placed via Alpaca paper API — no real money.
"""
from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json

import backtest as bt
import signals as sg
import notify
import strategy_model
from alpaca_client import AlpacaClient
from logger import get_logger

log = get_logger("trader")

ET = ZoneInfo("America/New_York")

# ── Constants ─────────────────────────────────────────────────────────────────
EOD_FLAT_HOUR,   EOD_FLAT_MIN   = 15, 40   # 3:40 PM ET — flatten before close
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 30

POSITION_SIZE_PCT = 0.05   # 5% of equity per position
MAX_POSITIONS     = sg.MAX_POSITIONS  # matches signal engine cap
TRAILING_STOP_PCT = 3.0    # sell if position is down >3% from entry
DAILY_LOSS_KILL   = -2.0   # halt trading if daily P&L < -2%


def _active_model() -> dict:
    return strategy_model.load_model()


# ── Market hours ──────────────────────────────────────────────────────────────
def _now_et() -> datetime:
    return datetime.now(ET)

def is_market_open() -> bool:
    now  = _now_et()
    if now.weekday() >= 5: return False
    mins = now.hour * 60 + now.minute
    return (MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN) <= mins < (EOD_FLAT_HOUR * 60 + EOD_FLAT_MIN)

def is_eod() -> bool:
    now  = _now_et()
    if now.weekday() >= 5: return False
    mins = now.hour * 60 + now.minute
    return mins >= EOD_FLAT_HOUR * 60 + EOD_FLAT_MIN


# ── Quote fetcher ─────────────────────────────────────────────────────────────
def fetch_quotes(
    symbols: list[str],
    client: "AlpacaClient | None" = None,
) -> dict[str, dict]:
    """Returns {sym: {price, prev_close, change_pct}} for a symbol basket.

    Uses Alpaca snapshots only. Missing symbols are skipped instead of mixing
    data vendors in the trading loop.
    """
    if not symbols:
        return {}

    client = client or AlpacaClient()
    try:
        out = client.get_snapshots(symbols)
    except Exception as e:
        log.warning("Alpaca get_snapshots failed: %s", e)
        return {}
    missing = [s for s in symbols if s.upper() not in out]
    if missing:
        log.warning("Alpaca snapshots missing %d/%d symbols: %s", len(missing), len(symbols), missing)
    log.info("fetch_quotes: resolved %d/%d symbols", len(out), len(symbols))
    return out


# ── Kill switch ───────────────────────────────────────────────────────────────
def _check_kill_switch(account: dict, model: dict) -> bool:
    """True if today's P&L has hit the daily loss limit."""
    equity      = float(account.get("equity")      or 0)
    last_equity = float(account.get("last_equity") or equity)
    if last_equity <= 0:
        return False
    daily_pct = (equity - last_equity) / last_equity * 100
    limit = float(model.get("daily_loss_kill_pct", DAILY_LOSS_KILL))
    if daily_pct <= limit:
        log.warning("Kill switch: daily P&L %.2f%% ≤ %.2f%% limit", daily_pct, limit)
        notify.send(f"🛑 RHT KILL SWITCH: daily P&L {daily_pct:.2f}% — halting trades today.")
        return True
    return False


# ── Position sizing ───────────────────────────────────────────────────────────
def _calc_qty(price: float, equity: float, model: dict, size_mult: float = 1.0) -> int:
    if price <= 0 or equity <= 0:
        return 0
    position_size_pct = float(model.get("position_size_pct", POSITION_SIZE_PCT)) * size_mult
    return max(1, int(equity * position_size_pct / price))


# ── Trailing stops ────────────────────────────────────────────────────────────
def _check_trailing_stops(client: AlpacaClient, positions: dict, quotes: dict, model: dict) -> list[str]:
    """Exit positions on loss stops, profit giveback, stale holds, or regime flip."""
    exited: list[str] = []
    memory = strategy_model.update_position_memory(positions)
    regime = sg.detect_regime(quotes)
    for sym, pos in list(positions.items()):
        sym = sym.upper()
        # unrealized_plpc is a decimal (e.g. -0.03 = -3%)
        pnl_pct = float(pos.get("unrealized_plpc") or 0) * 100
        trailing_stop_pct = float(model.get("trailing_stop_pct", TRAILING_STOP_PCT))
        peak_pnl_pct = float((memory.get(sym) or {}).get("peak_unrealized_pnl_pct") or pnl_pct)
        giveback_pct = peak_pnl_pct - pnl_pct
        hold_days = _holding_days(memory.get(sym) or {})
        reason = None

        if pnl_pct <= -trailing_stop_pct:
            reason = "loss_stop"
        elif (
            peak_pnl_pct >= float(model.get("profit_lock_trigger_pct", 2.0))
            and giveback_pct >= float(model.get("profit_giveback_pct", 1.0))
        ):
            reason = "profit_giveback"
        elif hold_days >= int(model.get("max_holding_days", 2)):
            reason = "max_holding_days"
        elif bool(model.get("exit_on_regime_flip", True)) and _regime_against_position(sym, regime):
            reason = "regime_flip"

        if not reason:
            continue

        qty   = int(abs(float(pos.get("qty") or 0)))
        price = quotes.get(sym, {}).get("price", 0)
        pnl   = float(pos.get("unrealized_pl") or 0)
        if qty <= 0:
            continue
        try:
            client.place_market_order(sym, qty, "sell")
            notify.trade_sell(sym, qty, price, pnl, reason)
            log.info("Exit %s: SELL %d %s @ $%.2f (pnl=%.2f%% peak=%.2f%% hold=%dd P&L $%.2f)",
                     reason, qty, sym, price, pnl_pct, peak_pnl_pct, hold_days, pnl)
            exited.append(sym)
        except Exception as e:
            log.error("%s sell failed for %s: %s", reason, sym, e)
    return exited


def _holding_days(memory_row: dict) -> int:
    first_seen = memory_row.get("first_seen_at")
    if not first_seen:
        return 0
    try:
        start = datetime.fromisoformat(str(first_seen).replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - start).days)
    except Exception:
        return 0


def _regime_against_position(symbol: str, regime: str) -> bool:
    """Exit a position only when the regime flips against the instrument direction.
    CHOPPY no longer forces exits — we just hold at reduced size.
    """
    symbol = symbol.upper()
    if regime == "BULL" and symbol in sg.BEAR_UNIVERSE:
        return True
    if regime == "BEAR" and symbol in sg.BULL_UNIVERSE:
        return True
    return False


# ── EOD flatten ───────────────────────────────────────────────────────────────
def run_eod_flat(client: AlpacaClient, positions: dict, quotes: dict) -> None:
    """Close all open positions before market close."""
    if not positions:
        log.info("EOD flat: no positions.")
        return
    for sym, pos in positions.items():
        qty   = int(abs(float(pos.get("qty") or 0)))
        price = quotes.get(sym, {}).get("price", 0)
        pnl   = float(pos.get("unrealized_pl") or 0)
        if qty <= 0:
            continue
        try:
            client.place_market_order(sym, qty, "sell")
            notify.trade_sell(sym, qty, price, pnl, "eod_flat")
            log.info("EOD flat: SELL %d %s @ $%.2f (P&L $%.2f)", qty, sym, price, pnl)
        except Exception as e:
            log.error("EOD flat sell failed for %s: %s", sym, e)

    try:
        account   = client.get_account()
        equity    = float(account.get("equity") or 0)
        last_eq   = float(account.get("last_equity") or equity)
        daily_pnl = equity - last_eq
        notify.eod_summary(equity, daily_pnl, len(positions))
    except Exception:
        pass


# ── Daily optimization ────────────────────────────────────────────────────────
_LAST_OPT_PATH = strategy_model.STATE_DIR / "last_optimization.json"


def _run_daily_optimization() -> None:
    """Run variant backtest once per trading day; apply winner params to strategy model."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        prev = json.loads(_LAST_OPT_PATH.read_text(encoding="utf-8")) if _LAST_OPT_PATH.exists() else {}
        if prev.get("date") == today:
            log.info("Daily optimization already ran today (%s); skipping.", today)
            return
    except Exception:
        pass

    log.info("Running daily variant optimization for %s …", today)
    try:
        result = bt.run_variants(period="1M")
        if not result.get("ok"):
            log.warning("run_variants returned ok=False: %s", result.get("error"))
            return

        winner = result.get("winner") or "current"
        obj = float(result.get("winner_objective") or 0.0)
        ranked = result.get("ranked") or []

        # Apply winner's position-param overrides to strategy model
        profile = sg.VARIANT_PROFILES.get(winner, {})
        param_overrides = {k: v for k, v in profile.items() if k in sg._PROFILE_PARAM_KEYS}
        model = strategy_model.load_model()
        updated = dict(model)
        updated["active_variant"] = winner
        if "conviction_override" in param_overrides:
            updated["min_conviction"] = int(param_overrides["conviction_override"])
        if "max_pos_override" in param_overrides:
            updated["max_positions"] = int(param_overrides["max_pos_override"])
        if "hold_days_override" in param_overrides:
            updated["max_holding_days"] = int(param_overrides["hold_days_override"])
        if "stop_pct_override" in param_overrides:
            updated["trailing_stop_pct"] = float(param_overrides["stop_pct_override"])
        if "lock_trigger_override" in param_overrides:
            updated["profit_lock_trigger_pct"] = float(param_overrides["lock_trigger_override"])
        strategy_model.save_model(updated)
        if param_overrides:
            log.info("Applied variant '%s' param overrides: %s", winner, param_overrides)

        # Persist to prevent re-run same day
        _LAST_OPT_PATH.write_text(
            json.dumps({"date": today, "winner": winner, "objective": obj, "ranked": ranked}, indent=2),
            encoding="utf-8",
        )

        # iMessage alert
        ranked_str = ", ".join(f"{n}={o:.2f}" for n, o in (ranked or [])[:4])
        notify.send(f"RHT optimize: winner={winner} obj={obj:.2f} [{ranked_str}]")
        log.info("Daily optimization done: winner=%s obj=%.3f", winner, obj)

    except Exception as e:
        log.error("Daily optimization failed: %s", e)


# ── Main loop ─────────────────────────────────────────────────────────────────
def main() -> None:
    now = _now_et()
    log.info("Trader tick at %s ET", now.strftime("%H:%M:%S"))
    model = _active_model()

    client = AlpacaClient()

    # ── Daily variant optimization (once per day) ──────────────────────────
    _run_daily_optimization()

    # Fetch quotes for full universe from Alpaca snapshots.
    quotes = fetch_quotes(sg.ALL_SYMBOLS, client=client)
    if not quotes:
        log.error("No quotes fetched — aborting."); return
    quotes = sg.enrich_quotes_with_indicators(quotes, sg.ALL_SYMBOLS, alpaca_client=client)

    # ── EOD: flatten everything ────────────────────────────────────────────
    if is_eod():
        log.info("EOD gate — flattening all positions.")
        positions = client.get_positions()
        run_eod_flat(client, positions, quotes)
        return

    if not is_market_open():
        log.info("Market closed — nothing to do."); return

    # ── Get live Alpaca state ──────────────────────────────────────────────
    try:
        account   = client.get_account()
        positions = client.get_positions()
    except Exception as e:
        log.error("Alpaca state fetch failed: %s", e); return

    equity = float(account.get("equity") or 0)
    if equity <= 0:
        log.error("Invalid equity: %s", equity); return

    # ── Kill switch ────────────────────────────────────────────────────────
    if _check_kill_switch(account, model):
        return

    # ── Trailing stop check ────────────────────────────────────────────────
    stopped_out = _check_trailing_stops(client, positions, quotes, model)
    if stopped_out:
        positions = client.get_positions()  # refresh after exits

    # ── Regime gate ────────────────────────────────────────────────────────
    regime = sg.detect_regime(quotes)
    max_positions = int(model.get("max_positions", MAX_POSITIONS))
    min_conviction = int(model.get("min_conviction", sg.MIN_CONVICTION))
    log.info("Regime: %s | Positions: %d/%d | Equity: $%.2f | Model gen=%s min=%d size=%.2f%%",
             regime, len(positions), max_positions, equity,
             model.get("generation"), min_conviction, float(model.get("position_size_pct", POSITION_SIZE_PCT)) * 100)

    # ── Signal scoring ─────────────────────────────────────────────────────
    # CHOPPY no longer blocks — signals come back at 50% size_mult
    if regime == "CHOPPY":
        log.info("CHOPPY — scanning for high-conviction setups at half size.")
    trade_signals = [sig for sig in sg.get_signals(quotes) if sig.score >= min_conviction]
    log.info("Signals: %s", [(s.symbol, s.score) for s in trade_signals])

    if not trade_signals:
        log.info("No conviction signals (min=%d) — staying in cash.", min_conviction)
        return

    # ── Enter new positions ────────────────────────────────────────────────
    for sig in trade_signals:
        if len(positions) >= max_positions:
            log.info("Max positions (%d) reached.", max_positions); break
        if sig.symbol in positions:
            continue  # already holding

        price = quotes.get(sig.symbol, {}).get("price", 0)
        if price <= 0:
            log.warning("No price for %s — skipping.", sig.symbol); continue

        # Apply regime size_mult (0.5 in CHOPPY, 1.0 in BULL/BEAR)
        qty = _calc_qty(price, equity, model, size_mult=sig.size_mult)
        if qty <= 0:
            continue

        try:
            client.place_market_order(sig.symbol, qty, "buy")
            notify.trade_buy(sig.symbol, qty, price, sig.score, sig.regime)
            log.info("BUY %d %s @ $%.2f  score=%d  regime=%s",
                     qty, sig.symbol, price, sig.score, sig.regime)
            positions[sig.symbol] = {"qty": qty, "entry": price,
                                     "unrealized_pl": 0, "unrealized_plpc": 0}
        except Exception as e:
            log.error("Buy order failed for %s: %s", sig.symbol, e)


if __name__ == "__main__":
    main()
