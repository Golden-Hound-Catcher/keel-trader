"""Legacy quarantine helpers (Stage 7+).

``r20_*`` packages and historical scripts remain in-tree for rollback and
helper modules (notifications, llm_manager, backups), but accidental use
should be loud unless the operator explicitly opts in. Jinja ``dashboard/``,
Vue ``/admin``, and ``/api/v1/admin/*`` were removed.

- ``KEEL_USE_LEGACY=1`` — acknowledge legacy scripts / silence import warnings
- ``KEEL_ALLOW_LEGACY_BACKEND=1`` — permit importing the retired
  ``r20_backend.app`` stub (410 only). Prefer ``uvicorn keel.api.app:app``.
"""
from __future__ import annotations

import os
import sys
import warnings
from typing import Final

_LEGACY_ENV: Final = "KEEL_USE_LEGACY"
_ALLOW_BACKEND_ENV: Final = "KEEL_ALLOW_LEGACY_BACKEND"
_EMITTED: set[str] = set()


def legacy_opt_in() -> bool:
    """True when the operator explicitly acknowledges legacy paths."""
    return os.environ.get(_LEGACY_ENV, "").strip() == "1"


def legacy_backend_allowed() -> bool:
    """True when running the legacy ASGI app is explicitly allowed.

    Also true under pytest so quarantine tests can import the retired
    ``r20_backend.app`` stub without setting the operator flag.
    """
    if os.environ.get(_ALLOW_BACKEND_ENV, "").strip() == "1":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if "pytest" in sys.modules:
        return True
    return False


def require_legacy_backend(*, component: str = "r20_backend.app") -> None:
    """Soft-block accidental ``uvicorn r20_backend.app:app`` without opt-in.

    Raises ``SystemExit(2)`` with a pointer to ``keel.api`` unless
    ``KEEL_ALLOW_LEGACY_BACKEND=1`` (or pytest import path).
    """
    if legacy_backend_allowed():
        return
    print(
        f"DISABLED: {component} is not a supported deployment entrypoint.\n"
        "Use the Keel primary API instead:\n"
        "  python -m uvicorn keel.api.app:app --host 0.0.0.0 --port 8080\n"
        "  # or: systemctl enable --now keel-api\n"
        "\n"
        "The admin HTTP API is removed; this entrypoint is a 410 stub only.\n"
        "To import the stub intentionally, set:\n"
        f"  {_ALLOW_BACKEND_ENV}=1\n"
        "See LEGACY.md and SPEC.md §11.\n",
        file=sys.stderr,
    )
    raise SystemExit(2)


def warn_legacy(
    component: str,
    *,
    prefer: str,
    stacklevel: int = 2,
    loud: bool = False,
) -> None:
    """Emit a one-shot DeprecationWarning unless KEEL_USE_LEGACY=1.

    When ``loud=True`` (CLI / ``__main__`` entrypoints), also print to stderr
    so accidental runs are obvious even if warnings are filtered.
    """
    if legacy_opt_in():
        return
    key = component.strip() or "legacy"
    if key in _EMITTED:
        return
    _EMITTED.add(key)
    message = (
        f"LEGACY: {key} is deprecated. Prefer: {prefer}. "
        f"Set {_LEGACY_ENV}=1 only for intentional rollback. See LEGACY.md."
    )
    warnings.warn(message, DeprecationWarning, stacklevel=max(2, stacklevel))
    if loud:
        print(message, file=sys.stderr)


__all__ = [
    "legacy_opt_in",
    "legacy_backend_allowed",
    "require_legacy_backend",
    "warn_legacy",
]
