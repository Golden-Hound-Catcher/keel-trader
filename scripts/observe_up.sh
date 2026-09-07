#!/usr/bin/env bash
# Start keel-api + keel-worker for Q0 live/paper observation (read-only hanging).
# Loads repo .env; does NOT disable KEEL_KILL_SWITCH. Idempotent: skips if already up.
# Port conflict: fail clearly on foreign listeners; KEEL_OBSERVE_FORCE=1 kills only
# PIDs recorded in our pidfiles (never unknown PIDs).
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
FORCE="${KEEL_OBSERVE_FORCE:-0}"

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
  fi
  return 1
}

_read_pid() {
  if [ -f "$1" ]; then
    cat "$1" 2>/dev/null || true
  fi
}

_listener_pids() {
  # PIDs listening on TCP $1 (best-effort; may be empty without privileges).
  _port=$1
  _pids=""
  if command -v fuser >/dev/null 2>&1; then
    # fuser prints "PORT/tcp:  PID PID" on stderr/stdout depending on version
    _pids=$(fuser "${_port}/tcp" 2>/dev/null | tr -cs '0-9' ' ' || true)
  fi
  if [ -z "$(echo "${_pids:-}" | tr -d '[:space:]')" ] && command -v ss >/dev/null 2>&1; then
    _pids=$(ss -ltnp 2>/dev/null \
      | awk -v p=":${_port}" 'index($4, p) && ($4 ~ p"$" || $4 ~ p"\\]") {print}' \
      | grep -oE 'pid=[0-9]+' \
      | cut -d= -f2 \
      | sort -u \
      | tr '\n' ' ' || true)
  fi
  echo "${_pids:-}" | tr -s ' ' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

_describe_pids() {
  for _p in $1; do
    if [ -z "$_p" ]; then
      continue
    fi
    _line=$(ps -o pid=,comm=,args= -p "$_p" 2>/dev/null | head -1 || true)
    if [ -n "$_line" ]; then
      echo "  listener pid $_p: $_line"
    else
      echo "  listener pid $_p: (process info unavailable)"
    fi
  done
}

_stop_our_pidfile() {
  # Kill only the PID recorded in our pidfile (never unknown listeners).
  _name=$1
  _pf=$2
  _pid=$(_read_pid "$_pf")
  if [ -z "${_pid:-}" ]; then
    rm -f "$_pf"
    return 0
  fi
  if /bin/kill -0 "$_pid" 2>/dev/null; then
    echo "KEEL_OBSERVE_FORCE=1: stopping our $_name pid=$_pid"
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
    fi
  else
    echo "KEEL_OBSERVE_FORCE=1: removing stale $_name pidfile (was $_pid)"
  fi
  rm -f "$_pf"
}

_port_in_use() {
  _lpids=$(_listener_pids "$PORT")
  if [ -n "$(echo "${_lpids:-}" | tr -d '[:space:]')" ]; then
    return 0
  fi
  # Fallback probe when fuser/ss cannot list PIDs
  if command -v curl >/dev/null 2>&1; then
    if curl -sf --connect-timeout 0.3 --max-time 0.5 "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
  fi
  # bash /dev/tcp probe
  if (echo >/dev/tcp/"$HOST"/"$PORT") >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

_ensure_api_port_clear() {
  if _alive "$API_PID_FILE"; then
    return 0
  fi
  # Stale pidfile (dead process) — drop it so we do not mis-identify ownership
  if [ -f "$API_PID_FILE" ]; then
    echo "removing stale keel-api pidfile (pid $(_read_pid "$API_PID_FILE") not alive)"
    rm -f "$API_PID_FILE"
  fi

  if ! _port_in_use; then
    return 0
  fi

  _lpids=$(_listener_pids "$PORT")
  echo "ERROR: port ${HOST}:${PORT} already in use — refusing to start a second keel-api (would leave a stale pid + hit a foreign /health)." >&2
  if [ -n "$(echo "${_lpids:-}" | tr -d '[:space:]')" ]; then
    echo "Listeners on :${PORT}:" >&2
    _describe_pids "$_lpids" >&2
  else
    echo "Could not resolve listener PID (try: ss -ltnp 'sport = :${PORT}' or fuser ${PORT}/tcp)." >&2
  fi

  if [ "$FORCE" = "1" ]; then
    # Only kill our recorded api pid (already gone if we got here); never kill foreign.
    _stop_our_pidfile "keel-api" "$API_PID_FILE"
    if _port_in_use; then
      echo "ERROR: KEEL_OBSERVE_FORCE=1 only stops observe pidfile PIDs; foreign listener on :${PORT} still present. Stop it manually, then retry." >&2
      exit 1
    fi
    return 0
  fi

  echo "Hint: stop the foreign process, or if it was a previous observe stack: ./scripts/observe_down.sh" >&2
  echo "      KEEL_OBSERVE_FORCE=1 only kills PIDs in observe pidfiles — never unknown listeners." >&2
  exit 1
}

_tail_log() {
  _log=$1
  _n=${2:-40}
  if [ -f "$_log" ]; then
    echo "---- last ${_n} lines: $_log ----" >&2
    tail -n "$_n" "$_log" >&2 || true
    echo "---- end log ----" >&2
  else
    echo "(no log file yet: $_log)" >&2
  fi
}

_surface_worker_lock() {
  _log=$1
  if [ -f "$_log" ] && grep -qiE 'scheduler already holds the lock|BlockingIOError|another Keel scheduler' "$_log" 2>/dev/null; then
    echo "HINT: worker log shows scheduler lock contention — another keel-worker may still hold data/.keel_scheduler.lock" >&2
    grep -iE 'scheduler already holds the lock|BlockingIOError|another Keel scheduler|sole scheduler' "$_log" 2>/dev/null | tail -n 5 >&2 || true
  fi
}

_ensure_api_port_clear

if _alive "$API_PID_FILE"; then
  echo "keel-api already running (pid $(cat "$API_PID_FILE")) — skip"
else
  /usr/bin/nohup "$PY" -m uvicorn keel.api.app:app --host "$HOST" --port "$PORT" \
    >>"$API_LOG" 2>&1 &
  echo $! >"$API_PID_FILE"
  _api_pid=$(cat "$API_PID_FILE")
  echo "started keel-api pid=${_api_pid} ${HOST}:${PORT}"
  sleep 1
  if ! /bin/kill -0 "$_api_pid" 2>/dev/null; then
    echo "ERROR: keel-api pid ${_api_pid} died immediately after start (often: address already in use on :${PORT})." >&2
    _tail_log "$API_LOG" 40
    rm -f "$API_PID_FILE"
    exit 1
  fi
fi

if _alive "$WORKER_PID_FILE"; then
  echo "keel-worker already running (pid $(cat "$WORKER_PID_FILE")) — skip"
else
  # If FORCE and stale worker pidfile, clean first
  if [ -f "$WORKER_PID_FILE" ] && ! _alive "$WORKER_PID_FILE"; then
    if [ "$FORCE" = "1" ]; then
      _stop_our_pidfile "keel-worker" "$WORKER_PID_FILE"
    else
      echo "removing stale keel-worker pidfile (pid $(_read_pid "$WORKER_PID_FILE") not alive)"
      rm -f "$WORKER_PID_FILE"
    fi
  fi
  /usr/bin/nohup "$PY" -m keel.worker >>"$WORKER_LOG" 2>&1 &
  echo $! >"$WORKER_PID_FILE"
  _worker_pid=$(cat "$WORKER_PID_FILE")
  echo "started keel-worker pid=${_worker_pid}"
  sleep 1
  if ! /bin/kill -0 "$_worker_pid" 2>/dev/null; then
    echo "ERROR: keel-worker pid ${_worker_pid} died immediately after start." >&2
    _surface_worker_lock "$WORKER_LOG"
    _tail_log "$WORKER_LOG" 40
    rm -f "$WORKER_PID_FILE"
    exit 1
  fi
  # Surface lock warnings even if process briefly survived then logged them
  if grep -qiE 'scheduler already holds the lock|another Keel scheduler' "$WORKER_LOG" 2>/dev/null; then
    # Re-check: lock failure exits the process
    sleep 0.5
    if ! /bin/kill -0 "$_worker_pid" 2>/dev/null; then
      echo "ERROR: keel-worker exited (scheduler lock)." >&2
      _surface_worker_lock "$WORKER_LOG"
      _tail_log "$WORKER_LOG" 40
      rm -f "$WORKER_PID_FILE"
      exit 1
    fi
  fi
fi

HEALTH_URL="http://${HOST}:${PORT}/health"
ok=0
i=0
while [ "$i" -lt 30 ]; do
  i=$((i + 1))
  _api_pid=$(_read_pid "$API_PID_FILE")
  if [ -n "${_api_pid:-}" ] && ! /bin/kill -0 "$_api_pid" 2>/dev/null; then
    echo "ERROR: keel-api pid ${_api_pid} died while waiting for /health." >&2
    _tail_log "$API_LOG" 40
    rm -f "$API_PID_FILE"
    exit 1
  fi
  if curl -sf --connect-timeout 1 --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
    echo "health: OK ($HEALTH_URL)"
    ok=1
    break
  fi
  sleep 1
done
if [ "$ok" -ne 1 ]; then
  echo "ERROR: /health not ready after ~30s — check $API_LOG" >&2
  _tail_log "$API_LOG" 40
  exit 1
fi

echo "Observe stack up (run_dir=$RUN_DIR)."
echo "  status:  ./scripts/observe_status.sh"
echo "  down:    ./scripts/observe_down.sh"
echo "  Monitor: optional Vite — see frontend/README.md (port 5173)"
echo "  Note: KEEL_KILL_SWITCH left unchanged by this script"
