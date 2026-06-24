"""POST /api/alistair/* — the coarse persona layer (load_context, daily_brief).

High-level Alistair tools that compose the connectors. `load_context` is the
session bootstrap (persona + routing + IDs + skills + memory); `daily_brief`
gathers the three read sources in one call. Descriptions are in Alistair's voice
so they carry through to the MCP tool layer unchanged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..config import get_settings
from ..security import require_api_key
from ..services import alistair as alistair_service
from ..services import notion as notion_service

router = APIRouter(
    prefix="/api/alistair",
    tags=["alistair"],
    dependencies=[Depends(require_api_key)],
)

_LOAD_CONTEXT_DOC = (
    "Load Alistair's operating context — call this FIRST at the start of every session. "
    "Returns who Alistair is (persona + voice), how to route what Owen says to the right "
    "tool, the stable Notion/PARA ID registry, the non-negotiable safety rules, the skill "
    "index (fetch a skill's full rules on demand with GET /api/skill/{slug}), and the live "
    "memory block of what you already know about Owen. Read-only."
)
_DAILY_BRIEF_DOC = (
    "Gather everything the daily brief needs in one call: Owen's Notion structure (active "
    "projects, Next actions, Someday items), today's Google Calendar, and the in-tray count. "
    "Any unconfigured or failing source is reported under 'unavailable' instead of failing the "
    "call. Read-only — it proposes, it never files. Then deliver per the daily-brief skill."
)


@router.post("/load-context", summary="Load Alistair's context (call first)", description=_LOAD_CONTEXT_DOC)
def load_context() -> dict:
    return alistair_service.load_context(get_settings())


@router.post("/daily-brief", summary="Compose the daily brief sources", description=_DAILY_BRIEF_DOC)
def daily_brief() -> dict:
    return alistair_service.daily_brief(get_settings())


_SAVE_REFERENCE_DOC = (
    "Append a reference to Owen's Notion References Tray (the 'Unorganised References' page) "
    "when he says 'add to tray', 'save this reference', etc. INSERT-ONLY and safe: it reads the "
    "tray first, finds the last entry above the END-OF-TRAY boundary, inserts one spacer + your "
    "entry there (NEVER overwrites the page), then re-fetches and verifies nothing else changed. "
    "It aborts rather than guess if the tray structure is missing. title is the H4 heading; body "
    "is a short casual description; link is optional ([text](url) or bare). Pass dry_run=true to "
    "preview the exact placement without writing. Never writes to the Library hub."
)
_ADD_ACTION_DOC = (
    "Create ONE Next action in Owen's Notion Actions database when he EXPLICITLY asks to add a "
    "task/action (not during the daily brief, which only proposes). Non-destructive create. "
    "name is the action title; status defaults to 'Next' (Next/Waiting/Someday/Done); due is an "
    "optional ISO date. Capture-only intent ('remind me to…') belongs in the in-tray instead."
)


class SaveReferenceRequest(BaseModel):
    title: str
    body: str | None = None
    link: str | None = None
    dry_run: bool = False


class AddActionRequest(BaseModel):
    name: str
    status: str = "Next"
    due: str | None = None


@router.post("/save-reference", summary="Append to the References Tray", description=_SAVE_REFERENCE_DOC)
def save_reference(body: SaveReferenceRequest) -> dict:
    return notion_service.op_save_reference(
        get_settings(), title=body.title, body=body.body, link=body.link, dry_run=body.dry_run
    )


@router.post("/add-action", summary="Add a Next action", description=_ADD_ACTION_DOC)
def add_action(body: AddActionRequest) -> dict:
    return notion_service.op_add_action(
        get_settings(), name=body.name, status=body.status, due=body.due
    )
