"""POST /api/memory/* — Alistair's long-term memory (SQLite event log).

The single source of truth for what Alistair remembers about {user}. `save` is the
ONLY write path; `get` returns the ranked, budget-trimmed block to load at the
start of a session; `list` is a raw debug/mirror view. Descriptions are written
in Alistair's voice so they carry through to the MCP tool layer unchanged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..config import get_settings
from ..security import require_api_key
from ..services import memory as memory_service
from ..skills import load_skill

router = APIRouter(
    prefix="/api/memory",
    tags=["memory"],
    dependencies=[Depends(require_api_key)],
)

_SAVE_DOC = (
    "Save one durable fact about {user} to long-term memory. Use this whenever you "
    "learn something worth remembering across sessions — a standing fact, a "
    "preference, an open commitment, or a short summary. "
    "type: 'fact' (identity, people, constraints, allergies), 'preference' (how "
    "{user} likes things done), 'action' (an open item/commitment), or 'summary' (a "
    "rolling recap). relevance 1-5: use 5 ONLY for permanent safety/identity facts "
    "that must never be forgotten (they are pinned and never evicted); 3 is the "
    "default. Writing is append-only and de-duplicated, so re-saving the same thing "
    "is safe. To forget something, send op='retract' with the same type+content."
)
_GET_DOC = (
    "Load what you remember about {user} as a compact, ready-to-read block. Call this "
    "at the start of every session (load_context does this for you). Returns the "
    "highest-value memories: permanent core facts are always included, the rest are "
    "ranked by recency x importance and trimmed to a token budget."
)
_LIST_DOC = (
    "Raw dump of every current memory with scores — for inspection/debugging or to "
    "mirror into Notion. Not for normal recall; use /get for that."
)
_SEARCH_DOC = (
    "Recall ANY stored memory by keyword across the FULL store — not just the "
    "always-loaded /get summary. The loaded block (/get) is capped to the highest-value "
    "memories; this searches everything, so use it when you need an older or more "
    "specific fact that may not be in the loaded block. Empty query returns the whole "
    "store ranked by recency x importance. Optional type filter: fact/preference/action/"
    "summary."
)


class SaveMemoryRequest(BaseModel):
    content: str
    type: str = "fact"
    relevance: int = 3
    tags: str | None = None
    source: str | None = "voice"
    op: str = "assert"  # 'assert' to remember, 'retract' to forget


class GetMemoryRequest(BaseModel):
    top_n: int | None = None
    max_tokens: int | None = None


class SearchMemoryRequest(BaseModel):
    query: str | None = None
    limit: int = 20
    type: str | None = None


@router.post("/save", summary="Remember a fact", description=_SAVE_DOC)
def save_memory(body: SaveMemoryRequest) -> dict:
    return memory_service.op_save_memory(
        get_settings(),
        content=body.content,
        type_=body.type,
        relevance=body.relevance,
        tags=body.tags,
        source=body.source,
        op=body.op,
    )


@router.post("/get", summary="Recall memory block", description=_GET_DOC)
def get_memory(body: GetMemoryRequest) -> dict:
    return memory_service.op_get_memory(
        get_settings(), top_n=body.top_n, max_tokens=body.max_tokens
    )


@router.post("/search", summary="Recall any memory by keyword", description=_SEARCH_DOC)
def search_memory(body: SearchMemoryRequest) -> dict:
    return memory_service.op_search_memory(
        get_settings(), query=body.query, limit=body.limit, type_=body.type
    )


@router.post("/list", summary="List raw memories", description=_LIST_DOC)
def list_memory() -> dict:
    return memory_service.op_list_memory(get_settings())


_MAINT_DOC = (
    "Everything needed to consolidate memory in one call: the full current store plus the "
    "step-by-step maintenance procedure. Call at the end of a conversation/session, during a "
    "brief, or on request to tidy memory, then act on the procedure with /save."
)


@router.post("/maintenance", summary="Memory consolidation kit", description=_MAINT_DOC)
def memory_maintenance() -> dict:
    proc = load_skill("memory-maintenance") or {}
    return {
        "procedure": proc.get("instructions", ""),
        "guardrails": "Never invent facts. Ask {user} before deleting personal data you're unsure "
                      "about. Re-assert identity/safety (relevance 5) facts before retracting any "
                      "near-duplicate. Report what you merged/retracted.",
        "store": memory_service.op_list_memory(get_settings()),
    }
