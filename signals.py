"""
signals.py — 6-signal technical confluence engine for daily profit maximization.

Philosophy:
- RSI + MACD + AVWAP + EMA(9/21) + Trendline + Price Action must align before entry
- Regime scales position SIZE, never blocks trades outright:
    BULL   = 100% size on longs
    BEAR   = 100% size on inverse ETFs, 50% on individual longs (hedging)
    CHOPPY = 50% size — momentum still exists in individual names
- No per-symbol hard-coded logic — pure formula-driven confluence
- ATR-based dynamic stops (1.5× ATR below entry, max 5% drawdown)
- Price-action confirmation: reversal candles, swing breaks, gaps, volume
- signal_breakdown returned per stock for iOS visualization

Universe: 30+ high-momentum stocks + leveraged ETFs
"""
from __future__ import annotations
import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ── Universe ──────────────────────────────────────────────────────────────────
BULL_ETF = ["TQQQ", "SOXL", "FNGU", "LABU"]
BEAR_ETF = ["SQQQ", "UVXY", "SPXS"]

# High-momentum names — scored in any regime, size scaled by regime
MOMENTUM_STOCKS = [
    # Semis / AI infrastructure
    "NVDA", "AMD", "ARM", "SMCI", "MRVL",
    # Mega-cap tech
    "META", "GOOGL", "AMZN", "MSFT", "AAPL",
    # High-beta growth
    "TSLA", "PLTR", "CRWD", "PANW", "NET",
    # Crypto proxies
    "MSTR", "COIN", "HOOD",
    # Space / quantum / speculative
    "RKLB", "IONQ", "RGTI", "BBAI",
    # FinTech / growth
    "SOFI", "AFRM",
]

BULL_UNIVERSE = BULL_ETF + MOMENTUM_STOCKS
BEAR_UNIVERSE = BEAR_ETF
ALL_SYMBOLS   = ["SPY", "QQQ"] + BULL_UNIVERSE + BEAR_UNIVERSE

MIN_CONVICTION = 65   # 0-100 scale; ≥65 required to generate a signal
MAX_POSITIONS  = 5    # max concurrent positions

# ── Indicator weights (default) ───────────────────────────────────────────────
# Each weight = max points contributed when signal is fully aligned.
# Opposite = negative points. Scale sums to 100 for clean 0–100 output.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "rsi":   20.0,   # RSI 14 — momentum zone check
    "macd":  22.0,   # MACD histogram direction + slope
    "avwap": 22.0,   # Anchored VWAP — institutional price anchor
    "ema":   18.0,   # EMA 9/21 crossover — trend direction
    "trend": 18.0,   # Trendline slope + price position
    "price_action": 12.0,  # Candles, structure breaks, gaps, volume confirmation
}
_TOTAL_MAX_WEIGHT = sum(_DEFAULT_WEIGHTS.values())  # 100

# ── Profile param keys (not indicator weights) ────────────────────────────────
_PROFILE_PARAM_KEYS = frozenset({
    "conviction_override", "size_mult", "max_pos_override",
    "hold_days_override", "stop_pct_override", "lock_trigger_override", "etf_only",
})

# ── Named strategy variant profiles ──────────────────────────────────────────
VARIANT_PROFILES: dict[str, dict] = {
    "current": {},

    "aggressive": {
        "rsi": 16.0, "macd": 24.0, "avwap": 16.0, "ema": 16.0, "trend": 16.0, "price_action": 12.0,
        "conviction_override": 60, "size_mult": 1.3,
    },

    "avwap_heavy": {
        "rsi": 13.0, "macd": 13.0, "avwap": 31.0, "ema": 13.0, "trend": 16.0, "price_action": 12.0,
    },

    "momentum_heavy": {
        "rsi": 13.0, "macd": 26.0, "avwap": 13.0, "ema": 22.0, "trend": 12.0, "price_action": 12.0,
        "conviction_override": 63,
    },

    "faster_exit": {
        "stop_pct_override": 2.0, "lock_trigger_override": 1.0, "hold_days_override": 1,
    },

    "defensive": {
        "rsi": 22.0, "macd": 13.0, "avwap": 22.0, "ema": 17.0, "trend": 14.0, "price_action": 12.0,
        "conviction_override": 78, "size_mult": 0.6, "max_pos_override": 2, "etf_only": True,
    },
}


