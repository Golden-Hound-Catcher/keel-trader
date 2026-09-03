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
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterator, Literal

BJ_TZ = timezone(timedelta(hours=8))


@dataclass
class TradeRecord:
    """Immutable trade record for the ledger."""
    id: int | None = None
    timestamp: float = 0.0
    inst_id: str = ""
    action: Literal["open", "close", "scale_in"] = "open"
    direction: Literal["long", "short"] = "long"
    size: float = 0.0
    price: float = 0.0
    pnl: float | None = None
    fee: float = 0.0
    strategy_tag: str = ""
    reason: str = ""
    metadata: dict[str, Any] | None = None

    @property
    def time_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp, tz=BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class DecisionRecord:
    """AI decision record for audit trail."""
    id: int | None = None
    timestamp: float = 0.0
    inst_id: str = ""
    action: str = "WAIT"
    confidence: float = 0.0
    entry_price: float | None = None
    take_profit: float | None = None
    stop_loss: float | None = None
    reason: str = ""
    calculus_data: dict[str, Any] | None = None
    raw_response: str | None = None


class KeelLedger:
    """
    Append-only SQLite ledger for trades and decisions.
    
    Thread-safe via connection-per-thread pattern.
    """

    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            from keel.config import get_settings
            db_path = get_settings().data_dir / "keel_ledger.db"
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

    def record_event(self, event_type: str, inst_id: str | None = None, data: dict[str, Any] | None = None) -> int:
        """Append a generic event. Returns the new ID."""
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO events (timestamp, event_type, inst_id, data) VALUES (?, ?, ?, ?)",
                (time.time(), event_type, inst_id, json.dumps(data) if data else None),
            )
            return cursor.lastrowid or 0

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

    def close(self) -> None:
        """Close the thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
