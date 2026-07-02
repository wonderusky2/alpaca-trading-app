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
  echo "→ Building image via Cloud Build (no Docker Desktop needed): ${FULL_IMAGE}"
  gcloud builds submit \
    --project="${GCP_PROJECT}" \
    --tag="${FULL_IMAGE}" \
    .

  echo "→ Tagging :latest (no rebuild)..."
  gcloud container images add-tag "${FULL_IMAGE}" "${LATEST_IMAGE}" --quiet

  echo "✓ Image pushed: ${FULL_IMAGE} + ${LATEST_IMAGE}"
fi

# ── 2. Apply K8s manifests ────────────────────────────────────────────────────
if $APPLY; then
  echo "→ Applying K8s manifests..."

  # Substitute the real project ID into manifests on-the-fly
  # SKIP secret.yaml — it has no data (Conjur-managed); applying it wipes live keys
  for f in k8s/*.yaml; do
    [[ "$f" == *"secret.yaml" ]] && continue
    sed "s|gcr.io/pure-tribute-440710-r8|gcr.io/${GCP_PROJECT}|g" "$f" \
      | kubectl apply -f -
  done

  echo "✓ Manifests applied."

  # Force rollout so the new :latest image is actually pulled.
  # kubectl apply on an unchanged manifest is a no-op even with imagePullPolicy:Always.
  if $BUILD; then
    echo "→ Rolling restart to pick up new image..."
    kubectl rollout restart deployment/alpaca-server --namespace alpaca-trader
  fi

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
    echo "⚠ Conjur dir not found — secret NOT updated."
    # Fail loudly if the cluster has no usable secret at all; otherwise the pod
    # comes up with empty Alpaca keys and QA "passes" against a broker-less server.
    if ! kubectl get secret alpaca-trader-secrets -n alpaca-trader >/dev/null 2>&1; then
      echo "✗ Cluster secret 'alpaca-trader-secrets' missing and Conjur unavailable to seed it — aborting."
      exit 1
    fi
    echo "  Existing cluster secret found — continuing with current keys. Re-seed manually if keys changed."
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

# ── 4. Post-deploy QA ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/qa_agent.py" ]] && ( $APPLY || $RESTART ); then

  # Wait for Flask to actually accept connections before running QA.
  # kubectl rollout status goes green as soon as the container starts,
  # but Flask takes a few more seconds to bind its port.
  echo "→ Waiting for Flask to be ready..."
  # Derive the live LB IP instead of hardcoding it — survives IP changes.
  SERVER_IP=$(kubectl get svc alpaca-server -n alpaca-trader \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  if [[ -z "${SERVER_IP}" ]]; then
    echo "✗ Could not resolve alpaca-server LoadBalancer IP — skipping QA."
    exit 1
  fi
  SERVER_URL="http://${SERVER_IP}:5001/api/lab/health"
  READY=false
  for i in $(seq 1 24); do            # up to 2 minutes (24 × 5s)
    if curl -sf --max-time 4 "${SERVER_URL}" > /dev/null 2>&1; then
      READY=true
      echo "✓ Server is ready (attempt ${i})."
      break
    fi
    echo "  … not ready yet (attempt ${i}/24) — waiting 5s"
    sleep 5
  done
  if ! $READY; then
    echo "✗ Server did not become ready after 2 min — skipping QA."
    exit 1
  fi

  echo ""
  echo "→ Running post-deploy QA suite..."
  # QA_SERVER_URL points qa_agent at the live pod — default is localhost which doesn't work in CI.
  # --fix is intentionally omitted: post-deploy QA must never mutate live pod state.
  if QA_SERVER_URL="http://${SERVER_IP}:5001" python3 "${SCRIPT_DIR}/qa_agent.py" --pod; then
    echo "✓ QA passed."
  else
    echo ""
    echo "✗ QA FAILED — review output above before shipping."
    echo "  To re-run: python3 qa_agent.py --pod --fix"
    exit 1
  fi
fi

# ── 5. Port-forward hint ──────────────────────────────────────────────────────
echo ""
echo "Access the dashboard (cluster is behind firewall — use port-forward):"
echo "  kubectl port-forward svc/alpaca-server 5001:5001 -n alpaca-trader"
echo "  open http://localhost:5001/lab"