# ── Regime detection ──────────────────────────────────────────────────────────
def detect_regime(quotes: dict) -> str:
    """
    BULL   — QQQ ≥ +0.4% AND index average ≥ +0.3%
    BEAR   — QQQ ≤ −0.5% AND index average ≤ −0.4%
    CHOPPY — everything else (still trade, 50% position size)
    """
    qqq = float((quotes.get("QQQ") or {}).get("change_pct") or 0)
    spy = float((quotes.get("SPY") or {}).get("change_pct") or 0)
    avg = (qqq + spy) / 2
    if qqq >= 0.4 and avg >= 0.3:    return "BULL"
    if qqq <= -0.5 and avg <= -0.4:  return "BEAR"
    return "CHOPPY"


def regime_size_multiplier(regime: str) -> float:
    """Scale position size by regime conviction. CHOPPY = 50%, never 0."""
    return {"BULL": 1.0, "BEAR": 1.0, "CHOPPY": 0.5}.get(regime, 0.5)


# ── Indicator enrichment ──────────────────────────────────────────────────────
def enrich_quotes_with_indicators(
    quotes: dict,
    symbols: list[str] | None = None,
    alpaca_client=None,          # AlpacaClient instance — preferred data source
    period: str = "10d",
    interval: str = "15m",
) -> dict:
    """Download OHLCV and attach technical indicators to each quote row.

    Uses Alpaca bars only. Missing bars leave the quote row unscored instead of
    mixing another vendor into live trading decisions.
    """
    symbols = symbols or list(quotes.keys())
    clean = sorted({str(s).upper() for s in symbols if str(s).strip()})
    if not clean:
        return quotes

    bars_by_sym: dict[str, list[dict]] = {}   # sym → list of OHLCV row dicts

    # ── 1. Alpaca bars (preferred) ────────────────────────────────────────────
    if alpaca_client is not None:
        try:
            import zoneinfo
            _et = zoneinfo.ZoneInfo("America/New_York")
            now_et = __import__("datetime").datetime.now(_et)
            _mins = now_et.hour * 60 + now_et.minute
            _market_open = now_et.weekday() < 5 and 570 <= _mins < 960

            tf = "15min" if _market_open else "1day"
            limit = 200 if _market_open else 120
            alpaca_bars = alpaca_client.get_historical_bars(clean, timeframe=tf, limit=limit)

            for sym, df in alpaca_bars.items():
                if df is None or df.empty:
                    continue
                rows = []
                for _, r in df.iterrows():
                    close = float(r.get("close", 0) or 0)
                    if close <= 0:
                        continue
                    rows.append({
                        "open":   float(r.get("open",   close) or close),
                        "high":   float(r.get("high",   close) or close),
                        "low":    float(r.get("low",    close) or close),
                        "close":  close,
                        "volume": float(r.get("volume", 0)     or 0),
                        "vwap":   float(r.get("vwap",   0)     or 0),
                    })
                if rows:
                    bars_by_sym[sym.upper()] = rows
        except Exception as e:
            logging.getLogger("signals").warning("Alpaca bars failed: %s", e)

    missing_from_alpaca = [s for s in clean if s not in bars_by_sym]
    if missing_from_alpaca:
        logging.getLogger("signals").warning(
            "Alpaca bars missing for %d/%d symbols: %s",
            len(missing_from_alpaca), len(clean), missing_from_alpaca,
        )

    # ── Compute technicals from bars ──────────────────────────────────────────
    qqq_return = None
    enriched = dict(quotes)
    for sym in clean:
        bars = bars_by_sym.get(sym)
        if not bars:
            continue
        try:
            technicals = compute_technicals(bars)
            if not technicals:
                continue
            if sym == "QQQ":
                qqq_return = technicals.get("return_5d_pct")
            row = dict(enriched.get(sym) or {})
            row["technicals"] = technicals
            enriched[sym] = row
        except Exception:
            continue

    if qqq_return is not None:
        for sym, row in list(enriched.items()):
            tech = dict((row or {}).get("technicals") or {})
            if "return_5d_pct" in tech:
                tech["relative_strength_qqq"] = round(
                    float(tech["return_5d_pct"]) - float(qqq_return), 3
                )
                row = dict(row)
                row["technicals"] = tech
                enriched[sym] = row
    return enriched


