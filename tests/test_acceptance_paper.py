"""P0 paper acceptance: run_paper_cycle with temp ledger (no OKX keys / CI-safe)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keel.config import refresh_settings
from keel.ledger import KeelLedger
from keel.worker.cycle import run_paper_cycle


class TestAcceptancePaper(unittest.TestCase):
    """Thin gate mirroring scripts/run_acceptance.sh without shell flakiness."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "acceptance.db"
        self._env_patch = patch.dict(
            os.environ,
            {
                "KEEL_OKX_API_KEY": "",
                "KEEL_OKX_SECRET_KEY": "",
                "KEEL_OKX_PASSPHRASE": "",
                "OKX_DEMO_API_KEY": "",
                "OKX_DEMO_SECRET_KEY": "",
                "OKX_DEMO_PASSPHRASE": "",
                "OKX_API_KEY": "",
                "OKX_SECRET_KEY": "",
                "OKX_PASSPHRASE": "",
                "KEEL_LEDGER_DB": str(self.db),
                "KEEL_KILL_SWITCH": "0",
                "KEEL_DECISION_POLICY": "rule",
                "KEEL_INSTRUMENTS": "BTC-USDT-SWAP",
            },
            clear=False,
        )
        self._env_patch.start()
        refresh_settings()
        self.ledger = KeelLedger(self.db)

    def tearDown(self) -> None:
        self.ledger.close()
        self._env_patch.stop()
        refresh_settings()
        self.temp.cleanup()

    def test_paper_cycle_summary_and_ledger_rows(self) -> None:
        summary = run_paper_cycle(
            ledger=self.ledger,
            force_paper=True,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="WAIT",
        )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary.get("mode"), "paper")
        self.assertIn("paper", str(summary.get("adapter", "")).lower())

        decisions = self.ledger.get_decisions(limit=10)
        self.assertGreaterEqual(len(decisions), 1)

        cycle = self.ledger.get_last_cycle_summary()
        self.assertIsNotNone(cycle)
        events = self.ledger.get_events(event_type="paper_cycle_complete", limit=5)
        self.assertGreaterEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
