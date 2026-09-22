#!/usr/bin/env python3
"""Read-only orphan open inventory report (no ledger writes).

Usage:
  PYTHONPATH=. .venv/bin/python scripts/orphan_inventory.py
  PYTHONPATH=. .venv/bin/python scripts/orphan_inventory.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Orphan open inventory (read-only)")
    parser.add_argument("--json", action="store_true", help="Print JSON detail")
    parser.add_argument(
        "--sample",
        type=int,
        default=20,
        help="Max sample rows (default 20)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Optional ledger path (default: settings.ledger_path)",
    )
    args = parser.parse_args()

    from keel.config import get_settings
    from keel.ledger import KeelLedger
    from keel.ledger.orphan_inventory import daily_loss_gate_honesty, inventory_orphans

    settings = get_settings()
    db = Path(args.db) if args.db else Path(settings.ledger_path)
    ledger = KeelLedger(db)
    try:
        inv = inventory_orphans(ledger, sample_limit=args.sample)
        honesty = daily_loss_gate_honesty(ledger)
        if args.json:
            payload = inv.to_detail_dict()
            payload["daily_loss_gate"] = honesty.to_status_dict()
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"ledger: {db}")
        print(
            f"orphans: total={inv.total} live={inv.live} "
            f"shadow={inv.shadow} other={inv.other}"
        )
        if inv.oldest_age_seconds is not None:
            print(
                f"age_seconds: oldest={inv.oldest_age_seconds:.0f} "
                f"newest={inv.newest_age_seconds:.0f}"
            )
        print("by_inst:")
        for inst, n in inv.by_inst.items():
            print(f"  {inst}: {n}")
        print("by_strategy_tag:")
        for tag, n in inv.by_strategy_tag.items():
            print(f"  {tag}: {n}")
        print(
            f"daily_loss_gate_effective={honesty.effective} "
            f"reason={honesty.reason} "
            f"closes_with_pnl={honesty.realized_close_count} "
            f"lifetime_closes={honesty.lifetime_close_count}"
        )
        if inv.sample:
            print(f"sample (oldest {len(inv.sample)}):")
            for row in inv.sample:
                print(
                    f"  id={row.trade_id} {row.inst_id} {row.direction} "
                    f"tag={row.strategy_tag} age_s={row.age_seconds:.0f}"
                )
        print(
            "note: orphans are not auto-closed; "
            "use smoke_close_reconcile_check.py for forward path."
        )
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
