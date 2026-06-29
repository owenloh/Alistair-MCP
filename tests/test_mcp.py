"""Tests for the Alistair MCP server (Streamable-HTTP tool surface + bearer guard).

Drives the FastMCP instance in-process: list_tools (names/descriptions/snake_case),
call_tool round-trips through the real services (memory save->get->retract), error
shaping, and the BearerAuthASGI guard. No network, no transport — the tool handlers
are the same code the REST API runs.
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Point memory at a throwaway DB before any settings are cached.
os.environ["MEMORY_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="mcp_mem_"), "mem.db")
os.environ.pop("SERVICE_API_KEY", None)
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GITHUB_GIST_TOKEN", None)
os.environ.pop("GITHUB_REPO_TOKEN", None)

from app import config as _cfg
_cfg.get_settings.cache_clear()
from app import mcp_server as M

R = []
def check(name, cond):
    R.append((name, bool(cond)))


def call(name, args):
    """Invoke an MCP tool and return its structured dict (parsed from the result)."""
    res = asyncio.run(M.mcp.call_tool(name, args))
    if isinstance(res, tuple):  # some SDK versions return (content, structured)
        for part in res:
            if isinstance(part, dict):
                return part
        res = res[0]
    # res is a list of content blocks; the dict is JSON in the first text block
    return json.loads(res[0].text)


tools = asyncio.run(M.mcp.list_tools())
by_name = {t.name: t for t in tools}
names = set(by_name)

# === server identity + tool registration ===
check("server name is snake_case alistair_assistant", M.mcp.name == "alistair_assistant")
check("60 tools registered (51 + 9 spotify)",
      len(tools) == 60)
check("tool present: search_memory", "search_memory" in names)
check("tool present: memory_maintenance", "memory_maintenance" in names)
for blk in ("notion_list_blocks", "notion_append_blocks", "notion_update_block",
            "notion_delete_blocks", "notion_move_blocks", "notion_markdown_spec"):
    check(f"tool present: {blk}", blk in names)
for sp in ("spotify_playlists", "spotify_playlist_tracks", "spotify_search", "spotify_devices",
           "spotify_transfer", "spotify_status", "spotify_play", "spotify_queue", "spotify_control"):
    check(f"tool present: {sp}", sp in names)
for wa in ("whatsapp_chats", "whatsapp_read", "whatsapp_search", "whatsapp_recent",
           "whatsapp_find", "whatsapp_draft"):
    check(f"tool present: {wa}", wa in names)
check("all tool names snake_case (no hyphens)",
      all(n.replace("_", "a").isalnum() and n == n.lower() and "-" not in n for n in names))
for core in ("load_context", "get_memory", "save_memory", "get_skill", "daily_brief",
             "project_context", "save_reference", "add_action", "notion_search", "notion_fetch",
             "notion_update_page", "calendar_today", "intray", "github_merge_pr",
             "github_whoami", "github_list_my_repos",
             "gmail_search", "gmail_read_thread", "gmail_create_draft", "gmail_list_drafts"):
    check(f"tool present: {core}", core in names)

# === descriptions are persona-loaded + carry the safety hooks ===
check("every tool has a real description", all((t.description or "") and len(t.description) > 20 for t in tools))
check("load_context says call first", "FIRST" in by_name["load_context"].description.upper())
check("load_context names Alistair", "Alistair" in by_name["load_context"].description)
check("save_memory marked the only write path", "ONLY" in by_name["save_memory"].description.upper())
check("notion_update_page warns about whole-page overwrite",
      "overwrites the whole page" in by_name["notion_update_page"].description.lower())
check("notion_update_page advertises the multi-match fail-safe",
      "replace_all_matches" in by_name["notion_update_page"].description)
check("notion_delete_blocks steers away from text-match deletes",
      "never delete by text" in by_name["notion_delete_blocks"].description.lower())
check("notion resources served (spec + skills template)",
      any("notion-markdown-spec" in str(r.uri) for r in asyncio.run(M.mcp.list_resources())))
check("github_merge_pr warns confirm-first",
      "confirm=false" in by_name["github_merge_pr"].description and
      "never merge" in by_name["github_merge_pr"].description.lower())
check("save_reference marked insert-only", "INSERT-ONLY" in by_name["save_reference"].description)

# === input schemas exist + reflect params ===
check("save_memory schema has content", "content" in (by_name["save_memory"].inputSchema.get("properties") or {}))
check("github_merge_pr schema has confirm", "confirm" in (by_name["github_merge_pr"].inputSchema.get("properties") or {}))
check("load_context takes no required args",
      not (by_name["load_context"].inputSchema.get("required") or []))

# === call_tool: load_context returns the constitution ===
ctx = call("load_context", {})
check("load_context -> persona Alistair", ctx.get("persona", {}).get("name") == "Alistair")
check("load_context -> has skills + memory", "skills" in ctx and "memory" in ctx)

# === call_tool: memory save -> get -> retract -> get (clean), via the MCP ===
saved = call("save_memory", {"content": "__mcp_smoke__ test fact", "type": "fact", "relevance": 1})
check("save_memory -> created", saved.get("status") == "created")
got = call("get_memory", {})
check("get_memory sees the smoke fact", "__mcp_smoke__" in got.get("memory_block", ""))
check("get_memory reports total 1", got.get("total_entries") == 1)
ret = call("save_memory", {"content": "__mcp_smoke__ test fact", "type": "fact", "op": "retract"})
check("save_memory retract -> retracted", ret.get("status") == "retracted")
got2 = call("get_memory", {})
check("get_memory clean after retract", "__mcp_smoke__" not in got2.get("memory_block", ""))
check("get_memory total back to 0", got2.get("total_entries") == 0)

# === call_tool: get_skill good + bad ===
sk = call("get_skill", {"slug": "notion-master"})
check("get_skill notion-master returns rules", isinstance(sk, dict) and "error" not in sk)
bad = call("get_skill", {"slug": "nope"})
check("get_skill bad slug -> error + available", bad.get("status") == 404 and "available" in bad)

# === call_tool: error shaping (no GitHub token -> clean 503 dict, not a crash) ===
pc = call("project_context", {"owner": "o", "repo": "r"})
check("project_context without token -> 503 error dict", pc.get("status") == 503 and "error" in pc)

# === call_tool: daily_brief degrades gracefully (nothing configured) ===
brief = call("daily_brief", {})
check("daily_brief returns unavailable list", isinstance(brief.get("unavailable"), list) and len(brief["unavailable"]) == 3)

# === call_tool: spotify degrades to a clean 503 when not configured (no crash) ===
sp_dev = call("spotify_devices", {})
check("spotify_devices without config -> 503 error dict", sp_dev.get("status") == 503 and "error" in sp_dev)
sp_search = call("spotify_search", {"query": "x"})
check("spotify_search without config -> 503 error dict", sp_search.get("status") == 503)


# === BearerAuthASGI guard ===
async def drive(wrapper, headers):
    scope = {"type": "http", "headers": headers}
    sent = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(m):
        sent.append(m)
    await wrapper(scope, receive, send)
    return sent

called = {"v": False}
async def inner(scope, receive, send):
    called["v"] = True
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})

wrap = M.BearerAuthASGI(inner)

# open mode (no SERVICE_API_KEY): passes through
os.environ.pop("SERVICE_API_KEY", None); _cfg.get_settings.cache_clear()
called["v"] = False
asyncio.run(drive(wrap, []))
check("open mode passes through", called["v"] is True)

# guarded mode: set a key
os.environ["SERVICE_API_KEY"] = "s3cret"; _cfg.get_settings.cache_clear()
called["v"] = False
sent = asyncio.run(drive(wrap, []))  # no auth header
check("guarded: missing token -> 401", sent[0]["status"] == 401 and called["v"] is False)
check("guarded: 401 sets WWW-Authenticate", any(h[0] == b"www-authenticate" for h in sent[0]["headers"]))

called["v"] = False
asyncio.run(drive(wrap, [(b"authorization", b"Bearer s3cret")]))
check("guarded: correct bearer passes", called["v"] is True)

called["v"] = False
asyncio.run(drive(wrap, [(b"x-api-key", b"s3cret")]))
check("guarded: correct X-API-Key passes", called["v"] is True)

called["v"] = False
sent = asyncio.run(drive(wrap, [(b"authorization", b"Bearer wrong")]))
check("guarded: wrong token -> 401", sent[0]["status"] == 401 and called["v"] is False)

os.environ.pop("SERVICE_API_KEY", None); _cfg.get_settings.cache_clear()


# === HTTP through the _MCPDispatcher: /mcp served at the exact path, no redirect ===
os.environ["SERVICE_API_KEY"] = "httpkey"; _cfg.get_settings.cache_clear()
from fastapi.testclient import TestClient
from app.main import asgi
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}}}
MH = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
with TestClient(asgi) as cl:
    # bare /mcp must NOT 307/404 — it serves directly
    r = cl.post("/mcp", json=INIT, headers={**MH, "Authorization": "Bearer httpkey"}, follow_redirects=False)
    check("/mcp (no slash) serves directly 200", r.status_code == 200)
    check("/mcp returns alistair_assistant", r.json().get("result", {}).get("serverInfo", {}).get("name") == "alistair_assistant")
    rslash = cl.post("/mcp/", json=INIT, headers={**MH, "Authorization": "Bearer httpkey"}, follow_redirects=False)
    check("/mcp/ also serves 200", rslash.status_code == 200)
    rno = cl.post("/mcp", json=INIT, headers=MH, follow_redirects=False)
    check("/mcp no auth -> 401 (not 307/404)", rno.status_code == 401)
    rwrong = cl.post("/mcp", json=INIT, headers={**MH, "Authorization": "Bearer nope"}, follow_redirects=False)
    check("/mcp wrong key -> 401", rwrong.status_code == 401)
    # REST still flows through the dispatcher to FastAPI
    check("dispatcher passes /health to FastAPI", cl.get("/health").status_code == 200)
    check("dispatcher passes /api/manifest to FastAPI", cl.get("/api/manifest").json()["counts"]["total"] == 82)
os.environ.pop("SERVICE_API_KEY", None); _cfg.get_settings.cache_clear()

# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
