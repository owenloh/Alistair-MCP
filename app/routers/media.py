"""POST /api/media/* — open a web link, transcribe a YouTube/Instagram video.

Thin router over app/services/media.py. Read-only: it fetches public web content
and never posts, comments, or stores anything. Descriptions live in _media_docs.py
so both the HTTP layer and the MCP tools share one contract.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..models import OpenLinkRequest, TranscribeRequest
from ..security import require_api_key
from ..services import media as media_service
from . import _media_docs as _docs

router = APIRouter(
    prefix="/api/media",
    tags=["media"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "/open-link",
    summary="Open a web link and return its readable content",
    description=_docs.OPEN_LINK,
)
def open_link(body: OpenLinkRequest) -> dict:
    return media_service.open_link(get_settings(), url=body.url, max_chars=body.max_chars)


@router.post(
    "/transcribe",
    summary="Transcribe a YouTube/Instagram video link",
    description=_docs.TRANSCRIBE,
)
def transcribe(body: TranscribeRequest) -> dict:
    return media_service.transcribe_video(get_settings(), url=body.url, lang=body.lang)
