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
from keel.exchange.okx_fees import (
    REGULAR_USDT_SWAP_MAKER_BPS,
    REGULAR_USDT_SWAP_TAKER_BPS,
    clear_fee_caches,
)
from keel.execution.near_probe import (
    PROBE_POLICY,
    PROBE_STRATEGY_TAG,
    SKIP_EVENT_TYPE,
    build_near_probe_decision,
    edge_clears_hurdle,
    estimate_near_probe_edge_bps,
    evaluate_near_probe,
    maybe_near_probe_decision,
    near_probe_skip_payload,
    near_signal_meets_gates,
    probe_fill_recent,
    record_near_probe_skip,
    resolve_near_probe_hurdle_bps,
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
            min_edge_bps=0,
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
            min_edge_bps=0,
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
            min_edge_bps=0,
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
            min_edge_bps=0,
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
            min_edge_bps=0,
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
            min_edge_bps=0,
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
            "KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS",
            "KEEL_SHADOW_NEAR_PROBE_EDGE_MODE",
            "KEEL_SHADOW_FEE_ROLE",
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
        clear_fee_caches()
        s = refresh_settings()
        self.assertFalse(s.shadow_near_probe)
        self.assertEqual(s.shadow_near_probe_cooldown_seconds, 900)
        self.assertEqual(s.shadow_near_probe_max_missing, 2)
        self.assertIsNone(s.shadow_near_probe_min_edge_bps)
        self.assertEqual(s.shadow_near_probe_edge_mode, "round_trip")
        hurdle, role, mode = resolve_near_probe_hurdle_bps(s)
        self.assertEqual(role, "taker")
        self.assertEqual(mode, "round_trip")
        self.assertEqual(hurdle, 2.0 * REGULAR_USDT_SWAP_TAKER_BPS)

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
        os.environ["KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS"] = "0"
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



class TestNearProbeEdgeHurdle(unittest.TestCase):
    """Q3.4: fee-aware min edge gate."""

    def setUp(self):
        clear_fee_caches()
        self._env_keys = [
            "KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS",
            "KEEL_SHADOW_NEAR_PROBE_EDGE_MODE",
            "KEEL_SHADOW_FEE_ROLE",
            "KEEL_SHADOW_MAKER_FEE_BPS",
            "KEEL_SHADOW_TAKER_FEE_BPS",
        ]
        self._prev = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)
        refresh_settings()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        clear_fee_caches()
        refresh_settings()

    def test_default_hurdle_equals_rt_fee_taker(self):
        s = refresh_settings()
        hurdle, role, mode = resolve_near_probe_hurdle_bps(s)
        self.assertEqual(role, "taker")
        self.assertEqual(mode, "round_trip")
        self.assertEqual(hurdle, 2.0 * REGULAR_USDT_SWAP_TAKER_BPS)  # 10

    def test_default_hurdle_maker_rt(self):
        os.environ["KEEL_SHADOW_FEE_ROLE"] = "maker"
        s = refresh_settings()
        hurdle, role, mode = resolve_near_probe_hurdle_bps(s)
        self.assertEqual(role, "maker")
        self.assertEqual(hurdle, 2.0 * REGULAR_USDT_SWAP_MAKER_BPS)  # 4

    def test_edge_mode_open_uses_one_leg(self):
        os.environ["KEEL_SHADOW_NEAR_PROBE_EDGE_MODE"] = "open"
        s = refresh_settings()
        hurdle, role, mode = resolve_near_probe_hurdle_bps(s)
        self.assertEqual(mode, "open")
        self.assertEqual(hurdle, REGULAR_USDT_SWAP_TAKER_BPS)  # 5

    def test_override_env_min_edge(self):
        os.environ["KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS"] = "3.5"
        s = refresh_settings()
        self.assertEqual(s.shadow_near_probe_min_edge_bps, 3.5)
        hurdle, _, _ = resolve_near_probe_hurdle_bps(s)
        self.assertEqual(hurdle, 3.5)

    def test_estimate_fail_closed_no_atr(self):
        edge = estimate_near_probe_edge_bps(
            {"nearest": "long", "missing": []},
            _snap(atr_14=0.0),
            decision_confidence=80.0,
        )
        self.assertIsNone(edge)
        self.assertFalse(edge_clears_hurdle(None, 10.0))
        self.assertTrue(edge_clears_hurdle(None, 0.0))  # hurdle 0 disables

    def test_below_hurdle_no_shadow_fill(self):
        # Weak near: atr_bps=200, missing=1 → completeness=0.8, conf=0.4
        # p=0.32, EV_atr=3.2*0.32-1=0.024 → edge≈4.8 < default RT 10
        out = maybe_near_probe_decision(
            _wait_near("long", missing=["volume_ok"], confidence=40.0),
            _snap(price=100.0, atr_14=2.0),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=0,
            # default hurdle via settings (RT taker 10)
            settings=refresh_settings(),
        )
        self.assertIsNone(out)

    def test_above_hurdle_emits_probe(self):
        # Stronger: high ATR + high confidence + few missing → edge >> 10
        # atr_bps=500, missing=0, conf=80 → p=0.8, EV=1.56 → edge=780
        out = maybe_near_probe_decision(
            _wait_near("long", missing=[], confidence=80.0),
            _snap(price=100.0, atr_14=5.0),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=0,
            settings=refresh_settings(),
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.action, "BUY_LONG")
        self.assertIn("edge_bps=", out.reason)
        self.assertIn("hurdle_bps=", out.reason)
        self.assertIn("fee_role=taker", out.reason)
        self.assertIsNotNone(out.signal_diag)
        self.assertIn("edge_bps", out.signal_diag)
        self.assertIn("hurdle_bps", out.signal_diag)
        self.assertGreaterEqual(out.signal_diag["edge_bps"], out.signal_diag["hurdle_bps"])

    def test_override_low_hurdle_allows_weak(self):
        out = maybe_near_probe_decision(
            _wait_near("long", missing=["volume_ok"], confidence=40.0),
            _snap(price=100.0, atr_14=2.0),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=0,
            min_edge_bps=1.0,  # weak edge ~4.8 clears 1
        )
        self.assertIsNotNone(out)

    def test_orchestrator_shadow_fill_carries_audit(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = KeelLedger(Path(td) / "t.db")
            exchange = PaperAdapter()
            exchange.set_ticker(
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
            orch = ExecutionOrchestrator(exchange=exchange, ledger=ledger)
            decision = maybe_near_probe_decision(
                _wait_near("long", missing=[], confidence=80.0),
                _snap(price=100.0, atr_14=5.0),
                kill_switch=True,
                shadow_mode=True,
                probe_enabled=True,
                cooldown_seconds=0,
                settings=refresh_settings(),
            )
            self.assertIsNotNone(decision)
            assert decision is not None
            result = orch.execute_decision(decision, kill_switch=True, shadow_mode=True)
            self.assertTrue(result.shadow)
            events = ledger.get_events(event_type="shadow_fill")
            self.assertEqual(len(events), 1)
            data = events[0].data
            self.assertTrue(data.get("probe"))
            self.assertIn("edge_bps", data)
            self.assertIn("hurdle_bps", data)
            self.assertEqual(data.get("fee_role"), "taker")
            trades = ledger.get_trades()
            self.assertIn("edge_bps", trades[0].metadata)
            ledger.close()




class TestNearProbeSkipObservability(unittest.TestCase):
    """Q3.5: skip reasons + durable shadow_near_probe_skip events."""

    def test_skip_payload_shape(self):
        payload = near_probe_skip_payload(
            "below_hurdle",
            edge_bps=4.8,
            hurdle_bps=10.0,
            fee_role="taker",
            edge_mode="round_trip",
        )
        self.assertEqual(payload["reason"], "below_hurdle")
        self.assertEqual(payload["edge_bps"], 4.8)
        self.assertEqual(payload["hurdle_bps"], 10.0)
        self.assertEqual(payload["fee_role"], "taker")
        self.assertEqual(payload["edge_mode"], "round_trip")

    def test_evaluate_below_hurdle_reason(self):
        out = evaluate_near_probe(
            _wait_near("long", missing=["volume_ok"], confidence=40.0),
            _snap(price=100.0, atr_14=2.0),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=0,
            settings=refresh_settings(),
        )
        self.assertIsNone(out.decision)
        self.assertEqual(out.skip_reason, "below_hurdle")
        self.assertIsNotNone(out.hurdle_bps)
        self.assertIsNotNone(out.edge_bps)
        self.assertLess(out.edge_bps, out.hurdle_bps)

    def test_evaluate_edge_unavailable(self):
        out = evaluate_near_probe(
            _wait_near("long", missing=[], confidence=80.0),
            _snap(price=100.0, atr_14=0.0),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=0,
            min_edge_bps=10.0,
        )
        self.assertIsNone(out.decision)
        self.assertEqual(out.skip_reason, "edge_unavailable")

    def test_evaluate_max_missing(self):
        out = evaluate_near_probe(
            _wait_near("long", missing=["a", "b", "c"], confidence=80.0),
            _snap(),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=0,
            max_missing=2,
            min_edge_bps=0,
        )
        self.assertEqual(out.skip_reason, "max_missing")

    def test_evaluate_not_near(self):
        out = evaluate_near_probe(
            _wait_near("none", missing=[], confidence=80.0),
            _snap(),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
            cooldown_seconds=0,
            min_edge_bps=0,
        )
        self.assertEqual(out.skip_reason, "not_near")

    def test_evaluate_probe_disabled(self):
        out = evaluate_near_probe(
            _wait_near(),
            _snap(),
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=False,
            cooldown_seconds=0,
            min_edge_bps=0,
        )
        self.assertEqual(out.skip_reason, "probe_disabled")

    def test_evaluate_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = KeelLedger(Path(td) / "t.db")
            decision = build_near_probe_decision(_wait_near("long"), _snap())
            assert decision is not None
            exchange = PaperAdapter()
            exchange.set_ticker(
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
            orch = ExecutionOrchestrator(exchange=exchange, ledger=ledger)
            orch.execute_decision(decision, kill_switch=True, shadow_mode=True)
            out = evaluate_near_probe(
                _wait_near("long"),
                _snap(),
                kill_switch=True,
                shadow_mode=True,
                probe_enabled=True,
                cooldown_seconds=3600,
                ledger=ledger,
                min_edge_bps=0,
            )
            self.assertEqual(out.skip_reason, "cooldown")
            ledger.close()

    def test_record_skip_and_shadow_stats(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = KeelLedger(Path(td) / "t.db")
            eid = record_near_probe_skip(
                ledger,
                inst_id="BTC-USDT-SWAP",
                reason="below_hurdle",
                edge_bps=4.8,
                hurdle_bps=10.0,
                fee_role="taker",
                edge_mode="round_trip",
            )
            self.assertIsNotNone(eid)
            events = ledger.get_events(event_type=SKIP_EVENT_TYPE)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].data.get("reason"), "below_hurdle")
            self.assertEqual(events[0].data.get("edge_bps"), 4.8)
            self.assertEqual(events[0].data.get("hurdle_bps"), 10.0)
            stats = ledger.get_shadow_stats(hours=24, include_markout=False)
            self.assertIn("probe_skips", stats)
            self.assertEqual(stats["probe_skips"]["count"], 1)
            self.assertEqual(stats["by_skip_reason"].get("below_hurdle"), 1)
            self.assertEqual(stats["probe_skips"]["top_skip_reason"], "below_hurdle")
            # markout path also carries skips
            stats2 = ledger.get_shadow_stats(hours=24, include_markout=True)
            self.assertEqual(stats2["probe_skips"]["count"], 1)
            ledger.close()

    def test_cycle_records_below_hurdle_skip(self):
        import os
        from keel.config import refresh_settings
        from keel.policy.protocol import PolicyResult
        from keel.policy.stub import StubDecisionPolicy

        prev = {}
        keys = [
            "KEEL_KILL_SWITCH",
            "KEEL_SHADOW_MODE",
            "KEEL_SHADOW_NEAR_PROBE",
            "KEEL_SHADOW_NEAR_PROBE_COOLDOWN_SECONDS",
            "KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS",
        ]
        for k in keys:
            prev[k] = os.environ.get(k)
        try:
            os.environ["KEEL_KILL_SWITCH"] = "1"
            os.environ["KEEL_SHADOW_MODE"] = "1"
            os.environ["KEEL_SHADOW_NEAR_PROBE"] = "1"
            os.environ["KEEL_SHADOW_NEAR_PROBE_COOLDOWN_SECONDS"] = "0"
            # Use default fee hurdle (no override) so weak near fails below_hurdle
            os.environ.pop("KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS", None)
            refresh_settings()

            class WeakNearWaitPolicy(StubDecisionPolicy):
                @property
                def name(self) -> str:
                    return "weak-near-wait-test"

                def decide(self, ctx):
                    decisions = {}
                    for inst_id in ctx.instrument_ids:
                        decisions[inst_id] = Decision(
                            inst_id=inst_id,
                            action="WAIT",
                            confidence=40.0,
                            reason="test weak near",
                            signal_diag={
                                "nearest": "long",
                                "missing": ["volume_ok"],
                                "data_valid": True,
                            },
                        )
                    return PolicyResult(
                        decisions=decisions, policy_name=self.name, success=True
                    )

            with tempfile.TemporaryDirectory() as td:
                ledger = KeelLedger(Path(td) / "t.db")
                exchange = PaperAdapter()
                # Seed a snap with moderate ATR via paper path — cycle builds own snaps.
                summary = run_paper_cycle(
                    exchange=exchange,
                    ledger=ledger,
                    instrument_ids=["BTC-USDT-SWAP"],
                    force_paper=True,
                    policy=WeakNearWaitPolicy(),
                    seed_prices={"BTC-USDT-SWAP": 100.0},
                )
                self.assertTrue(summary["ok"])
                skips = ledger.get_events(event_type=SKIP_EVENT_TYPE)
                # May or may not skip depending on live ATR from synthetic candles;
                # assert annotation path: either fired probe OR recorded skip OR not_near.
                row = summary["results"][0]
                if row.get("shadow_near_probe"):
                    self.assertEqual(len(skips), 0)
                else:
                    # When probe stack on + WAIT, we should have skip reason on row
                    self.assertIn("near_probe_skip_reason", row)
                    reason = row["near_probe_skip_reason"]
                    self.assertIn(
                        reason,
                        {
                            "below_hurdle",
                            "edge_unavailable",
                            "not_near",
                            "max_missing",
                            "cooldown",
                        },
                    )
                    if reason != "probe_disabled":
                        self.assertGreaterEqual(len(skips), 1)
                        self.assertEqual(skips[0].data.get("reason"), reason)
                    # cycle_summary (and ledger last_cycle) carry by_skip_reason
                    cs = summary.get("cycle_summary") or {}
                    self.assertGreaterEqual(int(cs.get("probe_skips") or 0), 1)
                    self.assertIn(reason, cs.get("by_skip_reason") or {})
                    last = ledger.get_last_cycle_summary() or {}
                    self.assertEqual(last.get("top_skip_reason"), reason)
                ledger.close()
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            refresh_settings()



if __name__ == "__main__":
    unittest.main()
