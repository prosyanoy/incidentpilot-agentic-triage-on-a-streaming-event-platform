#!/usr/bin/env bash
# One-command demo: kind cluster + image + Helm release + live incident report.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER="${CLUSTER:-incidentpilot}"
NS="${NAMESPACE:-incidentpilot}"
SCENARIO="${SCENARIO:-bad_deploy}"
IMAGE_REPO=incidentpilot
IMAGE_TAG=dev

for tool in docker kind kubectl helm; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 1; }
done

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --config deploy/kind-config.yaml
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

docker build -t "$IMAGE_REPO:$IMAGE_TAG" .
kind load docker-image "$IMAGE_REPO:$IMAGE_TAG" --name "$CLUSTER"

helm upgrade --install incidentpilot deploy/helm/incidentpilot \
  --namespace "$NS" --create-namespace \
  --set image.repository="$IMAGE_REPO" --set image.tag="$IMAGE_TAG" \
  --set generator.scenario="$SCENARIO" \
  --wait --timeout 240s

echo
echo ">>> replaying scenario '$SCENARIO' (20x speed, ~25s); waiting for the agent's report..."
kubectl -n "$NS" exec deploy/incidentpilot-api -- \
  incidentpilot show --url http://localhost:8000 --wait 180

echo
echo "API: http://localhost:8000/incidents   (tear down with: make demo-down)"
