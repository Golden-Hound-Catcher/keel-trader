"""
Read-only arming checklist (Q1) + economic acceptance gates (S1) + first-live (S2).

Reports whether it is *safe for the operator to clear* KEEL_KILL_SWITCH —
without ever writing env or placing orders. ready_to_arm does not flip the
kill-switch; the operator must still set KEEL_KILL_SWITCH=0 manually.

S1 adds read-only economic gates on shadow markout evidence so ready_to_arm
cannot go green without sufficient fee-aware sample quality.

S2 productizes a first-live Stage T gate checklist (`build_first_live`) answering
"can I open one minimum live order?" via status.first_live — still never
auto-clears kill or places orders.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Soft warning when optional equity is known and very small (Jo-scale live).
_TINY_EQUITY_USDT = 50.0

_SHADOW_REHEARSAL_MSG = "no recent shadow_fill rehearsal"

# S1 economic blockers (stable ids for Monitor / ops).
INSUFFICIENT_SHADOW_MARKOUT_SAMPLE = "insufficient_shadow_markout_sample"
PROBE_WIN_RATE_BELOW = "probe_win_rate_net_roundtrip_below_threshold"
AVG_NET_RT_BELOW = "avg_net_roundtrip_markout_bps_below_threshold"
# E3: prefer full-gate markout metrics when fire count reaches this threshold.
FULL_GATE_ECON_PREFER_MIN = 20
FULL_GATE_WIN_RATE_BELOW = "full_gate_win_rate_net_roundtrip_below_threshold"
# F1: FG economic gates use post_e31 markout only; never pre_e31 ~24% spray.
INSUFFICIENT_POST_E31_FULL_GATE_SAMPLE = "insufficient_post_e31_full_gate_sample"


@dataclass(frozen=True)
class ArmingReport:
    """Result of evaluate_arming — echoed on GET /api/v1/status as ``arming``."""

    ready_to_arm: bool
    kill_switch: bool
    capability: str
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    economic: dict[str, Any] | None = None


def _resolve_ledger(
    ledger: Any | None,
    ledger_path: str | Path | None,
    settings: Any,
) -> tuple[Any | None, bool]:
    """Return (ledger, owned). owned=True when we opened it and caller should close."""
    if ledger is not None:
        return ledger, False
    path = ledger_path
    if path is None and settings is not None:
        path = getattr(settings, "ledger_path", None) or getattr(settings, "ledger_db", None)
    if not path:
        return None, False
    try:
        p = Path(path)
        if not p.is_file():
            return None, False
        from keel.ledger import KeelLedger

        return KeelLedger(p), True
    except Exception:
        return None, False


def _has_recent_shadow_fill(ledger: Any, hours: float) -> bool:
    """True when ledger has a shadow_fill event within the last ``hours``.

    Includes Q3 near-probe fills (policy=shadow_near_probe) as well as
    forced/manual shadow fills — both are valid rehearsal evidence.
    """
    if ledger is None or hours <= 0:
        return False
    try:
        cutoff = time.time() - float(hours) * 3600.0
        events = ledger.get_events(event_type="shadow_fill", limit=100)
    except Exception:
        return False
    for ev in events or []:
        ts = getattr(ev, "timestamp", None)
        if ts is None and isinstance(ev, dict):
            ts = ev.get("timestamp")
        try:
            if ts is not None and float(ts) >= cutoff:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _horizon_row(markout: dict[str, Any] | None, horizon_seconds: int) -> dict[str, Any]:
    """Pick markout horizon row matching ``horizon_seconds`` (empty dict if missing)."""
    if not markout:
        return {}
    for row in markout.get("horizons") or []:
        try:
            if int(row.get("horizon_seconds", -1)) == int(horizon_seconds):
                return dict(row)
        except (TypeError, ValueError):
            continue
    return {}



def _economic_by_instrument(
    raw: dict[str, Any] | None,
    *,
    horizon_seconds: int,
) -> dict[str, dict[str, Any]]:
    """Compact per-inst economic snapshots for diagnosis (gate stays aggregate)."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    by_inst = raw.get("by_instrument")
    if not isinstance(by_inst, dict):
        return out
    for inst_id, payload in by_inst.items():
        if not isinstance(payload, dict):
            continue
        mk = payload.get("markout_300s") if int(horizon_seconds) == 300 else None
        if not isinstance(mk, dict):
            # Fall back: if horizon != 300 or missing compact block, still surface counts.
            mk = payload.get("markout_300s") if isinstance(payload.get("markout_300s"), dict) else {}
        out[str(inst_id)] = {
            "fill_count": int(payload.get("count") or 0),
            "probe_count": int(payload.get("probe_count") or 0),
            "sample_count": int(mk.get("sample_count") or 0) if isinstance(mk, dict) else 0,
            "probe_sample_count": int(mk.get("probe_sample_count") or 0)
            if isinstance(mk, dict)
            else 0,
            "avg_net_roundtrip_markout_bps": (
                mk.get("avg_net_roundtrip_markout_bps") if isinstance(mk, dict) else None
            ),
            "win_rate_net_roundtrip": (
                mk.get("win_rate_net_roundtrip") if isinstance(mk, dict) else None
            ),
            "probe_win_rate_net_roundtrip": (
                mk.get("probe_win_rate_net_roundtrip") if isinstance(mk, dict) else None
            ),
            "probe_avg_net_roundtrip_markout_bps": (
                mk.get("probe_avg_net_roundtrip_markout_bps")
                if isinstance(mk, dict)
                else None
            ),
            "by_skip_reason": dict(payload.get("by_skip_reason") or {}),
        }
    return out


