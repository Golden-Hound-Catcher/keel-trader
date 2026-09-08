"""
Public OKX market-data helpers (no API keys).

Stage 4: keep candle/ticker fetches in keel.exchange instead of shell CLI or
ad-hoc urllib copies in legacy scripts / API routers.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


OKX_PUBLIC_BASE = "https://www.okx.com"
DEFAULT_UA = "Keel-Trader/0.1"

# OKX /market/candles and /market/history-candles max limit per request.
_CANDLES_MAX_LIMIT = 300
_HISTORY_CANDLES_MAX_LIMIT = 100

_BAR_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1H": 3600,
    "2H": 7200,
    "4H": 14400,
    "6H": 21600,
    "12H": 43200,
    "1D": 86400,
}


def bar_duration_seconds(bar: str) -> int:
    """Return bar length in seconds; unknown bars default to 900 (15m)."""
    return int(_BAR_SECONDS.get(str(bar).strip(), 900))


def _parse_candle_rows(rows: list[Any]) -> list[list[float]]:
    """OKX returns newest-first; normalize to oldest→newest [ts_ms,o,h,l,c,vol]."""
    parsed: list[list[float]] = []
    for c in reversed(rows or []):
        if not c or len(c) < 6:
            continue
        parsed.append([float(x) for x in c[:6]])
    return parsed


def _get_json(url: str, *, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        raise ValueError(f"HTTP {e.code}: {body[:200]}") from e


def fetch_candles(
    inst_id: str,
    *,
    bar: str = "15m",
    limit: int = 50,
    timeout: float = 5.0,
    base_url: str = OKX_PUBLIC_BASE,
    after: int | str | None = None,
    before: int | str | None = None,
    endpoint: str = "candles",
) -> list[list[float]]:
    """
    Fetch OHLCV candles from OKX public API.

    Returns oldest→newest rows as [ts_ms, open, high, low, close, volume].

    ``after`` / ``before`` are OKX pagination timestamps in ms:
      - after  → records *earlier than* this ts (older pages)
      - before → records *newer than* this ts

    ``endpoint``: ``candles`` (recent) or ``history-candles`` (deeper history).
    """
    max_limit = (
        _HISTORY_CANDLES_MAX_LIMIT
        if endpoint == "history-candles"
        else _CANDLES_MAX_LIMIT
    )
    lim = max(1, min(int(limit), max_limit))
    params: dict[str, str] = {
        "instId": str(inst_id),
        "bar": str(bar),
        "limit": str(lim),
    }
    if after is not None and str(after) != "":
        params["after"] = str(int(after))
    if before is not None and str(before) != "":
        params["before"] = str(int(before))
    path = "history-candles" if endpoint == "history-candles" else "candles"
    url = (
        f"{base_url.rstrip('/')}/api/v5/market/{path}?"
        + urllib.parse.urlencode(params)
    )
    data = _get_json(url, timeout=timeout)
    if data.get("code") != "0":
        raise ValueError(data.get("msg", "OKX candles API error"))
    return _parse_candle_rows(data.get("data", []) or [])


def fetch_candles_paginated(
    inst_id: str,
    *,
    bar: str = "15m",
    max_bars: int = 700,
    timeout: float = 10.0,
    base_url: str = OKX_PUBLIC_BASE,
    sleep_s: float = 0.05,
    drop_unconfirmed_tail: bool = True,
) -> list[list[float]]:
    """
    Paginate OKX public candles oldest→newest up to ``max_bars``.

    Walks ``/market/candles`` then ``/market/history-candles`` via ``after``
    (older pages). Optionally drops the newest bar when it may still be forming
    (live ``candles`` endpoint includes the in-progress candle).
    """
    want = max(1, int(max_bars))
    newest_first_batches: list[list[list[float]]] = []
    after: int | None = None
    # Recent endpoint first (up to ~300/page), then history (100/page).
    for endpoint, page_limit in (
        ("candles", _CANDLES_MAX_LIMIT),
        ("history-candles", _HISTORY_CANDLES_MAX_LIMIT),
    ):
        # Switch to history once recent is exhausted or we still need more.
        while True:
            have = sum(len(b) for b in newest_first_batches)
            if have >= want:
                break
            lim = page_limit
            try:
                batch = fetch_candles(
                    inst_id,
                    bar=bar,
                    limit=lim,
                    timeout=timeout,
                    base_url=base_url,
                    after=after,
                    endpoint=endpoint,
                )
            except ValueError:
                if endpoint == "candles" and after is not None:
                    break  # fall through to history-candles
                raise
            if not batch:
                break
            # batch is oldest→newest; track oldest ts for next older page.
            oldest_ts = int(batch[0][0])
            if after is not None and oldest_ts >= after:
                break  # no progress
            newest_first_batches.append(batch)
            after = oldest_ts
            if len(batch) < lim:
                break
            if sleep_s > 0:
                time.sleep(sleep_s)
        have = sum(len(b) for b in newest_first_batches)
        if have >= want:
            break

    if not newest_first_batches:
        return []

    # Batches collected newest-page-first; each page is oldest→newest.
    # Dedupe by open-ts and sort ascending (robust to boundary overlap).
    by_ts: dict[int, list[float]] = {}
    for batch in newest_first_batches:
        for row in batch:
            by_ts[int(row[0])] = row
    merged = [by_ts[k] for k in sorted(by_ts)]

    if drop_unconfirmed_tail and merged:
        # Drop newest if its close time is still in the future (forming bar).
        bar_s = bar_duration_seconds(bar)
        newest_open_ms = float(merged[-1][0])
        close_ts = newest_open_ms / 1000.0 + bar_s
        if close_ts > time.time() - 1.0:
            merged = merged[:-1]

    if len(merged) > want:
        merged = merged[-want:]
    return merged


def fetch_ticker(inst_id: str, *, timeout: float = 5.0, base_url: str = OKX_PUBLIC_BASE) -> dict[str, Any]:
    """Fetch a public ticker dict for inst_id (raw OKX data[0])."""
    url = f"{base_url.rstrip('/')}/api/v5/market/ticker?instId={inst_id}"
    data = _get_json(url, timeout=timeout)
    if data.get("code") != "0":
        raise ValueError(data.get("msg", "OKX ticker API error"))
    rows = data.get("data") or []
    if not rows:
        raise ValueError(f"No ticker data for {inst_id}")
    return rows[0]
