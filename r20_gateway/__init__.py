"""LEGACY R20 Gateway runtime package (Stage 7 quarantine).

Prefer Keel for scheduling (``python -m keel.worker``). This package remains
for optional notification delivery only; job ticks stay off unless
``KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1``. Accidental imports warn unless
``KEEL_USE_LEGACY=1``. See LEGACY.md.
"""
from keel.legacy import warn_legacy

warn_legacy(
    "r20_gateway",
    prefer="python -m keel.worker  (notifications optional; no second scheduler)",
    stacklevel=2,
)

__version__ = "0.4.0"
