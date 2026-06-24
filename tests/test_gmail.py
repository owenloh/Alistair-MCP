"""Tests for the Gmail layer (read + draft only).

The gmail service's HTTP calls go through a fake httpx (canned responses routed by
method + URL substring) and token minting is stubbed, so the real parsing / MIME
building / draft logic runs without touching the network. A TestClient pass checks
the wiring (manifest, routes, validation, skill). Also asserts there is NO send path.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Endpoints open (no api-key guard) but Google trio present so configured.gmail = true.
os.environ.pop("SERVICE_API_KEY", None)
os.environ["GOOGLE_REFRESH_TOKEN"] = "r"
os.environ["GOOGLE_CLIENT_ID"] = "c"
os.environ["GOOGLE_CLIENT_SECRET"] = "s"

from app.config import Settings, get_settings
from app.services import ServiceError
from app.services import _google
from app.services import gmail

R = []
def check(name, cond):
    R.append((name, bool(cond)))


# ---- fake httpx for the gmail module: route (method, substr) -> FakeResp ----
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
    def __init__(self, routes):
        self.routes = routes  # list of (method, substr, FakeResp)
        self.calls = []
    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json))
        for m, sub, resp in self.routes:
            if m == method and sub in url:
                return resp
        raise AssertionError(f"no fake route for {method} {url}")

def patch(routes):
    gmail.httpx = FakeHttpx(routes)
    gmail.mint_access_token = lambda s: "tok"   # bypass real OAuth
    return gmail.httpx

st = Settings()


# === search: list ids then fetch metadata per id -> stubs ===
patch([
    ("GET", "/messages/m1", FakeResp(200, {
        "id": "m1", "threadId": "t1", "snippet": "snip text", "labelIds": ["INBOX"],
        "payload": {"headers": [
            {"name": "From", "value": "jane@x.com"},
            {"name": "Subject", "value": "Hi there"},
            {"name": "Date", "value": "Mon, 1 Jan 2026"}]}})),
    ("GET", "/messages", FakeResp(200, {"messages": [{"id": "m1", "threadId": "t1"}], "resultSizeEstimate": 1})),
])
out = gmail.search(st, query="from:jane", max_results=5)
check("search returns count 1", out["count"] == 1)
check("search parses From", out["messages"][0]["from"] == "jane@x.com")
check("search parses Subject", out["messages"][0]["subject"] == "Hi there")
check("search carries snippet", out["messages"][0]["snippet"] == "snip text")
check("search carries thread_id", out["messages"][0]["thread_id"] == "t1")


# === get_thread: text/plain body decoded ===
patch([("GET", "/threads/t1", FakeResp(200, {"id": "t1", "messages": [
    {"id": "m1", "payload": {
        "headers": [{"name": "From", "value": "a@x.com"}, {"name": "Subject", "value": "S"}],
        "mimeType": "text/plain",
        "body": {"data": base64.urlsafe_b64encode(b"Hello body line").decode()}}}]}))])
th = gmail.get_thread(st, thread_id="t1")
check("thread message_count 1", th["message_count"] == 1)
check("thread decodes plain body", th["messages"][0]["body"] == "Hello body line")
check("thread parses From", th["messages"][0]["from"] == "a@x.com")


# === get_thread: html-only falls back to stripped text ===
html_b64 = base64.urlsafe_b64encode(b"<p>Hi <b>there</b></p>").decode()
patch([("GET", "/threads/t2", FakeResp(200, {"messages": [
    {"id": "m", "payload": {"headers": [], "mimeType": "text/html", "body": {"data": html_b64}}}]}))])
th2 = gmail.get_thread(st, thread_id="t2")
body2 = th2["messages"][0]["body"]
check("thread html->text strips tags", "Hi there" in body2 and "<" not in body2)


# === create_draft: builds MIME, posts to /drafts, returns draft_id ===
fx = patch([("POST", "/drafts", FakeResp(200, {"id": "d1", "message": {"id": "msg1", "threadId": "t1"}}))])
res = gmail.create_draft(st, to="bob@x.com", subject="Subj line", body="Hello there body", thread_id="t1")
check("create returns draft_id", res["draft_id"] == "d1")
check("create created=True", res["created"] is True)
posted = fx.calls[-1][3]
raw = posted["message"]["raw"]
decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
check("draft MIME has To", "bob@x.com" in decoded)
check("draft MIME has Subject", "Subj line" in decoded)
check("draft MIME has body", "Hello there body" in decoded)
check("create threads via threadId", posted["message"]["threadId"] == "t1")


# === create_draft requires a recipient ===
try:
    gmail.create_draft(st, to="", subject="x", body="y")
    check("create-draft empty to -> error", False)
except ServiceError as e:
    check("create-draft empty to -> 422", e.status_code == 422)


# === delete_draft: DELETE only, returns deleted ===
fx = patch([("DELETE", "/drafts/d1", FakeResp(204))])
res = gmail.delete_draft(st, draft_id="d1")
check("delete returns deleted=True", res["deleted"] is True)
check("delete used DELETE verb", fx.calls[-1][0] == "DELETE")


# === scope error (403) surfaces as ServiceError with detail ===
patch([("GET", "/messages", FakeResp(403, {"error": {"message": "Request had insufficient authentication scopes."}}))])
try:
    gmail.search(st, query="x")
    check("403 raises", False)
except ServiceError as e:
    check("403 -> ServiceError 502", e.status_code == 502)
    check("403 detail mentions scopes", "scopes" in (e.detail or "").lower())


# === read + draft ONLY: no send path exists by design ===
check("no gmail.send", not hasattr(gmail, "send"))
check("no gmail.send_message", not hasattr(gmail, "send_message"))
check("no gmail.send_draft", not hasattr(gmail, "send_draft"))


# === shared Google helper: missing trio -> 503 ===
empty = Settings(google_refresh_token=None, google_client_id=None, google_client_secret=None)
try:
    _google.mint_access_token(empty)
    check("mint missing trio raises", False)
except ServiceError as e:
    check("mint missing trio -> 503", e.status_code == 503)


# === wiring via TestClient: manifest, routes, validation, skill ===
from fastapi.testclient import TestClient
get_settings.cache_clear()
from app.main import app
c = TestClient(app)

mani = c.get("/api/manifest").json()
check("manifest gmail has 6 tools", mani["counts"].get("gmail") == 6)
paths = [t["path"] for t in mani["function_apis"]["gmail"]]
for p in ["/api/gmail/search", "/api/gmail/get-thread", "/api/gmail/list-drafts",
          "/api/gmail/create-draft", "/api/gmail/update-draft", "/api/gmail/delete-draft"]:
    check(f"manifest lists {p}", p in paths)
search_desc = next(t["description"] for t in mani["function_apis"]["gmail"] if t["path"] == "/api/gmail/search")
check("search description mentions read-only", "read-only" in search_desc.lower())
draft_desc = next(t["description"] for t in mani["function_apis"]["gmail"] if t["path"] == "/api/gmail/create-draft")
check("create-draft description says never sends", "never sends" in draft_desc.lower())

bad = c.post("/api/gmail/create-draft", json={})  # missing required 'to'
check("create-draft validates body (422)", bad.status_code == 422)

root = c.get("/").json()
check("root reports gmail flag", "gmail" in root["configured"])
check("root gmail flag true (trio set)", root["configured"]["gmail"] is True)

sk = c.get("/api/skill/gmail").json()
check("gmail skill served", sk.get("skill") == "gmail")
check("gmail skill is draft-only", "drafts" in json.dumps(sk).lower() and "never send" in json.dumps(sk).lower())


# --- results ---
print("=== RESULTS (gmail) ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
