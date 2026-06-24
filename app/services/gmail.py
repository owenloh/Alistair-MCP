"""Gmail connector — read + draft, backed by the Gmail REST API v1.

Capabilities (deliberately limited to read + draft):
  * search   — find messages by Gmail query syntax (read-only)
  * get_thread — read a whole thread, rendered to clean text (read-only)
  * list_drafts / create_draft / update_draft / delete_draft — manage DRAFTS only

It NEVER sends mail and never touches real (non-draft) messages — no send, no
archive/label/delete of inbox mail. Sending stays a human action in Gmail. The only
writes are to the user's own Drafts.

Auth rides the shared Google refresh token (``app.services._google.mint_access_token``)
— the same token Calendar uses, just granted the Gmail scopes at consent time
(gmail.readonly + gmail.compose). A missing scope surfaces as Google's own
"insufficient authentication scopes" error (re-mint with scripts/get_google_token.py).
"""
from __future__ import annotations

import base64
import html as _html
import re
from email.message import EmailMessage
from typing import Any

import httpx

from . import ServiceError
from ..config import Settings
from ._google import mint_access_token, safe_body

_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_TIMEOUT = httpx.Timeout(30.0)
_STUB_HEADERS = ("From", "To", "Subject", "Date")


# ---------------------------------------------------------------------------
# HTTP plumbing (mirrors the calendar service's _request shape)
# ---------------------------------------------------------------------------

def _request(
    method: str,
    url: str,
    *,
    settings: Settings,
    op: str,
    params: dict | None = None,
    json: dict | None = None,
) -> httpx.Response:
    """Authenticated Gmail request; raises ServiceError on any non-2xx."""
    headers = {"Authorization": f"Bearer {mint_access_token(settings)}"}
    try:
        resp = httpx.request(method, url, params=params, json=json, headers=headers, timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        raise ServiceError(f"Network error reaching Gmail: {e}", status_code=502)
    if not (200 <= resp.status_code < 300):
        raise ServiceError(
            f"Gmail returned HTTP {resp.status_code} for {op}.",
            status_code=502,
            detail=safe_body(resp),
        )
    return resp


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _headers_map(payload: dict) -> dict[str, str]:
    return {h.get("name", ""): h.get("value", "") for h in (payload.get("headers") or [])}


def _pick_header(headers: dict[str, str], name: str) -> str:
    # Header names are case-insensitive; Gmail returns canonical caps but be safe.
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return ""


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _extract_body(payload: dict) -> str:
    """Walk a message payload and return readable text — prefers text/plain,
    falls back to stripped text/html. Handles multipart recursively."""
    plain: list[str] = []
    htmls: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        data = body.get("data")
        if part.get("parts"):
            for p in part["parts"]:
                walk(p)
        if data:
            try:
                text = _b64url_decode(data).decode("utf-8", "replace")
            except Exception:
                return
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                htmls.append(text)

    walk(payload)
    if plain:
        return "\n".join(t.strip() for t in plain).strip()
    if htmls:
        return _html_to_text("\n".join(htmls))
    return ""


def _build_raw(*, to: Any, subject: str, body: str, cc: Any = None, bcc: Any = None,
               in_reply_to: str | None = None) -> str:
    """Build an RFC-822 message and return it base64url-encoded for the Gmail API."""
    def _addr(v: Any) -> str:
        return ", ".join(v) if isinstance(v, (list, tuple)) else (v or "")

    msg = EmailMessage()
    msg["To"] = _addr(to)
    if cc:
        msg["Cc"] = _addr(cc)
    if bcc:
        msg["Bcc"] = _addr(bcc)
    msg["Subject"] = subject or ""
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body or "")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def search(settings: Settings, *, query: str, max_results: int = 20,
           label_ids: list[str] | None = None, **_ignored) -> dict:
    """Search messages with Gmail query syntax; returns lightweight stubs.

    Read-only. Two-step: list ids, then fetch metadata headers + snippet per id
    (capped to keep responses small)."""
    max_results = max(1, min(int(max_results or 20), 25))
    params: dict[str, Any] = {"q": query or "", "maxResults": max_results}
    if label_ids:
        params["labelIds"] = label_ids
    listing = _request("GET", f"{_BASE}/messages", settings=settings, op="search", params=params).json()
    ids = [m["id"] for m in (listing.get("messages") or [])][:max_results]

    results = []
    for mid in ids:
        meta = _request(
            "GET", f"{_BASE}/messages/{mid}", settings=settings, op="read message metadata",
            params=[("format", "metadata")] + [("metadataHeaders", h) for h in _STUB_HEADERS],
        ).json()
        hm = _headers_map(meta.get("payload") or {})
        results.append({
            "id": meta.get("id"),
            "thread_id": meta.get("threadId"),
            "from": _pick_header(hm, "From"),
            "to": _pick_header(hm, "To"),
            "subject": _pick_header(hm, "Subject"),
            "date": _pick_header(hm, "Date"),
            "snippet": _html.unescape(meta.get("snippet") or ""),
            "labels": meta.get("labelIds") or [],
        })
    return {"query": query, "count": len(results), "messages": results,
            "estimate": listing.get("resultSizeEstimate")}


