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
                    policy_name TEXT DEFAULT '',
                    prompt_modules TEXT,
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
            # Backward-compatible ALTERs for DBs created before P2 audit columns.
            self._ensure_column(conn, "decisions", "policy_name", "TEXT DEFAULT ''")
            self._ensure_column(conn, "decisions", "prompt_modules", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_decisions_policy ON decisions(policy_name)"
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection, table: str, column: str, typedef: str
    ) -> None:
        """ADD COLUMN if missing (safe for existing SQLite ledgers)."""
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(r[1]) for r in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")

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
        modules = decision.prompt_modules
        modules_json = json.dumps(list(modules)) if modules is not None else None
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO decisions (
                    timestamp, inst_id, action, confidence,
                    entry_price, take_profit, stop_loss,
                    reason, calculus_data, raw_response,
                    policy_name, prompt_modules
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    decision.policy_name or "",
                    modules_json,
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

    def get_decision_stats(self, hours: float = 24.0) -> dict[str, Any]:
        """
        Aggregate decision observability stats for the last ``hours`` window.

        Uses SQL GROUP BY on decisions; cycle/risk counts come from events.
        """
        hours_f = max(0.0, float(hours))
        since = time.time() - hours_f * 3600.0
        conn = self._get_conn()

        by_action: dict[str, int] = {}
        for row in conn.execute(
            "SELECT action, COUNT(*) AS n FROM decisions "
            "WHERE timestamp >= ? GROUP BY action",
            (since,),
        ):
            by_action[str(row["action"])] = int(row["n"])
        decision_count = sum(by_action.values())

        by_policy: dict[str, int] = {}
        for row in conn.execute(
            "SELECT COALESCE(policy_name, '') AS policy_name, COUNT(*) AS n "
            "FROM decisions WHERE timestamp >= ? GROUP BY COALESCE(policy_name, '')",
            (since,),
        ):
            by_policy[str(row["policy_name"] or "")] = int(row["n"])

        wait_n = int(by_action.get("WAIT", 0))
        wait_rate = (wait_n / decision_count) if decision_count else 0.0

        risk_row = conn.execute(
            "SELECT COUNT(*) AS n FROM events "
            "WHERE timestamp >= ? AND event_type = ?",
            (since, "risk_gate_blocked"),
        ).fetchone()
        risk_deny_events = int(risk_row["n"]) if risk_row else 0

        cycle_rows = conn.execute(
            "SELECT data FROM events "
            "WHERE timestamp >= ? AND event_type = ? "
            "ORDER BY timestamp DESC",
            (since, self.CYCLE_SUMMARY_EVENT),
        ).fetchall()
        cycle_count = len(cycle_rows)
        if cycle_count == 0:
            # Fallback for older ledgers that only wrote trader_cycle_complete.
            alt = conn.execute(
                "SELECT COUNT(*) AS n FROM events "
                "WHERE timestamp >= ? AND event_type = ?",
                (since, "trader_cycle_complete"),
            ).fetchone()
            cycle_count = int(alt["n"]) if alt else 0

        durations: list[float] = []
        for row in cycle_rows:
            raw = row["data"]
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            ms = payload.get("duration_ms")
            if isinstance(ms, (int, float)):
                durations.append(float(ms))
        avg_ms: float | None = (
            (sum(durations) / len(durations)) if durations else None
        )

        return {
            "hours": int(hours_f) if hours_f == int(hours_f) else hours_f,
            "decision_count": decision_count,
            "by_action": by_action,
            "by_policy": by_policy,
            "wait_rate": wait_rate,
            "risk_deny_events": risk_deny_events,
            "cycle_count": cycle_count,
            "avg_cycle_duration_ms": avg_ms,
        }

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
        keys = set(row.keys())
        modules_raw = row["prompt_modules"] if "prompt_modules" in keys else None
        modules: list[str] | None = None
        if modules_raw:
            parsed = json.loads(modules_raw)
            if isinstance(parsed, list):
                modules = [str(x) for x in parsed]
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
            policy_name=(row["policy_name"] if "policy_name" in keys else "") or "",
            prompt_modules=modules,
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
