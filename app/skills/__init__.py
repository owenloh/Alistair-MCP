"""Skill description registry.

Skills are "description APIs": they return rule sets (no code) telling Claude
what to do. Each lives as a JSON file in ./data/<slug>.json. Drop in a new file
and it is served automatically — no code change needed.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data"


@lru_cache
def list_slugs() -> list[str]:
    if not _DATA.exists():
        return []
    return sorted(p.stem for p in _DATA.glob("*.json"))


@lru_cache
def load_skill(slug: str) -> dict | None:
    # Guard against path traversal: only serve known data files.
    if slug not in set(list_slugs()):
        return None
    return json.loads((_DATA / f"{slug}.json").read_text(encoding="utf-8"))


@lru_cache
def skill_index() -> list[dict]:
    """Lightweight catalogue of skills: slug, name, description, url.

    Lets a caller discover what each skill is for (and how to fetch its rules)
    without pulling every full instruction body.
    """
    out: list[dict] = []
    for slug in list_slugs():
        data = load_skill(slug) or {}
        out.append({
            "slug": slug,
            "name": data.get("name", slug),
            "description": data.get("description", ""),
            "retrieve_with": f"get_skill('{slug}')",  # MCP: fetch the full procedure on demand
            "url": f"/api/skill/{slug}",               # REST equivalent
        })
    return out