def compute_technicals(bars: list[dict]) -> dict:
    """Compute RSI, MACD, AVWAP, EMA 9/21, trendline, ATR from OHLCV bars."""
    bars = [b for b in bars if float(b.get("close") or 0) > 0]
    if len(bars) < 26:
        return {}

    closes  = [float(b["close"])                    for b in bars]
    highs   = [float(b.get("high")   or b["close"]) for b in bars]
    lows    = [float(b.get("low")    or b["close"]) for b in bars]
    volumes = [float(b.get("volume") or 0)          for b in bars]

    ema9  = _ema_series(closes, 9)
    ema21 = _ema_series(closes, 21)
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_line   = [a - b for a, b in zip(ema12, ema26)]
    macd_signal = _ema_series(macd_line, 9)
    macd_hist   = [a - b for a, b in zip(macd_line, macd_signal)]

    rsi       = _rsi(closes, 14)
    atr       = _atr(highs, lows, closes, 14)
    vwap      = _vwap(bars)
    avwap_low  = _anchored_vwap(bars, anchor="low",  lookback=40)
    avwap_high = _anchored_vwap(bars, anchor="high", lookback=40)
    vol_ratio  = _volume_ratio(volumes, 20)
    ret_5d     = _window_return_pct(closes, min(len(closes) - 1, 26))
    trend      = _trendline_context(closes, lookback=30)
    fib        = _fib_context(highs, lows, closes[-1], lookback=40)
    price_action = _price_action_context(bars, lookback=20)

    price = closes[-1]
    return {
        "ema9":               round(ema9[-1],  4),
        "ema21":              round(ema21[-1], 4),
        "ema_trend":          "bullish" if ema9[-1] > ema21[-1] else "bearish",
        "ema_spread_pct":     round((ema9[-1] - ema21[-1]) / price * 100, 3) if price else 0,
        "macd":               round(macd_line[-1],   4),
        "macd_signal":        round(macd_signal[-1], 4),
        "macd_hist":          round(macd_hist[-1],   4),
        "macd_hist_slope":    round(macd_hist[-1] - macd_hist[-3], 4) if len(macd_hist) >= 3 else 0,
        "rsi14":              round(rsi, 2),
        "atr14":              round(atr, 4),
        "atr_pct":            round(atr / price * 100, 3) if price else 0,
        "vwap":               round(vwap, 4),
        "price_vs_vwap_pct":  round((price - vwap) / vwap * 100, 3) if vwap else 0,
        "avwap_low":          round(avwap_low, 4),
        "price_vs_avwap_low_pct":  round((price - avwap_low)  / avwap_low  * 100, 3) if avwap_low  else 0,
        "avwap_high":         round(avwap_high, 4),
        "price_vs_avwap_high_pct": round((price - avwap_high) / avwap_high * 100, 3) if avwap_high else 0,
        "volume_ratio":       round(vol_ratio, 3),
        "return_5d_pct":      round(ret_5d, 3),
        "trend_slope_pct":    round(trend["slope_pct"], 3),
        "trend_direction":    trend["direction"],
        "price_vs_trend_pct": round(trend["price_vs_trend_pct"], 3),
        "fib_position":       fib["position"],
        "fib_range_high":     round(fib["range_high"], 4),
        "fib_range_low":      round(fib["range_low"], 4),
        "fib_nearest_level":  fib["nearest_level"],
        "fib_nearest_price":  round(fib["nearest_price"], 4),
        "fib_distance_pct":   round(fib["distance_pct"], 3),
        "candle_pattern":     price_action["candle_pattern"],
        "candle_bias":        price_action["candle_bias"],
        "structure_signal":   price_action["structure_signal"],
        "gap_signal":         price_action["gap_signal"],
        "price_action_score": round(price_action["score"], 3),
        "price_action_label": price_action["label"],
        "updated_at":         datetime.now(timezone.utc).isoformat(),
    }


