"""Spotify control via SpotAPI (https://github.com/Aran404/SpotAPI).

This is the unofficial-API connector: it drives the user's Spotify the way the web
player does (Spotify Connect over a websocket), so it needs NO Spotify Developer
app and NO official OAuth — just a logged-in web session (cookies). It lets
Alistair browse playlists, search + play specific songs, and list/choose the
device playback runs on.

Design notes (why this file looks the way it does):

* **Lazy, graceful.** `spotapi` is imported only inside the helpers, so the app
  still boots and every other connector works even if the package or the cookies
  are missing — those calls just return a clean 503 (same policy as Notion/GitHub).
* **Cookie auth.** Password login needs a paid CAPTCHA solver, so the durable path
  is a logged-in session: set `SPOTIFY_COOKIES` (the open.spotify.com cookies, at
  minimum `sp_dc`) + `SPOTIFY_USERNAME`. `Login.from_cookies` does no network on
  construction; the first real call exercises the token.
* **Off-main-thread websocket.** SpotAPI's `WebsocketStreamer.__init__` calls
  `signal.signal(SIGINT, ...)`, which raises in any non-main thread — i.e. every
  web-server worker. `_no_signal()` neutralizes that during construction (the
  handler is only for CLI Ctrl+C, irrelevant on a server). We also close the
  websocket after each call so the per-request Player/PlayerStatus doesn't leak.
* **Reverse-engineered JSON.** Playlist/search responses are deeply nested
  GraphQL; the parsers extract the useful fields defensively and fall back to a
  recursive scan for `spotify:…` URIs, so a schema tweak degrades instead of 500s.

Everything returns a plain dict and raises ServiceError on a reportable failure.
"""
from __future__ import annotations

import contextlib
import json
import signal
import threading
from typing import Any

from . import ServiceError
from ..config import Settings

# Spotify URI helpers -----------------------------------------------------------
_TRACK = "spotify:track:"
_PLAYLIST = "spotify:playlist:"

# spotapi calls signal.signal() in a worker thread; serialize the temporary patch.
_signal_lock = threading.Lock()


# ---- lazy import + auth -------------------------------------------------------
def _spotapi():
    """Import spotapi lazily so a missing package never breaks app boot.

    Returns the handful of names this module needs. Raises a clean 503 (not an
    import crash) if the dependency isn't installed.
    """
    try:
        import spotapi  # noqa: F401
        from spotapi import (
            Config,
            Login,
            NoopLogger,
            Player,
            PlayerStatus,
            PublicPlaylist,
            PrivatePlaylist,
            Song,
        )
        from spotapi.http.request import TLSClient
    except Exception as e:  # ImportError or a transitive native-dep failure
        raise ServiceError(
            "Spotify support needs the 'spotapi[websocket]' package "
            f"({type(e).__name__}: {e}). Add it to requirements and redeploy.",
            status_code=503,
        )
    return {
        "Config": Config, "Login": Login, "NoopLogger": NoopLogger,
        "Player": Player, "PlayerStatus": PlayerStatus,
        "PublicPlaylist": PublicPlaylist, "PrivatePlaylist": PrivatePlaylist,
        "Song": Song, "TLSClient": TLSClient,
    }


def _parse_cookies(raw: str) -> Any:
    """Accept either a JSON object of cookies or a raw 'k=v; k2=v2' string.

    spotapi's from_cookies handles both a Mapping and a ';'-separated string, so
    we just normalise JSON to a dict and pass a plain string through untouched.
    """
    raw = (raw or "").strip()
    if not raw:
        return raw
    if raw[0] in "{[":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw


def _login(settings: Settings):
    """Build a logged-in SpotAPI session from the configured cookies.

    NoopLogger is mandatory here: spotapi's default stdout Logger.fatal() calls
    os._exit(1), which would kill the whole web server on any internal error.
    """
    if not (settings.spotify_cookies and settings.spotify_username):
        raise ServiceError(
            "Spotify is not configured. Set SPOTIFY_COOKIES (your open.spotify.com "
            "session cookies, at minimum sp_dc) and SPOTIFY_USERNAME.",
            status_code=503,
        )
    sp = _spotapi()
    cfg = sp["Config"](
        logger=sp["NoopLogger"](),
        solver=None,
        client=sp["TLSClient"]("chrome120", "", auto_retries=3),
    )
    dump = {
        "identifier": settings.spotify_username,
        "password": settings.spotify_password or "",
        "cookies": _parse_cookies(settings.spotify_cookies),
    }
    try:
        return sp, sp["Login"].from_cookies(dump, cfg)
    except Exception as e:
        raise ServiceError(_clean_err("Could not start a Spotify session", e), status_code=502)


