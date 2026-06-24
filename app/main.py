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

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import get_settings
from .mcp_server import mcp, mcp_asgi
from .routers import alistair, calendar, github, intray, memory, notion, skill
from .services import ServiceError
from .skills import skill_index

# Top intents mapped to call sequences, so a voice-mode Claude can self-bootstrap
# from /api/manifest with the smallest possible always-on prompt.
SHORTCUTS = [
    {"intent": "start of session / become Alistair (call first)",
     "calls": ["POST /api/alistair/load-context"]},
    {"intent": "daily brief / morning brief / what's on today",
     "calls": ["POST /api/alistair/daily-brief"],
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
    {"intent": "project status / what's happening with <project> / open PRs",
     "calls": ["POST /api/alistair/project-context {\"owner\":\"<owner>\",\"repo\":\"<repo>\"}"],
     "then": "summarise what moved / what's waiting in Alistair's voice"},
    {"intent": "merge a pull request (always preview first)",
     "calls": ["POST /api/github/merge-pr {\"owner\":\"..\",\"repo\":\"..\",\"number\":N}  (preview only)",
               "POST /api/github/merge-pr {\"owner\":\"..\",\"repo\":\"..\",\"number\":N,\"confirm\":true}  (after Owen confirms)"]},
    {"intent": "read code / a file / commits / issues on GitHub",
     "calls": ["POST /api/github/get-file", "POST /api/github/list-tree",
               "POST /api/github/recent-commits", "POST /api/github/list-prs"]},
    {"intent": "load what you remember about me (start of session)",
     "calls": ["POST /api/memory/get"]},
    {"intent": "remember / forget a fact about me",
     "calls": ["POST /api/memory/save {\"content\":\"<fact>\",\"type\":\"fact|preference|action|summary\",\"relevance\":1-5}",
               "POST /api/memory/save {\"op\":\"retract\",\"type\":\"...\",\"content\":\"<same text>\"}"]},
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    # The MCP Streamable-HTTP transport needs its session manager running for the
    # lifetime of the app. The mounted sub-app's lifespan is not auto-run, so we
    # run it here from the host app.
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Alistair Skills API",
    version=__version__,
    lifespan=lifespan,
    description=(
        "HTTP mirror of Claude's Notion + Google Calendar connectors, the "
        "Microsoft To Do in-tray, and the PARA skills — for use in voice mode "
        "(HTTP-only). Function APIs do things; description APIs (skills) say what "
        "to do; /api/manifest lists everything. The same tools are exposed as a "
        "remote MCP (Streamable-HTTP) at /mcp."
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
# Coarse persona layer (composes the connectors)
app.include_router(alistair.router)
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
            "github_read": bool(s.github_read_token),
            "memory_persistent": s.memory_is_persistent,
            "api_key_required": bool(s.service_api_key),
        },
        "mcp": {
            "endpoint": "/mcp",
            "transport": "streamable-http",
            "server_name": "alistair_assistant",
            "auth": "Bearer or X-API-Key = SERVICE_API_KEY (OAuth for claude.ai is the next step)",
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
        "notion": [], "calendar": [], "intray": [], "github": [], "memory": [],
        "alistair": [], "skill": []
    }
    prefixes = {
        "/api/notion": "notion", "/api/calendar": "calendar", "/api/intray": "intray",
        "/api/github": "github", "/api/memory": "memory", "/api/alistair": "alistair",
        "/api/skill": "skill",
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

    function_apis = {k: groups[k] for k in ("notion", "calendar", "intray", "github", "memory", "alistair")}
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


class _MCPDispatcher:
    """Serve the MCP at EXACTLY /mcp (and /mcp/...) with no trailing-slash redirect.

    A Starlette mount would 307 "/mcp" -> "/mcp/", and behind Railway's TLS proxy
    that Location is built as http:// (scheme downgrade) which breaks MCP clients.
    This top-level ASGI shim routes /mcp* straight to the MCP app (rewriting the
    path so its single "/" route matches) and forwards everything else — including
    the lifespan that runs the MCP session manager — to the FastAPI app.
    """

    def __init__(self, fastapi_app, mcp_app):
        self.fastapi_app = fastapi_app
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                inner = dict(scope)
                rest = path[len("/mcp"):] or "/"
                inner["path"] = rest
                inner["raw_path"] = rest.encode()
                inner["root_path"] = scope.get("root_path", "") + "/mcp"
                return await self.mcp_app(inner, receive, send)
        return await self.fastapi_app(scope, receive, send)


# The ASGI callable uvicorn serves (see railway.toml / Procfile: app.main:asgi).
asgi = _MCPDispatcher(app, mcp_asgi)
