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

    def get_decision_stats(
        self,
        hours: float = 24.0,
        market_source: str | None = None,
    ) -> dict[str, Any]:
        """
        Aggregate decision observability stats for the last ``hours`` window.

        Optional ``market_source`` (okx_public|synthetic) filters decisions whose
        ``calculus_data.market_source`` matches (stamped in worker cycle).
        ``None`` / ``any`` / empty → no market_source filter.

        Uses SQL GROUP BY on decisions; cycle/risk counts come from events.
        """
        hours_f = max(0.0, float(hours))
        since = time.time() - hours_f * 3600.0
        conn = self._get_conn()

        ms_raw = (market_source or "any").strip().lower()
        ms_filter = ms_raw if ms_raw in ("okx_public", "synthetic") else None
        ms_clause = ""
        ms_params: list[Any] = []
        if ms_filter:
            ms_clause = (
                " AND calculus_data IS NOT NULL"
                " AND json_extract(calculus_data, '$.market_source') = ?"
            )
            ms_params = [ms_filter]

        by_action: dict[str, int] = {}
        for row in conn.execute(
            "SELECT action, COUNT(*) AS n FROM decisions "
            f"WHERE timestamp >= ?{ms_clause} GROUP BY action",
            (since, *ms_params),
        ):
            by_action[str(row["action"])] = int(row["n"])
        decision_count = sum(by_action.values())

        by_policy: dict[str, int] = {}
        for row in conn.execute(
            "SELECT COALESCE(policy_name, '') AS policy_name, COUNT(*) AS n "
            f"FROM decisions WHERE timestamp >= ?{ms_clause} "
            "GROUP BY COALESCE(policy_name, '')",
            (since, *ms_params),
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
            "market_source": ms_filter or "any",
        }

    def get_shadow_stats(
        self,
        hours: float = 24.0,
        *,
        include_markout: bool = True,
        markout_horizons: list[int] | tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        """
        Aggregate shadow_fill rehearsal events for the last ``hours`` window.

        Returns count, by_action (from event data.action), last_timestamp,
        plus probe_count / by_policy so Monitor can distinguish Q3 near-probe
        fills (policy=shadow_near_probe) from forced/manual shadow fills.

        Q3.5: also ``probe_skips`` / ``by_skip_reason`` from durable
        ``shadow_near_probe_skip`` events (hours filterable).

        When ``include_markout`` (default True), also attaches offline markout
        aggregates (avg/median bps, win_rate by horizon) via factor_snapshots.
        """
        from keel.ledger.shadow_markout import compute_shadow_markout

        hours_f = max(0.0, float(hours))
        conn = self._get_conn()
        if include_markout:
            return compute_shadow_markout(
                conn,
                hours=hours_f,
                horizons=markout_horizons,
            )

        since = time.time() - hours_f * 3600.0
        rows = conn.execute(
            "SELECT timestamp, inst_id, data FROM events "
            "WHERE timestamp >= ? AND event_type = ? "
            "ORDER BY timestamp DESC",
            (since, "shadow_fill"),
        ).fetchall()
        by_action: dict[str, int] = {}
        by_policy: dict[str, int] = {}
        probe_count = 0
        last_ts: float | None = None
        inst_fill: dict[str, int] = {}
        inst_probe: dict[str, int] = {}
        inst_by_action: dict[str, dict[str, int]] = {}
        for row in rows:
            ts = float(row["timestamp"])
            if last_ts is None or ts > last_ts:
                last_ts = ts
            inst_key = str(row["inst_id"] or "").strip() or "UNKNOWN"
            action = "UNKNOWN"
            policy = "manual"
            payload = None
            raw = row["data"]
            if raw:
                try:
                    payload = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    payload = None
            is_probe = False
            if isinstance(payload, dict):
                if payload.get("action"):
                    action = str(payload["action"])
                if payload.get("policy"):
                    policy = str(payload["policy"])
                elif payload.get("probe") is True:
                    policy = "shadow_near_probe"
                if policy == "shadow_near_probe" or payload.get("probe") is True:
                    probe_count += 1
                    is_probe = True
            by_action[action] = by_action.get(action, 0) + 1
            by_policy[policy] = by_policy.get(policy, 0) + 1
            inst_fill[inst_key] = inst_fill.get(inst_key, 0) + 1
            if is_probe:
                inst_probe[inst_key] = inst_probe.get(inst_key, 0) + 1
            iba = inst_by_action.setdefault(inst_key, {})
            iba[action] = iba.get(action, 0) + 1
        from keel.ledger.shadow_markout import aggregate_probe_skips

        probe_skips = aggregate_probe_skips(conn, since=since)
        skips_by_inst = dict(probe_skips.get("by_instrument") or {})
        by_instrument: dict[str, dict[str, Any]] = {}
        for ik in sorted(set(inst_fill) | set(skips_by_inst)):
            by_instrument[ik] = {
                "count": int(inst_fill.get(ik, 0)),
                "probe_count": int(inst_probe.get(ik, 0)),
                "by_action": dict(inst_by_action.get(ik) or {}),
                "by_skip_reason": dict(skips_by_inst.get(ik) or {}),
            }
        return {
            "hours": int(hours_f) if hours_f == int(hours_f) else hours_f,
            "count": len(rows),
            "by_action": by_action,
            "by_policy": by_policy,
            "probe_count": probe_count,
            "last_timestamp": last_ts,
            "probe_skips": probe_skips,
            "by_skip_reason": dict(probe_skips.get("by_skip_reason") or {}),
            "by_instrument": by_instrument,
        }

    def get_shadow_markout(
        self,
        hours: float = 24.0,
        horizons: list[int] | tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        """Q3.2 offline shadow fill markout aggregates (read-only)."""
        from keel.ledger.shadow_markout import compute_shadow_markout

        return compute_shadow_markout(
            self._get_conn(),
            hours=max(0.0, float(hours)),
            horizons=horizons,
        )

    def get_quality_stats(self, hours: float = 24.0) -> dict[str, Any]:
        """
        Compact observation quality scorecard for the last ``hours`` window.

        Composes decision aggregates, market_source breakdown, near_signal_rate
        (WAIT rows whose signal_diag.nearest is long/short), E1 full_gate_fires
        (BUY_LONG/SELL_SHORT with signal_diag.missing==[]), shadow_fill stats,
        cheap cycle timing, and optional ``by_instrument`` breakdown —
        read-only, no trading side effects.
        """
        hours_f = max(0.0, float(hours))
        since = time.time() - hours_f * 3600.0
        conn = self._get_conn()

        # Decision action / wait_rate (unfiltered market_source).
        by_action: dict[str, int] = {}
        for row in conn.execute(
            "SELECT action, COUNT(*) AS n FROM decisions "
            "WHERE timestamp >= ? GROUP BY action",
            (since,),
        ):
            by_action[str(row["action"])] = int(row["n"])
        decision_count = sum(by_action.values())
        wait_n = int(by_action.get("WAIT", 0))
        wait_rate = (wait_n / decision_count) if decision_count else 0.0

        # market_source breakdown from calculus_data (okx_public / synthetic / unknown).
        market_source: dict[str, int] = {
            "okx_public": 0,
            "synthetic": 0,
            "unknown": 0,
        }
        for row in conn.execute(
            "SELECT json_extract(calculus_data, '$.market_source') AS ms, "
            "COUNT(*) AS n FROM decisions WHERE timestamp >= ? GROUP BY ms",
            (since,),
        ):
            raw = row["ms"]
            key = str(raw).strip().lower() if raw is not None else ""
            if key == "okx_public":
                market_source["okx_public"] += int(row["n"])
            elif key == "synthetic":
                market_source["synthetic"] += int(row["n"])
            else:
                market_source["unknown"] += int(row["n"])

        # near_signal_rate among WAIT: signal_diag.nearest in {long, short}.
        near_row = conn.execute(
            "SELECT COUNT(*) AS n FROM decisions "
            "WHERE timestamp >= ? AND UPPER(action) = 'WAIT' "
            "AND LOWER(COALESCE("
            "json_extract(calculus_data, '$.signal_diag.nearest'), '')) "
            "IN ('long', 'short')",
            (since,),
        ).fetchone()
        near_n = int(near_row["n"]) if near_row else 0
        near_signal_rate = (near_n / wait_n) if wait_n else 0.0

        # E1: full-gate fires (BUY_LONG/SELL_SHORT + empty missing + rule policy).
        from keel.ledger.full_gate import aggregate_full_gate_fires

        full_gate = aggregate_full_gate_fires(conn, since=since)
        full_gate_by_inst = dict(full_gate.get("by_instrument") or {})

        # Per-instrument quality breakdown (diagnosis for multi-inst observe).
        by_instrument: dict[str, dict[str, Any]] = {}
        inst_action_rows = conn.execute(
            "SELECT inst_id, action, COUNT(*) AS n FROM decisions "
            "WHERE timestamp >= ? GROUP BY inst_id, action",
            (since,),
        ).fetchall()
        for row in inst_action_rows:
            ik = str(row["inst_id"] or "").strip() or "UNKNOWN"
            act = str(row["action"])
            entry = by_instrument.setdefault(
                ik,
                {
                    "decision_count": 0,
                    "wait_rate": 0.0,
                    "near_signal_rate": 0.0,
                    "full_gate_fires": 0,
                    "by_action": {},
                    "market_source": {
                        "okx_public": 0,
                        "synthetic": 0,
                        "unknown": 0,
                    },
                },
            )
            n = int(row["n"])
            entry["by_action"][act] = entry["by_action"].get(act, 0) + n
            entry["decision_count"] += n
        for row in conn.execute(
            "SELECT inst_id, "
            "json_extract(calculus_data, '$.market_source') AS ms, "
            "COUNT(*) AS n FROM decisions WHERE timestamp >= ? "
            "GROUP BY inst_id, ms",
            (since,),
        ):
            ik = str(row["inst_id"] or "").strip() or "UNKNOWN"
            entry = by_instrument.setdefault(
                ik,
                {
                    "decision_count": 0,
                    "wait_rate": 0.0,
                    "near_signal_rate": 0.0,
                    "full_gate_fires": 0,
                    "by_action": {},
                    "market_source": {
                        "okx_public": 0,
                        "synthetic": 0,
                        "unknown": 0,
                    },
                },
            )
            raw = row["ms"]
            key = str(raw).strip().lower() if raw is not None else ""
            if key == "okx_public":
                entry["market_source"]["okx_public"] += int(row["n"])
            elif key == "synthetic":
                entry["market_source"]["synthetic"] += int(row["n"])
            else:
                entry["market_source"]["unknown"] += int(row["n"])
        for row in conn.execute(
            "SELECT inst_id, COUNT(*) AS n FROM decisions "
            "WHERE timestamp >= ? AND UPPER(action) = 'WAIT' "
            "AND LOWER(COALESCE("
            "json_extract(calculus_data, '$.signal_diag.nearest'), '')) "
            "IN ('long', 'short') GROUP BY inst_id",
            (since,),
        ):
            ik = str(row["inst_id"] or "").strip() or "UNKNOWN"
            if ik not in by_instrument:
                continue
            wait_i = int(by_instrument[ik]["by_action"].get("WAIT", 0))
            near_i = int(row["n"])
            by_instrument[ik]["_near_n"] = near_i
            by_instrument[ik]["_wait_n"] = wait_i
        for ik, entry in by_instrument.items():
            dc = int(entry["decision_count"])
            wait_i = int(entry["by_action"].get("WAIT", 0))
            near_i = int(entry.pop("_near_n", 0))
            entry.pop("_wait_n", None)
            entry["wait_rate"] = (wait_i / dc) if dc else 0.0
            entry["near_signal_rate"] = (near_i / wait_i) if wait_i else 0.0
            entry["full_gate_fires"] = int(full_gate_by_inst.get(ik, 0))

        # Cycle count + avg duration (reuse same event source as get_decision_stats).
        cycle_rows = conn.execute(
            "SELECT data FROM events "
            "WHERE timestamp >= ? AND event_type = ? "
            "ORDER BY timestamp DESC",
            (since, self.CYCLE_SUMMARY_EVENT),
        ).fetchall()
        cycle_count = len(cycle_rows)
        if cycle_count == 0:
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

        # Counts only — markout is heavier and exposed on /stats/shadow.
        shadow = self.get_shadow_stats(hours=hours_f, include_markout=False)

        probe_n = int(shadow.get("probe_count", 0))
        fg_n = int(full_gate.get("count", 0))
        if fg_n > 0 and probe_n == 0:
            econ_evidence = "full_gate"
        elif fg_n > 0 and probe_n > 0:
            econ_evidence = "mixed"
        elif probe_n > 0:
            econ_evidence = "probe"
        else:
            econ_evidence = "none"

        return {
            "hours": int(hours_f) if hours_f == int(hours_f) else hours_f,
            "market_source": market_source,
            "decision_count": decision_count,
            "wait_rate": wait_rate,
            "by_action": by_action,
            "near_signal_rate": near_signal_rate,
            "full_gate_fires": {
                "count": fg_n,
                "by_action": dict(full_gate.get("by_action") or {}),
                "by_instrument": full_gate_by_inst,
            },
            "economic_evidence": econ_evidence,
            "shadow": {
                "count": int(shadow.get("count", 0)),
                "by_action": dict(shadow.get("by_action") or {}),
                "by_policy": dict(shadow.get("by_policy") or {}),
                "probe_count": probe_n,
                "last_timestamp": shadow.get("last_timestamp"),
            },
            "cycle_count": cycle_count,
            "avg_cycle_duration_ms": avg_ms,
            "by_instrument": by_instrument,
        }

    def get_nearest_signals(
        self,
        hours: float = 24.0,
        instrument_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Latest decision per instrument within ``hours``, with signal_diag summary.

        When ``instrument_ids`` is set, only those inst_ids are considered
        (configured/watch list). Returns rows + summary counts for radar UX.
        """
        hours_f = max(0.0, float(hours))
        since = time.time() - hours_f * 3600.0
        conn = self._get_conn()

        params: list[Any] = [since]
        inst_filter = ""
        if instrument_ids is not None:
            ids = [str(i) for i in instrument_ids if i]
            if not ids:
                return {
                    "hours": int(hours_f) if hours_f == int(hours_f) else hours_f,
                    "signals": [],
                    "summary": {
                        "waiting": 0,
                        "long_nearest": 0,
                        "short_nearest": 0,
                        "fired_long": 0,
                        "fired_short": 0,
                    },
                }
            placeholders = ",".join("?" for _ in ids)
            inst_filter = f" AND inst_id IN ({placeholders})"
            params.extend(ids)

        # Latest row per inst_id in window (tie-break: highest id).
        sql = f"""
            SELECT d.* FROM decisions d
            INNER JOIN (
                SELECT inst_id, MAX(timestamp) AS max_ts
                FROM decisions
                WHERE timestamp >= ?{inst_filter}
                GROUP BY inst_id
            ) latest
              ON d.inst_id = latest.inst_id AND d.timestamp = latest.max_ts
            ORDER BY d.inst_id ASC
        """
        rows = conn.execute(sql, params).fetchall()

        # Deduplicate if multiple rows share same max timestamp.
        by_inst: dict[str, sqlite3.Row] = {}
        for row in rows:
            iid = str(row["inst_id"])
            prev = by_inst.get(iid)
            if prev is None or int(row["id"] or 0) > int(prev["id"] or 0):
                by_inst[iid] = row

        metric_keys = (
            "rsi_14",
            "trend_15m",
            "trend_1h",
            "trend_4h",
            "volume_ratio",
            "ema_9",
            "ema_21",
            "macd_histogram",
        )
        signals: list[dict[str, Any]] = []
        waiting = long_nearest = short_nearest = fired_long = fired_short = 0

        for iid in sorted(by_inst.keys()):
            rec = self._row_to_decision(by_inst[iid])
            calc = rec.calculus_data if isinstance(rec.calculus_data, dict) else {}
            diag = calc.get("signal_diag") if isinstance(calc, dict) else None
            if not isinstance(diag, dict):
                diag = {}

            nearest_raw = diag.get("nearest")
            nearest = str(nearest_raw) if nearest_raw is not None and str(nearest_raw) else None
            missing_raw = diag.get("missing")
            missing: list[str] = []
            if isinstance(missing_raw, list):
                missing = [str(x) for x in missing_raw if x is not None and str(x)]

            item: dict[str, Any] = {
                "inst_id": rec.inst_id,
                "action": rec.action,
                "timestamp": float(rec.timestamp),
                "nearest": nearest,
                "missing": missing,
            }
            for k in metric_keys:
                val = diag.get(k)
                if val is None and isinstance(calc, dict):
                    val = calc.get(k)
                item[k] = val if val is not None else None

            action_u = (rec.action or "").upper()
            if action_u == "WAIT":
                waiting += 1
                if nearest == "long":
                    long_nearest += 1
                elif nearest == "short":
                    short_nearest += 1
            elif action_u == "BUY_LONG":
                fired_long += 1
            elif action_u == "SELL_SHORT":
                fired_short += 1

            signals.append(item)

        return {
            "hours": int(hours_f) if hours_f == int(hours_f) else hours_f,
            "signals": signals,
            "summary": {
                "waiting": waiting,
                "long_nearest": long_nearest,
                "short_nearest": short_nearest,
                "fired_long": fired_long,
                "fired_short": fired_short,
            },
        }


    def export_decisions(
        self,
        *,
        hours: float | None = None,
        since: float | None = None,
        market_source: str | None = None,
        inst_id: str | None = None,
        inst_ids: list[str] | tuple[str, ...] | None = None,
        limit: int = 5000,
        include_factors: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Export recent decisions as plain dicts for offline JSONL/JSON replay.

        Includes instrument, action, timestamp, market_source, signal_diag,
        calculus_data, policy_name, and (when available) a matched factor
        snapshot for RuleDecisionPolicy offline compare.

        ``inst_id`` (single) or ``inst_ids`` (IN-list) filter the cohort;
        when both are set, ``inst_ids`` takes precedence.
        """
        from keel.ledger.decision_export import (
            build_export_row,
            factor_dict_from_snapshot,
        )

        hours_f = float(hours) if hours is not None else None
        if since is None and hours_f is not None:
            since = time.time() - max(0.0, hours_f) * 3600.0

        ms_raw = (market_source or "any").strip().lower()
        ms_filter = ms_raw if ms_raw in ("okx_public", "synthetic") else None

        ids: list[str] = []
        if inst_ids is not None:
            ids = [str(i).strip() for i in inst_ids if str(i).strip()]
        elif inst_id is not None and str(inst_id).strip():
            ids = [str(inst_id).strip()]

        conn = self._get_conn()
        query = "SELECT * FROM decisions WHERE 1=1"
        params: list[Any] = []
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(float(since))
        if len(ids) == 1:
            query += " AND inst_id = ?"
            params.append(ids[0])
        elif len(ids) > 1:
            placeholders = ",".join("?" for _ in ids)
            query += f" AND inst_id IN ({placeholders})"
            params.extend(ids)
        if ms_filter:
            query += (
                " AND calculus_data IS NOT NULL"
                " AND json_extract(calculus_data, '$.market_source') = ?"
            )
            params.append(ms_filter)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = conn.execute(query, params).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            rec = self._row_to_decision(row)
            factors = None
            if include_factors:
                # Exact timestamp match (cycle writes decision + factor with same now).
                frow = conn.execute(
                    """
                    SELECT * FROM factor_snapshots
                    WHERE inst_id = ? AND abs(timestamp - ?) < 0.05
                    ORDER BY abs(timestamp - ?) ASC, id DESC
                    LIMIT 1
                    """,
                    (rec.inst_id, float(rec.timestamp), float(rec.timestamp)),
                ).fetchone()
                if frow is not None:
                    factors = factor_dict_from_snapshot(self._row_to_factor(frow))
            out.append(
                build_export_row(
                    decision_id=rec.id,
                    timestamp=float(rec.timestamp),
                    inst_id=rec.inst_id,
                    action=rec.action,
                    policy_name=rec.policy_name or "",
                    calculus_data=rec.calculus_data,
                    entry_price=rec.entry_price,
                    factors=factors,
                )
            )
        return out

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
