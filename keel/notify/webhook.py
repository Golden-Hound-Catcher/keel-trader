"""
Simple webhook notifier: POST JSON to a configured URL.

Transport is injectable for unit tests (no live network in CI).
Formats:
  - keel (default): ``{"event": ..., "payload": ...}``
  - discord: ``{"content": text}`` truncated ≤1900 chars (no product Discord bot)
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Literal, Mapping

from keel.notify.protocol import NotifyEvent, NotifyResult

logger = logging.getLogger("keel.notify")

# (method, url, headers, body_bytes) -> response body str
HttpTransport = Callable[[str, str, dict[str, str], bytes | None], str]

NotifyFormat = Literal["keel", "discord"]
DISCORD_CONTENT_MAX = 1900


def _default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
) -> str:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def _normalize_format(fmt: str | None) -> NotifyFormat:
    raw = (fmt or "keel").strip().lower()
    if raw == "discord":
        return "discord"
    return "keel"


def build_webhook_body(event: NotifyEvent, *, format: str = "keel") -> dict[str, Any]:
    """Build POST JSON body for keel or discord webhook shape."""
    fmt = _normalize_format(format)
    if fmt == "discord":
        text = str(event.payload.get("text") or event.event or "")
        if len(text) > DISCORD_CONTENT_MAX:
            text = text[: DISCORD_CONTENT_MAX - 3] + "..."
        return {"content": text}
    return {
        "event": event.event,
        "payload": dict(event.payload),
    }


class WebhookNotifier:
    """
    POST JSON to ``url`` in keel or discord shape.

    Soft-fails: transport / HTTP errors become ``NotifyResult(success=False)``.
    """

    def __init__(
        self,
        url: str,
        *,
        transport: HttpTransport | None = None,
        timeout_note: str = "",
        extra_headers: Mapping[str, str] | None = None,
        format: str = "keel",
    ):
        self._url = (url or "").strip()
        self._transport = transport or _default_transport
        self._extra_headers = dict(extra_headers or {})
        self._timeout_note = timeout_note  # reserved for docs / future
        self._format: NotifyFormat = _normalize_format(format)

    @property
    def name(self) -> str:
        return "webhook"

    @property
    def url(self) -> str:
        return self._url

    @property
    def format(self) -> str:
        return self._format

    def notify(self, event: NotifyEvent) -> NotifyResult:
        if not self._url:
            return NotifyResult(success=False, detail="webhook url empty", skipped=True)

        body_obj = build_webhook_body(event, format=self._format)
        body = json.dumps(body_obj, ensure_ascii=False, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "keel-trader-notify/0.1",
            **self._extra_headers,
        }
        try:
            raw = self._transport("POST", self._url, headers, body)
            detail = f"ok bytes={len(raw)} format={self._format}"
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
