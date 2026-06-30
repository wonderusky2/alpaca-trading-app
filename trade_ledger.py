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
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at  TEXT    NOT NULL,   -- ISO UTC timestamp
    date         TEXT    NOT NULL,   -- YYYY-MM-DD (trading date)
    equity       REAL    NOT NULL,
    cash         REAL,
    regime       TEXT
);
CREATE TABLE IF NOT EXISTS order_intents (
    broker_order_id  TEXT PRIMARY KEY,
    submitted_at     TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    qty              INTEGER NOT NULL,
    intent           TEXT,
    regime           TEXT,
    model_gen        INTEGER,
    signal_score     INTEGER,
    signal_snapshot  TEXT,
    model_snapshot   TEXT,
    experiment_id    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS eq_snap_date ON equity_snapshots(date);
"""

_TRADE_MIGRATIONS = {
    "broker_order_id": "TEXT",
    "filled_at": "TEXT",
    "source": "TEXT",
    "intent": "TEXT",
    "experiment_id": "TEXT",
}


def _connect() -> sqlite3.Connection:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(LEDGER_PATH))
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_DDL)
    existing = {row[1] for row in con.execute("PRAGMA table_info(trades)")}
    for column, column_type in _TRADE_MIGRATIONS.items():
        if column not in existing:
            con.execute(f"ALTER TABLE trades ADD COLUMN {column} {column_type}")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS trades_broker_order_id "
        "ON trades(broker_order_id) WHERE broker_order_id IS NOT NULL"
    )
    con.commit()
    return con


def record_order_intent(
    broker_order_id: str,
    symbol: str,
    side: str,
    qty: int,
    intent: str = "",
    regime: str = "",
    signal_score: int | None = None,
    signal_snapshot: dict | None = None,
    model_snapshot: dict | None = None,
    experiment_id: str = "",
) -> None:
    """Persist the decision context for an order before its fill is reconciled."""
    if not broker_order_id:
        return
    if model_snapshot is None:
        try:
            model_snapshot = strategy_model.load_model()
        except Exception:
            model_snapshot = {}
    try:
        con = _connect()
        con.execute(
            """INSERT OR REPLACE INTO order_intents
               (broker_order_id, submitted_at, symbol, side, qty, intent, regime,
                model_gen, signal_score, signal_snapshot, model_snapshot, experiment_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(broker_order_id), datetime.now(timezone.utc).isoformat(),
                str(symbol).upper(), str(side).lower(), int(qty), str(intent or ""),
                str(regime or ""), model_snapshot.get("generation"), signal_score,
                json.dumps(signal_snapshot) if signal_snapshot else None,
                json.dumps(model_snapshot), str(experiment_id or ""),
            ),
        )
        con.commit()
        con.close()
    except Exception as exc:
        import logging
        logging.getLogger("trade_ledger").error("Order intent insert failed: %s", exc)


def reconcile_broker_orders(orders: list[dict]) -> dict:
    """Insert confirmed Alpaca fills exactly once, using broker values as truth."""
    filled = [
        order for order in (orders or [])
        if str(order.get("status") or "").lower() == "filled"
        and order.get("id")
        and float(order.get("filled_qty") or 0) > 0
        and float(order.get("filled_avg_price") or 0) > 0
    ]
    filled.sort(key=lambda order: str(order.get("filled_at") or order.get("submitted_at") or ""))
    inserted = 0
    skipped = 0
    try:
        con = _connect()
        for order in filled:
            order_id = str(order["id"])
            if con.execute(
                "SELECT 1 FROM trades WHERE broker_order_id=?", (order_id,)
            ).fetchone():
                skipped += 1
                continue
            side = str(order.get("side") or "").lower()
            if side not in {"buy", "sell"}:
                continue
            symbol = str(order.get("symbol") or "").upper()
            qty = int(float(order.get("filled_qty") or 0))
            price = float(order.get("filled_avg_price") or 0)
            filled_at = str(order.get("filled_at") or order.get("submitted_at") or datetime.now(timezone.utc).isoformat())
            intent_row = con.execute(
                """SELECT intent, regime, model_gen, signal_score, signal_snapshot,
                          model_snapshot, experiment_id
                   FROM order_intents WHERE broker_order_id=?""",
                (order_id,),
            ).fetchone()
            intent_data = intent_row or (None, None, None, None, None, None, None)
            if side == "sell":
                pnl, matched_experiment = _fifo_realized_match(con, symbol, qty, price)
            else:
                pnl = None
                matched_experiment = intent_data[6]
            con.execute(
                """INSERT INTO trades
                   (recorded_at, symbol, side, qty, price, notional, pnl,
                    exit_reason, regime, model_gen, signal_score, signal_snapshot,
                    model_snapshot, filled_at, broker_order_id, source, intent,
                    experiment_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           'alpaca_fill', ?, ?)""",
                (
                    filled_at, symbol, side, qty, price, qty * price, pnl,
                    intent_data[0] if side == "sell" else None,
                    intent_data[1], intent_data[2], intent_data[3], intent_data[4],
                    intent_data[5] or json.dumps({}), filled_at, order_id,
                    intent_data[0], matched_experiment,
                ),
            )
            inserted += 1
        con.commit()
        total = con.execute(
            "SELECT COUNT(*) FROM trades WHERE source='alpaca_fill'"
        ).fetchone()[0]
        con.close()
        return {"ok": True, "inserted": inserted, "skipped": skipped, "total": total}
    except Exception as exc:
        import logging
        logging.getLogger("trade_ledger").error("Broker reconciliation failed: %s", exc)
        return {"ok": False, "inserted": inserted, "skipped": skipped, "error": str(exc)}


