#!/usr/bin/env python3
"""
LLM vs rule shadow daily summary (Chinese-friendly JSON + text).

Reads ledger decisions with ``calculus_data.rule_shadow`` from the last N hours.
Counts agree/diverge, LLM-fire vs rule-fire; optional crude fee markout when
later factor mid prices are available.

F7: reports markout at multiple horizons (default 300/900/3600s) so geometry
(~hours of ATR risk) is not scored only on 5m mid.

F9: rule shadow now applies soft-4h / 15m not-opposing / ADX≥15 / RSI mid
veto (see RUNBOOK Phase F9). Expect rule fire-rate to drop toward LLM; still
shadow-only under ``KEEL_DECISION_POLICY=llm``. Offline fire-rate delta:
``scripts/tf_full_gate_replay.py --hours 72 --compare-f9``.

  PYTHONPATH=. python scripts/llm_vs_rule_daily.py \
    --db data/keel_ledger.db --hours 24

Recommend-only — never writes .env, never places orders.
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

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from keel.ledger import KeelLedger  # noqa: E402

_FIRE = frozenset({"BUY_LONG", "SELL_SHORT"})
_DEFAULT_RT_FEE_BPS = 10.0
_DEFAULT_HORIZONS = (300.0, 900.0, 3600.0)


def _is_fire(action: str) -> bool:
    return str(action or "").strip().upper() in _FIRE


def _side_sign(action: str) -> int | None:
    a = str(action or "").strip().upper()
    if a == "BUY_LONG":
        return 1
    if a == "SELL_SHORT":
        return -1
    return None


def _markout_bps(
    *,
    action: str,
    entry: float | None,
    later_mid: float | None,
    fee_bps: float,
) -> float | None:
    """Gross mid markout minus round-trip fee estimate (bps)."""
    sign = _side_sign(action)
    if sign is None or entry is None or later_mid is None:
        return None
    if entry <= 0 or later_mid <= 0:
        return None
    gross = sign * (later_mid - entry) / entry * 1e4
    return float(gross) - float(fee_bps)


def _later_mid_at(
    snaps: list[Any],
    *,
    decision_ts: float,
    horizon_seconds: float,
) -> float | None:
    after = [s for s in snaps if float(s.timestamp) > float(decision_ts)]
    if not after:
        return None
    target_ts = float(decision_ts) + float(horizon_seconds)
    best = None
    best_delta = None
    for s in after:
        delta = abs(float(s.timestamp) - target_ts)
        if best_delta is None or delta < best_delta:
            best = s
            best_delta = delta
    if best is not None:
        return float(best.price)
    return float(min(after, key=lambda s: float(s.timestamp)).price)


def _markout_bucket() -> dict[str, Any]:
    return {"llm": [], "rule_cf": []}



def _effective_config_header() -> dict[str, Any]:
    """P1-10 / P1-1: pin profile + min_confidence + asymmetry for day-over-day compare."""
    out: dict[str, Any] = {
        "profile": None,
        "min_confidence": None,
        "soft4h_block_15m_neutral": None,
        "llm_4h_mode": None,
        "rule_4h_mode": None,
        "asymmetry_note": (
            "LLM 15m=not-opposing unless SOFT4H_BLOCK_15M_NEUTRAL; "
            "rule TF 15m=same-dir; llm_demo LLM 4h=soft / rule 4h=hard"
        ),
    }
    try:
        from keel.config.settings import _env, get_settings, refresh_settings

        refresh_settings()
        settings = get_settings()
        out["profile"] = settings.profile
        out["min_confidence"] = float(_env("KEEL_LLM_MIN_CONFIDENCE", "60") or 60)
        raw = (_env("KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL", "") or "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            out["soft4h_block_15m_neutral"] = True
        elif raw in ("0", "false", "no", "off"):
            out["soft4h_block_15m_neutral"] = False
        else:
            out["soft4h_block_15m_neutral"] = False
        out["llm_4h_mode"] = (_env("KEEL_LLM_4H_MODE", "soft") or "soft").strip().lower()
        out["rule_4h_mode"] = (_env("KEEL_RULE_4H_MODE", "hard") or "hard").strip().lower()
    except Exception:
        pass
    return out



def _event_decision_id(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    raw = data.get("decision_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def sized_fill_funnel(ledger: KeelLedger, *, hours: float, limit: int = 5000) -> dict[str, Any]:
    """Correlate order_sized → filled / failed / risk_denied by decision_id (P2-5).

    ``order_sized`` only fires when size was clipped — unexplained = sized with a
    decision_id that never got filled, failed, or risk-blocked in the window.
    """
    since = time.time() - max(0.0, float(hours)) * 3600.0

    def _load(event_type: str) -> list[Any]:
        try:
            rows = ledger.get_events(event_type=event_type, limit=limit)
        except Exception:
            return []
        return [e for e in rows if float(getattr(e, "timestamp", 0) or 0) >= since]

    sized = _load("order_sized")
    filled = _load("order_filled")
    failed = _load("order_failed")
    denied = _load("risk_gate_blocked")

    filled_ids = {i for e in filled if (i := _event_decision_id(getattr(e, "data", None)))}
    failed_ids = {i for e in failed if (i := _event_decision_id(getattr(e, "data", None)))}
    denied_ids = {i for e in denied if (i := _event_decision_id(getattr(e, "data", None)))}

    sized_with_id = 0
    sized_no_id = 0
    to_filled = 0
    to_failed = 0
    to_denied = 0
    unexplained: list[int] = []
    seen_unexplained: set[int] = set()

    for e in sized:
        did = _event_decision_id(getattr(e, "data", None))
        if did is None:
            sized_no_id += 1
            continue
        sized_with_id += 1
        if did in filled_ids:
            to_filled += 1
        elif did in failed_ids:
            to_failed += 1
        elif did in denied_ids:
            to_denied += 1
        elif did not in seen_unexplained:
            seen_unexplained.add(did)
            unexplained.append(did)

    return {
        "hours": hours,
        "order_sized": len(sized),
        "order_filled": len(filled),
        "order_failed": len(failed),
        "risk_gate_blocked": len(denied),
        "sized_with_decision_id": sized_with_id,
        "sized_without_decision_id": sized_no_id,
        "sized_then_filled": to_filled,
        "sized_then_failed": to_failed,
        "sized_then_denied": to_denied,
        "unexplained_sized": len(unexplained),
        "unexplained_decision_ids_sample": unexplained[:20],
        "note": (
            "order_sized 仅在仓位被 clip 时写入；"
            "unexplained = 有 decision_id 但窗口内未见 filled/failed/risk_gate_blocked"
        ),
    }


def summarize(
    ledger: KeelLedger,
    *,
    hours: float,
    limit: int,
    fee_bps: float,
    markout_horizons: list[float],
) -> dict[str, Any]:
    since = time.time() - max(0.0, float(hours)) * 3600.0
    rows = ledger.get_decisions(since=since, limit=limit)

    with_shadow = 0
    agree = 0
    diverge = 0
    unclear = 0
    llm_fire = 0
    rule_fire = 0
    both_same = 0
    both_opp = 0
    llm_only = 0
    rule_only = 0
    both_wait = 0
    by_inst: dict[str, dict[str, int]] = {}
    horizons = [float(h) for h in markout_horizons if float(h) > 0]
    if not horizons:
        horizons = list(_DEFAULT_HORIZONS)
    by_h: dict[str, dict[str, list[float]]] = {
        str(int(h) if float(h).is_integer() else h): _markout_bucket() for h in horizons
    }

    for d in rows:
        calc = d.calculus_data if isinstance(d.calculus_data, dict) else None
        if not isinstance(calc, dict):
            continue
        shadow = calc.get("rule_shadow")
        if not isinstance(shadow, dict):
            continue
        with_shadow += 1
        primary = str(d.action or "").strip().upper()
        rule_action = str(shadow.get("action") or "").strip().upper()
        flag = shadow.get("agree")
        if flag is True:
            agree += 1
        elif flag is False:
            diverge += 1
        else:
            unclear += 1

        p_fire = _is_fire(primary)
        r_fire = _is_fire(rule_action)
        if p_fire:
            llm_fire += 1
        if r_fire:
            rule_fire += 1
        if p_fire and r_fire:
            if primary == rule_action:
                both_same += 1
            else:
                both_opp += 1
        elif p_fire and not r_fire:
            llm_only += 1
        elif r_fire and not p_fire:
            rule_only += 1
        else:
            both_wait += 1

        inst = str(d.inst_id or "")
        bucket = by_inst.setdefault(
            inst,
            {"n": 0, "agree": 0, "diverge": 0, "llm_fire": 0, "rule_fire": 0},
        )
        bucket["n"] += 1
        if flag is True:
            bucket["agree"] += 1
        elif flag is False:
            bucket["diverge"] += 1
        if p_fire:
            bucket["llm_fire"] += 1
        if r_fire:
            bucket["rule_fire"] += 1

        entry = None
        if p_fire:
            entry = d.entry_price
            if entry is None:
                entry = calc.get("entry_price")
        rule_entry = shadow.get("entry_price") if r_fire else None

        snaps: list[Any] = []
        try:
            snaps = list(ledger.get_factor_snapshots(inst_id=d.inst_id, limit=200))
        except Exception:
            snaps = []

        for h in horizons:
            key = str(int(h) if float(h).is_integer() else h)
            later_mid = _later_mid_at(
                snaps, decision_ts=float(d.timestamp), horizon_seconds=h
            )
            if p_fire and entry is not None:
                m = _markout_bps(
                    action=primary,
                    entry=float(entry),
                    later_mid=later_mid,
                    fee_bps=fee_bps,
                )
                if m is not None:
                    by_h[key]["llm"].append(m)
            if (not p_fire) and r_fire and rule_entry is not None:
                m = _markout_bps(
                    action=rule_action,
                    entry=float(rule_entry),
                    later_mid=later_mid,
                    fee_bps=fee_bps,
                )
                if m is not None:
                    by_h[key]["rule_cf"].append(m)

    def _avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 3) if xs else None

    def _win(xs: list[float]) -> float | None:
        return round(sum(1 for x in xs if x > 0) / len(xs), 3) if xs else None

    markouts: dict[str, Any] = {}
    for key, bag in by_h.items():
        llm_xs = bag["llm"]
        rule_xs = bag["rule_cf"]
        markouts[key] = {
            "horizon_seconds": float(key) if "." in key else int(key),
            "fee_rt_bps": fee_bps,
            "llm_fire_samples": len(llm_xs),
            "llm_avg_net_bps": _avg(llm_xs),
            "llm_win_rate": _win(llm_xs),
            "rule_only_cf_samples": len(rule_xs),
            "rule_only_cf_avg_net_bps": _avg(rule_xs),
            "rule_only_cf_win_rate": _win(rule_xs),
        }

    # Backward-compatible primary markout = first horizon (usually 300s).
    primary_key = next(iter(by_h))
    primary_mk = dict(markouts[primary_key])
    primary_mk["note"] = (
        "粗估：决策后因子 mid − 进出场手续费；多 horizon 见 markouts；"
        "样本不足时为 null。F7: 300/900/3600s 对齐几何风险时长。"
    )
    primary_mk["horizons_seconds"] = [
        float(k) if "." in k else int(k) for k in by_h.keys()
    ]

    invalid_n = 0
    try:
        stats = ledger.get_decision_stats(hours=float(hours))
        invalid_n = int(stats.get("decision_invalid_events") or 0)
    except Exception:
        invalid_n = 0

    return {
        "hours": hours,
        "decision_rows_scanned": len(rows),
        "with_rule_shadow": with_shadow,
        "agree": agree,
        "diverge": diverge,
        "unclear": unclear,
        "agree_rate": round(agree / with_shadow, 3) if with_shadow else None,
        "llm_fire": llm_fire,
        "rule_fire": rule_fire,
        "both_fire_same": both_same,
        "both_fire_opposite": both_opp,
        "llm_only_fire": llm_only,
        "rule_only_fire": rule_only,
        "both_wait": both_wait,
        "by_instrument": by_inst,
        "markout": primary_mk,
        "markouts": markouts,
        "decision_invalid_events": invalid_n,
        "effective_config": _effective_config_header(),
    }


def format_zh(summary: dict[str, Any]) -> str:
    lines = [
        "=== Keel LLM vs 规则影子 日摘要 ===",
    ]
    eff = summary.get("effective_config") or {}
    if eff:
        lines.append(
            f"生效: profile={eff.get('profile') or '(none)'} "
            f"| min_confidence={eff.get('min_confidence')} "
            f"| soft4h_block_15m_neutral={eff.get('soft4h_block_15m_neutral')} "
            f"| llm_4h={eff.get('llm_4h_mode')} rule_4h={eff.get('rule_4h_mode')}"
        )
        note = eff.get("asymmetry_note")
        if note:
            lines.append(f"门控不对称: {note}")
    inv = summary.get("decision_invalid_events")
    if inv is not None:
        lines.append(f"决策无效(decision_invalid): {inv}")
    lines.extend([
        f"窗口: 近 {summary['hours']} 小时 | 含影子决策: {summary['with_rule_shadow']}"
        f" / 扫描 {summary['decision_rows_scanned']}",
        f"一致 agree: {summary['agree']} | 分歧 diverge: {summary['diverge']}"
        f" | 不明: {summary['unclear']} | 一致率: {summary['agree_rate']}",
        f"LLM 开火: {summary['llm_fire']} | 规则开火: {summary['rule_fire']}",
        f"  双方同向: {summary['both_fire_same']} | 双方反向: {summary['both_fire_opposite']}",
        f"  仅 LLM: {summary['llm_only_fire']} | 仅规则: {summary['rule_only_fire']}"
        f" | 双方 WAIT: {summary['both_wait']}",
    ])
    markouts = summary.get("markouts") or {}
    if markouts:
        lines.append("费用后标记 (多 horizon, geometry-aware):")
        for key in sorted(markouts.keys(), key=lambda x: float(x)):
            mk = markouts[key]
            lines.append(
                f"  @{mk.get('horizon_seconds')}s fee={mk.get('fee_rt_bps')}bps |"
                f" LLM avg_net={mk.get('llm_avg_net_bps')} win={mk.get('llm_win_rate')}"
                f" n={mk.get('llm_fire_samples')} |"
                f" 仅规则CF avg_net={mk.get('rule_only_cf_avg_net_bps')}"
                f" win={mk.get('rule_only_cf_win_rate')} n={mk.get('rule_only_cf_samples')}"
            )
    else:
        mk = summary.get("markout") or {}
        lines.append(
            f"费用后标记 (horizon={mk.get('horizon_seconds')}s, fee={mk.get('fee_rt_bps')}bps):"
        )
        lines.append(
            f"  LLM开火 avg_net={mk.get('llm_avg_net_bps')} bps"
            f" win={mk.get('llm_win_rate')} n={mk.get('llm_fire_samples')}"
        )
        lines.append(
            f"  仅规则反事实 avg_net={mk.get('rule_only_cf_avg_net_bps')} bps"
            f" win={mk.get('rule_only_cf_win_rate')} n={mk.get('rule_only_cf_samples')}"
        )
    by_inst = summary.get("by_instrument") or {}
    if by_inst:
        lines.append("分品种:")
        for inst, b in sorted(by_inst.items()):
            lines.append(
                f"  {inst}: n={b['n']} agree={b['agree']} diverge={b['diverge']}"
                f" llm_fire={b['llm_fire']} rule_fire={b['rule_fire']}"
            )

    funnel = summary.get("sized_fill_funnel") or {}
    if funnel:
        lines.append(
            f"仓位漏斗 order_sized→filled: sized={funnel.get('order_sized')} "
            f"filled={funnel.get('order_filled')} failed={funnel.get('order_failed')} "
            f"denied={funnel.get('risk_gate_blocked')} | "
            f"sized→filled={funnel.get('sized_then_filled')} "
            f"→failed={funnel.get('sized_then_failed')} "
            f"→denied={funnel.get('sized_then_denied')} "
            f"未解释={funnel.get('unexplained_sized')}"
        )
        sample = funnel.get("unexplained_decision_ids_sample") or []
        if sample:
            lines.append(f"  未解释 decision_id 样本: {sample}")
    return "\n".join(lines)


def _parse_horizons(raw: str) -> list[float]:
    parts = [p.strip() for p in str(raw or "").split(",") if p.strip()]
    out: list[float] = []
    for p in parts:
        try:
            v = float(p)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out or list(_DEFAULT_HORIZONS)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="LLM vs rule shadow daily summary")
    p.add_argument(
        "--db",
        type=Path,
        default=_ROOT / "data" / "keel_ledger.db",
        help="SQLite ledger path",
    )
    p.add_argument("--hours", type=float, default=24.0, help="Lookback hours")
    p.add_argument("--limit", type=int, default=5000, help="Max decision rows")
    p.add_argument(
        "--fee-bps",
        type=float,
        default=_DEFAULT_RT_FEE_BPS,
        help="Round-trip fee bps for optional markout",
    )
    p.add_argument(
        "--markout-horizons",
        type=str,
        default="300,900,3600",
        help="Comma-separated markout horizons in seconds (F7 geometry-aware)",
    )
    p.add_argument(
        "--markout-horizon",
        type=float,
        default=None,
        help="Deprecated single horizon; if set, overrides --markout-horizons",
    )
    p.add_argument(
        "--json-only",
        action="store_true",
        help="Print JSON only (no Chinese text block)",
    )
    args = p.parse_args(argv)

    if not args.db.is_file():
        print(f"error: ledger not found: {args.db}", file=sys.stderr)
        return 2

    if args.markout_horizon is not None:
        horizons = [float(args.markout_horizon)]
    else:
        horizons = _parse_horizons(args.markout_horizons)

    ledger = KeelLedger(args.db)
    summary = summarize(
        ledger,
        hours=args.hours,
        limit=args.limit,
        fee_bps=args.fee_bps,
        markout_horizons=horizons,
    )
    summary["sized_fill_funnel"] = sized_fill_funnel(ledger, hours=float(args.hours))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.json_only:
        print()
        print(format_zh(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
