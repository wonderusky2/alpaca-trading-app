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
import re

import backtest as bt
import config
import learning_agent
import signals as sg
import notify
import strategy_model
import trade_ledger
from alpaca_client import AlpacaClient
from logger import get_logger

log = get_logger("trader")

ET = ZoneInfo("America/New_York")

# ── Constants ─────────────────────────────────────────────────────────────────
EOD_FLAT_HOUR,   EOD_FLAT_MIN   = 15, 40   # 3:40 PM ET — flatten before close
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 30

POSITION_SIZE_PCT     = 0.12   # paper target per position; capped by config
MAX_POSITIONS         = sg.MAX_POSITIONS  # matches signal engine cap
TRAILING_STOP_PCT     = 3.0    # sell if position is down >3% from entry
DAILY_LOSS_KILL       = -2.0   # halt trading if daily P&L < -2%
MAX_SINGLE_POS_PCT    = 0.25   # single-name exposure cap 25% of equity (#19)
CONSECUTIVE_LOSS_HALT = 3      # halt after N back-to-back losing trades (#20)
STALE_QUOTE_SECS      = 120    # reject quote older than 2 minutes (#20)
MAX_SPREAD_PCT        = 0.5    # reject if spread > 0.5% of mid (#20)
OPENING_WINDOW_MINS   = 5      # no new entries in first 5 min after open (#54)
NO_ENTRY_OPEN_MINS    = OPENING_WINDOW_MINS
NO_ENTRY_EOD_MINS     = 10
VOLUME_SURGE_RATIO    = 2.0    # bypass opening gate if any symbol ≥ 2× average volume (#54)
REVERSION_PROFIT_TARGET = 1.5  # % — take gains fast on mean reversion trades (#55)
REVERSION_TRAILING_STOP = 2.0  # % — tighter trailing stop for reversion trades (#55)
ROTATION_SCORE_FLOOR    = 35   # held score below this = conviction collapse
ROTATION_EDGE_MIN       = 20   # replacement must beat held score by this much
_COOLDOWN_MINUTES       = 30

_SAME_DAY_REENTRY_BLOCK_REASONS = {
    "signal_deterioration",
    "rotation_out",
    "price_action_breakdown",
    "price_action_bearish",
    "bearish_reversal_candle",
    "ema_death_cross",
    "avwap_breakdown",
    "macd_bearish_cross",
}
_REENTRY_COOLDOWN_MINUTES = {
    "loss_stop": 180,
    "profit_roundtrip": 180,
    "regime_flip": 240,
    "time_stop": 60,
    "disallowed_inventory": 1440,
}

CORE_SYMBOLS = {
    str(config.CORE_BENCHMARK_SYMBOL).upper(),
    str(config.CORE_HEDGE_SYMBOL).upper(),
}
_OPTION_SYMBOL_RE = re.compile(r"^([A-Z]+)\d{6}[CP]\d{8}$")


def _active_model() -> dict:
    return strategy_model.load_model()


def _submit_market_order(
    client: AlpacaClient,
    symbol: str,
    qty: int,
    side: str,
    *,
    intent: str,
    regime: str = "",
    model: dict | None = None,
    signal_score: int | None = None,
    signal_snapshot: dict | None = None,
) -> dict:
    """Submit an order and durably bind its decision context to the broker ID."""
    result = client.place_market_order(symbol, qty, side)
    trade_ledger.record_order_intent(
        result.get("order_id", ""), symbol, side, qty,
        intent=intent,
        regime=regime,
        model_snapshot=model or _active_model(),
        signal_score=signal_score,
        signal_snapshot=signal_snapshot,
        experiment_id=str(getattr(config, "ALPHA_EXPERIMENT_ID", "")),
    )
    return result


def _submit_close_position(
    client: AlpacaClient,
    symbol: str,
    qty: int,
    *,
    intent: str,
    regime: str = "",
    model: dict | None = None,
) -> dict:
    result = client.close_position(symbol)
    trade_ledger.record_order_intent(
        result.get("order_id", ""), symbol, "sell", qty,
        intent=intent,
        regime=regime,
        model_snapshot=model or _active_model(),
        experiment_id=str(getattr(config, "ALPHA_EXPERIMENT_ID", "")),
    )
    return result


def _load_trader_control() -> dict:
    path = strategy_model.STATE_DIR / "trader_control.json"
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"paused": False, "paper": True}


def _option_underlying(symbol: str) -> str | None:
    m = _OPTION_SYMBOL_RE.match(str(symbol or "").upper())
    return m.group(1) if m else None


def _legacy_unwind_reason(symbol: str) -> str | None:
    sym = str(symbol or "").upper()
    legacy_symbols = {str(s).upper() for s in getattr(config, "LEGACY_BENCHMARK_SYMBOLS", ("QQQ",))}
    legacy_option_roots = {str(s).upper() for s in getattr(config, "LEGACY_OPTION_UNDERLYINGS", ("QQQ",))}
    if sym in legacy_symbols:
        return "legacy benchmark core exposure"
    underlying = _option_underlying(sym)
    if underlying and underlying in legacy_option_roots:
        return f"legacy {underlying} option overlay"
    return None


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
    # Tighter (less negative) limit wins: config is a hard cap the adaptive
    # model can never loosen. min() picked the MORE negative value — bug.
    limit = max(float(model.get("daily_loss_kill_pct", DAILY_LOSS_KILL)), config.DAILY_LOSS_KILL_PCT)
    if daily_pct <= limit:
        log.warning("Kill switch: daily P&L %.2f%% ≤ %.2f%% limit", daily_pct, limit)
        _notify_kill_switch_once(daily_pct)
        return True
    return False


_KILL_NOTIFY_PATH = strategy_model.STATE_DIR / "kill_switch_notified.json"


def _notify_kill_switch_once(daily_pct: float) -> None:
    """Send the kill-switch alert once per trading day, not every 5-min tick."""
    today = _now_et().date().isoformat()
    try:
        if _KILL_NOTIFY_PATH.exists():
            if json.loads(_KILL_NOTIFY_PATH.read_text(encoding="utf-8")).get("date") == today:
                return
    except Exception:
        pass
    notify.send(f"🛑 RHT KILL SWITCH: daily P&L {daily_pct:.2f}% — halting new trades today.")
    try:
        strategy_model.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _KILL_NOTIFY_PATH.write_text(json.dumps({"date": today}), encoding="utf-8")
    except Exception:
        pass


# ── Position sizing ───────────────────────────────────────────────────────────
def _calc_qty(
    price: float,
    equity: float,
    model: dict,
    size_mult: float = 1.0,
    stop_pct: float | None = None,
) -> int:
    """Position size in shares.

    When stop_pct (distance to the stop, in %) is provided and RISK_PER_TRADE_PCT
    is configured, size so every trade risks the same fraction of equity:
        notional = equity * risk% / stop%
    capped at the notional position-size limit. Without a stop, falls back to
    fixed-notional sizing (old behavior).
    """
    if price <= 0 or equity <= 0:
        return 0
    raw_pct = float(model.get("position_size_pct", POSITION_SIZE_PCT))
    capped_pct = min(raw_pct, config.MAX_POSITION_SIZE_PCT) * size_mult
    target_value = equity * capped_pct
    risk_pct = float(getattr(config, "RISK_PER_TRADE_PCT", 0.0) or 0.0)
    if stop_pct and stop_pct > 0 and risk_pct > 0:
        # Equal dollar risk per trade; bounded by the hard notional cap so a
        # very tight stop can't balloon the position.
        risk_value = equity * (risk_pct / 100.0) * size_mult
        hard_cap_value = equity * float(config.MAX_POSITION_SIZE_PCT) * size_mult
        target_value = min(risk_value / (stop_pct / 100.0), hard_cap_value)
    min_order_value = max(
        float(getattr(config, "ALPHA_MIN_ORDER_VALUE", 0.0)),
        equity * float(getattr(config, "ALPHA_MIN_ORDER_PCT", 0.0)),
    )
    if target_value < min_order_value:
        target_value = min_order_value
    return max(0, int(target_value / price))


