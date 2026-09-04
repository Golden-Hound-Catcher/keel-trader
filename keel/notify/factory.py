"""
Notifier factory for Keel worker.

Empty ``KEEL_NOTIFY_WEBHOOK_URL`` (default) → NullNotifier.
Non-empty URL → WebhookNotifier. No QQ/WeCom/Telegram expansion.
"""
from __future__ import annotations

import logging
from typing import Any

from keel.config import Settings, get_settings
from keel.notify.null import NullNotifier
from keel.notify.protocol import Notifier
from keel.notify.webhook import HttpTransport, WebhookNotifier

logger = logging.getLogger("keel.notify")


def build_notifier(
    settings: Settings | None = None,
    *,
    transport: HttpTransport | None = None,
    force_null: bool = False,
) -> Notifier:
    """
    Select notifier from Keel settings.

    - force_null → NullNotifier
    - notify_webhook_url set → WebhookNotifier
    - otherwise → NullNotifier
    """
    settings = settings or get_settings()
    if force_null:
        notifier: Notifier = NullNotifier()
        logger.info("notifier=%s reason=force_null", notifier.name)
        return notifier

    url = (getattr(settings, "notify_webhook_url", "") or "").strip()
    if url:
        notifier = WebhookNotifier(url, transport=transport)
        logger.info("notifier=%s url_configured=1", notifier.name)
        return notifier

    notifier = NullNotifier()
    logger.info("notifier=%s reason=no_webhook_url", notifier.name)
    return notifier


def describe_notifier(notifier: Notifier) -> str:
    """Human-readable notifier label for logs / cycle summary."""
    return str(getattr(notifier, "name", type(notifier).__name__))


def cycle_notify_payload(summary: dict[str, Any]) -> dict[str, Any]:
    """
    Compact JSON-safe summary for webhook bodies.

    Keeps full ``results`` list but drops oversized / path-only fields.
    """
    return {
        "ok": summary.get("ok"),
        "mode": summary.get("mode"),
        "adapter": summary.get("adapter"),
        "policy": summary.get("policy"),
        "policy_success": summary.get("policy_success"),
        "branding": summary.get("branding"),
        "instruments": summary.get("instruments"),
        "daily_pnl": summary.get("daily_pnl"),
        "positions": summary.get("positions"),
        "results": summary.get("results"),
        "notifier": summary.get("notifier"),
    }
