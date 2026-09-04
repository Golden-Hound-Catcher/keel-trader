#!/usr/bin/env python3
"""
Offline P2 policy compare: run one paper cycle each for stub and rule.

Forces paper + temp ledger; no OKX keys / no network required.
Prints action histogram per policy and exits 0.

Usage (from repo root):
  PYTHONPATH=. python scripts/compare_policies_paper.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

# Clear OKX credentials so factory never picks live REST.
for key in (
    "KEEL_OKX_API_KEY",
    "KEEL_OKX_SECRET_KEY",
    "KEEL_OKX_PASSPHRASE",
    "OKX_DEMO_API_KEY",
    "OKX_DEMO_SECRET_KEY",
    "OKX_DEMO_PASSPHRASE",
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_PASSPHRASE",
):
    os.environ.pop(key, None)
    os.environ[key] = ""

# Repo root on sys.path when invoked as a script path.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from keel.config import refresh_settings  # noqa: E402
from keel.ledger import KeelLedger  # noqa: E402
from keel.policy import build_decision_policy  # noqa: E402
from keel.worker.cycle import run_paper_cycle  # noqa: E402

refresh_settings()

POLICIES = ("stub", "rule")
INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


def _histogram(results: list[dict]) -> dict[str, int]:
    return dict(Counter(str(r.get("action") or "UNKNOWN") for r in results))


def main() -> int:
    print("Keel P2 policy compare (paper, offline)")
    with tempfile.TemporaryDirectory(prefix="keel-p2-compare-") as tmp:
        for name in POLICIES:
            db = Path(tmp) / f"{name}.db"
            os.environ["KEEL_DECISION_POLICY"] = name
            refresh_settings()
            ledger = KeelLedger(db)
            try:
                policy = build_decision_policy(name=name)
                summary = run_paper_cycle(
                    ledger=ledger,
                    policy=policy,
                    force_paper=True,
                    instrument_ids=list(INSTRUMENTS),
                )
            finally:
                ledger.close()
            hist = _histogram(summary.get("results") or [])
            print(f"policy={name} mode={summary.get('mode')} actions={hist}")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
