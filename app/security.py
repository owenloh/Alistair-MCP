"""Optional shared-secret guard for the /api/* routes.

If SERVICE_API_KEY is set, every protected request must send a matching
`X-API-Key` header. If it is unset, the guard is a no-op (open API) so first
smoke tests are frictionless. Set it before exposing the public URL — these
endpoints read private Notion / calendar / in-tray data.
"""
from __future__ import annotations

from fastapi import Header, HTTPException, status

from .config import get_settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    expected = settings.service_api_key
    if not expected:
        return  # open mode
    if x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key header.",
        )
