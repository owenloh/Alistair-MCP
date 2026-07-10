"""Tests for the media connector (open_link + transcribe_video).

Network is faked by monkeypatching `media.httpx` with a stub exposing `.get`/`.post`
(routed by URL substring) and `HTTPError`, so nothing hits YouTube/Instagram/the web.
Covers: YouTube URL parsing, open_link HTML extraction, YouTube caption transcription
(json3 + XML fallback), the "no captions"/Instagram -> 503 path, and the external agent
fallback. A TestClient pass checks the routes + manifest wiring.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx as _httpx  # real, for HTTPError

os.environ.pop("SERVICE_API_KEY", None)
os.environ.pop("TRANSCRIBE_AGENT_URL", None)
os.environ.pop("TRANSCRIBE_AGENT_SECRET", None)

from app.config import Settings
from app.services import ServiceError
from app.services import media

R = []
def check(name, cond):
    R.append((name, bool(cond)))


# ---- fake httpx: route URL substring -> FakeResp (for .get and .post) ----
class FakeResp:
    def __init__(self, status=200, payload=None, text="", headers=None, url=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {}
        self.url = url or "https://example.com/final"
    def json(self):
        if self._payload is None:
            return json.loads(self.text)
        return self._payload

class FakeHttpx:
    HTTPError = _httpx.HTTPError
    def __init__(self, routes=None, raise_exc=None):
        self.routes = routes or []   # list of (substr, FakeResp)
        self.raise_exc = raise_exc
        self.calls = []
    def _match(self, url):
        for sub, resp in self.routes:
            if sub in url:
                return resp
        raise AssertionError(f"no fake route for {url}")
    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if self.raise_exc:
            raise self.raise_exc
        return self._match(url)
    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if self.raise_exc:
            raise self.raise_exc
        return self._match(url)

def patch(routes=None, raise_exc=None):
    media.httpx = FakeHttpx(routes, raise_exc)
    return media.httpx


st_plain = Settings()
st_agent = Settings(transcribe_agent_url="http://laptop.ts.net:9000/transcribe",
                    transcribe_agent_secret="sek")


# === URL parsing / platform detection ===
check("youtube watch id", media.youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ")
check("youtu.be id", media.youtube_id("https://youtu.be/dQw4w9WgXcQ?si=x") == "dQw4w9WgXcQ")
check("youtube shorts id", media.youtube_id("https://youtube.com/shorts/abc123DEF45") == "abc123DEF45")
check("non-youtube -> None", media.youtube_id("https://example.com/watch?v=x") is None)
check("platform youtube", media._platform("https://youtu.be/abc") == "youtube")
check("platform instagram", media._platform("https://www.instagram.com/reel/XYZ/") == "instagram")
check("platform web", media._platform("https://example.com/post") == "web")


# === open_link: extracts title/description/excerpt from HTML ===
HTML_PAGE = (
    "<html><head><title>Hello &amp; World</title>"
    '<meta name="description" content="A test page.">'
    '<meta property="og:site_name" content="ExampleSite">'
    "</head><body><script>var x=1;</script>"
    "<h1>Heading</h1><p>First para.</p><p>Second para.</p></body></html>"
)
patch(routes=[("example.com", FakeResp(200, text=HTML_PAGE,
                                       headers={"content-type": "text/html; charset=utf-8"},
                                       url="https://example.com/page"))])
res = media.open_link(st_plain, url="https://example.com/page")
check("open_link title unescaped", res["title"] == "Hello & World")
check("open_link description", res["description"] == "A test page.")
check("open_link site_name", res["site_name"] == "ExampleSite")
check("open_link strips scripts + tags", "var x=1" not in res["excerpt"] and "First para." in res["excerpt"])
check("open_link final_url", res["final_url"] == "https://example.com/page")
check("open_link platform web", res["platform"] == "web")

# bare host gets https:// prefix (no crash), and empty url errors
try:
    media.open_link(st_plain, url="")
    check("open_link empty -> error", False)
except ServiceError as e:
    check("open_link empty -> 422", e.status_code == 422)


# === transcribe_video: YouTube captions via json3 ===
PLAYER = {
    "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [
        {"baseUrl": "https://yt.timedtext/en", "languageCode": "en", "kind": "asr"},
    ]}},
    "videoDetails": {"title": "My Video", "author": "Chan", "lengthSeconds": "120"},
}
WATCH_HTML = "var ytInitialPlayerResponse = " + json.dumps(PLAYER) + "; var other = {};"
TIMEDTEXT_JSON3 = {"events": [
    {"tStartMs": 0, "segs": [{"utf8": "hello "}, {"utf8": "world"}]},
    {"tStartMs": 1500, "segs": [{"utf8": "second line"}]},
    {"tStartMs": 3000, "segs": [{"utf8": "\n"}]},  # whitespace-only -> dropped
]}
# Primary transport is the InnerTube player API (POST youtubei/v1/player).
patch(routes=[
    ("youtubei/v1/player", FakeResp(200, payload=PLAYER)),
    ("timedtext/en", FakeResp(200, payload=TIMEDTEXT_JSON3)),
])
tr = media.transcribe_video(st_plain, url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
check("yt transcript source", tr["source"] == "youtube-captions")
check("yt transcript text joined", tr["text"] == "hello world second line")
check("yt transcript segments", tr["segment_count"] == 2)
check("yt transcript title/author", tr["title"] == "My Video" and tr["author"] == "Chan")
check("yt transcript auto_generated", tr["auto_generated"] is True)
check("yt transcript duration", tr["duration_seconds"] == 120)
check("yt transcript language", tr["language"] == "en")

# XML fallback when json3 is empty
TIMEDTEXT_XML = '<transcript><text start="0" dur="2">hi &amp; bye</text>' \
                '<text start="2.5" dur="1">next</text></transcript>'
patch(routes=[
    ("youtubei/v1/player", FakeResp(200, payload=PLAYER)),
    ("fmt=json3", FakeResp(200, text="")),          # empty -> triggers XML fallback
    ("timedtext/en", FakeResp(200, text=TIMEDTEXT_XML)),
])
tr2 = media.transcribe_video(st_plain, url="https://youtu.be/dQw4w9WgXcQ")
check("yt XML fallback text", tr2["text"] == "hi & bye next")
check("yt XML fallback segments", tr2["segment_count"] == 2)

# player degraded but watch-page fallback carries the captions
patch(routes=[
    ("youtubei/v1/player", FakeResp(200, payload={"videoDetails": {"title": "x"}})),  # no captions, no OK status
    ("watch?v=", FakeResp(200, text=WATCH_HTML, headers={"content-type": "text/html"})),
    ("timedtext/en", FakeResp(200, payload=TIMEDTEXT_JSON3)),
])
trw = media.transcribe_video(st_plain, url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
check("yt watch-page fallback works", trw["text"] == "hello world second line")


# === no captions -> 503 when no agent; agent fallback when configured ===
# Player says the video is playable (status OK) but has no caption track.
NO_CAP_PLAYER = {"captions": {}, "playabilityStatus": {"status": "OK"},
                 "videoDetails": {"title": "Silent"}}
patch(routes=[("youtubei/v1/player", FakeResp(200, payload=NO_CAP_PLAYER))])
try:
    media.transcribe_video(st_plain, url="https://www.youtube.com/watch?v=noCaps00000")
    check("yt no captions -> error", False)
except ServiceError as e:
    check("yt no captions -> 503", e.status_code == 503 and "caption" in e.message.lower())

# === YouTube bot wall (LOGIN_REQUIRED) -> precise 502 mentioning cookies ===
BOT_PLAYER = {"playabilityStatus": {"status": "LOGIN_REQUIRED",
                                    "reason": "Sign in to confirm you're not a bot"}}
patch(routes=[
    ("youtubei/v1/player", FakeResp(200, payload=BOT_PLAYER)),
    ("watch?v=", FakeResp(429, text="")),  # watch page also throttled
])
try:
    media.transcribe_video(st_plain, url="https://www.youtube.com/watch?v=blocked00000")
    check("yt bot wall -> error", False)
except ServiceError as e:
    check("yt bot wall -> 502 + cookies hint",
          e.status_code == 502 and "YOUTUBE_COOKIES" in e.message)

# bot wall BUT agent configured -> falls through to the agent
patch(routes=[
    ("youtubei/v1/player", FakeResp(200, payload=BOT_PLAYER)),
    ("watch?v=", FakeResp(429, text="")),
    ("laptop.ts.net", FakeResp(200, payload={"text": "yt via agent"})),
])
tb = media.transcribe_video(st_agent, url="https://www.youtube.com/watch?v=blocked00000")
check("yt bot wall + agent -> agent transcript",
      tb["source"] == "transcription-agent" and tb["text"] == "yt via agent")

# Instagram with no agent -> 503
patch(routes=[])
try:
    media.transcribe_video(st_plain, url="https://www.instagram.com/reel/ABC123/")
    check("instagram no agent -> error", False)
except ServiceError as e:
    check("instagram no agent -> 503", e.status_code == 503)

# Instagram WITH agent configured -> proxied transcript
patch(routes=[("laptop.ts.net", FakeResp(200, payload={"text": "reel words here", "title": "Reel"}))])
ig = media.transcribe_video(st_agent, url="https://www.instagram.com/reel/ABC123/")
check("instagram agent source", ig["source"] == "transcription-agent")
check("instagram agent text", ig["text"] == "reel words here")
check("instagram agent platform", ig["platform"] == "instagram")
# the agent got the bearer secret + url
last = media.httpx.calls[-1]
check("agent POST carried url", last[0] == "POST" and last[2]["json"]["url"].endswith("/reel/ABC123/"))
check("agent POST carried bearer", last[2]["headers"]["Authorization"] == "Bearer sek")

# agent returning empty text -> clean 502
patch(routes=[("laptop.ts.net", FakeResp(200, payload={"text": "  "}))])
try:
    media.transcribe_video(st_agent, url="https://www.instagram.com/reel/ZZZ/")
    check("agent empty -> error", False)
except ServiceError as e:
    check("agent empty text -> 502", e.status_code == 502)

# missing url -> 422
try:
    media.transcribe_video(st_plain, url="")
    check("transcribe empty -> error", False)
except ServiceError as e:
    check("transcribe empty -> 422", e.status_code == 422)


# === TestClient wiring: routes exist + manifest lists media ===
from fastapi.testclient import TestClient
from app.main import app
cl = TestClient(app)
mani = cl.get("/api/manifest").json()
check("manifest has media group", "media" in mani["function_apis"])
check("manifest media has 2 endpoints", mani["counts"].get("media") == 2)
paths = {e["path"] for e in mani["function_apis"]["media"]}
check("manifest lists open-link + transcribe",
      "/api/media/open-link" in paths and "/api/media/transcribe" in paths)
# validation: missing url -> 422 from FastAPI
check("open-link requires url", cl.post("/api/media/open-link", json={}).status_code == 422)


# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
