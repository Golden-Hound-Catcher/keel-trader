"""Phase R4: fee-aware rule param suggest ranking + script smoke."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from keel.domain.records import DecisionRecord, FactorSnapshot
from keel.ledger import KeelLedger
from keel.ledger.rule_suggest import (
    ComboEval,
    RuleParamCombo,
    default_grid,
    evaluate_results,
    grid_search,
    rank_combos,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _clear_okx_env(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    for k in (
        "KEEL_OKX_API_KEY",
        "KEEL_OKX_SECRET_KEY",
        "KEEL_OKX_PASSPHRASE",
        "OKX_API_KEY",
        "OKX_SECRET_KEY",
        "OKX_PASSPHRASE",
        "OKX_DEMO_API_KEY",
        "OKX_DEMO_SECRET_KEY",
        "OKX_DEMO_PASSPHRASE",
    ):
        out[k] = ""
    out["PYTHONPATH"] = str(REPO_ROOT)
    out["KEEL_SKIP_DOTENV"] = "1"
    return out


def _fake_result(
    *,
    action: str,
    edge: float | None,
    missing: list[str] | None = None,
    skipped: bool = False,
) -> dict:
    if skipped:
        return {
            "inst_id": "BTC-USDT-SWAP",
            "action": None,
            "signal_diag": None,
            "skipped": True,
        }
    diag = {
        "nearest": "long" if action == "WAIT" else action.lower(),
        "missing": list(missing or []),
    }
    if edge is not None:
        diag["edge_hint_bps"] = edge
    return {
        "inst_id": "BTC-USDT-SWAP",
        "action": action,
        "signal_diag": diag,
        "skipped": False,
    }


class TestRankCombos(unittest.TestCase):
    def _eval(
        self,
        *,
        fires_edge: int,
        fire_count: int,
        cohort_n: int,
        vol_only: int,
        rsi_long: float = 45.0,
        rsi_short: float = 55.0,
        min_vol: float = 0.5,
        rsi_relax: bool = True,
    ) -> ComboEval:
        fire_rate = fire_count / cohort_n if cohort_n else 0.0
        return ComboEval(
            combo=RuleParamCombo(rsi_long, rsi_short, min_vol, rsi_relax),
            cohort_n=cohort_n,
            replayed=cohort_n,
            skipped=0,
            actions={"BUY_LONG": fire_count, "WAIT": cohort_n - fire_count},
            fire_count=fire_count,
            fire_rate=fire_rate,
            fires_edge_ge_hurdle=fires_edge,
            edge_ge_hurdle_count=fires_edge,
            edge_ge_hurdle_rate=fires_edge / cohort_n if cohort_n else 0.0,
            volume_ok_only_misses=vol_only,
            over_fire_cap=fire_rate > 0.25,
        )

    def test_rank_prefers_edge_fires_under_cap(self):
        # A: 2 edge fires, 10% fire rate
        a = self._eval(fires_edge=2, fire_count=2, cohort_n=20, vol_only=5)
        # B: 5 edge fires but 50% fire rate (over cap)
        b = self._eval(
            fires_edge=5,
            fire_count=10,
            cohort_n=20,
            vol_only=0,
            rsi_long=48.0,
            rsi_short=52.0,
        )
        # C: 1 edge fire, fewer vol-only misses than A
        c = self._eval(
            fires_edge=1,
            fire_count=1,
            cohort_n=20,
            vol_only=1,
            rsi_long=42.0,
        )
        ranked = rank_combos([b, c, a], max_fire_rate=0.25)
        self.assertEqual(ranked[0].combo.rsi_long_max, 45.0)  # A
        self.assertEqual(ranked[1].combo.rsi_long_max, 42.0)  # C
        self.assertTrue(ranked[-1].over_fire_cap)  # B last

    def test_rank_tiebreak_fewer_volume_ok_only(self):
        a = self._eval(
            fires_edge=2, fire_count=2, cohort_n=20, vol_only=8, rsi_long=45.0
        )
        b = self._eval(
            fires_edge=2, fire_count=2, cohort_n=20, vol_only=1, rsi_long=42.0
        )
        ranked = rank_combos([a, b], max_fire_rate=0.25)
        self.assertEqual(ranked[0].combo.rsi_long_max, 42.0)
        self.assertEqual(ranked[0].volume_ok_only_misses, 1)


class TestEvaluateResultsSynthetic(unittest.TestCase):
    def test_tiny_cohort_metrics(self):
        # 4 rows: 1 fire with edge 12, 1 fire with edge 5, 1 WAIT vol-only, 1 WAIT other
        results = [
            _fake_result(action="BUY_LONG", edge=12.0, missing=[]),
            _fake_result(action="SELL_SHORT", edge=5.0, missing=[]),
            _fake_result(action="WAIT", edge=3.0, missing=["volume_ok"]),
            _fake_result(action="WAIT", edge=11.0, missing=["rsi_short_ok"]),
            _fake_result(action="WAIT", edge=None, missing=["volume_ok"], skipped=True),
        ]
        combo = RuleParamCombo(45.0, 55.0, 0.5, True)
        ev = evaluate_results(
            results,
            combo=combo,
            replayed=4,
            skipped=1,
            hurdle_bps=10.0,
            max_fire_rate=0.25,
        )
        self.assertEqual(ev.cohort_n, 4)
        self.assertEqual(ev.fire_count, 2)
        self.assertEqual(ev.fires_edge_ge_hurdle, 1)  # only BUY_LONG @12
        self.assertEqual(ev.edge_ge_hurdle_count, 2)  # BUY 12 + WAIT 11
        self.assertEqual(ev.volume_ok_only_misses, 1)
        self.assertTrue(ev.over_fire_cap)  # 2/4 = 50%
        self.assertIn("volume_ok", ev.missing_gates)


class TestGridSearchTinyReplay(unittest.TestCase):
    """End-to-end grid on a tiny replayable factor row (no DB)."""

    def test_grid_finds_fire_without_blind_short40(self):
        # Near-long: RSI 44, all other long gates pass — fires when long_max≥45
        # or rsi_relax with relax_long_max 48.
        row = {
            "inst_id": "BTC-USDT-SWAP",
            "timestamp": time.time(),
            "action": "WAIT",
            "factors": {
                "price": 50000.0,
                "atr_14": 500.0,  # atr_bps=100 → edge_hint can clear 10bps
                "rsi_14": 44.0,
                "ema_9": 50100.0,
                "ema_21": 50000.0,
                "macd_histogram": 1.0,
                "trend_15m": "bullish",
                "volume_ratio": 0.8,
                "data_valid": True,
            },
            "calculus_data": {"market_source": "okx_public"},
            "replayable": True,
        }
        # Tiny grid: tight vs loose long max
        grid = [
            RuleParamCombo(40.0, 60.0, 0.5, False),  # no fire (rsi 44 > 40, relax off)
            RuleParamCombo(45.0, 55.0, 0.5, True),  # fire
            RuleParamCombo(48.0, 52.0, 0.35, True),  # fire, looser
        ]
        # Pad cohort so 1 fire is under 25% cap
        rows = [row] + [
            {
                **row,
                "inst_id": f"PAD-{i}",
                "factors": {
                    **row["factors"],
                    "rsi_14": 50.0,
                    "trend_15m": "neutral",
                    "macd_histogram": 0.0,
                    "volume_ratio": 0.1,
                },
            }
            for i in range(7)
        ]
        ranked = grid_search(
            rows, grid=grid, hurdle_bps=10.0, max_fire_rate=0.25
        )
        self.assertEqual(len(ranked), 3)
        # Best should be a firing combo under cap
        best = ranked[0]
        self.assertGreaterEqual(best.fire_count, 1)
        self.assertFalse(best.over_fire_cap)
        self.assertNotEqual(
            (best.combo.rsi_short_min, best.combo.rsi_long_max),
            (40.0, 40.0),
        )

    def test_default_grid_size(self):
        g = default_grid(include_rsi_relax=True)
        # 4 * 4 * 3 * 2 = 96
        self.assertEqual(len(g), 96)
        g2 = default_grid(include_rsi_relax=False)
        self.assertEqual(len(g2), 48)


class TestSuggestScript(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "ledger.db"
        self.ledger = KeelLedger(self.db_path)
        ts = time.time()
        self.ledger.record_factor_snapshot(
            FactorSnapshot(
                timestamp=ts,
                inst_id="BTC-USDT-SWAP",
                price=50000.0,
                rsi_14=44.0,
                ema_9=50100.0,
                ema_21=50000.0,
                atr_14=500.0,
                macd_histogram=1.0,
                trend_15m="bullish",
                volume_ratio=0.8,
                payload={"data_valid": True, "data_quality_reason": "okx_public"},
            )
        )
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=ts,
                inst_id="BTC-USDT-SWAP",
                action="WAIT",
                policy_name="rule",
                calculus_data={
                    "market_source": "okx_public",
                    "rsi_14": 44.0,
                    "trend_15m": "bullish",
                    "signal_diag": {
                        "data_valid": True,
                        "rsi_14": 44.0,
                        "volume_ratio": 0.8,
                        "ema_9": 50100.0,
                        "ema_21": 50000.0,
                        "macd_histogram": 1.0,
                        "trend_15m": "bullish",
                        "nearest": "long",
                        "missing": ["rsi_long_ok"],
                    },
                },
            )
        )
        self.ledger.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_script_db_and_json_out(self):
        script = REPO_ROOT / "scripts" / "suggest_rule_params.py"
        out = Path(self.temp_dir) / "suggest.json"
        env = _clear_okx_env(os.environ)
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--db",
                str(self.db_path),
                "--hours",
                "1",
                "--market-source",
                "okx_public",
                "--hurdle-bps",
                "10",
                "--top",
                "3",
                "--no-rsi-relax-grid",
                "--out",
                str(out),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        self.assertIn("suggest_rule_params", proc.stdout)
        self.assertIn("done", proc.stdout)
        self.assertIn("no .env write", proc.stdout)
        self.assertTrue(out.is_file())
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["cohort_n"], 1)
        self.assertGreaterEqual(len(payload["top"]), 1)
        self.assertIn("combo", payload["top"][0])


if __name__ == "__main__":
    unittest.main()
