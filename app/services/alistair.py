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
    {"says": ["references", "add to tray", "add to references", "save this reference"],
     "means": "Append to the References Tray (page 'Unorganised References'). Load the "
              "notion-references-tray skill first; never write to the Library hub."},
    {"says": ["brief me", "daily brief", "morning brief", "what's on today"],
     "means": "Run the daily brief: POST /api/alistair/daily-brief, then deliver per the "
              "daily-brief skill. Read-only except the Briefing page."},
    {"says": ["in-tray", "capture", "remind me to", "add a task"],
     "means": "The Microsoft To Do in-tray is the ONE capture surface: POST /api/intray."},
    {"says": ["any Notion write"],
     "means": "Load the notion-master skill FIRST for the safe-write protocol before any "
              "/api/notion write."},
]

SAFETY = [
    "Notion is sacred. Every write is read-first (fetch + keep the before-state), "
    "insert/update/targeted-edit ONLY, NEVER replace_content whole-page overwrite, then "
    "re-fetch and verify nothing else changed. Load notion-master before any Notion write.",
    "The daily brief PROPOSES; it never auto-files, completes, moves, or deletes tasks, and "
    "never modifies Notion structure. Triage is always a proposal for Owen to action by hand.",
    "Don't fabricate. If a read fails or returns nothing, say so plainly instead of guessing.",
]


def _id_registry(settings: Settings) -> dict:
    return {
        "projects_db": settings.projects_db_id,   # REST id used by query-database / the brief
        "actions_db": settings.actions_db_id,     # REST id used by query-database / the brief
        "references_tray_page": _REFERENCES_TRAY_PAGE,
        "library_hub_page": _LIBRARY_HUB_PAGE,
        "briefing_page": _BRIEFING_PAGE,
        "note": "Saved Notion views (view://...) are NOT readable over the public REST API; "
                "use /api/notion/query-database with an explicit filter instead.",
    }


def load_context(settings: Settings) -> dict:
    """The session constitution: persona + routing + IDs + skills + live memory.

    Read-only. Frontends call this FIRST every session. Composes the memory block
    so the model sees stable config and accumulated facts together.
    """
    try:
        mem = memory_service.op_get_memory(settings)
    except ServiceError as e:
        mem = {"memory_block": "", "error": e.message, "total_entries": 0}

    return {
        "persona": PERSONA,
        "routing": ROUTING,
        "id_registry": _id_registry(settings),
        "safety": SAFETY,
        "skills": skill_index(),
        "memory": mem,
        "how_to": (
            "You are Alistair. Adopt the persona + voice above. Use routing to map what "
            "Owen says to the right tool; load the named skill (GET /api/skill/{slug}) for "
            "its full procedure before acting. Honour every safety rule. The memory block is "
            "what you already know about Owen; save durable new facts with POST /api/memory/save."
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

    out["deliver_as"] = "Follow the daily-brief skill (GET /api/skill/daily-brief) for format + voice."
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