# ── Core: per-indicator signal breakdown ─────────────────────────────────────
def _signal_breakdown(
    tech: dict, bullish: bool, weights: dict
) -> tuple[int, dict]:
    """
    Compute 6-signal confluence score (0–100) + per-indicator breakdown dict.

    breakdown schema per indicator:
      {"status": "bullish"|"neutral"|"bearish", "label": str, "points": int, "weight": int}

    Scoring:
      Each indicator contributes in [-weight, +weight].
      Sum is mapped from [-total, +total] → [0, 100].
      Small volume and Fibonacci bonuses applied after normalization.
    """
    w = {**_DEFAULT_WEIGHTS, **{k: v for k, v in (weights or {}).items() if k in _DEFAULT_WEIGHTS}}
    total_weight = sum(w.values()) or 1.0

    rsi_val     = float(tech.get("rsi14")              or 50)
    macd_hist   = float(tech.get("macd_hist")          or 0)
    macd_slope  = float(tech.get("macd_hist_slope")    or 0)
    ema_trend   = str(tech.get("ema_trend")            or "")
    ema_spread  = float(tech.get("ema_spread_pct")     or 0)
    pvap_low    = float(tech.get("price_vs_avwap_low_pct")  or 0)
    pvap_high   = float(tech.get("price_vs_avwap_high_pct") or 0)
    pvwap       = float(tech.get("price_vs_vwap_pct")  or 0)
    trend_dir   = str(tech.get("trend_direction")      or "flat")
    vs_trend    = float(tech.get("price_vs_trend_pct") or 0)
    vol_ratio   = float(tech.get("volume_ratio")       or 1)
    fib_pos     = str(tech.get("fib_position")         or "unknown")
    pa_score    = float(tech.get("price_action_score") or 0)
    pa_label    = str(tech.get("price_action_label")   or "Price action neutral")

    breakdown: dict = {}
    raw: float = 0.0

    # ── RSI ────────────────────────────────────────────────────────────────────
    if bullish:
        if 52 <= rsi_val <= 70:
            rc, rs, rl = 1.0, "bullish", f"RSI {rsi_val:.0f} — momentum sweet zone"
        elif 70 < rsi_val <= 80:
            rc, rs, rl = 0.3, "neutral", f"RSI {rsi_val:.0f} — getting extended"
        elif rsi_val > 80:
            rc, rs, rl = -0.5, "bearish", f"RSI {rsi_val:.0f} — overbought, risk of reversal"
        elif 45 <= rsi_val < 52:
            rc, rs, rl = 0.1, "neutral", f"RSI {rsi_val:.0f} — warming up"
        else:
            rc, rs, rl = -1.0, "bearish", f"RSI {rsi_val:.0f} — momentum weak"
    else:
        if rsi_val <= 40:
            rc, rs, rl = 1.0, "bullish", f"RSI {rsi_val:.0f} — oversold, downside confirmed"
        elif rsi_val <= 50:
            rc, rs, rl = 0.4, "neutral", f"RSI {rsi_val:.0f} — weakening"
        else:
            rc, rs, rl = -0.8, "bearish", f"RSI {rsi_val:.0f} — too strong to short"

    raw += rc * w["rsi"]
    breakdown["rsi"] = {"status": rs, "label": rl, "points": round(rc * w["rsi"]), "weight": round(w["rsi"])}

    # ── MACD ──────────────────────────────────────────────────────────────────
    if bullish:
        if macd_hist > 0 and macd_slope > 0:
            mc, ms, ml = 1.0, "bullish", "MACD rising above zero — momentum building"
        elif macd_hist > 0:
            mc, ms, ml = 0.6, "bullish", "MACD above zero"
        elif macd_slope > 0:
            mc, ms, ml = 0.3, "neutral", "MACD improving from below zero"
        elif macd_hist < 0 and macd_slope < 0:
            mc, ms, ml = -1.0, "bearish", "MACD negative and falling"
        else:
            mc, ms, ml = -0.5, "bearish", "MACD below zero"
    else:
        if macd_hist < 0 and macd_slope < 0:
            mc, ms, ml = 1.0, "bullish", "MACD falling below zero — downtrend confirmed"
        elif macd_hist < 0:
            mc, ms, ml = 0.6, "bullish", "MACD below zero"
        elif macd_slope < 0:
            mc, ms, ml = 0.3, "neutral", "MACD fading"
        else:
            mc, ms, ml = -1.0, "bearish", "MACD positive — wrong side for a short"

    raw += mc * w["macd"]
    breakdown["macd"] = {"status": ms, "label": ml, "points": round(mc * w["macd"]), "weight": round(w["macd"])}

    # ── AVWAP ─────────────────────────────────────────────────────────────────
    if bullish:
        if pvap_low > 1.0 and pvwap > 0:
            ac, as_, al = 1.0, "bullish", f"Above anchored VWAP +{pvap_low:.1f}% — institutions in profit"
        elif pvap_low > 0 or pvwap > 0:
            ac, as_, al = 0.5, "bullish", "Above VWAP — price supported"
        elif pvap_low > -1.0:
            ac, as_, al = -0.2, "neutral", "Slightly below VWAP"
        else:
            ac, as_, al = -1.0, "bearish", f"Below anchored VWAP {pvap_low:.1f}% — selling pressure"
    else:
        if pvap_high < -1.0 or pvwap < -0.5:
            ac, as_, al = 1.0, "bullish", "Below VWAP — short setup confirmed"
        elif pvwap < 0:
            ac, as_, al = 0.4, "neutral", "Approaching VWAP from above"
        else:
            ac, as_, al = -0.8, "bearish", "Above VWAP — weak short entry"

    raw += ac * w["avwap"]
    breakdown["avwap"] = {"status": as_, "label": al, "points": round(ac * w["avwap"]), "weight": round(w["avwap"])}

    # ── EMA 9/21 ──────────────────────────────────────────────────────────────
    if bullish:
        if ema_trend == "bullish" and ema_spread > 0.3:
            ec, es, el = 1.0, "bullish", f"EMA9 > EMA21 (+{ema_spread:.1f}%) — uptrend locked in"
        elif ema_trend == "bullish":
            ec, es, el = 0.5, "bullish", "EMA9 above EMA21 — bullish bias"
        else:
            ec, es, el = -1.0, "bearish", "EMA9 below EMA21 — trend is down"
    else:
        if ema_trend == "bearish" and ema_spread < -0.3:
            ec, es, el = 1.0, "bullish", f"EMA9 < EMA21 ({ema_spread:.1f}%) — downtrend locked in"
        elif ema_trend == "bearish":
            ec, es, el = 0.5, "bullish", "EMA trend bearish"
        else:
            ec, es, el = -1.0, "bearish", "EMA trend bullish — don't short yet"

    raw += ec * w["ema"]
    breakdown["ema"] = {"status": es, "label": el, "points": round(ec * w["ema"]), "weight": round(w["ema"])}

    # ── Trendline ─────────────────────────────────────────────────────────────
    if bullish:
        if trend_dir == "up" and vs_trend >= -0.5:
            tc, ts, tl = 1.0, "bullish", "Uptrend intact — price riding the trendline"
        elif trend_dir == "up":
            tc, ts, tl = 0.3, "neutral", "Uptrend but price pulled back below"
        elif trend_dir == "flat":
            tc, ts, tl = 0.0, "neutral", "No clear trend — looking for direction"
        else:
            tc, ts, tl = -1.0, "bearish", "Downtrend — fighting the tape"
    else:
        if trend_dir == "down" and vs_trend <= 0.5:
            tc, ts, tl = 1.0, "bullish", "Downtrend confirmed — short has the wind"
        elif trend_dir == "down":
            tc, ts, tl = 0.4, "neutral", "Downtrend in place"
        elif trend_dir == "flat":
            tc, ts, tl = 0.0, "neutral", "No directional edge"
        else:
            tc, ts, tl = -1.0, "bearish", "Uptrend — wrong side of the tape"

    raw += tc * w["trend"]
    breakdown["trend"] = {"status": ts, "label": tl, "points": round(tc * w["trend"]), "weight": round(w["trend"])}

    # ── Price action: candles + structure + gap behavior ─────────────────────
    pac = pa_score if bullish else -pa_score
    if pac >= 0.55:
        ps = "bullish"
    elif pac <= -0.55:
        ps = "bearish"
    else:
        ps = "neutral"
    raw += pac * w["price_action"]
    breakdown["price_action"] = {
        "status": ps,
        "label": pa_label,
        "points": round(pac * w["price_action"]),
        "weight": round(w["price_action"]),
    }

    # ── Normalize to 0–100 ────────────────────────────────────────────────────
    # raw ∈ [-total_weight, +total_weight] → map to [0, 100]
    score = int(50.0 + (raw / total_weight) * 50.0)

    # ── Small bonuses (volume confirmation, Fibonacci) ────────────────────────
    if vol_ratio >= 1.2:
        score += 3  # high-volume confirmation of move
    if bullish and fib_pos in ("shallow_pullback", "golden_zone"):
        score += 3  # buying in golden zone
    elif bullish and fib_pos == "breakdown":
        score -= 5  # don't buy a breakdown
    elif not bullish and fib_pos in ("deep_pullback", "breakdown"):
        score += 3  # short at breakdown

    return max(0, min(100, score)), breakdown


