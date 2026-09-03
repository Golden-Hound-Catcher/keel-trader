"""
Paper trading adapter for Keel.

Implements ExchangeProtocol for local testing without real exchange connection.
This is the default when OKX credentials are not configured.
"""
from __future__ import annotations

import time
import threading
from typing import Literal
from dataclasses import replace

from keel.exchange.protocol import (
    ExchangeProtocol,
    Position,
    Order,
    Ticker,
    AccountBalance,
    OrderRequest,
    OrderResult,
)


class PaperAdapter:
    """
    Paper trading adapter for testing without real money.
    
    Simulates order fills at current market price.
    Thread-safe via locks.
    """

    def __init__(self, initial_balance: float = 10000.0):
        self._balance = initial_balance
        self._available = initial_balance
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._tickers: dict[str, Ticker] = {}
        self._order_counter = 0
        self._lock = threading.Lock()

    @property
    def is_demo(self) -> bool:
        return True

    @property
    def adapter_name(self) -> str:
        return "paper"

    def set_ticker(self, ticker: Ticker) -> None:
        """Set a ticker for paper trading (normally fed from market data)."""
        with self._lock:
            self._tickers[ticker.inst_id] = ticker

    def get_balance(self) -> AccountBalance:
        with self._lock:
            total_upl = sum(p.upl for p in self._positions.values())
            total_margin = sum(p.margin for p in self._positions.values())
            return AccountBalance(
                total_equity=self._balance + total_upl,
                available_balance=self._available,
                cash_balance=self._balance,
                unrealized_pnl=total_upl,
                margin_used=total_margin,
            )

    def get_positions(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())

    def get_position(self, inst_id: str) -> Position | None:
        with self._lock:
            for pos in self._positions.values():
                if pos.inst_id == inst_id:
                    return pos
            return None

    def get_ticker(self, inst_id: str) -> Ticker | None:
        with self._lock:
            return self._tickers.get(inst_id)

    def get_open_orders(self, inst_id: str | None = None) -> list[Order]:
        with self._lock:
            orders = [o for o in self._orders.values() if o.state == "live"]
            if inst_id:
                orders = [o for o in orders if o.inst_id == inst_id]
            return orders

    def place_order(self, request: OrderRequest) -> OrderResult:
        with self._lock:
            ticker = self._tickers.get(request.inst_id)
            if not ticker:
                return OrderResult(success=False, error=f"No ticker data for {request.inst_id}")

            self._order_counter += 1
            order_id = f"paper-{self._order_counter}"

            fill_price = ticker.last
            if request.order_type == "limit" and request.price:
                if request.side == "buy" and request.price < ticker.ask:
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        order=Order(
                            order_id=order_id,
                            inst_id=request.inst_id,
                            side=request.side,
                            pos_side=request.pos_side,
                            order_type=request.order_type,
                            size=request.size,
                            price=request.price,
                            state="live",
                            tp_trigger_price=request.tp_trigger_price,
                            sl_trigger_price=request.sl_trigger_price,
                            created_at=time.time(),
                        ),
                    )
                elif request.side == "sell" and request.price > ticker.bid:
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        order=Order(
                            order_id=order_id,
                            inst_id=request.inst_id,
                            side=request.side,
                            pos_side=request.pos_side,
                            order_type=request.order_type,
                            size=request.size,
                            price=request.price,
                            state="live",
                            tp_trigger_price=request.tp_trigger_price,
                            sl_trigger_price=request.sl_trigger_price,
                            created_at=time.time(),
                        ),
                    )
                fill_price = request.price

            pos_key = f"{request.inst_id}_{request.pos_side}"

            if request.reduce_only:
                existing = self._positions.get(pos_key)
                if not existing:
                    return OrderResult(success=False, error="No position to reduce")
                pnl = self._calculate_pnl(existing, fill_price)
                self._balance += pnl + existing.margin
                self._available += existing.margin
                del self._positions[pos_key]
            else:
                leverage = 3.0
                margin = (request.size * fill_price) / leverage
                if margin > self._available:
                    return OrderResult(success=False, error="Insufficient margin")

                self._available -= margin
                self._positions[pos_key] = Position(
                    inst_id=request.inst_id,
                    side=request.pos_side,
                    size=request.size,
                    avg_price=fill_price,
                    mark_price=fill_price,
                    leverage=leverage,
                    margin=margin,
                )

            order = Order(
                order_id=order_id,
                inst_id=request.inst_id,
                side=request.side,
                pos_side=request.pos_side,
                order_type=request.order_type,
                size=request.size,
                price=fill_price,
                state="filled",
                filled_size=request.size,
                tp_trigger_price=request.tp_trigger_price,
                sl_trigger_price=request.sl_trigger_price,
                created_at=time.time(),
            )

            return OrderResult(success=True, order_id=order_id, order=order)

    def cancel_order(self, inst_id: str, order_id: str) -> bool:
        with self._lock:
            if order_id in self._orders:
                self._orders[order_id] = replace(
                    self._orders[order_id], state="cancelled"
                )
                return True
            return False

    def close_position(self, inst_id: str, pos_side: Literal["long", "short"]) -> OrderResult:
        pos_key = f"{inst_id}_{pos_side}"
        with self._lock:
            position = self._positions.get(pos_key)
            if not position:
                return OrderResult(success=False, error="No position to close")

        return self.place_order(
            OrderRequest(
                inst_id=inst_id,
                side="sell" if pos_side == "long" else "buy",
                pos_side=pos_side,
                size=position.size,
                order_type="market",
                reduce_only=True,
            )
        )

    def _calculate_pnl(self, position: Position, exit_price: float) -> float:
        """Calculate PnL for closing a position."""
        if position.side == "long":
            return (exit_price - position.avg_price) * position.size
        else:
            return (position.avg_price - exit_price) * position.size

    def update_mark_prices(self) -> None:
        """Update position mark prices from current tickers."""
        with self._lock:
            for pos_key, position in list(self._positions.items()):
                ticker = self._tickers.get(position.inst_id)
                if ticker:
                    upl = self._calculate_pnl(position, ticker.last)
                    upl_ratio = upl / position.margin if position.margin > 0 else 0.0
                    self._positions[pos_key] = replace(
                        position,
                        mark_price=ticker.last,
                        upl=upl,
                        upl_ratio=upl_ratio,
                    )

# Stage 5 preferred alias
PaperExchange = PaperAdapter
