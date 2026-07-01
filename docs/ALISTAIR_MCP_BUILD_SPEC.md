# Alistair MCP — build spec (for the MCP-server phase)

> Provenance: drafted by the Claude Code session building the Pipecat "Alistair"
> voice super-assistant, which needs to consume this MCP. Captured 2026-06-24.
> This is the **reference for phase #1 (MCP wrap)** in `docs/ROADMAP.md`.

Goal: make **Alistair a single backend** that every LLM surface (claude.ai, the
Pipecat voice agent, Gemini) invokes. Alistair owns the identity, the memory,
and the tools. The LLM is just the reasoning engine; "Alistair" is the persona +
capabilities you opt into.

---

## 0. The one-line architecture

**One remote, public-HTTPS, OAuth-capable, Streamable-HTTP MCP server** = the
single source of truth for Alistair's persona + memory + tools. Every frontend
is a thin shell that connects to it. Native LLM memories (Claude/Gemini/ChatGPT)
are **disposable caches, never authoritative** — turn them off for Alistair so
there is exactly one memory.

## 1. Transport & protocol (non-negotiable constraints)

- **Remote Streamable-HTTP** (NOT stdio, NOT SSE). stdio only works in Claude Desktop; SSE is dropped by Gemini.
- **Public internet HTTPS** — claude.ai connects from Anthropic's *cloud* IPs, so localhost will not work for web/mobile. (Railway already gives you this.)
- **OAuth** (client id/secret), not bearer-token-only — claude.ai custom connectors require it.
- **Name it `snake_case`** (e.g. `alistair_assistant`) — Gemini rejects `-` in server names.
- This single shape works for: claude.ai custom connector, Claude Desktop/Code, Cursor, **Gemini CLI**, **Gemini Enterprise**. ❌ The *consumer* Gemini app (gemini.google.com / "Spark") is partnership-gated — no self-serve MCP today; don't count on it.

## 2. Expose EVERYTHING as TOOLS (the most important finding)

**No major MCP client auto-loads `resources` or `prompts` at session start — only TOOL names/descriptions enter the model's context.** Gemini's API ignores prompts/resources entirely. So:

- Persona, memory, and skills must be delivered through **tools**, not MCP resources/prompts.
- Put a **strong one-line Alistair hook in each tool's `description`** (descriptions are the one thing every frontend reliably sends to the model).
- Resources/prompts are fine as a *progressive-enhancement* layer for Claude Desktop/Cursor only — never depend on them.

**Tool surface (keep it lean — tool names cost context):**

| Tool | Purpose |
| --- | --- |
| `load_context()` | returns Alistair persona + the active skill rules (PARA, safe-write) — frontend calls this FIRST every session |
| `get_memory(query?)` | read durable memory (facts/preferences/context), optionally filtered |
| `save_memory(content, type, tags?)` | append a durable memory (the ONLY write path) |
| `notion_search / notion_fetch / notion_query / notion_update_page / notion_create_pages …` | Notion (call the Notion API directly — the old pre-configured connector wraps a **deprecated** package). **Paginate fetches past 300 blocks.** |
| `calendar_* , intray_* , github_*` | the existing Railway functions |

## 3. Memory = single source of truth, single writer

- **Canonical store: SQLite on Railway, append-only EVENT LOG.** Not mutable rows.
  `memory_events(id, ts, source['voice'|'claude'|'gemini'], op['assert'|'retract'|'merge'], type, content, relevance, tags, dedup_key)`.
  Read = fold the log to current state (latest assert per `dedup_key`, minus retracts).
- **The MCP process is the ONLY writer.** Serialize writes: per-store async lock + SQLite **WAL** mode + `busy_timeout ~5s` + jittered retry. Never let a frontend write its local cache back as truth.
- **Do NOT use plain md/JSON files or a Notion page as the canonical store** — both corrupt / race / rate-limit under concurrent writes (Notion ~3 req/s, 409 conflicts). md-in-Railway is only safe *behind* the single-writer lock; SQLite is safer.
- **Notion = optional human-readable MIRROR** (one-way, periodic, from the SQLite truth) so *you* can eyeball/edit memory. Edits flow back as a `save_memory` reconcile, not direct truth.
- **Avoid silent last-write-wins** (it overwrites shared truth with no evidence and breaks on clock skew). Append + dedup_key + retract is the safe pattern.
- **Retrieval:** rank entries by recency + importance, LLM-summarise the top ~8, return that on the `get_memory` tool. (How Pipecat does local memory; consistent with Anthropic-style memory.)
- **5-min frontend cache is acceptable**: a write by one frontend is visible to others within ≤5 min (fine for human-paced PA memory).

