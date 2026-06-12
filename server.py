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

import backtest as bt
import config
import notify
import signals as sg
import strategy_model
import trade_ledger
import trader
from alpaca_client import AlpacaClient
from logger import get_logger

log = get_logger("server")
app = Flask(__name__)
CORS(app)

# ── Trading control state (#43, #44) ─────────────────────────────────────────
_TRADER_STATE_PATH = strategy_model.STATE_DIR / "trader_control.json"

def _load_trader_control() -> dict:
    """Return current trading control flags. Defaults: paused=False, paper=True."""
    try:
        if _TRADER_STATE_PATH.exists():
            return json.loads(_TRADER_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"paused": False, "paper": True, "updated_at": None, "updated_by": "default"}

def _save_trader_control(state: dict) -> None:
    try:
        strategy_model.STATE_DIR.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _TRADER_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("save trader_control failed: %s", e)


def _auto_entry_gate() -> dict:
    """Mirror trader.py entry-only gates for honest dashboard/chat explanations."""
    ctrl = _load_trader_control()
    if ctrl.get("paused"):
        return {
            "blocked": True,
            "reason": "auto_paused",
            "summary": "Auto-trading is paused.",
        }

    now = trader._now_et()
    if not trader.is_market_open():
        if trader.is_eod():
            return {
                "blocked": True,
                "reason": "eod_flat",
                "summary": "Auto entries are blocked because the system is in the EOD flatten window.",
            }
        return {
            "blocked": True,
            "reason": "market_closed",
            "summary": "Auto entries are blocked because the market is closed.",
        }

    mins = now.hour * 60 + now.minute
    open_mins = trader.MARKET_OPEN_HOUR * 60 + trader.MARKET_OPEN_MIN
    eod_mins = trader.EOD_FLAT_HOUR * 60 + trader.EOD_FLAT_MIN
    open_until = open_mins + trader.NO_ENTRY_OPEN_MINS
    eod_block_start = eod_mins - trader.NO_ENTRY_EOD_MINS

    def _hhmm(total_mins: int) -> str:
        hour = total_mins // 60
        minute = total_mins % 60
        suffix = "AM" if hour < 12 else "PM"
        hour12 = hour % 12 or 12
        return f"{hour12}:{minute:02d} {suffix} ET"

    if mins < open_until:
        return {
            "blocked": True,
            "reason": "opening_no_entry_window",
            "next_eligible": _hhmm(open_until),
            "summary": (
                "Auto entries are blocked during the first "
                f"{trader.NO_ENTRY_OPEN_MINS} minutes after the open. "
                f"Next eligible auto-trading tick is after {_hhmm(open_until)}."
            ),
        }
    if mins >= eod_block_start:
        return {
            "blocked": True,
            "reason": "pre_eod_no_entry_window",
            "summary": (
                "Auto entries are blocked in the pre-EOD window so the system "
                f"can flatten before {_hhmm(eod_mins)}."
            ),
        }
    return {
        "blocked": False,
        "reason": "eligible",
        "summary": "Auto entries are eligible on the next trader tick.",
    }


def _chat_order_gate(
    qualifying_signals: list[dict],
    positions: list[dict],
    acct_snap: dict,
    acctg_snap: dict,
    scores_snap: dict,
) -> dict:
    """Return the risk gate that would apply to chat-described signal entries."""
    if not qualifying_signals:
        return {"ok": True, "errors": [], "warnings": []}

    model = strategy_model.load_model()
    equity = float((acct_snap or {}).get("equity") or 0)
    cash = float((acct_snap or {}).get("cash") or 0)
    regime = str((scores_snap or {}).get("regime") or "").upper()
    regime_mult = 0.5 if regime in ("CHOPPY", "CHOP") else 1.0
    alloc = equity * float(model.get("position_size_pct", 0.05)) * regime_mult

    orders = []
    deployed = 0.0
    for sig in qualifying_signals:
        price = float((sig.get("quote") or {}).get("price") or 0)
        if price <= 0:
            continue
        qty = max(1, int(alloc / price))
        cost = qty * price
        if cash > 0 and deployed + cost > cash * 0.95:
            break
        deployed += cost
        orders.append({
            "symbol": str(sig.get("symbol") or "").upper(),
            "side": "buy",
            "qty": qty,
            "estimated_value": round(cost, 2),
        })

    broker_positions = {
        str(p.get("symbol") or "").upper(): p
        for p in (positions or [])
        if str(p.get("symbol") or "").strip()
    }
    snapshot = {
        "account": acct_snap,
        "accounting": acctg_snap,
        "clock": (acctg_snap or {}).get("clock") or {},
        "positions": broker_positions,
    }
    try:
        snapshot["clock"] = get_client().get_clock()
    except Exception:
        pass
    return RiskAgent().order_gate(orders, snapshot)


# ── Push notification device token store (#41) ────────────────────────────────
_PUSH_TOKENS_PATH = strategy_model.STATE_DIR / "push_tokens.json"