def get_thread(settings: Settings, *, thread_id: str, **_ignored) -> dict:
    """Read a whole thread, every message rendered to readable text. Read-only."""
    if not thread_id:
        raise ServiceError("thread_id is required.", status_code=422)
    data = _request("GET", f"{_BASE}/threads/{thread_id}", settings=settings,
                    op="get thread", params={"format": "full"}).json()
    messages = []
    for m in (data.get("messages") or []):
        payload = m.get("payload") or {}
        hm = _headers_map(payload)
        messages.append({
            "id": m.get("id"),
            "from": _pick_header(hm, "From"),
            "to": _pick_header(hm, "To"),
            "cc": _pick_header(hm, "Cc"),
            "subject": _pick_header(hm, "Subject"),
            "date": _pick_header(hm, "Date"),
            "body": _extract_body(payload),
        })
    return {"thread_id": thread_id, "message_count": len(messages), "messages": messages}


def list_drafts(settings: Settings, *, max_results: int = 20, **_ignored) -> dict:
    """List existing drafts (id, to, subject, snippet). Read-only."""
    max_results = max(1, min(int(max_results or 20), 25))
    listing = _request("GET", f"{_BASE}/drafts", settings=settings, op="list drafts",
                       params={"maxResults": max_results}).json()
    drafts = []
    for d in (listing.get("drafts") or [])[:max_results]:
        did = d.get("id")
        full = _request("GET", f"{_BASE}/drafts/{did}", settings=settings,
                        op="read draft", params={"format": "metadata"}).json()
        msg = full.get("message") or {}
        hm = _headers_map(msg.get("payload") or {})
        drafts.append({
            "draft_id": did,
            "to": _pick_header(hm, "To"),
            "subject": _pick_header(hm, "Subject"),
            "snippet": _html.unescape(msg.get("snippet") or ""),
            "thread_id": msg.get("threadId"),
        })
    return {"count": len(drafts), "drafts": drafts}


def create_draft(settings: Settings, *, to: Any, subject: str, body: str,
                 cc: Any = None, bcc: Any = None, thread_id: str | None = None,
                 in_reply_to: str | None = None, **_ignored) -> dict:
    """Create a DRAFT (never sends). Pass thread_id (+ in_reply_to) to thread a reply."""
    if not to:
        raise ServiceError("'to' (recipient) is required.", status_code=422)
    raw = _build_raw(to=to, subject=subject, body=body, cc=cc, bcc=bcc, in_reply_to=in_reply_to)
    message: dict[str, Any] = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    out = _request("POST", f"{_BASE}/drafts", settings=settings, op="create draft",
                   json={"message": message}).json()
    msg = out.get("message") or {}
    return {"created": True, "draft_id": out.get("id"),
            "message_id": msg.get("id"), "thread_id": msg.get("threadId")}


def update_draft(settings: Settings, *, draft_id: str, to: Any, subject: str, body: str,
                 cc: Any = None, bcc: Any = None, thread_id: str | None = None,
                 in_reply_to: str | None = None, **_ignored) -> dict:
    """Replace the contents of an existing DRAFT (never sends)."""
    if not draft_id:
        raise ServiceError("draft_id is required.", status_code=422)
    if not to:
        raise ServiceError("'to' (recipient) is required.", status_code=422)
    raw = _build_raw(to=to, subject=subject, body=body, cc=cc, bcc=bcc, in_reply_to=in_reply_to)
    message: dict[str, Any] = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    out = _request("PUT", f"{_BASE}/drafts/{draft_id}", settings=settings, op="update draft",
                   json={"message": message}).json()
    msg = out.get("message") or {}
    return {"updated": True, "draft_id": out.get("id") or draft_id,
            "message_id": msg.get("id"), "thread_id": msg.get("threadId")}


def delete_draft(settings: Settings, *, draft_id: str, **_ignored) -> dict:
    """Delete a DRAFT (only a draft — never real mail)."""
    if not draft_id:
        raise ServiceError("draft_id is required.", status_code=422)
    _request("DELETE", f"{_BASE}/drafts/{draft_id}", settings=settings, op="delete draft")
    return {"deleted": True, "draft_id": draft_id}
