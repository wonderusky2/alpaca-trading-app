"""
server.py — Slim Flask backend for the Robinhood Trader dashboard.

Endpoints:
  GET  /api/status
  GET  /api/lab/overview
  GET  /api/lab/activity
  GET  /api/lab/portfolio/history
  GET  /api/lab/agents/decision
  GET  /api/lab/model
  POST /api/lab/model/learn
  POST /api/lab/backtest
  POST /api/lab/orders/preview
  POST /api/lab/orders/place
  POST /api/lab/live-scores/trigger
  GET  /lab
"""
from __future__ import annotations

import json
import os
import re
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import config
import notify
import signals as sg
import strategy_model
import trader
import yfinance as yf
from alpaca_client import AlpacaClient
from logger import get_logger

log = get_logger("server")
app = Flask(__name__)
CORS(app)

# ── API key auth ───────────────────────────────────────────────────────────────
_LAB_API_KEY: str = os.environ.get("LAB_API_KEY", "").strip()

def _check_api_key() -> bool:
    """Return True if request passes auth. No key configured = allow all (local dev)."""
    if not _LAB_API_KEY:
        return True
    provided = (
        request.headers.get("X-API-Key", "")
        or request.args.get("key", "")
    )
    return provided == _LAB_API_KEY

def require_api_key(f):
    """Decorator that returns 401 when API key is configured but not matched."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _check_api_key():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

# ── Paths ──────────────────────────────────────────────────────────────────────
STATE_DIR = Path(os.environ.get("STATE_DIR") or (Path.home() / ".robinhood-trader" / "state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
LAB_EVENTS_PATH     = STATE_DIR / "lab_events.jsonl"
LAB_PEAK_EQUITY_PATH = STATE_DIR / "lab_peak_equity.json"
DASHBOARD_PATH      = Path(__file__).parent / "portfolio_lab.html"

MARKET_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
}

# ── Symbol taxonomy ────────────────────────────────────────────────────────────
_BULL_ETFS   = {"TQQQ", "SOXL", "FNGU", "TECL", "UDOW"}
_BEAR_ETFS   = {"SQQQ", "SPXS", "SOXS", "TECS", "UVXY", "SDOW"}
_TECH_GROWTH = {
    "NVDA", "AMD", "TSLA", "MSTR", "COIN", "MARA",
    "PLTR", "CRWD", "HOOD", "RKLB", "ARM", "SMCI",
}

def _theme_for_symbol(symbol: str) -> str:
    s = symbol.upper()
    if s in _BULL_ETFS:   return "Bull ETF"
    if s in _BEAR_ETFS:   return "Bear/Hedge"
    if s in _TECH_GROWTH: return "Tech/Growth"
    return "Other"

# ── News term scoring (Alpaca headlines) ───────────────────────────────────────
_NEWS_NEGATIVE = {
    "downgrade", "cuts guidance", "cut guidance", "misses", "missed",
    "probe", "investigation", "lawsuit", "sued", "fraud", "sec charges",
    "recall", "bankruptcy", "default", "short report", "breach", "outage",
    "halted", "layoffs", "slump", "plunge", "falls after", "warning",
}
_NEWS_POSITIVE = {
    "upgrade", "raises guidance", "beat estimates", "beats",
    "contract", "partnership", "approval", "approved", "buyback",
    "record revenue", "surges", "jumps after",
}

def _news_score_delta(articles: list[dict]) -> tuple[int, list[str]]:
    """
    Scan up to 5 recent headlines and return (delta, matched_terms).
    Range: −20 to +15. Negative headlines outweigh positive (2×).
    """
    delta = 0
    matched: list[str] = []
    for art in articles[:5]:
        text = f"{art.get('headline','')} {art.get('summary','')}".lower()
        for term in _NEWS_NEGATIVE:
            if term in text:
                delta -= 8
                matched.append(f"-{term}")
        for term in _NEWS_POSITIVE:
            if term in text:
                delta += 5
                matched.append(f"+{term}")
    return max(-20, min(15, delta)), matched

# ── Singleton client ───────────────────────────────────────────────────────────
_client: AlpacaClient | None = None
_client_lock = threading.Lock()

def get_client() -> AlpacaClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AlpacaClient()
    return _client

# ── In-memory caches ───────────────────────────────────────────────────────────
_overview_cache:     dict = {"ts": 0.0, "data": None}
_live_scores_cache:  dict = {"ts": 0.0, "data": None}
_news_cache:         dict = {"ts": 0.0, "data": {}, "symbols": set()}   # symbol → articles
_OVERVIEW_TTL   = 30
_LIVE_SCORE_TTL = 300
_NEWS_TTL       = 600

# ── Events log ─────────────────────────────────────────────────────────────────
def _append_event(event_type: str, payload: dict) -> None:
    try:
        event = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "type":    event_type,
            "payload": payload,
        }
        with LAB_EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
        # iMessage notification for order events
        if event_type == "paper_orders_submitted" and config.IMESSAGE_NOTIFY_EVENTS:
            submitted = payload.get("submitted") or []
            sample = ", ".join(
                f"{str(o.get('side','')).upper()} {o.get('qty')} {o.get('symbol')}"
                for o in submitted[:5]
            )
            more = f" +{len(submitted)-5} more" if len(submitted) > 5 else ""
            notify.send(
                f"RHT: submitted {len(submitted)} PAPER order(s)"
                f"{f' ({sample}{more})' if sample else ''}."
            )
    except Exception as e:
        log.warning("Could not append lab event: %s", e)

def _load_events(limit: int = 50) -> list[dict]:
    if not LAB_EVENTS_PATH.exists():
        return []
    events: list[dict] = []
    try:
        with LAB_EVENTS_PATH.open(encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return list(reversed(events))[:limit]

# ── Peak equity tracker ────────────────────────────────────────────────────────
def _load_peak_state() -> dict:
    try:
        return json.loads(LAB_PEAK_EQUITY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_peak_state(state: dict) -> None:
    try:
        LAB_PEAK_EQUITY_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save peak equity state: %s", e)

def _update_peak_equity(equity: float, client: AlpacaClient | None = None) -> dict:
    enabled       = bool(config.PROTECT_GAINS_ENABLED)
    threshold_pct = float(config.PROTECT_GAINS_DRAWDOWN_PCT)
    state         = _load_peak_state()

    # Try to pull high-water mark from Alpaca portfolio history
    if client is not None and equity > 0:
        try:
            history  = client.get_portfolio_history(period="1M", timeframe="1D")
            equities = [
                (float(v), ts)
                for ts, v in zip(history.get("timestamp") or [], history.get("equity") or [])
                if v and float(v) > 0
            ]
            if equities:
                equities.append((equity, datetime.now(timezone.utc).isoformat()))
                broker_peak, broker_ts = max(equities, key=lambda x: x[0])
                local_peak = float(state.get("peak_equity") or 0)
                if broker_peak >= local_peak:
                    state = {**state, "peak_equity": round(broker_peak, 2),
                             "peak_ts": broker_ts, "source": "broker_portfolio_history"}
                    _save_peak_state(state)
        except Exception as e:
            log.debug("portfolio history fetch failed: %s", e)

    peak = float(state.get("peak_equity") or 0)
    now  = datetime.now(timezone.utc).isoformat()
    if equity > 0 and (peak <= 0 or equity > peak):
        peak  = equity
        state = {**state, "peak_equity": round(peak, 2), "peak_ts": now, "source": "local_state"}
        _save_peak_state(state)

    giveback     = max(0.0, peak - equity) if peak > 0 and equity > 0 else 0.0
    giveback_pct = (giveback / peak * 100) if peak else 0.0
    return {
        "enabled":         enabled,
        "peak_equity":     round(peak, 2),
        "peak_ts":         state.get("peak_ts"),
        "source":          state.get("source") or "local_state",
        "current_equity":  round(equity, 2),
        "giveback_dollars": round(giveback, 2),
        "giveback_pct":    round(giveback_pct, 2),
        "threshold_pct":   round(threshold_pct, 2),
        "breach":          bool(enabled and peak > 0 and equity > 0 and giveback_pct >= threshold_pct),
    }

# ── Risk state ─────────────────────────────────────────────────────────────────
def _live_risk_state(
    equity: float,
    unrealized_pnl: float,
    long_pnl: float,
    hedge_pnl: float,
    max_drift_pct: float,
    position_count: int,
    peak_state: dict,
    broker_pnl: float,
    gross_exposure_pct: float,
) -> dict:
    risk_pnl   = broker_pnl
    loss_pct   = (risk_pnl / equity * 100) if equity else 0.0
    hard_loss  = max(1000.0, equity * 0.01) if equity else 1000.0
    both_losing   = long_pnl < 0 and hedge_pnl < 0
    drift_breach  = max_drift_pct >= 5.0
    pg_breach     = bool(peak_state.get("breach"))
    redeploy_pct  = float(config.PROTECT_GAINS_REDEPLOY_GROSS_PCT)
    already_light = gross_exposure_pct <= redeploy_pct
    reentry_pct   = float(config.REENTRY_RECOVERY_GIVEBACK_PCT)
    reentry_ok    = bool(peak_state.get("enabled") and float(peak_state.get("giveback_pct") or 0) <= reentry_pct)

    if position_count and pg_breach and already_light:
        status, action = "recovery", "rebalance_review"
        message = (
            f"Protect-gains breach remains ({peak_state.get('giveback_pct')}% giveback), "
            f"but gross exposure is only {gross_exposure_pct:.1f}%. Review redeploy."
        )
    elif position_count and pg_breach:
        status, action = "breach", "reduce_risk"
        message = (
            f"Protect gains: equity pulled back {peak_state.get('giveback_pct')}% "
            f"from peak ${peak_state.get('peak_equity')}. Reduce exposure."
        )
    elif position_count and (risk_pnl <= -hard_loss or loss_pct <= -1.0 or both_losing):
        status, action = "breach", "reduce_risk"
        message = "Risk breach: reduce exposure before adding or rebalancing."
    elif position_count and risk_pnl < 0 and loss_pct <= -0.5:
        status, action = "warning", "rebalance_review"
        message = "Net P&L is negative; review before adding new positions."
    elif position_count and drift_breach:
        status, action = "review", "rebalance_review"
        message = "Weight drift is wide — review rebalance before adding."
    elif position_count:
        status, action = "normal", "hold"
        message = "Live risk is inside limits."
    else:
        status, action = "research", "research"
        message = "No broker portfolio. Score signals and build positions."

    return {
        "status":                   status,
        "action":                   action,
        "message":                  message,
        "loss_pct":                 round(loss_pct, 2),
        "loss_limit_dollars":       round(hard_loss, 2),
        "hedge_not_working":        both_losing,
        "drift_breach":             drift_breach,
        "protect_gains":            peak_state,
        "protect_gains_breach":     pg_breach,
        "gross_exposure_pct":       round(gross_exposure_pct, 2),
        "redeploy_gross_threshold_pct": round(redeploy_pct, 2),
        "reentry_allowed":          reentry_ok,
        "reentry_recovery_giveback_pct": round(reentry_pct, 2),
    }

# ── Accounting builder ─────────────────────────────────────────────────────────
_FILLED_STATUSES = frozenset({"filled", "partially filled", "filled outside model"})

def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None

def _latest_order_for(orders: list[dict], symbol: str, side: str | None = None) -> dict | None:
    symbol = symbol.upper()
    matches = [
        o for o in orders
        if str(o.get("symbol", "")).upper() == symbol
        and (side is None or str(o.get("side", "")).lower() == side.lower())
    ]
    return sorted(matches, key=lambda o: str(o.get("submitted_at") or o.get("filled_at") or ""), reverse=True)[0] if matches else None

def _build_accounting(
    broker_positions: dict,
    open_orders: list[dict],
    recent_orders: list[dict],
    account: dict,
    live_scores: dict | None = None,
) -> dict:
    equity   = float((account or {}).get("equity") or 0)
    open_ids = {str(o.get("id")) for o in (open_orders or [])}
    all_orders = list(open_orders or []) + [
        o for o in (recent_orders or []) if str(o.get("id")) not in open_ids
    ]
    rows: list[dict] = []
    seen: set[str] = set()

    # Score map from live signals (optional)
    score_map: dict[str, int] = {}
    if live_scores and live_scores.get("ok"):
        for entry in (live_scores.get("signals") or []):
            score_map[str(entry.get("symbol", "")).upper()] = int(entry.get("score") or 0)

    for symbol, broker in (broker_positions or {}).items():
        symbol = symbol.upper()
        seen.add(symbol)
        qty       = abs(float(broker.get("qty") or 0))
        entry_px  = float(broker.get("entry") or 0)
        mkt_val   = float(broker.get("market_val") or 0)
        pnl       = float(broker.get("unrealized_pl") or 0)
        pnl_pct   = float(broker.get("unrealized_plpc") or 0) * 100
        cur_px    = (abs(mkt_val) / qty) if qty else entry_px
        cur_wt    = (mkt_val / equity * 100) if equity else 0
        order     = _latest_order_for(all_orders, symbol)
        order_st  = str((order or {}).get("status") or "").lower()
        active_st = {"new", "accepted", "pending_new", "partially_filled", "submitted"}

        if order and order_st in active_st:
            status = f"pending {order_st}"
        elif order and order_st == "partially_filled":
            status = "partially filled"
        else:
            status = "filled"

        signal_score = score_map.get(symbol)
        rows.append({
            "symbol":            symbol,
            "side":              str(broker.get("side", "long")).upper(),
            "theme":             _theme_for_symbol(symbol),
            "source":            "broker_only",
            "status":            status,
            "order_id":          (order or {}).get("id"),
            "order_status":      (order or {}).get("status"),
            "submitted_at":      (order or {}).get("submitted_at"),
            "entry_date":        None,
            "holding_days":      None,
            "qty":               round(qty, 4),
            "entry_price":       round(entry_px, 2),
            "current_price":     round(cur_px, 2),
            "cost_basis":        round(abs(qty * entry_px), 2),
            "current_value":     round(mkt_val, 2),
            "gross_value":       round(abs(mkt_val), 2),
            "target_value":      0,
            "target_weight_pct": 0,
            "current_weight_pct": round(cur_wt, 2),
            "drift_pct":         round(cur_wt, 2),
            "unrealized_pnl":    round(pnl, 2),
            "unrealized_pnl_pct": round(pnl_pct, 2),
            "contribution_bps":  round((pnl / equity) * 10000, 1) if equity else 0,
            "signal_score":      signal_score,
            "action":            "hold" if pnl >= 0 else "review",
            "reason":            (
                f"Signal score: {signal_score}." if signal_score is not None
                else "No current signal for this symbol."
            ),
        })

    rows.sort(key=lambda r: (0 if "pending" in str(r["status"]) else 1, r["theme"], r["symbol"]))
    broker_rows     = [r for r in rows if r["status"] in _FILLED_STATUSES or r["status"] == "filled"]
    unrealized      = sum(float(r.get("unrealized_pnl") or 0) for r in rows)
    broker_pnl      = sum(float(r.get("unrealized_pnl") or 0) for r in broker_rows)
    hedge_pnl       = sum(float(r.get("unrealized_pnl") or 0) for r in rows if r.get("side") == "SHORT")
    long_pnl        = unrealized - hedge_pnl
    drift_max       = max([abs(float(r.get("drift_pct") or 0)) for r in rows] or [0])
    gross_exp_pct   = sum(abs(float(r.get("current_weight_pct") or 0)) for r in broker_rows)
    peak_state      = _update_peak_equity(equity, get_client() if equity else None)
    risk_state      = _live_risk_state(
        equity, unrealized, long_pnl, hedge_pnl,
        drift_max, len(broker_rows), peak_state, broker_pnl, gross_exp_pct,
    )
    anchor = datetime.now(timezone.utc)
    next_rebalance = _next_trading_day(anchor, int(config.REBALANCE_DAYS))

    return {
        "ok":                   True,
        "equity":               round(equity, 2),
        "positions":            rows,
        "filled_position_count": len(rows),   # all broker positions (filled + pending-order)
        "open_order_count":     len(open_orders or []),
        "pending_order_count":  len(open_orders or []),
        "unrealized_pnl":       round(unrealized, 2),
        "broker_pnl":           round(broker_pnl, 2),
        "long_pnl":             round(long_pnl, 2),
        "hedge_pnl":            round(hedge_pnl, 2),
        "gross_exposure_pct":   round(gross_exp_pct, 2),
        "max_drift_pct":        round(drift_max, 2),
        "risk_state":           risk_state,
        "next_rebalance_date":  next_rebalance.isoformat(),
        "events":               _load_events(limit=20),
    }


def _exit_recommendations(positions: dict, accounting: dict, live_scores: dict | None, model: dict) -> list[dict]:
    memory = strategy_model.update_position_memory(positions)
    regime = (live_scores or {}).get("regime") or "UNKNOWN"
    rows = accounting.get("positions") or []
    recommendations: list[dict] = []

    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        pnl_pct = float(row.get("unrealized_pnl_pct") or 0)
        peak_pnl_pct = float((memory.get(sym) or {}).get("peak_unrealized_pnl_pct") or pnl_pct)
        giveback_pct = peak_pnl_pct - pnl_pct
        hold_days = _holding_days(memory.get(sym) or {})
        reason = None

        if pnl_pct <= -float(model.get("trailing_stop_pct", 3.0)):
            reason = "loss_stop"
        elif peak_pnl_pct >= float(model.get("profit_lock_trigger_pct", 2.0)) and giveback_pct >= float(model.get("profit_giveback_pct", 1.0)):
            reason = "profit_giveback"
        elif hold_days >= int(model.get("max_holding_days", 2)):
            reason = "max_holding_days"
        elif bool(model.get("exit_on_regime_flip", True)) and _regime_against_position(sym, regime):
            reason = "regime_flip"

        if reason:
            recommendations.append({
                "symbol": sym,
                "side": "sell",
                "qty": row.get("qty"),
                "reason": reason,
                "unrealized_pnl": row.get("unrealized_pnl"),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "peak_unrealized_pnl_pct": round(peak_pnl_pct, 2),
                "giveback_pct": round(giveback_pct, 2),
                "holding_days": hold_days,
            })

    return recommendations


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
    symbol = symbol.upper()
    if regime == "CHOPPY":
        return True
    if regime == "BULL" and symbol in sg.BEAR_UNIVERSE:
        return True
    if regime == "BEAR" and symbol in sg.BULL_UNIVERSE:
        return True
    return False

def _next_trading_day(from_dt: datetime, n_days: int = 1):
    d = from_dt.date()
    count = 0
    while count < n_days:
        d = d + timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in MARKET_HOLIDAYS_2026:
            count += 1
    return d

# ── News fetch (cached) ────────────────────────────────────────────────────────
def _get_news_for_symbols(symbols: list[str]) -> dict[str, list[dict]]:
    """Return {symbol: [articles]} using a 10-min shared cache."""
    now = time.time()
    requested = {s.upper() for s in symbols}
    cached = _news_cache.get("data") or {}
    cached_symbols = set(_news_cache.get("symbols") or set())
    cache_ts = float(_news_cache.get("ts") or 0)
    if cached and requested.issubset(cached_symbols) and now - cache_ts < _NEWS_TTL:
        return {sym: cached.get(sym, []) for sym in requested}

    by_symbol: dict[str, list[dict]] = {}
    try:
        all_articles = get_client().get_news(symbols, limit=50)
        for art in all_articles:
            for sym in (art.get("symbols") or []):
                sym = sym.upper()
                if sym in requested:
                    by_symbol.setdefault(sym, []).append(art)
    except Exception as e:
        log.warning("News fetch failed: %s", e)

    _news_cache["ts"]   = now
    _news_cache["data"] = by_symbol
    _news_cache["symbols"] = requested
    return by_symbol

# ── Live scores (signal engine + Alpaca news) ──────────────────────────────────
def _run_live_scores() -> dict:
    """
    Full signal scoring pass:
    1. yfinance quotes for all watchlist symbols
    2. Alpaca news for each candidate
    3. News delta applied on top of pure-Python score
    """
    try:
        quotes   = trader.fetch_quotes(sg.ALL_SYMBOLS)
        quotes   = sg.enrich_quotes_with_indicators(quotes, sg.ALL_SYMBOLS)
        regime   = sg.detect_regime(quotes)
        news_map = _get_news_for_symbols(sg.ALL_SYMBOLS)
        universe = sg.BULL_UNIVERSE if regime in ("BULL", "CHOPPY") else sg.BEAR_UNIVERSE
        signals  = []

        for sym in universe:
            base_score = sg.score_symbol(sym, quotes, regime)
            if base_score < 50:
                continue
            _, technical_reasons = sg._technical_bias(sym, quotes, regime)

            # Gemini borderline boost (only 60-74, already in signals.py)
            if 60 <= base_score < 75:
                base_score += sg.gemini_sentiment_boost(sym, base_score)

            # Alpaca news delta
            articles = news_map.get(sym, [])
            news_delta, news_terms = _news_score_delta(articles)
            final_score = max(0, min(100, base_score + news_delta))

            signals.append({
                "symbol":      sym,
                "score":       final_score,
                "base_score":  base_score,
                "news_delta":  news_delta,
                "news_terms":  news_terms,
                "regime":      regime,
                "side":        "buy",
                "quote":       quotes.get(sym, {}),
                "technicals":   (quotes.get(sym, {}) or {}).get("technicals") or {},
                "technical_reasons": technical_reasons,
                "news":        (articles or [])[:3],   # top 3 headlines for UI
            })

        signals.sort(key=lambda x: x["score"], reverse=True)
        result = {
            "ok":         True,
            "scored_at":  datetime.now(timezone.utc).isoformat(),
            "regime":     regime,
            "signals":    signals,
            "top":        signals[:5],
        }
    except Exception as e:
        log.warning("live scores failed: %s", e)
        result = {"ok": False, "error": str(e), "signals": [], "top": []}

    _live_scores_cache["ts"]   = time.time()
    _live_scores_cache["data"] = result
    return result

# ── Preview order builder ──────────────────────────────────────────────────────
_POSITION_SIZE_PCT = 0.05   # 5% of equity per position

def _build_preview_orders(
    plan_type: str,
    account: dict,
    broker_positions: dict,
    live_scores: dict | None,
    custom_orders: list[dict] | None = None,
    model: dict | None = None,
) -> list[dict]:
    equity = float((account or {}).get("equity") or 0)
    cash   = float((account or {}).get("cash") or 0)
    model = strategy_model.sanitize_model(model or strategy_model.load_model())
    min_conviction = int(model["min_conviction"])
    position_size_pct = float(model["position_size_pct"])

    if plan_type == "custom" and custom_orders:
        return [
            {
                "symbol":          str(o.get("symbol", "")).upper(),
                "side":            str(o.get("side", "buy")).lower(),
                "qty":             int(o.get("qty") or 0),
                "estimated_value": 0,
                "reason":          "Manual order",
                "score":           None,
            }
            for o in custom_orders if o.get("symbol") and int(o.get("qty") or 0) > 0
        ]

    if plan_type == "exit_all":
        orders = []
        for sym, pos in (broker_positions or {}).items():
            qty = int(abs(float(pos.get("qty") or 0)))
            if qty <= 0:
                continue
            orders.append({
                "symbol":          sym.upper(),
                "side":            "sell",
                "qty":             qty,
                "estimated_value": round(abs(float(pos.get("market_val") or 0)), 2),
                "reason":          "Exit all positions",
                "score":           None,
            })
        return orders

    # build — use live scores if available, else run them now
    scores = live_scores if (live_scores and live_scores.get("ok")) else _run_live_scores()
    orders = []
    deployed_cash = 0.0

    for sig in (scores.get("signals") or []):
        if float(sig.get("score") or 0) < min_conviction:
            continue
        sym = sig["symbol"]
        if sym in broker_positions:
            continue  # already holding

        price = float((sig.get("quote") or {}).get("price") or 0)
        if price <= 0:
            try:
                price = get_client().get_current_price(sym) or 0
            except Exception:
                pass
        if price <= 0:
            continue

        alloc = equity * position_size_pct
        qty   = max(1, int(alloc / price))
        cost  = qty * price
        if deployed_cash + cost > cash * 0.95:   # leave 5% cash buffer
            break
        deployed_cash += cost

        orders.append({
            "symbol":          sym,
            "side":            "buy",
            "qty":             qty,
            "estimated_value": round(cost, 2),
            "reason":          f"Score {sig['score']} ({sig['regime']}) news_delta={sig.get('news_delta',0):+d}",
            "score":           sig["score"],
            "news":            (sig.get("news") or [])[:2],
            "model_generation": model.get("generation"),
        })

    return orders


# ── Agent runtime ─────────────────────────────────────────────────────────────
class PortfolioAgent:
    """Owns broker state, accounting, and portfolio history."""

    def snapshot(self, live_scores: dict | None = None, recent_limit: int = 50) -> dict:
        client      = get_client()
        account     = client.get_account()
        positions   = client.get_positions()
        open_orders = client.get_open_orders()
        recent      = client.get_recent_orders(limit=recent_limit)
        accounting  = _build_accounting(positions, open_orders, recent, account, live_scores)
        model = strategy_model.load_model()
        exit_recs = _exit_recommendations(positions, accounting, live_scores, model)
        accounting["exit_recommendations"] = exit_recs
        return {
            "account": account,
            "positions": positions,
            "open_orders": open_orders,
            "recent_orders": recent,
            "accounting": accounting,
            "clock": client.get_clock(),
            "exit_recommendations": exit_recs,
        }

    def history(self, period: str, timeframe: str) -> dict:
        return get_client().get_portfolio_history(period=period, timeframe=timeframe)


class MarketNewsAgent:
    """Owns signal scoring and headline risk."""

    def live_scores(self, force: bool = False) -> dict | None:
        now = time.time()
        cached = _live_scores_cache.get("data")
        if not force and cached and now - float(_live_scores_cache.get("ts") or 0) < _LIVE_SCORE_TTL:
            return cached
        if force:
            return _run_live_scores()
        return cached

    def position_risk(self, accounting: dict) -> list[dict]:
        held_syms = [
            p["symbol"] for p in (accounting.get("positions") or [])
            if p.get("status") == "filled"
        ]
        news_risk: list[dict] = []
        if not held_syms:
            return news_risk

        news_map = _get_news_for_symbols(held_syms)
        for sym in held_syms:
            articles = news_map.get(sym, [])
            delta, terms = _news_score_delta(articles)
            if delta < -5:
                top_headline = articles[0].get("headline", "") if articles else ""
                news_risk.append({
                    "symbol": sym,
                    "delta": delta,
                    "terms": terms,
                    "headline": top_headline[:100],
                })
        return sorted(news_risk, key=lambda x: x["delta"])


class RiskAgent:
    """Owns deterministic trading permission checks."""

    def order_gate(self, orders: list[dict], snapshot: dict) -> dict:
        model = strategy_model.load_model()
        accounting = snapshot.get("accounting") or {}
        account    = snapshot.get("account") or {}
        clock      = snapshot.get("clock") or {}
        positions  = snapshot.get("positions") or {}
        risk_state = accounting.get("risk_state") or {}

        buys = [o for o in orders if str(o.get("side") or "buy").lower() == "buy"]
        sells = [o for o in orders if str(o.get("side") or "buy").lower() == "sell"]
        errors: list[dict] = []
        warnings: list[str] = []

        for o in orders:
            sym = str(o.get("symbol") or "").upper()
            qty = int(float(o.get("qty") or 0))
            side = str(o.get("side") or "buy").lower()
            if not sym or qty <= 0:
                errors.append({"symbol": sym, "error": "Invalid symbol or qty"})
            if side not in {"buy", "sell"}:
                errors.append({"symbol": sym, "error": f"Unsupported side: {side}"})

        if buys and not bool(clock.get("is_open")):
            errors.append({"error": "Market is closed. Buy orders are blocked."})

        if buys and risk_state.get("action") == "reduce_risk":
            errors.append({"error": risk_state.get("message") or "Risk breach blocks new buy orders."})

        current_symbols = {str(s).upper() for s in (positions or {}).keys()}
        new_buy_symbols = {
            str(o.get("symbol") or "").upper()
            for o in buys
            if str(o.get("symbol") or "").upper() not in current_symbols
        }
        max_positions = int(model["max_positions"])
        if len(current_symbols | new_buy_symbols) > max_positions:
            errors.append({
                "error": f"Max positions would be exceeded ({len(current_symbols | new_buy_symbols)}/{max_positions})."
            })

        cash = float(account.get("cash") or 0)
        estimated_buy_value = sum(float(o.get("estimated_value") or 0) for o in buys)
        if estimated_buy_value > cash * 0.98:
            errors.append({"error": "Buy orders exceed available cash buffer."})

        if sells and not buys:
            warnings.append("Sell-only order set allowed even when risk is elevated.")

        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "risk_state": risk_state,
            "model": model,
        }


class TradingAgent:
    """Owns order previews and Alpaca submission."""

    def preview(self, plan_type: str, custom_orders: list[dict] | None, snapshot: dict, live_scores: dict | None) -> dict:
        orders = _build_preview_orders(
            plan_type,
            snapshot.get("account") or {},
            snapshot.get("positions") or {},
            live_scores,
            custom_orders,
            strategy_model.load_model(),
        )
        gate = RiskAgent().order_gate(orders, snapshot)
        return {
            "orders": orders,
            "risk_gate": gate,
            "decision": _decision_from_orders(orders, gate, snapshot.get("accounting") or {}, live_scores),
        }

    def place(self, orders: list[dict], snapshot: dict) -> dict:
        gate = RiskAgent().order_gate(orders, snapshot)
        if not gate.get("ok"):
            _append_event("orders_blocked_by_risk", {"orders": orders, "gate": gate})
            return {"ok": False, "submitted": [], "errors": gate.get("errors") or [], "risk_gate": gate}

        client    = get_client()
        submitted = []
        errors    = []
        for o in orders:
            sym  = str(o.get("symbol") or "").upper()
            side = str(o.get("side") or "buy").lower()
            qty  = int(float(o.get("qty") or 0))
            if not sym or qty <= 0:
                errors.append({"symbol": sym, "error": "Invalid symbol or qty"})
                continue
            try:
                result = client.place_market_order(sym, qty, side)
                submitted.append({**o, **result})
            except Exception as e:
                errors.append({"symbol": sym, "qty": qty, "side": side, "error": str(e)})

        return {"ok": len(submitted) > 0, "submitted": submitted, "errors": errors, "risk_gate": gate}


class ModelAgent:
    """Owns adaptive strategy parameters and guarded learning."""

    def current(self) -> dict:
        return strategy_model.load_model()

    def learn(self, backtest: dict, apply: bool = False) -> dict:
        current = self.current()
        proposal = strategy_model.propose_adjustment(current, backtest)
        if apply and proposal.get("changed"):
            updated = dict(proposal["proposed"])
            updated["last_backtest"] = backtest
            saved = strategy_model.save_model(updated)
            _append_event("model_updated", {
                "from": current,
                "to": saved,
                "backtest_summary": backtest.get("metrics") or {},
                "reasons": proposal.get("reasons") or [],
            })
            proposal["applied"] = True
            proposal["model"] = saved
        else:
            proposal["applied"] = False
            proposal["model"] = current
        return proposal


class BacktestAgent:
    """Evaluates recent portfolio/order evidence before model changes."""

    def run(self, period: str = "3M", timeframe: str = "1D") -> dict:
        client = get_client()
        history = client.get_portfolio_history(period=period, timeframe=timeframe)
        orders = client.get_recent_orders(limit=200)
        metrics = self._metrics_from_history(history)
        trade_metrics = self._metrics_from_orders(orders)
        metrics.update(trade_metrics)
        model = strategy_model.load_model()
        optimizer = self._daily_profit_optimizer(model, period=period)
        proposal = strategy_model.propose_adjustment(model, {"metrics": metrics, "optimizer": optimizer})
        return {
            "ok": True,
            "period": period,
            "timeframe": timeframe,
            "metrics": metrics,
            "optimizer": optimizer,
            "model": model,
            "proposal": proposal,
            "notes": [
                "Optimizer ranks candidate models by simulated daily profit, with drawdown and overtrading penalties.",
                "Learning changes are bounded and require an explicit apply request.",
            ],
        }

    def _metrics_from_history(self, history: dict) -> dict:
        equities = [float(v) for v in (history.get("equity") or []) if v and float(v) > 0]
        if len(equities) < 2:
            return {
                "total_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "equity_points": len(equities),
            }

        start = equities[0]
        end = equities[-1]
        peak = start
        max_drawdown = 0.0
        for equity in equities:
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)

        return {
            "total_return_pct": round((end - start) / start * 100, 2) if start else 0.0,
            "max_drawdown_pct": round(max_drawdown, 2),
            "start_equity": round(start, 2),
            "end_equity": round(end, 2),
            "equity_points": len(equities),
        }

    def _metrics_from_orders(self, orders: list[dict]) -> dict:
        by_symbol: dict[str, list[dict]] = {}
        for order in orders:
            if str(order.get("status") or "").lower() != "filled":
                continue
            by_symbol.setdefault(str(order.get("symbol") or "").upper(), []).append(order)

        realized: list[float] = []
        for symbol, rows in by_symbol.items():
            rows = sorted(rows, key=lambda o: str(o.get("filled_at") or o.get("submitted_at") or ""))
            cost_basis = 0.0
            qty_held = 0.0
            for order in rows:
                qty = float(order.get("filled_qty") or order.get("qty") or 0)
                price = float(order.get("filled_avg_price") or 0)
                if qty <= 0 or price <= 0:
                    continue
                if str(order.get("side") or "").lower() == "buy":
                    cost_basis += qty * price
                    qty_held += qty
                elif str(order.get("side") or "").lower() == "sell" and qty_held > 0:
                    avg_cost = cost_basis / qty_held if qty_held else 0
                    sell_qty = min(qty, qty_held)
                    realized.append((price - avg_cost) * sell_qty)
                    qty_held -= sell_qty
                    cost_basis = avg_cost * qty_held

        wins = [p for p in realized if p > 0]
        losses = [p for p in realized if p < 0]
        trade_count = len(realized)
        return {
            "trade_count": trade_count,
            "realized_pnl": round(sum(realized), 2),
            "win_rate": round(len(wins) / trade_count, 2) if trade_count else 0.0,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        }

    def _daily_profit_optimizer(self, model: dict, period: str = "3M") -> dict:
        candidates = strategy_model.candidate_models(model)
        data = self._load_daily_replay_data(period)
        if len(data) < 3:
            return {
                "ok": False,
                "best_model": None,
                "confidence": "low",
                "reasons": ["Not enough market data to replay daily candidates."],
                "candidates": [],
            }

        scored = [self._score_candidate(candidate, data) for candidate in candidates]
        scored.sort(key=lambda row: row["objective"], reverse=True)
        best = scored[0]
        base_score = self._score_candidate(model, data)
        improvement = best["objective"] - base_score["objective"]
        confidence = "medium" if len(data) >= 20 and improvement > 0.05 else "low"
        reasons = [
            f"Best candidate objective {best['objective']:.2f} vs current {base_score['objective']:.2f}.",
            f"Simulated daily return {best['daily_return_pct']:.2f}% with max drawdown {best['max_drawdown_pct']:.2f}%.",
        ]
        if improvement <= 0:
            reasons.append("Current model remains competitive; no aggressive change recommended.")

        return {
            "ok": True,
            "objective": "maximize_daily_profit",
            "confidence": confidence,
            "days": len(data),
            "best_model": best["model"] if improvement > 0 else None,
            "best": best,
            "current": base_score,
            "improvement": round(improvement, 4),
            "top_candidates": scored[:8],
            "reasons": reasons,
        }

    def _load_daily_replay_data(self, period: str) -> list[dict]:
        try:
            raw = yf.download(
                tickers=" ".join(sg.ALL_SYMBOLS),
                period=period,
                interval="1d",
                auto_adjust=False,
                progress=False,
                group_by="ticker",
                threads=True,
            )
        except Exception as e:
            log.warning("optimizer market data fetch failed: %s", e)
            return []

        rows: list[dict] = []
        for idx in range(1, len(raw.index)):
            day_quotes: dict[str, dict] = {}
            next_quotes: dict[str, dict] = {}
            for sym in sg.ALL_SYMBOLS:
                try:
                    prev_close = float(raw[(sym, "Close")].iloc[idx - 1])
                    close = float(raw[(sym, "Close")].iloc[idx])
                    if idx + 1 < len(raw.index):
                        next_close = float(raw[(sym, "Close")].iloc[idx + 1])
                    else:
                        next_close = close
                    if prev_close <= 0 or close <= 0:
                        continue
                    bars = []
                    for hist_idx in range(0, idx + 1):
                        try:
                            hist_close = float(raw[(sym, "Close")].iloc[hist_idx])
                            if hist_close <= 0:
                                continue
                            bars.append({
                                "open": float(raw[(sym, "Open")].iloc[hist_idx]),
                                "high": float(raw[(sym, "High")].iloc[hist_idx]),
                                "low": float(raw[(sym, "Low")].iloc[hist_idx]),
                                "close": hist_close,
                                "volume": float(raw[(sym, "Volume")].iloc[hist_idx]),
                            })
                        except Exception:
                            continue
                    day_quotes[sym] = {
                        "price": round(close, 4),
                        "prev_close": round(prev_close, 4),
                        "change_pct": round((close - prev_close) / prev_close * 100, 4),
                    }
                    technicals = sg.compute_technicals(bars)
                    if technicals:
                        day_quotes[sym]["technicals"] = technicals
                    next_quotes[sym] = {"price": round(next_close, 4)}
                except Exception:
                    continue

            qqq_tech = (day_quotes.get("QQQ") or {}).get("technicals") or {}
            qqq_return = qqq_tech.get("return_5d_pct")
            if qqq_return is not None:
                for sym, row in list(day_quotes.items()):
                    tech = dict((row or {}).get("technicals") or {})
                    if "return_5d_pct" in tech:
                        tech["relative_strength_qqq"] = round(float(tech["return_5d_pct"]) - float(qqq_return), 3)
                        row["technicals"] = tech

            if "QQQ" in day_quotes and "SPY" in day_quotes:
                rows.append({
                    "date": str(raw.index[idx].date()),
                    "quotes": day_quotes,
                    "next_quotes": next_quotes,
                })
        return rows

    def _score_candidate(self, model: dict, rows: list[dict]) -> dict:
        model = strategy_model.sanitize_model(model)
        equity = 100000.0
        peak = equity
        max_drawdown = 0.0
        trades = 0
        winning_days = 0
        losing_days = 0
        daily_returns: list[float] = []

        for row in rows:
            quotes = row["quotes"]
            next_quotes = row["next_quotes"]
            regime = sg.detect_regime(quotes)
            day_pnl = 0.0
            if regime != "CHOPPY":
                universe = sg.BULL_UNIVERSE if regime == "BULL" else sg.BEAR_UNIVERSE
                signals = []
                for sym in universe:
                    score = sg.score_symbol(sym, quotes, regime)
                    if score >= int(model["min_conviction"]):
                        signals.append((sym, score))
                signals.sort(key=lambda item: item[1], reverse=True)
                selected = signals[:int(model["max_positions"])]

                for sym, score in selected:
                    entry = float(quotes.get(sym, {}).get("price") or 0)
                    if entry <= 0:
                        continue
                    exit_px = self._simulated_exit_price(sym, rows, row, entry, model)
                    if exit_px <= 0:
                        continue
                    allocation = equity * float(model["position_size_pct"])
                    raw_pnl = allocation * ((exit_px - entry) / entry)
                    stop_loss = -allocation * (float(model["trailing_stop_pct"]) / 100)
                    day_pnl += max(raw_pnl, stop_loss)
                    trades += 1

            equity += day_pnl
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak else 0)
            day_return = day_pnl / max(equity - day_pnl, 1) * 100
            daily_returns.append(day_return)
            if day_pnl > 0:
                winning_days += 1
            elif day_pnl < 0:
                losing_days += 1

        total_return_pct = (equity - 100000.0) / 100000.0 * 100
        daily_return_pct = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
        trade_penalty = max(0, trades - len(rows) * int(model["max_positions"]) * 0.55) * 0.01
        drawdown_penalty = max_drawdown * 0.7
        objective = daily_return_pct * 10 + total_return_pct - drawdown_penalty - trade_penalty

        return {
            "model": model,
            "objective": round(objective, 4),
            "total_return_pct": round(total_return_pct, 2),
            "daily_return_pct": round(daily_return_pct, 3),
            "max_drawdown_pct": round(max_drawdown, 2),
            "trades": trades,
            "winning_days": winning_days,
            "losing_days": losing_days,
        }

    def _simulated_exit_price(self, symbol: str, rows: list[dict], entry_row: dict, entry: float, model: dict) -> float:
        try:
            start_index = rows.index(entry_row)
        except ValueError:
            start_index = 0

        max_days = int(model["max_holding_days"])
        lock_trigger = float(model["profit_lock_trigger_pct"])
        giveback_limit = float(model["profit_giveback_pct"])
        peak_return = 0.0
        last_price = entry

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


class DecisionCoordinator:
    """Composes portfolio, market/news, risk, and trading into app-ready state."""

    def __init__(self) -> None:
        self.portfolio = PortfolioAgent()
        self.market_news = MarketNewsAgent()
        self.trading = TradingAgent()
        self.model = ModelAgent()
        self.backtest = BacktestAgent()

    def overview(self) -> dict:
        live_scores = self.market_news.live_scores(force=False)
        snap = self.portfolio.snapshot(live_scores=live_scores)
        accounting = snap.get("accounting") or {}
        news_risk = self.market_news.position_risk(accounting)
        decision = _decision_from_state(accounting, live_scores, news_risk)
        model = self.model.current()
        narrative = _portfolio_narrative(accounting, live_scores, news_risk, model, decision)
        return {
            "ok": True,
            "paper": config.PAPER,
            "account": snap.get("account"),
            "clock": snap.get("clock"),
            "accounting": accounting,
            "live_scores": live_scores,
            "news_risk": news_risk,
            "market_sentiment": None,
            "portfolio_narrative": narrative,
            "decision": decision,
            "agents": _agent_status(decision, accounting, live_scores),
            "model": model,
        }

    def activity(self) -> dict:
        snap = self.portfolio.snapshot(live_scores=_live_scores_cache.get("data"), recent_limit=100)
        orders = snap.get("recent_orders") or []
        open_o = snap.get("open_orders") or []
        acctg = snap.get("accounting") or {}
        rows = acctg.get("positions") or []

        filled = [o for o in orders if str(o.get("status","")).lower() == "filled"]
        canceled = [o for o in orders if str(o.get("status","")).lower() in {"canceled","expired","rejected"}]
        last_fill = sorted(filled, key=lambda o: str(o.get("filled_at") or o.get("submitted_at") or ""), reverse=True)
        long_rows = [r for r in rows if r.get("side") != "SHORT"]
        short_rows = [r for r in rows if r.get("side") == "SHORT"]

        return {
            "ok": True,
            "recent_orders": orders[:50],
            "events": _load_events(limit=30),
            "trading_results": {
                "open_pnl": round(float(acctg.get("broker_pnl") or 0), 2),
                "long_pnl": round(sum(float(r.get("unrealized_pnl") or 0) for r in long_rows), 2),
                "hedge_pnl": round(sum(float(r.get("unrealized_pnl") or 0) for r in short_rows), 2),
                "winner_count": len([r for r in rows if float(r.get("unrealized_pnl") or 0) > 0]),
                "loser_count": len([r for r in rows if float(r.get("unrealized_pnl") or 0) < 0]),
                "filled_order_count": len(filled),
                "pending_order_count": len(open_o),
                "canceled_order_count": len(canceled),
                "last_fill": last_fill[0] if last_fill else None,
            },
        }


def _decision_from_state(accounting: dict, live_scores: dict | None, news_risk: list[dict]) -> dict:
    risk_state = accounting.get("risk_state") or {}
    positions = accounting.get("positions") or []
    top = (live_scores or {}).get("top") or []

    if risk_state.get("action") == "reduce_risk":
        action = "reduce_risk"
        summary = risk_state.get("message") or "Risk is elevated. Reduce exposure before adding."
        severity = "danger"
    elif accounting.get("exit_recommendations"):
        first = accounting["exit_recommendations"][0]
        action = "exit_positions"
        summary = f"Exit recommended for {first.get('symbol')}: {first.get('reason')}."
        severity = "warning"
    elif news_risk:
        action = "review_news"
        summary = f"News risk on {news_risk[0].get('symbol')}. Review before adding exposure."
        severity = "warning"
    elif positions:
        action = "monitor"
        summary = f"Monitoring {len(positions)} open position(s)."
        severity = "normal"
    elif top:
        action = "scan_ready"
        summary = f"Top signal: {top[0].get('symbol')} score {top[0].get('score')}."
        severity = "normal"
    else:
        action = "wait"
        summary = "No active positions or cached conviction signals."
        severity = "normal"

    return {
        "action": action,
        "severity": severity,
        "summary": summary,
        "risk_action": risk_state.get("action"),
        "risk_status": risk_state.get("status"),
    }


def _portfolio_narrative(
    accounting: dict,
    live_scores: dict | None,
    news_risk: list[dict],
    model: dict,
    decision: dict,
) -> dict:
    """Translate raw agent state into a portfolio-level explanation."""
    regime = str((live_scores or {}).get("regime") or "UNKNOWN").upper()
    prev_regime = str((live_scores or {}).get("previous_regime") or "").upper()
    top = ((live_scores or {}).get("top") or (live_scores or {}).get("signals") or [])[:5]
    exits = accounting.get("exit_recommendations") or []
    positions = accounting.get("positions") or []
    gross_exposure = float(accounting.get("gross_exposure_pct") or 0)
    open_pnl = float(accounting.get("broker_pnl") or accounting.get("unrealized_pnl") or 0)

    sentiment_from = prev_regime if prev_regime and prev_regime != regime else "market"
    sentiment_to = regime if regime != "UNKNOWN" else "unclear"

    evidence: list[str] = []
    for sig in top[:3]:
        sym = str(sig.get("symbol") or "?").upper()
        score = int(float(sig.get("score") or 0))
        quote = sig.get("quote") or {}
        tech = sig.get("technicals") or quote.get("technicals") or {}
        reasons = sig.get("technical_reasons") or []
        reason = str(reasons[0]) if reasons else ""
        avwap = float(tech.get("price_vs_avwap_low_pct") or tech.get("price_vs_avwap_high_pct") or 0)
        trend = str(tech.get("trend_direction") or "trend").replace("_", " ")
        fib = str(tech.get("fib_position") or "").replace("_", " ")
        detail = f"{sym} score {score}"
        if reason:
            detail += f", {reason}"
        elif avwap:
            detail += f", AVWAP {avwap:+.1f}%"
        if trend and trend != "trend":
            detail += f", {trend} trend"
        if fib and fib != "unknown":
            detail += f", fib {fib}"
        evidence.append(detail)

    sells: list[str] = []
    for item in exits[:3]:
        sym = str(item.get("symbol") or "?").upper()
        reason = str(item.get("reason") or "exit").replace("_", " ")
        pnl = float(item.get("unrealized_pnl_pct") or 0)
        giveback = float(item.get("giveback_pct") or 0)
        sells.append(f"Sell {sym}: {reason}, now {pnl:+.1f}%, giveback {giveback:.1f}%.")

    held = {str(p.get("symbol") or "").upper() for p in positions}
    buys: list[str] = []
    min_conviction = int(model.get("min_conviction") or 75)
    for sig in top:
        sym = str(sig.get("symbol") or "").upper()
        score = int(float(sig.get("score") or 0))
        if not sym or sym in held or score < min_conviction:
            continue
        quote = sig.get("quote") or {}
        tech = sig.get("technicals") or quote.get("technicals") or {}
        trend = str(tech.get("trend_direction") or "trend").replace("_", " ")
        avwap = float(tech.get("price_vs_avwap_low_pct") or 0)
        buys.append(f"Buy {sym}: score {score}, {trend} trend, AVWAP {avwap:+.1f}%.")
        if len(buys) >= 3:
            break

    if not buys and not sells:
        if decision.get("action") == "monitor":
            next_actions = ["Monitor current exposure; no portfolio change until signal or risk state changes."]
        elif decision.get("action") == "scan_ready":
            next_actions = ["Scan is ready; wait for risk gate and market clock before adding exposure."]
        else:
            next_actions = [decision.get("summary") or "Wait for a clearer portfolio signal."]
    else:
        next_actions = sells + buys

    model_note = (
        f"Model G{model.get('generation', 0)} is using min conviction {model.get('min_conviction')} "
        f"and max hold {model.get('max_holding_days')}d; backtest agent can tighten/loosen these after recent trade evidence."
    )

    why = evidence or [decision.get("summary") or "No strong technical evidence is cached yet."]
    if news_risk:
        nr = news_risk[0]
        why.insert(0, f"News risk: {nr.get('symbol')} delta {nr.get('delta')}, {nr.get('headline')}")

    summary = (
        f"Sentiment is moving from {sentiment_from} to {sentiment_to}. "
        f"Book exposure is {gross_exposure:.0f}% with open P&L ${open_pnl:,.0f}."
    )

    return {
        "sentiment_from": sentiment_from,
        "sentiment_to": sentiment_to,
        "summary": summary,
        "why": why[:4],
        "next_actions": next_actions[:5],
        "model_adjustment": model_note,
    }


def _decision_from_orders(orders: list[dict], gate: dict, accounting: dict, live_scores: dict | None) -> dict:
    if not orders:
        return {"action": "no_order", "severity": "normal", "summary": "No eligible orders."}
    if not gate.get("ok"):
        return {
            "action": "blocked",
            "severity": "danger",
            "summary": "; ".join(str(e.get("error") or e) for e in gate.get("errors") or []),
        }
    buys = [o for o in orders if str(o.get("side") or "buy").lower() == "buy"]
    sells = [o for o in orders if str(o.get("side") or "buy").lower() == "sell"]
    if sells and not buys:
        return {"action": "exit", "severity": "warning", "summary": f"Ready to submit {len(sells)} exit order(s)."}
    return {"action": "enter", "severity": "normal", "summary": f"Ready to submit {len(orders)} paper order(s)."}


def _agent_status(decision: dict, accounting: dict, live_scores: dict | None) -> dict:
    risk_state = accounting.get("risk_state") or {}
    return {
        "portfolio_agent": {
            "status": "online",
            "positions": len(accounting.get("positions") or []),
            "open_pnl": accounting.get("broker_pnl"),
        },
        "market_news_agent": {
            "status": "online" if live_scores else "idle",
            "regime": (live_scores or {}).get("regime"),
            "signal_count": len((live_scores or {}).get("signals") or []),
        },
        "risk_agent": {
            "status": risk_state.get("status") or "unknown",
            "action": risk_state.get("action") or "unknown",
        },
        "trading_agent": {
            "status": "armed" if config.fund_manager_order_submission_enabled() else "locked",
            "paper": config.PAPER,
        },
        "model_agent": {
            "status": "learning",
            "generation": strategy_model.load_model().get("generation"),
        },
        "backtest_agent": {
            "status": "ready",
        },
        "coordinator": {
            "action": decision.get("action"),
            "severity": decision.get("severity"),
            "summary": decision.get("summary"),
        },
    }


def _llm_explain_decision(decision: dict, overview: dict) -> str:
    fallback = decision.get("summary") or "Agent state is available."
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        return fallback

    try:
        import google.generativeai as genai  # type: ignore
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            system_instruction=(
                "Explain an automated paper-trading agent decision in one short paragraph. "
                "Do not recommend trades. Explain only what the system is doing and why."
            ),
        )
        account = overview.get("account") or {}
        accounting = overview.get("accounting") or {}
        risk = accounting.get("risk_state") or {}
        prompt = json.dumps({
            "decision": decision,
            "equity": account.get("equity"),
            "cash": account.get("cash"),
            "positions": accounting.get("positions") or [],
            "risk": risk,
            "news_risk": overview.get("news_risk") or [],
            "live_scores": overview.get("live_scores") or {},
        }, default=str)[:12000]
        response = model.generate_content(prompt)
        text = (response.text or "").strip()
        return text or fallback
    except Exception as e:
        log.warning("decision explanation error: %s", e)
        return fallback


_coordinator = DecisionCoordinator()

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    payload = {
        "ok": True,
        "paper": config.PAPER,
        "broker_connected": False,
        "market_open": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        clock = get_client().get_clock()
        payload["broker_connected"] = True
        payload["market_open"] = bool(getattr(clock, "is_open", False))
    except Exception as e:
        payload["broker_error"] = str(e)
    return jsonify(payload)


@app.route("/api/lab/overview")
@require_api_key
def api_lab_overview():
    now = time.time()
    if _overview_cache["data"] and now - float(_overview_cache["ts"] or 0) < _OVERVIEW_TTL:
        return jsonify(_overview_cache["data"])
    try:
        data = _coordinator.overview()
        _overview_cache["ts"]   = now
        _overview_cache["data"] = data
        return jsonify(data)
    except Exception as e:
        log.warning("overview error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/activity")
def api_lab_activity():
    try:
        return jsonify(_coordinator.activity())
    except Exception as e:
        log.warning("activity error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/portfolio/history")
@require_api_key
def api_lab_portfolio_history():
    period = str(request.args.get("period") or "1M")
    timeframe = str(request.args.get("timeframe") or "1D")
    allowed_periods = {"1D", "7D", "15D", "1M", "3M", "6M", "1A", "all"}
    allowed_timeframes = {"1Min", "5Min", "15Min", "1H", "1D"}

    if period not in allowed_periods:
        return jsonify({"ok": False, "error": f"Unsupported period: {period}"}), 400
    if timeframe not in allowed_timeframes:
        return jsonify({"ok": False, "error": f"Unsupported timeframe: {timeframe}"}), 400

    try:
        history = _coordinator.portfolio.history(period=period, timeframe=timeframe)
        return jsonify({
            "ok": True,
            "period": period,
            "timeframe": timeframe,
            "history": history,
        })
    except Exception as e:
        log.warning("portfolio history error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/agents/decision")
@require_api_key
def api_lab_agents_decision():
    try:
        overview = _coordinator.overview()
        decision = overview.get("decision") or {}
        explain = str(request.args.get("explain") or "").lower() in {"1", "true", "yes"}
        result = {
            "ok": True,
            "decision": decision,
            "agents": overview.get("agents") or {},
            "risk_state": (overview.get("accounting") or {}).get("risk_state") or {},
            "news_risk": overview.get("news_risk") or [],
        }
        if explain:
            result["explanation"] = _llm_explain_decision(decision, overview)
        return jsonify(result)
    except Exception as e:
        log.warning("agent decision error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/model")
@require_api_key
def api_lab_model():
    try:
        return jsonify({
            "ok": True,
            "model": _coordinator.model.current(),
            "bounds": strategy_model.BOUNDS,
        })
    except Exception as e:
        log.warning("model status error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/backtest", methods=["POST"])
@require_api_key
def api_lab_backtest():
    body = request.get_json(silent=True) or {}
    period = str(body.get("period") or "3M")
    timeframe = str(body.get("timeframe") or "1D")
    allowed_periods = {"1D", "7D", "15D", "1M", "3M", "6M", "1A", "all"}
    allowed_timeframes = {"1Min", "5Min", "15Min", "1H", "1D"}
    if period not in allowed_periods:
        return jsonify({"ok": False, "error": f"Unsupported period: {period}"}), 400
    if timeframe not in allowed_timeframes:
        return jsonify({"ok": False, "error": f"Unsupported timeframe: {timeframe}"}), 400
    try:
        return jsonify(_coordinator.backtest.run(period=period, timeframe=timeframe))
    except Exception as e:
        log.warning("backtest error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/model/learn", methods=["POST"])
@require_api_key
def api_lab_model_learn():
    body = request.get_json(silent=True) or {}
    apply_change = bool(body.get("apply") or False)
    period = str(body.get("period") or "3M")
    timeframe = str(body.get("timeframe") or "1D")
    try:
        backtest = _coordinator.backtest.run(period=period, timeframe=timeframe)
        result = _coordinator.model.learn(backtest, apply=apply_change)
        _overview_cache["data"] = None
        return jsonify({
            "ok": True,
            "backtest": backtest,
            "learning": result,
        })
    except Exception as e:
        log.warning("model learn error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/orders/preview", methods=["GET", "POST"])
@require_api_key
def api_lab_orders_preview():
    try:
        body         = request.get_json(silent=True) or {}
        plan_type    = str(body.get("plan_type") or "build").lower()
        custom_orders = body.get("orders")

        scores = _coordinator.market_news.live_scores(force=False)
        snap = _coordinator.portfolio.snapshot(live_scores=scores)
        preview = _coordinator.trading.preview(plan_type, custom_orders, snap, scores)

        return jsonify({
            "ok":         True,
            "plan_type":  plan_type,
            "orders":     preview["orders"],
            "account":    snap.get("account"),
            "regime":     (scores or {}).get("regime"),
            "scored_at":  (scores or {}).get("scored_at"),
            "risk_gate":  preview["risk_gate"],
            "decision":   preview["decision"],
        })
    except Exception as e:
        log.warning("preview error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/orders/place", methods=["POST"])
@require_api_key
def api_lab_orders_place():
    if not config.fund_manager_order_submission_enabled():
        return jsonify({
            "ok":    False,
            "error": "Order submission disabled. Set FUND_MANAGER_ORDER_SUBMISSION_ENABLED=true.",
        }), 403

    body   = request.get_json(silent=True) or {}
    orders = body.get("orders") or []
    if not orders:
        return jsonify({"ok": False, "error": "No orders provided."}), 400

    scores = _coordinator.market_news.live_scores(force=False)
    snap = _coordinator.portfolio.snapshot(live_scores=scores)
    result = _coordinator.trading.place(orders, snap)

    submitted = result.get("submitted") or []
    errors = result.get("errors") or []
    if submitted:
        _append_event("paper_orders_submitted", {
            "submitted": submitted,
            "errors": errors,
            "risk_gate": result.get("risk_gate") or {},
        })
    elif errors and (result.get("risk_gate") or {}).get("ok"):
        _append_event("paper_orders_failed", {
            "errors": errors,
            "risk_gate": result.get("risk_gate") or {},
        })
    _overview_cache["data"] = None   # invalidate overview cache

    return jsonify(result)


@app.route("/api/lab/news")
def api_lab_news():
    """Return news articles for watchlist symbols and held positions."""
    try:
        client    = get_client()
        positions = client.get_positions()
        held_syms = list(positions.keys())

        # Fetch watchlist news (uses cache)
        watchlist_syms = list({s.upper() for s in sg.ALL_SYMBOLS})
        news_map = _get_news_for_symbols(watchlist_syms + held_syms)

        def _tone(article: dict) -> str:
            delta, _ = _news_score_delta([article])
            if delta <= -8:   return "negative"
            if delta >= 5:    return "positive"
            return "neutral"

        def _enrich(articles: list[dict]) -> list[dict]:
            out = []
            for a in articles:
                out.append({**a, "tone": _tone(a)})
            return out

        # All articles flat (for ticker) — deduplicated by id
        seen_ids: set = set()
        all_articles = []
        for arts in news_map.values():
            for a in arts:
                aid = str(a.get("id",""))
                if aid not in seen_ids:
                    seen_ids.add(aid)
                    all_articles.append({**a, "tone": _tone(a)})
        all_articles.sort(key=lambda a: str(a.get("created_at") or ""), reverse=True)

        # Watchlist articles
        watchlist_articles = []
        for sym in watchlist_syms:
            for a in news_map.get(sym, []):
                aid = str(a.get("id",""))
                watchlist_articles.append({**a, "tone": _tone(a)})
        watchlist_articles.sort(key=lambda a: str(a.get("created_at") or ""), reverse=True)
        # deduplicate
        seen = set()
        wl_deduped = []
        for a in watchlist_articles:
            if a.get("id") not in seen:
                seen.add(a.get("id"))
                wl_deduped.append(a)

        # Held-position articles
        held_articles = []
        for sym in held_syms:
            for a in news_map.get(sym, []):
                held_articles.append({**a, "tone": _tone(a)})
        held_articles.sort(key=lambda a: str(a.get("created_at") or ""), reverse=True)
        seen2 = set()
        held_deduped = []
        for a in held_articles:
            if a.get("id") not in seen2:
                seen2.add(a.get("id"))
                held_deduped.append(a)

        return jsonify({
            "ok":                 True,
            "all_articles":       all_articles[:60],
            "watchlist_articles": wl_deduped[:40],
            "held_articles":      held_deduped[:20],
        })
    except Exception as e:
        log.warning("news error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/live-scores/trigger", methods=["POST"])
@require_api_key
def api_lab_live_scores_trigger():
    """Run full signal scoring pass (quotes + news) in the background."""
    def _run():
        _run_live_scores()
        _overview_cache["data"] = None

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Signal scoring started."})


@app.route("/api/lab/chat/message", methods=["POST"])
@require_api_key
def api_lab_chat_message():
    """Gemini-powered conversational endpoint.

    Request:  {"text": "user message"}
    Response: {"ok": true, "reply": "..."} or {"ok": true, "trade_proposal": {...}}
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No text provided."}), 400

    # ── Snapshot current state — read from the shared overview cache ─────────────
    # The overview cache is always fresh (TTL 30s, polled by the UI every 30s).
    # Avoids duplicate API calls and ensures the chat sees the same data the
    # sidebar shows. If cache is empty, trigger a fresh fetch.
    cache_snap = _overview_cache.get("data")
    if not cache_snap:
        try:
            # Force a fresh fetch by calling the singleton client directly
            _client_inst = get_client()
            _positions   = _client_inst.get_positions()
            _open_orders = _client_inst.get_open_orders()
            _recent      = _client_inst.get_recent_orders(limit=50)
            _account     = _client_inst.get_account()
            _accounting  = _build_accounting(_positions, _open_orders, _recent, _account, None)
            cache_snap = {
                "account":    _account,
                "accounting": _accounting,
                "live_scores": _live_scores_cache.get("data"),
            }
        except Exception as e:
            log.warning("chat context fallback fetch failed: %s", e)
            cache_snap = {}

    acct_snap   = cache_snap.get("account")    or {}
    acctg_snap  = cache_snap.get("accounting") or {}
    scores_snap = (cache_snap.get("live_scores") or {})

    # account fields may be strings (Alpaca returns strings for monetary values)
    def _flt(d, key, default=0.0):
        v = d.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    equity    = _flt(acct_snap, "equity")
    last_eq   = _flt(acct_snap, "last_equity", equity)
    cash      = _flt(acct_snap, "cash")
    daily_pnl = equity - last_eq

    # Positions come from accounting["positions"] — all broker positions regardless of order status
    raw_positions = acctg_snap.get("positions") or []
    positions     = raw_positions   # include pending-order positions too
    pos_lines     = []
    for p in positions:
        sym = p.get("symbol", "?")
        qty = p.get("qty") or "?"
        pnl = float(p.get("unrealized_pnl") or 0)
        val = float(p.get("current_value") or 0)
        pos_lines.append(f"{sym} qty={qty} val=${val:,.0f} P&L=${pnl:+.0f}")

    regime      = scores_snap.get("regime", "UNKNOWN")
    top_signals = scores_snap.get("top") or scores_snap.get("top_signals") or []
    sig_lines   = [
        f"{s.get('symbol')} score={s.get('score')} ({s.get('action','?')})"
        for s in top_signals[:5]
    ]

    # ── Intent: "exit all" ────────────────────────────────────────────────────
    text_low = text.lower()
    exit_keywords = {"exit all", "close all", "sell everything", "flatten", "get out", "liquidate"}
    if any(k in text_low for k in exit_keywords):
        if not positions:
            return jsonify({"ok": True, "reply": "No open positions to close."})
        orders = [
            {"symbol": p["symbol"],
             "qty": str(p.get("qty") or 1),
             "side": "sell",
             "estimated_value": float(p.get("current_value") or 0)}
            for p in positions
        ]
        return jsonify({
            "ok": True,
            "trade_proposal": {
                "orders": orders,
                "context": f"Close all {len(orders)} position(s) — paper trade only"
            }
        })

    # ── Build system + user prompt ────────────────────────────────────────────
    system_prompt = (
        "You are an agentic day-trading assistant for an Alpaca paper trading account. "
        "Speak concisely in 1–3 sentences. Format numbers with $ and commas. "
        "Never recommend real trades — everything here is paper/simulated. "
        "Do not create order JSON. Do not directly decide position size. "
        "Explain current portfolio, risk, signals, news, and agent decisions from the provided state."
    )

    context_block = (
        f"Account: equity=${equity:,.0f}, cash=${cash:,.0f}, daily P&L=${daily_pnl:+,.0f}\n"
        f"Regime: {regime}\n"
        f"Open positions: {', '.join(pos_lines) if pos_lines else 'none'}\n"
        f"Top signals: {', '.join(sig_lines) if sig_lines else 'none'}\n"
    )

    # ── Try Gemini ────────────────────────────────────────────────────────────
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        try:
            import google.generativeai as genai  # type: ignore
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel(
                model_name="gemini-2.0-flash",
                system_instruction=system_prompt,
            )
            full_user = f"{context_block}\nUser: {text}"
            response  = model.generate_content(full_user)
            raw = (response.text or "").strip()

            return jsonify({"ok": True, "reply": raw})
        except Exception as e:
            log.warning("Gemini chat error: %s", e)
            # fall through to simple fallback

    # ── Simple keyword fallback ───────────────────────────────────────────────
    reply = _simple_chat_response(text_low, equity, daily_pnl, cash, regime, pos_lines, sig_lines)
    return jsonify({"ok": True, "reply": reply})


