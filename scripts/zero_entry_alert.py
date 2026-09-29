#!/usr/bin/env python3
"""
48h zero-entry alert for digests (reads the ledger directly; API not required).

Same payload as ``GET /ready`` → ``zero_entry_alert`` and
``GET /api/v1/stats/zero_entry``.

Usage:
  .venv/bin/python scripts/zero_entry_alert.py            # one Chinese line (message_zh)
  .venv/bin/python scripts/zero_entry_alert.py --json     # full JSON payload
  .venv/bin/python scripts/zero_entry_alert.py --exit-code  # exit 0 ok / 2 market / 3 outage
Options: --db PATH (default settings.ledger_path), --hours N (override threshold).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from keel.config import get_settings  # noqa: E402
from keel.ledger import KeelLedger  # noqa: E402
from keel.ledger.zero_entry_alert import compute_zero_entry_alert  # noqa: E402

EXIT_BY_KIND = {"market": 2, "outage": 3}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print full JSON payload")
    ap.add_argument("--exit-code", action="store_true", help="exit 2=market, 3=outage when triggered")
    ap.add_argument("--db", default="", help="ledger path (default: settings.ledger_path)")
    ap.add_argument("--hours", type=float, default=None, help="threshold hours override")
    args = ap.parse_args(argv)

    settings = get_settings()
    ledger = KeelLedger(Path(args.db) if args.db else settings.ledger_path)
    payload = compute_zero_entry_alert(
        ledger,
        threshold_hours=args.hours,
        cycle_interval_seconds=settings.cycle_interval_seconds,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload["message_zh"])
    if args.exit_code and payload.get("triggered"):
        return EXIT_BY_KIND.get(str(payload.get("kind")), 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