@contextlib.contextmanager
def _no_signal():
    """Neutralise signal.signal() while spotapi opens its websocket.

    WebsocketStreamer registers a SIGINT handler in __init__; signal.signal only
    works in the main thread and raises everywhere else (every web worker). We
    swap in a shim that still honours main-thread registration (so CLI/tests are
    unaffected) but swallows the off-thread ValueError.
    """
    with _signal_lock:
        original = signal.signal

        def _shim(sig, handler):
            try:
                return original(sig, handler)
            except (ValueError, RuntimeError):
                return None  # not the main thread — ignore the CLI-only handler

        signal.signal = _shim
        try:
            yield
        finally:
            signal.signal = original


@contextlib.contextmanager
def _player_status(settings: Settings):
    """A read-only PlayerStatus (devices + state), websocket closed on exit."""
    sp, login = _login(settings)
    obj = None
    try:
        with _no_signal():
            obj = sp["PlayerStatus"](login)
        yield obj
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError(_clean_err("Spotify player error", e), status_code=502)
    finally:
        _close(obj)


@contextlib.contextmanager
def _player(settings: Settings, device_id: str | None = None):
    """A control Player (constructing it transfers playback to device_id/active).

    Player.__init__ needs an existing playback session (an active device + a play
    origin); if there is none it raises, which we turn into an actionable 409.
    """
    sp, login = _login(settings)
    obj = None
    try:
        with _no_signal():
            obj = sp["Player"](login, device_id)
        yield obj
    except ServiceError:
        raise
    except ValueError as e:
        raise ServiceError(
            "No active Spotify device. Open Spotify on a phone/desktop/web player "
            f"and start playing something, then try again. ({e})",
            status_code=409,
        )
    except Exception as e:
        raise ServiceError(_clean_err("Spotify player error", e), status_code=502)
    finally:
        _close(obj)


def _close(obj) -> None:
    ws = getattr(obj, "ws", None)
    if ws is not None:
        with contextlib.suppress(Exception):
            ws.close()


def _clean_err(prefix: str, e: Exception) -> str:
    msg = str(e).strip().replace("\n", " ")
    return f"{prefix}: {msg[:300]}" if msg else prefix


# ---- id / uri helpers ---------------------------------------------------------
def _track_uri(track: str) -> str:
    """Normalise a track ref (bare id, URL, or URI) to a spotify:track: URI."""
    t = track.strip()
    if t.startswith(_TRACK):
        return t
    if "track/" in t:  # open.spotify.com/track/<id>?...
        t = t.split("track/")[-1].split("?")[0]
    elif "track:" in t:
        t = t.split("track:")[-1]
    return f"{_TRACK}{t}"


def _playlist_uri(playlist: str) -> str:
    p = playlist.strip()
    if p.startswith(_PLAYLIST):
        return p
    if "playlist/" in p:
        p = p.split("playlist/")[-1].split("?")[0]
    elif "playlist:" in p:
        p = p.split("playlist:")[-1]
    return f"{_PLAYLIST}{p}"


# ---- response parsing (defensive) --------------------------------------------
def _artist_names(data: dict) -> list[str]:
    out: list[str] = []
    artists = (data.get("artists") or {}).get("items") or []
    for a in artists:
        name = (a.get("profile") or {}).get("name") or a.get("name")
        if name:
            out.append(name)
    return out


def _clean_track(data: dict) -> dict:
    """Pull the human-useful fields out of a GraphQL track node."""
    album = (data.get("albumOfTrack") or data.get("album") or {})
    dur = data.get("duration") or {}
    return {
        "name": data.get("name"),
        "uri": data.get("uri"),
        "artists": _artist_names(data),
        "album": album.get("name"),
        "duration_ms": dur.get("totalMilliseconds"),
    }


