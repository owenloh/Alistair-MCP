"""Descriptions for the Spotify connector tools (unofficial API via SpotAPI).

Single source of truth: both the HTTP router and the MCP @mcp.tool wrappers import
these, so every client sees the identical contract. Behaviour-first style, like the
other connector descriptions, with the two big caveats baked in (needs a logged-in
session; playback control needs an active Spotify Connect device).
"""

# --- browse (read-only; work without an active device) ---
LIST_PLAYLISTS = (
    "List Owen's Spotify playlists (his library). Read-only. Returns each playlist's "
    "name, uri, owner and track count — use a returned uri with spotify_playlist_tracks "
    "to see its songs, or as the 'playlist' context for spotify_play."
)

PLAYLIST_TRACKS = (
    "List the tracks in a Spotify playlist (pass its uri/URL/id; works for any playlist "
    "Owen can see). Read-only. Returns each track's name, artists, album and track uri — "
    "pass a track uri to spotify_play to play it, with this playlist as the context."
)

SEARCH = (
    "Search Spotify's catalogue for tracks by name/artist text. Read-only. Returns "
    "matches with name, artists, album and the track uri. This is how you turn 'play "
    "<song>' into a concrete track uri for spotify_play or spotify_queue."
)

# --- devices ---
DEVICES = (
    "Show the Spotify Connect devices available to play on (phone, desktop, web player, "
    "speakers) and which one is currently active. Read-only. Use this to let Owen choose "
    "where to play, then pass a device id or name to spotify_transfer / spotify_play. If "
    "the list is empty, Spotify isn't open on any device yet."
)

TRANSFER = (
    "Move Spotify playback to a specific device — 'play on my <device>'. Pass the device "
    "id (or a unique part of its name) from spotify_devices. Requires an active playback "
    "session to move; if nothing is playing, start something first. Reversible (transfer back)."
)

# --- now playing / control ---
STATUS = (
    "What's playing on Spotify right now: track title/album, the active device, shuffle/"
    "repeat state and what's up next. Read-only. The fast 'what am I listening to' check."
)

PLAY = (
    "Play a specific song on Spotify. Pass the track uri (from spotify_search or "
    "spotify_playlist_tracks). Best results: also pass a 'playlist' uri the track belongs "
    "to, so it plays in that playlist's context; without one it queues the track and skips "
    "to it. Optional 'device' (id or name) sends playback there first. Needs an active "
    "Spotify Connect device — open Spotify somewhere if nothing is playing."
)

QUEUE = (
    "Add a track to the end of the Spotify play queue (without interrupting the current "
    "song). Pass a track uri from spotify_search / spotify_playlist_tracks. Needs an "
    "active device."
)

CONTROL = (
    "Control Spotify playback. action is one of: pause, resume, next, previous, restart, "
    "shuffle_on, shuffle_off, repeat_on, repeat_off, volume, seek. For 'volume' pass "
    "value=0-100 (percent); for 'seek' pass value=position in milliseconds. Needs an "
    "active Spotify Connect device."
)