## 3b. Where each kind of knowledge lives (don't put it all in "memory")

| Kind | Example | Home | Loaded |
| --- | --- | --- | --- |
| **Persona + routing/aliases + ID registry** (stable "constitution") | "Ali" tone; "references" → Unorganised References the references-tray page id; "brief me" → daily-brief; Briefing/Projects/Actions IDs | **`load_context()`** tool | every session start |
| **Skill *procedures*** | daily-brief steps; notion-master read decision tree | **`get_skill(slug)`** tool (on demand; the skill *index* sits in `load_context`) | on demand |
| **Safety-critical rules** | safe-write: never replace whole page | **also duplicated into the tool's `description`** (`notion_update_page`) | always (in tool desc) |
| **Accumulating facts / user-added aliases** | the operator's background; learned prefs; "from now on X means Y" | **memory store** (`get_memory`/`save_memory`) | session start + as needed |

Rule of thumb: **stable config → `load_context`**, **procedures → `get_skill`**,
**dangerous rules → redundant in tool descriptions**, **changing/accumulating
knowledge → memory store**. Routing aliases like "references = Unorganised
References" are stable config (`load_context`), NOT dynamic memory — unless the
user adds new aliases by voice, which append to the memory store (also loaded at
start, so the model sees both). The current `SKILL.md` files migrate:
persona/routing/IDs → `load_context`; procedures → `get_skill`; the rest stays as
tool descriptions.

## 4. claude.ai wiring (replace the connectors + skills with ONE Alistair)

1. **Add the Alistair MCP as a custom connector** (Settings → Customize → Connectors → Add custom connector: HTTPS URL + OAuth). **Do NOT enable the official Notion/Todoist connectors** — all Notion flows through Alistair. (You can't "replace" the official connector in place; you just never enable it.)
2. **Upload ONE "Alistair" Skill** (Settings → Customize → Skills). Its YAML `description` is what triggers it. For strict opt-in ("only when I say Alistair"), set **`disable-model-invocation: true`** → it becomes user-invoked, not relevance-auto-loaded. The skill body = the Alistair persona + instructions to: (a) call `load_context()` + `get_memory()` at start, (b) use Alistair's Notion/calendar/intray tools (never a built-in connector), (c) call `save_memory()` when it learns something durable. **Collapse notion-master / daily-brief / intray / references-tray into this one skill** (or let the skill point at MCP-served rule tools).
3. **Pause claude.ai native memory** (Settings → Capabilities → Pause memory) so the MCP is the only memory. ⚠️ Claude does NOT auto-write to MCP memory — the skill MUST explicitly tell it when to `save_memory`/`get_memory`, or nothing persists.

## 5. Opt-in model

- Say **"Alistair"/"Ali"** → the skill loads (persona) + the MCP supplies tools+memory → full assistant.
- Don't say it → base Claude/Gemini, no Alistair context. (Custom Instructions/Projects are always-on, so they can't give clean opt-in — the **Skill with `disable-model-invocation`** is the right mechanism.)

## 6. Per-frontend connect summary

| Frontend | How it connects | Notes |
| --- | --- | --- |
| **claude.ai** | custom connector (HTTPS+OAuth) + Alistair skill | official Notion connector OFF; native memory paused |
| **Pipecat voice** (separate repo) | `MCPClient(StreamableHttpParameters(url, auth))`, preload-once + 5-min refresh | thin shell |
| **Gemini CLI / Enterprise** | register the Streamable-HTTP MCP | tools only; not the consumer app |
| **Claude Desktop / Cursor** | add the remote MCP | can also use prompts/resources if you add them |

## 7. Gotchas to bake in

- Public HTTPS + OAuth (not localhost, not bearer-only) for claude.ai · `snake_case` server name for Gemini · call Notion API directly (old connector = deprecated pkg) · **paginate Notion fetches >300 blocks** · custom connectors are **beta / "not verified by Anthropic"** · Free claude.ai = 1 custom connector (fine for one Alistair) · Gemini remote MCP needs Streamable HTTP and (as of mid-2026) doesn't work with Gemini-3 models yet.

---

### TL;DR for the builder

One **remote Streamable-HTTP MCP** on Railway (public HTTPS + OAuth, snake_case).
Everything is a **tool** (persona/memory/skills + domain), each with a
persona-loaded description. Memory = **append-only SQLite event log,
single-writer** (Notion only as a human mirror). On claude.ai: **one opt-in
Alistair skill + the custom connector, official connectors off, native memory
paused.** Frontends are thin shells that `load_context()` + `get_memory()` on
start and `save_memory()` as they go.