def _load_push_tokens() -> list[str]:
    try:
        if _PUSH_TOKENS_PATH.exists():
            return json.loads(_PUSH_TOKENS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []

def _save_push_tokens(tokens: list[str]) -> None:
    try:
        strategy_model.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _PUSH_TOKENS_PATH.write_text(json.dumps(list(set(tokens))), encoding="utf-8")
    except Exception as e:
        log.warning("save push tokens failed: %s", e)

def send_push_notification(title: str, body: str, data: dict | None = None) -> int:
    """Send APNs push to all registered devices. Returns count of attempted sends.

    Uses HTTP/2 APNs provider API via the 'httpx' library if available.
    Falls back silently if APNs is not configured.
    """
    apns_key   = os.environ.get("APNS_AUTH_KEY", "")
    apns_key_id = os.environ.get("APNS_KEY_ID", "")
    apns_team  = os.environ.get("APNS_TEAM_ID", "")
    bundle_id  = os.environ.get("APNS_BUNDLE_ID", "com.johnshelest.AlpacaAgent")
    tokens     = _load_push_tokens()
    if not tokens or not all([apns_key, apns_key_id, apns_team]):
        return 0   # not configured — silent no-op
    try:
        import jwt as _jwt, time as _time, httpx as _httpx
        issued = int(_time.time())
        token = _jwt.encode(
            {"iss": apns_team, "iat": issued},
            apns_key,
            algorithm="ES256",
            headers={"kid": apns_key_id},
        )
        headers = {
            "authorization": f"bearer {token}",
            "apns-topic": bundle_id,
            "apns-push-type": "alert",
        }
        payload = {
            "aps": {"alert": {"title": title, "body": body}, "sound": "default"},
            **(data or {}),
        }
        sent = 0
        apns_host = "https://api.push.apple.com"
        with _httpx.Client(http2=True, timeout=10) as client:
            for device_token in tokens:
                url = f"{apns_host}/3/device/{device_token}"
                r = client.post(url, json=payload, headers=headers)
                if r.status_code == 200:
                    sent += 1
                else:
                    log.warning("APNs rejected token %s…: %s %s",
                                device_token[:8], r.status_code, r.text)
        return sent
    except ImportError:
        log.debug("APNs push skipped — jwt/httpx not installed")
        return 0
    except Exception as e:
        log.warning("APNs push failed: %s", e)
        return 0

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
LAB_EVENT_STATE_PATH  = STATE_DIR / "lab_event_state.json"
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
_OVERVIEW_TTL      = 30
_LIVE_SCORE_TTL    = 300
_NEWS_TTL          = 600
_live_scores_lock  = threading.Lock()   # prevent overlapping score runs

# ── Background live-score refresh loop ────────────────────────────────────────
def _live_score_refresh_loop() -> None:
    """Auto-refresh live scores every 10 min during market hours, hourly otherwise.
    Runs as a daemon thread started at server startup.
    """
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
    MARKET_INTERVAL = 10 * 60   # 10 min during market hours

    def _market_open() -> bool:
        now = datetime.now(_ET)
        if now.weekday() >= 5:
            return False
        mins = now.hour * 60 + now.minute
        return 570 <= mins < 960   # 9:30 AM – 4:00 PM ET

    # Stagger first run by 15 s to let the server finish startup
    time.sleep(15)
    log.info("live-score refresh loop started")
    while True:
        if _market_open():
            try:
                if _live_scores_lock.acquire(blocking=False):
                    try:
                        _run_live_scores()
                    finally:
                        _live_scores_lock.release()
            except Exception as e:
                log.warning("live-score refresh loop error: %s", e)
            time.sleep(MARKET_INTERVAL)
        else:
            time.sleep(60)   # check every minute until market opens

threading.Thread(target=_live_score_refresh_loop, daemon=True, name="live-score-refresh").start()

# ── Portfolio event detection state (persisted across pod restarts) ───────────
def _load_event_state() -> dict:
    """Load event state from disk; returns empty state on first run or error."""
    try:
        raw = json.loads(LAB_EVENT_STATE_PATH.read_text(encoding="utf-8"))
        raw["exit_symbols"] = set(raw.get("exit_symbols") or [])
        raw["news_symbols"] = set(raw.get("news_symbols") or [])
        return raw
    except Exception:
        return {"regime": "", "exit_symbols": set(), "news_symbols": set(), "risk_action": "", "last_regime_event_ts": None}

def _save_event_state(state: dict) -> None:
    try:
        serializable = {
            **{k: v for k, v in state.items() if k not in ("exit_symbols", "news_symbols")},
            "exit_symbols": list(state.get("exit_symbols") or []),
            "news_symbols": list(state.get("news_symbols") or []),
        }
        LAB_EVENT_STATE_PATH.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save event state: %s", e)

_last_event_state: dict = _load_event_state()

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

_EVENT_MAX_AGE_DAYS = 7  # events older than this are silently dropped

def _load_events(limit: int = 50) -> list[dict]:
    if not LAB_EVENTS_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=_EVENT_MAX_AGE_DAYS)
    events: list[dict] = []
    try:
        with LAB_EVENTS_PATH.open(encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                    # Drop events older than max-age
                    ts_str = ev.get("ts") or ev.get("time") or ""
                    if ts_str:
                        try:
                            ev_ts = datetime.fromisoformat(ts_str)
                            if ev_ts < cutoff:
                                continue
                        except Exception:
                            pass
                    events.append(ev)
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
    _model   = strategy_model.load_model()
    _target_wt_pct = float(_model.get("position_size_pct", 0.05)) * 100
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
            "target_value":      round(equity * float(_model.get("position_size_pct", 0.05)), 2),
            "target_weight_pct": round(_target_wt_pct, 2),
            "current_weight_pct": round(cur_wt, 2),
            "drift_pct":         round(cur_wt - _target_wt_pct, 2),
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


def _signal_exit_reason(sym: str, live_scores: dict | None, held_score_map: dict) -> str | None:
    """Return an exit reason based on technical signal deterioration, or None."""
    # Use pre-scored held positions (from _score_held_positions called by overview)
    entry = held_score_map.get(sym.upper()) or {}
    score = int(float(entry.get("score") or 0))
    breakdown = entry.get("signal_breakdown") or {}
    technicals = entry.get("technicals") or {}

    # If score has collapsed to bearish territory, signal deterioration
    if score > 0 and score < 35:
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

    # RSI overbought (>78): likely exhaustion, consider taking profits
    rsi = float(technicals.get("rsi14") or 0)
    if rsi > 78:
        return "rsi_overbought"

    # MACD histogram turned negative: momentum reversal
    macd_hist = float(technicals.get("macd_hist") or 0)
    if macd_hist < -0.05:
        return "macd_bearish_cross"

    # EMA death cross (EMA9 < EMA21): short-term trend against position
    if breakdown.get("ema", {}).get("status") == "bearish":
        # Only flag if MACD also not bullish (two indicators agree)
        if breakdown.get("macd", {}).get("status") != "bullish":
            return "ema_death_cross"

    # Price fallen back below AVWAP: volume-weighted support lost
    if breakdown.get("avwap", {}).get("status") == "bearish":
        # Only flag if trend also bearish (not just a brief dip)
        if breakdown.get("trend", {}).get("status") == "bearish":
            return "avwap_breakdown"

    return None


def _exit_recommendations(positions: dict, accounting: dict, live_scores: dict | None, model: dict,
                           held_score_map: dict | None = None) -> list[dict]:
    memory = strategy_model.update_position_memory(positions)
    regime = (live_scores or {}).get("regime") or "UNKNOWN"
    rows = accounting.get("positions") or []
    recommendations: list[dict] = []
    _held = held_score_map or {}

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
        elif _held:
            # Technical signal checks (only run when we have fresh scores)
            reason = _signal_exit_reason(sym, live_scores, _held)

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
    """CHOPPY no longer forces exits — only directional flips do."""
    symbol = symbol.upper()
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
    1. Alpaca snapshots and bars for all watchlist symbols
    2. Alpaca news for each candidate
    3. News delta applied on top of pure-Python score
    """
    try:
        _cl      = get_client()
        quotes   = trader.fetch_quotes(sg.ALL_SYMBOLS, client=_cl)
        quotes   = sg.enrich_quotes_with_indicators(quotes, sg.ALL_SYMBOLS, alpaca_client=_cl)
        regime   = sg.detect_regime(quotes)
        news_map = _get_news_for_symbols(sg.ALL_SYMBOLS)
        universe = sg.BULL_UNIVERSE if regime in ("BULL", "CHOPPY") else sg.BEAR_UNIVERSE
        signals  = []

        for sym in universe:
            base_score, breakdown = sg.score_symbol(sym, quotes, regime)
            if base_score < 50:
                continue

            # Plain-English technical reasons from per-indicator breakdown
            technical_reasons = [bd["label"] for bd in breakdown.values() if bd.get("label")]

            # Gemini sentiment boost — cached, fires for borderline scores (#34)
            if 40 <= base_score <= 72:
                base_score += sg.gemini_sentiment_boost_cached(sym, base_score)

            # Alpaca news delta
            articles = news_map.get(sym, [])
            news_delta, news_terms = _news_score_delta(articles)
            final_score = max(0, min(100, base_score + news_delta))

            signals.append({
                "symbol":            sym,
                "score":             final_score,
                "base_score":        base_score,
                "news_delta":        news_delta,
                "news_terms":        news_terms,
                "regime":            regime,
                "side":              "buy",
                "quote":             quotes.get(sym, {}),
                "technicals":        (quotes.get(sym, {}) or {}).get("technicals") or {},
                "technical_reasons": technical_reasons,
                "signal_breakdown":  breakdown,
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


# ── Score held positions (always-on, even when market is closed) ───────────────
def _score_held_positions(held_symbols: list[str], regime: str) -> list[dict]:
    """Score currently held symbols using the same 6-signal engine as _run_live_scores.

    Returns entries in the same shape as live_scores['top'] so the iOS app's
    parseSignalInsights() can consume them without changes.
    """
    if not held_symbols:
        return []
    try:
        _cl    = get_client()
        quotes = trader.fetch_quotes(held_symbols, client=_cl)
        quotes = sg.enrich_quotes_with_indicators(quotes, held_symbols, alpaca_client=_cl)
        scored = []
        for sym in held_symbols:
            try:
                score, breakdown = sg.score_symbol(sym, quotes, regime)
                technical_reasons = [
                    bd["label"] for bd in breakdown.values() if bd.get("label")
                ]
                q = quotes.get(sym) or {}
                scored.append({
                    "symbol":            sym,
                    "score":             score,
                    "base_score":        score,
                    "news_delta":        0,
                    "news_terms":        [],
                    "regime":            regime,
                    "side":              "hold",
                    "quote":             q,
                    "technicals":        (q.get("technicals") or {}),
                    "technical_reasons": technical_reasons,
                    "signal_breakdown":  breakdown,
                    "news":              [],
                })
            except Exception as _se:
                log.debug("held-position score failed for %s: %s", sym, _se)
        return scored
    except Exception as e:
        log.warning("_score_held_positions failed: %s", e)
        return []


# ── Plain-English order reason ─────────────────────────────────────────────────
def _plain_entry_reason(sig: dict) -> str:
    """Convert a signal dict into a human-readable entry reason."""
    regime    = str(sig.get("regime") or "").upper()
    score     = int(float(sig.get("score") or 0))
    news_delta = int(sig.get("news_delta") or 0)
    symbol    = str(sig.get("symbol") or "")

    strength = "strong" if score >= 85 else "good" if score >= 70 else "moderate"

    regime_phrase = {
        "BULL": "market trending up",
        "BEAR": "hedging against a down market",
        "CHOP": "range-bound market",
        "CHOPPY": "range-bound market",
    }.get(regime, "current market conditions")

    reason = f"{strength.capitalize()} momentum signal — {regime_phrase}"

    if news_delta >= 5:
        reason += ", positive news tailwind"
    elif news_delta <= -5:
        reason += ", but watch the recent headlines"

    return reason


def _plain_exit_reason(rec: dict) -> str:
    """Convert an exit recommendation into a human-readable reason."""
    raw    = str(rec.get("reason") or "").lower()
    sym    = str(rec.get("symbol") or "")
    pnl    = float(rec.get("unrealized_pnl_pct") or 0)
    days   = int(rec.get("holding_days") or 0)
    pnl_str = f"{pnl:+.1f}%"

    if "loss_stop" in raw:
        return f"Down {abs(pnl):.1f}% — cut the loss"
    if "giveback" in raw:
        return f"Gave back gains, now {pnl_str} — lock in what's left"
    if "max_hold" in raw:
        return f"Held {days}d without moving — free up the capital"
    if "regime_flip" in raw or "bear" in raw:
        return f"Market turned against this position ({pnl_str})"
    if "rsi_overbought" in raw:
        return f"RSI overbought — momentum exhausted ({pnl_str})"
    if "macd_bearish" in raw:
        return f"MACD turned negative — momentum reversing ({pnl_str})"
    if "ema_death" in raw:
        return f"EMA crossover bearish — short-term trend flipped ({pnl_str})"
    if "avwap_breakdown" in raw:
        return f"Price broke below AVWAP + trend bearish ({pnl_str})"
    if "bearish_reversal_candle" in raw:
        return f"Bearish reversal candle confirmed ({pnl_str})"
    if "price_action_breakdown" in raw:
        return f"Price action broke support / failed breakout ({pnl_str})"
    if "price_action_bearish" in raw:
        return f"Price action turned bearish ({pnl_str})"
    if "signal_deterioration" in raw:
        return f"Signal score collapsed — conviction lost ({pnl_str})"
    if "news" in raw:
        return f"Bad news hit this stock ({pnl_str}) — exit to be safe"
    return f"Exit signal triggered ({pnl_str})"


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
            "reason":          _plain_entry_reason(sig),
            "score":           sig["score"],
            "news":            (sig.get("news") or [])[:2],
            "model_generation": model.get("generation"),
        })

    return orders


# ── Agent runtime ─────────────────────────────────────────────────────────────
class PortfolioAgent:
    """Owns broker state, accounting, and portfolio history."""

    def snapshot(self, live_scores: dict | None = None, recent_limit: int = 50,
                 held_score_map: dict | None = None) -> dict:
        client      = get_client()
        account     = client.get_account()
        positions   = client.get_positions()
        open_orders = client.get_open_orders()
        recent      = client.get_recent_orders(limit=recent_limit)
        accounting  = _build_accounting(positions, open_orders, recent, account, live_scores)
        model = strategy_model.load_model()
        exit_recs = _exit_recommendations(positions, accounting, live_scores, model, held_score_map)
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
            if sym.upper() in _BEAR_ETFS:
                continue  # Bearish news is GOOD for inverse/vol ETFs — don't flag as risk
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
        """Delegate to backtest.py run_variants for variant-aware optimization."""
        return bt.run_variants(period=period, base_model=model)

    def run_variants(self, period: str = "1M") -> dict:
        """Score all VARIANT_PROFILES over historical data and return ranked results."""
        model = strategy_model.load_model()
        return bt.run_variants(period=period, base_model=model)

    def _load_daily_replay_data(self, period: str) -> list[dict]:
        return bt.load_daily_replay_data(period)

    def _score_candidate(self, model: dict, rows: list[dict]) -> dict:
        return bt.score_candidate(model, rows)

    def _simulated_exit_price(self, symbol: str, rows: list[dict], entry_row: dict, entry: float, model: dict) -> float:
        return bt.simulated_exit_price(symbol, rows, entry_row, entry, model)


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
        if live_scores is None:
            threading.Thread(target=_run_live_scores, daemon=True).start()
            live_scores = _live_scores_cache.get("data")
        # Score held positions FIRST so exit recommendations can use signal data
        regime_str = str((live_scores or {}).get("regime") or "CHOPPY").upper()
        # We need the position list — do a quick positions fetch first
        try:
            _pos_for_scoring = get_client().get_positions()
            held_symbols = list((_pos_for_scoring or {}).keys())
        except Exception:
            held_symbols = []
        held_scores = _score_held_positions(held_symbols, regime_str)
        held_score_map = {s["symbol"]: s for s in held_scores}
        snap = self.portfolio.snapshot(live_scores=live_scores, held_score_map=held_score_map)
        accounting = snap.get("accounting") or {}
        news_risk = self.market_news.position_risk(accounting)
        decision = _decision_from_state(accounting, live_scores, news_risk)
        model = self.model.current()
        # Detect and emit portfolio-level events before building narrative
        try:
            _detect_and_emit_portfolio_events(
                regime=str((live_scores or {}).get("regime") or "UNKNOWN").upper(),
                news_risk=news_risk,
                exits=accounting.get("exit_recommendations") or [],
                risk_state=accounting.get("risk_state") or {},
            )
        except Exception as _ev_err:
            log.debug("event detection error: %s", _ev_err)
        narrative = _portfolio_narrative(accounting, live_scores, news_risk, model, decision)
        combined_scores = dict(live_scores or {})
        combined_scores["held_scores"] = held_scores
        return {
            "ok": True,
            "paper": config.PAPER,
            "account": snap.get("account"),
            "clock": snap.get("clock"),
            "accounting": accounting,
            "live_scores": combined_scores,
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
    """Portfolio-level narrative — describes the portfolio as a whole, not individual stocks."""
    regime     = str((live_scores or {}).get("regime") or "UNKNOWN").upper()
    prev_regime = _last_event_state.get("regime", "")
    exits      = accounting.get("exit_recommendations") or []
    positions  = accounting.get("positions") or []
    risk_state = accounting.get("risk_state") or {}
    top        = ((live_scores or {}).get("top") or [])[:5]

    gross_exposure = float(accounting.get("gross_exposure_pct") or 0)
    cash_pct       = max(0.0, 100.0 - gross_exposure)
    open_pnl       = float(accounting.get("broker_pnl") or accounting.get("unrealized_pnl") or 0)
    pos_count      = len(positions)
    winners        = sum(1 for p in positions if float(p.get("unrealized_pnl") or 0) > 0)
    losers         = sum(1 for p in positions if float(p.get("unrealized_pnl") or 0) < 0)

    top_scores     = [int(float(s.get("score") or 0)) for s in top if s.get("score")]
    avg_score      = sum(top_scores) // len(top_scores) if top_scores else 0
    min_conviction = int(model.get("min_conviction") or 75)

    sentiment_from = prev_regime.lower() if prev_regime and prev_regime != regime else "market"
    sentiment_to   = regime.lower() if regime != "UNKNOWN" else "unclear"

    # Plain-English regime description
    regime_desc = {
        "BULL":    "trending up",
        "BEAR":    "trending down",
        "CHOP":    "choppy — no clear direction",
        "CHOPPY":  "choppy — no clear direction",
        "UNKNOWN": "still loading",
    }.get(regime, "still loading")

    # ── Why — plain English, what's actually happening ────────────────────────
    why: list[str] = []

    if prev_regime and prev_regime != regime:
        old_desc = {"BULL": "trending up", "BEAR": "trending down", "CHOP": "choppy", "CHOPPY": "choppy"}.get(prev_regime, "unclear")
        why.append(f"The market just shifted from {old_desc} to {regime_desc}.")
    elif regime in ("UNKNOWN", ""):
        why.append("Market signals are still refreshing — check back in a moment.")
    else:
        why.append(f"The market is {regime_desc}.")

    if pos_count > 0:
        pnl_word = "up" if open_pnl >= 0 else "down"
        pnl_abs  = abs(open_pnl)
        if losers > 0 and winners > 0:
            why.append(f"You have {winners} position{'s' if winners>1 else ''} making money and {losers} losing — portfolio is {pnl_word} ${pnl_abs:,.0f} overall.")
        elif losers > 0:
            why.append(f"All {losers} open position{'s are' if losers>1 else ' is'} losing money. Portfolio is down ${pnl_abs:,.0f}.")
        else:
            why.append(f"All {winners} open position{'s are' if winners>1 else ' is'} in the green. Portfolio is up ${pnl_abs:,.0f}.")

    if news_risk:
        syms = ", ".join(nr.get("symbol", "?") for nr in news_risk[:2])
        extra = f" (and {len(news_risk)-2} more)" if len(news_risk) > 2 else ""
        why.append(f"Bad news is hitting {syms}{extra} — those positions need a close look.")

    if risk_state.get("action") == "reduce_risk":
        why.append("The portfolio has taken on too much risk. Time to pull back.")
    elif exits:
        why.append(f"{len(exits)} position{'s are' if len(exits)>1 else ' is'} triggering exit rules — acting on them protects what you've made.")

    if not pos_count:
        if avg_score == 0:
            why.append("You're fully in cash. Signal scan is running — entries appear when conditions are met.")
        elif regime == "BEAR":
            # Bear regime: high scores on bear ETFs are GOOD, not a "bullish" contradiction
            if avg_score >= min_conviction:
                why.append(f"You're fully in cash. Bear ETF signals scoring {avg_score} avg — at threshold for entries.")
            else:
                why.append(f"You're fully in cash. Watching for bear ETF setups (SQQQ, SPXS, UVXY) above {min_conviction}.")
        else:
            bias = "looking bullish" if avg_score >= 60 else "looking bearish" if avg_score <= 40 else "mixed"
            why.append(f"You're fully in cash. Market signals are {bias}.")

    # ── Next — what to do, in plain English ───────────────────────────────────
    next_actions: list[str] = []

    if risk_state.get("action") == "reduce_risk":
        next_actions.append("Pull back — reduce positions and don't add new ones until conditions improve.")
    elif exits:
        n = len(exits)
        next_actions.append(f"Sell the {n} position{'s' if n>1 else ''} that are triggering exit rules.")

    if not next_actions:
        if regime == "BULL" and cash_pct > 25:
            if avg_score >= min_conviction:
                next_actions.append(f"Market is trending up and you have {cash_pct:.0f}% in cash — good time to look for new entries.")
            else:
                next_actions.append(f"Market is BULL — scanner is looking for high-conviction entries. {cash_pct:.0f}% cash ready to deploy.")
        elif regime == "BEAR" and pos_count > 0:
            next_actions.append("Market is heading down — tighten your stops and consider reducing exposure.")
        elif regime == "BEAR":
            if avg_score >= min_conviction:
                next_actions.append(f"Bear ETFs scoring {avg_score} avg — system queuing SQQQ/SPXS/UVXY entries next tick.")
            else:
                next_actions.append(f"Bear regime. Watching for SQQQ/SPXS/UVXY to score ≥ {min_conviction}.")
        elif regime in ("CHOPPY", "CHOP") and pos_count > 0:
            next_actions.append(f"Market is choppy — system enters at 50% position size when signals score ≥ {min_conviction}.")
        elif regime in ("CHOPPY", "CHOP"):
            next_actions.append(f"Market is choppy — scanner active. System enters at 50% size when conviction ≥ {min_conviction}.")
        else:
            next_actions.append("Waiting for market data before making a call.")

    if news_risk and not any("bad news" in a.lower() or "news" in a.lower() for a in next_actions):
        next_actions.append("Review the news-hit positions before making any moves.")

    # ── Summary — one human sentence ──────────────────────────────────────────
    pnl_word = "up" if open_pnl >= 0 else "down"
    pnl_abs  = abs(open_pnl)
    if pos_count > 0:
        summary = (
            f"{int(cash_pct)}% of your money is in cash, {int(gross_exposure)}% invested across "
            f"{pos_count} position{'s' if pos_count>1 else ''}. You're {pnl_word} ${pnl_abs:,.0f}."
        )
    else:
        summary = f"You're fully in cash. Market is {regime_desc}."

    model_note = "Strategy auto-optimizes daily based on recent trade results."

    return {
        "sentiment_from": sentiment_from,
        "sentiment_to":   sentiment_to,
        "summary":        summary,
        "why":            why[:4],
        "next_actions":   next_actions[:4],
        "model_adjustment": model_note,
    }


def _detect_and_emit_portfolio_events(
    regime: str,
    news_risk: list[dict],
    exits: list[dict],
    risk_state: dict,
) -> None:
    """Emit events to lab_events.jsonl when significant portfolio-level changes occur."""
    global _last_event_state
    prev = _last_event_state

    # Regime change — debounced: suppress if a regime_change event fired within the last 5 min.
    # Prevents log spam from BEAR↔UNKNOWN oscillation on volatile open/close.
    prev_regime = prev.get("regime", "")
    last_regime_event_ts = prev.get("last_regime_event_ts")
    regime_cooldown_secs = 5 * 60  # 5 minutes
    now_ts = datetime.now(timezone.utc).timestamp()
    regime_cooled = (
        last_regime_event_ts is None
        or (now_ts - last_regime_event_ts) >= regime_cooldown_secs
    )
    if prev_regime and prev_regime != regime and regime_cooled:
        _append_event("regime_change", {
            "from": prev_regime,
            "to":   regime,
            "reason": f"Market regime shifted from {prev_regime} to {regime}. Signal universe and weights updated.",
        })
        prev["last_regime_event_ts"] = now_ts

    # New news risk symbols
    news_syms  = {str(n.get("symbol") or "") for n in news_risk}
    prev_news  = set(prev.get("news_symbols") or set())
    new_news   = news_syms - prev_news
    for sym in new_news:
        nr = next((n for n in news_risk if n.get("symbol") == sym), {})
        _append_event("news_risk_detected", {
            "symbol":     sym,
            "delta":      nr.get("delta", 0),
            "headline":   nr.get("headline", ""),
            "is_bear_etf": sym in sg.BEAR_ETF,
            "reason":     f"Negative news detected on {sym} — sentiment delta {nr.get('delta', 0):+d}.",
        })

    # New exit recommendations
    exit_syms  = {str(e.get("symbol") or "") for e in exits}
    prev_exits = set(prev.get("exit_symbols") or set())
    new_exits  = exit_syms - prev_exits
    for sym in new_exits:
        ex = next((e for e in exits if e.get("symbol") == sym), {})
        reason = str(ex.get("reason") or "exit").replace("_", " ")
        pnl    = float(ex.get("unrealized_pnl_pct") or 0)
        _append_event("exit_triggered", {
            "symbol": sym,
            "reason": reason,
            "pnl_pct": pnl,
            "message": f"{sym} flagged for exit: {reason} ({pnl:+.1f}% P&L).",
        })

    # Risk gate state change
    risk_action  = str(risk_state.get("action") or "")
    prev_risk    = prev.get("risk_action", "")
    if risk_action != prev_risk:
        if risk_action == "reduce_risk":
            _append_event("risk_gate_active", {
                "action":  risk_action,
                "message": risk_state.get("message", "Risk limit reached — exposure restricted."),
            })
        elif prev_risk == "reduce_risk":
            _append_event("risk_gate_cleared", {
                "message": "Risk gate cleared — exposure limits normalised.",
            })

    # Update tracked state
    new_state = {
        "regime":       regime,
        "exit_symbols": exit_syms,
        "news_symbols": news_syms,
        "risk_action":  risk_action,
    }
    _last_event_state = new_state
    _save_event_state(new_state)


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
            model_name="gemini-2.5-flash",
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

# ── Global error handler — always return JSON, never HTML ─────────────────────

@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    import traceback as _tb
    tb = _tb.format_exc()
    log.error("Unhandled exception in request %s %s: %s\n%s",
              request.method, request.path, e, tb)
    return jsonify({
        "ok": False,
        "error": str(e),
        "type": type(e).__name__,
    }), 500


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"ok": False, "error": "Not found", "path": request.path}), 404


@app.errorhandler(405)
def handle_405(e):
    return jsonify({"ok": False, "error": "Method not allowed"}), 405


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
        payload["market_open"] = bool(clock.get("is_open"))
        payload["clock"] = clock
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


@app.route("/api/lab/health")
@require_api_key
def api_lab_health():
    """Return system health: trader crash status, consecutive losses, kill-switch state."""
    import strategy_model as _sm
    alerts: list[dict] = []

    # ── Trader health file (written by trader.py on each CronJob run) ─────────
    health_path = _sm.STATE_DIR / "trader_health.json"
    trader_health: dict = {}
    try:
        if health_path.exists():
            trader_health = json.loads(health_path.read_text(encoding="utf-8"))
        if trader_health.get("status") == "error":
            alerts.append({
                "level":   "critical",
                "code":    "trader_crash",
                "message": f"Trader crashed: {trader_health.get('detail', 'unknown error')}",
                "ts":      trader_health.get("timestamp"),
            })
    except Exception:
        pass

    # ── Consecutive losses ────────────────────────────────────────────────────
    consec_path = _sm.STATE_DIR / "consecutive_losses.json"
    consecutive_losses = 0
    try:
        if consec_path.exists():
            consec_data = json.loads(consec_path.read_text(encoding="utf-8"))
            consecutive_losses = int(consec_data.get("count") or 0)
            limit = int(os.environ.get("CONSECUTIVE_LOSS_HALT", 3))
            if consecutive_losses >= limit:
                alerts.append({
                    "level":   "critical",
                    "code":    "consecutive_loss_halt",
                    "message": f"Kill gate active: {consecutive_losses} consecutive losses — no new entries.",
                    "ts":      consec_data.get("last_reset"),
                })
            elif consecutive_losses >= max(1, limit - 1):
                alerts.append({
                    "level":   "warning",
                    "code":    "consecutive_loss_warning",
                    "message": f"Warning: {consecutive_losses} consecutive loss(es) — {limit - consecutive_losses} away from halt.",
                    "ts":      consec_data.get("last_reset"),
                })
    except Exception:
        pass

    # ── Last successful trader run (stale if > 6 hours during market hours) ───
    last_run_ts = trader_health.get("timestamp")
    if last_run_ts:
        try:
            from datetime import datetime, timezone, timedelta
            last_run = datetime.fromisoformat(last_run_ts)
            age_hours = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
            if age_hours > 6:
                alerts.append({
                    "level":   "warning",
                    "code":    "trader_stale",
                    "message": f"Trader last ran {age_hours:.1f}h ago — CronJob may not be firing.",
                    "ts":      last_run_ts,
                })
        except Exception:
            pass

    return jsonify({
        "ok":                True,
        "alerts":            alerts,
        "consecutive_losses": consecutive_losses,
        "trader_health":     trader_health,
    })


@app.route("/api/lab/events", methods=["DELETE"])
@require_api_key
def api_lab_events_clear():
    """Truncate the events log. Safe to call at any time — clears the activity feed."""
    try:
        LAB_EVENTS_PATH.write_text("", encoding="utf-8")
        return jsonify({"ok": True, "message": "Events log cleared."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/activity")
@require_api_key
def api_lab_activity():
    try:
        return jsonify(_coordinator.activity())
    except Exception as e:
        log.warning("activity error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/portfolio/history")
@require_api_key
def api_lab_portfolio_history():
    period    = str(request.args.get("period") or "1M")
    timeframe = str(request.args.get("timeframe") or "1D")
    start     = request.args.get("start")   # ISO date string e.g. 2026-01-15
    end       = request.args.get("end")     # ISO date string e.g. 2026-03-01
    allowed_periods    = {"1D", "7D", "15D", "1M", "3M", "6M", "1A", "all"}
    allowed_timeframes = {"1Min", "5Min", "15Min", "1H", "1D"}

    if not start and period not in allowed_periods:
        return jsonify({"ok": False, "error": f"Unsupported period: {period}"}), 400
    if timeframe not in allowed_timeframes:
        return jsonify({"ok": False, "error": f"Unsupported timeframe: {timeframe}"}), 400

    try:
        if start:
            # Custom date range — pass start/end to Alpaca directly
            history = get_client().get_portfolio_history(
                timeframe=timeframe,
                date_start=start,
                date_end=end or None,
            )
        else:
            history = _coordinator.portfolio.history(period=period, timeframe=timeframe)
        return jsonify({
            "ok": True,
            "period": period if not start else "custom",
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


@app.route("/api/lab/variants")
@require_api_key
def api_lab_variants():
    """Return ranked variant results + win-rate history.

    ?period=1M  — Alpaca lookback period (default 1M)
    ?force=1    — skip today's cache and re-run backtest
    """
    period = str(request.args.get("period") or "1M")
    force = str(request.args.get("force") or "").lower() in {"1", "true", "yes"}
    try:
        history = strategy_model.load_variant_history()
        today = datetime.now(timezone.utc).date().isoformat()
        if not force and today in history:
            return jsonify({
                "ok": True,
                "source": "cached",
                "today": history[today],
                "history": history,
                "win_rates": strategy_model.variant_win_rates(history),
            })
        result = _coordinator.backtest.run_variants(period=period)
        return jsonify({
            "ok": True,
            "source": "live",
            "result": result,
            "history": strategy_model.load_variant_history(),
            "win_rates": strategy_model.variant_win_rates(),
        })
    except Exception as e:
        log.warning("variants error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/ledger")
@require_api_key
def api_lab_ledger():
    """Return recent trades from the durable SQLite ledger + win/loss summary."""
    try:
        limit = min(int(request.args.get("limit") or 200), 1000)
        trades = trade_ledger.recent_trades(limit=limit)
        summary = trade_ledger.win_loss_summary(limit=limit)
        return jsonify({"ok": True, "trades": trades, "summary": summary})
    except Exception as e:
        log.warning("ledger error: %s", e)
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
        entry_gate = _auto_entry_gate()
        decision = preview["decision"]
        if (
            entry_gate.get("blocked")
            and plan_type == "build"
            and preview.get("orders")
            and str(decision.get("action") or "") == "enter"
        ):
            decision = {
                **decision,
                "action": "wait",
                "severity": "warning",
                "summary": entry_gate["summary"],
            }

        return jsonify({
            "ok":         True,
            "plan_type":  plan_type,
            "orders":     preview["orders"],
            "account":    snap.get("account"),
            "regime":     (scores or {}).get("regime"),
            "scored_at":  (scores or {}).get("scored_at"),
            "risk_gate":  preview["risk_gate"],
            "entry_gate": entry_gate,
            "decision":   decision,
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
@require_api_key
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


@app.route("/api/lab/score/<symbol>")
@require_api_key
def api_lab_score_symbol(symbol):
    """On-demand full signal score for any ticker — not limited to the agent's universe.

    Response: {"ok": true, "signal": { same shape as live_scores.top[] }}
    """
    symbol = symbol.upper().strip()
    try:
        _cl    = get_client()
        cached = _live_scores_cache.get("data") or {}
        regime = str(cached.get("regime") or "CHOPPY").upper()
        quotes = trader.fetch_quotes([symbol], client=_cl)
        quotes = sg.enrich_quotes_with_indicators(quotes, [symbol], alpaca_client=_cl)
        score, breakdown = sg.score_symbol(symbol, quotes, regime)
        technical_reasons = [bd["label"] for bd in breakdown.values() if bd.get("label")]
        q    = quotes.get(symbol) or {}
        news = _get_news_for_symbols([symbol]).get(symbol, [])
        return jsonify({
            "ok": True,
            "signal": {
                "symbol":            symbol,
                "score":             score,
                "base_score":        score,
                "news_delta":        0,
                "news_terms":        [],
                "regime":            regime,
                "side":              "buy",
                "quote":             q,
                "technicals":        q.get("technicals") or {},
                "technical_reasons": technical_reasons,
                "signal_breakdown":  breakdown,
                "news":              news[:5],
            },
        })
    except Exception as e:
        log.warning("api_lab_score_symbol(%s) failed: %s", symbol, e)
        return jsonify({"ok": False, "error": str(e)}), 500


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

    cache_snap = _overview_cache.get("data") or {}

    # Match the trading preview path so chat explanations use the same score
    # and risk snapshot as the actual order gate.
    try:
        scores_snap = _coordinator.market_news.live_scores(force=False) or {}
        trade_snap = _coordinator.portfolio.snapshot(live_scores=scores_snap)
    except Exception as e:
        log.warning("chat context fetch failed: %s", e)
        scores_snap = cache_snap.get("live_scores") or {}
        trade_snap = cache_snap

    acct_snap   = trade_snap.get("account")    or {}
    acctg_snap  = trade_snap.get("accounting") or {}

    def _flt(d, key, default=0.0):
        v = d.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    equity    = _flt(acct_snap, "equity")
    # Use adjusted_last_equity (DAWN-neutralised) for display P&L;
    # raw last_equity is used by kill-switch logic elsewhere.
    last_eq   = _flt(acct_snap, "adjusted_last_equity",
                     _flt(acct_snap, "last_equity", equity))
    cash      = _flt(acct_snap, "cash")
    daily_pnl = equity - last_eq

    positions = acctg_snap.get("positions") or []
    pos_lines = []
    for p in positions:
        sym  = p.get("symbol", "?")
        qty  = p.get("qty") or "?"
        pnl  = float(p.get("unrealized_pnl") or 0)
        val  = float(p.get("current_value") or 0)
        days = p.get("holding_days") or ""
        pos_lines.append(f"{sym} qty={qty} val=${val:,.0f} P&L=${pnl:+.0f}{f' {days}d' if days else ''}")

    exit_recs   = acctg_snap.get("exit_recommendations") or []
    exit_lines  = [
        f"{r.get('symbol')} reason={r.get('reason')} pnl_pct={r.get('unrealized_pnl_pct')}%"
        for r in exit_recs
    ]
    risk_state  = acctg_snap.get("risk_state") or {}

    model_state = strategy_model.load_model()
    variant     = model_state.get("active_variant", "current")
    generation  = model_state.get("generation", 0)
    conviction  = model_state.get("min_conviction", 75)
    max_hold    = model_state.get("max_holding_days", 2)

    regime      = scores_snap.get("regime", "UNKNOWN")
    top_signals = scores_snap.get("top") or []

    # Rich signal breakdown for Gemini — include per-indicator status
    sig_lines = []
    for s in top_signals[:8]:
        sym   = s.get("symbol", "?")
        score = s.get("score", 0)
        bd    = s.get("signal_breakdown") or {}
        indicators = []
        for ind_name in ("rsi", "macd", "avwap", "ema", "trend", "price_action"):
            ind = bd.get(ind_name) or {}
            status = ind.get("status", "?")
            label  = ind.get("label", "")
            if label:
                indicators.append(f"{ind_name.upper()}({status}): {label}")
        ind_str = " | ".join(indicators) if indicators else "no breakdown"
        sig_lines.append(f"{sym} score={score}: {ind_str}")

    # Technicals for held positions
    pos_tech_lines = []
    all_scores = {s.get("symbol", "?"): s for s in (scores_snap.get("signals") or scores_snap.get("top") or [])}
    for p in positions:
        sym  = p.get("symbol", "?")
        tech = (all_scores.get(sym) or {}).get("technicals") or {}
        if tech:
            rsi = tech.get("rsi14", "?")
            ema = tech.get("ema_trend", "?")
            macd_h = tech.get("macd_hist", "?")
            avwap  = tech.get("price_vs_avwap_low_pct", "?")
            atr_pct = tech.get("atr_pct", "?")
            pos_tech_lines.append(
                f"{sym}: RSI={rsi} EMA={ema} MACD_hist={macd_h} AVWAP_low%={avwap} ATR%={atr_pct}"
            )

    text_low = text.lower()
    entry_gate = _auto_entry_gate()
    held_symbols = {str(p.get("symbol") or "").upper() for p in positions}
    qualifying_signals = [
        s for s in (scores_snap.get("signals") or scores_snap.get("top") or [])
        if int(float(s.get("score") or 0)) >= int(model_state.get("min_conviction", 75))
        and str(s.get("symbol") or "").upper() not in held_symbols
    ]

    why_not_keywords = {
        "why are you not buying", "why aren't you buying", "why not buying",
        "why no buy", "why no trade", "nothing to trade", "why aren't you trading",
        "why are you not trading", "why not trading",
    }
    if any(k in text_low for k in why_not_keywords):
        if not (scores_snap.get("signals") or scores_snap.get("top")):
            try:
                scores_snap = _coordinator.market_news.live_scores(force=True) or {}
                trade_snap = _coordinator.portfolio.snapshot(live_scores=scores_snap)
                acct_snap = trade_snap.get("account") or {}
                acctg_snap = trade_snap.get("accounting") or {}
                positions = acctg_snap.get("positions") or []
            except Exception as e:
                log.warning("chat forced score refresh failed: %s", e)
        try:
            preview = _coordinator.trading.preview("build", None, trade_snap, scores_snap)
        except Exception as e:
            log.warning("chat preview fetch failed: %s", e)
            preview = {}
        preview_orders = preview.get("orders") or []
        preview_gate = preview.get("risk_gate") or {}

        if entry_gate.get("blocked"):
            top = preview_orders[0] if preview_orders else (qualifying_signals[0] if qualifying_signals else None)
            lead = (
                f"{top.get('symbol')} qualifies at score {int(float(top.get('score') or 0))}, but "
                if top else
                ""
            )
            return jsonify({"ok": True, "reply": lead + entry_gate["summary"]})
        if preview_orders:
            top = preview_orders[0]
            if not preview_gate.get("ok"):
                err = (preview_gate.get("errors") or [{}])[0].get("error") or "Risk gate blocks new buys."
                return jsonify({
                    "ok": True,
                    "reply": (
                        f"{top.get('symbol')} qualifies at score {int(float(top.get('score') or 0))}, "
                        f"but new buys are blocked: {err}"
                    ),
                })
            return jsonify({
                "ok": True,
                "reply": (
                    f"{top.get('symbol')} qualifies at score {int(float(top.get('score') or 0))}. "
                    "Auto entries are eligible on the next trader tick."
                ),
            })
        if qualifying_signals:
            top = qualifying_signals[0]
            order_gate = _chat_order_gate(qualifying_signals, positions, acct_snap, acctg_snap, scores_snap)
            if not order_gate.get("ok"):
                err = (order_gate.get("errors") or [{}])[0].get("error") or "Risk gate blocks new buys."
                return jsonify({
                    "ok": True,
                    "reply": (
                        f"{top.get('symbol')} qualifies at score {int(float(top.get('score') or 0))}, "
                        f"but new buys are blocked: {err}"
                    ),
                })
            return jsonify({
                "ok": True,
                "reply": (
                    f"{top.get('symbol')} qualifies at score {int(float(top.get('score') or 0))}. "
                    "Auto entries are eligible on the next 5-minute trader tick."
                ),
            })
        return jsonify({"ok": True, "reply": "No qualifying signal is currently above the entry threshold."})

    # ── Fast-path: unusual volume ────────────────────────────────────────────
    vol_keywords = {"unusual volume", "high volume", "volume spike", "volume spikes",
                    "what has volume", "volume alert", "top volume", "most volume"}
    if any(k in text_low for k in vol_keywords):
        all_sigs = scores_snap.get("signals") or scores_snap.get("top") or []
        vol_hits = []
        for s in all_sigs:
            tech = (s.get("technicals") or {})
            vr = float(tech.get("volume_ratio") or 0)
            if vr >= 1.5:
                price = float((s.get("quote") or {}).get("price") or 0)
                chg   = float((s.get("quote") or {}).get("change_pct") or 0)
                vol_hits.append((s["symbol"], vr, price, chg, s.get("score", 0)))
        vol_hits.sort(key=lambda x: -x[1])
        if vol_hits:
            lines = [f"{sym}: {vr:.1f}x avg vol, ${price:.2f} ({chg:+.1f}%), score={score}"
                     for sym, vr, price, chg, score in vol_hits[:8]]
            return jsonify({"ok": True, "reply":
                "Unusual volume (≥1.5× average):\n" + "\n".join(lines)})
        return jsonify({"ok": True, "reply":
            "No unusual volume detected in the current signal universe. Run a scan to refresh data."})

    # ── Fast-path: "run live scores / scan" intent ───────────────────────────
    scan_keywords = {"run live scores", "run scores", "do it now", "trigger scan", "trigger scores",
                     "run scan", "scan now", "refresh signals", "run signals", "score now",
                     "start scan", "kick off scan", "force scan", "rescan"}
    if any(k in text_low for k in scan_keywords):
        threading.Thread(target=_run_live_scores, daemon=True).start()
        return jsonify({"ok": True, "reply":
            "Signal scan started. Takes ~30 seconds. Ask me 'what are the top signals?' when done."})

    # ── Fast-path: "exit all" intent ──────────────────────────────────────────
    exit_keywords = {"exit all", "close all", "sell everything", "flatten", "get out", "liquidate"}
    if any(k in text_low for k in exit_keywords):
        if not positions:
            return jsonify({"ok": True, "reply": "No open positions to close."})
        orders = [
            {"symbol": p["symbol"], "qty": str(p.get("qty") or 1),
             "side": "sell", "estimated_value": float(p.get("current_value") or 0)}
            for p in positions
        ]
        return jsonify({"ok": True, "trade_proposal": {
            "orders": orders,
            "summary": f"Close all {len(orders)} position(s) — paper trade only",
        }})

    # ── Fast-path: "place orders" / "enter" / "execute signals" intent ────────
    enter_keywords = {
        "place orders", "place the orders", "place order",
        "enter positions", "enter the positions", "enter trades",
        "execute orders", "execute trades", "execute signals",
        "make the trades", "make the trade", "submit orders",
        "buy signals", "buy the signals", "deploy capital",
        "go ahead", "just do it", "go for it", "do the trades",
    }
    if any(k in text_low for k in enter_keywords):
        cached_signals = (scores_snap.get("signals") or scores_snap.get("top") or [])
        held_syms = {p["symbol"].upper() for p in positions}
        _model = strategy_model.load_model()
        min_conv = int(_model.get("min_conviction", 75))
        size_pct = float(_model.get("position_size_pct", 0.05))
        regime_mult = 0.5 if regime in ("CHOPPY", "CHOP") else 1.0
        orders = []
        deployed = 0.0
        for sig in cached_signals:
            sym = str(sig.get("symbol") or "").upper()
            score = int(float(sig.get("score") or 0))
            if score < min_conv:
                continue
            if sym in held_syms:
                continue
            price = float((sig.get("quote") or {}).get("price") or 0)
            if price <= 0:
                try:
                    price = get_client().get_current_price(sym) or 0
                except Exception:
                    pass
            if price <= 0:
                continue
            alloc = equity * size_pct * regime_mult
            qty = max(1, int(alloc / price))
            cost = qty * price
            if deployed + cost > cash * 0.95:
                break
            deployed += cost
            orders.append({
                "symbol": sym,
                "side": "buy",
                "qty": qty,
                "estimated_value": round(cost, 2),
            })
        if not orders:
            no_sig_reply = (
                "No signals cached — run a scan first."
                if not cached_signals else
                f"No eligible signals above conviction {min_conv} that aren't already held."
            )
            return jsonify({"ok": True, "reply": no_sig_reply})
        if entry_gate.get("blocked"):
            return jsonify({"ok": True, "reply": entry_gate["summary"]})
        order_gate = _chat_order_gate(
            [
                s for s in cached_signals
                if str(s.get("symbol") or "").upper() in {o["symbol"] for o in orders}
            ],
            positions,
            acct_snap,
            acctg_snap,
            scores_snap,
        )
        if not order_gate.get("ok"):
            err = (order_gate.get("errors") or [{}])[0].get("error") or "Risk gate blocks new buys."
            return jsonify({"ok": True, "reply": f"New buys are blocked: {err}"})
        regime_note = " at 50% size (CHOPPY)" if regime_mult < 1 else ""
        return jsonify({"ok": True, "trade_proposal": {
            "orders": orders,
            "summary": (
                f"Enter top {len(orders)} signal(s){regime_note}: "
                + ", ".join(f"{o['symbol']} x{o['qty']}" for o in orders)
            ),
        }})

    # ── System prompt ─────────────────────────────────────────────────────────
    system_prompt = """\
You are an algorithmic trading analyst for an Alpaca PAPER trading account (no real money at risk).
Your job: tell the user in plain English what the market is doing, what the portfolio is doing, and what the signals say.

The strategy uses 6 technical indicators scored 0-100:
  RSI(14) — momentum zone (sweet: 52-70 bull, <45 bear)
  MACD histogram — direction and slope (positive+rising = bullish)
  AVWAP — anchored VWAP from swing low/high (price above = institutional support)
  EMA 9/21 — trend direction (EMA9>EMA21 = uptrend)
  Trendline — slope + price position (up + price above = trend intact)
  Price action — reversal candles, swing breakouts/breakdowns, gaps, and volume confirmation

Entry: score ≥ 65. Position size scales with regime (BULL=100%, CHOPPY=50%). Stops are ATR-based.
Regime: BULL when QQQ ≥+0.4%, BEAR when QQQ ≤-0.5%, CHOPPY otherwise.

Be concise and direct — talk like a prop trader, not a financial advisor. Use plain numbers.
Dollar amounts with $ and commas. Percentages with one decimal.

When user asks to BUY or SELL a specific stock (e.g. "buy 10 NVDA", "sell my TSLA", "buy AAPL"):
  Respond with ONLY this JSON (no markdown, no extra text):
  {"trade_proposal": {"orders": [{"symbol": "NVDA", "side": "buy", "qty": 0}], "summary": "Brief reason"}}
  - Set qty=0 to mean "auto-calculate from portfolio sizing" — the server will calculate qty from position_size_pct.
  - Only set qty > 0 if the user explicitly specifies a share count.
  - side must be "buy" or "sell"

When user says to place/enter/execute orders or "go ahead" or "just do it" (e.g. "place orders", "enter positions", "go for it", "deploy capital"):
  Respond with ONLY this JSON using the top qualifying signals from context (no markdown, no extra text):
  {"action": "place_top_signals"}

When user asks to run/trigger/start a scan or live scores (e.g. "run live scores", "do it now", "scan now", "refresh signals"):
  Respond with ONLY this JSON (no markdown, no extra text):
  {"action": "trigger_live_scores"}

When user asks to score, analyze, or get signal details for any specific stock (e.g. "score ZS", "analyze NVDA", "what's the signal on AAPL", "score this for me: MSFT", "get signal for TSLA"):
  Respond with ONLY this JSON (no markdown, no extra text):
  {"action": "score_symbol", "symbol": "ZS"}

When user asks about unusual volume, high volume stocks, or volume spikes (e.g. "what has unusual volume", "any volume spikes", "high volume today"):
  Respond with ONLY this JSON (no markdown, no extra text):
  {"action": "unusual_volume"}

For all other queries respond in plain text. Never fabricate data not in context. This is paper trading."""

    # ── Context block ─────────────────────────────────────────────────────────
    # ── SPY / QQQ market context ──────────────────────────────────────────────
    index_context = ""
    try:
        _cl = get_client()
        _idx_snaps = _cl.get_snapshots(["SPY", "QQQ"])
        _idx_lines = []
        for _idx_sym in ("SPY", "QQQ"):
            _sn = (_idx_snaps or {}).get(_idx_sym) or {}
            _dp = _sn.get("dailyBar") or _sn.get("daily_bar") or {}
            _lp = float(_sn.get("latestTrade", {}).get("p") or _dp.get("c") or 0)
            _prev = float(_dp.get("vw") or _dp.get("o") or _lp)
            _chg = ((_lp - _prev) / _prev * 100) if _prev else 0
            if _lp:
                _idx_lines.append(f"{_idx_sym} ${_lp:.2f} ({_chg:+.1f}%)")
        if _idx_lines:
            index_context = "Market indices: " + " | ".join(_idx_lines)
    except Exception as _ie:
        log.debug("index context fetch failed: %s", _ie)

    # ── Ad-hoc ticker mentioned in the query (not in universe) ───────────────
    import re as _re
    _ticker_pat = _re.compile(r'\b([A-Z]{2,5})\b')
    _mentioned = {m.group(1) for m in _ticker_pat.finditer(text.upper())} - {
        "BUY", "SELL", "ETF", "USD", "EOD", "ATR", "RSI", "EMA", "THE", "AND",
        "NOT", "FOR", "ALL", "TOP", "SPY", "QQQ", "GET", "WHY", "RUN", "NOW",
        "ANY", "ASK", "LET", "OUT",
    }
    _universe = {str(s.get("symbol") or "").upper() for s in (scores_snap.get("signals") or scores_snap.get("top") or [])}
    _adhoc_syms = _mentioned - _universe - held_symbols
    adhoc_context = ""
    if _adhoc_syms:
        try:
            _cl2 = get_client()
            _adhoc_snaps = _cl2.get_snapshots(list(_adhoc_syms)[:5])
            _adhoc_lines = []
            for _as in list(_adhoc_syms)[:5]:
                _sn2 = (_adhoc_snaps or {}).get(_as) or {}
                _dp2 = _sn2.get("dailyBar") or _sn2.get("daily_bar") or {}
                _lp2 = float(_sn2.get("latestTrade", {}).get("p") or _dp2.get("c") or 0)
                _prev2 = float(_dp2.get("o") or _lp2)
                _chg2 = ((_lp2 - _prev2) / _prev2 * 100) if _prev2 else 0
                if _lp2:
                    _adhoc_lines.append(f"{_as} ${_lp2:.2f} ({_chg2:+.1f}%)")
            if _adhoc_lines:
                adhoc_context = "Ad-hoc quote(s): " + " | ".join(_adhoc_lines)
        except Exception as _ae:
            log.debug("adhoc ticker fetch failed: %s", _ae)

    # ── Recent orders ──────────────────────────────────────────────────────────
    try:
        _recent_raw = get_client().get_recent_orders(limit=10)
        recent_order_lines = [
            f"{o.get('side','?').upper()} {o.get('filled_qty') or o.get('qty','?')} {o.get('symbol','?')} "
            f"@ ${float(o.get('filled_avg_price') or 0):,.2f} [{o.get('status','?')}] {str(o.get('filled_at') or o.get('submitted_at') or '')[:16]}"
            for o in (_recent_raw or [])[:8]
        ]
    except Exception:
        recent_order_lines = ["unavailable"]

    # ── Variant history ────────────────────────────────────────────────────────
    try:
        vh = strategy_model.variant_win_rates()
        variant_lines = [
            f"{v}: {d['wins']}/{d['total']} wins ({d['win_rate']*100:.0f}%) avg_obj={d['avg_objective']:.3f}"
            for v, d in sorted(vh.items(), key=lambda x: -x[1]['win_rate'])
        ]
    except Exception:
        variant_lines = ["unavailable"]

    # ── News risk ──────────────────────────────────────────────────────────────
    news_risk  = cache_snap.get("news_risk") or []
    news_lines = [
        f"{n.get('symbol')}: {n.get('headline','')[:80]} (delta={n.get('delta')})"
        for n in news_risk[:3]
    ] or ["none"]

    gross_exp  = float((acctg_snap.get("gross_exposure_pct")) or 0)
    drift_max  = float((acctg_snap.get("max_drift_pct")) or 0)
    next_reb   = acctg_snap.get("next_rebalance_date") or "unknown"

    pos_tech_str = "\n".join(pos_tech_lines) if pos_tech_lines else "no technicals available"
    context_block = (
        f"=== Portfolio State ===\n"
        f"Equity: ${equity:,.0f} | Cash: ${cash:,.0f} | Daily P&L: ${daily_pnl:+,.0f}\n"
        f"Gross exposure: {gross_exp:.1f}% | Max weight drift: {drift_max:.1f}% | Next rebalance: {next_reb}\n"
        f"Market regime: {regime}"
        + (f" | {index_context}" if index_context else "")
        + "\n"
        f"Open positions ({len(positions)}): {', '.join(pos_lines) if pos_lines else 'none'}\n"
        f"Position technicals:\n{pos_tech_str}\n"
        f"Exit recommendations: {', '.join(exit_lines) if exit_lines else 'none'}\n"
        f"Risk: {risk_state.get('status','?')} — {risk_state.get('message','')}\n"
        f"Auto entry gate: {'BLOCKED' if entry_gate.get('blocked') else 'ELIGIBLE'} — {entry_gate.get('summary','')}\n"
        f"\n=== Recent Orders ===\n"
        f"{chr(10).join(recent_order_lines)}\n"
        f"\n=== News Risk ===\n"
        f"{chr(10).join(news_lines)}\n"
        f"\n=== Strategy Model ===\n"
        f"Generation {generation} | Active variant: {variant} | Min conviction: {conviction}/100 | Max hold: {max_hold}d\n"
        f"\n=== Variant Win Rates (last 90 days) ===\n"
        f"{chr(10).join(variant_lines)}\n"
        + (f"\n{adhoc_context}\n" if adhoc_context else "")
        + f"\n=== Top Signal Candidates (with indicator breakdown) ===\n"
        f"{chr(10).join(sig_lines) if sig_lines else 'No signals cached — run live scores first.'}\n"
    )

    # ── Gemini ────────────────────────────────────────────────────────────────
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        reply = _simple_chat_response(text_low, equity, daily_pnl, cash, regime, pos_lines, sig_lines,
                                      variant_lines=variant_lines, recent_order_lines=recent_order_lines,
                                      model_state=model_state, entry_gate=entry_gate)
        return jsonify({"ok": True, "reply": reply, "variant": "keyword_fallback"})

    try:
        import google.generativeai as genai  # type: ignore
        genai.configure(api_key=gemini_key)
        gmodel = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=system_prompt,
        )
        full_user = f"{context_block}\nUser: {text}"
        response  = gmodel.generate_content(full_user)
        raw = (response.text or "").strip()

        # Try to parse as action / trade_proposal JSON
        if raw.startswith("{") and "action" in raw:
            try:
                parsed = json.loads(raw)
                if parsed.get("action") == "trigger_live_scores":
                    threading.Thread(target=_run_live_scores, daemon=True).start()
                    return jsonify({"ok": True, "reply":
                        "Signal scan started. Takes ~30 seconds. Ask me 'what are the top signals?' when done."})
                if parsed.get("action") == "unusual_volume":
                    all_sigs = scores_snap.get("signals") or scores_snap.get("top") or []
                    vol_hits = []
                    for s in all_sigs:
                        vr = float((s.get("technicals") or {}).get("volume_ratio") or 0)
                        if vr >= 1.5:
                            price = float((s.get("quote") or {}).get("price") or 0)
                            chg   = float((s.get("quote") or {}).get("change_pct") or 0)
                            vol_hits.append((s["symbol"], vr, price, chg, s.get("score", 0)))
                    vol_hits.sort(key=lambda x: -x[1])
                    if vol_hits:
                        lines = [f"{sym}: {vr:.1f}x avg vol, ${price:.2f} ({chg:+.1f}%), score={score}"
                                 for sym, vr, price, chg, score in vol_hits[:8]]
                        return jsonify({"ok": True, "reply":
                            "Unusual volume (≥1.5× average):\n" + "\n".join(lines)})
                    return jsonify({"ok": True, "reply":
                        "No unusual volume detected in the current signal universe. Run a scan to refresh data."})
                if parsed.get("action") == "score_symbol":
                    _score_sym = str(parsed.get("symbol") or "").upper().strip()
                    if not _score_sym:
                        return jsonify({"ok": True, "reply": "Couldn't identify the ticker to score."})
                    try:
                        _cl3   = get_client()
                        _c3    = _live_scores_cache.get("data") or {}
                        _reg3  = str(_c3.get("regime") or regime or "CHOPPY").upper()
                        _q3    = trader.fetch_quotes([_score_sym], client=_cl3)
                        _q3    = sg.enrich_quotes_with_indicators(_q3, [_score_sym], alpaca_client=_cl3)
                        _sc3, _bd3 = sg.score_symbol(_score_sym, _q3, _reg3)
                        _reas3 = [bd["label"] for bd in _bd3.values() if bd.get("label")]
                        _qd3   = _q3.get(_score_sym) or {}
                        _news3 = _get_news_for_symbols([_score_sym]).get(_score_sym, [])
                        _signal3 = {
                            "symbol": _score_sym, "score": _sc3, "base_score": _sc3,
                            "news_delta": 0, "news_terms": [], "regime": _reg3,
                            "side": "buy", "quote": _qd3,
                            "technicals": _qd3.get("technicals") or {},
                            "technical_reasons": _reas3,
                            "signal_breakdown": _bd3,
                            "news": _news3[:5],
                        }
                        _ind_summary = []
                        for _iname in ("rsi", "macd", "avwap", "ema", "trend", "price_action"):
                            _ind = _bd3.get(_iname) or {}
                            if _ind.get("label"):
                                _ind_summary.append(f"{_iname.upper()}: {_ind['label']} ({_ind.get('score',0)}/{_ind.get('weight',0)}pts)")
                        _reply3 = (
                            f"{_score_sym} scores {_sc3}/100 (regime: {_reg3})\n"
                            + ("\n".join(_ind_summary) if _ind_summary else "No indicator breakdown available.")
                        )
                        return jsonify({"ok": True, "reply": _reply3, "signal_insight": _signal3})
                    except Exception as _se:
                        log.warning("chat score_symbol(%s) failed: %s", _score_sym, _se)
                        return jsonify({"ok": True, "reply": f"Couldn't score {_score_sym}: {_se}"})
                if parsed.get("action") == "place_top_signals":
                    # Redirect to the same logic as the fast-path enter block
                    cached_signals = (scores_snap.get("signals") or scores_snap.get("top") or [])
                    held_syms = {p["symbol"].upper() for p in positions}
                    _model = strategy_model.load_model()
                    min_conv = int(_model.get("min_conviction", 75))
                    size_pct = float(_model.get("position_size_pct", 0.05))
                    regime_mult = 0.5 if regime in ("CHOPPY", "CHOP") else 1.0
                    _orders = []
                    _deployed = 0.0
                    for _sig in cached_signals:
                        _sym = str(_sig.get("symbol") or "").upper()
                        if int(float(_sig.get("score") or 0)) < min_conv:
                            continue
                        if _sym in held_syms:
                            continue
                        _price = float((_sig.get("quote") or {}).get("price") or 0)
                        if _price <= 0:
                            continue
                        _alloc = equity * size_pct * regime_mult
                        _qty = max(1, int(_alloc / _price))
                        _cost = _qty * _price
                        if _deployed + _cost > cash * 0.95:
                            break
                        _deployed += _cost
                        _orders.append({"symbol": _sym, "side": "buy", "qty": _qty,
                                        "estimated_value": round(_cost, 2)})
                    if _orders:
                        if entry_gate.get("blocked"):
                            return jsonify({"ok": True, "reply": entry_gate["summary"]})
                        order_gate = _chat_order_gate(
                            [
                                s for s in cached_signals
                                if str(s.get("symbol") or "").upper() in {o["symbol"] for o in _orders}
                            ],
                            positions,
                            acct_snap,
                            acctg_snap,
                            scores_snap,
                        )
                        if not order_gate.get("ok"):
                            err = (order_gate.get("errors") or [{}])[0].get("error") or "Risk gate blocks new buys."
                            return jsonify({"ok": True, "reply": f"New buys are blocked: {err}"})
                        _regime_note = " at 50% size (CHOPPY)" if regime_mult < 1 else ""
                        return jsonify({"ok": True, "trade_proposal": {
                            "orders": _orders,
                            "summary": (
                                f"Enter top {len(_orders)} signal(s){_regime_note}: "
                                + ", ".join(f"{o['symbol']} x{o['qty']}" for o in _orders)
                            ),
                        }})
                    return jsonify({"ok": True, "reply": "No eligible signals to enter right now."})
            except (json.JSONDecodeError, Exception):
                pass

        if raw.startswith("{") and "trade_proposal" in raw:
            try:
                parsed = json.loads(raw)
                proposal = parsed.get("trade_proposal") or {}
                orders = proposal.get("orders") or []
                # Sanitize orders
                clean_orders = []
                _tp_model    = strategy_model.load_model()
                _tp_size_pct = float(_tp_model.get("position_size_pct", 0.05))
                _tp_regime_m = 0.5 if regime in ("CHOPPY", "CHOP") else 1.0
                for o in orders:
                    sym  = str(o.get("symbol") or "").upper().strip()
                    side = str(o.get("side") or "buy").lower()
                    qty  = int(float(o.get("qty") or 0))
                    if sym and side in {"buy", "sell"}:
                        if qty == 0 and side == "buy":
                            # auto-calculate from portfolio sizing
                            try:
                                _price_tp = get_client().get_current_price(sym) or 0
                            except Exception:
                                _price_tp = 0
                            if _price_tp > 0:
                                _alloc_tp = equity * _tp_size_pct * _tp_regime_m
                                qty = max(1, int(_alloc_tp / _price_tp))
                        if qty > 0:
                            _est = round(qty * float(o.get("price") or 0), 2)
                            clean_orders.append({"symbol": sym, "side": side, "qty": qty,
                                                 "estimated_value": _est})
                if clean_orders:
                    return jsonify({"ok": True, "trade_proposal": {
                        "orders": clean_orders,
                        "summary": proposal.get("summary") or f"Agent proposal: {len(clean_orders)} order(s)",
                    }})
            except (json.JSONDecodeError, Exception) as parse_err:
                log.debug("trade_proposal parse failed: %s", parse_err)
                # Fall through to return as plain reply

        return jsonify({"ok": True, "reply": raw})

    except Exception as e:
        log.warning("Gemini chat error: %s", e)
        reply = _simple_chat_response(text_low, equity, daily_pnl, cash, regime, pos_lines, sig_lines,
                                      variant_lines=variant_lines, recent_order_lines=recent_order_lines,
                                      model_state=model_state, entry_gate=entry_gate)
        return jsonify({"ok": True, "reply": f"(LLM unavailable: {type(e).__name__}) {reply}",
                        "variant": "keyword_fallback"})


def _simple_chat_response(text, equity, daily_pnl, cash, regime, pos_lines, sig_lines,
                          variant_lines=None, recent_order_lines=None, model_state=None,
                          entry_gate=None):
    entry_gate = entry_gate or {}

    if any(k in text for k in (
        "why are you not buying", "why aren't you buying", "why not buying",
        "why no buy", "why no trade", "nothing to trade", "why aren't you trading",
        "why are you not trading", "why not trading",
    )):
        if entry_gate.get("blocked"):
            return entry_gate.get("summary") or "Auto entries are currently blocked by the trader gate."
        if sig_lines:
            return "Auto entries are eligible on the next trader tick. Top signals: " + "; ".join(sig_lines[:2]) + "."
        return "No qualifying signal is currently above the entry threshold."

    if any(k in text for k in ("p&l", "pnl", "profit", "loss", "how am i doing", "performance", "return")):
        sign = "+" if daily_pnl >= 0 else ""
        return (f"Equity ${equity:,.0f}, daily P&L {sign}${daily_pnl:,.0f}. "
                f"Cash available: ${cash:,.0f}.")

    if any(k in text for k in ("position", "holding", "open")):
        if pos_lines:
            return "Open positions: " + "; ".join(pos_lines) + "."
        return "No open positions. All cash."

    if any(k in text for k in ("signal", "scan", "score", "what to buy", "entry", "candidate")):
        if sig_lines:
            return "Top signals: " + "; ".join(sig_lines) + "."
        return "No signals cached yet. Trigger a scan or wait for next run."

    if any(k in text for k in ("regime", "market", "bull", "bear", "choppy")):
        desc = {
            "BULL":    "trending up — momentum strategies active.",
            "BEAR":    "trending down — hedges and exits favored.",
            "CHOP":    "choppy — system trades at 50% size when signals score ≥ 63.",
            "CHOPPY":  "choppy — system trades at 50% size when signals score ≥ 63.",
            "UNKNOWN": "still loading — signals refreshing.",
        }.get(regime, "still loading.")
        return f"Current market is {desc}"

    if any(k in text for k in ("cash", "buying power", "available", "balance")):
        return f"Cash available: ${cash:,.0f}."

    if any(k in text for k in ("order", "trade", "activity", "recent", "history", "filled")):
        if recent_order_lines and recent_order_lines != ["unavailable"]:
            return "Recent orders:\n" + "\n".join(recent_order_lines[:6])
        return "No recent order data available."

    if any(k in text for k in ("variant", "winning", "win rate", "best variant", "which variant")):
        if variant_lines and variant_lines != ["unavailable"]:
            return "Variant win rates:\n" + "\n".join(variant_lines)
        return "No variant history yet — run at least one daily optimization."

    if any(k in text for k in ("model", "strategy", "conviction", "generation", "param", "setting")):
        if model_state:
            m = model_state
            return (f"Strategy gen {m.get('generation',0)}: variant={m.get('active_variant','?')}, "
                    f"min_conviction={m.get('min_conviction',75)}, max_positions={m.get('max_positions',3)}, "
                    f"position_size={int(float(m.get('position_size_pct',0.05))*100)}%, "
                    f"trailing_stop={m.get('trailing_stop_pct',3.0)}%, "
                    f"max_hold={m.get('max_holding_days',2)}d.")
        return "Strategy model not available."

    return (f"Equity ${equity:,.0f} | P&L ${daily_pnl:+,.0f} | Regime {regime} | "
            f"{'No positions' if not pos_lines else str(len(pos_lines)) + ' positions open'}. "
            "Ask me about positions, signals, P&L, regime, orders, variant history, or model settings.")


@app.route("/api/lab/trader/control")
@require_api_key
def api_trader_control_get():
    """Return current trading control state (paused, paper mode)."""
    return jsonify({"ok": True, **_load_trader_control()})


@app.route("/api/lab/trader/pause", methods=["POST"])
@require_api_key
def api_trader_pause():
    """Pause auto-trading — trader.py will skip all entries/exits until resumed."""
    state = _load_trader_control()
    state["paused"] = True
    _save_trader_control(state)
    log.warning("Auto-trading PAUSED via API")
    send_push_notification("⏸ Trading Paused", "Auto-trading has been paused.")
    return jsonify({"ok": True, "paused": True})


@app.route("/api/lab/trader/resume", methods=["POST"])
@require_api_key
def api_trader_resume():
    """Resume auto-trading."""
    state = _load_trader_control()
    state["paused"] = False
    _save_trader_control(state)
    log.warning("Auto-trading RESUMED via API")
    send_push_notification("▶️ Trading Resumed", "Auto-trading has been resumed.")
    return jsonify({"ok": True, "paused": False})


@app.route("/api/lab/trader/mode", methods=["POST"])
@require_api_key
def api_trader_mode():
    """Switch between paper and live trading.

    Body: { "paper": true|false, "confirm": "CONFIRM LIVE" }
    Switching to live requires confirm=="CONFIRM LIVE" as an extra safety check.
    Note: 'paper' is always reset to True on pod restart — the state file persists
    across ticks but NOT across pod restarts, so live mode must be re-confirmed after
    any deploy or restart.
    """
    body = request.get_json(silent=True) or {}
    want_paper = bool(body.get("paper", True))

    if not want_paper:
        confirm = str(body.get("confirm") or "")
        if confirm != "CONFIRM LIVE":
            return jsonify({
                "ok": False,
                "error": "Switching to live requires confirm='CONFIRM LIVE' in request body.",
            }), 400
        # Also require live keys to be configured
        live_key = os.environ.get("ALPACA_LIVE_KEY", "").strip()
        if not live_key:
            return jsonify({
                "ok": False,
                "error": "ALPACA_LIVE_KEY env var not set — cannot switch to live.",
            }), 400

    state = _load_trader_control()
    prev_paper = state.get("paper", True)
    state["paper"] = want_paper
    _save_trader_control(state)

    mode = "paper" if want_paper else "LIVE"
    prev = "paper" if prev_paper else "LIVE"
    log.warning("Trading mode changed: %s → %s", prev, mode)
    send_push_notification(
        f"🔀 Mode: {mode.upper()}",
        f"Trading switched from {prev} to {mode}.",
    )
    return jsonify({"ok": True, "paper": want_paper, "mode": mode})


@app.route("/api/lab/push/register", methods=["POST"])
@require_api_key
def api_push_register():
    """Register a device token for APNs push notifications (#41)."""
    body  = request.get_json(silent=True) or {}
    token = str(body.get("device_token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "device_token required"}), 400
    tokens = _load_push_tokens()
    if token not in tokens:
        tokens.append(token)
        _save_push_tokens(tokens)
        log.info("Push token registered (%d total)", len(tokens))
    return jsonify({"ok": True, "registered": len(tokens)})


@app.route("/api/lab/push/send", methods=["POST"])
@require_api_key
def api_push_send():
    """Manually fire a push notification (test / admin use)."""
    body  = request.get_json(silent=True) or {}
    title = str(body.get("title") or "Alpaca Agent")
    msg   = str(body.get("body")  or "")
    if not msg:
        return jsonify({"ok": False, "error": "body required"}), 400
    sent = send_push_notification(title, msg)
    return jsonify({"ok": True, "sent": sent})


@app.route("/api/lab/gaps")
@require_api_key
def api_lab_gaps():
    """Pre-market gap scanner (#33) — returns symbols with |overnight gap| >= min_gap_pct.

    Query params:
      min_gap_pct  float  default 1.5
    """
    try:
        min_gap = float(request.args.get("min_gap_pct") or 1.5)
        client = get_client()
        gaps = sg.scan_premarket_gaps(min_gap_pct=min_gap, alpaca_client=client)
        return jsonify({"ok": True, "gaps": gaps, "min_gap_pct": min_gap})
    except Exception as e:
        log.warning("gaps error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/analytics/pnl-attribution")
@require_api_key
def api_lab_pnl_attribution():
    """P&L attribution by signal factor (#39).

    Joins the trade ledger with signal_snapshot to break down win rate and
    average P&L per contributing indicator (rsi, macd, avwap, ema, trend,
    price_action).  Only considers closed (sell) trades with a signal_snapshot
    on the corresponding buy.
    """
    try:
        import json as _json
        limit = min(int(request.args.get("limit") or 500), 2000)
        trades = trade_ledger.recent_trades(limit=limit)

        # Build buy snapshot index: symbol+recorded_at → snapshot
        buy_map: dict[str, dict] = {}
        for t in trades:
            if t["side"] == "buy" and t.get("signal_snapshot"):
                try:
                    snap = _json.loads(t["signal_snapshot"])
                    buy_map[t["symbol"]] = snap
                except Exception:
                    pass

        # Accumulate per-factor stats from sell rows
        factor_stats: dict[str, dict] = {}
        for t in trades:
            if t["side"] != "sell" or t.get("pnl") is None:
                continue
            snap = buy_map.get(t["symbol"])
            if not snap:
                continue
            breakdown = snap.get("breakdown") or {}
            pnl = float(t["pnl"])
            won = pnl > 0
            for factor, bd in breakdown.items():
                if not isinstance(bd, dict):
                    continue
                bias = bd.get("bias") or bd.get("candle_bias") or ""
                if not bias or bias == "neutral":
                    continue
                key = f"{factor}:{bias}"
                fs = factor_stats.setdefault(key, {"wins": 0, "losses": 0, "total_pnl": 0.0})
                if won:
                    fs["wins"] += 1
                else:
                    fs["losses"] += 1
                fs["total_pnl"] += pnl

        results = []
        for key, fs in factor_stats.items():
            total = fs["wins"] + fs["losses"]
            results.append({
                "factor":      key,
                "trades":      total,
                "wins":        fs["wins"],
                "losses":      fs["losses"],
                "win_rate":    round(fs["wins"] / total * 100, 1) if total else 0,
                "total_pnl":   round(fs["total_pnl"], 2),
                "avg_pnl":     round(fs["total_pnl"] / total, 2) if total else 0,
            })
        results.sort(key=lambda x: x["total_pnl"], reverse=True)

        summary = trade_ledger.win_loss_summary(limit=limit)
        return jsonify({"ok": True, "attribution": results, "summary": summary})
    except Exception as e:
        log.warning("pnl attribution error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab/analytics/drift")
@require_api_key
def api_lab_drift():
    """Live win-rate vs backtest expectation drift detector (#40).

    Returns a drift alert if the rolling live win rate deviates > 15 percentage
    points from the backtest win rate stored in strategy_model.
    """
    try:
        import json as _json
        # Live stats
        live_summary = trade_ledger.win_loss_summary(limit=100)
        live_win_rate = float(live_summary.get("win_rate_pct") or 0)
        live_trades   = int(live_summary.get("trades") or 0)

        # Backtest expectation from last model
        model = strategy_model.load_model()
        bt_win_rate = float(model.get("backtest_win_rate_pct") or 0)
        if bt_win_rate == 0:
            # Try to pull from the last variant result file
            try:
                opt_path = strategy_model.STATE_DIR / "last_optimization.json"
                if opt_path.exists():
                    opt = _json.loads(opt_path.read_text(encoding="utf-8"))
                    bt_win_rate = float(
                        (opt.get("winner_stats") or {}).get("win_rate_pct") or 0
                    )
            except Exception:
                pass

        drift = round(live_win_rate - bt_win_rate, 1) if bt_win_rate else None
        alert = bool(drift is not None and abs(drift) > 15 and live_trades >= 10)

        return jsonify({
            "ok":           True,
            "live_win_rate":   live_win_rate,
            "bt_win_rate":     bt_win_rate,
            "drift":           drift,
            "live_trades":     live_trades,
            "alert":           alert,
            "alert_msg":       (
                f"Live win rate {live_win_rate:.1f}% is {abs(drift):.1f}pp "
                f"{'above' if drift > 0 else 'below'} backtest expectation ({bt_win_rate:.1f}%). "
                "Consider reviewing the model."
            ) if alert else None,
        })
    except Exception as e:
        log.warning("drift error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


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
