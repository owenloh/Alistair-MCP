"""Personalisation: substitute neutral placeholders with the user's own values.

The source ships with NO hardcoded personal data. User-facing text uses neutral
placeholder tokens (e.g. ``{user}`` for the user's name, ``{references_tray_id}``
for a Notion page id). This module resolves those tokens from settings at the
model/HTTP boundary, so a fork just sets its own env vars and every persona string,
tool description and skill body reads correctly — nothing private is baked in.
"""
from __future__ import annotations

from typing import Any

from .config import Settings


def token_map(settings: Settings) -> dict[str, str]:
    """Placeholder -> configured value. A blank name falls back to the generic "the
    user"; missing ids resolve to '' (the feature that needs them is simply off)."""
    return {
        "{user}": (settings.user_name or "").strip() or "the user",
        "{timezone}": settings.calendar_timezone or "UTC",
        "{projects_db_id}": settings.projects_db_id or "",
        "{actions_db_id}": settings.actions_db_id or "",
        "{references_tray_id}": settings.references_tray_page_id or "",
        "{library_hub_id}": settings.library_hub_page_id or "",
        "{briefing_id}": settings.briefing_page_id or "",
    }


def apply(text: str, mapping: dict[str, str]) -> str:
    """Replace every placeholder in one string. No-op for text without a token."""
    if not text or "{" not in text:
        return text
    for token, value in mapping.items():
        if token in text:
            text = text.replace(token, value)
    return text


def personalize(obj: Any, settings: Settings) -> Any:
    """Recursively substitute placeholders in strings inside dicts/lists/tuples.

    Returns a new structure; leaves non-string leaves untouched. Safe to run over
    tool results and skill bodies at the boundary before they reach the model.
    """
    mapping = token_map(settings)
    return _walk(obj, mapping)


def _walk(obj: Any, mapping: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return apply(obj, mapping)
    if isinstance(obj, dict):
        return {k: _walk(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, mapping) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(v, mapping) for v in obj)
    return obj
