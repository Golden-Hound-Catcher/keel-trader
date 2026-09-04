"""
SQLite append-only ledger for Keel Trader.

Replaces JSON file IPC (data/*.json) with a proper database:
- Atomic writes
- No partial reads
- Proper querying
- Append-only for audit trail
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterator

from keel.domain.records import (
    BJ_TZ,
    DecisionRecord,
    FactorSnapshot,
    LedgerEvent,
    TradeRecord,
)

# Re-export for ``from keel.ledger.sqlite_ledger import TradeRecord`` callers
__all__ = ["KeelLedger", "TradeRecord", "DecisionRecord", "FactorSnapshot", "LedgerEvent"]


class KeelLedger:
    """
    Append-only SQLite ledger for trades and decisions.
    
    Thread-safe via connection-per-thread pattern.
    """

    CYCLE_SUMMARY_EVENT = "worker_cycle_summary"

    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            from keel.config import get_settings
            db_path = get_settings().ledger_path
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level="DEFERRED",
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Context manager for atomic transactions."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_schema(self) -> None:
        """Initialize database schema if needed."""
        with self._transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    inst_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    size REAL NOT NULL,
                    price REAL NOT NULL,
                    pnl REAL,
                    fee REAL DEFAULT 0,
                    strategy_tag TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    metadata TEXT,
                    created_at REAL DEFAULT (strftime('%s', 'now'))
                );
                
                CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_inst_id ON trades(inst_id);
                CREATE INDEX IF NOT EXISTS idx_trades_action ON trades(action);
                
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    inst_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    confidence REAL DEFAULT 0,
                    entry_price REAL,
                    take_profit REAL,
                    stop_loss REAL,
                    reason TEXT DEFAULT '',
                    calculus_data TEXT,
                    raw_response TEXT,
                    created_at REAL DEFAULT (strftime('%s', 'now'))
                );
                
                CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp);
                CREATE INDEX IF NOT EXISTS idx_decisions_inst_id ON decisions(inst_id);
                
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    inst_id TEXT,
                    data TEXT,
                    created_at REAL DEFAULT (strftime('%s', 'now'))
                );
                
                CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

                CREATE TABLE IF NOT EXISTS factor_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    inst_id TEXT NOT NULL,
                    price REAL DEFAULT 0,
                    rsi_14 REAL DEFAULT 0,
                    ema_9 REAL DEFAULT 0,
                    ema_21 REAL DEFAULT 0,
                    atr_14 REAL DEFAULT 0,
                    macd_histogram REAL DEFAULT 0,
                    trend_15m TEXT DEFAULT 'neutral',
                    volume_ratio REAL DEFAULT 1,
                    payload TEXT,
                    created_at REAL DEFAULT (strftime('%s', 'now'))
                );

                CREATE INDEX IF NOT EXISTS idx_factors_timestamp ON factor_snapshots(timestamp);
                CREATE INDEX IF NOT EXISTS idx_factors_inst_id ON factor_snapshots(inst_id);
            """)

    def record_trade(self, trade: TradeRecord) -> int:
        """Append a trade record. Returns the new ID."""
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trades (
                    timestamp, inst_id, action, direction, size, price,
                    pnl, fee, strategy_tag, reason, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.timestamp or time.time(),
                    trade.inst_id,
                    trade.action,
                    trade.direction,
                    trade.size,
                    trade.price,
                    trade.pnl,
                    trade.fee,
                    trade.strategy_tag,
                    trade.reason,
                    json.dumps(trade.metadata) if trade.metadata else None,
                ),
            )
            return cursor.lastrowid or 0

    def record_decision(self, decision: DecisionRecord) -> int:
        """Append a decision record. Returns the new ID."""
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO decisions (
                    timestamp, inst_id, action, confidence,
                    entry_price, take_profit, stop_loss,
                    reason, calculus_data, raw_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.timestamp or time.time(),
                    decision.inst_id,
                    decision.action,
                    decision.confidence,
                    decision.entry_price,
                    decision.take_profit,
                    decision.stop_loss,
                    decision.reason,
                    json.dumps(decision.calculus_data) if decision.calculus_data else None,
                    decision.raw_response,
                ),
            )
            return cursor.lastrowid or 0

    def record_event(
        self,
        event_type: str,
        inst_id: str | None = None,
        data: dict[str, Any] | None = None,
        *,
        timestamp: float | None = None,
    ) -> int:
        """Append a generic event. Returns the new ID."""
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO events (timestamp, event_type, inst_id, data) VALUES (?, ?, ?, ?)",
                (
                    time.time() if timestamp is None else float(timestamp),
                    event_type,
                    inst_id,
                    json.dumps(data) if data else None,
                ),
            )
            return cursor.lastrowid or 0

    def record_cycle_summary(self, summary: dict[str, Any]) -> int:
        """
        Persist a structured worker cycle summary for the monitor/status API.

        Event type: ``worker_cycle_summary``. Expected keys include timestamp,
        mode, adapter, policy, instruments, decision_counts, risk_denies,
        risk_deny_reasons (capped), error_count, errors (capped).
        """
        payload = dict(summary)
        ts = float(payload.get("timestamp") or time.time())
        payload["timestamp"] = ts
        return self.record_event(
            self.CYCLE_SUMMARY_EVENT,
            data=payload,
            timestamp=ts,
        )

    def get_last_cycle_summary(self) -> dict[str, Any] | None:
        """Return the most recent worker_cycle_summary payload, or None."""
        events = self.get_events(event_type=self.CYCLE_SUMMARY_EVENT, limit=1)
        if not events:
            return None
        ev = events[0]
        data = dict(ev.data or {})
        data.setdefault("timestamp", ev.timestamp)
        return data

    def get_trades(
        self,
        since: float | None = None,
        inst_id: str | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[TradeRecord]:
        """Query trades with optional filters."""
        conn = self._get_conn()
        query = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []

        if since is not None:
            query += " AND timestamp >= ?"
            params.append(since)
        if inst_id is not None:
            query += " AND inst_id = ?"
            params.append(inst_id)
        if action is not None:
            query += " AND action = ?"
            params.append(action)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_trade(row) for row in rows]

    def get_decisions(
        self,
        since: float | None = None,
        inst_id: str | None = None,
        limit: int = 100,
    ) -> list[DecisionRecord]:
        """Query decisions with optional filters."""
        conn = self._get_conn()
        query = "SELECT * FROM decisions WHERE 1=1"
        params: list[Any] = []

        if since is not None:
            query += " AND timestamp >= ?"
            params.append(since)
        if inst_id is not None:
            query += " AND inst_id = ?"
            params.append(inst_id)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_decision(row) for row in rows]

    def get_daily_pnl(self, date: str | None = None) -> float:
        """Get total realized PnL for a given date (Beijing time)."""
        if date is None:
            date = datetime.now(BJ_TZ).strftime("%Y-%m-%d")

        start_dt = datetime.strptime(f"{date} 00:00:00", "%Y-%m-%d %H:%M:%S")
        start_dt = start_dt.replace(tzinfo=BJ_TZ)
        end_dt = start_dt + timedelta(days=1)

        conn = self._get_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE timestamp >= ? AND timestamp < ? AND pnl IS NOT NULL",
            (start_dt.timestamp(), end_dt.timestamp()),
        ).fetchone()
        return float(row["total"]) if row else 0.0

    def get_latest_decision(self, inst_id: str, max_age_seconds: int = 300) -> DecisionRecord | None:
        """Get the most recent decision for an instrument if it's still fresh."""
        cutoff = time.time() - max_age_seconds
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM decisions WHERE inst_id = ? AND timestamp >= ? ORDER BY timestamp DESC LIMIT 1",
            (inst_id, cutoff),
        ).fetchone()
        return self._row_to_decision(row) if row else None

    def _row_to_trade(self, row: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            id=row["id"],
            timestamp=row["timestamp"],
            inst_id=row["inst_id"],
            action=row["action"],
            direction=row["direction"],
            size=row["size"],
            price=row["price"],
            pnl=row["pnl"],
            fee=row["fee"],
            strategy_tag=row["strategy_tag"],
            reason=row["reason"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else None,
        )

    def _row_to_decision(self, row: sqlite3.Row) -> DecisionRecord:
        return DecisionRecord(
            id=row["id"],
            timestamp=row["timestamp"],
            inst_id=row["inst_id"],
            action=row["action"],
            confidence=row["confidence"],
            entry_price=row["entry_price"],
            take_profit=row["take_profit"],
            stop_loss=row["stop_loss"],
            reason=row["reason"],
            calculus_data=json.loads(row["calculus_data"]) if row["calculus_data"] else None,
            raw_response=row["raw_response"],
        )


    def record_factor_snapshot(self, snap: FactorSnapshot) -> int:
        """Append a factor snapshot for API/history reads."""
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO factor_snapshots (
                    timestamp, inst_id, price, rsi_14, ema_9, ema_21,
                    atr_14, macd_histogram, trend_15m, volume_ratio, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap.timestamp or time.time(),
                    snap.inst_id,
                    snap.price,
                    snap.rsi_14,
                    snap.ema_9,
                    snap.ema_21,
                    snap.atr_14,
                    snap.macd_histogram,
                    snap.trend_15m,
                    snap.volume_ratio,
                    json.dumps(snap.payload) if snap.payload else None,
                ),
            )
            return cursor.lastrowid or 0

    def get_latest_factor_snapshot(
        self, inst_id: str, max_age_seconds: int = 3600
    ) -> FactorSnapshot | None:
        """Get the most recent factor snapshot for an instrument if still fresh."""
        cutoff = time.time() - max_age_seconds
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT * FROM factor_snapshots
            WHERE inst_id = ? AND timestamp >= ?
            ORDER BY timestamp DESC LIMIT 1
            """,
            (inst_id, cutoff),
        ).fetchone()
        return self._row_to_factor(row) if row else None

    def get_factor_snapshots(
        self,
        inst_id: str | None = None,
        limit: int = 50,
    ) -> list[FactorSnapshot]:
        """Query recent factor snapshots."""
        conn = self._get_conn()
        query = "SELECT * FROM factor_snapshots WHERE 1=1"
        params: list[Any] = []
        if inst_id is not None:
            query += " AND inst_id = ?"
            params.append(inst_id)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [self._row_to_factor(row) for row in rows]

    def get_events(
        self,
        event_type: str | None = None,
        inst_id: str | None = None,
        limit: int = 100,
    ) -> list[LedgerEvent]:
        """Query ledger events (risk blocks, cycle completes, resting orders)."""
        conn = self._get_conn()
        query = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type)
        if inst_id is not None:
            query += " AND inst_id = ?"
            params.append(inst_id)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [
            LedgerEvent(
                id=row["id"],
                timestamp=row["timestamp"],
                event_type=row["event_type"],
                inst_id=row["inst_id"],
                data=json.loads(row["data"]) if row["data"] else None,
            )
            for row in rows
        ]

    def _row_to_factor(self, row: sqlite3.Row) -> FactorSnapshot:
        return FactorSnapshot(
            id=row["id"],
            timestamp=row["timestamp"],
            inst_id=row["inst_id"],
            price=row["price"],
            rsi_14=row["rsi_14"],
            ema_9=row["ema_9"],
            ema_21=row["ema_21"],
            atr_14=row["atr_14"],
            macd_histogram=row["macd_histogram"],
            trend_15m=row["trend_15m"],
            volume_ratio=row["volume_ratio"],
            payload=json.loads(row["payload"]) if row["payload"] else None,
        )

    def close(self) -> None:
        """Close the thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
