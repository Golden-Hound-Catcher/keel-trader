"""
Typed Protocol for optional cycle notifications.

Notifier is a replaceable port: Null (default) or Webhook POST JSON.
Does not expand into QQ / WeCom / Telegram product channels; those stay
outside Keel core (see SPEC non-goals). Independent of r20_gateway.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class NotifyResult:
    """Outcome of a single notify attempt."""

    success: bool
    detail: str = ""
    skipped: bool = False


@dataclass(frozen=True)
class NotifyEvent:
    """
    Lightweight notification envelope.

    ``event`` is a stable name (e.g. ``trader_cycle_complete``).
    ``payload`` is JSON-serializable cycle summary / metadata.
    """

    event: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Notifier(Protocol):
    """
    Replaceable notification port.

    Worker cycle may call ``notify`` after a completed tick when configured.
    Implementations must not raise into the trading path for soft failures
    when used via the factory defaults (catch and return NotifyResult).
    """

    @property
    def name(self) -> str:
        """Stable adapter label for logs / cycle summary."""
        ...

    def notify(self, event: NotifyEvent) -> NotifyResult:
        """Deliver one notification. Prefer returning failure over raising."""
        ...
