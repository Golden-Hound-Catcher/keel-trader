#!/usr/bin/env python3
"""
F3: train/validation strategy pipeline on OKX public history (Jo protocol).

1) Analysis set (train): closed bars in [-14d, -7d)
2) Validation set: closed bars in [-7d, now)
3) Grid-search params ONLY on train (fee-aware 5m netRT)
4) Freeze ONE config by pre-declared rule; score once on valid
5) Compare chosen vs F0b baseline on the valid window alone

Public API only; never writes .env; never uses OKX keys.

  PYTHONPATH=. python scripts/okx_train_valid_strategy.py \
    --bars-15m 2200 --json-out /tmp/keel_f3_train_valid.json
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

from keel.backtest.okx_history_rule import rows_to_series  # noqa: E402
from keel.backtest.train_valid import (  # noqa: E402
    MIN_TRAIN_AVG_NET_RT_BPS,
    MIN_TRAIN_FG,
    TARGET_VALID_WIN,
    StrategyConfig,
    extract_train_metrics,
    f0b_baseline_config,
    make_train_valid_windows,
    modest_strategy_grid,
    rank_train_rows,
    run_config_on_window,
    select_primary_strategy,
    series_for_train_markout,
)
from keel.exchange.okx_public import fetch_candles_paginated  # noqa: E402
from keel.ledger.shadow_markout import DEFAULT_MARKOUT_HORIZONS_SECONDS  # noqa: E402

_DEFAULT_INST = "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP"


def _fmt_rate(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * float(x):.2f}%"


def _fmt_bps(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{float(x):+.2f}bps"


def _metrics_cells(m: dict[str, Any]) -> str:
    return (
        f"FG={m.get('full_gate_count')} "
        f"win={_fmt_rate(m.get('win_rate_net_rt_5m'))} "
        f"avg={_fmt_bps(m.get('avg_net_rt_bps_5m'))} "
        f"frac10={_fmt_rate(m.get('frac_clear_10bps_5m'))}"
    )


def _print_train_top(rows: list[dict[str, Any]], *, top: int = 5) -> None:
    print("=== F3 训练集排名 Top5（仅 train 指标；无偷看 valid）===")
    hdr = (
        f"{'#':>2} {'label':<52} {'FG':>4} {'win5m':>8} {'avg_net':>10} "
        f"{'frac10':>8} {'gates_ok':>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    for i, row in enumerate(rows[:top], start=1):
        cfg: StrategyConfig = row["config"]
        t = row["train"]
        gates = "Y" if row.get("gates_ok") else "N"
        print(
            f"{i:>2} {cfg.label():<52} {str(t.get('full_gate_count')):>4} "
            f"{_fmt_rate(t.get('win_rate_net_rt_5m')):>8} "
            f"{_fmt_bps(t.get('avg_net_rt_bps_5m')):>10} "
            f"{_fmt_rate(t.get('frac_clear_10bps_5m')):>8} "
            f"{gates:>8}"
        )


def _print_valid_compare(
    chosen_label: str,
    chosen_valid: dict[str, Any],
    baseline_valid: dict[str, Any],
    *,
    selection_note: str,
) -> None:
    print("=== F3 验证集（冻结参数，仅 valid 窗口）===")
    print(f"selection: {selection_note}")
    print(f"chosen:   {chosen_label}")
    print(f"  valid:  {_metrics_cells(chosen_valid)}")
    h900 = chosen_valid.get("by_horizon_900") or {}
    if h900:
        print(
            f"  900s:   win={_fmt_rate(h900.get('win_rate_net_rt'))} "
            f"avg={_fmt_bps(h900.get('avg_net_rt_bps'))} "
            f"n={h900.get('n_available')}"
        )
    bar = chosen_valid.get("barrier") or {}
    if bar:
        print(
            f"  barrier: win={_fmt_rate(bar.get('win_rate_net_rt'))} "
            f"avg={_fmt_bps(bar.get('avg_net_rt_bps'))} "
            f"n={bar.get('n_available')} exits={bar.get('by_exit_reason')}"
        )
    print("F0b baseline on same valid window:")
    print(f"  valid:  {_metrics_cells(baseline_valid)}")
    wr = chosen_valid.get("win_rate_net_rt_5m")
    if wr is None:
        verdict = "valid 无可用 5m markout；不可宣称成功。E0 freeze。"
    elif float(wr) >= TARGET_VALID_WIN:
        verdict = (
            f"valid 5m win={_fmt_rate(wr)} ≥ {TARGET_VALID_WIN:.0%} — "
            f"仍须人工审阅；E0 freeze（不自动 arm）。"
        )
    else:
        verdict = (
            f"valid 5m win={_fmt_rate(wr)} ≪ {TARGET_VALID_WIN:.0%} — "
            f"不宣称成功；继续等待 / 换家族前勿 arm。E0 freeze。"
        )
    print(f"verdict: {verdict}")
    return verdict  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "F3 train/valid OKX history strategy (public API; no keys / no .env write)"
        )
    )
    p.add_argument("--inst-ids", default=_DEFAULT_INST)
    p.add_argument(
        "--bars-15m",
        type=int,
        default=2200,
        help="15m bars to fetch (~14d decisions + lookback; 14d≈1344, 21d≈2016)",
    )
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--json-out", default=None)
    p.add_argument(
        "--skip-barrier",
        action="store_true",
        help="Skip barrier markout on the frozen valid run",
    )
    p.add_argument(
        "--now-ts",
        type=float,
        default=None,
        help="Override 'now' Unix seconds (tests / reproducibility)",
    )
    p.add_argument(
        "--max-grid",
        type=int,
        default=0,
        help="Optional cap on grid size (0=full modest grid)",
    )
    args = p.parse_args(argv)

    inst_ids = [x.strip() for x in str(args.inst_ids).split(",") if x.strip()]
    if not inst_ids:
        print("No --inst-ids", file=sys.stderr)
        return 2

    now_ts = float(args.now_ts) if args.now_ts is not None else time.time()
    windows = make_train_valid_windows(now_ts)
    print("=== F3 Train/Valid protocol（禁止偷看）===")
    print(
        f"now_ts={windows.now_ts:.0f} "
        f"train=[{windows.train_start_ts:.0f},{windows.train_end_ts:.0f}) "
        f"valid=[{windows.valid_start_ts:.0f},{windows.valid_end_ts:.0f})"
    )
    print(
        f"train≈{windows.to_dict()['train_days']:.2f}d "
        f"valid≈{windows.to_dict()['valid_days']:.2f}d "
        f"gates: FG≥{MIN_TRAIN_FG}, avg_net≥{MIN_TRAIN_AVG_NET_RT_BPS}bps; "
        f"select max train 5m win"
    )

    bars_15m = max(200, int(args.bars_15m))
    bars_1h = max(120, bars_15m // 4 + 40)
    bars_4h = max(80, bars_15m // 16 + 40)

    full_series = []
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
        full_series.append(rows_to_series(inst, rows_15, rows_1h, rows_4h))

    if not full_series:
        print("No series loaded", file=sys.stderr)
        return 1

    # Train series: truncate so markouts cannot peek into valid prices.
    train_series = [
        series_for_train_markout(s, train_end_ts=windows.train_end_ts)
        for s in full_series
    ]
    for s, t in zip(full_series, train_series):
        print(
            f"  {s.inst_id}: full_15m={len(s.candles_15m)} "
            f"train_trunc_15m={len(t.candles_15m)}"
        )

    grid = modest_strategy_grid()
    if int(args.max_grid) > 0:
        grid = grid[: int(args.max_grid)]
    print(f"grid cells={len(grid)} (train-only search)")

    horizons = DEFAULT_MARKOUT_HORIZONS_SECONDS
    train_rows: list[dict[str, Any]] = []
    for i, cfg in enumerate(grid, start=1):
        summary = run_config_on_window(
            train_series,
            cfg,
            decision_ts_min=windows.train_start_ts,
            decision_ts_max=windows.train_end_ts,
            include_barrier=False,
            horizons=horizons,
        )
        metrics = extract_train_metrics(summary)
        gates_ok = (
            int(metrics["full_gate_count"]) >= MIN_TRAIN_FG
            and metrics.get("avg_net_rt_bps_5m") is not None
            and float(metrics["avg_net_rt_bps_5m"]) >= MIN_TRAIN_AVG_NET_RT_BPS
        )
        train_rows.append(
            {
                "config": cfg,
                "train": metrics,
                "gates_ok": gates_ok,
            }
        )
        if i % 10 == 0 or i == len(grid):
            print(f"  train progress {i}/{len(grid)} ...")

    ranked = rank_train_rows(train_rows)
    _print_train_top(ranked, top=5)

    chosen_row, selection_note = select_primary_strategy(ranked)
    if chosen_row is None:
        print("No strategy selectable", file=sys.stderr)
        return 1
    chosen_cfg: StrategyConfig = chosen_row["config"]
    print("=== 选定主策略（冻结）===")
    print(f"config: {chosen_cfg.label()}")
    print(f"train:  {_metrics_cells(chosen_row['train'])}")
    print(f"note:   {selection_note}")

    # Validate once on holdout with full series (lookback may include train).
    print("running frozen chosen on valid ...")
    chosen_valid_summary = run_config_on_window(
        full_series,
        chosen_cfg,
        decision_ts_min=windows.valid_start_ts,
        decision_ts_max=windows.valid_end_ts,
        include_barrier=not bool(args.skip_barrier),
        horizons=horizons,
    )
    chosen_valid = extract_train_metrics(chosen_valid_summary)

    baseline_cfg = f0b_baseline_config()
    print("running F0b baseline on valid ...")
    baseline_valid_summary = run_config_on_window(
        full_series,
        baseline_cfg,
        decision_ts_min=windows.valid_start_ts,
        decision_ts_max=windows.valid_end_ts,
        include_barrier=False,
        horizons=horizons,
    )
    baseline_valid = extract_train_metrics(baseline_valid_summary)

    verdict = _print_valid_compare(
        chosen_cfg.label(),
        chosen_valid,
        baseline_valid,
        selection_note=selection_note,
    )

    print("--- residuals ---")
    print(
        "look-ahead: higher-TF closed-bar filter + train series truncate at "
        "train_end; valid decisions may use train candles as lookback only."
    )
    print(
        "fee model: taker RT ≈ 2× open_fee_bps (~10bps hurdle); funding ignored."
    )
    print(
        "grid bias: modest discrete grid (~40); selection on train win rate "
        "overfits noise — valid is the only honest score."
    )

    out: dict[str, Any] = {
        "phase": "F3",
        "protocol": "train[-14d,-7d) / valid[-7d,now); no peeking",
        "windows": windows.to_dict(),
        "selection_gates": {
            "min_train_fg": MIN_TRAIN_FG,
            "min_train_avg_net_rt_bps": MIN_TRAIN_AVG_NET_RT_BPS,
            "target_valid_win": TARGET_VALID_WIN,
        },
        "selection_note": selection_note,
        "grid_size": len(grid),
        "train_ranking_top5": [
            {
                "rank": i,
                "label": r["config"].label(),
                "config": r["config"].to_dict(),
                "train": r["train"],
                "gates_ok": r["gates_ok"],
            }
            for i, r in enumerate(ranked[:5], start=1)
        ],
        "chosen": {
            "label": chosen_cfg.label(),
            "config": chosen_cfg.to_dict(),
            "train": chosen_row["train"],
            "valid": chosen_valid,
        },
        "f0b_baseline_valid": {
            "label": baseline_cfg.label(),
            "config": baseline_cfg.to_dict(),
            "valid": baseline_valid,
        },
        "verdict": verdict,
        "fetch": {
            "inst_ids": [s.inst_id for s in full_series],
            "bars_15m_requested": bars_15m,
            "bars_15m_got": {s.inst_id: len(s.candles_15m) for s in full_series},
            "bars_1h_got": {s.inst_id: len(s.candles_1h) for s in full_series},
            "bars_4h_got": {s.inst_id: len(s.candles_4h) for s in full_series},
            "train_trunc_15m": {
                s.inst_id: len(t.candles_15m)
                for s, t in zip(full_series, train_series)
            },
        },
        "residuals": [
            "look-ahead controls via closed higher-TF + train truncate",
            "taker RT fee model; funding ignored",
            "grid selection bias on train win rate",
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
