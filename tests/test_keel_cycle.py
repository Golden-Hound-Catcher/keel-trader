"""Tests for Keel paper/demo vertical cycle (Stage 2)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keel.exchange.okx_rest import OkxRestAdapter
from keel.exchange.paper import PaperAdapter
from keel.ledger import KeelLedger
from keel.worker.cycle import (
    CYCLE_ERRORS_CAP,
    RISK_DENY_REASONS_CAP,
    build_cycle_summary,
    build_synthetic_candles,
    enrich_snapshot,
    market_source_from_quality_tags,
    rule_based_decision,
    run_paper_cycle,
)
from keel.worker.scheduler import KeelScheduler, JobSpec
from keel.factors.market_data import MarketSnapshot


class TestSyntheticFactors(unittest.TestCase):
    def test_build_and_enrich_snapshot(self):
        candles = build_synthetic_candles(65000.0, count=64)
        self.assertEqual(len(candles), 64)
        snap = MarketSnapshot(
            inst_id="BTC-USDT-SWAP",
            name="BTC",
            timestamp=candles[-1].timestamp,
            candles_15m=candles,
        )
        enrich_snapshot(snap)
        self.assertTrue(snap.data_valid)
        self.assertGreater(snap.price, 0)
        self.assertGreater(snap.atr_14, 0)
        decision = rule_based_decision(snap)
        self.assertIn(decision.action, ("BUY_LONG", "SELL_SHORT", "WAIT"))


class TestPaperCycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "cycle.db"
        self.ledger = KeelLedger(self.db)
        self.exchange = PaperAdapter(initial_balance=10_000.0)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_wait_cycle_records_decisions(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="WAIT",
        )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["branding"], "Keel Trader")
        self.assertEqual(summary["results"][0]["action"], "WAIT")
        latest = self.ledger.get_latest_decision("BTC-USDT-SWAP", max_age_seconds=60)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.action, "WAIT")

    def test_forced_long_executes_and_ledgers_trade(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="BUY_LONG",
        )
        self.assertTrue(summary["ok"])
        result = summary["results"][0]
        self.assertEqual(result["action"], "BUY_LONG")
        self.assertTrue(result["success"], msg=result)
        self.assertIsNotNone(result["order_id"])
        trades = self.ledger.get_trades(inst_id="BTC-USDT-SWAP")
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(len(self.exchange.get_positions()), 1)

    def test_multi_instrument_paper_cycle(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
        )
        self.assertEqual(summary["instruments"], 3)
        self.assertEqual(len(summary["results"]), 3)

    def test_forced_short_executes_and_ledgers_trade(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="SELL_SHORT",
        )
        self.assertTrue(summary["ok"])
        result = summary["results"][0]
        self.assertEqual(result["action"], "SELL_SHORT")
        self.assertTrue(result["success"], msg=result)
        self.assertTrue(result.get("filled"))
        trades = self.ledger.get_trades(inst_id="BTC-USDT-SWAP")
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(trades[0].direction, "short")

    def test_cycle_persists_factor_snapshots(self):
        run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            force_action="WAIT",
        )
        btc = self.ledger.get_latest_factor_snapshot("BTC-USDT-SWAP", max_age_seconds=60)
        self.assertIsNotNone(btc)
        self.assertGreater(btc.price, 0)
        snaps = self.ledger.get_factor_snapshots(limit=10)
        self.assertGreaterEqual(len(snaps), 2)

    def test_cycle_writes_structured_summary(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            force_action="WAIT",
        )
        self.assertIn("cycle_summary", summary)
        cs = summary["cycle_summary"]
        self.assertEqual(cs["mode"], "paper")
        self.assertEqual(cs["instruments"], 2)
        self.assertEqual(cs["decision_counts"].get("WAIT"), 2)
        self.assertEqual(cs["risk_denies"], 0)
        self.assertEqual(cs["risk_deny_reasons"], [])
        self.assertEqual(cs["error_count"], 0)
        self.assertEqual(cs["errors"], [])
        self.assertIn("duration_ms", cs)
        self.assertIsInstance(cs["duration_ms"], int)
        self.assertGreaterEqual(cs["duration_ms"], 0)
        self.assertEqual(cs.get("market_source"), "synthetic")

        stored = self.ledger.get_last_cycle_summary()
        self.assertIsNotNone(stored)
        self.assertEqual(stored["instruments"], 2)
        self.assertEqual(stored["decision_counts"].get("WAIT"), 2)
        self.assertEqual(stored["duration_ms"], cs["duration_ms"])
        self.assertEqual(stored.get("market_source"), "synthetic")
        events = self.ledger.get_events(event_type="worker_cycle_summary", limit=5)
        self.assertGreaterEqual(len(events), 1)


class TestBuildCycleSummaryRiskDenies(unittest.TestCase):
    def test_collects_gate_and_reason(self):
        cs = build_cycle_summary(
            timestamp=1.0,
            mode="paper",
            adapter="paper",
            policy="rule",
            instruments=2,
            results=[
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "action": "BUY_LONG",
                    "success": False,
                    "risk_gate_failed": "kill_switch",
                    "error": "Kill switch active",
                },
                {
                    "inst_id": "ETH-USDT-SWAP",
                    "action": "WAIT",
                    "success": True,
                },
            ],
        )
        self.assertEqual(cs["risk_denies"], 1)
        self.assertEqual(len(cs["risk_deny_reasons"]), 1)
        self.assertEqual(cs["risk_deny_reasons"][0]["gate"], "kill_switch")
        self.assertEqual(cs["risk_deny_reasons"][0]["reason"], "Kill switch active")
        self.assertEqual(cs["error_count"], 0)
        self.assertEqual(cs["errors"], [])

    def test_caps_reason_list(self):
        results = [
            {
                "inst_id": f"X{i}",
                "action": "BUY_LONG",
                "success": False,
                "risk_gate_failed": "max_positions",
                "error": f"deny-{i}",
            }
            for i in range(RISK_DENY_REASONS_CAP + 5)
        ]
        cs = build_cycle_summary(
            timestamp=1.0,
            mode="paper",
            adapter="paper",
            policy="rule",
            instruments=len(results),
            results=results,
        )
        self.assertEqual(cs["risk_denies"], RISK_DENY_REASONS_CAP + 5)
        self.assertEqual(len(cs["risk_deny_reasons"]), RISK_DENY_REASONS_CAP)
        self.assertEqual(cs["risk_deny_reasons"][0]["reason"], "deny-0")
        self.assertEqual(
            cs["risk_deny_reasons"][-1]["reason"],
            f"deny-{RISK_DENY_REASONS_CAP - 1}",
        )


class TestBuildCycleSummaryErrors(unittest.TestCase):
    def test_collects_error_count_and_detail(self):
        cs = build_cycle_summary(
            timestamp=1.0,
            mode="paper",
            adapter="paper",
            policy="rule",
            instruments=2,
            results=[
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "action": "BUY_LONG",
                    "success": False,
                    "error": "timeout",
                },
                {
                    "inst_id": "ETH-USDT-SWAP",
                    "action": "WAIT",
                    "success": True,
                },
            ],
        )
        self.assertEqual(cs["error_count"], 1)
        self.assertEqual(len(cs["errors"]), 1)
        self.assertEqual(cs["errors"][0]["inst_id"], "BTC-USDT-SWAP")
        self.assertEqual(cs["errors"][0]["error"], "timeout")
        self.assertEqual(cs["risk_denies"], 0)

    def test_caps_errors_list_keeps_full_count(self):
        results = [
            {
                "inst_id": f"X{i}",
                "action": "BUY_LONG",
                "success": False,
                "error": f"err-{i}",
            }
            for i in range(CYCLE_ERRORS_CAP + 5)
        ]
        cs = build_cycle_summary(
            timestamp=1.0,
            mode="paper",
            adapter="paper",
            policy="rule",
            instruments=len(results),
            results=results,
        )
        self.assertEqual(cs["error_count"], CYCLE_ERRORS_CAP + 5)
        self.assertEqual(len(cs["errors"]), CYCLE_ERRORS_CAP)
        self.assertEqual(cs["errors"][0]["error"], "err-0")
        self.assertEqual(
            cs["errors"][-1]["error"],
            f"err-{CYCLE_ERRORS_CAP - 1}",
        )


class TestKeelSchedulerTraderJob(unittest.TestCase):

    def test_trader_job_invokes_keel_cycle_module(self):
        scheduler = KeelScheduler(jobs=[JobSpec("trader", interval_seconds=60, timeout_seconds=30)])
        with patch("keel.worker.scheduler.subprocess.run") as run_mock:
            run_mock.return_value = type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
            scheduler._run_job(scheduler._jobs["trader"])
            args = run_mock.call_args[0][0]
            self.assertEqual(args[1:3], ["-m", "keel.worker.cycle"])
        scheduler._executor.shutdown(wait=False, cancel_futures=True)


class TestKeelWorkerCycleEntrypoint(unittest.TestCase):
    def test_keel_worker_cycle_entrypoint(self):
        import subprocess, sys
        env = os.environ.copy()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cycle.db"
            result = subprocess.run(
                [sys.executable, "-m", "keel.worker.cycle", "--db", str(db)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr + result.stdout)
            self.assertIn("Keel Trader", result.stdout)




class TestMarketSourceAggregation(unittest.TestCase):
    def test_from_quality_tags_okx_synthetic_mixed_unknown(self):
        self.assertEqual(market_source_from_quality_tags(["okx_public"]), "okx_public")
        self.assertEqual(market_source_from_quality_tags(["synthetic"]), "synthetic")
        self.assertEqual(
            market_source_from_quality_tags(["synthetic_fallback:net"]),
            "synthetic",
        )
        self.assertEqual(
            market_source_from_quality_tags(["okx_public", "synthetic_fallback:x"]),
            "mixed",
        )
        self.assertEqual(market_source_from_quality_tags([]), "unknown")
        self.assertEqual(market_source_from_quality_tags(["ok"]), "unknown")

    def test_build_cycle_summary_accepts_market_source(self):
        cs = build_cycle_summary(
            timestamp=1.0,
            mode="okx_rest",
            adapter="okx_rest",
            policy="rule",
            instruments=1,
            results=[{"inst_id": "BTC-USDT-SWAP", "action": "WAIT", "success": True}],
            quality_tags=["okx_public"],
        )
        self.assertEqual(cs["market_source"], "okx_public")

    def test_build_cycle_summary_mixed_from_tags(self):
        cs = build_cycle_summary(
            timestamp=1.0,
            mode="okx_rest",
            adapter="okx_rest",
            policy="rule",
            instruments=2,
            results=[
                {"inst_id": "BTC-USDT-SWAP", "action": "WAIT", "success": True},
                {"inst_id": "ETH-USDT-SWAP", "action": "WAIT", "success": True},
            ],
            quality_tags=["okx_public", "synthetic_fallback:timeout"],
        )
        self.assertEqual(cs["market_source"], "mixed")


class TestOkxPublicCandlesInCycle(unittest.TestCase):
    """OKX REST path uses public candles; paper stays synthetic / offline."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "okx_cycle.db"
        self.ledger = KeelLedger(self.db)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    @staticmethod
    def _fake_candle_rows(base: float = 67000.0, count: int = 64) -> list[list[float]]:
        """Oldest→newest OKX-shaped rows with rising-then-dip pattern."""
        rows: list[list[float]] = []
        price = base * 0.97
        stamp_ms = 1_700_000_000_000
        for i in range(count):
            open_px = price
            close_px = price * (1.0 + 0.0005 + (0.004 if i % 5 else -0.002))
            high_px = max(open_px, close_px) * 1.001
            low_px = min(open_px, close_px) * 0.999
            vol = 1500.0 + i * 10.0  # rising volume → volume_ratio >= 1
            rows.append([stamp_ms + i * 900_000, open_px, high_px, low_px, close_px, vol])
            price = close_px
        return rows

    def test_okx_adapter_uses_mocked_fetch_candles(self):
        rows = self._fake_candle_rows(67000.0)
        expected_close = rows[-1][4]
        exchange = OkxRestAdapter(
            api_key="k",
            secret_key="s",
            passphrase="p",
            demo=True,
            transport=lambda *a, **k: '{"code":"0","data":[]}',
        )

        def fake_fetch(inst_id, *, bar="15m", limit=50, **kwargs):
            if bar in ("15m", "1H", "1h"):
                return list(rows) if bar == "15m" else list(rows)[::4] or list(rows)
            return list(rows)

        with patch("keel.worker.cycle.fetch_candles", side_effect=fake_fetch):
            summary = run_paper_cycle(
                exchange=exchange,
                ledger=self.ledger,
                instrument_ids=["BTC-USDT-SWAP"],
                force_action="WAIT",
            )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["mode"], "okx_rest")
        snap = self.ledger.get_latest_factor_snapshot("BTC-USDT-SWAP", max_age_seconds=60)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.price, expected_close, places=4)
        self.assertTrue(snap.payload.get("data_valid"))
        self.assertEqual(snap.payload.get("data_quality_reason"), "okx_public")
        self.assertEqual(summary["cycle_summary"].get("market_source"), "okx_public")

    def test_okx_fetch_failure_falls_back_synthetic(self):
        exchange = OkxRestAdapter(
            api_key="k", secret_key="s", passphrase="p", demo=True,
            transport=lambda *a, **k: '{"code":"0","data":[]}',
        )

        with patch(
            "keel.worker.cycle.fetch_candles",
            side_effect=ValueError("network down"),
        ):
            summary = run_paper_cycle(
                exchange=exchange,
                ledger=self.ledger,
                instrument_ids=["BTC-USDT-SWAP"],
                force_action="WAIT",
            )
        self.assertTrue(summary["ok"])
        snap = self.ledger.get_latest_factor_snapshot("BTC-USDT-SWAP", max_age_seconds=60)
        self.assertIsNotNone(snap)
        self.assertGreater(snap.price, 0)
        reason = str(snap.payload.get("data_quality_reason") or "")
        self.assertTrue(reason.startswith("synthetic_fallback:"), msg=reason)
        self.assertEqual(summary["cycle_summary"].get("market_source"), "synthetic")

    def test_mixed_market_source_when_partial_fallback(self):
        exchange = OkxRestAdapter(
            api_key="k", secret_key="s", passphrase="p", demo=True,
            transport=lambda *a, **k: '{"code":"0","data":[]}',
        )
        rows = self._fake_candle_rows(67000.0)

        def fake_fetch(inst_id, *, bar="15m", limit=50, **kwargs):
            if inst_id.startswith("ETH"):
                raise ValueError("eth down")
            if bar in ("15m", "1H", "1h"):
                return list(rows) if bar == "15m" else list(rows)[::4] or list(rows)
            return list(rows)

        with patch("keel.worker.cycle.fetch_candles", side_effect=fake_fetch):
            summary = run_paper_cycle(
                exchange=exchange,
                ledger=self.ledger,
                instrument_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
                force_action="WAIT",
            )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["cycle_summary"].get("market_source"), "mixed")
        btc = self.ledger.get_latest_factor_snapshot("BTC-USDT-SWAP", max_age_seconds=60)
        eth = self.ledger.get_latest_factor_snapshot("ETH-USDT-SWAP", max_age_seconds=60)
        self.assertEqual(btc.payload.get("data_quality_reason"), "okx_public")
        self.assertTrue(
            str(eth.payload.get("data_quality_reason") or "").startswith("synthetic_fallback:")
        )

    def test_paper_path_does_not_call_fetch_candles(self):
        exchange = PaperAdapter(initial_balance=10_000.0)
        with patch("keel.worker.cycle.fetch_candles") as mocked:
            summary = run_paper_cycle(
                exchange=exchange,
                ledger=self.ledger,
                instrument_ids=["BTC-USDT-SWAP"],
                force_action="WAIT",
                force_paper=True,
            )
            mocked.assert_not_called()
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["mode"], "paper")
        snap = self.ledger.get_latest_factor_snapshot("BTC-USDT-SWAP", max_age_seconds=60)
        self.assertIsNotNone(snap)
        # paper seed prices → synthetic around DEFAULT_SEED (~65000 for BTC)
        self.assertGreater(snap.price, 50_000)

