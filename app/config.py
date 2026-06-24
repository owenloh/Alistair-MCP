"""Central settings — every secret comes from an environment variable, never hardcoded.

Defaults are provided only for non-secret identifiers (database ids, timezone,
API versions). All tokens default to None and the relevant endpoint returns a
clear 503 if its secret is missing, so the app still boots (and the skill
endpoints still work) even when connector secrets are absent.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Service ----
    railway_env: str = "development"
    service_api_key: str | None = None

    # ---- Notion ----
    notion_token: str | None = None
    notion_version: str = "2022-06-28"
    projects_db_id: str = "b9c0cd8cfa6c46d195ed87d7ef97d971"
    actions_db_id: str = "2ebc58c5861747488021fcc2a37d3a97"

    # ---- Google Calendar ----
    google_calendar_token: str | None = None
    google_calendar_id: str = "primary"
    # Accept the bundle's TIMEZONE name (and CALENDAR_TIMEZONE as an alias).
    calendar_timezone: str = Field(
        default="Europe/London",
        validation_alias=AliasChoices("TIMEZONE", "CALENDAR_TIMEZONE"),
    )
    # Auto-detect the current Google Calendar timezone per call (follows travel).
    timezone_auto: bool = True
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None

    # ---- Microsoft To Do in-tray ----
    ms_client_id: str | None = None
    ms_todo_list_id: str | None = None
    ms_tenant: str = "consumers"

    # ---- GitHub (gist storage for the MS token; also the future push-file route) ----
    github_token: str | None = None
    gist_id: str | None = None
    gist_filename: str = "mstodo_refresh_token"

    # ---- Memory (SQLite append-only event log) ----
    # Railway sets RAILWAY_VOLUME_MOUNT_PATH automatically when a volume is
    # attached; the DB lives there so it survives redeploys. With no volume the
    # DB falls back to ./data (ephemeral — works, but lost on redeploy).
    railway_volume_mount_path: str | None = None
    memory_db_path: str | None = None  # explicit override for the SQLite file
    memory_tau_days: float = 30.0      # decay constant (half-life ~= 21d)
    memory_core_relevance: int = 5     # relevance >= this is pinned, never evicted
    memory_top_n: int = 8              # cap on the decayed (non-core) tail
    memory_max_tokens: int = 1200      # token budget for the rendered memory block

    @property
    def is_production(self) -> bool:
        return self.railway_env.strip().lower() == "production"

    def memory_db_file(self) -> str:
        """Resolve the SQLite path: explicit override > Railway volume > ./data."""
        if self.memory_db_path:
            return self.memory_db_path
        base = self.railway_volume_mount_path or os.path.join(os.getcwd(), "data")
        return os.path.join(base, "alistair_memory.db")

    @property
    def memory_is_persistent(self) -> bool:
        """True only when a Railway volume backs the DB (survives redeploys)."""
        return bool(self.railway_volume_mount_path)


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so env is read once per process."""
    return Settings()