def _simple_chat_response(text, equity, daily_pnl, cash, regime, pos_lines, sig_lines):
    if any(k in text for k in ("p&l", "pnl", "profit", "loss", "how am i doing", "performance")):
        sign = "+" if daily_pnl >= 0 else ""
        return (f"Equity ${equity:,.0f}, daily P&L {sign}${daily_pnl:,.0f}. "
                f"Cash available: ${cash:,.0f}.")

    if any(k in text for k in ("position", "holding", "open")):
        if pos_lines:
            return "Open positions: " + "; ".join(pos_lines) + "."
        return "No open positions. All cash."

    if any(k in text for k in ("signal", "scan", "score", "what to buy", "entry")):
        if sig_lines:
            return "Top signals: " + "; ".join(sig_lines) + "."
        return "No signals cached yet. Trigger a scan via the button or wait for the next run."

    if any(k in text for k in ("regime", "market", "bull", "bear", "choppy")):
        desc = {
            "BULL":   "bullish — momentum strategies are eligible.",
            "BEAR":   "bearish — short/inverse ETFs eligible.",
            "CHOPPY": "choppy — no new entries, holding cash.",
        }.get(regime, "unknown.")
        return f"Current regime is {regime} — {desc}"

    if any(k in text for k in ("cash", "buying power", "available")):
        return f"Cash available: ${cash:,.0f}."

    return (f"Equity ${equity:,.0f} | P&L ${daily_pnl:+,.0f} | Regime {regime} | "
            f"{'No positions' if not pos_lines else str(len(pos_lines)) + ' positions open'}. "
            "Ask me about positions, signals, P&L, or regime.")


@app.route("/lab")
def lab():
    if DASHBOARD_PATH.exists():
        return send_from_directory(str(DASHBOARD_PATH.parent), DASHBOARD_PATH.name)
    return "<h2>Dashboard not found. Copy portfolio_lab.html to the robinhood-trader directory.</h2>", 404


@app.route("/")
def root():
    return lab()


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os as _os
    _host = _os.environ.get("FLASK_HOST", "127.0.0.1")
    _port = int(_os.environ.get("FLASK_PORT", "5001"))
    app.run(host=_host, port=_port, debug=False)
