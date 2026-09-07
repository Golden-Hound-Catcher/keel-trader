"""
Read-only arming checklist (Q1).

Reports whether it is *safe for the operator to clear* KEEL_KILL_SWITCH —
without ever writing env or placing orders. ready_to_arm does not flip the
kill-switch; the operator must still set KEEL_KILL_SWITCH=0 manually.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Soft warning when optional equity is known and very small (Jo-scale live).
_TINY_EQUITY_USDT = 50.0


@dataclass(frozen=True)
class ArmingReport:
    """Result of evaluate_arming — echoed on GET /api/v1/status as ``arming``."""

    ready_to_arm: bool
    kill_switch: bool
    capability: str
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def evaluate_arming(
    settings: Any,
    capability: str,
    *,
    equity_usdt: float | None = None,
    market_source: str | None = None,
    worker_stale: bool | None = None,
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

    Optional kwargs (equity / market_source / worker_stale) only feed
    warnings — unit tests can omit them to avoid network.
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

    ready = len(blockers) == 0
    return ArmingReport(
        ready_to_arm=ready,
        kill_switch=kill,
        capability=cap,
        blockers=blockers,
        warnings=warnings,
    )
