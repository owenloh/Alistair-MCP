"""Gmail connector endpoints (read + draft only).

One POST endpoint per tool, backed by the Gmail REST API v1 via
``app.services.gmail``. Mirrors the calendar router's shape: inline request models,
verbatim descriptions in ``_gmail_docs``, thin forwarding to the service.

Scope of capability is deliberately read + draft: search/read mail and manage the
user's own DRAFTS. There is no send endpoint and nothing here mutates real mail.
"""
from __future__ import annotations

from pydantic import BaseModel

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..security import require_api_key
from ..services import gmail as gmail_service
from . import _gmail_docs as docs

router = APIRouter(
    prefix="/api/gmail",
    tags=["gmail"],
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    maxResults: int = 20
    labelIds: list[str] | None = None


class GetThreadRequest(BaseModel):
    threadId: str


class ListDraftsRequest(BaseModel):
    maxResults: int = 20


class CreateDraftRequest(BaseModel):
    to: list[str] | str
    subject: str = ""
    body: str = ""
    cc: list[str] | str | None = None
    bcc: list[str] | str | None = None
    threadId: str | None = None
    inReplyTo: str | None = None


class UpdateDraftRequest(BaseModel):
    draftId: str
    to: list[str] | str
    subject: str = ""
    body: str = ""
    cc: list[str] | str | None = None
    bcc: list[str] | str | None = None
    threadId: str | None = None
    inReplyTo: str | None = None


class DeleteDraftRequest(BaseModel):
    draftId: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/search", summary="Search Gmail", description=docs.SEARCH)
def search(body: SearchRequest) -> dict:
    return gmail_service.search(
        get_settings(), query=body.query, max_results=body.maxResults, label_ids=body.labelIds,
    )


@router.post("/get-thread", summary="Read a Gmail thread", description=docs.GET_THREAD)
def get_thread(body: GetThreadRequest) -> dict:
    return gmail_service.get_thread(get_settings(), thread_id=body.threadId)


@router.post("/list-drafts", summary="List Gmail drafts", description=docs.LIST_DRAFTS)
def list_drafts(body: ListDraftsRequest | None = None) -> dict:
    return gmail_service.list_drafts(
        get_settings(), max_results=body.maxResults if body else 20,
    )


@router.post("/create-draft", summary="Create a Gmail draft", description=docs.CREATE_DRAFT)
def create_draft(body: CreateDraftRequest) -> dict:
    return gmail_service.create_draft(
        get_settings(), to=body.to, subject=body.subject, body=body.body,
        cc=body.cc, bcc=body.bcc, thread_id=body.threadId, in_reply_to=body.inReplyTo,
    )


@router.post("/update-draft", summary="Update a Gmail draft", description=docs.UPDATE_DRAFT)
def update_draft(body: UpdateDraftRequest) -> dict:
    return gmail_service.update_draft(
        get_settings(), draft_id=body.draftId, to=body.to, subject=body.subject,
        body=body.body, cc=body.cc, bcc=body.bcc, thread_id=body.threadId,
        in_reply_to=body.inReplyTo,
    )


@router.post("/delete-draft", summary="Delete a Gmail draft", description=docs.DELETE_DRAFT)
def delete_draft(body: DeleteDraftRequest) -> dict:
    return gmail_service.delete_draft(get_settings(), draft_id=body.draftId)
