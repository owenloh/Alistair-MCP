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
from .mcp_server import OAUTH_ENABLED, OAUTH_PATHS, mcp, mcp_asgi, oauth_provider
from .routers import alistair, calendar, github, gmail, intray, memory, notion, skill, spotify
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
    {"intent": "read / search email (what did <x> email, find that thread)",
     "calls": ["POST /api/gmail/search {\"query\":\"from:x newer_than:7d\"}",
               "POST /api/gmail/get-thread {\"threadId\":\"<id from search>\"}"],
     "then": "summarise in Alistair's voice; don't dump raw email"},
    {"intent": "draft an email / reply (DRAFT only — never sends)",
     "calls": ["GET /api/skill/gmail  (drafting etiquette first)",
               "POST /api/gmail/create-draft {\"to\":\"..\",\"subject\":\"..\",\"body\":\"..\",\"threadId\":\"<for a reply>\"}"],
     "then": "show Owen the draft; sending stays his action in Gmail"},
    {"intent": "project status / what's happening with <project> / open PRs",
     "calls": ["POST /api/alistair/project-context {\"owner\":\"<owner>\",\"repo\":\"<repo>\"}"],
     "then": "summarise what moved / what's waiting in Alistair's voice"},
    {"intent": "merge a pull request (always preview first)",
     "calls": ["POST /api/github/merge-pr {\"owner\":\"..\",\"repo\":\"..\",\"number\":N}  (preview only)",
               "POST /api/github/merge-pr {\"owner\":\"..\",\"repo\":\"..\",\"number\":N,\"confirm\":true}  (after Owen confirms)"]},
    {"intent": "what's my GitHub account / which repos do I have / find a repo",
     "calls": ["POST /api/github/whoami", "POST /api/github/list-my-repos"],
     "then": "use the returned full_name as owner/repo for the per-repo tools"},
    {"intent": "read code / a file / commits / issues on GitHub",
     "calls": ["POST /api/github/get-file", "POST /api/github/list-tree",
               "POST /api/github/recent-commits", "POST /api/github/list-prs"]},
    {"intent": "play music / control Spotify / what's playing",
     "calls": ["POST /api/spotify/search {\"query\":\"<song>\"}",
               "POST /api/spotify/play {\"track\":\"<uri>\",\"playlist\":\"<uri optional>\"}",
               "POST /api/spotify/control {\"action\":\"pause|resume|next|previous\"}"],
     "then": "for devices: /api/spotify/devices then /api/spotify/transfer; needs Spotify open on a device"},
    {"intent": "my Spotify playlists / play from a playlist",
     "calls": ["POST /api/spotify/playlists",
               "POST /api/spotify/playlist-tracks {\"playlist\":\"<uri>\"}"]},
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
app.include_router(gmail.router)
app.include_router(intray.router)
app.include_router(github.router)
app.include_router(spotify.router)
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
            "gmail": bool(s.google_refresh_token and s.google_client_id and s.google_client_secret),
            "intray": bool(s.ms_client_id and s.ms_todo_list_id and s.github_gist_token and s.gist_id),
            "github_push": bool(s.github_gist_token),
            "github_read": bool(s.github_read_token),
            "spotify": s.spotify_configured,
            "memory_persistent": s.memory_is_persistent,
            "api_key_required": bool(s.service_api_key),
        },
        "mcp": {
            "endpoint": "/mcp",
            "transport": "streamable-http",
            "server_name": "alistair_assistant",
            "oauth_enabled": OAUTH_ENABLED,
            "auth": (
                "OAuth 2.1 (dynamic client registration, operator-approved via password) + Bearer/X-API-Key = SERVICE_API_KEY"
                if OAUTH_ENABLED
                else "Bearer or X-API-Key = SERVICE_API_KEY (OAuth off — no public base URL set)"
            ),
            "oauth_metadata": "/.well-known/oauth-authorization-server" if OAUTH_ENABLED else None,
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
        "notion": [], "calendar": [], "gmail": [], "intray": [], "github": [],
        "spotify": [], "memory": [], "alistair": [], "skill": []
    }
    prefixes = {
        "/api/notion": "notion", "/api/calendar": "calendar", "/api/gmail": "gmail",
        "/api/intray": "intray", "/api/github": "github", "/api/spotify": "spotify",
        "/api/memory": "memory", "/api/alistair": "alistair", "/api/skill": "skill",
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

    function_apis = {k: groups[k] for k in ("notion", "calendar", "gmail", "intray", "github", "spotify", "memory", "alistair")}
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


# ---- OAuth approval gate (operator login shown on /authorize) ----
# Registered only when OAuth is on. /oauth/consent falls through the dispatcher to
# FastAPI (it is neither /mcp* nor an SDK OAuth route), so these handlers serve it.
# provider.authorize() redirects the browser here instead of auto-approving; the
# code is minted only after the operator enters the approval password.
if OAUTH_ENABLED:
    import html as _html

    from fastapi import Form
    from starlette.responses import HTMLResponse, RedirectResponse

    _CONSENT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Approve Alistair connection</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e7e9ee;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;padding:20px}
