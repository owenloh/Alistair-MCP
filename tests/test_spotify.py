"""Tests for the Spotify connector (unofficial API via SpotAPI).

spotapi itself (network, websocket, cookies) is never imported here. Instead we
exercise the real service logic — URI normalisation, the defensive GraphQL
parsers, device name->id resolution, the transport action map, and the play/queue
flows — against tiny fakes injected for the player contexts. The HTTP wiring
(routes in the manifest, 503 when unconfigured) is checked via TestClient.
"""
import contextlib
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("SERVICE_API_KEY", None)
os.environ.pop("SPOTIFY_COOKIES", None)
os.environ.pop("SPOTIFY_USERNAME", None)

from app.config import Settings
from app.services import ServiceError
from app.services import spotify as sp

R = []
def check(name, cond):
    R.append((name, bool(cond)))


# ---- tiny fakes -------------------------------------------------------------
class FakeDevice:
    def __init__(self, device_id, name, volume=32768, dtype="Computer", can_play=True):
        self.device_id = device_id
        self.name = name
        self.volume = volume
        self.device_type = dtype
        self.can_play = can_play

class FakeDevices:
    def __init__(self, devices, active=None):
        self.devices = {d.device_id: d for d in devices}
        self.active_device_id = active

class FakePlayer:
    """Records every transport call so the action map can be asserted."""
    def __init__(self):
        self.calls = []
        self.client = "auth-client"
        self.device_id = "from-dev"
        self.active_id = "to-dev"
    def pause(self): self.calls.append(("pause",))
    def resume(self): self.calls.append(("resume",))
    def skip_next(self): self.calls.append(("skip_next",))
    def skip_prev(self): self.calls.append(("skip_prev",))
    def restart_song(self): self.calls.append(("restart_song",))
    def set_shuffle(self, v): self.calls.append(("set_shuffle", v))
    def repeat_track(self, v): self.calls.append(("repeat_track", v))
    def set_volume(self, v): self.calls.append(("set_volume", v))
    def seek_to(self, ms): self.calls.append(("seek_to", ms))
    def add_to_queue(self, t): self.calls.append(("add_to_queue", t))
    def _play_song(self, f, t, track, playlist, uid):
        self.calls.append(("_play_song", track, playlist, uid))

# Fakes for the authenticated playlist resolution used by play(playlist=...)
class FakePublicPlaylist:
    def __init__(self, pid, client=None):
        self.pid = pid
    def paginate_playlist(self):
        def gen():
            yield {"items": [{"uid": "UID123", "itemV2": {"data": {
                "uri": "spotify:track:t1", "name": "X"}}}]}
        return gen()

class FakeSong:
    @staticmethod
    def parse_playlist_items(items, *, song_id=None, song_name=None, all_instances=False):
        uids = []
        for it in items:
            if song_id and song_id in it["itemV2"]["data"]["uri"]:
                uids.append(it["uid"])
                if all_instances:
                    return uids, True
        return uids, False

FAKE_SP = {"PublicPlaylist": FakePublicPlaylist, "Song": FakeSong}

def cm(obj):
    @contextlib.contextmanager
    def _cm(*a, **k):
        yield obj
    return _cm

S = Settings()  # nothing configured


# === URI normalisation ===
check("track id -> uri", sp._track_uri("4iV5W9uYEdYUVa79Axb7Rh") == "spotify:track:4iV5W9uYEdYUVa79Axb7Rh")
check("track uri passthrough", sp._track_uri("spotify:track:abc") == "spotify:track:abc")
check("track url -> uri", sp._track_uri("https://open.spotify.com/track/xyz?si=1") == "spotify:track:xyz")
check("playlist url -> uri", sp._playlist_uri("https://open.spotify.com/playlist/PL1?si=x") == "spotify:playlist:PL1")
check("playlist uri passthrough", sp._playlist_uri("spotify:playlist:PL2") == "spotify:playlist:PL2")

# === cookie parsing (JSON vs raw string) ===
check("cookies JSON -> dict", sp._parse_cookies('{"sp_dc":"x"}') == {"sp_dc": "x"})
check("cookies raw string passthrough", sp._parse_cookies("sp_dc=x; sp_key=y") == "sp_dc=x; sp_key=y")

