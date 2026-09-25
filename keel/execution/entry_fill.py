"""
Entry fill truth for limit orders (9/24 post-mortem).

OKX accepts a limit order long before it fills (11 of 18 LLM entries since
9/22 21:00 rested 2 s – 48 min). Recording the ledger ``open`` at acceptance produced
opens with no position ("#292 open while OKX flat"), limit price instead of
avgPx, and a false ``sl_tp_attach_failed=no_pending_oco`` because OKX only
materializes ``attachAlgoOrds`` once the parent fills.

Flow now:
- after ``place_order`` poll ``get_order`` briefly (KEEL_ENTRY_FILL_WAIT_SECONDS);
- filled → ledger open at avgPx / fillTime / fee, then confirm TP/SL (retry,
  and place a standalone full-position OCO when the attached one is missing);
- still resting → ``order_resting`` event only; each cycle
  ``reconcile_resting_entries`` records the open on fill or cancels after
  KEEL_ENTRY_TTL_SECONDS (0 = never cancel).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from keel.config.settings import _env

logger = logging.getLogger("keel.execution.entry_fill")

ORDER_RESTING_EVENT = "order_resting"
ORDER_RESOLVED_EVENT = "order_entry_resolved"
DEFAULT_FILL_WAIT_SECONDS = 8.0
DEFAULT_ENTRY_TTL_SECONDS = 900.0
_TERMINAL = frozenset({"filled", "canceled", "cancelled", "mmp_canceled"})


def _num(key: str, default: float) -> float:
    raw = (_env(key, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def entry_fill_wait_seconds() -> float:
    """Seconds to poll for a fill right after placement (clamped 0–30)."""
    return max(0.0, min(30.0, _num("KEEL_ENTRY_FILL_WAIT_SECONDS", DEFAULT_FILL_WAIT_SECONDS)))


def entry_ttl_seconds() -> float:
    """Cancel a still-resting entry after this many seconds (0 disables; max 1 day)."""
    return max(0.0, min(86400.0, _num("KEEL_ENTRY_TTL_SECONDS", DEFAULT_ENTRY_TTL_SECONDS)))


def _f(raw: Any) -> float:
    try:
        return float(raw) if raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class FillInfo:
    state: str
    filled_size: float
    avg_px: float
    fill_ts: float | None
    fee: float  # OKX sign: negative = cost
    attach_fail: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    @property
    def fully_filled(self) -> bool:
        return self.state == "filled" and self.filled_size > 0


def parse_order_row(row: dict[str, Any] | None) -> FillInfo | None:
    if not isinstance(row, dict):
        return None
    fill_ms = _f(row.get("fillTime"))
    fails: list[str] = []
    for algo in row.get("attachAlgoOrds") or []:
        if not isinstance(algo, dict):
            continue
        code = str(algo.get("failCode") or "").strip()
        why = str(algo.get("failReason") or "").strip()
        if code or why:
            fails.append(f"{code} {why}".strip())
    return FillInfo(
        state=str(row.get("state") or "unknown").lower(),
        filled_size=_f(row.get("accFillSz")),
        avg_px=_f(row.get("avgPx")) or _f(row.get("fillPx")),
        fill_ts=(fill_ms / 1000.0) if fill_ms > 0 else None,
        fee=_f(row.get("fee")),
        attach_fail="; ".join(fails),
    )


def fetch_fill(exchange: Any, inst_id: str, order_id: str | None) -> FillInfo | None:
    """None when the adapter cannot look orders up (paper / stubs) or on error."""
    getter = getattr(exchange, "get_order", None)
    if not callable(getter) or not order_id:
        return None
    try:
        return parse_order_row(getter(inst_id, str(order_id)))
    except Exception:
        logger.debug("get_order failed %s %s", inst_id, order_id, exc_info=True)
        return None


def await_fill(
    exchange: Any,
    inst_id: str,
    order_id: str | None,
    *,
    wait_seconds: float | None = None,
    poll_seconds: float = 1.0,
    sleep: Callable[[float], None] | None = None,
) -> FillInfo | None:
    """Poll ``get_order`` until terminal or ``wait_seconds`` spent. None if unsupported."""
    sleep = sleep or time.sleep
    budget = entry_fill_wait_seconds() if wait_seconds is None else max(0.0, float(wait_seconds))
    info = fetch_fill(exchange, inst_id, order_id)
    if info is None:
        return None
    spent = 0.0
    while not info.terminal and spent < budget:
        step = max(0.05, min(float(poll_seconds), budget - spent))
        sleep(step)
        spent += step
        nxt = fetch_fill(exchange, inst_id, order_id)
        if nxt is not None:
            info = nxt
    return info


def _matching_algos(algos: list[Any], inst_id: str, pos_side: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in algos or []:
        if not isinstance(row, dict):
            continue
        inst = str(row.get("instId") or row.get("inst_id") or "")
        if inst and inst != inst_id:
            continue
        side = str(row.get("posSide") or row.get("pos_side") or "").lower()
        if side and side not in (pos_side, "net"):
            continue
        out.append(row)
    return out


def confirm_protection(
    exchange: Any,
    ledger: Any,
    *,
    inst_id: str,
    pos_side: str,
    stop_loss: float | None,
    take_profit: float | None,
    event_base: dict[str, Any] | None = None,
    retries: int = 2,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """
    After a real fill: find the live TP/SL algo (retry — OKX creates it a beat
    after the fill). Missing → place a standalone full-position OCO. Returns a
    metadata stamp; ``{}`` when the adapter cannot list algos.
    """
    sleep = sleep or time.sleep
    want = (stop_loss or 0) > 0 or (take_profit or 0) > 0
    lister = getattr(exchange, "get_pending_oco", None)
    if not want or not callable(lister):
        return {}
    matching: list[dict[str, Any]] = []
    for attempt in range(max(0, int(retries)) + 1):
        try:
            matching = _matching_algos(list(lister(inst_id) or []), inst_id, pos_side)
        except Exception:
            matching = []
        if matching or attempt >= retries:
            break
        sleep(1.0)
    algo_ids = [
        str(a.get("algoId") or a.get("algo_id") or "")
        for a in matching
        if (a.get("algoId") or a.get("algo_id"))
    ]
    payload = {
        **(event_base or {}),
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "algo_ids": algo_ids,
        "pending_algo_count": len(matching),
    }
    stamp: dict[str, Any] = {"algo_ids": algo_ids, "pending_algo_count": len(matching)}
    if matching:
        ledger.record_event("sl_tp_attached", inst_id=inst_id, data=payload)
        stamp["sl_tp_attached"] = True
        return stamp

    placer = getattr(exchange, "place_oco_tpsl", None)
    repaired: str | None = None
    if callable(placer):
        try:
            repaired = placer(
                inst_id,
                pos_side,
                sl=float(stop_loss) if (stop_loss or 0) > 0 else None,
                tp=float(take_profit) if (take_profit or 0) > 0 else None,
            )
        except Exception:
            logger.warning("place_oco_tpsl failed %s", inst_id, exc_info=True)
            repaired = None
    if repaired:
        ledger.record_event(
            "sl_tp_repaired",
            inst_id=inst_id,
            data={**payload, "algo_ids": [str(repaired)], "reason": "attach_missing_after_fill"},
        )
        stamp.update(
            {
                "algo_ids": [str(repaired)],
                "sl_tp_attached": True,
                "sl_tp_repaired": True,
            }
        )
        return stamp

    ledger.record_event(
        "sl_tp_attach_failed",
        inst_id=inst_id,
        data={**payload, "reason": "no_pending_oco_after_fill"},
    )
    stamp["sl_tp_attach_failed"] = True
    stamp["sl_tp_attach_reason"] = "no_pending_oco_after_fill"
    return stamp


def pending_resting_entries(ledger: Any, *, limit: int = 200) -> list[Any]:
    """``order_resting`` events without a matching ``order_entry_resolved``."""
    try:
        resting = ledger.get_events(event_type=ORDER_RESTING_EVENT, limit=limit)
        resolved = ledger.get_events(event_type=ORDER_RESOLVED_EVENT, limit=limit * 2)
    except Exception:
        return []
    done = {str((e.data or {}).get("order_id") or "") for e in resolved}
    out = []
    for ev in resting:
        data = ev.data or {}
        oid = str(data.get("order_id") or "")
        # Only rows carrying replay context (older resting events were paper-only).
        if not oid or oid in done or not isinstance(data.get("ctx"), dict):
            continue
        out.append(ev)
    return out


def reconcile_resting_entries(
    exchange: Any,
    ledger: Any,
    *,
    record_open: Callable[[dict[str, Any], FillInfo], Any],
    now: float | None = None,
    ttl_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """
    Resolve resting LLM entries: ledger the open once filled, or cancel after
    TTL (recording any partial fill). Returns resolution rows for logging.
    """
    if ledger is None:
        return []
    stamp = time.time() if now is None else float(now)
    ttl = entry_ttl_seconds() if ttl_seconds is None else max(0.0, float(ttl_seconds))
    out: list[dict[str, Any]] = []
    for ev in pending_resting_entries(ledger):
        data = ev.data or {}
        ctx = dict(data.get("ctx") or {})
        inst = str(ev.inst_id or ctx.get("inst_id") or "")
        oid = str(data.get("order_id") or "")
        info = fetch_fill(exchange, inst, oid)
        if info is None:
            continue
        age = stamp - float(ev.timestamp or stamp)
        outcome: str | None = None
        if info.fully_filled:
            outcome = "filled"
        elif info.terminal:
            outcome = "canceled_partial" if info.filled_size > 0 else "canceled"
        elif ttl > 0 and age >= ttl:
            canceller = getattr(exchange, "cancel_order", None)
            ok = False
            if callable(canceller):
                try:
                    ok = bool(canceller(inst, oid))
                except Exception:
                    ok = False
            again = fetch_fill(exchange, inst, oid) or info
            if again.fully_filled:
                info, outcome = again, "filled"
            elif ok or again.terminal:
                info = again
                outcome = "canceled_stale_partial" if again.filled_size > 0 else "canceled_stale"
        if outcome is None:
            continue
        if info.filled_size > 0:
            try:
                record_open(ctx, info)
            except Exception:
                logger.exception("record_open failed for resting entry %s", oid)
                continue
        row = {
            "order_id": oid,
            "outcome": outcome,
            "filled_size": info.filled_size,
            "avg_px": info.avg_px,
            "age_seconds": round(age, 1),
            "ttl_seconds": ttl,
            "decision_id": ctx.get("decision_id"),
        }
        ledger.record_event(ORDER_RESOLVED_EVENT, inst_id=inst, data=row, timestamp=stamp)
        out.append(row)
    return out


__all__ = [
    "DEFAULT_ENTRY_TTL_SECONDS",
    "DEFAULT_FILL_WAIT_SECONDS",
    "FillInfo",
    "ORDER_RESOLVED_EVENT",
    "ORDER_RESTING_EVENT",
    "await_fill",
    "confirm_protection",
    "entry_fill_wait_seconds",
    "entry_ttl_seconds",
    "fetch_fill",
    "parse_order_row",
    "pending_resting_entries",
    "reconcile_resting_entries",
]
