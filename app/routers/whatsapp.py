"""WhatsApp connector endpoints (read + draft only).

The read tools (chats/messages/search) proxy to a small agent on the owner's laptop
(Baileys, its own linked device) reachable over Tailscale — the MCP stays stateless
and stores nothing. The draft tool returns a wa.me deep link and NEVER sends. Mirrors
the Gmail router's read + draft, never-sends shape.
"""
from __future__ import annotations

from pydantic import BaseModel

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..security import require_api_key
from ..services import whatsapp as whatsapp_service
from . import _whatsapp_docs as docs

router = APIRouter(
    prefix="/api/whatsapp",
    tags=["whatsapp"],
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ChatsRequest(BaseModel):
    limit: int = 20


class ReadRequest(BaseModel):
    chat: str
    limit: int = 30


class SearchRequest(BaseModel):
    query: str
    limit: int = 20


class RecentRequest(BaseModel):
    limit: int = 15


class FindRequest(BaseModel):
    query: str
    limit: int = 20


class DraftRequest(BaseModel):
    to: str
    body: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/chats", summary="List recent WhatsApp chats", description=docs.CHATS)
def chats(body: ChatsRequest | None = None) -> dict:
    return whatsapp_service.list_chats(get_settings(), limit=body.limit if body else 20)


@router.post("/messages", summary="Read a WhatsApp chat", description=docs.READ)
def messages(body: ReadRequest) -> dict:
    return whatsapp_service.read_messages(get_settings(), chat=body.chat, limit=body.limit)


@router.post("/search", summary="Search WhatsApp messages", description=docs.SEARCH)
def search(body: SearchRequest) -> dict:
    return whatsapp_service.search(get_settings(), query=body.query, limit=body.limit)


@router.post("/recent", summary="WhatsApp inbox (recent chats + previews)", description=docs.RECENT)
def recent(body: RecentRequest | None = None) -> dict:
    return whatsapp_service.recent(get_settings(), limit=body.limit if body else 15)


@router.post("/find", summary="Find a WhatsApp chat by name/number and read it", description=docs.FIND)
def find(body: FindRequest) -> dict:
    return whatsapp_service.find(get_settings(), query=body.query, limit=body.limit)


@router.post("/draft", summary="Draft a WhatsApp message (wa.me link, never sends)",
             description=docs.DRAFT)
def draft(body: DraftRequest) -> dict:
    return whatsapp_service.draft(get_settings(), to=body.to, body=body.body)
