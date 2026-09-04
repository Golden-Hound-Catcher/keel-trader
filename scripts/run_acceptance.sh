#!/usr/bin/env bash
# Paper acceptance: one worker --once against a temp ledger (no OKX keys).
# Usage: ./scripts/run_acceptance.sh
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY=python3
fi

TS=$(date +%Y%m%d%H%M%S)
LEDGER="${KEEL_ACCEPTANCE_LEDGER:-/tmp/keel-acceptance-${TS}-$$.db}"
# Prefer data/ if caller wants repo-local path:
# KEEL_ACCEPTANCE_LEDGER=data/acceptance-${TS}.db ./scripts/run_acceptance.sh

echo "==> Keel paper acceptance"
echo "    ledger: $LEDGER"
echo "    python: $PY"

# Force paper path: empty OKX keys (factory: force_paper or not okx_configured → PaperExchange)
export KEEL_OKX_API_KEY=
export KEEL_OKX_SECRET_KEY=
export KEEL_OKX_PASSPHRASE=
unset OKX_DEMO_API_KEY OKX_DEMO_SECRET_KEY OKX_DEMO_PASSPHRASE 2>/dev/null || true
unset OKX_LIVE_API_KEY OKX_LIVE_SECRET_KEY OKX_LIVE_PASSPHRASE 2>/dev/null || true
unset OKX_API_KEY OKX_SECRET_KEY OKX_PASSPHRASE 2>/dev/null || true
export OKX_DEMO_API_KEY= OKX_DEMO_SECRET_KEY= OKX_DEMO_PASSPHRASE=
export OKX_API_KEY= OKX_SECRET_KEY= OKX_PASSPHRASE=

export KEEL_LEDGER_DB="$LEDGER"
export KEEL_KILL_SWITCH=0
export KEEL_DECISION_POLICY="${KEEL_DECISION_POLICY:-rule}"
export PYTHONPATH="${PYTHONPATH:-.}"

# Narrow watchlist for speed
export KEEL_INSTRUMENTS="${KEEL_INSTRUMENTS:-BTC-USDT-SWAP}"

set +e
"$PY" -m keel.worker --once
RC=$?
set -e

if [ "$RC" -ne 0 ]; then
  echo "FAIL: keel.worker --once exited $RC"
  exit 1
fi

# Verify ledger has ≥1 decision or cycle summary event
set +e
"$PY" - "$LEDGER" <<'PY'
import sqlite3
import sys

db = sys.argv[1]
con = sqlite3.connect(db)
try:
    decisions = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
except sqlite3.Error as e:
    print(f"FAIL: cannot read decisions: {e}")
    sys.exit(1)
try:
    summaries = con.execute(
        "SELECT COUNT(*) FROM events WHERE event_type IN (?, ?)",
        ("worker_cycle_summary", "paper_cycle_complete"),
    ).fetchone()[0]
except sqlite3.Error as e:
    print(f"FAIL: cannot read events: {e}")
    sys.exit(1)
con.close()

print(f"    decisions={decisions} cycle_events={summaries}")
if decisions < 1 and summaries < 1:
    print("FAIL: ledger has no decisions and no cycle summary events")
    sys.exit(1)
print("PASS: paper cycle wrote ledger evidence")
sys.exit(0)
PY
CHECK_RC=$?
set -e

if [ "$CHECK_RC" -ne 0 ]; then
  echo "FAIL: ledger verification failed"
  exit 1
fi

echo "PASS: paper acceptance (exit 0, ledger OK)"
exit 0
