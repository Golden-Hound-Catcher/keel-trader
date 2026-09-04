"""Optional API token auth helpers."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from keel.config import get_settings


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    api_key = request.headers.get("X-API-Key")
    if api_key is not None and str(api_key).strip():
        return str(api_key).strip()
    return None


class ApiTokenMiddleware(BaseHTTPMiddleware):
    """When KEEL_API_TOKEN is set, require Bearer or X-API-Key on /api/v1/*."""

    async def dispatch(self, request: Request, call_next) -> Response:
        settings = get_settings()
        expected = (settings.api_token or "").strip()
        if not expected:
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api/v1"):
            return await call_next(request)

        provided = _extract_token(request)
        if provided is None or provided != expected:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)
