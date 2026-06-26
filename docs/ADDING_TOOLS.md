# Adding tools / integrations to the Alistair MCP

How to add new functionality (a new API like Google Workspace or Granola, or just a new
tool) so that **every** connected client — claude.ai, voice/Pipecat, Gemini, ChatGPT —
picks it up correctly after a deploy + reconnect, with no per-client prompt/memory/skill
edits.

## The core principle (why this works)

Clients discover tools dynamically (`tools/list`) at connect time, and the model learns a
tool purely from its **description** + the `load_context` **routing**. So the propagation
unit for any new capability is:

> **a self-sufficient tool description  +  a `load_context` ROUTING entry**

Author that once on the server, deploy, reconnect → it's live everywhere. Everything else
(a skill, a claude.ai trigger word) is for nuance only. Memory and persona are untouched by
new tools.

## The three-layer pattern (every tool follows it)

| Layer | File | What goes here |
|---|---|---|
| Service | `app/services/<name>.py` | one `op_*` function per capability; returns a plain dict; raises `ServiceError` on failure. Reuse the `httpx` client-wrapper pattern from `NotionClient` (`app/services/notion.py`). |
| Router | `app/routers/<name>.py` | Pydantic request model + `POST` endpoint with `dependencies=[Depends(require_api_key)]`, forwarding to `op_*`. Register it in `app/main.py`. |
| Descriptions | `app/routers/_<name>_docs.py` | the verbatim tool description strings (keep router files readable). |
| MCP tool | `app/mcp_server.py` | `@mcp.tool` wrapper — thin adapter calling the router/`op_*`, wrapped in `_run(...)`. **snake_case name, no hyphens** (Gemini rejects hyphens). |

Look at `notion_list_blocks` / `search_memory` as worked examples end-to-end.

## Step-by-step

### Phase 1 — build it (server)
1. **Auth/secrets first (the real work).** Add credentials to `app/config.py` (`Settings`) +
   `.env.example`, and set them on Railway. Reuse what exists where possible: Google
   Workspace can ride the existing Google OAuth in `app/services/_google.py` (just add
   scopes); a new API (Granola) needs its own key/OAuth + client. Get the token actually
   working before anything else — "the tool appeared" ≠ "the token works".
2. **Service** — new `app/services/<name>.py` with `op_*` functions.
3. **Router** — `app/routers/<name>.py` + `app/routers/_<name>_docs.py`; include the router
   in `app/main.py`.
4. **MCP tool** — `@mcp.tool` wrapper(s) in `app/mcp_server.py` (snake_case).

### Phase 2 — make every client use it correctly
5. **Tool description = the contract (universal).** This is the ONE thing every client
   receives. Make it self-sufficient: what it does, *when to reach for it*, inputs,
   read-vs-write, and any confirm-first rule. If a tool needs an external doc to be used
   correctly, the description is incomplete.
6. **`load_context` routing (universal).** Add a `ROUTING` entry in `app/services/alistair.py`
   mapping "what Owen says" → the new tool(s); add a `SAFETY` line if it's irreversible.
   `load_context` is itself a tool, so this reaches every client.
7. **Skill — only if it has multi-step nuance.** If usage is more than a description can
   hold (a protocol, ordering, gotchas), add `app/skills/data/<name>.json`. It's
   file-discovered (auto-appears in the skill index + `get_skill`); point to it from the
   tool description ("load `get_skill('<name>')` first"). Simple tools: skip it.
8. **`INSTRUCTIONS`** (`app/mcp_server.py`) — touch only if the new capability changes the
   global bootstrap. Usually not.
9. **claude.ai scoped skill — only for a genuinely NEW domain/trigger.** If it needs a new
   trigger word (e.g. "Granola" / "my meetings"), add it to the **Description** of
   `claude_skill/alistair/SKILL.md` so claude.ai activates Alistair for it. One line, one
   artifact. Gemini/ChatGPT need nothing. The skill **body** stays thin — never restate
   tool rules there (they live in the connector; duplicating them invites drift).
10. **Memory/persona — nothing.** New tools don't touch memory; never seed per-client.

### Phase 3 — verify & ship
11. **Tests** — add a test file (mock the API with the `FakeClient`/monkeypatch pattern from
    `tests/test_notion_block_tools.py` or `tests/test_memory.py`). **Update the counts in
    `tests/test_mcp.py`** (tool count + the `/api/manifest` total) — these are asserted and
    will fail if not bumped. Run the suite green.
12. **Deploy** — commit → merge to `main` → Railway auto-deploys. Confirm via `/api/manifest`
    counts or a `tools/list` curl.
13. **Reconnect each client (the one manual step).** Toggle/refresh the connector in
    claude.ai, Gemini, ChatGPT; restart the voice agent. They cache `tools/list` at connect
    and won't see new tools until they re-handshake.
14. **Smoke-test on ONE client** — actually call the new tool and confirm OAuth/scopes/refresh
    work end to end.

## The minimum that makes it propagate

For a tool to "just work" everywhere after deploy + reconnect you need exactly two things
authored well: a **self-sufficient tool description** + a **`load_context` ROUTING entry**.
Skill, trigger word, and `INSTRUCTIONS` edits are for nuance or claude.ai-only scoping.

## Cautions

- **Context bloat is the real scaling limit.** Every client loads *all* tool descriptions
  every turn. Google Workspace alone could add 15–30 tools. Keep descriptions tight; past
  ~60–80 tools, group/namespace or add a discovery/router tool (progressive disclosure) so
  the full list isn't always in context.
- **Confirm-first for irreversible actions** — bake "returns a preview unless `confirm=true`"
  into the description + a `SAFETY` entry, like `github_merge_pr` does.
- **Naming** — snake_case, no hyphens (Gemini).
- **One source of truth** — rules live server-side (descriptions / `load_context` / skills),
  never duplicated into a client's memory or the claude.ai skill body.

## Checklist (copy per integration)

- [ ] Secrets in `config.py` + `.env.example` + Railway; token verified working
- [ ] `app/services/<name>.py` (`op_*`)
- [ ] `app/routers/<name>.py` + `_<name>_docs.py`; router registered in `app/main.py`
- [ ] `@mcp.tool` wrapper(s) in `app/mcp_server.py` (snake_case, self-sufficient description)
- [ ] `ROUTING` entry (+ `SAFETY` if irreversible) in `app/services/alistair.py`
- [ ] Skill JSON in `app/skills/data/` *(only if multi-step nuance)*
- [ ] claude.ai trigger word in `SKILL.md` Description *(only if a new domain)*
- [ ] Tests + updated counts in `tests/test_mcp.py`; suite green
- [ ] Commit → merge to `main` → deploy; confirm `/api/manifest`
- [ ] Reconnect every client; smoke-test the new tool on one
