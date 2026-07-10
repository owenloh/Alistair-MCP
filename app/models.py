"""Request/response schemas shared across routers."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class IntrayRequest(BaseModel):
    """Body for POST /api/intray."""

    action: Literal["list", "add", "delete", "done"]
    title: str | None = Field(
        default=None, description="Item title — required for action=add."
    )
    task_id: str | None = Field(
        default=None, description="Task id — required for action=delete or done."
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"action": "list"},
                {"action": "add", "title": "Buy oat milk"},
                {"action": "done", "task_id": "AAMkAD..."},
                {"action": "delete", "task_id": "AAMkAD..."},
            ]
        }
    }


class OpenLinkRequest(BaseModel):
    """Body for POST /api/media/open-link."""

    url: str = Field(description="The web link to open/fetch (http(s); a bare host is prefixed with https).")
    max_chars: int = Field(
        default=4000, ge=200, le=20000,
        description="Max characters of body text to return in the excerpt.",
    )


class TranscribeRequest(BaseModel):
    """Body for POST /api/media/transcribe."""

    url: str = Field(description="A YouTube or Instagram video / reel / short URL.")
    lang: str | None = Field(
        default=None,
        description="Preferred caption language code (e.g. 'en'). Optional; best available otherwise.",
    )


class PushFileRequest(BaseModel):
    """Body for POST /api/github/push-file (the future-proofing extension)."""

    owner: str = Field(description="Repository owner (user or org).")
    repo: str = Field(description="Repository name.")
    path: str = Field(description="Path of the file within the repo.")
    content: str = Field(description="UTF-8 file content (plain text).")
    message: str = Field(description="Commit message.")
    branch: str | None = Field(
        default=None, description="Target branch. Defaults to the repo default."
    )


class _RepoRequest(BaseModel):
    """Shared owner/repo base for the GitHub read endpoints."""

    owner: str = Field(description="Repository owner (user or org).")
    repo: str = Field(description="Repository name.")


class GetFileRequest(_RepoRequest):
    path: str = Field(description="Path of the file within the repo.")
    ref: str | None = Field(default=None, description="Branch/tag/sha. Defaults to the repo default branch.")


class ListTreeRequest(_RepoRequest):
    ref: str | None = Field(default=None, description="Branch/tag/sha. Defaults to the repo default branch.")
    recursive: bool = Field(default=True, description="Recurse into all subdirectories.")


class ListMyReposRequest(BaseModel):
    """Body for POST /api/github/list-my-repos (the account-aware enumeration)."""

    visibility: Literal["all", "public", "private"] = "all"
    affiliation: str | None = Field(
        default=None,
        description="Optional comma-separated affiliations: owner,collaborator,"
        "organization_member. Defaults to all three.",
    )
    sort: Literal["created", "updated", "pushed", "full_name"] = "pushed"
    limit: int = Field(default=30, ge=1, le=100)


class SearchCodeRequest(BaseModel):
    query: str = Field(description="Code search query (GitHub code-search syntax).")
    owner: str | None = Field(default=None, description="Scope to this owner (user/org).")
    repo: str | None = Field(default=None, description="Scope to this repo (requires owner).")
    limit: int = Field(default=20, ge=1, le=100)


class RecentCommitsRequest(_RepoRequest):
    branch: str | None = Field(default=None, description="Branch/sha. Defaults to the repo default branch.")
    limit: int = Field(default=10, ge=1, le=100)


class ListPullsRequest(_RepoRequest):
    state: Literal["open", "closed", "all"] = "open"
    limit: int = Field(default=20, ge=1, le=100)


class ListIssuesRequest(_RepoRequest):
    state: Literal["open", "closed", "all"] = "open"
    limit: int = Field(default=20, ge=1, le=100)


class GetPullRequest(_RepoRequest):
    number: int = Field(description="Pull request number.")


class MergePullRequest(_RepoRequest):
    number: int = Field(description="Pull request number.")
    confirm: bool = Field(
        default=False,
        description="MUST be true to actually merge. With false (default) this only "
        "returns a preview of what would be merged and changes nothing.",
    )
    method: Literal["merge", "squash", "rebase"] = "merge"
    commit_title: str | None = None
    commit_message: str | None = None
