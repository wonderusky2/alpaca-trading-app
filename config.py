"""
config.py — Runtime configuration for robinhood-trader.
All secrets come from the environment (loaded by run.sh via Conjur).
"""
from __future__ import annotations
import os

# ── Alpaca paper trading keys ─────────────────────────────────────────────────
ALPACA_API_KEY: str = os.environ.get("ALPACA_PAPER_KEY", "")
ALPACA_API_SECRET: str = os.environ.get("ALPACA_PAPER_SECRET", "")
PAPER: bool = True                   # always paper — never live
ALPACA_DATA_FEED: str = "iex"        # IEX (free); change to "sip" with subscription
MAX_DAILY_BAR_STALE_DAYS: int = 5    # reject stale daily bars older than this

# ── Positions to ignore (phantom/delisted holdings) ───────────────────────────
IGNORED_POSITIONS: list[str] = ["DAWN"]   # stuck/delisted — exclude from counts and scoring

# ── Risk / protect-gains parameters ──────────────────────────────────────────
PROTECT_GAINS_ENABLED: bool = True
PROTECT_GAINS_DRAWDOWN_PCT: float = 5.0      # trigger when equity retreats 5% from peak
PROTECT_GAINS_REDEPLOY_GROSS_PCT: float = 10.0  # already de-risked threshold
REENTRY_RECOVERY_GIVEBACK_PCT: float = 4.5   # allow re-entry once giveback ≤ this

# ── Alpha-first paper trader discipline ──────────────────────────────────────
# The goal is to take concentrated, high-conviction stock risk while staying
# alive on bad days. These values are intentionally more assertive than the old
# benchmark-hugging defaults, but still bounded for paper safety.
MAX_AUTONOMOUS_POSITIONS: int = 3
MAX_POSITION_SIZE_PCT: float = 0.18
MAX_NEW_ENTRIES_PER_TICK: int = 1
MIN_MINUTES_BETWEEN_BUYS: int = 15
CHOPPY_ENTRY_MIN_SCORE: int = 78
DAILY_LOSS_KILL_PCT: float = -1.5
MARKET_DOWN_BLOCK_PCT: float = -0.8
BULL_TARGET_GROSS_PCT: float = 0.55
ALPHA_MIN_ORDER_VALUE: float = 2_500.0
ALPHA_MIN_ORDER_PCT: float = 0.025
CORE_BENCHMARK_SYMBOL: str = "QQQ"
CORE_BENCHMARK_TARGET_PCT: float = 1.00
CORE_BENCHMARK_MAX_PCT: float = 1.00
ALPHA_ALLOWED_SYMBOLS: tuple[str, ...] = (
    "AAPL",
    "AFRM",
    "COIN",
    "CRWD",
    "HOOD",
    "META",
    "MRVL",
    "MSFT",
    "NET",
    "NVDA",
    "PLTR",
    "SOFI",
    "TSLA",
)
ALPHA_BLOCKED_SYMBOLS: tuple[str, ...] = (
    "AMD",
    "ARM",
    "ENPH",
    "FNGU",
    "FSLR",
    "LABU",
    "MSTR",
    "PANW",
    "QCOM",
    "QQQ",
    "RKLB",
    "RGTI",
    "SEDG",
    "SMCI",
    "SOXL",
    "TQQQ",
    "UVXY",
)
ALPHA_BEAR_STAY_IN_CASH: bool = True
ALPHA_DISABLE_REVERSION: bool = True
ALPHA_MIN_HOLD_MINUTES: int = 15
ALPHA_MAX_ENTRIES_PER_SYMBOL_PER_DAY: int = 1
ALPHA_NO_REENTRY_AFTER_SELL_TODAY: bool = True
ALPHA_FORCE_EXIT_DISALLOWED_POSITIONS: bool = True
ALPHA_UNWIND_OPTIONS: bool = True

# ── QQQ regime core ──────────────────────────────────────────────────────────
# Keep the benchmark allocator available for inspection/backtests, but do not
# let it drive the autonomous paper trader when alpha mode is active.
CORE_TRADER_ENABLED: bool = False
CORE_BULL_TARGET_PCT: float = 1.00    # fully deployed in bull
CORE_CHOPPY_TARGET_PCT: float = 0.95  # near-full deployment in choppy — covered calls collect premium on top
CORE_BEAR_TARGET_PCT: float = 0.30   # go defensive in bear — 70% cash/hedge
CORE_REBALANCE_DRIFT_PCT: float = 0.03
CORE_MIN_ORDER_VALUE: float = 1_000.0
CORE_HEDGE_SYMBOL: str = "SQQQ"
CORE_HEDGE_TARGET_PCT: float = 0.00
CORE_HEDGE_MAX_PCT: float = 0.15
CORE_HEDGE_BEAR_ONLY: bool = True
CORE_TRIM_SATELLITES_ENABLED: bool = False
SATELLITE_TRADING_ENABLED: bool = True

# ── Session / overnight handling ─────────────────────────────────────────────
# Alpha mode is allowed to hold overnight. Set true only for explicit intraday
# liquidation experiments.
EOD_FLAT_ENABLED: bool = False

