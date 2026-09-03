"""
Keel API - Primary read-only FastAPI control plane (Stage 3).

Documented entry: ``uvicorn keel.api.app:app``.
This is a thin wrapper that composes routers.
Prefer this over legacy ``r20_backend.app`` (which still mounts the dashboard UI).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI

from keel import __version__
from keel.api.routers import health, status, positions, decisions, factors


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler - NO scheduler start here."""
    yield


def create_app() -> FastAPI:
    """Create the Keel API application."""
    app = FastAPI(
        title="Keel Trader API",
        description="Primary read-only control plane for Keel Trader (prefer over r20_backend.app)",
        version=__version__,
        lifespan=lifespan,
    )

    app.include_router(health.router, tags=["health"])
    app.include_router(status.router, prefix="/api/v1", tags=["status"])
    app.include_router(positions.router, prefix="/api/v1", tags=["positions"])
    app.include_router(decisions.router, prefix="/api/v1", tags=["decisions"])
    app.include_router(factors.router, prefix="/api/v1", tags=["factors"])

    return app


app = create_app()
