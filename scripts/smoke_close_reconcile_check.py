#!/usr/bin/env python3
"""Read-only smoke check for close_reconcile forward path.

Checks ledger for:
  - recent positions_seen (nonempty OR awaiting live position)
  - close / sl_hit / tp_hit counts
  - orphan open count
  - daily_loss_gate honesty flags

Safe against live demo (no orders, no writes).

Usage:
  PYTHONPATH=. .venv/bin/python scripts/smoke_close_reconcile_check.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from keel.config import get_settings
    from keel.execution.close_reconcile import (
        POSITIONS_SEEN_EVENT,
        load_last_positions_seen,
    )
    from keel.ledger import KeelLedger
    from keel.ledger.orphan_inventory import daily_loss_gate_honesty, inventory_orphans

    settings = get_settings()
    db = Path(settings.ledger_path)
    ledger = KeelLedger(db)
    try:
        inv = inventory_orphans(ledger, sample_limit=5)
        honesty = daily_loss_gate_honesty(ledger)
        last = load_last_positions_seen(ledger)
        events = ledger.get_events(event_type=POSITIONS_SEEN_EVENT, limit=5)
        closes = ledger.get_trades(action="close", limit=20)
        sl_hits = ledger.get_events(event_type="sl_hit", limit=5)
        tp_hits = ledger.get_events(event_type="tp_hit", limit=5)
        attached = ledger.get_events(event_type="sl_tp_attached", limit=20)
        attach_failed = ledger.get_events(event_type="sl_tp_attach_failed", limit=20)

        recent_nonempty = False
        last_count = 0
        last_ts = None
        if events:
            last_ts = float(events[0].timestamp or 0.0)
            data = events[0].data or {}
            last_count = int(data.get("count") or 0)
            recent_nonempty = last_count > 0 or bool(last)

        age = (time.time() - last_ts) if last_ts else None
        report = {
            "ledger": str(db),
            "positions_seen": {
                "events_sampled": len(events),
                "last_count": last_count,
                "last_age_seconds": round(age, 1) if age is not None else None,
                "tracked_keys": sorted(last.keys()),
                "status": (
                    "tracking_live" if recent_nonempty else "awaiting_live_position"
                ),
            },
            "closes": {
                "sample_count": len(closes),
                "sl_hit_sample": len(sl_hits),
                "tp_hit_sample": len(tp_hits),
            },
            "sl_tp_attach": {
                "attached_sample": len(attached),
                "attach_failed_sample": len(attach_failed),
            },
            "orphans": inv.to_status_dict(),
            "daily_loss_gate": honesty.to_status_dict(),
            "checklist": [
                "1) Place/wait for a live demo open that stays open ≥1 worker cycle",
                "2) Confirm positions_seen.count > 0 and open_trade_ids non-empty",
                "3) Flat via SL/TP/manual; next cycle should write trades.action=close",
                "4) Re-run this script: closes.sample_count >= 1, orphans decline for that id",
                "Do NOT invent historical closes for pre-baseline orphans",
            ],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not recent_nonempty and len(closes) == 0:
            print(
                "\nBLOCKER: no nonempty positions_seen and zero closes — "
                "forward path unproven (book flat or positions API empty).",
                file=sys.stderr,
            )
            return 2
        return 0
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
