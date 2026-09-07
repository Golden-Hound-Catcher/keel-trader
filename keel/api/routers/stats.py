"""Read-only decision / cycle observability stats."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query

from keel.api.deps import get_ledger
from keel.api.schemas import (
    DecisionStatsResponse,
    ProbeSkipsBlock,
    QualityInstrumentStats,
    QualityShadowBlock,
    QualityStatsResponse,
    ShadowFeeModel,
    ShadowInstrumentMarkout300,
    ShadowInstrumentStats,
    ShadowMarkoutActionStats,
    ShadowMarkoutBlock,
    ShadowMarkoutHorizon,
    ShadowStatsResponse,
)

router = APIRouter()


def _fee_model(raw: dict[str, Any] | None) -> ShadowFeeModel | None:
    if not isinstance(raw, dict):
        return None
    return ShadowFeeModel(
        source=str(raw.get("source") or "fallback"),
        inst_type=str(raw.get("inst_type") or "SWAP"),
        margin=str(raw.get("margin") or "USDT"),
        level=raw.get("level"),
        maker_bps=float(raw.get("maker_bps", 2.0)),
        taker_bps=float(raw.get("taker_bps", 5.0)),
        role=str(raw.get("role") or "taker"),
        open_fee_bps=float(raw.get("open_fee_bps", 5.0)),
        round_trip_fee_bps=float(raw.get("round_trip_fee_bps", 10.0)),
        funding_note=str(raw.get("funding_note") or ""),
        funding_applied=bool(raw.get("funding_applied", False)),
    )


def _action_stats(stats: dict[str, Any]) -> ShadowMarkoutActionStats:
    return ShadowMarkoutActionStats(
        sample_count=int(stats.get("sample_count", 0)),
        avg_markout_bps=stats.get("avg_markout_bps"),
        median_markout_bps=stats.get("median_markout_bps"),
        win_rate=stats.get("win_rate"),
        avg_net_open_markout_bps=stats.get("avg_net_open_markout_bps"),
        median_net_open_markout_bps=stats.get("median_net_open_markout_bps"),
        win_rate_net_open=stats.get("win_rate_net_open"),
        avg_net_roundtrip_markout_bps=stats.get("avg_net_roundtrip_markout_bps"),
        median_net_roundtrip_markout_bps=stats.get("median_net_roundtrip_markout_bps"),
        win_rate_net_roundtrip=stats.get("win_rate_net_roundtrip"),
    )


def _markout_block(raw: dict[str, Any] | None) -> ShadowMarkoutBlock | None:
    if not isinstance(raw, dict):
        return None
    horizons_out: list[ShadowMarkoutHorizon] = []
    for h in raw.get("horizons") or []:
        if not isinstance(h, dict):
            continue
        by_action_raw = h.get("by_action") or {}
        by_action: dict[str, ShadowMarkoutActionStats] = {}
        if isinstance(by_action_raw, dict):
            for act, stats in by_action_raw.items():
                if not isinstance(stats, dict):
                    continue
                by_action[str(act)] = _action_stats(stats)
        horizons_out.append(
            ShadowMarkoutHorizon(
                horizon_seconds=int(h.get("horizon_seconds", 0)),
                sample_count=int(h.get("sample_count", 0)),
                skipped=int(h.get("skipped", 0)),
                avg_markout_bps=h.get("avg_markout_bps"),
                median_markout_bps=h.get("median_markout_bps"),
                win_rate=h.get("win_rate"),
                avg_net_open_markout_bps=h.get("avg_net_open_markout_bps"),
                median_net_open_markout_bps=h.get("median_net_open_markout_bps"),
                win_rate_net_open=h.get("win_rate_net_open"),
                avg_net_roundtrip_markout_bps=h.get("avg_net_roundtrip_markout_bps"),
                median_net_roundtrip_markout_bps=h.get("median_net_roundtrip_markout_bps"),
                win_rate_net_roundtrip=h.get("win_rate_net_roundtrip"),
                probe_sample_count=int(h.get("probe_sample_count", 0)),
                probe_avg_markout_bps=h.get("probe_avg_markout_bps"),
                probe_median_markout_bps=h.get("probe_median_markout_bps"),
                probe_win_rate=h.get("probe_win_rate"),
                probe_avg_net_open_markout_bps=h.get("probe_avg_net_open_markout_bps"),
                probe_median_net_open_markout_bps=h.get(
                    "probe_median_net_open_markout_bps"
                ),
                probe_win_rate_net_open=h.get("probe_win_rate_net_open"),
                probe_avg_net_roundtrip_markout_bps=h.get(
                    "probe_avg_net_roundtrip_markout_bps"
                ),
                probe_median_net_roundtrip_markout_bps=h.get(
                    "probe_median_net_roundtrip_markout_bps"
                ),
                probe_win_rate_net_roundtrip=h.get("probe_win_rate_net_roundtrip"),
                funding_applied_count=int(h.get("funding_applied_count", 0)),
                by_action=by_action,
            )
        )
    return ShadowMarkoutBlock(
        price_source=str(raw.get("price_source") or "factor_snapshots"),
        horizons=horizons_out,
    )


def _probe_skips(raw: dict[str, Any] | None) -> ProbeSkipsBlock | None:
    if not isinstance(raw, dict):
        return None
    by = dict(raw.get("by_skip_reason") or {})
    return ProbeSkipsBlock(
        count=int(raw.get("count", 0)),
        by_skip_reason=by,
        top_skip_reason=raw.get("top_skip_reason"),
        last_reason=raw.get("last_reason"),
        last_timestamp=raw.get("last_timestamp"),
    )



def _quality_by_instrument(raw: dict[str, Any] | None) -> dict[str, QualityInstrumentStats]:
    out: dict[str, QualityInstrumentStats] = {}
    if not isinstance(raw, dict):
        return out
    for inst_id, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        out[str(inst_id)] = QualityInstrumentStats(
            decision_count=int(payload.get("decision_count", 0)),
            wait_rate=float(payload.get("wait_rate") or 0.0),
            near_signal_rate=float(payload.get("near_signal_rate") or 0.0),
            by_action=dict(payload.get("by_action") or {}),
            market_source=dict(payload.get("market_source") or {}),
        )
    return out


def _shadow_by_instrument(raw: dict[str, Any] | None) -> dict[str, ShadowInstrumentStats]:
    out: dict[str, ShadowInstrumentStats] = {}
    if not isinstance(raw, dict):
        return out
    for inst_id, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        mk_raw = payload.get("markout_300s")
        mk = None
        if isinstance(mk_raw, dict):
            mk = ShadowInstrumentMarkout300(
                sample_count=int(mk_raw.get("sample_count", 0)),
                probe_sample_count=int(mk_raw.get("probe_sample_count", 0)),
                avg_net_roundtrip_markout_bps=mk_raw.get("avg_net_roundtrip_markout_bps"),
                win_rate_net_roundtrip=mk_raw.get("win_rate_net_roundtrip"),
                probe_avg_net_roundtrip_markout_bps=mk_raw.get(
                    "probe_avg_net_roundtrip_markout_bps"
                ),
                probe_win_rate_net_roundtrip=mk_raw.get("probe_win_rate_net_roundtrip"),
            )
        out[str(inst_id)] = ShadowInstrumentStats(
            count=int(payload.get("count", 0)),
            probe_count=int(payload.get("probe_count", 0)),
            by_action=dict(payload.get("by_action") or {}),
            by_skip_reason=dict(payload.get("by_skip_reason") or {}),
            markout_300s=mk,
        )
    return out


def _shadow_response(hours: int, raw: dict[str, Any]) -> ShadowStatsResponse:
    skips_raw = raw.get("probe_skips") if isinstance(raw.get("probe_skips"), dict) else None
    by_skip = dict(raw.get("by_skip_reason") or {})
    if not by_skip and isinstance(skips_raw, dict):
        by_skip = dict(skips_raw.get("by_skip_reason") or {})
    return ShadowStatsResponse(
        hours=hours,
        count=int(raw.get("count", 0)),
        by_action=dict(raw.get("by_action") or {}),
        by_policy=dict(raw.get("by_policy") or {}),
        probe_count=int(raw.get("probe_count", 0)),
        last_timestamp=raw.get("last_timestamp"),
        probe_skips=_probe_skips(skips_raw),
        by_skip_reason=by_skip,
        fee_model=_fee_model(raw.get("fee_model") if isinstance(raw.get("fee_model"), dict) else None),
        markout=_markout_block(raw.get("markout")),
        by_instrument=_shadow_by_instrument(
            raw.get("by_instrument") if isinstance(raw.get("by_instrument"), dict) else None
        ),
    )


@router.get("/stats/decisions", response_model=DecisionStatsResponse)
def get_decision_stats(
    hours: int = Query(default=24, ge=1, le=168),
    market_source: Literal["okx_public", "synthetic", "any"] = Query(default="any"),
) -> DecisionStatsResponse:
    """Aggregate decision quality stats over the last ``hours`` (max 168)."""
    ledger = get_ledger()
    raw = ledger.get_decision_stats(
        hours=float(hours),
        market_source=market_source,
    )
    return DecisionStatsResponse(
        hours=hours,
        decision_count=int(raw.get("decision_count", 0)),
        by_action=dict(raw.get("by_action") or {}),
        by_policy=dict(raw.get("by_policy") or {}),
        wait_rate=float(raw.get("wait_rate") or 0.0),
        risk_deny_events=int(raw.get("risk_deny_events", 0)),
        cycle_count=int(raw.get("cycle_count", 0)),
        avg_cycle_duration_ms=raw.get("avg_cycle_duration_ms"),
        market_source=str(raw.get("market_source") or market_source or "any"),
    )


@router.get("/stats/shadow", response_model=ShadowStatsResponse)
def get_shadow_stats(
    hours: int = Query(default=24, ge=1, le=168),
) -> ShadowStatsResponse:
    """Counts + markout + Q3.5 probe_skips of shadow_fill / skip events over ``hours``."""
    ledger = get_ledger()
    raw = ledger.get_shadow_stats(hours=float(hours), include_markout=True)
    return _shadow_response(hours, raw)


@router.get("/stats/shadow_markout", response_model=ShadowStatsResponse)
def get_shadow_markout_stats(
    hours: int = Query(default=24, ge=1, le=168),
) -> ShadowStatsResponse:
    """Sibling alias focused on markout; same payload as ``/stats/shadow``."""
    ledger = get_ledger()
    raw = ledger.get_shadow_markout(hours=float(hours))
    return _shadow_response(hours, raw)


@router.get("/stats/quality", response_model=QualityStatsResponse)
def get_quality_stats(
    hours: int = Query(default=24, ge=1, le=168),
) -> QualityStatsResponse:
    """Compact observation quality scorecard over the last ``hours`` (max 168)."""
    ledger = get_ledger()
    raw = ledger.get_quality_stats(hours=float(hours))
    shadow_raw = raw.get("shadow") or {}
    return QualityStatsResponse(
        hours=hours,
        market_source=dict(raw.get("market_source") or {}),
        decision_count=int(raw.get("decision_count", 0)),
        wait_rate=float(raw.get("wait_rate") or 0.0),
        by_action=dict(raw.get("by_action") or {}),
        near_signal_rate=float(raw.get("near_signal_rate") or 0.0),
        shadow=QualityShadowBlock(
            count=int(shadow_raw.get("count", 0)),
            by_action=dict(shadow_raw.get("by_action") or {}),
            by_policy=dict(shadow_raw.get("by_policy") or {}),
            probe_count=int(shadow_raw.get("probe_count", 0)),
            last_timestamp=shadow_raw.get("last_timestamp"),
        ),
        cycle_count=int(raw.get("cycle_count", 0)),
        avg_cycle_duration_ms=raw.get("avg_cycle_duration_ms"),
        by_instrument=_quality_by_instrument(
            raw.get("by_instrument") if isinstance(raw.get("by_instrument"), dict) else None
        ),
    )
