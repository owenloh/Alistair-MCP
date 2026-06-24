"""POST /api/github/push-file — GitHub extension.

This is the "future-proofing" route called out in the spec. It is fully wired
and functional: the GitHubClient service already powers the in-tray's gist
storage, so exposing a repo write was a thin router on top. Add more GitHub
routes here the same way.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..models import (
    GetFileRequest,
    GetPullRequest,
    ListIssuesRequest,
    ListPullsRequest,
    ListTreeRequest,
    MergePullRequest,
    PushFileRequest,
    RecentCommitsRequest,
    SearchCodeRequest,
)
from ..security import require_api_key
from ..services import ServiceError
from ..services.github import GitHubClient, merge_pr_guarded

router = APIRouter(
    prefix="/api/github",
    tags=["github"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "/push-file",
    summary="Create or update a file in a repo",
    description="Commits content to owner/repo at path (create or update). "
    "Uses GITHUB_GIST_TOKEN.",
)
def push_file(body: PushFileRequest) -> dict:
    settings = get_settings()
    if not settings.github_gist_token:
        raise ServiceError("GITHUB_GIST_TOKEN is not configured.", status_code=503)
    with GitHubClient(settings.github_gist_token) as gh:
        return gh.push_file(
            owner=body.owner,
            repo=body.repo,
            path=body.path,
            content=body.content,
            message=body.message,
            branch=body.branch,
        )


def _read_token() -> str:
    """Token for the read + merge endpoints (dedicated repo PAT, else the gist one)."""
    token = get_settings().github_read_token
    if not token:
        raise ServiceError(
            "GITHUB_REPO_TOKEN (or GITHUB_GIST_TOKEN) is not configured.", status_code=503
        )
    return token


@router.post(
    "/get-file",
    summary="Read a file from a repo",
    description="Return the UTF-8 contents of one file in owner/repo at an optional ref "
    "(branch/tag/sha). Read-only. Errors clearly on directories or binary files.",
)
def get_file(body: GetFileRequest) -> dict:
    with GitHubClient(_read_token()) as gh:
        return gh.get_file(body.owner, body.repo, body.path, body.ref)


@router.post(
    "/list-tree",
    summary="List a repo's file tree",
    description="Return the file/dir tree of owner/repo at an optional ref. Read-only. "
    "Use this to discover paths before get-file.",
)
def list_tree(body: ListTreeRequest) -> dict:
    with GitHubClient(_read_token()) as gh:
        return gh.list_tree(body.owner, body.repo, body.ref, body.recursive)


@router.post(
    "/search-code",
    summary="Search code",
    description="GitHub code search, optionally scoped to an owner and/or repo. Read-only. "
    "Returns matching file paths and links.",
)
def search_code(body: SearchCodeRequest) -> dict:
    with GitHubClient(_read_token()) as gh:
        return gh.search_code(body.query, body.owner, body.repo, body.limit)


@router.post(
    "/recent-commits",
    summary="Recent commits",
    description="Latest commits on owner/repo (optionally a branch). Read-only. One-line "
    "messages, author and date.",
)
def recent_commits(body: RecentCommitsRequest) -> dict:
    with GitHubClient(_read_token()) as gh:
        return {"commits": gh.recent_commits(body.owner, body.repo, body.branch, body.limit)}


@router.post(
    "/list-prs",
    summary="List pull requests",
    description="Open (or closed/all) pull requests on owner/repo. Read-only. Number, title, "
    "head -> base, author, draft flag.",
)
def list_prs(body: ListPullsRequest) -> dict:
    with GitHubClient(_read_token()) as gh:
        return {"pull_requests": gh.list_prs(body.owner, body.repo, body.state, body.limit)}


@router.post(
    "/list-issues",
    summary="List issues",
    description="Open (or closed/all) issues on owner/repo (pull requests excluded). Read-only.",
)
def list_issues(body: ListIssuesRequest) -> dict:
    with GitHubClient(_read_token()) as gh:
        return {"issues": gh.list_issues(body.owner, body.repo, body.state, body.limit)}


@router.post(
    "/get-pr",
    summary="Get one pull request",
    description="Full detail for a single PR: state, mergeability, head/base, diff size. Read-only.",
)
def get_pr(body: GetPullRequest) -> dict:
    with GitHubClient(_read_token()) as gh:
        return gh.get_pr(body.owner, body.repo, body.number)


@router.post(
    "/merge-pr",
    summary="Merge a PR (requires explicit confirm)",
    description="Merge a pull request. SENSITIVE and near-irreversible, so it never merges by "
    "voice silently: with confirm=false (default) it ONLY returns a preview of exactly what "
    "would be merged and changes nothing. Re-call with confirm=true to actually merge. method "
    "is merge/squash/rebase.",
)
def merge_pr(body: MergePullRequest) -> dict:
    with GitHubClient(_read_token()) as gh:
        return merge_pr_guarded(
            gh, body.owner, body.repo, body.number,
            confirm=body.confirm, method=body.method,
            commit_title=body.commit_title, commit_message=body.commit_message,
        )
