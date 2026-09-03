"""LEGACY R20 standalone backend package (quarantine).

Prefer ``uvicorn keel.api.app:app``. The Vue ``/admin`` UI, Jinja dashboard,
``admin_auth``, and ``/api/v1/admin/*`` HTTP routes are **gone**. This package
still holds helper modules used by optional gateway/scripts (notifications,
llm_manager, backup_store, OKX helpers, etc.). Accidental imports warn unless
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
