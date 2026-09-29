#!/usr/bin/env bash
# Keel observe stack: reboot-proof "make sure it is up" (idempotent, single-flight, logged).
#
# Why: the box is a container that is re-created on reboot and hydrated from a
# snapshot of /workspace + /home/box. Processes never survive, and the snapshot
# ignores **/.venv/, **/venv/, node_modules/, __pycache__/, .cache/ … so the repo
# .venv vanished on every reboot (9/25, 9/28, 9/29). See docs/BOX_AUTOSTART.md.
#
# What it does (each step is a no-op when already fine):
#   1. venv: keep a persistent venv at $KEEL_VENV_DIR (default /home/box/.venvs/keel-trader,
#      home is snapshotted and venv/ is NOT ignored there) and point repo .venv at it
#      (a symlink named .venv survives the snapshot; only the .venv/ *dir* is ignored).
#      Recreates it with uv (fallback python3 -m venv + pip) if missing/broken.
#   2. stack: api/worker pid dead → scripts/observe_up.sh (starts only what is missing).
#      Both alive but /ready unreachable or worker_stale=true on 2 consecutive checks
#      → observe_down.sh + observe_up.sh.
#   3. watchdog: (default mode) start scripts/keel_watchdog.sh if not running.
#
# Never touches .env, KEEL_KILL_SWITCH / shadow / policy, orders or positions.
# Paused while data/run/keel-autostart.disabled exists (observe_down.sh creates it,
# observe_up.sh removes it) so an intentional stop is respected, also across reboots.
#
# Usage: scripts/keel_ensure_up.sh [--check] [--from TAG]
#   --check   one health pass only (used by the watchdog loop; does not spawn watchdog)
set -uo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

RUN_DIR="${KEEL_OBSERVE_RUN_DIR:-$ROOT/data/run}"
mkdir -p "$RUN_DIR"
LOG="${KEEL_WATCHDOG_LOG:-$RUN_DIR/keel-watchdog.log}"
LOCK="$RUN_DIR/keel-ensure.lock"
DISABLED="$RUN_DIR/keel-autostart.disabled"
STALE_STATE="$RUN_DIR/keel-watchdog.stale"
VENV_DIR="${KEEL_VENV_DIR:-/home/box/.venvs/keel-trader}"
PORT="${KEEL_API_PORT:-8080}"
HOST="${KEEL_API_HOST:-127.0.0.1}"
MODE=ensure
FROM=manual
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    --from) shift; FROM="${1:-?}" ;;
    *) ;;
  esac
  shift
done

log() { printf '%s [ensure:%s] %s\n' "$(date '+%F %T %Z')" "$FROM" "$*" >>"$LOG"; }

# Bounded log (keep last ~2000 lines when >1 MB).
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  tail -n 2000 "$LOG" >"$LOG.tmp" 2>/dev/null && mv -f "$LOG.tmp" "$LOG"
fi

# Single flight. fd 9 must NOT leak into the daemons we start (closed below with 9>&-).
exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

_alive() {
  [ -f "$1" ] || return 1
  _p=$(cat "$1" 2>/dev/null || true)
  [ -n "${_p:-}" ] && /bin/kill -0 "$_p" 2>/dev/null
}

_py_ok() {
  [ -x "$1" ] && "$1" -c 'import fastapi, uvicorn, pandas, numpy, requests, jinja2, cryptography' >/dev/null 2>&1
}

