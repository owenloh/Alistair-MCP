"""Reusable GitHub REST client.

Two responsibilities today:
  * gist read/write — backs the Microsoft in-tray's self-rolling refresh token.
  * push_file       — repo Contents API, ready for the future
                      POST /api/github/push-file route.

Both share one authenticated client, so adding GitHub-backed endpoints later is
a matter of calling an existing method.
"""
from __future__ import annotations

import base64

import httpx

from . import ServiceError

API_ROOT = "https://api.github.com"
_TIMEOUT = httpx.Timeout(30.0)


class GitHubClient:
    def __init__(self, token: str):
        if not token:
            raise ServiceError("GITHUB_TOKEN is not configured.", status_code=503)
        self._client = httpx.Client(
            timeout=_TIMEOUT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "alistair-skills-api",
            },
        )

    # ---- lifecycle ----
    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _raise(self, resp: httpx.Response, what: str) -> None:
        raise ServiceError(
            f"GitHub API error ({what}): HTTP {resp.status_code}.",
            status_code=502,
            detail=_safe_body(resp),
        )

    # ---- gists (token storage) ----
    def read_gist_file(self, gist_id: str, filename: str) -> str:
        try:
            resp = self._client.get(f"{API_ROOT}/gists/{gist_id}")
        except httpx.HTTPError as e:
            raise ServiceError(f"Network error reaching GitHub: {e}", status_code=502)
        if resp.status_code != 200:
            self._raise(resp, "read gist")
        files = resp.json().get("files", {})
        if filename not in files:
            raise ServiceError(
                f"Gist {gist_id} has no file named '{filename}'.",
                status_code=502,
            )
        f = files[filename]
        if f.get("truncated"):
            # Large file — content is omitted; fetch the raw blob directly.
            raw = httpx.get(
                f["raw_url"],
                headers={"User-Agent": "alistair-skills-api"},
                timeout=_TIMEOUT,
            )
            if raw.status_code != 200:
                self._raise(raw, "read gist raw")
            return raw.text.strip()
        return (f.get("content") or "").strip()

    def write_gist_file(self, gist_id: str, filename: str, content: str) -> None:
        try:
            resp = self._client.patch(
                f"{API_ROOT}/gists/{gist_id}",
                json={"files": {filename: {"content": content}}},
            )
        except httpx.HTTPError as e:
            raise ServiceError(f"Network error reaching GitHub: {e}", status_code=502)
        if resp.status_code != 200:
            self._raise(resp, "write gist")

    # ---- repo contents (future push-file route) ----
    def push_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str | None = None,
    ) -> dict:
        """Create or update a file via the Contents API. Returns commit + content info."""
        url = f"{API_ROOT}/repos/{owner}/{repo}/contents/{path}"

        # If the file already exists we must pass its blob sha to update it.
        sha = None
        get_params = {"ref": branch} if branch else None
        try:
            existing = self._client.get(url, params=get_params)
        except httpx.HTTPError as e:
            raise ServiceError(f"Network error reaching GitHub: {e}", status_code=502)
        if existing.status_code == 200:
            sha = existing.json().get("sha")
        elif existing.status_code not in (404,):
            self._raise(existing, "stat file")

        body: dict = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if branch:
            body["branch"] = branch
        if sha:
            body["sha"] = sha

        try:
            resp = self._client.put(url, json=body)
        except httpx.HTTPError as e:
            raise ServiceError(f"Network error reaching GitHub: {e}", status_code=502)
        if resp.status_code not in (200, 201):
            self._raise(resp, "push file")

        data = resp.json()
        commit = data.get("commit", {})
        content_info = data.get("content", {})
        return {
            "created": resp.status_code == 201,
            "updated": resp.status_code == 200,
            "path": content_info.get("path", path),
            "branch": branch,
            "commit_sha": commit.get("sha"),
            "html_url": content_info.get("html_url"),
        }

    # ---- generic read helper ----
    def _get_json(self, url: str, what: str, params: dict | None = None):
        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as e:
            raise ServiceError(f"Network error reaching GitHub: {e}", status_code=502)
        if resp.status_code != 200:
            self._raise(resp, what)
        return resp.json()

    @staticmethod
    def _decode(node: dict) -> str:
        """Decode a Contents-API blob (base64 or already-plain)."""
        raw = node.get("content", "") or ""
        if node.get("encoding") == "base64":
            return base64.b64decode(raw).decode("utf-8")  # may raise; callers handle
        return raw

    # ---- repo reads ----
    def get_repo(self, owner: str, repo: str) -> dict:
        d = self._get_json(f"{API_ROOT}/repos/{owner}/{repo}", "get repo")
        return {
            "full_name": d.get("full_name"),
            "description": d.get("description"),
            "default_branch": d.get("default_branch"),
            "private": d.get("private"),
            "pushed_at": d.get("pushed_at"),
            "open_issues_count": d.get("open_issues_count"),
            "language": d.get("language"),
            "html_url": d.get("html_url"),
        }

    def get_file(self, owner: str, repo: str, path: str, ref: str | None = None) -> dict:
        params = {"ref": ref} if ref else None
        d = self._get_json(
            f"{API_ROOT}/repos/{owner}/{repo}/contents/{path}", "get file", params
        )
        if isinstance(d, list):
            raise ServiceError(f"'{path}' is a directory, not a file — use list-tree.", status_code=400)
        if d.get("type") != "file":
            raise ServiceError(f"'{path}' is not a readable file (type={d.get('type')}).", status_code=400)
        try:
            text = self._decode(d)
        except (ValueError, UnicodeDecodeError):
            raise ServiceError(f"'{path}' is binary or not UTF-8 decodable.", status_code=415)
        return {
            "path": d.get("path", path), "sha": d.get("sha"), "size": d.get("size"),
            "ref": ref, "content": text, "html_url": d.get("html_url"),
        }

    def list_tree(self, owner: str, repo: str, ref: str | None = None, recursive: bool = True) -> dict:
        ref = ref or self.get_repo(owner, repo)["default_branch"]
        params = {"recursive": "1"} if recursive else None
        d = self._get_json(f"{API_ROOT}/repos/{owner}/{repo}/git/trees/{ref}", "list tree", params)
        entries = [
            {"path": t.get("path"), "type": t.get("type"), "size": t.get("size")}
            for t in d.get("tree", [])
        ]
        return {"ref": ref, "truncated": d.get("truncated", False), "count": len(entries), "entries": entries}

    def search_code(self, query: str, owner: str | None = None, repo: str | None = None, limit: int = 20) -> dict:
        q = query
        if owner and repo:
            q = f"{query} repo:{owner}/{repo}"
        elif owner:
            q = f"{query} user:{owner}"
        d = self._get_json(f"{API_ROOT}/search/code", "search code", {"q": q, "per_page": min(limit, 100)})
        items = [
            {"path": it.get("path"), "repo": (it.get("repository") or {}).get("full_name"),
             "html_url": it.get("html_url")}
            for it in d.get("items", [])
        ]
        return {"query": q, "total_count": d.get("total_count", 0), "items": items}

    def recent_commits(self, owner: str, repo: str, branch: str | None = None, limit: int = 10) -> list[dict]:
        params: dict = {"per_page": min(limit, 100)}
        if branch:
            params["sha"] = branch
        d = self._get_json(f"{API_ROOT}/repos/{owner}/{repo}/commits", "recent commits", params)
        out = []
        for c in d:
            commit = c.get("commit", {})
            author = commit.get("author") or {}
            out.append({
                "sha": (c.get("sha") or "")[:10],
                "message": (commit.get("message") or "").split("\n", 1)[0][:120],
                "author": author.get("name"), "date": author.get("date"),
                "html_url": c.get("html_url"),
            })
        return out

    def list_prs(self, owner: str, repo: str, state: str = "open", limit: int = 20) -> list[dict]:
        d = self._get_json(
            f"{API_ROOT}/repos/{owner}/{repo}/pulls", "list prs",
            {"state": state, "per_page": min(limit, 100)},
        )
        return [{
            "number": p.get("number"), "title": p.get("title"), "state": p.get("state"),
            "draft": p.get("draft"), "user": (p.get("user") or {}).get("login"),
            "head": (p.get("head") or {}).get("ref"), "base": (p.get("base") or {}).get("ref"),
            "html_url": p.get("html_url"),
        } for p in d]

    def list_issues(self, owner: str, repo: str, state: str = "open", limit: int = 20) -> list[dict]:
        d = self._get_json(
            f"{API_ROOT}/repos/{owner}/{repo}/issues", "list issues",
            {"state": state, "per_page": min(limit, 100)},
        )
        out = []
        for i in d:
            if "pull_request" in i:  # GitHub returns PRs in /issues; exclude them
                continue
            out.append({
                "number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
                "user": (i.get("user") or {}).get("login"),
                "labels": [lb.get("name") for lb in i.get("labels", [])],
                "comments": i.get("comments"), "html_url": i.get("html_url"),
            })
        return out

    def get_pr(self, owner: str, repo: str, number: int) -> dict:
        p = self._get_json(f"{API_ROOT}/repos/{owner}/{repo}/pulls/{number}", "get pr")
        return {
            "number": p.get("number"), "title": p.get("title"), "state": p.get("state"),
            "merged": bool(p.get("merged")), "mergeable": p.get("mergeable"),
            "mergeable_state": p.get("mergeable_state"), "draft": p.get("draft"),
            "user": (p.get("user") or {}).get("login"),
            "head": (p.get("head") or {}).get("ref"), "base": (p.get("base") or {}).get("ref"),
            "commits": p.get("commits"), "additions": p.get("additions"),
            "deletions": p.get("deletions"), "changed_files": p.get("changed_files"),
            "html_url": p.get("html_url"),
        }

    def get_readme(self, owner: str, repo: str, ref: str | None = None) -> dict:
        params = {"ref": ref} if ref else None
        d = self._get_json(f"{API_ROOT}/repos/{owner}/{repo}/readme", "get readme", params)
        try:
            text = self._decode(d)
        except (ValueError, UnicodeDecodeError):
            text = ""
        return {"path": d.get("path"), "content": text, "html_url": d.get("html_url")}

    def merge_pr(self, owner: str, repo: str, number: int, method: str = "merge",
                 commit_title: str | None = None, commit_message: str | None = None) -> dict:
        body: dict = {"merge_method": method}
        if commit_title:
            body["commit_title"] = commit_title
        if commit_message:
            body["commit_message"] = commit_message
        try:
            resp = self._client.put(
                f"{API_ROOT}/repos/{owner}/{repo}/pulls/{number}/merge", json=body
            )
        except httpx.HTTPError as e:
            raise ServiceError(f"Network error reaching GitHub: {e}", status_code=502)
        if resp.status_code != 200:
            # 405 = not mergeable, 409 = sha mismatch/conflict — surface GitHub's message
            self._raise(resp, "merge pr")
        data = resp.json()
        return {"merged": bool(data.get("merged")), "sha": data.get("sha"), "message": data.get("message")}


