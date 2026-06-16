"""POST /api/github/push-file — GitHub extension.

This is the "future-proofing" route called out in the spec. It is fully wired
and functional: the GitHubClient service already powers the in-tray's gist
storage, so exposing a repo write was a thin router on top. Add more GitHub
routes here the same way.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..models import PushFileRequest
from ..security import require_api_key
from ..services import ServiceError
from ..services.github import GitHubClient

router = APIRouter(
    prefix="/api/github",
    tags=["github"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "/push-file",
    summary="Create or update a file in a repo",
    description="Commits content to owner/repo at path (create or update). "
    "Uses GITHUB_TOKEN.",
)
def push_file(body: PushFileRequest) -> dict:
    settings = get_settings()
    if not settings.github_token:
        raise ServiceError("GITHUB_TOKEN is not configured.", status_code=503)
    with GitHubClient(settings.github_token) as gh:
        return gh.push_file(
            owner=body.owner,
            repo=body.repo,
            path=body.path,
            content=body.content,
            message=body.message,
            branch=body.branch,
        )
