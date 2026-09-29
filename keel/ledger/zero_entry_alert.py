"""
48h zero-entry alert (2026-09-29, paired with llm_demo ``KEEL_LLM_4H_MODE=hard``).

Hard-4h trades less (backtest fires/day 6.4 → 3.7) and a whole-day 4h-neutral
regime gives **0 fires**. This module answers, read-only from the ledger:

* How long since the last **filled** LLM entry (``trades.action in (open,
  scale_in)`` with ``strategy_tag='keel-llm'`` — fill-truth since PR #117)?
* If that is ≥ ``KEEL_ZERO_ENTRY_ALERT_HOURS`` (default 48; 0 disables), is it
  **market-driven** (worker healthy; show share of recent LLM decisions vetoed
  by 4h-hard / ADX floor / other gates / model WAIT and the 4h-neutral share)
  or a **service outage** (worker stale now, or cycle coverage in the window
  < 50 %)?

Surfaces: ``GET /ready`` → ``zero_entry_alert``; ``GET /api/v1/stats/quality``
→ ``zero_entry_alert`` + ``alerts[]``; ``GET /api/v1/stats/zero_entry``;
``scripts/zero_entry_alert.py``; worker scheduler log WARNING.
Never places / cancels orders, never changes config.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from keel.config.settings import _env

BJ_TZ = timezone(timedelta(hours=8))

DEFAULT_THRESHOLD_HOURS = 48.0
ENTRY_STRATEGY_TAG = "keel-llm"
ENTRY_ACTIONS = ("open", "scale_in")
# Below this share of expected cycles in the window, treat zero entries as outage.
MIN_CYCLE_COVERAGE = 0.5
ALERT_CODE = "zero_entry_48h"

VETO_KEYS = ("4h_hard", "htf_other", "adx", "position_cooldown", "other_gate", "llm_wait", "fire")
VETO_LABEL_ZH = {
    "4h_hard": "4h硬门槛否决",
    "htf_other": "1h/高周期否决",
    "adx": "ADX下限否决",
    "position_cooldown": "已有持仓/冷却",
    "other_gate": "其他门槛否决",
    "llm_wait": "模型自身WAIT",
    "fire": "通过门槛(开火)",
}


def zero_entry_threshold_hours() -> float:
    """``KEEL_ZERO_ENTRY_ALERT_HOURS`` (default 48; ≤0 disables)."""
    raw = (_env("KEEL_ZERO_ENTRY_ALERT_HOURS", "") or "").strip()
    if not raw:
        return DEFAULT_THRESHOLD_HOURS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_THRESHOLD_HOURS


def _stale_threshold_seconds(cycle_interval_seconds: int) -> int:
    # Same formula as keel.api.cycle_time.worker_stale_threshold_seconds
    # (not imported: keel.api.__init__ pulls in the FastAPI app).
    interval = max(1, int(cycle_interval_seconds))
    return max(interval * 2, interval + 300)


def _fmt_cst(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), BJ_TZ).strftime("%m-%d %H:%M CST")


def _classify(action: str, diag: dict[str, Any] | None) -> str:
    act = str(action or "").upper()
    if act in ("BUY_LONG", "SELL_SHORT"):
        return "fire"
    d = diag if isinstance(diag, dict) else {}
    missing_raw = d.get("missing")
    missing = [str(x) for x in missing_raw] if isinstance(missing_raw, list) else []
    if "htf_ok" in missing:
        cause = str(d.get("htf_veto_cause") or "")
        mode = str(d.get("llm_4h_mode") or "")
        if cause.startswith("4h_hard") or (mode == "hard" and cause != "1h" and not cause):
            return "4h_hard"
        return "htf_other"
    if "adx_ok" in missing:
        return "adx"
    if "book_ok" in missing or any("cooldown" in m for m in missing):
        return "position_cooldown"
    if missing:
        return "other_gate"
    return "llm_wait"


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def compute_zero_entry_alert(
    ledger: Any,
    *,
    now: float | None = None,
    threshold_hours: float | None = None,
    cycle_interval_seconds: int | None = None,
) -> dict[str, Any]:
    """Return the alert payload (plain dict; see module docstring)."""
    now_ts = float(now if now is not None else time.time())
    thr_h = float(threshold_hours if threshold_hours is not None else zero_entry_threshold_hours())
    if cycle_interval_seconds is None:
        from keel.config import get_settings

        cycle_interval_seconds = int(get_settings().cycle_interval_seconds)
    interval = max(1, int(cycle_interval_seconds))

    base: dict[str, Any] = {
        "code": ALERT_CODE,
        "triggered": False,
        "kind": "ok",
        "severity": "ok",
        "threshold_hours": thr_h,
        "checked_at": now_ts,
        "checked_at_cst": _fmt_cst(now_ts),
    }
    if thr_h <= 0:
        base.update(kind="disabled", message_zh="48h 零开仓告警已关闭（KEEL_ZERO_ENTRY_ALERT_HOURS=0）。")
        return base

    conn = ledger._get_conn()
    placeholders = ",".join("?" for _ in ENTRY_ACTIONS)
    row = conn.execute(
        f"SELECT id, timestamp, inst_id, direction FROM trades "
        f"WHERE strategy_tag = ? AND action IN ({placeholders}) "
        f"ORDER BY timestamp DESC LIMIT 1",
        (ENTRY_STRATEGY_TAG, *ENTRY_ACTIONS),
    ).fetchone()
    last_entry: dict[str, Any] | None = None
    if row is not None:
        last_entry = {
            "trade_id": int(row[0]),
            "timestamp": float(row[1]),
            "time_cst": _fmt_cst(float(row[1])),
            "inst_id": str(row[2]),
            "direction": str(row[3]),
        }
        ref_ts = float(row[1])
    else:
        # Fresh ledger: count from the first LLM decision so a new box is not
        # alerted before it has run for the threshold.
        first = conn.execute(
            "SELECT MIN(timestamp) FROM decisions WHERE policy_name = 'llm'"
        ).fetchone()
        ref_ts = float(first[0]) if first and first[0] is not None else now_ts
    hours_since = max(0.0, (now_ts - ref_ts) / 3600.0)

    # Worker liveness (last worker_cycle_summary).
    ev = conn.execute(
        "SELECT MAX(timestamp) FROM events WHERE event_type = 'worker_cycle_summary'"
    ).fetchone()
    last_cycle_ts = float(ev[0]) if ev and ev[0] is not None else None
    lag = None if last_cycle_ts is None else max(0, int(now_ts - last_cycle_ts))
    stale_thr = _stale_threshold_seconds(interval)
    worker_stale = lag is None or lag > stale_thr

    window_h = thr_h
    since = now_ts - window_h * 3600.0
    n_cycles = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'worker_cycle_summary' AND timestamp >= ?",
        (since,),
    ).fetchone()[0]
    expected = max(1.0, window_h * 3600.0 / interval)
    coverage = min(1.0, float(n_cycles or 0) / expected)

    counts = {k: 0 for k in VETO_KEYS}
    n_dec = 0
    n_4h_neutral = 0
    for action, calc_raw in conn.execute(
        "SELECT action, calculus_data FROM decisions "
        "WHERE timestamp >= ? AND policy_name = 'llm'",
        (since,),
    ):
        try:
            calc = json.loads(calc_raw) if calc_raw else {}
        except (TypeError, ValueError):
            calc = {}
        if not isinstance(calc, dict):
            calc = {}
        n_dec += 1
        counts[_classify(action, calc.get("signal_diag"))] += 1
        if str(calc.get("trend_4h") or "").lower() == "neutral":
            n_4h_neutral += 1
    shares = {k: (counts[k] / n_dec if n_dec else 0.0) for k in VETO_KEYS}
    neutral_share = n_4h_neutral / n_dec if n_dec else 0.0

    triggered = hours_since >= thr_h
    outage = worker_stale or coverage < MIN_CYCLE_COVERAGE
    if not triggered:
        kind, severity = "ok", "ok"
    elif outage:
        kind, severity = "outage", "critical"
    else:
        kind, severity = "market", "warning"

    veto_zh = "、".join(
        f"{VETO_LABEL_ZH[k]} {_pct(shares[k])}" for k in ("4h_hard", "adx", "htf_other", "position_cooldown", "other_gate", "llm_wait")
    )
    stats_zh = (
        f"近{window_h:g}h {n_dec} 条 LLM 决策：{veto_zh}；4h=neutral 占 {_pct(neutral_share)}"
    )
    last_zh = (
        f"{last_entry['time_cst']} {last_entry['inst_id']} {last_entry['direction']}"
        if last_entry
        else "账本中无 LLM 开仓记录"
    )
    lag_zh = "无周期记录" if lag is None else f"{lag}s 前"
    if kind == "ok":
        msg = (
            f"正常：最近一次 LLM 开仓成交 {hours_since:.1f}h 前（{last_zh}），"
            f"未达 {thr_h:g}h 零开仓阈值。"
        )
    elif kind == "market":
        msg = (
            f"⚠️ {thr_h:g}h 零开仓（行情驱动，非故障）：已 {hours_since:.1f}h 无 LLM 开仓成交"
            f"（上次 {last_zh}）。服务正常：worker 上次周期 {lag_zh}，"
            f"窗口周期覆盖率 {_pct(coverage)}。{stats_zh}。"
            f"多为 4h 无趋势/ADX 偏弱导致零开火；若持续可评估是否临时放宽 4h 模式。"
        )
    else:
        why = (
            f"worker 停滞（上次周期 {lag_zh}，阈值 {stale_thr}s）"
            if worker_stale
            else f"近{window_h:g}h 周期覆盖率仅 {_pct(coverage)}（服务曾中断，worker 当前已恢复）"
        )
        msg = (
            f"🛑 {thr_h:g}h 零开仓（服务故障，非行情）：已 {hours_since:.1f}h 无 LLM 开仓成交"
            f"（上次 {last_zh}），{why}。请先查 scripts/observe_status.sh 与 "
            f"data/run/keel-watchdog.log。参考：{stats_zh}。"
        )

    base.update(
        triggered=bool(triggered),
        kind=kind,
        severity=severity,
        hours_since_last_entry=round(hours_since, 2),
        last_entry=last_entry,
        worker_stale=bool(worker_stale),
        seconds_since_last_cycle=lag,
        worker_stale_threshold_seconds=stale_thr,
        window_hours=window_h,
        cycle_count=int(n_cycles or 0),
        cycle_coverage=round(coverage, 3),
        decisions_in_window=n_dec,
        veto_counts=counts,
        veto_shares={k: round(v, 3) for k, v in shares.items()},
        trend_4h_neutral_share=round(neutral_share, 3),
        message_zh=msg,
    )
    return base


def safe_compute_zero_entry_alert(ledger: Any, **kwargs: Any) -> dict[str, Any] | None:
    """Soft-fail wrapper for read paths (/ready, stats): None on any error."""
    try:
        return compute_zero_entry_alert(ledger, **kwargs)
    except Exception:
        return None


__all__ = [
    "ALERT_CODE",
    "DEFAULT_THRESHOLD_HOURS",
    "VETO_KEYS",
    "compute_zero_entry_alert",
    "safe_compute_zero_entry_alert",
    "zero_entry_threshold_hours",
]
