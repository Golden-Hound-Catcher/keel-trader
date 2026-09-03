#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
VENV_DIR=${VENV_DIR:-$ROOT/.venv}
OKX_CLI_SPEC=${OKX_CLI_SPEC:-@okx_ai/okx-trade-cli@^1.4.4}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "ERROR: Python 3 is required" >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo "ERROR: Node.js 18+ is required for OKX CLI" >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "ERROR: npm is required for OKX CLI" >&2; exit 1; }

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -r "$ROOT/requirements.txt"

if command -v okx >/dev/null 2>&1; then
  echo "OKX CLI already installed: $(okx --version 2>/dev/null | sed -n '1p')"
else
  echo "Installing official OKX CLI: $OKX_CLI_SPEC"
  npm install -g "$OKX_CLI_SPEC"
fi

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/env.example" "$ROOT/.env"
fi
chmod 600 "$ROOT/.env"
chmod +x "$ROOT/scripts/r20_okx_setup.py"

cat <<EOF

Keel Trader dependencies installed.
Supported runtime:
  - keel-api:    uvicorn keel.api.app:app   (deploy/keel-api.service)
  - keel-worker: python -m keel.worker      (deploy/keel-worker.service)
  - frontend/:   optional U1 monitor (see frontend/README.md)

Next:
  1. Edit $ROOT/.env (prefer KEEL_OKX_* ; keep KEEL_OKX_ENV=demo initially).
  2. Start only Keel units / processes — do not enable r20-*.service.
  3. Verify: python -m keel.worker --once && curl -s localhost:8080/health

Legacy r20_backend.app is soft-blocked unless KEEL_ALLOW_LEGACY_BACKEND=1.
See LEGACY.md / STANDALONE.md / deploy/README.md.
EOF
