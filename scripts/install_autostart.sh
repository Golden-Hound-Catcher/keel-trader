#!/usr/bin/env bash
# Install (idempotent) the ~/.bashrc hook that brings the Keel stack back after a
# box reboot. There is no cron/systemd in the box (PID 1 is tini); the first thing
# that runs after a reboot is an agent shell, whose environment snapshot sources
# ~/.bashrc (interactive). The hook only forks keel_ensure_up.sh in the background
# when the watchdog is not alive — silent, <10 ms otherwise.
# /home/box is part of the box snapshot, so the hook itself persists across reboots.
# Usage: scripts/install_autostart.sh [--uninstall]
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RC="${KEEL_AUTOSTART_RC:-$HOME/.bashrc}"
BEGIN="# >>> keel-trader autostart (scripts/install_autostart.sh) >>>"
END="# <<< keel-trader autostart <<<"
touch "$RC"
tmp=$(mktemp)
awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "$RC" >"$tmp"
if [ "${1:-}" != "--uninstall" ]; then
  cat >>"$tmp" <<HOOK
$BEGIN
if [ -z "\${KEEL_AUTOSTART_DISABLE:-}" ] && [ -x "$ROOT/scripts/keel_ensure_up.sh" ]; then
  _kw_pid=\$(cat "$ROOT/data/run/keel-watchdog.pid" 2>/dev/null)
  if [ -z "\$_kw_pid" ] || ! kill -0 "\$_kw_pid" 2>/dev/null; then
    (setsid nohup "$ROOT/scripts/keel_ensure_up.sh" --from bashrc </dev/null >/dev/null 2>&1 &) >/dev/null 2>&1
  fi
  unset _kw_pid
fi
$END
HOOK
  echo "installed hook in $RC"
else
  echo "removed hook from $RC"
fi
cat "$tmp" >"$RC"
rm -f "$tmp"
