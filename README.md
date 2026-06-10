# Alpaca Paper Trader

Algorithmic paper trading system built on Alpaca's API, deployed on GKE (Google Kubernetes Engine). Scores momentum stocks using a 6-signal confluence engine, detects market regime, and manages entries/exits with ATR-based stops.

---

## Architecture

```
signals.py          — regime detection, signal scoring engine, universe definition
trader.py           — main trading loop, exit logic, daily optimizer
alpaca_client.py    — Alpaca REST wrapper (snapshots, orders, bars)
backtest.py         — walk-forward backtester for strategy variant selection
strategy_model.py   — hyperparameter definitions, bounds, and variant profiles
config.py           — runtime config (API keys via Conjur, risk params)
server.py / app.py  — Flask API + macOS companion app
k8s/                — Kubernetes manifests (deployment, service, configmap, etc.)
ios/                — iOS SwiftUI companion app (AlpacaAgent)
```

**Deployment:** `gcloud builds submit` → Cloud Build → `gcr.io/.../alpaca-trader:latest` → `kubectl rollout restart` on GKE (no local Docker required).

---

## Trading Universe

| Bucket | Symbols |
|---|---|
| Bull ETFs | TQQQ, SOXL, FNGU, LABU |
| Bear ETFs | SQQQ, UVXY, SPXS |
| Momentum stocks | NVDA, AMD, ARM, SMCI, MRVL, META, GOOGL, AMZN, MSFT, AAPL, TSLA, PLTR, CRWD, PANW, NET, MSTR, COIN, HOOD, RKLB, IONQ, RGTI, BBAI, SOFI, AFRM |
| Breadth instruments | RSP, XLK, XLF, XLI, XLY *(fetched for regime validation, never traded)* |

---

## Signal Scoring (0–100)

Six indicators contribute to a confluence score. All weights sum to 100.

| Indicator | Default weight | What it measures |
|---|---|---|
| RSI 14 | 20 | Momentum zone (52–76 = sweet spot for longs) |
| MACD histogram | 22 | Direction + slope of momentum |
| Anchored VWAP | 22 | Institutional price anchor (low/high pivot) |
| EMA 9/21 | 18 | Trend direction and spread |
| Trendline | 18 | 30-bar slope and price position relative to trend |
| Price action | 12 | Candle patterns, structure breaks, gaps, volume |

**Entry threshold:** `min_conviction = 65` (default). Bear ETFs use `bear_etf_min_conviction = 50` in BEAR regime — their own technicals lag at the start of a down move; the regime call carries the primary signal.

**Hard veto:** MACD negative and falling → no entry regardless of total score.

---

## Regime Detection

`detect_regime()` in `signals.py` evaluates five factors:

1. **Intraday momentum** — QQQ + SPY `change_pct` average
2. **Multi-day trend** — SPY EMA9 vs EMA21 spread
3. **VIX level** — `<18` calm, `18–25` elevated, `>25` stressed
4. **Directional range** — SPY ATR% vs trendline slope (wide range + flat slope = whipsaw)
5. **Market breadth** *(new)*:
   - **RSP vs SPY divergence**: if SPY is up ≥0.4% but RSP lags by >0.3%, the rally is mega-cap-driven
   - **Sector confirmation**: requires ≥3/4 of XLK, XLF, XLI, XLY to be green for a BULL call
   - **Internal breadth**: fraction of momentum stocks trading above their intraday VWAP

| Regime | Behavior |
|---|---|
| **BULL** | Momentum up + trend confirms + VIX calm/elevated + breadth broad |
| **BEAR** | Momentum down — bear ETFs (SQQQ/SPXS/UVXY) traded as long positions |
| **CHOPPY** | Flat momentum, VIX stressed with no direction, trend/momentum disagree, or narrow SPY rally not confirmed by breadth |

**CHOPPY blocks all new entries.** Exits are still monitored on existing positions.

---

## Exit Logic

`_check_trailing_stops()` evaluates 7 ordered conditions per position:

1. **Hard stop** — price below ATR-based stop (1.5× ATR from entry, max 5%)
2. **Signal reversal** — confluence score drops below 50
3. **Below day's open** — price trades under the opening price of the day
4. **Below EMA21** — price crosses under the 21-day EMA
5. **Below VWAP** — price crosses under intraday VWAP
6. **Profit lock** — trailing stop triggered after position gains ≥1% (locks in gains)
7. **EOD flatten** — all positions closed before market close

**Sell cooldown:** 30 minutes after a sell before the same symbol can be re-entered.

---

## Strategy Variants & Optimizer

Six named profiles with different indicator weights:

| Profile | Character |
|---|---|
| `current` | Live model params |
| `aggressive` | Higher MACD weight, lower conviction threshold (60), 1.3× size |
| `avwap_heavy` | AVWAP raised to 31, suits trending days |
| `momentum_heavy` | EMA/MACD dominant, conviction 63 |
| `faster_exit` | Tighter stops, 1-day hold limit |
| `defensive` | High conviction (78), ETFs only, 0.6× size, max 2 positions |

**Daily optimizer** (`_run_daily_optimization`): runs at most weekly (≥7 days since last run), backtests over a 3-month period, and requires a ≥3.0 objective margin before switching from the current profile. Prevents overfitting to recent noise.

---

## Risk & Position Sizing

- `MAX_POSITIONS = 5`
- Position size = `(portfolio_value / MAX_POSITIONS) × regime_size_multiplier × profile_size_mult`
- ATR stop = `entry_price − (ATR14 × 1.5)`, floored at `entry_price × 0.95`
- Protect-gains mode: if equity retreats 5% from peak, de-risk; re-entry when giveback ≤ 4.5%

---

## Deployment

```bash
# Build and push to GCR via Cloud Build (no local Docker)
gcloud builds submit --tag gcr.io/$(gcloud config get-value project)/alpaca-trader:latest .

# Restart the GKE pod
kubectl rollout restart deployment/alpaca-server -n alpaca-trader
kubectl rollout status deployment/alpaca-server -n alpaca-trader
```

Secrets (Alpaca API keys, Gemini key) are loaded from **Conjur** via environment variables at pod startup. `PAPER=True` is hardcoded — live trading is never enabled.

---

## Configuration (`config.py`)

| Key | Default | Description |
|---|---|---|
| `PAPER` | `True` | Always paper — never live |
| `ALPACA_DATA_FEED` | `"iex"` | IEX (free); change to `"sip"` with subscription |
| `IGNORED_POSITIONS` | `["DAWN"]` | Phantom/delisted symbols to exclude from all logic |
| `PROTECT_GAINS_DRAWDOWN_PCT` | `5.0` | De-risk trigger (% retreat from equity peak) |
| `PROTECT_GAINS_REDEPLOY_GROSS_PCT` | `10.0` | Already de-risked threshold |
| `REENTRY_RECOVERY_GIVEBACK_PCT` | `4.5` | Re-entry allowed once giveback ≤ this |

---

## Data Feed Notes

- Primary: **Alpaca IEX** (free tier) — 15-min bars during market hours, 1-day bars otherwise
- Fallback 1: Alpaca 1-hour bars for symbols with insufficient intraday data
- Fallback 2: Alpaca 1-day bars
- Fallback 3: **yfinance** 1-hour bars for symbols still missing after Alpaca attempts
- VIX may not be available on IEX — `vix_regime` falls back to `"unknown"` which allows trading (fails open, not closed)
