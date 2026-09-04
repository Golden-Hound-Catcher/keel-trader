"""
Public OKX market-data helpers (no API keys).

Stage 4: keep candle/ticker fetches in keel.exchange instead of shell CLI or
ad-hoc urllib copies in legacy scripts / API routers.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


OKX_PUBLIC_BASE = "https://www.okx.com"
DEFAULT_UA = "Keel-Trader/0.1"


def fetch_candles(
    inst_id: str,
    *,
    bar: str = "15m",
    limit: int = 50,
    timeout: float = 5.0,
    base_url: str = OKX_PUBLIC_BASE,
) -> list[list[float]]:
    """
    Fetch OHLCV candles from OKX public API.

    Returns oldest→newest rows as [ts_ms, open, high, low, close, volume].
    """
    url = (
        f"{base_url.rstrip('/')}/api/v5/market/candles"
        f"?instId={inst_id}&bar={bar}&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        raise ValueError(f"HTTP {e.code}: {body[:200]}") from e

    if data.get("code") != "0":
        raise ValueError(data.get("msg", "OKX candles API error"))

    rows = data.get("data", []) or []
    # OKX returns newest-first; normalize to oldest→newest for factors.
    parsed: list[list[float]] = []
    for c in reversed(rows):
        parsed.append([float(x) for x in c[:6]])
    return parsed


def fetch_ticker(inst_id: str, *, timeout: float = 5.0, base_url: str = OKX_PUBLIC_BASE) -> dict[str, Any]:
    """Fetch a public ticker dict for inst_id (raw OKX data[0])."""
    url = f"{base_url.rstrip('/')}/api/v5/market/ticker?instId={inst_id}"
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != "0":
        raise ValueError(data.get("msg", "OKX ticker API error"))
    rows = data.get("data") or []
    if not rows:
        raise ValueError(f"No ticker data for {inst_id}")
    return rows[0]
