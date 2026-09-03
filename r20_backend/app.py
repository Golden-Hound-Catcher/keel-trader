"""LEGACY ASGI stub — admin HTTP API removed.

**Supported API:** ``uvicorn keel.api.app:app`` / ``deploy/keel-api.service``.

The Vue ``/admin/*`` UI, Jinja ``dashboard/``, and ``/api/v1/admin/*`` routes
are gone. This module remains only as a soft-blocked entrypoint so accidental
``uvicorn r20_backend.app:app`` still exits (or, with opt-in, serves a 410
stub) instead of silently failing.

Running without ``KEEL_ALLOW_LEGACY_BACKEND=1`` raises ``SystemExit(2)``.
Inventory: ``LEGACY.md``.
"""
from __future__ import annotations

from keel.legacy import require_legacy_backend, warn_legacy

require_legacy_backend(component="r20_backend.app")
warn_legacy(
    "r20_backend.app",
    prefer="uvicorn keel.api.app:app  (deploy/keel-api.service)",
    stacklevel=2,
)

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_RETIRED = {
    "ok": False,
    "status": "gone",
    "detail": (
        "r20_backend admin HTTP API (/api/v1/admin/*) has been removed. "
        "Use uvicorn keel.api.app:app (deploy/keel-api.service). "
        "See LEGACY.md / SPEC.md."
    ),
    "prefer": "uvicorn keel.api.app:app",
}

app = FastAPI(
    title="Keel Trader LEGACY stub (retired)",
    version="6.3.0",
    description="Soft-blocked stub; admin API removed. Prefer keel.api.app.",
)


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def retired(full_path: str, request: Request) -> JSONResponse:
    """Every path returns 410 Gone — no admin or legacy control-plane routes."""
    body = {**_RETIRED, "path": f"/{full_path}" if full_path else "/"}
    return JSONResponse(status_code=410, content=body)


__all__ = ["app"]
