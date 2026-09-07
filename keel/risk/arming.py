"""
Read-only arming checklist (Q1).

Reports whether it is *safe for the operator to clear* KEEL_KILL_SWITCH —
without ever writing env or placing orders. ready_to_arm does not flip the
kill-switch; the operator must still set KEEL_KILL_SWITCH=0 manually.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Soft warning when optional equity is known and very small (Jo-scale live).
_TINY_EQUITY_USDT = 50.0

_SHADOW_REHEARSAL_MSG = "no recent shadow_fill rehearsal"


@dataclass(frozen=True)
class ArmingReport:
    """Result of evaluate_arming — echoed on GET /api/v1/status as ``arming``."""

    ready_to_arm: bool
    kill_switch: bool
    capability: str
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
) -> ArmingReport:
    """
    Evaluate whether live/demo arming prerequisites are met.

    ``ready_to_arm`` is True only when:
      - OKX keys are configured
      - okx_environment is live or demo
      - capability == \"trade\"
      - risk limits (max_notional, max_daily_loss) are sane (> 0)

    Kill-switch state is reported but never auto-cleared. Even when
    ready_to_arm is True, the operator must still set KEEL_KILL_SWITCH=0
    manually (and accept capital risk).

    Optional kwargs (equity / market_source / worker_stale / ledger) only feed
    warnings or optional shadow-rehearsal blockers — unit tests can omit them.
    """
    blockers: list[str] = []
    warnings: list[str] = []

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
    )
