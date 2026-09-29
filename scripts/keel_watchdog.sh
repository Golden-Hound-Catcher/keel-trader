#!/usr/bin/env bash
# Keel watchdog loop: every $KEEL_WATCHDOG_INTERVAL seconds (default 120) run
# scripts/keel_ensure_up.sh --check. Exactly one instance (flock on
# data/run/keel-watchdog.lock held for the process lifetime; the lock fd is
# closed for children so daemons it (re)starts never inherit it).
# Started by keel_ensure_up.sh (manual, ~/.bashrc hook, heartbeat). See docs/BOX_AUTOSTART.md.
set -uo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1
RUN_DIR="${KEEL_OBSERVE_RUN_DIR:-$ROOT/data/run}"
mkdir -p "$RUN_DIR"
LOG="${KEEL_WATCHDOG_LOG:-$RUN_DIR/keel-watchdog.log}"
INTERVAL="${KEEL_WATCHDOG_INTERVAL:-120}"

exec 8>"$RUN_DIR/keel-watchdog.lock"
if ! flock -n 8; then
  exit 0
fi
echo $$ >"$RUN_DIR/keel-watchdog.pid"
_sleep_pid=""
trap '[ -n "$_sleep_pid" ] && kill "$_sleep_pid" 2>/dev/null; rm -f "$RUN_DIR/keel-watchdog.pid"; exit 0' TERM INT HUP
printf '%s [watchdog] started pid=%s interval=%ss\n' "$(date '+%F %T %Z')" "$$" "$INTERVAL" >>"$LOG"
while :; do
  "$ROOT/scripts/keel_ensure_up.sh" --check --from watchdog 8>&- </dev/null >/dev/null 2>&1
  sleep "$INTERVAL" 8>&- &
  _sleep_pid=$!
  wait "$_sleep_pid"
done
