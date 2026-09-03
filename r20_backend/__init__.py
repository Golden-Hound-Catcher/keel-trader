"""LEGACY R20 standalone backend package (Stage 7 quarantine).

Prefer ``uvicorn keel.api.app:app``. This package remains for admin routes and
the legacy read-only dashboard UI mount. Accidental imports warn unless
``KEEL_USE_LEGACY=1``. See LEGACY.md.
"""
from keel.legacy import warn_legacy

warn_legacy(
    "r20_backend",
    prefer="uvicorn keel.api.app:app  (deploy/keel-api.service)",
    stacklevel=2,
)

from .config import settings

__all__ = ["settings"]
