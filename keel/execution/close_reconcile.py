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

9/24 post-mortem fixes:
- A key (``inst|side``) that stays live while its tracked open ids change
  (old position SL'd, new one opened between two cycles — #274/#277) now
  closes the dropped ids instead of silently re-pointing the key.
- Exit price / PnL / fees come from OKX ``positions-history`` (closeAvgPx,
  realizedPnl, fee, fundingFee, uTime) when the adapter supports it; the old
  ticker-at-detection estimate is only a fallback.
- Live ``keel-llm`` opens inside ``KEEL_CLOSE_BACKFILL_LOOKBACK_HOURS`` that no
  live position covers are closed only when positions-history proves it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from keel.config.settings import _env
from keel.domain.instruments import lookup_instrument
from keel.domain.records import TradeRecord
from keel.execution.entry_fill import fetch_fill

logger = logging.getLogger("keel.execution.close_reconcile")

POSITIONS_SEEN_EVENT = "positions_seen"
DEFAULT_BACKFILL_LOOKBACK_HOURS = 72.0
_BACKFILL_GRACE_SECONDS = 120.0
_MAX_ORDER_LOOKUPS = 8
_HISTORY_MATCH_TOLERANCE_S = 5.0
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
        actual = str(algo.get("actualSide") or "").strip().lower()
        if actual in ("sl", "tp"):
            return actual  # type: ignore[return-value]
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
    pnl_override: float | None = None,
    fee: float = 0.0,
) -> CloseOutcome | None:
    """
    Append close trade + distinct hit/close event. Idempotent on open_trade_id.

    ``pnl_override`` (e.g. OKX realizedPnl, fee+funding inclusive) replaces the
    price-diff estimate; ``fee`` is stored as a positive cost.
    """
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
    if pnl_override is not None:
        pnl = float(pnl_override)
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
            fee=abs(float(fee or 0.0)),
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


def backfill_lookback_seconds() -> float:
    """KEEL_CLOSE_BACKFILL_LOOKBACK_HOURS (default 72; 0 disables the sweep)."""
    raw = (_env("KEEL_CLOSE_BACKFILL_LOOKBACK_HOURS", "") or "").strip()
    try:
        hours = float(raw) if raw else DEFAULT_BACKFILL_LOOKBACK_HOURS
    except ValueError:
        hours = DEFAULT_BACKFILL_LOOKBACK_HOURS
    return max(0.0, min(hours, 24.0 * 90)) * 3600.0


def _fetch_positions_history(exchange: Any) -> list[dict[str, Any]] | None:
    """None when the adapter has no positions-history (paper / mocks)."""
    getter = getattr(exchange, "get_positions_history", None)
    if not callable(getter):
        return None
    try:
        rows = getter()
    except Exception:
        logger.debug("positions history failed", exc_info=True)
        return []
    if not isinstance(rows, (list, tuple)):
        return None
    return [r for r in rows if isinstance(r, dict)]


def _is_live_open(t: TradeRecord) -> bool:
    meta = t.metadata or {}
    return (t.strategy_tag or "") == "keel-llm" and not meta.get("shadow") and not meta.get("probe")


