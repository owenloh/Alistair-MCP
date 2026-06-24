# Alistair — claude.ai rollout (your steps)

How to point claude.ai (and other LLM surfaces) at the one Alistair MCP. The
server is live on Railway; everything below is config you do once.

Reference: `docs/ALISTAIR_MCP_BUILD_SPEC.md` §4–6.

---

## 0. The endpoint

- **MCP URL:** `https://<your-railway-host>/mcp` (find the host in the Railway dashboard, or on the service's `/` page)
- **Server name:** `alistair_assistant` · **Transport:** Streamable-HTTP · **26 tools** (Notion, Calendar, **Gmail read+draft**, in-tray, GitHub, memory, persona/skills)
- **Self-contained:** the detailed skills (`notion-master`, `daily-brief`, `notion-references-tray`, `microsoft-todo-intray`, `gmail`) are served *inside* the MCP via the `get_skill` tool — so you upload only one thin bootstrap skill, and need **no other connectors or skill uploads**.
- **Auth:** claude.ai → **OAuth** (auto-discovered) **gated by an approval password** — only you can approve a connection. Every other client → **Bearer** `SERVICE_API_KEY`.

Check it's healthy: open `/` — the `mcp` block shows `"oauth_enabled": true`. If it
says `false`, set `PUBLIC_BASE_URL` (below) and redeploy.

## 1. Railway variables (one-time)

| Variable | Why | Required |
| --- | --- | --- |
| `SERVICE_API_KEY` | Bearer token for non-claude.ai clients; also accepted by the MCP. **Rotate the one shared in chat.** | yes |
| `OAUTH_APPROVAL_PASSWORD` | Password you type on the approval page when connecting claude.ai — this is what stops anyone else connecting. Falls back to `SERVICE_API_KEY` if unset; set a memorable-but-strong value. | strongly recommended |
| `PUBLIC_BASE_URL` | OAuth issuer, e.g. `https://<your-railway-host>`. Only needed if `RAILWAY_PUBLIC_DOMAIN` isn't auto-set (the `/` page tells you). | usually auto |
| Railway **Volume** mounted (any path) | Persists memory across redeploys. Without it memory is wiped each deploy (`/` shows `memory_persistent:false`). | recommended |
| `GITHUB_REPO_TOKEN` | Fine-grained PAT (repo read + PR) so `project_context`/`github_*` work. Falls back to the gist token, which lacks repo scope. | for GitHub tools |
| `GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN` | Calendar + Gmail. Mint with `scripts/get_google_token.py`. Scopes: `…/auth/calendar` (read+write) and, for Gmail, `gmail.readonly` + `gmail.compose`. Publish the OAuth app to **Production** or the refresh token expires in 7 days. | for Calendar/Gmail |

## 2. claude.ai (web/desktop)

1. **Settings → Connectors → Add custom connector.** Paste the **MCP URL**. claude.ai
   runs the OAuth dance automatically (registers itself via DCR). A small **"Approve
   Alistair connection" page** appears in your browser — enter your `OAUTH_APPROVAL_PASSWORD`
   and it issues the token. No client id/secret to paste. (Anyone without that password
   can't connect, even though the URL is public.)
2. **Do NOT enable** the official **Notion / Todoist** connectors. All Notion/tasks flow
   through Alistair. (You can't replace the official one in place — just leave it off.)
3. **Upload ONE "alistair" bootstrap Skill** (Settings → Capabilities → Skills). Put the
   trigger in the **`description`** (scope it to the "Alistair/Ali" wake word + your Notion/
   calendar/mail/tasks) so it auto-loads when you ask, not on unrelated turns. ⚠️ Do **not**
   rely on `disable-model-invocation: true` for opt-in — that frontmatter flag makes a skill
   **slash-command-only** (hidden from the model), so it won't fire on a spoken "Alistair" or
   in voice. The thin body tells Claude to: (a) call `load_context` + `get_memory` first,
   (b) `get_skill("notion-master")` before any Notion write, (c) use Alistair's tools only
   (never a built-in connector), (d) `save_memory` when it learns something durable. The
   detailed skills live **in the MCP** (`get_skill`) — you don't upload them separately.
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
verified: discovery metadata, dynamic client registration, the approval gate, the
authorization-code + PKCE exchange, refresh, and that a minted token authorizes `/mcp`
(23 in-process checks + the full flow live on Railway). If claude.ai reports an auth
error, check `/` shows `oauth_enabled:true` and that the connector URL ends in `/mcp`.

## 6. Security notes

- The **approval-password gate** on `/authorize` is what makes the public URL safe: OAuth
  no longer auto-approves, so knowing the URL is not enough — a connection is only issued
  after someone enters `OAUTH_APPROVAL_PASSWORD` on the consent page (the password is
  checked in constant time; the gate fails closed if no secret is configured).
- **Rotate** `SERVICE_API_KEY` and any Notion/GitHub/Microsoft tokens shared in plaintext.
- The non-OAuth path (Bearer `SERVICE_API_KEY`) still gates Claude Desktop/Code, Cursor,
  the voice shell and Gemini CLI.
- Defense in depth: you can also keep the repo private and/or regenerate the Railway domain.
- All secrets live in **Railway Variables**, never in chat.
