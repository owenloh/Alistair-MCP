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

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import get_settings
from .services import ServiceError
from .services import alistair as alistair_service
from .services import calendar as calendar_service
from .services import gmail as gmail_service
from .services import memory as memory_service
from .services import notion as notion_service
from .services import spotify as spotify_service
from .services import whatsapp as whatsapp_service
from .skills import list_slugs, load_skill

SERVER_NAME = "alistair_assistant"  # snake_case: Gemini rejects '-' in server names

INSTRUCTIONS = (
    "You are Alistair, Owen's operations assistant (direct, concise, brutally honest, no "
    "em dashes). This connector is the SAME Alistair across every client it's connected to "
    "(claude.ai, voice, Gemini, ChatGPT) — its tools, not this client's own features, are "
    "the shared brain. On first use this session: call load_context (persona + voice + "
    "routing + GTD/PARA workflow + ID registry + safety + skill index) and get_memory. "
    "MEMORY: this connector's store is the ONE shared source of truth for what you know "
    "about Owen — do NOT rely on this client's own memory. get_memory loads the consolidated "
    "block; use search_memory to recall anything older/specific that isn't in it. For ANY factual "
    "recall about Owen ('tell me about myself', 'what do you know about me', who/what/when about him), "
    "retrieve from Alistair (get_memory / search_memory) and answer from THIS store — treat it as "
    "canonical over any local/built-in memory, which may be stale. Save "
    "durable facts/preferences/open-loops with save_memory the moment they surface (read or "
    "search first to dedupe); never save transient or experiment data — that goes to Notion. "
    "At the END of a conversation/session, run a light memory tidy: call memory_maintenance and "
    "merge/retract the obvious duplicates you just created (it returns the full store + the procedure). "
    "Notion is sacred: load the notion-master skill before any Notion write, edit surgically, "
    "and never overwrite a whole page."
)

# OAuth turns on automatically once a public base URL is known (Railway sets
# RAILWAY_PUBLIC_DOMAIN; PUBLIC_BASE_URL overrides). claude.ai needs OAuth; without
# a base URL (local dev) we fall back to the bearer guard below.
BASE_URL = get_settings().resolved_base_url
OAUTH_ENABLED = bool(BASE_URL)

_auth_kwargs: dict = {}
oauth_provider = None  # set below when OAuth is enabled; imported by main for the consent routes
if OAUTH_ENABLED:
    from mcp.server.auth.settings import (
        AuthSettings,
        ClientRegistrationOptions,
        RevocationOptions,
    )

    from .mcp_oauth import SCOPES, SingleUserOAuthProvider

    oauth_provider = SingleUserOAuthProvider(
        service_key_getter=lambda: get_settings().service_api_key,
        approval_secret_getter=lambda: get_settings().oauth_approval_secret,
        base_url=BASE_URL,
    )
    _auth_kwargs = dict(
        auth_server_provider=oauth_provider,
        auth=AuthSettings(
            issuer_url=BASE_URL,
            resource_server_url=f"{BASE_URL}/mcp",
            required_scopes=SCOPES,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
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
    **_auth_kwargs,
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
        "Load what Alistair remembers about Owen: the CONSOLIDATED block (core facts always "
        "included, the rest ranked by recency x importance, token-budgeted). Call at session "
        "start alongside load_context. This is a summary, NOT everything — to recall an older "
        "or specific fact that isn't here, use search_memory. Read-only."
    ),
)
def get_memory(top_n: int | None = None, max_tokens: int | None = None) -> dict:
    return _run(lambda: memory_service.op_get_memory(get_settings(), top_n=top_n, max_tokens=max_tokens))


@mcp.tool(
    name="search_memory",
    description=(
        "Recall ANY stored memory by keyword across the FULL store — not just the consolidated "
        "block get_memory loads. Use it whenever a relevant older/specific fact might not be in "
        "the loaded block (people, past projects, preferences, commitments). Empty query returns "
        "the whole store ranked. Optional type filter: fact|preference|action|summary. Read-only."
    ),
)
def search_memory(query: str | None = None, limit: int = 20, type: str | None = None) -> dict:
    return _run(lambda: memory_service.op_search_memory(
        get_settings(), query=query, limit=limit, type_=type))


