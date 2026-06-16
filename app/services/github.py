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


def _safe_body(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        # surface GitHub's message but never echo request headers/tokens
        return str(data.get("message", data))[:300]
    except Exception:
        return resp.text[:300]
