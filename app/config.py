"""Central settings — every secret AND every personal identifier comes from an
environment variable, never hardcoded (so this is safe to publish).

Non-secret behavioural defaults are provided (timezone, API versions, token decay).
Secrets default to None and personal identifiers (the owner's name, Notion database
and page ids) default to empty; the relevant endpoint returns a clear 503 when a
required one is missing, so the app still boots (and the skill endpoints still work)
even when connector config is absent. Set your own values in .env / the host's vars.
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

    # ---- Owner / personalisation ----
    # The person this assistant works for. Every user-facing string uses the neutral
    # placeholder "{owner}", substituted with this at the model/HTTP boundary, so the
    # code ships with NO hardcoded personal name. Set OWNER_NAME in your .env.
    owner_name: str = "the operator"

    # ---- Service ----
    railway_env: str = "development"
    service_api_key: str | None = None
    # Public base URL for the MCP OAuth issuer/metadata. Railway sets
    # RAILWAY_PUBLIC_DOMAIN automatically; PUBLIC_BASE_URL is an explicit override
    # (e.g. a custom domain). OAuth is enabled only when one of these resolves.
    public_base_url: str | None = None
    railway_public_domain: str | None = None
    # Password shown on the OAuth approval page so only the operator can authorize a
    # claude.ai connection (closes the public-URL + auto-approve hole). Falls back to
    # SERVICE_API_KEY if unset; if neither is set the gate fails closed (denies all).
    oauth_approval_password: str | None = None

    # ---- Notion ----
    # No IDs are hardcoded: set your own workspace's ids via the env vars below.
    # They default to empty, and the relevant endpoint returns a clear 503 when a
    # required id is missing (the same graceful-degrade pattern used for secrets).
    notion_token: str | None = None
    notion_version: str = "2022-06-28"
    projects_db_id: str = ""            # PROJECTS_DB_ID — Notion database (REST id)
    actions_db_id: str = ""             # ACTIONS_DB_ID  — Notion database (REST id)
    # Notion page ids the code writes to / references. Empty by default so nothing
    # personal is baked in; set them to your own pages to enable those features.
    references_tray_page_id: str = ""   # REFERENCES_TRAY_PAGE_ID — save_reference target
    library_hub_page_id: str = ""       # LIBRARY_HUB_PAGE_ID — parent hub (NEVER written)
    briefing_page_id: str = ""          # BRIEFING_PAGE_ID — daily-brief write target

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

    # ---- GitHub: gist token (stores the rotating MS To Do refresh token) ----
    # Named GITHUB_GIST_TOKEN to distinguish it from the repo token below; the old
    # GITHUB_TOKEN env name still works (alias) so nothing breaks on rename.
    github_gist_token: str | None = Field(
        default=None, validation_alias=AliasChoices("GITHUB_GIST_TOKEN", "GITHUB_TOKEN")
    )
    gist_id: str | None = None
    gist_filename: str = "mstodo_refresh_token"
    # Separate fine-grained PAT for repo read + PR (merge), kept distinct from the gist
    # token. Falls back to the gist token if a dedicated one isn't set.
    github_repo_token: str | None = None

    # ---- Spotify (unofficial, via SpotAPI — no Developer app / official OAuth) ----
    # The durable path is a logged-in web session: SPOTIFY_COOKIES holds the
    # open.spotify.com cookies (raw "k=v; k2=v2" string OR a JSON object; sp_dc is
    # the essential one), SPOTIFY_USERNAME is the account email/username. Password
    # login is optional and needs a CAPTCHA solver, so it's not the default.
    spotify_cookies: str | None = None
    spotify_username: str | None = None
    spotify_password: str | None = None  # optional; only for password login + a solver

    # ---- WhatsApp (read via a laptop agent + draft via wa.me links; NEVER sends) ----
    # Reading proxies to a small Baileys agent on the owner's laptop (its own linked
    # device), reachable over Tailscale; the MCP stores nothing and only reads when the
    # laptop is online. Drafting needs none of this — it builds a wa.me deep link the
    # owner sends themselves.
    whatsapp_agent_url: str | None = None       # base URL of the laptop read-agent
    whatsapp_agent_secret: str | None = None    # shared bearer secret for that agent
    whatsapp_default_country_code: str = "44"   # normalise bare local numbers to E.164

    # ---- Memory (SQLite append-only event log) ----
    # Railway sets RAILWAY_VOLUME_MOUNT_PATH automatically when a volume is
    # attached; the DB lives there so it survives redeploys. With no volume the
    # DB falls back to ./data (ephemeral — works, but lost on redeploy).
    railway_volume_mount_path: str | None = None
    memory_db_path: str | None = None  # explicit override for the SQLite file
    memory_tau_days: float = 30.0      # decay constant (half-life ~= 21d)
    memory_core_relevance: int = 5     # relevance >= this is pinned, never evicted
    memory_top_n: int = 12             # cap on the decayed tail (search_memory recalls the rest)
    memory_max_tokens: int = 1200      # token budget for the rendered memory block

    @property
    def is_production(self) -> bool:
        return self.railway_env.strip().lower() == "production"

    @property
    def spotify_configured(self) -> bool:
        """True when a logged-in Spotify session (cookies + account) is available."""
        return bool(self.spotify_cookies and self.spotify_username)

    @property
    def whatsapp_read_configured(self) -> bool:
        """True when the laptop read-agent URL is set (drafting works regardless)."""
        return bool(self.whatsapp_agent_url)

    @property
    def resolved_base_url(self) -> str | None:
        """Public https base URL for OAuth metadata, or None (then OAuth is off
        and the MCP uses the bearer guard). Explicit override wins over Railway's."""
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        if self.railway_public_domain:
            return f"https://{self.railway_public_domain.strip().rstrip('/')}"
        return None

    @property
    def oauth_approval_secret(self) -> str | None:
        """Secret the operator types on the OAuth approval page. Dedicated password
        if set, else the service key, else None (gate denies all approvals)."""
        return self.oauth_approval_password or self.service_api_key

    @property
    def github_read_token(self) -> str | None:
        """Token for the repo-read + merge_pr endpoints. Prefer the dedicated
        fine-grained PAT; fall back to the gist token so one token also works."""
        return self.github_repo_token or self.github_gist_token

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
