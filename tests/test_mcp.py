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
check("22 tools registered", len(tools) == 22)
check("all tool names snake_case (no hyphens)",
      all(n.replace("_", "a").isalnum() and n == n.lower() and "-" not in n for n in names))
for core in ("load_context", "get_memory", "save_memory", "get_skill", "daily_brief",
             "project_context", "save_reference", "add_action", "notion_search", "notion_fetch",
             "notion_update_page", "calendar_today", "intray", "github_merge_pr"):
    check(f"tool present: {core}", core in names)

# === descriptions are persona-loaded + carry the safety hooks ===
check("every tool has a real description", all((t.description or "") and len(t.description) > 20 for t in tools))
check("load_context says call first", "FIRST" in by_name["load_context"].description.upper())
check("load_context names Alistair", "Alistair" in by_name["load_context"].description)
check("save_memory marked the only write path", "ONLY" in by_name["save_memory"].description.upper())
check("notion_update_page warns never overwrite whole page",
      "never overwrite" in by_name["notion_update_page"].description.lower())
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

# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