def _core_target_for_regime(regime: str) -> tuple[float, float]:
    """Return (QQQ target pct, hedge target pct) for the current regime."""
    regime = str(regime or "").upper()
    if regime == "BULL":
        return float(config.CORE_BULL_TARGET_PCT), 0.0
    if regime == "BEAR":
        hedge = float(config.CORE_HEDGE_TARGET_PCT) if bool(config.CORE_HEDGE_BEAR_ONLY) else 0.0
        hedge = min(max(hedge, 0.0), float(config.CORE_HEDGE_MAX_PCT))
        return float(config.CORE_BEAR_TARGET_PCT), hedge
    return float(config.CORE_CHOPPY_TARGET_PCT), 0.0


def _position_market_value(positions: dict, symbol: str) -> float:
    pos = (positions or {}).get(symbol.upper()) or {}
    return abs(float(pos.get("market_val") or pos.get("market_value") or 0))


def _place_rebalance_order(
    client: AlpacaClient,
    symbol: str,
    side: str,
    qty: int,
    price: float,
    reason: str,
    regime: str,
) -> bool:
    if qty <= 0:
        return False
    try:
        _submit_market_order(
            client, symbol, qty, side, intent=reason, regime=regime,
        )
        value = qty * price
        log.info("CORE %s: %s %d %s @ $%.2f (~$%.0f)", reason, side.upper(), qty, symbol, price, value)
        return True
    except Exception as e:
        log.error("Core rebalance failed: %s %d %s: %s", side, qty, symbol, e)
        return False


def _rebalance_core_sleeve(client: AlpacaClient, account: dict, positions: dict, quotes: dict, regime: str) -> bool:
    """Manage QQQ as the primary sleeve; cash is the main hedge.

    Returns True when any order was submitted. This intentionally avoids the
    stock signal gates; QQQ is the benchmark allocation, not an alpha signal.
    """
    if not bool(config.CORE_TRADER_ENABLED):
        return False

    equity = float(account.get("equity") or 0)
    cash = float(account.get("cash") or 0)
    buying_power = float(account.get("buying_power") or cash or 0)
    if equity <= 0:
        return False

    qqq = str(config.CORE_BENCHMARK_SYMBOL).upper()
    hedge = str(config.CORE_HEDGE_SYMBOL).upper()
    qqq_target_pct, hedge_target_pct = _core_target_for_regime(regime)
    qqq_target_pct = min(max(qqq_target_pct, 0.0), float(config.CORE_BENCHMARK_MAX_PCT))
    hedge_target_pct = min(max(hedge_target_pct, 0.0), float(config.CORE_HEDGE_MAX_PCT))

    drift_value = equity * float(config.CORE_REBALANCE_DRIFT_PCT)
    min_order_value = max(float(config.CORE_MIN_ORDER_VALUE), equity * 0.0025)
    changed = False

    qqq_current_value = _position_market_value(positions, qqq)
    qqq_target_value = equity * qqq_target_pct
    qqq_cash_needed = max(0.0, qqq_target_value - qqq_current_value - cash * 0.98)
    if (
        bool(getattr(config, "CORE_TRIM_SATELLITES_ENABLED", False))
        and qqq_cash_needed >= min_order_value
    ):
        for sat_sym, sat_pos in sorted(
            ((s.upper(), p) for s, p in (positions or {}).items() if s.upper() not in CORE_SYMBOLS),
            key=lambda item: float(item[1].get("market_val") or 0),
        ):
            sat_qty = int(abs(float(sat_pos.get("qty") or 0)))
            sat_value = abs(float(sat_pos.get("market_val") or 0))
            sat_price = float((quotes.get(sat_sym) or {}).get("price") or 0)
            if sat_qty <= 0 or sat_value < min_order_value:
                continue
            if sat_price <= 0:
                sat_price = sat_value / sat_qty if sat_qty > 0 else 0
            if sat_price <= 0:
                continue
            if _place_rebalance_order(client, sat_sym, "sell", sat_qty, sat_price, "fund_qqq_core", regime):
                changed = True
                cash += sat_qty * sat_price
                qqq_cash_needed = max(0.0, qqq_cash_needed - sat_qty * sat_price)
                log.info("Core funding: sold satellite %s, remaining QQQ cash need ~$%.0f.", sat_sym, qqq_cash_needed)
                if qqq_cash_needed < min_order_value:
                    break

    def _rebalance_symbol(
        symbol: str,
        target_pct: float,
        cash_available: float,
        buying_power_available: float,
    ) -> tuple[bool, float, float]:
        price = float((quotes.get(symbol) or {}).get("price") or 0)
        if price <= 0:
            log.warning("Core rebalance skipped %s: no valid price.", symbol)
            return False, cash_available, buying_power_available

        current_value = _position_market_value(positions, symbol)
        target_value = equity * target_pct
        delta = target_value - current_value
        if abs(delta) < max(drift_value, min_order_value):
            log.info(
                "Core %s in band: current %.1f%% target %.1f%%.",
                symbol, current_value / equity * 100, target_pct * 100,
            )
            return False, cash_available, buying_power_available

        if delta > 0:
            funding_limit = max(cash_available * 0.98, buying_power_available * 0.95)
            order_value = min(delta, funding_limit)
            side = "buy"
        else:
            order_value = min(abs(delta), current_value)
            side = "sell"

        if order_value < min_order_value:
            return False, cash_available, buying_power_available
        qty = int(order_value / price)
        if qty <= 0:
            return False, cash_available, buying_power_available

        ok = _place_rebalance_order(
            client, symbol, side, qty, price,
            reason=f"regime_{regime.lower()}_target_{target_pct:.0%}",
            regime=regime,
        )
        if ok and side == "buy":
            cash_available -= qty * price
            buying_power_available -= qty * price
        elif ok and side == "sell":
            cash_available += qty * price
            buying_power_available += qty * price
        return ok, cash_available, buying_power_available

    # Primary QQQ sleeve first, then hedge. In non-BEAR regimes, the hedge
    # target is zero, so this will close existing inverse exposure.
    ok, cash, buying_power = _rebalance_symbol(qqq, qqq_target_pct, cash, buying_power)
    changed = changed or ok
    ok, cash, buying_power = _rebalance_symbol(hedge, hedge_target_pct, cash, buying_power)
    changed = changed or ok
    return changed


# ── Conviction size multiplier (Scott Redler style) ───────────────────────────
def _conviction_size_mult(score: int) -> float:
    """Scale size into A/A+ setups while shrinking borderline entries.

    The hard per-position cap still applies, so this only changes how quickly
    we deploy risk across the score curve.
    """
    if score >= 92:
        return 1.35
    if score >= 86:
        return 1.20
    if score >= 78:
        return 1.0
    return 0.75


# ── Hold duration helper ──────────────────────────────────────────────────────
def _holding_hours(memory_row: dict) -> float:
    first_seen = memory_row.get("first_seen_at")
    if not first_seen:
        return 0.0
    try:
        start = datetime.fromisoformat(str(first_seen).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - start).total_seconds() / 3600)
    except Exception:
        return 0.0


def _holding_days(memory_row: dict) -> int:
    return int(_holding_hours(memory_row) / 24)


def _minimum_hold_blocks_exit(memory_row: dict, reason: str) -> bool:
    """Suppress indicator-noise exits during the configured price-discovery window."""
    if reason in {"loss_stop", "regime_flip", "profit_target"}:
        return False
    minimum = int(getattr(config, "ALPHA_MIN_HOLD_MINUTES", 0) or 0)
    return minimum > 0 and (_holding_hours(memory_row) * 60) < minimum


