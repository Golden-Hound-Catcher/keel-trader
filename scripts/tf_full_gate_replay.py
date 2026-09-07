#!/usr/bin/env python3
"""
E2C: offline TF+E2B full-gate fire-rate replay on local ledger snapshots.

Forces ``KEEL_RULE_VARIANT`` in-process (default ``trend_follow``) with current
E2B defaults (soft volume without RSI extreme + MACD lag). Never writes ``.env``,
never touches OKX keys, never pushes/restarts.

  PYTHONPATH=. python scripts/tf_full_gate_replay.py \
    --db data/keel_ledger.db --hours 168 --variant trend_follow --compare-mr

Markout: skipped for counterfactual replay fires — use
``scripts/full_gate_markout.py`` for live full-gate decisions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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

# Avoid dotenv pulling secrets into this process if something imports settings.
os.environ.setdefault("KEEL_SKIP_DOTENV", "1")

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from keel.ledger import KeelLedger  # noqa: E402
from keel.ledger.tf_fire_replay import (  # noqa: E402
    load_replay_rows,
    normalize_variant,
    replay_under_variant,
)


def _fmt_rate(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * x:.2f}%"


def _print_summary(label: str, summary: dict) -> None:
    print(f"=== E2C TF full-gate replay [{label}] ===")
    print(
        f"variant={summary.get('variant')} n={summary.get('n_snapshots')} "
        f"skipped_incomplete={summary.get('skipped_incomplete')} "
        f"full_gate={summary.get('full_gate_count')} "
        f"rate={_fmt_rate(summary.get('full_gate_rate'))}"
    )
    print(f"by_action={summary.get('by_action')}")
    print(
        f"near_1_missing={summary.get('near_1_missing_count')} "
        f"({_fmt_rate(summary.get('near_1_missing_rate'))}) "
        f"soft_tf={summary.get('soft_tf_count')} "
        f"macd_lag_ok={summary.get('macd_lag_ok_count')}"
    )
    print(f"top_missing_gates={summary.get('top_missing_gates')}")
    print(f"top_missing_combos={summary.get('top_missing_combos')}")
    print("--- by_instrument ---")
    for inst, info in (summary.get("by_instrument") or {}).items():
        print(
            f"  {inst}: n={info.get('n')} full_gate={info.get('full_gate')} "
            f"rate={_fmt_rate(info.get('full_gate_rate'))} "
            f"actions={info.get('by_action')} "
            f"near_1={info.get('near_1_missing')}"
        )
    mo = summary.get("markout") or {}
    if mo.get("skipped"):
        print(f"markout: skipped — {mo.get('reason')}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Offline TF+E2B full-gate fire-rate replay "
            "(forced KEEL_RULE_VARIANT in-process; no .env write)"
        )
    )
    p.add_argument(
        "--db",
        default=os.environ.get("KEEL_LEDGER_DB") or str(_ROOT / "data" / "keel_ledger.db"),
        help="SQLite ledger path (default: data/keel_ledger.db or KEEL_LEDGER_DB)",
    )
    p.add_argument("--hours", type=float, default=168.0, help="Lookback hours (default 168)")
    p.add_argument(
        "--variant",
        default="trend_follow",
        help="Forced rule variant: trend_follow (default) | mean_revert",
    )
    p.add_argument(
        "--compare-mr",
        action="store_true",
        help="Also replay mean_revert side-by-side",
    )
    p.add_argument(
        "--market-source",
        default="any",
        help="okx_public | synthetic | any (default any)",
    )
    p.add_argument(
        "--prefer",
        choices=("decisions", "factors"),
        default="decisions",
        help="Load via decisions+factor join (default) or factor_snapshots only",
    )
    p.add_argument("--limit", type=int, default=50000, help="Max rows to load")
    p.add_argument(
        "--json-out",
        default=None,
        help="Optional path to write full JSON summary",
    )
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(
            f"DB missing: {db_path} — skip smoke (no crash). "
            "Provide --db or KEEL_LEDGER_DB when ledger exists.",
            file=sys.stderr,
        )
        return 0

    variant = normalize_variant(args.variant)
    ledger = KeelLedger(db_path)
    try:
        rows = load_replay_rows(
            ledger,
            hours=float(args.hours),
            market_source=str(args.market_source),
            limit=int(args.limit),
            prefer=str(args.prefer),
        )
    finally:
        ledger.close()

    print(
        f"loaded rows={len(rows)} hours={args.hours} db={db_path} "
        f"prefer={args.prefer} market_source={args.market_source}"
    )
    if not rows:
        print("No rows in lookback — nothing to replay.")
        return 0

    _results, summary = replay_under_variant(rows, variant)
    _print_summary(variant, summary)

    payload: dict = {
        "hours": float(args.hours),
        "db": str(db_path),
        "prefer": args.prefer,
        "market_source": args.market_source,
        "primary": summary,
    }

    if args.compare_mr and variant != "mean_revert":
        _mr_results, mr_summary = replay_under_variant(rows, "mean_revert")
        _print_summary("mean_revert", mr_summary)
        payload["compare_mean_revert"] = mr_summary
        # Compact delta line
        print("--- delta TF − MR ---")
        print(
            f"full_gate_delta="
            f"{int(summary.get('full_gate_count') or 0) - int(mr_summary.get('full_gate_count') or 0)} "
            f"near_1_delta="
            f"{int(summary.get('near_1_missing_count') or 0) - int(mr_summary.get('near_1_missing_count') or 0)}"
        )

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
