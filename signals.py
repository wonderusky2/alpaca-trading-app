"""
signals.py — Revised signal scoring focused on daily profit maximization.

Philosophy:
- ETF regime flips (TQQQ ↔ SQQQ) are the PRIMARY trade — liquid, clean signal
- Single-stock entries require strong regime + positive intraday momentum
- CHOPPY = cash, never enter new positions (this was the #1 swing-bot mistake)
- Max 3 positions — concentrate capital, don't spread thin
- Never buy a red stock, even on a green day

Root-cause fixes vs. old swing-bot logic:
  - Old regime: chg >= -0.3 → BULL  (called BULL in -0.3% markets — wrong)
  - New regime: requires QQQ ≥ +0.4% confirmed by SPY ≥ +0.1%
  - Old: CHOPPY multiplied scores by 0.6 (still generated signals) — now returns 0
  - Old: 12 positions across slow themes (Solar, Infrastructure) — now max 3 ETF-first
  - Old: MIN_CONVICTION=70, too loose — now 75
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ── Universe ──────────────────────────────────────────────────────────────────
# ETFs first — regime flip instruments, most liquid
BULL_ETF    = ["TQQQ", "SOXL"]
BEAR_ETF    = ["SQQQ", "UVXY", "SPXS"]

# High-momentum single names — only when regime is confirmed BULL
BULL_STOCKS = ["NVDA", "AMD", "TSLA", "MSTR", "COIN", "PLTR", "CRWD"]

BULL_UNIVERSE = BULL_ETF + BULL_STOCKS
BEAR_UNIVERSE = BEAR_ETF

ALL_SYMBOLS = ["SPY", "QQQ"] + BULL_UNIVERSE + BEAR_UNIVERSE

MIN_CONVICTION = 75   # raised from 70 — fewer, better entries
MAX_POSITIONS  = 3    # concentrate in top 3 only

# ── Regime detection ──────────────────────────────────────────────────────────
def detect_regime(quotes: dict) -> str:
    """
    BULL   — QQQ ≥ +0.4% AND index average ≥ +0.3%
    BEAR   — QQQ ≤ −0.5% AND index average ≤ −0.4%
    CHOPPY — everything else → hold cash, no new entries

    Key fix: old code used chg >= -0.3 as BULL threshold,
    which triggered 'BULL' on basically flat or mildly negative days.
    """
    qqq = quotes.get("QQQ", {}).get("change_pct", 0.0)
    spy = quotes.get("SPY", {}).get("change_pct", 0.0)
    avg = (qqq + spy) / 2

    if qqq >= 0.4 and avg >= 0.3:    return "BULL"
    if qqq <= -0.5 and avg <= -0.4:  return "BEAR"
    return "CHOPPY"


# ── Technical indicator enrichment ────────────────────────────────────────────
def enrich_quotes_with_indicators(
    quotes: dict,
    symbols: list[str] | None = None,
    period: str = "10d",
    interval: str = "15m",
) -> dict:
    """
    Add momentum indicators to quote rows.

    Indicators:
    - EMA 9 / EMA 21 trend
    - MACD 12/26/9 and histogram
    - RSI 14
    - VWAP and anchored VWAP from recent swing low/high
    - ATR 14 and volume ratio
    - relative strength versus QQQ

    If market data fails, the original quote dict is returned unchanged.
    """
    symbols = symbols or list(quotes.keys())
    clean = sorted({str(s).upper() for s in symbols if str(s).strip()})
    if not clean:
        return quotes

    try:
        import yfinance as yf

        raw = yf.download(
            tickers=" ".join(clean),
            period=period,
            interval=interval,
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception:
        return quotes

    qqq_return = None
    enriched = dict(quotes)
    for sym in clean:
        try:
            frame = raw[sym] if len(clean) > 1 else raw
            bars = _bars_from_frame(frame)
            technicals = compute_technicals(bars)
            if not technicals:
                continue
            if sym == "QQQ":
                qqq_return = technicals.get("return_5d_pct")
            row = dict(enriched.get(sym, {}))
            row["technicals"] = technicals
            enriched[sym] = row
        except Exception:
            continue

    if qqq_return is not None:
        for sym, row in list(enriched.items()):
            tech = dict((row or {}).get("technicals") or {})
            if "return_5d_pct" in tech:
                tech["relative_strength_qqq"] = round(float(tech["return_5d_pct"]) - float(qqq_return), 3)
                row = dict(row)
                row["technicals"] = tech
                enriched[sym] = row
    return enriched


def compute_technicals(bars: list[dict]) -> dict:
    """Compute compact technical indicators from OHLCV bars."""
    bars = [b for b in bars if float(b.get("close") or 0) > 0]
    if len(bars) < 26:
        return {}

    closes = [float(b["close"]) for b in bars]
    highs = [float(b.get("high") or b["close"]) for b in bars]
    lows = [float(b.get("low") or b["close"]) for b in bars]
    volumes = [float(b.get("volume") or 0) for b in bars]

    ema9_series = _ema_series(closes, 9)
    ema21_series = _ema_series(closes, 21)
    ema12_series = _ema_series(closes, 12)
    ema26_series = _ema_series(closes, 26)
    macd_series = [a - b for a, b in zip(ema12_series, ema26_series)]
    signal_series = _ema_series(macd_series, 9)
    hist_series = [a - b for a, b in zip(macd_series, signal_series)]
    rsi = _rsi(closes, 14)
    atr = _atr(highs, lows, closes, 14)
    vwap = _vwap(bars)
    avwap_low = _anchored_vwap(bars, anchor="low", lookback=40)
    avwap_high = _anchored_vwap(bars, anchor="high", lookback=40)
    volume_ratio = _volume_ratio(volumes, 20)
    return_5d_pct = _window_return_pct(closes, min(len(closes) - 1, 26))
    price = closes[-1]
    trend = _trendline_context(closes, lookback=30)
    fib = _fib_context(highs, lows, price, lookback=40)

    return {
        "ema9": round(ema9_series[-1], 4),
        "ema21": round(ema21_series[-1], 4),
        "ema_trend": "bullish" if ema9_series[-1] > ema21_series[-1] else "bearish",
        "ema_spread_pct": round((ema9_series[-1] - ema21_series[-1]) / price * 100, 3) if price else 0,
        "macd": round(macd_series[-1], 4),
        "macd_signal": round(signal_series[-1], 4),
        "macd_hist": round(hist_series[-1], 4),
        "macd_hist_slope": round(hist_series[-1] - hist_series[-3], 4) if len(hist_series) >= 3 else 0,
        "rsi14": round(rsi, 2),
        "atr14": round(atr, 4),
        "atr_pct": round(atr / price * 100, 3) if price else 0,
        "vwap": round(vwap, 4),
        "price_vs_vwap_pct": round((price - vwap) / vwap * 100, 3) if vwap else 0,
        "avwap_low": round(avwap_low, 4),
        "price_vs_avwap_low_pct": round((price - avwap_low) / avwap_low * 100, 3) if avwap_low else 0,
        "avwap_high": round(avwap_high, 4),
        "price_vs_avwap_high_pct": round((price - avwap_high) / avwap_high * 100, 3) if avwap_high else 0,
        "volume_ratio": round(volume_ratio, 3),
        "return_5d_pct": round(return_5d_pct, 3),
        "trend_slope_pct": round(trend["slope_pct"], 3),
        "trend_direction": trend["direction"],
        "price_vs_trend_pct": round(trend["price_vs_trend_pct"], 3),
        "fib_position": fib["position"],
        "fib_range_high": round(fib["range_high"], 4),
        "fib_range_low": round(fib["range_low"], 4),
        "fib_nearest_level": fib["nearest_level"],
        "fib_nearest_price": round(fib["nearest_price"], 4),
        "fib_distance_pct": round(fib["distance_pct"], 3),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _bars_from_frame(frame) -> list[dict]:
    rows = []
    clean = frame.dropna(how="all")
    for _, row in clean.iterrows():
        close = _field(row, "Close")
        if close <= 0:
            continue
        rows.append({
            "open": _field(row, "Open", close),
            "high": _field(row, "High", close),
            "low": _field(row, "Low", close),
            "close": close,
            "volume": _field(row, "Volume", 0),
        })
    return rows


def _field(row, name: str, fallback: float = 0.0) -> float:
    try:
        value = row[name]
        if value != value:
            return fallback
        return float(value)
    except Exception:
        return fallback


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * alpha + out[-1] * (1 - alpha))
    return out


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = []
    losses = []
    for prev, cur in zip(closes[-period - 1:-1], closes[-period:]):
        change = cur - prev
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 0.0
    trs = []
    for idx in range(1, len(closes)):
        trs.append(max(
            highs[idx] - lows[idx],
            abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        ))
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


def _vwap(bars: list[dict]) -> float:
    total_pv = 0.0
    total_v = 0.0
    for b in bars:
        volume = float(b.get("volume") or 0)
        typical = (float(b.get("high") or 0) + float(b.get("low") or 0) + float(b.get("close") or 0)) / 3
        total_pv += typical * volume
        total_v += volume
    return total_pv / total_v if total_v > 0 else float(bars[-1].get("close") or 0)


def _anchored_vwap(bars: list[dict], anchor: str, lookback: int = 40) -> float:
    recent = bars[-min(lookback, len(bars)):]
    if not recent:
        return 0.0
    if anchor == "high":
        anchor_idx = max(range(len(recent)), key=lambda i: float(recent[i].get("high") or 0))
    else:
        anchor_idx = min(range(len(recent)), key=lambda i: float(recent[i].get("low") or 0))
    return _vwap(recent[anchor_idx:])


def _volume_ratio(volumes: list[float], period: int = 20) -> float:
    if len(volumes) < 2:
        return 1.0
    window = [v for v in volumes[-period - 1:-1] if v > 0]
    if not window:
        return 1.0
    return volumes[-1] / (sum(window) / len(window))


def _window_return_pct(closes: list[float], lookback: int) -> float:
    if len(closes) <= lookback or closes[-lookback - 1] <= 0:
        return 0.0
    return (closes[-1] - closes[-lookback - 1]) / closes[-lookback - 1] * 100


def _trendline_context(closes: list[float], lookback: int = 30) -> dict:
    window = closes[-min(lookback, len(closes)):]
    if len(window) < 3:
        return {"slope_pct": 0.0, "direction": "flat", "price_vs_trend_pct": 0.0}
    n = len(window)
    x_mean = (n - 1) / 2
    y_mean = sum(window) / n
    denom = sum((i - x_mean) ** 2 for i in range(n)) or 1
    slope = sum((i - x_mean) * (price - y_mean) for i, price in enumerate(window)) / denom
    trend_last = y_mean + slope * ((n - 1) - x_mean)
    last = window[-1]
    slope_pct = slope / last * 100 if last else 0.0
    if slope_pct > 0.03:
        direction = "up"
    elif slope_pct < -0.03:
        direction = "down"
    else:
        direction = "flat"
    return {
        "slope_pct": slope_pct,
        "direction": direction,
        "price_vs_trend_pct": (last - trend_last) / trend_last * 100 if trend_last else 0.0,
    }


def _fib_context(highs: list[float], lows: list[float], price: float, lookback: int = 40) -> dict:
    high_window = highs[-min(lookback, len(highs)):]
    low_window = lows[-min(lookback, len(lows)):]
    if not high_window or not low_window:
        return {
            "position": "unknown",
            "range_high": 0.0,
            "range_low": 0.0,
            "nearest_level": "n/a",
            "nearest_price": 0.0,
            "distance_pct": 0.0,
        }
    hi = max(high_window)
    lo = min(low_window)
    span = hi - lo
    if span <= 0:
        return {
            "position": "flat",
            "range_high": hi,
            "range_low": lo,
            "nearest_level": "n/a",
            "nearest_price": hi,
            "distance_pct": 0.0,
        }
    levels = {
        "23.6": hi - span * 0.236,
        "38.2": hi - span * 0.382,
        "50.0": hi - span * 0.5,
        "61.8": hi - span * 0.618,
        "78.6": hi - span * 0.786,
    }
    nearest_level, nearest_price = min(levels.items(), key=lambda item: abs(price - item[1]))
    retrace = (hi - price) / span
    if retrace <= 0.236:
        position = "near_high"
    elif retrace <= 0.5:
        position = "shallow_pullback"
    elif retrace <= 0.618:
        position = "golden_zone"
    elif retrace <= 0.786:
        position = "deep_pullback"
    else:
        position = "breakdown"
    return {
        "position": position,
        "range_high": hi,
        "range_low": lo,
        "nearest_level": nearest_level,
        "nearest_price": nearest_price,
        "distance_pct": (price - nearest_price) / nearest_price * 100 if nearest_price else 0.0,
    }


def _technical_bias(sym: str, quotes: dict, regime: str) -> tuple[int, list[str]]:
    tech = (quotes.get(sym) or {}).get("technicals") or {}
    if not tech:
        return 0, ["no technical history"]

    bullish = regime == "BULL" or sym.upper() in BEAR_UNIVERSE
    score = 0
    reasons: list[str] = []
    rsi = float(tech.get("rsi14") or 50)
    macd_hist = float(tech.get("macd_hist") or 0)
    macd_slope = float(tech.get("macd_hist_slope") or 0)
    ema_trend = str(tech.get("ema_trend") or "")
    price_vs_vwap = float(tech.get("price_vs_vwap_pct") or 0)
    price_vs_avwap_low = float(tech.get("price_vs_avwap_low_pct") or 0)
    price_vs_avwap_high = float(tech.get("price_vs_avwap_high_pct") or 0)
    rel_strength = float(tech.get("relative_strength_qqq") or 0)
    volume_ratio = float(tech.get("volume_ratio") or 1)
    trend_direction = str(tech.get("trend_direction") or "flat")
    price_vs_trend = float(tech.get("price_vs_trend_pct") or 0)
    fib_position = str(tech.get("fib_position") or "unknown")

    if bullish:
        if ema_trend == "bullish":
            score += 8; reasons.append("EMA9>EMA21")
        else:
            score -= 10; reasons.append("EMA trend bearish")
        if macd_hist > 0:
            score += 8; reasons.append("MACD positive")
        elif macd_slope > 0:
            score += 3; reasons.append("MACD improving")
        else:
            score -= 8; reasons.append("MACD negative")
        if 50 <= rsi <= 72:
            score += 6; reasons.append(f"RSI {rsi:.0f}")
        elif 72 < rsi <= 80:
            score += 1; reasons.append(f"RSI extended {rsi:.0f}")
        elif rsi > 80:
            score -= 5; reasons.append(f"RSI hot {rsi:.0f}")
        else:
            score -= 6; reasons.append(f"RSI weak {rsi:.0f}")
        if price_vs_vwap > 0:
            score += 6; reasons.append("above VWAP")
        else:
            score -= 6; reasons.append("below VWAP")
        if price_vs_avwap_low > 0:
            score += 5; reasons.append("above AVWAP low")
        if rel_strength > 0 and sym not in ("TQQQ", "SOXL"):
            score += 4; reasons.append("RS>QQQ")
        if trend_direction == "up" and price_vs_trend >= -0.5:
            score += 6; reasons.append("trendline up")
        elif trend_direction == "down":
            score -= 6; reasons.append("trendline down")
        if fib_position in ("shallow_pullback", "golden_zone"):
            score += 4; reasons.append(f"fib {fib_position}")
        elif fib_position == "breakdown":
            score -= 8; reasons.append("fib breakdown")
    else:
        if ema_trend == "bearish":
            score += 8; reasons.append("EMA9<EMA21")
        else:
            score -= 8; reasons.append("EMA trend bullish")
        if macd_hist < 0:
            score += 8; reasons.append("MACD bearish")
        elif macd_slope < 0:
            score += 3; reasons.append("MACD fading")
        else:
            score -= 8; reasons.append("MACD positive")
        if rsi <= 50:
            score += 6; reasons.append(f"RSI {rsi:.0f}")
        else:
            score -= 5; reasons.append(f"RSI not weak {rsi:.0f}")
        if price_vs_vwap < 0 or price_vs_avwap_high < 0:
            score += 6; reasons.append("below VWAP/AVWAP")
        else:
            score -= 4; reasons.append("above VWAP")
        if trend_direction == "down" and price_vs_trend <= 0.5:
            score += 6; reasons.append("trendline down")
        elif trend_direction == "up":
            score -= 6; reasons.append("trendline up")
        if fib_position in ("deep_pullback", "breakdown"):
            score += 4; reasons.append(f"fib {fib_position}")

    if volume_ratio >= 1.15:
        score += 4; reasons.append("volume confirms")
    elif volume_ratio < 0.65:
        score -= 3; reasons.append("thin volume")
    return score, reasons


# ── Signal scoring ────────────────────────────────────────────────────────────
def score_symbol(sym: str, quotes: dict, regime: str) -> int:
    """
    Returns 0–100 conviction score.
    0 = never trade. ≥75 = enter (MIN_CONVICTION).

    CHOPPY always returns 0 — cash is a position.
    Positive intraday momentum required for all long entries.
    ETFs score higher than stocks — cleaner regime signal, more liquid.
    """
    if regime == "CHOPPY":
        return 0  # Do not trade in flat/mixed markets

    q   = quotes.get(sym, {})
    chg = q.get("change_pct", 0.0)
    qqq = quotes.get("QQQ", {}).get("change_pct", 0.0)
    spy = quotes.get("SPY", {}).get("change_pct", 0.0)
    tech_score, _ = _technical_bias(sym, quotes, regime)

    # ── BULL regime ───────────────────────────────────────────────────────────
    if regime == "BULL":
        if sym in BEAR_ETF:
            return 0  # Never hold bear ETFs on a bull day

        # ── TQQQ — primary bull instrument, pure QQQ-derivative ──────────────
        if sym == "TQQQ":
            if qqq < 0.3: return 0                    # need visible QQQ move
            s = 65
            s += 10 if qqq > 0.6  else 0
            s += 10 if qqq > 1.0  else 0
            s +=  8 if qqq > 1.5  else 0
            s -=  8 if chg <= 0   else 0              # TQQQ itself must be moving
            s += tech_score
            return min(100, max(0, s))

        # ── SOXL — semiconductor 3x, needs semi confirmation ─────────────────
        if sym == "SOXL":
            nvda = quotes.get("NVDA", {}).get("change_pct", 0.0)
            amd  = quotes.get("AMD",  {}).get("change_pct", 0.0)
            semi_avg = (nvda + amd) / 2
            if semi_avg < 0.5 or chg <= 0: return 0
            s = 60
            s += 15 if semi_avg > 1.5 else 0
            s += 10 if semi_avg > 2.5 else 0
            s += tech_score
            return min(100, s)

        # ── Single stocks — require green day + regime alignment ──────────────
        if chg <= 0:
            return 0  # Never buy a falling stock, even on a green day

        if sym == "NVDA":
            if chg < 0.5: return 0
            s = 55
            s += 10 if chg > 1.0  else 0
            s += 12 if chg > 2.5  else 0
            s +=  5 if qqq > 0.5  else 0
            s += tech_score
            return min(100, s)

        if sym == "AMD":
            nvda_chg = quotes.get("NVDA", {}).get("change_pct", 0.0)
            if nvda_chg <= 0 or chg < 0.5: return 0  # AMD needs NVDA leadership
            s = 50
            s += 10 if chg      > 1.5 else 0
            s +=  8 if nvda_chg > 1.5 else 0
            s += tech_score
            return min(100, s)

        if sym == "TSLA":
            if chg < 1.5: return 0                    # only enter on strong TSLA days
            s = 55
            s += 15 if chg > 3.0 else 0
            s +=  8 if chg > 5.0 else 0
            s += tech_score
            return min(100, s)

        if sym in ("MSTR", "COIN"):
            # Crypto proxies — require both moving up together
            mstr = quotes.get("MSTR", {}).get("change_pct", 0.0)
            coin = quotes.get("COIN", {}).get("change_pct", 0.0)
            if mstr <= 0 or coin <= 0: return 0
            s = 50
            s += 12 if chg > 3.0 else 0
            s += 10 if chg > 5.0 else 0
            s += tech_score
            return min(100, s)

        if sym in ("PLTR", "CRWD"):
            if chg < 1.0: return 0
            s = 48
            s += 12 if chg > 2.0 else 0
            s += 10 if chg > 4.0 else 0
            s += tech_score
            return min(100, s)

        return 0  # Unknown symbol

    # ── BEAR regime ───────────────────────────────────────────────────────────
    else:
        if sym in BULL_UNIVERSE:
            return 0  # No longs on a bear day

        # ── SQQQ — primary bear instrument ───────────────────────────────────
        if sym == "SQQQ":
            if qqq > -0.5: return 0                   # require QQQ clearly down
            s = 68
            s += 12 if qqq < -1.0  else 0
            s += 10 if qqq < -1.8  else 0
            s -=  8 if chg <= 0    else 0             # SQQQ must be rising
            s += tech_score
            return min(100, max(0, s))

        # ── UVXY — VIX 2x, only on genuine fear spikes ───────────────────────
        if sym == "UVXY":
            if qqq > -1.0: return 0                   # need serious selling first
            s = 62
            s += 15 if qqq < -1.5 else 0
            s +=  8 if chg >  2.0 else 0
            s += tech_score
            return min(100, s)

        # ── SPXS — S&P 3x inverse ────────────────────────────────────────────
        if sym == "SPXS":
            if spy > -0.4: return 0
            s = 62
            s += 12 if spy < -0.8 else 0
            s +=  8 if spy < -1.2 else 0
            s += tech_score
            return min(100, s)

        return 0


# ── Gemini sentiment boost — kept but NOT in the hot path ────────────────────
def gemini_sentiment_boost(sym: str, score: int) -> int:
    """
    Optional AI check for borderline scores (60–79).
    Called by server.py's live-scores, NOT by trader.py's hot loop.
    Too slow (2s API call) to run in the 5-min trading loop.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or score < 60 or score >= 80:
        return 0
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model  = genai.GenerativeModel("gemini-2.0-flash")
        prompt = (
            f"One line only. Is {sym} bullish or bearish RIGHT NOW based on "
            f"recent news? Reply with just: BULLISH, BEARISH, or NEUTRAL."
        )
        resp = model.generate_content(prompt)
        text = resp.text.strip().upper()
        if "BULLISH" in text:  return 10
        if "BEARISH" in text:  return -12
        return 0
    except Exception:
        return 0


# ── Signal dataclass ──────────────────────────────────────────────────────────
@dataclass
class TradeSignal:
    symbol:  str
    score:   int
    regime:  str
    side:    str = "buy"
    signals: list[str] = field(default_factory=list)


def get_signals(quotes: dict) -> list[TradeSignal]:
    """
    Fast signal scan — no external API calls, no Gemini.
    Returns at most MAX_POSITIONS top signals.

    Use server.py's _run_live_scores() for the dashboard's
    full Alpaca-news-enhanced version.
    """
    regime = detect_regime(quotes)
    if regime == "CHOPPY":
        return []  # Cash is a position

    universe = BULL_UNIVERSE if regime == "BULL" else BEAR_UNIVERSE
    results  = []
    for sym in universe:
        s = score_symbol(sym, quotes, regime)
        if s >= MIN_CONVICTION:
            _, reasons = _technical_bias(sym, quotes, regime)
            results.append(TradeSignal(symbol=sym, score=s, regime=regime, signals=reasons))

    return sorted(results, key=lambda x: x.score, reverse=True)[:MAX_POSITIONS]
