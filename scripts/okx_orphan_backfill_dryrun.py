#!/usr/bin/env python3
"""STUB: dry-run list OKX history candidates for orphan closes — NEVER writes.

This PR keeps inventory-only for historical orphans. If you later implement
high-confidence fill matching, flag closes as exit_reason=backfill_* and
require an explicit --write flag (not present here).

Usage:
  PYTHONPATH=. .venv/bin/python scripts/okx_orphan_backfill_dryrun.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from keel.config import get_settings
    from keel.ledger import KeelLedger
    from keel.ledger.orphan_inventory import inventory_orphans

    settings = get_settings()
    ledger = KeelLedger(Path(settings.ledger_path))
    try:
        inv = inventory_orphans(ledger, sample_limit=10)
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "writes": False,
                    "orphan_total": inv.total,
                    "sample_open_trade_ids": [r.trade_id for r in inv.sample],
                    "message": (
                        "No OKX fill matching implemented; "
                        "refusing to invent closes. Prefer forward reconcile."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
