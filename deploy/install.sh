#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
VENV_DIR=${VENV_DIR:-$ROOT/.venv}
INSTALL_SYSTEMD=${INSTALL_SYSTEMD:-0}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "ERROR: Python 3 is required" >&2; exit 1; }

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -r "$ROOT/requirements.txt"

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/env.example" "$ROOT/.env"
fi
chmod 600 "$ROOT/.env"

rewrite_unit() {
  # Rewrite WorkingDirectory / EnvironmentFile / PATH for this install root.
  src=$1
  dest=$2
  sed \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$ROOT|" \
    -e "s|^EnvironmentFile=.*|EnvironmentFile=$ROOT/.env|" \
    -e "s|^Environment=PATH=.*|Environment=PATH=$ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin|" \
    "$src" > "$dest"
}

UNIT_API="$ROOT/deploy/keel-api.service"
UNIT_WORKER="$ROOT/deploy/keel-worker.service"
REWRITE_API="$ROOT/deploy/keel-api.service.local"
REWRITE_WORKER="$ROOT/deploy/keel-worker.service.local"
rewrite_unit "$UNIT_API" "$REWRITE_API"
rewrite_unit "$UNIT_WORKER" "$REWRITE_WORKER"

cat <<MSG

Keel Trader dependencies installed.
Supported runtime:
  - keel-api:    uvicorn keel.api.app:app   (deploy/keel-api.service)
  - keel-worker: python -m keel.worker      (deploy/keel-worker.service)
  - frontend/:   optional U1 monitor (see frontend/README.md)

OKX: Keel uses keel.exchange REST (not the legacy okx CLI / r20_okx_setup helpers).
  Set KEEL_OKX_ENV=demo|live and KEEL_OKX_API_KEY / KEEL_OKX_SECRET_KEY / KEEL_OKX_PASSPHRASE in .env
  (see env.example). Keep KEEL_OKX_ENV=demo initially.

Ops docs: $ROOT/RUNBOOK.md
Paper acceptance: $ROOT/scripts/run_acceptance.sh
Optional smoke: $ROOT/scripts/ops_smoke.sh

Recommended path:
  1. Edit $ROOT/.env (prefer KEEL_OKX_* ; keep KEEL_OKX_ENV=demo initially).
  2. Install systemd units (see below), then:
       sudo systemctl enable --now keel-api keel-worker
  3. Verify: ./scripts/run_acceptance.sh && curl -s localhost:8080/health

MSG

SYSTEMD_DIR=/etc/systemd/system
echo "Rewrote unit templates → $REWRITE_API , $REWRITE_WORKER"
echo "  (WorkingDirectory/EnvironmentFile/PATH → $ROOT)"
echo
echo "Note: unit User=/Group= may still say r20 — edit if your host user differs."
echo

if [ "$INSTALL_SYSTEMD" = "1" ]; then
  if [ "$(id -u)" -eq 0 ]; then
    cp "$REWRITE_API" "$SYSTEMD_DIR/keel-api.service"
    cp "$REWRITE_WORKER" "$SYSTEMD_DIR/keel-worker.service"
    systemctl daemon-reload
    echo "Installed units to $SYSTEMD_DIR (daemon-reload done)."
    echo "Next: systemctl enable --now keel-api keel-worker"
  else
    echo "INSTALL_SYSTEMD=1 but not root — run:"
    echo "  sudo cp $REWRITE_API $SYSTEMD_DIR/keel-api.service"
    echo "  sudo cp $REWRITE_WORKER $SYSTEMD_DIR/keel-worker.service"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl enable --now keel-api keel-worker"
  fi
else
  echo "To install systemd units later:"
  echo "  INSTALL_SYSTEMD=1 $0"
  echo "  # or:"
  echo "  sudo cp $REWRITE_API $SYSTEMD_DIR/keel-api.service"
  echo "  sudo cp $REWRITE_WORKER $SYSTEMD_DIR/keel-worker.service"
  echo "  sudo systemctl daemon-reload && sudo systemctl enable --now keel-api keel-worker"
fi

echo
echo "See RUNBOOK.md / deploy/README.md / LEGACY.md / STANDALONE.md."
