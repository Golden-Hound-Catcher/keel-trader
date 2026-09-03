"""
Exchange adapter factory for Keel worker / execution.

Single entry point: use OkxRestAdapter when OKX keys are configured,
otherwise PaperExchange (local simulation). No shell CLI.
"""
from __future__ import annotations

import logging
from typing import Any

from keel.config import Settings, get_settings
from keel.exchange.okx_rest import OkxRestAdapter
from keel.exchange.paper import PaperExchange
from keel.exchange.protocol import ExchangeProtocol

logger = logging.getLogger("keel.exchange")


def build_exchange(
    settings: Settings | None = None,
    *,
    paper_balance: float = 10_000.0,
    force_paper: bool = False,
    transport: Any = None,
) -> ExchangeProtocol:
    """
    Select exchange adapter from Keel settings.

    - KEEL_OKX_* (or OKX_* aliases) present → OkxRestAdapter (demo/live)
    - otherwise → PaperExchange
    - force_paper=True always returns PaperExchange (tests / --once paper)
    """
    settings = settings or get_settings()

    if force_paper or not settings.okx_configured:
        exchange: ExchangeProtocol = PaperExchange(initial_balance=paper_balance)
        logger.info(
            "exchange adapter=%s reason=%s",
            getattr(exchange, "adapter_name", "paper"),
            "force_paper" if force_paper else "no_okx_keys",
        )
        return exchange

    exchange = OkxRestAdapter.from_settings(settings, transport=transport)
    logger.info(
        "exchange adapter=%s env=%s",
        exchange.adapter_name,
        settings.okx_environment,
    )
    return exchange


def describe_exchange(exchange: ExchangeProtocol) -> str:
    """Human-readable adapter label for logs / cycle summary."""
    name = getattr(exchange, "adapter_name", None)
    if name:
        return str(name)
    if isinstance(exchange, PaperExchange):
        return "paper"
    if isinstance(exchange, OkxRestAdapter):
        return exchange.adapter_name
    return type(exchange).__name__
