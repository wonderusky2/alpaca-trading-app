"""Deterministic post-close report for the active paper-trading experiment."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import config
import notify
import strategy_model
import trade_ledger
from alpaca_client import AlpacaClient


REPORT_PATH = strategy_model.STATE_DIR / "experiment_report.json"


def build_report(account: dict | None = None, positions: dict | None = None) -> dict:
    experiment_id = str(config.ALPHA_EXPERIMENT_ID)
    rows = [
        row for row in trade_ledger.recent_trades(limit=2000)
        if row.get("experiment_id") == experiment_id
    ]
    exits = [row for row in rows if row.get("side") == "sell" and row.get("pnl") is not None]
    summary = trade_ledger.edge_summary(limit=2000, experiment_id=experiment_id)

    by_symbol: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    by_exit: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    hold_buckets = {
        "under_15m": {"trades": 0, "pnl": 0.0},
        "15m_to_6h": {"trades": 0, "pnl": 0.0},
        "over_6h": {"trades": 0, "pnl": 0.0},
        "unknown": {"trades": 0, "pnl": 0.0},
    }
    for row in exits:
        pnl = float(row.get("pnl") or 0)
        symbol = str(row.get("symbol") or "UNKNOWN")
        reason = str(row.get("exit_reason") or row.get("intent") or "unknown")
        for target, key in ((by_symbol, symbol), (by_exit, reason)):
            target[key]["trades"] += 1
            target[key]["pnl"] += pnl
            target[key]["wins"] += int(pnl > 0)
        hold = row.get("hold_minutes")
        if hold is None:
            bucket = "unknown"
        elif float(hold) < 15:
            bucket = "under_15m"
        elif float(hold) <= 360:
            bucket = "15m_to_6h"
        else:
            bucket = "over_6h"
        hold_buckets[bucket]["trades"] += 1
        hold_buckets[bucket]["pnl"] += pnl

    def finalize(groups: dict[str, dict]) -> dict[str, dict]:
        result = {}
        for key, value in groups.items():
            trades = int(value["trades"])
            result[key] = {
                "trades": trades,
                "pnl": round(float(value["pnl"]), 2),
                "expectancy": round(float(value["pnl"]) / trades, 2) if trades else 0.0,
                "win_rate_pct": round(100 * int(value["wins"]) / trades, 1) if trades else 0.0,
            }
        return result

    for bucket in hold_buckets.values():
        bucket["pnl"] = round(float(bucket["pnl"]), 2)
        bucket["expectancy"] = (
            round(float(bucket["pnl"]) / int(bucket["trades"]), 2)
            if bucket["trades"] else 0.0
        )

    closed = int(summary.get("trades") or 0)
    expectancy = float(summary.get("expectancy") or 0)
    minimum = int(config.EDGE_GATE_MIN_CLOSED_TRADES)
    if closed < minimum:
        status = "COLLECTING"
    elif expectancy > float(config.EDGE_GATE_MIN_EXPECTANCY):
        status = "EDGE_CANDIDATE"
    else:
        status = "SHUTDOWN_CANDIDATE"

    account = account or {}
    positions = positions or {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "experiment_id": experiment_id,
        "status": status,
        "minimum_closed_trades": minimum,
        "summary": summary,
        "lifetime_summary": trade_ledger.win_loss_summary(limit=2000),
        "by_symbol": finalize(by_symbol),
        "by_exit_reason": finalize(by_exit),
        "hold_buckets": hold_buckets,
        "account": {
            "equity": float(account.get("equity") or 0),
            "cash": float(account.get("cash") or 0),
            "buying_power": float(account.get("buying_power") or 0),
        },
        "open_positions": {
            symbol: {
                "qty": float(position.get("qty") or 0),
                "market_value": float(position.get("market_val") or 0),
                "unrealized_pnl": float(position.get("unrealized_pl") or 0),
                "unrealized_pnl_pct": float(position.get("unrealized_plpc") or 0) * 100,
            }
            for symbol, position in positions.items()
        },
    }


def write_report(report: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(REPORT_PATH)


def main() -> None:
    client = AlpacaClient()
    reconciliation = trade_ledger.reconcile_broker_orders(client.get_recent_orders(limit=500))
    account = client.get_account()
    positions = client.get_positions()
    report = build_report(account, positions)
    report["reconciliation"] = reconciliation
    write_report(report)
    summary = report["summary"]
    print(json.dumps(report, sort_keys=True), flush=True)
    notify.send(
        "Alpha experiment close\n"
        f"Status: {report['status']}\n"
        f"Closed: {summary.get('trades', 0)}/{report['minimum_closed_trades']} · "
        f"P&L: ${float(summary.get('total_pnl') or 0):+,.2f} · "
        f"Expectancy: ${float(summary.get('expectancy') or 0):+,.2f}\n"
        f"Open positions: {len(report['open_positions'])}"
    )


if __name__ == "__main__":
    main()
