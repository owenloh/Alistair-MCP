# Alistair — claude.ai rollout (your steps)

How to point claude.ai (and other LLM surfaces) at the one Alistair MCP. The
server is live on Railway; everything below is config you do once.

Reference: `docs/ALISTAIR_MCP_BUILD_SPEC.md` §4–6.

---

## 0. The endpoint

- **MCP URL:** `https://<your-railway-host>/mcp` (e.g. `https://web-production-2144c.up.railway.app/mcp`)
- **Server name:** `alistair_assistant` · **Transport:** Streamable-HTTP
- **Auth:** claude.ai → **OAuth** (auto-discovered). Every other client → **Bearer** `SERVICE_API_KEY`.

Check it's healthy: open `/` — the `mcp` block shows `"oauth_enabled": true`. If it
says `false`, set `PUBLIC_BASE_URL` (below) and redeploy.

## 1. Railway variables (one-time)

| Variable | Why | Required |
| --- | --- | --- |
| `SERVICE_API_KEY` | Bearer token for non-claude.ai clients; also accepted by the MCP. **Rotate the one shared in chat.** | yes |
| `PUBLIC_BASE_URL` | OAuth issuer, e.g. `https://web-production-2144c.up.railway.app`. Only needed if `RAILWAY_PUBLIC_DOMAIN` isn't auto-set (the `/` page tells you). | usually auto |
| Railway **Volume** mounted (any path) | Persists memory across redeploys. Without it memory is wiped each deploy (`/` shows `memory_persistent:false`). | recommended |
| `GITHUB_REPO_TOKEN` | Fine-grained PAT (repo read + PR) so `project_context`/`github_*` work. Falls back to the gist token, which lacks repo scope. | for GitHub tools |

## 2. claude.ai (web/desktop)

1. **Settings → Connectors → Add custom connector.** Paste the **MCP URL**. claude.ai
   runs the OAuth dance automatically — our server auto-registers the client (DCR),
   auto-approves (you are the only user), and issues a token. No client id/secret to paste.
2. **Do NOT enable** the official **Notion / Todoist** connectors. All Notion/tasks flow
   through Alistair. (You can't replace the official one in place — just leave it off.)
3. **Upload ONE "Alistair" Skill** (Settings → Capabilities → Skills). In its YAML set
   **`disable-model-invocation: true`** so it is **opt-in** — it loads only when you say
   "Alistair"/"Ali", not by relevance. The skill body tells Claude to: (a) call
   `load_context()` + `get_memory()` at the start, (b) use Alistair's tools only (never a
   built-in connector), (c) call `save_memory()` when it learns something durable.
4. **Pause native memory** (Settings → Capabilities) so the MCP is the only memory. ⚠️
   Claude does not auto-write to MCP memory — the skill must tell it when to `save_memory`.

## 3. Other clients (work today via Bearer)

- **Claude Desktop / Code, Cursor:** add the remote MCP at the URL with header
  `Authorization: Bearer <SERVICE_API_KEY>`.
- **Pipecat voice shell:** `MCPClient(StreamableHttpParameters(url=".../mcp", headers={"Authorization": f"Bearer {KEY}"}))`, preload-once + ~5-min refresh.
- **Gemini CLI / Enterprise:** register the Streamable-HTTP MCP (tools only; not the consumer Gemini app).

## 4. Opt-in model

Say **"Alistair"/"Ali"** → the skill loads (persona) + the MCP supplies tools + memory →
full assistant. Don't say it → plain Claude, no Alistair context. (The Skill with
`disable-model-invocation` is what gives clean opt-in; Custom Instructions/Projects are
always-on and can't.)

## 5. The one thing I can't test from here

The end-to-end **claude.ai OAuth connect** needs to be done from your claude.ai account
(Anthropic's cloud can't be reached from this build env). Everything underneath is
verified: discovery metadata, dynamic client registration, the authorization-code + PKCE
exchange, refresh, and that a minted token authorizes `/mcp` (22 in-process checks), plus
the live Bearer handshake against Railway. If claude.ai reports an auth error, check
`/` shows `oauth_enabled:true` and that the connector URL ends in `/mcp`.

## 6. Security notes

- **Rotate** `SERVICE_API_KEY` and any Notion/GitHub tokens shared in plaintext.
- Auto-approve OAuth means anyone who knows the URL *and* completes the flow could connect.
  Mitigations: keep the URL private; the bearer key still gates non-OAuth access. If you
  later want a login gate or multi-user, move to per-identity tokens (a future step).
- All secrets live in **Railway Variables**, never in chat.
