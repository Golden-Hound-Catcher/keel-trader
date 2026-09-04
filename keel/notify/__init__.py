"""Keel optional notification port — Null / Webhook stub interface."""
from keel.notify.factory import build_notifier, cycle_notify_payload, describe_notifier
from keel.notify.null import NullNotifier
from keel.notify.protocol import NotifyEvent, NotifyResult, Notifier
from keel.notify.webhook import WebhookNotifier

__all__ = [
    "Notifier",
    "NotifyEvent",
    "NotifyResult",
    "NullNotifier",
    "WebhookNotifier",
    "build_notifier",
    "describe_notifier",
    "cycle_notify_payload",
]
