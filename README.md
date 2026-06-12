# Alpaca Trading Agent

Autonomous paper-trading system running on Google Kubernetes Engine. Scores equities across six technical indicators, detects the current market regime, and places/exits positions through Alpaca's paper-trading API. A Flask API serves a web dashboard and an iOS companion app.

**Safety hardcoded**: `PAPER = True` in `config.py`. Live trading requires both a separate `ALPACA_LIVE_KEY` env var and the confirmation string `"CONFIRM LIVE"` typed at runtime — pod restarts always revert to paper.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Google Kubernetes Engine            │
│                                                      │
│  ┌──────────────────┐    ┌───────────────────────┐  │
│  │  Deployment      │    │  CronJob              │  │
│  │  server.py       │    │  trader.py            │  │
│  │  (Flask API)     │    │  every 5 min M–F      │  │
│  │  port 5001       │    │  concurrency: Forbid  │  │
│  └────────┬─────────┘    └───────────┬───────────┘  │
│           │                          │               │
│           └──────────┬───────────────┘               │
│                      │  PVC (state/, trade ledger)   │
└──────────────────────┼───────────────────────────────┘
                       │
          ┌────────────┼────────────┐
          │            │            │
    Web browser   iOS app     Alpaca API
   portfolio_lab  SwiftUI    paper-trading
