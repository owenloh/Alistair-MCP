"""Media connector — open a web link, and transcribe a YouTube/Instagram video.

Two read-only capabilities, both dependency-light (httpx only, no ffmpeg / no
audio download in-process):

  * ``open_link``       — fetch ANY http(s) URL and return clean, readable content:
    final URL, status, page title, meta/OpenGraph description, and a plain-text
    excerpt of the body (tags stripped). YouTube links are enriched with their
    public oEmbed (title/author/thumbnail).
  * ``transcribe_video`` — return the SPOKEN transcript of a video link.
      - YouTube: pulls the video's own caption track directly (works out of the
        box, no key, no download) and returns the text + timed segments.
      - Instagram / caption-less YouTube / audio: there is no free caption track,
        so this proxies to an OPTIONAL external speech-to-text agent
        (TRANSCRIBE_AGENT_URL, same laptop-agent pattern as WhatsApp reads). If no
        agent is configured it returns a clean 503 explaining what is missing —
        it never fabricates a transcript.

Everything raises ``ServiceError`` on a recoverable failure so the router / MCP
layer returns a readable message instead of a 500.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from typing import Any

import httpx

from . import ServiceError
from ..config import Settings

# A browser-ish UA + language: some sites (YouTube included) serve a bare or
# consent-walled page to an unknown client.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
_TIMEOUT = httpx.Timeout(20.0)
_AGENT_TIMEOUT = httpx.Timeout(180.0)  # speech-to-text can be slow

_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}
_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}


class _NoCaptions(Exception):
    """Internal: a YouTube video has no usable caption track (fall back to the agent)."""


# --------------------------------------------------------------------------- #
# URL parsing / platform detection
# --------------------------------------------------------------------------- #

def _parse(url: str) -> urllib.parse.ParseResult:
    if not re.match(r"^https?://", url or "", re.I):
        url = "https://" + (url or "")
    return urllib.parse.urlparse(url)


def youtube_id(url: str) -> str | None:
    """Extract the 11-ish-char video id from any common YouTube URL form, else None."""
    u = _parse(url)
    host = (u.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return None
    if host == "youtu.be":
        vid = u.path.lstrip("/").split("/")[0]
        return vid or None
    if u.path == "/watch":
        v = urllib.parse.parse_qs(u.query).get("v", [None])[0]
        return v or None
    for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
        if u.path.startswith(prefix):
            return u.path[len(prefix):].split("/")[0] or None
    return None


def _is_instagram(url: str) -> bool:
    return (_parse(url).hostname or "").lower() in _INSTAGRAM_HOSTS


def _platform(url: str) -> str:
    if youtube_id(url):
        return "youtube"
    if _is_instagram(url):
        return "instagram"
    return "web"


# --------------------------------------------------------------------------- #
# HTTP helpers (module-level httpx so tests can monkeypatch media.httpx)
# --------------------------------------------------------------------------- #

def _get(url: str, *, headers: dict | None = None, params: dict | None = None) -> httpx.Response:
    h = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    try:
        return httpx.get(url, headers=h, params=params, timeout=_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as e:
        raise ServiceError(f"Network error fetching {url}.", status_code=502, detail=str(e)[:200])


# --------------------------------------------------------------------------- #
# open_link — fetch + extract readable content
# --------------------------------------------------------------------------- #

def _meta_content(text: str, key: str) -> str | None:
    """Value of a <meta property|name="key" content="..."> (order-insensitive)."""
    esc = re.escape(key)
    for attr in ("property", "name"):
        m = re.search(
            rf'<meta[^>]+{attr}=["\']{esc}["\'][^>]*content=["\'](.*?)["\']',
            text, re.I | re.DOTALL,
        )
        if m:
            return html.unescape(m.group(1)).strip()
        m = re.search(
            rf'<meta[^>]+content=["\'](.*?)["\'][^>]*{attr}=["\']{esc}["\']',
            text, re.I | re.DOTALL,
        )
        if m:
            return html.unescape(m.group(1)).strip()
    return None


def _visible_text(page: str) -> str:
    """Strip a HTML document down to readable plain text."""
    t = re.sub(r"(?is)<(script|style|noscript|template|svg)[^>]*>.*?</\1>", " ", page)
    t = re.sub(r"(?is)<br\s*/?>", "\n", t)
    t = re.sub(r"(?is)</(p|div|h[1-6]|li|tr|section|article)>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n[ \t]*\n\s*", "\n\n", t)
    return t.strip()


def open_link(settings: Settings, *, url: str, max_chars: int = 4000) -> dict:
    """Fetch a URL and return {final_url, status, title, description, excerpt, ...}."""
    if not url or not url.strip():
        raise ServiceError("url is required.", status_code=422)
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    max_chars = max(200, min(int(max_chars or 4000), 20000))

    resp = _get(url)
    ctype = resp.headers.get("content-type", "")
    out: dict[str, Any] = {
        "url": url,
        "final_url": str(resp.url),
        "status": resp.status_code,
        "content_type": ctype,
        "platform": _platform(url),
    }
    if resp.status_code >= 400:
        out["note"] = f"The page returned HTTP {resp.status_code}."

    is_htmlish = (not ctype) or ("html" in ctype) or ("xml" in ctype) or ctype.startswith("text")
    if not is_htmlish:
        out["note"] = f"Non-HTML content ({ctype}); returning headers/metadata only."
        if ctype.startswith("application/json"):
            out["text"] = resp.text[:max_chars]
        return out

    page = resp.text
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    out["title"] = html.unescape(title.group(1)).strip() if title else _meta_content(page, "og:title")
    out["description"] = _meta_content(page, "description") or _meta_content(page, "og:description")
    out["site_name"] = _meta_content(page, "og:site_name")
    out["og_image"] = _meta_content(page, "og:image")
    canon = re.search(r'(?is)<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](.*?)["\']', page)
    if canon:
        out["canonical"] = html.unescape(canon.group(1)).strip()

    body = _visible_text(page)
    out["characters"] = len(body)
    out["truncated"] = len(body) > max_chars
    out["excerpt"] = body[:max_chars]

    # Enrich YouTube with its public oEmbed (reliable title/author even if the
    # watch page is consent-walled). Best-effort — never fails the call.
    if out["platform"] == "youtube":
        try:
            oe = _get(
                "https://www.youtube.com/oembed",
                params={"url": url, "format": "json"},
            )
            if oe.status_code == 200:
                oj = oe.json()
                out["oembed"] = {
                    "title": oj.get("title"),
                    "author": oj.get("author_name"),
                    "thumbnail": oj.get("thumbnail_url"),
                }
        except Exception:
            pass

    out["note"] = out.get("note") or (
        "Read-only fetch of public web content. Summarise for the user; don't dump the raw excerpt."
    )
    return out


# --------------------------------------------------------------------------- #
# transcribe_video — YouTube captions, else the optional STT agent
# --------------------------------------------------------------------------- #

def _json_after(text: str, marker: str) -> dict | None:
    """Return the balanced JSON object that follows ``marker`` in ``text`` (or None).

    Brace-matched so it survives braces/quotes inside the JSON (a plain regex can't).
    """
    i = text.find(marker)
    if i == -1:
        return None
    i = text.find("{", i)
    if i == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:j + 1])
                except Exception:
                    return None
    return None


# YouTube's InnerTube player API returns the same player JSON (captions +
# videoDetails) as the watch page but is far less rate-limited than scraping HTML.
_INNERTUBE_PLAYER = "https://www.youtube.com/youtubei/v1/player"
_INNERTUBE_BODY = {
    "context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00", "hl": "en"}},
}


def _cookie_header(settings: Settings) -> str:
    """CONSENT cookie (sidesteps the EU consent interstitial) plus, if configured, the
    operator's logged-in YouTube session — which lifts YouTube's datacenter bot wall."""
    ck = (settings.youtube_cookies or "").strip()
    base = "CONSENT=YES+cb"
    return f"{base}; {ck}" if ck else base


def _captions_ok(data: dict | None) -> bool:
    return bool(data and data.get("captions"))


def _player_data(video_id: str, settings: Settings) -> dict:
    """Return YouTube player JSON (captions + videoDetails).

    Tries the InnerTube player API first (not HTML-scraped, rarely 429s), then falls
    back to scraping the watch page. Raises a precise ServiceError when YouTube blocks
    the server (datacenter bot wall) — the honest failure, never a fabricated result.
    """
    cookie = _cookie_header(settings)
    data: dict | None = None

    # 1) InnerTube player API (JSON in, JSON out).
    try:
        resp = httpx.post(
            _INNERTUBE_PLAYER,
            json={**_INNERTUBE_BODY, "videoId": video_id},
            headers={
                "User-Agent": _UA,
                "Content-Type": "application/json",
                "Origin": "https://www.youtube.com",
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": cookie,
            },
            timeout=_TIMEOUT,
        )
        if 200 <= resp.status_code < 300:
            data = resp.json()
    except Exception:
        data = None

    if _captions_ok(data):
        return data
    # Playable but genuinely no caption track — no point scraping.
    if data and ((data.get("playabilityStatus") or {}).get("status") == "OK"):
        return data

    # 2) Fallback: scrape the watch page (helps when the API is degraded).
    try:
        watch = _get(f"https://www.youtube.com/watch?v={video_id}&hl=en", headers={"Cookie": cookie})
    except ServiceError:
        watch = None
    if watch is not None and watch.status_code == 200:
        scraped = _json_after(watch.text, "ytInitialPlayerResponse")
        if scraped and (_captions_ok(scraped) or (scraped.get("playabilityStatus") or {}).get("status") == "OK"):
            return scraped
        if scraped and data is None:
            data = scraped

    # 3) Nothing usable — explain exactly why.
    status = (data or {}).get("playabilityStatus") or {}
    reason = status.get("reason") or ""
    if status.get("status") == "LOGIN_REQUIRED" or "bot" in reason.lower() or "sign in" in reason.lower():
        raise ServiceError(
            f'YouTube is blocking this server as a suspected bot ("{reason or "sign in required"}"). '
            "This is common from datacenter/cloud IPs (including Railway). Set YOUTUBE_COOKIES to a "
            "logged-in browser session (same idea as SPOTIFY_COOKIES), or configure TRANSCRIBE_AGENT_URL.",
            status_code=502,
        )
    if data is None:
        raise ServiceError(
            "Could not reach YouTube to read this video (it may be rate-limiting this server, or the "
            "video is private/unavailable).",
            status_code=502,
        )
    return data


def _caption_tracks(video_id: str, settings: Settings) -> tuple[list[dict], dict]:
    """Return (caption tracks, videoDetails) for a YouTube video."""
    data = _player_data(video_id, settings)
    tracks = (
        data.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    ) or []
    return tracks, data.get("videoDetails", {}) or {}


def _pick_track(tracks: list[dict], lang: str | None) -> dict | None:
    if not tracks:
        return None
    if lang:
        for t in tracks:
            if (t.get("languageCode") or "").lower().startswith(lang.lower()):
                return t
    # Prefer English, then a human (non-ASR) track, else the first available.
    return sorted(
        tracks,
        key=lambda t: (
            (t.get("languageCode") or "").lower().startswith("en"),
            t.get("kind") != "asr",
        ),
        reverse=True,
    )[0]


_XML_TEXT = re.compile(r'<text[^>]*\bstart="([\d.]+)"[^>]*>(.*?)</text>', re.DOTALL)


def _segments_from_timedtext(base_url: str) -> list[dict]:
    """Fetch a caption track's timedtext and return [{start, text}] segments."""
    if not base_url:
        return []
    sep = "&" if "?" in base_url else "?"
    # json3 is the clean, structured format.
    resp = _get(base_url + sep + "fmt=json3")
    segs: list[dict] = []
    if resp.status_code == 200 and resp.text.strip():
        try:
            data = resp.json()
        except Exception:
            data = None
        if data and data.get("events"):
            for ev in data["events"]:
                txt = "".join(s.get("utf8", "") for s in ev.get("segs", []) or [])
                txt = txt.strip()
                if txt:
                    segs.append({"start": round(ev.get("tStartMs", 0) / 1000, 2), "text": txt})
            if segs:
                return segs
    # Fall back to the XML format.
    resp = _get(base_url)
    if resp.status_code == 200 and resp.text.strip():
        for start, raw in _XML_TEXT.findall(resp.text):
            txt = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
            if txt:
                segs.append({"start": float(start), "text": txt})
    return segs


def _youtube_transcript(video_id: str, url: str, lang: str | None, settings: Settings) -> dict:
    tracks, details = _caption_tracks(video_id, settings)
    track = _pick_track(tracks, lang)
    if track is None:
        raise _NoCaptions()
    segments = _segments_from_timedtext(track.get("baseUrl", ""))
    if not segments:
        raise _NoCaptions()
    text = re.sub(r"\s+", " ", " ".join(s["text"] for s in segments)).strip()
    length = details.get("lengthSeconds")
    return {
        "source": "youtube-captions",
        "platform": "youtube",
        "url": url,
        "video_id": video_id,
        "title": details.get("title"),
        "author": details.get("author"),
        "language": track.get("languageCode"),
        "auto_generated": track.get("kind") == "asr",
        "duration_seconds": int(length) if str(length or "").isdigit() else None,
        "characters": len(text),
        "segment_count": len(segments),
        "text": text,
        "segments": segments,
        "note": (
            "Transcript pulled from the video's caption track"
            + (" (auto-generated captions — expect minor errors)." if track.get("kind") == "asr" else ".")
            + " Summarise for the user; don't dump the raw transcript."
        ),
    }


def _agent_transcribe(settings: Settings, url: str, lang: str | None, platform: str) -> dict:
    """Proxy to the optional external speech-to-text agent (Instagram / audio)."""
    base = (settings.transcribe_agent_url or "").rstrip("/")
    secret = settings.transcribe_agent_secret or ""
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    try:
        resp = httpx.post(
            base, json={"url": url, "lang": lang}, headers=headers, timeout=_AGENT_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise ServiceError(
            "The transcription agent is unreachable (it may be offline).",
            status_code=503,
            detail=str(e)[:200],
        )
    if resp.status_code == 401:
        raise ServiceError(
            "The transcription agent rejected the shared secret (check TRANSCRIBE_AGENT_SECRET).",
            status_code=502,
        )
    if not (200 <= resp.status_code < 300):
        raise ServiceError(
            f"The transcription agent returned HTTP {resp.status_code}.",
            status_code=502,
            detail=(resp.text or "")[:300],
        )
    try:
        data = resp.json()
    except Exception:
        raise ServiceError(
            "The transcription agent returned a non-JSON response.",
            status_code=502,
            detail=(resp.text or "")[:300],
        )
    text = (data.get("text") or "").strip()
    if not text:
        raise ServiceError(
            "The transcription agent returned no transcript for this link.",
            status_code=502,
        )
    return {
        "source": "transcription-agent",
        "platform": platform,
        "url": url,
        "title": data.get("title"),
        "language": data.get("language") or lang,
        "characters": len(text),
        "segments": data.get("segments"),
        "text": text,
        "note": "Transcript from the external speech-to-text agent. Summarise for the user.",
    }


def _agent_or_503(settings: Settings, url: str, lang: str | None, platform: str, reason: str) -> dict:
    if settings.transcribe_agent_configured:
        return _agent_transcribe(settings, url, lang, platform)
    raise ServiceError(
        f"{reason} Built-in transcription only covers YouTube videos that have a caption "
        "track. To transcribe Instagram, audio, or caption-less YouTube, point "
        "TRANSCRIBE_AGENT_URL at a speech-to-text agent (see .env.example).",
        status_code=503,
    )


def transcribe_video(settings: Settings, *, url: str, lang: str | None = None) -> dict:
    """Return the spoken transcript of a YouTube or Instagram video link.

    YouTube uses its own caption track (free). Instagram / caption-less YouTube /
    audio route to the optional TRANSCRIBE_AGENT_URL; without it, a clean 503.
    """
    if not url or not url.strip():
        raise ServiceError("url is required (a YouTube or Instagram video link).", status_code=422)
    url = url.strip()
    platform = _platform(url)

    if platform == "youtube":
        try:
            return _youtube_transcript(youtube_id(url), url, lang, settings)
        except _NoCaptions:
            return _agent_or_503(
                settings, url, lang, platform,
                reason="This YouTube video has no caption track.",
            )
        except ServiceError:
            # YouTube blocked/failed the read. If an STT agent is configured it can
            # still handle YouTube; otherwise surface the precise reason (bot wall etc).
            if settings.transcribe_agent_configured:
                return _agent_transcribe(settings, url, lang, platform)
            raise

    if platform == "instagram":
        return _agent_or_503(
            settings, url, lang, platform,
            reason="Instagram videos have no public caption track to read.",
        )

    # Any other link: only works if the agent can handle it.
    return _agent_or_503(
        settings, url, lang, platform,
        reason="That link isn't a recognised YouTube or Instagram video.",
    )