def merge_pr_guarded(
    gh: "GitHubClient", owner: str, repo: str, number: int, *,
    confirm: bool = False, method: str = "merge",
    commit_title: str | None = None, commit_message: str | None = None,
) -> dict:
    """Two-step merge guard — never merge by voice silently.

    With confirm=False (the default) this returns a PREVIEW of exactly what would
    be merged and does NOT touch the PR. Only confirm=True performs the merge.
    Merging is near-irreversible, so the safe default is to do nothing.
    """
    pr = gh.get_pr(owner, repo, number)
    preview = {
        "number": pr["number"], "title": pr["title"], "state": pr["state"],
        "head": pr["head"], "base": pr["base"], "merged": pr["merged"],
        "mergeable": pr["mergeable"], "mergeable_state": pr["mergeable_state"],
        "draft": pr["draft"], "changed_files": pr["changed_files"], "html_url": pr["html_url"],
    }
    if pr["merged"]:
        return {"merged": False, "already_merged": True, "preview": preview,
                "message": f"PR #{number} is already merged; nothing to do."}
    if not confirm:
        return {
            "merged": False, "confirm_required": True, "preview": preview,
            "message": (f"About to merge PR #{number} \"{pr['title']}\" "
                        f"({pr['head']} -> {pr['base']}) via '{method}'. This is "
                        "near-irreversible. Re-call with confirm=true to proceed."),
        }
    result = gh.merge_pr(owner, repo, number, method=method,
                         commit_title=commit_title, commit_message=commit_message)
    return {"merged": bool(result.get("merged")), "confirmed": True,
            "preview": preview, "result": result}


def _safe_body(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        # surface GitHub's message but never echo request headers/tokens
        return str(data.get("message", data))[:300]
    except Exception:
        return resp.text[:300]
