#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
VENV_DIR=${VENV_DIR:-$ROOT/.venv}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "ERROR: Python 3 is required" >&2; exit 1; }

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -r "$ROOT/requirements.txt"

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/env.example" "$ROOT/.env"
fi
chmod 600 "$ROOT/.env"

cat <<EOF

Keel Trader dependencies installed.
Supported runtime:
  - keel-api:    uvicorn keel.api.app:app   (deploy/keel-api.service)
  - keel-worker: python -m keel.worker      (deploy/keel-worker.service)
  - frontend/:   optional U1 monitor (see frontend/README.md)

OKX: Keel uses keel.exchange REST (not the legacy okx CLI / r20_okx_setup helpers).
  Set KEEL_OKX_ENV=demo|live and KEEL_OKX_API_KEY / KEEL_OKX_SECRET_KEY / KEEL_OKX_PASSPHRASE in .env
  (see env.example). Keep KEEL_OKX_ENV=demo initially.

Next:
  1. Edit $ROOT/.env (prefer KEEL_OKX_* ; keep KEEL_OKX_ENV=demo initially).
  2. Start only Keel units / processes — r20_* packages and r20-*.service units are removed.
  3. Verify: python -m keel.worker --once && curl -s localhost:8080/health

See LEGACY.md / STANDALONE.md / deploy/README.md.
EOF
