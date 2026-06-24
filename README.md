# Alistair Skills API

An HTTP workaround that mirrors Claude's **Notion** and **Google Calendar**
connectors, the **Microsoft To Do in-tray**, and the four **PARA skills** — so a
**voice-mode Claude** (which can reach the internet over HTTP but cannot use
connectors or skills) can reproduce the *same behaviour* it has on desktop.

The trick: each connector tool is re-exposed as an HTTP endpoint whose
description is copied (near-)verbatim from the real connector, backed by the
official REST API. Same model + same tool descriptions + HTTP access ⇒ same
behaviour, without the connectors.

## Three layers

| Layer | What it is | Endpoints |
|-------|-----------|-----------|
| **Function APIs** | Connector tools that *do* things | `/api/notion/*` (16), `/api/calendar/*` (9), `/api/intray` (1), `/api/github/*` (1) |
| **Description APIs** | Skills that tell Claude *what to do* (no code) | `/api/skill/{notion-master \| daily-brief \| notion-references-tray \| microsoft-todo-intray}` |
| **Manifest** | The catalogue of everything | `GET /api/manifest`, plus `/docs` and `/openapi.json` |

So it is **~30 endpoints, not 7** — three "connectors" that each contain many
tool-APIs, plus the four skill description-APIs, plus discovery.

### Notion function APIs (`/api/notion/*`)
`search`, `fetch`, `create-pages`, `update-page`, `move-pages`, `duplicate-page`,
`create-database`, `update-data-source`, `create-comment`, `get-comments`,
`get-users`, `get-teams`, `create-view`, `update-view`, `query-database`,
`query` (the PARA daily-brief read: `ACTIVE_PROJECTS`, `NEXT_ACTIONS`,
`SOMEDAY_PROJECTS`, `SOMEDAY_ACTIONS`).

### Google Calendar function APIs (`/api/calendar/*`)
`today`, `list-events`, `list-calendars`, `get-event`, `create-event`,
`update-event`, `delete-event`, `respond-to-event`, `suggest-time`.

### In-tray function API (`/api/intray`)
`POST /api/intray` with `{"action": "list"|"add"|"delete"|"done", "title"?, "task_id"?}`
(Microsoft To Do, single hard-scoped list, self-rolling token via a private gist).

## Fidelity (honest notes)

Backends are the official REST APIs, so behaviour matches the connectors closely
but not always byte-for-byte:

- **Exact / clean:** all Calendar tools, the in-tray, Notion `search`, `fetch`,
  `query-database`, `query`, `get-users`, `create-pages`, `update-page`
  (`update_properties` / `insert_content` / `update_content` / `replace_content`),
  `create-comment`, `get-comments`.
- **Best-effort:** `duplicate-page` (shallow copy of top-level blocks),
  `create-database` / `update-data-source` (common column types; not
  RELATION/ROLLUP/FORMULA), `move-pages` (pages only).
- **Returns 501 (not in the public REST API):** `get-teams`, `create-view`,
  `update-view`, and `update-page` commands `apply_template` / `update_verification`.
  Use `query-database` for filtered reads instead of views.

The Notion write path honours the skills' **safe-write protocol** at the
block level: `update_content` matches `old_str` against block text, an
append-only `old_str→new_str` becomes an append after the matched block, and an
empty `new_str` deletes the matched block(s). `replace_content` refuses to drop
child pages unless `allow_deleting_content` is set.

## Environment variables

All secrets come from env vars only (see `.env.example`). Nothing is hardcoded.

| Var | Used by | Notes |
|-----|---------|-------|
| `NOTION_TOKEN` | Notion | Internal integration secret (`ntn_…`). Share the Projects + Actions DBs with it. |
| `PROJECTS_DB_ID`, `ACTIONS_DB_ID` | Notion | Default to the PARA DB ids; override if they change. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Calendar | **Recommended durable path** — if all three are set, the service mints a fresh access token on every call (never goes stale). |
| `GOOGLE_CALENDAR_TOKEN` | Calendar | Optional fallback Bearer token; **not needed** if the trio above is set. |
| `GOOGLE_CALENDAR_ID`, `TIMEZONE`, `TIMEZONE_AUTO` | Calendar | `primary`; `TIMEZONE` is the home/fallback zone (`Europe/London`; `CALENDAR_TIMEZONE` alias). With `TIMEZONE_AUTO=true` (default) the service auto-detects your **current** Google Calendar timezone each call so it follows you when travelling; a per-call `timeZone` arg always overrides. |
| `MS_CLIENT_ID`, `MS_TODO_LIST_ID`, `MS_TENANT` | In-tray | Azure public-client id; the in-tray list id; `consumers` for personal MS accounts. |
| `GITHUB_GIST_TOKEN`, `GIST_ID`, `GIST_FILENAME` | In-tray + GitHub | Classic PAT (`gist` scope); private gist storing the MS refresh token. |
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
