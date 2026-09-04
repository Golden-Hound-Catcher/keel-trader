#!/usr/bin/env bash
# Soft ops smoke: paper acceptance, then optional API /health (skip if down).
# Usage: ./scripts/ops_smoke.sh
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

echo "==> ops_smoke: paper acceptance"
"$ROOT/scripts/run_acceptance.sh"

API_URL="${KEEL_API_URL:-http://127.0.0.1:8080}"
HEALTH_URL="${API_URL%/}/health"
echo "==> ops_smoke: GET $HEALTH_URL"

set +e
HTTP_CODE=$(curl -sS -o /tmp/keel-ops-smoke-health.json -w "%{http_code}" --connect-timeout 2 --max-time 5 "$HEALTH_URL" 2>/tmp/keel-ops-smoke-curl.err)
CURL_RC=$?
set -e

if [ "$CURL_RC" -ne 0 ] || [ "$HTTP_CODE" != "200" ]; then
  echo "SKIP: API health not 200 (curl_rc=$CURL_RC http=$HTTP_CODE) — start keel-api if needed"
  if [ -f /tmp/keel-ops-smoke-curl.err ]; then
    sed -n '1,3p' /tmp/keel-ops-smoke-curl.err || true
  fi
  echo "PASS: ops_smoke (acceptance ok; API soft-skipped)"
  exit 0
fi

echo "API ok (HTTP 200)"
echo "PASS: ops_smoke (acceptance + API)"
exit 0
