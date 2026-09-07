"""
Phase R4: offline fee-aware Rule param suggestions from a ledger cohort.

Grid-search helpers + ranking, including **per-instrument** cohort filters
(multi-inst observe often shows different skip profiles). Does **not** write
``.env`` — recommend only. Fee hurdle default matches OKX Regular taker
round-trip (~10 bps).
"""
from __future__ import annotations

import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from keel.ledger.decision_export import (
    action_histogram,
    near_signal_rate,
    replay_rule_on_rows,
)

_FIRE_ACTIONS = frozenset({"BUY_LONG", "SELL_SHORT"})

# Modest default grid (Phase R4).
DEFAULT_RSI_LONG_MAX = (40.0, 42.0, 45.0, 48.0)
DEFAULT_RSI_SHORT_MIN = (52.0, 55.0, 58.0, 60.0)
DEFAULT_MIN_VOL = (0.35, 0.5, 0.7)

_THRESHOLD_KEYS = (
    "KEEL_RULE_RSI_LONG_MAX",
    "KEEL_RULE_RSI_SHORT_MIN",
    "KEEL_RULE_MIN_VOLUME_RATIO",
    "KEEL_RULE_RSI_RELAX_ENABLE",
)


@dataclass(frozen=True)
class RuleParamCombo:
    rsi_long_max: float
    rsi_short_min: float
    min_vol: float
    rsi_relax: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "rsi_long_max": self.rsi_long_max,
            "rsi_short_min": self.rsi_short_min,
            "min_vol": self.min_vol,
            "rsi_relax": self.rsi_relax,
        }


@dataclass
class ComboEval:
    """Metrics for one threshold combo on a replayed cohort."""

    combo: RuleParamCombo
    cohort_n: int
    replayed: int
    skipped: int
    actions: dict[str, int] = field(default_factory=dict)
    near_signal_rate: float = 0.0
    fire_count: int = 0
    fire_rate: float = 0.0
    fires_edge_ge_hurdle: int = 0
    edge_ge_hurdle_count: int = 0
    edge_ge_hurdle_rate: float = 0.0
    volume_ok_only_misses: int = 0
    missing_gates: dict[str, int] = field(default_factory=dict)
    over_fire_cap: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["combo"] = self.combo.as_dict()
        return d


def default_grid(*, include_rsi_relax: bool = True) -> list[RuleParamCombo]:
    """Build the modest Phase R4 search space."""
    combos: list[RuleParamCombo] = []
    relax_opts: Sequence[bool] = (True, False) if include_rsi_relax else (True,)
    for rsi_long in DEFAULT_RSI_LONG_MAX:
        for rsi_short in DEFAULT_RSI_SHORT_MIN:
            for min_vol in DEFAULT_MIN_VOL:
                for relax in relax_opts:
                    combos.append(
                        RuleParamCombo(
                            rsi_long_max=float(rsi_long),
                            rsi_short_min=float(rsi_short),
                            min_vol=float(min_vol),
                            rsi_relax=bool(relax),
                        )
                    )
    return combos


def _apply_combo(combo: RuleParamCombo) -> dict[str, str | None]:
    saved = {k: os.environ.get(k) for k in _THRESHOLD_KEYS}
    os.environ["KEEL_RULE_RSI_LONG_MAX"] = str(combo.rsi_long_max)
    os.environ["KEEL_RULE_RSI_SHORT_MIN"] = str(combo.rsi_short_min)
    os.environ["KEEL_RULE_MIN_VOLUME_RATIO"] = str(combo.min_vol)
    os.environ["KEEL_RULE_RSI_RELAX_ENABLE"] = "1" if combo.rsi_relax else "0"
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def missing_gate_histogram(results: list[dict[str, Any]]) -> dict[str, int]:
    """Count gate names in ``signal_diag.missing`` (non-skipped rows)."""
    counts: Counter[str] = Counter()
    for row in results:
        if row.get("skipped"):
            continue
        diag = row.get("signal_diag") or {}
        if not isinstance(diag, dict):
            continue
        missing = diag.get("missing")
        if not isinstance(missing, list):
            continue
        for gate in missing:
            counts[str(gate)] += 1
        action = str(row.get("action") or "").upper()
        if action in _FIRE_ACTIONS and not missing:
            counts["__fired__"] += 1
    return dict(counts.most_common())


