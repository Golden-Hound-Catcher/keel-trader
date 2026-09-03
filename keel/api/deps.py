"""Shared FastAPI dependencies for keel.api."""
from __future__ import annotations

from pathlib import Path

from keel.config import get_settings
from keel.ledger import KeelLedger

# Test / process override for the SQLite path (also settable via KEEL_LEDGER_DB).
_LEDGER_PATH_OVERRIDE: Path | None = None


def set_ledger_path_override(path: Path | str | None) -> None:
    """Override ledger DB path (used by integration tests). Pass None to clear."""
    global _LEDGER_PATH_OVERRIDE
    _LEDGER_PATH_OVERRIDE = Path(path) if path is not None else None


def get_ledger() -> KeelLedger:
    """Return a ledger bound to settings / override path."""
    if _LEDGER_PATH_OVERRIDE is not None:
        return KeelLedger(_LEDGER_PATH_OVERRIDE)
    settings = get_settings()
    return KeelLedger(settings.ledger_path)
