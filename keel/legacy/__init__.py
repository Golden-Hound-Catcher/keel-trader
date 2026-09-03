"""Legacy quarantine helpers (Stage 7).

``r20_*`` packages and historical scripts remain in-tree for rollback and the
legacy admin/dashboard mount, but accidental use should be loud unless the
operator explicitly opts in with ``KEEL_USE_LEGACY=1``.
"""
from __future__ import annotations

import os
import sys
import warnings
from typing import Final

_LEGACY_ENV: Final = "KEEL_USE_LEGACY"
_EMITTED: set[str] = set()


def legacy_opt_in() -> bool:
    """True when the operator explicitly acknowledges legacy paths."""
    return os.environ.get(_LEGACY_ENV, "").strip() == "1"


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


__all__ = ["legacy_opt_in", "warn_legacy"]