def _edge_bps(diag: Any) -> float | None:
    if not isinstance(diag, dict):
        return None
    raw = diag.get("edge_hint_bps")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def evaluate_results(
    results: list[dict[str, Any]],
    *,
    combo: RuleParamCombo,
    replayed: int,
    skipped: int,
    hurdle_bps: float = 10.0,
    max_fire_rate: float = 0.25,
) -> ComboEval:
    """
    Derive ranking metrics from a ``replay_rule_on_rows`` result list.

    ``fires_edge_ge_hurdle`` counts full BUY/SELL fires whose ``edge_hint_bps``
    is finite and ≥ ``hurdle_bps`` (default OKX taker RT ≈ 10).
    """
    considered = [r for r in results if not r.get("skipped")]
    cohort_n = len(considered)
    hist = action_histogram(results)
    fire_count = sum(hist.get(a, 0) for a in _FIRE_ACTIONS)
    fire_rate = (fire_count / cohort_n) if cohort_n else 0.0

    fires_edge = 0
    edge_pass = 0
    vol_only = 0
    for row in considered:
        diag = row.get("signal_diag") or {}
        edge = _edge_bps(diag)
        if edge is not None and edge >= hurdle_bps:
            edge_pass += 1
        action = str(row.get("action") or "").upper()
        if action in _FIRE_ACTIONS and edge is not None and edge >= hurdle_bps:
            fires_edge += 1
        if action == "WAIT" and isinstance(diag, dict):
            missing = diag.get("missing")
            if isinstance(missing, list) and missing == ["volume_ok"]:
                vol_only += 1

    return ComboEval(
        combo=combo,
        cohort_n=cohort_n,
        replayed=replayed,
        skipped=skipped,
        actions=hist,
        near_signal_rate=near_signal_rate(results),
        fire_count=fire_count,
        fire_rate=fire_rate,
        fires_edge_ge_hurdle=fires_edge,
        edge_ge_hurdle_count=edge_pass,
        edge_ge_hurdle_rate=(edge_pass / cohort_n) if cohort_n else 0.0,
        volume_ok_only_misses=vol_only,
        missing_gates=missing_gate_histogram(results),
        over_fire_cap=fire_rate > max_fire_rate if cohort_n else False,
    )


def evaluate_combo_on_rows(
    rows: list[dict[str, Any]],
    combo: RuleParamCombo,
    *,
    hurdle_bps: float = 10.0,
    max_fire_rate: float = 0.25,
) -> ComboEval:
    """Apply ``combo`` env thresholds, replay rows, restore env, return metrics."""
    saved = _apply_combo(combo)
    try:
        results, replayed, skipped = replay_rule_on_rows(rows)
        return evaluate_results(
            results,
            combo=combo,
            replayed=replayed,
            skipped=skipped,
            hurdle_bps=hurdle_bps,
            max_fire_rate=max_fire_rate,
        )
    finally:
        _restore_env(saved)


def rank_combos(
    evals: Iterable[ComboEval],
    *,
    max_fire_rate: float = 0.25,
) -> list[ComboEval]:
    """
    Rank suggestions:

    1. Prefer combos under the fire-rate cap (≤ ``max_fire_rate`` of cohort).
    2. Maximize fires with ``edge_hint_bps ≥ hurdle``.
    3. Prefer fewer ``volume_ok``-only misses.
    4. Tie-break: fewer total fires, then looser RSI band last (stable).
    """
    items = list(evals)

    def key(ev: ComboEval) -> tuple:
        over = 1 if ev.fire_rate > max_fire_rate else 0
        c = ev.combo
        # With real fires: prefer tighter bands. With none: prefer looser
        # exploratory bands (closer to eventually clearing mid-band RSI).
        has_fire = ev.fire_count > 0 or ev.fires_edge_ge_hurdle > 0
        if has_fire:
            band = (c.rsi_long_max, -c.rsi_short_min, -c.min_vol)
        else:
            band = (-c.rsi_long_max, c.rsi_short_min, c.min_vol)
        return (
            over,
            -ev.fires_edge_ge_hurdle,
            ev.volume_ok_only_misses,
            ev.fire_count,
            *band,
            0 if c.rsi_relax else 1,
        )

    return sorted(items, key=key)


def grid_search(
    rows: list[dict[str, Any]],
    *,
    grid: Sequence[RuleParamCombo] | None = None,
    hurdle_bps: float = 10.0,
    max_fire_rate: float = 0.25,
    include_rsi_relax: bool = True,
) -> list[ComboEval]:
    """Evaluate full grid and return ranked ``ComboEval`` list."""
    combos = list(grid) if grid is not None else default_grid(
        include_rsi_relax=include_rsi_relax
    )
    evals = [
        evaluate_combo_on_rows(
            rows, c, hurdle_bps=hurdle_bps, max_fire_rate=max_fire_rate
        )
        for c in combos
    ]
    return rank_combos(evals, max_fire_rate=max_fire_rate)


def row_inst_id(row: dict[str, Any]) -> str:
    """Normalize ``inst_id`` / ``instrument`` from an export or replay row."""
    raw = row.get("inst_id") if row.get("inst_id") not in (None, "") else row.get("instrument")
    return str(raw or "").strip()


