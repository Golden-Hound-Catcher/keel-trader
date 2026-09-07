#!/usr/bin/env bash
# Start keel-api + keel-worker for Q0 live/paper observation (read-only hanging).
# Loads repo .env; does NOT disable KEEL_KILL_SWITCH. Idempotent: skips if already up.
# Usage: ./scripts/observe_up.sh
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

ENV_FILE="$ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: missing $ENV_FILE — copy env.example → .env and configure" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

export PYTHONPATH="${PYTHONPATH:-.}"
if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
elif [ -x "$ROOT/.venv/bin/python3" ]; then
  PY="$ROOT/.venv/bin/python3"
else
  PY="${PYTHON:-python3}"
fi

RUN_DIR="${KEEL_OBSERVE_RUN_DIR:-$ROOT/data/run}"
mkdir -p "$RUN_DIR"
API_PID_FILE="$RUN_DIR/keel-api.pid"
WORKER_PID_FILE="$RUN_DIR/keel-worker.pid"
API_LOG="$RUN_DIR/keel-api.log"
WORKER_LOG="$RUN_DIR/keel-worker.log"

PORT="${KEEL_API_PORT:-8080}"
HOST="${KEEL_API_HOST:-127.0.0.1}"

# Optional warn only: live without OKX triple → paper fallback likely
_okx_env=$(printf "%s" "${KEEL_OKX_ENV:-demo}" | tr "[:upper:]" "[:lower:]")
if [ "$_okx_env" = "live" ]; then
  _k="${KEEL_OKX_API_KEY:-${OKX_LIVE_API_KEY:-${OKX_API_KEY:-}}}"
  _s="${KEEL_OKX_SECRET_KEY:-${OKX_LIVE_SECRET_KEY:-${OKX_SECRET_KEY:-}}}"
  _p="${KEEL_OKX_PASSPHRASE:-${OKX_LIVE_PASSPHRASE:-${OKX_PASSPHRASE:-}}}"
  if [ -z "$_k" ] || [ -z "$_s" ] || [ -z "$_p" ]; then
    echo "WARN: KEEL_OKX_ENV=live but OKX API triple incomplete — exchange may fall back to paper" >&2
  fi
fi
# Intentionally do not set or clear KEEL_KILL_SWITCH

_alive() {
  _pf=$1
  if [ -f "$_pf" ]; then
    _pid=$(cat "$_pf" 2>/dev/null || true)
    if [ -n "${_pid:-}" ] && /bin/kill -0 "$_pid" 2>/dev/null; then
      return 0
    fi
    rm -f "$_pf"
  fi
  return 1
}

if _alive "$API_PID_FILE"; then
  echo "keel-api already running (pid $(cat "$API_PID_FILE")) — skip"
else
  /usr/bin/nohup "$PY" -m uvicorn keel.api.app:app --host "$HOST" --port "$PORT" \
    >>"$API_LOG" 2>&1 &
  echo $! >"$API_PID_FILE"
  echo "started keel-api pid=$(cat "$API_PID_FILE") ${HOST}:${PORT}"
fi

if _alive "$WORKER_PID_FILE"; then
  echo "keel-worker already running (pid $(cat "$WORKER_PID_FILE")) — skip"
else
  /usr/bin/nohup "$PY" -m keel.worker >>"$WORKER_LOG" 2>&1 &
  echo $! >"$WORKER_PID_FILE"
  echo "started keel-worker pid=$(cat "$WORKER_PID_FILE")"
fi

HEALTH_URL="http://${HOST}:${PORT}/health"
ok=0
i=0
while [ "$i" -lt 30 ]; do
  i=$((i + 1))
  if curl -sf --connect-timeout 1 --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
    echo "health: OK ($HEALTH_URL)"
    ok=1
    break
  fi
  sleep 1
done
if [ "$ok" -ne 1 ]; then
  echo "WARN: /health not ready after ~30s — check $API_LOG" >&2
fi

echo "Observe stack up (run_dir=$RUN_DIR)."
echo "  status:  ./scripts/observe_status.sh"
echo "  down:    ./scripts/observe_down.sh"
echo "  Monitor: optional Vite — see frontend/README.md (port 5173)"
echo "  Note: KEEL_KILL_SWITCH left unchanged by this script"
