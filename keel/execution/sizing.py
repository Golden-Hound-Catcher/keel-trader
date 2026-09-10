"""
Position sizing for OKX-style swap contracts.

LLM / rule decisions propose ``margin_usdt`` and ``leverage``. Execution must
convert that into exchange ``sz`` (contract count), using each instrument's
``contract_value``, then clip to remaining risk-cap headroom instead of
hard-rejecting an oversized proposal.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from keel.domain.instruments import (
    Instrument,
    contract_face_usdt,
    lookup_instrument,
)

DEFAULT_MARGIN_USDT = 50.0


@dataclass(frozen=True)
class SizeConstraints:
    """Remaining-capacity inputs (absolute caps, not yet minus existing)."""

    max_margin: float
    max_notional: float
    max_contracts: float
    available_margin: float | None = None
    existing_margin: float = 0.0
    existing_notional: float = 0.0
    existing_size: float = 0.0


@dataclass(frozen=True)
class SizedOrder:
    margin_usdt: float
    leverage: int
    notional: float
    size: float
    clipped: bool
    clip_notes: tuple[str, ...] = ()
    error: str = ""


def round_contracts(raw: float, instrument: Instrument) -> float:
    """Floor to the instrument size precision (OKX sz is never rounded up)."""
    if raw <= 0:
        return 0.0
    precision = max(int(instrument.size_precision), 0)
    if precision == 0:
        return float(math.floor(raw + 1e-12))
    factor = 10 ** precision
    return math.floor(raw * factor + 1e-12) / factor


def size_order(
    *,
    inst_id: str,
    requested_margin: float,
    leverage: int,
    entry_price: float,
    constraints: SizeConstraints,
    instrument: Instrument | None = None,
) -> SizedOrder:
    """
    Convert requested margin into a contract size that fits remaining caps.

    Returns ``error`` (size 0) when even the exchange minimum lot cannot fit.
    """
    inst = instrument or lookup_instrument(inst_id)
    lev = max(int(leverage), 1)
    notes: list[str] = []

    if entry_price <= 0:
        return SizedOrder(
            margin_usdt=0.0,
            leverage=lev,
            notional=0.0,
            size=0.0,
            clipped=True,
            error="Invalid entry price for sizing",
        )

    face = contract_face_usdt(entry_price, inst)
    if face <= 0:
        return SizedOrder(
            margin_usdt=0.0,
            leverage=lev,
            notional=0.0,
            size=0.0,
            clipped=True,
            error="Invalid contract face value for sizing",
        )

    room_margin = max(0.0, float(constraints.max_margin) - float(constraints.existing_margin))
    room_notional = max(0.0, float(constraints.max_notional) - float(constraints.existing_notional))
    room_contracts = max(0.0, float(constraints.max_contracts) - float(constraints.existing_size))

    if constraints.available_margin is not None:
        available = max(0.0, float(constraints.available_margin))
        if available < room_margin:
            notes.append("available_balance")
            room_margin = available

    req_margin = float(requested_margin)
    if req_margin <= 0:
        req_margin = DEFAULT_MARGIN_USDT
        notes.append("default_margin")

    target_margin = min(req_margin, room_margin)
    if target_margin + 1e-9 < req_margin:
        notes.append("max_margin")

    target_notional = target_margin * lev
    if target_notional > room_notional + 1e-9:
        target_notional = room_notional
        notes.append("max_notional")

    max_n_from_contracts = room_contracts * face
    if target_notional > max_n_from_contracts + 1e-9:
        target_notional = max_n_from_contracts
        notes.append("max_contracts")

    raw_size = target_notional / face if face > 0 else 0.0
    size = round_contracts(raw_size, inst)
    min_size = float(inst.min_size) if inst.min_size > 0 else 1.0
    min_notional = min_size * face
    min_margin = min_notional / lev

    if size + 1e-12 < min_size:
        fits_min = (
            min_size <= room_contracts + 1e-12
            and min_notional <= room_notional + 1e-6
            and min_margin <= room_margin + 1e-6
        )
        if fits_min:
            size = min_size
            notes.append("min_lot")
        else:
            return SizedOrder(
                margin_usdt=0.0,
                leverage=lev,
                notional=0.0,
                size=0.0,
                clipped=True,
                clip_notes=tuple(notes),
                error=(
                    f"不足以开最小仓：1 张约 {min_notional:.2f}U 名义 / "
                    f"{min_margin:.2f}U 保证金，剩余上限 名义 {room_notional:.2f}U / "
                    f"保证金 {room_margin:.2f}U / 张数 {room_contracts:.4g}"
                ),
            )

    notional = size * face
    margin = notional / lev
    requested_for_clip = float(requested_margin) if float(requested_margin) > 0 else DEFAULT_MARGIN_USDT
    clipped = bool(notes) or abs(margin - requested_for_clip) > 0.05

    return SizedOrder(
        margin_usdt=round(margin, 8),
        leverage=lev,
        notional=round(notional, 8),
        size=size,
        clipped=clipped,
        clip_notes=tuple(notes),
        error="",
    )
