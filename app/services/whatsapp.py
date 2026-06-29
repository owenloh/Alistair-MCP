"""WhatsApp connector — read (via a laptop agent) + draft (wa.me deep link).

DELIBERATELY read + draft only. This connector NEVER sends a WhatsApp message —
the same hard limit the Gmail connector keeps for mail.

Two halves, by design:
  * READ — ``list_chats`` / ``read_messages`` / ``search`` proxy to a small agent
    running on Owen's laptop (Baileys, its OWN linked device), reachable over
    Tailscale. The MCP stays stateless: it just forwards an authed HTTP GET and
    returns what the agent holds. Nothing is stored in the cloud. If the laptop is
    off, the read tools return a clean "agent offline" (ServiceError 503) — never a
    crash and never a fabrication.
  * DRAFT — ``draft`` builds a ``wa.me/<number>?text=...`` deep link (no session, no
    network). Owen taps it and his NORMAL WhatsApp opens with the text pre-filled in
    the compose box for him to review and SEND HIMSELF.

Reading is privacy-first (messages live only on the laptop and flow only when the
authed read tool is called); drafting needs no session at all.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

import httpx

from . import ServiceError
from ..config import Settings

_TIMEOUT = httpx.Timeout(15.0)


# ---------------------------------------------------------------------------
# Read half — proxy to the laptop agent (online-only, stores nothing)
# ---------------------------------------------------------------------------

def _require_agent(settings: Settings) -> tuple[str, str]:
    url = (settings.whatsapp_agent_url or "").rstrip("/")
    if not url:
        raise ServiceError(
            "WhatsApp reading is not configured — set WHATSAPP_AGENT_URL to the laptop "
            "read-agent. Drafting (whatsapp_draft) still works without it.",
            status_code=503,
        )
    return url, (settings.whatsapp_agent_secret or "")


def _agent_get(settings: Settings, path: str, params: dict | None = None) -> Any:
    """Authed GET against the laptop agent; raises a clean ServiceError on any problem
    (offline laptop, bad secret, non-2xx, non-JSON) so the model gets a readable message."""
    base, secret = _require_agent(settings)
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    try:
        resp = httpx.get(f"{base}{path}", params=params, headers=headers, timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        # Laptop off / unreachable: an honest "offline", not a 500.
        raise ServiceError(
            "WhatsApp agent is offline — Owen's laptop may be off or asleep. Reading needs "
            "it online; drafting still works.",
            status_code=503,
            detail=str(e)[:200],
        )
    if resp.status_code == 401:
        raise ServiceError(
            "WhatsApp agent rejected the shared secret (check WHATSAPP_AGENT_SECRET).",
            status_code=502,
        )
    if not (200 <= resp.status_code < 300):
        raise ServiceError(
            f"WhatsApp agent returned HTTP {resp.status_code}.",
            status_code=502,
            detail=(resp.text or "")[:300],
        )
    try:
        return resp.json()
    except Exception:
        raise ServiceError(
            "WhatsApp agent returned a non-JSON response.",
            status_code=502,
            detail=(resp.text or "")[:300],
        )


def _as_list(data: Any, key: str) -> list:
    if isinstance(data, dict):
        return data.get(key) or []
    return data or []


def list_chats(settings: Settings, *, limit: int = 20, **_ignored) -> dict:
    """List recent WhatsApp chats (id/jid, name, last-message time, unread). Read-only."""
    limit = max(1, min(int(limit or 20), 50))
    chats = _as_list(_agent_get(settings, "/chats", params={"limit": limit}), "chats")
    return {"count": len(chats), "chats": chats}


def read_messages(settings: Settings, *, chat: str, limit: int = 30, **_ignored) -> dict:
    """Read recent messages in one chat by its id/jid (from list_chats). Read-only."""
    if not chat:
        raise ServiceError("chat (a chat id/jid from whatsapp_chats) is required.", status_code=422)
    limit = max(1, min(int(limit or 30), 100))
    msgs = _as_list(_agent_get(settings, "/messages", params={"chat": chat, "limit": limit}), "messages")
    return {"chat": chat, "count": len(msgs), "messages": msgs}


def search(settings: Settings, *, query: str, limit: int = 20, **_ignored) -> dict:
    """Search messages by text; returns matches with their chat id. Read-only."""
    if not query:
        raise ServiceError("query is required.", status_code=422)
    limit = max(1, min(int(limit or 20), 50))
    msgs = _as_list(_agent_get(settings, "/search", params={"q": query, "limit": limit}), "messages")
    return {"query": query, "count": len(msgs), "messages": msgs}


# ---------------------------------------------------------------------------
# Draft half — pure wa.me deep link (no session, NEVER sends)
# ---------------------------------------------------------------------------

def _normalise_number(raw: str, default_cc: str) -> str:
    """Best-effort E.164 digits (no '+') for a wa.me link.

    Strips spaces/dashes/brackets; drops a leading '+' or '00'; a bare local number
    (leading 0) gets the default country code. Returns digits only ('' if none)."""
    s = "".join(ch for ch in (raw or "") if ch.isdigit() or ch == "+")
    if s.startswith("+"):
        return s[1:]
    if s.startswith("00"):
        return s[2:]
    cc = "".join(ch for ch in (default_cc or "") if ch.isdigit())
    if s.startswith("0"):
        return cc + s[1:]
    return s


def draft(settings: Settings, *, to: str, body: str, **_ignored) -> dict:
    """Build a wa.me deep link that opens WhatsApp with ``body`` pre-filled. NEVER sends.

    ``to`` is a phone number (any common format). If it has no digits it is treated as a
    contact name and resolved via the agent's /contacts when the laptop is online; otherwise
    we ask for a number."""
    if not body:
        raise ServiceError("body (the message text to pre-fill) is required.", status_code=422)
    if not to:
        raise ServiceError(
            "to (a phone number, or a contact name if the laptop agent is online) is required.",
            status_code=422,
        )

    raw = to
    resolved_from = None
    if not any(ch.isdigit() for ch in to):
        # A name — try the agent's contact resolver (read-only, best effort).
        try:
            contacts = _as_list(_agent_get(settings, "/contacts", params={"query": to}), "contacts")
            if contacts:
                raw = contacts[0].get("number") or contacts[0].get("jid") or ""
                resolved_from = to
            else:
                raw = ""
        except ServiceError:
            raw = ""
        if not raw:
            raise ServiceError(
                f"Couldn't resolve '{to}' to a phone number (agent offline or no match). "
                "Give a phone number, e.g. +44 7700 900000.",
                status_code=422,
            )

    number = _normalise_number(raw, settings.whatsapp_default_country_code)
    if not number:
        raise ServiceError(f"'{to}' is not a usable phone number.", status_code=422)
    link = f"https://wa.me/{number}?text={urllib.parse.quote(body)}"
    out: dict[str, Any] = {
        "to": to,
        "number": number,
        "body": body,
        "link": link,
        "note": (
            "Opens WhatsApp with this text pre-filled in the compose box. Owen reviews and "
            "SENDS it himself — Alistair never sends."
        ),
    }
    if resolved_from:
        out["resolved_from_name"] = resolved_from
    return out
