"""Skill description APIs.

GET /api/skill            -> list available skills
GET /api/skill/{slug}     -> that skill's rules as JSON

The four skills (notion-master, daily-brief, notion-references-tray,
microsoft-todo-intray) are served by the {slug} route from JSON data files.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..security import require_api_key
from ..skills import list_slugs, load_skill, skill_index

router = APIRouter(
    prefix="/api/skill",
    tags=["skill"],
    dependencies=[Depends(require_api_key)],
)


@router.get("", summary="List available skills")
def index() -> dict:
    return {
        "skills": skill_index(),
        "usage": (
            "GET /api/skill/{slug} for a skill's full rules. Fetch the relevant "
            "skill before performing it — e.g. notion-master for the Notion "
            "safe-write rules before any /api/notion write."
        ),
    }


@router.get(
    "/{slug}",
    summary="Get a skill's rules",
    description="Returns the named skill's rules (verbatim instructions + a "
    "structured section breakdown). Slugs: notion-master, daily-brief, "
    "notion-references-tray, microsoft-todo-intray.",
)
def get_skill(slug: str) -> dict:
    data = load_skill(slug)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown skill '{slug}'. Available: {list_slugs()}",
        )
    return data
