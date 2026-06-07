"""portfolio.py — Thread-safe paper portfolio state management."""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock

PORTFOLIO_PATH = Path.home() / "Documents" / "paper_trading" / "paper_portfolio.json"
LOCK_PATH      = Path(str(PORTFOLIO_PATH) + ".lock")
START_CAPITAL  = 100_000.0
MAX_POSITION_PCT   = 0.05
TRAILING_STOP_PCT  = 0.02
DAILY_LOSS_LIMIT   = 0.02
MAX_DRAWDOWN       = 0.20
MAX_OPEN_POSITIONS = 6


def _load() -> dict:
    with open(PORTFOLIO_PATH) as f:
        return json.load(f)

def _save(p: dict) -> None:
    p["account"]["last_updated"] = datetime.now(timezone.utc).isoformat()
    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(p, f, indent=2)

def _recalc(p: dict) -> None:
    equity = sum(pos["market_value"] for pos in p["positions"].values())
    total  = p["account"]["cash"] + equity
    p["account"]["total_value"]   = round(total, 2)
    p["account"]["total_pnl"]     = round(total - START_CAPITAL, 2)
    p["account"]["total_pnl_pct"] = round((total - START_CAPITAL) / START_CAPITAL * 100, 4)
    if total > p["account"]["peak_value"]:
        p["account"]["peak_value"] = total
    drawdown = (p["account"]["peak_value"] - total) / p["account"]["peak_value"]
    if drawdown >= MAX_DRAWDOWN or p["account"]["daily_pnl_pct"] <= -DAILY_LOSS_LIMIT:
        p["account"]["kill_switch_triggered"] = True

def daily_reset_if_needed(p: dict) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    if p["account"].get("last_trading_day") != today:
        p["account"]["daily_pnl"]           = 0.0
        p["account"]["daily_pnl_pct"]       = 0.0
        p["account"]["day_trades_used"]     = 0
        p["account"]["kill_switch_triggered"] = False
        p["account"]["last_trading_day"]    = today
        p["account"]["daily_open_value"]    = p["account"]["total_value"]

def update_prices(price_map: dict[str, float]) -> list[str]:
    """Update positions, raise trailing stops. Returns list of stop-triggered symbols."""
    with FileLock(LOCK_PATH):
        p = _load()
        stops_hit = []
        for sym, price in price_map.items():
            pos = p["positions"].get(sym)
            if not pos: continue
            pos["last_price"]        = price
            pos["market_value"]      = round(pos["quantity"] * price, 2)
            pos["unrealized_pnl"]    = round(pos["market_value"] - pos["quantity"] * pos["avg_cost"], 2)
            pos["unrealized_pnl_pct"]= round((price - pos["avg_cost"]) / pos["avg_cost"] * 100, 2)
            if price > pos.get("high_price", price):
                pos["high_price"]  = price
                new_stop = round(price * (1 - TRAILING_STOP_PCT), 2)
                if new_stop > pos["stop_price"]:
                    pos["stop_price"] = new_stop
            if price <= pos["stop_price"]:
                stops_hit.append(sym)
        _recalc(p)
        # Update daily pnl
        open_val = p["account"].get("daily_open_value", START_CAPITAL)
        p["account"]["daily_pnl"]     = round(p["account"]["total_value"] - open_val, 2)
        p["account"]["daily_pnl_pct"] = round(p["account"]["daily_pnl"] / open_val * 100, 4)
        _save(p)
        return stops_hit

def buy(sym: str, price: float, score: int, regime: str) -> dict | None:
    with FileLock(LOCK_PATH):
        p = _load()
        if p["account"]["kill_switch_triggered"]:
            return None
        if sym in p["positions"]:
            return None
        if len(p["positions"]) >= MAX_OPEN_POSITIONS:
            return None
        conviction = score / 100
        max_dollars = p["account"]["total_value"] * MAX_POSITION_PCT
        dollars = max_dollars * max(0.6, min(1.0, conviction))
        dollars = min(dollars, p["account"]["cash"])
        shares  = int(dollars / price)
        if shares < 1: return None
        cost = shares * price
        p["account"]["cash"] -= cost
        p["positions"][sym] = {
            "quantity": shares, "avg_cost": price, "last_price": price,
            "market_value": cost, "unrealized_pnl": 0.0, "unrealized_pnl_pct": 0.0,
            "stop_price": round(price * (1 - TRAILING_STOP_PCT), 2),
            "high_price": price, "strategy": regime, "score": score,
            "entry_date": datetime.now(timezone.utc).isoformat(),
        }
        trade = {"id": len(p["trade_log"]) + 1,
                 "timestamp": datetime.now(timezone.utc).isoformat(),
                 "symbol": sym, "side": "buy", "quantity": shares,
                 "price": price, "value": round(cost, 2),
                 "score": score, "regime": regime}
        p["trade_log"].append(trade)
        _recalc(p); _save(p)
        return trade

def sell(sym: str, price: float, reason: str = "manual") -> dict | None:
    with FileLock(LOCK_PATH):
        p = _load()
        pos = p["positions"].get(sym)
        if not pos: return None
        qty      = pos["quantity"]
        proceeds = qty * price
        realized = proceeds - qty * pos["avg_cost"]
        p["account"]["cash"] += proceeds
        del p["positions"][sym]
        trade = {"id": len(p["trade_log"]) + 1,
                 "timestamp": datetime.now(timezone.utc).isoformat(),
                 "symbol": sym, "side": "sell", "quantity": qty,
                 "price": price, "value": round(proceeds, 2),
                 "realized_pnl": round(realized, 2),
                 "realized_pnl_pct": round(realized / (qty * pos["avg_cost"]) * 100, 2),
                 "reason": reason}
        p["trade_log"].append(trade)
        _recalc(p)
        open_val = p["account"].get("daily_open_value", START_CAPITAL)
        p["account"]["daily_pnl"]     = round(p["account"]["total_value"] - open_val, 2)
        p["account"]["daily_pnl_pct"] = round(p["account"]["daily_pnl"] / open_val * 100, 4)
        _save(p); return trade

def status() -> dict:
    return _load()