def _fetch_markout_stats(
    ledger: Any,
    *,
    hours: float,
    horizon_seconds: int,
    markout_stats: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return shadow markout aggregate dict, or None when unavailable."""
    if markout_stats is not None:
        return markout_stats
    if ledger is None:
        return None
    getter = getattr(ledger, "get_shadow_markout", None)
    if not callable(getter):
        return None
    try:
        return getter(hours=float(hours), horizons=(int(horizon_seconds),))
    except TypeError:
        # Older fakes / stubs may only accept hours=.
        try:
            return getter(hours=float(hours))
        except Exception:
            return None
    except Exception:
        return None


def evaluate_economic_gates(
    settings: Any,
    *,
    ledger: Any | None = None,
    markout_stats: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """
    Read-only economic acceptance gates for arming (Stage S1).

    Insufficient shadow/probe markout sample is always a **blocker** (never a
    pass). Probe skips dominated by ``below_hurdle`` are informational only
    and do not block by themselves.

    Returns ``(blockers, economic_summary)``.
    """
    enabled = bool(getattr(settings, "arming_econ_enabled", True))
    hours = float(
        getattr(settings, "arming_econ_hours", None)
        or getattr(settings, "arming_shadow_hours", 24.0)
        or 24.0
    )
    min_fills = int(getattr(settings, "arming_econ_min_fills", 10) or 10)
    min_probe_fills = int(getattr(settings, "arming_econ_min_probe_fills", 5) or 5)
    min_sample = int(getattr(settings, "arming_econ_min_markout_sample", 5) or 5)
    horizon = int(getattr(settings, "arming_econ_markout_horizon_seconds", 300) or 300)
    min_win = float(getattr(settings, "arming_econ_min_probe_win_rate_net_rt", 0.55) or 0.55)
    min_avg_net = float(getattr(settings, "arming_econ_min_avg_net_rt_bps", 0.0) or 0.0)

    summary: dict[str, Any] = {
        "enabled": enabled,
        "hours": hours,
        "horizon_seconds": horizon,
        "min_fills": min_fills,
        "min_probe_fills": min_probe_fills,
        "min_markout_sample": min_sample,
        "min_probe_win_rate_net_rt": min_win,
        "min_avg_net_rt_bps": min_avg_net,
        "fill_count": 0,
        "probe_count": 0,
        "sample_count": 0,
        "probe_sample_count": 0,
        "probe_win_rate_net_roundtrip": None,
        "win_rate_net_roundtrip": None,
        "avg_net_roundtrip_markout_bps": None,
        "fills_ok": False,
        "sample_ok": False,
        "passed": False,
        "by_skip_reason": {},
        "by_instrument": {},
        "economic_sample_source": "insufficient",
        "full_gate_fires": 0,
        "full_gate_fires_post_e31": 0,
        "full_gate_fires_pre_e31": 0,
        "full_gate_sample_count": 0,
        "full_gate_win_rate_net_roundtrip": None,
        "full_gate_avg_net_roundtrip_markout_bps": None,
        "full_gate_frac_clear_net_rt_hurdle": None,
        "full_gate_cohort_used": None,
        "note": (
            "Kill-switch is never auto-cleared; economic gates are read-only. "
            "Probe skips dominated by below_hurdle do not block alone. "
            "by_instrument is diagnostic only; overall gate remains aggregate. "
            "E3/F1: when full_gate_fires≥20 prefer fee-aware post_e31 (strict_tf) "
            "5m netRT markout only — never pre_e31 spray. Need post_e31 markout "
            "sample ≥ min_markout_sample (default 5); else "
            "insufficient_post_e31_full_gate_sample. Prefer threshold remains "
            "FULL_GATE_ECON_PREFER_MIN=20 on total FG fires. "
            "E0 freeze — no near_probe / no kill clear."
        ),
    }

    if not enabled:
        summary["passed"] = True
        summary["note"] = "Economic gates disabled (KEEL_ARMING_ECON_ENABLED=0)."
        return [], summary

    raw = _fetch_markout_stats(
        ledger,
        hours=hours,
        horizon_seconds=horizon,
        markout_stats=markout_stats,
    )
    blockers: list[str] = []

    if raw is None:
        blockers.append(INSUFFICIENT_SHADOW_MARKOUT_SAMPLE)
        summary["fills_ok"] = False
        summary["sample_ok"] = False
        return blockers, summary

    fill_count = int(raw.get("count") or 0)
    probe_count = int(raw.get("probe_count") or 0)
    by_skip = dict(raw.get("by_skip_reason") or {})
    hrow = _horizon_row(raw.get("markout") if isinstance(raw.get("markout"), dict) else None, horizon)
    sample_count = int(hrow.get("sample_count") or 0)
    probe_sample = int(hrow.get("probe_sample_count") or 0)
    probe_wr = hrow.get("probe_win_rate_net_roundtrip")
    overall_wr = hrow.get("win_rate_net_roundtrip")
    avg_net = hrow.get("avg_net_roundtrip_markout_bps")

    fills_ok = fill_count >= min_fills or probe_count >= min_probe_fills
    sample_ok = sample_count >= min_sample

    # E1/F1 annotation: cohort-split full-gate fires + post_e31 markout only.
    fg_n = 0
    fg_post_n = 0
    fg_pre_n = 0
    if ledger is not None:
        try:
            qs = ledger.get_quality_stats(hours=hours)
            fg_block = qs.get("full_gate_fires") if isinstance(qs, dict) else None
            if isinstance(fg_block, dict):
                fg_n = int(fg_block.get("count") or 0)
                by_c = fg_block.get("by_cohort") if isinstance(
                    fg_block.get("by_cohort"), dict
                ) else {}
                fg_post_n = int((by_c.get("post_e31") or {}).get("count") or 0)
                fg_pre_n = int((by_c.get("pre_e31") or {}).get("count") or 0)
            elif isinstance(fg_block, (int, float)):
                fg_n = int(fg_block)
        except Exception:
            fg_n = 0
            fg_post_n = 0
            fg_pre_n = 0

    # E3/F1: when total FG fires ≥20, prefer post_e31 5m netRT only (never pre_e31).
    # Sample policy: reuse arming_econ_min_markout_sample (default 5). Prefer
    # threshold FULL_GATE_ECON_PREFER_MIN=20 still keys off total FG fire count.
    fg_sample = 0
    fg_wr = None
    fg_avg = None
    fg_frac = None
    prefer_fg = False
    prefer_fg_attempt = fg_n >= FULL_GATE_ECON_PREFER_MIN
    fg_cohort_used = None
    insufficient_post = False
    if prefer_fg_attempt and ledger is not None:
        try:
            getter = getattr(ledger, "get_full_gate_markout", None)
            if callable(getter):
                fg_raw = getter(
                    hours=hours,
                    horizons=(int(horizon),),
                    apply_funding=False,
                    settings=settings,
                    cohort="post_e31",
                )
            else:
                from keel.ledger.full_gate import compute_full_gate_markout

                conn = ledger._get_conn()
                fg_raw = compute_full_gate_markout(
                    conn,
                    hours=hours,
                    horizons=(int(horizon),),
                    apply_funding=False,
                    settings=settings,
                    cohort="post_e31",
                )
            hrow_fg = _horizon_row(
                fg_raw.get("markout") if isinstance(fg_raw, dict) else None,
                horizon,
            )
            fg_sample = int(hrow_fg.get("sample_count") or 0)
            fg_wr = hrow_fg.get("win_rate_net_roundtrip")
            fg_avg = hrow_fg.get("avg_net_roundtrip_markout_bps")
            fg_frac = hrow_fg.get("frac_clear_net_rt_hurdle")
            if fg_frac is None and isinstance(fg_raw, dict):
                fg_frac = fg_raw.get("frac_clear_net_rt_hurdle")
            if fg_sample >= min_sample:
                prefer_fg = True
                fg_cohort_used = "post_e31"
            else:
                insufficient_post = True
                fg_cohort_used = "post_e31_insufficient"
        except Exception:
            prefer_fg = False
            insufficient_post = True
            fg_cohort_used = "post_e31_insufficient"

    if prefer_fg:
        sample_source = "full_gate_post_e31"
    elif insufficient_post:
        sample_source = "insufficient_post_e31"
    elif fg_post_n > 0 and probe_count == 0 and probe_sample == 0:
        sample_source = "full_gate_post_e31"
    elif fg_pre_n > 0 and fg_post_n == 0 and probe_count == 0:
        sample_source = "stale_pre_e31"
    elif probe_count > 0 or probe_sample > 0:
        sample_source = "probe" if (fg_n == 0) else "mixed"
    elif fill_count > 0:
        sample_source = "shadow_non_probe"
    else:
        sample_source = "insufficient"

    if prefer_fg:
        # Post-e31 FG evidence can satisfy sample/fills when probe cohort is thin.
        fills_ok = True
        sample_ok = True
        overall_wr = fg_wr if fg_wr is not None else overall_wr
        avg_net = fg_avg if fg_avg is not None else avg_net
        sample_count = fg_sample if fg_sample else sample_count

    summary.update(
        {
            "fill_count": fill_count,
            "probe_count": probe_count,
            "sample_count": sample_count,
            "probe_sample_count": probe_sample,
            "probe_win_rate_net_roundtrip": probe_wr,
            "win_rate_net_roundtrip": overall_wr,
            "avg_net_roundtrip_markout_bps": avg_net,
            "fills_ok": fills_ok,
            "sample_ok": sample_ok,
            "by_skip_reason": by_skip,
            "by_instrument": _economic_by_instrument(raw, horizon_seconds=horizon),
            "economic_sample_source": sample_source,
            "full_gate_fires": fg_n,
            "full_gate_fires_post_e31": fg_post_n,
            "full_gate_fires_pre_e31": fg_pre_n,
            "full_gate_sample_count": fg_sample,
            "full_gate_win_rate_net_roundtrip": fg_wr,
            "full_gate_avg_net_roundtrip_markout_bps": fg_avg,
            "full_gate_frac_clear_net_rt_hurdle": fg_frac,
            "full_gate_cohort_used": fg_cohort_used,
        }
    )

    # F1: when FG prefer path is armed (fires≥20) but post_e31 sample thin,
    # block with insufficient_post_e31 — never evaluate pre_e31 win rate.
    if insufficient_post:
        blockers.append(INSUFFICIENT_POST_E31_FULL_GATE_SAMPLE)
        summary["sample_ok"] = False
        summary["passed"] = False
        return blockers, summary

    if not fills_ok or not sample_ok:
        blockers.append(INSUFFICIENT_SHADOW_MARKOUT_SAMPLE)
        return blockers, summary

    # Prefer FG net-RT when sample sufficient; else probe; else overall shadow.
    wr: float | None
    wr_blocker = PROBE_WIN_RATE_BELOW
    if prefer_fg and fg_wr is not None:
        try:
            wr = float(fg_wr)
            wr_blocker = FULL_GATE_WIN_RATE_BELOW
        except (TypeError, ValueError):
            wr = None
    elif probe_sample >= min_sample and probe_wr is not None:
        try:
            wr = float(probe_wr)
        except (TypeError, ValueError):
            wr = None
    elif overall_wr is not None:
        try:
            wr = float(overall_wr)
        except (TypeError, ValueError):
            wr = None
    else:
        wr = None

    if wr is None:
        blockers.append(INSUFFICIENT_SHADOW_MARKOUT_SAMPLE)
        summary["sample_ok"] = False
        return blockers, summary

    if wr < min_win:
        blockers.append(wr_blocker)

    if avg_net is None:
        blockers.append(INSUFFICIENT_SHADOW_MARKOUT_SAMPLE)
        summary["sample_ok"] = False
        return blockers, summary

    try:
        avg_net_f = float(avg_net)
    except (TypeError, ValueError):
        blockers.append(INSUFFICIENT_SHADOW_MARKOUT_SAMPLE)
        summary["sample_ok"] = False
        return blockers, summary

    if avg_net_f < min_avg_net:
        blockers.append(AVG_NET_RT_BELOW)

    summary["passed"] = len(blockers) == 0
    return blockers, summary


def evaluate_arming(
    settings: Any,
    capability: str,
    *,
    equity_usdt: float | None = None,
    market_source: str | None = None,
    worker_stale: bool | None = None,
    ledger: Any | None = None,
    ledger_path: str | Path | None = None,
    shadow_hours: float | None = None,
    markout_stats: dict[str, Any] | None = None,
) -> ArmingReport:
    """
    Evaluate whether live/demo arming prerequisites are met.

    ``ready_to_arm`` is True only when:
      - OKX keys are configured
      - okx_environment is live or demo
      - capability == \"trade\"
      - risk limits (max_notional, max_daily_loss) are sane (> 0)
      - (S1) economic shadow-markout gates pass when enabled and a ledger
        (or injected markout_stats) is available

    Kill-switch state is reported but never auto-cleared. Even when
    ready_to_arm is True, the operator must still set KEEL_KILL_SWITCH=0
    manually (and accept capital risk).

    Optional kwargs (equity / market_source / worker_stale / ledger) only feed
    warnings or optional shadow-rehearsal / economic blockers — unit tests can
    omit them. Economic gates are skipped when no ledger and no markout_stats
    (checklist-only unit path); production status always supplies a ledger.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    economic: dict[str, Any] | None = None

    cap = (capability or "").strip().lower() or "none"
    kill = bool(getattr(settings, "kill_switch", False))
    okx_configured = bool(getattr(settings, "okx_configured", False))
    env = (getattr(settings, "okx_environment", "") or "").strip().lower()

    if not okx_configured:
        blockers.append("OKX keys not configured")

    if env not in ("live", "demo"):
        blockers.append(f"okx_environment must be live or demo (got {env!r})")

    if cap != "trade":
        blockers.append(
            f"okx_capability must be trade (got {cap!r}; read-only keys cannot place orders)"
        )

    max_notional = float(getattr(settings, "max_notional_per_instrument", 0) or 0)
    max_daily_loss = float(getattr(settings, "max_daily_loss_usdt", 0) or 0)
    if max_notional <= 0:
        blockers.append("max_notional_per_instrument must be > 0")
    if max_daily_loss <= 0:
        blockers.append("max_daily_loss_usdt must be > 0")

    # Live first-trade caps must also be sane when env=live.
    if env == "live":
        live_n = float(getattr(settings, "live_max_notional_per_instrument", 0) or 0)
        live_c = int(getattr(settings, "live_max_contracts_per_instrument", 0) or 0)
        if live_n <= 0:
            blockers.append("live_max_notional_per_instrument must be > 0")
        if live_c <= 0:
            blockers.append("live_max_contracts_per_instrument must be > 0")

    # Informational — not blockers for ready_to_arm.
    if kill:
        warnings.append(
            "kill_switch is ON — clear KEEL_KILL_SWITCH=0 manually only after checklist is green"
        )
    else:
        warnings.append(
            "kill_switch is already OFF — trading gates are open if capability=trade"
        )

    if env == "demo":
        warnings.append("okx_environment=demo (simulated); use live for real capital")

    if equity_usdt is not None:
        try:
            eq = float(equity_usdt)
        except (TypeError, ValueError):
            eq = None
        if eq is not None and eq < _TINY_EQUITY_USDT:
            warnings.append(
                f"tiny equity ≈ {eq:.2f} USDT (< {_TINY_EQUITY_USDT:.0f}) — size risk carefully"
            )

    if market_source:
        ms = str(market_source).strip().lower()
        if ms and ms not in ("okx_public",):
            warnings.append(
                f"market_source={market_source!r} (prefer okx_public before live arming)"
            )

    if worker_stale is True:
        warnings.append("worker_stale — confirm trader cycle is healthy before arming")

    # Shadow rehearsal evidence (optional ledger or path).
    hours = shadow_hours
    if hours is None:
        hours = float(getattr(settings, "arming_shadow_hours", 24.0) or 24.0)
    require_shadow = bool(getattr(settings, "arming_require_shadow", False))
    led, owned = _resolve_ledger(ledger, ledger_path, settings)
    try:
        if led is not None:
            if not _has_recent_shadow_fill(led, hours):
                if require_shadow:
                    blockers.append(_SHADOW_REHEARSAL_MSG)
                else:
                    warnings.append(_SHADOW_REHEARSAL_MSG)

        # S1 economic gates — additional; only when ledger or injected stats present.
        econ_enabled = bool(getattr(settings, "arming_econ_enabled", True))
        if econ_enabled and (led is not None or markout_stats is not None):
            econ_blockers, economic = evaluate_economic_gates(
                settings,
                ledger=led,
                markout_stats=markout_stats,
            )
            for b in econ_blockers:
                if b not in blockers:
                    blockers.append(b)
            # Informational: below_hurdle-dominated skips are OK (not a blocker).
            by_skip = (economic or {}).get("by_skip_reason") or {}
            if by_skip:
                top = max(by_skip.items(), key=lambda kv: kv[1])
                if top[0] == "below_hurdle" and top[1] > 0:
                    warnings.append(
                        "probe_skips dominated by below_hurdle (OK — not an arming blocker)"
                    )
        elif econ_enabled:
            economic = {
                "enabled": True,
                "passed": False,
                "note": (
                    "Economic gates enabled but no ledger/markout_stats in this "
                    "evaluation — skipped (production status always supplies ledger)."
                ),
            }
    finally:
        if owned and led is not None:
            close = getattr(led, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    ready = len(blockers) == 0
    return ArmingReport(
        ready_to_arm=ready,
        kill_switch=kill,
        capability=cap,
        blockers=blockers,
        warnings=warnings,
        economic=economic,
    )

# ---------------------------------------------------------------------------
# Stage S2: first-live checklist (read-only productization of Stage T gate)
# ---------------------------------------------------------------------------

_FIRST_LIVE_HUMAN_STEPS: tuple[str, ...] = (
    "wait_for_economic_pass",
    "set_tiny_notional",
    "clear_kill_manually",
    "verify_one_fill",
    "re_enable_kill",
)


@dataclass(frozen=True)
class FirstLiveReport:
    """Aggregated answer to: can I open one minimum live order? (never clears kill)."""

    allowed_now: bool
    kill_switch: bool
    shadow_mode: bool
    shadow_near_probe: bool
    capability: str
    ready_to_arm: bool
    blockers: list[str] = field(default_factory=list)
    economic: dict[str, Any] | None = None
    suggested_live_caps: dict[str, Any] = field(default_factory=dict)
    human_steps: list[str] = field(default_factory=list)
    note: str = ""


def build_first_live(
    settings: Any,
    arming: ArmingReport,
    *,
    shadow_mode: bool | None = None,
    shadow_near_probe: bool | None = None,
) -> FirstLiveReport:
    """
    Productize a first-live checklist from existing arming + settings (Stage S2).

    Read-only: never writes env, never clears kill-switch, never places orders.
    ``allowed_now`` is False while kill is on OR economic gates have not passed
    (or other arming blockers remain / shadow still on).
    """
    kill = bool(getattr(settings, "kill_switch", False))
    shadow = bool(shadow_mode) if shadow_mode is not None else bool(
        getattr(settings, "shadow_mode", False)
    )
    near_probe = (
        bool(shadow_near_probe)
        if shadow_near_probe is not None
        else bool(getattr(settings, "shadow_near_probe", False))
    )
    cap = (arming.capability or "").strip().lower() or "none"
    ready = bool(arming.ready_to_arm)
    blockers = list(arming.blockers or [])
    economic = arming.economic

    econ_passed = True
    if economic is not None and economic.get("enabled", True):
        econ_passed = bool(economic.get("passed"))
    elif economic is not None and economic.get("enabled") is False:
        econ_passed = True
    elif bool(getattr(settings, "arming_econ_enabled", True)):
        # Enabled but no summary yet → not passed.
        econ_passed = False

    # Explicit gate: kill on OR economic not passed → not allowed.
    # Also require ready_to_arm and shadow off for a real live minimum order.
    allowed = (
        (not kill)
        and econ_passed
        and ready
        and (not shadow)
    )

    caps = {
        "live_max_notional_per_instrument": float(
            getattr(settings, "live_max_notional_per_instrument", 200.0) or 200.0
        ),
        "live_max_contracts_per_instrument": int(
            getattr(settings, "live_max_contracts_per_instrument", 5) or 5
        ),
        "env_keys": [
            "KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT",
            "KEEL_LIVE_MAX_CONTRACTS_PER_INSTRUMENT",
        ],
    }

    note = (
        "Read-only Stage T gate checklist. Never auto-clears KEEL_KILL_SWITCH; "
        "never places orders. allowed_now requires kill off, economic pass, "
        "ready_to_arm, and shadow_mode off."
    )

    return FirstLiveReport(
        allowed_now=allowed,
        kill_switch=kill,
        shadow_mode=shadow,
        shadow_near_probe=near_probe,
        capability=cap,
        ready_to_arm=ready,
        blockers=blockers,
        economic=economic,
        suggested_live_caps=caps,
        human_steps=list(_FIRST_LIVE_HUMAN_STEPS),
        note=note,
    )
