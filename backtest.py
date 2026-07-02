"""
backtest.py — Daily variant optimizer shared between server.py and trader.py.

Extracted from BacktestAgent in server.py to avoid circular imports:
  server.py imports trader (for fetch_quotes)
  trader.py needs backtest logic
  → both import from this module instead.

Public API:
  load_daily_replay_data(period)           → list[dict]
  score_candidate(model, rows)             → dict
  score_candidate_variant(model, rows, ind_weights, etf_only) → dict
  simulated_exit_price(sym, rows, entry_row, entry, model)    → float
  run_variants(period, base_model)         → dict  ← main entry point
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
import signals as sg
import strategy_model
from alpaca_client import AlpacaClient

# Realistic fill assumptions: 0.05% slippage each side (bid/ask + market impact).
# Alpaca charges $0 commission. Total round-trip cost = 0.10%.
_SLIPPAGE_BPS = 5   # basis points per side (0.05%)
_SLIPPAGE_RT  = _SLIPPAGE_BPS * 2 / 10_000  # round-trip fraction deducted from gross P&L


def _period_to_limit(period: str) -> int:
    return {
        "1D": 5,
        "7D": 10,
        "15D": 20,
        "1M": 35,
        "3M": 90,
        "6M": 180,
        "1A": 260,
        "all": 520,
    }.get(str(period or "3M"), 90)


def _frame_rows(frame) -> list[dict]:
    rows: list[dict] = []
    if frame is None or getattr(frame, "empty", True):
        return rows
    for _, row in frame.iterrows():
        close = float(row.get("close", 0) or 0)
        if close <= 0:
            continue
        rows.append({
            "timestamp": row.get("timestamp"),
            "open":   float(row.get("open", close) or close),
            "high":   float(row.get("high", close) or close),
            "low":    float(row.get("low", close) or close),
            "close":  close,
            "volume": float(row.get("volume", 0) or 0),
            "vwap":   float(row.get("vwap", 0) or 0),
        })
    return rows


def _row_date(row: dict, fallback: int) -> str:
    ts = row.get("timestamp")
    if hasattr(ts, "date"):
        return str(ts.date())
    if isinstance(ts, str) and ts:
        return ts[:10]
    return f"bar-{fallback}"


def _to_date_key(ts, fallback: int) -> str:
    if hasattr(ts, "date"):
        return str(ts.date())
    if isinstance(ts, str) and ts:
        return ts[:10]
    return f"bar-{fallback}"


def _bar_quote(row: dict, prev_close: float) -> dict:
    close = float(row.get("close") or 0)
    open_px = float(row.get("open") or close)
    high = float(row.get("high") or max(open_px, close))
    low = float(row.get("low") or min(open_px, close))
    return {
        "price":      round(close, 4),
        "open":       round(open_px, 4),
        "high":       round(high, 4),
        "low":        round(low, 4),
        "close":      round(close, 4),
        "prev_close": round(prev_close, 4),
        "change_pct": round((close - prev_close) / prev_close * 100, 4) if prev_close > 0 else 0.0,
        "volume":     float(row.get("volume", 0) or 0),
    }


def load_daily_replay_data(period: str = "3M", client: AlpacaClient | None = None) -> list[dict]:
    """
    Load Alpaca daily OHLCV history for ALL_SYMBOLS, compute technicals for each
    bar, and return a list of {date, quotes, next_quotes} day-rows.
    """
    try:
        client = client or AlpacaClient()
        raw = client.get_historical_bars(
            sg.ALL_SYMBOLS,
            timeframe="1day",
            limit=_period_to_limit(period) + 2,
        )
    except Exception:
        return []

    bars_by_symbol = {
        sym: _frame_rows(frame)
        for sym, frame in (raw or {}).items()
    }
    bars_by_symbol = {sym: rows for sym, rows in bars_by_symbol.items() if len(rows) >= 3}
    if not bars_by_symbol:
        return []

    max_len = max(len(rows) for rows in bars_by_symbol.values())
    rows: list[dict] = []
    for idx in range(1, max_len - 1):
        day_quotes: dict[str, dict] = {}
        next_quotes: dict[str, dict] = {}
        for sym in sg.ALL_SYMBOLS:
            sym_rows = bars_by_symbol.get(sym)
            if not sym_rows or idx >= len(sym_rows) - 1:
                continue
            try:
                prev_close = float(sym_rows[idx - 1]["close"])
                close = float(sym_rows[idx]["close"])
                next_close = float(sym_rows[idx + 1]["close"])
                if prev_close <= 0 or close <= 0:
                    continue
                bars = sym_rows[:idx + 1]
                day_quotes[sym] = {
                    "price":      round(close, 4),
                    "prev_close": round(prev_close, 4),
                    "change_pct": round((close - prev_close) / prev_close * 100, 4),
                }
                technicals = sg.compute_technicals(bars)
                if technicals:
                    day_quotes[sym]["technicals"] = technicals
                next_quotes[sym] = {"price": round(next_close, 4)}
            except Exception:
                continue

        # Compute relative strength vs QQQ for each symbol
        qqq_tech = (day_quotes.get("QQQ") or {}).get("technicals") or {}
        qqq_return = qqq_tech.get("return_5d_pct")
        if qqq_return is not None:
            for sym, row in list(day_quotes.items()):
                tech = dict((row or {}).get("technicals") or {})
                if "return_5d_pct" in tech:
                    tech["relative_strength_qqq"] = round(
                        float(tech["return_5d_pct"]) - float(qqq_return), 3
                    )
                    row["technicals"] = tech

        if "QQQ" in day_quotes and "SPY" in day_quotes:
            qqq_rows = bars_by_symbol.get("QQQ") or []
            rows.append({
                "date":        _row_date(qqq_rows[idx] if idx < len(qqq_rows) else {}, idx),
                "quotes":      day_quotes,
                "next_quotes": next_quotes,
            })
    return rows


# ── v5 parity helpers ─────────────────────────────────────────────────────────
# The live engine (trader.py) fixes an ATR-based stop at entry and sizes each
# trade to equal dollar risk. The backtest must simulate the same strategy or
# the weekly optimizer tunes parameters for a system that is never traded.

def _v5_entry_stop_pct(quotes: dict, symbol: str, model: dict) -> float:
    """Stop distance (%) fixed at entry: 1.5×ATR clamped to [1%, 5%].

    Mirrors signals.get_signals() atr_stop + trader._check_trailing_stops()
    bounds. Falls back to the model's flat trailing stop when no ATR exists.
    """
    tech = (quotes.get(symbol) or {}).get("technicals") or {}
    atr_pct = float(tech.get("atr_pct") or 0)
    if atr_pct > 0:
        return min(max(atr_pct * 1.5, 1.0), 5.0)
    return float(model.get("trailing_stop_pct", 3.0))


def _v5_allocation_pct(stop_pct: float, model: dict) -> float:
    """Fraction of equity per position under equal-dollar-risk sizing.

    notional% = RISK_PER_TRADE_PCT / stop%, capped at MAX_POSITION_SIZE_PCT.
    Matches trader._calc_qty(); falls back to fixed notional if risk sizing
    is disabled in config.
    """
    max_pct = float(getattr(config, "MAX_POSITION_SIZE_PCT", 0.18))
    risk_pct = float(getattr(config, "RISK_PER_TRADE_PCT", 0.0) or 0.0)
    if risk_pct > 0 and stop_pct > 0:
        return min((risk_pct / 100.0) / (stop_pct / 100.0), max_pct)
    return min(float(model.get("position_size_pct", 0.16)), max_pct)


def simulated_exit_price(
    symbol: str, rows: list[dict], entry_row: dict, entry: float, model: dict,
    stop_pct: float | None = None,
) -> float:
    """Simulate the live exit stack for one position, mirroring trader.py.

    Gates evaluated each day in priority order:
      1. Hard stop:       ret_pct ≤ -stop (ATR-based per position, v5)
      2. EOD flat:        modelled as max_holding_days expiry (gate 4)
      3. Regime flip:     skipped in backtest (no positional direction info)
      3b. Profit roundtrip: peak ≥ lock_trigger AND ret ≤ 0 → breakeven exit
      4. Max hold:        hold ≥ max_holding_days → exit at close
      5a. Profit target:  ret_pct ≥ profit_target_pct → exit immediately
      5b. Trailing stop:  peak_ret ≥ lock_trigger AND giveback ≥ giveback_limit
    """
    try:
        start_index = rows.index(entry_row)
    except ValueError:
        start_index = 0

    max_days       = int(model.get("max_holding_days", 2))
    hard_stop_pct  = float(stop_pct) if stop_pct and stop_pct > 0 else float(model.get("trailing_stop_pct", 3.0))
    profit_target  = float(model.get("profit_target_pct", 3.0))
    lock_trigger   = float(model.get("profit_lock_trigger_pct", profit_target))
    giveback_limit = float(model.get("profit_giveback_pct", 1.0))
    peak_return    = 0.0
    last_price     = entry

    for offset in range(1, max_days + 1):
        idx = min(start_index + offset, len(rows) - 1)
        price = float((rows[idx].get("quotes") or {}).get(symbol, {}).get("price") or last_price)
        if price <= 0:
            price = last_price
        ret_pct = (price - entry) / entry * 100 if entry else 0.0
        peak_return = max(peak_return, ret_pct)
        last_price = price

        # Gate 1: hard stop
        if ret_pct <= -hard_stop_pct:
            return price

        # Gate 3b: profit roundtrip — a locked winner must not go red (v5 parity)
        if peak_return >= lock_trigger and ret_pct <= 0:
            return price

        # Gate 4: max hold (acts as EOD flat proxy)
        if offset >= max_days:
            return price

        # Gate 5a: profit target
        if ret_pct >= profit_target:
            return price

        # Gate 5b: trailing stop once lock trigger reached
        if peak_return >= lock_trigger and (peak_return - ret_pct) >= giveback_limit:
            return price

        if idx >= len(rows) - 1:
            return price

    return last_price


# Legacy fixed cap — superseded by _v5_allocation_pct() (equal-dollar-risk
# sizing capped at config.MAX_POSITION_SIZE_PCT, matching the live engine).
# The old 0.03 cap made the backtest trade at 1/6th of live size, which muted
# drawdowns and distorted variant ranking.
_BACKTEST_MAX_SIZE_PCT = float(getattr(config, "MAX_POSITION_SIZE_PCT", 0.18))


def _simulate_day(row: dict, rows: list[dict], model: dict, ind_weights: dict, etf_only: bool) -> tuple[float, int]:
    """Simulate one trading day. Returns (day_pnl, trades_executed).

    Parity with trader.py (#22):
    - CHOPPY is no longer blocked; entries proceed at 1.0× size (with 3% cap)
    - Sizing capped at _BACKTEST_MAX_SIZE_PCT regardless of model param
    - Exit logic uses same 5-gate stack as trader.py via simulated_exit_price()
    """
    quotes = row["quotes"]
    regime = sg.detect_regime(quotes)

    day_pnl   = 0.0
    trades    = 0
    # CHOPPY: same size as BULL/BEAR — the 3% cap is what limits exposure,
    # not a regime-based multiplier. (Previously CHOPPY returned 0 here, which
    # caused the optimizer to train on a phantom strategy that skipped CHOPPY.)
    size_mult = sg.regime_size_multiplier(regime)   # returns 1.0 for all regimes

    universe = (sg.BEAR_UNIVERSE + sg.MOMENTUM_STOCKS) if regime == "BEAR" else sg.BULL_UNIVERSE
    if etf_only:
        etf_set  = set(sg.BULL_ETF + sg.BEAR_ETF)
        universe = [s for s in universe if s in etf_set]

    bear_etf_conv = int(model.get("bear_etf_min_conviction", sg.BEAR_ETF_MIN_CONVICTION))
    signals = []
    for sym in universe:
        score, _ = sg.score_symbol(sym, quotes, regime, weights=ind_weights)
        threshold = bear_etf_conv if (regime == "BEAR" and sym in sg.BEAR_ETF) else int(model["min_conviction"])
        if score >= threshold:
            signals.append((sym, score))
    signals.sort(key=lambda item: item[1], reverse=True)
    selected = signals[:int(model["max_positions"])]

    equity = 100_000.0  # reference — pnl is proportional
    for sym, _ in selected:
        entry = float(quotes.get(sym, {}).get("price") or 0)
        if entry <= 0:
            continue
        # v5 parity: ATR stop fixed at entry drives both the exit and the size.
        stop_pct = _v5_entry_stop_pct(quotes, sym, model)
        exit_px = simulated_exit_price(sym, rows, row, entry, model, stop_pct=stop_pct)
        if exit_px <= 0:
            continue
        allocation  = equity * _v5_allocation_pct(stop_pct, model) * size_mult
        gross_ret   = (exit_px - entry) / entry
        net_ret     = gross_ret - _SLIPPAGE_RT   # subtract round-trip slippage
        raw_pnl     = allocation * net_ret
        stop_loss   = -allocation * (stop_pct / 100)
        day_pnl    += max(raw_pnl, stop_loss)
        trades     += 1

    return day_pnl, trades


def _signal_candidates(row: dict, model: dict, ind_weights: dict, etf_only: bool) -> list[tuple[str, int]]:
    quotes = row.get("quotes") or {}
    regime = sg.detect_regime(quotes)
    universe = (sg.BEAR_UNIVERSE + sg.MOMENTUM_STOCKS) if regime == "BEAR" else sg.BULL_UNIVERSE
    if etf_only:
        etf_set = set(sg.BULL_ETF + sg.BEAR_ETF)
        universe = [s for s in universe if s in etf_set]

    bear_etf_conv = int(model.get("bear_etf_min_conviction", sg.BEAR_ETF_MIN_CONVICTION))
    out: list[tuple[str, int]] = []
    for sym in universe:
        score, _ = sg.score_symbol(sym, quotes, regime, weights=ind_weights)
        threshold = bear_etf_conv if (regime == "BEAR" and sym in sg.BEAR_ETF) else int(model["min_conviction"])
        if score >= threshold:
            out.append((sym, score))
    return sorted(out, key=lambda item: item[1], reverse=True)


def _mark_to_market(cash: float, positions: dict[str, dict], row: dict) -> float:
    quotes = row.get("quotes") or {}
    value = cash
    for sym, pos in positions.items():
        price = float((quotes.get(sym) or {}).get("close") or (quotes.get(sym) or {}).get("price") or pos["entry_price"])
        value += float(pos["qty"]) * max(price, 0.0)
    return value


def _simulate_portfolio(model: dict, rows: list[dict], ind_weights: dict, etf_only: bool = False) -> dict:
    """Stateful walk-forward simulator.

    Signals are computed from today's close and filled on the next trading day's
    open. Positions occupy slots until a stop/target/trailing/max-hold exit
    closes them. This is intentionally more conservative than score_candidate()
    because it is used for validation, not fast variant ranking.
    """
    model = strategy_model.sanitize_model(model)
    cash = 100_000.0
    positions: dict[str, dict] = {}
    pending_entries: list[dict] = []
    peak_equity = cash
    max_drawdown = 0.0
    total_entries = 0
    closed_trades = 0
    winning_trades = 0
    losing_trades = 0
    winning_days = 0
    losing_days = 0
    daily_returns: list[float] = []
    last_equity = cash

    max_positions = int(model["max_positions"])
    max_days = int(model.get("max_holding_days", 2))
    hard_stop_pct = float(model.get("trailing_stop_pct", 3.0))
    profit_target = float(model.get("profit_target_pct", 3.0))
    lock_trigger = float(model.get("profit_lock_trigger_pct", profit_target))
    giveback_limit = float(model.get("profit_giveback_pct", 1.0))
    buy_slip = _SLIPPAGE_BPS / 10_000
    sell_slip = _SLIPPAGE_BPS / 10_000

    for idx, row in enumerate(rows):
        quotes = row.get("quotes") or {}

        # Fill orders generated from the previous close at today's open.
        next_pending: list[dict] = []
        for order in pending_entries:
            sym = order["symbol"]
            if sym in positions or len(positions) >= max_positions:
                continue
            q = quotes.get(sym) or {}
            open_px = float(q.get("open") or q.get("price") or 0)
            if open_px <= 0:
                next_pending.append(order)
                continue
            equity_for_size = max(_mark_to_market(cash, positions, row), 1.0)
            # v5 parity: ATR stop from the signal day fixes both size and stop.
            stop_pct = float(order.get("stop_pct") or 0) or hard_stop_pct
            allocation = min(
                equity_for_size * _v5_allocation_pct(stop_pct, model),
                cash * 0.98,
            )
            if allocation <= 0:
                continue
            fill_px = open_px * (1 + buy_slip)
            qty = allocation / fill_px
            cost = qty * fill_px
            if qty <= 0 or cost > cash:
                continue
            cash -= cost
            positions[sym] = {
                "symbol": sym,
                "qty": qty,
                "entry_price": fill_px,
                "entry_cost": cost,
                "entry_index": idx,
                "entry_date": row.get("date"),
                "peak_return_pct": 0.0,
                "stop_pct": stop_pct,
            }
            total_entries += 1
        pending_entries = next_pending

        # Process exits using current day's OHLC.
        for sym, pos in list(positions.items()):
            q = quotes.get(sym) or {}
            if not q:
                continue
            open_px = float(q.get("open") or q.get("price") or pos["entry_price"])
            high = float(q.get("high") or q.get("price") or pos["entry_price"])
            low = float(q.get("low") or q.get("price") or pos["entry_price"])
            close = float(q.get("close") or q.get("price") or pos["entry_price"])
            entry_px = float(pos["entry_price"])
            pos_stop_pct = float(pos.get("stop_pct") or 0) or hard_stop_pct
            stop_px = entry_px * (1 - pos_stop_pct / 100)
            target_px = entry_px * (1 + profit_target / 100)
            exit_px = 0.0

            high_ret = (high - entry_px) / entry_px * 100 if entry_px else 0.0
            close_ret = (close - entry_px) / entry_px * 100 if entry_px else 0.0
            pos["peak_return_pct"] = max(float(pos.get("peak_return_pct") or 0), high_ret)
            peak_ret = float(pos.get("peak_return_pct") or 0)
            age = idx - int(pos["entry_index"])

            if low <= stop_px:
                exit_px = min(open_px, stop_px) if open_px < stop_px else stop_px
            elif high >= target_px:
                exit_px = target_px
            elif peak_ret >= lock_trigger and close_ret <= 0:
                # v5 profit-roundtrip gate: locked winners never close red
                exit_px = close
            elif peak_ret >= lock_trigger and (peak_ret - close_ret) >= giveback_limit:
                exit_px = close
            elif age >= max_days:
                exit_px = close
            elif idx >= len(rows) - 1:
                exit_px = close

            if exit_px > 0:
                proceeds = float(pos["qty"]) * exit_px * (1 - sell_slip)
                cash += proceeds
                pnl = proceeds - float(pos["entry_cost"])
                closed_trades += 1
                if pnl > 0:
                    winning_trades += 1
                elif pnl < 0:
                    losing_trades += 1
                del positions[sym]

        end_equity = _mark_to_market(cash, positions, row)
        peak_equity = max(peak_equity, end_equity)
        max_drawdown = max(max_drawdown, (peak_equity - end_equity) / peak_equity * 100 if peak_equity else 0)
        day_return = (end_equity - last_equity) / max(last_equity, 1) * 100
        daily_returns.append(day_return)
        if end_equity > last_equity:
            winning_days += 1
        elif end_equity < last_equity:
            losing_days += 1
        last_equity = end_equity

        # Generate signals from today's close for tomorrow's open.
        if idx < len(rows) - 1:
            held_or_pending = set(positions.keys()) | {o["symbol"] for o in pending_entries}
            open_slots = max_positions - len(held_or_pending)
            if open_slots > 0 and cash > 0:
                for sym, score in _signal_candidates(row, model, ind_weights, etf_only):
                    if sym in held_or_pending:
                        continue
                    if sym not in (rows[idx + 1].get("quotes") or {}):
                        continue
                    pending_entries.append({
                        "symbol": sym, "score": score, "signal_date": row.get("date"),
                        "stop_pct": _v5_entry_stop_pct(quotes, sym, model),
                    })
                    held_or_pending.add(sym)
                    open_slots -= 1
                    if open_slots <= 0:
                        break

    total_return_pct = (last_equity - 100_000.0) / 100_000.0 * 100
    daily_return_pct = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
    trade_penalty = max(0, total_entries - len(rows) * max_positions * 0.35) * 0.01
    drawdown_penalty = max_drawdown * 0.7
    objective = daily_return_pct * 10 + total_return_pct - drawdown_penalty - trade_penalty

    return {
        "model": model,
        "objective": round(objective, 4),
        "total_return_pct": round(total_return_pct, 2),
        "daily_return_pct": round(daily_return_pct, 3),
        "max_drawdown_pct": round(max_drawdown, 2),
        "trades": closed_trades,
        "entries": total_entries,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "winning_days": winning_days,
        "losing_days": losing_days,
        "ending_equity": round(last_equity, 2),
    }


def score_candidate(model: dict, rows: list[dict]) -> dict:
    """Score an anonymous parameter-set model using default indicator weights."""
    return score_candidate_variant(model, rows, {}, False)


def score_candidate_variant(
    model: dict, rows: list[dict], ind_weights: dict, etf_only: bool = False
) -> dict:
    """
    Simulate a full period with `model` params and `ind_weights` indicator weights.
    Returns performance metrics and an objective score.

    Objective = daily_return_pct * 10 + total_return_pct − drawdown_penalty − trade_penalty
    """
    model = strategy_model.sanitize_model(model)
    equity      = 100_000.0
    peak        = equity
    max_drawdown = 0.0
    total_trades = 0
    winning_days = 0
    losing_days  = 0
    daily_returns: list[float] = []

    for row in rows:
        day_pnl, trades = _simulate_day(row, rows, model, ind_weights, etf_only)
        equity      += day_pnl
        peak         = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak else 0)
        day_return   = day_pnl / max(equity - day_pnl, 1) * 100
        daily_returns.append(day_return)
        total_trades += trades
        if day_pnl > 0:
            winning_days += 1
        elif day_pnl < 0:
            losing_days += 1

    total_return_pct  = (equity - 100_000.0) / 100_000.0 * 100
    daily_return_pct  = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
    trade_penalty     = max(0, total_trades - len(rows) * int(model["max_positions"]) * 0.55) * 0.01
    drawdown_penalty  = max_drawdown * 0.7
    objective         = daily_return_pct * 10 + total_return_pct - drawdown_penalty - trade_penalty

    return {
        "model":             model,
        "objective":         round(objective, 4),
        "total_return_pct":  round(total_return_pct, 2),
        "daily_return_pct":  round(daily_return_pct, 3),
        "max_drawdown_pct":  round(max_drawdown, 2),
        "trades":            total_trades,
        "winning_days":      winning_days,
        "losing_days":       losing_days,
    }


def run_variants(period: str = "1M", base_model: dict | None = None) -> dict:
    """
    Score all named VARIANT_PROFILES on daily replay data.
    Records the winner to variant history and returns a ranked result dict.

    Returns:
    {
      ok, period, days, run_at,
      variants: {name: score_dict},
      ranked:   [(name, objective), ...],   # best first
      winner:   str,
      winner_objective: float,
    }
    """
    if base_model is None:
        base_model = strategy_model.load_model()

    data = load_daily_replay_data(period)
    if len(data) < 3:
        return {
            "ok":      False,
            "error":   "Not enough market data for variant comparison.",
            "variants": {},
            "ranked":  [],
            "winner":  "current",
            "winner_objective": 0.0,
            "days":    len(data),
        }

    results: dict[str, dict] = {}

    for variant_name, profile in sg.VARIANT_PROFILES.items():
        # Build variant model: start from base, apply param overrides
        variant_model = dict(base_model)
        if "conviction_override" in profile:
            variant_model["min_conviction"] = int(profile["conviction_override"])
        if "size_mult" in profile:
            base_size = float(strategy_model.DEFAULT_MODEL["position_size_pct"])
            variant_model["position_size_pct"] = round(base_size * float(profile["size_mult"]), 4)
        if "max_pos_override" in profile:
            variant_model["max_positions"] = int(profile["max_pos_override"])
        if "hold_days_override" in profile:
            variant_model["max_holding_days"] = int(profile["hold_days_override"])
        if "stop_pct_override" in profile:
            variant_model["trailing_stop_pct"] = float(profile["stop_pct_override"])
        if "lock_trigger_override" in profile:
            variant_model["profit_lock_trigger_pct"] = float(profile["lock_trigger_override"])

        # Extract indicator weights (exclude param-only keys)
        ind_weights = {k: v for k, v in profile.items() if k not in sg._PROFILE_PARAM_KEYS}
        etf_only    = bool(profile.get("etf_only", False))

        result = score_candidate_variant(variant_model, data, ind_weights, etf_only)
        result["variant"] = variant_name
        results[variant_name] = result

    ranked = sorted(results.items(), key=lambda x: x[1]["objective"], reverse=True)
    winner = ranked[0][0]

    # Persist winner for today
    today = datetime.now(timezone.utc).date().isoformat()
    strategy_model.record_variant_result(
        today, winner,
        {name: r["objective"] for name, r in results.items()}
    )

    return {
        "ok":               True,
        "period":           period,
        "days":             len(data),
        "run_at":           datetime.now(timezone.utc).isoformat(),
        "variants":         results,
        "ranked":           [(name, round(r["objective"], 4)) for name, r in ranked],
        "winner":           winner,
        "winner_objective": results[winner]["objective"],
    }


# ── Walk-forward validation ───────────────────────────────────────────────────

def load_historical_data_yf(
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> dict[str, list[dict]]:
    """
    Load daily OHLCV bars from yfinance for a date range.
    Returns {SYM: [bar_dicts]} in the same format as _frame_rows().
    Used by run_walk_forward() to get multi-year history without Alpaca limits.
    """
    try:
        import yfinance as yf
    except ImportError:
        return {}

    result: dict[str, list[dict]] = {}

    # Yahoo Finance blocks bare cloud IPs — spoof a browser session
    try:
        import requests
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        yf.utils.get_json.__globals__.get("requests", None)  # no-op ping
    except Exception:
        _session = None

    # yfinance multi-ticker download
    try:
        dl_kwargs: dict = dict(
            start=start_date,
            end=end_date,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=False,  # serial avoids Yahoo rate-limiting bursts
        )
        if _session is not None:
            dl_kwargs["session"] = _session
        tickers_df = yf.download(symbols, **dl_kwargs)
    except Exception:
        return result

    if tickers_df is None or getattr(tickers_df, "empty", True):
        return result

    has_multi = hasattr(tickers_df.columns, "levels") and len(tickers_df.columns.levels) > 1

    for sym in symbols:
        try:
            if has_multi:
                sym_upper = sym.upper()
                level1 = tickers_df.columns.get_level_values(1)
                if sym_upper not in level1:
                    continue
                df = tickers_df.xs(sym_upper, axis=1, level=1)
            elif len(symbols) == 1:
                df = tickers_df
            else:
                continue

            if df is None or df.empty:
                continue

            rows = []
            for ts, row in df.iterrows():
                close = float(row.get("Close", 0) or 0)
                if close <= 0:
                    continue
                rows.append({
                    "timestamp": ts,
                    "open":   float(row.get("Open",   close) or close),
                    "high":   float(row.get("High",   close) or close),
                    "low":    float(row.get("Low",    close) or close),
                    "close":  close,
                    "volume": float(row.get("Volume", 0)     or 0),
                    "vwap":   0.0,
                })
            if len(rows) >= 26:
                result[sym.upper()] = rows
        except Exception:
            continue

    return result


def _precompute_technicals(
    bars_by_symbol: dict[str, list[dict]],
    symbols: list[str],
) -> dict[str, dict[int, dict]]:
    """
    Precompute technicals for every (symbol, bar-index) pair.

    Uses a 60-bar rolling lookback instead of growing windows — sufficient
    for all EMA/MACD/RSI/ATR convergence while keeping runtime O(n) per
    symbol instead of O(n²).
    """
    LOOKBACK = 60
    cache: dict[str, dict[int, dict]] = {}
    for sym in symbols:
        sym_rows = bars_by_symbol.get(sym) or []
        cache[sym] = {}
        for idx in range(26, len(sym_rows)):
            bars = sym_rows[max(0, idx - LOOKBACK + 1): idx + 1]
            try:
                tech = sg.compute_technicals(bars)
                if tech:
                    cache[sym][idx] = tech
            except Exception:
                pass
    return cache


def _build_day_rows_yf(
    bars_by_symbol: dict[str, list[dict]],
    symbols: list[str],
    tech_cache: dict[str, dict[int, dict]],
) -> list[dict]:
    """
    Convert pre-loaded bars + pre-computed technicals into the day-rows format
    expected by score_candidate_variant().  Mirror of load_daily_replay_data()
    but uses the tech_cache to avoid O(n²) recomputation.
    """
    if not bars_by_symbol:
        return []

    rows: list[dict] = []
    by_date: dict[str, dict[str, tuple[int, dict]]] = {}
    sorted_dates: set[str] = set()

    for sym in symbols:
        sym_rows = bars_by_symbol.get(sym) or []
        for idx, bar in enumerate(sym_rows):
            date_key = _to_date_key(bar.get("timestamp"), idx)
            by_date.setdefault(date_key, {})[sym] = (idx, bar)
            sorted_dates.add(date_key)

    for date_key in sorted(sorted_dates):
        day_quotes: dict[str, dict] = {}
        next_quotes: dict[str, dict] = {}

        for sym in symbols:
            sym_rows = bars_by_symbol.get(sym) or []
            current = (by_date.get(date_key) or {}).get(sym)
            if not current:
                continue
            idx, bar = current
            if idx <= 0 or idx >= len(sym_rows) - 1:
                continue
            try:
                prev_close = float(sym_rows[idx - 1]["close"])
                close = float(bar.get("close") or 0)
                next_bar = sym_rows[idx + 1]
                next_close = float(next_bar.get("close") or 0)
                next_open = float(next_bar.get("open") or next_close)
                if prev_close <= 0 or close <= 0 or next_close <= 0 or next_open <= 0:
                    continue

                entry = _bar_quote(bar, prev_close)
                tech = (tech_cache.get(sym) or {}).get(idx)
                if tech:
                    entry["technicals"] = tech
                day_quotes[sym] = entry
                next_quotes[sym] = {
                    "open": round(next_open, 4),
                    "price": round(next_close, 4),
                    "close": round(next_close, 4),
                }
            except Exception:
                continue

        # Relative strength vs QQQ
        qqq_tech   = (day_quotes.get("QQQ") or {}).get("technicals") or {}
        qqq_return = qqq_tech.get("return_5d_pct")
        if qqq_return is not None:
            for sym, q in list(day_quotes.items()):
                tech = dict((q or {}).get("technicals") or {})
                if "return_5d_pct" in tech:
                    tech["relative_strength_qqq"] = round(
                        float(tech["return_5d_pct"]) - float(qqq_return), 3
                    )
                    q["technicals"] = tech

        if "QQQ" in day_quotes and "SPY" in day_quotes:
            rows.append({
                "date":        date_key,
                "quotes":      day_quotes,
                "next_quotes": next_quotes,
            })

    return rows


def run_walk_forward(
    start_date:    str = "2022-01-01",
    end_date:      str | None = None,
    window_months: int = 3,
    model:         dict | None = None,
) -> dict:
    """
    Walk-forward validation: score the current model across rolling time windows.

    Strategy:
      - Load full history via yfinance (no Alpaca day-limit)
      - Split into windows of `window_months` months (~21 trading days each)
      - Score each window with a stateful next-open portfolio simulator
      - Return per-window metrics + aggregate Sharpe / total return / verdict

    No train/test split needed — we're not fitting here, we're evaluating a
    fixed model. Signals are generated from today's close and filled no earlier
    than the next trading day's open.

    Returns dict with keys: ok, windows (list), sharpe, total_return_pct,
    profitable_windows, max_drawdown_pct, avg_win_rate, equity_curve, verdict.
    """
    import math

    from datetime import date as _date
    if end_date is None:
        end_date = _date.today().isoformat()

    if model is None:
        model = strategy_model.load_model()
    model = strategy_model.sanitize_model(model)

    # ── 1. Load history ───────────────────────────────────────────────────────
    # Try Alpaca first (works in GKE — no Yahoo Finance rate-limit issues).
    # Fall back to yfinance for local dev where Alpaca keys may be absent.
    all_bars: dict = {}
    _alpaca_err = ""
    try:
        from alpaca_client import AlpacaClient
        _client = AlpacaClient()
        _alpaca_bars = _client.get_historical_bars_range(
            sg.ALL_SYMBOLS, start_date, end_date, timeframe="1day"
        )
        if _alpaca_bars:
            all_bars = _alpaca_bars
    except Exception as _e:
        _alpaca_err = str(_e)

    if not all_bars:
        # Fallback: yfinance (works locally, blocked on cloud IPs)
        all_bars = load_historical_data_yf(sg.ALL_SYMBOLS, start_date, end_date)

    if not all_bars:
        return {
            "ok": False,
            "error": (
                f"No historical data available. "
                f"Alpaca: {_alpaca_err or 'returned empty'}. "
                f"yfinance: rate-limited or unavailable."
            ),
        }

    # ── 2. Precompute technicals once ─────────────────────────────────────────
    tech_cache = _precompute_technicals(all_bars, sg.ALL_SYMBOLS)

    # ── 3. Build day-rows ─────────────────────────────────────────────────────
    all_rows = _build_day_rows_yf(all_bars, sg.ALL_SYMBOLS, tech_cache)
    if len(all_rows) < 30:
        return {"ok": False, "error": f"Only {len(all_rows)} rows — not enough for walk-forward."}

    # ── 4. Chop into windows ──────────────────────────────────────────────────
    window_size = window_months * 21   # ~trading days per month
    windows_data: list[list[dict]] = []
    i = 0
    while i + window_size <= len(all_rows):
        windows_data.append(all_rows[i: i + window_size])
        i += window_size
    # Tail window: include remainder if it's at least 2 weeks
    if i < len(all_rows) and (len(all_rows) - i) >= 10:
        windows_data.append(all_rows[i:])

    if not windows_data:
        return {"ok": False, "error": "Not enough data for even one window."}

    # ── 5. Score each window ──────────────────────────────────────────────────
    window_results: list[dict] = []
    running_equity = 100_000.0
    equity_curve   = [round(running_equity, 2)]

    for w_idx, w_rows in enumerate(windows_data):
        res = _simulate_portfolio(model, w_rows, {}, False)

        # Regime breakdown for this window
        regime_counts: dict[str, int] = {}
        for row in w_rows:
            r = sg.detect_regime(row["quotes"])
            regime_counts[r] = regime_counts.get(r, 0) + 1
        total_regime_days = sum(regime_counts.values()) or 1
        dominant = max(regime_counts, key=lambda k: regime_counts[k])

        trade_total = (res.get("winning_trades", 0) + res.get("losing_trades", 0)) or 1
        win_rate = round(float(res.get("winning_trades", 0)) / trade_total * 100, 1)
        day_total = (res["winning_days"] + res["losing_days"]) or 1
        day_win_rate = round(res["winning_days"] / day_total * 100, 1)

        running_equity *= (1 + res["total_return_pct"] / 100)
        equity_curve.append(round(running_equity, 2))

        window_results.append({
            "window":          w_idx + 1,
            "start":           w_rows[0]["date"],
            "end":             w_rows[-1]["date"],
            "days":            len(w_rows),
            "return_pct":      res["total_return_pct"],
            "max_drawdown":    res["max_drawdown_pct"],
            "win_rate":        win_rate,
            "day_win_rate":    day_win_rate,
            "trades":          res["trades"],
            "entries":         res.get("entries", res["trades"]),
            "winning_trades":  res.get("winning_trades", 0),
            "losing_trades":   res.get("losing_trades", 0),
            "winning_days":    res["winning_days"],
            "losing_days":     res["losing_days"],
            "daily_return_pct": res["daily_return_pct"],
            "dominant_regime": dominant,
            "regime_mix":      {
                k: round(v / total_regime_days * 100, 1)
                for k, v in sorted(regime_counts.items())
            },
        })

    # ── 6. Aggregate ──────────────────────────────────────────────────────────
    returns   = [w["return_pct"]   for w in window_results]
    drawdowns = [w["max_drawdown"] for w in window_results]
    win_rates = [w["win_rate"]     for w in window_results]

    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / max(len(returns) - 1, 1)
    std_ret  = variance ** 0.5

    # Annualise: scale Sharpe from per-window to per-year
    periods_per_year = 12.0 / window_months
    sharpe = round((mean_ret / std_ret) * math.sqrt(periods_per_year), 3) if std_ret > 0 else 0.0

    total_return_pct   = round((running_equity - 100_000.0) / 100_000.0 * 100, 2)
    profitable_windows = sum(1 for r in returns if r > 0)
    max_dd             = round(max(drawdowns), 2)
    avg_win_rate       = round(sum(win_rates) / len(win_rates), 1)

    # Verdict
    if sharpe >= 1.0 and profitable_windows / len(window_results) >= 0.65:
        verdict = "EDGE CONFIRMED"
        verdict_color = "green"
    elif sharpe >= 0.5 and profitable_windows / len(window_results) >= 0.5:
        verdict = "MARGINAL EDGE"
        verdict_color = "amber"
    else:
        verdict = "NO EDGE DETECTED"
        verdict_color = "red"

    return {
        "ok":                  True,
        "start_date":          start_date,
        "end_date":            end_date,
        "window_months":       window_months,
        "total_windows":       len(window_results),
        "profitable_windows":  profitable_windows,
        "total_return_pct":    total_return_pct,
        "mean_window_return":  round(mean_ret, 3),
        "std_window_return":   round(std_ret, 3),
        "sharpe":              sharpe,
        "max_drawdown_pct":    max_dd,
        "avg_win_rate":        avg_win_rate,
        "win_rate_label":      "closed trades",
        "methodology":         "date-aligned bars; signals at close; entries next open; stateful positions; 5 bps slippage per side",
        "backtest_engine_version": 2,
        "equity_curve":        equity_curve,
        "windows":             window_results,
        "verdict":             verdict,
        "verdict_color":       verdict_color,
        "run_at":              datetime.now(timezone.utc).isoformat(),
    }


# ── QQQ regime core backtest ──────────────────────────────────────────────────

def _core_targets_for_regime(regime: str) -> tuple[float, float]:
    regime = str(regime or "").upper()
    if regime == "BULL":
        return float(config.CORE_BULL_TARGET_PCT), 0.0
    if regime == "BEAR":
        hedge = float(config.CORE_HEDGE_TARGET_PCT) if bool(config.CORE_HEDGE_BEAR_ONLY) else 0.0
        return float(config.CORE_BEAR_TARGET_PCT), min(hedge, float(config.CORE_HEDGE_MAX_PCT))
    return float(config.CORE_CHOPPY_TARGET_PCT), 0.0


def _annualized_sharpe(daily_returns_pct: list[float]) -> float:
    import math
    if len(daily_returns_pct) < 2:
        return 0.0
    mean = sum(daily_returns_pct) / len(daily_returns_pct)
    variance = sum((r - mean) ** 2 for r in daily_returns_pct) / (len(daily_returns_pct) - 1)
    std = variance ** 0.5
    if std <= 0:
        return 0.0
    return round((mean / std) * math.sqrt(252), 3)


def _max_drawdown_from_curve(curve: list[float]) -> float:
    peak = curve[0] if curve else 0.0
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100)
    return round(max_dd, 2)


def run_core_regime_backtest(
    start_date: str = "2022-01-01",
    end_date: str | None = None,
    initial_equity: float = 100_000.0,
) -> dict:
    """Backtest the QQQ-first regime allocator.

    Regime is detected from today's close and target weights are applied at the
    next open. The strategy compares against 100% QQQ buy-and-hold.
    """
    from datetime import date as _date
    if end_date is None:
        end_date = _date.today().isoformat()

    core = str(config.CORE_BENCHMARK_SYMBOL).upper()
    hedge = str(config.CORE_HEDGE_SYMBOL).upper()
    # Yahoo uses ^VIX while Alpaca/live snapshots use VIX. The regime detector
    # safely treats missing VIX as unknown, so exclude it from this local replay
    # instead of letting one symbol kill the whole backtest.
    symbols = sorted((set(sg.ALL_SYMBOLS) - {"VIX"}) | {core, hedge})

    all_bars: dict = {}
    alpaca_err = ""
    try:
        client = AlpacaClient()
        all_bars = client.get_historical_bars_range(symbols, start_date, end_date, timeframe="1day")
    except Exception as e:
        alpaca_err = str(e)

    if not all_bars:
        all_bars = load_historical_data_yf(symbols, start_date, end_date)
    if not all_bars:
        return {
            "ok": False,
            "error": (
                f"No historical data available. Alpaca: {alpaca_err or 'returned empty'}. "
                "yfinance unavailable or rate-limited."
            ),
        }

    tech_cache = _precompute_technicals(all_bars, symbols)
    rows = _build_day_rows_yf(all_bars, symbols, tech_cache)
    rows = [r for r in rows if core in (r.get("quotes") or {}) and "SPY" in (r.get("quotes") or {})]
    if len(rows) < 30:
        return {"ok": False, "error": f"Only {len(rows)} usable daily rows."}

    buy_slip = _SLIPPAGE_BPS / 10_000
    sell_slip = _SLIPPAGE_BPS / 10_000

    cash = float(initial_equity)
    core_qty = 0.0
    hedge_qty = 0.0
    pending_targets: tuple[float, float] | None = None
    orders = 0
    regime_counts: dict[str, int] = {}
    target_counts: dict[str, int] = {}
    strategy_curve: list[float] = []
    benchmark_curve: list[float] = []
    daily_returns: list[float] = []
    benchmark_returns: list[float] = []
    last_equity = float(initial_equity)
    last_bench = float(initial_equity)

    first_open = float((rows[0]["quotes"].get(core) or {}).get("open") or 0)
    if first_open <= 0:
        first_open = float((rows[0]["quotes"].get(core) or {}).get("price") or 0)
    if first_open <= 0:
        return {"ok": False, "error": f"No usable {core} open price."}
    benchmark_qty = initial_equity / (first_open * (1 + buy_slip))

    def _price(row: dict, symbol: str, field: str) -> float:
        q = (row.get("quotes") or {}).get(symbol) or {}
        return float(q.get(field) or q.get("price") or q.get("close") or 0)

    def _mark(row: dict, field: str = "close") -> tuple[float, float, float]:
        core_px = _price(row, core, field)
        hedge_px = _price(row, hedge, field) if hedge in (row.get("quotes") or {}) else 0.0
        equity = cash + core_qty * core_px + hedge_qty * hedge_px
        bench = benchmark_qty * core_px
        return equity, bench, core_px

    for idx, row in enumerate(rows):
        # Apply yesterday's regime target at today's open.
        if pending_targets is not None:
            open_core = _price(row, core, "open")
            open_hedge = _price(row, hedge, "open") if hedge in (row.get("quotes") or {}) else 0.0
            if open_core > 0:
                open_equity = cash + core_qty * open_core + (hedge_qty * open_hedge if open_hedge > 0 else 0)
                target_core_value = open_equity * pending_targets[0]
                target_hedge_value = open_equity * pending_targets[1]

                # Sell excess first to fund buys.
                current_core_value = core_qty * open_core
                if current_core_value > target_core_value:
                    sell_value = current_core_value - target_core_value
                    sell_qty = min(core_qty, sell_value / open_core)
                    if sell_qty * open_core >= float(config.CORE_MIN_ORDER_VALUE):
                        cash += sell_qty * open_core * (1 - sell_slip)
                        core_qty -= sell_qty
                        orders += 1

                if open_hedge > 0:
                    current_hedge_value = hedge_qty * open_hedge
                    if current_hedge_value > target_hedge_value:
                        sell_value = current_hedge_value - target_hedge_value
                        sell_qty = min(hedge_qty, sell_value / open_hedge)
                        if sell_qty * open_hedge >= float(config.CORE_MIN_ORDER_VALUE):
                            cash += sell_qty * open_hedge * (1 - sell_slip)
                            hedge_qty -= sell_qty
                            orders += 1

                # Buy to target after sells.
                current_core_value = core_qty * open_core
                if current_core_value < target_core_value:
                    buy_value = min(target_core_value - current_core_value, cash * 0.98)
                    if buy_value >= float(config.CORE_MIN_ORDER_VALUE):
                        fill_px = open_core * (1 + buy_slip)
                        qty = buy_value / fill_px
                        cash -= qty * fill_px
                        core_qty += qty
                        orders += 1

                if open_hedge > 0:
                    current_hedge_value = hedge_qty * open_hedge
                    if current_hedge_value < target_hedge_value:
                        buy_value = min(target_hedge_value - current_hedge_value, cash * 0.98)
                        if buy_value >= float(config.CORE_MIN_ORDER_VALUE):
                            fill_px = open_hedge * (1 + buy_slip)
                            qty = buy_value / fill_px
                            cash -= qty * fill_px
                            hedge_qty += qty
                            orders += 1

        equity, bench, _ = _mark(row, "close")
        strategy_curve.append(round(equity, 2))
        benchmark_curve.append(round(bench, 2))

        if idx > 0:
            daily_returns.append((equity - last_equity) / max(last_equity, 1) * 100)
            benchmark_returns.append((bench - last_bench) / max(last_bench, 1) * 100)
        last_equity = equity
        last_bench = bench

        regime = sg.detect_regime(row.get("quotes") or {})
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
        pending_targets = _core_targets_for_regime(regime)
        target_key = f"{pending_targets[0]:.0%}/{pending_targets[1]:.0%}"
        target_counts[target_key] = target_counts.get(target_key, 0) + 1

    strategy_return = (strategy_curve[-1] - initial_equity) / initial_equity * 100
    benchmark_return = (benchmark_curve[-1] - initial_equity) / initial_equity * 100
    profitable_days = sum(1 for r in daily_returns if r > 0)
    day_count = len(daily_returns) or 1
    verdict = "BEATS QQQ" if strategy_return > benchmark_return and _max_drawdown_from_curve(strategy_curve) <= _max_drawdown_from_curve(benchmark_curve) else "DOES NOT BEAT QQQ"
    if strategy_return > benchmark_return and _max_drawdown_from_curve(strategy_curve) > _max_drawdown_from_curve(benchmark_curve):
        verdict = "HIGHER RETURN, HIGHER DRAWDOWN"

    return {
        "ok": True,
        "strategy": "QQQ regime core",
        "start_date": rows[0]["date"],
        "end_date": rows[-1]["date"],
        "days": len(rows),
        "initial_equity": round(initial_equity, 2),
        "core_symbol": core,
        "hedge_symbol": hedge,
        "targets": {
            "bull": float(config.CORE_BULL_TARGET_PCT),
            "choppy": float(config.CORE_CHOPPY_TARGET_PCT),
            "bear": float(config.CORE_BEAR_TARGET_PCT),
            "bear_hedge": float(config.CORE_HEDGE_TARGET_PCT),
        },
        "strategy_return_pct": round(strategy_return, 2),
        "benchmark_symbol": core,
        "benchmark_return_pct": round(benchmark_return, 2),
        "excess_return_pct": round(strategy_return - benchmark_return, 2),
        "strategy_max_drawdown_pct": _max_drawdown_from_curve(strategy_curve),
        "benchmark_max_drawdown_pct": _max_drawdown_from_curve(benchmark_curve),
        "strategy_sharpe": _annualized_sharpe(daily_returns),
        "benchmark_sharpe": _annualized_sharpe(benchmark_returns),
        "profitable_day_rate": round(profitable_days / day_count * 100, 1),
        "orders": orders,
        "regime_counts": regime_counts,
        "target_counts": target_counts,
        "equity_curve": strategy_curve,
        "benchmark_curve": benchmark_curve,
        "methodology": "regime from close; rebalance next open; QQQ core; BEAR-only inverse hedge; 5 bps slippage per trade",
        "backtest_engine_version": 1,
        "verdict": verdict,
        "verdict_color": "green" if verdict == "BEATS QQQ" else "amber" if verdict == "HIGHER RETURN, HIGHER DRAWDOWN" else "red",
        "run_at": datetime.now(timezone.utc).isoformat(),
    }
