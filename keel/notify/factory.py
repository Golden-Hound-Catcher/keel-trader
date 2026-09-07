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


def notify_severity(
    *,
    ok: bool,
    risk_denies: int,
    error_count: int,
    near_signal: bool = False,
) -> str:
    """Map cycle outcome to ``ok`` | ``warn`` | ``error``."""
    if (not ok) or error_count > 0:
        return "error"
    if risk_denies > 0 or near_signal:
        return "warn"
    return "ok"


def _near_signal_alert_reasons(results: Any) -> list[str]:
    """
    Near-signal / fire reasons that should flip ``alert=True``.

    - action BUY_LONG / SELL_SHORT (order intent fired)
    - signal_diag.nearest in {long, short} with len(missing) <= 2
    """
    if not isinstance(results, list):
        return []
    reasons: list[str] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        inst = str(row.get("inst_id") or "?")
        action = str(row.get("action") or "")
        if action in ("BUY_LONG", "SELL_SHORT"):
            reasons.append(f"near_signal:{inst}:action={action}")
            continue
        diag = row.get("signal_diag")
        if not isinstance(diag, dict):
            continue
        nearest = diag.get("nearest")
        missing = diag.get("missing") if isinstance(diag.get("missing"), list) else []
        if nearest in ("long", "short") and len(missing) <= 2:
            reasons.append(
                f"near_signal:{inst}:nearest={nearest}:missing={len(missing)}"
            )
    return reasons


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
    near_n = _as_int(payload.get("near_signal_count", 0))
    if near_n > 0 or payload.get("near_signal"):
        parts.append(f"near_signal={near_n or 1}")
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
    ``alert=True`` on deny/error **or** near-signal (nearest long/short with
    ``len(missing)<=2``) / BUY_LONG|SELL_SHORT fire. Keeps full ``results``
    list but drops oversized / path-only fields.
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
    results = summary.get("results")
    near_reasons = _near_signal_alert_reasons(results)
    near_signal = bool(near_reasons)
    alert_reasons: list[str] = []
    if not ok:
        alert_reasons.append("ok_false")
    if risk_denies > 0:
        alert_reasons.append("risk_denies")
    if error_count > 0:
        alert_reasons.append("errors")
    alert_reasons.extend(near_reasons)
    alert = (not ok) or risk_denies > 0 or error_count > 0 or near_signal
    severity = notify_severity(
        ok=ok,
        risk_denies=risk_denies,
        error_count=error_count,
        near_signal=near_signal,
    )

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
        "results": results,
        "notifier": summary.get("notifier"),
        "risk_denies": risk_denies,
        "risk_deny_reasons": risk_deny_reasons,
        "error_count": error_count,
        "errors": errors,
        "duration_ms": duration_ms,
        "near_signal": near_signal,
        "near_signal_count": len(near_reasons),
        "alert_reasons": _cap_list(alert_reasons, NOTIFY_LIST_CAP),
        "alert": alert,
        "severity": severity,
    }
    payload["text"] = notify_text_line(payload)
    return payload
