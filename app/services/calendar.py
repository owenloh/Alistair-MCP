"""Google Calendar connector — full toolset backed by the Calendar REST API v3.

Token resolution:
  * If GOOGLE_REFRESH_TOKEN + GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET are set,
    mint a fresh access token per call (never goes stale) — recommended for a
    long-running Railway service.
  * Otherwise use GOOGLE_CALENDAR_TOKEN directly as a Bearer access token.

Each public function takes ``settings: Settings`` as its first argument, uses the
existing ``_access_token`` helper for auth, talks to the Calendar REST API v3 with
synchronous httpx, and returns plain dicts. On any non-2xx upstream response a
``ServiceError`` is raised carrying the upstream code and a safe body snippet.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, time as dt_time
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import ServiceError
from ..config import Settings

_BASE = "https://www.googleapis.com/calendar/v3"
_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TIMEOUT = httpx.Timeout(30.0)


def _access_token(settings: Settings) -> str:
    if settings.google_refresh_token and settings.google_client_id and settings.google_client_secret:
        try:
            resp = httpx.post(
                _TOKEN_URL,
                data={
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "refresh_token": settings.google_refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as e:
            raise ServiceError(f"Network error reaching Google OAuth: {e}", status_code=502)
        if resp.status_code != 200:
            raise ServiceError(
                "Could not refresh Google access token.",
                status_code=502,
                detail=_safe_body(resp),
            )
        token = resp.json().get("access_token")
        if not token:
            raise ServiceError("Google token endpoint returned no access_token.", status_code=502)
        return token

    if settings.google_calendar_token:
        return settings.google_calendar_token

    raise ServiceError(
        "GOOGLE_CALENDAR_TOKEN is not configured (and no refresh-token trio set).",
        status_code=503,
    )


def _zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ServiceError(
            f"Unknown timezone '{name}'. Use a valid IANA name, e.g. America/New_York.",
            status_code=400,
        )


# Short-lived cache for the auto-detected account timezone so we don't add a
# lookup (and token mint) to every calendar call. Refreshes within _TZ_TTL, so a
# timezone change while travelling is picked up automatically within minutes.
_TZ_CACHE: dict[str, Any] = {"value": None, "ts": 0.0}
_TZ_TTL = 600.0


def _account_timezone(settings: Settings) -> str | None:
    """Best-effort current Google Calendar timezone (follows travel).

    Reads the user's Calendar timezone *setting*; when you let Google update your
    calendar's timezone on arrival in a new country, this tracks it. Returns None
    (so callers fall back) if disabled or unavailable.
    """
    if not settings.timezone_auto:
        return None
    now = time.time()
    if _TZ_CACHE["value"] and now - _TZ_CACHE["ts"] < _TZ_TTL:
        return _TZ_CACHE["value"]
    try:
        resp = _request(
            "GET", f"{_BASE}/users/me/settings/timezone",
            settings=settings, op="resolve timezone",
        )
        value = resp.json().get("value")
    except ServiceError:
        return None
    if value:
        _TZ_CACHE["value"] = value
        _TZ_CACHE["ts"] = now
    return value


def _resolve_tz(settings: Settings, requested: str | None) -> str:
    """Timezone precedence: explicit request > live account tz > TIMEZONE default.

    Blank/whitespace values are ignored so an empty TIMEZONE env var can't break
    resolution; the final fallback is always a valid zone.
    """
    if requested and requested.strip():
        return requested.strip()
    auto = _account_timezone(settings)
    if auto:
        return auto
    return settings.calendar_timezone.strip() or "UTC"


def _tz(settings: Settings) -> ZoneInfo:
    return _zoneinfo(_resolve_tz(settings, None))


def current_timezone(settings: Settings) -> str:
    """Public: the timezone Alistair is operating in — the live Google Calendar
    account timezone when auto-detect is on and reachable (so it follows travel),
    else the configured default. Used by load_context's `now` block."""
    return _resolve_tz(settings, None)


