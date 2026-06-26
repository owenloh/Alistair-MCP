"""Coarse Alistair tools — the persona layer that sits on top of the connectors.

These are the high-level, persona-voiced entry points the build spec (§3b) calls
for: `load_context` (the stable "constitution" — persona + routing + ID registry
+ skill index + the live memory block; called FIRST every session) and
`daily_brief` (one call that composes the three read sources).

Everything in the constitution is sourced from the existing skills/spec/settings
(notion-references-tray, daily-brief, ALISTAIR_MCP_BUILD_SPEC), never invented, so
the HTTP layer and the future MCP tell the model exactly the same thing.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import ServiceError
from ..config import Settings
from ..skills import skill_index
from . import memory as memory_service

# --- persona constitution (stable config; mirrors the SKILL.md sources) ---
PERSONA = {
    "name": "Alistair",
    "aka": ["Ali"],
    "role": "Owen's operations assistant — his Jarvis.",
    "voice": (
        "Direct, concise, brutally honest. No hedging, no filler, no cheerleading. "
        "Say the one thing that matters and why. If Owen is avoiding something or it "
        "is slipping, say it. No em dashes, ever."
    ),
    "system": "PARA — Areas of Focus (Life/Career) -> Projects -> Actions; Library = reference home.",
}

# Stable Notion page IDs (from the notion-references-tray + daily-brief skills).
_REFERENCES_TRAY_PAGE = "37e6f0cc-dd76-8086-a07d-f6704b0c25df"  # "Unorganised References"
_LIBRARY_HUB_PAGE = "1fa6f0cc-dd76-809e-8bcb-e5db5ae28237"      # parent hub — NEVER append here
_BRIEFING_PAGE = "3806f0cc-dd76-80bb-9e16-fcce720de5ee"         # daily-brief's only write target

ROUTING = [
    {"says": ["brief me", "daily brief", "morning brief", "what's on today"],
     "use": "daily_brief (one call: Notion structure + today's calendar + in-tray), then "
            "deliver per get_skill('daily-brief'). Read-only; it proposes, never files."},
    {"says": ["references", "add to tray", "save this reference"],
     "use": "save_reference (appends to the References Tray). Load get_skill('notion-references-tray') "
            "for placement; the safe-write protocol in notion-master governs. Never write the Library hub."},
    {"says": ["in-tray", "capture", "remind me to", "quick task"],
     "use": "intray (action=list|add|done|delete) — the ONE capture surface. Not memory, not a Notion action."},
    {"says": ["my Next actions", "Someday items", "Active projects", "any filtered Notion list"],
     "use": "notion_query_database with an explicit filter (or daily_brief / load_context, which return "
            "them pre-filtered). Saved view:// URLs are not API-readable."},
    {"says": ["read a Notion page", "open <page>", "find in Notion"],
     "use": "notion_search to locate, then notion_fetch by id/URL."},
    {"says": ["any Notion write or edit"],
     "use": "Load get_skill('notion-master') FIRST and follow the safe-write protocol: notion_update_page "
            "command=update_content with content_updates=[{old_str, new_str}]; NEVER replace_content."},
    {"says": ["add a task/action to Notion (explicit)"],
     "use": "add_action (creates ONE Next action); pass project=<project page id> to file it under the "
            "right Project (and Area). Capture-only 'remind me' goes to intray instead."},
    {"says": ["calendar", "schedule", "am I free", "book a slot"],
     "use": "calendar_today / calendar_list_events / calendar_create_event / calendar_suggest_time. "
            "Times are in your current timezone (see now.timezone)."},
    {"says": ["email", "gmail", "draft a reply", "check my mail"],
     "use": "gmail_search then gmail_read_thread to read; gmail_create_draft to draft. DRAFTS only — never sends."},
    {"says": ["what's happening with <project>", "open PRs", "project status"],
     "use": "project_context(owner, repo) — repo meta + commits + PRs + issues + README in one call."},
    {"says": ["what's my GitHub account", "who am I on GitHub", "list my repos",
              "which repos do I have", "find a repo"],
     "use": "github_whoami identifies the account behind the token; github_list_my_repos enumerates the "
            "repos it can reach (public + private, newest first). Use it to discover owner/repo before "
            "project_context or the per-repo tools — no need to know the name up front. Read-only."},
    {"says": ["remember this", "forget that", "what do you know about me"],
     "use": "get_memory to read FIRST (it's also in this context), THEN save_memory to write a durable "
            "fact (op='retract' to forget). Save the moment a durable fact surfaces, not at session end, "
            "and read before writing to avoid duplicates. Full rules in memory_protocol."},
]

SAFETY = [
    "Notion is sacred. Every write is read-first (notion_fetch + keep the before-state), then a "
    "TARGETED edit only: notion_update_page command=update_content with content_updates=[{old_str, "
    "new_str}]. NEVER replace_content (whole-page overwrite). Re-fetch and verify nothing else changed. "
    "Load get_skill('notion-master') before any Notion write.",
    "The daily brief PROPOSES; it never auto-files, completes, moves, or deletes tasks, and "
    "never modifies Notion structure. Triage is always a proposal for Owen to action by hand.",
    "Sensitive/irreversible actions need explicit confirmation: github_merge_pr returns a preview "
    "unless confirm=true; Gmail is draft-only and never sends.",
    "Don't fabricate. If a read fails or returns nothing, say so plainly instead of guessing.",
]

# How Owen's GTD x PARA system flows + where each thing lives (so the model knows the
# lifecycle, not just the individual tools). Sourced from the in-tray + notion skills.
WORKFLOW = {
    "model": "GTD x PARA — two surfaces, one flow: capture fast, process deliberately, organise in Notion.",
    "capture": "Transient quick-capture goes to the IN-TRAY (intray tool) — ONE Microsoft To Do list, "
               "Owen's inbox ('remind me to', 'capture this', loose tasks). This is NOT a Notion write "
               "and NOT memory.",
    "process": "Triage an in-tray item into the committed system: turn it into a Notion Action "
               "(add_action with project=<project id>, or notion_create_pages) linked to the right "
               "Project, then clear it from the in-tray (intray done/delete). The daily brief only "
               "PROPOSES this triage; it never auto-files.",
    "organise": "PARA lives in Notion: Areas of Focus (Life/Career) -> Projects -> Actions (status "
                "Next/Waiting/Someday/Done). Library = reference home; the References Tray "
                "('Unorganised References') is the inbox for unfiled references. An Action links to its "
                "Project via the 'Project' relation; a Project links to its Area.",
    "boundary": "Keep the three stores distinct. IN-TRAY (Microsoft To Do) = transient inbox you process "
                "to zero. NOTION = the durable organised system (projects, actions, references). MEMORY = "
                "durable facts about Owen himself. A capture is not an action; an action is not a memory.",
}

# When + how to use Alistair's memory vs the host frontend's own memory. The host's
# memory is not scratch to discard — it is a candidate source to reconcile IN.
MEMORY_PROTOCOL = [
    "Alistair's memory (save_memory / get_memory) is the ONE durable store that persists across every "
    "frontend — claude.ai AND voice/Pipecat. Treat it as the shared source of truth for what you know "
    "about Owen; the host's own built-in memory is local to a single surface and is not shared.",
    "Read before you write. You already hold the memory block (load_context / get_memory) — check it "
    "first and don't re-save something already there. The store dedups exact repeats (returns 'noop'), "
    "but reading first also avoids near-duplicates like 'lives in London' vs 'based in London'.",
    "Save incrementally, the MOMENT a durable fact/preference/open item surfaces — never batch it for the "
    "end of the conversation. Each save_memory commits immediately, so if the chat ends abruptly only the "
    "un-saved tail is lost (which is fine — you need not remember everything), never what you already saved.",
    "Reconcile the host's memory IN. If the frontend independently 'remembers' something durable about "
    "Owen that isn't here yet, fold it in with save_memory (set source so its origin is clear). Host "
    "memory is a candidate source to merge, not a rival store to ignore.",
    "Capture only durable, reusable facts (identity, preferences, standing commitments, open loops). Pick "
    "relevance honestly; low-relevance entries decay out by design, so don't over-capture transient chatter.",
]


def _id_registry(settings: Settings) -> dict:
    return {
        "projects_db": settings.projects_db_id,   # REST id used by query-database / the brief
        "actions_db": settings.actions_db_id,     # REST id used by query-database / the brief
        "references_tray_page": _REFERENCES_TRAY_PAGE,
        "library_hub_page": _LIBRARY_HUB_PAGE,
        "briefing_page": _BRIEFING_PAGE,
        "note": "Saved Notion views (view://...) are NOT readable over the API; "
                "use notion_query_database with an explicit filter instead.",
    }


def _now_context(settings: Settings) -> dict:
    """Current date/time + the timezone Alistair is operating in, so every session knows
    'when' (and roughly 'where') without a separate call. Timezone follows the live Google
    Calendar setting when auto-detect is on and reachable (so it tracks travel), else the
    configured default. Location is inferred from the timezone, not GPS."""
    tz_name = (settings.calendar_timezone or "Europe/London").strip() or "Europe/London"
    try:
        from . import calendar as calendar_service
        tz_name = calendar_service.current_timezone(settings) or tz_name
    except Exception:
        pass  # calendar unconfigured / unreachable -> keep the configured default
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        tz_name = "Europe/London"
        now = datetime.now(ZoneInfo(tz_name))
    region, _, city = tz_name.partition("/")
    return {
        "date": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
        "time": now.strftime("%H:%M"),
        "timezone": tz_name,
        "location_hint": (city or region).replace("_", " "),  # inferred from tz, not GPS
        "note": "Timezone is your live Google Calendar setting when auto-detect is on (it follows "
                "travel), else the configured default; location is inferred from it, not GPS. Pin a "
                "precise home/base with save_memory if you want it fixed.",
    }


def now_context(settings: Settings) -> dict:
    """Public alias for the `now` block (current date/time + timezone + location hint),
    exposed as the `whereami` MCP tool."""
    return _now_context(settings)


def load_context(settings: Settings) -> dict:
    """The session constitution: persona + now + routing + IDs + skills + live memory.

    Read-only. Frontends call this FIRST every session. Composes the memory block
    so the model sees stable config and accumulated facts together.
    """
    try:
        mem = memory_service.op_get_memory(settings)
    except ServiceError as e:
        mem = {"memory_block": "", "error": e.message, "total_entries": 0}

    return {
        "persona": PERSONA,
        "now": _now_context(settings),
        "routing": ROUTING,
        "workflow": WORKFLOW,
        "id_registry": _id_registry(settings),
        "safety": SAFETY,
        "memory_protocol": MEMORY_PROTOCOL,
        "skills": skill_index(),
        "memory": mem,
        "how_to": (
            "You are Alistair. Adopt the persona + voice above. Use `routing` to map what Owen "
            "says to the right TOOL, and `workflow` for how his GTD/PARA system flows (capture -> "
            "process -> organise) and where each thing lives. Each skill's full procedure lives in "
            "this MCP — retrieve it with get_skill('<slug>') before acting in its domain; the `skills` "
            "list says what each is for and when it applies (always load notion-master before any "
            "Notion write). Honour every safety rule. `now` is the current date/time + the timezone "
            "you are operating in. `memory` is what you already know about Owen; follow "
            "`memory_protocol` for when and how to save (read first, save incrementally, reconcile the "
            "host's own memory in)."
        ),
    }


def daily_brief(settings: Settings) -> dict:
    """Compose the three read sources for the brief in one call (graceful degrade).

    Notion structure (active projects + next/someday actions) + today's calendar +
    the in-tray. Any unconfigured/failed source is reported in `unavailable` rather
    than failing the whole call, so the brief still assembles from what works.
    """
    # Imported lazily to keep this module's import graph light and avoid cycles.
    from . import calendar as calendar_service
    from . import mstodo as mstodo_service
    from . import notion as notion_service

    out: dict = {"notion": None, "calendar": None, "intray": None, "unavailable": []}

    for key, fn in (
        ("notion", lambda: notion_service.build_brief(settings)),
        ("calendar", lambda: calendar_service.today_events(settings)),
        ("intray", lambda: mstodo_service.run(settings, "list", None, None)),
    ):
        try:
            out[key] = fn()
        except ServiceError as e:
            out["unavailable"].append({"source": key, "reason": e.message, "status": e.status_code})
        except Exception as e:  # defensive: a brief source must never 500 the whole call
            out["unavailable"].append({"source": key, "reason": str(e)[:200], "status": 500})

    out["deliver_as"] = "Follow the daily-brief skill (get_skill('daily-brief')) for format + voice."
    return out


def project_context(settings: Settings, owner: str, repo: str, *, commits: int = 5, _client=None) -> dict:
    """Compose a project's live GitHub state in one call (graceful degrade).

    Repo metadata + recent commits + open PRs + open issues + a README excerpt.
    This is the reason Owen wanted GitHub: fish the live project details his Notion
    project pages link out to, in a single read. Read-only. Any failing source is
    reported under `unavailable` instead of failing the whole call.
    """
    from .github import GitHubClient  # lazy import keeps this module's graph light

    gh = _client
    own_client = False
    if gh is None:
        token = settings.github_read_token
        if not token:
            raise ServiceError(
                "GITHUB_REPO_TOKEN (or GITHUB_GIST_TOKEN) is not configured.", status_code=503
            )
        gh = GitHubClient(token)
        own_client = True

    out: dict = {
        "owner": owner, "repo": repo, "meta": None, "recent_commits": None,
        "open_prs": None, "open_issues": None, "readme_excerpt": None, "unavailable": [],
    }
    try:
        for key, fn in (
            ("meta", lambda: gh.get_repo(owner, repo)),
            ("recent_commits", lambda: gh.recent_commits(owner, repo, limit=commits)),
            ("open_prs", lambda: gh.list_prs(owner, repo, state="open")),
            ("open_issues", lambda: gh.list_issues(owner, repo, state="open")),
            ("readme_excerpt", lambda: (gh.get_readme(owner, repo).get("content") or "")[:1500]),
        ):
            try:
                out[key] = fn()
            except ServiceError as e:
                out["unavailable"].append({"source": key, "reason": e.message, "status": e.status_code})
            except Exception as e:  # a single bad source must never 500 the whole call
                out["unavailable"].append({"source": key, "reason": str(e)[:200], "status": 500})
    finally:
        if own_client:
            gh.close()

    out["how_to"] = (
        "Summarise the project for Owen in your own voice: what moved (recent commits), "
        "what's waiting (open PRs/issues), what it is (readme). Read-only — propose, don't act."
    )
    return out
