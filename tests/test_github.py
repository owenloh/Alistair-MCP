"""Tests for the GitHub read layer, the merge_pr confirm-guard, and project_context.

The GitHubClient HTTP methods are exercised against a fake httpx client (canned
responses routed by URL), so the real parsing/decoding/exclusion logic runs
without touching the network. merge_pr_guarded and project_context are driven by
small high-level fakes that also assert *no* destructive call happens unless asked.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import Settings
from app.services import ServiceError
from app.services import alistair as a
from app.services.github import GitHubClient, merge_pr_guarded

R = []
def check(name, cond):
    R.append((name, bool(cond)))


# ---- fake httpx client: route GET/PUT by URL substring -> (status, payload) ----
class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
    def json(self):
        return self._payload

class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes  # list of (method, substr, status, payload)
        self.calls = []
    def _match(self, method, url):
        for m, sub, st, pl in self.routes:
            if m == method and sub in url:
                return FakeResp(st, pl)
        raise AssertionError(f"no fake route for {method} {url}")
    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        return self._match("GET", url)
    def put(self, url, json=None):
        self.calls.append(("PUT", url, json))
        return self._match("PUT", url)

def mk(routes):
    gh = GitHubClient("x")     # token must be truthy; real client is discarded
    gh._client.close()
    gh._client = FakeHTTP(routes)
    return gh


# === get_file: base64 decode + directory/binary guards ===
b64 = base64.b64encode(b"hello world").decode()
gh = mk([("GET", "/contents/README.md", 200,
         {"type": "file", "encoding": "base64", "content": b64,
          "path": "README.md", "sha": "abc", "size": 11, "html_url": "u"})])
f = gh.get_file("o", "r", "README.md")
check("get_file decodes base64", f["content"] == "hello world")
check("get_file keeps path/sha", f["path"] == "README.md" and f["sha"] == "abc")

gh = mk([("GET", "/contents/src", 200, [{"type": "dir", "name": "src"}])])
try:
    gh.get_file("o", "r", "src"); check("get_file dir -> 400", False)
except ServiceError as e:
    check("get_file dir -> 400", e.status_code == 400)

gh = mk([("GET", "/contents/logo.png", 200,
         {"type": "file", "encoding": "base64", "content": "!!!notbase64!!!", "path": "logo.png"})])
try:
    gh.get_file("o", "r", "logo.png"); check("get_file binary -> 415", False)
except ServiceError as e:
    check("get_file binary -> 415", e.status_code == 415)

# === list_tree: explicit ref skips the default-branch lookup ===
gh = mk([("GET", "/git/trees/main", 200,
         {"truncated": False, "tree": [
             {"path": "app", "type": "tree"},
             {"path": "app/main.py", "type": "blob", "size": 42}]})])
tr = gh.list_tree("o", "r", ref="main")
check("list_tree count", tr["count"] == 2)
check("list_tree ref echoed", tr["ref"] == "main")
check("list_tree entry shape", tr["entries"][1]["path"] == "app/main.py")

# list_tree with no ref resolves the default branch first
gh = mk([("GET", "/repos/o/r", 200, {"default_branch": "trunk"}),
         ("GET", "/git/trees/trunk", 200, {"tree": []})])
tr2 = gh.list_tree("o", "r")
check("list_tree resolves default branch", tr2["ref"] == "trunk")

# === search_code: scoping + item shape ===
gh = mk([("GET", "/search/code", 200,
         {"total_count": 1, "items": [
             {"path": "a.py", "repository": {"full_name": "o/r"}, "html_url": "u"}]})])
sc = gh.search_code("foo", owner="o", repo="r")
check("search_code scopes to repo", "repo:o/r" in sc["query"])
check("search_code item repo", sc["items"][0]["repo"] == "o/r")
check("search_code total_count", sc["total_count"] == 1)

# === recent_commits: sha truncation + first-line message ===
gh = mk([("GET", "/commits", 200, [
    {"sha": "abcdef1234567890ff", "html_url": "u",
     "commit": {"message": "fix: the thing\n\nlong body here",
                "author": {"name": "Owen", "date": "2026-01-01T00:00:00Z"}}}])])
cm = gh.recent_commits("o", "r")
check("commit sha truncated to 10", cm[0]["sha"] == "abcdef1234")
check("commit message first line only", cm[0]["message"] == "fix: the thing")
check("commit author", cm[0]["author"] == "Owen")

# === list_prs: head/base flattened ===
gh = mk([("GET", "/pulls", 200, [
    {"number": 3, "title": "feat", "state": "open", "draft": False,
     "user": {"login": "owen"}, "head": {"ref": "dev"}, "base": {"ref": "main"}, "html_url": "u"}])])
prs = gh.list_prs("o", "r")
check("list_prs head/base", prs[0]["head"] == "dev" and prs[0]["base"] == "main")
check("list_prs user", prs[0]["user"] == "owen")

# === list_issues: PRs excluded, labels flattened ===
gh = mk([("GET", "/issues", 200, [
    {"number": 1, "title": "real issue", "state": "open", "user": {"login": "o"},
     "labels": [{"name": "bug"}, {"name": "p1"}], "comments": 2, "html_url": "u"},
    {"number": 2, "title": "i am a PR", "state": "open", "pull_request": {"url": "x"}}])])
iss = gh.list_issues("o", "r")
check("list_issues excludes PRs", len(iss) == 1 and iss[0]["number"] == 1)
check("list_issues flattens labels", iss[0]["labels"] == ["bug", "p1"])

# === get_pr: mergeability surfaced ===
gh = mk([("GET", "/pulls/5", 200,
         {"number": 5, "title": "t", "state": "open", "merged": False,
          "mergeable": True, "mergeable_state": "clean", "draft": False,
          "user": {"login": "o"}, "head": {"ref": "dev"}, "base": {"ref": "main"},
          "commits": 2, "additions": 10, "deletions": 1, "changed_files": 3, "html_url": "u"})])
pr = gh.get_pr("o", "r", 5)
check("get_pr mergeable", pr["mergeable"] is True and pr["merged"] is False)
check("get_pr changed_files", pr["changed_files"] == 3)

# === merge_pr (raw PUT) ===
gh = mk([("PUT", "/pulls/5/merge", 200,
         {"merged": True, "sha": "deadbeef", "message": "Pull Request successfully merged"})])
mr = gh.merge_pr("o", "r", 5)
check("merge_pr merged", mr["merged"] is True and mr["sha"] == "deadbeef")

# merge error (405 not mergeable) surfaces as ServiceError
gh = mk([("PUT", "/pulls/9/merge", 405, {"message": "Pull Request is not mergeable"})])
try:
    gh.merge_pr("o", "r", 9); check("merge_pr 405 -> error", False)
except ServiceError as e:
    check("merge_pr 405 -> error", e.status_code == 502)


# ---- merge_pr_guarded: the confirm gate (high-level fake) ----
class FakeGH:
    def __init__(self, pr):
        self._pr = pr
        self.merge_calls = []
    def get_pr(self, o, r, n):
        return dict(self._pr)
    def merge_pr(self, o, r, n, method="merge", commit_title=None, commit_message=None):
        self.merge_calls.append((o, r, n, method))
        return {"merged": True, "sha": "s", "message": "ok"}

BASE_PR = {"number": 7, "title": "My PR", "state": "open", "merged": False,
           "mergeable": True, "mergeable_state": "clean", "draft": False,
           "head": "dev", "base": "main", "changed_files": 2, "html_url": "u"}

# confirm=False -> preview only, NO merge call
fg = FakeGH(BASE_PR)
res = merge_pr_guarded(fg, "o", "r", 7, confirm=False)
check("preview does not merge", res["merged"] is False and res.get("confirm_required") is True)
check("preview makes no merge call", fg.merge_calls == [])
check("preview echoes PR number+title", '#7' in res["message"] and "My PR" in res["message"])
check("preview carries head->base", res["preview"]["head"] == "dev" and res["preview"]["base"] == "main")

# confirm=True -> actually merges, exactly once
fg2 = FakeGH(BASE_PR)
res2 = merge_pr_guarded(fg2, "o", "r", 7, confirm=True, method="squash")
check("confirm merges", res2["merged"] is True and res2.get("confirmed") is True)
check("confirm makes exactly one merge call", len(fg2.merge_calls) == 1)
check("confirm passes method through", fg2.merge_calls[0][3] == "squash")

# already-merged -> no-op even with confirm=True
fg3 = FakeGH({**BASE_PR, "merged": True})
res3 = merge_pr_guarded(fg3, "o", "r", 7, confirm=True)
check("already merged is a no-op", res3["merged"] is False and res3.get("already_merged") is True)
check("already merged makes no merge call", fg3.merge_calls == [])


# ---- project_context: composition + graceful degrade ----
class ProjectGH:
    def get_repo(self, o, r):
        return {"full_name": f"{o}/{r}", "default_branch": "main", "description": "d"}
    def recent_commits(self, o, r, limit=5):
        return [{"sha": "a", "message": "m"}]
    def list_prs(self, o, r, state="open"):
        return [{"number": 1, "title": "pr"}]
    def list_issues(self, o, r, state="open"):
        return [{"number": 2, "title": "iss"}]
    def get_readme(self, o, r):
        return {"content": "# Title\nbody text"}

pc = a.project_context(Settings(), "o", "r", _client=ProjectGH())
check("project_context meta", pc["meta"]["full_name"] == "o/r")
check("project_context commits", len(pc["recent_commits"]) == 1)
check("project_context open_prs", pc["open_prs"][0]["title"] == "pr")
check("project_context open_issues", pc["open_issues"][0]["number"] == 2)
check("project_context readme excerpt", pc["readme_excerpt"].startswith("# Title"))
check("project_context nothing unavailable", pc["unavailable"] == [])

class PartialGH(ProjectGH):
    def list_issues(self, o, r, state="open"):
        raise ServiceError("boom", status_code=502)

pc2 = a.project_context(Settings(), "o", "r", _client=PartialGH())
check("project_context degrades one source", any(u["source"] == "open_issues" for u in pc2["unavailable"]))
check("project_context keeps the good sources", pc2["recent_commits"] is not None and pc2["meta"] is not None)


# ---- HTTP layer via TestClient (no tokens -> 503; manifest counts) ----
for k in ("GITHUB_TOKEN", "GITHUB_GIST_TOKEN", "GITHUB_REPO_TOKEN", "SERVICE_API_KEY"):
    os.environ.pop(k, None)
from app import config as _cfg
_cfg.get_settings.cache_clear()
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)

gf = c.post("/api/github/get-file", json={"owner": "o", "repo": "r", "path": "README.md"})
check("get-file 503 without token", gf.status_code == 503)
mp = c.post("/api/github/merge-pr", json={"owner": "o", "repo": "r", "number": 1})
check("merge-pr 503 without token", mp.status_code == 503)
pcx = c.post("/api/alistair/project-context", json={"owner": "o", "repo": "r"})
check("project-context 503 without token", pcx.status_code == 503)

# bad body still validates (422), proving the route + schema are wired
bad = c.post("/api/github/merge-pr", json={"owner": "o", "repo": "r"})  # missing number
check("merge-pr validates body (422)", bad.status_code == 422)

mani = c.get("/api/manifest").json()
check("manifest github has 9 tools", mani["counts"].get("github") == 9)
check("manifest alistair has 5 tools", mani["counts"].get("alistair") == 5)
names = [t["path"] for t in mani["function_apis"]["github"]]
check("manifest lists merge-pr", "/api/github/merge-pr" in names)
check("manifest lists get-file", "/api/github/get-file" in names)
merge_desc = next(t["description"] for t in mani["function_apis"]["github"] if t["path"] == "/api/github/merge-pr")
check("merge-pr description warns about confirm", "confirm=true" in merge_desc)
root = c.get("/").json()
check("root reports github_read flag", "github_read" in root["configured"])

# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
