"""
Smoke tests — run at Docker build time to catch crashes before deploy.
Validates imports, attribute references, and key function signatures.
Any failure = non-zero exit = build fails.
"""
import sys
import os
import tempfile
from pathlib import Path

# ── Stub out secrets so imports don't fail ────────────────────────────────────
for var in ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL",
            "LAB_API_KEY", "GOOGLE_API_KEY", "IMESSAGE_PHONE"]:
    os.environ.setdefault(var, "test_stub")

errors = []

# ── 1. All modules import cleanly ─────────────────────────────────────────────
modules = {}
for mod in ["config", "logger", "notify", "signals", "strategy_model", "backtest"]:
    try:
        modules[mod] = __import__(mod)
        print(f"  ✓ import {mod}")
    except Exception as e:
        errors.append(f"import {mod}: {e}")
        print(f"  ✗ import {mod}: {e}")

# trader.py: import without running main()
try:
    import importlib.util, types
    spec = importlib.util.spec_from_file_location("trader", "trader.py")
    trader_mod = types.ModuleType("trader")
    # Exec only the top-level definitions, skip the `if __name__ == "__main__"` block
    with open("trader.py") as f:
        src = f.read()
    # Replace the bottom guard so it doesn't execute main()
    src = src.replace('if __name__ == "__main__":', 'if False:')
    exec(compile(src, "trader.py", "exec"), trader_mod.__dict__)
    modules["trader"] = trader_mod
    print("  ✓ import trader (dry-run)")
except Exception as e:
    errors.append(f"import trader: {e}")
    print(f"  ✗ import trader: {e}")

# ── 2. Critical attribute references ──────────────────────────────────────────
checks = [
    ("config",         "IGNORED_POSITIONS"),
    ("config",         "PAPER"),
    ("signals",        "BULL_UNIVERSE"),
    ("signals",        "BEAR_UNIVERSE"),
    ("signals",        "ALL_SYMBOLS"),
    ("signals",        "MIN_CONVICTION"),
    ("signals",        "BEAR_ETF_MIN_CONVICTION"),
    ("signals",        "VARIANT_PROFILES"),
    ("signals",        "_PROFILE_PARAM_KEYS"),
    ("signals",        "get_signals"),
    ("signals",        "detect_regime"),
    ("signals",        "score_symbol"),
    ("signals",        "enrich_quotes_with_indicators"),
    ("strategy_model", "load_model"),
    ("strategy_model", "sanitize_model"),
    ("strategy_model", "load_position_memory"),
    ("strategy_model", "update_position_memory"),
    ("notify",         "send"),
    ("notify",         "trade_sell"),
    ("notify",         "trade_pyramid"),
]

for mod_name, attr in checks:
    mod = modules.get(mod_name)
    if mod is None:
        continue
    if not hasattr(mod, attr):
        errors.append(f"{mod_name}.{attr} — MISSING")
        print(f"  ✗ {mod_name}.{attr} missing")
    else:
        print(f"  ✓ {mod_name}.{attr}")

# ── 3. trader.py key functions exist ──────────────────────────────────────────
trader = modules.get("trader")
if trader:
    for fn in ["_conviction_size_mult", "_calc_qty", "_holding_hours", "_check_trailing_stops", "_rotate_stale_positions", "_reentry_block_status", "main", "fetch_quotes"]:
        if not hasattr(trader, fn):
            errors.append(f"trader.{fn} — MISSING")
            print(f"  ✗ trader.{fn} missing")
        else:
            print(f"  ✓ trader.{fn}")

# ── 4. Conviction multiplier sanity ───────────────────────────────────────────
# Alpha mode uses tiered conviction sizing: press A+ setups, trim borderline ones.
if trader and hasattr(trader, "_conviction_size_mult"):
    fn = trader._conviction_size_mult
    assert fn(95) == 1.35,  f"score 95 should size at 1.35x, got {fn(95)}"
    assert fn(88) == 1.20,  f"score 88 should size at 1.20x, got {fn(88)}"
    assert fn(80) == 1.0,   f"score 80 should size at 1.0x, got {fn(80)}"
    assert fn(60) == 0.75,  f"score 60 should size at 0.75x, got {fn(60)}"
    print("  ✓ _conviction_size_mult tiered alpha sizing")

