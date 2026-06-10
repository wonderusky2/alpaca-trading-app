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

        # Technicals for context-aware exits
        tech  = (quotes.get(sym) or {}).get("technicals") or {}
        price = float(quotes.get(sym, {}).get("price") or 0)

        # 1. Signal reversal: only exit when score is truly weak (<50), not just below
        #    entry threshold — avoids stop-outs on single-tick noise.
        current_score, _ = sg.score_symbol(sym, quotes, regime)
        reversal_floor = int(model.get("signal_reversal_threshold", 50))
        if 0 < current_score < reversal_floor:
            reason = "signal_reversal"

        # 2. Hard loss stop
        elif pnl_pct <= -trailing_stop_pct:
            reason = "loss_stop"

        # 3. Price closed below the day's opening price — intraday thesis broken
        elif price > 0:
            daily_open = float(quotes.get(sym, {}).get("daily_open") or 0)
            if daily_open > 0 and price < daily_open:
                reason = "below_day_open"

        # 4. Price crossed below EMA21 — trend has turned
        elif price > 0 and tech:
            ema21 = float(tech.get("ema21") or 0)
            if ema21 > 0 and price < ema21:
                reason = "below_ema21"

            # 5. Price >1% below VWAP — institutional flow has turned negative
            elif float(tech.get("price_vs_vwap_pct") or 0) < -1.0:
                reason = "below_vwap"

        # 5. Profit giveback
        elif (
            peak_pnl_pct >= float(model.get("profit_lock_trigger_pct", 2.0))
            and giveback_pct >= float(model.get("profit_giveback_pct", 1.0))
        ):
            reason = "profit_giveback"

        # 6. Stale hold
        elif hold_days >= int(model.get("max_holding_days", 2)):
            reason = "max_holding_days"

        # 7. Regime flipped against the position
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
            _record_sell(sym)
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
_LAST_OPT_PATH      = strategy_model.STATE_DIR / "last_optimization.json"
_SELL_COOLDOWN_PATH = strategy_model.STATE_DIR / "sell_cooldown.json"
_COOLDOWN_MINUTES   = 30


def _load_sell_cooldowns() -> dict:
    try:
        return json.loads(_SELL_COOLDOWN_PATH.read_text(encoding="utf-8")) if _SELL_COOLDOWN_PATH.exists() else {}
    except Exception:
        return {}


def _record_sell(sym: str) -> None:
    cooldowns = _load_sell_cooldowns()
    cooldowns[sym.upper()] = datetime.now(timezone.utc).isoformat()
    try:
        _SELL_COOLDOWN_PATH.write_text(json.dumps(cooldowns, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save sell cooldown: %s", e)


def _in_cooldown(sym: str) -> bool:
    sold_at_str = _load_sell_cooldowns().get(sym.upper())
    if not sold_at_str:
        return False
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(sold_at_str)).total_seconds()
        return elapsed < _COOLDOWN_MINUTES * 60
    except Exception:
        return False


def _run_daily_optimization() -> None:
    """Run variant backtest weekly; apply winner only if it meaningfully beats 'current'.

    Cadence : weekly (≥7 days since last run) — daily re-fitting chases noise.
    Period  : 3M (~60 trading days) — 1M is statistically meaningless for 5 variants.
    Margin  : winner must beat 'current' by ≥3.0 objective points; otherwise keep current.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        prev = json.loads(_LAST_OPT_PATH.read_text(encoding="utf-8")) if _LAST_OPT_PATH.exists() else {}
        last_date_str = prev.get("date") or ""
        if last_date_str:
            try:
                days_since = (
                    datetime.now(timezone.utc).date()
                    - datetime.fromisoformat(last_date_str).date()
                ).days
                if days_since < 7:
                    log.info("Optimization ran %d day(s) ago — next run in %d day(s).", days_since, 7 - days_since)
                    return
            except Exception:
                pass
    except Exception:
        pass

    log.info("Running weekly variant optimization for %s …", today)
    try:
        result = bt.run_variants(period="3M")
        if not result.get("ok"):
            log.warning("run_variants returned ok=False: %s", result.get("error"))
            return

        winner     = result.get("winner") or "current"
        winner_obj = float(result.get("winner_objective") or 0.0)
        ranked     = result.get("ranked") or []

        # Only switch variants if winner beats 'current' by a meaningful margin
        variants    = result.get("variants") or {}
        current_obj = float((variants.get("current") or {}).get("objective") or 0.0)
        MIN_MARGIN  = 3.0

        if winner != "current" and winner_obj <= current_obj + MIN_MARGIN:
            log.info(
                "Optimization: '%s' obj=%.2f vs current=%.2f — margin %.2f < %.2f required; keeping 'current'.",
                winner, winner_obj, current_obj, winner_obj - current_obj, MIN_MARGIN,
            )
            winner = "current"

        model   = strategy_model.load_model()
        updated = dict(model)
        updated["active_variant"] = winner

        if winner != "current":
            profile         = sg.VARIANT_PROFILES.get(winner, {})
            param_overrides = {k: v for k, v in profile.items() if k in sg._PROFILE_PARAM_KEYS}
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
            log.info("Applied variant '%s' param overrides: %s", winner, param_overrides)

        strategy_model.save_model(updated)

        _LAST_OPT_PATH.write_text(
            json.dumps({"date": today, "winner": winner, "objective": winner_obj, "ranked": ranked}, indent=2),
            encoding="utf-8",
        )

        ranked_str = ", ".join(f"{n}={o:.2f}" for n, o in (ranked or [])[:4])
        notify.send(f"RHT optimize: winner={winner} obj={winner_obj:.2f} current={current_obj:.2f} [{ranked_str}]")
        log.info("Weekly optimization done: winner=%s obj=%.3f current=%.3f", winner, winner_obj, current_obj)

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
    # CHOPPY = lowest signal-to-noise. Run exits but take no new positions.
    if regime == "CHOPPY":
        log.info("CHOPPY regime — no new entries today. Exits still monitored.")
        return
    bear_etf_min_conviction = int(model.get("bear_etf_min_conviction", sg.BEAR_ETF_MIN_CONVICTION))
    trade_signals = sg.get_signals(quotes, min_conviction=min_conviction, bear_etf_min_conviction=bear_etf_min_conviction)
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
        if _in_cooldown(sig.symbol):
            log.info("Cooldown: %s sold within last %dm — skipping rebuy.", sig.symbol, _COOLDOWN_MINUTES)
            continue

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