ensure_venv() {
  if _py_ok "$ROOT/.venv/bin/python"; then
    return 0
  fi
  log "repo .venv missing/broken — repairing (persistent venv: $VENV_DIR)"
  if ! _py_ok "$VENV_DIR/bin/python"; then
    rm -rf "$VENV_DIR"
    mkdir -p "$(dirname "$VENV_DIR")"
    _t0=$(date +%s)
    if command -v uv >/dev/null 2>&1; then
      uv venv -q --python "$(command -v python3)" "$VENV_DIR" >>"$LOG" 2>&1 \
        && uv pip install -q --python "$VENV_DIR/bin/python" -r "$ROOT/requirements.txt" >>"$LOG" 2>&1
    else
      python3 -m venv "$VENV_DIR" >>"$LOG" 2>&1 \
        && "$VENV_DIR/bin/pip" install -q -r "$ROOT/requirements.txt" >>"$LOG" 2>&1
    fi
    if ! _py_ok "$VENV_DIR/bin/python"; then
      log "ERROR: venv build failed at $VENV_DIR"
      return 1
    fi
    log "built venv $VENV_DIR in $(( $(date +%s) - _t0 ))s"
  fi
  if [ -L "$ROOT/.venv" ] || [ ! -e "$ROOT/.venv" ]; then
    ln -sfn "$VENV_DIR" "$ROOT/.venv"
  elif _alive "$RUN_DIR/keel-api.pid" || _alive "$RUN_DIR/keel-worker.pid"; then
    log "WARN: broken real .venv dir in use by a live process — not replacing"
    return 1
  else
    rm -rf "$ROOT/.venv" && ln -sfn "$VENV_DIR" "$ROOT/.venv"
  fi
  log "repo .venv -> $(readlink "$ROOT/.venv")"
}

ready_json() {
  curl -sf --connect-timeout 2 --max-time 5 "http://${HOST}:${PORT}/ready" 2>/dev/null || true
}

ensure_stack() {
  if [ -f "$DISABLED" ]; then
    return 0
  fi
  if ! _alive "$RUN_DIR/keel-api.pid" || ! _alive "$RUN_DIR/keel-worker.pid"; then
    _api=dead; _wk=dead
    _alive "$RUN_DIR/keel-api.pid" && _api=alive
    _alive "$RUN_DIR/keel-worker.pid" && _wk=alive
    if [ "$_wk" = dead ]; then
      # A dead worker can leave its in-flight cycle child running; let it finish first
      # so the new worker never runs a second cycle in parallel.
      _w=0
      while pgrep -f "$ROOT/.venv/bin/python -m keel.worker.cycle" >/dev/null 2>&1 && [ "$_w" -lt 180 ]; do
        [ "$_w" -eq 0 ] && log "orphan worker cycle still running — waiting (max 180s)"
        sleep 2
        _w=$((_w + 2))
      done
    fi
    log "api=$_api worker=$_wk → observe_up.sh"
    if "$ROOT/scripts/observe_up.sh" >>"$LOG" 2>&1 9>&-; then
      log "observe_up OK: $(ready_json)"
    else
      log "ERROR: observe_up.sh failed (rc=$?)"
    fi
    rm -f "$STALE_STATE"
    return 0
  fi
  _r=$(ready_json)
  if [ -n "$_r" ] && ! printf '%s' "$_r" | grep -q '"worker_stale":true'; then
    rm -f "$STALE_STATE"
    return 0
  fi
  _n=$(( $(cat "$STALE_STATE" 2>/dev/null || echo 0) + 1 ))
  echo "$_n" >"$STALE_STATE"
  log "unhealthy #$_n (ready=${_r:-unreachable})"
  if [ "$_n" -ge 2 ]; then
    log "restarting stack (observe_down + observe_up)"
    "$ROOT/scripts/observe_down.sh" --keep-autostart >>"$LOG" 2>&1 9>&- || true
    "$ROOT/scripts/observe_up.sh" >>"$LOG" 2>&1 9>&- || log "ERROR: observe_up.sh failed"
    rm -f "$STALE_STATE"
    log "after restart: $(ready_json)"
  fi
}

ensure_watchdog() {
  if _alive "$RUN_DIR/keel-watchdog.pid"; then
    return 0
  fi
  setsid nohup "$ROOT/scripts/keel_watchdog.sh" >>"$LOG" 2>&1 9>&- </dev/null &
  sleep 0.5
  log "watchdog started pid=$(cat "$RUN_DIR/keel-watchdog.pid" 2>/dev/null || echo '?')"
}

ensure_venv || true
ensure_stack
if [ "$MODE" = ensure ]; then
  ensure_watchdog
fi
exit 0
