#!/usr/bin/env bash
# Stop observe stack processes started by observe_up.sh (pid files under data/run/).
# Usage: ./scripts/observe_down.sh [--keep-autostart]
# An intentional stop pauses the autostart watchdog (creates data/run/keel-autostart.disabled,
# persists across reboots); observe_up.sh clears it. --keep-autostart (used by the watchdog's
# own restart) leaves autostart armed. See docs/BOX_AUTOSTART.md.
set -euo pipefail
KEEP_AUTOSTART=0
for _a in "$@"; do
  case "$_a" in
    --keep-autostart) KEEP_AUTOSTART=1 ;;
  esac
done

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

if [ "$KEEP_AUTOSTART" != "1" ]; then
  mkdir -p "$RUN_DIR"
  date '+%F %T %Z observe_down (autostart paused)' >"$RUN_DIR/keel-autostart.disabled"
  echo "autostart watchdog paused ($RUN_DIR/keel-autostart.disabled) — observe_up.sh re-arms it"
fi
# keel-worker runs each cycle as a child process (keel.worker.cycle). Killing only the
# parent orphaned an in-flight cycle, and the next worker then ran a second cycle in
# parallel (seen 2026-09-29 09:27). Remember the children, stop the parent, then let an
# in-flight cycle finish (bounded) before terminating it.
_worker_children=""
if [ -f "$WORKER_PID_FILE" ]; then
  _wpid=$(cat "$WORKER_PID_FILE" 2>/dev/null || true)
  if [ -n "${_wpid:-}" ] && command -v pgrep >/dev/null 2>&1; then
    _worker_children=$(pgrep -P "$_wpid" 2>/dev/null | tr '\n' ' ' || true)
  fi
fi
_stop "keel-worker" "$WORKER_PID_FILE"
_wait_children() {
  _max="${KEEL_OBSERVE_CYCLE_WAIT:-180}"
  for _c in $_worker_children; do
    [ -n "$_c" ] || continue
    /bin/kill -0 "$_c" 2>/dev/null || continue
    echo "waiting up to ${_max}s for in-flight worker cycle pid=$_c to finish"
    _s=0
    while /bin/kill -0 "$_c" 2>/dev/null && [ "$_s" -lt "$_max" ]; do
      sleep 1
      _s=$((_s + 1))
    done
    if /bin/kill -0 "$_c" 2>/dev/null; then
      /bin/kill "$_c" 2>/dev/null || true
      sleep 3
      /bin/kill -0 "$_c" 2>/dev/null && /bin/kill -9 "$_c" 2>/dev/null || true
      echo "terminated worker cycle pid=$_c after ${_max}s"
    else
      echo "worker cycle pid=$_c finished (${_s}s)"
    fi
  done
}
_wait_children
_stop "keel-api" "$API_PID_FILE"
echo "Observe stack down."
