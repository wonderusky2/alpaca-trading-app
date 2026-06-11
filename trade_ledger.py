"""
trade_ledger.py — append-only SQLite trade log (#21).

Schema (trades table):
  id              INTEGER PRIMARY KEY AUTOINCREMENT
  recorded_at     TEXT    ISO timestamp (UTC)
  symbol          TEXT
  side            TEXT    "buy" | "sell"
  qty             INTEGER
  price           REAL
  notional        REAL    qty * price
  pnl             REAL    NULL on buy; realized P&L on sell
  exit_reason     TEXT    NULL on buy; e.g. "loss_stop", "profit_target"
  regime          TEXT    BULL | BEAR | CHOPPY at time of trade
  model_gen       INTEGER strategy_model generation at time of trade
  signal_score    INTEGER conviction score (NULL on sell)
  signal_snapshot TEXT    JSON blob of full signal at entry
  model_snapshot  TEXT    JSON blob of model params at trade time

Never UPDATE or DELETE.  New corrections go in as offsetting rows.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import os

import strategy_model

LEDGER_PATH = Path(os.environ.get("STATE_DIR") or (Path.home() / ".robinhood-trader" / "state")) / "trades.db"

_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at      TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    side             TEXT    NOT NULL,
    qty              INTEGER NOT NULL,
    price            REAL    NOT NULL,
    notional         REAL    NOT NULL,
    pnl              REAL,
    exit_reason      TEXT,
    regime           TEXT,
    model_gen        INTEGER,
    signal_score     INTEGER,
    signal_snapshot  TEXT,
    model_snapshot   TEXT
);
"""


def _connect() -> sqlite3.Connection:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(LEDGER_PATH))
    con.execute(_DDL)
    con.commit()
    return con


def record_buy(
    symbol: str,
    qty: int,
    price: float,
    regime: str = "",
    signal_score: int | None = None,
    signal_snapshot: dict | None = None,
    model_snapshot: dict | None = None,
) -> None:
    """Append a buy row."""
    if model_snapshot is None:
        try:
            model_snapshot = strategy_model.load_model()
        except Exception:
            model_snapshot = {}
    row = {
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "symbol":          symbol.upper(),
        "side":            "buy",
        "qty":             qty,
        "price":           price,
        "notional":        qty * price,
        "pnl":             None,
        "exit_reason":     None,
        "regime":          regime,
        "model_gen":       model_snapshot.get("generation"),
        "signal_score":    signal_score,
        "signal_snapshot": json.dumps(signal_snapshot) if signal_snapshot else None,
        "model_snapshot":  json.dumps(model_snapshot),
    }
    _insert(row)


def record_sell(
    symbol: str,
    qty: int,
    price: float,
    pnl: float,
    exit_reason: str = "",
    regime: str = "",
    model_snapshot: dict | None = None,
) -> None:
    """Append a sell row."""
    if model_snapshot is None:
        try:
            model_snapshot = strategy_model.load_model()
        except Exception:
            model_snapshot = {}
    row = {
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "symbol":          symbol.upper(),
        "side":            "sell",
        "qty":             qty,
        "price":           price,
        "notional":        qty * price,
        "pnl":             pnl,
        "exit_reason":     exit_reason,
        "regime":          regime,
        "model_gen":       model_snapshot.get("generation"),
        "signal_score":    None,
        "signal_snapshot": None,
        "model_snapshot":  json.dumps(model_snapshot),
    }
    _insert(row)


def _insert(row: dict) -> None:
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    sql = f"INSERT INTO trades ({cols}) VALUES ({placeholders})"
    try:
        con = _connect()
        con.execute(sql, list(row.values()))
        con.commit()
        con.close()
    except Exception as e:
        # Ledger failure must never crash the trading loop
        import logging
        logging.getLogger("trade_ledger").error("Ledger insert failed: %s", e)


def recent_trades(limit: int = 200) -> list[dict]:
    """Return the most recent trades as a list of dicts."""
    try:
        con = _connect()
        cur = con.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        con.close()
        return rows
    except Exception:
        return []


def win_loss_summary(limit: int = 500) -> dict:
    """Basic win/loss stats from the ledger for closed (sell) rows."""
    try:
        con = _connect()
        cur = con.execute(
            "SELECT pnl FROM trades WHERE side='sell' AND pnl IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        pnls = [r[0] for r in cur.fetchall()]
        con.close()
    except Exception:
        pnls = []

    if not pnls:
        return {"trades": 0}

    wins  = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win  = sum(wins) / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    return {
        "trades":       len(pnls),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1),
        "avg_win":      round(avg_win, 2),
        "avg_loss":     round(avg_loss, 2),
        "total_pnl":    round(sum(pnls), 2),
        "expectancy":   round(
            (len(wins) / len(pnls)) * avg_win + (len(losses) / len(pnls)) * avg_loss, 2
        ) if pnls else 0.0,
    }
