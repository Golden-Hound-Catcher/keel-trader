#!/usr/bin/env python3
"""
F0b: offline OKX historical candle backtest under E3.1 rules.

Pulls public OKX candles (paginated), walks closed 15m bars with worker-like
snapshots, TF+require_4h+E2B + fire cooldown, fee-aware markout (taker RT).

Never writes .env, never uses OKX keys, never pushes/restarts.

  PYTHONPATH=. python scripts/okx_history_rule_backtest.py \
    --bars-15m 700 --cooldown-seconds 900 --variant trend_follow
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

os.environ.setdefault("KEEL_SKIP_DOTENV", "1")

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from keel.backtest.okx_history_rule import (  # noqa: E402
    DEFAULT_COOLDOWN_SECONDS,
    rows_to_series,
    walk_forward_backtest,
)
from keel.exchange.okx_public import fetch_candles_paginated  # noqa: E402
from keel.ledger.shadow_markout import DEFAULT_MARKOUT_HORIZONS_SECONDS  # noqa: E402
from keel.ledger.tf_fire_replay import normalize_variant  # noqa: E402

_DEFAULT_INST = "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP"


def _fmt_rate(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * float(x):.2f}%"


def _fmt_bps(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{float(x):+.2f}bps"


def _print_summary(summary: dict) -> None:
    print("=== F0b OKX history rule backtest (E3.1) ===")
    print(
        f"variant={summary.get('variant')} require_4h={summary.get('require_4h')} "
        f"macd_lag_bps={summary.get('macd_lag_bps')} "
        f"cooldown_s={summary.get('cooldown_seconds')}"
    )
    print(
        f"n_steps={summary.get('n_steps')} full_gate={summary.get('full_gate_count')} "
        f"rate={_fmt_rate(summary.get('full_gate_rate'))} "
        f"cooldown_suppressed={summary.get('cooldown_suppressed')}"
    )
    print(f"by_action={summary.get('by_action')}")
    mo = summary.get("markout") or {}
    print(
        f"5m netRT: win={_fmt_rate(mo.get('win_rate_net_rt_5m'))} "
        f"avg={_fmt_bps(mo.get('avg_net_rt_bps_5m'))} "
        f"frac_clear_10bps={_fmt_rate(mo.get('frac_clear_10bps_5m'))}"
    )
    h900 = (mo.get("by_horizon") or {}).get("900") or {}
    if h900:
        print(
            f"900s netRT: win={_fmt_rate(h900.get('win_rate_net_rt'))} "
            f"avg={_fmt_bps(h900.get('avg_net_rt_bps'))} "
            f"frac_clear_10bps={_fmt_rate(h900.get('frac_clear_hurdle'))}"
        )
    print(f"fee_model={mo.get('fee_model')}")
    print("--- by_horizon ---")
    for h, row in (mo.get("by_horizon") or {}).items():
        print(
            f"  {h}s: n={row.get('n_available')} "
            f"win_netRT={_fmt_rate(row.get('win_rate_net_rt'))} "
            f"avg_netRT={_fmt_bps(row.get('avg_net_rt_bps'))} "
            f"frac_clear={_fmt_rate(row.get('frac_clear_hurdle'))}"
        )
    print("--- by_instrument ---")
    for inst, info in (summary.get("by_instrument") or {}).items():
        print(
            f"  {inst}: n_steps={info.get('n_steps')} "
            f"full_gate={info.get('full_gate')} "
            f"rate={_fmt_rate(info.get('full_gate_rate'))} "
            f"actions={info.get('by_action')}"
        )
    print(f"note: {summary.get('note_vs_live_post_e31')}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Offline OKX candle backtest under E3.1 TF+require_4h+E2B+cooldown "
            "(public API only; no keys / no .env write)"
        )
    )
    p.add_argument(
        "--inst-ids",
        default=_DEFAULT_INST,
        help=f"Comma-separated SWAP ids (default {_DEFAULT_INST})",
    )
    p.add_argument(
        "--bars-15m",
        type=int,
        default=700,
        help="Max 15m bars to fetch per inst (~7d=672; default 700)",
    )
    p.add_argument(
        "--cooldown-seconds",
        type=int,
        default=DEFAULT_COOLDOWN_SECONDS,
        help="Per-inst fire cooldown seconds (default 900)",
    )
    p.add_argument(
        "--variant",
        default="trend_follow",
        help="Rule variant: trend_follow (default) | mean_revert",
    )
    p.add_argument(
        "--no-require-4h",
        action="store_true",
        help="Disable KEEL_RULE_TF_REQUIRE_4H for this run",
    )
    p.add_argument(
        "--json-out",
        default=None,
        help="Optional path to write full JSON summary",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout per OKX page (default 15)",
    )
    args = p.parse_args(argv)

    inst_ids = [x.strip() for x in str(args.inst_ids).split(",") if x.strip()]
    if not inst_ids:
        print("No --inst-ids", file=sys.stderr)
        return 2

    bars_15m = max(64, int(args.bars_15m))
    # Aligned higher-TF history: cover the 15m wall span + EMA lookback.
    bars_1h = max(100, bars_15m // 4 + 40)
    bars_4h = max(80, bars_15m // 16 + 40)

    series_list = []
    for inst in inst_ids:
        print(f"fetching {inst} 15m≤{bars_15m} 1H≤{bars_1h} 4H≤{bars_4h} ...")
        rows_15 = fetch_candles_paginated(
            inst, bar="15m", max_bars=bars_15m, timeout=float(args.timeout)
        )
        rows_1h = fetch_candles_paginated(
            inst, bar="1H", max_bars=bars_1h, timeout=float(args.timeout)
        )
        rows_4h = fetch_candles_paginated(
            inst, bar="4H", max_bars=bars_4h, timeout=float(args.timeout)
        )
        print(
            f"  got 15m={len(rows_15)} 1H={len(rows_1h)} 4H={len(rows_4h)}"
        )
        if len(rows_15) < 64:
            print(f"  skip {inst}: insufficient 15m history", file=sys.stderr)
            continue
        series_list.append(rows_to_series(inst, rows_15, rows_1h, rows_4h))

    if not series_list:
        print("No series loaded", file=sys.stderr)
        return 1

    summary = walk_forward_backtest(
        series_list,
        variant=normalize_variant(args.variant),
        cooldown_seconds=int(args.cooldown_seconds),
        require_4h=not bool(args.no_require_4h),
        horizons=DEFAULT_MARKOUT_HORIZONS_SECONDS,
    )
    # Attach fetch meta for JSON.
    summary["fetch"] = {
        "inst_ids": [s.inst_id for s in series_list],
        "bars_15m_requested": bars_15m,
        "bars_15m_got": {s.inst_id: len(s.candles_15m) for s in series_list},
        "bars_1h_got": {s.inst_id: len(s.candles_1h) for s in series_list},
        "bars_4h_got": {s.inst_id: len(s.candles_4h) for s in series_list},
    }
    _print_summary(summary)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Trim bulky entries markouts detail optionally kept.
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
