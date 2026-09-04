"""Unit tests for typed API response models and OpenAPI honesty."""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.api.schemas import (
    BalanceResponse,
    CycleError,
    DailyPnlResponse,
    DecisionItem,
    DecisionsResponse,
    EventItem,
    FactorsResponse,
    HealthResponse,
    ReadyResponse,
    MacdBlock,
    PositionsResponse,
    LastCycleSummary,
    RiskDenyReason,
    ConfigResponse,
    StatusResponse,
    TradesResponse,
)
from keel.domain import Decision, DecisionRecord, FactorSnapshot, LedgerEvent, TradeRecord


class TestApiSchemas(unittest.TestCase):
    def test_last_cycle_summary_model(self):
        m = LastCycleSummary(
            timestamp=1.0,
            mode="paper",
            adapter="paper",
            policy="rule",
            instruments=1,
            decision_counts={"WAIT": 1},
            risk_denies=1,
            risk_deny_reasons=[RiskDenyReason(gate="kill_switch", reason="armed")],
            error_count=1,
            errors=[CycleError(inst_id="BTC-USDT-SWAP", error="timeout")],
            duration_ms=42,
        )
        self.assertEqual(m.mode, "paper")
        self.assertEqual(m.duration_ms, 42)
        self.assertEqual(m.risk_denies, 1)
        self.assertEqual(m.risk_deny_reasons[0].gate, "kill_switch")
        self.assertEqual(m.error_count, 1)
        self.assertEqual(m.errors[0].inst_id, "BTC-USDT-SWAP")
        self.assertEqual(m.errors[0].error, "timeout")
        status = StatusResponse(
            version="0.1.0",
            mode="read_only_control_plane",
            uptime_seconds=1,
            environment="demo",
            credentials={"okx": False, "llm": False},
            ledger_db="/tmp/x.db",
            last_cycle=m,
        )
        self.assertEqual(status.last_cycle.instruments, 1)

    def test_last_cycle_errors_coerce_from_ledger_json(self):
        """Status router validates ledger dicts; extra keys ignored."""
        raw = {
            "timestamp": 1.0,
            "mode": "paper",
            "adapter": "paper",
            "policy": "rule",
            "instruments": 1,
            "decision_counts": {"WAIT": 1},
            "risk_denies": 0,
            "risk_deny_reasons": [],
            "error_count": 2,
            "errors": [
                {"inst_id": "ETH-USDT-SWAP", "error": "boom", "legacy_extra": True},
                {"error": "no-inst"},
            ],
            "duration_ms": 5,
            "unknown_top_level": "ignored",
        }
        m = LastCycleSummary.model_validate(raw)
        self.assertEqual(m.error_count, 2)
        self.assertEqual(len(m.errors), 2)
        self.assertIsInstance(m.errors[0], CycleError)
        self.assertEqual(m.errors[0].inst_id, "ETH-USDT-SWAP")
        self.assertEqual(m.errors[0].error, "boom")
        self.assertIsNone(m.errors[1].inst_id)
        self.assertEqual(m.errors[1].error, "no-inst")
        dumped = m.model_dump()
        self.assertNotIn("legacy_extra", dumped["errors"][0])
        self.assertNotIn("unknown_top_level", dumped)

    def test_health_response_model(self):
        m = HealthResponse(
            status="ok",
            service="keel-trader",
            version="0.1.0",
            timestamp=1,
            environment="demo",
        )
        self.assertEqual(m.model_dump()["status"], "ok")

    def test_decision_item_from_record(self):
        rec = DecisionRecord(
            id=1,
            timestamp=100.0,
            inst_id="BTC-USDT-SWAP",
            action="WAIT",
            confidence=10.0,
        )
        item = DecisionItem(
            id=rec.id,
            timestamp=rec.timestamp,
            inst_id=rec.inst_id,
            action=rec.action,
            confidence=rec.confidence,
        )
        self.assertEqual(item.inst_id, "BTC-USDT-SWAP")

    def test_domain_decision_shared(self):
        d = Decision(inst_id="ETH-USDT-SWAP", action="WAIT")
        self.assertTrue(d.valid)

    def test_ledger_event_shape(self):
        e = LedgerEvent(id=1, timestamp=1.0, event_type="paper_cycle_complete", data={"ok": True})
        item = EventItem(
            id=e.id,
            timestamp=e.timestamp,
            event_type=e.event_type,
            inst_id=e.inst_id,
            data=e.data,
        )
        self.assertEqual(item.event_type, "paper_cycle_complete")

    def test_factors_response_requires_macd(self):
        fr = FactorsResponse(
            inst_id="BTC-USDT-SWAP",
            source="ledger",
            price=100.0,
            ema_9=99.0,
            ema_21=98.0,
            rsi_14=55.0,
            atr_14=1.0,
            macd=MacdBlock(line=0.1, signal=0.05, histogram=0.05),
        )
        self.assertEqual(fr.source, "ledger")

    def test_openapi_includes_typed_paths(self):
        client = TestClient(create_app())
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        self.assertIn("/health", paths)
        self.assertIn("/api/v1/status", paths)
        self.assertIn("/api/v1/positions", paths)
        self.assertIn("/api/v1/balance", paths)
        self.assertIn("/api/v1/decisions", paths)
        self.assertIn("/api/v1/trades", paths)
        self.assertIn("/api/v1/events", paths)
        self.assertIn("/api/v1/factors/{inst_id}", paths)
        # response_model registers component schemas
        comps = schema.get("components", {}).get("schemas", {})
        self.assertIn("HealthResponse", comps)
        self.assertIn("StatusResponse", comps)
        self.assertIn("LastCycleSummary", comps)
        last_cycle_props = comps["LastCycleSummary"]["properties"]
        self.assertIn("duration_ms", last_cycle_props)
        self.assertIn("risk_deny_reasons", last_cycle_props)
        self.assertIn("error_count", last_cycle_props)
        self.assertIn("errors", last_cycle_props)
        self.assertIn("RiskDenyReason", comps)
        self.assertIn("CycleError", comps)
        self.assertIn("DecisionsResponse", comps)
        self.assertIn("FactorsResponse", comps)
        status_props = comps["StatusResponse"]["properties"]
        self.assertIn("last_cycle", status_props)
        self.assertIn("kill_switch", status_props)
        self.assertIn("seconds_since_last_cycle", status_props)
        self.assertIn("worker_stale", status_props)
        self.assertIn("ConfigResponse", comps)
        config_props = comps["ConfigResponse"]["properties"]
        self.assertIn("kill_switch", config_props)
        self.assertIn("instruments", config_props)
        self.assertIn("notify_configured", config_props)
        self.assertIn("exchange_mode", config_props)
        self.assertIn("cycle_interval_seconds", config_props)
        self.assertIn("scheduler_jobs", config_props)
        self.assertIn("legacy_scheduler_jobs", config_props)
        self.assertIn("decision_policy", config_props)
        self.assertIn("decision_policy", status_props)
        self.assertIn("DailyPnlResponse", comps)
        self.assertIn("/api/v1/pnl/daily", paths)
        self.assertIn("/ready", paths)
        self.assertIn("ReadyResponse", comps)
        ready_props = comps["ReadyResponse"]["properties"]
        self.assertIn("seconds_since_last_cycle", ready_props)
        self.assertIn("worker_stale", ready_props)

    def test_kill_switch_on_status_and_config(self):
        status = StatusResponse(
            version="0.1.0",
            mode="read_only_control_plane",
            uptime_seconds=1,
            environment="demo",
            credentials={"okx": False, "llm": False},
            ledger_db="/tmp/x.db",
            kill_switch=True,
            decision_policy="rule",
            seconds_since_last_cycle=12,
        )
        self.assertTrue(status.kill_switch)
        self.assertEqual(status.decision_policy, "rule")
        self.assertEqual(status.seconds_since_last_cycle, 12)
        cfg = ConfigResponse(
            environment="demo",
            max_positions=6,
            max_daily_loss=150.0,
            max_asset_margin=600.0,
            llm_model="gpt-4o",
            kill_switch=False,
            decision_policy="stub",
            instruments=["BTC-USDT-SWAP"],
            notify_configured=True,
            exchange_mode="paper",
            cycle_interval_seconds=900,
            scheduler_jobs=["trader"],
            legacy_scheduler_jobs=False,
        )
        self.assertFalse(cfg.kill_switch)
        self.assertEqual(cfg.decision_policy, "stub")
        self.assertEqual(cfg.instruments, ["BTC-USDT-SWAP"])
        self.assertTrue(cfg.notify_configured)
        self.assertEqual(cfg.exchange_mode, "paper")
        self.assertEqual(cfg.cycle_interval_seconds, 900)
        self.assertEqual(cfg.scheduler_jobs, ["trader"])
        self.assertFalse(cfg.legacy_scheduler_jobs)

    def test_daily_pnl_response_model(self):
        m = DailyPnlResponse(date="2026-09-04", realized_pnl=12.5)
        self.assertEqual(m.source, "ledger")
        self.assertEqual(m.realized_pnl, 12.5)
        self.assertIsNone(
            StatusResponse(
                version="0.1.0",
                mode="read_only_control_plane",
                uptime_seconds=1,
                environment="demo",
                credentials={"okx": False, "llm": False},
                ledger_db="/tmp/x.db",
            ).seconds_since_last_cycle
        )


