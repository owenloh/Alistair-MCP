"""POST /api/alistair/* — the coarse persona layer (load_context, daily_brief).

High-level Alistair tools that compose the connectors. `load_context` is the
session bootstrap (persona + routing + IDs + skills + memory); `daily_brief`
gathers the three read sources in one call. Descriptions are in Alistair's voice
so they carry through to the MCP tool layer unchanged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..security import require_api_key
from ..services import alistair as alistair_service

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
