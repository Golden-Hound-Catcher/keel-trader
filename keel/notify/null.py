"""No-op notifier used when notifications are not configured."""
from __future__ import annotations

from keel.notify.protocol import NotifyEvent, NotifyResult


class NullNotifier:
    """Silent default — always succeeds as skipped."""

    @property
    def name(self) -> str:
        return "null"

    def notify(self, event: NotifyEvent) -> NotifyResult:
        return NotifyResult(success=True, detail="null", skipped=True)
