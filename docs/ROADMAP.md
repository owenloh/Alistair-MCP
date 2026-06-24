# Alistair — Roadmap & Decisions

A living triage of everything raised while turning the **Alistair Skills API**
into the single **Alistair backend**. Last updated 2026-06-24.

**Agreed build order:**
**#3 Notion fidelity** (read→100% + write parity + pagination)
→ **#2 memory + coarse tools**
→ **GitHub read/merge**
→ **#1 MCP wrap**
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
| **Memory** — SQLite event-log on a volume, rank→summarise | ✅ **built & tested** · ⚠️ attach a volume to persist | `app/services/memory.py` + `/api/memory/{save,get,list}`; **44 golden checks**; formula → `docs/MEMORY_FORMULA.md` |
| **Coarse Alistair tools** — load_context, get_skill, daily_brief … | 🔨 active (#2) | next, composed on top of memory |
| **GitHub** read + merge_pr + project_context | 📋 queued | needs `GITHUB_REPO_TOKEN` ⚠️ |
| **MCP wrap** — Streamable-HTTP + OAuth, everything-as-tools | 📋 queued (#1) | spec saved |
| **claude.ai rollout** config | 📋 queued ⚠️ | steps documented |

**Notion-fidelity milestone (#3): ✅ DONE** — read shipped (34/35 live), write shipped &
hardened (124 checks, 16 review bugs fixed, **live round-trip 38/39**).
**Milestone #2 in progress:** memory layer ✅ built/tested/deployed (44 checks) — **⚠️ attach a
Railway volume to make it persist across redeploys** (steps in §#2 below); coarse tools next.

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
| SQLite append-only event log + WAL/single-writer + the exact scoring/selection formula | ✅ **built, tested & deployed** | `app/services/memory.py`; **44 golden checks** (fold, earliest-`created_at`, decay, core-pin, top_n, token-trim, dedup, retract, validation, reconnect-persistence). |
| `save_memory` (only write path, assert/retract, dedup) / `get_memory` (ranked block) / `list_memory` (raw mirror) | ✅ **built & deployed** | `POST /api/memory/{save,get,list}`, persona-voiced descriptions for the MCP. Live-verified. |
| Railway **volume** so the DB survives redeploys | ⚠️ **your action** | Code auto-uses `RAILWAY_VOLUME_MOUNT_PATH` when present (and reports `memory_persistent` at `/`). **Steps:** Railway → service → **Variables/Volumes → New Volume**, mount at e.g. `/data`; the app picks it up on the next deploy. Until then memory works but is **ephemeral** (lost on redeploy). |
| Coarse tools: `load_context`, `get_skill`, `daily_brief`, `add_to_intray`, `save_reference`, `add_action`, `project_context` | 🔨 **active** | persona/routing/IDs + memory block → `load_context`; procedures → `get_skill`; dangerous rules duplicated into tool descriptions. `project_context` waits on GitHub (#7). |

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
| Read endpoints: get-file, list-tree, search-code, recent-commits, list-prs / issues | 📋 queued | `GitHubClient` + the `push-file` route already exist as the base. |
| `merge_pr` with **explicit in-turn confirm** (never merge-by-voice silently) | 📋 queued | Sensitive / near-irreversible. |
| `project_context` coarse tool (fish project details that Notion pages link to) | 📋 queued | The reason you wanted GitHub. |
| `GITHUB_REPO_TOKEN` — separate **fine-grained PAT** (repo read + PR), so the gist token stays minimal | ⚠️ your action | Add in **Railway Variables**; don't paste in chat. |

## #1 — MCP wrap + rollout

Reference: **`docs/ALISTAIR_MCP_BUILD_SPEC.md`**. Key constraints:
- One **remote Streamable-HTTP** MCP on Railway: **public HTTPS + OAuth**, **`snake_case`** name (`alistair_assistant`).
- **Everything is a TOOL** — no client auto-loads resources/prompts; persona/memory/skills ship as tools, each with a persona-loaded `description`; `load_context()` is called first every session.
- Memory = the single-writer SQLite event log above. Talk to Notion directly (the old connector wraps a deprecated package).

| Item | Status |
| --- | --- |
| FastAPI → MCP (FastMCP / fastapi_mcp), Streamable HTTP, OAuth | 📋 queued |
| Tools wired (domain + persona + memory) with persona descriptions | 📋 queued |
| Hand you the `/mcp` URL + auth | 📋 queued |

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
