#!/usr/bin/env python3
"""
Phase E1: offline fee-aware markout on full-gate rule fires.

For BUY_LONG/SELL_SHORT decisions with signal_diag.missing==[] (rule policy),
use shadow_fill entry when present else counterfactual decision price, then
report gross / net round-trip markout at 60/300/900s (OKX fee model).

Recommend-only — never writes .env, never enables near_probe, never places
orders. E0 freeze: keep near_probe off; do not lower the 10bps fee hurdle.

  PYTHONPATH=. python scripts/full_gate_markout.py \
    --db data/keel_ledger.db --hours 168 --market-source okx_public

  PYTHONPATH=. python scripts/near_entry_markout.py --full-gate-only \
    --db data/keel_ledger.db --hours 168
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
from keel.ledger.full_gate import compute_full_gate_markout  # noqa: E402
from keel.ledger.near_entry_markout import (  # noqa: E402
    DEFAULT_CLEAR_HURDLE_BPS,
)
from keel.ledger.shadow_markout import (  # noqa: E402
    DEFAULT_MARKOUT_HORIZONS_SECONDS,
)


def _parse_horizons(raw: str | None) -> list[int]:
    if not raw or not str(raw).strip():
        return list(DEFAULT_MARKOUT_HORIZONS_SECONDS)
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out or list(DEFAULT_MARKOUT_HORIZONS_SECONDS)


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * x:.1f}%"


def _fmt_bps(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.2f}"


def _print_summary(result: dict) -> None:
    print("=== E1 full-gate markout (recommend-only) ===")
    print(
        f"hours={result.get('hours')} count={result.get('count')} "
        f"cohort={result.get('cohort')} market_source={result.get('market_source')}"
    )
    print(
        f"by_action={result.get('by_action')} "
        f"entry_sources={result.get('entry_sources')}"
    )
    skipped = result.get("skipped_no_price", 0)
    if skipped:
        print(f"skipped_no_price={skipped}")
    fm = result.get("fee_model") or {}
    print(
        f"fee_model source={fm.get('source')} role={fm.get('role')} "
        f"open={fm.get('open_fee_bps')} rt={fm.get('round_trip_fee_bps')} "
        f"funding_applied={fm.get('funding_applied')}"
    )
    frac = result.get("frac_clear_net_rt_hurdle")
    print(
        f"frac_clear_{result.get('clear_hurdle_bps')}bps_net_at_"
        f"{result.get('clear_horizon_seconds')}s={_fmt_pct(frac)}"
    )
    print("--- horizons ---")
    for h in (result.get("markout") or {}).get("horizons") or []:
        print(
            f"  {h.get('horizon_seconds')}s: n={h.get('sample_count')} "
            f"skip={h.get('skipped')} "
            f"gross_avg={_fmt_bps(h.get('avg_markout_bps'))} "
            f"gross_win={_fmt_pct(h.get('win_rate'))} "
            f"netRT_avg={_fmt_bps(h.get('avg_net_roundtrip_markout_bps'))} "
            f"netRT_win={_fmt_pct(h.get('win_rate_net_roundtrip'))} "
            f"clear@hurdle={_fmt_pct(h.get('frac_clear_net_rt_hurdle'))}"
        )
    print("--- by_instrument ---")
    for inst, info in (result.get("by_instrument") or {}).items():
        mk = info.get("markout_300s") or {}
        print(
            f"  {inst}: count={info.get('count')} "
            f"actions={info.get('by_action')} "
            f"300s n={mk.get('sample_count')} "
            f"netRT_avg={_fmt_bps(mk.get('avg_net_roundtrip_markout_bps'))} "
            f"netRT_win={_fmt_pct(mk.get('win_rate_net_roundtrip'))} "
            f"clear={_fmt_pct(mk.get('frac_clear_net_rt_hurdle'))}"
        )
    print(result.get("note") or "")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "E1 offline full-gate markout on BUY_LONG/SELL_SHORT with "
            "signal_diag.missing==[] (recommend-only; local SQLite)"
        )
    )
    p.add_argument(
        "--db",
        type=Path,
        default=_ROOT / "data" / "keel_ledger.db",
        help="SQLite ledger path (default: data/keel_ledger.db)",
    )
    p.add_argument("--hours", type=float, default=168.0, help="Lookback window hours")
    p.add_argument(
        "--market-source",
        default="any",
        choices=("any", "okx_public", "synthetic"),
        help="Filter calculus_data.market_source",
    )
    p.add_argument(
        "--inst-id",
        default=None,
        help="Optional instrument filter (comma-separated ok)",
    )
    p.add_argument(
        "--horizons",
        default="60,300,900",
        help="Comma-separated markout horizons in seconds",
    )
    p.add_argument(
        "--clear-hurdle-bps",
        type=float,
        default=DEFAULT_CLEAR_HURDLE_BPS,
        help="Net-RT hurdle for clear fraction (default 10)",
    )
    p.add_argument(
        "--no-funding",
        action="store_true",
        help="Skip funding adjustment (trading fees still applied)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional JSON output path",
    )
    p.add_argument(
        "--json-only",
        action="store_true",
        help="Print only JSON (no human summary)",
    )
    args = p.parse_args(argv)

    db = args.db
    if not db.is_file():
        print(f"error: ledger not found: {db}", file=sys.stderr)
        return 2

    ledger = KeelLedger(db)
    try:
        result = compute_full_gate_markout(
            ledger._get_conn(),
            hours=float(args.hours),
            horizons=_parse_horizons(args.horizons),
            market_source=args.market_source,
            inst_id=args.inst_id,
            apply_funding=not args.no_funding,
            clear_hurdle_bps=float(args.clear_hurdle_bps),
        )
    finally:
        ledger.close()

    if args.out:
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        if not args.json_only:
            print(f"wrote {args.out}")
    if args.json_only:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_summary(result)
        print("--- json ---")
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
