"""
Instrument definitions for Keel Trader.

Replaces the scattered TARGET_INSTRUMENTS lists throughout the codebase.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Instrument:
    """Trading instrument definition."""
    inst_id: str
    name: str
    asset_type: Literal["crypto", "stock", "commodity", "index"] = "crypto"
    contract_value: float = 1.0
    price_precision: int = 2
    size_precision: int = 0
    min_size: int = 1
    base_risk_usd: float = 15.0

    @classmethod
    def from_okx_swap(cls, symbol: str, contract_value: float = 1.0, price_precision: int = 2) -> "Instrument":
        """Create an instrument from OKX swap symbol like 'BTC-USDT-SWAP'."""
        name = symbol.replace("-USDT-SWAP", "").replace("-SWAP", "")
        return cls(
            inst_id=symbol,
            name=name,
            contract_value=contract_value,
            price_precision=price_precision,
        )


# Default crypto perpetual instruments
DEFAULT_CRYPTO_INSTRUMENTS = [
    Instrument(
        inst_id="BTC-USDT-SWAP",
        name="BTC",
        contract_value=0.01,
        price_precision=1,
        base_risk_usd=20.0,
    ),
    Instrument(
        inst_id="ETH-USDT-SWAP",
        name="ETH",
        contract_value=0.1,
        price_precision=2,
        base_risk_usd=15.0,
    ),
    Instrument(
        inst_id="SOL-USDT-SWAP",
        name="SOL",
        contract_value=1.0,
        price_precision=2,
        base_risk_usd=15.0,
    ),
    Instrument(
        inst_id="DOGE-USDT-SWAP",
        name="DOGE",
        contract_value=1000.0,
        price_precision=5,
        base_risk_usd=10.0,
    ),
    Instrument(
        inst_id="SUI-USDT-SWAP",
        name="SUI",
        contract_value=1.0,
        price_precision=4,
        base_risk_usd=10.0,
    ),
    Instrument(
        inst_id="LINK-USDT-SWAP",
        name="LINK",
        contract_value=1.0,
        price_precision=3,
        base_risk_usd=10.0,
    ),
]


class InstrumentPool:
    """
    Pool of tradable instruments.
    
    Centralizes instrument management instead of scattered lists.
    """

    def __init__(self, instruments: list[Instrument] | None = None):
        self._instruments = {i.inst_id: i for i in (instruments or DEFAULT_CRYPTO_INSTRUMENTS)}

    def get(self, inst_id: str) -> Instrument | None:
        """Get instrument by ID."""
        return self._instruments.get(inst_id)

    def get_by_name(self, name: str) -> Instrument | None:
        """Get instrument by short name (e.g., 'BTC')."""
        for inst in self._instruments.values():
            if inst.name == name:
                return inst
        return None

    def all(self) -> list[Instrument]:
        """Get all instruments."""
        return list(self._instruments.values())

    def __len__(self) -> int:
        return len(self._instruments)

    def __iter__(self):
        return iter(self._instruments.values())
