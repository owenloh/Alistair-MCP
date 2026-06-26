# Alistair Skills API

An HTTP + **remote MCP** service that mirrors Claude's **Notion**, **Google
Calendar**, and **Gmail** connectors, the **Microsoft To Do in-tray**, and the
**PARA skills** — so a **voice-mode / claude.ai Claude** (which can't use the
desktop connectors/skills) can reproduce the *same behaviour*. The same tools ship
as one remote MCP (`alistair_assistant`, Streamable-HTTP at `/mcp`) **and** as
plain HTTP endpoints, and the skills are served *inside* the MCP via `get_skill`,
so Alistair is self-contained — no separate skill uploads or other connectors needed.

The trick: each connector tool is re-exposed as an HTTP endpoint whose
description is copied (near-)verbatim from the real connector, backed by the
official REST API. Same model + same tool descriptions + HTTP access ⇒ same
behaviour, without the connectors.

## Three layers

| Layer | What it is | Endpoints |
|-------|-----------|-----------|
| **Function APIs** | Connector tools that *do* things | `/api/notion/*` (16), `/api/calendar/*` (9), `/api/gmail/*` (6), `/api/intray` (1), `/api/github/*` (11), `/api/memory/*` (3), `/api/alistair/*` (5) |
| **Description APIs** | Skills that tell Claude *what to do* (no code) | `/api/skill/{notion-master \| daily-brief \| notion-references-tray \| microsoft-todo-intray \| gmail}` (also via the MCP `get_skill` tool) |
| **Manifest** | The catalogue of everything | `GET /api/manifest`, plus `/docs` and `/openapi.json` |

So it is **~55 endpoints** — five "connectors" (Notion, Calendar, Gmail, in-tray,
GitHub) that each contain many tool-APIs, plus the persona/memory layer, the skill
description-APIs, and discovery. The high-value subset is also exposed as **37 MCP
tools** on `alistair_assistant`.

### Notion function APIs (`/api/notion/*`)
`search`, `fetch`, `create-pages`, `update-page`, `move-pages`, `duplicate-page`,
`create-database`, `update-data-source`, `create-comment`, `get-comments`,
`get-users`, `get-teams`, `create-view`, `update-view`, `query-database`,
`query` (the PARA daily-brief read: `ACTIVE_PROJECTS`, `NEXT_ACTIONS`,
`SOMEDAY_PROJECTS`, `SOMEDAY_ACTIONS`), plus the **block-id primitives**
`list-blocks`, `append-blocks`, `update-block`, `delete-blocks`, `move-blocks`.

### Google Calendar function APIs (`/api/calendar/*`)
`today`, `list-events`, `list-calendars`, `get-event`, `create-event`,
`update-event`, `delete-event`, `respond-to-event`, `suggest-time`.

### In-tray function API (`/api/intray`)
`POST /api/intray` with `{"action": "list"|"add"|"delete"|"done", "title"?, "task_id"?}`
(Microsoft To Do, single hard-scoped list, self-rolling token via a private gist).

### Gmail function APIs (`/api/gmail/*`)
`search`, `get-thread`, `list-drafts`, `create-draft`, `update-draft`, `delete-draft`
— **read + draft only; it never sends.** Rides the same Google token as Calendar
(needs the `gmail.readonly` + `gmail.compose` scopes; see `scripts/get_google_token.py`).

## Fidelity (honest notes)

Backends are the official REST APIs, so behaviour matches the connectors closely
but not always byte-for-byte:

- **Exact / clean:** all Calendar tools, the in-tray, Notion `search`, `fetch`,
  `query-database`, `query`, `get-users`, `create-pages`, `update-page`
  (`update_properties` / `insert_content` / `update_content` / `replace_content`),
  `create-comment`, `get-comments`, and the block-id tools `list-blocks` /
  `append-blocks` / `update-block` / `delete-blocks`.
- **Best-effort:** `duplicate-page` (shallow copy of top-level blocks),
  `create-database` / `update-data-source` (common column types; not
  RELATION/ROLLUP/FORMULA), `move-pages` (pages only), `move-blocks` (no native
  REST move — copies each subtree to the new spot then deletes the original, so
  block ids change).
