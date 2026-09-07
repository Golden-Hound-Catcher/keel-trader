#!/usr/bin/env bash
# Show observe stack pid liveness + /health /ready snippets (no secrets).
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
}

_snip "/health"
_snip "/ready"
