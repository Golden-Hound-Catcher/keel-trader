#!/usr/bin/env python3
"""
F4: multi-timeframe short-vs-short / long-vs-long train/valid (Jo protocol).

Per entry timeframe T ∈ {5m, 15m, 30m, 1H, 4H}:
  1) Fetch OKX public candles for T + confirm mid/high
  2) Same calendar split: train [-14d,-7d), valid [-7d, now)
  3) Markout / barrier horizons = bar multiples of T (NOT fixed 300s)
  4) Modest grid on train only → freeze ONE config → validate once
  5) Print table: train best + valid win/avg/n per T

Public API only; never writes .env; never uses OKX keys. E0 freeze.

  PYTHONPATH=. python scripts/okx_multitf_train_valid.py \
    --inst-ids BTC-USDT-SWAP \
    --entry-bars 5m,15m,30m,1H,4H \
    --json-out /tmp/keel_f4_multitf.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
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

from keel.backtest.multitf import (  # noqa: E402
    ENTRY_BARS,
    EntryTfHorizonSpec,
    bars_to_fetch_for_windows,
    entry_tf_spec,
    modest_multitf_grid,
    rows_to_entry_series,
    series_for_train_markout_multitf,
)
from keel.backtest.train_valid import (  # noqa: E402
    MIN_TRAIN_AVG_NET_RT_BPS,
    MIN_TRAIN_FG,
    TARGET_VALID_WIN,
    StrategyConfig,
    extract_train_metrics,
    make_train_valid_windows,
    rank_train_rows,
    run_config_on_window,
    select_primary_strategy,
)
from keel.exchange.okx_public import fetch_candles_paginated  # noqa: E402

_DEFAULT_INST = "BTC-USDT-SWAP"


def _fmt_rate(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * float(x):.2f}%"


def _fmt_bps(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{float(x):+.2f}bps"


def _run_one_tf(
    *,
    entry_bar: str,
    inst_ids: list[str],
    windows: Any,
    timeout: float,
    max_grid: int,
    skip_barrier: bool,
) -> dict[str, Any]:
    spec: EntryTfHorizonSpec = entry_tf_spec(entry_bar)
    fetch_plan = bars_to_fetch_for_windows(entry_bar)
    horizons = spec.markout_horizons_seconds()
    primary_h = spec.primary_seconds
    print(
        f"\n=== Entry TF {spec.bar} "
        f"(bar={spec.bar_seconds}s primary={spec.primary_bars}bars/"
        f"{primary_h}s secondary={spec.secondary_bars}bars/"
        f"{spec.secondary_seconds}s) ==="
    )
    print(
        f"confirm mid={spec.confirm_mid} high={spec.confirm_high} "
        f"horizons_s={list(horizons)} cooldown_bars={list(spec.cooldown_bars)}"
    )

    full_series = []
    for inst in inst_ids:
        n_e = int(fetch_plan["entry"])
        n_m = int(fetch_plan["mid"])
        n_h = int(fetch_plan["high"])
        print(
            f"fetching {inst} {spec.bar}≤{n_e} "
            f"{spec.confirm_mid}≤{n_m} {spec.confirm_high}≤{n_h} ..."
        )
        rows_e = fetch_candles_paginated(
            inst, bar=spec.bar, max_bars=n_e, timeout=timeout
        )
        rows_m = fetch_candles_paginated(
            inst, bar=spec.confirm_mid, max_bars=n_m, timeout=timeout
        )
        rows_h = fetch_candles_paginated(
            inst, bar=spec.confirm_high, max_bars=n_h, timeout=timeout
        )
        print(
            f"  got {spec.bar}={len(rows_e)} "
            f"{spec.confirm_mid}={len(rows_m)} {spec.confirm_high}={len(rows_h)}"
        )
        if len(rows_e) < 64:
            print(f"  skip {inst}: insufficient {spec.bar} history", file=sys.stderr)
            continue
        full_series.append(
            rows_to_entry_series(
                inst, rows_e, rows_m, rows_h, entry_bar=spec.bar
            )
        )

    if not full_series:
        return {
            "entry_bar": spec.bar,
            "error": "no_series",
            "spec": spec.to_dict(),
        }

    train_series = [
        series_for_train_markout_multitf(s, train_end_ts=windows.train_end_ts)
        for s in full_series
    ]
    for s, t in zip(full_series, train_series):
        print(
            f"  {s.inst_id}: full_entry={len(s.candles_15m)} "
            f"train_trunc={len(t.candles_15m)}"
        )

    grid = modest_multitf_grid(spec.bar)
    if max_grid > 0:
        grid = grid[:max_grid]
    print(f"grid cells={len(grid)} (train-only; horizon-aligned to {spec.bar})")

    train_rows: list[dict[str, Any]] = []
    for i, cfg in enumerate(grid, start=1):
        summary = run_config_on_window(
            train_series,
            cfg,
            decision_ts_min=windows.train_start_ts,
            decision_ts_max=windows.train_end_ts,
            include_barrier=False,
            horizons=horizons,
            clear_horizon_seconds=primary_h,
            entry_bar=spec.bar,
            confirm_mid_bar=spec.confirm_mid,
            confirm_high_bar=spec.confirm_high,
        )
        metrics = extract_train_metrics(summary)
        gates_ok = (
            int(metrics["full_gate_count"]) >= MIN_TRAIN_FG
            and metrics.get("avg_net_rt_bps_5m") is not None
            and float(metrics["avg_net_rt_bps_5m"]) >= MIN_TRAIN_AVG_NET_RT_BPS
        )
        train_rows.append(
            {"config": cfg, "train": metrics, "gates_ok": gates_ok}
        )
        if i % 5 == 0 or i == len(grid):
            print(f"  train progress {i}/{len(grid)} ...")

    ranked = rank_train_rows(train_rows)
    chosen_row, selection_note = select_primary_strategy(ranked)
    if chosen_row is None:
        return {
            "entry_bar": spec.bar,
            "error": "no_selectable",
            "spec": spec.to_dict(),
            "selection_note": selection_note,
        }

    chosen_cfg: StrategyConfig = chosen_row["config"]
    print(f"chosen: {chosen_cfg.label()}")
    print(
        f"train:  FG={chosen_row['train'].get('full_gate_count')} "
        f"win={_fmt_rate(chosen_row['train'].get('win_rate_net_rt_5m'))} "
        f"avg={_fmt_bps(chosen_row['train'].get('avg_net_rt_bps_5m'))} "
        f"(primary={primary_h}s)"
    )
    print(f"note:   {selection_note}")

    print("running frozen chosen on valid ...")
    chosen_valid_summary = run_config_on_window(
        full_series,
        chosen_cfg,
        decision_ts_min=windows.valid_start_ts,
        decision_ts_max=windows.valid_end_ts,
        include_barrier=not skip_barrier,
        horizons=horizons,
        clear_horizon_seconds=primary_h,
        barrier_timeout_seconds=float(primary_h),
        entry_bar=spec.bar,
        confirm_mid_bar=spec.confirm_mid,
        confirm_high_bar=spec.confirm_high,
    )
    chosen_valid = extract_train_metrics(chosen_valid_summary)

    wr = chosen_valid.get("win_rate_net_rt_5m")
    if wr is None:
        verdict = f"{spec.bar}: valid 无可用 primary markout；不可宣称成功。E0 freeze。"
    elif float(wr) >= TARGET_VALID_WIN:
        verdict = (
            f"{spec.bar}: valid primary win={_fmt_rate(wr)} ≥ "
            f"{TARGET_VALID_WIN:.0%} — 仍须人工审阅；E0 freeze。"
        )
    else:
        verdict = (
            f"{spec.bar}: valid primary win={_fmt_rate(wr)} ≪ "
            f"{TARGET_VALID_WIN:.0%} — 不宣称成功。E0 freeze。"
        )
    print(f"valid:  FG={chosen_valid.get('full_gate_count')} "
          f"win={_fmt_rate(wr)} "
          f"avg={_fmt_bps(chosen_valid.get('avg_net_rt_bps_5m'))}")
    print(f"verdict: {verdict}")

    return {
        "entry_bar": spec.bar,
        "spec": spec.to_dict(),
        "selection_note": selection_note,
        "grid_size": len(grid),
        "chosen": {
            "label": chosen_cfg.label(),
            "config": chosen_cfg.to_dict(),
            "train": chosen_row["train"],
            "valid": chosen_valid,
        },
        "train_ranking_top3": [
            {
                "rank": i,
                "label": r["config"].label(),
                "train": r["train"],
                "gates_ok": r["gates_ok"],
            }
            for i, r in enumerate(ranked[:3], start=1)
        ],
        "verdict": verdict,
        "fetch": {
            "inst_ids": [s.inst_id for s in full_series],
            "bars_entry_got": {
                s.inst_id: len(s.candles_15m) for s in full_series
            },
            "bars_mid_got": {s.inst_id: len(s.candles_1h) for s in full_series},
            "bars_high_got": {
                s.inst_id: len(s.candles_4h) for s in full_series
            },
            "train_trunc_entry": {
                s.inst_id: len(t.candles_15m)
                for s, t in zip(full_series, train_series)
            },
        },
    }


def _print_summary_table(results: list[dict[str, Any]]) -> None:
    print("\n=== F4 multi-TF summary (short-vs-short / long-vs-long) ===")
    hdr = (
        f"{'TF':<4} {'prim_s':>7} {'cfg':<44} "
        f"{'tr_FG':>5} {'tr_win':>8} {'tr_avg':>10} "
        f"{'va_FG':>5} {'va_win':>8} {'va_avg':>10} {'va_n':>5}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        if r.get("error"):
            print(f"{r.get('entry_bar','?'):<4} ERROR {r.get('error')}")
            continue
        ch = r["chosen"]
        tr = ch["train"]
        va = ch["valid"]
        primary_s = int((r.get("spec") or {}).get("primary_seconds") or 0)
        prim = va.get("primary_markout") or {}
        n = prim.get("n_available")
        if n is None:
            n = va.get("full_gate_count")
        print(
            f"{r['entry_bar']:<4} {primary_s:>7} {ch['label'][:44]:<44} "
            f"{str(tr.get('full_gate_count')):>5} "
            f"{_fmt_rate(tr.get('win_rate_net_rt_5m')):>8} "
            f"{_fmt_bps(tr.get('avg_net_rt_bps_5m')):>10} "
            f"{str(va.get('full_gate_count')):>5} "
            f"{_fmt_rate(va.get('win_rate_net_rt_5m')):>8} "
            f"{_fmt_bps(va.get('avg_net_rt_bps_5m')):>10} "
            f"{str(n):>5}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "F4 multi-TF train/valid (horizon-aligned; public API; no keys)"
        )
    )
    p.add_argument("--inst-ids", default=_DEFAULT_INST)
    p.add_argument(
        "--entry-bars",
        default=",".join(ENTRY_BARS),
        help="Comma-separated entry TFs (default: 5m,15m,30m,1H,4H)",
    )
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--json-out", default=None)
    p.add_argument("--skip-barrier", action="store_true")
    p.add_argument("--now-ts", type=float, default=None)
    p.add_argument(
        "--max-grid",
        type=int,
        default=0,
        help="Optional cap on per-TF grid size (0=full modest multitf grid)",
    )
    args = p.parse_args(argv)

    inst_ids = [x.strip() for x in str(args.inst_ids).split(",") if x.strip()]
    entry_bars = [
        x.strip() for x in str(args.entry_bars).split(",") if x.strip()
    ]
    if not inst_ids or not entry_bars:
        print("Need --inst-ids and --entry-bars", file=sys.stderr)
        return 2

    for b in entry_bars:
        entry_tf_spec(b)  # validate early

    now_ts = float(args.now_ts) if args.now_ts is not None else time.time()
    windows = make_train_valid_windows(now_ts)
    print("=== F4 Multi-TF Train/Valid（短对短 / 长对长；禁止偷看）===")
    print(
        f"now_ts={windows.now_ts:.0f} "
        f"train=[{windows.train_start_ts:.0f},{windows.train_end_ts:.0f}) "
        f"valid=[{windows.valid_start_ts:.0f},{windows.valid_end_ts:.0f})"
    )
    print(
        f"same calendar split for all T; "
        f"gates: FG≥{MIN_TRAIN_FG}, avg_net≥{MIN_TRAIN_AVG_NET_RT_BPS}bps; "
        f"target valid win {TARGET_VALID_WIN:.0%}"
    )
    print(f"entry_bars={entry_bars} inst={inst_ids}")

    results: list[dict[str, Any]] = []
    for bar in entry_bars:
        results.append(
            _run_one_tf(
                entry_bar=bar,
                inst_ids=inst_ids,
                windows=windows,
                timeout=float(args.timeout),
                max_grid=int(args.max_grid),
                skip_barrier=bool(args.skip_barrier),
            )
        )

    _print_summary_table(results)

    print("\n--- residuals ---")
    print(
        "horizon alignment: primary/secondary are bar multiples of entry T; "
        "do not compare 5m entry@300s vs 4H entry@300s."
    )
    print(
        "look-ahead: higher-TF closed-bar filter + train series truncate at "
        "train_end; valid may use train candles as lookback only."
    )
    print(
        "fee model: taker RT ≈ 2× open_fee_bps (~10bps); funding ignored."
    )
    print(
        "grid bias: modest per-T grid; selection on train win — valid is the "
        "only honest score. E0 freeze."
    )

    any_ok = False
    for r in results:
        wr = ((r.get("chosen") or {}).get("valid") or {}).get(
            "win_rate_net_rt_5m"
        )
        if wr is not None and float(wr) >= TARGET_VALID_WIN:
            any_ok = True
    if not any_ok:
        print(
            "overall: all TFs ≪ 0.55 or n/a — do not claim success / do not arm."
        )

    out: dict[str, Any] = {
        "phase": "F4",
        "protocol": (
            "multi-TF short-vs-short/long-vs-long; "
            "train[-14d,-7d)/valid[-7d,now); horizons=bar multiples of T"
        ),
        "windows": windows.to_dict(),
        "selection_gates": {
            "min_train_fg": MIN_TRAIN_FG,
            "min_train_avg_net_rt_bps": MIN_TRAIN_AVG_NET_RT_BPS,
            "target_valid_win": TARGET_VALID_WIN,
        },
        "entry_bars": entry_bars,
        "inst_ids": inst_ids,
        "by_timeframe": results,
        "residuals": [
            "horizon-aligned bar multiples (no fixed-300s cross-mix)",
            "closed higher-TF + train truncate look-ahead control",
            "taker RT fee model; funding ignored",
            "per-T modest grid selection bias",
        ],
        "e0_freeze": True,
    }
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