class TestReadyResponseModel(unittest.TestCase):
    def test_ready_response_defaults_and_fields(self):
        m = ReadyResponse(ready=True, okx_configured=False, llm_configured=True)
        self.assertTrue(m.ready)
        self.assertIsNone(m.seconds_since_last_cycle)
        self.assertFalse(m.worker_stale)
        stale = ReadyResponse(
            ready=False,
            okx_configured=True,
            llm_configured=False,
            seconds_since_last_cycle=950,
            worker_stale=True,
        )
        self.assertFalse(stale.ready)
        self.assertEqual(stale.seconds_since_last_cycle, 950)
        self.assertTrue(stale.worker_stale)


class TestDomainRecordsExports(unittest.TestCase):
    def test_trade_and_factor_snapshot(self):
        t = TradeRecord(inst_id="BTC-USDT-SWAP", action="open", direction="long", size=1, price=100)
        self.assertEqual(t.action, "open")
        s = FactorSnapshot(inst_id="BTC-USDT-SWAP", price=100.0, rsi_14=50.0)
        self.assertEqual(s.trend_15m, "neutral")


class TestSecondsSinceLastCycleHelper(unittest.TestCase):
    def test_parse_unix_and_iso(self):
        from keel.api.cycle_time import parse_cycle_timestamp, seconds_since_last_cycle
        import time

        self.assertAlmostEqual(parse_cycle_timestamp(1_700_000_000.0), 1_700_000_000.0)
        self.assertIsNotNone(parse_cycle_timestamp("2026-09-04T03:00:00+00:00"))
        self.assertIsNone(parse_cycle_timestamp("not-a-timestamp"))
        self.assertIsNone(parse_cycle_timestamp(None))
        self.assertIsNone(seconds_since_last_cycle(None))
        self.assertIsNone(seconds_since_last_cycle({"timestamp": "bogus"}))
        now = time.time()
        lag = seconds_since_last_cycle({"timestamp": now - 30})
        self.assertIsInstance(lag, int)
        self.assertGreaterEqual(lag, 29)
        self.assertLessEqual(lag, 35)

    def test_worker_stale_threshold_formula(self):
        from keel.api.cycle_time import is_worker_stale, worker_stale_threshold_seconds

        # Default 900 → max(1800, 1200) = 1800
        self.assertEqual(worker_stale_threshold_seconds(900), 1800)
        # Short interval → interval+300 wins
        self.assertEqual(worker_stale_threshold_seconds(60), 360)
        # Long interval → 2× wins
        self.assertEqual(worker_stale_threshold_seconds(600), 1200)
        self.assertFalse(is_worker_stale(None, 900))
        self.assertFalse(is_worker_stale(1800, 900))
        self.assertTrue(is_worker_stale(1801, 900))



if __name__ == "__main__":
    unittest.main()