# ── Exit stack (#23) ──────────────────────────────────────────────────────────
# Five gates evaluated in priority order.  No other exit logic.
#
#   Gate 1 — Hard stop:     pnl_pct ≤ -trailing_stop_pct
#   Gate 2 — EOD flat:      handled separately in run_eod_flat()
#   Gate 3 — Regime flip:   bull name held in BEAR market (or vice versa)
#   Gate 4 — Max hold:      held ≥ max_holding_days
#   Gate 5 — Profit target: pnl_pct ≥ profit_target_pct
#            Trailing stop: once target hit, exit if giveback ≥ profit_giveback_pct
#
# Removed: partial exits, velocity stop, breakeven stop, signal reversal,
#          below_day_open, below_ema21, below_vwap.  These generated noise without
#          measured edge.  Add back individually only after ledger proves the gate.

def _check_trailing_stops(client: AlpacaClient, positions: dict, quotes: dict, model: dict, regime: str = "") -> list[str]:
    """Simple 5-gate exit stack. EOD flat is handled separately."""
    exited: list[str] = []
    memory = strategy_model.update_position_memory(positions)
    strategies = _load_position_strategies()
    if not regime:
        regime = sg.detect_regime(quotes)

    for sym, pos in list(positions.items()):
        sym = sym.upper()
        if sym in CORE_SYMBOLS:
            continue
        pnl_pct   = float(pos.get("unrealized_plpc") or 0) * 100
        pnl       = float(pos.get("unrealized_pl") or 0)
        price     = float((quotes.get(sym) or {}).get("price") or 0)
        hold_days = _holding_days(memory.get(sym) or {})
        hold_hours = _holding_hours(memory.get(sym) or {})
        entry_price = float((memory.get(sym) or {}).get("entry_price") or 0)
        peak_pnl_pct = float((memory.get(sym) or {}).get("peak_unrealized_pnl_pct") or pnl_pct)
        giveback_pct = peak_pnl_pct - pnl_pct

        # ATR-based stop fixed at entry (per-position); model default otherwise.
        # Bounded so a data glitch can't produce an absurd stop.
        pos_meta = strategies.get(sym) or {}
        pos_stop = float(pos_meta.get("stop_pct") or 0)
        hard_stop_pct    = min(max(pos_stop, 1.0), 5.0) if pos_stop > 0 else float(model.get("trailing_stop_pct", TRAILING_STOP_PCT))
        max_hold_days    = int(model.get("max_holding_days", 2))
        profit_target    = float(model.get("profit_target_pct", 3.0))
        giveback_trigger = float(model.get("profit_giveback_pct", 1.0))
        lock_trigger     = float(model.get("profit_lock_trigger_pct", profit_target))

        # Reversion trades: tighter params — take gains fast, cut losses faster (#55)
        if pos_meta.get("strategy") == "reversion":
            hard_stop_pct = min(hard_stop_pct, REVERSION_TRAILING_STOP)
            profit_target = min(profit_target, REVERSION_PROFIT_TARGET)
            max_hold_days = min(max_hold_days, 1)
            lock_trigger  = min(lock_trigger,  REVERSION_PROFIT_TARGET)

        reason: str | None = None

        # Gate 1: hard stop
        if pnl_pct <= -hard_stop_pct:
            reason = "loss_stop"

        # Gate 3: regime flip
        if not reason and _regime_against_position(sym, regime):
            reason = "regime_flip"

        # Gate 4: once a winner has earned a lock, do not let it round-trip red
        if not reason and peak_pnl_pct >= lock_trigger and pnl_pct <= 0:
            reason = "profit_roundtrip"

        # Gate 5: max hold
        if not reason and hold_days >= max_hold_days:
            reason = "max_holding_days"

        # Gate 6a: profit target hit — flat out
        if not reason and pnl_pct >= profit_target:
            reason = "profit_target"

        # Gate 6b: trailing stop after lock trigger reached
        if not reason and peak_pnl_pct >= lock_trigger and giveback_pct >= giveback_trigger:
            reason = "profit_giveback"

        if not reason:
            continue
        if _minimum_hold_blocks_exit(memory.get(sym) or {}, reason):
            log.info(
                "Minimum hold blocked %s exit for %s at %.1fm (need %dm).",
                reason, sym, hold_hours * 60,
                int(getattr(config, "ALPHA_MIN_HOLD_MINUTES", 0) or 0),
            )
            continue

        qty = int(abs(float(pos.get("qty") or 0)))
        if qty <= 0:
            continue
        try:
            _submit_market_order(
                client, sym, qty, "sell", intent=reason, regime=regime, model=model,
            )
            notify.trade_sell(sym, qty, price, pnl, reason,
                              entry_price=entry_price, pnl_pct=pnl_pct, hold_hours=hold_hours)
            log.info("Exit %s: SELL %d %s @ $%.2f (pnl=%.2f%% peak=%.2f%% hold=%dd P&L $%.2f)",
                     reason, qty, sym, price, pnl_pct, peak_pnl_pct, hold_days, pnl)
            _record_sell(sym, pnl=pnl, exit_reason=reason)
            _clear_position_strategy(sym)
            exited.append(sym)
        except Exception as e:
            log.error("%s sell failed for %s: %s", reason, sym, e)
    return exited


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


def _held_signal_exit_reason(sym: str, quotes: dict, regime: str) -> str | None:
    """Mirror the dashboard deterioration logic inside the live trader."""
    score, breakdown = sg.score_symbol(sym, quotes, regime)
    technicals = (quotes.get(sym) or {}).get("technicals") or {}

    if score > 0 and score < ROTATION_SCORE_FLOOR:
        return "signal_deterioration"

    price_action = breakdown.get("price_action", {}) or {}
    pa_status = str(price_action.get("status") or "")
    pa_label = str(price_action.get("label") or "").lower()
    if pa_status == "bearish":
        if "shooting star" in pa_label or "bearish engulfing" in pa_label:
            return "bearish_reversal_candle"
        if "failed breakout" in pa_label or "breakdown" in pa_label:
            return "price_action_breakdown"
        if breakdown.get("macd", {}).get("status") != "bullish":
            return "price_action_bearish"

    rsi = float(technicals.get("rsi14") or 0)
    if rsi > 78:
        return "rsi_overbought"

    macd_hist = float(technicals.get("macd_hist") or 0)
    if macd_hist < -0.05:
        return "macd_bearish_cross"

    if breakdown.get("ema", {}).get("status") == "bearish" and breakdown.get("macd", {}).get("status") != "bullish":
        return "ema_death_cross"

    if breakdown.get("avwap", {}).get("status") == "bearish" and breakdown.get("trend", {}).get("status") == "bearish":
        return "avwap_breakdown"

    return None