.card{background:#161922;border:1px solid #272d3a;border-radius:16px;padding:30px;max-width:390px;width:100%;box-shadow:0 12px 50px rgba(0,0,0,.45)}
h1{font-size:19px;margin:0 0 6px}p{color:#9aa4b4;font-size:13.5px;line-height:1.55;margin:0 0 22px}
.who{color:#7aa2ff;font-weight:600}
input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid #2d3340;background:#0f1115;color:#e7e9ee;font-size:14px;outline:none}
input:focus{border-color:#5b8cff}
button{width:100%;margin-top:14px;padding:12px;border:0;border-radius:10px;background:#5b8cff;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#4a7df0}.err{color:#ff7a7a;font-size:13px;margin:12px 0 0}
.foot{color:#5b6472;font-size:11.5px;margin:18px 0 0;text-align:center}
</style></head><body><div class="card">
<h1>Approve Alistair connection</h1>
<p><span class="who">%%CLIENT%%</span> wants to connect to your Alistair assistant &mdash; this grants access to your Notion, calendar, tasks and memory. Enter your approval password to allow it.</p>
<form method="post" action="/oauth/consent">
<input type="hidden" name="txn" value="%%TXN%%">
<input type="password" name="password" placeholder="Approval password" autofocus autocomplete="current-password">
%%ERROR%%
<button type="submit">Approve</button></form>
<p class="foot">If you didn&rsquo;t start this from claude.ai, close this page.</p>
</div></body></html>"""

    _EXPIRED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Link expired</title>
<style>body{font-family:system-ui,sans-serif;background:#0f1115;color:#e7e9ee;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;padding:20px;text-align:center}
.card{max-width:380px}h1{font-size:18px}p{color:#9aa4b4;font-size:14px;line-height:1.5}</style></head>
<body><div class="card"><h1>This approval link has expired</h1>
<p>Go back to claude.ai and try connecting the Alistair connector again.</p></div></body></html>"""

    def _render_consent(txn: str, client_id: str, error: str = "") -> HTMLResponse:
        err = f'<p class="err">{_html.escape(error)}</p>' if error else ""
        page = (
            _CONSENT_PAGE.replace("%%TXN%%", _html.escape(txn))
            .replace("%%CLIENT%%", _html.escape(client_id or "A client"))
            .replace("%%ERROR%%", err)
        )
        return HTMLResponse(page)

    @app.get("/oauth/consent", include_in_schema=False)
    async def oauth_consent_form(txn: str = ""):
        client_id = oauth_provider.pending_client(txn)
        if not client_id:
            return HTMLResponse(_EXPIRED_PAGE, status_code=400)
        return _render_consent(txn, client_id)

    @app.post("/oauth/consent", include_in_schema=False)
    async def oauth_consent_submit(txn: str = Form(""), password: str = Form("")):
        client_id = oauth_provider.pending_client(txn)
        if not client_id:
            return HTMLResponse(_EXPIRED_PAGE, status_code=400)
        if not oauth_provider.verify_approval(password):
            return _render_consent(txn, client_id, error="Incorrect password — try again.")
        target = oauth_provider.complete_authorization(txn)
        if not target:
            return HTMLResponse(_EXPIRED_PAGE, status_code=400)
        return RedirectResponse(target, status_code=302)


class _MCPDispatcher:
    """Serve the MCP at EXACTLY /mcp (and /mcp/...) with no trailing-slash redirect.

    A Starlette mount would 307 "/mcp" -> "/mcp/", and behind Railway's TLS proxy
    that Location is built as http:// (scheme downgrade) which breaks MCP clients.
    This top-level ASGI shim routes /mcp* straight to the MCP app (rewriting the
    path so its single "/" route matches) and forwards everything else — including
    the lifespan that runs the MCP session manager — to the FastAPI app.
    """

    def __init__(self, fastapi_app, mcp_app, oauth_paths):
        self.fastapi_app = fastapi_app
        self.mcp_app = mcp_app
        self.oauth_paths = oauth_paths

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
            # OAuth discovery/token/register/revoke live at the app root when OAuth
            # is on; route those exact paths to the MCP app (they are public).
            if self.oauth_paths and (path in self.oauth_paths or path.startswith("/.well-known/oauth")):
                return await self.mcp_app(scope, receive, send)
        return await self.fastapi_app(scope, receive, send)


# The ASGI callable uvicorn serves (see railway.toml / Procfile: app.main:asgi).
asgi = _MCPDispatcher(app, mcp_asgi, OAUTH_PATHS)
