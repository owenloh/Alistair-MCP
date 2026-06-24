"""Alistair MCP server — the single remote tool surface (build spec phase #1).

One **remote Streamable-HTTP** MCP named `alistair_assistant` (snake_case so
Gemini accepts it). EVERYTHING is a TOOL — no client reliably auto-loads MCP
resources/prompts, so persona, memory, skills and the domain connectors all ship
as tools, each with an Alistair-voiced `description` (the one thing every
frontend sends to the model). `load_context` is called FIRST every session.

The tools are thin in-process adapters over the existing service/router layer:
no self-HTTP, no second copy of the logic, the exact same code the REST API runs.
Auth here is a bearer/X-API-Key guard on the mounted ASGI app (works today for
Claude Desktop/Code, Cursor, the Pipecat voice shell and Gemini CLI). claude.ai's
OAuth is the documented next step; see docs/ROADMAP.md.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import get_settings
from .services import ServiceError
from .services import alistair as alistair_service
from .services import calendar as calendar_service
from .services import memory as memory_service
from .services import notion as notion_service
from .skills import list_slugs, load_skill

SERVER_NAME = "alistair_assistant"  # snake_case: Gemini rejects '-' in server names

INSTRUCTIONS = (
    "You are Alistair, Owen's operations assistant. Call load_context FIRST every "
    "session to load persona, routing, the PARA ID registry, safety rules, the skill "
    "index and live memory. Read get_memory at the start and save_memory whenever you "
    "learn something durable about Owen. Notion is sacred: load the notion-master skill "
    "before any Notion write, and never overwrite a whole page. Be direct and concise."
)

mcp = FastMCP(
    name=SERVER_NAME,
    instructions=INSTRUCTIONS,
    stateless_http=True,      # each call is independent — simple to mount + scale
    json_response=True,       # plain JSON responses (no SSE framing) for broad client compat
    streamable_http_path="/",  # mounted at /mcp by main.py, so the endpoint is exactly /mcp
    # DNS-rebinding protection guards localhost servers from malicious web pages; this
    # is a public server behind Railway's TLS proxy with its own bearer/OAuth auth, so
    # the default host allowlist would (wrongly) 421 the Railway domain and claude.ai.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _run(fn):
    """Execute a service call, turning a ServiceError into a clean tool result
    (same {error, detail, status} shape the REST layer returns) instead of a raw
    exception, so the model gets a readable message it can act on."""
    try:
        return fn()
    except ServiceError as e:
        return {"error": e.message, "detail": e.detail, "status": e.status_code}


# ===================== persona / memory / skills (the core) =====================
@mcp.tool(
    name="load_context",
    description=(
        "Become Alistair — CALL THIS FIRST at the start of every session. Returns who "
        "Alistair is (persona + brutally-honest voice), how to route what Owen says to the "
        "right tool, the stable Notion/PARA ID registry, the non-negotiable safety rules, "
        "the skill index (fetch full rules with get_skill), and the live memory block of "
        "what you already know about Owen. Read-only."
    ),
)
def load_context() -> dict:
    return _run(lambda: alistair_service.load_context(get_settings()))


@mcp.tool(
    name="get_memory",
    description=(
        "Load what Alistair remembers about Owen: a ranked, decayed, token-budgeted memory "
        "block (durable facts, preferences, context). Call at session start alongside "
        "load_context. Read-only."
    ),
)
def get_memory(top_n: int | None = None, max_tokens: int | None = None) -> dict:
    return _run(lambda: memory_service.op_get_memory(get_settings(), top_n=top_n, max_tokens=max_tokens))


@mcp.tool(
    name="save_memory",
    description=(
        "The ONLY way to write Alistair's memory. Append a durable fact about Owen when you "
        "learn something lasting ('from now on…', a preference, background). type is "
        "fact|preference|action|summary; relevance 1-5 (5 = core, never forgotten). To forget, "
        "pass op='retract' with the same type+content. Capture-only 'remind me' tasks go to the "
        "in-tray, not here."
    ),
)
def save_memory(content: str, type: str = "fact", relevance: int = 3,
                tags: str | None = None, op: str = "assert") -> dict:
    return _run(lambda: memory_service.op_save_memory(
        get_settings(), content=content, type_=type, relevance=relevance,
        tags=tags, op=op, source="mcp",
    ))


@mcp.tool(
    name="get_skill",
    description=(
        "Fetch one Alistair skill's full procedure on demand (the index is in load_context). "
        "Slugs: notion-master (safe-write protocol — load before ANY Notion write), daily-brief, "
        "notion-references-tray, microsoft-todo-intray."
    ),
)
def get_skill(slug: str) -> dict:
    data = load_skill(slug)
    if data is None:
        return {"error": f"Unknown skill '{slug}'.", "available": list_slugs(), "status": 404}
    return data


@mcp.tool(
    name="daily_brief",
    description=(
        "Compose Owen's daily brief in one call: Notion structure (active projects, Next/Someday "
        "actions), today's calendar, and the in-tray. Graceful — any failing source is reported "
        "under 'unavailable'. Read-only; it PROPOSES, it never files. Then deliver per the "
        "daily-brief skill."
    ),
)
def daily_brief() -> dict:
    return _run(lambda: alistair_service.daily_brief(get_settings()))


@mcp.tool(
    name="project_context",
    description=(
        "Pull a project's live GitHub state in one call when Owen asks 'what's happening with "
        "<project>' or 'any open PRs': repo metadata + recent commits + open PRs + open issues + "
        "a README excerpt for owner/repo. The GitHub side of the daily brief. Read-only; summarise "
        "what moved / what's waiting in Alistair's voice."
    ),
)
def project_context(owner: str, repo: str, commits: int = 5) -> dict:
    return _run(lambda: alistair_service.project_context(get_settings(), owner=owner, repo=repo, commits=commits))


@mcp.tool(
    name="save_reference",
    description=(
        "Append a reference to Owen's Notion References Tray ('add to tray', 'save this reference'). "
        "INSERT-ONLY and safe: reads the tray, finds the last entry above the END-OF-TRAY boundary, "
        "inserts one spacer + your entry there (NEVER overwrites the page), re-fetches and verifies. "
        "Aborts rather than guess if the structure is missing. dry_run=true previews placement "
        "without writing. Never writes to the Library hub."
    ),
)
def save_reference(title: str, body: str | None = None, link: str | None = None, dry_run: bool = False) -> dict:
    return _run(lambda: notion_service.op_save_reference(
        get_settings(), title=title, body=body, link=link, dry_run=dry_run,
    ))


@mcp.tool(
    name="add_action",
    description=(
        "Create ONE Next action in Owen's Notion Actions database when he EXPLICITLY asks to add a "
        "task/action (not during the daily brief, which only proposes). status defaults to 'Next' "
        "(Next/Waiting/Someday/Done); due is an optional ISO date. Capture-only 'remind me to…' "
        "belongs in the in-tray instead."
    ),
)
def add_action(name: str, status: str = "Next", due: str | None = None) -> dict:
    return _run(lambda: notion_service.op_add_action(get_settings(), name=name, status=status, due=due))


# ===================== Notion (read + safe write) =====================
@mcp.tool(
    name="notion_search",
    description="Search Owen's Notion workspace for pages/databases by text. Read-only. Returns matches "
    "with ids + titles; follow up with notion_fetch to read one.",
)
def notion_search(query: str, page_size: int = 10) -> dict:
    from .routers.notion import SearchRequest, search
    return _run(lambda: search(SearchRequest(query=query, page_size=page_size)))


@mcp.tool(
    name="notion_fetch",
    description="Fetch one Notion page/database by id or URL, rendered to markdown. Read-only. Long pages "
    "paginate: pass the returned next_cursor back as start_cursor to read on (past 300 blocks).",
)
def notion_fetch(id: str, start_cursor: str | None = None, page_size: int = 100) -> dict:
    from .routers.notion import FetchRequest, fetch
    return _run(lambda: fetch(FetchRequest(id=id, start_cursor=start_cursor, page_size=page_size)))


@mcp.tool(
    name="notion_query_database",
    description="Query a Notion database with an explicit filter/sorts object (use this instead of saved "
    "views, which the public API can't read). Read-only.",
)
def notion_query_database(database_id: str, filter: dict | None = None,
                          sorts: list | None = None, page_size: int = 100,
                          start_cursor: str | None = None) -> dict:
    from .routers.notion import QueryDatabaseRequest, query_database
    return _run(lambda: query_database(QueryDatabaseRequest(
        database_id=database_id, filter=filter, sorts=sorts,
        page_size=page_size, start_cursor=start_cursor,
    )))


@mcp.tool(
    name="notion_update_page",
    description=(
        "Edit a Notion page. DANGEROUS — Notion is sacred. Load the notion-master skill FIRST for the "
        "safe-write protocol. INSERT/UPDATE/targeted-edit ONLY; NEVER overwrite a whole page. Use "
        "after_block_id to insert at a precise point. allow_deleting_content must be explicitly true to "
        "remove anything. Always read the page first and re-verify after."
    ),
)
def notion_update_page(page_id: str, command: str, content: str | None = None,
                       new_str: str | None = None, after_block_id: str | None = None,
                       properties: dict | None = None, allow_deleting_content: bool = False) -> dict:
    from .routers.notion import UpdatePageRequest, update_page
    return _run(lambda: update_page(UpdatePageRequest(
        page_id=page_id, command=command, content=content, new_str=new_str,
        after_block_id=after_block_id, properties=properties,
        allow_deleting_content=allow_deleting_content,
    )))


@mcp.tool(
    name="notion_create_pages",
    description="Create one or more new Notion pages under a parent. Each page is {content?, properties?, "
    "icon?, cover?}. Non-destructive (creates new pages). Load notion-master if unsure of structure.",
)
def notion_create_pages(pages: list[dict], parent: dict | None = None) -> dict:
    from .routers.notion import CreatePagesRequest, PageSpec, create_pages
    return _run(lambda: create_pages(CreatePagesRequest(
        pages=[PageSpec(**p) for p in pages], parent=parent,
    )))


# ===================== Calendar =====================
@mcp.tool(
    name="calendar_today",
    description="Owen's events for today, in his current timezone. Read-only. The fast path for "
    "'what's on today' (also part of daily_brief).",
)
def calendar_today(time_zone: str | None = None) -> dict:
    return _run(lambda: calendar_service.today_events(get_settings(), time_zone=time_zone))


@mcp.tool(
    name="calendar_list_events",
    description="List Owen's calendar events in a time window (ISO startTime/endTime), optional text filter. "
    "Read-only.",
)
def calendar_list_events(startTime: str | None = None, endTime: str | None = None,
                         fullText: str | None = None, timeZone: str | None = None,
                         pageSize: int = 100) -> dict:
    from .routers.calendar import ListEventsRequest, list_events
    return _run(lambda: list_events(ListEventsRequest(
        startTime=startTime, endTime=endTime, fullText=fullText, timeZone=timeZone, pageSize=pageSize,
    )))


@mcp.tool(
    name="calendar_create_event",
    description="Create a calendar event for Owen (ISO startTime/endTime, or allDay=true). Optional "
    "location, description, attendees, addGoogleMeetUrl. Confirm details with Owen before creating.",
)
def calendar_create_event(summary: str, startTime: str, endTime: str, allDay: bool = False,
                          timeZone: str | None = None, location: str | None = None,
                          description: str | None = None, attendees: list[str] | None = None,
                          addGoogleMeetUrl: bool = False) -> dict:
    from .routers.calendar import CreateEventRequest, create_event
    return _run(lambda: create_event(CreateEventRequest(
        summary=summary, startTime=startTime, endTime=endTime, allDay=allDay, timeZone=timeZone,
        location=location, description=description, attendees=attendees, addGoogleMeetUrl=addGoogleMeetUrl,
    )))


@mcp.tool(
    name="calendar_suggest_time",
    description="Suggest free meeting slots across attendees in a window (ISO startTime/endTime, "
    "durationMinutes). Read-only — proposes times, books nothing.",
)
def calendar_suggest_time(attendeeEmails: list[str], startTime: str, endTime: str,
                          durationMinutes: int = 30, timeZone: str | None = None) -> dict:
    from .routers.calendar import SuggestTimeRequest, suggest_time
    return _run(lambda: suggest_time(SuggestTimeRequest(
        attendeeEmails=attendeeEmails, startTime=startTime, endTime=endTime,
        durationMinutes=durationMinutes, timeZone=timeZone,
    )))


# ===================== In-tray (the one capture surface) =====================
@mcp.tool(
    name="intray",
    description="Owen's Microsoft To Do in-tray — the ONE capture surface for 'remind me to…', 'capture "
    "this', quick tasks. action=list|add|done|delete (add needs title; done/delete need task_id from a "
    "prior list).",
)
def intray(action: str, title: str | None = None, task_id: str | None = None) -> dict:
    from .models import IntrayRequest
    from .routers.intray import intray as _intray
    return _run(lambda: _intray(IntrayRequest(action=action, title=title, task_id=task_id)))


# ===================== GitHub =====================
def _gh_token() -> str:
    token = get_settings().github_read_token
    if not token:
        raise ServiceError("GITHUB_REPO_TOKEN (or GITHUB_TOKEN) is not configured.", status_code=503)
    return token


@mcp.tool(
    name="github_get_file",
    description="Read one UTF-8 file from a GitHub repo (owner/repo/path, optional ref). Read-only.",
)
def github_get_file(owner: str, repo: str, path: str, ref: str | None = None) -> dict:
    from .services.github import GitHubClient

    def go():
        with GitHubClient(_gh_token()) as gh:
            return gh.get_file(owner, repo, path, ref)
    return _run(go)


@mcp.tool(
    name="github_list_prs",
    description="List pull requests on a GitHub repo (state open|closed|all). Read-only.",
)
def github_list_prs(owner: str, repo: str, state: str = "open", limit: int = 20) -> dict:
    from .services.github import GitHubClient

    def go():
        with GitHubClient(_gh_token()) as gh:
            return {"pull_requests": gh.list_prs(owner, repo, state, limit)}
    return _run(go)


@mcp.tool(
    name="github_recent_commits",
    description="Recent commits on a GitHub repo (optional branch). Read-only — one-line messages, author, date.",
)
def github_recent_commits(owner: str, repo: str, branch: str | None = None, limit: int = 10) -> dict:
    from .services.github import GitHubClient

    def go():
        with GitHubClient(_gh_token()) as gh:
            return {"commits": gh.recent_commits(owner, repo, branch, limit)}
    return _run(go)


@mcp.tool(
    name="github_merge_pr",
    description=(
        "Merge a GitHub pull request. SENSITIVE and near-irreversible — NEVER merge silently: with "
        "confirm=false (default) this ONLY returns a preview of what would be merged and changes "
        "nothing. Re-call with confirm=true after Owen confirms. method is merge|squash|rebase."
    ),
)
def github_merge_pr(owner: str, repo: str, number: int, confirm: bool = False, method: str = "merge") -> dict:
    from .services.github import GitHubClient, merge_pr_guarded

    def go():
        with GitHubClient(_gh_token()) as gh:
            return merge_pr_guarded(gh, owner, repo, number, confirm=confirm, method=method)
    return _run(go)


# ---- the mounted ASGI app + bearer guard ----
mcp_app = mcp.streamable_http_app()  # also creates mcp.session_manager (run in main's lifespan)


class BearerAuthASGI:
    """Guard the MCP endpoint with the shared SERVICE_API_KEY (Bearer or X-API-Key).

    If SERVICE_API_KEY is unset the guard is a no-op (open) — same policy as the REST
    layer. This is the interim auth that works for Claude Desktop/Code, Cursor, the
    Pipecat voice shell and Gemini CLI; claude.ai's OAuth is the next step.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Mounted at /mcp: a request to exactly "/mcp" arrives here with path "".
        # Normalize to "/" so the inner app matches its route directly instead of
        # issuing a trailing-slash 307 — that redirect also downgrades https->http
        # behind Railway's TLS proxy, which breaks clients (and claude.ai).
        if scope.get("path") in ("", None):
            scope = dict(scope)
            scope["path"] = "/"
        expected = get_settings().service_api_key
        if expected:
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            xkey = headers.get(b"x-api-key", b"").decode()
            bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            if bearer != expected and xkey != expected:
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="alistair"'),
                ]})
                await send({"type": "http.response.body",
                            "body": b'{"error":"Missing or invalid token for the Alistair MCP."}'})
                return
        return await self.app(scope, receive, send)


mcp_asgi = BearerAuthASGI(mcp_app)
