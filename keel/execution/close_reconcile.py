"""
Reconcile exchange flats into ledger close trades.

OKX demo often exits via attached TP/SL algo orders without going through
``ExecutionOrchestrator``, so opens were recorded but closes were not.

Design (no invented history):
- Each cycle snapshots live positions → ``positions_seen`` event with matched
  ``open_trade_ids``.
- Next cycle, keys present last time but gone now → record ``trades(action=close)``
  plus ``sl_hit`` / ``tp_hit`` / ``close`` events.
- First cycle after deploy only baselines; historical orphan opens stay unmatched.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from keel.domain.instruments import lookup_instrument
from keel.domain.records import TradeRecord

logger = logging.getLogger("keel.execution.close_reconcile")

POSITIONS_SEEN_EVENT = "positions_seen"
ExitReason = Literal["sl", "tp", "manual", "unknown"]


@dataclass(frozen=True)
class CloseOutcome:
    inst_id: str
    direction: str
    size: float
    price: float
    pnl: float | None
    exit_reason: ExitReason
    open_trade_id: int
    trade_id: int
    event_type: str


def _pos_field(pos: Any, name: str) -> Any:
    if isinstance(pos, dict):
        return pos.get(name)
    return getattr(pos, name, None)


def realized_pnl(
    *,
    side: str,
    entry: float,
    exit_price: float,
    size: float,
    inst_id: str,
) -> float:
    """USDT-margined SWAP PnL for ``size`` contracts."""
    cv = lookup_instrument(inst_id).contract_value or 1.0
    signed = (float(exit_price) - float(entry)) * float(size) * float(cv)
    if str(side).lower() == "short":
        signed = -signed
    return signed


def infer_exit_reason(
    *,
    side: str,
    entry: float,
    exit_price: float,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    algo: dict[str, Any] | None = None,
) -> ExitReason:
    """Classify SL vs TP vs unknown from fill vs triggers (and optional algo row)."""
    side_s = str(side or "").lower()
    exit_px = float(exit_price or 0.0)
    entry_f = float(entry or 0.0)
    sl = float(stop_loss) if stop_loss not in (None, "", 0, 0.0) else None
    tp = float(take_profit) if take_profit not in (None, "", 0, 0.0) else None

    if algo:
        try:
            trigger = float(algo.get("triggerPx") or 0) or None
        except (TypeError, ValueError):
            trigger = None
        try:
            algo_sl = float(algo.get("slTriggerPx") or algo.get("sl_trigger_price") or 0) or None
        except (TypeError, ValueError):
            algo_sl = None
        try:
            algo_tp = float(algo.get("tpTriggerPx") or algo.get("tp_trigger_price") or 0) or None
        except (TypeError, ValueError):
            algo_tp = None
        if algo_sl is not None:
            sl = algo_sl if sl is None else sl
        if algo_tp is not None:
            tp = algo_tp if tp is None else tp
        if trigger is not None and sl is not None and abs(trigger - sl) <= max(1e-8, abs(sl) * 1e-6):
            return "sl"
        if trigger is not None and tp is not None and abs(trigger - tp) <= max(1e-8, abs(tp) * 1e-6):
            return "tp"

    if exit_px <= 0 or entry_f <= 0:
        return "unknown"

    # Prefer proximity of exit to SL/TP levels.
    if sl is not None and tp is not None:
        if abs(exit_px - sl) <= abs(exit_px - tp):
            return "sl"
        return "tp"
    if sl is not None and abs(exit_px - sl) / max(abs(sl), 1e-9) < 0.003:
        return "sl"
    if tp is not None and abs(exit_px - tp) / max(abs(tp), 1e-9) < 0.003:
        return "tp"

    # Directional fallback vs entry when only one side of book is known.
    if side_s == "long":
        if sl is not None and exit_px <= sl:
            return "sl"
        if tp is not None and exit_px >= tp:
            return "tp"
    elif side_s == "short":
        if sl is not None and exit_px >= sl:
            return "sl"
        if tp is not None and exit_px <= tp:
            return "tp"
    return "unknown"


def _event_type_for_reason(reason: ExitReason) -> str:
    if reason == "sl":
        return "sl_hit"
    if reason == "tp":
        return "tp_hit"
    return "close"


def _closed_open_ids(ledger: Any, inst_id: str) -> set[int]:
    """Open trade ids that already have a close row linked via metadata."""
    out: set[int] = set()
    try:
        closes = ledger.get_trades(inst_id=inst_id, action="close", limit=500)
    except Exception:
        return out
    for t in closes:
        meta = t.metadata or {}
        raw = meta.get("open_trade_id")
        if raw is None:
            continue
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def unmatched_open_trades(ledger: Any, inst_id: str, direction: str) -> list[TradeRecord]:
    """Open/scale_in rows for inst+side without a linked close."""
    closed = _closed_open_ids(ledger, inst_id)
    opens: list[TradeRecord] = []
    for action in ("open", "scale_in"):
        try:
            rows = ledger.get_trades(inst_id=inst_id, action=action, limit=500)
        except Exception:
            rows = []
        for t in rows:
            if str(t.direction).lower() != str(direction).lower():
                continue
            if t.id is None or int(t.id) in closed:
                continue
            opens.append(t)
    opens.sort(key=lambda t: (float(t.timestamp or 0.0), int(t.id or 0)))
    return opens


def match_open_trade_ids(opens: list[TradeRecord], size: float) -> list[int]:
    """Newest-first covers of ``size`` contracts → open trade ids to track."""
    remaining = float(size)
    ids: list[int] = []
    for t in reversed(opens):
        if t.id is None:
            continue
        ids.append(int(t.id))
        remaining -= float(t.size or 0.0)
        if remaining <= 1e-12:
            break
    return list(reversed(ids))


def load_last_positions_seen(ledger: Any) -> dict[str, dict[str, Any]]:
    """Return last ``positions_seen`` map keyed by ``inst_id|side``."""
    try:
        events = ledger.get_events(event_type=POSITIONS_SEEN_EVENT, limit=1)
    except Exception:
        return {}
    if not events:
        return {}
    data = events[0].data or {}
    positions = data.get("positions")
    if not isinstance(positions, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in positions:
        if not isinstance(row, dict):
            continue
        inst = str(row.get("inst_id") or "")
        side = str(row.get("side") or "").lower()
        if not inst or side not in ("long", "short"):
            continue
        out[f"{inst}|{side}"] = row
    return out


def _fetch_algo_history(exchange: Any, inst_id: str | None = None) -> list[dict[str, Any]]:
    getter = getattr(exchange, "get_algo_history", None)
    if not callable(getter):
        return []
    try:
        rows = list(getter(inst_id=inst_id) or [])
    except TypeError:
        try:
            rows = list(getter(inst_id) or [])
        except Exception:
            logger.debug("algo history failed", exc_info=True)
            return []
    except Exception:
        logger.debug("algo history failed", exc_info=True)
        return []
    return [r for r in rows if isinstance(r, dict)]


def _pick_algo_fill(
    algos: list[dict[str, Any]],
    *,
    inst_id: str,
    side: str,
    since_ts: float,
) -> dict[str, Any] | None:
    """Best-effort effective algo fill for inst+side after ``since_ts``."""
    side_s = str(side).lower()
    candidates: list[tuple[float, dict[str, Any]]] = []
    for row in algos:
        if str(row.get("instId") or row.get("inst_id") or "") != inst_id:
            continue
        pos_side = str(row.get("posSide") or row.get("pos_side") or "").lower()
        if pos_side and pos_side not in ("net", side_s):
            continue
        state = str(row.get("state") or "").lower()
        if state and state not in ("effective", "partially_effective"):
            # Some payloads omit state when already filtered by the adapter.
            if state not in ("", "filled"):
                continue
        ts_ms = row.get("uTime") or row.get("cTime") or row.get("triggerTime") or 0
        try:
            ts = float(ts_ms) / (1000.0 if float(ts_ms) > 1e12 else 1.0)
        except (TypeError, ValueError):
            ts = 0.0
        if since_ts and ts and ts + 1.0 < since_ts:
            continue
        candidates.append((ts, row))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _exit_price_from_sources(
    *,
    exchange: Any,
    inst_id: str,
    algo: dict[str, Any] | None,
    fallback_mark: float | None,
) -> float:
    if algo:
        for key in ("actualPx", "actual_px", "avgPx", "fillPx"):
            raw = algo.get(key)
            try:
                px = float(raw) if raw not in (None, "") else 0.0
            except (TypeError, ValueError):
                px = 0.0
            if px > 0:
                return px
    getter = getattr(exchange, "get_ticker", None)
    if callable(getter):
        try:
            ticker = getter(inst_id)
        except Exception:
            ticker = None
        if ticker is not None:
            last = float(_pos_field(ticker, "last") or 0.0)
            if last > 0:
                return last
    if fallback_mark and float(fallback_mark) > 0:
        return float(fallback_mark)
    return 0.0


def record_close_for_open(
    ledger: Any,
    *,
    open_trade: TradeRecord,
    exit_price: float,
    exit_reason: ExitReason,
    now: float | None = None,
    algo: dict[str, Any] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> CloseOutcome | None:
    """Append close trade + distinct hit/close event. Idempotent on open_trade_id."""
    if open_trade.id is None:
        return None
    open_id = int(open_trade.id)
    if open_id in _closed_open_ids(ledger, open_trade.inst_id):
        return None
    ts = time.time() if now is None else float(now)
    entry = float(open_trade.price or 0.0)
    size = float(open_trade.size or 0.0)
    direction = str(open_trade.direction or "long").lower()
    if direction not in ("long", "short"):
        direction = "long"
    pnl = None
    if entry > 0 and exit_price > 0 and size > 0:
        pnl = realized_pnl(
            side=direction,
            entry=entry,
            exit_price=exit_price,
            size=size,
            inst_id=open_trade.inst_id,
        )
    meta: dict[str, Any] = {
        "open_trade_id": open_id,
        "exit_reason": exit_reason,
        "entry_price": entry,
    }
    open_meta = open_trade.metadata or {}
    for key in ("order_id", "decision_id", "take_profit", "stop_loss", "leverage"):
        if key in open_meta:
            meta[key] = open_meta[key]
    if algo:
        meta["algo_id"] = str(algo.get("algoId") or algo.get("algo_id") or "")
        meta["algo_state"] = str(algo.get("state") or "")
    if extra_meta:
        meta.update(extra_meta)

    trade_id = ledger.record_trade(
        TradeRecord(
            timestamp=ts,
            inst_id=open_trade.inst_id,
            action="close",
            direction=direction,  # type: ignore[arg-type]
            size=size,
            price=float(exit_price),
            pnl=pnl,
            strategy_tag=open_trade.strategy_tag or "keel-llm",
            reason=f"exit:{exit_reason}",
            metadata=meta,
        )
    )
    event_type = _event_type_for_reason(exit_reason)
    ledger.record_event(
        event_type,
        inst_id=open_trade.inst_id,
        data={
            "open_trade_id": open_id,
            "close_trade_id": trade_id,
            "exit_reason": exit_reason,
            "direction": direction,
            "size": size,
            "price": float(exit_price),
            "pnl": pnl,
            "entry_price": entry,
        },
        timestamp=ts,
    )
    return CloseOutcome(
        inst_id=open_trade.inst_id,
        direction=direction,
        size=size,
        price=float(exit_price),
        pnl=pnl,
        exit_reason=exit_reason,
        open_trade_id=open_id,
        trade_id=int(trade_id),
        event_type=event_type,
    )


def _build_current_snapshot(
    exchange: Any,
    ledger: Any,
    positions: list[Any],
) -> dict[str, dict[str, Any]]:
    snap: dict[str, dict[str, Any]] = {}
    for pos in positions:
        inst = str(_pos_field(pos, "inst_id") or "")
        side = str(_pos_field(pos, "side") or "").lower()
        size = float(_pos_field(pos, "size") or 0.0)
        avg = float(_pos_field(pos, "avg_price") or 0.0)
        mark = float(_pos_field(pos, "mark_price") or 0.0)
        if not inst or side not in ("long", "short") or size <= 0:
            continue
        opens = unmatched_open_trades(ledger, inst, side)
        open_ids = match_open_trade_ids(opens, size)
        snap[f"{inst}|{side}"] = {
            "inst_id": inst,
            "side": side,
            "size": size,
            "avg_price": avg,
            "mark_price": mark,
            "open_trade_ids": open_ids,
        }
    return snap


def reconcile_closed_positions(
    exchange: Any,
    ledger: Any,
    *,
    now: float | None = None,
    positions: list[Any] | None = None,
) -> list[CloseOutcome]:
    """
    Detect positions that vanished since the last cycle and ledger closes.

    First successful snapshot only baselines (no closes). Subsequent cycles
    emit close rows solely for ``open_trade_ids`` that were tracked while live.

    Pass ``positions`` when the caller already fetched the book (avoids an extra
    OKX round-trip / 503). When omitted, fetches via ``exchange.get_positions``.
    """
    if ledger is None:
        return []
    if positions is None:
        getter = getattr(exchange, "get_positions", None)
        if not callable(getter):
            return []
        try:
            positions = list(getter() or [])
        except Exception:
            logger.warning("close_reconcile: get_positions failed", exc_info=True)
            return []
    else:
        positions = list(positions)

    ts = time.time() if now is None else float(now)
    last = load_last_positions_seen(ledger)
    current = _build_current_snapshot(exchange, ledger, positions)
    outcomes: list[CloseOutcome] = []

    if last:
        vanished_keys = set(last) - set(current)
        # Prefetch algo history once when anything vanished.
        algo_cache: dict[str, list[dict[str, Any]]] = {}
        for key in vanished_keys:
            prev = last[key]
            inst = str(prev.get("inst_id") or "")
            side = str(prev.get("side") or "").lower()
            if not inst or side not in ("long", "short"):
                continue
            open_ids = prev.get("open_trade_ids") or []
            if not isinstance(open_ids, list) or not open_ids:
                # Nothing tracked while live — do not invent closes from orphans.
                continue
            if inst not in algo_cache:
                algo_cache[inst] = _fetch_algo_history(exchange, inst)
            algos = algo_cache[inst]
            # Oldest tracked open timestamp bounds algo search.
            open_rows: list[TradeRecord] = []
            for oid in open_ids:
                try:
                    oid_i = int(oid)
                except (TypeError, ValueError):
                    continue
                # Look up among unmatched + already-listed opens for this inst.
                found = None
                for t in unmatched_open_trades(ledger, inst, side):
                    if t.id == oid_i:
                        found = t
                        break
                if found is None:
                    # May already be closed (idempotent skip) or missing.
                    continue
                open_rows.append(found)
            if not open_rows:
                continue
            since_ts = min(float(t.timestamp or 0.0) for t in open_rows)
            algo = _pick_algo_fill(algos, inst_id=inst, side=side, since_ts=since_ts)
            fallback_mark = prev.get("mark_price") or prev.get("avg_price")
            exit_px = _exit_price_from_sources(
                exchange=exchange,
                inst_id=inst,
                algo=algo,
                fallback_mark=float(fallback_mark) if fallback_mark else None,
            )
            for open_trade in open_rows:
                meta = open_trade.metadata or {}
                sl = meta.get("stop_loss")
                tp = meta.get("take_profit")
                try:
                    sl_f = float(sl) if sl not in (None, "") else None
                except (TypeError, ValueError):
                    sl_f = None
                try:
                    tp_f = float(tp) if tp not in (None, "") else None
                except (TypeError, ValueError):
                    tp_f = None
                reason = infer_exit_reason(
                    side=side,
                    entry=float(open_trade.price or 0.0),
                    exit_price=exit_px,
                    stop_loss=sl_f,
                    take_profit=tp_f,
                    algo=algo,
                )
                # If we have no price at all, still record with entry as last resort
                # so the open is closed for bookkeeping (pnl None / 0).
                px = exit_px if exit_px > 0 else float(open_trade.price or 0.0)
                outcome = record_close_for_open(
                    ledger,
                    open_trade=open_trade,
                    exit_price=px,
                    exit_reason=reason,
                    now=ts,
                    algo=algo,
                    extra_meta={"source": "position_vanish"},
                )
                if outcome is not None:
                    outcomes.append(outcome)

    # Always refresh baseline snapshot (including first run).
    try:
        ledger.record_event(
            POSITIONS_SEEN_EVENT,
            data={
                "positions": list(current.values()),
                "count": len(current),
            },
            timestamp=ts,
        )
    except Exception:
        logger.debug("close_reconcile: failed to write positions_seen", exc_info=True)

    if outcomes:
        logger.info(
            "close_reconcile: recorded %s close(s): %s",
            len(outcomes),
            ", ".join(f"{o.inst_id}/{o.exit_reason}" for o in outcomes),
        )
    return outcomes


__all__ = [
    "POSITIONS_SEEN_EVENT",
    "CloseOutcome",
    "infer_exit_reason",
    "match_open_trade_ids",
    "realized_pnl",
    "reconcile_closed_positions",
    "record_close_for_open",
    "unmatched_open_trades",
]
