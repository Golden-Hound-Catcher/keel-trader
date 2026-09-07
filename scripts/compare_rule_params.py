#!/usr/bin/env python3
"""
Offline Q2 rule-param compare: A/B thresholds on synthetic cycles OR ledger cohort.

No network / no OKX keys. Prints action histograms + near-signal rates + missing-gate histograms; exit 0.

Synthetic (default — same paper snaps, invents market via paper cycle):

  PYTHONPATH=. python scripts/compare_rule_params.py \\
    --rsi-long-max-a 42 --rsi-short-min-a 58 --min-vol-a 1.0 \\
    --rsi-long-max-b 35 --rsi-short-min-b 65 --min-vol-b 1.2

From exported JSONL/JSON (preferred for okx_public observed decisions):

  PYTHONPATH=. python scripts/compare_rule_params.py \\
    --from-ledger /tmp/decisions.jsonl \\
    --rsi-long-max-a 42 --rsi-short-min-a 58 --min-vol-a 1.0 \\
    --rsi-long-max-b 35 --rsi-short-min-b 65 --min-vol-b 1.2

Direct from SQLite (export+replay in one step):

  PYTHONPATH=. python scripts/compare_rule_params.py \\
    --db data/keel_ledger.db --hours 48 --market-source okx_public \\
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
from keel.ledger.decision_export import (  # noqa: E402
    action_histogram,
    load_export_path,
    near_signal_rate,
    replay_rule_on_rows,
)
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


_THRESHOLD_KEYS = (
    "KEEL_RULE_RSI_LONG_MAX",
    "KEEL_RULE_RSI_SHORT_MIN",
    "KEEL_RULE_MIN_VOLUME_RATIO",
)


def _apply_thresholds(rsi_long: float, rsi_short: float, min_vol: float) -> dict[str, str | None]:
    """Set rule thresholds; return previous values for restore."""
    saved = {k: os.environ.get(k) for k in _THRESHOLD_KEYS}
    os.environ["KEEL_RULE_RSI_LONG_MAX"] = str(rsi_long)
    os.environ["KEEL_RULE_RSI_SHORT_MIN"] = str(rsi_short)
    os.environ["KEEL_RULE_MIN_VOLUME_RATIO"] = str(min_vol)
    return saved


def _restore_thresholds(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v



def missing_gate_histogram(results: list) -> dict[str, int]:
    """Count how often each gate appears in signal_diag.missing (replay or cycle)."""
    counts: Counter[str] = Counter()
    for row in results:
        if isinstance(row, dict):
            if row.get("skipped"):
                continue
            diag = row.get("signal_diag") or {}
            action = str(row.get("action") or "").upper()
        else:
            diag = getattr(row, "signal_diag", None) or {}
            action = str(getattr(row, "action", "") or "").upper()
        if not isinstance(diag, dict):
            continue
        missing = diag.get("missing")
        if not isinstance(missing, list):
            continue
        for gate in missing:
            counts[str(gate)] += 1
        if action in ("BUY_LONG", "SELL_SHORT") and not missing:
            counts["__fired__"] += 1
    return dict(counts.most_common())


def _print_missing(label: str, results: list) -> None:
    hist = missing_gate_histogram(results)
    n = sum(1 for r in results if not (isinstance(r, dict) and r.get("skipped")))
    vol_n = hist.get("volume_ok", 0)
    vol_pct = (100.0 * vol_n / n) if n else 0.0
    print(f"missing_gates[{label}]={hist} volume_ok_in_missing={vol_n}/{n} ({vol_pct:.1f}%)")


def _near_signal_rate_cycle(results: list[dict]) -> float:
    """Fraction of WAIT decisions with nearest in {long, short} (paper cycle)."""
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


def _histogram_cycle(results: list[dict]) -> dict[str, int]:
    return dict(Counter(str(r.get("action") or "UNKNOWN") for r in results))


def _run_synthetic_set(
    label: str,
    *,
    rsi_long: float,
    rsi_short: float,
    min_vol: float,
    tmp: Path,
) -> None:
    saved = _apply_thresholds(rsi_long, rsi_short, min_vol)
    try:
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
        hist = _histogram_cycle(results)
        near_rate = _near_signal_rate_cycle(results)
        print(
            f"set={label} rsi_long_max={rsi_long} rsi_short_min={rsi_short} "
            f"min_vol={min_vol} actions={hist} near_signal_rate={near_rate:.3f} "
            f"n={len(results)}"
        )
        _print_missing(label, results)
    finally:
        _restore_thresholds(saved)


def _run_ledger_set(
    label: str,
    *,
    rows: list[dict],
    rsi_long: float,
    rsi_short: float,
    min_vol: float,
) -> None:
    saved = _apply_thresholds(rsi_long, rsi_short, min_vol)
    try:
        # rule_based_decision reads env thresholds live — no refresh_settings needed
        results, replayed, skipped = replay_rule_on_rows(rows)
        hist = action_histogram(results)
        near_rate = near_signal_rate(results)
        print(
            f"set={label} rsi_long_max={rsi_long} rsi_short_min={rsi_short} "
            f"min_vol={min_vol} actions={hist} near_signal_rate={near_rate:.3f} "
            f"n={replayed} skipped_incomplete={skipped}"
        )
        _print_missing(label, results)
    finally:
        _restore_thresholds(saved)


def _parse_thresholds(args: argparse.Namespace) -> tuple[
    tuple[float, float, float], tuple[float, float, float]
]:
    a_long = args.rsi_long_max_a if args.rsi_long_max_a is not None else _env_float(
        "KEEL_RULE_RSI_LONG_MAX", 42.0
    )
    a_short = args.rsi_short_min_a if args.rsi_short_min_a is not None else _env_float(
        "KEEL_RULE_RSI_SHORT_MIN", 58.0
    )
    a_vol = args.min_vol_a if args.min_vol_a is not None else _env_float(
        "KEEL_RULE_MIN_VOLUME_RATIO", 0.5
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
    return (a_long, a_short, a_vol), (b_long, b_short, b_vol)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Offline rule threshold A vs B compare (synthetic or ledger)"
    )
    p.add_argument("--rsi-long-max-a", type=float, default=None)
    p.add_argument("--rsi-short-min-a", type=float, default=None)
    p.add_argument("--min-vol-a", type=float, default=None)
    p.add_argument("--rsi-long-max-b", type=float, default=None)
    p.add_argument("--rsi-short-min-b", type=float, default=None)
    p.add_argument("--min-vol-b", type=float, default=None)
    p.add_argument(
        "--from-ledger",
        type=Path,
        default=None,
        help="JSONL/JSON export from scripts/export_decisions.py",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite ledger path (replay observed decisions; implies ledger mode)",
    )
    p.add_argument(
        "--hours",
        type=float,
        default=48.0,
        help="Lookback hours when using --db (default 48)",
    )
    p.add_argument(
        "--market-source",
        default="okx_public",
        choices=("any", "okx_public", "synthetic"),
        help="Filter when using --db (default okx_public)",
    )
    p.add_argument("--limit", type=int, default=5000, help="Max rows when using --db")
    args = p.parse_args(argv)

    a, b = _parse_thresholds(args)

    if args.from_ledger is not None and args.db is not None:
        print("error: use either --from-ledger or --db, not both", file=sys.stderr)
        return 2

    if args.from_ledger is not None or args.db is not None:
        if args.from_ledger is not None:
            if not args.from_ledger.is_file():
                print(f"error: export not found: {args.from_ledger}", file=sys.stderr)
                return 1
            rows = load_export_path(args.from_ledger)
            source = f"file:{args.from_ledger}"
        else:
            assert args.db is not None
            if not args.db.is_file():
                print(f"error: ledger not found: {args.db}", file=sys.stderr)
                return 1
            ledger = KeelLedger(args.db)
            try:
                rows = ledger.export_decisions(
                    hours=float(args.hours),
                    market_source=args.market_source,
                    limit=int(args.limit),
                    include_factors=True,
                )
            finally:
                ledger.close()
            source = (
                f"db:{args.db} hours={args.hours} market_source={args.market_source}"
            )

        print(
            f"Keel Q2 rule-param compare (ledger replay, offline) source={source} "
            f"cohort_n={len(rows)}"
        )
        if not rows:
            print(
                "warning: empty cohort — nothing to replay "
                "(check --hours / --market-source / export path)"
            )
        _run_ledger_set("A", rows=rows, rsi_long=a[0], rsi_short=a[1], min_vol=a[2])
        _run_ledger_set("B", rows=rows, rsi_long=b[0], rsi_short=b[1], min_vol=b[2])
        print(
            "note: incomplete calculus/factor rows are skipped "
            "(see skipped_incomplete); prefer factor_snapshots join + signal_diag"
        )
        print("done")
        return 0

    print("Keel Q2 rule-param compare (paper, offline, same synthetic snaps)")
    with tempfile.TemporaryDirectory(prefix="keel-q2-rule-compare-") as tmp:
        root = Path(tmp)
        _run_synthetic_set("A", rsi_long=a[0], rsi_short=a[1], min_vol=a[2], tmp=root)
        _run_synthetic_set("B", rsi_long=b[0], rsi_short=b[1], min_vol=b[2], tmp=root)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
