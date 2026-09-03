"""LEGACY R20 standalone backend package (Stage 7 quarantine).

Prefer ``uvicorn keel.api.app:app``. This package remains for soft-blocked
``/api/v1/admin/*`` remnant APIs only — the Vue ``/admin`` UI and Jinja
dashboard are gone. Accidental imports warn unless ``KEEL_USE_LEGACY=1``.
See LEGACY.md.
"""
from keel.legacy import warn_legacy

warn_legacy(
    "r20_backend",
    prefer="uvicorn keel.api.app:app  (deploy/keel-api.service)",
    stacklevel=2,
)

from .config import settings

__all__ = ["settings"]
