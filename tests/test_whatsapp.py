"""Tests for the WhatsApp layer (read + draft only, never sends).

Reads proxy to a laptop agent — faked via a stub httpx with a `.get` (canned responses
routed by URL substring, or made to raise to simulate the laptop being offline). Drafts
are pure wa.me links (no network), asserted exactly. A TestClient pass checks the wiring
(manifest, routes, validation, skill) and that there is NO send path.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx as _httpx  # real, for HTTPError + simulating the laptop being offline

# Endpoints open (no api-key guard). Leave the agent unset by default so the
# "reading not configured" path is exercised; tests that need it pass Settings kwargs.
os.environ.pop("SERVICE_API_KEY", None)
os.environ.pop("WHATSAPP_AGENT_URL", None)
os.environ.pop("WHATSAPP_AGENT_SECRET", None)

from app.config import Settings, get_settings
from app.services import ServiceError
from app.services import whatsapp

R = []
def check(name, cond):
    R.append((name, bool(cond)))


# ---- fake httpx for the whatsapp module: route URL substr -> FakeResp (or raise) ----
class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

class FakeHttpx:
    HTTPError = _httpx.HTTPError  # so the service's `except httpx.HTTPError` resolves
    def __init__(self, routes=None, raise_exc=None):
        self.routes = routes or []  # list of (substr, FakeResp)
        self.raise_exc = raise_exc
        self.calls = []
    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self.raise_exc:
            raise self.raise_exc
        for sub, resp in self.routes:
            if sub in url:
                return resp
        raise AssertionError(f"no fake route for GET {url}")

def patch(routes=None, raise_exc=None):
    whatsapp.httpx = FakeHttpx(routes, raise_exc)
    return whatsapp.httpx

# Settings with the laptop agent configured (read tests need this); and without.
st_agent = Settings(whatsapp_agent_url="http://laptop.ts.net:8765", whatsapp_agent_secret="sek")
st_plain = Settings()


# === draft: pure wa.me link, deterministic, never sends ===
d = whatsapp.draft(st_plain, to="+44 7700 900000", body="hi there")
check("draft builds wa.me link", d["link"] == "https://wa.me/447700900000?text=hi%20there")
check("draft strips to digits", d["number"] == "447700900000")
check("draft note says the user sends it themselves", "send" in d["note"].lower() and "yourself" in d["note"].lower())

# a bare local number gets the default country code (44)
d2 = whatsapp.draft(st_plain, to="07700900000", body="yo")
check("draft local number -> E.164 with cc", d2["number"] == "447700900000")

# the body is url-encoded into the link
d3 = whatsapp.draft(st_plain, to="447700900000", body="see you @ 5pm?")
check("draft url-encodes body", d3["link"] == "https://wa.me/447700900000?text=see%20you%20%40%205pm%3F")

# missing body -> 422
try:
    whatsapp.draft(st_plain, to="447700900000", body="")
    check("draft empty body -> error", False)
except ServiceError as e:
    check("draft empty body -> 422", e.status_code == 422)

# a NAME with no agent online -> can't resolve -> 422 (never a crash)
try:
    whatsapp.draft(st_plain, to="Howie", body="hi")
    check("draft unresolvable name -> error", False)
except ServiceError as e:
    check("draft unresolvable name -> 422", e.status_code == 422)


# === read: proxies to the agent, parses JSON ===
patch([("/chats", FakeResp(200, {"chats": [{"jid": "j1", "name": "Howie", "unread": 2}]}))])
ch = whatsapp.list_chats(st_agent, limit=10)
check("list_chats count 1", ch["count"] == 1)
check("list_chats parses name", ch["chats"][0]["name"] == "Howie")

patch([("/messages", FakeResp(200, {"messages": [{"from": "Howie", "text": "you around?", "from_me": False}]}))])
ms = whatsapp.read_messages(st_agent, chat="j1", limit=20)
check("read_messages count 1", ms["count"] == 1)
check("read_messages carries text", ms["messages"][0]["text"] == "you around?")

patch([("/search", FakeResp(200, {"messages": [{"chat": "j1", "text": "michelin"}]}))])
se = whatsapp.search(st_agent, query="michelin")
check("search count 1", se["count"] == 1)

# read requires a chat id
try:
    whatsapp.read_messages(st_agent, chat="")
    check("read empty chat -> error", False)
except ServiceError as e:
    check("read empty chat -> 422", e.status_code == 422)


# === recent (inbox) ===
patch([("/recent", FakeResp(200, {"chats": [{"jid": "447379648355@s.whatsapp.net", "name": "fatty chloe", "lastTs": 123, "unread": 1, "lastText": "hey"}]}))])
rc = whatsapp.recent(st_agent, limit=10)
check("recent count 1", rc["count"] == 1)
check("recent carries last-text preview", rc["chats"][0]["lastText"] == "hey")

# === resolve (name/number/jid -> canonical {jid,name,number}) ===
patch([("/resolve", FakeResp(200, {"jid": "447379648355@s.whatsapp.net", "name": "fatty chloe", "number": "447379648355"}))])
rv = whatsapp.resolve(st_agent, query="chloe")
check("resolve returns number", rv["number"] == "447379648355")
check("resolve returns name", rv["name"] == "fatty chloe")

# === find: resolve -> read in ONE hop ===
patch([
    ("/resolve", FakeResp(200, {"jid": "447379648355@s.whatsapp.net", "name": "fatty chloe", "number": "447379648355"})),
    ("/messages", FakeResp(200, {"messages": [{"from": "fatty chloe", "fromMe": False, "ts": 123, "text": "hi"}]})),
])
fd = whatsapp.find(st_agent, query="chloe")
check("find found True", fd["found"] is True)
check("find resolved contact", fd["contact"]["number"] == "447379648355")
check("find read messages", fd["count"] == 1 and fd["messages"][0]["text"] == "hi")

# find: no match -> found False (not a crash)
patch([("/resolve", FakeResp(200, {"jid": "", "name": "", "number": ""}))])
nf = whatsapp.find(st_agent, query="nobody")
check("find no-match -> found False", nf["found"] is False)

# === draft by NAME now resolves via /resolve to the canonical number ===
patch([("/resolve", FakeResp(200, {"jid": "447379648355@s.whatsapp.net", "name": "fatty chloe", "number": "447379648355"}))])
dn = whatsapp.draft(st_agent, to="fatty chloe", body="call me")
check("draft by name resolves number", dn["number"] == "447379648355")
check("draft by name builds link", dn["link"] == "https://wa.me/447379648355?text=call%20me")
check("draft by name notes resolved_from", dn.get("resolved_from_name") == "fatty chloe")


# === offline: laptop agent unreachable -> clean 503, not a crash ===
patch(raise_exc=_httpx.ConnectError("connection refused"))
try:
    whatsapp.list_chats(st_agent)
    check("offline raises", False)
except ServiceError as e:
    check("offline -> 503", e.status_code == 503)
    check("offline message mentions offline", "offline" in e.message.lower())


# === reading not configured (no agent url) -> 503 ===
try:
    whatsapp.list_chats(st_plain)
    check("no agent raises", False)
except ServiceError as e:
    check("no agent -> 503", e.status_code == 503)


# === read + draft ONLY: no send path exists by design ===
check("no whatsapp.send", not hasattr(whatsapp, "send"))
check("no whatsapp.send_message", not hasattr(whatsapp, "send_message"))


# === wiring via TestClient: manifest, routes, validation, skill ===
from fastapi.testclient import TestClient
get_settings.cache_clear()
from app.main import app
c = TestClient(app)

mani = c.get("/api/manifest").json()
check("manifest whatsapp has 6 tools", mani["counts"].get("whatsapp") == 6)
paths = [t["path"] for t in mani["function_apis"]["whatsapp"]]
for p in ["/api/whatsapp/chats", "/api/whatsapp/messages", "/api/whatsapp/search",
          "/api/whatsapp/recent", "/api/whatsapp/find", "/api/whatsapp/draft"]:
    check(f"manifest lists {p}", p in paths)
draft_desc = next(t["description"] for t in mani["function_apis"]["whatsapp"] if t["path"] == "/api/whatsapp/draft")
check("draft description says never sends", "never sends" in draft_desc.lower())
chats_desc = next(t["description"] for t in mani["function_apis"]["whatsapp"] if t["path"] == "/api/whatsapp/chats")
check("chats description says read-only", "read-only" in chats_desc.lower())

# draft works over HTTP with no agent configured (pure link)
hd = c.post("/api/whatsapp/draft", json={"to": "+447700900000", "body": "hi"})
check("http draft 200", hd.status_code == 200)
check("http draft returns link", hd.json()["link"].startswith("https://wa.me/447700900000"))

# reading without an agent -> clean 503 (not a crash)
hc = c.post("/api/whatsapp/chats", json={})
check("http chats without agent -> 503", hc.status_code == 503)

bad = c.post("/api/whatsapp/messages", json={})  # missing required 'chat'
check("messages validates body (422)", bad.status_code == 422)

root = c.get("/").json()
check("root reports whatsapp flag", "whatsapp_read" in root["configured"])

sk = c.get("/api/skill/whatsapp").json()
check("whatsapp skill served", sk.get("skill") == "whatsapp")
check("whatsapp skill is read+draft only", "never sends" in json.dumps(sk).lower())


# --- results ---
print("=== RESULTS (whatsapp) ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
