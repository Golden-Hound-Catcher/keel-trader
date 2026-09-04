"""LEGACY R20 standalone backend package (quarantine).

Prefer ``uvicorn keel.api.app:app``. The Vue ``/admin`` UI, Jinja dashboard,
``admin_auth``, and ``/api/v1/admin/*`` HTTP routes are **gone**. Helper modules
that only served gateway/scripts are **deleted**. This package retains soft-blocked
stubs (``app``, ``scheduler``) so accidental imports still exit or return 410.
Accidental package imports warn unless ``KEEL_USE_LEGACY=1``. See LEGACY.md.
"""
from keel.legacy import warn_legacy

warn_legacy(
    "r20_backend",
    prefer="uvicorn keel.api.app:app  (deploy/keel-api.service)",
    stacklevel=2,
)

__all__: list[str] = []
