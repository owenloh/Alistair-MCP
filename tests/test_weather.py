"""Tests for the weather connector (parse_when + geocode + forecast shaping).

Network is faked by monkeypatching `weather.httpx` with a stub exposing `.get`
(routed by URL substring) and `HTTPError`, so nothing hits Open-Meteo. Covers: the
natural-language time parser (tonight / tomorrow morning / in a week / this weekend /
next Friday / exact dates and ranges), location resolution (explicit, lat-lon, and
the timezone fallback), range clamping, forecast shaping and the summary line. A
TestClient pass checks the route + manifest wiring.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx as _httpx  # real, for HTTPError

os.environ.pop("SERVICE_API_KEY", None)

from app.config import Settings
from app.services import ServiceError
from app.services import weather

R = []
def check(name, cond):
    R.append((name, bool(cond)))


# ---- fake httpx: route URL substring -> FakeResp ----
class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
    def json(self):
        return self._payload

class FakeHttpx:
    HTTPError = _httpx.HTTPError
    Timeout = _httpx.Timeout
    def __init__(self, routes=None, raise_exc=None):
        self.routes = routes or []
        self.raise_exc = raise_exc
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.raise_exc:
            raise self.raise_exc
        for sub, resp in self.routes:
            if sub in url:
                return resp
        raise AssertionError(f"no fake route for {url}")

def patch(routes=None, raise_exc=None):
    weather.httpx = FakeHttpx(routes, raise_exc)
    return weather.httpx


TZ = ZoneInfo("Europe/London")
# A fixed "now": Wednesday 2026-07-29, 14:30 local.
NOW = datetime(2026, 7, 29, 14, 30, tzinfo=TZ)


# === parse_when: the point of the feature ===
def w(phrase):
    return weather.parse_when(phrase, NOW)

r = w(None)
check("empty -> now", r["label"] == "now" and r["start"] == NOW and r["granularity"] == "hour")
check("empty -> 6h window", r["end"] == NOW + timedelta(hours=6))

r = w("tonight")
check("tonight starts 18:00 today", r["start"].hour == 18 and r["start"].day == 29)
check("tonight ends at midnight", r["end"].day == 30 and r["end"].hour == 0)
check("tonight is hourly", r["granularity"] == "hour")

r = w("today")
check("today = whole day", r["start"].day == 29 and r["start"].hour == 0 and r["end"].day == 30)
check("today is daily", r["granularity"] == "day")

r = w("tomorrow")
check("tomorrow = 30th", r["start"].day == 30 and r["end"].day == 31 and r["granularity"] == "day")

r = w("tomorrow morning")
check("tomorrow morning 06-12 on the 30th",
      r["start"].day == 30 and r["start"].hour == 6 and r["end"].hour == 12)
check("tomorrow morning label", r["label"] == "tomorrow morning")

r = w("this evening")
check("this evening 18-22 today", r["start"].day == 29 and r["start"].hour == 18 and r["end"].hour == 22)

r = w("yesterday")
check("yesterday = 28th", r["start"].day == 28 and r["end"].day == 29)

r = w("day after tomorrow")
check("day after tomorrow = 31st", r["start"].day == 31)

r = w("in a week")
check("in a week = 2026-08-05", r["start"].date().isoformat() == "2026-08-05")
check("in a week is one whole day", r["granularity"] == "day" and r["end"].date().isoformat() == "2026-08-06")

r = w("in 3 days")
check("in 3 days = 2026-08-01", r["start"].date().isoformat() == "2026-08-01")

r = w("in 5 hours")
check("in 5 hours is hourly", r["granularity"] == "hour" and r["start"] == NOW + timedelta(hours=5))

r = w("next 5 days")
check("next 5 days spans 29th-2nd",
      r["start"].date().isoformat() == "2026-07-29" and r["end"].date().isoformat() == "2026-08-03")

r = w("this weekend")
check("this weekend = Sat 1 Aug .. Sun 2 Aug",
      r["start"].date().isoformat() == "2026-08-01" and r["end"].date().isoformat() == "2026-08-03")

r = w("next week")
check("next week starts Mon 3 Aug", r["start"].date().isoformat() == "2026-08-03")
check("next week ends after Sun 9 Aug", r["end"].date().isoformat() == "2026-08-10")

# 2026-07-29 is a Wednesday.
r = w("friday")
check("friday = 31 Jul", r["start"].date().isoformat() == "2026-07-31")
r = w("next friday")
check("next friday = 7 Aug", r["start"].date().isoformat() == "2026-08-07")
r = w("monday")
check("monday rolls forward to 3 Aug", r["start"].date().isoformat() == "2026-08-03")
r = w("saturday night")
check("saturday night = 1 Aug 18:00",
      r["start"].date().isoformat() == "2026-08-01" and r["start"].hour == 18)

# exact dates
r = w("2026-08-03")
check("ISO date", r["start"].date().isoformat() == "2026-08-03" and r["granularity"] == "day")
r = w("3 aug")
check("'3 aug'", r["start"].date().isoformat() == "2026-08-03")
r = w("august 3rd")
check("'august 3rd'", r["start"].date().isoformat() == "2026-08-03")
r = w("3/8")
check("'3/8' is day/month", r["start"].date().isoformat() == "2026-08-03")
r = w("2026-08-03 18:00")
check("ISO datetime -> hourly at 18:00",
      r["granularity"] == "hour" and r["start"].hour == 18 and r["start"].day == 3)

# ranges
r = w("2026-08-03 to 2026-08-07")
check("ISO range",
      r["start"].date().isoformat() == "2026-08-03" and r["end"].date().isoformat() == "2026-08-08")
r = w("3 aug to 7 aug")
check("named-month range",
      r["start"].date().isoformat() == "2026-08-03" and r["end"].date().isoformat() == "2026-08-08")
r = w("3-7 aug")
check("compact '3-7 aug' range",
      r["start"].date().isoformat() == "2026-08-03" and r["end"].date().isoformat() == "2026-08-08")

# a bare date already past by more than a week rolls to next year
r = w("1 jan")
check("past bare date rolls to next year", r["start"].year == 2027)

# unparseable -> graceful 24h fallback, and it says so
r = w("when the vibes are right")
check("unparseable falls back to 24h", r["end"] == NOW + timedelta(hours=24))
check("unparseable says so", "couldn't read" in r["label"])


# === WMO codes ===
check("code 0 = clear sky", weather.describe_code(0) == "clear sky")
check("code 61 = light rain", weather.describe_code(61) == "light rain")
check("unknown code is safe", weather.describe_code(None) == "unknown conditions")


# === geocoding ===
GEO = {"results": [
    {"name": "Kuala Lumpur", "country": "Malaysia", "admin1": "Kuala Lumpur",
     "latitude": 3.1412, "longitude": 101.6865, "timezone": "Asia/Kuala_Lumpur"},
    {"name": "Kuala Lumpur", "country": "Indonesia", "admin1": "Riau",
     "latitude": 0.5, "longitude": 101.0, "timezone": "Asia/Jakarta"},
]}

# The service resolves "today"/"tonight" against the REAL clock in the location's
# timezone, so the fake payload is built around today-in-KL rather than a fixed date.
KL = ZoneInfo("Asia/Kuala_Lumpur")
TODAY_KL = datetime.now(KL).date()


def forecast_payload(days=None, hours_on=None, tz="Asia/Kuala_Lumpur"):
    days = days or (TODAY_KL.isoformat(),)
    hours_on = hours_on or TODAY_KL.isoformat()
    times = [f"{hours_on}T{h:02d}:00" for h in range(24)]
    return {
        "timezone": tz,
        "hourly_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h", "precipitation": "mm"},
        "current": {"time": f"{hours_on}T14:30", "temperature_2m": 31.2,
                    "apparent_temperature": 36.4, "relative_humidity_2m": 74,
                    "precipitation": 0.0, "weather_code": 2, "wind_speed_10m": 8.1,
                    "cloud_cover": 55, "is_day": 1},
        "hourly": {
            "time": times,
            "temperature_2m": [26 + (h % 8) for h in range(24)],
            "apparent_temperature": [29 + (h % 8) for h in range(24)],
            "precipitation_probability": [10 * (h % 10) for h in range(24)],
            "precipitation": [0.0] * 24,
            "weather_code": [61 if 18 <= h < 22 else 2 for h in range(24)],
            "wind_speed_10m": [7.0] * 24,
            "relative_humidity_2m": [70] * 24,
            "cloud_cover": [50] * 24,
            "uv_index": [3] * 24,
        },
        "daily": {
            "time": list(days),
            "weather_code": [61] * len(days),
            "temperature_2m_max": [33.0] * len(days),
            "temperature_2m_min": [25.0] * len(days),
            "apparent_temperature_max": [38.0] * len(days),
            "apparent_temperature_min": [27.0] * len(days),
            "precipitation_sum": [4.2] * len(days),
            "precipitation_probability_max": [80] * len(days),
            "wind_speed_10m_max": [12.0] * len(days),
            "uv_index_max": [9.0] * len(days),
            "sunrise": [f"{d}T07:05" for d in days],
            "sunset": [f"{d}T19:25" for d in days],
        },
    }

# calendar_timezone is aliased to TIMEZONE/CALENDAR_TIMEZONE, so pass the alias.
st = Settings(TIMEZONE="Asia/Kuala_Lumpur", timezone_auto=False)

fx = patch(routes=[("geocoding-api", FakeResp(200, GEO)),
                   ("api.open-meteo.com", FakeResp(200, forecast_payload()))])
res = weather.weather(st, location="Kuala Lumpur", when="today")
check("resolved location name", res["location"]["name"] == "Kuala Lumpur")
check("resolved country", res["location"]["country"] == "Malaysia")
check("resolved_from requested", res["location"]["resolved_from"] == "requested")
check("current conditions mapped", res["now"]["conditions"] == "partly cloudy")
check("current temp rounded", res["now"]["temperature"] == 31.2)
check("daily shaped", res["days"][0]["temp_max"] == 33.0 and res["days"][0]["conditions"] == "light rain")
check("daily weekday", res["days"][0]["weekday"] == TODAY_KL.strftime("%A"))
check("units echoed", res["units"]["temperature"] == "°C")
check("summary mentions place", "Kuala Lumpur" in res["summary"])
check("alternatives surfaced as a note", any("other places match" in n for n in res.get("notes", [])))
check("source is open-meteo", "Open-Meteo" in res["source"])

# the forecast request carried an explicit date range + timezone=auto
params = fx.calls[-1][1]["params"]
check("forecast asks for a date range", "start_date" in params and "end_date" in params)
check("forecast timezone auto", params["timezone"] == "auto")
check("forecast asks for current when today is in range", "current" in params)

# --- 'tonight' slices the hourly series to the evening ---
fx = patch(routes=[("geocoding-api", FakeResp(200, GEO)),
                   ("api.open-meteo.com", FakeResp(200, forecast_payload()))])
res = weather.weather(st, location="Kuala Lumpur", when="tonight")
check("tonight window labelled", res["window"]["label"] == "tonight")
check("tonight returns hourly rows", len(res["hours"]) == 6)
check("tonight hours start at 18:00", res["hours"][0]["hour"] == "18:00")
check("tonight hours are rainy", res["hours"][0]["conditions"] == "light rain")
check("tonight summary has a rain chance", "rain chance" in res["summary"])

# --- units=imperial flips the API params ---
fx = patch(routes=[("geocoding-api", FakeResp(200, GEO)),
                   ("api.open-meteo.com", FakeResp(200, forecast_payload()))])
weather.weather(st, location="Kuala Lumpur", when="today", units="imperial")
params = fx.calls[-1][1]["params"]
check("imperial temperature unit", params["temperature_unit"] == "fahrenheit")
check("imperial wind unit", params["wind_speed_unit"] == "mph")

try:
    weather.weather(st, location="KL", when="today", units="kelvin")
    check("bad units -> error", False)
except ServiceError as e:
    check("bad units -> 400", e.status_code == 400)

# --- lat,lon skips geocoding entirely ---
fx = patch(routes=[("api.open-meteo.com", FakeResp(200, forecast_payload()))])
res = weather.weather(st, location="3.1412,101.6865", when="today")
check("lat,lon skips geocoding", all("geocoding" not in c[0] for c in fx.calls))
check("lat,lon resolved_from", res["location"]["resolved_from"] == "coordinates")
check("lat,lon coords used", fx.calls[-1][1]["params"]["latitude"] == 3.1412)

# --- no location -> inferred from the user's timezone ---
fx = patch(routes=[("geocoding-api", FakeResp(200, GEO)),
                   ("api.open-meteo.com", FakeResp(200, forecast_payload()))])
res = weather.weather(st, when="today")
check("timezone fallback geocodes the tz city", "Kuala+Lumpur" in fx.calls[0][0]
      or fx.calls[0][1]["params"]["name"] == "Kuala Lumpur")
check("resolved_from mentions the timezone", "Asia/Kuala_Lumpur" in res["location"]["resolved_from"])

# --- unknown place -> clean 404 ---
patch(routes=[("geocoding-api", FakeResp(200, {"results": []}))])
try:
    weather.weather(st, location="Zzzzqqq", when="today")
    check("unknown place -> error", False)
except ServiceError as e:
    check("unknown place -> 404", e.status_code == 404)

# --- beyond the forecast horizon -> clean 400, not a guess ---
patch(routes=[("geocoding-api", FakeResp(200, GEO)),
              ("api.open-meteo.com", FakeResp(200, forecast_payload()))])
far = (TODAY_KL + timedelta(days=200)).isoformat()
try:
    weather.weather(st, location="Kuala Lumpur", when=far)
    check("far future -> error", False)
except ServiceError as e:
    check("far future -> 400", e.status_code == 400)
    check("far future error explains the range", "16 days" in e.message)

# --- upstream failure -> clean 502 ---
patch(routes=[("geocoding-api", FakeResp(200, GEO))], raise_exc=_httpx.HTTPError("boom"))
try:
    weather.weather(st, location="Kuala Lumpur", when="today")
    check("network error -> error", False)
except ServiceError as e:
    check("network error -> 502", e.status_code == 502)

patch(routes=[("geocoding-api", FakeResp(200, GEO)),
              ("api.open-meteo.com", FakeResp(400, {"reason": "bad params"}))])
try:
    weather.weather(st, location="Kuala Lumpur", when="today")
    check("api 400 -> error", False)
except ServiceError as e:
    check("api error surfaces the reason", "bad params" in e.message)


# === TestClient wiring: route exists + manifest lists weather ===
from fastapi.testclient import TestClient
from app.main import app
cl = TestClient(app)
mani = cl.get("/api/manifest").json()
check("manifest has weather group", "weather" in mani["function_apis"])
check("manifest weather has 1 endpoint", mani["counts"].get("weather") == 1)
check("manifest lists /api/weather",
      {e["path"] for e in mani["function_apis"]["weather"]} == {"/api/weather"})
check("manifest weather description mentions time phrasing",
      "tonight" in mani["function_apis"]["weather"][0]["description"])
# body is fully optional (bare {} = weather here, now)
patch(routes=[("geocoding-api", FakeResp(200, GEO)),
              ("api.open-meteo.com", FakeResp(200, forecast_payload()))])
check("POST /api/weather accepts an empty body",
      cl.post("/api/weather", json={}).status_code == 200)
check("bad units rejected by the schema",
      cl.post("/api/weather", json={"units": "kelvin"}).status_code == 422)

# === MCP tool is registered ===
from app.mcp_server import mcp
import asyncio
tools = {t.name for t in asyncio.run(mcp.list_tools())}
check("mcp exposes the weather tool", "weather" in tools)


# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