# ── Symbol scoring ────────────────────────────────────────────────────────────
def score_symbol(
    sym: str,
    quotes: dict,
    regime: str,
    weights: dict | None = None,
) -> tuple[int, dict]:
    """
    Score a single symbol using 6-signal confluence.

    Returns (score: int 0–100, signal_breakdown: dict).
    score ≥ MIN_CONVICTION → trade signal.

    Regime determines direction (long vs inverse ETF), never blocks scoring.
    Pure formula — no per-symbol special cases.
    """
    q    = quotes.get(sym) or {}
    tech = q.get("technicals") or {}
    if not tech:
        return 0, {}

    in_bear = sym in BEAR_ETF

    if regime == "BEAR":
        # Bear ETFs are our long play — score them on bearish market conditions
        # Stocks: evaluate as longs (can still run), but size will be 50%
        bullish = not in_bear
    else:
        # BULL or CHOPPY: bear ETFs don't belong, everything else scored bullish
        if in_bear:
            return 0, {}
        bullish = True

    ind_weights = {k: v for k, v in (weights or {}).items() if k in _DEFAULT_WEIGHTS}
    score, breakdown = _signal_breakdown(tech, bullish, ind_weights)

    # Mild penalty for strong intraday counter-moves
    chg = float(q.get("change_pct") or 0)
    if bullish and chg < -1.5:
        score = max(0, score - 8)   # falling stock on long side
    elif not bullish and chg > 1.5:
        score = max(0, score - 8)   # bear ETF falling hard = bad sign

    return score, breakdown