if __name__ == "__main__":
    unittest.main()


class TestMultiTfTrends(unittest.TestCase):
    """R5: enrich classifies distinct trend_15m / trend_1h / trend_4h."""

    def _bullish_closes(self, n: int, start: float = 100.0) -> list[float]:
        # Strictly rising so EMA9 > EMA21 > EMA55 and price > EMA9.
        return [start + i * 1.5 for i in range(n)]

    def _bearish_closes(self, n: int, start: float = 200.0) -> list[float]:
        return [start - i * 1.5 for i in range(n)]

    def _candles_from_closes(self, closes: list[float], *, step: float = 900.0):
        from keel.factors.market_data import Candle

        out = []
        for i, c in enumerate(closes):
            out.append(
                Candle(
                    timestamp=1_700_000_000.0 + i * step,
                    open=c * 0.999,
                    high=c * 1.002,
                    low=c * 0.998,
                    close=c,
                    volume=1000.0 + i,
                )
            )
        return out

    def test_enrich_distinct_multi_tf_trends(self):
        from keel.worker.cycle import enrich_snapshot

        # 15m rising (bullish), 1h falling (bearish), 4h flat-ish → neutral-ish
        c15 = self._candles_from_closes(self._bullish_closes(64), step=900.0)
        c1h = self._candles_from_closes(self._bearish_closes(64), step=3600.0)
        # Mild oscillation → typically neutral under strict EMA alignment
        flat = [150.0 + ((-1) ** i) * 0.2 for i in range(64)]
        c4h = self._candles_from_closes(flat, step=14400.0)

        snap = MarketSnapshot(
            inst_id="SOL-USDT-SWAP",
            name="SOL",
            timestamp=c15[-1].timestamp,
            candles_15m=c15,
            candles_1h=c1h,
            candles_4h=c4h,
        )
        enrich_snapshot(snap)
        self.assertTrue(snap.data_valid)
        self.assertEqual(snap.trend_15m, "bullish")
        self.assertEqual(snap.trend_1h, "bearish")
        # 4h oscillatory should not copy 15m/1h blindly
        self.assertNotEqual(snap.trend_4h, snap.trend_15m)
        self.assertIn(snap.trend_4h, ("bullish", "bearish", "neutral"))

    def test_enrich_falls_back_when_higher_tf_missing(self):
        from keel.worker.cycle import enrich_snapshot

        c15 = self._candles_from_closes(self._bullish_closes(64), step=900.0)
        snap = MarketSnapshot(
            inst_id="BTC-USDT-SWAP",
            name="BTC",
            timestamp=c15[-1].timestamp,
            candles_15m=c15,
            candles_1h=[],
            candles_4h=[],
        )
        enrich_snapshot(snap)
        self.assertEqual(snap.trend_15m, "bullish")
        # Empty higher TF → copy 15m (documented fallback)
        self.assertEqual(snap.trend_1h, snap.trend_15m)
        self.assertEqual(snap.trend_4h, snap.trend_1h)

    def test_classify_trend_from_candles_helper(self):
        from keel.worker.cycle import classify_trend_from_candles

        bull = self._candles_from_closes(self._bullish_closes(64))
        bear = self._candles_from_closes(self._bearish_closes(64))
        self.assertEqual(classify_trend_from_candles(bull), "bullish")
        self.assertEqual(classify_trend_from_candles(bear), "bearish")
        self.assertEqual(classify_trend_from_candles([]), "neutral")

    def test_synthetic_paper_path_sets_distinct_series(self):
        """Paper/synthetic builds 1h/4h via subsample; enrich must not force-equal."""
        from keel.worker.cycle import build_synthetic_candles, enrich_snapshot

        c15 = build_synthetic_candles(145.0, count=64, drift=0.002, volatility=0.02)
        c1h = c15[::4] or c15
        c4h = c15[::16] or c15
        snap = MarketSnapshot(
            inst_id="SOL-USDT-SWAP",
            name="SOL",
            timestamp=c15[-1].timestamp,
            candles_15m=c15,
            candles_1h=c1h,
            candles_4h=c4h,
        )
        enrich_snapshot(snap)
        self.assertTrue(snap.data_valid)
        # Values are independently classified (may or may not match numerically)
        for t in (snap.trend_15m, snap.trend_1h, snap.trend_4h):
            self.assertIn(t, ("bullish", "bearish", "neutral"))