@mcp.tool(
    name="save_memory",
    description=(
        "The ONLY way to write Alistair's memory — the shared store every connected client "
        "(claude.ai, voice, Gemini) reads. Save DURABLE things only: a standing fact, a "
        "preference, or an open commitment/loop. Do NOT save transient or experiment data "
        "(coffee numbers, run logs, one-off values) — that belongs in Notion. type is "
        "fact|preference|action|summary; relevance 1-5, and 5 is ONLY for permanent "
        "identity/safety facts (pinned, never evicted) — default 3. Read/search first to avoid "
        "duplicates (writing is append-only + de-duped). To forget, pass op='retract' with the "
        "same type+content. Capture-only 'remind me' tasks go to the in-tray, not here."
    ),
)
def save_memory(content: str, type: str = "fact", relevance: int = 3,
                tags: str | None = None, op: str = "assert") -> dict:
    return _run(lambda: memory_service.op_save_memory(
        get_settings(), content=content, type_=type, relevance=relevance,
        tags=tags, op=op, source="mcp",
    ))


@mcp.tool(
    name="memory_maintenance",
    description=(
        "Get everything needed to CONSOLIDATE Alistair's memory in one call: the full current store "
        "(every entry) plus the step-by-step maintenance procedure. Call this to tidy memory — at the "
        "END of a conversation/session (e.g. a voice agent's wrap-up), during a brief, or when Owen says "
        "'tidy your memory'. Then act on the returned procedure with save_memory (merge near-duplicates "
        "into one canonical entry, retract stale/contradictory/transient entries, downgrade over-pinned "
        "ones). The append-only log is reversible. Read-only itself — it only reads + returns the rules."
    ),
)
def memory_maintenance() -> dict:
    def _do():
        proc = load_skill("memory-maintenance") or {}
        store = memory_service.op_list_memory(get_settings())
        return {
            "procedure": proc.get("instructions", ""),
            "guardrails": "Never invent facts. Ask Owen before deleting personal data you're unsure "
                          "about. Re-assert identity/safety (relevance 5) facts before retracting any "
                          "near-duplicate. Report what you merged/retracted.",
            "store": store,
        }
    return _run(_do)


@mcp.tool(
    name="get_skill",
    description=(
        "Fetch one Alistair skill's full procedure on demand (the index is in load_context). "
        "Slugs: notion-master (safe-write protocol — load before ANY Notion write), daily-brief, "
        "weekly-brief, notion-references-tray, microsoft-todo-intray, spotify (control playback / "
        "browse playlists / pick a device), memory-maintenance (consolidate/tidy long-term memory)."
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
        "(Next/Waiting/Someday/Done); due is an optional ISO date. project optionally files it "
        "under one or more Projects (a Notion page id/URL or a list) via the 'Project' relation, so "
        "it lands under the right Project and Area in PARA — pass it whenever you know the parent "
        "project. Capture-only 'remind me to…' belongs in the in-tray instead."
    ),
)
def add_action(name: str, status: str = "Next", due: str | None = None,
               project: str | list[str] | None = None) -> dict:
    return _run(lambda: notion_service.op_add_action(
        get_settings(), name=name, status=status, due=due, project=project))