# ── ATR-based dynamic stop price ─────────────────────────────────────────────
def atr_stop_price(sym: str, quotes: dict, entry_price: float, multiplier: float = 1.5) -> float:
    """
    Compute stop-loss price = entry - (ATR14 × multiplier).
    Falls back to 2% hard stop if no ATR available.
    Never wider than 5% drawdown from entry.
    """
    atr = float((quotes.get(sym) or {}).get("technicals", {}).get("atr14") or 0)
    if atr > 0:
        stop = entry_price - atr * multiplier
    else:
        stop = entry_price * 0.98
    return round(max(stop, entry_price * 0.95), 4)


# ── Signal dataclass ──────────────────────────────────────────────────────────
@dataclass
class TradeSignal:
    symbol:           str
    score:            int
    regime:           str
    side:             str   = "buy"
    size_mult:        float = 1.0   # regime + profile size multiplier
    atr_stop:         float = 0.0   # absolute stop-loss price at entry
    signals:          list[str] = field(default_factory=list)
    signal_breakdown: dict      = field(default_factory=dict)  # per-indicator for iOS


# ── Main signal scan ──────────────────────────────────────────────────────────
def get_signals(quotes: dict, profile_name: str = "current") -> list[TradeSignal]:
    """
    Scan all symbols, return top signals ranked by conviction.

    CHOPPY regime no longer returns empty list — it returns signals at 50% size.
    profile_name: key into VARIANT_PROFILES for weight/param overrides.
    Returns at most max_pos signals.
    """
    regime  = detect_regime(quotes)
    profile = VARIANT_PROFILES.get(profile_name) or {}

    min_conv   = int(profile.get("conviction_override",  MIN_CONVICTION))
    max_pos    = int(profile.get("max_pos_override",     MAX_POSITIONS))
    etf_only   = bool(profile.get("etf_only",           False))
    ind_weights = {k: v for k, v in profile.items() if k not in _PROFILE_PARAM_KEYS}
    size_mult  = float(profile.get("size_mult", 1.0)) * regime_size_multiplier(regime)

    universe = (BEAR_UNIVERSE + MOMENTUM_STOCKS) if regime == "BEAR" else BULL_UNIVERSE
    if etf_only:
        etf_set  = set(BULL_ETF + BEAR_ETF)
        universe = [s for s in universe if s in etf_set]

    results: list[TradeSignal] = []
    for sym in universe:
        score, breakdown = score_symbol(sym, quotes, regime, weights=ind_weights)
        if score < min_conv:
            continue

        tech  = (quotes.get(sym) or {}).get("technicals") or {}
        price = float((quotes.get(sym) or {}).get("price") or
                      (quotes.get(sym) or {}).get("ask")  or 0)
        atr_stop = 0.0
        if price > 0:
            atr = float(tech.get("atr14") or 0)
            atr_stop = round(max(price - atr * 1.5, price * 0.95), 4) if atr > 0 else round(price * 0.98, 4)

        signal_labels = [bd["label"] for bd in breakdown.values() if bd.get("label")]

        results.append(TradeSignal(
            symbol=sym,
            score=score,
            regime=regime,
            side="buy",
            size_mult=round(size_mult, 2),
            atr_stop=atr_stop,
            signals=signal_labels,
            signal_breakdown=breakdown,
        ))

    return sorted(results, key=lambda x: x.score, reverse=True)[:max_pos]


