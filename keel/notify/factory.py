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

# Cap deny/error detail lists in webhook payloads (matches cycle caps).
NOTIFY_LIST_CAP = 20


def build_notifier(
    settings: Settings | None = None,
    *,
    transport: HttpTransport | None = None,
    force_null: bool = False,
) -> Notifier:
    """
    Select notifier from Keel settings.

    - force_null → NullNotifier
    - notify_webhook_url set → WebhookNotifier (respects notify_format)
    - otherwise → NullNotifier
    """
    settings = settings or get_settings()
    if force_null:
        notifier: Notifier = NullNotifier()
        logger.info("notifier=%s reason=force_null", notifier.name)
        return notifier

    url = (getattr(settings, "notify_webhook_url", "") or "").strip()
    if url:
        fmt = getattr(settings, "notify_format", "keel") or "keel"
        notifier = WebhookNotifier(url, transport=transport, format=str(fmt))
        logger.info(
            "notifier=%s url_configured=1 format=%s alerts_only=%s",
            notifier.name,
            fmt,
            bool(getattr(settings, "notify_alerts_only", False)),
        )
        return notifier

    notifier = NullNotifier()
    logger.info("notifier=%s reason=no_webhook_url", notifier.name)
    return notifier


def describe_notifier(notifier: Notifier) -> str:
    """Human-readable notifier label for logs / cycle summary."""
    return str(getattr(notifier, "name", type(notifier).__name__))


def _cap_list(items: Any, cap: int = NOTIFY_LIST_CAP) -> list[Any]:
    if not isinstance(items, list):
        return []
    return list(items[: max(0, int(cap))])


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def notify_severity(*, ok: bool, risk_denies: int, error_count: int) -> str:
    """Map cycle outcome to ``ok`` | ``warn`` | ``error``."""
    if (not ok) or error_count > 0:
        return "error"
    if risk_denies > 0:
        return "warn"
    return "ok"


def notify_text_line(payload: dict[str, Any]) -> str:
    """Short human line for chat apps (Discord content, etc.)."""
    sev = payload.get("severity") or "ok"
    mode = payload.get("mode") or "?"
    ok = payload.get("ok")
    denies = payload.get("risk_denies", 0)
    errs = payload.get("error_count", 0)
    parts = [
        f"Keel [{sev}]",
        f"mode={mode}",
        f"ok={ok}",
        f"denies={denies}",
        f"errors={errs}",
    ]
    pnl = payload.get("daily_pnl")
    if pnl is not None:
        parts.append(f"pnl={pnl}")
    ms = payload.get("duration_ms")
    if ms is not None:
        parts.append(f"{ms}ms")
    return " ".join(str(p) for p in parts)


def cycle_notify_payload(summary: dict[str, Any]) -> dict[str, Any]:
    """
    Compact JSON-safe summary for webhook bodies.

    Enriches with risk/error/duration/alert/severity/text for actionable hooks.
    Keeps full ``results`` list but drops oversized / path-only fields.
    """
    cs = summary.get("cycle_summary") if isinstance(summary.get("cycle_summary"), dict) else {}
    risk_denies = _as_int(summary.get("risk_denies", cs.get("risk_denies", 0)))
    error_count = _as_int(summary.get("error_count", cs.get("error_count", 0)))
    duration_ms = _as_int(summary.get("duration_ms", cs.get("duration_ms", 0)))
    risk_deny_reasons = _cap_list(
        summary.get("risk_deny_reasons", cs.get("risk_deny_reasons", [])),
        NOTIFY_LIST_CAP,
    )
    errors = _cap_list(summary.get("errors", cs.get("errors", [])), NOTIFY_LIST_CAP)
    ok = bool(summary.get("ok", True))
    alert = (not ok) or risk_denies > 0 or error_count > 0
    severity = notify_severity(ok=ok, risk_denies=risk_denies, error_count=error_count)

    payload: dict[str, Any] = {
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
        "risk_denies": risk_denies,
        "risk_deny_reasons": risk_deny_reasons,
        "error_count": error_count,
        "errors": errors,
        "duration_ms": duration_ms,
        "alert": alert,
        "severity": severity,
    }
    payload["text"] = notify_text_line(payload)
    return payload
