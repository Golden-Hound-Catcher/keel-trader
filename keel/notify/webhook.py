"""
Simple webhook notifier: POST JSON to a configured URL.

Transport is injectable for unit tests (no live network in CI).
Payload shape is Keel-native (event + payload); not vendor-specific.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from keel.notify.protocol import NotifyEvent, NotifyResult

logger = logging.getLogger("keel.notify")

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


class WebhookNotifier:
    """
    POST ``{"event": ..., "payload": ...}`` to ``url``.

    Soft-fails: transport / HTTP errors become ``NotifyResult(success=False)``.
    """

    def __init__(
        self,
        url: str,
        *,
        transport: HttpTransport | None = None,
        timeout_note: str = "",
        extra_headers: Mapping[str, str] | None = None,
    ):
        self._url = (url or "").strip()
        self._transport = transport or _default_transport
        self._extra_headers = dict(extra_headers or {})
        self._timeout_note = timeout_note  # reserved for docs / future

    @property
    def name(self) -> str:
        return "webhook"

    @property
    def url(self) -> str:
        return self._url

    def notify(self, event: NotifyEvent) -> NotifyResult:
        if not self._url:
            return NotifyResult(success=False, detail="webhook url empty", skipped=True)

        body_obj: dict[str, Any] = {
            "event": event.event,
            "payload": dict(event.payload),
        }
        body = json.dumps(body_obj, ensure_ascii=False, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "keel-trader-notify/0.1",
            **self._extra_headers,
        }
        try:
            raw = self._transport("POST", self._url, headers, body)
            detail = f"ok bytes={len(raw)}"
            logger.info("notify webhook event=%s detail=%s", event.event, detail)
            return NotifyResult(success=True, detail=detail)
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            logger.warning("notify webhook failed event=%s detail=%s", event.event, detail)
            return NotifyResult(success=False, detail=detail)
        except Exception as exc:  # noqa: BLE001 — soft-fail notify path
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("notify webhook failed event=%s detail=%s", event.event, detail)
            return NotifyResult(success=False, detail=detail)
