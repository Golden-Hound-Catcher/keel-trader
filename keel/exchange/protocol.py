"""
Typed Protocol for exchange access.

This defines the interface that all exchange adapters must implement.
No shell CLI in the happy path - everything goes through typed methods.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Literal, runtime_checkable


@dataclass(frozen=True)
class Ticker:
    """Market ticker data."""
    inst_id: str
    last: float
    bid: float
    ask: float
    open_24h: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    vol_24h: float = 0.0
    timestamp: float = 0.0

    @property
    def change_24h_pct(self) -> float:
        if self.open_24h > 0:
            return (self.last - self.open_24h) / self.open_24h * 100
        return 0.0


@dataclass(frozen=True)
class Position:
    """Open position data."""
    inst_id: str
    side: Literal["long", "short"]
    size: float
    avg_price: float
    mark_price: float
    leverage: float
    upl: float = 0.0
    upl_ratio: float = 0.0
    margin: float = 0.0
    liq_price: float | None = None

    @property
    def notional(self) -> float:
        return self.size * self.mark_price


@dataclass(frozen=True)
class Order:
    """Order data."""
    order_id: str
    inst_id: str
    side: Literal["buy", "sell"]
    pos_side: Literal["long", "short", "net"]
    order_type: Literal["market", "limit"]
    size: float
    price: float | None = None
    state: Literal["live", "filled", "cancelled", "partially_filled"] = "live"
    filled_size: float = 0.0
    tp_trigger_price: float | None = None
    sl_trigger_price: float | None = None
    created_at: float = 0.0


@dataclass(frozen=True)
class AccountBalance:
    """Account balance data."""
    total_equity: float
    available_balance: float
    cash_balance: float
    unrealized_pnl: float
    margin_used: float = 0.0

    @property
    def margin_usage_pct(self) -> float:
        if self.total_equity > 0:
            return (self.total_equity - self.available_balance) / self.total_equity * 100
        return 0.0


@dataclass
class OrderRequest:
    """Request to place an order."""
    inst_id: str
    side: Literal["buy", "sell"]
    pos_side: Literal["long", "short"]
    size: float
    order_type: Literal["market", "limit"] = "limit"
    price: float | None = None
    tp_trigger_price: float | None = None
    sl_trigger_price: float | None = None
    reduce_only: bool = False


@dataclass
class OrderResult:
    """Result of placing an order."""
    success: bool
    order_id: str | None = None
    error: str | None = None
    order: Order | None = None


@runtime_checkable
class ExchangeProtocol(Protocol):
    """
    Protocol defining the exchange interface.
    
    All exchange adapters (OKX REST, paper trading, etc.) must implement this.
    """

    def get_balance(self) -> AccountBalance:
        """Get account balance."""
        ...

    def get_positions(self) -> list[Position]:
        """Get all open positions."""
        ...

    def get_position(self, inst_id: str) -> Position | None:
        """Get position for a specific instrument."""
        ...

    def get_ticker(self, inst_id: str) -> Ticker | None:
        """Get market ticker for an instrument."""
        ...

    def get_open_orders(self, inst_id: str | None = None) -> list[Order]:
        """Get open orders, optionally filtered by instrument."""
        ...

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Place a new order."""
        ...

    def cancel_order(self, inst_id: str, order_id: str) -> bool:
        """Cancel an order. Returns True if successful."""
        ...

    def close_position(self, inst_id: str, pos_side: Literal["long", "short"]) -> OrderResult:
        """Close a position entirely."""
        ...

    @property
    def is_demo(self) -> bool:
        """Whether this is a demo/paper trading environment."""
        ...