# === track cleaning (artists + album extraction) ===
node = {
    "uri": "spotify:track:t1", "name": "Song A",
    "artists": {"items": [{"profile": {"name": "Artist 1"}}, {"profile": {"name": "Artist 2"}}]},
    "albumOfTrack": {"name": "Album X"},
    "duration": {"totalMilliseconds": 1234},
}
ct = sp._clean_track(node)
check("clean_track name", ct["name"] == "Song A")
check("clean_track artists", ct["artists"] == ["Artist 1", "Artist 2"])
check("clean_track album", ct["album"] == "Album X")
check("clean_track duration", ct["duration_ms"] == 1234)

# === playlist extraction: known libraryV3 path ===
lib = {"data": {"me": {"libraryV3": {"items": [
    {"item": {"data": {"uri": "spotify:playlist:P1", "name": "Chill",
                       "ownerV2": {"data": {"name": "Owen"}}, "content": {"totalCount": 12}}}},
    {"item": {"data": {"uri": "spotify:album:A1", "name": "Some Album"}}},  # not a playlist -> skipped
]}}}}
pls = sp._extract_playlists(lib, 50)
check("extract_playlists finds the playlist", len(pls) == 1 and pls[0]["uri"] == "spotify:playlist:P1")
check("extract_playlists owner", pls[0]["owner"] == "Owen")
check("extract_playlists track count", pls[0]["total_tracks"] == 12)
check("extract_playlists skips non-playlist", all(p["uri"].startswith("spotify:playlist:") for p in pls))

# === playlist extraction: fallback recursive scan when shape drifts ===
weird = {"something": {"nested": [{"uri": "spotify:playlist:PX", "name": "Found It"}]}}
pls2 = sp._extract_playlists(weird, 50)
check("extract_playlists fallback scan", len(pls2) == 1 and pls2[0]["uri"] == "spotify:playlist:PX")

# === search extraction ===
items = [{"item": {"data": {"uri": "spotify:track:s1", "name": "Hit",
                            "artists": {"items": [{"profile": {"name": "A"}}]}}}}]
st = sp._extract_search_tracks(items, 10)
check("search extraction", len(st) == 1 and st[0]["uri"] == "spotify:track:s1" and st[0]["artists"] == ["A"])

# === device dict (volume 0-65535 -> percent) ===
dd = sp._device_dict(FakeDevice("d1", "Phone", volume=65535))
check("device volume -> 100%", dd["volume_percent"] == 100)
check("device fields", dd["id"] == "d1" and dd["name"] == "Phone" and dd["type"] == "Computer")

# === device resolution: id, name substring, none, ambiguous ===
devs = FakeDevices([FakeDevice("idA", "Owen's iPhone"), FakeDevice("idB", "Kitchen Speaker"),
                    FakeDevice("idC", "Office Speaker")], active="idA")
check("resolve by exact id", sp._resolve_device(devs, "idB") == "idB")
check("resolve by unique name fragment", sp._resolve_device(devs, "iphone") == "idA")
try:
    sp._resolve_device(devs, "nope"); check("resolve no match -> 404", False)
except ServiceError as e:
    check("resolve no match -> 404", e.status_code == 404)
try:
    sp._resolve_device(devs, "speaker"); check("resolve ambiguous -> 409", False)
except ServiceError as e:
    check("resolve ambiguous -> 409", e.status_code == 409)

# === _no_signal restores signal.signal and swallows off-thread ValueError ===
original = signal.signal
errs = []
def worker():
    with sp._no_signal():
        try:
            signal.signal(signal.SIGINT, signal.SIG_DFL)  # raises ValueError off-main-thread
        except Exception as e:
            errs.append(e)
t = threading.Thread(target=worker); t.start(); t.join()
check("_no_signal swallows off-thread signal error", errs == [])
check("_no_signal restores signal.signal", signal.signal is original)

