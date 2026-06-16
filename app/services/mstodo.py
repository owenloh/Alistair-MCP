"""Microsoft To Do in-tray — list / add / delete / done on ONE hard-scoped list.

Port of the microsoft-todo-intray skill's scripts/intray.py:
  * The MS refresh token lives in a private GitHub gist (via GitHubClient).
  * Each call reads it, exchanges it for an access token, and writes back the
    fresh refresh token Microsoft returns, so it self-renews.
  * Every Graph call targets the single configured list id — never another list.
"""
from __future__ import annotations

import httpx

from . import ServiceError
from ..config import Settings
from .github import GitHubClient

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/Tasks.ReadWrite offline_access"
_TIMEOUT = httpx.Timeout(30.0)


def _authority(settings: Settings) -> str:
    return f"https://login.microsoftonline.com/{settings.ms_tenant}/oauth2/v2.0"


def _require_config(settings: Settings) -> None:
    missing = [
        name
        for name, val in (
            ("MS_CLIENT_ID", settings.ms_client_id),
            ("MS_TODO_LIST_ID", settings.ms_todo_list_id),
            ("GITHUB_TOKEN", settings.github_token),
            ("GIST_ID", settings.gist_id),
        )
        if not val
    ]
    if missing:
        raise ServiceError(
            "Missing config value(s): " + ", ".join(missing), status_code=503
        )


def _access_token(settings: Settings, gh: GitHubClient) -> str:
    refresh = gh.read_gist_file(settings.gist_id, settings.gist_filename)
    try:
        resp = httpx.post(
            _authority(settings) + "/token",
            data={
                "client_id": settings.ms_client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "scope": SCOPE,
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise ServiceError(f"Network error reaching Microsoft: {e}", status_code=502)
    if resp.status_code != 200:
        raise ServiceError(
            "Could not obtain a Microsoft access token. The refresh token may be "
            "expired or revoked — re-run get_token.py to reseed the gist.",
            status_code=502,
            detail=_safe_body(resp),
        )
    tok = resp.json()
    access = tok.get("access_token")
    if not access:
        raise ServiceError("Microsoft returned no access_token.", status_code=502)

    new_refresh = tok.get("refresh_token")
    if new_refresh and new_refresh != refresh:
        gh.write_gist_file(settings.gist_id, settings.gist_filename, new_refresh)
    return access


def _tasks_url(settings: Settings, suffix: str = "") -> str:
    return f"{GRAPH}/me/todo/lists/{settings.ms_todo_list_id}/tasks{suffix}"


def _graph(method: str, url: str, token: str, json_body: dict | None = None):
    try:
        resp = httpx.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise ServiceError(f"Network error reaching Microsoft Graph: {e}", status_code=502)
    if resp.status_code >= 400:
        raise ServiceError(
            f"Microsoft Graph returned HTTP {resp.status_code} for {method} tasks.",
            status_code=502,
            detail=_safe_body(resp),
        )
    if not resp.content:
        return {}
    return resp.json()


# ---- operations ----

def _list(settings: Settings, token: str) -> dict:
    data = _graph(
        "GET",
        _tasks_url(settings, "?$top=100&$orderby=createdDateTime%20desc"),
        token,
    )
    items = [
        {"id": t["id"], "title": t.get("title", "(untitled)"), "status": t.get("status")}
        for t in data.get("value", [])
        if t.get("status") != "completed"
    ]
    return {"action": "list", "count": len(items), "items": items}


def _add(settings: Settings, token: str, title: str | None) -> dict:
    title = (title or "").strip()
    if not title:
        raise ServiceError("action=add requires a non-empty 'title'.", status_code=400)
    t = _graph("POST", _tasks_url(settings), token, json_body={"title": title})
    return {"action": "add", "added": {"id": t.get("id"), "title": t.get("title")}}


def _delete(settings: Settings, token: str, task_id: str | None) -> dict:
    if not task_id:
        raise ServiceError("action=delete requires 'task_id'.", status_code=400)
    _graph("DELETE", _tasks_url(settings, "/" + task_id), token)
    return {"action": "delete", "deleted": task_id}


def _done(settings: Settings, token: str, task_id: str | None) -> dict:
    if not task_id:
        raise ServiceError("action=done requires 'task_id'.", status_code=400)
    t = _graph(
        "PATCH",
        _tasks_url(settings, "/" + task_id),
        token,
        json_body={"status": "completed"},
    )
    return {"action": "done", "completed": {"id": task_id, "title": t.get("title")}}


def run(settings: Settings, action: str, title: str | None, task_id: str | None) -> dict:
    _require_config(settings)
    with GitHubClient(settings.github_token) as gh:
        token = _access_token(settings, gh)
        if action == "list":
            return _list(settings, token)
        if action == "add":
            return _add(settings, token, title)
        if action == "delete":
            return _delete(settings, token, task_id)
        if action == "done":
            return _done(settings, token, task_id)
    raise ServiceError(f"Unknown action '{action}'.", status_code=400)


def _safe_body(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            if "error_description" in data:
                return str(data["error_description"])[:300]
            if "error" in data:
                err = data["error"]
                return str(err.get("message", err) if isinstance(err, dict) else err)[:300]
        return str(data)[:300]
    except Exception:
        return resp.text[:300]