def _fifo_realized_match(
    con: sqlite3.Connection,
    symbol: str,
    sell_qty: int,
    sell_price: float,
) -> tuple[float | None, str | None]:
    """Return FIFO realized P&L and the experiment owning the matched entry lots."""
    rows = con.execute(
        """SELECT side, qty, price, experiment_id FROM trades
           WHERE symbol=? AND source='alpaca_fill'
           ORDER BY COALESCE(filled_at, recorded_at), id""",
        (symbol,),
    ).fetchall()
    lots: list[list] = []
    for side, qty, price, experiment_id in rows:
        remaining = float(qty)
        if side == "buy":
            lots.append([remaining, float(price), experiment_id])
            continue
        while remaining > 0 and lots:
            used = min(remaining, lots[0][0])
            lots[0][0] -= used
            remaining -= used
            if lots[0][0] <= 0:
                lots.pop(0)
    remaining = float(sell_qty)
    pnl = 0.0
    matched = 0.0
    matched_experiments: set[str | None] = set()
    while remaining > 0 and lots:
        used = min(remaining, lots[0][0])
        pnl += used * (sell_price - lots[0][1])
        matched += used
        matched_experiments.add(lots[0][2])
        lots[0][0] -= used
        remaining -= used
        if lots[0][0] <= 0:
            lots.pop(0)
    experiment_id = None
    if matched == float(sell_qty) and len(matched_experiments) == 1:
        candidate = next(iter(matched_experiments))
        experiment_id = str(candidate) if candidate else None
    return (round(pnl, 2) if matched > 0 else None), experiment_id


def _fifo_realized_pnl(con: sqlite3.Connection, symbol: str, sell_qty: int, sell_price: float) -> float | None:
    """Backward-compatible realized P&L helper."""
    return _fifo_realized_match(con, symbol, sell_qty, sell_price)[0]


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


def record_equity_snapshot(equity: float, cash: float = 0.0, regime: str = "") -> None:
    """Record today's equity. One row per calendar date (UPSERT by date)."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now   = datetime.now(timezone.utc).isoformat()
        con   = _connect()
        con.execute(
            """INSERT INTO equity_snapshots (recorded_at, date, equity, cash, regime)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 recorded_at=excluded.recorded_at,
                 equity=excluded.equity,
                 cash=excluded.cash,
                 regime=excluded.regime""",
            (now, today, equity, cash, regime),
        )
        con.commit()
        con.close()
    except Exception as e:
        import logging
        logging.getLogger("trade_ledger").error("equity snapshot failed: %s", e)


def equity_period_baselines() -> dict:
    """Return equity at start-of-week, start-of-quarter, start-of-year from snapshots."""
    try:
        from datetime import date, timedelta
        today = date.today()
        # Start of current week (Monday)
        bow   = today - timedelta(days=today.weekday())
        # Start of current quarter
        boq   = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
        # Start of current year
        boy   = date(today.year, 1, 1)

        con = _connect()
        result = {}
        for key, target in [("week", bow), ("qtd", boq), ("ytd", boy)]:
            # Find the closest snapshot on or before target date
            cur = con.execute(
                "SELECT date, equity FROM equity_snapshots WHERE date <= ? ORDER BY date DESC LIMIT 1",
                (target.isoformat(),),
            )
            row = cur.fetchone()
            if row:
                result[key] = {"date": row[0], "equity": row[1]}
        con.close()
        return result
    except Exception:
        return {}


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


def win_loss_summary(limit: int = 500, experiment_id: str | None = None) -> dict:
    """Basic win/loss stats from the ledger for closed (sell) rows."""
    try:
        con = _connect()
        where = "side='sell' AND pnl IS NOT NULL"
        params: list = []
        if experiment_id:
            where += " AND experiment_id=?"
            params.append(experiment_id)
        params.append(limit)
        cur = con.execute(
            f"SELECT pnl FROM trades WHERE {where} ORDER BY id DESC LIMIT ?",
            params,
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


def edge_summary(limit: int = 500, experiment_id: str | None = None) -> dict:
    """Return trade evidence used by entry/promotion gates."""
    summary = win_loss_summary(limit=limit, experiment_id=experiment_id)
    first_trade_at = None
    try:
        con = _connect()
        if experiment_id:
            cur = con.execute(
                "SELECT MIN(recorded_at) FROM trades WHERE experiment_id=?",
                (experiment_id,),
            )
        else:
            cur = con.execute("SELECT MIN(recorded_at) FROM trades")
        first_trade_at = cur.fetchone()[0]
        con.close()
    except Exception:
        first_trade_at = None

    paper_days = 0.0
    if first_trade_at:
        try:
            first_dt = datetime.fromisoformat(str(first_trade_at).replace("Z", "+00:00"))
            paper_days = max(0.0, (datetime.now(timezone.utc) - first_dt).total_seconds() / 86400)
        except Exception:
            paper_days = 0.0

    trades = int(summary.get("trades") or 0)
    expectancy = float(summary.get("expectancy") or 0.0)
    return {
        **summary,
        "paper_days": round(paper_days, 1),
        "first_trade_at": first_trade_at,
        "expectancy_positive": bool(trades > 0 and expectancy > 0),
        "experiment_id": experiment_id,
    }
