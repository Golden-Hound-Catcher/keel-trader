#!/usr/bin/env python3
"""
F2c: side-by-side OKX history strategy compare on the same candles.

A) trend_follow E3.1 (ext=0, pullback=0) — baseline fixed-horizon markout
B) mean_revert (same cooldown)
C) same TF fires as A with ATR barrier exit (TP 2.2×ATR / SL 1.0×ATR /
   timeout 900s) — measurement only vs fixed 300s

Public API only; never writes .env; never uses OKX keys.

  PYTHONPATH=. python scripts/okx_history_strategy_compare.py \
    --bars-15m 700 --cooldown-seconds 900 \
    --json-out /tmp/keel_f2c_strategy_compare.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

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
    DEFAULT_BARRIER_SL_ATR,
    DEFAULT_BARRIER_TIMEOUT_SECONDS,
    DEFAULT_BARRIER_TP_ATR,
    DEFAULT_COOLDOWN_SECONDS,
    rows_to_series,
    walk_forward_backtest,
)
from keel.exchange.okx_public import fetch_candles_paginated  # noqa: E402
from keel.ledger.shadow_markout import DEFAULT_MARKOUT_HORIZONS_SECONDS  # noqa: E402

_DEFAULT_INST = "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP"
_TARGET_WIN = 0.55


def _fmt_rate(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * float(x):.2f}%"


def _fmt_bps(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{float(x):+.2f}bps"


def _row_from_summary(label: str, summary: dict[str, Any], *, barrier: bool = False) -> dict[str, Any]:
    mo = summary.get("markout") or {}
    if barrier:
        b = mo.get("barrier") or {}
        return {
            "label": label,
            "variant": summary.get("variant"),
            "full_gate": summary.get("full_gate_count"),
            "metric": "barrier_tp_sl_timeout",
            "n_available": b.get("n_available"),
            "win_rate_net_rt": b.get("win_rate_net_rt"),
            "avg_net_rt_bps": b.get("avg_net_rt_bps"),
            "frac_clear_hurdle": b.get("frac_clear_hurdle"),
            "by_exit_reason": b.get("by_exit_reason"),
        }
    return {
        "label": label,
        "variant": summary.get("variant"),
        "full_gate": summary.get("full_gate_count"),
        "metric": "fixed_300s",
        "n_available": (mo.get("by_horizon") or {}).get("300", {}).get("n_available"),
        "win_rate_net_rt": mo.get("win_rate_net_rt_5m"),
        "avg_net_rt_bps": mo.get("avg_net_rt_bps_5m"),
        "frac_clear_hurdle": mo.get("frac_clear_10bps_5m"),
        "by_exit_reason": None,
    }


def _print_table(rows: list[dict[str, Any]]) -> None:
    print("=== F2c strategy compare (same candles) ===")
    hdr = (
        f"{'label':<28} {'FG':>4} {'metric':<22} {'win':>8} {'avg_net':>10} "
        f"{'frac_clear':>10}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['label']:<28} {str(r.get('full_gate')):>4} "
            f"{str(r.get('metric')):<22} "
            f"{_fmt_rate(r.get('win_rate_net_rt')):>8} "
            f"{_fmt_bps(r.get('avg_net_rt_bps')):>10} "
            f"{_fmt_rate(r.get('frac_clear_hurdle')):>10}"
        )
        if r.get("by_exit_reason"):
            print(f"  barrier exits: {r['by_exit_reason']}")


def _verdict(rows: list[dict[str, Any]]) -> str:
    bits: list[str] = []
    best = None
    for r in rows:
        wr = r.get("win_rate_net_rt")
        if wr is None:
            continue
        if best is None or float(wr) > float(best[1]):
            best = (r["label"], float(wr), r.get("full_gate") or 0)
        if float(wr) >= _TARGET_WIN and (r.get("full_gate") or 0) >= 20:
            bits.append(
                f"{r['label']} meets ≥{_TARGET_WIN:.0%} win with FG≥20 "
                f"(win={_fmt_rate(wr)}, FG={r.get('full_gate')})."
            )
    if bits:
        return " ".join(bits) + " Still E0 freeze — do not arm from this alone."
    if best is None:
        return (
            f"No usable markout samples. Keep waiting / gather more candles. "
            f"Target win≥{_TARGET_WIN:.0%} unmet. E0 freeze."
        )
    label, wr, fg = best
    return (
        f"Honest verdict: none reach win≥{_TARGET_WIN:.0%} with usable FG "
        f"(best={label} win={_fmt_rate(wr)} FG={fg}). "
        f"Recommendation: keep waiting on live post_e31 OR change family "
        f"(MR vs TF) only if MR clearly dominates on same candles; treat "
        f"barrier as measurement only (not a live exit change). E0 freeze."
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "F2c: side-by-side TF vs mean_revert vs TF-barrier on same OKX "
            "candles (public API; no keys / no .env write)"
        )
    )
    p.add_argument("--inst-ids", default=_DEFAULT_INST)
    p.add_argument("--bars-15m", type=int, default=700)
    p.add_argument(
        "--cooldown-seconds",
        type=int,
        default=DEFAULT_COOLDOWN_SECONDS,
    )
    p.add_argument("--json-out", default=None)
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument(
        "--skip-barrier",
        action="store_true",
        help="Skip optional C) TF barrier markout",
    )
    args = p.parse_args(argv)

    inst_ids = [x.strip() for x in str(args.inst_ids).split(",") if x.strip()]
    if not inst_ids:
        print("No --inst-ids", file=sys.stderr)
        return 2

    bars_15m = max(64, int(args.bars_15m))
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
        print(f"  got 15m={len(rows_15)} 1H={len(rows_1h)} 4H={len(rows_4h)}")
        if len(rows_15) < 64:
            print(f"  skip {inst}: insufficient 15m history", file=sys.stderr)
            continue
        series_list.append(rows_to_series(inst, rows_15, rows_1h, rows_4h))

    if not series_list:
        print("No series loaded", file=sys.stderr)
        return 1

    cd = int(args.cooldown_seconds)
    horizons = DEFAULT_MARKOUT_HORIZONS_SECONDS

    print("running A) trend_follow E3.1 ext=0 pullback=0 ...")
    a = walk_forward_backtest(
        series_list,
        variant="trend_follow",
        cooldown_seconds=cd,
        require_4h=True,
        horizons=horizons,
        include_barrier=not bool(args.skip_barrier),
        max_extension_atr=0.0,
        pullback=False,
    )

    print("running B) mean_revert (same cooldown) ...")
    b = walk_forward_backtest(
        series_list,
        variant="mean_revert",
        cooldown_seconds=cd,
        require_4h=True,  # ignored by MR; kept for env parity
        horizons=horizons,
        include_barrier=False,
        max_extension_atr=0.0,
        pullback=False,
    )

    rows = [
        _row_from_summary("A) TF E3.1 ext0 pb0", a, barrier=False),
        _row_from_summary("B) mean_revert", b, barrier=False),
    ]
    if not args.skip_barrier:
        rows.append(
            _row_from_summary(
                "C) TF barrier TP2.2/SL1.0",
                a,
                barrier=True,
            )
        )

    _print_table(rows)
    verdict = _verdict(rows)
    print(f"target_win={_TARGET_WIN:.0%}")
    print(f"verdict: {verdict}")
    print(
        f"barrier geometry: TP={DEFAULT_BARRIER_TP_ATR}×ATR "
        f"SL={DEFAULT_BARRIER_SL_ATR}×ATR timeout={DEFAULT_BARRIER_TIMEOUT_SECONDS}s "
        f"(conservative SL-first if both print same bar)"
    )

    out: dict[str, Any] = {
        "phase": "F2c",
        "target_win_rate": _TARGET_WIN,
        "cooldown_seconds": cd,
        "fetch": {
            "inst_ids": [s.inst_id for s in series_list],
            "bars_15m_requested": bars_15m,
            "bars_15m_got": {s.inst_id: len(s.candles_15m) for s in series_list},
            "bars_1h_got": {s.inst_id: len(s.candles_1h) for s in series_list},
            "bars_4h_got": {s.inst_id: len(s.candles_4h) for s in series_list},
        },
        "comparison": rows,
        "verdict": verdict,
        "A_trend_follow": {k: v for k, v in a.items() if k != "entries"},
        "B_mean_revert": {k: v for k, v in b.items() if k != "entries"},
        "note": (
            "Same candles for A/B/C. C reuses A entries with barrier exit "
            "markout (measurement only — not a live policy change). "
            "E0 freeze unchanged."
        ),
    }
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
