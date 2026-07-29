"""Weather connector — instant forecast for a place and a time, no web search.

One capability, ``weather(...)``, backed by **Open-Meteo** (free, no API key, no
sign-up):

  * geocoding  — ``geocoding-api.open-meteo.com`` turns "Kuala Lumpur" into a
    lat/lon + country + IANA timezone. Omit the location and it falls back to
    {user}'s own timezone/location hint (the same inference ``whereami`` uses).
  * forecast   — ``api.open-meteo.com`` returns current conditions, hour-by-hour
    detail and per-day summaries in ONE call. Range: 92 days of history through
    16 days of forecast.

The interesting part is ``parse_when``: it turns the way people actually ask
("tonight", "tomorrow morning", "in a week", "this weekend", "next Friday",
"3-7 Aug", "2026-08-03") into a concrete local start/end window, then the
forecast is sliced to exactly that window. Everything is resolved in the
LOCATION's timezone (Open-Meteo's ``timezone=auto``), so "tonight" means tonight
where the weather is, not where the server is.

Recoverable failures raise ``ServiceError`` so the router / MCP layer returns a
readable message instead of a 500.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import ServiceError
from ..config import Settings

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = httpx.Timeout(15.0)

# Open-Meteo's own bounds for the forecast endpoint.
_MAX_PAST_DAYS = 92
_MAX_FUTURE_DAYS = 16

# Hour-of-day windows for the vague-but-universal parts of a day.
_DAY_PARTS = {
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (18, 22),
    "night": (18, 24),
    "tonight": (18, 24),
    "overnight": (20, 24),
    "midday": (11, 14),
    "noon": (11, 14),
    "lunchtime": (11, 14),
}

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4,
    "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# WMO weather interpretation codes -> plain English.
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "light rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with light hail",
    99: "thunderstorm with heavy hail",
}


def describe_code(code: Any) -> str:
    """Plain-English label for a WMO weather code (never raises)."""
    try:
        return _WMO.get(int(code), "unknown conditions")
    except (TypeError, ValueError):
        return "unknown conditions"


# --------------------------------------------------------------------------- #
# "when" parsing
# --------------------------------------------------------------------------- #

def _day_window(d: date, tz: ZoneInfo, start_h: int = 0, end_h: int = 24
                ) -> tuple[datetime, datetime]:
    """[start_h, end_h) on day `d` as tz-aware datetimes; end_h=24 is midnight next day."""
    start = datetime.combine(d, time(hour=start_h), tzinfo=tz)
    if end_h >= 24:
        end = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=tz)
    else:
        end = datetime.combine(d, time(hour=end_h), tzinfo=tz)
    return start, end


def _next_weekday(today: date, weekday: int, *, force_next_week: bool = False) -> date:
    """The coming `weekday` (0=Mon), today included.

    force_next_week is the "next Friday" reading: that weekday in the week AFTER
    this one, counting weeks as Monday-Sunday — so on a Wednesday, "next Friday"
    is 9 days out, not 2.
    """
    if force_next_week:
        monday_next = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        return monday_next + timedelta(days=weekday)
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def _parse_date_token(token: str, today: date) -> date | None:
    """One date in ISO ('2026-08-03', '08-03'), or '3 Aug' / 'Aug 3' / '3/8' form."""
    t = token.strip().lower().rstrip(",")
    if not t:
        return None

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    # "3 aug", "3rd august", "3 aug 2026"
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?(?:\s+(\d{4}))?", t)
    if m and m.group(2) in _MONTHS:
        return _make_date(int(m.group(3) or 0), _MONTHS[m.group(2)], int(m.group(1)), today)

    # "aug 3", "august 3rd 2026"
    m = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", t)
    if m and m.group(1) in _MONTHS:
        return _make_date(int(m.group(3) or 0), _MONTHS[m.group(1)], int(m.group(2)), today)

    # "3/8" or "3/8/2026" — day/month (the rest of the world's order).
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", t)
    if m:
        year = int(m.group(3) or 0)
        if year and year < 100:
            year += 2000
        return _make_date(year, int(m.group(2)), int(m.group(1)), today)

    return None


def _make_date(year: int, month: int, day: int, today: date) -> date | None:
    """Build a date; with no year, pick the reading nearest to today (this year or next)."""
    if year:
        try:
            return date(year, month, day)
        except ValueError:
            return None
    for candidate_year in (today.year, today.year + 1):
        try:
            d = date(candidate_year, month, day)
        except ValueError:
            continue
        # A bare "3 Aug" that has already passed by more than a week means next year.
        if d >= today - timedelta(days=7):
            return d
    return None


def parse_when(when: str | None, now: datetime) -> dict:
    """Turn a natural-language time phrase into a concrete window in `now`'s timezone.

    Returns ``{"start", "end", "label", "granularity"}`` where start/end are
    tz-aware datetimes (end exclusive) and granularity is "hour" (a slice of a
    day — show the hourly detail) or "day" (one or more whole days).

    Unrecognised phrases fall back to the next 24 hours rather than erroring, and
    say so in the label, so the tool still answers something useful.
    """
    tz = now.tzinfo or ZoneInfo("UTC")
    today = now.date()
    raw = (when or "").strip()
    t = re.sub(r"\s+", " ", raw.lower()).strip()
    t = re.sub(r"^(the\s+)?(weather\s+)?(for|on|at|in)\s+", "", t) if t.startswith(
        ("the ", "weather ", "for ", "on ", "at ")) else t
    t = t.strip("?. ")

    def out(start: datetime, end: datetime, label: str, gran: str) -> dict:
        return {"start": start, "end": end, "label": label, "granularity": gran}

    # ---- now / empty ----
    if not t or t in {"now", "right now", "current", "currently", "at the moment", "today's weather"}:
        return out(now, now + timedelta(hours=6), "now", "hour")

    # ---- compact range: "3-7 aug", "aug 3-7" (no spaces around the dash) ----
    m = re.fullmatch(
        r"(\d{1,2})(?:st|nd|rd|th)?\s*[-–]\s*(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?"
        r"(?:,?\s+(\d{4}))?", t)
    if not m:
        m2 = re.fullmatch(
            r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*[-–]\s*(\d{1,2})(?:st|nd|rd|th)?"
            r"(?:,?\s+(\d{4}))?", t)
        if m2 and m2.group(1) in _MONTHS:
            m = None
            d1 = _make_date(int(m2.group(4) or 0), _MONTHS[m2.group(1)], int(m2.group(2)), today)
            d2 = _make_date(int(m2.group(4) or 0), _MONTHS[m2.group(1)], int(m2.group(3)), today)
            if d1 and d2 and d2 >= d1:
                start, _ = _day_window(d1, tz)
                _, end = _day_window(d2, tz)
                return out(start, end, f"{d1.isoformat()} to {d2.isoformat()}", "day")
    elif m.group(3) in _MONTHS:
        month, year = _MONTHS[m.group(3)], int(m.group(4) or 0)
        d1 = _make_date(year, month, int(m.group(1)), today)
        d2 = _make_date(year, month, int(m.group(2)), today)
        if d1 and d2 and d2 >= d1:
            start, _ = _day_window(d1, tz)
            _, end = _day_window(d2, tz)
            return out(start, end, f"{d1.isoformat()} to {d2.isoformat()}", "day")

    # ---- explicit range: "3 aug to 7 aug", "2026-08-03..2026-08-07" ----
    for sep in (" to ", " through ", " thru ", " until ", " till ", "..", " - ", " – "):
        if sep in t:
            left, _, right = t.partition(sep)
            d1 = _parse_date_token(left, today)
            d2 = _parse_date_token(right, today)
            if d1 and d2 and d2 >= d1:
                start, _ = _day_window(d1, tz)
                _, end = _day_window(d2, tz)
                return out(start, end, f"{d1.isoformat()} to {d2.isoformat()}", "day")
            # "3-7 aug" — bare day number on the left, month on the right.
            m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", left.strip())
            if m and d2:
                try:
                    d1 = date(d2.year, d2.month, int(m.group(1)))
                except ValueError:
                    d1 = None
                if d1 and d1 <= d2:
                    start, _ = _day_window(d1, tz)
                    _, end = _day_window(d2, tz)
                    return out(start, end, f"{d1.isoformat()} to {d2.isoformat()}", "day")
            break

    # ---- day-part on a named day: "tomorrow morning", "friday night" ----
    part = next((p for p in _DAY_PARTS if re.search(rf"\b{p}\b", t)), None)
    base = re.sub(rf"\b{part}\b", " ", t).strip() if part else t
    base = re.sub(r"\b(this|on|in the|the)\b", " ", base).strip()
    base = re.sub(r"\s+", " ", base)

    # "tonight" / "overnight" carry their own day (today) as well as their hours.
    if part in {"tonight", "overnight"} and not base:
        s, e = _day_window(today, tz, *_DAY_PARTS[part])
        return out(s, e, part, "hour")

    day: date | None = None
    day_label = ""
    if base in {"", "today"}:
        day, day_label = (today, "today") if part else (None, "")
    elif base == "tomorrow":
        day, day_label = today + timedelta(days=1), "tomorrow"
    elif base in {"yesterday"}:
        day, day_label = today - timedelta(days=1), "yesterday"
    elif base in {"day after tomorrow", "overmorrow"}:
        day, day_label = today + timedelta(days=2), "the day after tomorrow"
    elif base in _WEEKDAYS:
        day = _next_weekday(today, _WEEKDAYS[base])
        day_label = day.strftime("%A")
    elif base.startswith("next ") and base[5:] in _WEEKDAYS:
        day = _next_weekday(today, _WEEKDAYS[base[5:]], force_next_week=True)
        day_label = f"next {day.strftime('%A')}"
    else:
        parsed = _parse_date_token(base, today)
        if parsed:
            day, day_label = parsed, parsed.isoformat()

    if day is not None:
        if part:
            s, e = _day_window(day, tz, *_DAY_PARTS[part])
            return out(s, e, f"{day_label} {part}".strip(), "hour")
        s, e = _day_window(day, tz)
        return out(s, e, day_label or day.isoformat(), "day")

    if part and base in {"", "today"}:
        s, e = _day_window(today, tz, *_DAY_PARTS[part])
        return out(s, e, f"this {part}", "hour")

    # ---- plain "today" ----
    if t in {"today", "rest of today", "the rest of today"}:
        s, e = _day_window(today, tz)
        if t != "today":
            s = now
            return out(s, e, "the rest of today", "hour")
        return out(s, e, "today", "day")

    # ---- weekend ----
    if "weekend" in t:
        nxt = t.startswith("next")
        sat = _next_weekday(today, 5, force_next_week=nxt)
        start, _ = _day_window(sat, tz)
        _, end = _day_window(sat + timedelta(days=1), tz)
        return out(start, end, "next weekend" if nxt else "this weekend", "day")

    # ---- "in N hours/days/weeks", "N days from now", "a week from today" ----
    m = re.fullmatch(
        r"(?:in|after)?\s*(a|an|\d+)\s*(hour|hr|day|week|fortnight|month)s?"
        r"(?:\s*(?:from\s+(?:now|today)|time|later|ahead))?", t)
    if m:
        n = 1 if m.group(1) in {"a", "an"} else int(m.group(1))
        unit = m.group(2)
        if unit in {"hour", "hr"}:
            start = now + timedelta(hours=n)
            return out(start, start + timedelta(hours=3), f"in {n} hour{'s' if n != 1 else ''}", "hour")
        days = {"day": 1, "week": 7, "fortnight": 14, "month": 30}[unit] * n
        target = today + timedelta(days=days)
        s, e = _day_window(target, tz)
        count = "a" if (n == 1 and m.group(1) in {"a", "an"}) else str(n)
        return out(s, e, f"in {count} {unit}{'s' if n != 1 else ''} ({target.isoformat()})", "day")

    # ---- "next N days", "this week", "next week", "next 3 days" ----
    m = re.fullmatch(r"(?:the\s+)?next\s+(\d+)\s*(day|hour|week)s?", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "hour":
            return out(now, now + timedelta(hours=n), f"the next {n} hours", "hour")
        days = n * (7 if unit == "week" else 1)
        start, _ = _day_window(today, tz)
        _, end = _day_window(today + timedelta(days=days - 1), tz)
        return out(start, end, f"the next {days} days", "day")

    if t in {"this week", "rest of the week", "the rest of the week"}:
        start, _ = _day_window(today, tz)
        _, end = _day_window(_next_weekday(today, 6), tz)
        return out(start, end, "the rest of this week", "day")

    if t in {"next week"}:
        mon = _next_weekday(today, 0, force_next_week=True)
        start, _ = _day_window(mon, tz)
        _, end = _day_window(mon + timedelta(days=6), tz)
        return out(start, end, f"next week ({mon.isoformat()})", "day")

    if t in {"week", "a week", "the week"}:
        start, _ = _day_window(today, tz)
        _, end = _day_window(today + timedelta(days=6), tz)
        return out(start, end, "the next 7 days", "day")

    # ---- ISO datetime "2026-08-03T18:00" / "2026-08-03 18:00" ----
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[t ](\d{1,2})(?::(\d{2}))?", t)
    if m:
        d = _parse_date_token(m.group(1), today)
        if d:
            hour = min(int(m.group(2)), 23)
            start = datetime.combine(d, time(hour=hour), tzinfo=tz)
            return out(start, start + timedelta(hours=3), f"{d.isoformat()} {hour:02d}:00", "hour")

    # ---- bare date anywhere in the phrase ----
    parsed = _parse_date_token(t, today)
    if parsed:
        s, e = _day_window(parsed, tz)
        return out(s, e, parsed.isoformat(), "day")

    # ---- give up gracefully ----
    return out(now, now + timedelta(hours=24),
               f"the next 24 hours (couldn't read {raw!r} as a time)", "hour")


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #

def _timezone_location_hint(settings: Settings) -> tuple[str, str]:
    """({user}'s live timezone, a searchable place name inferred from it).

    Same inference as `whereami`: the live Google Calendar zone when auto-detect is
    on and reachable, else the configured default. 'Asia/Kuala_Lumpur' -> 'Kuala Lumpur'.
    """
    tz_name = (settings.calendar_timezone or "UTC").strip() or "UTC"
    try:
        from . import calendar as calendar_service
        tz_name = calendar_service.current_timezone(settings) or tz_name
    except Exception:
        pass  # calendar unconfigured / unreachable -> keep the configured default
    region, _, city = tz_name.partition("/")
    hint = (city or region).split("/")[-1].replace("_", " ")
    return tz_name, hint


def geocode(location: str) -> dict:
    """Resolve a place name to coordinates via Open-Meteo's geocoder."""
    try:
        resp = httpx.get(
            _GEOCODE_URL,
            params={"name": location, "count": 5, "language": "en", "format": "json"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise ServiceError(f"Geocoding lookup failed: {e}", status_code=502) from e
    if resp.status_code >= 400:
        raise ServiceError(
            f"Geocoding lookup failed ({resp.status_code}).", status_code=502
        )
    results = (resp.json() or {}).get("results") or []
    if not results:
        raise ServiceError(
            f"Couldn't find a place called {location!r}. Try a bigger nearby city, "
            "add the country ('Cambridge, US'), or pass lat/lon directly.",
            status_code=404,
        )
    top = results[0]
    return {
        "name": top.get("name"),
        "country": top.get("country"),
        "admin1": top.get("admin1"),
        "latitude": top.get("latitude"),
        "longitude": top.get("longitude"),
        "timezone": top.get("timezone"),
        "population": top.get("population"),
        "alternatives": [
            {"name": r.get("name"), "admin1": r.get("admin1"), "country": r.get("country")}
            for r in results[1:4]
        ],
    }


def _parse_latlon(location: str) -> tuple[float, float] | None:
    """Accept a raw 'lat,lon' pair so callers with GPS can skip geocoding."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", location or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def _resolve_location(settings: Settings, location: str | None) -> dict:
    """Explicit place > 'lat,lon' > {user}'s timezone-inferred home."""
    if location and location.strip():
        pair = _parse_latlon(location)
        if pair:
            return {"name": f"{pair[0]:.4f}, {pair[1]:.4f}", "country": None,
                    "latitude": pair[0], "longitude": pair[1], "timezone": None,
                    "source": "coordinates"}
        place = geocode(location.strip())
        place["source"] = "requested"
        return place

    tz_name, hint = _timezone_location_hint(settings)
    try:
        place = geocode(hint)
    except ServiceError as e:
        raise ServiceError(
            f"No location given and I couldn't resolve one from your timezone "
            f"({tz_name}). Say where you want the weather for.",
            status_code=e.status_code,
        ) from e
    place["source"] = f"inferred from your timezone ({tz_name})"
    return place


# --------------------------------------------------------------------------- #
# Forecast
# --------------------------------------------------------------------------- #

_HOURLY_VARS = (
    "temperature_2m,apparent_temperature,precipitation_probability,precipitation,"
    "weather_code,wind_speed_10m,relative_humidity_2m,cloud_cover,uv_index"
)
_DAILY_VARS = (
    "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
    "apparent_temperature_min,precipitation_sum,precipitation_probability_max,"
    "wind_speed_10m_max,uv_index_max,sunrise,sunset"
)
_CURRENT_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "weather_code,wind_speed_10m,wind_direction_10m,cloud_cover,is_day"
)


def _fetch_forecast(lat: float, lon: float, start: date, end: date,
                    units: str, want_current: bool) -> dict:
    params: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "hourly": _HOURLY_VARS,
        "daily": _DAILY_VARS,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    if want_current:
        params["current"] = _CURRENT_VARS
    if units == "imperial":
        params.update({"temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                       "precipitation_unit": "inch"})
    try:
        resp = httpx.get(_FORECAST_URL, params=params, timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        raise ServiceError(f"Weather lookup failed: {e}", status_code=502) from e
    if resp.status_code >= 400:
        reason = ""
        try:
            reason = (resp.json() or {}).get("reason") or ""
        except Exception:
            pass
        raise ServiceError(
            f"Weather lookup failed ({resp.status_code}){': ' + reason if reason else ''}.",
            status_code=502,
        )
    return resp.json() or {}


def _series(block: dict, key: str, i: int) -> Any:
    values = (block or {}).get(key) or []
    return values[i] if i < len(values) else None


def _round(value: Any, nd: int = 1) -> Any:
    return round(value, nd) if isinstance(value, (int, float)) else value


def _clamp_range(start: datetime, end: datetime, today: date) -> tuple[date, date, str | None]:
    """Clip the requested window to what Open-Meteo actually serves; say so if clipped."""
    first, last = start.date(), (end - timedelta(seconds=1)).date()
    floor, ceiling = today - timedelta(days=_MAX_PAST_DAYS), today + timedelta(days=_MAX_FUTURE_DAYS)
    note = None
    if last < floor or first > ceiling:
        raise ServiceError(
            f"That date is outside the forecast range ({floor.isoformat()} to "
            f"{ceiling.isoformat()}). Open-Meteo gives 92 days of history and 16 days ahead.",
            status_code=400,
        )
    if first < floor:
        first, note = floor, f"clipped to {floor.isoformat()} (92 days of history is the limit)"
    if last > ceiling:
        last, note = ceiling, f"clipped to {ceiling.isoformat()} (16 days ahead is the limit)"
    return first, last, note


def weather(settings: Settings, *, location: str | None = None,
            when: str | None = None, units: str = "metric") -> dict:
    """Forecast for a place and a time window, resolved in the LOCATION's timezone.

    location: place name, "lat,lon", or None ({user}'s timezone-inferred home).
    when:     natural language ("tonight", "tomorrow morning", "in a week",
              "this weekend", "next Friday", "3-7 Aug") or an ISO date/range.
    units:    "metric" (C, km/h, mm) or "imperial" (F, mph, inch).
    """
    units = (units or "metric").strip().lower()
    if units not in {"metric", "imperial"}:
        raise ServiceError("units must be 'metric' or 'imperial'.", status_code=400)

    place = _resolve_location(settings, location)
    lat, lon = place.get("latitude"), place.get("longitude")
    if lat is None or lon is None:
        raise ServiceError("Couldn't resolve coordinates for that location.", status_code=404)

    # Resolve "tonight" in the place's own timezone, not the server's.
    try:
        tz = ZoneInfo(place.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local_now = datetime.now(tz)
    window = parse_when(when, local_now)
    start, end = window["start"], window["end"]

    first, last, clip_note = _clamp_range(start, end, local_now.date())
    want_current = first <= local_now.date() <= last
    data = _fetch_forecast(float(lat), float(lon), first, last, units, want_current)

    # The API echoes the resolved zone; re-resolve so hour stamps compare correctly.
    api_tz = data.get("timezone")
    if api_tz:
        try:
            tz = ZoneInfo(api_tz)
            local_now = datetime.now(tz)
            if not (place.get("timezone") or "").strip():
                place["timezone"] = api_tz
                # Re-parse now that we know the real zone (matters for "tonight").
                window = parse_when(when, local_now)
                start, end = window["start"], window["end"]
        except (ZoneInfoNotFoundError, ValueError):
            pass

    units_block = {
        "temperature": (data.get("hourly_units") or {}).get("temperature_2m", "°C"),
        "wind_speed": (data.get("hourly_units") or {}).get("wind_speed_10m", "km/h"),
        "precipitation": (data.get("hourly_units") or {}).get("precipitation", "mm"),
    }

    hours = _slice_hours(data.get("hourly") or {}, tz, start, end)
    days = _shape_days(data.get("daily") or {}, start.date(), (end - timedelta(seconds=1)).date())

    result: dict[str, Any] = {
        "location": {
            "name": place.get("name"),
            "admin1": place.get("admin1"),
            "country": place.get("country"),
            "latitude": lat,
            "longitude": lon,
            "timezone": place.get("timezone") or api_tz,
            "resolved_from": place.get("source"),
        },
        "asked_for": when or "now",
        "window": {
            "label": window["label"],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "granularity": window["granularity"],
            "timezone": place.get("timezone") or api_tz,
        },
        "units": units_block,
        "days": days,
        "hours": hours if window["granularity"] == "hour" or len(days) <= 2 else [],
        "source": "Open-Meteo (open-meteo.com), no API key",
    }

    current = data.get("current") or {}
    if current:
        result["now"] = {
            "time": current.get("time"),
            "conditions": describe_code(current.get("weather_code")),
            "temperature": _round(current.get("temperature_2m")),
            "feels_like": _round(current.get("apparent_temperature")),
            "humidity_pct": current.get("relative_humidity_2m"),
            "precipitation": current.get("precipitation"),
            "wind_speed": _round(current.get("wind_speed_10m")),
            "cloud_cover_pct": current.get("cloud_cover"),
            "is_daylight": bool(current.get("is_day")),
        }

    notes = [n for n in (clip_note,) if n]
    if place.get("alternatives"):
        notes.append(
            "other places match that name: "
            + "; ".join(
                ", ".join(x for x in (a.get("name"), a.get("admin1"), a.get("country")) if x)
                for a in place["alternatives"]
            )
        )
    if notes:
        result["notes"] = notes
    result["summary"] = _summarise(result, window["label"], units_block)
    return result


def _slice_hours(hourly: dict, tz: ZoneInfo, start: datetime, end: datetime) -> list[dict]:
    """Hourly rows inside [start, end), capped so a long range stays readable."""
    stamps = hourly.get("time") or []
    rows: list[dict] = []
    for i, stamp in enumerate(stamps):
        try:
            at = datetime.fromisoformat(stamp).replace(tzinfo=tz)
        except ValueError:
            continue
        if not (start <= at < end):
            continue
        rows.append({
            "time": stamp,
            "hour": at.strftime("%H:%M"),
            "conditions": describe_code(_series(hourly, "weather_code", i)),
            "temperature": _round(_series(hourly, "temperature_2m", i)),
            "feels_like": _round(_series(hourly, "apparent_temperature", i)),
            "rain_chance_pct": _series(hourly, "precipitation_probability", i),
            "precipitation": _series(hourly, "precipitation", i),
            "wind_speed": _round(_series(hourly, "wind_speed_10m", i)),
            "humidity_pct": _series(hourly, "relative_humidity_2m", i),
            "cloud_cover_pct": _series(hourly, "cloud_cover", i),
        })
    if len(rows) > 48:  # keep the payload sane on multi-day windows
        rows = rows[::3][:48]
    return rows


def _shape_days(daily: dict, first: date, last: date) -> list[dict]:
    days: list[dict] = []
    for i, stamp in enumerate(daily.get("time") or []):
        try:
            d = date.fromisoformat(stamp)
        except ValueError:
            continue
        if not (first <= d <= last):
            continue
        days.append({
            "date": stamp,
            "weekday": d.strftime("%A"),
            "conditions": describe_code(_series(daily, "weather_code", i)),
            "temp_max": _round(_series(daily, "temperature_2m_max", i)),
            "temp_min": _round(_series(daily, "temperature_2m_min", i)),
            "feels_like_max": _round(_series(daily, "apparent_temperature_max", i)),
            "feels_like_min": _round(_series(daily, "apparent_temperature_min", i)),
            "rain_chance_pct": _series(daily, "precipitation_probability_max", i),
            "precipitation_total": _series(daily, "precipitation_sum", i),
            "wind_speed_max": _round(_series(daily, "wind_speed_10m_max", i)),
            "uv_index_max": _series(daily, "uv_index_max", i),
            "sunrise": _series(daily, "sunrise", i),
            "sunset": _series(daily, "sunset", i),
        })
    return days


def _summarise(result: dict, label: str, units_block: dict) -> str:
    """One line the model can read out verbatim if it wants."""
    place = result["location"]["name"] or "there"
    deg = units_block.get("temperature", "°C")
    hours = result.get("hours") or []
    days = result.get("days") or []

    if hours and len(days) <= 2:
        temps = [h["temperature"] for h in hours if isinstance(h["temperature"], (int, float))]
        chances = [h["rain_chance_pct"] for h in hours
                   if isinstance(h["rain_chance_pct"], (int, float))]
        conds = [h["conditions"] for h in hours]
        common = max(set(conds), key=conds.count) if conds else "unknown conditions"
        bits = [f"{place}, {label}: {common}"]
        if temps:
            bits.append(f"{min(temps):.0f}-{max(temps):.0f}{deg}")
        if chances:
            bits.append(f"rain chance up to {max(chances):.0f}%")
        return ", ".join(bits) + "."

    if days:
        lows = [d["temp_min"] for d in days if isinstance(d["temp_min"], (int, float))]
        highs = [d["temp_max"] for d in days if isinstance(d["temp_max"], (int, float))]
        wettest = max(
            (d for d in days if isinstance(d["rain_chance_pct"], (int, float))),
            key=lambda d: d["rain_chance_pct"], default=None,
        )
        bits = [f"{place}, {label}: {days[0]['conditions']}" if len(days) == 1
                else f"{place}, {label} ({len(days)} days)"]
        if lows and highs:
            bits.append(f"{min(lows):.0f}-{max(highs):.0f}{deg}")
        if wettest:
            bits.append(
                f"wettest {wettest['weekday']} at {wettest['rain_chance_pct']:.0f}% rain chance"
                if len(days) > 1 else f"{wettest['rain_chance_pct']:.0f}% rain chance"
            )
        return ", ".join(bits) + "."

    now = result.get("now")
    if now:
        return (f"{place} right now: {now['conditions']}, {now['temperature']}{deg} "
                f"(feels {now['feels_like']}{deg}).")
    return f"No forecast data returned for {place} ({label})."