def _rotate_stale_positions(
    client: AlpacaClient,
    positions: dict,
    quotes: dict,
    model: dict,
    regime: str,
    trade_signals: list,
) -> list[str]:
    """Kill stale holdings so capital can rotate into stronger live leaders."""
    exited: list[str] = []
    memory = strategy_model.load_position_memory()
    candidate_scores = {str(sig.symbol).upper(): int(sig.score) for sig in (trade_signals or [])}
    best_replacements = [sig for sig in (trade_signals or []) if str(sig.symbol).upper() not in {s.upper() for s in positions.keys()}]

    for sym, pos in list((positions or {}).items()):
        sym = str(sym).upper()
        if sym in CORE_SYMBOLS:
            continue
        if _minimum_hold_blocks_exit(memory.get(sym) or {}, "signal_rotation"):
            log.info(
                "Minimum hold blocked rotation review for %s at %.1fm.",
                sym, _holding_hours(memory.get(sym) or {}) * 60,
            )
            continue

        current_score, _ = sg.score_symbol(sym, quotes, regime)
        reason = _held_signal_exit_reason(sym, quotes, regime)
        replacement = None

        if best_replacements:
            for sig in best_replacements:
                edge = int(sig.score) - int(current_score)
                if int(sig.score) >= int(model.get("min_conviction", sg.MIN_CONVICTION)) and edge >= ROTATION_EDGE_MIN:
                    replacement = sig
                    break

        if not reason and replacement and current_score < int(model.get("min_conviction", sg.MIN_CONVICTION)):
            reason = "rotation_out"

        if not reason:
            continue

        qty = int(abs(float(pos.get("qty") or 0)))
        if qty <= 0:
            continue
        price = float((quotes.get(sym) or {}).get("price") or 0)
        pnl = float(pos.get("unrealized_pl") or 0)
        pnl_pct = float(pos.get("unrealized_plpc") or 0) * 100
        entry_price = float((memory.get(sym) or {}).get("entry_price") or 0)
        hold_hours = _holding_hours(memory.get(sym) or {})
        reason_for_notify = reason
        if reason == "rotation_out" and replacement is not None:
            reason_for_notify = f"rotation_out_to_{replacement.symbol.lower()}"

        try:
            _submit_market_order(
                client, sym, qty, "sell", intent=reason, regime=regime, model=model,
            )
            notify.trade_sell(
                sym, qty, price, pnl, reason_for_notify,
                entry_price=entry_price, pnl_pct=pnl_pct, hold_hours=hold_hours,
            )
            _record_sell(sym, pnl=pnl, exit_reason=reason)
            _clear_position_strategy(sym)
            log.info(
                "ROTATION EXIT %s: SELL %d %s @ $%.2f score=%d replacement=%s",
                reason, qty, sym, price, current_score, getattr(replacement, "symbol", "-"),
            )
            exited.append(sym)
        except Exception as e:
            log.error("Rotation exit failed for %s: %s", sym, e)

    return exited


# ── EOD flatten ───────────────────────────────────────────────────────────────
def run_eod_flat(client: AlpacaClient, positions: dict, quotes: dict) -> None:
    """Close all open positions before market close."""
    if not positions:
        log.info("EOD flat: no positions.")
        return
    memory = strategy_model.load_position_memory()
    for sym, pos in positions.items():
        sym = sym.upper()
        if sym in CORE_SYMBOLS:
            log.info("EOD flat: keeping core sleeve position %s.", sym)
            continue
        qty   = int(abs(float(pos.get("qty") or 0)))
        price = quotes.get(sym, {}).get("price", 0)
        pnl   = float(pos.get("unrealized_pl") or 0)
        pnl_pct = float(pos.get("unrealized_plpc") or 0) * 100
        entry_price = float((memory.get(sym.upper()) or {}).get("entry_price") or 0)
        hold_hours = _holding_hours(memory.get(sym.upper()) or {})
        if qty <= 0:
            continue
        try:
            _submit_market_order(
                client, sym, qty, "sell", intent="eod_flat", regime="EOD",
            )
            notify.trade_sell(sym, qty, price, pnl, "eod_flat",
                              entry_price=entry_price, pnl_pct=pnl_pct, hold_hours=hold_hours)
            _clear_position_strategy(sym)
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


def _liquidate_legacy_benchmark_positions(
    client: AlpacaClient,
    positions: dict,
    regime: str,
) -> list[str]:
    """Close leftover QQQ core / QQQ option baggage before alpha trading resumes."""
    if not bool(getattr(config, "LEGACY_UNWIND_ENABLED", False)):
        return []
    if bool(getattr(config, "CORE_TRADER_ENABLED", False)):
        return []

    closed: list[str] = []
    for sym, pos in (positions or {}).items():
        sym = str(sym or "").upper()
        legacy_reason = _legacy_unwind_reason(sym)
        if not legacy_reason:
            continue

        qty = int(abs(float(pos.get("qty") or 0)))
        if qty <= 0:
            continue

        market_val = abs(float(pos.get("market_val") or 0))
        price = (market_val / qty) if market_val > 0 and qty > 0 else 0.0
        pnl = float(pos.get("unrealized_pl") or 0)
        side = str(pos.get("side") or "").upper()

        try:
            _submit_close_position(
                client, sym, qty, intent="legacy_benchmark_unwind", regime=regime,
            )
            _clear_position_strategy(sym)
            notify.send(
                f"Legacy unwind: closed {sym} ({qty} units) because alpha mode should not carry {legacy_reason}."
            )
            log.info(
                "LEGACY UNWIND: closed %s qty=%d side=%s approx_price=$%.2f pnl=$%.2f reason=%s",
                sym, qty, side or "?", price, pnl, legacy_reason,
            )
            closed.append(sym)
        except Exception as e:
            log.error("Legacy unwind failed for %s: %s", sym, e)

    return closed


def _allowed_alpha_symbols() -> set[str]:
    allowed = {
        str(sym).upper()
        for sym in getattr(config, "ALPHA_ALLOWED_SYMBOLS", ())
        if str(sym).strip()
    }
    return allowed or set(sg.MOMENTUM_STOCKS)


def _blocked_alpha_symbols() -> set[str]:
    return {
        str(sym).upper()
        for sym in getattr(config, "ALPHA_BLOCKED_SYMBOLS", ())
        if str(sym).strip()
    }


def _liquidate_disallowed_positions(
    client: AlpacaClient,
    positions: dict,
    regime: str,
) -> list[str]:
    if not bool(getattr(config, "ALPHA_FORCE_EXIT_DISALLOWED_POSITIONS", False)):
        return []

    allowed = _allowed_alpha_symbols()
    blocked = _blocked_alpha_symbols()
    unwind_options = bool(getattr(config, "ALPHA_UNWIND_OPTIONS", False))
    closed: list[str] = []
    for sym, pos in (positions or {}).items():
        sym = str(sym or "").upper()
        if sym in CORE_SYMBOLS:
            continue

        underlying = _option_underlying(sym)
        disallowed = False
        reason = ""
        if underlying:
            if unwind_options:
                disallowed = True
                reason = "option overlays are disabled in alpha mode"
        elif sym in blocked or (allowed and sym not in allowed):
            disallowed = True
            reason = "symbol is outside the approved alpha universe"
        if not disallowed:
            continue

        qty = int(abs(float(pos.get("qty") or 0)))
        if qty <= 0:
            continue
        market_val = abs(float(pos.get("market_val") or pos.get("market_value") or 0))
        price = (market_val / qty) if market_val > 0 and qty > 0 else 0.0
        pnl = float(pos.get("unrealized_pl") or 0)
        side = str(pos.get("side") or "").upper()
        try:
            _submit_close_position(
                client, sym, qty, intent="disallowed_inventory", regime=regime,
            )
            _record_sell(sym, pnl=pnl, exit_reason="disallowed_inventory")
            _clear_position_strategy(sym)
            notify.send(f"Alpha reset: closed {sym} because {reason}.")
            log.info("ALPHA UNWIND: closed %s qty=%d side=%s reason=%s", sym, qty, side or "?", reason)
            closed.append(sym)
        except Exception as e:
            log.error("Alpha unwind failed for %s: %s", sym, e)
    return closed


# ── Daily optimization ────────────────────────────────────────────────────────
_LAST_OPT_PATH          = strategy_model.STATE_DIR / "last_optimization.json"
_SELL_COOLDOWN_PATH     = strategy_model.STATE_DIR / "sell_cooldown.json"
_STRATEGY_MEMORY_PATH   = strategy_model.STATE_DIR / "position_strategy.json"


def _load_position_strategies() -> dict:
    """Returns {sym: {"strategy": "momentum"|"reversion", "stop_pct": float|None}}.

    Older files stored a bare strategy string per symbol; those are normalized
    to dicts on read so callers have one shape to deal with.
    """
    try:
        if _STRATEGY_MEMORY_PATH.exists():
            raw = json.loads(_STRATEGY_MEMORY_PATH.read_text(encoding="utf-8"))
            out: dict = {}
            for sym, val in (raw or {}).items():
                out[str(sym).upper()] = val if isinstance(val, dict) else {"strategy": str(val)}
            return out
    except Exception:
        pass
    return {}


