"""POST /api/intray — Microsoft To Do in-tray (list / add / delete / done)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..models import IntrayRequest
from ..security import require_api_key
from ..services import mstodo as mstodo_service

router = APIRouter(
    prefix="/api/intray",
    tags=["intray"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "",
    summary="Manage the Microsoft To Do in-tray",
    description="action=list returns open items; add needs title; "
    "delete/done need task_id.",
)
def intray(body: IntrayRequest) -> dict:
    return mstodo_service.run(get_settings(), body.action, body.title, body.task_id)
