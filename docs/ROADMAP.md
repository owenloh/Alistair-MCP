# Alistair — Roadmap & Decisions

A living triage of everything raised while turning the **Alistair Skills API**
into the single **Alistair backend**. Last updated 2026-06-24.

**Agreed build order:**
**#3 Notion fidelity** (read→100% + write parity + pagination) ✅
→ **#2 memory + coarse tools** ✅
→ **GitHub read/merge** ✅ built on dev
→ **#1 MCP wrap** 🔨 transport + 22 tools built on dev; **OAuth** is the one piece left
→ **claude.ai rollout** (your config).

Legend: ✅ done · 🔨 active · 📋 queued · 🚫 won't / can't · ⚠️ your action

---

## Progress snapshot — toward a solid Alistair MCP

The MCP is the last layer; everything below is the substrate it will expose as tools.

| Layer (what the MCP needs) | State | Where it lives |
| --- | --- | --- |
| **Notion read** — connector-exact markdown (tables, mentions, colors, files, synced) + pagination | ✅ **done & deployed** | live on Railway; 34/35 live diff, 18 golden |
| **Notion write** — markdown→blocks parity (containers, colors, spans, media, mentions) | ✅ **done, deployed & live-verified** | **124 golden checks**; 27-agent review fixed **16 bugs**; **live create→fetch round-trip = 38/39 byte-identical** (Notion accepted every block; 1 diff is Notion's own URL normalization) |
| **Calendar / MS To-Do** domain ops | ✅ already in service | `app/services/*` |
| **Token persistence** (Google / MS refresh) | ✅ resolved | Google=env, MS=gist |
| **Memory** — SQLite event-log on a volume, rank→summarise | ✅ **deployed & live-verified** · ⚠️ attach a volume to persist | `app/services/memory.py` + `/api/memory/{save,get,list}`; **44 golden checks**; live save→get→retract round-trip clean. Formula → `docs/MEMORY_FORMULA.md` |
| **Coarse Alistair tools** — load_context, daily_brief, save_reference, add_action … | ✅ **4 deployed & live-verified** (52 checks) | `POST /api/alistair/{load-context,daily-brief,save-reference,add-action}`. Live: load_context returns the constitution; daily_brief composed 7 projects / 14 next actions / 1 cal event / 1 in-tray; **save_reference dry_run anchored correctly on the real tray** (no write). `get_skill`/`add_to_intray` already exist; `project_context` waits on GitHub #7 |
| **GitHub** read + merge_pr + project_context | ✅ **built & tested on dev** (49 checks) | 8 read/merge routes + `project_context`; `merge_pr` is preview-unless-`confirm=true`. Needs `GITHUB_REPO_TOKEN` ⚠️ to run live |
| **MCP wrap** — Streamable-HTTP + OAuth, everything-as-tools | 🔨 **transport + 22 tools built & tested on dev** (45 checks) · OAuth ⚠️ left | `app/mcp_server.py`; `alistair_assistant` mounted at `/mcp`, bearer/X-API-Key auth, persona-loaded tool descriptions. OAuth (claude.ai) is the remaining step |
| **claude.ai rollout** config | 📋 queued ⚠️ | steps documented |

**Notion-fidelity milestone (#3): ✅ DONE** — read shipped (34/35 live), write shipped &
hardened (124 checks, 16 review bugs fixed, **live round-trip 38/39**).
**Milestone #2 — ✅ DONE, deployed & live-verified:** memory layer (44 checks) + 4 coarse tools —
`load_context`, `daily_brief`, `save_reference`, `add_action` (52 checks) — **220 checks total**,
shipped to prod via **PR #3** (merged through the GitHub API since the local relay blocks `main`
pushes) and verified live. Remaining: **⚠️ attach a Railway volume** so memory persists
(`memory_persistent` is currently false); one live `save_reference`/`add_action` **write** to
confirm against the real workspace (held for Owen's ok — the dry_run already proved the anchor);
`project_context` follows GitHub (#7).

---

## #3 — Notion fidelity (read + write parity with the connector)

| Item | Status | How we proceed |
| --- | --- | --- |
| Read renderer: headings (incl. h4), toggle, callout, columns, `<empty-block/>`, image, lists, quote, code, divider, inline bold/italic/code/link/math | ✅ done | committed `2b0c76b`. |
| Read renderer → **100%**: **tables** (multi-line `<table>/<tr>/<td>`), **databases** (`<database inline>`/`<mention-database>`), **mentions** (page/database/user + `<mention-date>`), files (`<video/audio/file/pdf>`), `<table_of_contents/>`, `<synced_block>`, `<unknown/>`, exact escape set `\ * ~ \` $ [ ] < > { } \| ^` | ✅ done | Aligned to the authoritative `notion://docs/enhanced-markdown-spec`. Live diff on the Library hub = **34/35 lines byte-identical**; 18 golden checks pass. Deployed. |
| **Colors on read** `{color=..}` (block) + `<span color=>`/`<span underline=>` (inline) | ✅ done | Landed *with* the write parser so they round-trip symmetrically. `_bg`↔`_background` translated. |
| **Remaining read gaps** (documented): bold **child-page title** (`**Lie Theory**` — REST `child_page.title` is a plain string, needs N+1 fetch); tables/dates/files/synced are spec-exact + unit-tested but not yet live-diffed (no workspace page has one) | 🔨 tracked | minor / REST-limited |
| Write parser `markdown_to_blocks` — recursive: `<details>` toggles, `<callout>`, `<columns>/<column>`, multi-line `<table>`, `<synced_block>`/reference, `$$` equation, `<table_of_contents/>`, media, image captions, `<page>`→`link_to_page`; tab-indent→child nesting; block colors + inline span color/underline; `<br>`; richer mentions (date+tz, user) | ✅ built · 🔨 verifying | Committed to dev `cce5db5`. 57 golden checks pass incl. an idempotent md→blocks→md round-trip. Fixed `#### → heading_4` and a `<mention-date>` timezone-`/` regex bug. Adversarial review running (REST-acceptance + edge cases) before it merges to main. |
| **Pagination** of fetch (cursor + `has_more`) so long & deep pages come through **in full** | ✅ done | `op_fetch` returns `has_more`/`next_cursor`; caller pages until null. Deployed. |
| Caps: depth 4 → 6; per-response block cap is now just a chunk-size safety | ✅ done | Pagination, not a bigger cap, is the real fix. Deployed. |
| Acceptance diff: read hub 34/35 ✅; write md→blocks→md golden ✅; **live create→fetch diff** of a written page ✅ (38/39) | ✅ done | A throwaway page with table/callout/columns/colors/bold-italic/equation created via the API round-tripped 38/39; Notion accepted all blocks. |

**Parity verdict (honest):** **read = done & deployed** (every connector block/inline *type* to
the authoritative spec, incl. colors; live page diffed 34/35, the 1 diff is a REST limitation).
**Write = done, deployed & live-verified** — recursive containers, colors, spans, media, mentions
parse and round-trip; 124 golden checks; a 27-agent adversarial review found & fixed 16 bugs
(4 Notion-400 risks + round-trip corruption); a **live create→fetch on a real page round-tripped
38/39 byte-identical**, Notion accepting every block (the 1 diff = Notion's own URL canonicalization).
Remaining known gaps: child-page-title bold (REST limit); tables/dates/files/synced not yet
*connector*-diffed (no workspace sample); the documented empty-callout+leading-paragraph edge.

## #2 — Memory + coarse tools

**Memory model — DECIDED (matches the Pipecat build spec):**

- **Canonical store: SQLite on a Railway _volume_.** The container filesystem is
  ephemeral (dies on redeploy); a plain file would be lost — the **volume persists**.
- **Append-only event log** — `memory_events(id, ts, source, op, type, content, relevance, tags, dedup_key)`.
  Read = fold the log (latest `assert` per `dedup_key`, minus `retract`). No mutable
  rows, no silent last-write-wins.
- **Single writer = the MCP process** (async lock + SQLite WAL + `busy_timeout` + retry).
- **Retrieval = rank by recency + importance → LLM-summarise the top ~8 → return via `get_memory`.**
  (Exactly how Pipecat does local memory; consistent with Anthropic-style memory.)
- **Notion = one-way human-readable MIRROR only** (so you can eyeball/edit). Your edits
  flow back as a `save_memory` reconcile — never as direct truth.

| Item | Status | Notes |
| --- | --- | --- |
| SQLite append-only event log + WAL/single-writer + the exact scoring/selection formula | ✅ **deployed & live-verified** | `app/services/memory.py`; **44 golden checks**; live save→get→retract round-trip clean on prod. |
| `save_memory` (only write path, assert/retract, dedup) / `get_memory` (ranked block) / `list_memory` (raw mirror) | ✅ **deployed & live-verified** | `POST /api/memory/{save,get,list}`, persona-voiced descriptions for the MCP. |
| `load_context` (persona + routing + ID registry + skill index + live memory) / `daily_brief` (compose the 3 read sources, graceful degrade) | ✅ **deployed & live-verified** | `POST /api/alistair/{load-context,daily-brief}`; **28 checks**. Live: load_context returns the constitution; daily_brief composed 7 projects / 14 next actions / 1 cal event / 1 in-tray. |
| `get_skill` / `add_to_intray` coarse tools | ✅ already exist | `GET /api/skill/{slug}` and `POST /api/intray` cover these. |
| `save_reference` (References Tray append) / `add_action` (Actions row) | ✅ **deployed** · live write held | `POST /api/alistair/{save-reference,add-action}`; **24 checks**. Both **insert/create-only, never replace_content**. save_reference: read-first → find last entry above the END-OF-TRAY boundary → insert one spacer + entry → re-fetch + verify; **aborts** if structure missing; `dry_run`. **Live dry_run anchored correctly on the real tray** (`reddit.com/r/ClaudeAI thread`); the actual write is held for Owen's ok. |
| `project_context` coarse tool | 📋 queued | Waits on the GitHub read layer (#7). |
| **Deploy `main`** (Railway redeploys from `main`) | ✅ **deployed via PR #3** | Direct `git push origin main` is blocked here (HTTP 503 / sideband-disconnect — the local relay, not GitHub: `main` shows `protected:false`). Worked around by opening + merging **PR #3** through the GitHub API (Owen-authorized). Prod redeployed in ~60s; new build live (`memory_persistent` flag present). |
| Railway **volume** so the DB survives redeploys | ⚠️ **your action** | Confirmed live: `/` reports `memory_persistent: false` (no volume yet). Code auto-uses `RAILWAY_VOLUME_MOUNT_PATH` when present. **Steps:** Railway → service → **Variables/Volumes → New Volume**, mount at e.g. `/data`. Until then memory works but is **ephemeral** (wiped each redeploy). |

## Token storage — RESOLVED

**Your question: the Google refresh token is in `.env`/Railway — if I restart, is it gone?**
**No.** Two reasons:

1. **Railway env vars live in Railway's config, not the ephemeral container fs** — they
   survive restarts *and* redeploys.
2. **The app never rotates the Google refresh token.** `calendar.py::_access_token` mints a
   short-lived **access** token per call from the env refresh token and *ignores* any
   `refresh_token` in the response. The access token is in-memory and regenerated every
   call, so losing it on restart is harmless. There is **no newer refresh token held in
   volatile memory** that a restart could lose.

The Google refresh token only dies if **Google** invalidates it:
- OAuth consent screen in **"Testing"** publishing status → **7-day** expiry → **⚠️ publish your app to "In production."**
- Manual revocation / password change / 6-month inactivity / >50 tokens issued.
Storage location can't fix any of those.

**Contrast — Microsoft To Do _does_ rotate:** Microsoft returns a fresh `refresh_token`
each refresh, and `mstodo.py` writes it back to the **gist**. That's why MS needs persisted
storage and Google, as coded, does not.

**Decision:**
- **Google** → stays in env (correct as-is). Just confirm the OAuth app is **Published**.
- **MS** → stays in the gist for now (works, rotating).
- When we stand up the volume + SQLite, *optionally* move rotating secrets (the MS token)
  into a `secrets` table there to unify storage and drop the GitHub dependency. **Low
  priority, not required.**

## GitHub read + merge (before the MCP wrap)

| Item | Status | Notes |
| --- | --- | --- |
| Read endpoints: get-file, list-tree, search-code, recent-commits, list-prs / issues, get-pr | ✅ **built & tested on dev** | `POST /api/github/*`. On `GitHubClient`; base64 decode, PR-vs-issue split, binary/dir guards. **49 checks.** |
| `merge_pr` with **explicit in-turn confirm** (never merge-by-voice silently) | ✅ **built & tested on dev** | `merge_pr_guarded`: `confirm=false` (default) returns a PREVIEW and changes nothing; only `confirm=true` merges. Tests assert no merge call without confirm. |
| `project_context` coarse tool (fish project details that Notion pages link to) | ✅ **built & tested on dev** | `POST /api/alistair/project-context` — repo meta + commits + open PRs + open issues + README excerpt, graceful-degrade like `daily_brief`. |
| `GITHUB_REPO_TOKEN` — separate **fine-grained PAT** (repo read + PR), so the gist token stays minimal | ⚠️ your action | Add in **Railway Variables** (falls back to `GITHUB_TOKEN` if unset). Don't paste in chat. Until it exists the read/merge routes return a clean 503. |

## #1 — MCP wrap + rollout

Reference: **`docs/ALISTAIR_MCP_BUILD_SPEC.md`**. Key constraints:
- One **remote Streamable-HTTP** MCP on Railway: **public HTTPS + OAuth**, **`snake_case`** name (`alistair_assistant`).
- **Everything is a TOOL** — no client auto-loads resources/prompts; persona/memory/skills ship as tools, each with a persona-loaded `description`; `load_context()` is called first every session.
- Memory = the single-writer SQLite event log above. Talk to Notion directly (the old connector wraps a deprecated package).

| Item | Status |
| --- | --- |
| FastAPI → MCP (official `mcp` SDK FastMCP), **Streamable HTTP**, mounted at `/mcp` | ✅ **built & tested on dev** — `alistair_assistant`, stateless+JSON responses, DNS-rebinding protection off (public server), boots + does the initialize handshake (protocol 2025-06-18) |
| Tools wired (domain + persona + memory) with persona descriptions | ✅ **built & tested on dev** — **22 tools** (load_context, get/save_memory, get_skill, daily_brief, project_context, save_reference, add_action, notion_*, calendar_*, intray, github_*), each Alistair-voiced; safety hooks duplicated into descriptions; all in-process over the existing services. **45 checks.** |
| **OAuth** (claude.ai custom-connector requirement) | ⚠️ **the one piece left.** Interim **bearer/X-API-Key** guard works today for Claude Desktop/Code, Cursor, the Pipecat voice shell and Gemini CLI. claude.ai needs OAuth 2.1 + dynamic client registration; approach is a decision (auto-approve single-user vs proxy to Google). |
| Hand you the `/mcp` URL + auth | ⏳ after deploy: `https://<railway-host>/mcp`, `Authorization: Bearer <SERVICE_API_KEY>` |

**claude.ai rollout — ⚠️ your action (I'll document the exact steps):** add the Alistair MCP
as a custom connector; **do not enable** the official Notion/Todoist connectors; upload **one**
Alistair skill with `disable-model-invocation: true` (opt-in on "Alistair/Ali"); **pause native
memory** so the MCP is the only memory.

## 🚫 Won't / can't (parity gaps to accept)

- `get-teams`, `create-view`, `update-view`, `apply_template`, `update_verification` — **not in
  Notion's public REST API**; they stay **501**. The connector reaches them via Notion's internal
  API; we can't. Use `query-database` for filtered reads instead of views.
- `notion_query` SQL / data-sources mode — the connector's SQL is a **paid Notion-AI feature**;
  REST uses filter objects (and the 2025-09-03 data-sources query). Functional parity, different
  shape. Deferred.
- **Multi-tenancy** ("a different person's Alistair") — v1 is **single-user**. Multi-tenant = key
  memory + tokens by OAuth identity. Future.

## ⚠️ Your actions (security)

- **Rotate the `SERVICE_API_KEY`** you pasted in chat — it's in the transcript.
- **Rotate** any Notion `ntn_` / GitHub `ghp_` tokens shared in plaintext earlier.
- Put **all** secrets in **Railway Variables**, never in chat. Add `GITHUB_REPO_TOKEN` as a
  separate fine-grained PAT.
- Confirm your **Google OAuth app is "Published"**, not "Testing" (see Token storage).
