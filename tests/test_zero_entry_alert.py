"""48h zero-entry alert: market-driven vs outage, API / script / scheduler surfaces."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.api.deps import set_ledger_path_override
from keel.config import refresh_settings
from keel.domain import TradeRecord
from keel.domain.decision import Decision
from keel.domain.records import DecisionRecord
from keel.factors.market_data import MarketSnapshot
from keel.ledger import KeelLedger
from keel.ledger.zero_entry_alert import _classify, compute_zero_entry_alert
from keel.policy.edge_overlay import apply_llm_book_lock, apply_llm_edge_overlay

ROOT = Path(__file__).resolve().parents[1]
H = 3600.0
INTERVAL = 300


def _diag(missing: list[str], **extra: object) -> dict:
    d: dict = {"missing": missing, "nearest": "none"}
    d.update(extra)
    return d


def _add_cycles(led: KeelLedger, now: float, start_ago_h: float, end_ago_h: float = 0.0) -> None:
    ts = now - start_ago_h * H
    while ts <= now - end_ago_h * H:
        led.record_cycle_summary({"timestamp": ts, "mode": "okx_rest"})
        ts += INTERVAL


def _add_decision(led: KeelLedger, ts: float, action: str = "WAIT", diag: dict | None = None,
                  trend_4h: str = "neutral") -> None:
    calc: dict = {"trend_4h": trend_4h, "policy_name": "llm"}
    if diag is not None:
        calc["signal_diag"] = diag
    led.record_decision(
        DecisionRecord(timestamp=ts, inst_id="BTC-USDT-SWAP", action=action,
                       calculus_data=calc, policy_name="llm")
    )


def _add_entry(led: KeelLedger, ts: float, tag: str = "keel-llm", action: str = "open") -> None:
    led.record_trade(
        TradeRecord(timestamp=ts, inst_id="SOL-USDT-SWAP", action=action,  # type: ignore[arg-type]
                    direction="short", size=1.0, price=117.85, strategy_tag=tag)
    )


def _market_scenario(led: KeelLedger, now: float) -> None:
    """Entry 50h ago, worker healthy all window, 4h-hard dominates vetoes."""
    _add_entry(led, now - 50 * H)
    _add_cycles(led, now, 49.0, end_ago_h=0.02)
    base = now - 10 * H
    for i in range(6):
        _add_decision(led, base + i, diag=_diag(["htf_ok"], llm_4h_mode="hard",
                                                htf_veto_cause="4h_hard_neutral"))
    for i in range(2):
        _add_decision(led, base + 10 + i, diag=_diag(["adx_ok"]), trend_4h="bullish")
    for i in range(2):
        _add_decision(led, base + 20 + i)  # model itself WAIT


class _TmpLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "zero_entry.db"
        self.led = KeelLedger(self.db)
        self.now = time.time()
        self._saved_hours = os.environ.pop("KEEL_ZERO_ENTRY_ALERT_HOURS", None)

    def tearDown(self) -> None:
        self.led.close()
        if self._saved_hours is not None:
            os.environ["KEEL_ZERO_ENTRY_ALERT_HOURS"] = self._saved_hours
        else:
            os.environ.pop("KEEL_ZERO_ENTRY_ALERT_HOURS", None)
        self.tmp.cleanup()

    def alert(self, **kw: object) -> dict:
        return compute_zero_entry_alert(self.led, now=self.now, cycle_interval_seconds=INTERVAL, **kw)


class TestZeroEntryCompute(_TmpLedger):
    def test_recent_entry_not_triggered(self) -> None:
        _add_entry(self.led, self.now - 10 * H)
        _add_cycles(self.led, self.now, 12.0)
        a = self.alert()
        self.assertFalse(a["triggered"])
        self.assertEqual(a["kind"], "ok")
        self.assertEqual(a["threshold_hours"], 48.0)
        self.assertAlmostEqual(a["hours_since_last_entry"], 10.0, places=1)
        self.assertEqual(a["last_entry"]["inst_id"], "SOL-USDT-SWAP")
        self.assertIn("正常", a["message_zh"])

    def test_market_driven_with_veto_shares(self) -> None:
        _market_scenario(self.led, self.now)
        a = self.alert()
        self.assertTrue(a["triggered"])
        self.assertEqual(a["kind"], "market")
        self.assertEqual(a["severity"], "warning")
        self.assertFalse(a["worker_stale"])
        self.assertGreaterEqual(a["cycle_coverage"], 0.9)
        self.assertEqual(a["decisions_in_window"], 10)
        self.assertEqual(a["veto_counts"]["4h_hard"], 6)
        self.assertAlmostEqual(a["veto_shares"]["4h_hard"], 0.6)
        self.assertAlmostEqual(a["veto_shares"]["adx"], 0.2)
        self.assertAlmostEqual(a["veto_shares"]["llm_wait"], 0.2)
        self.assertAlmostEqual(a["trend_4h_neutral_share"], 0.8)
        msg = a["message_zh"]
        self.assertIn("行情驱动", msg)
        self.assertIn("4h硬门槛否决 60%", msg)
        self.assertIn("ADX下限否决 20%", msg)
        self.assertIn("4h=neutral 占 80%", msg)

    def test_outage_worker_stale(self) -> None:
        _add_entry(self.led, self.now - 50 * H)
        _add_cycles(self.led, self.now, 49.0, end_ago_h=2.0)  # last cycle 2h ago
        a = self.alert()
        self.assertTrue(a["triggered"])
        self.assertEqual(a["kind"], "outage")
        self.assertEqual(a["severity"], "critical")
        self.assertTrue(a["worker_stale"])
        self.assertIn("服务故障", a["message_zh"])
        self.assertIn("worker 停滞", a["message_zh"])

    def test_outage_low_cycle_coverage_even_if_worker_recovered(self) -> None:
        _add_entry(self.led, self.now - 60 * H)
        _add_cycles(self.led, self.now, 10.0)  # only last 10h of 48h covered
        a = self.alert()
        self.assertTrue(a["triggered"])
        self.assertEqual(a["kind"], "outage")
        self.assertFalse(a["worker_stale"])
        self.assertLess(a["cycle_coverage"], 0.5)
        self.assertIn("服务曾中断", a["message_zh"])

    def test_shadow_opens_and_closes_do_not_count(self) -> None:
        _add_entry(self.led, self.now - 50 * H)
        _add_entry(self.led, self.now - 1 * H, tag="keel-shadow")
        _add_entry(self.led, self.now - 1 * H, action="close")
        _add_cycles(self.led, self.now, 49.0, end_ago_h=0.02)
        a = self.alert()
        self.assertTrue(a["triggered"])
        self.assertEqual(a["kind"], "market")
        self.assertAlmostEqual(a["hours_since_last_entry"], 50.0, places=1)

    def test_fresh_ledger_counts_from_first_llm_decision(self) -> None:
        _add_cycles(self.led, self.now, 5.0)
        _add_decision(self.led, self.now - 5 * H)
        a = self.alert()
        self.assertFalse(a["triggered"])
        self.assertIsNone(a["last_entry"])
        self.assertIn("无 LLM 开仓记录", a["message_zh"])

    def test_threshold_env_and_disable(self) -> None:
        _add_entry(self.led, self.now - 10 * H)
        _add_cycles(self.led, self.now, 12.0)
        os.environ["KEEL_ZERO_ENTRY_ALERT_HOURS"] = "6"
        a = self.alert()
        self.assertTrue(a["triggered"])
        self.assertEqual(a["threshold_hours"], 6.0)
        os.environ["KEEL_ZERO_ENTRY_ALERT_HOURS"] = "0"
        d = self.alert()
        self.assertFalse(d["triggered"])
        self.assertEqual(d["kind"], "disabled")


def _snap(**kw: object) -> MarketSnapshot:
    fields: dict = dict(inst_id="BTC-USDT-SWAP", name="BTC", timestamp=1.0, price=80000.0,
                        atr_14=300.0, rsi_14=40.0, trend_15m="bearish", trend_1h="bearish",
                        trend_4h="neutral", data_valid=True)
    fields.update(kw)
    return MarketSnapshot(**fields)  # type: ignore[arg-type]


def _short() -> Decision:
    return Decision(inst_id="BTC-USDT-SWAP", action="SELL_SHORT", confidence=75.0,
                    entry_price=80000.0, take_profit=78500.0, stop_loss=80700.0,
                    leverage=3, margin_usdt=50.0, reason="short")


class TestClassifyFromRealOverlay(unittest.TestCase):
    KEYS = ("KEEL_LLM_4H_MODE", "KEEL_LLM_ADX_MIN", "KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL")

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL"] = "0"

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_hard_4h_neutral_classified_4h_hard(self) -> None:
        os.environ["KEEL_LLM_4H_MODE"] = "hard"
        os.environ["KEEL_LLM_ADX_MIN"] = "0"
        out = apply_llm_edge_overlay(_short(), _snap())
        self.assertEqual(out.action, "WAIT")
        self.assertEqual(_classify(out.action, out.signal_diag), "4h_hard")

    def test_adx_veto_classified_adx(self) -> None:
        os.environ["KEEL_LLM_4H_MODE"] = "hard"
        os.environ["KEEL_LLM_ADX_MIN"] = "20"
        snap = _snap(trend_4h="bearish")
        snap.adx_14 = 12.0  # type: ignore[attr-defined]
        out = apply_llm_edge_overlay(_short(), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertEqual(_classify(out.action, out.signal_diag), "adx")

    def test_book_lock_classified_position(self) -> None:
        out = apply_llm_book_lock(_short(), [{"inst_id": "BTC-USDT-SWAP", "size": 1, "side": "short"}])
        self.assertEqual(_classify(out.action, out.signal_diag), "position_cooldown")

    def test_llm_wait_and_fire(self) -> None:
        self.assertEqual(_classify("WAIT", None), "llm_wait")
        self.assertEqual(_classify("SELL_SHORT", {"missing": []}), "fire")


class TestZeroEntrySurfaces(_TmpLedger):
    def setUp(self) -> None:
        super().setUp()
        _market_scenario(self.led, self.now)
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        self._saved_interval = os.environ.get("KEEL_CYCLE_INTERVAL_SECONDS")
        os.environ["KEEL_CYCLE_INTERVAL_SECONDS"] = str(INTERVAL)
        refresh_settings()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        if self._saved_interval is None:
            os.environ.pop("KEEL_CYCLE_INTERVAL_SECONDS", None)
        else:
            os.environ["KEEL_CYCLE_INTERVAL_SECONDS"] = self._saved_interval
        refresh_settings()
        super().tearDown()

    def test_ready_exposes_alert_without_flipping_ready(self) -> None:
        body = self.client.get("/ready").json()
        self.assertTrue(body["ready"])
        z = body["zero_entry_alert"]
        self.assertTrue(z["triggered"])
        self.assertEqual(z["kind"], "market")
        self.assertIn("行情驱动", z["message_zh"])
        self.assertIn("4h_hard", z["veto_shares"])

    def test_quality_alert_list_and_endpoint(self) -> None:
        q = self.client.get("/api/v1/stats/quality?hours=24").json()
        self.assertEqual([a["code"] for a in q["alerts"]], ["zero_entry_48h"])
        self.assertEqual(q["alerts"][0]["severity"], "warning")
        self.assertEqual(q["zero_entry_alert"]["kind"], "market")
        z = self.client.get("/api/v1/stats/zero_entry").json()
        self.assertEqual(z["kind"], "market")
        self.assertEqual(z["code"], "zero_entry_48h")

    def test_script_json_and_exit_code(self) -> None:
        env = dict(os.environ, KEEL_SKIP_DOTENV="1")
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "zero_entry_alert.py"), "--db", str(self.db),
             "--exit-code"],
            capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=60,
        )
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("行情驱动", r.stdout)

    def test_scheduler_logs_warning_throttled(self) -> None:
        from keel.worker.scheduler import KeelScheduler

        sched = KeelScheduler()
        try:
            self.assertIsNone(sched._check_zero_entry_alert(now_mono=1000.0))  # warm-up
            with self.assertLogs("keel.worker.alerts", level="WARNING") as cm:
                payload = sched._check_zero_entry_alert(now_mono=1061.0)
            self.assertEqual(payload["kind"], "market")
            self.assertIn("ZERO_ENTRY_ALERT kind=market", cm.output[0])
            with self.assertNoLogs("keel.worker.alerts", level="WARNING"):
                sched._check_zero_entry_alert(now_mono=1061.0 + 901.0)  # <1h since warn
            with self.assertLogs("keel.worker.alerts", level="WARNING"):
                sched._check_zero_entry_alert(now_mono=1061.0 + 3700.0)
        finally:
            if sched._zero_entry_ledger is not None:
                sched._zero_entry_ledger.close()
            sched._executor.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
