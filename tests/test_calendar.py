"""Tests for the Google Calendar layer, focused on timezone-offset handling.

Google's Calendar API rejects a naive "floating" datetime (no UTC offset) on
``timeMin``/``timeMax`` and on event ``dateTime`` values with HTTP 400 — the
``timeZone`` param does not supply the offset for those bounds. The service now
normalizes naive local times by appending the resolved zone's (DST-correct)
offset, so Alistair can pass a bare local time and it "works directly".

HTTP calls go through a fake httpx (routed by method + URL substring) and token
minting is stubbed, so the real request-building runs without touching the
network. A TestClient pass checks the HTTP wiring.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# No api-key guard for the HTTP pass; Google trio present so calls are "configured".
os.environ.pop("SERVICE_API_KEY", None)
os.environ["GOOGLE_REFRESH_TOKEN"] = "r"
os.environ["GOOGLE_CLIENT_ID"] = "c"
os.environ["GOOGLE_CLIENT_SECRET"] = "s"
# Pin a DST-observing zone and disable live auto-detect so tz resolution is offline
# and deterministic (Europe/London: +01:00 in summer, +00:00 in winter).
os.environ["TIMEZONE_AUTO"] = "false"
os.environ["CALENDAR_TIMEZONE"] = "Europe/London"

from app.config import Settings
from app.services import calendar as cal

R = []
def check(name, cond):
    R.append((name, bool(cond)))


# ---- fake httpx for the calendar module: route (method, substr) -> FakeResp ----
class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

class FakeHttpx:
    def __init__(self, routes):
        self.routes = routes  # list of (method, substr, FakeResp)
        self.calls = []
    def _route(self, method, url):
        for m, sub, resp in self.routes:
            if m == method and sub in url:
                return resp
        raise AssertionError(f"no fake route for {method} {url}")
    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json))
        return self._route(method, url)
    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, None))
        return self._route("GET", url)
    def post(self, url, params=None, json=None, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, params, json))
        return self._route("POST", url)

def patch(routes):
    fake = FakeHttpx(routes)
    cal.httpx = fake
    cal._access_token = lambda s: "tok"       # bypass real OAuth
    return fake

st = Settings()


# === _ensure_offset: the core normalization ===
# Naive local time gets the DST-correct offset for the resolved zone.
check("naive summer -> BST +01:00",
      cal._ensure_offset("2026-07-02T14:00:00", "Europe/London") == "2026-07-02T14:00:00+01:00")
check("naive winter -> GMT +00:00",
      cal._ensure_offset("2026-01-02T14:00:00", "Europe/London") == "2026-01-02T14:00:00+00:00")
check("naive summer in NY -> -04:00",
      cal._ensure_offset("2026-07-02T14:00:00", "America/New_York") == "2026-07-02T14:00:00-04:00")
# An explicit offset (or Z) is authoritative — never rewritten.
check("existing offset passes through unchanged",
      cal._ensure_offset("2026-07-02T14:00:00+05:00", "Europe/London") == "2026-07-02T14:00:00+05:00")
check("trailing Z passes through unchanged",
      cal._ensure_offset("2026-07-02T14:00:00Z", "Europe/London") == "2026-07-02T14:00:00Z")
# Date-only (start of day) is localized so list bounds are still accepted.
check("date-only -> midnight local with offset",
      cal._ensure_offset("2026-07-02", "Europe/London") == "2026-07-02T00:00:00+01:00")
# Defensive: empty / None / unparseable are returned untouched (Google validates).
check("None passes through", cal._ensure_offset(None, "Europe/London") is None)
check("empty string passes through", cal._ensure_offset("", "Europe/London") == "")
check("unparseable passes through", cal._ensure_offset("next tuesday", "Europe/London") == "next tuesday")


# === list_events: naive bounds are sent to Google WITH an offset ===
fake = patch([("GET", "/events", FakeResp(200, {"items": []}))])
cal.list_events(st, start_time="2026-07-02T00:00:00", end_time="2026-07-03T00:00:00")
_, _, params, _ = fake.calls[-1]
check("list timeMin gained offset", params["timeMin"] == "2026-07-02T00:00:00+01:00")
check("list timeMax gained offset", params["timeMax"] == "2026-07-03T00:00:00+01:00")
check("list still passes timeZone name", params["timeZone"] == "Europe/London")

# Explicit offset on input is preserved verbatim (no double-conversion).
fake = patch([("GET", "/events", FakeResp(200, {"items": []}))])
cal.list_events(st, start_time="2026-07-02T00:00:00-07:00")
_, _, params, _ = fake.calls[-1]
check("list preserves caller's explicit offset", params["timeMin"] == "2026-07-02T00:00:00-07:00")


# === create_event: event dateTime carries an offset ===
_created = {
    "id": "ev1", "summary": "Sync", "htmlLink": "http://x",
    "start": {"dateTime": "2026-07-02T14:00:00+01:00"},
    "end": {"dateTime": "2026-07-02T15:00:00+01:00"},
}
fake = patch([("POST", "/events", FakeResp(200, _created))])
cal.create_event(st, summary="Sync", start_time="2026-07-02T14:00:00", end_time="2026-07-02T15:00:00")
_, _, _, body = fake.calls[-1]
check("create start dateTime gained offset", body["start"]["dateTime"] == "2026-07-02T14:00:00+01:00")
check("create end dateTime gained offset", body["end"]["dateTime"] == "2026-07-02T15:00:00+01:00")
check("create keeps timeZone alongside offset", body["start"]["timeZone"] == "Europe/London")

# All-day create stays a bare date (no offset, no dateTime).
fake = patch([("POST", "/events", FakeResp(200, {
    "id": "ev2", "summary": "Holiday",
    "start": {"date": "2026-07-02"}, "end": {"date": "2026-07-03"}})),
])
cal.create_event(st, summary="Holiday", start_time="2026-07-02", end_time="2026-07-03", all_day=True)
_, _, _, body = fake.calls[-1]
check("all-day create emits date only", body["start"] == {"date": "2026-07-02"})


# === suggest_time: naive window bounds are normalized before freeBusy ===
fake = patch([("POST", "/freeBusy", FakeResp(200, {"calendars": {}}))])
out = cal.suggest_time(st, attendee_emails=["primary"],
                       start_time="2026-07-02T09:00:00", end_time="2026-07-02T17:00:00")
_, _, _, body = fake.calls[-1]
check("suggest_time timeMin gained offset", body["timeMin"] == "2026-07-02T09:00:00+01:00")
check("suggest_time returns suggestions list", isinstance(out.get("suggestions"), list))


# === HTTP layer via TestClient: create-event wiring, naive time accepted ===
from app import config as _cfg
_cfg.get_settings.cache_clear()
cal._access_token = lambda s: "tok"
cal.httpx = FakeHttpx([("POST", "/events", FakeResp(200, _created))])
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
resp = c.post("/api/calendar/create-event", json={
    "summary": "Sync", "startTime": "2026-07-02T14:00:00", "endTime": "2026-07-02T15:00:00",
})
check("HTTP create-event 200 with naive time", resp.status_code == 200)
_, _, _, body = cal.httpx.calls[-1]
check("HTTP path also normalized the offset", body["start"]["dateTime"] == "2026-07-02T14:00:00+01:00")


# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
