#!/usr/bin/env bash
# Deploys CityPulse to the team namespace at http://<team host>/app.
# Credentials are read from the environment (or the single /config/*.config) and only ever
# passed to kubectl; they are never echoed or written to disk.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$(dirname "$HERE")"
KUBECTL="${KUBECTL:-$(command -v kubectl || echo "$HOME/.local/bin/kubectl")}"

if [ -z "${INGRESS_URL:-}" ] || [ -z "${PASSWORD:-}" ]; then
  mapfile -t CONFIGS < <(find /config -maxdepth 1 -type f -name '*.config' | sort)
  (( ${#CONFIGS[@]} == 1 )) || { echo "expected exactly one /config/*.config" >&2; exit 1; }
  set -a; source "${CONFIGS[0]}"; set +a
fi
: "${WANDB_API_KEY:?WANDB_API_KEY is not set}"
: "${WANDB_TEAM:?WANDB_TEAM is not set}"
: "${WANDB_PROJECT:?WANDB_PROJECT is not set}"

NS="${NS:-$USERNAME}"
APP_HOST="${INGRESS_URL#http://}"; APP_HOST="${APP_HOST#https://}"; APP_HOST="${APP_HOST%%/*}"
# Pods can't resolve the public team host; the backend ingress maps /api straight to this Service.
VSS_INTERNAL_URL="${VSS_INTERNAL_URL:-http://video-backend-service.$NS.svc.cluster.local:8000}"

code_args=()
for f in main.py settings.py vss.py llm.py director.py timeline.py render.py tts.py cli.py requirements.txt; do
  code_args+=("--from-file=$APP/$f")
done
"$KUBECTL" -n "$NS" create configmap toronto-cut-code "${code_args[@]}" --dry-run=client -o yaml | "$KUBECTL" apply -f -
"$KUBECTL" -n "$NS" create configmap toronto-cut-static --from-file="$APP/static" --dry-run=client -o yaml | "$KUBECTL" apply -f -

"$KUBECTL" -n "$NS" create secret generic toronto-cut-secrets \
  --from-literal=VSS_URL="$VSS_INTERNAL_URL" \
  --from-literal=VSS_USERNAME="$USERNAME" \
  --from-literal=VSS_PASSWORD="$PASSWORD" \
  --from-literal=WANDB_API_KEY="$WANDB_API_KEY" \
  --from-literal=WANDB_TEAM="$WANDB_TEAM" \
  --from-literal=WANDB_PROJECT="$WANDB_PROJECT" \
  --dry-run=client -o yaml | "$KUBECTL" apply -f -

# Pod storage is ephemeral; save films rendered on the cluster before the pod is replaced.
OLD_POD="$("$KUBECTL" -n "$NS" get pod -l app=toronto-cut --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [ -n "$OLD_POD" ]; then
  mkdir -p "$APP/data/renders"
  for id in $("$KUBECTL" -n "$NS" exec "$OLD_POD" -- ls /data/app/renders 2>/dev/null); do
    [ -e "$APP/data/renders/$id" ] || "$KUBECTL" -n "$NS" cp "$OLD_POD:/data/app/renders/$id" "$APP/data/renders/$id" >/dev/null
  done
fi

sed "s|\${APP_HOST}|$APP_HOST|g" "$HERE/k8s.yaml" | "$KUBECTL" -n "$NS" apply -f -
"$KUBECTL" -n "$NS" rollout restart deploy/toronto-cut
"$KUBECTL" -n "$NS" rollout status deploy/toronto-cut --timeout=300s

if compgen -G "$APP/data/renders/*/edl.json" >/dev/null; then
  POD="$("$KUBECTL" -n "$NS" get pod -l app=toronto-cut --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}')"
  "$KUBECTL" -n "$NS" exec "$POD" -- mkdir -p /data/app/renders
  for d in "$APP"/data/renders/*/; do
    [ -f "$d/edl.json" ] && "$KUBECTL" -n "$NS" cp "$d" "$POD:/data/app/renders/$(basename "$d")" >/dev/null
  done
fi
echo "CityPulse → http://$APP_HOST/app"
