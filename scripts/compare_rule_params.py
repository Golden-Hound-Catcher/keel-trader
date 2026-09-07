#!/usr/bin/env python3
"""
Offline Q2 rule-param compare: same synthetic snapshots, two threshold sets.

No network / no OKX keys. Prints action histograms + near-signal rates; exit 0.

Thresholds via CLI (preferred) or env A/B pairs:

  PYTHONPATH=. python scripts/compare_rule_params.py \\
    --rsi-long-max-a 42 --rsi-short-min-a 58 --min-vol-a 1.0 \\
    --rsi-long-max-b 35 --rsi-short-min-b 65 --min-vol-b 1.2

Env fallback (when CLI omitted): KEEL_RULE_* for A; KEEL_RULE_*_B for B.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

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

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from keel.config import refresh_settings  # noqa: E402
from keel.ledger import KeelLedger  # noqa: E402
from keel.policy import RuleDecisionPolicy  # noqa: E402
from keel.worker.cycle import run_paper_cycle  # noqa: E402

INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]


def _env_float(key: str, default: float) -> float:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _apply_thresholds(rsi_long: float, rsi_short: float, min_vol: float) -> None:
    os.environ["KEEL_RULE_RSI_LONG_MAX"] = str(rsi_long)
    os.environ["KEEL_RULE_RSI_SHORT_MIN"] = str(rsi_short)
    os.environ["KEEL_RULE_MIN_VOLUME_RATIO"] = str(min_vol)


def _near_signal_rate(results: list[dict]) -> float:
    """Fraction of WAIT decisions with nearest in {long, short}."""
    if not results:
        return 0.0
    near = 0
    for row in results:
        action = str(row.get("action") or "").upper()
        if action != "WAIT":
            continue
        diag = row.get("signal_diag") or {}
        if not isinstance(diag, dict):
            continue
        nearest = str(diag.get("nearest") or "").lower()
        if nearest in ("long", "short"):
            near += 1
    return near / len(results)


def _histogram(results: list[dict]) -> dict[str, int]:
    return dict(Counter(str(r.get("action") or "UNKNOWN") for r in results))


def _run_set(
    label: str,
    *,
    rsi_long: float,
    rsi_short: float,
    min_vol: float,
    tmp: Path,
) -> None:
    _apply_thresholds(rsi_long, rsi_short, min_vol)
    refresh_settings()
    db = tmp / f"{label}.db"
    ledger = KeelLedger(db)
    try:
        summary = run_paper_cycle(
            ledger=ledger,
            policy=RuleDecisionPolicy(),
            force_paper=True,
            instrument_ids=list(INSTRUMENTS),
        )
    finally:
        ledger.close()
    results = list(summary.get("results") or [])
    hist = _histogram(results)
    near_rate = _near_signal_rate(results)
    print(
        f"set={label} rsi_long_max={rsi_long} rsi_short_min={rsi_short} "
        f"min_vol={min_vol} actions={hist} near_signal_rate={near_rate:.3f} "
        f"n={len(results)}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Offline rule threshold A vs B compare")
    p.add_argument("--rsi-long-max-a", type=float, default=None)
    p.add_argument("--rsi-short-min-a", type=float, default=None)
    p.add_argument("--min-vol-a", type=float, default=None)
    p.add_argument("--rsi-long-max-b", type=float, default=None)
    p.add_argument("--rsi-short-min-b", type=float, default=None)
    p.add_argument("--min-vol-b", type=float, default=None)
    args = p.parse_args(argv)

    a_long = args.rsi_long_max_a if args.rsi_long_max_a is not None else _env_float(
        "KEEL_RULE_RSI_LONG_MAX", 42.0
    )
    a_short = args.rsi_short_min_a if args.rsi_short_min_a is not None else _env_float(
        "KEEL_RULE_RSI_SHORT_MIN", 58.0
    )
    a_vol = args.min_vol_a if args.min_vol_a is not None else _env_float(
        "KEEL_RULE_MIN_VOLUME_RATIO", 1.0
    )
    b_long = args.rsi_long_max_b if args.rsi_long_max_b is not None else _env_float(
        "KEEL_RULE_RSI_LONG_MAX_B", 35.0
    )
    b_short = args.rsi_short_min_b if args.rsi_short_min_b is not None else _env_float(
        "KEEL_RULE_RSI_SHORT_MIN_B", 65.0
    )
    b_vol = args.min_vol_b if args.min_vol_b is not None else _env_float(
        "KEEL_RULE_MIN_VOLUME_RATIO_B", 1.2
    )

    print("Keel Q2 rule-param compare (paper, offline, same synthetic snaps)")
    with tempfile.TemporaryDirectory(prefix="keel-q2-rule-compare-") as tmp:
        root = Path(tmp)
        _run_set("A", rsi_long=a_long, rsi_short=a_short, min_vol=a_vol, tmp=root)
        _run_set("B", rsi_long=b_long, rsi_short=b_short, min_vol=b_vol, tmp=root)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