def _walk_uris(obj: Any, kind: str, seen: set[str], out: list[dict]) -> None:
    """Recursively collect dict nodes whose 'uri' is of the given kind.

    A resilient fallback for when the exact GraphQL path drifts: we still find the
    playlists/tracks by their stable URI shape rather than 500-ing.
    """
    if isinstance(obj, dict):
        uri = obj.get("uri")
        if isinstance(uri, str) and uri.startswith(kind) and uri not in seen and obj.get("name"):
            seen.add(uri)
            out.append(obj)
        for v in obj.values():
            _walk_uris(v, kind, seen, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_uris(v, kind, seen, out)


def _extract_playlists(resp: Any, limit: int) -> list[dict]:
    nodes: list[dict] = []
    seen: set[str] = set()
    # Known path first: data.me.libraryV3.items[].item.data
    try:
        items = resp["data"]["me"]["libraryV3"]["items"]
        for it in items:
            data = (it.get("item") or {}).get("data") or it.get("data") or {}
            uri = data.get("uri", "")
            if uri.startswith(_PLAYLIST) and uri not in seen and data.get("name"):
                seen.add(uri)
                nodes.append(data)
    except (KeyError, TypeError, AttributeError):
        pass
    if not nodes:  # fallback: scan for any playlist URIs in the payload
        _walk_uris(resp, _PLAYLIST, seen, nodes)
    out = []
    for d in nodes[:limit]:
        owner = (d.get("ownerV2") or {}).get("data") or d.get("owner") or {}
        count = ((d.get("content") or {}).get("totalCount")
                 or (d.get("totalLength")))
        out.append({
            "name": d.get("name"),
            "uri": d.get("uri"),
            "owner": owner.get("name") or owner.get("username"),
            "description": d.get("description"),
            "total_tracks": count,
        })
    return out


def _extract_search_tracks(items: list, limit: int) -> list[dict]:
    out: list[dict] = []
    for it in items:
        data = (it.get("item") or {}).get("data") or it.get("data") or it
        if isinstance(data, dict) and (data.get("uri") or "").startswith(_TRACK):
            out.append(_clean_track(data))
        if len(out) >= limit:
            break
    if not out:  # fallback scan
        nodes: list[dict] = []
        _walk_uris(items, _TRACK, set(), nodes)
        out = [_clean_track(n) for n in nodes[:limit]]
    return out


def _device_dict(dev) -> dict:
    """A spotapi Device dataclass -> a clean dict (volume normalised to 0-100)."""
    vol = getattr(dev, "volume", None)
    return {
        "id": getattr(dev, "device_id", None),
        "name": getattr(dev, "name", None),
        "type": getattr(dev, "device_type", None),
        "volume_percent": round(vol / 65535 * 100) if isinstance(vol, (int, float)) else None,
        "can_play": getattr(dev, "can_play", None),
    }


# ---- operations: browse (read-only, no websocket) -----------------------------
def list_playlists(settings: Settings, *, limit: int = 50) -> dict:
    """the user's library playlists (logged-in). Read-only."""
    sp, login = _login(settings)
    pp = sp["PrivatePlaylist"](login)
    try:
        resp = pp.get_library(limit)
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError(_clean_err("Could not list playlists", e), status_code=502)
    playlists = _extract_playlists(resp, limit)
    return {"count": len(playlists), "playlists": playlists}


def playlist_tracks(settings: Settings, playlist: str, *, limit: int = 50) -> dict:
    """Tracks in a playlist (works on any public/owned playlist URI). Read-only."""
    sp, login = _login(settings)
    uri = _playlist_uri(playlist)
    pid = uri.split(":")[-1]
    try:
        info = sp["PublicPlaylist"](pid, client=login.client).get_playlist_info(limit=min(limit, 343))
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError(_clean_err("Could not read playlist", e), status_code=502)
    content = (((info.get("data") or {}).get("playlistV2") or {}).get("content") or {})
    items = content.get("items") or []
    tracks = []
    for it in items[:limit]:
        data = (it.get("itemV2") or {}).get("data") or {}
        t = _clean_track(data)
        t["uid"] = it.get("uid")
        tracks.append(t)
    name = (((info.get("data") or {}).get("playlistV2") or {}).get("name"))
    return {
        "playlist": {"name": name, "uri": uri},
        "total_tracks": content.get("totalCount"),
        "count": len(tracks),
        "tracks": tracks,
    }


def search_tracks(settings: Settings, query: str, *, limit: int = 10) -> dict:
    """Search the Spotify catalogue for tracks. Read-only. Returns track URIs."""
    sp, login = _login(settings)
    try:
        resp = sp["Song"](client=login.client).query_songs(query, limit=min(limit, 100))
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError(_clean_err("Could not search Spotify", e), status_code=502)
    items = ((((resp.get("data") or {}).get("searchV2") or {}).get("tracksV2") or {}).get("items")) or []
    tracks = _extract_search_tracks(items, limit)
    return {"query": query, "count": len(tracks), "tracks": tracks}


# ---- operations: devices / status (websocket, read-only) ----------------------
def list_devices(settings: Settings) -> dict:
    """Available Spotify Connect devices + which one is active. Read-only."""
    with _player_status(settings) as ps:
        devs = ps.device_ids
    devices = [_device_dict(d) for d in devs.devices.values()]
    active = devs.active_device_id
    for d in devices:
        d["active"] = d["id"] == active
    return {
        "count": len(devices),
        "active_device_id": active,
        "devices": devices,
        "hint": ("If empty, open Spotify on a phone/desktop/web player so it appears "
                 "as a Connect device."),
    }


def now_playing(settings: Settings) -> dict:
    """What's playing right now + the active device + shuffle/repeat. Read-only."""
    with _player_status(settings) as ps:
        state = ps.state
        devs = ps.device_ids
    track = state.track
    md = getattr(track, "metadata", None)
    opts = state.options
    active_id = devs.active_device_id
    active = devs.devices.get(active_id) if active_id else None
    out = {
        "is_playing": (state.is_paused is False) if state.is_paused is not None else state.is_playing,
        "is_paused": state.is_paused,
        "track": {
            "title": getattr(md, "title", None),
            "album": getattr(md, "album_title", None),
            "uri": getattr(track, "uri", None),
            "artist_uri": getattr(md, "artist_uri", None),
        } if track else None,
        "context": getattr(state.context_metadata, "context_description", None) if state.context_metadata else None,
        "context_uri": state.context_uri,
        "shuffle": getattr(opts, "shuffling_context", None) if opts else None,
        "repeat_track": getattr(opts, "repeating_track", None) if opts else None,
        "repeat_context": getattr(opts, "repeating_context", None) if opts else None,
        "active_device": _device_dict(active) if active else None,
        "up_next": [getattr(getattr(t, "metadata", None), "title", None) for t in (state.next_tracks or [])[:5]],
    }
    return out


# ---- operations: control (websocket, write) -----------------------------------
def _resolve_device(devs, device: str) -> str:
    """Map a device id OR a (case-insensitive, substring) name to a device id."""
    if device in devs.devices:
        return device
    needle = device.strip().lower()
    matches = [d for d in devs.devices.values()
               if needle in (getattr(d, "name", "") or "").lower()]
    if len(matches) == 1:
        return matches[0].device_id
    available = [{"id": d.device_id, "name": d.name} for d in devs.devices.values()]
    if not matches:
        raise ServiceError(f"No device matches '{device}'.", status_code=404, detail=available)
    raise ServiceError(
        f"'{device}' matches {len(matches)} devices; be more specific.",
        status_code=409, detail=[{"id": m.device_id, "name": m.name} for m in matches],
    )


def transfer_playback(settings: Settings, device: str) -> dict:
    """Move playback to a chosen device (by id or name). Needs an active session."""
    # Resolve the target name->id first (read-only), then build the Player on it
    # (constructing Player(device_id=...) performs the transfer).
    with _player_status(settings) as ps:
        target = _resolve_device(ps.device_ids, device)
        name = ps.device_ids.devices[target].name
    with _player(settings, device_id=target):
        pass
    return {"transferred": True, "device": {"id": target, "name": name}}


def _find_track_uid(sp: dict, client, playlist_id: str, track_id: str) -> str | None:
    """Find a track's playlist-item uid by reading the playlist over the AUTHENTICATED
    session. SpotAPI's own Player.play_track re-fetches the playlist with a fresh
    unauthenticated client, which returns a shape missing 'content' (KeyError) — so we
    resolve the uid ourselves and drive the lower-level _play_song directly.
    """
    gen = sp["PublicPlaylist"](playlist_id, client=client).paginate_playlist()
    try:
        for chunk in gen:
            uids, _stop = sp["Song"].parse_playlist_items(
                chunk.get("items", []), song_id=track_id, all_instances=True)
            if uids:
                return uids[0]
    finally:
        gen.close()
    return None


def play(settings: Settings, *, track: str, playlist: str | None = None,
         device: str | None = None) -> dict:
    """Play a specific track.

    With a playlist URI it plays the track IN that playlist's context (the proper
    Spotify behaviour). Without one it falls back to queueing the track and
    skipping to it. An optional device sends playback there first.
    """
    track_uri = _track_uri(track)
    target_id = None
    if device:
        with _player_status(settings) as ps:
            target_id = _resolve_device(ps.device_ids, device)
    with _player(settings, device_id=target_id) as p:
        if playlist:
            sp = _spotapi()
            pid = _playlist_uri(playlist).split(":")[-1]
            tid = track_uri.split(":")[-1]
            uid = _find_track_uid(sp, p.client, pid, tid)
            if uid is None:
                raise ServiceError(
                    f"Track {tid} is not in playlist {pid} — play it without a "
                    "playlist, or pick a playlist it belongs to.", status_code=404)
            # Same play command SpotAPI uses, but with our authenticated uid.
            p._play_song(p.device_id, p.active_id, tid, pid, uid)
            mode = "played_in_playlist"
        else:
            p.add_to_queue(track_uri)
            p.skip_next()
            mode = "queued_then_skipped"
    return {"playing": True, "mode": mode, "track_uri": track_uri,
            "playlist": _playlist_uri(playlist) if playlist else None,
            "device_id": target_id}


def queue(settings: Settings, track: str) -> dict:
    """Add a track to the end of the playback queue."""
    track_uri = _track_uri(track)
    with _player(settings) as p:
        p.add_to_queue(track_uri)
    return {"queued": True, "track_uri": track_uri}


# action -> what it does on the Player (value-taking ones handled separately)
_SIMPLE_ACTIONS = {
    "pause": lambda p: p.pause(),
    "resume": lambda p: p.resume(),
    "next": lambda p: p.skip_next(),
    "previous": lambda p: p.skip_prev(),
    "restart": lambda p: p.restart_song(),
    "shuffle_on": lambda p: p.set_shuffle(True),
    "shuffle_off": lambda p: p.set_shuffle(False),
    "repeat_on": lambda p: p.repeat_track(True),
    "repeat_off": lambda p: p.repeat_track(False),
}


def control(settings: Settings, action: str, value: float | None = None) -> dict:
    """Playback transport: pause/resume/next/previous/restart, shuffle/repeat
    on/off, plus volume (value=0-100) and seek (value=position in ms)."""
    action = (action or "").strip().lower()
    if action == "volume":
        if value is None or not (0 <= value <= 100):
            raise ServiceError("volume needs value 0-100.", status_code=400)
        with _player(settings) as p:
            p.set_volume(value / 100.0)
        return {"ok": True, "action": "volume", "volume_percent": value}
    if action == "seek":
        if value is None or value < 0:
            raise ServiceError("seek needs value = position in milliseconds.", status_code=400)
        with _player(settings) as p:
            p.seek_to(int(value))
        return {"ok": True, "action": "seek", "position_ms": int(value)}
    fn = _SIMPLE_ACTIONS.get(action)
    if fn is None:
        raise ServiceError(
            f"Unknown action '{action}'. Use one of: "
            + ", ".join(list(_SIMPLE_ACTIONS) + ["volume", "seek"]) + ".",
            status_code=400,
        )
    with _player(settings) as p:
        fn(p)
    return {"ok": True, "action": action}