# === control: action map (drive a fake player) ===
fp = FakePlayer()
sp._player = cm(fp)  # monkeypatch the player context for control/play/queue
sp.control(S, "pause"); check("control pause", fp.calls[-1] == ("pause",))
sp.control(S, "resume"); check("control resume", fp.calls[-1] == ("resume",))
sp.control(S, "next"); check("control next -> skip_next", fp.calls[-1] == ("skip_next",))
sp.control(S, "previous"); check("control previous -> skip_prev", fp.calls[-1] == ("skip_prev",))
sp.control(S, "shuffle_on"); check("control shuffle_on", fp.calls[-1] == ("set_shuffle", True))
sp.control(S, "repeat_off"); check("control repeat_off", fp.calls[-1] == ("repeat_track", False))
sp.control(S, "volume", 50); check("control volume -> 0.5", fp.calls[-1] == ("set_volume", 0.5))
sp.control(S, "seek", 30000); check("control seek ms", fp.calls[-1] == ("seek_to", 30000))

# === control: bad inputs -> clean errors ===
for bad, val, code in [("frobnicate", None, 400), ("volume", None, 400), ("volume", 999, 400), ("seek", -1, 400)]:
    try:
        sp.control(S, bad, val); check(f"control {bad}/{val} -> {code}", False)
    except ServiceError as e:
        check(f"control {bad}/{val} -> {code}", e.status_code == code)

# === play: with playlist -> resolve uid (authenticated) then _play_song ===
fp = FakePlayer(); sp._player = cm(fp); sp._spotapi = lambda: FAKE_SP
sp.play(S, track="t1", playlist="spotify:playlist:P9")
check("play with playlist -> _play_song with resolved uid",
      fp.calls == [("_play_song", "t1", "P9", "UID123")])
# track not in the playlist -> clean 404
fp = FakePlayer(); sp._player = cm(fp); sp._spotapi = lambda: FAKE_SP
try:
    sp.play(S, track="notthere", playlist="spotify:playlist:P9"); check("play not-in-playlist -> 404", False)
except ServiceError as e:
    check("play not-in-playlist -> 404", e.status_code == 404)
# no playlist -> queue then skip
fp = FakePlayer(); sp._player = cm(fp)
sp.play(S, track="spotify:track:t2")
check("play no playlist -> queue then skip", fp.calls == [("add_to_queue", "spotify:track:t2"), ("skip_next",)])

# === queue ===
fp = FakePlayer(); sp._player = cm(fp)
sp.queue(S, "https://open.spotify.com/track/q1")
check("queue normalises + adds", fp.calls == [("add_to_queue", "spotify:track:q1")])

# === devices / now_playing via a fake PlayerStatus context ===
class FakeStatus:
    def __init__(self):
        self.device_ids = FakeDevices([FakeDevice("d1", "Laptop"), FakeDevice("d2", "Phone")], active="d2")
sp._player_status = cm(FakeStatus())
dv = sp.list_devices(S)
check("list_devices count + active", dv["count"] == 2 and dv["active_device_id"] == "d2")
check("list_devices marks active", any(d["active"] and d["id"] == "d2" for d in dv["devices"]))

# === transfer: resolves name then reports the chosen device ===
sp._player_status = cm(FakeStatus())
sp._player = cm(FakePlayer())
tr = sp.transfer_playback(S, "phone")
check("transfer resolves name -> id", tr["transferred"] and tr["device"]["id"] == "d2")

# === graceful 503 when unconfigured (real _login, no monkeypatch) ===
# restore the real contexts so _login runs for real on empty settings
import importlib
sp2 = importlib.reload(sp)
try:
    sp2.list_playlists(Settings()); check("unconfigured -> 503", False)
except ServiceError as e:
    check("unconfigured -> 503", e.status_code == 503 and "not configured" in e.message.lower())

# === HTTP wiring via TestClient: routes in manifest + 503 unconfigured ===
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    mani = c.get("/api/manifest").json()
    check("manifest spotify has 9 tools", mani["counts"].get("spotify") == 9)
    paths = {t["path"] for t in mani["function_apis"]["spotify"]}
    for p in ("/api/spotify/playlists", "/api/spotify/search", "/api/spotify/devices",
              "/api/spotify/play", "/api/spotify/transfer", "/api/spotify/control"):
        check(f"manifest lists {p}", p in paths)
    check("spotify skill discoverable", any(s["slug"] == "spotify" for s in mani["description_apis"]["skills"]))
    r = c.post("/api/spotify/devices", json={})
    check("POST /api/spotify/devices unconfigured -> 503", r.status_code == 503)
    r2 = c.post("/api/spotify/search", json={"query": "x"})
    check("POST /api/spotify/search unconfigured -> 503", r2.status_code == 503)

# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