```

**`server.py`** — Flask API (always running). Serves the web dashboard, answers iOS polling, runs the Gemini chat agent, manages trading controls.

**`trader.py`** — CronJob that fires every 5 minutes on weekdays. Runs signal scoring, checks regime, applies all kill gates, then enters or exits positions.

**Shared modules**: `signals.py` (scoring), `strategy_model.py` (model state + optimizer), `backtest.py` (variant replay), `alpaca_client.py` (broker abstraction), `trade_ledger.py` (SQLite log), `notify.py` (push + webhook).

**State persistence**: a Kubernetes PVC mounted at `/data/state` holds model JSON, position memory, event log, peak-equity tracker, and trader control flags. All survive pod restarts.

---

## Trading Strategy

### Universe

| Category | Symbols |
|---|---|
| Bull 3× ETFs | TQQQ, SOXL, FNGU, LABU |
| Bear ETFs | SQQQ, UVXY, SPXS |
| Momentum stocks | HOOD, COIN, PANW, CRWD, NVDA, AMD, TSLA, META, GOOGL, AMZN, MSFT, AAPL, MSTR, PLTR, SOFI, SMCI, SHOP, SNOW, NET, and others |

### Signal Scoring (0–100)

Every symbol is scored across six indicators; scores sum to 100.

| Indicator | Weight | Logic |
|---|---|---|
| RSI(14) | 20 pts | Sweet zone 52–70 bull, <45 bear |
| MACD histogram | 22 pts | Direction + slope; positive & rising = bullish |
| AVWAP | 22 pts | Anchored VWAP from swing low/high; price above = institutional support |
| EMA 9/21 | 18 pts | EMA9 > EMA21 = uptrend |
| Trendline | 18 pts | Slope + price position relative to trendline |
| Price action | 12 pts | Reversal candles, breakouts/breakdowns, gaps, volume confirmation |

**Entry threshold**: score ≥ 65 (paper default). Bear ETFs use a lower threshold of 50.

### Regime Detection

`detect_regime()` classifies the market into BULL, BEAR, or CHOPPY by scoring:

- **Momentum**: QQQ/SPY intraday change — ≥ +0.4% → BULL, ≤ −0.5% → BEAR
- **EMA trend**: SPY EMA9 vs EMA21
- **VIX level**: < 18 = calm, 18–25 = elevated, > 25 = stressed
- **ATR ranging**: high ATR relative to price = volatile/ranging
- **Breadth**: RSP vs SPY divergence, sector count, internal breadth percentage

Regime affects position sizing (CHOPPY → 50% size) and bear ETF eligibility.

### Strategy Variants

The model optimizer backtests six parameter profiles and proposes the best-performing one (human must accept):

| Variant | Character |
|---|---|
| `current` | Default balanced profile |
| `aggressive` | Lower conviction threshold, higher sizing |
| `avwap_heavy` | AVWAP indicator weighted higher |
| `momentum_heavy` | RSI/MACD weighted higher |
| `faster_exit` | Tighter trailing stop, shorter max hold |
| `defensive` | Higher conviction threshold, smaller sizing |

---

## Risk Management

### Position Sizing
- Per-position size: 3% of portfolio equity
- Single-name cap: 25% of portfolio
- Max open positions: 5
- Max positions per sector: 2
- Regime multiplier: CHOPPY = 50% size

### Kill Gates (block entry)

| Gate | Threshold |
|---|---|
| Stale quote | Last trade > 120 seconds old |
| Wide spread | Bid/ask > 0.5% of price |
| Consecutive losses | 3 back-to-back losses halts entries |
| Daily loss kill | Portfolio down > −2% on the day |
| Opening window | No entries in first 30 min after open |
| EOD window | No entries in last 30 min before 3:40 PM ET |
| Earnings filter | No entry within 2 days of earnings date |
| Post-loss cooldown | Per-symbol cooldown after a losing trade |

### Exit Stack (priority order)
1. **Hard trailing stop**: 3% drawdown from position peak
2. **EOD flatten**: all positions closed by 3:40 PM ET daily
3. **Regime flip**: exit if market regime reverses against position direction
4. **Max hold**: 2 days maximum holding period
5. **Protect-gains**: 5% drawdown from portfolio peak triggers defensive exits
6. **Profit target**: configurable via model params

---

## Deployment

### Prerequisites
- GCP project with GKE cluster
- Artifact Registry or GCR for container images
- CyberArk Conjur for secrets (or equivalent — update `k8s/secret.yaml`)
- `gcloud` CLI authenticated

### Secrets

Injected at pod start via CyberArk Secrets Provider (maps Conjur paths → K8s secret keys):

```
ALPACA_PAPER_KEY    → personal-secrets/alpaca/paper-key
ALPACA_PAPER_SECRET → personal-secrets/alpaca/paper-secret
GEMINI_API_KEY      → personal-secrets/google/gemini/api-key
NOTIFY_WEBHOOK_URL  → personal-secrets/alpaca/notify-webhook  (optional)
```

No plaintext credentials are ever stored in Git.

### Special-Sauce Files (NOT in Git — gitignored)

These files contain the core strategy IP and must never be committed:

```
signals.py          ← signal scoring engine
trader.py           ← trading loop + kill gates
backtest.py         ← variant optimizer
config.py           ← runtime config + safety flags
strategy_model.py   ← model state + optimizer bounds
```

Backed up to iCloud only:
```
~/Library/Mobile Documents/com~apple~CloudDocs/alpaca-trader-sauce/
```

### Build & Deploy

```bash
./deploy.sh
```

`deploy.sh` runs `gcloud builds submit`, tags the image `gcr.io/<PROJECT>/alpaca-trader:latest`, applies K8s manifests, and waits for rollout. Cloud Build (~4 min) runs `test_smoke.py` — a failed smoke test aborts the deploy before the image is pushed.

The Dockerfile copies `portfolio_lab.html` into the image so the web dashboard is served from Flask with no separate static file server.

### Kubernetes Resources

| Resource | Purpose |
|---|---|
| `Deployment` alpaca-server | Flask API, 1 replica, Recreate strategy |
| `CronJob` alpaca-trader | trader.py, `*/5 * * * 1-5`, NY timezone, Forbid concurrency |
| `Service` LoadBalancer | Exposes port 5001 externally |
| `PVC` alpaca-trader-state | Persistent state across restarts |
| `ConfigMap` | Non-secret env vars: DATA_FEED, STATE_DIR, FLASK_HOST/PORT |
| `Secret` | Populated by Conjur at pod start |

### CronJob Details
- Schedule: `*/5 * * * 1-5` (every 5 minutes, Mon–Fri)
- Timezone: `America/New_York`
- `concurrencyPolicy: Forbid` — skips tick if previous one is still running
- `activeDeadlineSeconds: 240` — kills job if it runs > 4 minutes

---

## Web Dashboard (`portfolio_lab.html`)

Single-file HTML/CSS/JS served at `/` and `/lab`. No build step — Flask serves it directly.

### Layout
- **Sidebar** (left, 240px): account stats at top, then positions/watchlist panel below
- **Main area**: chat interface with message history and quick-action buttons

### Features

**Positions / Watchlist panel**: when holdings exist, shows each position with P&L and portfolio weight. When flat (no holdings), switches to WATCHLIST mode showing the top signal candidates with score badge, price, change%, and top reason. Identical behavior to the iOS app.

**Signal drill-down modal**: click any position or watchlist row to open a detail view showing:
- Symbol + BUY/SELL pill + current price + change%
- "WHY SCORE X?" — per-indicator breakdown (colored dot + name + label + pts earned/max weight)
- News articles with headline, source, timestamp, and link
- Signal factor reasons

**Chat**: Gemini-powered (`gemini-2.5-flash`). Understands natural language questions about P&L, positions, signals, regime, recent orders, model settings. Handles commands like "exit all", "place orders", "run scan". Falls back to keyword matching if `GEMINI_API_KEY` is unavailable.

**Trading controls**: Auto on/off toggle, Paper/Live mode toggle (requires typing "CONFIRM LIVE"), forced signal scan.

**Health alerts**: banner for critical errors and backtest drift warnings.

### API Key
`API_KEY = 'CHANGE_ME'` in the HTML — set it to match `LAB_API_KEY` in the pod environment. If `LAB_API_KEY` is unset in the pod, all requests pass auth (local dev).

---

## iOS App

SwiftUI app in `ios/AlpacaAgent/`. Polls `/api/lab/overview` every 30 seconds via `AgentViewModel`.

### Key Views

| View | Purpose |
|---|---|
| Dashboard | Equity, daily P&L, regime, market status |
| `RobinhoodPositionsList` | Positions (holdings exist) or Watchlist (flat) — tap any row |
| `StockDetailSheet` | Full signal drill-down: score breakdown, news, reasons |
| Activity feed | Recent fills, events, model changes |
| Model tab | Strategy params, variant win rates, accept/reject optimizer proposals |
| Trading controls | Auto-trading pause, paper/live mode toggle |

### Signal Display Logic
- Score ≥ `minConviction` → **hot** badge (green)
- Score ≥ 85% of `minConviction` → **warm** badge (amber)
- Below → **cold** badge (gray)

### Push Notifications
APNs alerts on: fills (buy/sell), trailing stop hits, regime flips, consecutive loss halts, daily loss kill, critical errors.

### Key Files

| File | Purpose |
|---|---|
| `AgentViewModel.swift` | Data layer; polls API, parses all JSON, owns `signalInsights` |
| `ContentView.swift` | Root view; `RobinhoodPositionsList`, `StockDetailSheet` |
| `Models.swift` | `SignalInsight`, `PositionRow`, `SignalIndicator`, `SignalNewsItem`, etc. |

---

## Configuration

### `config.py` (not in Git)

| Setting | Default | Notes |
|---|---|---|
| `PAPER` | `True` | **Hardcoded** — never set to False in this file |
| `IGNORED_POSITIONS` | `["DAWN"]` | DAWN is stuck/delisted; Alpaca returns 422 on close. Excluded from all P&L and signal logic. |
| `PROTECT_GAINS_DRAWDOWN_PCT` | 5.0% | Defensive exits if portfolio drops 5% from peak |

### `strategy_model.py` DEFAULT_MODEL (not in Git)

| Param | Default |
|---|---|
| `min_conviction` | 63 |
| `position_size_pct` | 5% |
| `trailing_stop_pct` | 3.0% |
| `max_holding_days` | 2 |
| `exit_on_regime_flip` | True |
| `learning_mode` | `"paper_safe"` |

### `trader.py` Key Constants (not in Git)

| Constant | Value |
|---|---|
| EOD flatten | 3:40 PM ET |
| Position size | 3% of equity |
| Trailing stop | 3% from peak |
| Daily loss kill | −2% |
| Consecutive loss halt | 3 losses |
| Stale quote gate | 120 seconds |
| Max spread | 0.5% |
| No-entry opening window | 30 min |
| No-entry EOD window | 30 min |

---

## Trade Ledger

SQLite at `$STATE_DIR/trades.db`. Append-only — rows are never updated or deleted.

```sql
CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     TEXT,    -- ISO-8601 UTC
    symbol          TEXT,
    side            TEXT,    -- "buy" | "sell"
    qty             REAL,
    price           REAL,
    notional        REAL,
    pnl             REAL,    -- realized P&L on sells
    exit_reason     TEXT,    -- "trailing_stop" | "eod_flat" | "regime_flip" | "max_hold" | "profit_target"
    regime          TEXT,    -- market regime at trade time
    model_gen       INTEGER, -- strategy model generation
    signal_score    REAL,    -- entry signal score (0–100)
    signal_snapshot TEXT,    -- JSON: full signal breakdown at entry
    model_snapshot  TEXT     -- JSON: model params at entry
);
```

Accessible via `GET /api/lab/ledger`. Used by the variant optimizer for performance attribution.

---

## API Reference

All endpoints require `X-API-Key: <LAB_API_KEY>` header (omit if `LAB_API_KEY` not set in pod).

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Health check + broker connectivity |
| GET | `/api/lab/overview` | Full snapshot: positions, signals, regime, model, narrative |
| GET | `/api/lab/activity` | Recent fills and events |
| GET | `/api/lab/portfolio/history` | Equity curve data |
| GET | `/api/lab/agents/decision` | Current agent decision |
| GET | `/api/lab/model` | Strategy model state |
| POST | `/api/lab/model/learn` | Accept optimizer proposal |
| POST | `/api/lab/backtest` | Run variant optimizer |
| GET | `/api/lab/variants` | Variant win-rate history |
| GET | `/api/lab/ledger` | Trade history from SQLite |
| GET/POST | `/api/lab/orders/preview` | Preview proposed orders |
| POST | `/api/lab/orders/place` | Place orders (paper; live requires confirmation) |
| GET | `/api/lab/news` | Cached news for held/candidate symbols |
| POST | `/api/lab/live-scores/trigger` | Force live signal re-score |
| GET | `/api/lab/score/<symbol>` | On-demand full signal score for any ticker |
| POST | `/api/lab/chat/message` | Gemini chat `{"text": "..."}` — supports `signal_insight` in response |
| GET | `/api/lab/trader/control` | Get pause/paper state |
| POST | `/api/lab/trader/pause` | Pause auto-trading |
| POST | `/api/lab/trader/resume` | Resume auto-trading |
| POST | `/api/lab/trader/mode` | Switch paper/live (requires confirmation string) |
| POST | `/api/lab/push/register` | Register APNs device token |
| GET | `/api/lab/gaps` | Pre-market gap scan |
| GET | `/api/lab/analytics/pnl-attribution` | P&L breakdown by signal factor |
| GET | `/api/lab/analytics/drift` | Backtest vs live drift alert |
| DELETE | `/api/lab/events` | Clear event log |
| GET | `/` or `/lab` | Web dashboard (portfolio_lab.html) |

---

## Local Development

```bash
# Create .env from template
cp .env.example .env   # add ALPACA_PAPER_KEY, ALPACA_PAPER_SECRET, GEMINI_API_KEY

# Run Flask server
python server.py
# Dashboard at http://localhost:5001

# Run one trader tick (paper only, PAPER=True hardcoded)
python trader.py

# Smoke test (same check run at Docker build time)
python test_smoke.py
```

Docker local run:
```bash
docker build -t alpaca-trader .
docker run -p 5001:5001 --env-file .env alpaca-trader
```

---

## Notifications

**macOS (dev)**: iMessage via AppleScript to configured phone number.

**Linux / GKE (prod)**: stdout (captured by K8s logs) + HTTP webhook (`NOTIFY_WEBHOOK_URL` env var) + APNs push to registered iOS devices.

Events that trigger notifications: fills, trailing stop hits, regime flips, consecutive loss halts, daily loss kill, critical errors.

---

## Security

- `PAPER = True` is a hardcoded constant, not an env var
- Live trading additionally requires `ALPACA_LIVE_KEY` env var (separate from paper key) AND runtime confirmation string `"CONFIRM LIVE"` — pod restart always reverts to paper
- Strategy source files are gitignored; backed up to iCloud only
- No plaintext secrets in Git — all credentials injected by Conjur at pod start
- `DAWN` is permanently in `IGNORED_POSITIONS` — removing it breaks P&L accounting and Alpaca returns 422 on close attempts
