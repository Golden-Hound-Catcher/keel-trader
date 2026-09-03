"""Position endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from keel.config import get_settings
from keel.exchange import OKXRestAdapter, PaperAdapter

router = APIRouter()


def _get_exchange():
    """Get exchange adapter based on configuration."""
    settings = get_settings()
    if settings.okx_configured:
        return OKXRestAdapter(
            api_key=settings.okx_api_key,
            secret_key=settings.okx_secret_key,
            passphrase=settings.okx_passphrase,
            demo=settings.is_demo,
        )
    return PaperAdapter()


@router.get("/positions")
def get_positions():
    """Get current positions."""
    try:
        exchange = _get_exchange()
        positions = exchange.get_positions()
        return {
            "count": len(positions),
            "positions": [
                {
                    "inst_id": p.inst_id,
                    "side": p.side,
                    "size": p.size,
                    "avg_price": p.avg_price,
                    "mark_price": p.mark_price,
                    "upl": p.upl,
                    "upl_ratio": p.upl_ratio,
                    "leverage": p.leverage,
                    "margin": p.margin,
                }
                for p in positions
            ],
            "source": "okx" if isinstance(exchange, OKXRestAdapter) else "paper",
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/balance")
def get_balance():
    """Get account balance."""
    try:
        exchange = _get_exchange()
        balance = exchange.get_balance()
        return {
            "total_equity": balance.total_equity,
            "available": balance.available_balance,
            "cash": balance.cash_balance,
            "unrealized_pnl": balance.unrealized_pnl,
            "margin_usage_pct": balance.margin_usage_pct,
            "source": "okx" if isinstance(exchange, OKXRestAdapter) else "paper",
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