# ── Gemini sentiment boost (optional, not in hot path) ───────────────────────
def gemini_sentiment_boost(sym: str, score: int) -> int:
    """AI sentiment check for borderline scores. Too slow for main loop."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or score < 60 or score >= 80:
        return 0
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")
        resp  = model.generate_content(
            f"One line only. Is {sym} bullish or bearish RIGHT NOW based on "
            f"recent news? Reply with just: BULLISH, BEARISH, or NEUTRAL."
        )
        text = resp.text.strip().upper()
        if "BULLISH" in text: return 10
        if "BEARISH" in text: return -12
        return 0
    except Exception:
        return 0


# ── Math helpers ──────────────────────────────────────────────────────────────
def _bars_from_frame(frame) -> list[dict]:
    rows = []
    clean = frame.dropna(how="all")
    for _, row in clean.iterrows():
        close = _field(row, "Close")
        if close <= 0:
            continue
        rows.append({
            "open":   _field(row, "Open",   close),
            "high":   _field(row, "High",   close),
            "low":    _field(row, "Low",    close),
            "close":  close,
            "volume": _field(row, "Volume", 0),
        })
    return rows


def _field(row, name: str, fallback: float = 0.0) -> float:
    try:
        v = row[name]
        return fallback if v != v else float(v)
    except Exception:
        return fallback


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * alpha + out[-1] * (1 - alpha))
    return out


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains, losses = [], []
    for prev, cur in zip(closes[-period - 1:-1], closes[-period:]):
        d = cur - prev
        gains.append(max(d, 0)); losses.append(abs(min(d, 0)))
    ag, al = sum(gains) / period, sum(losses) / period
    return 100.0 if al == 0 else 100 - (100 / (1 + ag / al))


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 0.0
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        for i in range(1, len(closes))
    ]
    w = trs[-period:]
    return sum(w) / len(w) if w else 0.0


def _vwap(bars: list[dict]) -> float:
    pv = v = 0.0
    for b in bars:
        vol = float(b.get("volume") or 0)
        typ = (float(b.get("high") or 0) + float(b.get("low") or 0) + float(b.get("close") or 0)) / 3
        pv += typ * vol; v += vol
    return pv / v if v > 0 else float(bars[-1].get("close") or 0)


def _anchored_vwap(bars: list[dict], anchor: str, lookback: int = 40) -> float:
    recent = bars[-min(lookback, len(bars)):]
    if not recent:
        return 0.0
    if anchor == "high":
        idx = max(range(len(recent)), key=lambda i: float(recent[i].get("high") or 0))
    else:
        idx = min(range(len(recent)), key=lambda i: float(recent[i].get("low")  or 0))
    return _vwap(recent[idx:])


def _volume_ratio(volumes: list[float], period: int = 20) -> float:
    if len(volumes) < 2:
        return 1.0
    window = [v for v in volumes[-period - 1:-1] if v > 0]
    return volumes[-1] / (sum(window) / len(window)) if window else 1.0


def _window_return_pct(closes: list[float], lookback: int) -> float:
    if len(closes) <= lookback or closes[-lookback - 1] <= 0:
        return 0.0
    return (closes[-1] - closes[-lookback - 1]) / closes[-lookback - 1] * 100


def _trendline_context(closes: list[float], lookback: int = 30) -> dict:
    window = closes[-min(lookback, len(closes)):]
    if len(window) < 3:
        return {"slope_pct": 0.0, "direction": "flat", "price_vs_trend_pct": 0.0}
    n = len(window)
    xm = (n - 1) / 2
    ym = sum(window) / n
    denom = sum((i - xm) ** 2 for i in range(n)) or 1
    slope = sum((i - xm) * (p - ym) for i, p in enumerate(window)) / denom
    trend_last = ym + slope * ((n - 1) - xm)
    last = window[-1]
    sp = slope / last * 100 if last else 0.0
    direction = "up" if sp > 0.03 else "down" if sp < -0.03 else "flat"
    return {
        "slope_pct":         sp,
        "direction":         direction,
        "price_vs_trend_pct": (last - trend_last) / trend_last * 100 if trend_last else 0.0,
    }


def _fib_context(highs: list[float], lows: list[float], price: float, lookback: int = 40) -> dict:
    hw = highs[-min(lookback, len(highs)):]
    lw = lows[-min(lookback,  len(lows)):]
    _empty = {"position": "unknown", "range_high": 0.0, "range_low": 0.0,
               "nearest_level": "n/a", "nearest_price": 0.0, "distance_pct": 0.0}
    if not hw or not lw:
        return _empty
    hi, lo = max(hw), min(lw)
    span = hi - lo
    if span <= 0:
        return {"position": "flat", "range_high": hi, "range_low": lo,
                "nearest_level": "n/a", "nearest_price": hi, "distance_pct": 0.0}
    levels = {
        "23.6": hi - span * 0.236, "38.2": hi - span * 0.382,
        "50.0": hi - span * 0.5,   "61.8": hi - span * 0.618, "78.6": hi - span * 0.786,
    }
    nl, np_ = min(levels.items(), key=lambda item: abs(price - item[1]))
    retrace = (hi - price) / span
    if retrace <= 0.236:   pos = "near_high"
    elif retrace <= 0.5:   pos = "shallow_pullback"
    elif retrace <= 0.618: pos = "golden_zone"
    elif retrace <= 0.786: pos = "deep_pullback"
    else:                  pos = "breakdown"
    return {
        "position": pos, "range_high": hi, "range_low": lo,
        "nearest_level": nl, "nearest_price": np_,
        "distance_pct": (price - np_) / np_ * 100 if np_ else 0.0,
    }


def _price_action_context(bars: list[dict], lookback: int = 20) -> dict:
    """Return compact candle/structure/gap context.

    score is directional: +1 bullish price action, -1 bearish, 0 neutral.
    Uses only recent OHLCV bars so it works intraday and on daily bars.
    """
    _empty = {
        "candle_pattern": "none",
        "candle_bias": "neutral",
        "structure_signal": "inside_range",
        "gap_signal": "none",
        "score": 0.0,
        "label": "Price action neutral",
    }
    if len(bars) < 3:
        return _empty

    def f(bar: dict, key: str, fallback: float = 0.0) -> float:
        return float(bar.get(key) or fallback)

    last = bars[-1]
    prev = bars[-2]
    open_ = f(last, "open", f(last, "close"))
    high = f(last, "high", f(last, "close"))
    low = f(last, "low", f(last, "close"))
    close = f(last, "close")
    prev_open = f(prev, "open", f(prev, "close"))
    prev_close = f(prev, "close")
    prev_high = f(prev, "high", prev_close)
    prev_low = f(prev, "low", prev_close)
    if close <= 0 or high <= low:
        return _empty

    rng = max(high - low, 1e-9)
    body = abs(close - open_)
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    body_ratio = body / rng
    close_pos = (close - low) / rng
    vol_ratio = _volume_ratio([f(b, "volume", 0.0) for b in bars], 20)
    vol_confirm = vol_ratio >= 1.2

    candle_pattern = "none"
    candle_score = 0.0
    if body_ratio <= 0.12:
        candle_pattern = "doji"
        candle_score = 0.0
    if upper >= body * 2.0 and close_pos <= 0.45:
        candle_pattern = "shooting_star"
        candle_score = -0.8
    if lower >= body * 2.0 and close_pos >= 0.55:
        candle_pattern = "hammer"
        candle_score = 0.7
    if close > open_ and prev_close < prev_open and close >= prev_open and open_ <= prev_close:
        candle_pattern = "bullish_engulfing"
        candle_score = 0.9
    if close < open_ and prev_close > prev_open and close <= prev_open and open_ >= prev_close:
        candle_pattern = "bearish_engulfing"
        candle_score = -0.9

    recent = bars[-min(lookback + 1, len(bars)):-1]
    recent_high = max(f(b, "high", f(b, "close")) for b in recent) if recent else prev_high
    recent_low = min(f(b, "low", f(b, "close")) for b in recent) if recent else prev_low
    structure_signal = "inside_range"
    structure_score = 0.0
    if close > recent_high:
        structure_signal = "breakout"
        structure_score = 0.9
    elif close < recent_low:
        structure_signal = "breakdown"
        structure_score = -0.9
    elif low < recent_low and close > recent_low:
        structure_signal = "failed_breakdown"
        structure_score = 0.7
    elif high > recent_high and close < recent_high:
        structure_signal = "failed_breakout"
        structure_score = -0.7

    gap_signal = "none"
    gap_score = 0.0
    gap_pct = (open_ - prev_close) / prev_close * 100 if prev_close else 0.0
    if gap_pct >= 1.0:
        if close > open_:
            gap_signal = "gap_up_hold"
            gap_score = 0.35
        else:
            gap_signal = "gap_up_fade"
            gap_score = -0.45
    elif gap_pct <= -1.0:
        if close < open_:
            gap_signal = "gap_down_follow"
            gap_score = -0.35
        else:
            gap_signal = "gap_down_reclaim"
            gap_score = 0.45

    score = candle_score * 0.45 + structure_score * 0.40 + gap_score * 0.15
    if vol_confirm and abs(score) > 0.25:
        score *= 1.15
    score = max(-1.0, min(1.0, score))

    parts = []
    if candle_pattern != "none":
        parts.append(candle_pattern.replace("_", " "))
    if structure_signal != "inside_range":
        parts.append(structure_signal.replace("_", " "))
    if gap_signal != "none":
        parts.append(gap_signal.replace("_", " "))
    if vol_confirm and parts:
        parts.append("volume confirmed")
    label = "Price action neutral"
    if parts:
        label = "Price action: " + ", ".join(parts)

    return {
        "candle_pattern": candle_pattern,
        "candle_bias": "bullish" if score > 0.25 else "bearish" if score < -0.25 else "neutral",
        "structure_signal": structure_signal,
        "gap_signal": gap_signal,
        "score": score,
        "label": label,
    }
