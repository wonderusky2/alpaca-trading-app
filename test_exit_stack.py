"""
test_exit_stack.py — table-driven tests for the live exit stack and sizing.

Runs at Docker build time (after test_smoke.py). The exit stack is the
highest-stakes code in the system: a silent regression here loses money on
every position. Any failure = non-zero exit = build fails.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# Isolated state + stub secrets BEFORE any project import
_TMP = tempfile.mkdtemp(prefix="exit_stack_test_")
os.environ["STATE_DIR"] = _TMP
os.environ.setdefault("ALPACA_PAPER_KEY", "test_stub")
os.environ.setdefault("ALPACA_PAPER_SECRET", "test_stub")

import types
import config
import strategy_model

# Load trader without executing main()
with open(os.path.join(os.path.dirname(__file__), "trader.py")) as f:
    _src = f.read().replace('if __name__ == "__main__":', 'if False:')
trader = types.ModuleType("trader")
exec(compile(_src, "trader.py", "exec"), trader.__dict__)

MODEL = strategy_model.sanitize_model({})   # v5 defaults
NOW = datetime.now(timezone.utc)
failures: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✓ {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"  ✗ {name}: {detail}")


def run_exit_stack(*, pnl_pct, peak_pct=None, hold_hours=2.0, stop_pct=None,
                   strategy="momentum", symbol="NVDA", regime="BULL"):
    """Drive _check_trailing_stops for one position; return exit intent or None."""
    peak_pct = pnl_pct if peak_pct is None else peak_pct
    # Seed position memory (peak + entry age)
    strategy_model.save_position_memory({symbol: {
        "first_seen_at": (NOW - timedelta(hours=hold_hours)).isoformat(),
        "entry_price": 100.0,
        "peak_unrealized_pnl_pct": peak_pct,
        "current_price": 100.0 * (1 + pnl_pct / 100),
    }})
    # Seed per-position strategy / ATR stop
    trader._STRATEGY_MEMORY_PATH.write_text(json.dumps({
        symbol: {"strategy": strategy, **({"stop_pct": stop_pct} if stop_pct else {})}
    }), encoding="utf-8")

    orders = []
    trader._submit_market_order = lambda client, sym, qty, side, **kw: orders.append(
        {"symbol": sym, "qty": qty, "side": side, "intent": kw.get("intent")}) or {"order_id": "t"}
    trader.notify.trade_sell = lambda *a, **k: None
    trader.notify.send = lambda *a, **k: None
    trader._record_sell = lambda *a, **k: None

    positions = {symbol: {"qty": 10, "entry": 100.0,
                          "unrealized_pl": pnl_pct * 10,
                          "unrealized_plpc": pnl_pct / 100}}
    quotes = {symbol: {"price": 100.0 * (1 + pnl_pct / 100)}}
    exited = trader._check_trailing_stops(object(), positions, quotes, MODEL, regime=regime)
    if exited:
        return orders[0]["intent"]
    return None


print("Exit stack gate tests (v5 model: stop=%.1f%%, target=%.1f%%, lock=%.1f%%, giveback=%.2f%%)"
      % (MODEL["trailing_stop_pct"], MODEL["profit_target_pct"],
         MODEL["profit_lock_trigger_pct"], MODEL["profit_giveback_pct"]))

# ── Gate 1: hard stop ─────────────────────────────────────────────────────────
check("loss past model stop exits as loss_stop",
      run_exit_stack(pnl_pct=-2.5) == "loss_stop")
check("small loss above stop does not exit",
      run_exit_stack(pnl_pct=-1.0) is None)
check("wide ATR stop (4%) tolerates -2.5%",
      run_exit_stack(pnl_pct=-2.5, stop_pct=4.0) is None)
check("wide ATR stop (4%) fires at -4.5%",
      run_exit_stack(pnl_pct=-4.5, stop_pct=4.0) == "loss_stop")
check("tight ATR stop (1.2%) fires at -1.5%",
      run_exit_stack(pnl_pct=-1.5, stop_pct=1.2) == "loss_stop")
check("insane stop_pct is clamped to 5% (fires at -5.5%)",
      run_exit_stack(pnl_pct=-5.5, stop_pct=50.0) == "loss_stop")
check("insane stop_pct clamped: -3% survives even with stop_pct=50",
      run_exit_stack(pnl_pct=-3.0, stop_pct=50.0) is None)

# ── Gate: regime flip — longs are cut when the regime turns BEAR ─────────────
check("long stock held into BEAR regime exits as regime_flip",
      run_exit_stack(pnl_pct=0.5, regime="BEAR") == "regime_flip")
check("long stock in CHOPPY is held (no forced regime exit)",
      run_exit_stack(pnl_pct=0.5, regime="CHOPPY") is None)

# ── Gate: profit roundtrip ────────────────────────────────────────────────────
check("locked winner (peak 2%) going red exits as profit_roundtrip",
      run_exit_stack(pnl_pct=-0.1, peak_pct=2.0) == "profit_roundtrip")
check("unlocked position (peak 1%) going slightly red survives",
      run_exit_stack(pnl_pct=-0.1, peak_pct=1.0) is None)

# ── Gate: max hold ────────────────────────────────────────────────────────────
check("position held past max_holding_days exits",
      run_exit_stack(pnl_pct=0.3, hold_hours=(MODEL["max_holding_days"] * 24 + 1))
      == "max_holding_days")

# ── Gate: profit target ───────────────────────────────────────────────────────
check("pnl above profit target exits as profit_target",
      run_exit_stack(pnl_pct=MODEL["profit_target_pct"] + 0.5) == "profit_target")

# ── Gate: profit giveback ─────────────────────────────────────────────────────
check("locked winner giving back > giveback exits as profit_giveback",
      run_exit_stack(pnl_pct=1.0, peak_pct=2.0) == "profit_giveback")
check("locked winner within giveback tolerance survives",
      run_exit_stack(pnl_pct=1.8, peak_pct=2.0) is None)

# ── Minimum-hold suppression ──────────────────────────────────────────────────
check("fresh position: noise exit (giveback) is suppressed",
      run_exit_stack(pnl_pct=1.0, peak_pct=2.0, hold_hours=0.05) is None)
check("fresh position: hard loss stop is NOT suppressed",
      run_exit_stack(pnl_pct=-2.5, hold_hours=0.05) == "loss_stop")
check("fresh position: profit target is NOT suppressed",
      run_exit_stack(pnl_pct=4.0, hold_hours=0.05) == "profit_target")

# ── Reversion tightening ──────────────────────────────────────────────────────
check("reversion trade takes profit at 1.6% (tightened target)",
      run_exit_stack(pnl_pct=1.6, strategy="reversion") == "profit_target")
check("momentum trade at 1.6% does not take profit",
      run_exit_stack(pnl_pct=1.6, peak_pct=1.6) is None)

# ── Core sleeve immunity ──────────────────────────────────────────────────────
core_sym = sorted(trader.CORE_SYMBOLS)[0]
check("core sleeve position is never exited by the stack",
      run_exit_stack(pnl_pct=-9.0, symbol=core_sym) is None)

# ── Risk-based sizing invariants ─────────────────────────────────────────────
q2 = trader._calc_qty(100, 100_000, MODEL, stop_pct=2.0)
q4 = trader._calc_qty(100, 100_000, MODEL, stop_pct=4.0)
q1 = trader._calc_qty(100, 100_000, MODEL, stop_pct=1.0)
check("equal dollar risk: notional halves when stop doubles",
      abs(q2 - 2 * q4) <= 2, f"q2={q2} q4={q4}")
check("tight stop capped at MAX_POSITION_SIZE_PCT",
      q1 * 100 <= 100_000 * config.MAX_POSITION_SIZE_PCT + 100, f"q1={q1}")
check("zero price yields zero qty", trader._calc_qty(0, 100_000, MODEL) == 0)

# ── Kill switch cap direction ─────────────────────────────────────────────────
loose_model = dict(MODEL); loose_model["daily_loss_kill_pct"] = -2.5
trader.notify.send = lambda *a, **k: None
acct = {"equity": 98_100, "last_equity": 100_000}   # -1.9% day
check("config caps a looser adaptive kill limit (halts at -1.9%)",
      trader._check_kill_switch(acct, loose_model) is True)
acct = {"equity": 99_000, "last_equity": 100_000}   # -1.0% day
check("kill switch stays quiet above the limit",
      trader._check_kill_switch(acct, loose_model) is False)

print()
if failures:
    print(f"EXIT STACK TESTS FAILED — {len(failures)} failure(s)")
    for f in failures:
        print(f"  • {f}")
    sys.exit(1)
print("EXIT STACK TESTS PASSED")