# ── 5. Sizing / migration sanity ─────────────────────────────────────────────
if trader and hasattr(trader, "_calc_qty"):
    qty = trader._calc_qty(price=500, equity=100000, model={"position_size_pct": 0.01}, size_mult=1.0)
    assert qty >= 5, f"min alpha order floor should prevent toy orders, got qty={qty}"
    print("  ✓ _calc_qty enforces meaningful order floor")

if trader and hasattr(trader, "_reentry_block_status"):
    real_recent_trades = trader.trade_ledger.recent_trades
    try:
        trader.trade_ledger.recent_trades = lambda limit=300: [{
            "symbol": "SMCI",
            "side": "sell",
            "recorded_at": trader.datetime.now(trader.timezone.utc).isoformat(),
            "exit_reason": "rotation_out",
        }]
        blocked, reason = trader._reentry_block_status("SMCI")
        assert blocked, "same-day rotation exit should block immediate re-entry"
        assert "next trading session" in reason.lower(), f"unexpected cooldown message: {reason}"
        print("  ✓ same-day rotation exits block re-entry")
    finally:
        trader.trade_ledger.recent_trades = real_recent_trades

if trader and hasattr(trader, "_entry_limit_block_status"):
    real_recent_trades = trader.trade_ledger.recent_trades
    try:
        trader.trade_ledger.recent_trades = lambda limit=300: [{
            "symbol": "HOOD",
            "side": "buy",
            "recorded_at": trader.datetime.now(trader.timezone.utc).isoformat(),
        }]
        blocked, reason = trader._entry_limit_block_status("HOOD")
        assert blocked, "daily symbol entry limit should block repeated same-day entries"
        assert "entry slot" in reason.lower(), f"unexpected entry limit message: {reason}"
        print("  ✓ same-day symbol entry cap blocks churn")
    finally:
        trader.trade_ledger.recent_trades = real_recent_trades

sm = modules.get("strategy_model")
if sm:
    migrated = sm.sanitize_model({"version": 2})
    assert migrated["version"] >= 4, f"expected v4 migration, got {migrated['version']}"
    assert migrated["max_positions"] <= 3, f"expected concentrated posture, got {migrated['max_positions']}"
    assert migrated["max_holding_days"] <= 1, f"expected faster exit posture, got {migrated['max_holding_days']}"
    assert migrated["position_size_pct"] >= 0.16, f"expected meaningful sizing, got {migrated['position_size_pct']}"
    print("  ✓ strategy_model migrates into concentrated alpha defaults")

# ── 6. Broker fill ledger is idempotent and uses FIFO realized P&L ──────────
try:
    import trade_ledger
    real_ledger_path = trade_ledger.LEDGER_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        trade_ledger.LEDGER_PATH = Path(tmpdir) / "trades.db"
        orders = [
            {"id": "buy-1", "symbol": "AAPL", "side": "buy", "status": "filled",
             "filled_qty": 10, "filled_avg_price": 100, "filled_at": "2026-01-01T15:00:00+00:00"},
            {"id": "sell-1", "symbol": "AAPL", "side": "sell", "status": "filled",
             "filled_qty": 10, "filled_avg_price": 105, "filled_at": "2026-01-01T16:00:00+00:00"},
        ]
        first = trade_ledger.reconcile_broker_orders(orders)
        second = trade_ledger.reconcile_broker_orders(orders)
        rows = trade_ledger.recent_trades(limit=10)
        sell = next(row for row in rows if row["side"] == "sell")
        assert first["inserted"] == 2 and second["inserted"] == 0, (first, second)
        assert len(rows) == 2, rows
        assert sell["pnl"] == 50.0, sell
        assert all(row["source"] == "alpaca_fill" for row in rows), rows
    trade_ledger.LEDGER_PATH = real_ledger_path
    print("  ✓ broker fill reconciliation is idempotent with FIFO P&L")
except Exception as e:
    errors.append(f"broker fill reconciliation: {e}")
    print(f"  ✗ broker fill reconciliation: {e}")

# ── Result ────────────────────────────────────────────────────────────────────
print()
if errors:
    print(f"SMOKE TEST FAILED — {len(errors)} error(s):")
    for e in errors:
        print(f"  • {e}")
    sys.exit(1)
else:
    print("SMOKE TEST PASSED")
    sys.exit(0)
