#!/usr/bin/env python3
"""
F6: ATR trail exit + ADX regime + SuperTrend soft-entry compare.

Public OKX candles only; never writes .env; strips keys. E0 freeze.

Per entry TF (default 15m,30m,1H):
  1) Same calendar split train [-14d,-7d) / valid last 7d
  2) Modest grid on train (primary = trail netRT if n>0 else barrier)
  3) Freeze ONE config per variant; validate once
  4) Print comparison table

  PYTHONPATH=. python scripts/okx_f6_exit_regime_compare.py \
    --inst-ids BTC-USDT-SWAP \
    --entry-bars 15m,30m,1H \
    --json-out /tmp/keel_f6_exit_regime.json
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

from keel.backtest.f6_compare import (  # noqa: E402
    F6StrategyConfig,
    modest_f6_grids,
    pack_row,
    primary_exit_metrics,
    run_f6_config_on_window,
    select_by_primary_exit,
)
from keel.backtest.multitf import (  # noqa: E402
    EntryTfHorizonSpec,
    bars_to_fetch_for_windows,
    entry_tf_spec,
    rows_to_entry_series,
    series_for_train_markout_multitf,
)
from keel.backtest.train_valid import (  # noqa: E402
    TARGET_VALID_WIN,
    make_train_valid_windows,
)
from keel.exchange.okx_public import fetch_candles_paginated  # noqa: E402

_DEFAULT_INST = "BTC-USDT-SWAP"
_VARIANTS = ("trend_follow", "supertrend", "donchian")


def _fmt_rate(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * float(x):.2f}%"


def _fmt_bps(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{float(x):+.2f}bps"


def _run_variant_on_tf(
    *,
    variant: str,
    grid: list[F6StrategyConfig],
    train_series: list[Any],
    full_series: list[Any],
    windows: Any,
    spec: EntryTfHorizonSpec,
    max_grid: int,
) -> dict[str, Any]:
    cells = grid[:max_grid] if max_grid > 0 else grid
    horizons = spec.markout_horizons_seconds()
    primary_h = spec.primary_seconds
    print(f"\n--- variant={variant} grid={len(cells)} TF={spec.bar} ---")

    train_rows: list[dict[str, Any]] = []
    for i, cfg in enumerate(cells, start=1):
        summary = run_f6_config_on_window(
            train_series,
            cfg,
            decision_ts_min=windows.train_start_ts,
            decision_ts_max=windows.train_end_ts,
            horizons=horizons,
            clear_horizon_seconds=primary_h,
            barrier_timeout_seconds=float(primary_h),
            entry_bar=spec.bar,
            confirm_mid_bar=spec.confirm_mid,
            confirm_high_bar=spec.confirm_high,
        )
        row = pack_row(cfg, summary)
        train_rows.append(row)
        if i % 4 == 0 or i == len(cells):
            print(f"  train progress {i}/{len(cells)} ...")

    ranked = sorted(
        train_rows,
        key=lambda r: (
            float((r.get("primary_exit") or {}).get("win_rate_net_rt") or -1.0),
            float((r.get("primary_exit") or {}).get("avg_net_rt_bps") or -1e9),
            int((r.get("train") or {}).get("full_gate_count") or 0),
        ),
        reverse=True,
    )
    chosen_row, selection_note = select_by_primary_exit(ranked)
    if chosen_row is None:
        return {
            "variant": variant,
            "error": "no_selectable",
            "selection_note": selection_note,
            "grid_size": len(cells),
        }

    chosen_cfg = F6StrategyConfig(**chosen_row["config"])
    pe = chosen_row.get("primary_exit") or {}
    print(f"chosen: {chosen_cfg.label()}")
    print(
        f"train:  FG={chosen_row['train'].get('full_gate_count')} "
        f"pri={pe.get('primary')} win={_fmt_rate(pe.get('win_rate_net_rt'))} "
        f"avg={_fmt_bps(pe.get('avg_net_rt_bps'))} n={pe.get('n')}"
    )
    print(f"note:   {selection_note}")

    print("running frozen chosen on valid ...")
    valid_summary = run_f6_config_on_window(
        full_series,
        chosen_cfg,
        decision_ts_min=windows.valid_start_ts,
        decision_ts_max=windows.valid_end_ts,
        horizons=horizons,
        clear_horizon_seconds=primary_h,
        barrier_timeout_seconds=float(primary_h),
        entry_bar=spec.bar,
        confirm_mid_bar=spec.confirm_mid,
        confirm_high_bar=spec.confirm_high,
    )
    from keel.backtest.train_valid import extract_train_metrics

    valid_trainish = extract_train_metrics(valid_summary)
    valid_pe = primary_exit_metrics(valid_summary)
    wr = valid_pe.get("win_rate_net_rt")
    avg = valid_pe.get("avg_net_rt_bps")
    n = int(valid_pe.get("n") or 0)
    fg = int(valid_trainish.get("full_gate_count") or 0)

    passes = (
        wr is not None
        and float(wr) >= TARGET_VALID_WIN
        and avg is not None
        and float(avg) >= 0.0
        and n >= 20
    )
    if wr is None:
        verdict = f"{variant}/{spec.bar}: valid 无可用 exit；不可宣称成功。E0 freeze。"
    elif passes:
        verdict = (
            f"{variant}/{spec.bar}: valid {valid_pe.get('primary')} "
            f"win={_fmt_rate(wr)} avg={_fmt_bps(avg)} n={n} ≥0.55 & avg≥0 & n≥20 — "
            f"仍须人工审阅；勿翻 .env。E0 freeze。"
        )
    else:
        verdict = (
            f"{variant}/{spec.bar}: valid {valid_pe.get('primary')} "
            f"win={_fmt_rate(wr)} avg={_fmt_bps(avg)} n={n} — "
            f"未过 0.55/avg≥0/n≥20。E0 freeze。"
        )
    print(
        f"valid:  FG={fg} pri={valid_pe.get('primary')} "
        f"win={_fmt_rate(wr)} avg={_fmt_bps(avg)} n={n}"
    )
    print(f"verdict: {verdict}")

    return {
        "variant": variant,
        "selection_note": selection_note,
        "grid_size": len(cells),
        "passes_valid_gate": bool(passes),
        "chosen": {
            "label": chosen_cfg.label(),
            "config": chosen_cfg.to_dict(),
            "train": chosen_row["train"],
            "train_primary_exit": chosen_row.get("primary_exit"),
            "train_barrier": chosen_row.get("barrier"),
            "train_trail": chosen_row.get("trail"),
            "valid": valid_trainish,
            "valid_primary_exit": valid_pe,
            "valid_barrier": (valid_summary.get("markout") or {}).get("barrier"),
            "valid_trail": (valid_summary.get("markout") or {}).get("trail"),
        },
        "train_ranking_top3": [
            {
                "rank": i,
                "label": r["label"],
                "train": r["train"],
                "primary_exit": r["primary_exit"],
            }
            for i, r in enumerate(ranked[:3], start=1)
        ],
        "verdict": verdict,
    }


def _run_one_tf(
    *,
    entry_bar: str,
    inst_ids: list[str],
    windows: Any,
    timeout: float,
    max_grid: int,
) -> dict[str, Any]:
    spec = entry_tf_spec(entry_bar)
    fetch_plan = bars_to_fetch_for_windows(entry_bar)
    print(
        f"\n=== Entry TF {spec.bar} "
        f"(bar={spec.bar_seconds}s primary={spec.primary_bars}bars/"
        f"{spec.primary_seconds}s) ==="
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
            print(f"  skip {inst}: insufficient history", file=sys.stderr)
            continue
        full_series.append(
            rows_to_entry_series(inst, rows_e, rows_m, rows_h, entry_bar=spec.bar)
        )

    if not full_series:
        return {"entry_bar": spec.bar, "error": "no_series", "spec": spec.to_dict()}

    train_series = [
        series_for_train_markout_multitf(s, train_end_ts=windows.train_end_ts)
        for s in full_series
    ]

    grids = modest_f6_grids()
    by_variant: dict[str, Any] = {}
    for variant in _VARIANTS:
        by_variant[variant] = _run_variant_on_tf(
            variant=variant,
            grid=grids[variant],
            train_series=train_series,
            full_series=full_series,
            windows=windows,
            spec=spec,
            max_grid=max_grid,
        )

    return {
        "entry_bar": spec.bar,
        "spec": spec.to_dict(),
        "by_variant": by_variant,
        "fetch": {
            "inst_ids": [s.inst_id for s in full_series],
            "bars_entry_got": {s.inst_id: len(s.candles_15m) for s in full_series},
        },
    }


def _print_summary_table(results: list[dict[str, Any]]) -> None:
    print("\n=== F6 exit/regime compare (trail|barrier primary) ===")
    hdr = (
        f"{'TF':<4} {'variant':<13} {'cfg':<48} "
        f"{'tr_FG':>5} {'tr_pW':>8} {'tr_pAvg':>10} "
        f"{'va_FG':>5} {'va_pW':>8} {'va_pAvg':>10} {'va_n':>5} {'pass':>5}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        if r.get("error"):
            print(f"{r.get('entry_bar','?'):<4} ERROR {r.get('error')}")
            continue
        for variant in _VARIANTS:
            vr = (r.get("by_variant") or {}).get(variant) or {}
            if vr.get("error"):
                print(f"{r['entry_bar']:<4} {variant:<13} ERROR {vr.get('error')}")
                continue
            ch = vr.get("chosen") or {}
            tr = ch.get("train") or {}
            tp = ch.get("train_primary_exit") or {}
            va = ch.get("valid") or {}
            vp = ch.get("valid_primary_exit") or {}
            print(
                f"{r['entry_bar']:<4} {variant:<13} {str(ch.get('label',''))[:48]:<48} "
                f"{str(tr.get('full_gate_count')):>5} "
                f"{_fmt_rate(tp.get('win_rate_net_rt')):>8} "
                f"{_fmt_bps(tp.get('avg_net_rt_bps')):>10} "
                f"{str(va.get('full_gate_count')):>5} "
                f"{_fmt_rate(vp.get('win_rate_net_rt')):>8} "
                f"{_fmt_bps(vp.get('avg_net_rt_bps')):>10} "
                f"{str(vp.get('n')):>5} "
                f"{'YES' if vr.get('passes_valid_gate') else 'no':>5}"
            )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="F6 trail/ADX/ST-soft compare (public API; no keys)"
    )
    p.add_argument(
        "--inst-ids",
        default=_DEFAULT_INST,
        help="Comma-separated OKX swap ids (default BTC-USDT-SWAP)",
    )
    p.add_argument(
        "--entry-bars",
        default="15m,30m,1H",
        help="Comma-separated entry TFs (default 15m,30m,1H)",
    )
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument(
        "--max-grid",
        type=int,
        default=0,
        help="Cap grid cells per variant (0=all modest grid)",
    )
    p.add_argument("--json-out", default="")
    p.add_argument(
        "--now-ts",
        type=float,
        default=0.0,
        help="Override now (unix) for reproducible windows; 0=time.time()",
    )
    args = p.parse_args(argv)

    inst_ids = [x.strip() for x in str(args.inst_ids).split(",") if x.strip()]
    entry_bars = [x.strip() for x in str(args.entry_bars).split(",") if x.strip()]
    now_ts = float(args.now_ts) if float(args.now_ts) > 0 else time.time()
    windows = make_train_valid_windows(now_ts)

    print("F6 exit/regime compare — public candles only; E0 freeze")
    print(
        f"windows: train=[{windows.train_start_ts:.0f},{windows.train_end_ts:.0f}) "
        f"valid=[{windows.valid_start_ts:.0f},{windows.valid_end_ts:.0f})"
    )
    print(f"inst_ids={inst_ids} entry_bars={entry_bars}")

    results: list[dict[str, Any]] = []
    for i, bar in enumerate(entry_bars):
        if i > 0:
            time.sleep(12.0)
        try:
            results.append(
                _run_one_tf(
                    entry_bar=bar,
                    inst_ids=inst_ids,
                    windows=windows,
                    timeout=float(args.timeout),
                    max_grid=int(args.max_grid),
                )
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR on {bar}: {exc}", file=sys.stderr)
            results.append({"entry_bar": bar, "error": str(exc)})
            time.sleep(20.0)

    _print_summary_table(results)

    any_pass = any(
        bool((vr or {}).get("passes_valid_gate"))
        for r in results
        for vr in (r.get("by_variant") or {}).values()
    )
    if not any_pass:
        print(
            "\nHonest: no variant/TF cleared valid trail|barrier ≥0.55 with "
            "avg_net≥0 and n≥20. Do not flip .env / clear kill / enable near_probe."
        )
    else:
        print(
            "\nAt least one cell cleared valid gate — still do NOT flip .env; "
            "open PR for Jo review. E0 freeze."
        )

    payload = {
        "phase": "F6",
        "protocol": "exit_regime_trail_primary",
        "windows": windows.to_dict(),
        "inst_ids": inst_ids,
        "entry_bars": entry_bars,
        "results": results,
        "any_passes_valid_gate": bool(any_pass),
        "generated_at": time.time(),
    }
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"wrote {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
