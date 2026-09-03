"""
OKX REST adapter for Keel (V5).

Implements ExchangeProtocol with direct HTTP — no shell CLI.
- Signed private endpoints when API key / secret / passphrase are present
- Public market endpoints otherwise (ticker works without keys)
- Injectable transport for unit tests (no live network required in CI)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from keel.exchange.protocol import (
    AccountBalance,
    ExchangeProtocol,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    Ticker,
)

# (method, url, headers, body_bytes) -> response body str
HttpTransport = Callable[[str, str, dict[str, str], bytes | None], str]


def _default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
) -> str:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class OKXRestAdapter:
    """
    OKX V5 REST adapter (demo or live).

    Prefer constructing via ``from_settings`` / ``keel.exchange.factory.build_exchange``.
    """

    BASE_URL = "https://www.okx.com"

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        passphrase: str = "",
        *,
        demo: bool = True,
        base_url: str = BASE_URL,
        transport: HttpTransport | None = None,
    ):
        self._api_key = (api_key or "").strip()
        self._secret_key = (secret_key or "").strip()
        self._passphrase = (passphrase or "").strip()
        self._demo = bool(demo)
        self._base_url = (base_url or self.BASE_URL).rstrip("/")
        self._transport = transport or _default_transport

    @classmethod
    def from_settings(cls, settings: Any, *, transport: HttpTransport | None = None) -> "OKXRestAdapter":
        """Build from keel.config.Settings (or any object with okx_* fields)."""
        return cls(
            api_key=getattr(settings, "okx_api_key", "") or "",
            secret_key=getattr(settings, "okx_secret_key", "") or "",
            passphrase=getattr(settings, "okx_passphrase", "") or "",
            demo=bool(getattr(settings, "is_demo", True)),
            transport=transport,
        )

    @property
    def is_demo(self) -> bool:
        return self._demo

    @property
    def credentials_configured(self) -> bool:
        return bool(self._api_key and self._secret_key and self._passphrase)

    @property
    def adapter_name(self) -> str:
        env = "demo" if self._demo else "live"
        mode = "signed" if self.credentials_configured else "public"
        return f"okx_rest:{env}:{mode}"

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        message = f"{timestamp}{method}{request_path}{body}"
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
        *,
        signed: bool = True,
    ) -> dict[str, Any]:
        method = method.upper()
        query = ""
        if params:
            # Stable order helps tests; OKX accepts any order.
            items = [(k, v) for k, v in params.items() if v is not None]
            query = urllib.parse.urlencode(items)

        request_path = path + (f"?{query}" if query else "")
        url = self._base_url + request_path
        body_str = (
            json.dumps(body, separators=(",", ":")) if body is not None else ""
        )
        body_bytes = body_str.encode("utf-8") if body_str else None

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Keel-Trader/0.1",
        }

        if signed:
            if not self.credentials_configured:
                raise ValueError(
                    "OKX private endpoint requires KEEL_OKX_API_KEY / "
                    "KEEL_OKX_SECRET_KEY / KEEL_OKX_PASSPHRASE (or OKX_* aliases)"
                )
            timestamp = _utc_timestamp()
            headers.update(
                {
                    "OK-ACCESS-KEY": self._api_key,
                    "OK-ACCESS-SIGN": self._sign(
                        timestamp, method, request_path, body_str
                    ),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self._passphrase,
                }
            )
            if self._demo:
                headers["x-simulated-trading"] = "1"

        try:
            raw = self._transport(method, url, headers, body_bytes)
            result = json.loads(raw)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else ""
            raise ValueError(f"HTTP {e.code}: {error_body[:300]}") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid OKX JSON response: {e}") from e

        code = result.get("code")
        if code not in (None, "0", 0):
            raise ValueError(f"OKX API error: {result.get('msg', 'unknown')}")
        return result

    def _public_request(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._request("GET", path, params=params, signed=False)

    def get_balance(self) -> AccountBalance:
        result = self._request("GET", "/api/v5/account/balance")
        data = (result.get("data") or [{}])[0]
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
        result = self._request(
            "GET", "/api/v5/account/positions", params={"instType": "SWAP"}
        )
        positions: list[Position] = []
        for p in result.get("data") or []:
            size = float(p.get("pos", 0) or 0)
            if size == 0:
                continue
            pos_side = (p.get("posSide") or "net").lower()
            if pos_side == "net":
                pos_side = "long" if size > 0 else "short"
            if pos_side not in ("long", "short"):
                pos_side = "long" if size > 0 else "short"
            positions.append(
                Position(
                    inst_id=p.get("instId", ""),
                    side=pos_side,  # type: ignore[arg-type]
                    size=abs(size),
                    avg_price=float(p.get("avgPx", 0) or 0),
                    mark_price=float(p.get("markPx", 0) or 0),
                    leverage=float(p.get("lever", 1) or 1),
                    upl=float(p.get("upl", 0) or 0),
                    upl_ratio=float(p.get("uplRatio", 0) or 0),
                    margin=float(p.get("imr", 0) or 0),
                    liq_price=float(p.get("liqPx", 0) or 0) or None,
                )
            )
        return positions

    def get_position(self, inst_id: str) -> Position | None:
        for pos in self.get_positions():
            if pos.inst_id == inst_id:
                return pos
        return None

    def get_ticker(self, inst_id: str) -> Ticker | None:
        try:
            result = self._public_request(
                "/api/v5/market/ticker",
                params={"instId": inst_id},
            )
            data = (result.get("data") or [{}])[0]
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
        params: dict[str, Any] = {"instType": "SWAP"}
        if inst_id:
            params["instId"] = inst_id
        result = self._request("GET", "/api/v5/trade/orders-pending", params=params)
        orders: list[Order] = []
        for o in result.get("data") or []:
            state_raw = (o.get("state") or "live").lower()
            if state_raw in ("filled", "canceled", "cancelled"):
                continue
            state: Literal["live", "partially_filled"] = (
                "partially_filled" if state_raw == "partially_filled" else "live"
            )
            pos_side = (o.get("posSide") or "net").lower()
            if pos_side not in ("long", "short", "net"):
                pos_side = "net"
            ord_type = (o.get("ordType") or "limit").lower()
            if ord_type not in ("market", "limit"):
                ord_type = "limit"
            side = (o.get("side") or "buy").lower()
            if side not in ("buy", "sell"):
                side = "buy"
            orders.append(
                Order(
                    order_id=o.get("ordId", ""),
                    inst_id=o.get("instId", ""),
                    side=side,  # type: ignore[arg-type]
                    pos_side=pos_side,  # type: ignore[arg-type]
                    order_type=ord_type,  # type: ignore[arg-type]
                    size=float(o.get("sz", 0) or 0),
                    price=float(o.get("px", 0) or 0) or None,
                    state=state,
                    filled_size=float(o.get("accFillSz", 0) or 0),
                    tp_trigger_price=float(o.get("tpTriggerPx", 0) or 0) or None,
                    sl_trigger_price=float(o.get("slTriggerPx", 0) or 0) or None,
                    created_at=float(o.get("cTime", 0) or 0) / 1000,
                )
            )
        return orders

    def place_order(self, request: OrderRequest) -> OrderResult:
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
            data = (result.get("data") or [{}])[0]
            if str(data.get("sCode", "0")) != "0":
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
        try:
            result = self._request(
                "POST",
                "/api/v5/trade/cancel-order",
                body={"instId": inst_id, "ordId": order_id},
            )
            data = (result.get("data") or [{}])[0]
            return str(data.get("sCode", "0")) == "0"
        except Exception:
            return False

    def close_position(
        self, inst_id: str, pos_side: Literal["long", "short"]
    ) -> OrderResult:
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
            data = (result.get("data") or [{}])[0]
            if str(result.get("code", "0")) != "0":
                return OrderResult(
                    success=False, error=result.get("msg", "Close failed")
                )
            return OrderResult(success=True, order_id=data.get("ordId"))
        except Exception as e:
            return OrderResult(success=False, error=str(e))


# Preferred Stage 5 name (typed Protocol adapter for OKX demo/live REST).
OkxRestAdapter = OKXRestAdapter