# ── Legacy benchmark cleanup ─────────────────────────────────────────────────
# When alpha mode is active, any leftover QQQ core inventory or QQQ option
# overlays from the old benchmark system should be unwound before new alpha risk
# is added. This keeps the book honest: beat the benchmark, don't quietly hold it.
LEGACY_UNWIND_ENABLED: bool = True
LEGACY_BENCHMARK_SYMBOLS: tuple[str, ...] = ("QQQ",)
LEGACY_OPTION_UNDERLYINGS: tuple[str, ...] = ("QQQ",)

# ── QQQ options hedge lab ────────────────────────────────────────────────────
# Options are proposed as paper-only protection. The app should prove that puts
# beat cash/SQQQ hedges before live execution is ever considered.
OPTIONS_HEDGE_ENABLED: bool = False
OPTIONS_HEDGE_PAPER_ONLY: bool = True
OPTIONS_HEDGE_UNDERLYING: str = "QQQ"
OPTIONS_HEDGE_MIN_DTE: int = 21
OPTIONS_HEDGE_MAX_DTE: int = 60
OPTIONS_HEDGE_OTM_PCT: float = 0.05
OPTIONS_HEDGE_NOTIONAL_PCT: float = 0.25
OPTIONS_HEDGE_MAX_PREMIUM_PCT: float = 0.01

# ── QQQ directional call buying (BULL regime — max returns) ──────────────────
# Buy slightly-OTM QQQ calls in BULL regime to amplify upside beyond spot QQQ.
# Kept paper-only; must show positive expectancy before live consideration.
OPTIONS_CALL_BUY_ENABLED: bool = False
OPTIONS_CALL_BUY_PAPER_ONLY: bool = True
OPTIONS_CALL_BUY_UNDERLYING: str = "QQQ"
OPTIONS_CALL_BUY_MIN_DTE: int = 21         # minimum 3 weeks to avoid rapid theta decay
OPTIONS_CALL_BUY_MAX_DTE: int = 45         # cap at ~6 weeks for responsive delta
OPTIONS_CALL_BUY_OTM_PCT: float = 0.02     # target 2% OTM — high delta, affordable premium
OPTIONS_CALL_BUY_MAX_PREMIUM_PCT: float = 0.05   # never spend more than 5% of equity on premium
OPTIONS_CALL_BUY_ALLOW_CHOPPY: bool = False       # calls only in BULL — CHOPPY uses covered calls for income instead

# ── QQQ covered-call income lab ──────────────────────────────────────────────
# Covered calls collect premium by selling upside. Keep this paper-only until
# evidence shows it improves benchmark-relative results after assignment risk.
OPTIONS_CALL_INCOME_ENABLED: bool = False
OPTIONS_CALL_INCOME_PAPER_ONLY: bool = True
OPTIONS_CALL_UNDERLYING: str = "QQQ"
OPTIONS_CALL_MIN_DTE: int = 7
OPTIONS_CALL_MAX_DTE: int = 21
OPTIONS_CALL_OTM_PCT: float = 0.04
OPTIONS_CALL_MAX_OVERWRITE_PCT: float = 1.00
OPTIONS_CALL_MIN_PREMIUM_PCT: float = 0.001

# ── Options automation ────────────────────────────────────────────────────────
# Keep the options lab available, but do not auto-fire options orders while the
# stock alpha engine itself is still being validated.
OPTIONS_AUTOMATION_ENABLED: bool = False
OPTIONS_AUTOMATION_SUBMIT_ORDERS: bool = False
OPTIONS_AUTOMATION_INTERVAL_SECONDS: int = 15 * 60
OPTIONS_AUTOMATION_COOLDOWN_SECONDS: int = 60 * 60
EDGE_GATE_MIN_CLOSED_TRADES: int = 20
EDGE_GATE_MIN_EXPECTANCY: float = 0.0
ALPHA_EXPERIMENT_ID: str = "alpha_v4_attributed"
PROMOTION_MIN_PAPER_DAYS: int = 30
PROMOTION_MIN_CLOSED_TRADES: int = 100

# ── Rebalancing ───────────────────────────────────────────────────────────────
REBALANCE_DAYS: int = 1              # next rebalance window

# ── Performance ledger ────────────────────────────────────────────────────────
PERFORMANCE_LEDGER_MIN_INTERVAL_SECONDS: int = 300

# ── Notifications ─────────────────────────────────────────────────────────────
IMESSAGE_NOTIFY_EVENTS: bool = True  # send iMessage on orders/events

# ── Order submission guard ────────────────────────────────────────────────────
def fund_manager_order_submission_enabled() -> bool:
    """
    Paper orders from the dashboard are enabled by default.
    Set FUND_MANAGER_ORDER_SUBMISSION_ENABLED=false to lock the dashboard.
    """
    val = os.environ.get("FUND_MANAGER_ORDER_SUBMISSION_ENABLED", "true")
    return val.lower() not in ("false", "0", "no", "off")


def order_mutations_armed() -> bool:
    """True for paper account — no extra guard needed."""
    return True
