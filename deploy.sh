#!/usr/bin/env bash
# ── deploy.sh — Build, push, and deploy alpaca-trader to GCP K8s ─────────────
#
# Usage:
#   ./deploy.sh                        # build + push + apply all manifests
#   ./deploy.sh --build-only           # build and push image only
#   ./deploy.sh --apply-only           # apply K8s manifests only (no rebuild)
#   ./deploy.sh --restart              # rolling restart of the server deployment
#
# Prerequisites:
#   gcloud auth configure-docker
#   kubectl configured to your GCP cluster
#
# Edit these two variables:
GCP_PROJECT="pure-tribute-440710-r8"
IMAGE="gcr.io/${GCP_PROJECT}/alpaca-trader"
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BUILD=true
APPLY=true
RESTART=false

for arg in "$@"; do
  case "$arg" in
    --build-only)  APPLY=false ;;
    --apply-only)  BUILD=false ;;
    --restart)     BUILD=false; APPLY=false; RESTART=true ;;
  esac
done

TAG=$(git rev-parse --short HEAD 2>/dev/null || echo "latest")
FULL_IMAGE="${IMAGE}:${TAG}"
LATEST_IMAGE="${IMAGE}:latest"

# ── 1. Build and push ─────────────────────────────────────────────────────────
if $BUILD; then
  echo "→ Building image: ${FULL_IMAGE}"
  docker build \
    --platform linux/amd64 \
    -t "${FULL_IMAGE}" \
    -t "${LATEST_IMAGE}" \
    .

  echo "→ Pushing to GCR..."
  docker push "${FULL_IMAGE}"
  docker push "${LATEST_IMAGE}"
  echo "✓ Image pushed: ${FULL_IMAGE}"
fi

# ── 2. Apply K8s manifests ────────────────────────────────────────────────────
if $APPLY; then
  echo "→ Applying K8s manifests..."

  # Substitute the real project ID into manifests on-the-fly
  for f in k8s/*.yaml; do
    sed "s|gcr.io/pure-tribute-440710-r8|gcr.io/${GCP_PROJECT}|g" "$f" \
      | kubectl apply -f -
  done

  echo "✓ Manifests applied."

  # ── Seed K8s secret from Conjur (Conjur init-container not installed) ──────
  CONJUR_DIR="${HOME}/Code/conjur-secret-manager"
  if [ -d "${CONJUR_DIR}" ]; then
    echo "→ Seeding K8s secret from Conjur..."
    eval "$(cd "${CONJUR_DIR}" && npm run --silent export 2>/dev/null)"
    kubectl create secret generic alpaca-trader-secrets \
      --from-literal=ALPACA_PAPER_KEY="${ALPACA_PAPER_KEY:-}" \
      --from-literal=ALPACA_PAPER_SECRET="${ALPACA_PAPER_SECRET:-}" \
      --from-literal=GEMINI_API_KEY="${GEMINI_API_KEY:-}" \
      --from-literal=NOTIFY_WEBHOOK_URL="${NOTIFY_WEBHOOK_URL:-}" \
      -n alpaca-trader \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "✓ Secret seeded."
  else
    echo "⚠ Conjur dir not found — secret NOT updated. Run manually if keys changed."
  fi

  # Wait for the server deployment to roll out
  echo "→ Waiting for rollout..."
  kubectl rollout status deployment/alpaca-server \
    --namespace alpaca-trader \
    --timeout=120s
  echo "✓ Server deployment is live."
fi

# ── 3. Restart only ───────────────────────────────────────────────────────────
if $RESTART; then
  echo "→ Rolling restart of alpaca-server..."
  kubectl rollout restart deployment/alpaca-server --namespace alpaca-trader
  kubectl rollout status deployment/alpaca-server --namespace alpaca-trader --timeout=120s
  echo "✓ Restart complete."
fi

# ── 4. Port-forward hint ──────────────────────────────────────────────────────
echo ""
echo "Access the dashboard (cluster is behind firewall — use port-forward):"
echo "  kubectl port-forward svc/alpaca-server 5001:5001 -n alpaca-trader"
echo "  open http://localhost:5001/lab"
