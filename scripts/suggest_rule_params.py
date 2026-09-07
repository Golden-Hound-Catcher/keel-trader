#!/usr/bin/env python3
"""
Phase R4: offline fee-aware Rule param search on a ledger cohort.

Finds modest RSI / volume / rsi_relax settings that produce some non-WAIT
fires with edge_hint_bps ≥ OKX taker round-trip (~10 bps) without flooding
the cohort (default fire-rate cap 25%). **Does not write .env** — recommend only.

  PYTHONPATH=. python scripts/suggest_rule_params.py \\
    --db data/keel_ledger.db --hours 168 --market-source okx_public

  PYTHONPATH=. python scripts/suggest_rule_params.py \\
    --from-ledger /tmp/decisions.jsonl --hurdle-bps 10 --out /tmp/suggest.json
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
from keel.ledger.decision_export import load_export_path  # noqa: E402
from keel.ledger.rule_suggest import (  # noqa: E402
    ComboEval,
    default_grid,
    grid_search,
)


def _load_rows(args: argparse.Namespace) -> tuple[list[dict], str]:
    if args.from_ledger is not None and args.db is not None:
        raise SystemExit("error: use either --from-ledger or --db, not both")

    if args.from_ledger is not None:
        if not args.from_ledger.is_file():
            raise SystemExit(f"error: export not found: {args.from_ledger}")
        rows = load_export_path(args.from_ledger)
        return rows, f"file:{args.from_ledger}"

    db = args.db if args.db is not None else _ROOT / "data" / "keel_ledger.db"
    if not db.is_file():
        raise SystemExit(f"error: ledger not found: {db}")
    ledger = KeelLedger(db)
    try:
        rows = ledger.export_decisions(
            hours=float(args.hours),
            market_source=args.market_source,
            limit=int(args.limit),
            include_factors=True,
        )
    finally:
        ledger.close()
    source = f"db:{db} hours={args.hours} market_source={args.market_source}"
    return rows, source


def _print_eval(rank: int, ev: ComboEval, *, hurdle_bps: float) -> None:
    c = ev.combo
    flag = " OVER_CAP" if ev.over_fire_cap else ""
    top_miss = list(ev.missing_gates.items())[:5]
    print(
        f"#{rank}{flag} rsi_long_max={c.rsi_long_max} rsi_short_min={c.rsi_short_min} "
        f"min_vol={c.min_vol} rsi_relax={'on' if c.rsi_relax else 'off'} "
        f"fires={ev.fire_count}/{ev.cohort_n} ({100.0 * ev.fire_rate:.1f}%) "
        f"fires_edge>={hurdle_bps:g}bps={ev.fires_edge_ge_hurdle} "
        f"edge_ge_hurdle_rate={ev.edge_ge_hurdle_rate:.3f} "
        f"near_signal_rate={ev.near_signal_rate:.3f} "
        f"vol_ok_only={ev.volume_ok_only_misses} "
        f"actions={ev.actions}"
    )
    print(f"    missing_top={top_miss}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Offline fee-aware Rule param grid search (ledger cohort; no .env write)"
        )
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite ledger (default: data/keel_ledger.db when --from-ledger omitted)",
    )
    p.add_argument(
        "--from-ledger",
        type=Path,
        default=None,
        help="JSONL/JSON export from scripts/export_decisions.py",
    )
    p.add_argument("--hours", type=float, default=168.0, help="Lookback when using --db")
    p.add_argument(
        "--market-source",
        default="okx_public",
        choices=("any", "okx_public", "synthetic"),
        help="Filter when using --db (default okx_public)",
    )
    p.add_argument("--limit", type=int, default=5000, help="Max rows when using --db")
    p.add_argument(
        "--hurdle-bps",
        type=float,
        default=10.0,
        help="edge_hint_bps floor (default 10 = OKX taker RT)",
    )
    p.add_argument(
        "--max-fire-rate",
        type=float,
        default=0.25,
        help="Fire-rate cap as fraction of cohort (default 0.25)",
    )
    p.add_argument(
        "--top",
        type=int,
        default=5,
        help="How many ranked recommendations to print (default 5)",
    )
    p.add_argument(
        "--no-rsi-relax-grid",
        action="store_true",
        help="Skip rsi_relax on/off axis (always leave relax enabled)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional JSON path for full ranked results",
    )
    args = p.parse_args(argv)

    # Default to local ledger when neither source is given.
    if args.from_ledger is None and args.db is None:
        args.db = _ROOT / "data" / "keel_ledger.db"

    rows, source = _load_rows(args)
    include_relax = not args.no_rsi_relax_grid
    grid = default_grid(include_rsi_relax=include_relax)

    print(
        f"Keel R4 suggest_rule_params (offline, no .env write) source={source} "
        f"cohort_n={len(rows)} grid_n={len(grid)} hurdle_bps={args.hurdle_bps} "
        f"max_fire_rate={args.max_fire_rate}"
    )
    if not rows:
        print(
            "warning: empty cohort — nothing to search "
            "(check --hours / --market-source / export path)"
        )
        if args.out is not None:
            payload = {
                "source": source,
                "cohort_n": 0,
                "hurdle_bps": args.hurdle_bps,
                "max_fire_rate": args.max_fire_rate,
                "recommendations": [],
                "note": "empty cohort",
            }
            args.out.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"wrote {args.out}")
        print("done")
        return 0

    ranked = grid_search(
        rows,
        grid=grid,
        hurdle_bps=float(args.hurdle_bps),
        max_fire_rate=float(args.max_fire_rate),
        include_rsi_relax=include_relax,
    )

    top_n = max(1, int(args.top))
    print(f"top {min(top_n, len(ranked))} recommendations (ranked):")
    for i, ev in enumerate(ranked[:top_n], start=1):
        _print_eval(i, ev, hurdle_bps=float(args.hurdle_bps))

    under = [e for e in ranked if not e.over_fire_cap and e.fires_edge_ge_hurdle > 0]
    if under:
        best = under[0]
        c = best.combo
        print(
            "recommended (manual .env only — script does not write): "
            f"KEEL_RULE_RSI_LONG_MAX={c.rsi_long_max} "
            f"KEEL_RULE_RSI_SHORT_MIN={c.rsi_short_min} "
            f"KEEL_RULE_MIN_VOLUME_RATIO={c.min_vol} "
            f"KEEL_RULE_RSI_RELAX_ENABLE={'1' if c.rsi_relax else '0'}"
        )
        print(
            f"  rationale: fires_edge>={args.hurdle_bps:g}bps="
            f"{best.fires_edge_ge_hurdle}, fire_rate={100.0 * best.fire_rate:.1f}% "
            f"(cap {100.0 * args.max_fire_rate:.0f}%), "
            f"vol_ok_only_misses={best.volume_ok_only_misses}"
        )
    else:
        any_fire = [e for e in ranked if e.fire_count > 0 and not e.over_fire_cap]
        if any_fire:
            print(
                "note: some under-cap fires exist but none cleared "
                f"edge_hint_bps>={args.hurdle_bps:g}; prefer observe / wider ATR "
                "rather than blindly setting RSI short_min=40"
            )
        else:
            print(
                "note: no under-cap non-WAIT fires in this grid — keep observing; "
                "do not blindly set RSI short_min=40"
            )

    if args.out is not None:
        payload = {
            "source": source,
            "cohort_n": len(rows),
            "hurdle_bps": args.hurdle_bps,
            "max_fire_rate": args.max_fire_rate,
            "grid_n": len(grid),
            "recommendations": [e.as_dict() for e in ranked],
            "top": [e.as_dict() for e in ranked[:top_n]],
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.out}")

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
