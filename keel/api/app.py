"""
Keel API - Primary read-only FastAPI control plane (Stage 3).

Documented entry: ``uvicorn keel.api.app:app``.
This is a thin wrapper that composes routers.
Primary documented API entrypoint (``r20_*`` packages removed).

Phase U1: optionally serves ``frontend/dist`` as a static SPA when present.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from keel import __version__
from keel.api.auth import ApiTokenMiddleware
from keel.api.routers import health, status, positions, decisions, factors, pnl, stats

# repo_root/frontend/dist (keel/api/app.py -> parents[2] == repo root)
_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
_SPA_RESERVED = frozenset({"api", "docs", "openapi.json", "redoc", "health", "ready", "assets"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler - NO scheduler start here."""
    yield


def create_app() -> FastAPI:
    """Create the Keel API application."""
    app = FastAPI(
        title="Keel Trader API",
        description="Primary read-only control plane for Keel Trader",
        version=__version__,
        lifespan=lifespan,
    )

    # Optional KEEL_API_TOKEN: protect /api/v1/*; leave /health, /docs, /openapi.json open
    app.add_middleware(ApiTokenMiddleware)

    app.include_router(health.router, tags=["health"])
    app.include_router(status.router, prefix="/api/v1", tags=["status"])
    app.include_router(positions.router, prefix="/api/v1", tags=["positions"])
    app.include_router(decisions.router, prefix="/api/v1", tags=["decisions"])
    app.include_router(factors.router, prefix="/api/v1", tags=["factors"])
    app.include_router(pnl.router, prefix="/api/v1", tags=["pnl"])
    app.include_router(stats.router, prefix="/api/v1", tags=["stats"])

    # Optional U1 static monitor (built with: cd frontend && npm run build)
    if _FRONTEND_DIST.is_dir() and (_FRONTEND_DIST / "index.html").is_file():
        assets = _FRONTEND_DIST / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        index_html = _FRONTEND_DIST / "index.html"

        @app.get("/")
        def spa_index():
            return FileResponse(index_html)

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str):
            first = full_path.split("/", 1)[0]
            if first in _SPA_RESERVED or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not found")
            candidate = (_FRONTEND_DIST / full_path).resolve()
            try:
                candidate.relative_to(_FRONTEND_DIST.resolve())
            except ValueError:
                raise HTTPException(status_code=404, detail="Not found")
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_html)

    return app


app = create_app()
