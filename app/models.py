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