def parse_inst_id_args(values: Sequence[str] | None) -> list[str]:
    """
    Parse CLI ``--inst-id`` values: repeatable flags and/or comma-separated lists.

    Empty tokens dropped; order preserved; duplicates removed.
    """
    if not values:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        for part in str(raw).split(","):
            inst = part.strip()
            if not inst or inst in seen:
                continue
            seen.add(inst)
            out.append(inst)
    return out


def distinct_inst_ids(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Unique instrument ids in cohort order-of-first-seen, then sorted stably."""
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        iid = row_inst_id(row)
        if not iid or iid in seen:
            continue
        seen.add(iid)
        out.append(iid)
    return sorted(out)


def filter_rows_by_inst_ids(
    rows: Sequence[dict[str, Any]],
    inst_ids: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """
    Keep rows whose ``inst_id`` is in ``inst_ids``.

    ``None`` / empty ``inst_ids`` → return a shallow copy of all rows (no filter).
    """
    if not inst_ids:
        return list(rows)
    allow = {str(i).strip() for i in inst_ids if str(i).strip()}
    if not allow:
        return list(rows)
    return [r for r in rows if row_inst_id(r) in allow]


@dataclass(frozen=True)
class InstSuggestSummary:
    """Best under-cap combo for one instrument (for cross-inst comparison)."""

    inst_id: str
    cohort_n: int
    best: ComboEval | None
    fires_edge_ge_hurdle: int = 0
    fire_rate: float = 0.0
    edge_ge_hurdle_rate: float = 0.0
    volume_ok_only_misses: int = 0
    has_fee_clearing_fire: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "inst_id": self.inst_id,
            "cohort_n": self.cohort_n,
            "fires_edge_ge_hurdle": self.fires_edge_ge_hurdle,
            "fire_rate": self.fire_rate,
            "edge_ge_hurdle_rate": self.edge_ge_hurdle_rate,
            "volume_ok_only_misses": self.volume_ok_only_misses,
            "has_fee_clearing_fire": self.has_fee_clearing_fire,
            "best": self.best.as_dict() if self.best is not None else None,
        }


def summarize_inst_best(
    inst_id: str,
    ranked: Sequence[ComboEval],
    *,
    max_fire_rate: float = 0.25,
) -> InstSuggestSummary:
    """Pick the best under-cap eval for ``inst_id`` (or top overall if none)."""
    under = [e for e in ranked if not e.over_fire_cap]
    pool = under if under else list(ranked)
    best = pool[0] if pool else None
    if best is None:
        return InstSuggestSummary(inst_id=inst_id, cohort_n=0, best=None)
    return InstSuggestSummary(
        inst_id=inst_id,
        cohort_n=best.cohort_n,
        best=best,
        fires_edge_ge_hurdle=best.fires_edge_ge_hurdle,
        fire_rate=best.fire_rate,
        edge_ge_hurdle_rate=best.edge_ge_hurdle_rate,
        volume_ok_only_misses=best.volume_ok_only_misses,
        has_fee_clearing_fire=best.fires_edge_ge_hurdle > 0
        and best.fire_rate <= max_fire_rate,
    )


def rank_instruments_by_fee_clearing(
    summaries: Sequence[InstSuggestSummary],
) -> list[InstSuggestSummary]:
    """
    Rank symbols by closeness to fee-clearing fires.

    Prefer: fee-clearing under-cap fires → higher edge-hurdle fire count →
    higher edge_ge_hurdle_rate → fewer volume_ok-only misses → larger cohort.
    """

    def key(s: InstSuggestSummary) -> tuple:
        return (
            0 if s.has_fee_clearing_fire else 1,
            -s.fires_edge_ge_hurdle,
            -s.edge_ge_hurdle_rate,
            s.volume_ok_only_misses,
            -s.cohort_n,
            s.inst_id,
        )

    return sorted(summaries, key=key)


def grid_search_per_instrument(
    rows: list[dict[str, Any]],
    *,
    inst_ids: Sequence[str] | None = None,
    grid: Sequence[RuleParamCombo] | None = None,
    hurdle_bps: float = 10.0,
    max_fire_rate: float = 0.25,
    include_rsi_relax: bool = True,
) -> dict[str, list[ComboEval]]:
    """
    Run ``grid_search`` independently on each instrument's filtered cohort.

    When ``inst_ids`` is None/empty, discovers distinct ids from ``rows``.
    Instruments with an empty filtered cohort still appear with an empty list.
    """
    targets = list(inst_ids) if inst_ids else distinct_inst_ids(rows)
    out: dict[str, list[ComboEval]] = {}
    for iid in targets:
        subset = filter_rows_by_inst_ids(rows, [iid])
        if not subset:
            out[iid] = []
            continue
        out[iid] = grid_search(
            subset,
            grid=grid,
            hurdle_bps=hurdle_bps,
            max_fire_rate=max_fire_rate,
            include_rsi_relax=include_rsi_relax,
        )
    return out
