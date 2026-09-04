"""Settings: KEEL_INSTRUMENTS parse + InstrumentPool.from_ids + cycle/config wiring."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.api.deps import set_ledger_path_override
from keel.config import parse_instruments, refresh_settings
from keel.domain.instruments import (
    DEFAULT_CRYPTO_INSTRUMENTS,
    InstrumentPool,
)
from keel.ledger import KeelLedger
from keel.worker.cycle import run_paper_cycle


class TestParseInstruments(unittest.TestCase):
    def test_empty_falls_back_to_defaults(self):
        expected = tuple(i.inst_id for i in DEFAULT_CRYPTO_INSTRUMENTS)
        self.assertEqual(parse_instruments(""), expected)
        self.assertEqual(parse_instruments(None), expected)
        self.assertEqual(parse_instruments("  , , "), expected)
        self.assertEqual(parse_instruments([]), expected)

    def test_strip_dedupe_preserve_order(self):
        self.assertEqual(
            parse_instruments(" BTC-USDT-SWAP , ETH-USDT-SWAP, BTC-USDT-SWAP "),
            ("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        )
        self.assertEqual(
            parse_instruments(["ETH-USDT-SWAP", "", "SOL-USDT-SWAP", "ETH-USDT-SWAP"]),
            ("ETH-USDT-SWAP", "SOL-USDT-SWAP"),
        )


class TestInstrumentPoolFromIds(unittest.TestCase):
    def test_known_ids_reuse_default_specs(self):
        btc = next(i for i in DEFAULT_CRYPTO_INSTRUMENTS if i.inst_id == "BTC-USDT-SWAP")
        pool = InstrumentPool.from_ids(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        got = pool.get("BTC-USDT-SWAP")
        self.assertIsNotNone(got)
        self.assertEqual(got.contract_value, btc.contract_value)
        self.assertEqual(got.price_precision, btc.price_precision)
        self.assertEqual([i.inst_id for i in pool.all()], ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

    def test_unknown_id_builds_from_okx_swap(self):
        pool = InstrumentPool.from_ids(["XYZ-USDT-SWAP"])
        inst = pool.get("XYZ-USDT-SWAP")
        self.assertIsNotNone(inst)
        self.assertEqual(inst.name, "XYZ")
        self.assertEqual(inst.inst_id, "XYZ-USDT-SWAP")

    def test_empty_ids_default_pool(self):
        pool = InstrumentPool.from_ids([])
        self.assertEqual(len(pool), len(DEFAULT_CRYPTO_INSTRUMENTS))


class TestInstrumentsEnvSettings(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("KEEL_INSTRUMENTS", None)
        refresh_settings()

    def test_default_when_unset(self):
        os.environ.pop("KEEL_INSTRUMENTS", None)
        s = refresh_settings()
        expected = tuple(i.inst_id for i in DEFAULT_CRYPTO_INSTRUMENTS)
        self.assertEqual(s.instruments, expected)

    def test_parse_custom_env(self):
        os.environ["KEEL_INSTRUMENTS"] = "BTC-USDT-SWAP, ETH-USDT-SWAP"
        s = refresh_settings()
        self.assertEqual(s.instruments, ("BTC-USDT-SWAP", "ETH-USDT-SWAP"))


class TestCycleUsesSettingsInstruments(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("KEEL_INSTRUMENTS", None)
        refresh_settings()

    def test_cycle_instrument_count_follows_settings(self):
        os.environ["KEEL_INSTRUMENTS"] = "BTC-USDT-SWAP,ETH-USDT-SWAP"
        refresh_settings()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cycle.db"
            ledger = KeelLedger(db)
            try:
                summary = run_paper_cycle(ledger=ledger, force_paper=True)
                self.assertEqual(summary["instruments"], 2)
                ids = [r["inst_id"] for r in summary["results"]]
                self.assertEqual(ids, ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
            finally:
                ledger.close()


class TestConfigExposesSettingsInstrumentsAndPolicy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "cfg.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        os.environ["KEEL_INSTRUMENTS"] = "BTC-USDT-SWAP,ETH-USDT-SWAP"
        os.environ["KEEL_DECISION_POLICY"] = "stub"
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.ledger.close()
        set_ledger_path_override(None)
        for key in ("KEEL_LEDGER_DB", "KEEL_INSTRUMENTS", "KEEL_DECISION_POLICY"):
            os.environ.pop(key, None)
        refresh_settings()
        self.temp.cleanup()

    def test_config_and_status_share_decision_policy_and_instruments(self):
        cfg = self.client.get("/api/v1/config").json()
        st = self.client.get("/api/v1/status").json()
        self.assertEqual(cfg["instruments"], ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        self.assertEqual(cfg["decision_policy"], "stub")
        self.assertEqual(st["decision_policy"], "stub")
        self.assertEqual(cfg["decision_policy"], st["decision_policy"])


if __name__ == "__main__":
    unittest.main()