class _FillTsResolver:
    """Open fill time: metadata ``fill_ts`` else OKX order ``fillTime`` (bounded lookups)."""

    def __init__(self, exchange: Any, max_lookups: int = _MAX_ORDER_LOOKUPS) -> None:
        self._exchange = exchange
        self._left = int(max_lookups)

    def __call__(self, t: TradeRecord) -> float | None:
        meta = t.metadata or {}
        try:
            ts = float(meta.get("fill_ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0:
            return ts
        oid = meta.get("order_id")
        if not oid or self._left <= 0:
            return None
        self._left -= 1
        info = fetch_fill(self._exchange, t.inst_id, str(oid))
        return info.fill_ts if info is not None else None


def match_history_close(
    history: list[dict[str, Any]],
    *,
    inst_id: str,
    side: str,
    fill_ts: float,
    tolerance_s: float = _HISTORY_MATCH_TOLERANCE_S,
) -> dict[str, Any] | None:
    """Closed position whose open time (cTime) matches the entry fill time."""
    best: tuple[float, dict[str, Any]] | None = None
    for row in history:
        if str(row.get("instId") or "") != inst_id:
            continue
        pos_side = str(row.get("posSide") or "").lower()
        if pos_side not in ("net", "", str(side).lower()):
            continue
        try:
            c_ts = float(row.get("cTime") or 0) / 1000.0
            close_px = float(row.get("closeAvgPx") or 0)
            u_ts = float(row.get("uTime") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        if close_px <= 0 or u_ts <= 0:
            continue
        gap = abs(c_ts - float(fill_ts))
        if gap > tolerance_s:
            continue
        if best is None or gap < best[0]:
            best = (gap, row)
    return best[1] if best else None


def _history_close_fields(row: dict[str, Any], open_trade: TradeRecord) -> dict[str, Any]:
    def f(key: str) -> float:
        try:
            return float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    total = f("closeTotalPos") or float(open_trade.size or 0.0) or 1.0
    share = min(1.0, float(open_trade.size or total) / total) if total > 0 else 1.0
    return {
        "exit_px": f("closeAvgPx"),
        "close_ts": f("uTime") / 1000.0,
        "net_pnl": f("realizedPnl") * share,
        "gross_pnl": f("pnl") * share,
        "fee_total": f("fee") * share,
        "funding_fee": f("fundingFee") * share,
        "okx_open_avg_px": f("openAvgPx"),
        "pos_id": str(row.get("posId") or ""),
        "close_type": str(row.get("type") or ""),
    }


def _close_from_history(
    ledger: Any,
    open_trade: TradeRecord,
    row: dict[str, Any],
    *,
    algos: list[dict[str, Any]],
    source: str,
) -> CloseOutcome | None:
    h = _history_close_fields(row, open_trade)
    meta = open_trade.metadata or {}
    side = str(open_trade.direction or "long").lower()

    def fnum(raw: Any) -> float | None:
        try:
            return float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    algo = _pick_algo_fill(
        algos, inst_id=open_trade.inst_id, side=side, since_ts=float(open_trade.timestamp or 0.0)
    )
    entry = h["okx_open_avg_px"] or float(open_trade.price or 0.0)
    reason = infer_exit_reason(
        side=side,
        entry=entry,
        exit_price=h["exit_px"],
        stop_loss=fnum(meta.get("stop_loss")),
        take_profit=fnum(meta.get("take_profit")),
        algo=algo,
    )
    return record_close_for_open(
        ledger,
        open_trade=open_trade,
        exit_price=h["exit_px"],
        exit_reason=reason,
        now=h["close_ts"],
        algo=algo,
        pnl_override=h["net_pnl"],
        fee=abs(h["fee_total"]),
        extra_meta={
            "source": source,
            "pnl_basis": "okx_realized_net",
            "gross_pnl": h["gross_pnl"],
            "fee_total": h["fee_total"],
            "funding_fee": h["funding_fee"],
            "okx_open_avg_px": h["okx_open_avg_px"],
            "pos_id": h["pos_id"],
            "close_type": h["close_type"],
        },
    )


def _close_estimated(
    exchange: Any,
    ledger: Any,
    open_trade: TradeRecord,
    *,
    prev: dict[str, Any],
    algos: list[dict[str, Any]],
    ts: float,
) -> CloseOutcome | None:
    """Legacy estimate (algo actualPx → ticker → last mark) when no history row."""
    side = str(open_trade.direction or "long").lower()
    algo = _pick_algo_fill(
        algos, inst_id=open_trade.inst_id, side=side, since_ts=float(open_trade.timestamp or 0.0)
    )
    fallback_mark = prev.get("mark_price") or prev.get("avg_price")
    exit_px = _exit_price_from_sources(
        exchange=exchange,
        inst_id=open_trade.inst_id,
        algo=algo,
        fallback_mark=float(fallback_mark) if fallback_mark else None,
    )
    meta = open_trade.metadata or {}
    try:
        sl_f = float(meta["stop_loss"]) if meta.get("stop_loss") not in (None, "") else None
    except (TypeError, ValueError):
        sl_f = None
    try:
        tp_f = float(meta["take_profit"]) if meta.get("take_profit") not in (None, "") else None
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
    # No price at all → close at entry for bookkeeping (pnl 0).
    px = exit_px if exit_px > 0 else float(open_trade.price or 0.0)
    return record_close_for_open(
        ledger,
        open_trade=open_trade,
        exit_price=px,
        exit_reason=reason,
        now=ts,
        algo=algo,
        extra_meta={"source": "position_vanish", "pnl_basis": "estimate_gross"},
    )


def reconcile_closed_positions(
    exchange: Any,
    ledger: Any,
    *,
    now: float | None = None,
    positions: list[Any] | None = None,
) -> list[CloseOutcome]:
    """
    Detect closed ledger opens and record closes.

    1. Keys live last snapshot but gone now → close their tracked ids.
    2. Keys still live whose tracked ids changed → close the dropped ids.
    3. Sweep: live ``keel-llm`` opens (lookback window) that no live position
       covers → close only when OKX positions-history proves the close.

    Exit data prefers positions-history (true avg exit, net realized PnL,
    fees, close time); otherwise the legacy estimate. First snapshot only
    baselines paths 1–2. Pass ``positions`` to reuse a book fetch.
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
    tracked_now: set[int] = set()
    for row in current.values():
        for oid in row.get("open_trade_ids") or []:
            try:
                tracked_now.add(int(oid))
            except (TypeError, ValueError):
                continue

    # open_id -> (prev snapshot row | None for sweep-only)
    candidates: dict[int, dict[str, Any] | None] = {}
    for key, prev in (last or {}).items():
        prev_ids = prev.get("open_trade_ids") or []
        if not isinstance(prev_ids, list) or not prev_ids:
            # Nothing tracked while live — do not invent closes from orphans.
            continue
        for oid in prev_ids:
            try:
                oid_i = int(oid)
            except (TypeError, ValueError):
                continue
            if oid_i not in tracked_now:
                candidates[oid_i] = prev

    lookback = backfill_lookback_seconds()
    sweep: list[TradeRecord] = []
    if lookback > 0:
        seen_keys: set[str] = set()
        try:
            recent = ledger.get_trades(since=ts - lookback, action="open", limit=500)
        except Exception:
            recent = []
        for t in recent:
            key = f"{t.inst_id}|{str(t.direction).lower()}"
            if key in seen_keys or not _is_live_open(t):
                continue
            seen_keys.add(key)
            for u in unmatched_open_trades(ledger, t.inst_id, str(t.direction)):
                if u.id is None or int(u.id) in tracked_now or int(u.id) in candidates:
                    continue
                if not _is_live_open(u):
                    continue
                if float(u.timestamp or 0.0) < ts - lookback:
                    continue
                if ts - float(u.timestamp or 0.0) < _BACKFILL_GRACE_SECONDS:
                    continue
                sweep.append(u)

    if candidates or sweep:
        history = _fetch_positions_history(exchange)
        fill_ts_of = _FillTsResolver(exchange)
        algo_cache: dict[str, list[dict[str, Any]]] = {}

        def algos_for(inst: str) -> list[dict[str, Any]]:
            if inst not in algo_cache:
                algo_cache[inst] = _fetch_algo_history(exchange, inst)
            return algo_cache[inst]

        def by_history(t: TradeRecord, source: str) -> CloseOutcome | None | bool:
            if history is None:
                return False
            fts = fill_ts_of(t)
            if fts is None:
                return False
            row = match_history_close(
                history, inst_id=t.inst_id, side=str(t.direction), fill_ts=fts
            )
            if row is None:
                return False
            return _close_from_history(
                ledger, t, row, algos=algos_for(t.inst_id), source=source
            )

        open_by_id: dict[int, TradeRecord] = {}
        for oid_i, prev in candidates.items():
            inst = str((prev or {}).get("inst_id") or "")
            side = str((prev or {}).get("side") or "").lower()
            if not inst or side not in ("long", "short"):
                continue
            for t in unmatched_open_trades(ledger, inst, side):
                if t.id is not None:
                    open_by_id[int(t.id)] = t
        for oid_i, prev in candidates.items():
            t = open_by_id.get(oid_i)
            if t is None:
                continue  # already closed (idempotent) or missing
            res = by_history(t, "okx_positions_history")
            if res is False:
                res = _close_estimated(
                    exchange, ledger, t, prev=prev or {}, algos=algos_for(t.inst_id), ts=ts
                )
            if isinstance(res, CloseOutcome):
                outcomes.append(res)
        for t in sweep:
            res = by_history(t, "okx_positions_history_backfill")
            if isinstance(res, CloseOutcome):
                outcomes.append(res)

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
    "backfill_lookback_seconds",
    "infer_exit_reason",
    "match_history_close",
    "match_open_trade_ids",
    "realized_pnl",
    "reconcile_closed_positions",
    "record_close_for_open",
    "unmatched_open_trades",
]