- **Returns 501 (not in the public REST API):** `get-teams`, `create-view`,
  `update-view`, and `update-page` commands `apply_template` / `update_verification`.
  Use `query-database` for filtered reads instead of views.

### Notion writes: text-tools vs block-id-tools

There are two write surfaces, by design, and the `notion-master` skill routes
between them:

- **Text-tools — in-block PROSE edits.** `update-page` `update_content`
  (`content_updates=[{old_str, new_str}]`), `insert_content`, `replace_content`.
  Use these to fix a typo, reword a line, or append to a block. `update_content`
  is **fail-safe** (mirrors claude.ai's connector, then exceeds it):
  - **Multi-match guard** — if `old_str` matches more than once it fails (409)
    with the match count and a snippet of each, unless `replace_all_matches=true`.
    It never silently picks one or deletes all.
  - **Block-boundary aware** — a single `old_str` must resolve within one block; if
    it would span/delete across blocks it fails (400) naming them, unless
    `allow_cross_block=true` (delete only). A partial-block match is spliced in
    place (surrounding text + inline bold/links/`checked`/color preserved).
  - **Child-page guard** — any edit (and `replace_content`) that would delete a
    child page/database, including nested, fails (400) listing them unless
    `allow_deleting_content=true`.
- **Block-id-tools — ALL STRUCTURE.** `list-blocks` (ids + type + depth + parent),
  `append-blocks` (typed Notion blocks, native nesting), `update-block`,
  `delete-blocks` (deterministic — only the listed ids), `move-blocks`. Use these
  to nest loose blocks into a toggle, reorder, or delete specific/duplicate blocks
  by id — never delete structure by text match.

The Notion-flavored markdown dialect (toggles, nesting, tables, …) is documented
once and served two ways: the MCP resource `alistair://docs/notion-markdown-spec`
and the equivalent `notion_markdown_spec` tool (so clients that don't load
resources still get it). Skills are likewise served as `get_skill` **and** the
`alistair://skills/{slug}` resource template.

## Environment variables

All secrets come from env vars only (see `.env.example`). Nothing is hardcoded.

| Var | Used by | Notes |
|-----|---------|-------|
| `NOTION_TOKEN` | Notion | Internal integration secret (`ntn_…`). Share the Projects + Actions DBs with it. |
| `PROJECTS_DB_ID`, `ACTIONS_DB_ID` | Notion | Default to the PARA DB ids; override if they change. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Calendar | **Recommended durable path** — if all three are set, the service mints a fresh access token on every call (never goes stale). |
| `GOOGLE_CALENDAR_TOKEN` | Calendar | Optional fallback Bearer token; **not needed** if the trio above is set. |
| `GOOGLE_CALENDAR_ID`, `TIMEZONE`, `TIMEZONE_AUTO` | Calendar | `primary`; `TIMEZONE` is the home/fallback zone (`Europe/London`; `CALENDAR_TIMEZONE` alias). With `TIMEZONE_AUTO=true` (default) the service auto-detects your **current** Google Calendar timezone each call so it follows you when travelling (also surfaced in `load_context.now`); a per-call `timeZone` arg always overrides. |
| Google scopes | Calendar + Gmail | Calendar read **+ write** needs `…/auth/calendar`; Gmail read + draft needs `gmail.readonly` + `gmail.compose`. Mint a token covering both with `scripts/get_google_token.py`, then set `GOOGLE_REFRESH_TOKEN`. |
| `MS_CLIENT_ID`, `MS_TODO_LIST_ID`, `MS_TENANT` | In-tray | Azure public-client id; the in-tray list id; `consumers` for personal MS accounts. |
| `GITHUB_GIST_TOKEN`, `GIST_ID`, `GIST_FILENAME` | In-tray + GitHub | Classic PAT (`gist` scope); private gist storing the MS refresh token. Also the fallback for the read/merge tools if `GITHUB_REPO_TOKEN` is unset. |
| `GITHUB_REPO_TOKEN` | GitHub | Repo read + PR token, **distinct** from the gist token. Powers `whoami`/`list-my-repos` (account-aware: reports the account it belongs to and enumerates the repos it can reach, public + private) and the read/`merge-pr` tools. Scope is the token's — a fine-grained PAT only sees what you grant it; a classic `repo`-scope PAT sees everything the account can. Falls back to `GITHUB_GIST_TOKEN`. |
| `SERVICE_API_KEY` | All `/api/*` | Optional. If set, every call must send `X-API-Key`. |
| `RAILWAY_ENV` | Service | `production` on Railway. |

## Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in real values
uvicorn app.main:app --reload   # http://127.0.0.1:8000/docs
```

## Deploy to Railway

1. Push this repo to GitHub and create a Railway service from it.
2. Set the env vars above in the service **Variables** tab (set `RAILWAY_ENV=production`
   and a strong `SERVICE_API_KEY`).
3. Railway uses `railway.toml` (Nixpacks → `uvicorn app.main:app --host 0.0.0.0 --port $PORT`).
   A `Procfile` is included as a fallback. Health check: `/health`.

## Voice-mode setup (the triggering prompt)

Voice mode can't see this API on its own — it needs one small instruction
telling it the API exists and how to discover the rest. Put a **compact pointer**
in your **Claude profile → Personal preferences / custom instructions** (those
are applied in voice mode; that is the deterministic home — more reliable than
memory). Keep it small and let the model fetch `/api/manifest` and the skill
endpoints for detail, so you don't bloat every conversation.

```
In voice mode I have no Notion/Google Calendar connectors and no skills. When a
request touches my Notion (projects, actions, next actions, someday, daily brief,
references/in-tray), my Google Calendar, or my Microsoft To Do in-tray, DON'T say
you can't — call my HTTP API instead.

Base: https://<your-app>.up.railway.app   Header on every call: X-API-Key: <key>

Discover: GET /api/manifest lists every tool with its exact description.
Skill rules: GET /api/skill/{notion-master|daily-brief|notion-references-tray|microsoft-todo-intray}.

Shortcuts (use directly, no need to fetch the manifest first):
- Daily brief -> POST /api/notion/query ; POST /api/calendar/today ;
  POST /api/intray {"action":"list"} ; then deliver per the daily-brief skill.
- "add X to in-tray" -> POST /api/intray {"action":"add","title":"X"} ;
  "what's in my in-tray" -> {"action":"list"}.
- Notion writes -> first GET /api/skill/notion-master for the safe-write rules,
  then /api/notion/fetch and /api/notion/update-page.
Only fetch the manifest or a skill when you need detail beyond these shortcuts.
```

Why this shape: the **manifest + skill endpoints are the "few APIs that tell it
how to find the others"**, so the always-on prompt stays tiny. The inline
shortcuts cover the highest-frequency intents with zero discovery round-trips
(important for voice latency); everything else is one `GET /api/manifest` away.

### Even smaller (self-bootstrapping) variant

The API is self-describing, so you can drop the inline shortcuts entirely. The
`GET /api/manifest` response carries `how_to_use` + a `shortcuts` block (intent →
call sequence) and the skill catalogue, and `GET /api/skill` lists each skill
with its description. That lets the always-on prompt shrink to:

```
In voice mode I have no connectors/skills. For anything about my Notion, Google
Calendar, or Microsoft To Do in-tray, call my HTTP API instead of refusing:
GET https://<your-app>.up.railway.app/api/manifest  (header X-API-Key: <key>)
and follow its "shortcuts" and tool descriptions; fetch /api/skill/<slug> for a
skill's rules before performing it.
```

Trade-off: this costs one `GET /api/manifest` round-trip on first use each
session (slower first reply in voice). The longer prompt above avoids that for
the common intents — pick based on whether you care more about prompt size or
voice latency.

## Extending (future-proofing)

- **More GitHub:** `app/services/github.py` (`GitHubClient`) already powers the
  in-tray gist + `push-file`. Add a route in `app/routers/github.py` calling a new
  client method — e.g. `POST /api/github/push-file` is already wired this way.
- **More skills:** drop a `app/skills/data/<slug>.json` file; it is served at
  `/api/skill/<slug>` automatically (no code change).
- **More connector tools:** add an `op_*` in the service + a route with the
  verbatim description.

## Security

`.env` is gitignored; only `.env.example` (placeholders) is committed. The
uploaded skill `config.json` files contained **live secrets** (Notion `ntn_`
token, GitHub `ghp_` PAT, gist id) — those are **not** in this repo, but since
they were shared in plaintext you should **rotate the Notion token and the GitHub
PAT** and keep them only in Railway's Variables.