@mcp.tool(
    name="whereami",
    description=(
        "Owen's current date, time, timezone and inferred location — call this if you are unsure "
        "'when' or 'where' he is. Read-only. Timezone is his LIVE Google Calendar setting "
        "(auto-detected, follows travel); location is inferred from that timezone, not GPS. If he "
        "pinned a precise home/base via save_memory, prefer that."
    ),
)
def whereami() -> dict:
    return alistair_service.now_context(get_settings())


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
        "Edit a Notion page's text/prose. DANGEROUS — Notion is sacred; load the notion-master skill FIRST. "
        "For STRUCTURAL changes (nesting into a toggle, reordering, deleting specific/duplicate blocks) use the "
        "block-id tools instead (notion_list_blocks -> notion_append_blocks/notion_update_block/"
        "notion_delete_blocks by id). command='update_content' with content_updates=[{old_str, new_str, "
        "replace_all_matches?, allow_cross_block?, allow_deleting_content?}] is the SAFE in-block edit — anchor "
        "old_str on unique existing text; to ADD make new_str start with old_str plus the addition; to DELETE a "
        "block set new_str empty. FAIL-SAFE: if old_str matches >1 place it ERRORS (409) with each match unless "
        "replace_all_matches=true; if old_str would span/delete across blocks it ERRORS (400) unless "
        "allow_cross_block=true; deleting a child page/database ERRORS (400) unless allow_deleting_content=true. "
        "command='insert_content' appends content (after_block_id places it). command='update_properties' edits "
        "database fields. command='replace_content' OVERWRITES THE WHOLE PAGE — almost never what you want. Read "
        "the markdown spec (alistair://docs/notion-markdown-spec or notion_markdown_spec) before composing markdown."
    ),
)
def notion_update_page(page_id: str, command: str, content: str | None = None,
                       content_updates: list[dict] | None = None, new_str: str | None = None,
                       after_block_id: str | None = None, properties: dict | None = None,
                       allow_deleting_content: bool = False) -> dict:
    from .routers.notion import UpdatePageRequest, update_page
    return _run(lambda: update_page(UpdatePageRequest(
        page_id=page_id, command=command, content=content, content_updates=content_updates,
        new_str=new_str, after_block_id=after_block_id, properties=properties,
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


@mcp.tool(
    name="notion_move_pages",
    description="Move one or more Notion pages/databases under a new parent (file them in PARA). "
    "page_or_database_ids is a list of ids; new_parent is e.g. {\"page_id\": \"...\"} or "
    "{\"database_id\": \"...\"}. Notion is sacred — confirm the destination with Owen first.",
)
def notion_move_pages(page_or_database_ids: list[str], new_parent: dict) -> dict:
    from .routers.notion import MovePagesRequest, move_pages
    return _run(lambda: move_pages(MovePagesRequest(
        page_or_database_ids=page_or_database_ids, new_parent=new_parent)))


@mcp.tool(
    name="notion_duplicate_page",
    description="Duplicate a Notion page (templating — e.g. spin up a new project from a template). "
    "Non-destructive (creates a copy); shallow copy of top-level blocks. Returns the new page.",
)
def notion_duplicate_page(page_id: str) -> dict:
    from .routers.notion import DuplicatePageRequest, duplicate_page
    return _run(lambda: duplicate_page(DuplicatePageRequest(page_id=page_id)))


@mcp.tool(
    name="notion_get_comments",
    description="Read the comments/discussion on a Notion page. Read-only. include_resolved=true also "
    "returns resolved threads.",
)
def notion_get_comments(page_id: str, include_resolved: bool = False) -> dict:
    from .routers.notion import GetCommentsRequest, get_comments
    return _run(lambda: get_comments(GetCommentsRequest(page_id=page_id, include_resolved=include_resolved)))


@mcp.tool(
    name="notion_create_comment",
    description="Add a comment to a Notion page (markdown). Use when Owen wants to leave a note or "
    "question on a page without editing its content.",
)
def notion_create_comment(page_id: str, markdown: str) -> dict:
    from .routers.notion import CreateCommentRequest, create_comment
    return _run(lambda: create_comment(CreateCommentRequest(page_id=page_id, markdown=markdown)))


# ---- block-ID primitives (deterministic structure; claude.ai's connector lacks these) ----
@mcp.tool(
    name="notion_list_blocks",
    description=(
        "List a Notion page/block's children as {id, type, text, has_children, parent_id, depth}. "
        "recursive=true walks the whole subtree; otherwise paginate with start_cursor/next_cursor. "
        "The ids are deterministic handles for the block-id write tools below. Read-only. To delete "
        "specific/duplicate blocks, get their ids here then call notion_delete_blocks — NEVER delete "
        "by text match."
    ),
)
def notion_list_blocks(page_id: str, recursive: bool = False, start_cursor: str | None = None) -> dict:
    from .routers.notion import ListBlocksRequest, list_blocks
    return _run(lambda: list_blocks(ListBlocksRequest(
        page_id=page_id, recursive=recursive, start_cursor=start_cursor)))


@mcp.tool(
    name="notion_append_blocks",
    description=(
        "Append typed Notion block objects under a parent (native nesting; no markdown round-trip). "
        "blocks = list of Notion REST block objects, e.g. a toggle with children "
        "{\"type\":\"toggle\",\"toggle\":{\"rich_text\":[...],\"children\":[...]}}; `after` places them "
        "after an existing child by id. To nest loose blocks into a toggle: append a toggle WITH "
        "children here, then delete the old loose blocks by id with notion_delete_blocks. Read the "
        "notion-markdown-spec (resource or notion_markdown_spec tool) for block shapes; do not guess."
    ),
)
def notion_append_blocks(parent_id: str, blocks: list[dict], after: str | None = None) -> dict:
    from .routers.notion import AppendBlocksRequest, append_blocks
    return _run(lambda: append_blocks(AppendBlocksRequest(parent_id=parent_id, blocks=blocks, after=after)))


@mcp.tool(
    name="notion_update_block",
    description=(
        "Update ONE Notion block in place by id. block = the type payload, e.g. "
        "{\"paragraph\":{\"rich_text\":[...]}} or {\"to_do\":{\"checked\":true}}. Get the id from "
        "notion_list_blocks/notion_fetch. Cannot change a block's type."
    ),
)
def notion_update_block(block_id: str, block: dict) -> dict:
    from .routers.notion import UpdateBlockRequest, update_block
    return _run(lambda: update_block(UpdateBlockRequest(block_id=block_id, block=block)))


@mcp.tool(
    name="notion_delete_blocks",
    description=(
        "Delete specific Notion blocks by id — deterministic: ONLY the listed blocks are removed. "
        "THIS is the safe way to delete duplicates or specific blocks; NEVER delete by text match. "
        "block_ids from notion_list_blocks/notion_fetch. Returns per-id success. Fails (lists them) if "
        "a block is/contains a child page or database unless allow_deleting_content=true."
    ),
)
def notion_delete_blocks(block_ids: list[str], allow_deleting_content: bool = False) -> dict:
    from .routers.notion import DeleteBlocksRequest, delete_blocks
    return _run(lambda: delete_blocks(DeleteBlocksRequest(
        block_ids=block_ids, allow_deleting_content=allow_deleting_content)))


@mcp.tool(
    name="notion_move_blocks",
    description=(
        "Move blocks (in order) to directly after another block, within that block's parent — "
        "reorder/restructure. No native REST move, so each block's subtree is copied to the new "
        "position and the original deleted (children preserved; ids change). Get ids from "
        "notion_list_blocks."
    ),
)
def notion_move_blocks(block_ids: list[str], after_block_id: str) -> dict:
    from .routers.notion import MoveBlocksRequest, move_blocks
    return _run(lambda: move_blocks(MoveBlocksRequest(block_ids=block_ids, after_block_id=after_block_id)))


@mcp.tool(
    name="notion_markdown_spec",
    description=(
        "Return the Alistair Notion-flavored markdown spec (exact dialect for headings, lists, "
        "dividers, toggles + nesting, callouts, tables, code, math, mentions, colors). Read this BEFORE "
        "composing markdown for any Notion write; do not guess syntax. Same text as the MCP resource "
        "alistair://docs/notion-markdown-spec."
    ),
)
def notion_markdown_spec() -> dict:
    from .docs import NOTION_MARKDOWN_SPEC
    return {"uri": "alistair://docs/notion-markdown-spec", "format": "markdown",
            "content": NOTION_MARKDOWN_SPEC}


# ===================== MCP resources (progressive enhancement) =====================
# Every served doc is ALSO an equivalent tool above, so clients that don't load
# resources (Gemini/voice) still get the content. Resource-capable clients
# (claude.ai, Claude Desktop, Cursor) get the cleaner resource view.
@mcp.resource(
    "alistair://docs/notion-markdown-spec",
    name="Notion markdown spec",
    description="The exact Notion-flavored markdown dialect Alistair reads/writes. Read before "
    "composing markdown for any Notion write. Equivalent tool: notion_markdown_spec.",
    mime_type="text/markdown",
)
def _res_notion_markdown_spec() -> str:
    from .docs import NOTION_MARKDOWN_SPEC
    return NOTION_MARKDOWN_SPEC


@mcp.resource(
    "alistair://skills/{slug}",
    name="Alistair skill",
    description="An Alistair skill's full rules (notion-master, daily-brief, notion-references-tray, "
    "microsoft-todo-intray, gmail). Equivalent tool: get_skill.",
    mime_type="application/json",
)
def _res_skill(slug: str) -> str:
    data = load_skill(slug)
    if data is None:
        return json.dumps({"error": f"Unknown skill '{slug}'.", "available": list_slugs()})
    return json.dumps(data, ensure_ascii=False)


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


@mcp.tool(
    name="calendar_get_event",
    description="Get one calendar event by id (full details: time, attendees, location, description). Read-only.",
)
def calendar_get_event(eventId: str, calendarId: str | None = None) -> dict:
    from .routers.calendar import GetEventRequest, get_event
    return _run(lambda: get_event(GetEventRequest(eventId=eventId, calendarId=calendarId)))


@mcp.tool(
    name="calendar_update_event",
    description="Edit an EXISTING calendar event (by eventId): change summary/startTime/endTime/location/"
    "description, or set allDay. Only the fields you pass change. Confirm the change with Owen first.",
)
def calendar_update_event(eventId: str, summary: str | None = None, startTime: str | None = None,
                          endTime: str | None = None, allDay: bool = False, timeZone: str | None = None,
                          location: str | None = None, description: str | None = None,
                          addGoogleMeetUrl: bool = False) -> dict:
    from .routers.calendar import UpdateEventRequest, update_event
    return _run(lambda: update_event(UpdateEventRequest(
        eventId=eventId, summary=summary, startTime=startTime, endTime=endTime, allDay=allDay,
        timeZone=timeZone, location=location, description=description, addGoogleMeetUrl=addGoogleMeetUrl)))


@mcp.tool(
    name="calendar_delete_event",
    description="Delete a calendar event by id. SENSITIVE and hard to undo — confirm with Owen before "
    "calling; never delete silently.",
)
def calendar_delete_event(eventId: str, calendarId: str | None = None) -> dict:
    from .routers.calendar import DeleteEventRequest, delete_event
    return _run(lambda: delete_event(DeleteEventRequest(eventId=eventId, calendarId=calendarId)))


@mcp.tool(
    name="calendar_respond_to_event",
    description="RSVP to an event Owen was invited to: responseStatus = accepted | declined | tentative. "
    "Optional responseComment. (Only works on events where Owen is an attendee, not ones he organizes.)",
)
def calendar_respond_to_event(eventId: str, responseStatus: str, responseComment: str | None = None) -> dict:
    from .routers.calendar import RespondToEventRequest, respond_to_event
    return _run(lambda: respond_to_event(RespondToEventRequest(
        eventId=eventId, responseStatus=responseStatus, responseComment=responseComment)))


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
        raise ServiceError("GITHUB_REPO_TOKEN (or GITHUB_GIST_TOKEN) is not configured.", status_code=503)
    return token


@mcp.tool(
    name="github_whoami",
    description="Identify the GitHub account Alistair's token belongs to: login, name, type and "
    "repo counts (including how many private repos the token can reach). Read-only — answers "
    "'what's my GitHub account' / 'who am I on GitHub' / 'which account are you acting as'.",
)
def github_whoami() -> dict:
    from .services.github import GitHubClient

    def go():
        with GitHubClient(_gh_token()) as gh:
            return gh.get_authenticated_user()
    return _run(go)


@mcp.tool(
    name="github_list_my_repos",
    description="List Owen's GitHub repos — every repository the token can reach, public AND "
    "private, most-recently-active first. Read-only. Use this to discover owner/repo before the "
    "per-repo tools (no need to know the name up front). visibility is all|public|private; "
    "affiliation optionally filters owner|collaborator|organization_member.",
)
def github_list_my_repos(visibility: str = "all", affiliation: str | None = None,
                         sort: str = "pushed", limit: int = 30) -> dict:
    from .services.github import GitHubClient

    def go():
        with GitHubClient(_gh_token()) as gh:
            return gh.list_my_repos(visibility, affiliation, sort, limit)
    return _run(go)


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


# ===================== Gmail (read + draft) =====================
@mcp.tool(
    name="gmail_search",
    description=(
        "Search Owen's Gmail with Gmail query syntax (e.g. 'from:bank newer_than:7d', "
        "'is:unread label:work', 'subject:invoice has:attachment'). Read-only. Returns "
        "message stubs (from, subject, snippet, date, thread_id); follow up with "
        "gmail_read_thread to read one. Keep max_results small."
    ),
)
def gmail_search(query: str, max_results: int = 20) -> dict:
    return _run(lambda: gmail_service.search(get_settings(), query=query, max_results=max_results))


@mcp.tool(
    name="gmail_read_thread",
    description=(
        "Read one Gmail thread by thread_id — every message rendered to clean text "
        "(headers + body). Read-only. Summarise it for Owen; don't dump the raw text, and "
        "don't repeat secrets/2FA codes you see."
    ),
)
def gmail_read_thread(thread_id: str) -> dict:
    return _run(lambda: gmail_service.get_thread(get_settings(), thread_id=thread_id))


@mcp.tool(
    name="gmail_list_drafts",
    description="List Owen's existing Gmail drafts (draft_id, to, subject, snippet). Read-only.",
)
def gmail_list_drafts(max_results: int = 20) -> dict:
    return _run(lambda: gmail_service.list_drafts(get_settings(), max_results=max_results))


@mcp.tool(
    name="gmail_create_draft",
    description=(
        "Draft an email for Owen — creates a DRAFT only, it NEVER sends. Provide to, subject, "
        "body (optional cc). For a reply, pass thread_id and in_reply_to (the original "
        "message's Message-ID) so it threads. Write in Owen's voice, keep it tight, then show "
        "him the draft — sending stays his action in Gmail."
    ),
)
def gmail_create_draft(to: str, subject: str, body: str, cc: str | None = None,
                       thread_id: str | None = None, in_reply_to: str | None = None) -> dict:
    return _run(lambda: gmail_service.create_draft(
        get_settings(), to=to, subject=subject, body=body, cc=cc,
        thread_id=thread_id, in_reply_to=in_reply_to,
    ))


# ===================== WhatsApp (read + draft, NEVER sends) =====================
@mcp.tool(
    name="whatsapp_chats",
    description=(
        "List Owen's recent WhatsApp chats (chat id, name, last-message time, unread). "
        "Read-only, and ONLINE-ONLY: it reads from the WhatsApp agent on Owen's laptop, so it "
        "only works while that laptop is on — if it's offline, say so plainly (don't "
        "fabricate). Use a returned chat id with whatsapp_read."
    ),
)
def whatsapp_chats(limit: int = 20) -> dict:
    return _run(lambda: whatsapp_service.list_chats(get_settings(), limit=limit))


@mcp.tool(
    name="whatsapp_read",
    description=(
        "Read recent messages in one WhatsApp chat by its chat id (from whatsapp_chats). "
        "Read-only, online-only (via Owen's laptop agent). Summarise for Owen; it's his "
        "private messaging — never repeat secrets, OTP/2FA codes or passwords you see."
    ),
)
def whatsapp_read(chat: str, limit: int = 30) -> dict:
    return _run(lambda: whatsapp_service.read_messages(get_settings(), chat=chat, limit=limit))


@mcp.tool(
    name="whatsapp_search",
    description=(
        "Search Owen's WhatsApp messages by text. Read-only, online-only (laptop agent). "
        "Returns matches with their chat id so you can whatsapp_read the full thread."
    ),
)
def whatsapp_search(query: str, limit: int = 20) -> dict:
    return _run(lambda: whatsapp_service.search(get_settings(), query=query, limit=limit))


@mcp.tool(
    name="whatsapp_recent",
    description=(
        "WhatsApp inbox — the most recent chats with a last-message preview + unread count, "
        "newest first. Read-only, ONLINE-ONLY (laptop agent). Best tool for 'what's new on "
        "WhatsApp' / 'any new messages' — one call, no opening each chat. If the laptop's off, say so."
    ),
)
def whatsapp_recent(limit: int = 15) -> dict:
    return _run(lambda: whatsapp_service.recent(get_settings(), limit=limit))


@mcp.tool(
    name="whatsapp_find",
    description=(
        "Find a WhatsApp chat by CONTACT NAME or phone number and read its recent messages in one "
        "step. Read-only, online-only. Resolves e.g. 'Chloe' (or a number) to the right chat and "
        "returns {jid,name,number} + recent messages. Use this for 'read my chat with X' / 'what did "
        "X say on WhatsApp' instead of guessing from whatsapp_search text matches. Says so if no match."
    ),
)
def whatsapp_find(query: str, limit: int = 20) -> dict:
    return _run(lambda: whatsapp_service.find(get_settings(), query=query, limit=limit))


@mcp.tool(
    name="whatsapp_draft",
    description=(
        "Draft a WhatsApp message for Owen — returns a wa.me link that opens his NORMAL "
        "WhatsApp with the text pre-filled in the compose box for him to review and SEND "
        "HIMSELF. It NEVER sends and needs no laptop/session. 'to' = a phone number (any "
        "format; a bare local number uses the default country code) or a contact name "
        "(resolved via the laptop agent if it's online); 'body' = the message. 1:1 only. "
        "Write in Owen's voice, keep it tight, then hand him the link."
    ),
)
def whatsapp_draft(to: str, body: str) -> dict:
    return _run(lambda: whatsapp_service.draft(get_settings(), to=to, body=body))


# ===================== Spotify (unofficial API via SpotAPI) =====================
# Descriptions are imported verbatim from the router docs (single source of truth).
from .routers import _spotify_docs as _spdocs  # noqa: E402


@mcp.tool(name="spotify_playlists", description=_spdocs.LIST_PLAYLISTS)
def spotify_playlists(limit: int = 50) -> dict:
    return _run(lambda: spotify_service.list_playlists(get_settings(), limit=limit))


@mcp.tool(name="spotify_playlist_tracks", description=_spdocs.PLAYLIST_TRACKS)
def spotify_playlist_tracks(playlist: str, limit: int = 50) -> dict:
    return _run(lambda: spotify_service.playlist_tracks(get_settings(), playlist, limit=limit))


@mcp.tool(name="spotify_search", description=_spdocs.SEARCH)
def spotify_search(query: str, limit: int = 10) -> dict:
    return _run(lambda: spotify_service.search_tracks(get_settings(), query, limit=limit))


@mcp.tool(name="spotify_devices", description=_spdocs.DEVICES)
def spotify_devices() -> dict:
    return _run(lambda: spotify_service.list_devices(get_settings()))


@mcp.tool(name="spotify_transfer", description=_spdocs.TRANSFER)
def spotify_transfer(device: str) -> dict:
    return _run(lambda: spotify_service.transfer_playback(get_settings(), device))


@mcp.tool(name="spotify_status", description=_spdocs.STATUS)
def spotify_status() -> dict:
    return _run(lambda: spotify_service.now_playing(get_settings()))


@mcp.tool(name="spotify_play", description=_spdocs.PLAY)
def spotify_play(track: str, playlist: str | None = None, device: str | None = None) -> dict:
    return _run(lambda: spotify_service.play(
        get_settings(), track=track, playlist=playlist, device=device))


@mcp.tool(name="spotify_queue", description=_spdocs.QUEUE)
def spotify_queue(track: str) -> dict:
    return _run(lambda: spotify_service.queue(get_settings(), track))


@mcp.tool(name="spotify_control", description=_spdocs.CONTROL)
def spotify_control(action: str, value: float | None = None) -> dict:
    return _run(lambda: spotify_service.control(get_settings(), action, value))


# ---- the mounted ASGI app + auth ----
mcp_app = mcp.streamable_http_app()  # also creates mcp.session_manager (run in main's lifespan)

# When OAuth is on the SDK adds its endpoints (/authorize, /token, /register,
# /revoke, /.well-known/*) at the app root. Collect them so main's dispatcher can
# route those exact paths to the MCP app too (not just /mcp).
OAUTH_PATHS: set[str] = set()
if OAUTH_ENABLED:
    for _route in mcp_app.routes:
        _p = getattr(_route, "path", None)
        if _p and _p != "/":
            OAUTH_PATHS.add(_p)


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


# OAuth on -> the SDK's own middleware enforces auth on /mcp (and the OAuth routes
# are public), and the provider also accepts the SERVICE_API_KEY as a bearer token,
# so existing bearer clients keep working. OAuth off -> use the bearer guard.
mcp_asgi = mcp_app if OAUTH_ENABLED else BearerAuthASGI(mcp_app)
