#!/usr/bin/env bash
# run.sh — Load secrets then execute trader.py
# Called by launchd every 5 minutes.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/trader.log"
IVSENTINEL_ENV="${IVSENTINEL_ENV:-$HOME/Code/.ivsentinel.env}"
CONJUR_EXPORT_CMD="${CONJUR_EXPORT_CMD:-}"
CONJUR_DIR="${CONJUR_DIR:-$HOME/Code/conjur-secret-manager}"
K8S_SECRET_NAME="${K8S_SECRET_NAME:-alpaca-trader-secrets}"
K8S_NAMESPACE="${K8S_NAMESPACE:-alpaca-trader}"

if [[ -x "$DIR/venv_server/bin/python" ]]; then
  VENV="$DIR/venv_server/bin/python"
elif [[ -x "$DIR/venv/bin/python" ]]; then
  VENV="$DIR/venv/bin/python"
else
  VENV="$(command -v python3)"
fi

load_local_env() {
  source "$IVSENTINEL_ENV" 2>/dev/null || true

  if [[ -n "$CONJUR_EXPORT_CMD" ]]; then
    eval "$CONJUR_EXPORT_CMD" 2>/dev/null || true
  elif [[ -d "$CONJUR_DIR" ]]; then
    eval "$(cd "$CONJUR_DIR" && npm run --silent export 2>/dev/null)" || true
  fi
}

load_k8s_secret_fallback() {
  if [[ -n "${ALPACA_PAPER_KEY:-}" && -n "${ALPACA_PAPER_SECRET:-}" ]]; then
    return 0
  fi
  command -v kubectl >/dev/null 2>&1 || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  local secret_json
  secret_json="$(kubectl get secret "$K8S_SECRET_NAME" -n "$K8S_NAMESPACE" -o json 2>/dev/null || true)"
  [[ -n "$secret_json" ]] || return 0
  eval "$(
    SECRET_JSON="$secret_json" python3 <<'PY'
import base64
import json
import os
data = json.loads(os.environ["SECRET_JSON"]).get("data") or {}
for key in ("ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET", "GEMINI_API_KEY", "NOTIFY_WEBHOOK_URL"):
    val = data.get(key)
    if not val:
        continue
    decoded = base64.b64decode(val).decode("utf-8")
    print(f"export {key}={json.dumps(decoded)}")
PY
  )" 2>/dev/null || true
}

load_local_env
load_k8s_secret_fallback

echo "--- $(date) ---" >> "$LOG"
"$VENV" "$DIR/trader.py" >> "$LOG" 2>&1
