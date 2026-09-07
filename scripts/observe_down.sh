#!/usr/bin/env bash
# Stop observe stack processes started by observe_up.sh (pid files under data/run/).
# Usage: ./scripts/observe_down.sh
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

_stop() {
  _name=$1
  _pf=$2
  if [ ! -f "$_pf" ]; then
    echo "$_name: no pidfile ($_pf)"
    return 0
  fi
  _pid=$(cat "$_pf" 2>/dev/null || true)
  if [ -z "${_pid:-}" ]; then
    echo "$_name: empty pidfile — removing"
    rm -f "$_pf"
    return 0
  fi
  if ! /bin/kill -0 "$_pid" 2>/dev/null; then
    echo "$_name: not running (stale pid $_pid) — removing pidfile"
    rm -f "$_pf"
    return 0
  fi
  /bin/kill "$_pid" 2>/dev/null || true
  _j=0
  while [ "$_j" -lt 10 ]; do
    _j=$((_j + 1))
    if ! /bin/kill -0 "$_pid" 2>/dev/null; then
      break
    fi
    sleep 0.3
  done
  if /bin/kill -0 "$_pid" 2>/dev/null; then
    /bin/kill -9 "$_pid" 2>/dev/null || true
    echo "stopped $_name pid=$_pid (forced)"
  else
    echo "stopped $_name pid=$_pid"
  fi
  rm -f "$_pf"
}

_stop "keel-worker" "$WORKER_PID_FILE"
_stop "keel-api" "$API_PID_FILE"
echo "Observe stack down."
