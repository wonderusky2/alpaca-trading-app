# ── Alpaca Trader — GCP container image ───────────────────────────────────────
# Single image for both server.py (Deployment) and trader.py (CronJob).
# Override CMD in the K8s manifest for the CronJob:
#   command: ["python", "trader.py"]
#
# Build:
#   docker build -t alpaca-trader .
# Run server locally:
#   docker run -p 5001:5001 --env-file .env alpaca-trader
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# System deps for alpaca-py and pandas wheels on slim images
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt1-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements-k8s.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY alpaca_client.py \
     backtest.py \
     config.py \
     experiment_monitor.py \
     learning_agent.py \
     logger.py \
     notify.py \
     portfolio.py \
     portfolio_lab.html \
     server.py \
     signals.py \
     strategy_model.py \
     trade_ledger.py \
     trader.py \
     test_smoke.py \
     ./

# ── Smoke test — fails the build if any import or attribute check blows up ────
RUN python test_smoke.py

# State directory (override with STATE_DIR env var; mount a PVC here)
RUN mkdir -p /data/state
ENV STATE_DIR=/data/state

# Flask port
EXPOSE 5001

# Default: run the API server
CMD ["python", "server.py"]
