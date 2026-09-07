"""
OKX API key capability probe (Q1).

Classifies configured keys as read-only vs trade-capable (trade-read) without
placing, cancelling, or closing any orders. Results are cached ~60s so status
polling does not hammer OKX.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from keel.exchange.okx_rest import HttpTransport, OkxRestAdapter

logger = logging.getLogger("keel.exchange.capability")

CapabilityLevel = Literal["none", "paper", "read", "trade", "error"]

CACHE_TTL_SECONDS = 60.0

# OKX / HTTP signals that commonly mean the key lacks trade (or any) permission.
_PERMISSION_CODE_RE = re.compile(
    r"\b(50110|50113|50105|50111|401)\b",
    re.IGNORECASE,
)
_PERMISSION_KEYWORD_RE = re.compile(
    r"permission|unauthorized|not\s+authorized|no\s+permission|"
    r"access\s+denied|forbidden|lack(?:s|ing)?\s+permission|"
    r"api\s*key.*(?:trade|permission)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CapabilityProbe:
    """Result of an OKX key capability probe."""

    level: CapabilityLevel
    detail: str = ""


# Module-level cache (settings fingerprint → avoid cross-env stale hits).
_cache_at: float = 0.0
_cache_fingerprint: str = ""
_cache_result: CapabilityProbe | None = None


def clear_capability_cache() -> None:
    """Drop cached probe result (tests / after key rotation)."""
    global _cache_at, _cache_fingerprint, _cache_result
    _cache_at = 0.0
    _cache_fingerprint = ""
    _cache_result = None


def is_permission_denied(exc_or_msg: BaseException | str) -> bool:
    """True when an OKX/HTTP error looks like missing API permission."""
    text = str(exc_or_msg)
    if _PERMISSION_CODE_RE.search(text):
        return True
    if _PERMISSION_KEYWORD_RE.search(text):
        return True
    return False


def _fingerprint(settings: Any) -> str:
    """Non-secret cache key: env + whether keys present + key lengths."""
    api = (getattr(settings, "okx_api_key", "") or "").strip()
    secret = (getattr(settings, "okx_secret_key", "") or "").strip()
    phrase = (getattr(settings, "okx_passphrase", "") or "").strip()
    env = getattr(settings, "okx_environment", "") or ""
    return f"{env}|{bool(api)}|{len(api)}|{len(secret)}|{len(phrase)}"


def _short_reason(exc: BaseException, *, limit: int = 160) -> str:
    msg = str(exc).strip().replace("\n", " ")
    if len(msg) > limit:
        return msg[: limit - 1] + "…"
    return msg


def probe_okx_capability(
    settings: Any,
    *,
    transport: HttpTransport | None = None,
    force_refresh: bool = False,
    force_paper: bool = False,
    cache_ttl: float = CACHE_TTL_SECONDS,
) -> CapabilityProbe:
    """
    Probe whether OKX keys can read account and/or trade endpoints.

    Levels:
      - none  — keys not configured
      - paper — explicit paper exchange path (no live OKX probe)
      - read  — balance OK; orders-pending fails with permission-like error
      - trade — orders-pending succeeds (at least trade-read)
      - error — unexpected failure (detail carries short reason)

    Never calls place_order / cancel_order / close_position.
    """
    global _cache_at, _cache_fingerprint, _cache_result

    if force_paper:
        return CapabilityProbe("paper", "paper exchange")

    if not getattr(settings, "okx_configured", False):
        return CapabilityProbe("none", "keys not configured")

    fp = _fingerprint(settings)
    now = time.monotonic()
    if (
        not force_refresh
        and _cache_result is not None
        and _cache_fingerprint == fp
        and (now - _cache_at) < cache_ttl
    ):
        return _cache_result

    result = _probe_live(settings, transport=transport)
    _cache_at = now
    _cache_fingerprint = fp
    _cache_result = result
    return result


def _probe_live(
    settings: Any,
    *,
    transport: HttpTransport | None,
) -> CapabilityProbe:
    adapter = OkxRestAdapter.from_settings(settings, transport=transport)

    try:
        adapter.get_balance()
    except Exception as exc:
        reason = _short_reason(exc)
        if is_permission_denied(exc):
            # Account read itself denied — treat as unexpected for our ladder.
            return CapabilityProbe("error", f"balance permission denied: {reason}")
        return CapabilityProbe("error", f"balance failed: {reason}")

    try:
        adapter.get_open_orders()
    except Exception as exc:
        reason = _short_reason(exc)
        if is_permission_denied(exc):
            return CapabilityProbe("read", reason)
        return CapabilityProbe("error", f"orders-pending failed: {reason}")

    return CapabilityProbe("trade", "orders-pending ok")
