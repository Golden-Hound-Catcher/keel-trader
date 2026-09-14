#!/usr/bin/env python3
"""
F2c + P0 + P1 + P2 + P5: side-by-side OKX history strategy compare on the same candles.

A) trend_follow E3.1 (ext=0, pullback=0) — baseline fixed-horizon markout
B) mean_revert (same cooldown)
C) same TF fires as A with ATR barrier exit (TP 2.2×ATR / SL 1.0×ATR /
   timeout 900s) — measurement only vs fixed 300s
C2) same C path repriced at Regular maker 2bps/leg (RT 4 vs taker 10)
D) same TF fires as A with Supertrend ATR trail (no TP; ratchet SL;
   flip or timeout 4h) — P0 measurement only
E) KEEL_RULE_VARIANT=regime router (squeeze/shock WAIT; trend TF 15m+1h;
   range VWAP/%B fade) — P1 measurement only
F) KEEL_RULE_VARIANT=score (P2: 0–5 score ≥4; tighter range; RSI veto) —
   measurement only. LLM veto overlay is not part of this offline walk.
G) same fires: ATR barrier TP 1.0 (full early TP) vs 1R scale-half + rest 2.2
H) KEEL_RULE_VARIANT=squeeze_release (P5: first expansion after squeeze +
   ST+1h) with 4h hard time-stop barrier; H2 = same path maker reprice

Public API only; never writes .env; never uses OKX keys.

  PYTHONPATH=. python scripts/okx_history_strategy_compare.py \
    --bars-15m 700 --cooldown-seconds 900 \
    --json-out /tmp/keel_p0_strategy_compare.json
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
    DEFAULT_EARLY_TP_ATR,
    DEFAULT_HARD_TIME_STOP_SECONDS,
    DEFAULT_MFE_TIMEOUT_SECONDS,
    DEFAULT_SCALE_FRACTION,
    DEFAULT_TRAIL_SL_ATR,
    DEFAULT_TRAIL_TIMEOUT_SECONDS,
    reprice_exit_rows,
    rows_to_series,
    walk_forward_backtest,
)
from keel.exchange.okx_fees import (  # noqa: E402
    REGULAR_USDT_SWAP_MAKER_BPS,
    REGULAR_USDT_SWAP_TAKER_BPS,
)
from keel.exchange.okx_public import fetch_candles_paginated  # noqa: E402
from keel.factors.technical import (  # noqa: E402
    DEFAULT_SUPERTREND_MULTIPLIER,
    DEFAULT_SUPERTREND_PERIOD,
)
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


def _row_from_summary(
    label: str,
    summary: dict[str, Any],
    *,
    barrier: bool = False,
    trail: bool = False,
    barrier_1r: bool = False,
    scale: bool = False,
) -> dict[str, Any]:
    mo = summary.get("markout") or {}
    if scale:
        s = mo.get("scale") or {}
        return {
            "label": label,
            "variant": summary.get("variant"),
            "full_gate": summary.get("full_gate_count"),
            "metric": "scale_1r_half",
            "n_available": s.get("n_available"),
            "win_rate_net_rt": s.get("win_rate_net_rt"),
            "avg_net_rt_bps": s.get("avg_net_rt_bps"),
            "frac_clear_hurdle": s.get("frac_clear_hurdle"),
            "by_exit_reason": s.get("by_exit_reason"),
        }
    if barrier_1r:
        b = mo.get("barrier_1r") or {}
        return {
            "label": label,
            "variant": summary.get("variant"),
            "full_gate": summary.get("full_gate_count"),
            "metric": "barrier_tp1.0",
            "n_available": b.get("n_available"),
            "win_rate_net_rt": b.get("win_rate_net_rt"),
            "avg_net_rt_bps": b.get("avg_net_rt_bps"),
            "frac_clear_hurdle": b.get("frac_clear_hurdle"),
            "by_exit_reason": b.get("by_exit_reason"),
        }
    if trail:
        t = mo.get("trail") or {}
        return {
            "label": label,
            "variant": summary.get("variant"),
            "full_gate": summary.get("full_gate_count"),
            "metric": "supertrend_trail",
            "n_available": t.get("n_available"),
            "win_rate_net_rt": t.get("win_rate_net_rt"),
            "avg_net_rt_bps": t.get("avg_net_rt_bps"),
            "frac_clear_hurdle": t.get("frac_clear_hurdle"),
            "by_exit_reason": t.get("by_exit_reason"),
            "avg_hold_seconds": t.get("avg_hold_seconds"),
        }
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
            "avg_hold_seconds": b.get("avg_hold_seconds"),
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


def _row_from_block(
    label: str,
    *,
    variant: Any,
    full_gate: Any,
    metric: str,
    block: dict[str, Any],
) -> dict[str, Any]:
    return {
        "label": label,
        "variant": variant,
        "full_gate": full_gate,
        "metric": metric,
        "n_available": block.get("n_available"),
        "win_rate_net_rt": block.get("win_rate_net_rt"),
        "avg_net_rt_bps": block.get("avg_net_rt_bps"),
        "frac_clear_hurdle": block.get("frac_clear_hurdle"),
        "by_exit_reason": block.get("by_exit_reason"),
        "avg_hold_seconds": block.get("avg_hold_seconds"),
        "open_fee_bps": block.get("open_fee_bps"),
        "round_trip_fee_bps": block.get("round_trip_fee_bps"),
        "fee_role": block.get("fee_role"),
    }


def _barrier_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in summary.get("entries") or []:
        if e.get("suppressed_cooldown"):
            continue
        b = e.get("barrier")
        if isinstance(b, dict):
            out.append(b)
    return out


def _print_mfe(label: str, summary: dict[str, Any]) -> None:
    m = (summary.get("markout") or {}).get("mfe") or {}
    n = m.get("n_available") or 0
    print(f"--- MFE/MAE {label} n={n} (1R=1.0 ATR SL; 4h or SL) ---")
    if not n:
        print("  no samples")
        return

    def _pct(x: float | None) -> str:
        if x is None:
            return "n/a"
        return f"{100.0 * float(x):.1f}%"

    if m.get("avg_mfe_r") is None:
        print("  no mfe avg")
        return
    print(
        f"  avg MFE {float(m['avg_mfe_r']):.2f}R  MAE {float(m.get('avg_mae_r') or 0):.2f}R"
        f"  | 15m MFE {float(m.get('avg_mfe_r_15m') or 0):.2f}R  "
        f"MAE {float(m.get('avg_mae_r_15m') or 0):.2f}R"
    )
    tf = m.get("touch_frac") or {}
    t15 = m.get("touch_frac_15m") or {}
    bits = "  touch 4h: " + " ".join(
        f"{k}R={_pct(tf.get(k))}" for k in ("0.5", "1", "1.5", "2.2")
    )
    bits15 = "  touch 15m: " + " ".join(
        f"{k}R={_pct(t15.get(k))}" for k in ("0.5", "1", "1.5", "2.2")
    )
    print(bits)
    print(bits15)
    n1 = m.get("n_touched_1r") or 0
    among = m.get("after_1r_frac_among_touched") or {}
    print(
        f"  after touching 1R (n={n1}): "
        f"hit 2.2R={_pct(among.get('tp_2_2'))}  "
        f"then SL={_pct(among.get('sl'))}  "
        f"timeout={_pct(among.get('timeout'))}"
    )


def _print_table(rows: list[dict[str, Any]]) -> None:
    print("=== F2c/P0 strategy compare (same candles) ===")
    hdr = (
        f"{'label':<36} {'FG':>4} {'metric':<22} {'win':>8} {'avg_net':>10} "
        f"{'frac_clear':>10}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['label']:<36} {str(r.get('full_gate')):>4} "
            f"{str(r.get('metric')):<22} "
            f"{_fmt_rate(r.get('win_rate_net_rt')):>8} "
            f"{_fmt_bps(r.get('avg_net_rt_bps')):>10} "
            f"{_fmt_rate(r.get('frac_clear_hurdle')):>10}"
        )
        if r.get("by_exit_reason"):
            extra = ""
            hold = r.get("avg_hold_seconds")
            if hold is not None:
                extra = f" avg_hold={float(hold):.0f}s"
            print(f"  exits: {r['by_exit_reason']}{extra}")


def _verdict(rows: list[dict[str, Any]]) -> str:
    bits: list[str] = []
    best = None
    trail_row = next((r for r in rows if r.get("metric") == "supertrend_trail"), None)
    barrier_row = next(
        (r for r in rows if r.get("metric") == "barrier_tp_sl_timeout"), None
    )
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
    extra = ""
    if trail_row is not None and barrier_row is not None:
        tn = trail_row.get("avg_net_rt_bps")
        bn = barrier_row.get("avg_net_rt_bps")
        if tn is not None and bn is not None:
            extra = (
                f" P0 trail vs F2c barrier avg_net "
                f"{_fmt_bps(float(tn))} vs {_fmt_bps(float(bn))}."
            )
            if float(tn) > 0:
                extra += " Trail net>0 on this sample — still measurement only."
    if bits:
        return " ".join(bits) + extra + " Still E0 freeze — do not arm from this alone."
    if best is None:
        return (
            f"No usable markout samples. Keep waiting / gather more candles. "
            f"Target win≥{_TARGET_WIN:.0%} unmet. E0 freeze."
        )
    label, wr, fg = best
    return (
        f"Honest verdict: none reach win≥{_TARGET_WIN:.0%} with usable FG "
        f"(best={label} win={_fmt_rate(wr)} FG={fg})."
        f"{extra} "
        f"Do not flip live family or exits from this compare. E0 freeze."
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "F2c/P0: side-by-side TF vs mean_revert vs TF-barrier vs "
            "Supertrend trail on same OKX candles (public API; no keys / no .env write)"
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
    p.add_argument(
        "--skip-trail",
        action="store_true",
        help="Skip optional D) Supertrend trail markout",
    )
    p.add_argument(
        "--skip-regime",
        action="store_true",
        help="Skip optional E) P1 regime-router walk",
    )
    p.add_argument(
        "--skip-score",
        action="store_true",
        help="Skip optional F) P2 scored-router walk",
    )
    p.add_argument(
        "--skip-mfe",
        action="store_true",
        help="Skip MFE/MAE path stats on A/E/F fires",
    )
    p.add_argument(
        "--skip-early-tp",
        action="store_true",
        help="Skip G) TP 1.0 full close and 1R scale-half markout",
    )
    p.add_argument(
        "--skip-squeeze",
        action="store_true",
        help="Skip optional H) P5 squeeze-release walk",
    )
    p.add_argument(
        "--skip-maker",
        action="store_true",
        help="Skip C2/H2 Regular maker (2bps/leg) reprice of barrier paths",
    )
    p.add_argument(
        "--hard-timeout-seconds",
        type=float,
        default=DEFAULT_HARD_TIME_STOP_SECONDS,
        help="P5 squeeze-release barrier timeout (default 14400 = 4h)",
    )
    p.add_argument(
        "--squeeze-cooldown-seconds",
        type=int,
        default=int(DEFAULT_HARD_TIME_STOP_SECONDS),
        help="P5 squeeze-release fire cooldown (default 14400 = 4h)",
    )
    p.add_argument(
        "--trail-timeout-seconds",
        type=float,
        default=DEFAULT_TRAIL_TIMEOUT_SECONDS,
        help="P0 trail timeout (default 14400 = 4h)",
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
    want_barrier = not bool(args.skip_barrier)
    want_trail = not bool(args.skip_trail)
    want_regime = not bool(args.skip_regime)
    want_score = not bool(args.skip_score)
    want_mfe = not bool(args.skip_mfe)
    want_early = not bool(args.skip_early_tp)
    want_squeeze = not bool(args.skip_squeeze)
    want_maker = not bool(args.skip_maker)

    print("running A) trend_follow E3.1 ext=0 pullback=0 ...")
    a = walk_forward_backtest(
        series_list,
        variant="trend_follow",
        cooldown_seconds=cd,
        require_4h=True,
        horizons=horizons,
        include_barrier=want_barrier,
        include_trail=want_trail,
        trail_timeout_seconds=float(args.trail_timeout_seconds),
        include_mfe=want_mfe,
        include_early_tp=want_early,
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
        include_trail=False,
        max_extension_atr=0.0,
        pullback=False,
    )

    e_regime: dict[str, Any] | None = None
    if want_regime:
        print("running E) regime router (4h off, range VWAP/%B) ...")
        e_regime = walk_forward_backtest(
            series_list,
            variant="regime",
            cooldown_seconds=cd,
            require_4h=False,
            horizons=horizons,
            include_barrier=want_barrier,
            include_trail=False,
            include_mfe=want_mfe,
            include_early_tp=want_early,
            max_extension_atr=0.0,
            pullback=False,
        )

    f_score: dict[str, Any] | None = None
    if want_score:
        print("running F) score router (4h off, score>=4, tighter range) ...")
        f_score = walk_forward_backtest(
            series_list,
            variant="score",
            cooldown_seconds=cd,
            require_4h=False,
            horizons=horizons,
            include_barrier=want_barrier,
            include_trail=False,
            include_mfe=want_mfe,
            include_early_tp=want_early,
            max_extension_atr=0.0,
            pullback=False,
        )

    h_squeeze: dict[str, Any] | None = None
    if want_squeeze:
        sq_cd = max(0, int(args.squeeze_cooldown_seconds))
        hard_to = float(args.hard_timeout_seconds)
        print(
            f"running H) squeeze_release (cd={sq_cd}s, barrier timeout={hard_to:.0f}s) ..."
        )
        h_squeeze = walk_forward_backtest(
            series_list,
            variant="squeeze_release",
            cooldown_seconds=sq_cd,
            require_4h=False,
            horizons=horizons,
            include_barrier=True,
            barrier_timeout_seconds=hard_to,
            include_trail=False,
            include_mfe=False,
            include_early_tp=False,
            max_extension_atr=0.0,
            pullback=False,
        )

    c2_maker: dict[str, Any] | None = None
    h2_maker: dict[str, Any] | None = None
    if want_maker and want_barrier:
        c2_maker = reprice_exit_rows(
            _barrier_rows(a),
            open_fee_bps=REGULAR_USDT_SWAP_MAKER_BPS,
            extra={
                "tp_atr": DEFAULT_BARRIER_TP_ATR,
                "sl_atr": DEFAULT_BARRIER_SL_ATR,
                "timeout_seconds": DEFAULT_BARRIER_TIMEOUT_SECONDS,
            },
        )
    if want_maker and h_squeeze is not None:
        h2_maker = reprice_exit_rows(
            _barrier_rows(h_squeeze),
            open_fee_bps=REGULAR_USDT_SWAP_MAKER_BPS,
            extra={
                "tp_atr": DEFAULT_BARRIER_TP_ATR,
                "sl_atr": DEFAULT_BARRIER_SL_ATR,
                "timeout_seconds": float(args.hard_timeout_seconds),
            },
        )

    rows = [
        _row_from_summary("A) TF E3.1 ext0 pb0", a, barrier=False),
        _row_from_summary("B) mean_revert", b, barrier=False),
    ]
    if want_barrier:
        rows.append(
            _row_from_summary(
                "C) TF barrier TP2.2/SL1.0",
                a,
                barrier=True,
            )
        )
        if c2_maker is not None:
            rows.append(
                _row_from_block(
                    "C2) TF barrier maker 4bps RT",
                    variant=a.get("variant"),
                    full_gate=a.get("full_gate_count"),
                    metric="barrier_maker_reprice",
                    block=c2_maker,
                )
            )
    if want_trail:
        rows.append(
            _row_from_summary(
                "D) TF Supertrend trail",
                a,
                trail=True,
            )
        )
    if e_regime is not None:
        rows.append(_row_from_summary("E) regime 300s", e_regime, barrier=False))
        if want_barrier:
            rows.append(
                _row_from_summary(
                    "E2) regime barrier",
                    e_regime,
                    barrier=True,
                )
            )
    if f_score is not None:
        rows.append(_row_from_summary("F) score 300s", f_score, barrier=False))
        if want_barrier:
            rows.append(
                _row_from_summary(
                    "F2) score barrier",
                    f_score,
                    barrier=True,
                )
            )
    if want_early:
        rows.append(
            _row_from_summary("G) TF TP1.0 full", a, barrier_1r=True)
        )
        rows.append(
            _row_from_summary("G2) TF 1R scale-half", a, scale=True)
        )
        if f_score is not None:
            rows.append(
                _row_from_summary("G3) score TP1.0 full", f_score, barrier_1r=True)
            )
            rows.append(
                _row_from_summary("G4) score 1R scale-half", f_score, scale=True)
            )
    if h_squeeze is not None:
        rows.append(
            _row_from_summary(
                "H) squeeze_release 4h stop",
                h_squeeze,
                barrier=True,
            )
        )
        if h2_maker is not None:
            rows.append(
                _row_from_block(
                    "H2) squeeze_release maker 4bps RT",
                    variant=h_squeeze.get("variant"),
                    full_gate=h_squeeze.get("full_gate_count"),
                    metric="barrier_maker_reprice",
                    block=h2_maker,
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
    if want_maker:
        print(
            f"maker reprice: Regular maker {REGULAR_USDT_SWAP_MAKER_BPS:.1f}bps/leg "
            f"(RT {2.0 * REGULAR_USDT_SWAP_MAKER_BPS:.0f}bps) vs taker "
            f"{REGULAR_USDT_SWAP_TAKER_BPS:.1f}bps/leg "
            f"(RT {2.0 * REGULAR_USDT_SWAP_TAKER_BPS:.0f}bps); "
            "100% limit fill assumed, queue miss not modeled"
        )
    if want_trail:
        print(
            f"trail geometry: Supertrend ATR{DEFAULT_SUPERTREND_PERIOD}×"
            f"{DEFAULT_SUPERTREND_MULTIPLIER} + initial SL "
            f"{DEFAULT_TRAIL_SL_ATR}×ATR timeout={float(args.trail_timeout_seconds):.0f}s "
            f"(no TP; ratchet; flip at close)"
        )
    if e_regime is not None:
        print(f"E by_regime fires: {e_regime.get('by_regime')}")
        print(
            "regime geometry: squeeze/shock WAIT; trend=TF 15m+1h (4h off); "
            "range=percent_b 0.20/0.80 + VWAP side + RSI 50 + volume"
        )
    if f_score is not None:
        print(f"F by_regime fires: {f_score.get('by_regime')}")
        print(
            "score geometry: same router; trend must HTF+ST then 0-5 fire>=4 "
            "(pullback on VWAP discount, MACD, volume) + RSI veto 70/30; "
            "range must %B 0.15/0.85 + VWAP + score>=4 (no soft_range)"
        )
    if h_squeeze is not None:
        print(f"H by_regime fires: {h_squeeze.get('by_regime')}")
        print(
            "squeeze_release geometry: prev TTM squeeze + now expand + Supertrend "
            f"+ 1h agree + RSI veto 70/30; cooldown={int(args.squeeze_cooldown_seconds)}s; "
            f"hard time-stop {float(args.hard_timeout_seconds):.0f}s "
            f"(TP {DEFAULT_BARRIER_TP_ATR}/SL {DEFAULT_BARRIER_SL_ATR} ATR). "
            "Expansion bar is the event (no P1 shock WAIT)."
        )
    if want_mfe:
        _print_mfe("A TF", a)
        if e_regime is not None:
            _print_mfe("E regime", e_regime)
        if f_score is not None:
            _print_mfe("F score", f_score)
    if want_early:
        print(
            f"early-TP geometry: full close at {DEFAULT_EARLY_TP_ATR}×ATR; "
            f"scale {DEFAULT_SCALE_FRACTION:.0%} at 1R then rest to "
            f"{DEFAULT_BARRIER_TP_ATR}×ATR; SL {DEFAULT_BARRIER_SL_ATR}×ATR; "
            f"timeout {DEFAULT_BARRIER_TIMEOUT_SECONDS}s (measurement only)"
        )
        print(f"MFE window: {DEFAULT_MFE_TIMEOUT_SECONDS:.0f}s or SL")

    out: dict[str, Any] = {
        "phase": "F2c+P0+P1+P2+P5+maker",
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
            "Same candles for A–H. C/D/G reuse A entries; C2/H2 reprice the same "
            "barrier path at Regular maker 2bps/leg (100% fill assumed). "
            "E is P1 regime; F is P2 score; H is P5 squeeze-release + 4h time-stop. "
            "MFE/MAE is path research, not a live exit. E0 freeze unchanged."
        ),
    }
    if e_regime is not None:
        out["E_regime"] = {k: v for k, v in e_regime.items() if k != "entries"}
    if f_score is not None:
        out["F_score"] = {k: v for k, v in f_score.items() if k != "entries"}
    if h_squeeze is not None:
        out["H_squeeze_release"] = {k: v for k, v in h_squeeze.items() if k != "entries"}
    if c2_maker is not None:
        out["C2_maker"] = c2_maker
    if h2_maker is not None:
        out["H2_maker"] = h2_maker
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
