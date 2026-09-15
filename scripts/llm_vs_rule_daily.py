#!/usr/bin/env python3
"""
LLM vs rule shadow daily summary (Chinese-friendly JSON + text).

Reads ledger decisions with ``calculus_data.rule_shadow`` from the last N hours.
Counts agree/diverge, LLM-fire vs rule-fire; optional crude fee markout when
later factor mid prices are available.

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


def summarize(
    ledger: KeelLedger,
    *,
    hours: float,
    limit: int,
    fee_bps: float,
    markout_horizon_seconds: float,
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
    llm_markouts: list[float] = []
    rule_cf_markouts: list[float] = []  # rule-only counterfactual

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

        # Optional fee markout via later factor snapshots.
        entry = None
        if p_fire:
            entry = d.entry_price
            if entry is None:
                entry = calc.get("entry_price")
        rule_entry = shadow.get("entry_price") if r_fire else None

        later_mid = None
        try:
            snaps = ledger.get_factor_snapshots(inst_id=d.inst_id, limit=80)
            after = [s for s in snaps if float(s.timestamp) > float(d.timestamp)]
            # snaps are newest-first; pick closest at/after horizon
            target_ts = float(d.timestamp) + float(markout_horizon_seconds)
            best = None
            best_delta = None
            for s in after:
                delta = abs(float(s.timestamp) - target_ts)
                if best_delta is None or delta < best_delta:
                    best = s
                    best_delta = delta
            if best is not None:
                later_mid = float(best.price)
            elif after:
                later_mid = float(min(after, key=lambda s: float(s.timestamp)).price)
        except Exception:
            later_mid = None

        if p_fire and entry is not None:
            m = _markout_bps(
                action=primary, entry=float(entry), later_mid=later_mid, fee_bps=fee_bps
            )
            if m is not None:
                llm_markouts.append(m)
        if (not p_fire) and r_fire and rule_entry is not None:
            m = _markout_bps(
                action=rule_action,
                entry=float(rule_entry),
                later_mid=later_mid,
                fee_bps=fee_bps,
            )
            if m is not None:
                rule_cf_markouts.append(m)

    def _avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 3) if xs else None

    def _win(xs: list[float]) -> float | None:
        return round(sum(1 for x in xs if x > 0) / len(xs), 3) if xs else None

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
        "markout": {
            "horizon_seconds": markout_horizon_seconds,
            "fee_rt_bps": fee_bps,
            "llm_fire_samples": len(llm_markouts),
            "llm_avg_net_bps": _avg(llm_markouts),
            "llm_win_rate": _win(llm_markouts),
            "rule_only_cf_samples": len(rule_cf_markouts),
            "rule_only_cf_avg_net_bps": _avg(rule_cf_markouts),
            "rule_only_cf_win_rate": _win(rule_cf_markouts),
            "note": "粗估：决策后因子 mid − 进出场手续费；样本不足时为 null",
        },
    }


def format_zh(summary: dict[str, Any]) -> str:
    lines = [
        "=== Keel LLM vs 规则影子 日摘要 ===",
        f"窗口: 近 {summary['hours']} 小时 | 含影子决策: {summary['with_rule_shadow']}"
        f" / 扫描 {summary['decision_rows_scanned']}",
        f"一致 agree: {summary['agree']} | 分歧 diverge: {summary['diverge']}"
        f" | 不明: {summary['unclear']} | 一致率: {summary['agree_rate']}",
        f"LLM 开火: {summary['llm_fire']} | 规则开火: {summary['rule_fire']}",
        f"  双方同向: {summary['both_fire_same']} | 双方反向: {summary['both_fire_opposite']}",
        f"  仅 LLM: {summary['llm_only_fire']} | 仅规则: {summary['rule_only_fire']}"
        f" | 双方 WAIT: {summary['both_wait']}",
    ]
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
    return "\n".join(lines)


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
        "--markout-horizon",
        type=float,
        default=300.0,
        help="Seconds after decision for mid markout",
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

    ledger = KeelLedger(args.db)
    summary = summarize(
        ledger,
        hours=args.hours,
        limit=args.limit,
        fee_bps=args.fee_bps,
        markout_horizon_seconds=args.markout_horizon,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.json_only:
        print()
        print(format_zh(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