def _format_event(ev: dict, tz: ZoneInfo) -> dict:
    start = ev.get("start", {})
    end = ev.get("end", {})
    all_day = "date" in start and "dateTime" not in start

    if all_day:
        time_str = "All day"
    else:
        dt_raw = start.get("dateTime")
        try:
            dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
            time_str = dt.astimezone(tz).strftime("%H:%M")
        except (AttributeError, ValueError):
            time_str = dt_raw or "?"

    event = {
        "time": time_str,
        "title": ev.get("summary", "(no title)"),
        "notes": ev.get("description", "") or "",
        "all_day": all_day,
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
    }
    if ev.get("location"):
        event["location"] = ev["location"]
    return event


def today_events(settings: Settings, *, time_zone: str | None = None) -> dict:
    tz_name = _resolve_tz(settings, time_zone)
    tz = _zoneinfo(tz_name)
    token = _access_token(settings)

    now = datetime.now(tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    url = _EVENTS_URL.format(cal=quote(settings.google_calendar_id, safe=""))
    params = {
        "timeMin": start_of_day.isoformat(),
        "timeMax": end_of_day.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeZone": tz_name,
        "maxResults": "100",
    }
    try:
        resp = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise ServiceError(f"Network error reaching Google Calendar: {e}", status_code=502)
    if resp.status_code != 200:
        raise ServiceError(
            f"Google Calendar returned HTTP {resp.status_code}. "
            "A 401 usually means the access token expired (set the refresh-token trio "
            "for auto-refresh).",
            status_code=502,
            detail=_safe_body(resp),
        )

    items = resp.json().get("items", [])
    events = [_format_event(ev, tz) for ev in items if ev.get("status") != "cancelled"]
    return {
        "date": start_of_day.date().isoformat(),
        "timezone": tz_name,
        "count": len(events),
        "events": events,
    }


def _safe_body(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            return str(err.get("message", err) if isinstance(err, dict) else err)[:300]
        return str(data)[:300]
    except Exception:
        return resp.text[:300]


# ---------------------------------------------------------------------------
# Shared HTTP plumbing for the full connector
# ---------------------------------------------------------------------------

def _headers(settings: Settings) -> dict:
    return {"Authorization": f"Bearer {_access_token(settings)}"}


def _cal(settings: Settings, calendar_id: str | None) -> str:
    """Resolve and URL-encode the target calendarId for a path segment."""
    return quote(calendar_id or settings.google_calendar_id, safe="")


def _request(
    method: str,
    url: str,
    *,
    settings: Settings,
    op: str,
    params: dict | None = None,
    json: dict | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    """Perform an authenticated request and raise ServiceError on failure.

    ``op`` is a short human description used in the error message (e.g.
    "list events"). Any non-2xx response becomes a 502 ServiceError carrying a
    safe snippet of the upstream body.
    """
    hdrs = _headers(settings)
    if headers:
        hdrs.update(headers)
    try:
        resp = httpx.request(
            method,
            url,
            params=params,
            json=json,
            headers=hdrs,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise ServiceError(f"Network error reaching Google Calendar: {e}", status_code=502)
    if not (200 <= resp.status_code < 300):
        raise ServiceError(
            f"Google Calendar returned HTTP {resp.status_code} for {op}.",
            status_code=502,
            detail=_safe_body(resp),
        )
    return resp


def _send_updates(notification_level: str | None) -> str:
    """Map a connector notificationLevel to the REST ``sendUpdates`` value."""
    mapping = {
        "NONE": "none",
        "EXTERNAL_ONLY": "externalOnly",
        "ALL": "all",
    }
    if not notification_level:
        return "externalOnly"
    return mapping.get(notification_level.strip().upper(), "externalOnly")


def _transparency(availability: str | None) -> str | None:
    """Map availability ('busy'/'free') to the REST ``transparency`` value."""
    if not availability:
        return None
    av = availability.strip().lower()
    if av == "busy":
        return "opaque"
    if av == "free":
        return "transparent"
    return None


def _time_field(value: str, all_day: bool, time_zone: str) -> dict:
    """Build a Calendar start/end object from an ISO8601 string.

    For all-day events emit ``{date}`` (date portion only); otherwise emit
    ``{dateTime, timeZone}``.
    """
    if all_day:
        return {"date": value[:10]}
    return {"dateTime": value, "timeZone": time_zone}


def _compact_event(ev: dict) -> dict:
    """Reduce a full event resource to a compact, voice-friendly dict."""
    start = ev.get("start", {})
    end = ev.get("end", {})
    out: dict[str, Any] = {
        "id": ev.get("id"),
        "summary": ev.get("summary"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": ev.get("location"),
        "description": ev.get("description"),
        "attendees": [
            {"email": a.get("email"), "responseStatus": a.get("responseStatus")}
            for a in ev.get("attendees", [])
        ],
        "htmlLink": ev.get("htmlLink"),
        "status": ev.get("status"),
    }
    if ev.get("hangoutLink"):
        out["hangoutLink"] = ev["hangoutLink"]
    if ev.get("conferenceData"):
        out["conferenceData"] = ev["conferenceData"]
    return out


# ---------------------------------------------------------------------------
# 1. list_events
# ---------------------------------------------------------------------------

def list_events(
    settings: Settings,
    *,
    calendar_id: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    time_zone: str | None = None,
    full_text: str | None = None,
    order_by: str | None = None,
    page_size: int = 100,
    page_token: str | None = None,
    single_events: bool = True,
) -> dict:
    """List events satisfying the given time/text constraints.

    Mirrors the connector's list_events tool. ``orderBy`` "startTime" is only
    sent to Google when ``singleEvents`` is true (a REST requirement).
    """
    url = _EVENTS_URL.format(cal=_cal(settings, calendar_id))
    tz = _resolve_tz(settings, time_zone)

    params: dict[str, Any] = {
        "singleEvents": "true" if single_events else "false",
        "maxResults": page_size,
        "timeZone": tz,
    }
    if start_time:
        params["timeMin"] = start_time
    if end_time:
        params["timeMax"] = end_time
    if full_text:
        params["q"] = full_text
    if page_token:
        params["pageToken"] = page_token

    # orderBy: "startTime" requires singleEvents=true. "startTimeDesc" is not a
    # native REST value, so order ascending then reverse below. "lastModified"
    # passes through as "updated".
    reverse = False
    if order_by == "startTime" and single_events:
        params["orderBy"] = "startTime"
    elif order_by == "startTimeDesc" and single_events:
        params["orderBy"] = "startTime"
        reverse = True
    elif order_by == "lastModified":
        params["orderBy"] = "updated"

    resp = _request("GET", url, settings=settings, op="list events", params=params)
    data = resp.json()
    items = [ev for ev in data.get("items", []) if ev.get("status") != "cancelled"]
    if reverse:
        items = list(reversed(items))
    events = [_compact_event(ev) for ev in items]

    result: dict[str, Any] = {"events": events}
    if data.get("nextPageToken"):
        result["nextPageToken"] = data["nextPageToken"]
    return result


# ---------------------------------------------------------------------------
# 2. list_calendars
# ---------------------------------------------------------------------------

def list_calendars(
    settings: Settings,
    *,
    page_size: int | None = None,
    page_token: str | None = None,
) -> dict:
    """Return the calendars on the user's calendar list (compact form)."""
    url = f"{_BASE}/users/me/calendarList"
    params: dict[str, Any] = {}
    if page_size is not None:
        params["maxResults"] = page_size
    if page_token:
        params["pageToken"] = page_token

    resp = _request("GET", url, settings=settings, op="list calendars", params=params)
    data = resp.json()
    calendars = [
        {
            "id": c.get("id"),
            "summary": c.get("summary"),
            "primary": c.get("primary", False),
            "accessRole": c.get("accessRole"),
            "timeZone": c.get("timeZone"),
        }
        for c in data.get("items", [])
    ]
    result: dict[str, Any] = {"calendars": calendars}
    if data.get("nextPageToken"):
        result["nextPageToken"] = data["nextPageToken"]
    return result


# ---------------------------------------------------------------------------
# 3. get_event
# ---------------------------------------------------------------------------

def get_event(
    settings: Settings,
    *,
    event_id: str,
    calendar_id: str | None = None,
) -> dict:
    """Return a single event (full event JSON passed through)."""
    url = f"{_BASE}/calendars/{_cal(settings, calendar_id)}/events/{quote(event_id, safe='')}"
    resp = _request("GET", url, settings=settings, op="get event")
    return resp.json()


# ---------------------------------------------------------------------------
# 4. create_event
# ---------------------------------------------------------------------------

def create_event(
    settings: Settings,
    *,
    summary: str,
    start_time: str,
    end_time: str,
    all_day: bool = False,
    calendar_id: str | None = None,
    time_zone: str | None = None,
    location: str | None = None,
    description: str | None = None,
    attendees: list[str] | None = None,
    add_google_meet_url: bool = False,
    color_id: str | None = None,
    visibility: str | None = None,
    availability: str | None = None,
    recurrence: list[str] | None = None,
    reminders: list[dict] | None = None,
    notification_level: str | None = None,
) -> dict:
    """Create a calendar event and return a compact summary of the result."""
    url = _EVENTS_URL.format(cal=_cal(settings, calendar_id))
    tz = _resolve_tz(settings, time_zone)

    body: dict[str, Any] = {
        "summary": summary,
        "start": _time_field(start_time, all_day, tz),
        "end": _time_field(end_time, all_day, tz),
    }
    if location is not None:
        body["location"] = location
    if description is not None:
        body["description"] = description
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]
    if visibility is not None:
        body["visibility"] = visibility
    transparency = _transparency(availability)
    if transparency is not None:
        body["transparency"] = transparency
    if recurrence:
        body["recurrence"] = recurrence
    if reminders is not None:
        body["reminders"] = {"useDefault": False, "overrides": reminders}
    if color_id is not None:
        body["colorId"] = color_id

    params: dict[str, Any] = {"sendUpdates": _send_updates(notification_level)}
    if add_google_meet_url:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        params["conferenceDataVersion"] = 1

    resp = _request("POST", url, settings=settings, op="create event", params=params, json=body)
    ev = resp.json()
    start = ev.get("start", {})
    end = ev.get("end", {})
    return {
        "id": ev.get("id"),
        "htmlLink": ev.get("htmlLink"),
        "summary": ev.get("summary"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "hangoutLink": ev.get("hangoutLink"),
    }


# ---------------------------------------------------------------------------
# 5. update_event
# ---------------------------------------------------------------------------

def _merge_attendees(
    existing: Iterable[dict],
    added: list[str] | None,
    removed: list[str] | None,
) -> list[dict]:
    """Merge an attendee list: add new emails, drop removed ones (by email)."""
    removed_set = {e.lower() for e in (removed or [])}
    merged: list[dict] = [
        a for a in existing if (a.get("email") or "").lower() not in removed_set
    ]
    have = {(a.get("email") or "").lower() for a in merged}
    for email in added or []:
        if email.lower() not in have:
            merged.append({"email": email})
            have.add(email.lower())
    return merged


def update_event(
    settings: Settings,
    *,
    event_id: str,
    calendar_id: str | None = None,
    summary: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    all_day: bool = False,
    time_zone: str | None = None,
    location: str | None = None,
    description: str | None = None,
    added_attendees: list[str] | None = None,
    removed_attendees: list[str] | None = None,
    color_id: str | None = None,
    visibility: str | None = None,
    availability: str | None = None,
    recurrence: list[str] | None = None,
    reminders: list[dict] | None = None,
    add_google_meet_url: bool = False,
    notification_level: str | None = None,
) -> dict:
    """Patch a calendar event, only sending the fields that changed."""
    cal = _cal(settings, calendar_id)
    eid = quote(event_id, safe="")
    url = f"{_BASE}/calendars/{cal}/events/{eid}"
    tz = _resolve_tz(settings, time_zone)

    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if start_time is not None:
        body["start"] = _time_field(start_time, all_day, tz)
    if end_time is not None:
        body["end"] = _time_field(end_time, all_day, tz)
    if location is not None:
        body["location"] = location
    if description is not None:
        body["description"] = description
    if visibility is not None:
        body["visibility"] = visibility
    transparency = _transparency(availability)
    if transparency is not None:
        body["transparency"] = transparency
    if recurrence is not None:
        body["recurrence"] = recurrence
    if reminders is not None:
        body["reminders"] = {"useDefault": False, "overrides": reminders}
    if color_id is not None:
        body["colorId"] = color_id

    # Attendee add/remove requires a read-merge-write against the live event.
    if added_attendees or removed_attendees:
        current = get_event(settings, event_id=event_id, calendar_id=calendar_id)
        body["attendees"] = _merge_attendees(
            current.get("attendees", []), added_attendees, removed_attendees
        )

    params: dict[str, Any] = {"sendUpdates": _send_updates(notification_level)}
    if add_google_meet_url:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        params["conferenceDataVersion"] = 1

    resp = _request("PATCH", url, settings=settings, op="update event", params=params, json=body)
    ev = resp.json()
    start = ev.get("start", {})
    end = ev.get("end", {})
    return {
        "id": ev.get("id"),
        "summary": ev.get("summary"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "htmlLink": ev.get("htmlLink"),
        "hangoutLink": ev.get("hangoutLink"),
    }


# ---------------------------------------------------------------------------
# 6. delete_event
# ---------------------------------------------------------------------------

def delete_event(
    settings: Settings,
    *,
    event_id: str,
    calendar_id: str | None = None,
    notification_level: str | None = None,
) -> dict:
    """Delete a calendar event."""
    cal = _cal(settings, calendar_id)
    eid = quote(event_id, safe="")
    url = f"{_BASE}/calendars/{cal}/events/{eid}"
    params = {"sendUpdates": _send_updates(notification_level)}
    _request("DELETE", url, settings=settings, op="delete event", params=params)
    return {"deleted": True, "eventId": event_id}


# ---------------------------------------------------------------------------
# 7. respond_to_event
# ---------------------------------------------------------------------------

def respond_to_event(
    settings: Settings,
    *,
    event_id: str,
    response_status: str,
    calendar_id: str | None = None,
    response_comment: str | None = None,
    notification_level: str | None = None,
) -> dict:
    """Set the current user's responseStatus on an event (RSVP)."""
    cal = _cal(settings, calendar_id)
    eid = quote(event_id, safe="")
    url = f"{_BASE}/calendars/{cal}/events/{eid}"

    event = get_event(settings, event_id=event_id, calendar_id=calendar_id)
    attendees = event.get("attendees", [])
    found = False
    for attendee in attendees:
        if attendee.get("self"):
            attendee["responseStatus"] = response_status
            if response_comment is not None:
                attendee["comment"] = response_comment
            found = True
            break
    if not found:
        raise ServiceError("You are not an attendee of this event.", status_code=400)

    params = {"sendUpdates": _send_updates(notification_level)}
    _request(
        "PATCH",
        url,
        settings=settings,
        op="respond to event",
        params=params,
        json={"attendees": attendees},
    )
    return {"eventId": event_id, "responseStatus": response_status}


# ---------------------------------------------------------------------------
# 8. suggest_time
# ---------------------------------------------------------------------------

def _parse_iso(value: str) -> datetime:
    """Parse an ISO8601 string into a datetime (tolerates a trailing 'Z')."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_hour(value: str | None, default: dt_time) -> dt_time:
    """Parse an 'HH:MM' preference into a time, falling back to a default."""
    if not value:
        return default
    try:
        parts = value.split(":")
        return dt_time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return default


def _merge_busy(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Sort and coalesce overlapping/adjacent busy intervals."""
    if not intervals:
        return []
    intervals.sort(key=lambda iv: iv[0])
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def suggest_time(
    settings: Settings,
    *,
    attendee_emails: list[str],
    start_time: str,
    end_time: str,
    duration_minutes: int = 30,
    time_zone: str | None = None,
    preferences: dict | None = None,
) -> dict:
    """Suggest free slots across attendees' calendars via the freeBusy API.

    Busy intervals from every attendee are merged; the search window
    [start_time, end_time] is then walked to find free gaps of at least
    ``duration_minutes``, honoring working-hour and weekend preferences.
    """
    tz_name = _resolve_tz(settings, time_zone)
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise ServiceError(
            f"Unknown timezone '{tz_name}'. Install tzdata or pass a valid IANA timeZone.",
            status_code=400,
        )

    prefs = preferences or {}
    start_hour = _parse_hour(prefs.get("startHour"), dt_time(9, 0))
    end_hour = _parse_hour(prefs.get("endHour"), dt_time(17, 0))
    exclude_weekends = bool(prefs.get("excludeWeekends", False))
    page_size = int(prefs.get("pageSize", 5) or 5)

    window_start = _parse_iso(start_time).astimezone(tz)
    window_end = _parse_iso(end_time).astimezone(tz)
    duration = timedelta(minutes=duration_minutes)

    body = {
        "timeMin": start_time,
        "timeMax": end_time,
        "timeZone": tz_name,
        "items": [{"id": email} for email in attendee_emails],
    }
    resp = _request("POST", f"{_BASE}/freeBusy", settings=settings, op="suggest time", json=body)
    data = resp.json()

    busy: list[tuple[datetime, datetime]] = []
    for cal in data.get("calendars", {}).values():
        for period in cal.get("busy", []):
            try:
                busy.append(
                    (_parse_iso(period["start"]).astimezone(tz),
                     _parse_iso(period["end"]).astimezone(tz))
                )
            except (KeyError, ValueError):
                continue
    busy = _merge_busy(busy)

    suggestions: list[dict] = []
    # Walk forward across the window, hopping over busy blocks and respecting
    # the per-day working-hour bounds and (optionally) weekends.
    cursor = window_start
    while cursor + duration <= window_end and len(suggestions) < page_size:
        # Skip excluded weekends.
        if exclude_weekends and cursor.weekday() >= 5:
            cursor = datetime.combine(cursor.date() + timedelta(days=1), start_hour, tzinfo=tz)
            continue
        # Clamp to the working day's start.
        day_start = datetime.combine(cursor.date(), start_hour, tzinfo=tz)
        day_end = datetime.combine(cursor.date(), end_hour, tzinfo=tz)
        if cursor < day_start:
            cursor = day_start
        # Past the working day's end -> jump to the next day's start.
        if cursor >= day_end or cursor + duration > day_end:
            cursor = datetime.combine(cursor.date() + timedelta(days=1), start_hour, tzinfo=tz)
            continue

        # Find the busy block overlapping the cursor, if any.
        overlap_end = None
        for b_start, b_end in busy:
            if b_start <= cursor < b_end:
                overlap_end = b_end
                break
        if overlap_end is not None:
            cursor = overlap_end
            continue

        # Earliest busy start after the cursor bounds the candidate slot.
        slot_limit = min(day_end, window_end)
        for b_start, b_end in busy:
            if b_start > cursor:
                slot_limit = min(slot_limit, b_start)
                break

        if cursor + duration <= slot_limit:
            slot_end = cursor + duration
            suggestions.append({"start": cursor.isoformat(), "end": slot_end.isoformat()})
            cursor = slot_end
        else:
            # Not enough room before the next obstacle; jump past it.
            cursor = slot_limit

    return {"suggestions": suggestions}
