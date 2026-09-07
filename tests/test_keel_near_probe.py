"""Q3 near-signal shadow probe: settings gates, cycle conversion, cooldown, safety."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from keel.config import refresh_settings
from keel.domain.decision import Decision
from keel.exchange.paper import PaperAdapter
from keel.exchange.protocol import Ticker
from keel.execution.near_probe import (
    PROBE_POLICY,
    PROBE_STRATEGY_TAG,
    build_near_probe_decision,
    maybe_near_probe_decision,
    near_signal_meets_gates,
    probe_fill_recent,
    should_attempt_near_probe,
)
from keel.execution.orchestrator import ExecutionOrchestrator
from keel.factors.market_data import MarketSnapshot
from keel.ledger import KeelLedger
from keel.risk.arming import evaluate_arming
from keel.worker.cycle import run_paper_cycle


def _snap(**kwargs) -> MarketSnapshot:
    base = dict(
        inst_id="BTC-USDT-SWAP",
        name="BTC",
        timestamp=time.time(),
        bid=99.9,
        ask=100.1,
        price=100.0,
        atr_14=2.0,
        rsi_14=40.0,
        ema_9=101.0,
        ema_21=100.0,
        macd_histogram=0.1,
        trend_15m="bullish",
        volume_ratio=1.2,
        data_valid=True,
    )
    base.update(kwargs)
    return MarketSnapshot(**base)


def _wait_near(nearest: str = "long", missing: list | None = None, confidence: float = 40.0) -> Decision:
    miss = missing if missing is not None else ["volume_ok"]
    return Decision(
        inst_id="BTC-USDT-SWAP",
        action="WAIT",
        confidence=confidence,
        reason="no rule signal",
        signal_diag={
            "nearest": nearest,
            "missing": miss,
            "data_valid": True,
        },
    )


class TestNearProbeGates(unittest.TestCase):
    def test_meets_gates_strong_near(self):
        self.assertTrue(
            near_signal_meets_gates(
                {"nearest": "long", "missing": ["volume_ok"]},
                max_missing=2,
            )
        )

    def test_rejects_too_many_missing(self):
        self.assertFalse(
            near_signal_meets_gates(
                {"nearest": "long", "missing": ["a", "b", "c"]},
                max_missing=2,
            )
        )

    def test_rejects_nearest_none(self):
        self.assertFalse(
            near_signal_meets_gates({"nearest": "none", "missing": []}, max_missing=2)
        )

    def test_min_confidence_gate(self):
        self.assertFalse(
            near_signal_meets_gates(
                {"nearest": "short", "missing": []},
                max_missing=2,
                min_confidence=50.0,
                decision_confidence=40.0,
            )
        )
        self.assertTrue(
            near_signal_meets_gates(
                {"nearest": "short", "missing": []},
                max_missing=2,
                min_confidence=50.0,
                decision_confidence=60.0,
            )
        )

    def test_should_attempt_requires_all_three(self):
        self.assertTrue(
            should_attempt_near_probe(kill_switch=True, shadow_mode=True, probe_enabled=True)
        )
        self.assertFalse(
            should_attempt_near_probe(kill_switch=False, shadow_mode=True, probe_enabled=True)
        )
        self.assertFalse(
            should_attempt_near_probe(kill_switch=True, shadow_mode=False, probe_enabled=True)
        )
        self.assertFalse(
            should_attempt_near_probe(kill_switch=True, shadow_mode=True, probe_enabled=False)
        )


class TestBuildProbeDecision(unittest.TestCase):
    def test_long_near_builds_buy(self):
        d = build_near_probe_decision(_wait_near("long"), _snap())
        self.assertIsNotNone(d)
        assert d is not None
        self.assertEqual(d.action, "BUY_LONG")
        self.assertTrue(d.valid)
        self.assertTrue(d.reason.startswith(PROBE_POLICY))

    def test_short_near_builds_sell(self):
        d = build_near_probe_decision(_wait_near("short"), _snap(trend_15m="bearish"))
        self.assertIsNotNone(d)
        assert d is not None
        self.assertEqual(d.action, "SELL_SHORT")


class TestMaybeNearProbe(unittest.TestCase):
    def test_probe_off_returns_none(self):
        out = maybe_near_probe_decision(
            _wait_near(),
            _snap(),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=False,
            cooldown_seconds=0,
        )
        self.assertIsNone(out)

    def test_probe_on_kill_shadow_near_long(self):
        out = maybe_near_probe_decision(
            _wait_near("long", missing=["volume_ok"]),
            _snap(),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=0,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.action, "BUY_LONG")

    def test_without_kill_no_probe(self):
        out = maybe_near_probe_decision(
            _wait_near(),
            _snap(),
            kill_switch=False,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=0,
        )
        self.assertIsNone(out)

    def test_without_shadow_no_probe(self):
        out = maybe_near_probe_decision(
            _wait_near(),
            _snap(),
            kill_switch=True,
            shadow_mode=False,
            probe_enabled=True,
            cooldown_seconds=0,
        )
        self.assertIsNone(out)


class TestProbeCooldownAndOrchestrator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        self.ledger = KeelLedger(self.db)
        self.exchange = PaperAdapter()
        self.exchange.set_ticker(
            Ticker(
                inst_id="BTC-USDT-SWAP",
                last=100.0,
                bid=99.9,
                ask=100.1,
                open_24h=100.0,
                high_24h=101.0,
                low_24h=99.0,
                vol_24h=1_000_000.0,
                timestamp=time.time(),
            )
        )
        self.orch = ExecutionOrchestrator(exchange=self.exchange, ledger=self.ledger)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_probe_shadow_fill_tagged(self):
        place_calls: list = []

        def boom(request):
            place_calls.append(request)
            raise AssertionError("place_order must not be called")

        self.exchange.place_order = boom  # type: ignore[method-assign]
        decision = build_near_probe_decision(_wait_near("long"), _snap())
        self.assertIsNotNone(decision)
        assert decision is not None
        result = self.orch.execute_decision(
            decision, kill_switch=True, shadow_mode=True
        )
        self.assertTrue(result.success)
        self.assertTrue(result.shadow)
        self.assertEqual(place_calls, [])
        events = self.ledger.get_events(event_type="shadow_fill")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data.get("policy"), PROBE_POLICY)
        self.assertTrue(events[0].data.get("probe"))
        trades = self.ledger.get_trades()
        self.assertEqual(trades[0].strategy_tag, PROBE_STRATEGY_TAG)
        self.assertTrue(trades[0].metadata.get("probe"))

    def test_cooldown_respected(self):
        decision = build_near_probe_decision(_wait_near("long"), _snap())
        assert decision is not None
        self.orch.execute_decision(decision, kill_switch=True, shadow_mode=True)
        self.assertTrue(
            probe_fill_recent(
                self.ledger,
                inst_id="BTC-USDT-SWAP",
                cooldown_seconds=3600,
            )
        )
        out = maybe_near_probe_decision(
            _wait_near("long"),
            _snap(),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=3600,
            ledger=self.ledger,
        )
        self.assertIsNone(out)
        # cooldown_seconds=0 disables cooldown
        out2 = maybe_near_probe_decision(
            _wait_near("long"),
            _snap(),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=0,
            ledger=self.ledger,
        )
        self.assertIsNotNone(out2)

    def test_shadow_stats_probe_count(self):
        decision = build_near_probe_decision(_wait_near("long"), _snap())
        assert decision is not None
        self.orch.execute_decision(decision, kill_switch=True, shadow_mode=True)
        stats = self.ledger.get_shadow_stats(hours=24)
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["probe_count"], 1)
        self.assertEqual(stats["by_policy"].get(PROBE_POLICY), 1)

    def test_arming_counts_probe_fill(self):
        decision = build_near_probe_decision(_wait_near("long"), _snap())
        assert decision is not None
        self.orch.execute_decision(decision, kill_switch=True, shadow_mode=True)
        settings = MagicMock(
            kill_switch=True,
            okx_configured=True,
            okx_environment="live",
            max_notional_per_instrument=2000.0,
            max_daily_loss_usdt=150.0,
            live_max_notional_per_instrument=200.0,
            live_max_contracts_per_instrument=5,
            arming_shadow_hours=24.0,
            arming_require_shadow=False,
        )
        report = evaluate_arming(settings, "trade", ledger=self.ledger)
        self.assertFalse(any("no recent shadow_fill rehearsal" in w for w in report.warnings))


class TestNearProbeSettingsAndCycle(unittest.TestCase):
    def setUp(self):
        self._env_keys = [
            "KEEL_KILL_SWITCH",
            "KEEL_SHADOW_MODE",
            "KEEL_SHADOW_NEAR_PROBE",
            "KEEL_SHADOW_NEAR_PROBE_COOLDOWN_SECONDS",
            "KEEL_SHADOW_NEAR_PROBE_MAX_MISSING",
            "KEEL_FORCE_ACTION",
        ]
        self._prev = {k: os.environ.get(k) for k in self._env_keys}

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        refresh_settings()

    def test_settings_default_off(self):
        for k in self._env_keys:
            os.environ.pop(k, None)
        s = refresh_settings()
        self.assertFalse(s.shadow_near_probe)
        self.assertEqual(s.shadow_near_probe_cooldown_seconds, 900)
        self.assertEqual(s.shadow_near_probe_max_missing, 2)

    def test_settings_env_on(self):
        os.environ["KEEL_SHADOW_NEAR_PROBE"] = "1"
        os.environ["KEEL_SHADOW_NEAR_PROBE_COOLDOWN_SECONDS"] = "60"
        os.environ["KEEL_SHADOW_NEAR_PROBE_MAX_MISSING"] = "1"
        s = refresh_settings()
        self.assertTrue(s.shadow_near_probe)
        self.assertEqual(s.shadow_near_probe_cooldown_seconds, 60)
        self.assertEqual(s.shadow_near_probe_max_missing, 1)

    def test_cycle_probe_off_no_shadow_fill(self):
        os.environ["KEEL_KILL_SWITCH"] = "1"
        os.environ["KEEL_SHADOW_MODE"] = "1"
        os.environ["KEEL_SHADOW_NEAR_PROBE"] = "0"
        refresh_settings()
        with tempfile.TemporaryDirectory() as td:
            ledger = KeelLedger(Path(td) / "t.db")
            summary = run_paper_cycle(
                ledger=ledger,
                instrument_ids=["BTC-USDT-SWAP"],
                force_paper=True,
            )
            self.assertTrue(summary["ok"])
            self.assertEqual(len(ledger.get_events(event_type="shadow_fill")), 0)
            ledger.close()

    def test_cycle_probe_on_with_forced_wait_near_via_unit_path(self):
        """Integration via orchestrator path already covered; cycle with probe on
        and synthetic WAIT rarely near-fires — verify probe off matrix + settings
        wiring here. Full near conversion covered in TestMaybeNearProbe."""
        os.environ["KEEL_KILL_SWITCH"] = "1"
        os.environ["KEEL_SHADOW_MODE"] = "0"
        os.environ["KEEL_SHADOW_NEAR_PROBE"] = "1"
        refresh_settings()
        with tempfile.TemporaryDirectory() as td:
            ledger = KeelLedger(Path(td) / "t.db")
            place_calls: list = []
            exchange = PaperAdapter()

            def boom(request):
                place_calls.append(request)
                raise AssertionError("must not place_order")

            exchange.place_order = boom  # type: ignore[method-assign]
            summary = run_paper_cycle(
                exchange=exchange,
                ledger=ledger,
                instrument_ids=["BTC-USDT-SWAP"],
                force_paper=True,
                force_action="WAIT",
            )
            self.assertTrue(summary["ok"])
            self.assertEqual(place_calls, [])
            self.assertEqual(len(ledger.get_events(event_type="shadow_fill")), 0)
            ledger.close()

    def test_cycle_probe_injects_shadow_when_diag_near(self):
        """Monkeypatch maybe path by forcing a crafted decision through cycle policy."""
        os.environ["KEEL_KILL_SWITCH"] = "1"
        os.environ["KEEL_SHADOW_MODE"] = "1"
        os.environ["KEEL_SHADOW_NEAR_PROBE"] = "1"
        os.environ["KEEL_SHADOW_NEAR_PROBE_COOLDOWN_SECONDS"] = "0"
        refresh_settings()

        from keel.policy.protocol import PolicyResult
        from keel.policy.stub import StubDecisionPolicy

        class NearWaitPolicy(StubDecisionPolicy):
            @property
            def name(self) -> str:
                return "near-wait-test"

            def decide(self, ctx):
                decisions = {}
                for inst_id in ctx.instrument_ids:
                    snap = ctx.snapshots.get(inst_id)
                    decisions[inst_id] = Decision(
                        inst_id=inst_id,
                        action="WAIT",
                        confidence=40.0,
                        reason="test near wait",
                        signal_diag={
                            "nearest": "long",
                            "missing": ["volume_ok"],
                            "data_valid": True,
                            "rsi_14": getattr(snap, "rsi_14", 40.0) if snap else 40.0,
                        },
                    )
                return PolicyResult(decisions=decisions, policy_name=self.name, success=True)

        with tempfile.TemporaryDirectory() as td:
            ledger = KeelLedger(Path(td) / "t.db")
            place_calls: list = []
            exchange = PaperAdapter()

            def boom(request):
                place_calls.append(request)
                raise AssertionError("must not place_order")

            exchange.place_order = boom  # type: ignore[method-assign]
            summary = run_paper_cycle(
                exchange=exchange,
                ledger=ledger,
                instrument_ids=["BTC-USDT-SWAP"],
                force_paper=True,
                policy=NearWaitPolicy(),
            )
            self.assertTrue(summary["ok"])
            self.assertEqual(place_calls, [])
            events = ledger.get_events(event_type="shadow_fill")
            self.assertGreaterEqual(len(events), 1)
            self.assertEqual(events[0].data.get("policy"), PROBE_POLICY)
            self.assertTrue(events[0].data.get("probe"))
            # Policy decision recorded as WAIT
            decisions = ledger.get_decisions(limit=5)
            self.assertTrue(any(d.action == "WAIT" for d in decisions))
            row = summary["results"][0]
            self.assertEqual(row["action"], "WAIT")
            self.assertTrue(row.get("shadow_near_probe"))
            self.assertEqual(row.get("exec_action"), "BUY_LONG")
            self.assertTrue(row.get("shadow"))
            ledger.close()


if __name__ == "__main__":
    unittest.main()
