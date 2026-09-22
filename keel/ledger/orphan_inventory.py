"""
Orphan open inventory + daily-loss gate honesty (read-only).

An orphan is an ``open`` / ``scale_in`` trade with no linked ``close`` row
(``metadata.open_trade_id``). Historical orphans are never auto-closed here —
inventory only. DailyLossGate uses realized ``trades.pnl``; without closes it
is always 0, so status surfaces ``daily_loss_gate_effective=false``.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

BJ_TZ = timezone(timedelta(hours=8))

# strategy_tag prefixes used in production dual-log.
_LIVE_TAGS = frozenset({"keel-llm", "keel", ""})
_SHADOW_PREFIXES = ("keel-shadow",)


@dataclass(frozen=True)
class OrphanRow:
    trade_id: int
    inst_id: str
    direction: str
    size: float
    price: float
    strategy_tag: str
    timestamp: float
    age_seconds: float
    cohort: str  # live | shadow | other


@dataclass(frozen=True)
class OrphanInventory:
    total: int
    live: int
    shadow: int
    other: int
    by_inst: dict[str, int] = field(default_factory=dict)
    by_strategy_tag: dict[str, int] = field(default_factory=dict)
    oldest_age_seconds: float | None = None
    newest_age_seconds: float | None = None
    # Sample of oldest orphans (capped) for operator tooling — not on /status.
    sample: tuple[OrphanRow, ...] = ()

    def to_status_dict(self) -> dict[str, Any]:
        """Compact payload for GET /api/v1/status."""
        return {
            "total": self.total,
            "live": self.live,
            "shadow": self.shadow,
            "other": self.other,
            "by_inst": dict(self.by_inst),
            "by_strategy_tag": dict(self.by_strategy_tag),
            "oldest_age_seconds": self.oldest_age_seconds,
            "newest_age_seconds": self.newest_age_seconds,
        }

    def to_detail_dict(self, *, include_sample: bool = True) -> dict[str, Any]:
        out = self.to_status_dict()
        if include_sample:
            out["sample"] = [asdict(r) for r in self.sample]
        return out


@dataclass(frozen=True)
class DailyLossGateHonesty:
    effective: bool
    reason: str | None
    realized_close_count: int
    realized_close_count_today: int
    lifetime_close_count: int

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "daily_loss_gate_effective": self.effective,
            "daily_loss_gate_reason": self.reason,
            "realized_close_count": self.realized_close_count,
            "realized_close_count_today": self.realized_close_count_today,
            "lifetime_close_count": self.lifetime_close_count,
        }


def _cohort(strategy_tag: str | None) -> str:
    tag = str(strategy_tag or "").strip()
    if any(tag.startswith(p) for p in _SHADOW_PREFIXES):
        return "shadow"
    if tag in _LIVE_TAGS or tag.startswith("keel-llm"):
        return "live"
    return "other"


def _closed_open_ids_from_conn(conn: Any) -> set[int]:
    """All open_trade_id values linked from close rows (JSON metadata)."""
    out: set[int] = set()
    rows = conn.execute(
        "SELECT metadata FROM trades WHERE action = 'close' AND metadata IS NOT NULL"
    ).fetchall()
    for row in rows:
        raw = row["metadata"] if hasattr(row, "keys") else row[0]
        if not raw:
            continue
        try:
            meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        oid = meta.get("open_trade_id")
        if oid is None:
            continue
        try:
            out.add(int(oid))
        except (TypeError, ValueError):
            continue
    return out


def inventory_orphans(
    ledger: Any,
    *,
    now: float | None = None,
    sample_limit: int = 20,
) -> OrphanInventory:
    """
    Count open/scale_in rows without a linked close.

    Uses SQL + metadata scan (read-only). Does not invent closes.
    """
    ts = time.time() if now is None else float(now)
    conn = ledger._get_conn()
    closed = _closed_open_ids_from_conn(conn)
    rows = conn.execute(
        "SELECT id, inst_id, direction, size, price, strategy_tag, timestamp "
        "FROM trades WHERE action IN ('open', 'scale_in') ORDER BY timestamp ASC"
    ).fetchall()

    orphans: list[OrphanRow] = []
    by_inst: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    live = shadow = other = 0

    for row in rows:
        tid = int(row["id"])
        if tid in closed:
            continue
        tag = str(row["strategy_tag"] or "")
        cohort = _cohort(tag)
        age = max(0.0, ts - float(row["timestamp"] or 0.0))
        orphans.append(
            OrphanRow(
                trade_id=tid,
                inst_id=str(row["inst_id"] or ""),
                direction=str(row["direction"] or ""),
                size=float(row["size"] or 0.0),
                price=float(row["price"] or 0.0),
                strategy_tag=tag,
                timestamp=float(row["timestamp"] or 0.0),
                age_seconds=age,
                cohort=cohort,
            )
        )
        inst = str(row["inst_id"] or "") or "?"
        by_inst[inst] = by_inst.get(inst, 0) + 1
        by_tag[tag or "(empty)"] = by_tag.get(tag or "(empty)", 0) + 1
        if cohort == "live":
            live += 1
        elif cohort == "shadow":
            shadow += 1
        else:
            other += 1

    oldest = orphans[0].age_seconds if orphans else None
    newest = orphans[-1].age_seconds if orphans else None
    sample = tuple(orphans[: max(0, int(sample_limit))])
    return OrphanInventory(
        total=len(orphans),
        live=live,
        shadow=shadow,
        other=other,
        by_inst=dict(sorted(by_inst.items(), key=lambda kv: (-kv[1], kv[0]))),
        by_strategy_tag=dict(sorted(by_tag.items(), key=lambda kv: (-kv[1], kv[0]))),
        oldest_age_seconds=oldest,
        newest_age_seconds=newest,
        sample=sample,
    )


def count_realized_closes(
    ledger: Any,
    *,
    date: str | None = None,
) -> tuple[int, int, int]:
    """
    Return ``(lifetime_closes, lifetime_with_pnl, today_with_pnl)``.

    ``today`` uses Beijing calendar day (same as ``get_daily_pnl``).
    """
    conn = ledger._get_conn()
    lifetime = conn.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE action = 'close'"
    ).fetchone()
    lifetime_n = int(lifetime["n"] if lifetime else 0)

    with_pnl = conn.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE action = 'close' AND pnl IS NOT NULL"
    ).fetchone()
    with_pnl_n = int(with_pnl["n"] if with_pnl else 0)

    if date is None:
        date = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    start_dt = datetime.strptime(f"{date} 00:00:00", "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=BJ_TZ
    )
    end_dt = start_dt + timedelta(days=1)
    today = conn.execute(
        "SELECT COUNT(*) AS n FROM trades "
        "WHERE action = 'close' AND pnl IS NOT NULL "
        "AND timestamp >= ? AND timestamp < ?",
        (start_dt.timestamp(), end_dt.timestamp()),
    ).fetchone()
    today_n = int(today["n"] if today else 0)
    return lifetime_n, with_pnl_n, today_n


def daily_loss_gate_honesty(ledger: Any, *, date: str | None = None) -> DailyLossGateHonesty:
    """
    Honesty flag for DailyLossGate.

    Gate remains in the risk chain, but without any realized close pnl it
    cannot trip (``get_daily_pnl`` stays 0). Surface that clearly.
    """
    lifetime_n, with_pnl_n, today_n = count_realized_closes(ledger, date=date)
    if with_pnl_n <= 0:
        return DailyLossGateHonesty(
            effective=False,
            reason="no_realized_closes",
            realized_close_count=with_pnl_n,
            realized_close_count_today=today_n,
            lifetime_close_count=lifetime_n,
        )
    return DailyLossGateHonesty(
        effective=True,
        reason=None,
        realized_close_count=with_pnl_n,
        realized_close_count_today=today_n,
        lifetime_close_count=lifetime_n,
    )


__all__ = [
    "OrphanInventory",
    "OrphanRow",
    "DailyLossGateHonesty",
    "inventory_orphans",
    "daily_loss_gate_honesty",
    "count_realized_closes",
]
