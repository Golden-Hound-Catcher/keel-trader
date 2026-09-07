#!/usr/bin/env python3
"""
Export recent ledger decisions to JSONL/JSON for offline rule-param replay.

No network / no OKX keys. Reads local SQLite only.

  PYTHONPATH=. python scripts/export_decisions.py \\
    --db data/keel_ledger.db --hours 48 --market-source okx_public \\
    --format jsonl --out /tmp/decisions.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

for key in (
    "KEEL_OKX_API_KEY",
    "KEEL_OKX_SECRET_KEY",
    "KEEL_OKX_PASSPHRASE",
    "OKX_DEMO_API_KEY",
    "OKX_DEMO_SECRET_KEY",
    "OKX_DEMO_PASSPHRASE",
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_PASSPHRASE",
):
    os.environ.pop(key, None)
    os.environ[key] = ""

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from keel.ledger import KeelLedger  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Export ledger decisions to JSONL/JSON (offline replay)"
    )
    p.add_argument(
        "--db",
        type=Path,
        default=_ROOT / "data" / "keel_ledger.db",
        help="SQLite ledger path (default: data/keel_ledger.db)",
    )
    p.add_argument("--hours", type=float, default=48.0, help="Lookback window hours")
    p.add_argument(
        "--market-source",
        default="any",
        choices=("any", "okx_public", "synthetic"),
        help="Filter calculus_data.market_source",
    )
    p.add_argument("--inst-id", default=None, help="Optional instrument filter")
    p.add_argument("--limit", type=int, default=5000, help="Max rows")
    p.add_argument(
        "--format",
        choices=("jsonl", "json"),
        default="jsonl",
        dest="fmt",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path (default: stdout)",
    )
    p.add_argument(
        "--no-factors",
        action="store_true",
        help="Skip factor_snapshots join (calculus/signal_diag only)",
    )
    args = p.parse_args(argv)

    db = args.db
    if not db.is_file():
        print(f"error: ledger not found: {db}", file=sys.stderr)
        return 1

    ledger = KeelLedger(db)
    try:
        rows = ledger.export_decisions(
            hours=float(args.hours),
            market_source=args.market_source,
            inst_id=args.inst_id,
            limit=int(args.limit),
            include_factors=not args.no_factors,
        )
    finally:
        ledger.close()

    replayable = sum(1 for r in rows if r.get("replayable"))
    meta = {
        "n": len(rows),
        "replayable": replayable,
        "skipped_incomplete": len(rows) - replayable,
        "hours": args.hours,
        "market_source": args.market_source,
        "db": str(db),
    }

    if args.fmt == "json":
        payload = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    else:
        payload = "".join(
            json.dumps(r, ensure_ascii=False) + "\n" for r in rows
        )

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
        print(
            f"wrote {meta['n']} decisions "
            f"(replayable={meta['replayable']} "
            f"incomplete={meta['skipped_incomplete']}) -> {args.out}",
            file=sys.stderr,
        )
    else:
        # metadata on stderr so stdout stays pure JSONL/JSON
        print(
            f"export n={meta['n']} replayable={meta['replayable']} "
            f"incomplete={meta['skipped_incomplete']} "
            f"hours={meta['hours']} market_source={meta['market_source']}",
            file=sys.stderr,
        )
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
