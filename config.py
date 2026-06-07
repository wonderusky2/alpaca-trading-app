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
IGNORED_POSITIONS: list[str] = []

# ── Risk / protect-gains parameters ──────────────────────────────────────────
PROTECT_GAINS_ENABLED: bool = True
PROTECT_GAINS_DRAWDOWN_PCT: float = 5.0      # trigger when equity retreats 5% from peak
PROTECT_GAINS_REDEPLOY_GROSS_PCT: float = 10.0  # already de-risked threshold
REENTRY_RECOVERY_GIVEBACK_PCT: float = 4.5   # allow re-entry once giveback ≤ this

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
