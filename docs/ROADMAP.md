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

## #3 — Notion fidelity (read + write parity with the connector)

| Item | Status | How we proceed |
| --- | --- | --- |
| Read renderer: headings (incl. h4), toggle, callout, columns, `<empty-block/>`, image, lists, quote, code, divider, inline bold/italic/code/link/math | ✅ done | committed `2b0c76b`. ≈85% structural parity. |
| Read renderer → **100%**: **tables** `<table>`, **databases / data-sources**, **mentions** (`<mention-page>`, `<mention-user>`, date), synced / `<unknown>` blocks, exact special-char escaping (`\$ \| \< \> \[ \] \* \\`) | 🔨 active | You flagged tables + databases + mentions as must-be-100%. This is the next change. |
| Write parser `markdown_to_blocks`: `#### → heading_4` (confirmed creatable), `<empty-block/>`, `<details>/<summary>` toggles + children, `<callout>` + children, `![](url)`, inline math, tables, mentions; tab-indent → child nesting | 🔨 active | The symmetric write side. Today it has the `#### → heading_3` bug and drops rich blocks. |
| **Pagination** of fetch (cursor/offset + `has_more`) so long & deep pages come through **in full** — never truncated, never an oversized single response | 🔨 active | You confirmed: "must add pagination such that all is fetched properly." This — not a bigger block cap — is the real fix for long pages. |
| Caps: depth 4 → 6; the per-response block cap survives only as a chunk-size safety | 🔨 active | The connector's own fetch of your tray overflowed at **85K chars** — proof a single dump has a hard ceiling, so pagination beats a bigger number. Folded into the pagination work. |
| Acceptance: connector-vs-Railway-API **diff** on real pages (tray, coffee, references, one deep/long page) | 📋 queued | After the above deploys. |

**Parity verdict today (honest):** read ≈85% (→100% with tables/mentions/db/escaping);
**write not yet** (parser rewrite is active). Full read+write parity is **not reached yet**.

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
| Railway **volume** + SQLite scaffolding | 📋 queued | Prereq for memory (and optional token consolidation). |
| `save_memory` (only write path) / `get_memory` / summarise | 📋 queued | |
| Coarse tools: `load_context`, `get_skill`, `daily_brief`, `add_to_intray`, `save_reference`, `add_action`, `project_context` | 📋 queued | persona/routing/IDs → `load_context`; procedures → `get_skill`; dangerous rules duplicated into tool descriptions. |

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
