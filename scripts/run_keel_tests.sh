# Run Keel core tests (same as: make test)
# Usage: sh scripts/run_keel_tests.sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY=python3
fi
exec "$PY" -m pytest tests/test_keel_*.py -v "$@"
