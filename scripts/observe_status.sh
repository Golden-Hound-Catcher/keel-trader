#!/usr/bin/env bash
# Show observe stack pid liveness + /health /ready snippets (no secrets).
# Warns when .env says live+keys but /health|/ready look demo/unconfigured (stale API).
# Usage: ./scripts/observe_status.sh
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT/.env"
  set +a
fi

RUN_DIR="${KEEL_OBSERVE_RUN_DIR:-$ROOT/data/run}"
API_PID_FILE="$RUN_DIR/keel-api.pid"
WORKER_PID_FILE="$RUN_DIR/keel-worker.pid"
PORT="${KEEL_API_PORT:-8080}"
HOST="${KEEL_API_HOST:-127.0.0.1}"
BASE="http://${HOST}:${PORT}"

_show_pid() {
  _name=$1
  _pf=$2
  if [ -f "$_pf" ]; then
    _pid=$(cat "$_pf" 2>/dev/null || true)
    if [ -n "${_pid:-}" ] && /bin/kill -0 "$_pid" 2>/dev/null; then
      echo "$_name: alive pid=$_pid"
    else
      echo "$_name: dead/stale pidfile=${_pid:-empty}"
    fi
  else
    echo "$_name: not started (no pidfile)"
  fi
}

echo "run_dir=$RUN_DIR"
_show_pid "keel-api" "$API_PID_FILE"
_show_pid "keel-worker" "$WORKER_PID_FILE"

_HEALTH_BODY=""
_READY_BODY=""

_snip() {
  _path=$1
  _url="${BASE}${_path}"
  echo "--- GET ${_path} ---"
  set +e
  _body=$(curl -sS --connect-timeout 2 --max-time 5 "$_url" 2>/tmp/keel-observe-curl.err)
  _rc=$?
  set -e
  if [ "$_rc" -ne 0 ]; then
    echo "(unreachable: curl_rc=$_rc)"
    sed -n "1,2p" /tmp/keel-observe-curl.err 2>/dev/null || true
    return 0
  fi
  # /health and /ready have no secrets; truncate for readability
  printf "%s\n" "$_body" | head -c 500
  echo
  if [ "$_path" = "/health" ]; then
    _HEALTH_BODY="$_body"
  elif [ "$_path" = "/ready" ]; then
    _READY_BODY="$_body"
  fi
}

_snip "/health"
_snip "/ready"

# Mismatch: .env expects live+keys but API reports demo / okx_configured=false
_okx_env=$(printf "%s" "${KEEL_OKX_ENV:-demo}" | tr "[:upper:]" "[:lower:]")
_k="${KEEL_OKX_API_KEY:-${OKX_LIVE_API_KEY:-${OKX_API_KEY:-}}}"
_s="${KEEL_OKX_SECRET_KEY:-${OKX_LIVE_SECRET_KEY:-${OKX_SECRET_KEY:-}}}"
_p="${KEEL_OKX_PASSPHRASE:-${OKX_LIVE_PASSPHRASE:-${OKX_PASSPHRASE:-}}}"
_env_live_keys=0
if [ "$_okx_env" = "live" ] && [ -n "$_k" ] && [ -n "$_s" ] && [ -n "$_p" ]; then
  _env_live_keys=1
fi

if [ "$_env_live_keys" -eq 1 ]; then
  _mismatch=0
  if printf "%s" "$_HEALTH_BODY" | grep -q '"environment"[[:space:]]*:[[:space:]]*"demo"'; then
    _mismatch=1
  fi
  if printf "%s" "$_READY_BODY" | grep -q '"okx_configured"[[:space:]]*:[[:space:]]*false'; then
    _mismatch=1
  fi
  if [ "$_mismatch" -eq 1 ]; then
    echo "WARN: .env has KEEL_OKX_ENV=live + OKX keys, but /health|/ready look like demo or okx_configured=false." >&2
    echo "      Likely a STALE API on :${PORT} (different process/env). Check: ./scripts/observe_down.sh then observe_up, or ss -ltnp 'sport = :${PORT}'." >&2
  fi
fi
