"""
OKX REST adapter for Keel.

Implements ExchangeProtocol using direct HTTP calls to OKX V5 API.
No shell CLI in the happy path.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Literal

from keel.exchange.protocol import (
    ExchangeProtocol,
    Position,
    Order,
    Ticker,
    AccountBalance,
    OrderRequest,
    OrderResult,
)


class OKXRestAdapter:
    """
    OKX REST API adapter.
    
    Uses V5 API with HMAC-SHA256 authentication.
    """

    DEMO_BASE_URL = "https://www.okx.com"
    LIVE_BASE_URL = "https://www.okx.com"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        demo: bool = True,
    ):
        self._api_key = api_key
        self._secret_key = secret_key
        self._passphrase = passphrase
        self._demo = demo
        self._base_url = self.DEMO_BASE_URL if demo else self.LIVE_BASE_URL

    @property
    def is_demo(self) -> bool:
        return self._demo

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """Generate HMAC-SHA256 signature."""
        message = timestamp + method + path + body
        mac = hmac.new(
            self._secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        signed: bool = True,
    ) -> dict[str, Any]:
        """Make an HTTP request to OKX API."""
        url = self._base_url + path
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if query:
                url += "?" + query
                path += "?" + query

        body_str = json.dumps(body) if body else ""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}Z"

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Keel-Trader/0.1",
        }

        if signed:
            signature = self._sign(timestamp, method, path, body_str)
            headers.update({
                "OK-ACCESS-KEY": self._api_key,
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self._passphrase,
            })
            if self._demo:
                headers["x-simulated-trading"] = "1"

        req = urllib.request.Request(
            url,
            data=body_str.encode("utf-8") if body_str else None,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("code") != "0":
                    raise ValueError(f"OKX API error: {result.get('msg', 'unknown')}")
                return result
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else ""
            raise ValueError(f"HTTP {e.code}: {error_body}") from e

    def _public_request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make an unsigned public API request."""
        return self._request("GET", path, params=params, signed=False)

    def get_balance(self) -> AccountBalance:
        """Get account balance."""
        result = self._request("GET", "/api/v5/account/balance")
        data = result.get("data", [{}])[0]
        
        usdt_detail = next(
            (d for d in data.get("details", []) if d.get("ccy") == "USDT"),
            {},
        )

        return AccountBalance(
            total_equity=float(usdt_detail.get("eq", 0) or 0),
            available_balance=float(usdt_detail.get("availBal", 0) or 0),
            cash_balance=float(usdt_detail.get("cashBal", 0) or 0),
            unrealized_pnl=float(usdt_detail.get("upl", 0) or 0),
            margin_used=float(data.get("imr", 0) or 0),
        )

    def get_positions(self) -> list[Position]:
        """Get all open positions."""
        result = self._request("GET", "/api/v5/account/positions", params={"instType": "SWAP"})
        positions = []

        for p in result.get("data", []):
            size = float(p.get("pos", 0) or 0)
            if size == 0:
                continue

            pos_side = p.get("posSide", "net").lower()
            if pos_side == "net":
                pos_side = "long" if size > 0 else "short"

            positions.append(Position(
                inst_id=p.get("instId", ""),
                side=pos_side,
                size=abs(size),
                avg_price=float(p.get("avgPx", 0) or 0),
                mark_price=float(p.get("markPx", 0) or 0),
                leverage=float(p.get("lever", 1) or 1),
                upl=float(p.get("upl", 0) or 0),
                upl_ratio=float(p.get("uplRatio", 0) or 0),
                margin=float(p.get("imr", 0) or 0),
                liq_price=float(p.get("liqPx", 0) or 0) or None,
            ))

        return positions

    def get_position(self, inst_id: str) -> Position | None:
        """Get position for a specific instrument."""
        for pos in self.get_positions():
            if pos.inst_id == inst_id:
                return pos
        return None

    def get_ticker(self, inst_id: str) -> Ticker | None:
        """Get market ticker for an instrument."""
        try:
            result = self._public_request(
                "/api/v5/market/ticker",
                params={"instId": inst_id},
            )
            data = result.get("data", [{}])[0]
            if not data:
                return None

            return Ticker(
                inst_id=inst_id,
                last=float(data.get("last", 0) or 0),
                bid=float(data.get("bidPx", 0) or 0),
                ask=float(data.get("askPx", 0) or 0),
                open_24h=float(data.get("open24h", 0) or 0),
                high_24h=float(data.get("high24h", 0) or 0),
                low_24h=float(data.get("low24h", 0) or 0),
                vol_24h=float(data.get("vol24h", 0) or 0),
                timestamp=float(data.get("ts", 0) or 0) / 1000,
            )
        except Exception:
            return None

    def get_open_orders(self, inst_id: str | None = None) -> list[Order]:
        """Get open orders."""
        params: dict[str, Any] = {"instType": "SWAP"}
        if inst_id:
            params["instId"] = inst_id

        result = self._request("GET", "/api/v5/trade/orders-pending", params=params)
        orders = []

        for o in result.get("data", []):
            state = o.get("state", "live").lower()
            if state == "partially_filled":
                state = "partially_filled"
            elif state in ("filled", "canceled", "cancelled"):
                continue
            else:
                state = "live"

            pos_side = o.get("posSide", "net").lower()
            if pos_side not in ("long", "short", "net"):
                pos_side = "net"

            orders.append(Order(
                order_id=o.get("ordId", ""),
                inst_id=o.get("instId", ""),
                side=o.get("side", "buy").lower(),
                pos_side=pos_side,
                order_type=o.get("ordType", "limit").lower(),
                size=float(o.get("sz", 0) or 0),
                price=float(o.get("px", 0) or 0) or None,
                state=state,
                filled_size=float(o.get("accFillSz", 0) or 0),
                tp_trigger_price=float(o.get("tpTriggerPx", 0) or 0) or None,
                sl_trigger_price=float(o.get("slTriggerPx", 0) or 0) or None,
                created_at=float(o.get("cTime", 0) or 0) / 1000,
            ))

        return orders

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Place a new order."""
        body: dict[str, Any] = {
            "instId": request.inst_id,
            "tdMode": "cross",
            "side": request.side,
            "posSide": request.pos_side,
            "ordType": request.order_type,
            "sz": str(request.size),
        }

        if request.price is not None:
            body["px"] = str(request.price)

        if request.reduce_only:
            body["reduceOnly"] = True

        if request.tp_trigger_price is not None:
            body["tpTriggerPx"] = str(request.tp_trigger_price)
            body["tpOrdPx"] = "-1"

        if request.sl_trigger_price is not None:
            body["slTriggerPx"] = str(request.sl_trigger_price)
            body["slOrdPx"] = "-1"

        try:
            result = self._request("POST", "/api/v5/trade/order", body=body)
            data = result.get("data", [{}])[0]

            if data.get("sCode") != "0":
                return OrderResult(
                    success=False,
                    error=data.get("sMsg", "Order rejected"),
                )

            return OrderResult(
                success=True,
                order_id=data.get("ordId"),
                order=Order(
                    order_id=data.get("ordId", ""),
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
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def cancel_order(self, inst_id: str, order_id: str) -> bool:
        """Cancel an order."""
        try:
            result = self._request(
                "POST",
                "/api/v5/trade/cancel-order",
                body={"instId": inst_id, "ordId": order_id},
            )
            data = result.get("data", [{}])[0]
            return data.get("sCode") == "0"
        except Exception:
            return False

    def close_position(self, inst_id: str, pos_side: Literal["long", "short"]) -> OrderResult:
        """Close a position entirely."""
        try:
            result = self._request(
                "POST",
                "/api/v5/trade/close-position",
                body={
                    "instId": inst_id,
                    "mgnMode": "cross",
                    "posSide": pos_side,
                    "autoCxl": True,
                },
            )
            data = result.get("data", [{}])[0]

            if result.get("code") != "0":
                return OrderResult(success=False, error=result.get("msg", "Close failed"))

            return OrderResult(success=True, order_id=data.get("ordId"))
        except Exception as e:
            return OrderResult(success=False, error=str(e))
