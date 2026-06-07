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

import signals as sg
import strategy_model
from alpaca_client import AlpacaClient


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


def simulated_exit_price(
    symbol: str, rows: list[dict], entry_row: dict, entry: float, model: dict
) -> float:
    """Simulate exit: hold up to max_holding_days, apply profit-lock giveback."""
    try:
        start_index = rows.index(entry_row)
    except ValueError:
        start_index = 0

    max_days      = int(model["max_holding_days"])
    lock_trigger  = float(model["profit_lock_trigger_pct"])
    giveback_limit = float(model["profit_giveback_pct"])
    peak_return   = 0.0
    last_price    = entry

    for offset in range(1, max_days + 1):
        idx = min(start_index + offset, len(rows) - 1)
        price = float((rows[idx].get("quotes") or {}).get(symbol, {}).get("price") or last_price)
        ret_pct = (price - entry) / entry * 100 if entry else 0.0
        peak_return = max(peak_return, ret_pct)
        last_price = price
        if peak_return >= lock_trigger and (peak_return - ret_pct) >= giveback_limit:
            return price
        if idx >= len(rows) - 1:
            return price

    return last_price


def _simulate_day(row: dict, rows: list[dict], model: dict, ind_weights: dict, etf_only: bool) -> tuple[float, int]:
    """
    Simulate one trading day. Returns (day_pnl, trades_executed).
    CHOPPY regime: still trade, apply 50% size multiplier (no longer skipped).
    """
    quotes    = row["quotes"]
    regime    = sg.detect_regime(quotes)
    day_pnl   = 0.0
    trades    = 0
    size_mult = sg.regime_size_multiplier(regime)

    universe = (sg.BEAR_UNIVERSE + sg.MOMENTUM_STOCKS) if regime == "BEAR" else sg.BULL_UNIVERSE
    if etf_only:
        etf_set  = set(sg.BULL_ETF + sg.BEAR_ETF)
        universe = [s for s in universe if s in etf_set]

    signals = []
    for sym in universe:
        score, _ = sg.score_symbol(sym, quotes, regime, weights=ind_weights)
        if score >= int(model["min_conviction"]):
            signals.append((sym, score))
    signals.sort(key=lambda item: item[1], reverse=True)
    selected = signals[:int(model["max_positions"])]

    equity = 100_000.0  # reference — pnl is proportional
    for sym, _ in selected:
        entry = float(quotes.get(sym, {}).get("price") or 0)
        if entry <= 0:
            continue
        exit_px = simulated_exit_price(sym, rows, row, entry, model)
        if exit_px <= 0:
            continue
        allocation = equity * float(model["position_size_pct"]) * size_mult
        raw_pnl    = allocation * ((exit_px - entry) / entry)
        stop_loss  = -allocation * (float(model["trailing_stop_pct"]) / 100)
        day_pnl   += max(raw_pnl, stop_loss)
        trades    += 1

    return day_pnl, trades


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
