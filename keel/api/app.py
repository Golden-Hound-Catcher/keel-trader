"""
Keel API - Read-only FastAPI control plane.

This is a thin wrapper that composes routers.
Replaces the 1900+ LOC god module r20_backend/app.py.
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
        description="Read-only control plane for Keel Trader",
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