def _record_position_strategy(sym: str, strategy: str, stop_pct: float | None = None) -> None:
    data = _load_position_strategies()
    entry: dict = {"strategy": strategy}
    if stop_pct and stop_pct > 0:
        entry["stop_pct"] = round(float(stop_pct), 2)
    data[sym.upper()] = entry
    try:
        strategy_model.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _STRATEGY_MEMORY_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save position strategy for %s: %s", sym, e)


def _clear_position_strategy(sym: str) -> None:
    data = _load_position_strategies()
    data.pop(sym.upper(), None)
    try:
        _STRATEGY_MEMORY_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not clear position strategy for %s: %s", sym, e)


def _load_sell_cooldowns() -> dict:
    try:
        return json.loads(_SELL_COOLDOWN_PATH.read_text(encoding="utf-8")) if _SELL_COOLDOWN_PATH.exists() else {}
    except Exception:
        return {}


def _parse_trade_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _is_same_trading_day(ts: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return ts.astimezone(ET).date() == now.astimezone(ET).date()


def _legacy_cooldown_status(sym: str) -> tuple[bool, str]:
    raw = _load_sell_cooldowns().get(sym.upper())
    if not raw:
        return False, ""
    sold_at_str = raw.get("sold_at") if isinstance(raw, dict) else raw
    reason = str((raw.get("exit_reason") if isinstance(raw, dict) else "") or "").lower()
    sold_at = _parse_trade_ts(sold_at_str)
    if sold_at is None:
        return False, ""
    minutes = int(_REENTRY_COOLDOWN_MINUTES.get(reason, _COOLDOWN_MINUTES))
    elapsed = max(0.0, (datetime.now(timezone.utc) - sold_at).total_seconds() / 60)
    if elapsed < minutes:
        wait = max(1, int(round(minutes - elapsed)))
        detail = reason.replace("_", " ") if reason else "recent sell"
        return True, f"{sym.upper()} sold {elapsed:.0f}m ago ({detail}); wait about {wait}m."
    return False, ""


def _reentry_block_status(sym: str) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    symbol = str(sym or "").upper()
    for row in trade_ledger.recent_trades(limit=300):
        if str(row.get("symbol") or "").upper() != symbol:
            continue
        side = str(row.get("side") or "").lower()
        if side != "sell":
            continue
        reason = str(row.get("exit_reason") or "").lower()
        sold_at = _parse_trade_ts(str(row.get("recorded_at") or ""))
        if sold_at is None:
            continue
        if bool(getattr(config, "ALPHA_NO_REENTRY_AFTER_SELL_TODAY", False)) and _is_same_trading_day(sold_at, now=now):
            detail = reason.replace("_", " ") if reason else "sell"
            return True, (
                f"{symbol} already exited today ({detail}); "
                "stand down until the next trading session."
            )
        if reason in _SAME_DAY_REENTRY_BLOCK_REASONS and _is_same_trading_day(sold_at, now=now):
            return True, (
                f"{symbol} exited for {reason.replace('_', ' ')} today; "
                "do not re-enter until the next trading session."
            )
        minutes = int(_REENTRY_COOLDOWN_MINUTES.get(reason, _COOLDOWN_MINUTES))
        elapsed = max(0.0, (now - sold_at).total_seconds() / 60)
        if elapsed < minutes:
            wait = max(1, int(round(minutes - elapsed)))
            detail = reason.replace("_", " ") if reason else "recent sell"
            return True, f"{symbol} sold {elapsed:.0f}m ago ({detail}); wait about {wait}m."
        break
    return _legacy_cooldown_status(symbol)


def _symbol_entries_today(sym: str) -> int:
    now = datetime.now(timezone.utc)
    symbol = str(sym or "").upper()
    buys = 0
    for row in trade_ledger.recent_trades(limit=300):
        if str(row.get("symbol") or "").upper() != symbol:
            continue
        if str(row.get("side") or "").lower() != "buy":
            continue
        bought_at = _parse_trade_ts(str(row.get("recorded_at") or ""))
        if bought_at is None:
            continue
        if not _is_same_trading_day(bought_at, now=now):
            continue
        buys += 1
    return buys


def _entry_limit_block_status(sym: str) -> tuple[bool, str]:
    max_entries = int(getattr(config, "ALPHA_MAX_ENTRIES_PER_SYMBOL_PER_DAY", 0) or 0)
    if max_entries <= 0:
        return False, ""
    entries_today = _symbol_entries_today(sym)
    if entries_today >= max_entries:
        return True, (
            f"{str(sym).upper()} already used its {max_entries} "
            f"entry slot{'s' if max_entries != 1 else ''} today."
        )
    return False, ""


# Administrative exits — not a verdict on the strategy, so they must not feed
# the consecutive-loss halt (#7). A losing forced unwind of out-of-universe
# inventory says nothing about signal quality.
_NON_STRATEGY_EXITS = {"disallowed_inventory", "legacy_benchmark_unwind", "eod_flat"}


def _record_sell(sym: str, pnl: float = 0.0, exit_reason: str = "") -> None:
    """Record sell cooldown + update consecutive-loss counter (#20)."""
    reason = str(exit_reason or "").lower()
    cooldowns = _load_sell_cooldowns()
    cooldowns[sym.upper()] = {
        "sold_at": datetime.now(timezone.utc).isoformat(),
        "exit_reason": reason,
    }
    try:
        _SELL_COOLDOWN_PATH.write_text(json.dumps(cooldowns, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save sell cooldown: %s", e)
    if reason in _NON_STRATEGY_EXITS:
        return
    # Track win/loss streak for consecutive-loss kill gate
    streak = _record_trade_outcome(won=(pnl > 0))
    if pnl <= 0:
        log.info("Consecutive losses after %s exit: %d", sym, streak)


def _in_cooldown(sym: str) -> bool:
    return _reentry_block_status(sym)[0]


# ── Kill gates (#20) ──────────────────────────────────────────────────────────
_CONSEC_LOSS_PATH = strategy_model.STATE_DIR / "consecutive_losses.json"


def _load_consec_losses() -> dict:
    """Returns {"count": int, "last_reset": iso_str}."""
    try:
        if _CONSEC_LOSS_PATH.exists():
            return json.loads(_CONSEC_LOSS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"count": 0, "last_reset": None}


def _record_trade_outcome(won: bool) -> int:
    """Append outcome to consecutive-loss counter. Returns current streak."""
    data = _load_consec_losses()
    if won:
        data["count"] = 0
    else:
        data["count"] = int(data.get("count") or 0) + 1
    data["last_reset"] = datetime.now(timezone.utc).isoformat()
    try:
        strategy_model.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _CONSEC_LOSS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save consecutive losses: %s", e)
    return int(data["count"])


def _consecutive_loss_halt() -> bool:
    """Return True if we've hit CONSECUTIVE_LOSS_HALT losing trades in a row."""
    count = int(_load_consec_losses().get("count") or 0)
    if count >= CONSECUTIVE_LOSS_HALT:
        log.warning(
            "Consecutive-loss kill gate: %d losses in a row (limit=%d) — halting new entries.",
            count, CONSECUTIVE_LOSS_HALT,
        )
        notify.send(
            f"🛑 RHT KILL GATE: {count} consecutive losing trades — no new entries this session."
        )
        return True
    return False


def _recent_buy_blocked() -> bool:
    """Rate-limit new entries so paper evidence is clean and not churny."""
    min_minutes = int(config.MIN_MINUTES_BETWEEN_BUYS)
    if min_minutes <= 0:
        return False
    for row in trade_ledger.recent_trades(limit=50):
        if str(row.get("side") or "").lower() != "buy":
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("recorded_at")).replace("Z", "+00:00"))
            age_mins = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            if age_mins < min_minutes:
                log.info("Entry rate limit: last buy %.0fm ago; need %dm.", age_mins, min_minutes)
                return True
        except Exception:
            continue
        break
    return False


def _edge_gate_blocks_entries() -> bool:
    """Block fresh buys once the ledger has enough evidence and expectancy is not positive."""
    experiment_id = str(getattr(config, "ALPHA_EXPERIMENT_ID", "")) or None
    evidence = trade_ledger.edge_summary(limit=500, experiment_id=experiment_id)
    trades = int(evidence.get("trades") or 0)
    expectancy = float(evidence.get("expectancy") or 0.0)
    min_trades = int(config.EDGE_GATE_MIN_CLOSED_TRADES)
    min_expectancy = float(config.EDGE_GATE_MIN_EXPECTANCY)
    if trades >= min_trades and expectancy <= min_expectancy:
        log.warning(
            "Edge gate blocked experiment %s: %d closed trades, expectancy $%.2f <= $%.2f.",
            experiment_id, trades, expectancy, min_expectancy,
        )
        return True
    return False


def _market_down_day_blocks_longs(quotes: dict) -> bool:
    threshold = float(config.MARKET_DOWN_BLOCK_PCT)
    spy = float((quotes.get("SPY") or {}).get("change_pct") or 0)
    qqq = float((quotes.get("QQQ") or {}).get("change_pct") or 0)
    if spy <= threshold and qqq <= threshold:
        log.warning(
            "Market-down gate blocked new long entries: SPY %.2f%%, QQQ %.2f%% <= %.2f%%.",
            spy, qqq, threshold,
        )
        return True
    return False


def _quote_is_stale(quote: dict) -> bool:
    """Return True if the quote timestamp is older than STALE_QUOTE_SECS."""
    ts_str = quote.get("timestamp") or quote.get("updated_at") or ""
    if not ts_str:
        return False   # no timestamp field — can't judge, allow through
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > STALE_QUOTE_SECS
    except Exception:
        return False


def _spread_too_wide(quote: dict) -> bool:
    """Return True if bid/ask spread exceeds MAX_SPREAD_PCT of mid."""
    bid = float(quote.get("bid") or quote.get("bid_price") or 0)
    ask = float(quote.get("ask") or quote.get("ask_price") or 0)
    if bid <= 0 or ask <= 0:
        return False   # no spread data — allow through
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return False
    spread_pct = (ask - bid) / mid * 100
    return spread_pct > MAX_SPREAD_PCT


def _in_opening_window(quotes: dict) -> bool:
    """Return True (block entries) if within the opening window blackout period.

    Volume-surge fast path: if any tradeable symbol shows ≥ VOLUME_SURGE_RATIO×
    average volume, the opening gate is bypassed — institutional catalyst moves
    are already underway and waiting costs the entry. (#54)
    """
    now  = _now_et()
    mins = now.hour * 60 + now.minute - (MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN)
    if mins < 0 or mins >= OPENING_WINDOW_MINS:
        return False

    # Volume-surge fast path
    for sym in sg.BULL_UNIVERSE:
        vol_ratio = float((quotes.get(sym) or {}).get("technicals", {}).get("volume_ratio") or 0)
        if vol_ratio >= VOLUME_SURGE_RATIO:
            log.info("Opening window bypass: %s surge %.1fx — allowing early entries.", sym, vol_ratio)
            return False

    return True


def _intraday_confirms_entry(sym: str, client: AlpacaClient) -> bool:
    """5-min intraday confirmation gate — fires before every new buy.

    Checks three conditions on the last 30 5-min bars (~2.5 hours):
      1. Price is above 5-min VWAP  (intraday momentum direction)
      2. 5-min RSI < 72             (not buying into a blow-off top)
      3. Latest bar volume ≥ 0.8×   20-bar avg (volume not drying up)

    Returns True (allow entry) on pass or on any data failure (fail-open
    so a bad API call never silently blocks all trades).
    """
    try:
        bars_map = client.get_historical_bars([sym], timeframe="5min", limit=30)
        df = bars_map.get(sym.upper())
        if df is None or df.empty or len(df) < 10:
            return True  # not enough data — fail open

        closes  = df["close"].tolist()
        volumes = df["volume"].tolist()
        vwaps   = df["vwap"].tolist() if "vwap" in df.columns else []

        # ── 1. Price vs 5-min VWAP ───────────────────────────────────────────
        if vwaps:
            latest_vwap = float(vwaps[-1] or 0)
            latest_close = float(closes[-1] or 0)
            if latest_vwap > 0 and latest_close < latest_vwap * 0.998:
                log.info("Intraday gate BLOCK %s: price $%.2f below 5-min VWAP $%.2f",
                         sym, latest_close, latest_vwap)
                return False

        # ── 2. 5-min RSI < 72 ────────────────────────────────────────────────
        if len(closes) >= 15:
            deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            gains  = [d for d in deltas if d > 0]
            losses = [-d for d in deltas if d < 0]
            avg_gain = sum(gains[-14:]) / 14 if gains else 0
            avg_loss = sum(losses[-14:]) / 14 if losses else 1e-9
            rs  = avg_gain / avg_loss
            rsi = 100 - 100 / (1 + rs)
            if rsi > 72:
                log.info("Intraday gate BLOCK %s: 5-min RSI %.1f overbought", sym, rsi)
                return False

        # ── 3. Volume not drying up ───────────────────────────────────────────
        if len(volumes) >= 10:
            avg_vol   = sum(volumes[-20:]) / min(20, len(volumes))
            last_vol  = float(volumes[-1] or 0)
            if avg_vol > 0 and last_vol < avg_vol * 0.5:
                log.info("Intraday gate BLOCK %s: volume drying up (%.0f vs avg %.0f)",
                         sym, last_vol, avg_vol)
                return False

        return True
    except Exception as e:
        log.debug("Intraday confirm failed for %s (%s) — fail open", sym, e)
        return True  # fail open


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

        # ── PROPOSE ONLY — do NOT auto-apply (#18) ────────────────────────────
        # Winner is logged and written to last_optimization.json for inspection
        # via /api/lab/variants, but the model is NEVER mutated automatically.
        # A human must POST /api/lab/model/learn with {"apply": true} to act on it.
        log.info(
            "Weekly optimization PROPOSAL (not applied): winner=%s obj=%.3f current=%.3f — "
            "apply manually via /api/lab/model/learn",
            winner, winner_obj, current_obj,
        )

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
    ctrl = _load_trader_control()

    client = AlpacaClient()
    reconciliation = trade_ledger.reconcile_broker_orders(client.get_recent_orders(limit=500))
    log.info("Broker fill reconciliation: %s", reconciliation)

    # ── Daily variant optimization (once per day) ──────────────────────────
    _run_daily_optimization()

    # ── Self-learning cycle (guardrailed, journaled, auto-rollback) ────────
    try:
        rollback = learning_agent.check_rollback()
        if rollback:
            model = _active_model()   # model changed — reload
        learned = learning_agent.maybe_learn()
        if learned.get("actions"):
            log.info("Learning actions: %s", learned["actions"])
            model = _active_model()   # pick up any applied changes this tick
    except Exception as e:
        log.warning("Learning cycle failed (non-fatal): %s", e)

    # Fetch quotes for full universe from Alpaca snapshots.
    quote_symbols = sorted(set(sg.ALL_SYMBOLS) | CORE_SYMBOLS)
    quotes = fetch_quotes(quote_symbols, client=client)
    if not quotes:
        log.error("No quotes fetched — aborting."); return
    quotes = sg.enrich_quotes_with_indicators(quotes, quote_symbols, alpaca_client=client)

    if ctrl.get("paused"):
        log.warning("Trader control is paused — skipping automated order mutations.")
        return

    # ── EOD: flatten only for explicit intraday mode ───────────────────────
    if bool(getattr(config, "EOD_FLAT_ENABLED", False)) and is_eod():
        log.info("EOD gate — flattening all positions.")
        positions = client.get_positions()
        run_eod_flat(client, positions, quotes)
        return

    # Holiday-aware market check (#4): broker clock is truth (knows holidays and
    # half-days); local weekday/time window is only the fallback if the API fails.
    market_open: bool | None = None
    try:
        market_open = bool(client.get_clock().get("is_open"))
    except Exception as e:
        log.warning("Broker clock fetch failed (%s) — using local calendar fallback.", e)
    if market_open is None:
        market_open = is_market_open() or is_eod()  # local window incl. 15:40-16:00
    if not market_open:
        log.info("Market closed — nothing to do."); return

    # ── Get live Alpaca state ──────────────────────────────────────────────
    try:
        account   = client.get_account()
        positions = client.get_positions()
    except Exception as e:
        log.error("Alpaca state fetch failed: %s", e); return

    legacy_closed = _liquidate_legacy_benchmark_positions(client, positions, regime="LEGACY_UNWIND")
    if legacy_closed:
        log.info("Legacy unwind submitted for %s. Skipping new entries until next tick.", ", ".join(legacy_closed))
        return
    alpha_unwind = _liquidate_disallowed_positions(client, positions, regime="ALPHA_RESET")
    if alpha_unwind:
        log.info("Alpha reset unwind submitted for %s. Skipping new entries until next tick.", ", ".join(alpha_unwind))
        return

    equity = float(account.get("equity") or 0)
    if equity <= 0:
        log.error("Invalid equity: %s", equity); return

    # ── Regime gate ────────────────────────────────────────────────────────
    regime = sg.detect_regime(quotes)

    # ── Trailing stop check ────────────────────────────────────────────────
    # Runs BEFORE the kill switch: risk exits must keep working on the worst
    # days. The kill switch only halts NEW risk, never position management.
    stopped_out = _check_trailing_stops(client, positions, quotes, model, regime=regime)
    if stopped_out:
        positions = client.get_positions()  # refresh after exits

    # ── Kill switch (blocks new entries/rebalancing, not exits) ────────────
    if _check_kill_switch(account, model):
        return

    # ── EOD window (#5): 15:40-16:00 ET manages exits only ─────────────────
    # Previously the trader went fully dark after 15:40, leaving stops
    # unmanaged for the final 20 minutes of the session.
    if is_eod():
        log.info("EOD window (≥15:40 ET): exits managed above; no new risk.")
        return

    # ── QQQ core sleeve ────────────────────────────────────────────────────
    if _rebalance_core_sleeve(client, account, positions, quotes, regime):
        positions = client.get_positions()
        account = client.get_account()

    satellite_positions = {
        sym: pos for sym, pos in (positions or {}).items()
        if sym.upper() not in CORE_SYMBOLS
    }
    max_positions = min(int(model.get("max_positions", MAX_POSITIONS)), int(config.MAX_AUTONOMOUS_POSITIONS))
    min_conviction = int(model.get("min_conviction", sg.MIN_CONVICTION))
    if regime == "CHOPPY":
        min_conviction = max(min_conviction, int(config.CHOPPY_ENTRY_MIN_SCORE))
    log.info("Regime: %s | Satellite positions: %d/%d | Total positions: %d | Equity: $%.2f | Model gen=%s min=%d size=%.2f%%",
             regime, len(satellite_positions), max_positions, len(positions), equity,
             model.get("generation"), min_conviction, float(model.get("position_size_pct", POSITION_SIZE_PCT)) * 100)

    # ── Kill gate: opening window (#54) ───────────────────────────────────
    if _in_opening_window(quotes):
        log.info("Opening window gate: first %dm after open — no new entries (use volume surge to bypass).",
                 OPENING_WINDOW_MINS)
        return

    if _edge_gate_blocks_entries():
        return

    if _recent_buy_blocked():
        return

    if regime != "BEAR" and _market_down_day_blocks_longs(quotes):
        return

    if not bool(getattr(config, "SATELLITE_TRADING_ENABLED", False)):
        log.info("Satellite stock entries disabled — QQQ core sleeve only.")
        return

    # ── Strategy router (#55) ──────────────────────────────────────────────
    # CHOPPY → mean reversion (oversold bounce); BULL/BEAR → momentum.
    bear_etf_min_conviction = int(model.get("bear_etf_min_conviction", sg.BEAR_ETF_MIN_CONVICTION))
    if regime == "BEAR" and bool(getattr(config, "ALPHA_BEAR_STAY_IN_CASH", False)):
        log.info("BEAR regime alpha reset: stay in cash, no fresh longs.")
        return

    # Active variant from the model actually drives signal weights now.
    # Previously the optimizer could "win" a variant that live trading never
    # used because get_signals() was always called with the default profile.
    profile_name = str(model.get("active_variant") or "current")
    if profile_name not in sg.VARIANT_PROFILES:
        profile_name = "current"

    if regime == "CHOPPY" and not bool(getattr(config, "ALPHA_DISABLE_REVERSION", False)):
        trade_signals = sg.get_signals_reversion(quotes, min_conviction=min_conviction)
        log.info("CHOPPY → reversion strategy: %s", [(s.symbol, s.score) for s in trade_signals])
    else:
        trade_signals = sg.get_signals(quotes, profile_name=profile_name, min_conviction=min_conviction, bear_etf_min_conviction=bear_etf_min_conviction)
        log.info("Signals (variant=%s): %s", profile_name, [(s.symbol, s.score) for s in trade_signals])

    rotated_out = _rotate_stale_positions(client, positions, quotes, model, regime, trade_signals)
    if rotated_out:
        positions = client.get_positions()
        satellite_positions = {
            sym: pos for sym, pos in (positions or {}).items()
            if sym.upper() not in CORE_SYMBOLS
        }

    if not trade_signals:
        log.info("No conviction signals (min=%d) — staying in cash.", min_conviction)
        return

    # ── Pyramid adds: scale into existing winners ──────────────────────────
    # Runs BEFORE new entries so we add to winners even when at max_positions.
    pyramid_trigger = float(model.get("pyramid_trigger_pct", 0.5))
    max_pyramid_adds = int(model.get("max_pyramid_adds", 1))
    use_conviction_scale = bool(model.get("conviction_size_scale", True))
    pyramid_memory = strategy_model.load_position_memory()
    pyramid_added: list[str] = []

    # Pyramiding disabled (#19): avg_win $22 / avg_loss $44 means scaling into
    # winners just concentrates risk in a negative-expectancy system. Re-enable
    # only after backtest shows positive expectancy over ≥3 months.
    PYRAMIDING_ENABLED = False

    if PYRAMIDING_ENABLED and pyramid_trigger > 0 and max_pyramid_adds > 0:
        for sym, pos in list(positions.items()):
            sym = sym.upper()
            if sym in config.IGNORED_POSITIONS:
                continue

            pnl_pct = float(pos.get("unrealized_plpc") or 0) * 100
            if pnl_pct < pyramid_trigger:
                continue  # not profitable enough yet

            mem = pyramid_memory.get(sym) or {}
            addon_count = int(mem.get("pyramid_add_count") or 0)
            if addon_count >= max_pyramid_adds:
                continue  # already added max times today

            # Don't pyramid into a position in "free ride" mode (already partialed)
            if mem.get("partial_exit_done"):
                continue

            price_now  = float(quotes.get(sym, {}).get("price") or 0)
            entry_price = float(mem.get("entry_price") or 0)
            if price_now <= 0:
                continue
            if entry_price > 0 and price_now <= entry_price:
                continue  # only add ABOVE entry — never average down

            # Score still needs to be at threshold
            current_score, _ = sg.score_symbol(sym, quotes, regime)
            if current_score < min_conviction:
                continue

            # Size the add-on using conviction scaling, same as initial entry
            regime_size_mult = 0.5 if regime == "CHOPPY" else 1.0
            conv_mult = _conviction_size_mult(current_score) if use_conviction_scale else 1.0
            addon_qty = _calc_qty(price_now, equity, model, size_mult=conv_mult * regime_size_mult)

            # Hard cap: total position must stay within max_single_position_pct
            max_pos_pct = min(float(model.get("max_single_position_pct", MAX_SINGLE_POS_PCT)), MAX_SINGLE_POS_PCT)
            current_value = float(pos.get("market_value") or 0) or (float(pos.get("qty") or 0) * price_now)
            headroom = equity * max_pos_pct - current_value
            max_qty_by_cap = int(headroom / price_now) if price_now > 0 and headroom > 0 else 0
            addon_qty = min(addon_qty, max_qty_by_cap)

            if addon_qty <= 0:
                log.info("Pyramid: %s at cap (%.1f%% of equity) — skipping add", sym, current_value / equity * 100)
                continue

            try:
                _submit_market_order(
                    client, sym, addon_qty, "buy",
                    intent="pyramid_add", regime=regime, model=model,
                    signal_score=current_score,
                    signal_snapshot={"strategy": "momentum", "pyramid_add": addon_count + 1},
                )
                notify.trade_pyramid(
                    sym, addon_qty, price_now, current_score, regime,
                    pnl_pct=pnl_pct, entry_price=entry_price,
                    add_num=addon_count + 1,
                )
                log.info(
                    "PYRAMID ADD %d/%d: BUY %d %s @ $%.2f  pnl=%.2f%%  score=%d  entry=$%.2f",
                    addon_count + 1, max_pyramid_adds, addon_qty, sym, price_now,
                    pnl_pct, current_score, entry_price,
                )
                mem["pyramid_add_count"] = addon_count + 1
                mem["pyramid_add_price"] = price_now
                pyramid_memory[sym] = mem
                pyramid_added.append(sym)
            except Exception as e:
                log.error("Pyramid add failed for %s: %s", sym, e)

    if pyramid_added:
        strategy_model.save_position_memory(pyramid_memory)
        positions = client.get_positions()  # refresh after pyramid adds

    # ── Enter new positions ────────────────────────────────────────────────
    # Kill gate: consecutive losses (#20)
    if _consecutive_loss_halt():
        return

    bought_list: list[tuple[str, int, float, int]] = []  # (sym, qty, price, score)

    # Buying-power guard (#6): size off equity but never submit more than the
    # account can fund — Alpaca rejects those orders and the tick is wasted.
    available_funds = float(account.get("buying_power") or account.get("cash") or 0)

    for sig in trade_signals:
        if len(bought_list) >= int(config.MAX_NEW_ENTRIES_PER_TICK):
            log.info("Entry throttle: max %d new entry per tick reached.", config.MAX_NEW_ENTRIES_PER_TICK)
            break
        if len(satellite_positions) >= max_positions:
            log.info("Max positions (%d) reached.", max_positions); break
        if sig.symbol in positions:
            continue  # already holding
        if learning_agent.is_symbol_blocked(sig.symbol):
            log.info("Learned blocklist: skipping %s (persistent negative expectancy).", sig.symbol)
            continue
        blocked, cooldown_reason = _reentry_block_status(sig.symbol)
        if blocked:
            log.info("Re-entry blocked for %s: %s", sig.symbol, cooldown_reason)
            continue
        entry_blocked, entry_reason = _entry_limit_block_status(sig.symbol)
        if entry_blocked:
            log.info("Daily entry limit blocked for %s: %s", sig.symbol, entry_reason)
            continue

        quote = quotes.get(sig.symbol, {})
        price = quote.get("price", 0)
        if price <= 0:
            log.warning("No price for %s — skipping.", sig.symbol); continue

        # Kill gate: stale quote (#20)
        if _quote_is_stale(quote):
            log.warning("Stale quote for %s (>%ds) — skipping entry.", sig.symbol, STALE_QUOTE_SECS)
            continue

        # Kill gate: wide spread (#20)
        if _spread_too_wide(quote):
            log.warning("Wide spread for %s (>%.1f%%) — skipping entry.", sig.symbol, MAX_SPREAD_PCT)
            continue

        # Kill gate: 5-min intraday confirmation (#60)
        if not _intraday_confirms_entry(sig.symbol, client):
            log.info("Intraday gate blocked entry for %s — skipping.", sig.symbol)
            continue

        # Conviction-scaled size: high score = bigger initial position
        conv_mult = _conviction_size_mult(sig.score) if use_conviction_scale else 1.0
        total_size_mult = sig.size_mult * conv_mult

        # ATR stop distance (%) from the signal — drives risk-based sizing and
        # is persisted so the exit stack enforces the same stop it was sized on.
        entry_stop_pct = 0.0
        atr_stop = float(getattr(sig, "atr_stop", 0) or 0)
        if atr_stop > 0 and price > 0 and atr_stop < price:
            entry_stop_pct = (price - atr_stop) / price * 100

        # Hard cap: never exceed max_single_position_pct of equity in one name
        max_pos_pct = min(float(model.get("max_single_position_pct", MAX_SINGLE_POS_PCT)), MAX_SINGLE_POS_PCT)
        max_qty_by_cap = int(equity * max_pos_pct / price) if price > 0 else 0
        qty = min(
            _calc_qty(price, equity, model, size_mult=total_size_mult,
                      stop_pct=entry_stop_pct or None),
            max_qty_by_cap,
        )

        # Buying-power cap (#6)
        max_qty_by_funds = int(available_funds * 0.98 / price) if price > 0 else 0
        if qty > max_qty_by_funds:
            log.info("Funding cap: %s qty %d → %d (available ~$%.0f).",
                     sig.symbol, qty, max_qty_by_funds, available_funds)
            qty = max_qty_by_funds
        if qty <= 0:
            continue

        try:
            _submit_market_order(
                client, sig.symbol, qty, "buy",
                intent="entry", regime=sig.regime, model=model,
                signal_score=sig.score,
                signal_snapshot={
                    "symbol": sig.symbol,
                    "score": sig.score,
                    "regime": sig.regime,
                    "size_mult": sig.size_mult,
                    "strategy": getattr(sig, "strategy", "momentum"),
                },
            )
            notify.trade_buy(sig.symbol, qty, price, sig.score, sig.regime)
            log.info("BUY %d %s @ $%.2f  score=%d  regime=%s  conv_mult=%.1fx  size=$%.0f",
                     qty, sig.symbol, price, sig.score, sig.regime, conv_mult, qty * price)
            positions[sig.symbol] = {"qty": qty, "entry": price,
                                     "unrealized_pl": 0, "unrealized_plpc": 0}
            satellite_positions[sig.symbol] = positions[sig.symbol]
            _record_position_strategy(
                sig.symbol, getattr(sig, "strategy", "momentum"),
                stop_pct=entry_stop_pct or None,
            )
            available_funds -= qty * price
            bought_list.append((sig.symbol, qty, price, sig.score))
        except Exception as e:
            log.error("Buy order failed for %s: %s", sig.symbol, e)

    # ── Post-buy summary (one message covering all new entries this tick) ──
    if bought_list:
        lines = "\n".join(
            f"  {sym}: {qty}sh @ ${price:.2f}  (${qty*price:,.0f})  score={score}"
            for sym, qty, price, score in bought_list
        )
        notify.send(
            f"🟢 Opened {len(bought_list)} new position(s) · Regime: {regime}\n"
            f"{lines}\n"
            f"Satellite held: {len(satellite_positions)}  ·  Total held: {len(positions)}  ·  Equity: ${equity:,.0f}"
        )


_HEALTH_PATH = strategy_model.STATE_DIR / "trader_health.json"


def _write_health(status: str, detail: str = "") -> None:
    """Write a health state file so the server /api/lab/health endpoint can surface it."""
    try:
        strategy_model.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _HEALTH_PATH.write_text(
            json.dumps({
                "status":    status,          # "ok" | "error"
                "detail":    detail,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
        _write_health("ok")
    except Exception as _crash_exc:
        _msg = f"🚨 TRADER CRASH: {type(_crash_exc).__name__}: {_crash_exc}"
        log.exception("Trader crashed")
        try:
            notify.send(_msg)
        except Exception:
            pass
        _write_health("error", str(_crash_exc))
        raise
