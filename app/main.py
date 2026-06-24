"""Alistair Skills API — FastAPI app.

An HTTP workaround that mirrors Claude's Notion + Google Calendar connectors and
the Microsoft To Do in-tray + skills, so a voice-mode Claude (HTTP-only) can
reproduce the same behaviour it has with the real connectors and skills.

Three layers:
  * Function APIs   — connector tools that DO things (/api/notion/*, /api/calendar/*, /api/intray, /api/github/*)
  * Description APIs — skills that tell Claude WHAT to do (/api/skill/*)
  * Manifest API    — the catalogue of everything (/api/manifest, plus /docs, /openapi.json)
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import get_settings
from .routers import calendar, github, intray, memory, notion, skill
from .services import ServiceError
from .skills import skill_index

# Top intents mapped to call sequences, so a voice-mode Claude can self-bootstrap
# from /api/manifest with the smallest possible always-on prompt.
SHORTCUTS = [
    {"intent": "daily brief / morning brief / what's on today",
     "calls": ["POST /api/notion/query", "POST /api/calendar/today",
               "POST /api/intray {\"action\":\"list\"}"],
     "then": "deliver per GET /api/skill/daily-brief"},
    {"intent": "add something to my in-tray",
     "calls": ["POST /api/intray {\"action\":\"add\",\"title\":\"<text>\"}"]},
    {"intent": "what's in my in-tray",
     "calls": ["POST /api/intray {\"action\":\"list\"}"]},
    {"intent": "complete / remove an in-tray item",
     "calls": ["POST /api/intray {\"action\":\"done|delete\",\"task_id\":\"<id from list>\"}"]},
    {"intent": "read Notion (find/open a page, query a database)",
     "calls": ["POST /api/notion/search", "POST /api/notion/fetch",
               "POST /api/notion/query-database"]},
    {"intent": "write to Notion (incl. the references tray)",
     "calls": ["GET /api/skill/notion-master  (safe-write rules first)",
               "POST /api/notion/fetch", "POST /api/notion/update-page"]},
    {"intent": "calendar",
     "calls": ["POST /api/calendar/list-events", "POST /api/calendar/create-event",
               "POST /api/calendar/suggest-time"]},
    {"intent": "load what you remember about me (start of session)",
     "calls": ["POST /api/memory/get"]},
    {"intent": "remember / forget a fact about me",
     "calls": ["POST /api/memory/save {\"content\":\"<fact>\",\"type\":\"fact|preference|action|summary\",\"relevance\":1-5}",
               "POST /api/memory/save {\"op\":\"retract\",\"type\":\"...\",\"content\":\"<same text>\"}"]},
]

app = FastAPI(
    title="Alistair Skills API",
    version=__version__,
    description=(
        "HTTP mirror of Claude's Notion + Google Calendar connectors, the "
        "Microsoft To Do in-tray, and the PARA skills — for use in voice mode "
        "(HTTP-only). Function APIs do things; description APIs (skills) say what "
        "to do; /api/manifest lists everything."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connectors (function APIs)
app.include_router(notion.router)
app.include_router(calendar.router)
app.include_router(intray.router)
app.include_router(github.router)
app.include_router(memory.router)
# Skills (description APIs)
app.include_router(skill.router)


@app.exception_handler(ServiceError)
def handle_service_error(request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message, "detail": exc.detail},
    )


@app.get("/health", tags=["meta"], summary="Health check")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", tags=["meta"], summary="Service overview")
def root() -> dict:
    s = get_settings()
    return {
        "service": "Alistair Skills API",
        "version": __version__,
        "environment": s.railway_env,
        "layers": {
            "function_apis": "connector tools that do things (notion, calendar, intray, github)",
            "description_apis": "skills that say what to do (/api/skill/*)",
            "manifest": "/api/manifest, /docs, /openapi.json",
        },
        "configured": {
            "notion": bool(s.notion_token),
            "calendar": bool(s.google_calendar_token or s.google_refresh_token),
            "intray": bool(s.ms_client_id and s.ms_todo_list_id and s.github_token and s.gist_id),
            "github_push": bool(s.github_token),
            "memory_persistent": s.memory_is_persistent,
            "api_key_required": bool(s.service_api_key),
        },
        "docs": "/docs",
        "manifest": "/api/manifest",
        "how_to": (
            "GET /api/manifest for every tool + intent shortcuts; GET /api/skill "
            "for skill rules. Send X-API-Key on every /api/* call."
        ),
    }


@app.get("/api/manifest", tags=["meta"], summary="Catalogue of every tool/skill")
def manifest() -> dict:
    """Machine-readable list of every endpoint with its verbatim description.

    This is the "what they do" catalogue a voice-mode Claude can fetch in one
    call to discover all function APIs (connector tools) and description APIs
    (skills), each with the exact description copied from the real connector.

    Built from the OpenAPI schema so it stays correct regardless of how routers
    are stored internally.
    """
    spec = app.openapi()
    groups: dict[str, list[dict]] = {
        "notion": [], "calendar": [], "intray": [], "github": [], "memory": [], "skill": []
    }
    prefixes = {
        "/api/notion": "notion", "/api/calendar": "calendar", "/api/intray": "intray",
        "/api/github": "github", "/api/memory": "memory", "/api/skill": "skill",
    }
    for path, item in spec.get("paths", {}).items():
        key = next((g for pre, g in prefixes.items() if path.startswith(pre)), None)
        if key is None:
            continue
        for method, op in item.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            groups[key].append({
                "name": op.get("operationId"),
                "method": method.upper(),
                "path": path,
                "summary": op.get("summary", ""),
                "description": op.get("description", ""),
            })

    function_apis = {k: groups[k] for k in ("notion", "calendar", "intray", "github", "memory")}
    description_apis = {
        "list_endpoint": "GET /api/skill",
        "get_endpoint": "GET /api/skill/{slug}",
        "skills": skill_index(),
    }
    counts = {k: len(v) for k, v in function_apis.items()}
    counts["skills"] = len(description_apis["skills"])
    counts["total"] = sum(counts.values())
    return {
        "service": "Alistair Skills API",
        "version": __version__,
        "how_to_use": (
            "Function APIs do things; description APIs (skills) say what to do. "
            "Send X-API-Key on every /api/* call. For common requests, follow "
            "'shortcuts'; otherwise pick a tool by its description below and fetch "
            "the relevant skill for its rules before acting."
        ),
        "shortcuts": SHORTCUTS,
        "function_apis": function_apis,
        "description_apis": description_apis,
        "counts": counts,
    }
