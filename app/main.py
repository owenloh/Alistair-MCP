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
from .routers import calendar, github, intray, notion, skill
from .services import ServiceError

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
            "api_key_required": bool(s.service_api_key),
        },
        "docs": "/docs",
        "manifest": "/api/manifest",
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
        "notion": [], "calendar": [], "intray": [], "github": [], "skill": []
    }
    prefixes = {
        "/api/notion": "notion", "/api/calendar": "calendar", "/api/intray": "intray",
        "/api/github": "github", "/api/skill": "skill",
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

    function_apis = {k: groups[k] for k in ("notion", "calendar", "intray", "github")}
    description_apis = {"skills": groups["skill"]}
    counts = {k: len(v) for k, v in {**function_apis, **description_apis}.items()}
    counts["total"] = sum(counts.values())
    return {
        "service": "Alistair Skills API",
        "version": __version__,
        "function_apis": function_apis,
        "description_apis": description_apis,
        "counts": counts,
    }
