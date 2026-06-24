"""Shared Google OAuth helper — mint a fresh access token from the refresh-token trio.

Both the Calendar and Gmail services authenticate the same way: a long-lived
``GOOGLE_REFRESH_TOKEN`` (plus client id/secret) is exchanged for a short-lived
access token on each call, so nothing goes stale on a long-running Railway service.
Gmail rides the *same* refresh token as Calendar — it only needs the extra Gmail
scope granted at consent time (see scripts/get_google_token.py).

Kept tiny and dependency-light (sync httpx) to match the rest of the service layer.
"""
from __future__ import annotations

import httpx

from . import ServiceError
from ..config import Settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TIMEOUT = httpx.Timeout(30.0)


def safe_body(resp: httpx.Response) -> str:
    """Best-effort, length-capped snippet of an upstream error body (no secrets)."""
    try:
        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            if isinstance(err, dict):
                # Google often nests the human message under error.message.
                return str(err.get("message", err))[:300]
            return str(err)[:300]
        return str(data)[:300]
    except Exception:
        return resp.text[:300]


def mint_access_token(settings: Settings) -> str:
    """Exchange the refresh-token trio for a fresh Google access token.

    Raises ServiceError(503) if the trio isn't configured, or 502 on a network/OAuth
    failure (carrying a safe snippet of Google's response).
    """
    if not (
        settings.google_refresh_token
        and settings.google_client_id
        and settings.google_client_secret
    ):
        raise ServiceError(
            "Google is not configured: set GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET (mint the token with scripts/get_google_token.py).",
            status_code=503,
        )
    try:
        resp = httpx.post(
            _TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": settings.google_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise ServiceError(f"Network error reaching Google OAuth: {e}", status_code=502)
    if resp.status_code != 200:
        raise ServiceError(
            "Could not refresh Google access token.",
            status_code=502,
            detail=safe_body(resp),
        )
    token = resp.json().get("access_token")
    if not token:
        raise ServiceError("Google token endpoint returned no access_token.", status_code=502)
    return token
