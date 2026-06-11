"""
Smoke tests — run at Docker build time to catch crashes before deploy.
Validates imports, attribute references, and key function signatures.
Any failure = non-zero exit = build fails.
"""
import sys
import os

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
    for fn in ["_conviction_size_mult", "_calc_qty", "_holding_hours", "_check_trailing_stops", "main", "fetch_quotes"]:
        if not hasattr(trader, fn):
            errors.append(f"trader.{fn} — MISSING")
            print(f"  ✗ trader.{fn} missing")
        else:
            print(f"  ✓ trader.{fn}")

# ── 4. Conviction multiplier sanity ───────────────────────────────────────────
if trader and hasattr(trader, "_conviction_size_mult"):
    fn = trader._conviction_size_mult
    assert fn(95) == 4.0,   f"score 95 should be 4x, got {fn(95)}"
    assert fn(85) == 2.5,   f"score 85 should be 2.5x, got {fn(85)}"
    assert fn(75) == 1.5,   f"score 75 should be 1.5x, got {fn(75)}"
    assert fn(60) == 1.0,   f"score 60 should be 1x, got {fn(60)}"
    print("  ✓ _conviction_size_mult values correct")

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
