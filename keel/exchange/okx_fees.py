"""
OKX-official fee helpers for Q3.3 fee-aware shadow markout.

Trading fees (USDT-margined SWAP):
  Prefer live account rates via signed GET /api/v5/account/trade-fee?instType=SWAP.
  For instruments like BTC-USDT-SWAP use **makerU / takerU** (USDT-margined),
  NOT crypto-margined maker/taker. See:
  https://www.okx.com/docs-v5/en/#trading-account-rest-api-get-fee-rates
  https://www.okx.com/help/okx-will-make-changes-to-the-get-fee-rates-interface
  Fee schedule (Regular fallback): https://www.okx.com/fees
  How fees are calculated: https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee

OKX trade-fee API rates are typically negative when you *pay* and positive for
rebates. We convert to markout ``*_bps`` where **positive = cost**, **negative =
rebate**, so ``net_bps = gross_bps - fee_bps`` preserves rebate sign.

Fallback = OKX Regular USDT-margined futures/swap: maker 0.0200% (2 bps),
taker 0.0500% (5 bps). Cache ~1h; soft-fail when keys missing / call fails.

Funding (separate from trading fees):
  Funding fee = position value × funding rate; only if holding at settlement
  (often 00:00/08:00/16:00 UTC). Public: GET /api/v5/public/funding-rate.
  FAQ: https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from keel.exchange.okx_rest import HttpTransport, OkxRestAdapter

logger = logging.getLogger("keel.exchange.okx_fees")

# OKX Regular USDT-margined futures/swap schedule (positive = cost in bps).
REGULAR_USDT_SWAP_MAKER_BPS = 2.0
REGULAR_USDT_SWAP_TAKER_BPS = 5.0

FEE_CACHE_TTL_SECONDS = 3600.0
FUNDING_CACHE_TTL_SECONDS = 300.0

# Standard UTC perpetual funding hours (many OKX swaps).
STANDARD_FUNDING_HOURS_UTC: tuple[int, ...] = (0, 8, 16)

FeeRole = Literal["taker", "maker"]
FeeSource = Literal["live", "fallback", "override"]

OKX_PUBLIC_BASE = "https://www.okx.com"
_DEFAULT_UA = "Keel-Trader/0.1"


@dataclass(frozen=True)
class SwapFeeRates:
    """Resolved USDT-margined SWAP maker/taker rates in markout bps."""

    maker_bps: float
    taker_bps: float
    source: FeeSource
    level: str | None = None
    inst_type: str = "SWAP"
    margin: str = "USDT"


# Module-level trade-fee cache.
_fee_cache_at: float = 0.0
_fee_cache_fp: str = ""
_fee_cache_rates: SwapFeeRates | None = None

# Per-inst funding rate cache (rate as OKX decimal, e.g. 0.0001).
_funding_cache: dict[str, tuple[float, float | None]] = {}


def clear_fee_caches() -> None:
    """Drop cached trade-fee + funding rates (tests / key rotation)."""
    global _fee_cache_at, _fee_cache_fp, _fee_cache_rates, _funding_cache
    _fee_cache_at = 0.0
    _fee_cache_fp = ""
    _fee_cache_rates = None
    _funding_cache = {}


def okx_rate_to_cost_bps(okx_rate: float) -> float:
    """
    Convert OKX trade-fee API rate to markout cost bps.

    OKX: negative rate ≈ you pay; positive ≈ rebate.
    Ours: positive bps = cost, negative = rebate.
    """
    return float(okx_rate) * -10_000.0


def _fingerprint(settings: Any) -> str:
    api = (getattr(settings, "okx_api_key", "") or "").strip()
    secret = (getattr(settings, "okx_secret_key", "") or "").strip()
    phrase = (getattr(settings, "okx_passphrase", "") or "").strip()
    env = getattr(settings, "okx_environment", "") or ""
    return f"{env}|{bool(api)}|{len(api)}|{len(secret)}|{len(phrase)}"


def _parse_u_rates(data_row: dict[str, Any]) -> tuple[float, float, str | None] | None:
    """Extract makerU/takerU → (maker_bps, taker_bps, level)."""
    raw_maker = data_row.get("makerU")
    raw_taker = data_row.get("takerU")
    if raw_maker is None or raw_taker is None or raw_maker == "" or raw_taker == "":
        return None
    try:
        maker_bps = okx_rate_to_cost_bps(float(raw_maker))
        taker_bps = okx_rate_to_cost_bps(float(raw_taker))
    except (TypeError, ValueError):
        return None
    level = data_row.get("level")
    level_s = str(level) if level is not None and str(level) != "" else None
    return maker_bps, taker_bps, level_s


def fetch_live_swap_usdt_fees(
    settings: Any,
    *,
    transport: HttpTransport | None = None,
) -> SwapFeeRates | None:
    """
    Signed GET /api/v5/account/trade-fee?instType=SWAP → makerU/takerU.

    Returns None on missing keys / soft failure (caller falls back).
    """
    configured = bool(getattr(settings, "okx_configured", False))
    if not configured:
        api = (getattr(settings, "okx_api_key", "") or "").strip()
        secret = (getattr(settings, "okx_secret_key", "") or "").strip()
        phrase = (getattr(settings, "okx_passphrase", "") or "").strip()
        configured = bool(api and secret and phrase)
    if not configured:
        return None
    try:
        adapter = OkxRestAdapter.from_settings(settings, transport=transport)
        result = adapter._request(
            "GET",
            "/api/v5/account/trade-fee",
            params={"instType": "SWAP"},
            signed=True,
        )
        rows = result.get("data") or []
        if not rows or not isinstance(rows[0], dict):
            return None
        parsed = _parse_u_rates(rows[0])
        if parsed is None:
            logger.info(
                "OKX trade-fee missing makerU/takerU; soft-fail to Regular fallback"
            )
            return None
        maker_bps, taker_bps, level = parsed
        return SwapFeeRates(
            maker_bps=maker_bps,
            taker_bps=taker_bps,
            source="live",
            level=level,
        )
    except Exception as exc:
        logger.info("OKX trade-fee soft-fail: %s", exc)
        return None


def get_swap_usdt_fee_rates(
    settings: Any | None = None,
    *,
    transport: HttpTransport | None = None,
    force_refresh: bool = False,
    cache_ttl: float = FEE_CACHE_TTL_SECONDS,
) -> SwapFeeRates:
    """
    Resolve USDT-SWAP maker/taker bps: override → live (cached) → Regular fallback.

    Env overrides ``KEEL_SHADOW_MAKER_FEE_BPS`` / ``KEEL_SHADOW_TAKER_FEE_BPS``
    (via settings) force source=override when either is set.
    """
    global _fee_cache_at, _fee_cache_fp, _fee_cache_rates

    if settings is None:
        from keel.config import get_settings

        settings = get_settings()

    maker_override = getattr(settings, "shadow_maker_fee_bps", None)
    taker_override = getattr(settings, "shadow_taker_fee_bps", None)
    if maker_override is not None or taker_override is not None:
        return SwapFeeRates(
            maker_bps=float(
                maker_override
                if maker_override is not None
                else REGULAR_USDT_SWAP_MAKER_BPS
            ),
            taker_bps=float(
                taker_override
                if taker_override is not None
                else REGULAR_USDT_SWAP_TAKER_BPS
            ),
            source="override",
            level=None,
        )

    fp = _fingerprint(settings)
    now = time.time()
    if (
        not force_refresh
        and _fee_cache_rates is not None
        and _fee_cache_fp == fp
        and (now - _fee_cache_at) < cache_ttl
    ):
        return _fee_cache_rates

    live = fetch_live_swap_usdt_fees(settings, transport=transport)
    if live is not None:
        _fee_cache_at = now
        _fee_cache_fp = fp
        _fee_cache_rates = live
        return live

    fallback = SwapFeeRates(
        maker_bps=REGULAR_USDT_SWAP_MAKER_BPS,
        taker_bps=REGULAR_USDT_SWAP_TAKER_BPS,
        source="fallback",
        level="Regular",
    )
    # Cache fallback too so we do not retry hammer on every /stats call.
    _fee_cache_at = now
    _fee_cache_fp = fp
    _fee_cache_rates = fallback
    return fallback


def role_fee_bps(rates: SwapFeeRates, role: FeeRole) -> float:
    """Single-leg fee in bps for the configured role."""
    if role == "maker":
        return float(rates.maker_bps)
    return float(rates.taker_bps)


def build_fee_model(
    settings: Any | None = None,
    *,
    transport: HttpTransport | None = None,
    funding_note: str | None = None,
) -> dict[str, Any]:
    """
    Top-level ``fee_model`` payload for /stats/shadow*.

    ``open_fee_bps`` = one leg at role rate; ``round_trip_fee_bps`` = 2 × role
    (sign preserved so negative maker rebate → negative RT).
    """
    if settings is None:
        from keel.config import get_settings

        settings = get_settings()

    role_raw = str(getattr(settings, "shadow_fee_role", "taker") or "taker").lower()
    role: FeeRole = "maker" if role_raw == "maker" else "taker"
    rates = get_swap_usdt_fee_rates(settings, transport=transport)
    open_bps = role_fee_bps(rates, role)
    rt_bps = 2.0 * open_bps
    note = funding_note or (
        "Funding is separate from trading fees; v1 applies at most one standard "
        "UTC boundary (00/08/16) when fill→horizon crosses and a public rate is "
        "available, else funding_applied=false (short horizons usually 0)."
    )
    out: dict[str, Any] = {
        "source": rates.source,
        "inst_type": rates.inst_type,
        "margin": rates.margin,
        "maker_bps": rates.maker_bps,
        "taker_bps": rates.taker_bps,
        "role": role,
        "open_fee_bps": open_bps,
        "round_trip_fee_bps": rt_bps,
        "funding_note": note,
    }
    if rates.level is not None:
        out["level"] = rates.level
    return out


def crosses_standard_funding_boundary(fill_ts: float, end_ts: float) -> bool:
    """True if (fill_ts, end_ts] contains a 00/08/16 UTC funding settlement."""
    if end_ts <= fill_ts:
        return False
    # Walk hour buckets from just after fill to end.
    start_hour = int(fill_ts // 3600) + 1
    end_hour = int(end_ts // 3600)
    for hour_idx in range(start_hour, end_hour + 1):
        boundary = hour_idx * 3600
        if boundary <= fill_ts or boundary > end_ts:
            continue
        # hour_idx mod 24 in UTC
        hour_utc = hour_idx % 24
        if hour_utc in STANDARD_FUNDING_HOURS_UTC:
            return True
    return False


def fetch_public_funding_rate(
    inst_id: str,
    *,
    timeout: float = 5.0,
    base_url: str = OKX_PUBLIC_BASE,
    transport: Any | None = None,
) -> float | None:
    """
    Public funding rate as OKX decimal (e.g. 0.0001), or None on failure.

    Does not invent rates. GET /api/v5/public/funding-rate?instId=
    """
    global _funding_cache
    now = time.time()
    cached = _funding_cache.get(inst_id)
    if cached is not None and (now - cached[0]) < FUNDING_CACHE_TTL_SECONDS:
        return cached[1]

    path = f"/api/v5/public/funding-rate?instId={inst_id}"
    rate: float | None = None
    try:
        if transport is not None:
            # Test transport: (method, url, headers, body) -> str
            url = base_url.rstrip("/") + path
            raw = transport("GET", url, {"User-Agent": _DEFAULT_UA}, None)
            payload = json.loads(raw)
        else:
            url = base_url.rstrip("/") + path
            req = urllib.request.Request(url, headers={"User-Agent": _DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("code") not in (None, "0", 0):
            rate = None
        else:
            rows = payload.get("data") or []
            if (
                rows
                and isinstance(rows[0], dict)
                and rows[0].get("fundingRate") not in (None, "")
            ):
                rate = float(rows[0]["fundingRate"])
    except Exception as exc:
        logger.info("public funding-rate soft-fail for %s: %s", inst_id, exc)
        rate = None

    _funding_cache[inst_id] = (now, rate)
    return rate


def funding_markout_bps(action: str, funding_rate: float) -> float:
    """
    Directional funding impact in bps on trader markout.

    Positive funding rate → longs pay shorts.
    Long: −rate×1e4; Short: +rate×1e4.
    """
    bps = float(funding_rate) * 10_000.0
    act = str(action or "").upper().strip()
    if act in ("BUY_LONG", "LONG", "BUY"):
        return -bps
    if act in ("SELL_SHORT", "SHORT", "SELL"):
        return bps
    return 0.0


__all__ = [
    "REGULAR_USDT_SWAP_MAKER_BPS",
    "REGULAR_USDT_SWAP_TAKER_BPS",
    "FEE_CACHE_TTL_SECONDS",
    "STANDARD_FUNDING_HOURS_UTC",
    "SwapFeeRates",
    "clear_fee_caches",
    "okx_rate_to_cost_bps",
    "fetch_live_swap_usdt_fees",
    "get_swap_usdt_fee_rates",
    "role_fee_bps",
    "build_fee_model",
    "crosses_standard_funding_boundary",
    "fetch_public_funding_rate",
    "funding_markout_bps",
]
